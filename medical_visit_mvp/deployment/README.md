# Deployment and operations

Educational prototype evaluated with **fictional patients**. It is not a certified medical device and has not had a
HIPAA or 21 CFR Part 11 assessment (see `data/documents/COMPLIANCE_MATRIX.md`).

## Single computer (default)

```powershell
cd C:\Users\amina\Downloads\Medical_Visit_MVP\medical_visit_mvp
powershell -ExecutionPolicy Bypass -File deployment\run_ui.ps1     # screen: http://localhost:8501
powershell -ExecutionPolicy Bypass -File deployment\run_api.ps1    # API:    http://127.0.0.1:8000/docs
```

Both bind to `127.0.0.1` only and run `python -m backend.manage migrate` first (a backup is written before any
schema change). Ollama must be running locally (`ollama serve`, model `qwen3:8b`).

## Beyond one computer

1. Put both processes behind HTTPS (`Caddyfile.example`) and restrict network access (VPN or firewall).
2. Set `AUDIT_HMAC_KEY` in the service environment, not in the project folder.
3. Restrict the data folder to the service account:
   `icacls data /inheritance:r /grant:r "<service-account>:(OI)(CI)M" "Administrators:(OI)(CI)F"`
4. Use full-disk encryption (BitLocker) on the machine holding `data/`; SQLite files are not encrypted by the app.

## Backups and restore

- Automatic: a copy is written to `data/database/backups/` before every migration.
- Routine (operator's responsibility): copy `data/database/visits.sqlite` with SQLite's online backup while the app
  runs, for example
  `..\venv\Scripts\python.exe -c "from backend.db import copy_database as c; c('data/database/visits.sqlite', 'D:/backup/visits.sqlite')"`
  and store it encrypted. Test restores. Backups contain clinical data and the audit trail.
- Restore: stop the app, replace `data/database/visits.sqlite` with the backup, start the app, run
  `python -m backend.manage verify-audit`.

## Retention

- Temporary audio and pseudonym mappings: `TMP_AUDIO_RETENTION_HOURS` (default 24), purged at start-up and by
  `python -m backend.manage purge` (schedule it with Task Scheduler for long-running servers).
- Clinical records and audit events: never deleted by the application. The retention period is set by your
  organisation and applicable law; deleting records is a manual, documented procedure outside the app.
