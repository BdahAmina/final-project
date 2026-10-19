# Deployment and operations

Built to comply with the HIPAA Security Rule and 21 CFR Part 11 (`data/documents/COMPLIANCE_MATRIX.md`). The
operating organization completes compliance by following `data/documents/compliance/` (policies, risk analysis,
validation). It is not a certified medical device.

## Before first use (compliance checklist)

1. Start the app once: the data key is created in `data/keys/data.key` (Windows DPAPI) and existing data is encrypted
   by migration 0003, after an automatic backup.
2. Escrow the key: `..\venv\Scripts\python.exe -m backend.manage export-key --out E:\key.txt`, store it offline
   (safe, two custodians), delete the file. Without it, encrypted data and backups cannot be recovered.
3. Create the administrators (two recommended, for two-person MFA resets) and set up their authenticator apps at
   first login. Doctors set up theirs at first login too.
4. Schedule the daily backup (Task Scheduler): `..\venv\Scripts\python.exe -m backend.manage backup --out <folder>`.
5. Run the validation: `..\venv\Scripts\python.exe -m tools.validate --executed-by "<name>"`, then execute PQ and
   sign the report (`data/documents/compliance/VALIDATION_PLAN.md`).
6. Adopt and sign `SECURITY_POLICIES.md`, `SIGNATURE_POLICY.md` and `RISK_ANALYSIS.md`.

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
2. If the service runs under another Windows account, set `DATA_ENCRYPTION_KEY` (the escrowed key) in the service
   environment, not in the project folder.
3. Restrict the data folder to the service account:
   `icacls data /inheritance:r /grant:r "<service-account>:(OI)(CI)M" "Administrators:(OI)(CI)F"`
4. Use full-disk encryption (BitLocker) on the machine holding `data/` as well; the app already encrypts clinical
   content and names inside the database.

## Backups and restore

- Automatic: a copy is written to `data/database/backups/` before every migration.
- Daily (Task Scheduler): `..\venv\Scripts\python.exe -m backend.manage backup --out D:\backup` copies both databases
  while the app runs and checks each copy (integrity, test decryption, audit chain). Contents stay encrypted. Take a
  weekly copy off-site (policy §9 in `data/documents/compliance/SECURITY_POLICIES.md`).
- Restore test (monthly): `..\venv\Scripts\python.exe -m backend.manage verify-backup D:\backup\<file>.sqlite`.
- Copies made before encryption at rest (the pre-0003 migration backup, legacy files in `data/runtime/`) are sealed as
  whole encrypted files (`*.enc`) automatically or with `python -m backend.manage seal-plaintext`. To use one, run
  `python -m backend.manage unseal <file>.enc --out <file>.sqlite` and delete the decrypted file afterwards.
- Restore: stop the app, replace `data/database/visits.sqlite` with the backup, make sure the data key is available
  (`data/keys/data.key` under the same Windows account, or `DATA_ENCRYPTION_KEY` set to the escrowed key), start the
  app, run `python -m backend.manage verify-audit`.

## Retention

- Temporary audio and pseudonym mappings: `TMP_AUDIO_RETENTION_HOURS` (default 24), purged at start-up and by
  `python -m backend.manage purge` (schedule it with Task Scheduler for long-running servers).
- Clinical records and audit events: never deleted by the application. The retention period is set by your
  organisation and applicable law; deleting records is a manual, documented procedure outside the app.
