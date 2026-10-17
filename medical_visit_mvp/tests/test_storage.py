"""Storage, authorization, sessions, audit trail and version-bound approval (fictional data)."""
import io
import math
import sqlite3
import struct
import sys
import tempfile
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sqlalchemy import LargeBinary, select, text, update
from tests.accounts import ADMIN, DOC, OTHER_DOC, OTHER_PAT, PASSWORD, PAT, add_accounts
from backend import audit, db, security
from backend.models import Base, LoginSession, User
from backend.pipeline import Pipeline
from backend.repository import Conflict, NotFound, PseudonymStore, Repository, approval_hash


def transcript(text="אני משתעל.", roles=None):
    return {"segments": [{"id": "S1", "start": 0.0, "end": 2.0, "speaker": "SPEAKER_00", "text": text,
                          "words": [{"start": 0.0, "end": 0.5, "text": "אני"}], "flags": []}],
            "roles": roles or {"SPEAKER_00": "patient"}, "language": "he", "source": "microphone"}


def wav_bytes(seconds=1):
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(i / 8))) for i in range(16000 * seconds)))
    return b.getvalue()


class Base_(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)  # registered first, so it runs after every engine is disposed
        self.repo = Repository(Path(self.dir.name) / "visits.sqlite")
        self.addCleanup(self.repo.dispose)
        add_accounts(self.repo)
        self.visit = self.repo.create_visit(DOC, "patient_a", "he", consent=True)

    def sign(self, c, p, t, plan=None, plan_id=None, user=DOC):
        return self.repo.approve(user, self.visit, c, p, approval_hash(*self.contents(c, p), t, plan), user["id"], PASSWORD,
                                 plan_version_id=plan_id)

    def contents(self, c, p):
        with self.repo._tx(False) as s:
            from backend.models import SummaryVersion
            return s.get(SummaryVersion, c).content, s.get(SummaryVersion, p).content

    def approved_visit(self):
        t = self.repo.add_transcript(DOC, self.visit, transcript(), "auto")
        c = self.repo.add_summary(DOC, self.visit, "clinician", {"facts": ["a"]}, t, False, "model")
        p = self.repo.add_summary(DOC, self.visit, "patient", {"items": ["b"]}, t, False, "model", c)
        self.sign(c, p, t)
        return t, c, p

    def events(self, **where):
        with self.repo._tx(False) as s:
            return [e for e in audit.query(s, page_size=500)["events"] if all(e[k] == v for k, v in where.items())]


class AccountTests(Base_):
    def test_passwords_hashed_and_policy_enforced(self):
        with self.repo._tx(False) as s:
            stored = s.get(User, "doctor_a").password_hash
        self.assertNotIn(PASSWORD, stored)
        self.assertTrue(stored.startswith("pbkdf2_sha256$"))
        self.assertEqual(self.repo.authenticate("Doctor_A ", PASSWORD)["id"], "doctor_a")
        self.assertIsNone(self.repo.authenticate("doctor_a", "wrong-password-123"))
        for bad in ("short", "new_user-is-me-1", "password123"):
            with self.assertRaises(ValueError): self.repo.create_user(ADMIN, "new_user", "patient", "x", bad)

    def test_only_admin_manages_users_and_roles_no_self_elevation(self):
        for actor in (DOC, PAT, None):
            with self.assertRaises(NotFound): self.repo.create_user(actor, "new_user", "admin", "x", PASSWORD)
            with self.assertRaises(NotFound): self.repo.set_role(actor, "patient_a", "admin")
        self.repo.create_user(ADMIN, "new_doc", "doctor", "x", PASSWORD)
        self.assertTrue(self.repo.authenticate("new_doc", PASSWORD)["must_change_password"])
        self.repo.set_role(ADMIN, "new_doc", "patient")
        self.assertEqual(self.repo.user("new_doc")["role"], "patient")
        with self.assertRaises(Conflict): self.repo.set_role(ADMIN, ADMIN["id"], "doctor")  # last admin
        with self.assertRaises(Conflict): self.repo.bootstrap_admin("second_admin", "x", PASSWORD)  # CLI only once
        self.assertTrue(self.events(action="user_role_changed", outcome="success"))
        self.assertTrue(self.events(action="user_created", outcome="denied"))

    def test_doctor_adds_patient_assigned_to_themselves_only(self):
        for actor in (PAT, ADMIN, None):
            with self.assertRaises(NotFound): self.repo.create_patient(actor, "new_pat", "x", PASSWORD)
        self.repo.create_patient(DOC, "new_pat", "מטופלת חדשה", PASSWORD)
        self.assertEqual(self.repo.user("new_pat")["role"], "patient")
        self.assertTrue(self.repo.authenticate("new_pat", PASSWORD)["must_change_password"])
        self.assertIn("new_pat", [p["id"] for p in self.repo.users(DOC, "patient")])
        self.assertNotIn("new_pat", [p["id"] for p in self.repo.users(OTHER_DOC, "patient")])
        with self.assertRaises(Conflict): self.repo.create_patient(DOC, "new_pat", "x", PASSWORD)
        self.assertTrue(self.events(action="care_assigned", outcome="success"))

    def test_lockout_after_failed_logins(self):
        for _ in range(security.MAX_FAILED_LOGINS):
            self.assertIsNone(self.repo.authenticate("patient_a", "not-the-password-1"))
        self.assertIsNone(self.repo.authenticate("patient_a", PASSWORD))  # locked even with the right password
        self.assertTrue(self.events(action="account_locked"))
        self.assertTrue(self.events(action="login", outcome="denied", record_id="patient_a"))
        self.repo.reset_password(ADMIN, "patient_a", "brand-new-secret-9")
        self.assertIsNotNone(self.repo.authenticate("patient_a", "brand-new-secret-9"))

    def test_sessions_expire_idle_and_absolute_and_logout(self):
        token = self.repo.create_session(DOC)
        self.assertEqual(self.repo.session_user(token)["id"], "doctor_a")
        self.assertIsNone(self.repo.session_user("forged-token"))
        with self.repo._tx(False) as s:
            self.assertNotIn(token, s.scalars(select(LoginSession.token_sha256)).all())  # only the hash is stored
        with self.repo._tx() as s:
            s.execute(update(LoginSession).values(last_seen_at=time.time() - security.SESSION_IDLE_MINUTES * 60 - 5))
        self.assertIsNone(self.repo.session_user(token))
        token = self.repo.create_session(DOC)
        with self.repo._tx() as s:
            s.execute(update(LoginSession).values(expires_at=time.time() - 1))
        self.assertIsNone(self.repo.session_user(token))
        token = self.repo.create_session(DOC)
        self.repo.end_session(token, DOC)
        self.assertIsNone(self.repo.session_user(token))
        self.assertTrue(self.events(action="logout"))
        self.assertTrue(self.events(action="session_expired"))

    def test_password_aging_forces_change(self):
        with self.repo._tx() as s:
            s.execute(update(User).values(must_change_password=False))
        self.assertFalse(self.repo.authenticate("doctor_a", PASSWORD)["must_change_password"])
        with self.repo._tx() as s:
            s.execute(update(User).where(User.id == "doctor_a")
                      .values(password_changed_at=time.time() - (security.PASSWORD_MAX_AGE_DAYS + 1) * 86400))
        self.assertTrue(self.repo.authenticate("doctor_a", PASSWORD)["must_change_password"])

    def test_password_change_ends_sessions(self):
        token = self.repo.create_session(DOC)
        with self.assertRaises(ValueError): self.repo.change_password(DOC, "wrong-password-1", "new-password-long-1")
        self.repo.change_password(DOC, PASSWORD, "new-password-long-1")
        self.assertIsNone(self.repo.session_user(token))
        self.assertIsNotNone(self.repo.authenticate("doctor_a", "new-password-long-1"))

    def test_deactivated_user_cannot_log_in(self):
        token = self.repo.create_session(PAT)
        self.repo.set_active(ADMIN, "patient_a", False)
        self.assertIsNone(self.repo.session_user(token))
        self.assertIsNone(self.repo.authenticate("patient_a", PASSWORD))


class AccessTests(Base_):
    def test_cross_patient_and_cross_doctor_access_denied_and_audited(self):
        self.approved_visit()
        for user in (OTHER_DOC, OTHER_PAT, ADMIN):
            with self.assertRaises(NotFound): self.repo.visit(user, self.visit)
            with self.assertRaises(NotFound): self.repo.approved(user, self.visit)
            with self.assertRaises(NotFound): self.repo.record_export(user, self.visit)
        with self.assertRaises(NotFound): self.repo.transcripts(PAT, self.visit)
        with self.assertRaises(NotFound): self.repo.add_transcript(PAT, self.visit, transcript(), "edited")
        with self.assertRaises(NotFound): self.repo.create_visit(PAT, "patient_a", "he", True)
        with self.assertRaises(ValueError): self.repo.create_visit(DOC, "patient_b", "he", True)  # not assigned
        denied = self.events(outcome="denied", record_id=self.visit)
        self.assertTrue({"doctor_b", "patient_b", "admin_root", "patient_a"} <= {e["actor_id"] for e in denied})

    def test_admin_has_no_clinical_access(self):
        self.approved_visit()
        self.assertEqual(self.repo.visits(ADMIN), [])
        self.assertEqual(self.repo.users(ADMIN, "patient"), [])
        with self.assertRaises(NotFound): self.repo.latest_summary(ADMIN, self.visit, "clinician")
        with self.assertRaises(NotFound): self.repo.assistant_history(ADMIN, self.visit)

    def test_revoking_care_assignment_removes_doctor_access(self):
        self.approved_visit()
        assignment = next(a for a in self.repo.assignments(ADMIN) if a["doctor_id"] == "doctor_a")
        self.repo.revoke_assignment(ADMIN, assignment["id"])
        with self.assertRaises(NotFound): self.repo.visit(DOC, self.visit)
        self.assertEqual(self.repo.visits(DOC), [])
        self.assertIsNotNone(self.repo.approved(PAT, self.visit))  # the patient keeps their approved summary

    def test_patient_sees_only_own_approved_content(self):
        self.repo.add_transcript(DOC, self.visit, transcript(), "auto")
        self.assertIsNone(self.repo.approved(PAT, self.visit))
        with self.assertRaises(NotFound): self.repo.record_export(PAT, self.visit)
        self.approved_visit()
        view = self.repo.approved(PAT, self.visit)
        self.assertEqual(view["patient"], {"items": ["b"]})
        self.assertNotIn("clinician", view)
        self.assertNotIn("transcript_versions", self.repo.record_export(PAT, self.visit))
        self.assertIn("transcript_versions", self.repo.record_export(DOC, self.visit))


class VersionTests(Base_):
    def test_segments_and_speakers_are_rows_and_versions_are_kept(self):
        t1 = self.repo.add_transcript(DOC, self.visit, transcript(), "auto")
        self.repo.add_transcript(DOC, self.visit, transcript("אני משתעל מאוד.", {"SPEAKER_00": "patient"}), "edited", t1)
        self.assertEqual(self.repo.latest_transcript(DOC, self.visit, "auto")["content"]["segments"][0]["text"], "אני משתעל.")
        self.assertEqual(self.repo.latest_transcript(DOC, self.visit)["content"]["segments"][0]["words"][0]["text"], "אני")

    def test_transcript_edit_invalidates_approval_and_is_audited(self):
        t, _, _ = self.approved_visit()
        self.assertIsNotNone(self.repo.approved(DOC, self.visit))
        self.repo.add_transcript(DOC, self.visit, transcript("תיקון"), "edited", t)
        self.assertIsNone(self.repo.approved(DOC, self.visit))
        self.assertIsNone(self.repo.current_summary(DOC, self.visit, "clinician"))
        self.assertEqual(self.repo.approvals(DOC, self.visit)[0]["revoked_reason"], "transcript_changed")
        self.assertTrue(self.events(action="approval_invalidated"))

    def test_speaker_reassignment_and_summary_edit_invalidate(self):
        t, c, _ = self.approved_visit()
        self.repo.add_summary(DOC, self.visit, "clinician", {"facts": ["edited"]}, t, False, "edited")
        self.assertIsNone(self.repo.approved(DOC, self.visit))
        self.assertIsNone(self.repo.current_summary(DOC, self.visit, "patient"))
        t, _, _ = self.approved_visit()
        self.repo.add_transcript(DOC, self.visit, transcript(roles={"SPEAKER_00": "doctor"}), "edited", t)
        self.assertIsNone(self.repo.approved(PAT, self.visit))

    def test_signature_requires_own_credentials_and_exact_version(self):
        t = self.repo.add_transcript(DOC, self.visit, transcript(), "auto")
        c = self.repo.add_summary(DOC, self.visit, "clinician", {"facts": ["a"]}, t, False, "model")
        p = self.repo.add_summary(DOC, self.visit, "patient", {"items": ["b"]}, t, False, "model", c)
        sha = approval_hash({"facts": ["a"]}, {"items": ["b"]}, t)
        with self.assertRaises(ValueError): self.repo.approve(DOC, self.visit, c, p, sha, "doctor_a", "wrong-password-1")
        with self.assertRaises(ValueError): self.repo.approve(DOC, self.visit, c, p, sha, "doctor_b", PASSWORD)  # someone else's
        with self.assertRaises(Conflict): self.repo.approve(DOC, self.visit, c, p, "0" * 64, "doctor_a", PASSWORD)
        p2 = self.repo.add_summary(DOC, self.visit, "patient", {"items": ["c"]}, t, False, "edited", c)
        with self.assertRaises(Conflict): self.repo.approve(DOC, self.visit, c, p, sha, "doctor_a", PASSWORD)  # stale version
        a = self.repo.approve(DOC, self.visit, c, p2, approval_hash({"facts": ["a"]}, {"items": ["c"]}, t), "doctor_a", PASSWORD)
        sig = self.repo.approved(DOC, self.visit)["signature"]
        self.assertEqual((sig["signer_id"], sig["signer_name"]), ("doctor_a", "ד״ר בדיונית א"))
        self.assertIn("Reviewed and approved", sig["meaning"])
        self.assertTrue(self.events(action="approval_signed", outcome="failure"))
        self.assertTrue(self.events(action="approval_signed", outcome="success", record_id=str(a)))

    def test_blocking_issues_prevent_approval(self):
        t = self.repo.add_transcript(DOC, self.visit, transcript(), "auto")
        c = self.repo.add_summary(DOC, self.visit, "clinician", {"facts": []}, t, True, "model")
        p = self.repo.add_summary(DOC, self.visit, "patient", {"items": []}, t, False, "model", c)
        with self.assertRaises(Conflict): self.sign(c, p, t)

    def test_approval_void_if_newer_version_bypasses_revocation(self):
        t, c, _ = self.approved_visit()
        from backend.models import SummaryVersion
        with self.repo._tx() as s:  # a write that skips Repository.add_summary and its revoke
            s.add(SummaryVersion(visit_id=self.visit, kind="patient", version=99, transcript_version_id=t, parent_id=c,
                                 origin="edited", content={"items": ["x"]}, blocking=False, created_by="doctor_a", created_at=0.0))
        self.assertIsNone(self.repo.approved(DOC, self.visit))

    def test_stale_transcript_cannot_receive_summary(self):
        t1 = self.repo.add_transcript(DOC, self.visit, transcript(), "auto")
        self.repo.add_transcript(DOC, self.visit, transcript("x"), "edited", t1)
        with self.assertRaises(Conflict): self.repo.add_summary(DOC, self.visit, "clinician", {}, t1, False, "model")


class AuditTests(Base_):
    def test_events_have_required_fields_and_no_secrets_or_clinical_text(self):
        audit.new_correlation_id()
        self.approved_visit()
        self.repo.authenticate("doctor_a", "wrong-password-xyz")
        events = self.events()
        for e in events:
            for field in ("event_id", "ts_utc", "action", "outcome", "hash", "prev_hash"):
                self.assertTrue(e[field], (field, e))
            self.assertTrue(e["ts_utc"].endswith("Z"))
        blob = repr(events)
        for secret in (PASSWORD, "wrong-password-xyz", "אני משתעל", "pbkdf2"):
            self.assertNotIn(secret, blob)
        actions = {e["action"] for e in events}
        self.assertTrue({"user_created", "care_assigned", "visit_created", "transcription_saved", "summary_generated",
                         "approval_signed", "login"} <= actions)
        signed = next(e for e in events if e["action"] == "approval_signed")
        self.assertEqual((signed["actor_id"], signed["actor_role"]), ("doctor_a", "doctor"))
        self.assertTrue(signed["record_version"].startswith("c1/p1"))
        self.assertTrue(signed["correlation_id"])

    def test_forbidden_detail_keys_are_rejected(self):
        with self.repo._tx() as s:
            with self.assertRaises(ValueError): audit.record(s, DOC, "x", details={"transcript": "secret text"})

    def test_audit_is_append_only_and_chain_detects_tampering(self):
        self.approved_visit()
        self.assertTrue(self.repo.verify_audit(ADMIN)["ok"])
        with self.repo._tx() as s:
            with self.assertRaises(Exception): s.execute(text("UPDATE audit_events SET outcome='success'"))
        with self.repo._tx() as s:
            with self.assertRaises(Exception): s.execute(text("DELETE FROM audit_events"))
        # Someone with direct file access drops the trigger and edits a row: the chain check notices.
        con = sqlite3.connect(self.repo.path)
        con.execute("DROP TRIGGER audit_events_no_update")
        con.execute("UPDATE audit_events SET actor_id='someone_else' WHERE seq=3")
        con.commit(); con.close()
        result = self.repo.verify_audit(ADMIN)
        self.assertFalse(result["ok"])
        self.assertEqual(result["first_bad_seq"], 3)

    def test_ordinary_users_cannot_read_export_or_verify_audit(self):
        for user in (DOC, PAT):
            with self.assertRaises(NotFound): self.repo.audit_events(user)
            with self.assertRaises(NotFound): self.repo.audit_export(user)
            with self.assertRaises(NotFound): self.repo.verify_audit(user)
        self.assertFalse(hasattr(self.repo, "delete_audit_event") or hasattr(self.repo, "update_audit_event"))

    def test_admin_viewer_filters_paginates_and_exports(self):
        self.approved_visit()
        page = self.repo.audit_events(ADMIN, page=1, page_size=3)
        self.assertEqual(len(page["events"]), 3)
        self.assertGreater(page["total"], 3)
        only = self.repo.audit_events(ADMIN, actor_id="doctor_a", action="approval_signed")
        self.assertTrue(only["events"] and all(e["action"] == "approval_signed" for e in only["events"]))
        csv_text = self.repo.audit_export(ADMIN, "csv")
        self.assertTrue(csv_text.startswith("seq,event_id,ts_utc"))
        self.assertIn('"approval_signed"', self.repo.audit_export(ADMIN, "json"))

    def test_hmac_key_makes_chain_keyed(self):
        with patch.dict("os.environ", {"AUDIT_HMAC_KEY": "fictional-test-key"}):
            self.repo.authenticate("doctor_a", PASSWORD)
            self.assertTrue(self.repo.verify_audit(ADMIN)["keyed"])
        self.assertFalse(self.repo.verify_audit(ADMIN)["ok"])  # keyed events cannot be verified without the key


class MigrationTests(unittest.TestCase):
    def test_pre_migration_database_is_stamped_upgraded_and_backed_up(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "old.sqlite"
            con = sqlite3.connect(path)
            con.executescript("""
                CREATE TABLE users (id VARCHAR(40) PRIMARY KEY, role VARCHAR(10) NOT NULL, display VARCHAR(80) NOT NULL,
                  password_hash VARCHAR(200) NOT NULL, active BOOLEAN NOT NULL, created_by VARCHAR(40), created_at FLOAT NOT NULL,
                  CHECK (role IN ('doctor','patient')));
                CREATE TABLE login_sessions (token_sha256 VARCHAR(64) PRIMARY KEY, user_id VARCHAR(40) NOT NULL, expires_at FLOAT NOT NULL);
                CREATE TABLE visits (id VARCHAR(32) PRIMARY KEY, doctor_id VARCHAR(40) NOT NULL, patient_id VARCHAR(40) NOT NULL,
                  language VARCHAR(20), state VARCHAR(20) NOT NULL, error_category VARCHAR(40), consent BOOLEAN NOT NULL,
                  input_origin VARCHAR(20), created_at FLOAT NOT NULL, updated_at FLOAT NOT NULL);
                CREATE TABLE transcript_versions (id INTEGER PRIMARY KEY, visit_id VARCHAR(32) NOT NULL, version INTEGER NOT NULL,
                  origin VARCHAR(10) NOT NULL, parent_id INTEGER, language VARCHAR(20), source VARCHAR(20) NOT NULL,
                  roles_confirmed BOOLEAN NOT NULL, redaction_reviewed BOOLEAN NOT NULL, notices JSON NOT NULL,
                  created_by VARCHAR(40) NOT NULL, created_at FLOAT NOT NULL);
                CREATE TABLE summary_versions (id INTEGER PRIMARY KEY, visit_id VARCHAR(32) NOT NULL, kind VARCHAR(10) NOT NULL,
                  version INTEGER NOT NULL, transcript_version_id INTEGER NOT NULL, parent_id INTEGER, origin VARCHAR(10) NOT NULL,
                  content JSON NOT NULL, blocking BOOLEAN NOT NULL, created_by VARCHAR(40) NOT NULL, created_at FLOAT NOT NULL);
                CREATE TABLE approvals (id INTEGER PRIMARY KEY, visit_id VARCHAR(32) NOT NULL, clinician_version_id INTEGER NOT NULL,
                  patient_version_id INTEGER NOT NULL, content_sha256 VARCHAR(64) NOT NULL, approved_by VARCHAR(40) NOT NULL,
                  approved_at FLOAT NOT NULL, revoked_at FLOAT, revoked_reason VARCHAR(40));
                CREATE TABLE transcript_segments (id INTEGER PRIMARY KEY, transcript_version_id INTEGER NOT NULL, segment_key VARCHAR(20) NOT NULL,
                  position INTEGER NOT NULL, start FLOAT NOT NULL, "end" FLOAT NOT NULL, speaker_label VARCHAR(40) NOT NULL, text TEXT NOT NULL,
                  words JSON NOT NULL, flags JSON NOT NULL);
                CREATE TABLE speaker_assignments (id INTEGER PRIMARY KEY, transcript_version_id INTEGER NOT NULL, speaker_label VARCHAR(40) NOT NULL,
                  role VARCHAR(10) NOT NULL, assigned_by VARCHAR(40) NOT NULL, assigned_at FLOAT NOT NULL);
                CREATE TABLE jobs (id INTEGER PRIMARY KEY, visit_id VARCHAR(32) NOT NULL, kind VARCHAR(20) NOT NULL, status VARCHAR(20) NOT NULL,
                  attempts INTEGER NOT NULL, error_category VARCHAR(40), created_at FLOAT NOT NULL, updated_at FLOAT NOT NULL);
                INSERT INTO users VALUES ('dr_old','doctor','ד״ר ותיק','pbkdf2_sha256$1000$00$00',1,NULL,1.0);
                INSERT INTO users VALUES ('pt_old','patient','מטופל ותיק','pbkdf2_sha256$1000$00$00',1,'dr_old',1.0);
                INSERT INTO visits VALUES ('v1','dr_old','pt_old','he','draft',NULL,1,'manual',1.0,1.0);
            """)
            con.commit(); con.close()
            self.assertEqual(db.migrate(path), ["0002"])
            counts = db.table_counts(path)
            self.assertEqual((counts["users"], counts["visits"], counts["care_assignments"]), (2, 1, 1))
            self.assertEqual(len(list((Path(d) / "backups").iterdir())), 1)
            self.assertEqual(db.migrate(path), [])


class AudioRetentionTests(Base_):
    def test_audio_is_kept_outside_the_database_and_deletions_audited(self):
        self.assertFalse([c for t in Base.metadata.tables.values() for c in t.columns if isinstance(c.type, LargeBinary)])
        tmp = Path(self.dir.name) / "tmp_audio"
        store = PseudonymStore(Path(self.dir.name) / "pseudonyms.sqlite")
        self.addCleanup(store.dispose)
        pipe = Pipeline(self.repo, store, tmp)
        with self.assertRaises(NotFound): pipe.store_audio(PAT, self.visit, wav_bytes(), "visit.wav")
        self.assertFalse(any(tmp.iterdir()))  # nothing written for an unauthorized user
        path = pipe.store_audio(DOC, self.visit, wav_bytes(), "visit.wav")
        self.assertEqual(path.parent, tmp)
        with self.assertRaises(NotFound): pipe.end_session(OTHER_DOC, self.visit)
        self.assertTrue(path.exists())
        pipe.end_session(DOC, self.visit)
        self.assertFalse(path.exists())
        self.assertTrue(self.events(action="temp_audio_stored"))
        self.assertTrue(self.events(action="temp_audio_deleted"))

    def test_retention_purge_removes_old_audio(self):
        tmp = Path(self.dir.name) / "tmp_audio"
        store = PseudonymStore(Path(self.dir.name) / "pseudonyms.sqlite")
        self.addCleanup(store.dispose)
        pipe = Pipeline(self.repo, store, tmp)
        old = tmp / "x-old.wav"
        old.write_bytes(b"x")
        import os
        os.utime(old, (time.time() - 48 * 3600,) * 2)
        self.assertEqual(pipe.cleanup_stale(24)["audio_files"], 1)
        self.assertFalse(old.exists())
        self.assertTrue(self.events(action="retention_purge"))


if __name__ == "__main__":
    unittest.main()
