"""Data access with authorization and auditing (SQLite through SQLAlchemy, see backend/db.py).

Authorization is enforced here, on every lookup — the UI and API only translate errors:
- admin:   accounts, roles, care assignments and the audit trail. No clinical content.
- doctor:  visits where they are the visit doctor AND have an active care assignment to the patient.
- patient: only their own visits, and only approved content.
Unauthorized access raises NotFound (so ids of other users' records are not confirmed) and is
written to the audit trail as a denied event.

Versioning: transcripts, summaries, patient context and plans are append-only versions. Any new
version that affects approved content revokes the visit's approval in the same transaction and
records an `approval_invalidated` audit event. Approval is an electronic signature (re-entered
username + password, printed name, UTC time, meaning) bound to a SHA-256 of the exact versions.

Temporary audio is never stored here (see backend/retention.py). Pseudonym mappings live in a
separate database file (PseudonymStore).
"""
import hashlib
import json
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session, sessionmaker
from backend import audit, db, security
from backend.models import (STATES, Approval, AssistantConversation, AssistantMessage, AssistantSuggestion, AuditEvent,
                            Base, CareAssignment, Job, LoginSession, MappingBase, PatientContextVersion, PlanVersion,
                            PseudonymMapping, SpeakerAssignment, SummaryVersion, TranscriptSegment, TranscriptVersion,
                            User, Visit)

RUNTIME = db.RUNTIME
SIGNATURE_MEANING = "אישור: בדקתי ואני מאשר/ת את הסיכומים והתוכנית כמשקפים את הביקור (Reviewed and approved)"
TRANSCRIPT_ACTIONS = {"auto": "transcription_saved", "manual": "transcript_manual_saved", "edited": "transcript_edited",
                      "redacted": "transcript_redacted"}
__all__ = ["Repository", "PseudonymStore", "NotFound", "Conflict", "content_hash", "approval_hash", "Base", "User",
           "LoginSession", "SummaryVersion", "TranscriptSegment", "SpeakerAssignment", "AuditEvent", "STATES", "RUNTIME"]


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


def content_hash(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def approval_hash(clinician: dict, patient: dict, transcript_version_id, plan: dict = None) -> str:
    """Hash signed by an approval. Approvals without a plan keep the original 3-part form."""
    if plan is None:
        return content_hash(clinician, patient, transcript_version_id)
    return content_hash(clinician, patient, transcript_version_id, plan)


def _now():
    return time.time()


class Repository:
    def __init__(self, path=None):
        self.path = Path(path) if path else db.DB_PATH
        self.applied_migrations = db.migrate(self.path)
        self._read = sessionmaker(db.engine(self.path, immediate=False), expire_on_commit=False)
        self._write = sessionmaker(db.engine(self.path, immediate=True), expire_on_commit=False)

    def dispose(self):
        self._read.kw["bind"].dispose()
        self._write.kw["bind"].dispose()

    @contextmanager
    def _tx(self, write=True):
        with (self._write if write else self._read)() as session, session.begin():
            yield session

    def _event(self, actor, action, outcome="success", record_type=None, record_id=None, record_version=None, **details):
        """Audit event in its own transaction (for denials and failures whose main transaction rolled back)."""
        with self._tx() as s:
            audit.record(s, actor, action, outcome, record_type, record_id, record_version, details)

    # ---- accounts ------------------------------------------------------------------
    def has_users(self) -> bool:
        with self._tx(False) as s:
            return s.scalar(select(func.count()).select_from(User)) > 0

    def has_admin(self) -> bool:
        with self._tx(False) as s:
            return s.scalar(select(func.count()).select_from(User).where(User.role == "admin", User.active.is_(True))) > 0

    @staticmethod
    def _check_new_user(username, role, display, password):
        username = (username or "").strip().lower()
        if problem := security.username_problem(username):
            raise ValueError(problem)
        if role not in ("admin", "doctor", "patient"):
            raise ValueError("תפקיד לא תקין.")
        if not (display or "").strip():
            raise ValueError("נדרש שם מלא.")
        if problem := security.password_problem(password, username):
            raise ValueError(problem)
        return username

    def bootstrap_admin(self, username, display, password):
        """First administrator, from the command line only (backend/manage.py). Refused once an admin exists."""
        username = self._check_new_user(username, "admin", display, password)
        with self._tx() as s:
            if s.scalar(select(func.count()).select_from(User).where(User.role == "admin")):
                raise Conflict("כבר קיים חשבון מנהל/ת. משתמשים נוספים נוצרים מתוך המערכת.")
            if s.get(User, username):
                raise Conflict("שם המשתמש כבר קיים.")
            now = _now()
            s.add(User(id=username, role="admin", display=display.strip(), password_hash=security.hash_password(password),
                       active=True, created_by=None, created_at=now, password_changed_at=now, must_change_password=False))
            audit.record(s, audit.SYSTEM, "user_created", record_type="user", record_id=username, details={"role": "admin", "via": "cli"})
        return username

    def _admin(self, actor, action):
        if not actor or actor.get("role") != "admin":
            self._event(actor, action, "denied")
            raise NotFound("פעולה זמינה למנהל/ת מערכת בלבד")

    def create_user(self, actor, username, role, display, password):
        """Admin only. The new user must change the initial password at first login."""
        self._admin(actor, "user_created")
        username = self._check_new_user(username, role, display, password)
        with self._tx() as s:
            if s.get(User, username):
                raise Conflict("שם המשתמש כבר קיים.")
            s.add(User(id=username, role=role, display=display.strip(), password_hash=security.hash_password(password),
                       active=True, created_by=actor["id"], created_at=_now(), password_changed_at=_now(),
                       must_change_password=True))
            audit.record(s, actor, "user_created", record_type="user", record_id=username, details={"role": role})
        return username

    def create_patient(self, actor, username, display, password):
        """Doctor only: a new patient account, assigned to this doctor in the same transaction. The patient must change
        the initial password at first login. Admins still manage roles, deactivation and other assignments."""
        if not actor or actor.get("role") != "doctor":
            self._event(actor, "user_created", "denied")
            raise NotFound("פעולה זמינה לרופא/ה בלבד")
        username = self._check_new_user(username, "patient", display, password)
        with self._tx() as s:
            if s.get(User, username):
                raise Conflict("שם המשתמש כבר קיים.")
            now = _now()
            s.add(User(id=username, role="patient", display=display.strip(), password_hash=security.hash_password(password),
                       active=True, created_by=actor["id"], created_at=now, password_changed_at=now, must_change_password=True))
            s.flush()  # the user row must exist before the assignment that references it
            a = CareAssignment(doctor_id=actor["id"], patient_id=username, reason="מטופל/ת שנוצר/ה על ידי הרופא/ה",
                               assigned_by=actor["id"], assigned_at=now)
            s.add(a)
            s.flush()
            audit.record(s, actor, "user_created", record_type="user", record_id=username, details={"role": "patient", "via": "doctor"})
            audit.record(s, actor, "care_assigned", record_type="care_assignment", record_id=a.id,
                         details={"doctor": actor["id"], "patient": username})
        return username

    def set_role(self, actor, user_id, role):
        self._admin(actor, "user_role_changed")
        if role not in ("admin", "doctor", "patient"):
            raise ValueError("תפקיד לא תקין.")
        with self._tx() as s:
            u = s.get(User, user_id)
            if not u:
                raise NotFound("משתמש לא קיים")
            if u.role == "admin" and role != "admin" and self._active_admins(s) <= 1:
                raise Conflict("לא ניתן להסיר את מנהל/ת המערכת האחרון/ה.")
            old, u.role = u.role, role
            s.execute(delete(LoginSession).where(LoginSession.user_id == u.id))
            audit.record(s, actor, "user_role_changed", record_type="user", record_id=u.id, details={"from": old, "to": role})

    def set_active(self, actor, user_id, active):
        self._admin(actor, "user_activation_changed")
        with self._tx() as s:
            u = s.get(User, user_id)
            if not u:
                raise NotFound("משתמש לא קיים")
            if u.role == "admin" and not active and self._active_admins(s) <= 1:
                raise Conflict("לא ניתן להשבית את מנהל/ת המערכת האחרון/ה.")
            u.active = bool(active)
            if not active:
                s.execute(delete(LoginSession).where(LoginSession.user_id == u.id))
            audit.record(s, actor, "user_activated" if active else "user_deactivated", record_type="user", record_id=u.id)

    def reset_password(self, actor, user_id, new_password):
        """Admin reset (lost password): ends sessions, unlocks, and forces a change at next login."""
        self._admin(actor, "password_reset")
        with self._tx() as s:
            u = s.get(User, user_id)
            if not u:
                raise NotFound("משתמש לא קיים")
            if problem := security.password_problem(new_password, u.id):
                raise ValueError(problem)
            u.password_hash, u.must_change_password = security.hash_password(new_password), True
            u.failed_logins, u.locked_until, u.password_changed_at = 0, None, _now()
            s.execute(delete(LoginSession).where(LoginSession.user_id == u.id))
            audit.record(s, actor, "password_reset", record_type="user", record_id=u.id)

    @staticmethod
    def _active_admins(s):
        return s.scalar(select(func.count()).select_from(User).where(User.role == "admin", User.active.is_(True)))

    def all_users(self, actor):
        self._admin(actor, "users_listed")
        with self._tx(False) as s:
            return [u.to_dict() | {"active": u.active, "locked": bool(u.locked_until and u.locked_until > _now()),
                                   "failed_logins": u.failed_logins, "must_change_password": u.must_change_password,
                                   "created_at": u.created_at} for u in s.scalars(select(User).order_by(User.role, User.id))]

    # ---- login and sessions ------------------------------------------------------------
    def authenticate(self, username, password):
        """User dict (with must_change_password) for valid credentials, else None. Counts failures and
        locks the account after MAX_FAILED_LOGINS; every attempt is audited without the password."""
        username = (username or "").strip().lower()
        with self._tx() as s:
            u = s.get(User, username)
            ok = security.check_password(password or "", u.password_hash if u and u.active else security.DUMMY_HASH)
            if u and u.locked_until and u.locked_until > _now():
                audit.record(s, {"id": username, "role": u.role}, "login", "denied", "user", username, details={"reason": "locked"})
                return None
            if not u or not u.active or not ok:
                details = {"reason": "unknown_user" if not u else "inactive" if not u.active else "bad_password"}
                if u and u.active:
                    u.failed_logins = (u.failed_logins or 0) + 1
                    if u.failed_logins >= security.MAX_FAILED_LOGINS:
                        u.locked_until = _now() + security.LOCKOUT_MINUTES * 60
                        details["locked"] = True
                audit.record(s, {"id": username, "role": u.role if u else None}, "login", "failure", "user", username, details=details)
                if details.get("locked"):
                    audit.record(s, audit.SYSTEM, "account_locked", "success", "user", username,
                                 details={"failed_logins": u.failed_logins, "minutes": security.LOCKOUT_MINUTES})
                return None
            u.failed_logins, u.locked_until = 0, None
            expired = bool(u.password_changed_at and _now() - u.password_changed_at > security.PASSWORD_MAX_AGE_DAYS * 86400)
            audit.record(s, u.to_dict(), "login", "success", "user", u.id, details={"password_expired": expired})
            return u.to_dict() | {"must_change_password": bool(u.must_change_password or expired)}

    def create_session(self, user) -> str:
        token, digest = security.new_token()
        now = _now()
        with self._tx() as s:
            s.execute(delete(LoginSession).where(LoginSession.expires_at < now))
            s.add(LoginSession(token_sha256=digest, user_id=user["id"], created_at=now, last_seen_at=now,
                               expires_at=now + security.SESSION_MAX_HOURS * 3600))
        return token

    def session_user(self, token):
        """The user for a live session, refreshing its idle timer; None if expired, idle too long or unknown."""
        digest = security.token_hash(token)
        now = _now()
        with self._tx() as s:
            row = s.get(LoginSession, digest)
            if not row:
                return None
            idle = row.last_seen_at and now - row.last_seen_at > security.SESSION_IDLE_MINUTES * 60
            u = s.get(User, row.user_id)
            if row.expires_at <= now or idle or not u or not u.active:
                s.delete(row)
                audit.record(s, u.to_dict() if u else None, "session_expired", record_type="user", record_id=row.user_id,
                             details={"reason": "idle" if idle else "expired" if row.expires_at <= now else "inactive"})
                return None
            row.last_seen_at = now
            audit.session_ref.set(digest[:16])
            return u.to_dict() | {"must_change_password": bool(u.must_change_password)}

    def end_session(self, token, user=None):
        with self._tx() as s:
            n = s.execute(delete(LoginSession).where(LoginSession.token_sha256 == security.token_hash(token))).rowcount
            if n:
                audit.record(s, user, "logout", record_type="user", record_id=user["id"] if user else None)

    def change_password(self, user, old, new):
        """Also ends every login session of the user (callers create a fresh one)."""
        with self._tx() as s:
            u = s.get(User, user["id"])
            if not u or not security.check_password(old or "", u.password_hash):
                audit.record(s, user, "password_changed", "failure", "user", user["id"], details={"reason": "bad_password"})
                raise ValueError("הסיסמה הנוכחית שגויה.")
            if problem := security.password_problem(new, u.id):
                raise ValueError(problem)
            if security.check_password(new, u.password_hash):
                raise ValueError("הסיסמה החדשה חייבת להיות שונה מהקודמת.")
            u.password_hash, u.must_change_password, u.password_changed_at = security.hash_password(new), False, _now()
            s.execute(delete(LoginSession).where(LoginSession.user_id == u.id))
            audit.record(s, user, "password_changed", record_type="user", record_id=u.id)

    def user(self, user_id):
        with self._tx(False) as s:
            u = s.get(User, user_id)
            if not u or not u.active:
                raise NotFound("משתמש לא קיים")
            return u.to_dict()

    # ---- care assignments (admin) ----------------------------------------------------------
    def assign(self, actor, doctor_id, patient_id, reason):
        """Grant a doctor access to a patient's visits. Also the documented emergency-access path
        (reason required, audited, revocable)."""
        self._admin(actor, "care_assigned")
        if not (reason or "").strip():
            raise ValueError("נדרשת סיבה לשיוך.")
        with self._tx() as s:
            d, p = s.get(User, doctor_id), s.get(User, patient_id)
            if not d or d.role != "doctor" or not p or p.role != "patient":
                raise ValueError("יש לבחור רופא/ה ומטופל/ת קיימים.")
            if self._assignment(s, doctor_id, patient_id):
                raise Conflict("השיוך כבר קיים.")
            a = CareAssignment(doctor_id=doctor_id, patient_id=patient_id, reason=reason.strip()[:200],
                               assigned_by=actor["id"], assigned_at=_now())
            s.add(a)
            s.flush()
            audit.record(s, actor, "care_assigned", record_type="care_assignment", record_id=a.id,
                         details={"doctor": doctor_id, "patient": patient_id})
            return a.id

    def revoke_assignment(self, actor, assignment_id):
        self._admin(actor, "care_revoked")
        with self._tx() as s:
            a = s.get(CareAssignment, assignment_id)
            if not a or a.revoked_at:
                raise NotFound("השיוך לא נמצא")
            a.revoked_at, a.revoked_by = _now(), actor["id"]
            audit.record(s, actor, "care_revoked", record_type="care_assignment", record_id=a.id,
                         details={"doctor": a.doctor_id, "patient": a.patient_id})

    def assignments(self, actor):
        self._admin(actor, "care_listed")
        with self._tx(False) as s:
            return [{c: getattr(a, c) for c in ("id", "doctor_id", "patient_id", "reason", "assigned_by", "assigned_at",
                                                "revoked_at")} for a in s.scalars(select(CareAssignment).order_by(CareAssignment.id.desc()))]

    @staticmethod
    def _assignment(s, doctor_id, patient_id):
        return s.scalar(select(CareAssignment).where(CareAssignment.doctor_id == doctor_id, CareAssignment.patient_id == patient_id,
                                                     CareAssignment.revoked_at.is_(None)).limit(1))

    def users(self, actor, role=None):
        """Directory for clinical screens: a doctor sees only their assigned patients; others see nobody."""
        if not actor or actor.get("role") != "doctor" or role not in (None, "patient"):
            return []
        with self._tx(False) as s:
            q = (select(User).join(CareAssignment, CareAssignment.patient_id == User.id)
                 .where(CareAssignment.doctor_id == actor["id"], CareAssignment.revoked_at.is_(None), User.active.is_(True))
                 .order_by(User.display).distinct())
            return [u.to_dict() for u in s.scalars(q)]

    # ---- access ----------------------------------------------------------------------------------
    def _visit(self, s: Session, user, visit_id, doctor_only=True) -> Visit:
        v = s.get(Visit, visit_id)
        role = (user or {}).get("role")
        if v and role == "doctor" and v.doctor_id == user["id"] and self._assignment(s, user["id"], v.patient_id):
            return v
        if v and not doctor_only and role == "patient" and v.patient_id == user["id"]:
            return v
        raise NotFound("הביקור לא נמצא")

    @contextmanager
    def _clinical(self, user, visit_id, action, write=False, doctor_only=True):
        """Transaction plus authorization for one visit; denials are audited after the rollback."""
        try:
            with self._tx(write) as s:
                yield s, self._visit(s, user, visit_id, doctor_only)
        except NotFound:
            self._event(user, action, "denied", "visit", visit_id)
            raise

    def require_doctor(self, user, visit_id, action):
        """Raise NotFound (and audit the denial) unless `user` is the authorized doctor of the visit."""
        with self._clinical(user, visit_id, action):
            pass

    def _accessed(self, user, action, record_type, record_id, record_version=None):
        """Clinical read event, once per login session per record version."""
        with self._tx() as s:
            if not audit.seen(s, user, action, record_type, record_id, record_version):
                audit.record(s, user, action, record_type=record_type, record_id=record_id, record_version=record_version)

    @staticmethod
    def _visit_dict(v: Visit):
        return {c: getattr(v, c) for c in ("id", "doctor_id", "patient_id", "language", "state", "error_category",
                                           "consent", "input_origin", "created_at", "updated_at")}

    @staticmethod
    def _revoke(s: Session, user, visit_id, reason, only_with_plan=False):
        q = select(Approval).where(Approval.visit_id == visit_id, Approval.revoked_at.is_(None))
        if only_with_plan:
            q = q.where(Approval.plan_version_id.is_not(None))
        for a in s.scalars(q):
            a.revoked_at, a.revoked_reason = _now(), reason
            audit.record(s, user, "approval_invalidated", record_type="approval", record_id=a.id, details={"reason": reason})

    # ---- visits ------------------------------------------------------------------------------------
    def create_visit(self, user, patient_id, language, consent):
        if (user or {}).get("role") != "doctor":
            self._event(user, "visit_created", "denied", "visit", None)
            raise NotFound("פעולה זמינה לרופא בלבד")
        if not consent:
            raise ValueError("נדרשת הסכמת המטופל/ת להקלטה ולעיבוד השיחה.")
        with self._tx() as s:
            patient = s.get(User, patient_id)
            if not patient or patient.role != "patient" or not self._assignment(s, user["id"], patient_id):
                raise ValueError("המטופל/ת אינו/ה משויך/ת אליך. פנו למנהל/ת המערכת.")
            now = _now()
            v = Visit(id=uuid.uuid4().hex[:12], doctor_id=user["id"], patient_id=patient_id, language=language,
                      state="received", consent=True, created_at=now, updated_at=now)
            s.add(v)
            s.flush()
            audit.record(s, user, "visit_created", record_type="visit", record_id=v.id, details={"consent": True})
            return v.id

    def visit(self, user, visit_id):
        with self._clinical(user, visit_id, "visit_viewed", doctor_only=False) as (s, v):
            result = self._visit_dict(v)
        self._accessed(user, "visit_viewed", "visit", visit_id)
        return result

    def visits(self, user):
        role = (user or {}).get("role")
        if role not in ("doctor", "patient"):
            return []
        with self._tx(False) as s:
            q = select(Visit).order_by(Visit.created_at.desc())
            if role == "doctor":
                q = q.join(CareAssignment, (CareAssignment.patient_id == Visit.patient_id) & (CareAssignment.doctor_id == Visit.doctor_id)) \
                    .where(Visit.doctor_id == user["id"], CareAssignment.revoked_at.is_(None)).distinct()
            else:
                q = q.where(Visit.patient_id == user["id"])
            return [self._visit_dict(v) for v in s.scalars(q)]

    def set_state(self, user, visit_id, state, error_category=None, input_origin=None):
        if state not in STATES:
            raise ValueError(state)
        with self._clinical(user, visit_id, "visit_state_changed", write=True) as (s, v):
            old = v.state
            v.state, v.error_category, v.updated_at = state, error_category, _now()
            if input_origin:
                v.input_origin = input_origin
            if state == "failed" or old != state:
                audit.record(s, user, "transcription_failed" if state == "failed" and error_category in ("input", "transcription")
                             else "visit_state_changed", "failure" if state == "failed" else "success", "visit", visit_id,
                             details={"from": old, "to": state, "error_category": error_category})

    # ---- transcripts ---------------------------------------------------------------------------------
    def add_transcript(self, user, visit_id, content: dict, origin, parent_id=None):
        """Store a new transcript version with segment and speaker rows. Revokes any approval."""
        with self._clinical(user, visit_id, TRANSCRIPT_ACTIONS.get(origin, "transcript_edited"), write=True) as (s, v):
            now = _now()
            version = (s.scalar(select(func.max(TranscriptVersion.version)).where(TranscriptVersion.visit_id == visit_id)) or 0) + 1
            t = TranscriptVersion(visit_id=visit_id, version=version, origin=origin, parent_id=parent_id,
                                  language=content.get("language"), source=content.get("source", "microphone"),
                                  roles_confirmed=bool(content.get("roles_confirmed")),
                                  redaction_reviewed=bool(content.get("redaction_reviewed")),
                                  notices=list(content.get("notices", [])), created_by=user["id"], created_at=now)
            t.segments = [TranscriptSegment(segment_key=seg["id"], position=n, start=seg.get("start", 0.0), end=seg.get("end", 0.0),
                                            speaker_label=seg.get("speaker", "UNKNOWN"), text=seg["text"],
                                            words=seg.get("words", []), flags=seg.get("flags", []))
                          for n, seg in enumerate(content["segments"])]
            t.speakers = [SpeakerAssignment(speaker_label=label, role=role, assigned_by=user["id"], assigned_at=now)
                          for label, role in content.get("roles", {}).items()]
            s.add(t)
            s.flush()
            audit.record(s, user, TRANSCRIPT_ACTIONS.get(origin, "transcript_edited"), record_type="transcript",
                         record_id=t.id, record_version=version,
                         details={"visit": visit_id, "segments": len(t.segments), "roles_confirmed": t.roles_confirmed,
                                  "redaction_reviewed": t.redaction_reviewed, "parent": parent_id})
            self._revoke(s, user, visit_id, "transcript_changed")
            v.state, v.error_category, v.updated_at = "source_review", None, now
            return t.id

    def transcripts(self, user, visit_id):
        with self._clinical(user, visit_id, "transcript_viewed") as (s, v):
            rows = s.scalars(select(TranscriptVersion).where(TranscriptVersion.visit_id == visit_id).order_by(TranscriptVersion.version))
            result = [{"id": t.id, "visit_id": t.visit_id, "version": t.version, "origin": t.origin, "parent_id": t.parent_id,
                       "created_by": t.created_by, "created_at": t.created_at, "content": t.to_content()} for t in rows]
        if result:
            self._accessed(user, "transcript_viewed", "transcript", result[-1]["id"], result[-1]["version"])
        return result

    def latest_transcript(self, user, visit_id, origin=None):
        rows = [t for t in self.transcripts(user, visit_id) if origin is None or t["origin"] == origin]
        return rows[-1] if rows else None

    # ---- summaries (recorded visit facts) --------------------------------------------------------------
    @staticmethod
    def _latest_summary(s, visit_id, kind):
        return s.scalar(select(SummaryVersion).where(SummaryVersion.visit_id == visit_id, SummaryVersion.kind == kind)
                        .order_by(SummaryVersion.version.desc()).limit(1))

    @staticmethod
    def _latest_transcript_id(s, visit_id):
        return s.scalar(select(func.max(TranscriptVersion.id)).where(TranscriptVersion.visit_id == visit_id))

    def add_summary(self, user, visit_id, kind, content: dict, transcript_version_id, blocking, origin, parent_id=None):
        """Store a new summary version. Revokes any approval of the visit."""
        action = "summary_generated" if origin == "model" else "summary_edited"
        with self._clinical(user, visit_id, action, write=True) as (s, v):
            if transcript_version_id != self._latest_transcript_id(s, visit_id):
                raise Conflict("התמלול השתנה מאז שהטיוטה נוצרה. צרו אותה מחדש מהגרסה הנוכחית.")
            if kind == "patient":
                parent = self._latest_summary(s, visit_id, "clinician")
                if parent is None or parent_id != parent.id:
                    raise Conflict("סיכום המטופל חייב להיווצר מגרסת טיוטת הרופא הנוכחית.")
            version = (s.scalar(select(func.max(SummaryVersion.version)).where(SummaryVersion.visit_id == visit_id, SummaryVersion.kind == kind)) or 0) + 1
            row = SummaryVersion(visit_id=visit_id, kind=kind, version=version, transcript_version_id=transcript_version_id,
                                 parent_id=parent_id, origin=origin, content=content, blocking=bool(blocking),
                                 created_by=user["id"], created_at=_now())
            s.add(row)
            s.flush()
            audit.record(s, user, action, record_type=f"{kind}_summary", record_id=row.id, record_version=version,
                         details={"visit": visit_id, "transcript_version_id": transcript_version_id, "blocking": bool(blocking),
                                  "model": (content or {}).get("model", "")[:60] if origin == "model" else None})
            self._revoke(s, user, visit_id, f"{kind}_summary_changed")
            v.state, v.error_category, v.updated_at = "draft", None, _now()
            return row.id

    def latest_summary(self, user, visit_id, kind):
        with self._clinical(user, visit_id, "summary_viewed") as (s, v):
            row = self._latest_summary(s, visit_id, kind)
            result = row.to_dict() if row else None
        if result:
            self._accessed(user, "summary_viewed", f"{kind}_summary", result["id"], result["version"])
        return result

    @classmethod
    def _current_summary(cls, s, visit_id, kind):
        row = cls._latest_summary(s, visit_id, kind)
        if not row or row.transcript_version_id != cls._latest_transcript_id(s, visit_id):
            return None
        if kind == "patient":
            clinician = cls._latest_summary(s, visit_id, "clinician")
            if not clinician or row.parent_id != clinician.id:
                return None
        return row

    def current_summary(self, user, visit_id, kind):
        """Latest summary only if still derived from the latest upstream versions."""
        with self._clinical(user, visit_id, "summary_viewed") as (s, v):
            row = self._current_summary(s, visit_id, kind)
            result = row.to_dict() if row else None
        if result:
            self._accessed(user, "summary_viewed", f"{kind}_summary", result["id"], result["version"])
        return result

    # ---- patient context (doctor-entered) -------------------------------------------------------------------
    def add_patient_context(self, user, visit_id, content: dict):
        """New doctor-entered context version. Suggestions made under the old context become stale, and an
        approval that includes a plan is revoked so the plan is reassessed."""
        with self._clinical(user, visit_id, "patient_context_updated", write=True) as (s, v):
            version = (s.scalar(select(func.max(PatientContextVersion.version)).where(PatientContextVersion.visit_id == visit_id)) or 0) + 1
            row = PatientContextVersion(visit_id=visit_id, version=version, content=content, created_by=user["id"], created_at=_now())
            s.add(row)
            s.flush()
            audit.record(s, user, "patient_context_updated", record_type="patient_context", record_id=row.id, record_version=version,
                         details={"visit": visit_id})
            self._revoke(s, user, visit_id, "patient_context_changed", only_with_plan=True)
            return row.id

    def patient_context(self, user, visit_id):
        with self._clinical(user, visit_id, "patient_context_viewed") as (s, v):
            row = s.scalar(select(PatientContextVersion).where(PatientContextVersion.visit_id == visit_id)
                           .order_by(PatientContextVersion.version.desc()).limit(1))
            result = {"id": row.id, "version": row.version, "content": row.content, "created_by": row.created_by,
                      "created_at": row.created_at} if row else None
        if result:
            self._accessed(user, "patient_context_viewed", "patient_context", result["id"], result["version"])
        return result

    @classmethod
    def _context_version(cls, s, visit_id) -> str:
        """Fingerprint of everything the assistant may rely on. Any change makes earlier suggestions stale."""
        clinician = cls._current_summary(s, visit_id, "clinician")
        ctx = s.scalar(select(PatientContextVersion).where(PatientContextVersion.visit_id == visit_id)
                       .order_by(PatientContextVersion.version.desc()).limit(1))
        return content_hash({"transcript": cls._latest_transcript_id(s, visit_id),
                             "clinician": [clinician.id, clinician.content] if clinician else None,
                             "context": [ctx.id, ctx.content] if ctx else None})

    def context_version(self, user, visit_id) -> str:
        with self._clinical(user, visit_id, "assistant_context_loaded") as (s, v):
            return self._context_version(s, visit_id)

    # ---- doctor plan (after the visit; separate from recorded facts) ------------------------------------------
    @staticmethod
    def _latest_plan(s, visit_id):
        return s.scalar(select(PlanVersion).where(PlanVersion.visit_id == visit_id).order_by(PlanVersion.version.desc()).limit(1))

    def _add_plan(self, s, user, visit_id, items, reason):
        parent = self._latest_plan(s, visit_id)
        row = PlanVersion(visit_id=visit_id, version=(parent.version + 1) if parent else 1, parent_id=parent.id if parent else None,
                          content={"items": items}, reason=reason, created_by=user["id"], created_at=_now())
        s.add(row)
        s.flush()
        audit.record(s, user, "plan_revised", record_type="plan", record_id=row.id, record_version=row.version,
                     details={"visit": visit_id, "reason": reason, "items": len(items)})
        self._revoke(s, user, visit_id, "plan_changed")
        return row

    def save_plan(self, user, visit_id, items: list, parent_id, reason="doctor_edit"):
        """Doctor edits the plan (remove/reword/add own items). Items keep their provenance."""
        with self._clinical(user, visit_id, "plan_revised", write=True) as (s, v):
            current = self._latest_plan(s, visit_id)
            if (current.id if current else None) != parent_id:
                raise Conflict("התוכנית עודכנה במקום אחר. רעננו לפני השמירה.")
            version = self._context_version(s, visit_id)
            for item in items:
                item.setdefault("context_version", version)
                if reason == "reassessed":
                    item["context_version"] = version
            return self._add_plan(s, user, visit_id, items, reason).id

    def current_plan(self, user, visit_id):
        with self._clinical(user, visit_id, "plan_viewed") as (s, v):
            row = self._latest_plan(s, visit_id)
            if not row:
                return None
            ctx = self._context_version(s, visit_id)
            result = row.to_dict() | {"stale_items": [i["id"] for i in row.content["items"] if i.get("context_version") != ctx],
                                      "context_version": ctx}
        self._accessed(user, "plan_viewed", "plan", result["id"], result["version"])
        return result

    # ---- clinical assistant records ------------------------------------------------------------------------
    def other_patient_names(self, user, visit_id) -> list[str]:
        """Names/usernames of every patient except this visit's, used only to reject questions about other
        patients (never displayed or returned by the API)."""
        with self._clinical(user, visit_id, "assistant_context_loaded") as (s, v):
            rows = s.execute(select(User.id, User.display).where(User.role == "patient", User.id != v.patient_id)).all()
            return [x for row in rows for x in row if x and len(x) >= 3]

    def assistant_conversation(self, user, visit_id) -> int:
        """The doctor's conversation for this visit (created on first use). Never shared across visits."""
        with self._clinical(user, visit_id, "assistant_opened", write=True) as (s, v):
            conv = s.scalar(select(AssistantConversation).where(AssistantConversation.visit_id == visit_id,
                                                                AssistantConversation.doctor_id == user["id"]).limit(1))
            if not conv:
                conv = AssistantConversation(visit_id=visit_id, doctor_id=user["id"], created_at=_now())
                s.add(conv)
                s.flush()
                audit.record(s, user, "assistant_opened", record_type="assistant_conversation", record_id=conv.id,
                             details={"visit": visit_id})
            return conv.id

    def add_assistant_exchange(self, user, visit_id, conversation_id, question: str, response: dict, status: str,
                               context_version: str, model: str, suggestions: list[dict]):
        """Persist one doctor question and the validated response with its suggestions, atomically."""
        with self._clinical(user, visit_id, "assistant_request", write=True) as (s, v):
            conv = s.get(AssistantConversation, conversation_id)
            if not conv or conv.visit_id != visit_id or conv.doctor_id != user["id"]:
                raise NotFound("השיחה לא נמצאה")
            now = _now()
            q = AssistantMessage(conversation_id=conv.id, visit_id=visit_id, author="doctor", status="asked",
                                 content={"question": question}, context_version=context_version, created_at=now)
            s.add(q)
            s.flush()
            audit.record(s, user, "assistant_request", record_type="assistant_message", record_id=q.id,
                         details={"visit": visit_id, "context_version": context_version[:16]})
            a = AssistantMessage(conversation_id=conv.id, visit_id=visit_id, author="assistant", status=status,
                                 content=response, context_version=context_version, model=model, created_at=now)
            s.add(a)
            s.flush()
            ids = []
            for sug in suggestions:
                row = AssistantSuggestion(message_id=a.id, visit_id=visit_id, kind=sug["kind"], text=sug["text"],
                                          rationale=sug.get("rationale", ""), citations=sug.get("citations", []),
                                          medication=sug.get("medication"), safety=sug.get("safety"),
                                          context_version=context_version, status="pending")
                s.add(row)
                s.flush()
                ids.append(row.id)
            audit.record(s, user, "assistant_result", record_type="assistant_message", record_id=a.id,
                         details={"visit": visit_id, "status": status, "suggestions": len(ids), "model": model[:60] if model else None,
                                  "citations": sum(len(x.get("citations", [])) for x in suggestions)})
            return a.id, ids

    def assistant_history(self, user, visit_id):
        with self._clinical(user, visit_id, "assistant_viewed") as (s, v):
            conv = s.scalar(select(AssistantConversation).where(AssistantConversation.visit_id == visit_id,
                                                                AssistantConversation.doctor_id == user["id"]).limit(1))
            if not conv:
                return []
            ctx = self._context_version(s, visit_id)
            out = []
            for m in s.scalars(select(AssistantMessage).where(AssistantMessage.conversation_id == conv.id).order_by(AssistantMessage.id)):
                item = {"id": m.id, "author": m.author, "status": m.status, "content": m.content, "created_at": m.created_at,
                        "context_version": m.context_version, "model": m.model, "stale": m.context_version != ctx}
                if m.author == "assistant":
                    item["suggestions"] = [self._suggestion_dict(x, ctx) for x in
                                           s.scalars(select(AssistantSuggestion).where(AssistantSuggestion.message_id == m.id).order_by(AssistantSuggestion.id))]
                out.append(item)
            return out

    @staticmethod
    def _suggestion_dict(x, ctx):
        return {c: getattr(x, c) for c in ("id", "message_id", "kind", "text", "rationale", "citations", "medication", "safety",
                                           "context_version", "status", "final_text", "decided_by", "decided_at")} | \
            {"stale": x.context_version != ctx}

    def decide_suggestion(self, user, visit_id, suggestion_id, decision, edited_text=None):
        """Doctor accepts, edits or rejects one suggestion. Accepted/edited suggestions are appended to a new
        doctor-authored plan version with provenance; the transcript and summaries are never changed."""
        if decision not in ("accepted", "edited", "rejected"):
            raise ValueError("החלטה לא תקינה.")
        with self._clinical(user, visit_id, "assistant_suggestion_decided", write=True) as (s, v):
            x = s.get(AssistantSuggestion, suggestion_id)
            if not x or x.visit_id != visit_id:
                raise NotFound("ההצעה לא נמצאה")
            if x.status != "pending":
                raise Conflict("כבר התקבלה החלטה על הצעה זו.")
            ctx = self._context_version(s, visit_id)
            if decision != "rejected" and x.context_version != ctx:
                raise Conflict("ההצעה אינה עדכנית: נתוני המטופל השתנו מאז שנוצרה. בקשו הערכה מחדש.")
            final = (edited_text or "").strip() if decision == "edited" else x.text
            if decision == "edited" and not final:
                raise ValueError("נדרש נוסח ערוך.")
            x.status, x.final_text, x.decided_by, x.decided_at = decision, final if decision != "rejected" else None, user["id"], _now()
            audit.record(s, user, "assistant_suggestion_decided", record_type="assistant_suggestion", record_id=x.id,
                         details={"visit": visit_id, "decision": decision, "kind": x.kind})
            plan_id = None
            if decision != "rejected":
                current = self._latest_plan(s, visit_id)
                items = list(current.content["items"]) if current else []
                items.append({"id": f"P{uuid.uuid4().hex[:8]}", "text": final, "source": "assistant_suggestion",
                              "suggestion_id": x.id, "message_id": x.message_id, "kind": x.kind, "citations": x.citations,
                              "original_text": x.text, "edited": decision == "edited", "context_version": ctx,
                              "added_by": user["id"], "added_at": _now()})
                plan_id = self._add_plan(s, user, visit_id, items, "suggestion_" + decision).id
            return plan_id

    # ---- approval (electronic signature) and export ------------------------------------------------------------
    def approve(self, user, visit_id, clinician_version_id, patient_version_id, expected_sha256,
                username=None, password=None, plan_version_id=None, meaning=SIGNATURE_MEANING):
        """Sign the exact current versions. Requires re-entering username and password at every signing."""
        with self._tx(False) as s:
            u = s.get(User, (username or "").strip().lower())
        if not u or u.id != (user or {}).get("id") or not security.check_password(password or "", u.password_hash):
            self._event(user, "approval_signed", "failure", "visit", visit_id, reason="signature_credentials")
            raise ValueError("החתימה נכשלה: שם המשתמש או הסיסמה שגויים. החתימה דורשת את פרטי הכניסה שלך.")
        with self._clinical(user, visit_id, "approval_signed", write=True) as (s, v):
            t_latest = self._latest_transcript_id(s, visit_id)
            c, p = self._latest_summary(s, visit_id, "clinician"), self._latest_summary(s, visit_id, "patient")
            plan = self._latest_plan(s, visit_id)
            if not c or not p or c.id != clinician_version_id or p.id != patient_version_id or (plan.id if plan else None) != plan_version_id:
                raise Conflict("הגרסה שאושרה אינה הגרסה האחרונה. רעננו ובדקו שוב.")
            if c.transcript_version_id != t_latest or p.parent_id != c.id:
                raise Conflict("הטיוטות אינן מבוססות על הגרסאות הנוכחיות.")
            if c.blocking or p.blocking:
                raise Conflict("יש בעיות חוסמות שלא תוקנו. לא ניתן לאשר.")
            if plan:
                ctx = self._context_version(s, visit_id)
                if any(i.get("context_version") != ctx for i in plan.content["items"]):
                    raise Conflict("פריטים בתוכנית נוצרו לפני שינוי בנתוני המטופל. בצעו הערכה מחדש של התוכנית לפני האישור.")
            sha = approval_hash(c.content, p.content, t_latest, plan.content if plan else None)
            if sha != expected_sha256:
                raise Conflict("תוכן הטיוטות השתנה מאז הבדיקה. בדקו שוב לפני האישור.")
            self._revoke(s, user, visit_id, "superseded")
            a = Approval(visit_id=visit_id, clinician_version_id=c.id, patient_version_id=p.id,
                         plan_version_id=plan.id if plan else None, transcript_version_id=t_latest, content_sha256=sha,
                         approved_by=user["id"], approved_at=_now(), signer_name=u.display, meaning=meaning,
                         signature_method="username+password re-entry")
            s.add(a)
            v.state, v.updated_at = "approved", _now()
            s.flush()
            audit.record(s, user, "approval_signed", record_type="approval", record_id=a.id,
                         record_version=f"c{c.version}/p{p.version}" + (f"/plan{plan.version}" if plan else ""),
                         details={"visit": visit_id, "sha256": sha[:16]})
            return a.id

    def _live_approval(self, s, visit_id):
        """The visit's valid approval with its signed versions, or None (also None if the hash no longer matches)."""
        a = s.scalar(select(Approval).where(Approval.visit_id == visit_id, Approval.revoked_at.is_(None)).order_by(Approval.id.desc()).limit(1))
        if not a:
            return None
        c, p = s.get(SummaryVersion, a.clinician_version_id), s.get(SummaryVersion, a.patient_version_id)
        plan = s.get(PlanVersion, a.plan_version_id) if a.plan_version_id else None
        if approval_hash(c.content, p.content, c.transcript_version_id, plan.content if plan else None) != a.content_sha256:
            return None
        latest_c, latest_p, latest_plan = (self._latest_summary(s, visit_id, "clinician"), self._latest_summary(s, visit_id, "patient"),
                                           self._latest_plan(s, visit_id))
        if (latest_c.id, latest_p.id) != (c.id, p.id) or c.transcript_version_id != self._latest_transcript_id(s, visit_id):
            return None
        if (latest_plan.id if latest_plan else None) != (plan.id if plan else None):
            return None
        return a, c, p, plan

    def approved(self, user, visit_id):
        """Approved content valid for this user, or None. Patients receive only their summary and approved plan."""
        with self._clinical(user, visit_id, "approved_viewed", doctor_only=False) as (s, v):
            live = self._live_approval(s, visit_id)
            if not live:
                return None
            a, c, p, plan = live
            result = {"approval_id": a.id, "approved_at": a.approved_at, "content_sha256": a.content_sha256,
                      "patient": p.content, "visit_id": visit_id, "created_at": v.created_at,
                      "versions": {"clinician": [c.id, c.version], "patient": [p.id, p.version],
                                   "plan": [plan.id, plan.version] if plan else None, "transcript": c.transcript_version_id},
                      "signature": {"signer_name": a.signer_name, "signer_id": a.approved_by, "signed_at": a.approved_at,
                                    "meaning": a.meaning, "method": a.signature_method},
                      "plan": [{"text": i["text"], "source": i["source"]} for i in plan.content["items"]] if plan else []}
            if user["role"] == "doctor":
                result["clinician"] = c.content
                result["plan_items"] = plan.content["items"] if plan else []
        self._accessed(user, "approved_viewed", "approval", result["approval_id"])
        return result

    def approvals(self, user, visit_id):
        """Approval history, newest first, including why each one was revoked."""
        with self._clinical(user, visit_id, "approvals_viewed") as (s, v):
            rows = s.scalars(select(Approval).where(Approval.visit_id == visit_id).order_by(Approval.id.desc()))
            return [{c: getattr(a, c) for c in ("id", "clinician_version_id", "patient_version_id", "plan_version_id", "content_sha256",
                                                "approved_by", "approved_at", "signer_name", "meaning", "signature_method",
                                                "revoked_at", "revoked_reason")} for a in rows]

    def record_export(self, user, visit_id):
        """Complete structured record with every version, for the treating doctor; patients get only approved content."""
        if (user or {}).get("role") == "patient":
            approved = self.approved(user, visit_id)
            if not approved:
                self._event(user, "export", "denied", "visit", visit_id, format="json", reason="not_approved")
                raise NotFound("אין גרסה מאושרת")
            data = {k: approved[k] for k in ("visit_id", "approval_id", "approved_at", "content_sha256", "versions", "signature",
                                             "patient", "plan")}
            self._event(user, "export", "success", "approval", approved["approval_id"], format="json")
            return data | {"approval_status": "approved"}
        with self._clinical(user, visit_id, "export", doctor_only=True) as (s, v):
            ts = s.scalars(select(TranscriptVersion).where(TranscriptVersion.visit_id == visit_id).order_by(TranscriptVersion.version))
            data = {"visit": self._visit_dict(v),
                    "transcript_versions": [{"id": t.id, "version": t.version, "origin": t.origin, "parent_id": t.parent_id,
                                             "created_by": t.created_by, "created_at": t.created_at, "content": t.to_content()} for t in ts],
                    "summary_versions": [x.to_dict() for x in s.scalars(select(SummaryVersion).where(SummaryVersion.visit_id == visit_id).order_by(SummaryVersion.id))],
                    "patient_context_versions": [{"id": x.id, "version": x.version, "content": x.content, "created_by": x.created_by,
                                                  "created_at": x.created_at} for x in
                                                 s.scalars(select(PatientContextVersion).where(PatientContextVersion.visit_id == visit_id).order_by(PatientContextVersion.id))],
                    "plan_versions": [x.to_dict() for x in s.scalars(select(PlanVersion).where(PlanVersion.visit_id == visit_id).order_by(PlanVersion.id))],
                    "assistant_suggestions": [self._suggestion_dict(x, "") for x in
                                              s.scalars(select(AssistantSuggestion).where(AssistantSuggestion.visit_id == visit_id).order_by(AssistantSuggestion.id))],
                    "approvals": [{c: getattr(a, c) for c in ("id", "clinician_version_id", "patient_version_id", "plan_version_id",
                                                              "transcript_version_id", "content_sha256", "approved_by", "approved_at",
                                                              "signer_name", "meaning", "signature_method", "revoked_at", "revoked_reason")}
                                  for a in s.scalars(select(Approval).where(Approval.visit_id == visit_id).order_by(Approval.id))]}
            live = self._live_approval(s, visit_id)
            data["approval_status"] = {"approved": True, "approval_id": live[0].id} if live else {"approved": False}
            for x in data["assistant_suggestions"]:
                x.pop("stale", None)
            audit.record(s, user, "export", record_type="visit", record_id=visit_id, details={"format": "json", "scope": "full_record"})
        return data

    def log_export(self, user, visit_id, approval_id, fmt):
        self._event(user, "export", "success", "approval", approval_id, format=fmt, visit=visit_id)

    # ---- audit (admin) -------------------------------------------------------------------------------------------
    def audit_events(self, actor, page=1, page_size=50, **filters):
        self._admin(actor, "audit_viewed")
        with self._tx() as s:
            result = audit.query(s, page=page, page_size=page_size, **filters)
            audit.record(s, actor, "audit_viewed", details={"page": page, **{k: v for k, v in filters.items() if v}})
        return result

    def audit_export(self, actor, fmt="csv", **filters):
        self._admin(actor, "audit_exported")
        if fmt not in ("csv", "json"):
            raise ValueError("format")
        with self._tx() as s:
            events = [e.to_dict() for e in s.scalars(select(AuditEvent).order_by(AuditEvent.seq))]
            events = [e for e in events if all(not v or str(e.get(k)) == str(v) for k, v in filters.items())]
            audit.record(s, actor, "audit_exported", details={"format": fmt, "events": len(events)})
        return audit.to_csv(events) if fmt == "csv" else json.dumps(events, ensure_ascii=False, indent=1)

    def verify_audit(self, actor):
        self._admin(actor, "audit_verified")
        with self._tx() as s:
            result = audit.verify(s)
            audit.record(s, actor, "audit_verified", "success" if result["ok"] else "failure",
                         details={"checked": result["checked"], "ok": result["ok"]})
        return result

    def log_event(self, actor, action, outcome="success", record_type=None, record_id=None, **details):
        """Events recorded by other modules (retention, worker, exports). actor=None means the system."""
        self._event(actor or audit.SYSTEM, action, outcome, record_type, record_id, **details)

    # ---- jobs ------------------------------------------------------------------------------------------------------
    def add_job(self, user, visit_id, kind):
        with self._clinical(user, visit_id, "job_created", write=True) as (s, v):
            now = _now()
            job = Job(visit_id=visit_id, kind=kind, status="queued", created_at=now, updated_at=now)
            s.add(job)
            s.flush()
            return job.id

    def update_job(self, job_id, status, error_category=None, attempt=False):
        with self._tx() as s:
            job = s.get(Job, job_id)
            job.status, job.error_category, job.updated_at = status, error_category, _now()
            job.attempts += int(attempt)

    def job(self, user, job_id):
        with self._tx(False) as s:
            job = s.get(Job, job_id)
            visit_id = job.visit_id if job else None
        if not job:
            raise NotFound("המשימה לא נמצאה")
        with self._clinical(user, visit_id, "job_viewed") as (s, v):
            return s.get(Job, job_id).to_dict()


class PseudonymStore:
    """Reversible placeholder mappings, stored apart from transcripts and deleted at session end."""

    def __init__(self, path=None):
        self.path = Path(path) if path else db.PSEUDONYM_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db.relocate_legacy(self.path)
        engine = db.engine(self.path, immediate=True)
        MappingBase.metadata.create_all(engine)
        self._session = sessionmaker(engine)

    def dispose(self):
        self._session.kw["bind"].dispose()

    def save(self, visit_id, mapping):
        with self._session() as s, s.begin():
            s.execute(delete(PseudonymMapping).where(PseudonymMapping.visit_id == visit_id))
            s.add_all(PseudonymMapping(visit_id=visit_id, placeholder=k, original=v, created_at=_now()) for k, v in mapping.items())

    def get(self, visit_id):
        with self._session() as s:
            return {m.placeholder: m.original for m in s.scalars(select(PseudonymMapping).where(PseudonymMapping.visit_id == visit_id))}

    def delete(self, visit_id):
        with self._session() as s, s.begin():
            return s.execute(delete(PseudonymMapping).where(PseudonymMapping.visit_id == visit_id)).rowcount

    def purge_older_than(self, hours=24):
        with self._session() as s, s.begin():
            return s.execute(delete(PseudonymMapping).where(PseudonymMapping.created_at < _now() - hours * 3600)).rowcount
