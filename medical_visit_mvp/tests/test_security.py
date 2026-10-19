"""Encryption at rest, MFA, cryptographic signatures, trusted time and security alerts
(HIPAA 164.312(a)(2)(iv), (c)(2), (d); 21 CFR 11.10(e), 11.70, 11.200, 11.300)."""
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from tests.accounts import ADMIN, DOC, PASSWORD, add_accounts
from backend import crypto, db, security
from backend.models import Approval, SigningKey
from backend.repository import Conflict, PseudonymStore, Repository, approval_hash
from tests.test_storage import Base_, transcript


def code(secret, offset=0):
    return crypto.totp(secret, int(time.time() // 30) + offset)


def enrol(repo, user):
    secret = repo.begin_mfa_enrollment(user)["secret"]
    repo.confirm_mfa(user, code(secret, -1))  # an earlier step, so the current code is still unused afterwards
    return secret


class EncryptionAtRestTests(Base_):
    def raw(self, sql):
        con = sqlite3.connect(self.repo.path)
        try:
            return con.execute(sql).fetchall()
        finally:
            con.close()

    def test_clinical_content_and_names_are_ciphertext_in_the_file(self):
        t = self.repo.add_transcript(DOC, self.visit, transcript("כואב לי הגרון מאוד"), "auto")
        self.repo.add_summary(DOC, self.visit, "clinician", {"facts": ["כאב גרון"]}, t, False, "model")
        for (value,) in self.raw("SELECT text FROM transcript_segments") + self.raw("SELECT content FROM summary_versions") \
                + self.raw("SELECT display FROM users"):
            self.assertTrue(value.startswith(crypto.PREFIX), value[:30])
            self.assertNotIn("גרון", value)
        self.assertEqual(self.repo.transcripts(DOC, self.visit)[0]["content"]["segments"][0]["text"], "כואב לי הגרון מאוד")
        self.assertEqual(self.repo.user("doctor_a")["display"], "ד״ר בדיונית א")

    def test_wrong_key_cannot_read_and_altered_ciphertext_is_detected(self):
        value = crypto.encrypt("סוד")
        tampered = value[:-4] + ("AAAA" if not value.endswith("AAAA") else "BBBB")
        with self.assertRaises(crypto.CryptoError):
            crypto.decrypt(tampered)
        with patch.dict(crypto._cache, {"data": b"\x01" * 32}):
            with self.assertRaises(crypto.CryptoError):
                crypto.decrypt(value)

    def test_pseudonym_mappings_are_encrypted(self):
        store = PseudonymStore(Path(self.dir.name) / "m.sqlite")
        self.addCleanup(store.dispose)
        store.save(self.visit, {"PATIENT_1": "ישראלה ישראלי"})
        con = sqlite3.connect(store.path)
        (value,) = con.execute("SELECT original FROM mappings").fetchone()
        con.close()
        self.assertTrue(crypto.is_encrypted(value))
        self.assertEqual(store.get(self.visit), {"PATIENT_1": "ישראלה ישראלי"})


class MigrationEncryptsExistingDataTests(unittest.TestCase):
    def test_plaintext_rows_from_revision_0002_are_encrypted_by_0003(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "v.sqlite"
            with patch.object(db, "head_revision", return_value="0002"):
                from alembic import command
                eng = db.engine(path, immediate=True, foreign_keys=False)
                with eng.begin() as conn:
                    cfg = db._config()
                    cfg.attributes["connection"] = conn
                    command.upgrade(cfg, "0002")
                eng.dispose()
            con = sqlite3.connect(path)
            con.execute("INSERT INTO users (id, role, display, password_hash, active, created_at, failed_logins, must_change_password) "
                        "VALUES ('doc', 'doctor', 'שם גלוי', 'x', 1, 0, 0, 0)")
            con.commit()
            con.close()
            self.assertEqual(db.migrate(path), ["0003"])
            con = sqlite3.connect(path)
            (display,) = con.execute("SELECT display FROM users").fetchone()
            con.close()
            self.assertTrue(crypto.is_encrypted(display))
            self.assertEqual(crypto.decrypt(display), "שם גלוי")
            self.assertTrue(list((Path(d) / "backups").glob("*before-0003*")))  # backup written before encrypting


class MfaTests(Base_):
    def setUp(self):
        super().setUp()
        patcher = patch.object(security, "MFA_REQUIRED_ROLES", {"admin", "doctor"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_required_role_must_enrol_and_cannot_get_a_session_without_a_code(self):
        user = self.repo.authenticate("doctor_a", PASSWORD)
        self.assertEqual(user["mfa"], "enroll")
        with self.assertRaises(Conflict):
            self.repo.create_session(user)
        secret = self.repo.begin_mfa_enrollment(user)["secret"]
        with self.assertRaises(ValueError):
            self.repo.confirm_mfa(user, "000000" if code(secret) != "000000" else "111111")
        verified = self.repo.confirm_mfa(user, code(secret))
        self.assertTrue(self.repo.session_user(self.repo.create_session(verified)))
        self.assertEqual(self.repo.authenticate("doctor_a", PASSWORD)["mfa"], "verify")

    def test_code_is_single_use_and_wrong_codes_lock_the_account(self):
        secret = enrol(self.repo, DOC)
        current = code(secret)
        self.assertTrue(self.repo.verify_mfa("doctor_a", current)["mfa_verified"])
        self.assertIsNone(self.repo.verify_mfa("doctor_a", current))  # replay refused
        wrong = "123456" if code(secret, 1) != "123456" else "654321"
        for _ in range(security.MAX_FAILED_LOGINS):
            self.repo.verify_mfa("doctor_a", wrong)
        self.assertIsNone(self.repo.verify_mfa("doctor_a", code(secret, 1)))  # locked
        self.assertTrue(self.events(action="account_locked"))

    def test_mfa_reset_needs_a_second_administrator(self):
        enrol(self.repo, DOC)
        with self.assertRaises(ValueError):
            self.repo.reset_mfa(ADMIN, "doctor_a", ADMIN["id"], PASSWORD)  # the same admin twice
        self.repo.create_user(ADMIN, "admin_two", "admin", "מנהל/ת שני/ה", PASSWORD)
        with self.assertRaises(ValueError):
            self.repo.reset_mfa(ADMIN, "doctor_a", "admin_two", "wrong-password-12")
        self.repo.reset_mfa(ADMIN, "doctor_a", "admin_two", PASSWORD)
        self.assertEqual(self.repo.authenticate("doctor_a", PASSWORD)["mfa"], "enroll")
        self.assertTrue(self.events(action="mfa_reset", outcome="success"))
        self.assertTrue(self.events(action="mfa_reset", outcome="denied"))
        self.assertTrue(self.repo.security_alerts(ADMIN))


class CryptographicSignatureTests(Base_):
    def setUp(self):
        super().setUp()
        patcher = patch.object(security, "MFA_REQUIRED_ROLES", {"admin", "doctor"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def drafts(self):
        t = self.repo.add_transcript(DOC, self.visit, transcript(), "auto")
        c = self.repo.add_summary(DOC, self.visit, "clinician", {"facts": ["a"]}, t, False, "model")
        p = self.repo.add_summary(DOC, self.visit, "patient", {"items": ["b"]}, t, False, "model", c)
        return t, c, p, approval_hash(*self.contents(c, p), t)

    def test_signing_requires_mfa_code_and_produces_a_verifiable_ed25519_signature(self):
        t, c, p, sha = self.drafts()
        with self.assertRaises(ValueError):
            self.repo.approve(DOC, self.visit, c, p, sha, "doctor_a", PASSWORD)  # MFA not enrolled
        secret = enrol(self.repo, DOC)
        with self.assertRaises(ValueError):
            self.repo.approve(DOC, self.visit, c, p, sha, "doctor_a", PASSWORD)  # no code
        self.repo.approve(DOC, self.visit, c, p, sha, "doctor_a", PASSWORD, totp_code=code(secret))
        sig = self.repo.approved(DOC, self.visit)["signature"]
        self.assertEqual((sig["algorithm"], sig["mfa"]), ("Ed25519", True))
        export = self.repo.record_export(DOC, self.visit)
        a, key = export["approvals"][-1], export["signing_keys"][0]
        import json
        self.assertTrue(crypto.verify(key["public_key_ed25519"], json.loads(a["manifest"]), a["signature"]))
        self.assertEqual(json.loads(a["manifest"])["content_sha256"], sha)

    def test_altered_signature_or_manifest_voids_the_approval(self):
        t, c, p, sha = self.drafts()
        secret = enrol(self.repo, DOC)
        aid = self.repo.approve(DOC, self.visit, c, p, sha, "doctor_a", PASSWORD, totp_code=code(secret))
        with self.repo._tx() as s:
            a = s.get(Approval, aid)
            a.manifest = a.manifest.replace("doctor_a", "doctor_b")
        self.assertIsNone(self.repo.approved(DOC, self.visit))
        self.assertFalse(self.repo.approvals(DOC, self.visit)[0]["signature_valid"])

    def test_key_follows_password_change_and_admin_reset_retires_it(self):
        t, c, p, sha = self.drafts()
        secret = enrol(self.repo, DOC)
        self.repo.approve(DOC, self.visit, c, p, sha, "doctor_a", PASSWORD, totp_code=code(secret))
        self.repo.change_password(DOC, PASSWORD, "new-password-for-test-1")
        self.repo.approve(DOC, self.visit, c, p, sha, "doctor_a", "new-password-for-test-1", totp_code=code(secret, 1))
        with self.repo._tx(False) as s:
            self.assertEqual(len(s.query(SigningKey).all()), 1)  # same key, re-wrapped with the new password
        self.repo.reset_password(ADMIN, "doctor_a", "reset-password-for-t-2")
        later = time.time() + 60  # one-time codes are single use: the next signature needs a later time step
        with patch.object(crypto.time, "time", return_value=later):
            self.repo.approve(DOC, self.visit, c, p, sha, "doctor_a", "reset-password-for-t-2",
                              totp_code=crypto.totp(secret, int(later // 30)))
        with self.repo._tx(False) as s:
            keys = s.query(SigningKey).order_by(SigningKey.created_at).all()
            self.assertEqual([k.retired_reason for k in keys], ["password_reset", None])
        self.assertTrue(all(a["signature_valid"] for a in self.repo.approvals(DOC, self.visit)))  # old signatures still verify

    def test_clock_skew_blocks_signing(self):
        t, c, p, sha = self.drafts()
        secret = enrol(self.repo, DOC)
        with patch.object(crypto, "clock_check", return_value={"source": "SNTP test", "offset_seconds": 600.0, "verified": True}):
            with self.assertRaises(Conflict):
                self.repo.approve(DOC, self.visit, c, p, sha, "doctor_a", PASSWORD, totp_code=code(secret))


class MfaInterfaceTests(unittest.TestCase):
    """The real login screen: enrolment on first login, then a code at every login."""

    def setUp(self):
        from tests.test_pipeline import InterfaceTests
        self.ui = InterfaceTests("test_first_run_has_no_self_registration")
        self.ui.setUp()
        self.addCleanup(self.ui.doCleanups)
        self.ui.accounts()
        patcher = patch.object(security, "MFA_REQUIRED_ROLES", {"admin", "doctor"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def code_step(self, at, code_value):
        at.text_input(key="mfa_code").input(code_value)
        self.ui.click(at, "אישור")

    def test_enrol_then_code_required_at_next_login(self):
        repo = self.ui.pipe.repo
        at = self.ui.app("doctor_a")
        self.assertIn("הגדרת אימות דו־שלבי", self.ui.text(at))
        secret = at.code[0].value
        self.code_step(at, "000000" if code(secret) != "000000" else "111111")
        self.assertIn("הקוד שגוי", self.ui.text(at))
        self.code_step(at, code(secret, -1))
        self.assertIn("סביבת עבודה קלינית", self.ui.text(at))
        at = self.ui.app("doctor_a")
        self.assertNotIn("סביבת עבודה קלינית", self.ui.text(at))  # password alone is not enough
        self.code_step(at, code(secret))
        self.assertIn("סביבת עבודה קלינית", self.ui.text(at))
        self.assertTrue(repo.authenticate("doctor_a", PASSWORD)["mfa"] == "verify")


class TotpTests(unittest.TestCase):
    def test_rfc6238_reference_value(self):
        secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # RFC 6238 test key "12345678901234567890"
        self.assertEqual(crypto.totp(secret, 59 // 30), "287082")
        self.assertEqual(crypto.totp(secret, 1111111109 // 30), "081804")


if __name__ == "__main__":
    unittest.main()
