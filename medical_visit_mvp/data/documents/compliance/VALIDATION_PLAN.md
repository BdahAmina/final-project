# Validation plan (21 CFR 11.10(a), 11.10(k))

Risk-based computer system validation, following FDA's *General Principles of Software Validation* and the
2024 Q&A on electronic systems in clinical investigations. The validation is complete when:
- IQ and OQ have passed, which `python -m tools.validate` executes and records;
- PQ has been executed;
- the report has been signed (§8).

## 1. Intended use

Recording a medical visit (audio or pasted text), local transcription, doctor review, drafting of a clinician
summary and a plain-language patient summary, a doctor plan, an electronic signature of approval, and patient
access to the approved record. An optional clinical assistant helps the doctor only; it never writes to the record.

## 2. User requirements (URS) and risk

| ID | Requirement | Risk | Verified by (automated, OQ) |
|---|---|---|---|
| R1 | Only authorized users get access; roles and care assignments are checked on every record | High | `test_storage.AccessTests.*`, `test_api.test_role_restrictions_admin_has_no_clinical_access`, `test_api.test_cross_user_access_and_protected_exports` |
| R2 | Unique accounts; password policy, lockout, aging, automatic logoff | High | `test_storage.AccountTests.*`, `test_api.test_login_logout_lockout_and_correlation_id` |
| R3 | MFA for administrators and doctors at login and signing; codes single use | High | `test_security.MfaTests.*`, `test_security.MfaInterfaceTests`, `test_security.TotpTests` |
| R4 | Clinical data and names encrypted at rest; tampering detected | High | `test_security.EncryptionAtRestTests.*`, `test_security.MigrationEncryptsExistingDataTests` |
| R5 | Secure, time-stamped, append-only audit trail of every create, modify, access, sign and export action, with no clinical text | High | `test_storage.AuditTests.*`, `test_api.test_admin_audit_viewer_filters_paginates_and_exports` |
| R6 | Records are versioned; earlier versions are never overwritten | High | `test_storage.VersionTests.test_segments_and_speakers_are_rows_and_versions_are_kept` |
| R7 | Signature requires username, password and code; manifests name, time and meaning; is Ed25519-signed and bound to the exact versions; any change voids it | High | `test_security.CryptographicSignatureTests.*`, `test_storage.VersionTests.test_signature_requires_own_credentials_and_exact_version`, `test_storage.VersionTests.test_approval_void_if_newer_version_bypasses_revocation` |
| R8 | Signing is blocked when the clock is off from trusted time | Medium | `test_security.test_clock_skew_blocks_signing` |
| R9 | Enforced workflow order (roles, then redaction, then extraction, then patient summary, then signature) | High | `test_components.PipelineTests.test_end_to_end_with_review_gates_and_patient_view` |
| R10 | Extracted facts must be supported by verbatim quotes; no invented doses | High | `test_components.ValidatorTests.*` |
| R11 | Accurate and complete copies (TXT, PDF, JSON with signatures and keys) | Medium | `test_api.test_cross_user_access_and_protected_exports`, `test_api.test_edit_after_approval_blocks_export_of_changed_draft` |
| R12 | Patients see only their own approved content | High | `test_storage.AccessTests.test_patient_sees_only_own_approved_content` |
| R13 | Temporary audio and identifier mappings are deleted on schedule | Medium | `test_storage.AudioRetentionTests.*`, `test_components.PipelineTests.test_session_end_deletes_temp_audio_and_mapping` |
| R14 | No single administrator can take over an account | High | `test_security.test_mfa_reset_needs_a_second_administrator`, `test_security.test_key_follows_password_change_and_admin_reset_retires_it` |
| R15 | Schema changes keep data and are backed up first | Medium | `test_storage.MigrationTests.*` |
| R16 | The clinical assistant answers only about the current visit and cannot change the record | Medium | `test_assistant.*`, `test_api.test_assistant_endpoints_are_doctor_and_visit_bound` |
| R17 | Interface in Hebrew, Arabic and English; record content is never machine-translated | Low | `test_i18n.*` |

## 3. Installation Qualification (IQ)

`python -m tools.validate` checks and records:
- Python 3.11 or newer, and the minimum package versions from requirements.txt;
- the database at the head schema revision;
- an intact audit chain;
- the data key is available and protected;
- the system clock is within tolerance of trusted time;
- the SHA-256 manifest of every source file.

The manifest identifies exactly what was validated. A later report with a different manifest shows the change.

## 4. Operational Qualification (OQ)

The same command runs the whole automated suite and lists every test case with its result. The suite covers R1–R17
above. **Acceptance:** 0 failures and 0 errors.

## 5. Performance Qualification (PQ)

PQ is executed by a doctor and the security official on the production computer, with fictional patients, and
recorded on this sheet.

| # | Scenario | Expected result | Pass / initials |
|---|---|---|---|
| P1 | Administrator signs in (password and code) and creates a doctor | Doctor must change the password and set up MFA at first login | |
| P2 | Doctor adds a patient and creates a visit with consent | Patient appears assigned to this doctor only | |
| P3 | Record 1–2 minutes of a role-played visit, then transcribe | Transcript shown for review; audio not kept afterwards | |
| P4 | Confirm speakers, run and confirm redaction, extract, create the patient summary | Each step is unlocked only after the previous one | |
| P5 | Sign with a wrong code, then the right one | Wrong code refused and an alert shown to the administrator; the right code signs; the screen shows "Ed25519 verified" | |
| P6 | Edit the transcript after signing | Approval revoked with a reason; the patient no longer sees it | |
| P7 | Patient signs in and downloads the PDF | PDF shows name, UTC time, meaning, algorithm, key fingerprint and time source | |
| P8 | Run `manage backup`, then `manage verify-backup` on the copy | "ok: integrity, … decrypted, audit chain … verified" | |
| P9 | Five wrong passwords for one account | Account locked; the alert shows on the admin screen | |
| P10 | Two administrators reset a doctor's MFA | Works only with the second administrator's password and code | |

## 6. Traceability

Every regulatory provision maps to requirements and tests in `../COMPLIANCE_MATRIX.md`. Every requirement maps to
tests in §2 above. Every test result is listed in the dated validation report in `data/validation/`.

## 7. Change control

1. Every change is made on a Git branch with a description of what changed and why.
2. Impact assessment: which requirements (R1–R17) are affected. If the change touches a High-risk requirement, the
   security official approves it before release.
3. Tests are added or updated for the change; the full suite must pass.
4. `python -m tools.validate --executed-by "<name>"` is run on the target computer. The new report is reviewed and
   signed. PQ scenarios touching changed functions are repeated.
5. A schema change comes with an Alembic migration (a backup is taken automatically) and a restore test.
6. Reports and approvals are kept for 6 years.

## 8. Approval of this validation

| Role | Name | Signature | Date |
|---|---|---|---|
| Executed by | | | |
| Reviewed by (security official) | | | |
| Approved for use (system owner) | | | |
