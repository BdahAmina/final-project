"""FastAPI service for the visit workflow.

Run locally:  python -m uvicorn backend.api:app --host 127.0.0.1 --port 8000
Auth: POST /auth/login {username, password} returns a session token; send `Authorization: Bearer <token>`.
Sessions end after SESSION_IDLE_MINUTES of inactivity or SESSION_MAX_HOURS in total (backend/security.py).
Accounts with a temporary password may only change it until they do.
Every request gets a correlation id (X-Correlation-ID, echoed back) that is written to the audit trail.
Authorization is enforced in backend/repository.py on every record lookup; the role dependencies here are
only a first filter. All processing is local (faster-whisper, Ollama). There is no TLS in this process:
put it behind HTTPS (deployment/README.md) before exposing it beyond this computer.
"""
import re
from typing import Literal, Optional
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from backend import audit, export
from backend.assistant import ClinicalAssistant
from backend.pipeline import Pipeline
from backend.repository import Conflict, NotFound
from backend.schemas import ClinicianDraft, PatientContext, PatientSummary, Transcript
from backend.transcriber import AudioInputError
from backend.worker import Worker

app = FastAPI(title="Medical visit summarizer (educational prototype)", version="0.4")
pipeline = worker = assistant = None
CORRELATION = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def services() -> Pipeline:
    """Created on first use so that importing this module never touches the database."""
    global pipeline, worker, assistant
    if pipeline is None:
        pipeline = Pipeline()
        worker = Worker(pipeline)
        assistant = ClinicalAssistant(pipeline.repo)
        pipeline.cleanup_stale()
    return pipeline


@app.middleware("http")
async def correlation(request: Request, call_next):
    value = request.headers.get("X-Correlation-ID", "")
    value = value if CORRELATION.match(value) else audit.new_correlation_id()
    audit.correlation_id.set(value)
    audit.session_ref.set("")
    response = await call_next(request)
    response.headers["X-Correlation-ID"] = value
    return response


def bearer(authorization: str = Header(default="")):
    return authorization.removeprefix("Bearer ").strip()


def session_user(token: str = Depends(bearer)):
    user = services().repo.session_user(token) if token else None
    if not user:
        raise HTTPException(401, "login required")
    return user


def current_user(user=Depends(session_user)):
    if user.get("must_change_password"):
        raise HTTPException(403, "password change required")
    return user


def doctor(user=Depends(current_user)):
    if user["role"] != "doctor":
        raise HTTPException(404, "not found")
    return user


def admin(user=Depends(current_user)):
    if user["role"] != "admin":
        raise HTTPException(404, "not found")
    return user


def guard(fn, *args, **kwargs):
    """Map domain errors to HTTP without leaking details of records the caller may not see."""
    try:
        return fn(*args, **kwargs)
    except NotFound:
        raise HTTPException(404, "not found") from None
    except Conflict as exc:
        raise HTTPException(409, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


def attachment(content, media_type, name):
    return Response(content, media_type=media_type, headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---- request bodies -------------------------------------------------------------------------------------
class Login(BaseModel):
    username: str
    password: str
    totp: Optional[str] = None  # one-time code, required for MFA users


class MfaReset(BaseModel):
    second_admin: str
    second_password: str
    second_totp: Optional[str] = None


class NewUser(BaseModel):
    username: str
    display: str
    password: str
    role: Literal["admin", "doctor", "patient"] = "patient"


class RoleChange(BaseModel):
    role: Literal["admin", "doctor", "patient"]


class Activation(BaseModel):
    active: bool


class PasswordReset(BaseModel):
    new_password: str


class PasswordChange(BaseModel):
    old_password: str
    new_password: str


class Assignment(BaseModel):
    doctor_id: str
    patient_id: str
    reason: str


class NewVisit(BaseModel):
    patient_id: str
    language: Literal["he", "ar", "en", "mixed"] = "he"
    consent: bool


class ManualText(BaseModel):
    text: str


class TranscriptEdit(BaseModel):
    parent_id: int
    transcript: Transcript


class DraftEdit(BaseModel):
    draft: ClinicianDraft


class PatientEdit(BaseModel):
    summary: PatientSummary


class Language(BaseModel):
    language: Literal["Hebrew", "Arabic", "English"] = "Hebrew"


class Question(BaseModel):
    question: str


class Decision(BaseModel):
    decision: Literal["accepted", "edited", "rejected"]
    edited_text: Optional[str] = None


class PlanEdit(BaseModel):
    parent_id: Optional[int]
    items: list[dict]
    reason: Literal["doctor_edit", "reassessed"] = "doctor_edit"


class ApprovalRequest(BaseModel):
    clinician_version_id: int
    patient_version_id: int
    plan_version_id: Optional[int] = None
    sha256: str
    username: str
    password: str
    totp: Optional[str] = None


# ---- auth -------------------------------------------------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/auth/login")
def login(body: Login):
    repo = services().repo
    user = repo.authenticate(body.username, body.password)
    if not user:
        raise HTTPException(401, "wrong username or password, or account locked")
    if user["mfa"] == "enroll":
        raise HTTPException(403, "MFA enrolment required: sign in once through the app to set up an authenticator")
    if user["mfa"] == "verify":
        user = repo.verify_mfa(user["id"], body.totp)
        if not user:
            raise HTTPException(401, "one-time code missing, wrong or already used")
    return {"token": repo.create_session(user), "user": user}


@app.post("/auth/logout")
def logout(token: str = Depends(bearer), user=Depends(session_user)):
    services().repo.end_session(token, user)
    return {"logged_out": True}


@app.get("/auth/me")
def me(user=Depends(session_user)):
    return user


@app.put("/auth/password")
def change_password(body: PasswordChange, user=Depends(session_user)):
    guard(services().repo.change_password, user, body.old_password, body.new_password)
    return {"changed": True, "note": "all sessions ended; log in again"}


# ---- admin: accounts, roles, care assignments, audit (no clinical content) ------------------------------------------
@app.get("/admin/users")
def list_users(user=Depends(admin)):
    return guard(services().repo.all_users, user)


@app.post("/admin/users")
def create_user(body: NewUser, user=Depends(admin)):
    return {"username": guard(services().repo.create_user, user, body.username, body.role, body.display, body.password)}


@app.put("/admin/users/{user_id}/role")
def set_role(user_id: str, body: RoleChange, user=Depends(admin)):
    guard(services().repo.set_role, user, user_id, body.role)
    return {"changed": True}


@app.put("/admin/users/{user_id}/active")
def set_active(user_id: str, body: Activation, user=Depends(admin)):
    guard(services().repo.set_active, user, user_id, body.active)
    return {"changed": True}


@app.post("/admin/users/{user_id}/mfa-reset")
def reset_mfa(user_id: str, body: MfaReset, user=Depends(admin)):
    guard(services().repo.reset_mfa, user, user_id, body.second_admin, body.second_password, body.second_totp)
    return {"mfa_reset": True}


@app.get("/admin/security-alerts")
def security_alerts(hours: int = 24, user=Depends(admin)):
    return guard(services().repo.security_alerts, user, hours)


@app.post("/admin/users/{user_id}/password-reset")
def reset_password(user_id: str, body: PasswordReset, user=Depends(admin)):
    guard(services().repo.reset_password, user, user_id, body.new_password)
    return {"reset": True}


@app.get("/admin/assignments")
def assignments(user=Depends(admin)):
    return guard(services().repo.assignments, user)


@app.post("/admin/assignments")
def assign(body: Assignment, user=Depends(admin)):
    return {"assignment_id": guard(services().repo.assign, user, body.doctor_id, body.patient_id, body.reason)}


@app.delete("/admin/assignments/{assignment_id}")
def revoke(assignment_id: int, user=Depends(admin)):
    guard(services().repo.revoke_assignment, user, assignment_id)
    return {"revoked": True}


@app.get("/admin/audit")
def audit_events(page: int = 1, page_size: int = 50, actor_id: Optional[str] = None, action: Optional[str] = None,
                 record_id: Optional[str] = None, outcome: Optional[str] = None, since: Optional[str] = None,
                 until: Optional[str] = None, user=Depends(admin)):
    return guard(services().repo.audit_events, user, page=page, page_size=page_size, actor_id=actor_id, action=action,
                 record_id=record_id, outcome=outcome, since=since, until=until)


@app.get("/admin/audit/export")
def audit_export(format: Literal["csv", "json"] = "csv", user=Depends(admin)):
    data = guard(services().repo.audit_export, user, format)
    return attachment(data, "text/csv; charset=utf-8" if format == "csv" else "application/json", f"audit_events.{format}")


@app.get("/admin/audit/verify")
def audit_verify(user=Depends(admin)):
    return guard(services().repo.verify_audit, user)


# ---- doctor: visits and workflow --------------------------------------------------------------------------------
@app.get("/patients")
def patients(user=Depends(doctor)):
    return services().repo.users(user, "patient")


@app.post("/visits")
def create_visit(body: NewVisit, user=Depends(doctor)):
    return {"visit_id": guard(services().repo.create_visit, user, body.patient_id, body.language, body.consent)}


@app.get("/visits")
def list_visits(user=Depends(current_user)):
    repo = services().repo
    visits = repo.visits(user)
    if user["role"] == "patient":
        return [{"id": v["id"], "created_at": v["created_at"]} for v in visits if repo.approved(user, v["id"])]
    return visits


@app.get("/visits/{visit_id}")
def get_visit(visit_id: str, user=Depends(doctor)):
    return guard(services().repo.visit, user, visit_id)


@app.post("/visits/{visit_id}/audio")
def upload_audio(visit_id: str, file: UploadFile = File(...), language: Optional[str] = Form(None),
                 model: Literal["small", "turbo", "large-v3"] = Form("small"), user=Depends(doctor)):
    data = file.file.read()
    try:
        path = services().store_audio(user, visit_id, data, file.filename or "upload.wav")
    except NotFound:
        raise HTTPException(404, "not found") from None
    except AudioInputError as exc:
        raise HTTPException(422, str(exc)) from None
    lang = None if language in (None, "", "mixed", "auto") else language
    return {"job_id": worker.submit(user, visit_id, "transcribe", path=str(path), language=lang, model=model, source="upload")}


@app.post("/visits/{visit_id}/transcript/manual")
def manual(visit_id: str, body: ManualText, user=Depends(doctor)):
    return {"transcript_version_id": guard(services().manual_transcript, user, visit_id, body.text)}


@app.get("/visits/{visit_id}/transcript")
def transcript(visit_id: str, user=Depends(doctor)):
    return guard(services().repo.transcripts, user, visit_id)


@app.put("/visits/{visit_id}/transcript")
def edit_transcript(visit_id: str, body: TranscriptEdit, user=Depends(doctor)):
    return {"transcript_version_id": guard(services().save_review, user, visit_id, body.transcript, body.parent_id)}


@app.post("/visits/{visit_id}/redaction")
def redact(visit_id: str, user=Depends(doctor)):
    version, findings = guard(services().redact, user, visit_id)
    return {"transcript_version_id": version, "findings": findings}


@app.put("/visits/{visit_id}/redaction")
def confirm_redaction(visit_id: str, body: TranscriptEdit, user=Depends(doctor)):
    return {"transcript_version_id": guard(services().confirm_redaction, user, visit_id, body.transcript, body.parent_id)}


@app.post("/visits/{visit_id}/extraction")
def extraction(visit_id: str, body: Language, user=Depends(doctor)):
    guard(services().repo.require_doctor, user, visit_id, "extraction_requested")
    return {"job_id": worker.submit(user, visit_id, "extract", language=body.language)}


@app.put("/visits/{visit_id}/clinician")
def edit_clinician(visit_id: str, body: DraftEdit, user=Depends(doctor)):
    return {"summary_version_id": guard(services().save_clinician, user, visit_id, body.draft)}


@app.post("/visits/{visit_id}/patient-summary")
def patient_summary(visit_id: str, body: Language, user=Depends(doctor)):
    guard(services().repo.require_doctor, user, visit_id, "patient_summary_requested")
    return {"job_id": worker.submit(user, visit_id, "patient_summary", language=body.language)}


@app.put("/visits/{visit_id}/patient-summary")
def edit_patient(visit_id: str, body: PatientEdit, user=Depends(doctor)):
    return {"summary_version_id": guard(services().save_patient, user, visit_id, body.summary)}


@app.get("/jobs/{job_id}")
def job(job_id: int, user=Depends(doctor)):
    return guard(services().repo.job, user, job_id)


# ---- doctor: patient context, assistant, plan -------------------------------------------------------------------
@app.get("/visits/{visit_id}/context")
def get_context(visit_id: str, user=Depends(doctor)):
    return guard(services().repo.patient_context, user, visit_id)


@app.put("/visits/{visit_id}/context")
def put_context(visit_id: str, body: PatientContext, user=Depends(doctor)):
    return {"patient_context_id": guard(services().repo.add_patient_context, user, visit_id, body.model_dump())}


@app.post("/visits/{visit_id}/assistant")
def ask_assistant(visit_id: str, body: Question, user=Depends(doctor)):
    services()
    return guard(assistant.ask, user, visit_id, body.question)


@app.get("/visits/{visit_id}/assistant")
def assistant_history(visit_id: str, user=Depends(doctor)):
    return guard(services().repo.assistant_history, user, visit_id)


@app.post("/visits/{visit_id}/assistant/suggestions/{suggestion_id}")
def decide(visit_id: str, suggestion_id: int, body: Decision, user=Depends(doctor)):
    return {"plan_version_id": guard(services().repo.decide_suggestion, user, visit_id, suggestion_id, body.decision, body.edited_text)}


@app.get("/visits/{visit_id}/plan")
def get_plan(visit_id: str, user=Depends(doctor)):
    return guard(services().repo.current_plan, user, visit_id)


@app.put("/visits/{visit_id}/plan")
def put_plan(visit_id: str, body: PlanEdit, user=Depends(doctor)):
    return {"plan_version_id": guard(services().repo.save_plan, user, visit_id, body.items, body.parent_id, body.reason)}


# ---- approval (electronic signature) and exports ----------------------------------------------------------------
@app.get("/visits/{visit_id}/review")
def review(visit_id: str, user=Depends(doctor)):
    package = guard(services().review_package, user, visit_id)
    if not package:
        raise HTTPException(409, "no current drafts")
    return package


@app.post("/visits/{visit_id}/approval")
def approve(visit_id: str, body: ApprovalRequest, user=Depends(doctor)):
    return {"approval_id": guard(services().approve, user, visit_id, body.clinician_version_id, body.patient_version_id,
                                 body.sha256, body.username, body.password, body.plan_version_id, body.totp)}


@app.get("/visits/{visit_id}/export")
def export_visit(visit_id: str, format: Literal["txt", "pdf", "json"] = "txt", user=Depends(current_user)):
    """Approved content only (txt/pdf). json: full versioned record for the doctor, approved content for the patient."""
    repo = services().repo
    if format == "json":
        return attachment(export.to_json(guard(repo.record_export, user, visit_id)), "application/json", f"visit_{visit_id}.json")
    approved = guard(repo.approved, user, visit_id)
    if not approved:
        repo.log_event(user, "export", "denied", "visit", visit_id, format=format, reason="not_approved")
        raise HTTPException(409, "no valid approved version")
    clinician = user["role"] == "doctor"
    content = guard(export.to_pdf, approved, clinician) if format == "pdf" else export.to_text(approved, clinician)
    repo.log_export(user, visit_id, approved["approval_id"], format)
    return attachment(content, "application/pdf" if format == "pdf" else "text/plain; charset=utf-8", f"visit_{visit_id}.{format}")


@app.get("/patient/visits/{visit_id}")
def patient_view(visit_id: str, user=Depends(current_user)):
    if user["role"] != "patient":
        raise HTTPException(404, "not found")
    approved = guard(services().repo.approved, user, visit_id)
    if not approved:
        raise HTTPException(404, "not found")
    return {"visit_id": visit_id, "approved_at": approved["approved_at"], "summary": approved["patient"], "plan": approved["plan"],
            "signature": approved["signature"]}


@app.delete("/visits/{visit_id}/session")
def end_session(visit_id: str, user=Depends(doctor)):
    guard(services().end_session, user, visit_id)
    return {"deleted": True}
