# Implementation status

Status values: **implemented** (code + automated test), **partial**, **missing**, **unverified**
(code exists but is not verified beyond mocks), **blocked** (needs an external resource).

## Baseline before this stage (2026-10-16)

Assessed from the repository before changes in this stage.

| # | Requirement | Baseline status | Evidence |
|---|---|---|---|
| 1 | Core workflow (record/upload → transcribe → speakers → summary → approval → patient summary → export) | implemented | `backend/pipeline.py`, `design/ui.py`, `tests/test_pipeline.py`, `tests/test_api.py` |
| 1 | AI suggestions separated from visit facts | missing | no assistant existed |
| 2 | SQLite + SQLAlchemy, users/visits/segments/speakers/summaries/approvals | implemented | `backend/repository.py` |
| 2 | Database under `data/database/` | missing | was `data/runtime/visits.sqlite` |
| 2 | Migrations | missing | `create_all` only |
| 2 | Assistant conversations, audit events tables | missing | |
| 2 | Temporary audio retention policy | partial | deleted after job / session end / 24 h, not configurable or audited |
| 3 | Login, hashed passwords, sessions | partial | PBKDF2 + DB sessions for API; UI kept user id in Streamlit session without expiry |
| 3 | Admin role, no clinical access for admin | missing | roles were doctor/patient only |
| 3 | Session expiry / idle logoff in UI, lockout | missing | |
| 4 | Audit trail | missing | |
| 5 | PDF export of approved summaries | implemented | `backend/export.py` |
| 5 | JSON export of records and versions, audit export | missing | |
| 5 | Version-bound approval and invalidation | implemented | `Repository.approve`, `tests/test_storage.py` |
| 5 | Electronic signature (re-authentication, meaning, manifestation) | missing | approval was a button + checkbox |
| 6 | Doctor-only clinical assistant with retrieval, citations, safety checks | missing | |
| 7 | Identifier redaction with manual review | implemented | `backend/redaction.py`, review step in UI |
| 7 | Local processing default | partial | an optional cloud provider existed (removed in this stage at user request) |
| 8 | Folder layout (`data/database`, `data/knowledge`, `data/samples`, `data/evaluation`, `deployment/`) | missing | samples had been moved to a backup at the user's earlier request |
| 9 | Tests listed in requirement 9 | partial | storage/access/approval tests existed; no assistant, audit, admin tests |
| 10 | Status document, proposal update, compliance matrix | missing | |

The section below is the current status and is updated at the end of each stage.

## Current status (2026-10-17, after stages A–H)

Stages: A remove cloud provider · B database location + migrations + models · C accounts, roles, sessions ·
D audit trail · E signatures, versions, exports · F knowledge base + clinical assistant · G UI · H docs, deployment,
evaluation. Test suite: `..\venv\Scripts\python.exe -m unittest discover -s tests` → **98 tests, all passing**
(2026-10-17). Real-model evaluation: see row 9 and `data/evaluation/results/`.

| # | Requirement | Status | Files | Verification evidence |
|---|---|---|---|---|
| 1 | Core workflow preserved (mic/upload → transcription → speakers/correction → structured summary → doctor review → patient summary → export) | implemented | `backend/pipeline.py`, `design/doctor.py` | `test_pipeline.InterfaceTests.test_full_workflow_with_signature_patient_view_and_revocation`, `test_components.PipelineTests.*`, `test_api.*` |
| 1 | AI suggestions never appear as visit facts | implemented | `backend/models.py` (`PlanVersion` separate from `SummaryVersion`), `backend/repository.py` (`decide_suggestion`) | `test_assistant.ReviewAndStalenessTests.test_suggestions_never_change_recorded_visit_facts`, `…test_patient_sees_plan_only_after_doctor_approval_and_labelled_as_plan` |
| 2 | Reuse existing SQLite/SQLAlchemy; persist users, roles, visits, segments, speakers, summaries, approvals, assistant conversations, audit events | implemented | `backend/models.py`, `backend/repository.py` | `test_storage.*` |
| 2 | Database under `data/database/` | implemented | `backend/db.py` | Real database relocated 2026-10-17 (old file renamed, kept); `test_storage.MigrationTests` |
| 2 | Migrations; no data deleted | implemented | `backend/migrations/` (Alembic 0001 baseline, 0002) | Real DB migrated with all rows kept (2 users, 2 visits, 11 transcripts, 92 segments, 3 summaries, 1 approval) and pre-migration backup; legacy approval hash still verifies; `test_storage.MigrationTests` |
| 2 | Document DB path and inspection | implemented | `data/documents/DATABASE.md`, `python -m backend.manage info` | Command run on the real DB |
| 2 | Temporary audio outside DB with retention/deletion | implemented | `backend/retention.py`, `backend/pipeline.py`, `backend/worker.py` | `test_storage.AudioRetentionTests.*`, `test_api.test_audio_upload_runs_local_job_and_removes_temp_file` |
| 3 | Login, logout, hashed passwords, protected sessions, expiry (idle + absolute), lockout | implemented | `backend/security.py`, `backend/repository.py`, `design/ui.py`, `backend/api.py` | `test_storage.AccountTests.*`, `test_api.test_login_logout_lockout_and_correlation_id`, `test_pipeline.InterfaceTests.test_session_expiry_returns_to_login` |
| 3 | Three roles; admin without clinical access; no self-registration or self-elevation; no hardcoded credentials | implemented | `backend/repository.py`, `backend/manage.py` (`create-admin`), `design/admin.py` | `test_storage.AccessTests.test_admin_has_no_clinical_access`, `AccountTests.test_only_admin_manages_users_and_roles_no_self_elevation`, `test_api.test_role_restrictions_admin_has_no_clinical_access`, `test_pipeline.InterfaceTests.test_first_run_has_no_self_registration` |
| 3 | Enforcement on every lookup incl. downloads and assistant | implemented | `Repository._clinical` / `_visit` | `test_storage.AccessTests.*`, `test_api.test_cross_user_access_and_protected_exports`, `test_api.test_assistant_endpoints_are_doctor_and_visit_bound` |
| 3 | MFA | missing | — | Not implemented (gap in compliance matrix) |
| 4 | Audit events for all listed actions with required fields; no secrets/clinical text | implemented | `backend/audit.py`, `backend/repository.py` | `test_storage.AuditTests.test_events_have_required_fields_and_no_secrets_or_clinical_text`, `test_assistant.ReviewAndStalenessTests.test_audit_has_request_and_result_without_clinical_text` |
| 4 | Append-only for ordinary users; tamper evidence | implemented (not tamper-proof) | triggers in migration 0002, hash chain + optional `AUDIT_HMAC_KEY` | `test_storage.AuditTests.test_audit_is_append_only_and_chain_detects_tampering`, `…test_ordinary_users_cannot_read_export_or_verify_audit` |
| 4 | Admin audit viewer with filtering and pagination | implemented | `design/admin.py`, `GET /admin/audit` | `test_api.test_admin_audit_viewer_filters_paginates_and_exports`, `test_pipeline.InterfaceTests.test_admin_console_has_audit_and_no_clinical_content` |
| 5 | PDF of approved summaries with ids/versions/timestamps/status, RTL fonts | implemented | `backend/export.py` | `test_api.test_cross_user_access_and_protected_exports` (PDF bytes + text manifest). Hebrew shaping via uharfbuzz; visual rendering not inspected by a person — **unverified** |
| 5 | JSON export of records/versions; CSV/JSON audit export | implemented | `Repository.record_export`, `audit_export` | `test_api.test_cross_user_access_and_protected_exports`, `test_api.test_admin_audit_viewer_filters_paginates_and_exports` |
| 5 | Edit → new version + invalidation; approval names exact version; no export of changed draft | implemented | `Repository.approve/_live_approval/_revoke` | `test_storage.VersionTests.*`, `test_api.test_edit_after_approval_blocks_export_of_changed_draft` |
| 6 | Doctor-only assistant panel "עוזר קליני לרופא" | implemented | `backend/assistant.py`, `design/assistant_panel.py` | `test_assistant.*` (29), `test_pipeline.InterfaceTests.test_assistant_panel_rejects_unrelated_question` |
| 6A | Patient relevance, binding, reset on switch, rejection, ambiguity | implemented | `rules_relevance`, `assistant_conversation`, `load_context` | `test_assistant.RelevanceTests.*`, `ContextIsolationTests.*` |
| 6B | Reliable context, no invented facts, untrusted data | implemented | `_validate`, `load_context` | `EvidenceTests.test_known_facts_cannot_be_invented`, `ContextIsolationTests.test_injection_inside_visit_text_is_data_and_unsupported_output_removed` |
| 6C | Curated, versioned authoritative sources with metadata; retrieval before generation; validated citations | **partial** | `data/knowledge/`, `tools/knowledge_ingest.py`, `backend/knowledge.py` | 3 FDA labels ingested (104 passages, verbatim, versioned). **Guideline source blocked** (CDC HTTP 403): no clinical guidelines in the collection. `EvidenceTests.*` |
| 6D | Medication safety checks; unknown ≠ negative; no verified-DB claim; no doses; no prescriptions | implemented (label-text based) | `medication_safety` | `MedicationSafetyTests.*`. **Blocked:** no verified drug-interaction database is available, so interaction checks are label-text mentions only |
| 6E | Structured validated response | implemented | `ModelAnswer`, response dict | `EvidenceTests.test_supported_medication_option_is_kept_with_verified_citation` |
| 6F | Draft labelling, accept/edit/reject audited, plan revision with provenance, patient sees plan after approval, staleness | implemented | `decide_suggestion`, `save_plan`, `current_plan` | `ReviewAndStalenessTests.*` |
| 7 | Fictional data for development; redaction with manual review; secrets in env; no sensitive logs; local by default, no silent cloud | implemented | `data/samples/`, `backend/redaction.py`, `.env.example`, `.gitignore` | `test_pipeline.LocalOnlyTests.*`, `test_pipeline.AudioTests.test_unrelated_failure_is_not_retried_and_not_logged_with_content` |
| 7 | HIPAA Security Rule and 21 CFR Part 11 compliance: encryption at rest, MFA, Ed25519 signatures, trusted time, security alerts, verified backups; policies, risk analysis, validation | implemented | `backend/crypto.py`, `backend/repository.py`, `tools/validate.py`, `data/documents/COMPLIANCE_MATRIX.md`, `data/documents/compliance/` | `test_security.*`, `python -m tools.validate` |
| 8 | Folder organization | implemented | `backend/`, `design/`, `data/{database,documents,knowledge,samples,evaluation}`, `tests/`, `deployment/` | — |
| 8 | Docker Compose packaging (from original proposal) | missing | — | Scripts + HTTPS example only |
| 9 | Tests for every item in requirement 9 | implemented | `tests/` | 98 passing; mapping in `CLINICAL_ASSISTANT.md` and `COMPLIANCE_MATRIX.md` |
| 9 | Real-model evaluation on fictional cases | implemented (results below) | `data/evaluation/evaluate_assistant.py` | `data/evaluation/results/assistant_eval_qwen3_8b_*.{json,md}` |
| 9 | Clinical evaluation and qualified review | **missing — pending** | — | See `CLINICAL_ASSISTANT.md` → Evaluation → Pending |
| 10 | Status document, proposal update, compliance matrix | implemented | this file, `PROPOSAL_UPDATE.md` + `Medical_Visit_Project_Proposal_HE_v2.docx`, `COMPLIANCE_MATRIX.md` | — |

Unverified by a person: Hebrew/Arabic PDF visual layout; the Streamlit data-editor interactions (AppTest cannot drive
them; their conversions are unit-tested); microphone recording in a browser after these changes.
