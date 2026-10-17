"""Single-threaded background worker for long-running visit jobs.

Job status is persisted in SQLite so the UI/API can poll it. Temporary audio
is deleted when its transcription job finishes, whether it succeeded or not.
Logs contain only job ids, kinds and error categories.
"""
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from backend.llm_client import LLMError, OllamaClient
from backend.repository import Conflict
from backend.transcriber import AudioInputError

LOG = logging.getLogger(__name__)


class Worker:
    def __init__(self, pipeline, client_factory=OllamaClient):
        self.pipeline = pipeline
        self.client_factory = client_factory
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="visit-worker")

    def submit(self, user, visit_id, kind, **params):
        job_id = self.pipeline.repo.add_job(user, visit_id, kind)
        self.pool.submit(self._run, job_id, user, visit_id, kind, params)
        return job_id

    def _run(self, job_id, user, visit_id, kind, params):
        repo = self.pipeline.repo
        repo.update_job(job_id, "running", attempt=True)
        try:
            if kind == "transcribe":
                path = Path(params["path"])
                try:
                    self.pipeline.transcribe(user, visit_id, path.read_bytes(), path.name, params.get("language"),
                                             params.get("model", "small"), source=params.get("source", "upload"),
                                             progress=lambda stage: repo.update_job(job_id, "running:" + stage))
                finally:
                    self.pipeline.delete_audio(user, visit_id, path)
            elif kind == "extract":
                self.pipeline.extract(user, visit_id, self.client_factory(), params.get("language", "Hebrew"))
            elif kind == "patient_summary":
                self.pipeline.generate_patient(user, visit_id, self.client_factory(), params.get("language", "Hebrew"))
            else:
                raise ValueError("unknown job")
            repo.update_job(job_id, "done")
        except Exception as exc:
            category = ("input" if isinstance(exc, AudioInputError) else "model" if isinstance(exc, LLMError)
                        else "conflict" if isinstance(exc, Conflict) else "processing" if isinstance(exc, ValueError) else "internal")
            LOG.warning("Job failed: job=%s kind=%s category=%s error=%s", job_id, kind, category, type(exc).__name__)
            repo.update_job(job_id, "failed", error_category=category)
            if category == "internal":
                try:
                    repo.set_state(user, visit_id, "failed", error_category="internal")
                except Exception:
                    pass
