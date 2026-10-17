"""HTTP API: authentication, role restrictions, protected exports, assistant endpoints (fictional data)."""
import io
import json
import struct
import sys
import tempfile
import time
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fastapi.testclient import TestClient
from sqlalchemy import update
from tests.accounts import PASSWORD, add_accounts
from tests.test_components import FakeClient
from backend import api
from backend.assistant import ClinicalAssistant
from backend.knowledge import load
from backend.models import User
from backend.pipeline import Pipeline
from backend.repository import PseudonymStore, Repository
from backend.worker import Worker

EXTRACTION = {"facts": [
    {"category": "treatment", "statement": "אקמול 500 מ\"ג", "segment_ids": ["S2"], "quote": "קח אקמול 500 מ\"ג",
     "treatment_status": "recommended", "medication": {"name": "אקמול", "dose": "500", "unit": "מ\"ג"}}]}
PATIENT = {"items": [{"text": "קח אקמול 500 מ\"ג.", "fact_ids": ["F1"]}]}


def wav():
    b = io.BytesIO()
    with wave.open(b, "wb") as f:
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(16000); f.writeframes(struct.pack("<h", 900) * 32000)
    return b.getvalue()


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        d = Path(self.dir.name)
        repo = Repository(d / "v.sqlite")
        self.addCleanup(repo.dispose)
        store = PseudonymStore(d / "m.sqlite")
        self.addCleanup(store.dispose)
        self.pipeline = Pipeline(repo, store, d / "tmp")
        self.client_model = FakeClient(json.dumps(EXTRACTION, ensure_ascii=False), json.dumps(PATIENT, ensure_ascii=False))
        self.worker = Worker(self.pipeline, client_factory=lambda: self.client_model)
        self.addCleanup(self.worker.pool.shutdown, wait=True)
        self.assistant = ClinicalAssistant(repo, load(), client_factory=lambda: FakeClient())
        for name, value in (("pipeline", self.pipeline), ("worker", self.worker), ("assistant", self.assistant)):
            p = patch.object(api, name, value)
            p.start(); self.addCleanup(p.stop)
        self.http = TestClient(api.app)
        add_accounts(repo)
        with repo._tx() as s:  # skip the forced first-login password change except where a test checks it
            s.execute(update(User).values(must_change_password=False))
        self.tokens = {}

    def H(self, user):
        if user not in self.tokens:
            r = self.http.post("/auth/login", json={"username": user, "password": PASSWORD})
            self.assertEqual(r.status_code, 200, r.text)
            self.tokens[user] = r.json()["token"]
        return {"Authorization": f"Bearer {self.tokens[user]}"}

    def wait(self, job_id):
        for _ in range(200):
            job = self.http.get(f"/jobs/{job_id}", headers=self.H("doctor_a")).json()
            if job["status"] in ("done", "failed"):
                return job
            time.sleep(0.02)
        self.fail("job did not finish")

    def new_visit(self):
        r = self.http.post("/visits", json={"patient_id": "patient_a", "consent": True}, headers=self.H("doctor_a"))
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["visit_id"]

    def approved_visit(self):
        v = self.new_visit()
        doc = self.H("doctor_a")
        self.http.post(f"/visits/{v}/transcript/manual", json={"text": "מטופל: כואב לי הראש.\nרופא: קח אקמול 500 מ\"ג."}, headers=doc)
        t = self.http.get(f"/visits/{v}/transcript", headers=doc).json()[-1]
        t["content"]["roles_confirmed"] = True
        self.http.put(f"/visits/{v}/transcript", json={"parent_id": t["id"], "transcript": t["content"]}, headers=doc)
        self.http.post(f"/visits/{v}/redaction", headers=doc)
        t = self.http.get(f"/visits/{v}/transcript", headers=doc).json()[-1]
        self.assertEqual(self.http.put(f"/visits/{v}/redaction", json={"parent_id": t["id"], "transcript": t["content"]}, headers=doc).status_code, 200)
        self.assertEqual(self.wait(self.http.post(f"/visits/{v}/extraction", json={}, headers=doc).json()["job_id"])["status"], "done")
        self.assertEqual(self.http.get(f"/visits/{v}/export", headers=doc).status_code, 409)
        self.assertEqual(self.wait(self.http.post(f"/visits/{v}/patient-summary", json={}, headers=doc).json()["job_id"])["status"], "done")
        pkg = self.http.get(f"/visits/{v}/review", headers=doc).json()
        body = {"clinician_version_id": pkg["clinician"]["id"], "patient_version_id": pkg["patient"]["id"], "sha256": pkg["sha256"],
                "username": "doctor_a", "password": PASSWORD}
        self.assertEqual(self.http.post(f"/visits/{v}/approval", json=body | {"password": "wrong-password-1"}, headers=doc).status_code, 422)
        self.assertEqual(self.http.post(f"/visits/{v}/approval", json=body | {"sha256": "x"}, headers=doc).status_code, 409)
        ok = self.http.post(f"/visits/{v}/approval", json=body, headers=doc)
        self.assertEqual(ok.status_code, 200, ok.text)
        return v, pkg

    def test_login_logout_lockout_and_correlation_id(self):
        self.assertEqual(self.http.post("/auth/login", json={"username": "doctor_a", "password": "wrong-pass-123"}).status_code, 401)
        self.assertEqual(self.http.get("/visits", headers={"Authorization": "Bearer demo-doctor_a"}).status_code, 401)
        r = self.http.get("/auth/me", headers=self.H("doctor_a") | {"X-Correlation-ID": "test-correlation-1"})
        self.assertEqual((r.json()["id"], r.headers["X-Correlation-ID"]), ("doctor_a", "test-correlation-1"))
        self.http.post("/auth/logout", headers=self.H("doctor_a"))
        self.assertEqual(self.http.get("/auth/me", headers=self.H("doctor_a")).status_code, 401)
        for _ in range(5):
            self.http.post("/auth/login", json={"username": "patient_b", "password": "wrong-pass-123"})
        self.assertEqual(self.http.post("/auth/login", json={"username": "patient_b", "password": PASSWORD}).status_code, 401)

    def test_temporary_password_must_be_changed_first(self):
        r = self.http.post("/admin/users", json={"username": "new.doctor", "display": "x", "password": PASSWORD, "role": "doctor"},
                           headers=self.H("admin_root"))
        self.assertEqual(r.status_code, 200, r.text)
        token = self.http.post("/auth/login", json={"username": "new.doctor", "password": PASSWORD}).json()["token"]
        h = {"Authorization": f"Bearer {token}"}
        self.assertEqual(self.http.get("/visits", headers=h).status_code, 403)
        self.assertEqual(self.http.put("/auth/password", json={"old_password": PASSWORD, "new_password": "a-fresh-secret-42"}, headers=h).status_code, 200)
        self.assertEqual(self.http.get("/visits", headers=h).status_code, 401)  # sessions end on password change
        token = self.http.post("/auth/login", json={"username": "new.doctor", "password": "a-fresh-secret-42"}).json()["token"]
        self.assertEqual(self.http.get("/visits", headers={"Authorization": f"Bearer {token}"}).status_code, 200)

    def test_role_restrictions_admin_has_no_clinical_access(self):
        v, _ = self.approved_visit()
        adm = self.H("admin_root")
        for path in (f"/visits/{v}", f"/visits/{v}/transcript", f"/visits/{v}/assistant", f"/patient/visits/{v}"):
            self.assertEqual(self.http.get(path, headers=adm).status_code, 404, path)
        self.assertEqual(self.http.get(f"/visits/{v}/export?format=json", headers=adm).status_code, 404)
        self.assertEqual(self.http.get("/visits", headers=adm).json(), [])
        for user in ("doctor_a", "patient_a"):
            for path in ("/admin/users", "/admin/audit", "/admin/audit/export", "/admin/assignments"):
                self.assertEqual(self.http.get(path, headers=self.H(user)).status_code, 404, (user, path))
        r = self.http.post("/admin/users", json={"username": "new.patient", "display": "x", "password": "long-enough-pass-1"}, headers=adm)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.http.post("/admin/users", json={"username": "evil", "display": "x", "password": "long-enough-pass-1", "role": "admin"},
                                        headers=self.H("doctor_a")).status_code, 404)

    def test_cross_user_access_and_protected_exports(self):
        v, _ = self.approved_visit()
        for user in ("doctor_b", "patient_b"):
            for fmt in ("txt", "pdf", "json"):
                self.assertEqual(self.http.get(f"/visits/{v}/export?format={fmt}", headers=self.H(user)).status_code, 404, (user, fmt))
        doc, pat = self.H("doctor_a"), self.H("patient_a")
        txt = self.http.get(f"/visits/{v}/export", headers=doc).text
        for needle in ("סיכום לרופא", "SHA-256", "חתימה אלקטרונית", "doctor_a", "מועד החתימה (UTC)", "מזהה אישור"):
            self.assertIn(needle, txt)
        self.assertNotIn("סיכום לרופא", self.http.get(f"/visits/{v}/export", headers=pat).text)
        pdf = self.http.get(f"/visits/{v}/export?format=pdf", headers=doc)
        self.assertEqual(pdf.status_code, 200, pdf.text)
        self.assertTrue(pdf.content.startswith(b"%PDF"))
        full = json.loads(self.http.get(f"/visits/{v}/export?format=json", headers=doc).text)
        self.assertTrue(full["approval_status"]["approved"])
        self.assertTrue({"transcript_versions", "summary_versions", "approvals", "plan_versions"} <= set(full))
        own = json.loads(self.http.get(f"/visits/{v}/export?format=json", headers=pat).text)
        self.assertNotIn("transcript_versions", own)
        self.assertEqual(own["approval_status"], "approved")

    def test_edit_after_approval_blocks_export_of_changed_draft(self):
        v, pkg = self.approved_visit()
        doc = self.H("doctor_a")
        draft = pkg["clinician"]["content"]
        draft["facts"][0]["statement"] = "אקמול 500 מ\"ג לפי הצורך"
        self.http.put(f"/visits/{v}/clinician", json={"draft": draft}, headers=doc)
        for fmt in ("txt", "pdf"):
            self.assertEqual(self.http.get(f"/visits/{v}/export?format={fmt}", headers=doc).status_code, 409)
        self.assertEqual(self.http.get(f"/patient/visits/{v}", headers=self.H("patient_a")).status_code, 404)
        self.assertFalse(json.loads(self.http.get(f"/visits/{v}/export?format=json", headers=doc).text)["approval_status"]["approved"])

    def test_admin_audit_viewer_filters_paginates_and_exports(self):
        self.approved_visit()
        adm = self.H("admin_root")
        page = self.http.get("/admin/audit?page_size=5", headers=adm).json()
        self.assertEqual(len(page["events"]), 5)
        signed = self.http.get("/admin/audit?action=approval_signed&outcome=success", headers=adm).json()["events"]
        self.assertEqual(len(signed), 1)
        self.assertTrue(signed[0]["correlation_id"])
        self.assertTrue(self.http.get("/admin/audit/export?format=csv", headers=adm).text.startswith("seq,event_id"))
        self.assertTrue(self.http.get("/admin/audit/verify", headers=adm).json()["ok"])

    def test_assistant_endpoints_are_doctor_and_visit_bound(self):
        v = self.new_visit()
        for user in ("doctor_b", "patient_a", "admin_root"):
            r = self.http.post(f"/visits/{v}/assistant", json={"question": "אילו נתונים חסרים?"}, headers=self.H(user))
            self.assertEqual(r.status_code, 404, user)
        r = self.http.post(f"/visits/{v}/assistant", json={"question": "כתוב לי קוד בפייתון"}, headers=self.H("doctor_a"))
        self.assertEqual(r.json()["status"], "rejected_unrelated")
        self.assertEqual(self.http.put(f"/visits/{v}/context", json={"allergy_status": "listed"}, headers=self.H("doctor_a")).status_code, 422)

    def test_audio_upload_runs_local_job_and_removes_temp_file(self):
        v = self.new_visit()
        engine = Mock()
        engine.transcribe.return_value = (iter([SimpleNamespace(start=0, end=2, text="שלום", words=[])]), SimpleNamespace(duration=2, language="he"))
        with patch("backend.transcriber.local_model", return_value=engine), patch("backend.diarization.ready", return_value=(False, "x")):
            r = self.http.post(f"/visits/{v}/audio", files={"file": ("r.wav", wav(), "audio/wav")}, headers=self.H("doctor_a"))
            job = self.wait(r.json()["job_id"])
        self.assertEqual(job["status"], "done")
        self.assertEqual(list(self.pipeline.tmp.glob("*")), [])
        t = self.http.get(f"/visits/{v}/transcript", headers=self.H("doctor_a")).json()[-1]
        self.assertEqual(t["origin"], "auto")
        self.assertTrue(any("הפרדת" in n for n in t["content"]["notices"]))

    def test_bad_audio_rejected(self):
        v = self.new_visit()
        r = self.http.post(f"/visits/{v}/audio", files={"file": ("r.exe", b"MZ", "application/octet-stream")}, headers=self.H("doctor_a"))
        self.assertEqual(r.status_code, 422)
        r = self.http.post(f"/visits/{v}/audio", files={"file": ("r.wav", b"not wav", "audio/wav")}, headers=self.H("doctor_a"))
        self.assertEqual(r.status_code, 422)


if __name__ == "__main__":
    unittest.main()
