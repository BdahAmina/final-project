"""Append-only, hash-chained audit trail.

Every event records: event id, UTC timestamp, actor id and role, action, record type/id/version,
outcome, correlation id and a short session reference (a prefix of the session-token hash, never
the token). `details` carries only small non-clinical values; keys that could carry secrets or
clinical text are rejected.

Integrity: each event's hash covers the previous event's hash and its own fields. With
AUDIT_HMAC_KEY set (environment variable, kept outside the database) the hash is an HMAC, so
rewriting the chain also requires the key. Without it the chain is plain SHA-256 and only detects
edits by someone who does not recompute the chain. Database triggers reject UPDATE and DELETE.
None of this makes an ordinary SQLite file tamper-proof: anyone who can write the file can drop
the triggers; keep the database on access-controlled storage and back it up.
"""
import csv
import hashlib
import hmac
import io
import json
import os
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from sqlalchemy import func, select
from backend.models import AuditEvent

correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")
session_ref: ContextVar[str] = ContextVar("session_ref", default="")
FORBIDDEN_KEYS = {"password", "token", "text", "transcript", "prompt", "content", "question", "answer", "audio",
                  "quote", "statement", "summary", "name", "display", "original"}
SYSTEM = {"id": "system", "role": "system"}
GENESIS = "0" * 64


def new_correlation_id() -> str:
    value = uuid.uuid4().hex
    correlation_id.set(value)
    return value


def utc_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _key():
    raw = os.getenv("AUDIT_HMAC_KEY", "").strip()
    return raw.encode() if raw else None


def _safe_details(details: dict) -> dict:
    out = {}
    for k, v in (details or {}).items():
        if k.lower() in FORBIDDEN_KEYS:
            raise ValueError(f"audit details may not contain '{k}'")
        if isinstance(v, str):
            v = v[:120]
        elif isinstance(v, (list, tuple)):
            v = [x if isinstance(x, (int, float, bool)) or x is None else str(x)[:60] for x in list(v)[:20]]
        elif not isinstance(v, (int, float, bool)) and v is not None:
            v = str(v)[:120]
        out[k] = v
    return out


def _digest(prev: str, fields: dict, alg: str) -> str:
    payload = (prev + json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))).encode()
    if alg == "hmac-sha256":
        key = _key()
        if key is None:
            raise RuntimeError("AUDIT_HMAC_KEY is required to verify keyed audit events")
        return hmac.new(key, payload, hashlib.sha256).hexdigest()
    return hashlib.sha256(payload).hexdigest()


def _hashed_fields(e) -> dict:
    return {k: getattr(e, k) for k in ("event_id", "ts_utc", "actor_id", "actor_role", "action", "record_type",
                                       "record_id", "record_version", "outcome", "correlation_id", "session_ref", "details")}


def record(s, actor, action, outcome="success", record_type=None, record_id=None, record_version=None, details=None):
    """Append one event inside the caller's write transaction (so it commits or rolls back with the change)."""
    actor = actor or SYSTEM
    details = _safe_details(details)
    details["_alg"] = "hmac-sha256" if _key() else "sha256"
    e = AuditEvent(event_id=str(uuid.uuid4()), ts_utc=utc_iso(time.time()), actor_id=actor.get("id"),
                   actor_role=actor.get("role"), action=action, record_type=record_type,
                   record_id=None if record_id is None else str(record_id),
                   record_version=None if record_version is None else str(record_version), outcome=outcome,
                   correlation_id=correlation_id.get() or None, session_ref=session_ref.get() or None, details=details)
    e.prev_hash = s.scalar(select(AuditEvent.hash).order_by(AuditEvent.seq.desc()).limit(1)) or GENESIS
    e.hash = _digest(e.prev_hash, _hashed_fields(e), details["_alg"])
    s.add(e)
    s.flush()
    return e.event_id


def seen(s, actor, action, record_type, record_id, record_version) -> bool:
    """True if this session already logged this access (keeps Streamlit reruns from flooding the trail)."""
    ref = session_ref.get()
    if not ref:
        return False
    return s.scalar(select(func.count()).select_from(AuditEvent).where(
        AuditEvent.session_ref == ref, AuditEvent.actor_id == actor["id"], AuditEvent.action == action,
        AuditEvent.record_type == record_type, AuditEvent.record_id == str(record_id),
        AuditEvent.record_version == (None if record_version is None else str(record_version)))) > 0


def verify(s) -> dict:
    """Recompute the chain. Detects edits, deletions and reordering made without recomputing hashes."""
    prev, checked = GENESIS, 0
    for e in s.scalars(select(AuditEvent).order_by(AuditEvent.seq)):
        alg = (e.details or {}).get("_alg", "sha256")
        try:
            expected = _digest(prev, _hashed_fields(e), alg)
        except RuntimeError as exc:
            return {"ok": False, "checked": checked, "first_bad_seq": e.seq, "problem": str(exc)}
        if e.prev_hash != prev or e.hash != expected:
            return {"ok": False, "checked": checked, "first_bad_seq": e.seq, "problem": "hash mismatch"}
        prev, checked = e.hash, checked + 1
    return {"ok": True, "checked": checked, "first_bad_seq": None, "keyed": _key() is not None,
            "problem": "" if _key() else "AUDIT_HMAC_KEY not set: chain is unkeyed SHA-256"}


def query(s, actor_id=None, action=None, record_id=None, outcome=None, since=None, until=None, page=1, page_size=50):
    q = select(AuditEvent)
    if actor_id:
        q = q.where(AuditEvent.actor_id == actor_id)
    if action:
        q = q.where(AuditEvent.action == action)
    if record_id:
        q = q.where(AuditEvent.record_id == str(record_id))
    if outcome:
        q = q.where(AuditEvent.outcome == outcome)
    if since:
        q = q.where(AuditEvent.ts_utc >= since)
    if until:
        q = q.where(AuditEvent.ts_utc <= until)
    total = s.scalar(select(func.count()).select_from(q.subquery()))
    page_size = max(1, min(int(page_size), 500))
    rows = s.scalars(q.order_by(AuditEvent.seq.desc()).offset((max(1, int(page)) - 1) * page_size).limit(page_size))
    return {"total": total, "page": page, "page_size": page_size, "events": [e.to_dict() for e in rows]}


def to_csv(events: list[dict]) -> str:
    buf = io.StringIO()
    cols = ["seq", "event_id", "ts_utc", "actor_id", "actor_role", "action", "record_type", "record_id",
            "record_version", "outcome", "correlation_id", "session_ref", "details", "prev_hash", "hash"]
    w = csv.DictWriter(buf, fieldnames=cols)
    w.writeheader()
    for e in events:
        w.writerow({**e, "details": json.dumps(e["details"], ensure_ascii=False, sort_keys=True)})
    return buf.getvalue()
