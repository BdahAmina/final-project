# Database

## Location

| File | Content |
|---|---|
| `medical_visit_mvp/data/database/visits.sqlite` | Users, sessions, care assignments, visits, transcripts, summaries, patient context, doctor plans, approvals/signatures, assistant conversations and suggestions, audit trail, jobs |
| `medical_visit_mvp/data/database/pseudonyms.sqlite` | Placeholder → original identifier mappings (kept apart, deleted after `TMP_AUDIO_RETENTION_HOURS`) |
| `medical_visit_mvp/data/database/backups/` | Automatic copy written before every schema migration |
| `medical_visit_mvp/data/runtime/tmp_audio/` | Temporary audio for API uploads (never in the database; see `backend/retention.py`) |

Full path on this computer: `C:\Users\amina\Downloads\Medical_Visit_MVP\medical_visit_mvp\data\database\visits.sqlite`.
The whole `data/database/` and `data/runtime/` folders are excluded from Git.

Databases created by earlier versions in `data/runtime/` are copied here automatically on first start
(SQLite online backup + integrity check). The old file is renamed `*.moved-to-data-database-<time>`, never deleted.

## Schema and migrations

SQLAlchemy models: `backend/models.py`. Migrations (Alembic): `backend/migrations/versions/`.

| Revision | Change |
|---|---|
| 0001 | Baseline: schema as created before migrations existed (pre-existing databases are stamped, not rebuilt) |
| 0002 | Admin role, lockout/session fields, care assignments (back-filled from existing visits), e-signature fields on approvals, patient context, doctor plans, assistant tables, append-only audit trail with triggers |

`python -m backend.manage migrate` (also run at every start) applies pending migrations inside one transaction,
after writing a backup, and fails if any foreign-key violation remains. Downgrades are not supported: restore a
backup instead. To add a migration, write a new file in `backend/migrations/versions/` with `down_revision` set to
the current head and update `backend/models.py` to match.

## Tables

| Table | Purpose |
|---|---|
| `users` | Username (id), role (`admin`/`doctor`/`patient`), PBKDF2 password hash, active, lockout counters, password age |
| `login_sessions` | SHA-256 of session tokens, created/last-seen/expiry times |
| `care_assignments` | Which doctor may access which patient (with reason, granted/revoked by) |
| `visits` | One visit: doctor, patient, consent, state |
| `transcript_versions` / `transcript_segments` / `speaker_assignments` | Every transcript version with its segments and speaker→role mapping |
| `summary_versions` | Clinician facts and patient summaries (recorded visit content only) |
| `patient_context_versions` | Doctor-entered age, allergies, medications, pregnancy, renal/hepatic status |
| `plan_versions` | Doctor-authored plan after the visit; items keep assistant provenance |
| `approvals` | Electronic signatures: exact versions, SHA-256, signer name, UTC time, meaning, revocation reason |
| `assistant_conversations` / `assistant_messages` / `assistant_suggestions` | Doctor questions, validated assistant responses, per-suggestion decisions |
| `audit_events` | Append-only, hash-chained audit trail (UPDATE/DELETE blocked by triggers) |
| `jobs` | Background API jobs |
| `alembic_version` | Current schema revision |

## Inspecting the tables

Read-only summary (paths, revision, row counts):

```powershell
cd C:\Users\amina\Downloads\Medical_Visit_MVP\medical_visit_mvp
..\venv\Scripts\python.exe -m backend.manage info
```

Look at a table without risk of writing (opens the file read-only):

```powershell
..\venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect('file:data/database/visits.sqlite?mode=ro', uri=True); print([r[0] for r in c.execute(\"select name from sqlite_master where type='table'\")]); print(c.execute('select seq, ts_utc, actor_id, action, outcome from audit_events order by seq desc limit 10').fetchall())"
```

A GUI such as DB Browser for SQLite can open the file **read-only** (`Open Database Read Only`). Do not edit rows
by hand: the application would not record the change, and edited audit rows break the hash chain
(`python -m backend.manage verify-audit` reports the first altered event).

The admin screen (יומן ביקורת) shows the audit trail with filters and pagination and exports it as CSV or JSON.
