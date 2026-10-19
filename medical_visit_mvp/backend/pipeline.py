"""Visit workflow: audio -> transcript -> roles -> redaction -> facts -> drafts -> approval.

States: received, transcribing, source_review, extracting, draft, approved, failed.
Every new upstream version invalidates dependent drafts and approvals
(enforced in repository.py). All processing is local: faster-whisper for
transcription, Ollama for extraction and summaries. Nothing is sent to a cloud service.
"""
import logging
import re
import uuid
from pathlib import Path
from backend import db, diarization, redaction, retention, transcriber
from backend.repository import Conflict, PseudonymStore, Repository, approval_hash
from backend.schemas import ClinicianDraft, PatientSummary, Segment, Transcript, blocking_issues
from backend.summarizer import extract_facts, patient_summary
from backend.validators import revalidate, revalidate_patient

LOG = logging.getLogger(__name__)
TMP_AUDIO = db.TMP_AUDIO
MAX_ATTEMPTS = 2
ALLOWED_AUDIO = {".wav", ".mp3", ".m4a", ".ogg", ".flac", ".webm"}
PREFIXES = {
    "doctor": ("רופא", "רופאה", "הרופא", "ד״ר", "طبيب", "طبيبة", "الطبيب", "دكتور", "doctor", "dr"),
    "patient": ("מטופל", "מטופלת", "המטופל", "مريض", "مريضة", "المريض", "patient"),
}


def parse_manual(text: str) -> Transcript:
    """Turn pasted text with 'רופא:' / 'מטופל:' prefixes into segments; origin recorded as manual."""
    segments, roles = [], {}
    for line in (l.strip() for l in text.splitlines()):
        if not line:
            continue
        speaker = "UNKNOWN"
        m = re.match(r"^([^:：]{1,15})[:：]\s*(.*)$", line)
        if m:
            label = m.group(1).strip().lower().rstrip(".")
            for role, names in PREFIXES.items():
                if label in names:
                    speaker, line = f"SPEAKER_{role.upper()}", m.group(2)
                    roles[speaker] = role
        if line:
            segments.append(Segment(id=f"S{len(segments) + 1}", text=line, speaker=speaker,
                                    flags=["unassigned"] if speaker == "UNKNOWN" else []))
    if not segments:
        raise ValueError("התמלול ריק.")
    return Transcript(segments=segments, roles=roles, source="manual")


class Pipeline:
    def __init__(self, repo=None, pseudonyms=None, tmp_dir=None):
        self.repo = repo or Repository()
        self.pseudonyms = pseudonyms or PseudonymStore()
        self.tmp = Path(tmp_dir) if tmp_dir else TMP_AUDIO
        self.tmp.mkdir(parents=True, exist_ok=True)

    # ---- audio -------------------------------------------------------------
    def store_audio(self, user, visit_id, data: bytes, filename: str) -> Path:
        """Temporary file for the background transcription job (API path). Never stored in the database;
        deleted by the job, at session end, or by retention.purge()."""
        suffix = Path(filename).suffix.lower()
        if suffix not in ALLOWED_AUDIO:
            raise transcriber.AudioInputError("סוג קובץ לא נתמך. השתמשו ב־WAV, MP3, M4A, OGG, FLAC או WEBM.")
        transcriber.check_audio(data, filename)
        self.repo.set_state(user, visit_id, "received")  # doctor-only access check before anything is written
        path = self.tmp / f"{visit_id}-{uuid.uuid4().hex[:8]}{suffix}"
        path.write_bytes(data)
        self.repo.log_event(user, "temp_audio_stored", record_type="visit", record_id=visit_id, bytes=len(data),
                            retention_hours=retention.TMP_AUDIO_RETENTION_HOURS)
        return path

    def delete_audio(self, user, visit_id, path: Path):
        Path(path).unlink(missing_ok=True)
        self.repo.log_event(user, "temp_audio_deleted", record_type="visit", record_id=visit_id, reason="job_finished")

    def transcribe(self, user, visit_id, data: bytes, filename: str, language=None, model="small",
                   source="microphone", use_diarization=True, progress=lambda stage: None):
        """Transcribe locally with faster-whisper (+ local diarization when available).
        Audio stays in memory; returns transcript version id."""
        self.repo.set_state(user, visit_id, "transcribing", input_origin=source)
        notices = []
        try:
            progress("decode")
            transcriber.check_audio(data, filename)
            audio = transcriber.decode(data)
            result = None
            for attempt in range(1, MAX_ATTEMPTS + 1):
                try:
                    progress("transcribe")
                    result = transcriber.transcribe_local(data, filename, language, model, audio=audio)
                    break
                except transcriber.AudioInputError:
                    raise
                except ValueError:
                    if attempt == MAX_ATTEMPTS:
                        raise
                    LOG.warning("Transcription retry: visit=%s attempt=%d", visit_id, attempt)
            if result.get("notice"):
                notices.append(result["notice"])
            segments = result["segments"]
            if use_diarization:
                progress("diarize")
                try:
                    segments = diarization.align(segments, diarization.diarize(audio))
                except diarization.DiarizationUnavailable as exc:
                    notices.append(str(exc))
                except Exception as exc:
                    LOG.warning("Diarization failed: visit=%s error=%s", visit_id, type(exc).__name__)
                    notices.append("הפרדת הדוברים נכשלה; שייכו את הדוברים ידנית.")
            for s in segments:
                s.setdefault("speaker", "UNKNOWN")
                if s["speaker"] == "UNKNOWN":
                    s.setdefault("flags", []).append("unassigned")
            transcript = Transcript(segments=[Segment(**{k: v for k, v in s.items() if k in Segment.model_fields}) for s in segments],
                                    language=result["language"], notices=notices, source=source)
            return self.repo.add_transcript(user, visit_id, transcript.model_dump(), "auto")
        except ValueError as exc:
            category = "input" if isinstance(exc, transcriber.AudioInputError) else "transcription"
            self.repo.set_state(user, visit_id, "failed", error_category=category)
            raise

    def manual_transcript(self, user, visit_id, text):
        transcript = parse_manual(text)
        self.repo.set_state(user, visit_id, "received", input_origin="manual")
        return self.repo.add_transcript(user, visit_id, transcript.model_dump(), "manual")

    # ---- source review -----------------------------------------------------
    def transcript(self, user, visit_id):
        row = self.repo.latest_transcript(user, visit_id)
        return (row, Transcript(**row["content"])) if row else (None, None)

    def save_review(self, user, visit_id, transcript: Transcript, parent_id):
        """Save corrected text/roles. Any edit drops redaction approval and dependent drafts."""
        current, _ = self.transcript(user, visit_id)
        if not current or current["id"] != parent_id:
            raise Conflict("התמלול עודכן במקום אחר. רעננו לפני השמירה.")
        transcript = transcript.model_copy(update={"redaction_reviewed": False})
        return self.repo.add_transcript(user, visit_id, transcript.model_dump(), "edited", parent_id)

    def redact(self, user, visit_id):
        row, transcript = self.transcript(user, visit_id)
        if not transcript:
            raise ValueError("אין תמלול לביקור.")
        if not transcript.roles_confirmed:
            raise ValueError("אשרו תחילה את שיוך הדוברים לרופא ולמטופל.")
        redacted, mapping, findings = redaction.redact_transcript(transcript)
        self.pseudonyms.save(visit_id, {**self.pseudonyms.get(visit_id), **mapping})
        version = self.repo.add_transcript(user, visit_id, redacted.model_copy(update={"redaction_reviewed": False}).model_dump(), "redacted", row["id"])
        return version, findings

    def confirm_redaction(self, user, visit_id, transcript: Transcript, parent_id):
        """Store the manually reviewed redacted text, re-running rules over any new edits."""
        current, _ = self.transcript(user, visit_id)
        if not current or current["id"] != parent_id or current["origin"] != "redacted":
            raise Conflict("יש להריץ השחרה על הגרסה הנוכחית לפני האישור.")
        redacted, mapping, _ = redaction.redact_transcript(transcript)
        self.pseudonyms.save(visit_id, {**self.pseudonyms.get(visit_id), **mapping})
        return self.repo.add_transcript(user, visit_id, redacted.model_copy(update={"redaction_reviewed": True}).model_dump(), "redacted", parent_id)

    def ready_for_extraction(self, transcript: Transcript, origin):
        problems = []
        if not transcript.roles_confirmed:
            problems.append("שיוך הדוברים לא אושר.")
        if origin != "redacted" or not transcript.redaction_reviewed:
            problems.append("בדיקת ההשחרה לא אושרה.")
        unassigned = [s.id for s in transcript.segments if transcript.role_of(s) == "unknown"]
        if unassigned:
            problems.append("מקטעים ללא תפקיד דובר: " + ", ".join(unassigned[:10]))
        return problems

    # ---- drafts ------------------------------------------------------------
    def extract(self, user, visit_id, client, language="Hebrew"):
        row, transcript = self.transcript(user, visit_id)
        if not transcript:
            raise ValueError("אין תמלול לביקור.")
        if problems := self.ready_for_extraction(transcript, row["origin"]):
            raise ValueError(" ".join(problems))
        self.repo.set_state(user, visit_id, "extracting")
        try:
            draft = extract_facts(transcript, client, language)
        except ValueError:
            self.repo.set_state(user, visit_id, "failed", error_category="extraction")
            raise
        return self.repo.add_summary(user, visit_id, "clinician", draft.model_dump(), row["id"], bool(blocking_issues(draft.facts)), "model")

    def save_clinician(self, user, visit_id, draft: ClinicianDraft):
        row, transcript = self.transcript(user, visit_id)
        draft = revalidate(draft, transcript)
        return self.repo.add_summary(user, visit_id, "clinician", draft.model_dump(), row["id"], bool(blocking_issues(draft.facts)), "edited")

    def generate_patient(self, user, visit_id, client, language="Hebrew"):
        clinician = self.repo.current_summary(user, visit_id, "clinician")
        if not clinician:
            raise ValueError("אין טיוטת רופא עדכנית.")
        if clinician["blocking"]:
            raise ValueError("יש לתקן את הבעיות החוסמות בטיוטת הרופא לפני יצירת סיכום למטופל.")
        draft = ClinicianDraft(**clinician["content"])
        summary = patient_summary(draft, client, language)
        return self.repo.add_summary(user, visit_id, "patient", summary.model_dump(), clinician["transcript_version_id"],
                                     bool(blocking_issues(summary.items)), "model", clinician["id"])

    def save_patient(self, user, visit_id, summary: PatientSummary):
        clinician = self.repo.current_summary(user, visit_id, "clinician")
        if not clinician:
            raise Conflict("טיוטת הרופא השתנתה. צרו את סיכום המטופל מחדש.")
        summary = revalidate_patient(summary, ClinicianDraft(**clinician["content"]))
        return self.repo.add_summary(user, visit_id, "patient", summary.model_dump(), clinician["transcript_version_id"],
                                     bool(blocking_issues(summary.items)), "edited", clinician["id"])

    def review_package(self, user, visit_id):
        """Current versions (recorded facts, patient summary, doctor plan) and the hash the doctor signs."""
        c = self.repo.current_summary(user, visit_id, "clinician")
        p = self.repo.current_summary(user, visit_id, "patient")
        if not c or not p:
            return None
        plan = self.repo.current_plan(user, visit_id)
        return {"clinician": c, "patient": p, "plan": plan,
                "sha256": approval_hash(c["content"], p["content"], c["transcript_version_id"], plan["content"] if plan else None)}

    def approve(self, user, visit_id, clinician_version_id, patient_version_id, sha256, username=None, password=None,
                plan_version_id=None, totp_code=None):
        return self.repo.approve(user, visit_id, clinician_version_id, patient_version_id, sha256,
                                 username=username, password=password, plan_version_id=plan_version_id, totp_code=totp_code)

    # ---- retention ---------------------------------------------------------
    def end_session(self, user, visit_id):
        """Delete temporary audio and the identifier mapping for a visit (doctor of the visit only)."""
        self.repo.require_doctor(user, visit_id, "temp_audio_deleted")
        files = 0
        for f in self.tmp.glob(f"{visit_id}-*"):
            f.unlink(missing_ok=True)
            files += 1
        mappings = self.pseudonyms.delete(visit_id)
        self.repo.log_event(user, "temp_audio_deleted", record_type="visit", record_id=visit_id, files=files, reason="session_end")
        self.repo.log_event(user, "pseudonym_mapping_deleted", record_type="visit", record_id=visit_id, mappings=mappings)

    def cleanup_stale(self, hours=None):
        return retention.purge(self.repo, self.pseudonyms, self.tmp, hours)
