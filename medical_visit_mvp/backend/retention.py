"""Retention policy for temporary data that must not live in the database.

- Temporary audio (data/runtime/tmp_audio/): written only by the API upload path, deleted when its
  transcription job ends (success or failure), when the doctor ends the visit session, and in any
  case after TMP_AUDIO_RETENTION_HOURS (default 24) by purge(), which runs at API/UI start-up and
  via `python -m backend.manage purge`. The Streamlit screen never writes audio to disk.
- Pseudonym mappings (data/database/pseudonyms.sqlite): same lifetime as temporary audio.
- Clinical records and audit events are NOT deleted by the application. Their retention period is
  an organizational/legal decision (see data/documents/COMPLIANCE_MATRIX.md).
Every deletion is recorded as an audit event (counts and visit ids only).
"""
import os
import time
from pathlib import Path

TMP_AUDIO_RETENTION_HOURS = float(os.getenv("TMP_AUDIO_RETENTION_HOURS", "24"))


def purge(repo, pseudonyms, tmp_dir: Path, hours: float = None) -> dict:
    hours = TMP_AUDIO_RETENTION_HOURS if hours is None else hours
    cutoff = time.time() - hours * 3600
    removed = 0
    tmp_dir = Path(tmp_dir)
    if tmp_dir.exists():
        for f in tmp_dir.iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
                removed += 1
    mappings = pseudonyms.purge_older_than(hours)
    if removed or mappings:
        repo.log_event(None, "retention_purge", details_files=removed, details_mappings=mappings, hours=hours)
    return {"audio_files": removed, "mappings": mappings}
