"""SQLAlchemy ORM models for the visit database (data/database/visits.sqlite).

Schema changes go through Alembic migrations in backend/migrations; these
models must match the latest migration. Timestamps are UTC epoch seconds
(`time.time()`); audit events also store an ISO-8601 UTC string.

Encryption at rest: columns typed EncryptedText / EncryptedJSON hold clinical content and personal names as
AES-256-GCM ciphertext (backend/crypto.py). They cannot be searched or sorted in SQL.
"""
from typing import Optional
from sqlalchemy import JSON, Boolean, CheckConstraint, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from backend.crypto import EncryptedJSON, EncryptedText

STATES = ("received", "transcribing", "source_review", "extracting", "draft", "approved", "failed")
ROLES = ("admin", "doctor", "patient")


class Base(DeclarativeBase):
    pass


# ---- accounts -------------------------------------------------------------------
class User(Base):
    """`id` is the login username. Roles are assigned only by an admin (or the bootstrap CLI)."""
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    role: Mapped[str] = mapped_column(String(10))
    display: Mapped[str] = mapped_column(EncryptedText)
    password_hash: Mapped[str] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[float] = mapped_column(Float)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[Optional[float]] = mapped_column(Float)
    password_changed_at: Mapped[Optional[float]] = mapped_column(Float)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    # MFA (RFC 6238): secret encrypted at rest; last_step blocks replay of a used code.
    mfa_secret: Mapped[Optional[str]] = mapped_column(EncryptedText)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    mfa_last_step: Mapped[Optional[int]] = mapped_column(Integer)
    # Current Ed25519 signing key; the private key is wrapped with the user's password (backend/crypto.py).
    sign_key_wrapped: Mapped[Optional[str]] = mapped_column(Text)
    __table_args__ = (CheckConstraint("role IN ('admin','doctor','patient')", name="ck_users_role"),)

    def to_dict(self):
        return {"id": self.id, "role": self.role, "display": self.display}


class SigningKey(Base):
    """Every public signing key a user has had, kept forever so old signatures stay verifiable."""
    __tablename__ = "signing_keys"
    fingerprint: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    public_key: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[float] = mapped_column(Float)
    retired_at: Mapped[Optional[float]] = mapped_column(Float)
    retired_reason: Mapped[Optional[str]] = mapped_column(String(40))


class LoginSession(Base):
    """Only the SHA-256 of the session token is stored."""
    __tablename__ = "login_sessions"
    token_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    expires_at: Mapped[float] = mapped_column(Float)
    created_at: Mapped[Optional[float]] = mapped_column(Float)
    last_seen_at: Mapped[Optional[float]] = mapped_column(Float)


class CareAssignment(Base):
    """Which doctor may access which patient's visits. Managed by admins; also the emergency-access mechanism."""
    __tablename__ = "care_assignments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doctor_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    patient_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    reason: Mapped[str] = mapped_column(String(200))
    assigned_by: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"))
    assigned_at: Mapped[float] = mapped_column(Float)
    revoked_at: Mapped[Optional[float]] = mapped_column(Float)
    revoked_by: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"))


# ---- visits and transcripts ----------------------------------------------------------
class Visit(Base):
    __tablename__ = "visits"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    doctor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    patient_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    language: Mapped[Optional[str]] = mapped_column(String(20))
    state: Mapped[str] = mapped_column(String(20))
    error_category: Mapped[Optional[str]] = mapped_column(String(40))
    consent: Mapped[bool] = mapped_column(Boolean)
    input_origin: Mapped[Optional[str]] = mapped_column(String(20))
    created_at: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[float] = mapped_column(Float)
    __table_args__ = (CheckConstraint("state IN ('" + "','".join(STATES) + "')"),)


class TranscriptVersion(Base):
    __tablename__ = "transcript_versions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    visit_id: Mapped[str] = mapped_column(ForeignKey("visits.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    origin: Mapped[str] = mapped_column(String(10))
    parent_id: Mapped[Optional[int]] = mapped_column(ForeignKey("transcript_versions.id"))
    language: Mapped[Optional[str]] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(20))
    roles_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    redaction_reviewed: Mapped[bool] = mapped_column(Boolean, default=False)
    notices: Mapped[list] = mapped_column(EncryptedJSON, default=list)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[float] = mapped_column(Float)
    segments: Mapped[list["TranscriptSegment"]] = relationship(order_by="TranscriptSegment.position", cascade="all, delete-orphan")
    speakers: Mapped[list["SpeakerAssignment"]] = relationship(cascade="all, delete-orphan")
    __table_args__ = (UniqueConstraint("visit_id", "version"),
                      CheckConstraint("origin IN ('auto','edited','manual','redacted')"))

    def to_content(self) -> dict:
        return {"segments": [s.to_dict() for s in self.segments],
                "roles": {a.speaker_label: a.role for a in self.speakers},
                "language": self.language, "notices": list(self.notices or []), "source": self.source,
                "roles_confirmed": self.roles_confirmed, "redaction_reviewed": self.redaction_reviewed}


class TranscriptSegment(Base):
    __tablename__ = "transcript_segments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    transcript_version_id: Mapped[int] = mapped_column(ForeignKey("transcript_versions.id"), index=True)
    segment_key: Mapped[str] = mapped_column(String(20))
    position: Mapped[int] = mapped_column(Integer)
    start: Mapped[float] = mapped_column(Float)
    end: Mapped[float] = mapped_column(Float)
    speaker_label: Mapped[str] = mapped_column(String(40))
    text: Mapped[str] = mapped_column(EncryptedText)
    words: Mapped[list] = mapped_column(EncryptedJSON, default=list)
    flags: Mapped[list] = mapped_column(JSON, default=list)
    __table_args__ = (UniqueConstraint("transcript_version_id", "segment_key"),)

    def to_dict(self):
        return {"id": self.segment_key, "start": self.start, "end": self.end, "speaker": self.speaker_label,
                "text": self.text, "words": list(self.words or []), "flags": list(self.flags or [])}


class SpeakerAssignment(Base):
    """Clinician-confirmed mapping of an anonymous speaker label to a role, per transcript version."""
    __tablename__ = "speaker_assignments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    transcript_version_id: Mapped[int] = mapped_column(ForeignKey("transcript_versions.id"), index=True)
    speaker_label: Mapped[str] = mapped_column(String(40))
    role: Mapped[str] = mapped_column(String(10))
    assigned_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    assigned_at: Mapped[float] = mapped_column(Float)
    __table_args__ = (UniqueConstraint("transcript_version_id", "speaker_label"),
                      CheckConstraint("role IN ('doctor','patient','other','unknown')"))


# ---- summaries, plan, approvals ------------------------------------------------------------
class SummaryVersion(Base):
    """Facts recorded in the visit (clinician) and the patient-facing summary of them. Never contains AI suggestions."""
    __tablename__ = "summary_versions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    visit_id: Mapped[str] = mapped_column(ForeignKey("visits.id"), index=True)
    kind: Mapped[str] = mapped_column(String(10))
    version: Mapped[int] = mapped_column(Integer)
    transcript_version_id: Mapped[int] = mapped_column(ForeignKey("transcript_versions.id"))
    parent_id: Mapped[Optional[int]] = mapped_column(ForeignKey("summary_versions.id"))
    origin: Mapped[str] = mapped_column(String(10))
    content: Mapped[dict] = mapped_column(EncryptedJSON)
    blocking: Mapped[bool] = mapped_column(Boolean)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[float] = mapped_column(Float)
    __table_args__ = (UniqueConstraint("visit_id", "kind", "version"),
                      CheckConstraint("kind IN ('clinician','patient')"))

    def to_dict(self):
        return {c: getattr(self, c) for c in ("id", "visit_id", "kind", "version", "transcript_version_id", "parent_id",
                                              "origin", "blocking", "created_by", "created_at")} | {"content": self.content}


class PatientContextVersion(Base):
    """Doctor-entered clinical context (age, allergies, medications, pregnancy, renal/hepatic status).
    Each field distinguishes 'unknown' from a confirmed value. Versioned; never inferred by a model."""
    __tablename__ = "patient_context_versions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    visit_id: Mapped[str] = mapped_column(ForeignKey("visits.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict] = mapped_column(EncryptedJSON)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[float] = mapped_column(Float)
    __table_args__ = (UniqueConstraint("visit_id", "version"),)


class PlanVersion(Base):
    """Doctor-authored plan written after the visit, separate from what was said in the visit.
    Items may originate from accepted assistant suggestions; their provenance is kept per item."""
    __tablename__ = "plan_versions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    visit_id: Mapped[str] = mapped_column(ForeignKey("visits.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    parent_id: Mapped[Optional[int]] = mapped_column(ForeignKey("plan_versions.id"))
    content: Mapped[dict] = mapped_column(EncryptedJSON)
    reason: Mapped[str] = mapped_column(String(40))
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[float] = mapped_column(Float)
    __table_args__ = (UniqueConstraint("visit_id", "version"),)

    def to_dict(self):
        return {c: getattr(self, c) for c in ("id", "visit_id", "version", "parent_id", "reason", "created_by",
                                              "created_at")} | {"content": self.content}


class Approval(Base):
    """An electronic signature of approval over exact versions. `content_sha256` binds the signature to
    the signed content. Legacy rows (before signatures) have no signer_name/meaning."""
    __tablename__ = "approvals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    visit_id: Mapped[str] = mapped_column(ForeignKey("visits.id"), index=True)
    clinician_version_id: Mapped[int] = mapped_column(ForeignKey("summary_versions.id"))
    patient_version_id: Mapped[int] = mapped_column(ForeignKey("summary_versions.id"))
    plan_version_id: Mapped[Optional[int]] = mapped_column(ForeignKey("plan_versions.id"))
    transcript_version_id: Mapped[Optional[int]] = mapped_column(ForeignKey("transcript_versions.id"))
    content_sha256: Mapped[str] = mapped_column(String(64))
    approved_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[float] = mapped_column(Float)
    signer_name: Mapped[Optional[str]] = mapped_column(String(80))
    meaning: Mapped[Optional[str]] = mapped_column(String(200))
    signature_method: Mapped[Optional[str]] = mapped_column(String(40))
    # Cryptographic signature (Ed25519) over `manifest` (JSON: visit, versions, sha256, signer, meaning, time).
    manifest: Mapped[Optional[str]] = mapped_column(Text)
    signature: Mapped[Optional[str]] = mapped_column(String(128))
    key_fingerprint: Mapped[Optional[str]] = mapped_column(ForeignKey("signing_keys.fingerprint"))
    mfa_verified: Mapped[Optional[bool]] = mapped_column(Boolean)
    time_source: Mapped[Optional[str]] = mapped_column(String(80))
    clock_offset: Mapped[Optional[float]] = mapped_column(Float)
    revoked_at: Mapped[Optional[float]] = mapped_column(Float)
    revoked_reason: Mapped[Optional[str]] = mapped_column(String(40))


# ---- clinical assistant ------------------------------------------------------------------
class AssistantConversation(Base):
    """Bound to exactly one visit and one doctor."""
    __tablename__ = "assistant_conversations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    visit_id: Mapped[str] = mapped_column(ForeignKey("visits.id"), index=True)
    doctor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[float] = mapped_column(Float)


class AssistantMessage(Base):
    """A doctor question or a validated assistant response (JSON). Clinical content: never copied to audit."""
    __tablename__ = "assistant_messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("assistant_conversations.id"), index=True)
    visit_id: Mapped[str] = mapped_column(ForeignKey("visits.id"), index=True)
    author: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(20))
    content: Mapped[dict] = mapped_column(EncryptedJSON)
    context_version: Mapped[Optional[str]] = mapped_column(String(64))
    model: Mapped[Optional[str]] = mapped_column(String(80))
    created_at: Mapped[float] = mapped_column(Float)
    __table_args__ = (CheckConstraint("author IN ('doctor','assistant')"),)


class AssistantSuggestion(Base):
    """One validated suggestion from an assistant response, with the doctor's decision."""
    __tablename__ = "assistant_suggestions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("assistant_messages.id"), index=True)
    visit_id: Mapped[str] = mapped_column(ForeignKey("visits.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    text: Mapped[str] = mapped_column(EncryptedText)
    rationale: Mapped[str] = mapped_column(EncryptedText)
    citations: Mapped[list] = mapped_column(EncryptedJSON, default=list)
    medication: Mapped[Optional[dict]] = mapped_column(EncryptedJSON)
    safety: Mapped[Optional[dict]] = mapped_column(EncryptedJSON)
    context_version: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    final_text: Mapped[Optional[str]] = mapped_column(EncryptedText)
    decided_by: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[Optional[float]] = mapped_column(Float)
    __table_args__ = (CheckConstraint("status IN ('pending','accepted','edited','rejected')"),)


# ---- audit and jobs ------------------------------------------------------------------------
class AuditEvent(Base):
    """Append-only (database triggers reject UPDATE/DELETE; the application has no update path).
    Hash-chained for tamper evidence. NOT tamper-proof: anyone with write access to the file can
    drop the triggers and rewrite the chain (unless AUDIT_HMAC_KEY is kept outside their reach)."""
    __tablename__ = "audit_events"
    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(36), unique=True)
    ts_utc: Mapped[str] = mapped_column(String(32), index=True)
    actor_id: Mapped[Optional[str]] = mapped_column(String(40), index=True)
    actor_role: Mapped[Optional[str]] = mapped_column(String(10))
    action: Mapped[str] = mapped_column(String(60), index=True)
    record_type: Mapped[Optional[str]] = mapped_column(String(40))
    record_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    record_version: Mapped[Optional[str]] = mapped_column(String(32))
    outcome: Mapped[str] = mapped_column(String(20))
    correlation_id: Mapped[Optional[str]] = mapped_column(String(64))
    session_ref: Mapped[Optional[str]] = mapped_column(String(16))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))
    __table_args__ = (CheckConstraint("outcome IN ('success','denied','failure')"),)

    def to_dict(self):
        return {c: getattr(self, c) for c in ("seq", "event_id", "ts_utc", "actor_id", "actor_role", "action",
                                              "record_type", "record_id", "record_version", "outcome",
                                              "correlation_id", "session_ref", "details", "prev_hash", "hash")}


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    visit_id: Mapped[str] = mapped_column(ForeignKey("visits.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error_category: Mapped[Optional[str]] = mapped_column(String(40))
    created_at: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[float] = mapped_column(Float)

    def to_dict(self):
        return {c: getattr(self, c) for c in ("id", "visit_id", "kind", "status", "attempts", "error_category", "created_at", "updated_at")}


# ---- pseudonym mappings (separate database file) ----------------------------------------------
class MappingBase(DeclarativeBase):
    pass


class PseudonymMapping(MappingBase):
    __tablename__ = "mappings"
    visit_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    placeholder: Mapped[str] = mapped_column(String(40), primary_key=True)
    original: Mapped[str] = mapped_column(EncryptedText)
    created_at: Mapped[float] = mapped_column(Float)
