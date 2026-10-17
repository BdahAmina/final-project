# HIPAA and 21 CFR Part 11 — applicability and requirements matrix

**Status: no compliance or certification is claimed.** This document maps regulatory provisions to controls that exist
in the code, the tests that exercise them, and the gaps that remain. It was prepared by the developer, not by a
compliance officer, lawyer or auditor. An appropriate assessment is required before any claim of compliance.

Sources consulted (official text, retrieved 2026-10-16/17):
- 45 CFR 164.312 and 164.308/164.316 via the eCFR API, "up to date as of 2026-10-14" — https://www.ecfr.gov/current/title-45/subtitle-A/subchapter-C/part-164/subpart-C
- 21 CFR Part 11 via the eCFR API, "up to date as of 2026-10-14" — https://www.ecfr.gov/current/title-21/chapter-I/subchapter-A/part-11
- HHS, *HIPAA Security Rule NPRM* (published 2025-01-06, 90 FR 898). No final rule was found as of 2026-10; HHS states the current Security Rule remains in effect — https://www.hhs.gov/hipaa/for-professionals/security/hipaa-security-rule-nprm/index.html
- FDA, *Part 11, Electronic Records; Electronic Signatures — Scope and Application* (2003) — https://www.fda.gov/regulatory-information/search-fda-guidance-documents/part-11-electronic-records-electronic-signatures-scope-and-application
- FDA, *Electronic Systems, Electronic Records, and Electronic Signatures in Clinical Investigations: Q&A* (Oct 2024, Rev. 1) — https://www.fda.gov/media/166215/download
- FDA, *Clinical Decision Support Software*, final guidance issued 2026-01-29 (supersedes 2026-01-06), docket FDA-2017-D-6569 — https://www.fda.gov/regulatory-information/search-fda-guidance-documents/clinical-decision-support-software

## 1. Applicability to this project

| Framework | Applies to this prototype? | Reasoning |
|---|---|---|
| HIPAA Privacy/Security Rules | **No, as operated** | HIPAA binds covered entities and business associates handling PHI. This is an educational prototype developed and evaluated with fictional patients; it holds no PHI. If a U.S. covered entity deployed it with real patients, the Security Rule would apply to that entity's whole environment, of which this software is one part. |
| 21 CFR Part 11 | **No, as operated** | Part 11 applies to records required by FDA predicate rules or submitted to FDA (§11.1(b)). Routine clinical visit documentation is not such a record. It could apply if the system were used as a source system in an FDA-regulated clinical investigation. FDA interprets Part 11 narrowly and exercises enforcement discretion on validation, audit trail, record retention and copies (2003 guidance); access limits, operational/authority/device checks and all electronic-signature provisions remain enforced. |
| FDA device rules for the clinical assistant | **Not determined** | The assistant is designed toward the non-device CDS criteria in FD&C Act §520(o)(1)(E) as interpreted in the 2026 CDS guidance (doctor-only, shows its basis, the doctor decides). Whether it is a device is a regulatory determination that has not been made. See `CLINICAL_ASSISTANT.md`. |
| Israeli law (likely actual jurisdiction) | **Not assessed** | Hebrew/Arabic use suggests Israel, where the Privacy Protection Law 5741-1981, the Privacy Protection (Data Security) Regulations 5777-2017 and the Patient's Rights Law 5756-1996 are the relevant frameworks. Neither HIPAA nor Part 11 replaces them. |

The controls below were implemented as good practice and as preparation for such an assessment, not because the
regulations currently bind the prototype.

Status legend: **Implemented** (code + automated test), **Partial**, **Gap** (not implemented), **Organizational**
(cannot be met by software alone), **N/A**.

## 2. HIPAA Security Rule — technical safeguards (45 CFR 164.312)

| Provision | Status | Implemented control | Evidence (tests) | Remaining gap |
|---|---|---|---|---|
| (a)(1) Access control | Implemented | Role-based access (admin/doctor/patient) plus per-record checks in `Repository._visit`: doctor must be the visit doctor **and** hold an active care assignment; patient only own approved content; admin no clinical content. Denials raise NotFound and are audited. | `test_storage.AccessTests.*`, `test_api.test_role_restrictions_admin_has_no_clinical_access`, `test_api.test_cross_user_access_and_protected_exports`, `test_assistant.ContextIsolationTests.test_assistant_requires_authorized_doctor` | No attribute-based rules (e.g. clinic/shift); assignment review is manual. |
| (a)(2)(i) Unique user identification (Required) | Implemented | Unique usernames (primary key, never deleted, so never reassigned); no shared or demo accounts; no self-registration. | `test_storage.AccountTests.test_only_admin_manages_users_and_roles_no_self_elevation` | — |
| (a)(2)(ii) Emergency access procedure (Required) | Partial | An admin can grant a care assignment with a mandatory reason (audited, revocable) — the documented emergency path. | `test_storage.AccessTests.test_revoking_care_assignment_removes_doctor_access` | Written emergency procedure, after-hours admin availability and post-event review are organizational. No self-service break-glass. |
| (a)(2)(iii) Automatic logoff (Addressable) | Implemented | Server-side session idle timeout (15 min) and absolute expiry (8 h), configurable; UI returns to login. | `test_storage.AccountTests.test_sessions_expire_idle_and_absolute_and_logout`, `test_pipeline.InterfaceTests.test_session_expiry_returns_to_login` | — |
| (a)(2)(iv) Encryption and decryption (Addressable) | Gap | None at application level. Deployment guide requires full-disk encryption and encrypted backups. | — | SQLite files and backups are plaintext to anyone with file access. Consider SQLCipher or an encrypted volume; document the addressable decision. |
| (b) Audit controls | Implemented | `audit_events`: login success/failure/lockout, logout, session expiry, user/role/assignment changes, visit creation, clinical record access (once per session per version), transcription, edits, summary generation, assistant requests/results/decisions, signatures, approval invalidation, exports, temporary-data deletion. Admin viewer with filters/pagination; CSV/JSON export. No passwords, tokens, audio, transcripts or prompts. | `test_storage.AuditTests.*`, `test_api.test_admin_audit_viewer_filters_paginates_and_exports`, `test_assistant.ReviewAndStalenessTests.test_audit_has_request_and_result_without_clinical_text` | No automated review or alerting; audit trail lives in the same SQLite file as the data. |
| (c)(1) Integrity | Implemented | Append-only versioning of transcripts, summaries, context and plans; no in-place edits; approvals revoked in the same transaction as any change; audit UPDATE/DELETE blocked by triggers. | `test_storage.VersionTests.*` | Someone with direct file access can still alter rows. |
| (c)(2) Mechanism to authenticate ePHI (Addressable) | Partial | SHA-256 over signed versions, recomputed on every read/export; HMAC-SHA-256 hash chain over audit events (with `AUDIT_HMAC_KEY`). | `test_storage.AuditTests.test_audit_is_append_only_and_chain_detects_tampering`, `test_storage.VersionTests.test_approval_void_if_newer_version_bypasses_revocation` | Unsigned (non-approved) versions are not hashed; without `AUDIT_HMAC_KEY` the chain is unkeyed and can be recomputed by an attacker. |
| (d) Person or entity authentication | Partial | PBKDF2-SHA256 (600k iterations), password policy, lockout after 5 failures, forced change of admin-issued temporary passwords, password aging. | `test_storage.AccountTests.*`, `test_api.test_login_logout_lockout_and_correlation_id`, `test_api.test_temporary_password_must_be_changed_first` | No multi-factor authentication. |
| (e)(1) Transmission security / (e)(2)(i) integrity / (e)(2)(ii) encryption (Addressable) | Gap (in-app) | Processes bind to 127.0.0.1; `deployment/Caddyfile.example` provides HTTPS for use beyond one computer. No cloud transmission: all models run locally (the former cloud provider was removed). | `test_pipeline.LocalOnlyTests.*` | No TLS in the application itself; HTTPS deployment is the operator's responsibility. |

## 3. HIPAA — administrative, physical and privacy provisions (selected)

| Provision | Status | Note |
|---|---|---|
| 164.308(a)(1)(ii)(A)-(B) Risk analysis and risk management (Required) | Organizational / Gap | Not performed. A documented risk analysis is a prerequisite for any deployment with real data. |
| 164.308(a)(2) Assigned security responsibility | Organizational | No security official designated. |
| 164.308(a)(3)-(4) Workforce security, information access management | Partial | Software supports role and assignment management (admin UI, audited). Authorization and termination procedures are organizational. |
| 164.308(a)(5) Security awareness and training | Organizational | Not provided. |
| 164.308(a)(6) Security incident procedures | Organizational / Gap | Audit trail supports investigation; no incident response plan or alerting. |
| 164.308(a)(7) Contingency plan (backup, disaster recovery) | Partial | Automatic pre-migration backups; online-backup helper and restore steps in `deployment/README.md`. No scheduled or off-site backups, no tested recovery plan. |
| 164.308(b) Business associate contracts | N/A as built | No third-party processor: transcription and models run locally. Ollama model downloads are not PHI flows. |
| 164.310 Physical safeguards | Organizational | Workstation and device controls belong to the operating organization. |
| 164.316(b)(2) Retain required documentation 6 years | Organizational | Applies to policies and procedures documentation, not to medical records. |
| 164.502(b), 164.514(d) Minimum necessary | Partial | Admin has no clinical access; patients see only approved patient-facing content; the assistant loads only the bound visit's reviewed facts and doctor-entered context. Doctors see all versions of their assigned visits. |
| 164.514(b) De-identification | **Not met — by design statement** | The redaction step is pseudonymization with manual review; it is **not** Safe Harbor or Expert Determination de-identification (names without context, voices and free-text dates can remain). Pseudonymized transcripts remain PHI. |
| 164.524 Right of access | Partial | Patients can view and export (TXT/PDF/JSON) their approved summary and plan. They cannot access transcripts or full version history through the app; fulfilling access requests for the designated record set is organizational. |
| 164.400-414 Breach notification | Organizational | Audit trail supports scoping; no detection/notification process. |
| Security Rule NPRM (2025) | Not in force | If finalized it would add e.g. MFA, encryption, asset inventory and regular testing requirements; the gaps above (MFA, encryption, TLS) would become more pressing. |

## 4. 21 CFR Part 11

| Provision | Status | Implemented control | Evidence (tests) | Remaining gap |
|---|---|---|---|---|
| 11.10(a) Validation | Partial | 98 automated tests (all passing on 2026-10-17); real-model evaluation on fictional cases (`data/evaluation`). | `tests/`, `data/evaluation/results/` | No formal validation (requirements specification, risk assessment, IQ/OQ/PQ, traceability matrix, change control). FDA expects a risk-based approach. |
| 11.10(b) Accurate and complete copies (human-readable and electronic) | Implemented | PDF/TXT of approved records with ids, versions, UTC timestamps, approval status, SHA-256 and signature manifestation; JSON export of the complete record with every version, signature and suggestion decision. | `test_api.test_cross_user_access_and_protected_exports`, `test_api.test_edit_after_approval_blocks_export_of_changed_draft` | Copies are not certified copies (no attestation process). |
| 11.10(c) Protection of records for the retention period | Partial | No deletion path for clinical records or audit events; versions are append-only; backups before migrations. | `test_storage.MigrationTests.*` | Retention schedule, scheduled/off-site backups, encryption at rest and long-term format migration are organizational/gaps. |
| 11.10(d) Limiting system access | Implemented | See HIPAA (a)(1). | as above | — |
| 11.10(e) Secure, computer-generated, time-stamped audit trail; changes do not obscure previous information; retained | Implemented (with limits) | Every create/modify/delete-type action is audited automatically with UTC timestamp; previous versions are kept as rows, never overwritten; audit is append-only (triggers + hash chain) and never deleted; admin export for review/copying. | `test_storage.AuditTests.*`, `test_storage.VersionTests.*` | Timestamps come from the server clock (no trusted time source). An ordinary SQLite file is **not** tamper-proof: a person with file access can drop triggers and, without the HMAC key, rewrite the chain. |
| 11.10(f) Operational system checks (sequencing) | Implemented | Roles confirmed → redaction reviewed → extraction → patient summary from the current clinician draft → signature over current versions only; stale plan items block signing. | `test_components.PipelineTests.test_end_to_end_with_review_gates_and_patient_view`, `test_assistant.ReviewAndStalenessTests.test_patient_fact_change_marks_suggestions_and_plan_stale` | — |
| 11.10(g) Authority checks | Implemented | Role and care-assignment checks on every record lookup; signing requires the signer's own credentials; only admins manage accounts. | `test_storage.AccessTests.*`, `test_storage.VersionTests.test_signature_requires_own_credentials_and_exact_version` | — |
| 11.10(h) Device checks | Gap / N/A | Not implemented (no terminal or input-device validation). | — | Organizational decision whether needed. |
| 11.10(i) Education, training, experience | Organizational | — | — | Training records. |
| 11.10(j) Written policies holding signers accountable | Organizational | The signing screen states the meaning and that the signature is recorded. | — | Signed policy acknowledgement by each signer. |
| 11.10(k) Controls over systems documentation | Gap | Documentation is in the project folder. | — | The project folder is **not under version control** (no Git repository was found); change control requires one. |
| 11.30 Controls for open systems | N/A (closed system on localhost) | — | — | If exposed over a network: TLS, document encryption, digital signatures. |
| 11.50 Signature manifestations | Implemented | Printed name, UTC date/time and meaning on screen, in TXT/PDF and in JSON; stored with the same controls as the record. | `test_storage.VersionTests.test_signature_requires_own_credentials_and_exact_version`, `test_api.test_cross_user_access_and_protected_exports` | — |
| 11.70 Signature/record linking | Partial | Each signature stores the SHA-256 of the exact signed versions; any later version revokes it; reads/exports recompute the hash, so a signature cannot be moved to other content by ordinary means. | `test_storage.VersionTests.*`, `test_api.test_edit_after_approval_blocks_export_of_changed_draft` | Not a cryptographic (private-key) signature: someone with database write access could recompute a hash. |
| 11.100(a) Unique to one individual, not reused or reassigned | Implemented | Usernames are permanent primary keys; deactivated accounts keep their id. | `test_storage.AccountTests.*` | — |
| 11.100(b) Verify identity before issuing | Organizational | Admin screen instructs to verify identity before creating an account. | — | Documented identity-proofing procedure. |
| 11.100(c) Certification to FDA | Organizational / N/A | — | — | Only if Part 11 applies. |
| 11.200(a)(1) Two distinct components; (i) series in one session; (ii) separate sessions | Implemented | Every signing requires re-entry of username **and** password (satisfies both (i) and (ii)). | `test_storage.VersionTests.test_signature_requires_own_credentials_and_exact_version`, `test_pipeline.InterfaceTests.test_full_workflow_with_signature_patient_view_and_revocation` | — |
| 11.200(a)(2)-(3) Used only by genuine owner; misuse requires collaboration of two or more individuals | Partial / Gap | Passwords hashed; admin resets force a change at next login and are audited. | `test_storage.AccountTests.test_lockout_after_failed_logins` | A single admin can reset a password and log in as the user before they notice; a two-person reset procedure or MFA is needed. |
| 11.300(a) Uniqueness of ID/password combination | Implemented | Unique usernames. | as above | — |
| 11.300(b) Periodic check / revision (aging) | Implemented | `PASSWORD_MAX_AGE_DAYS` (default 180) forces a change at login. | `test_storage.AccountTests.test_password_aging_forces_change` | Periodic account review is organizational. |
| 11.300(c) Loss management | Partial | Admin deactivation and reset end all sessions. | `test_storage.AccountTests.test_deactivated_user_cannot_log_in` | No tokens/cards; procedure is organizational. |
| 11.300(d) Transaction safeguards; detect and urgently report unauthorized attempts | Partial | Lockout after repeated failures; `login` failure and `account_locked` audit events visible to admins. | `test_storage.AccountTests.test_lockout_after_failed_logins` | No immediate notification to a security unit. |
| 11.300(e) Device testing | N/A | No tokens or cards. | — | — |

## 5. "Approval button" vs. electronic signature

The earlier approval checkbox and button were **not** an electronic signature. They are replaced by a signing step
that (1) re-authenticates the signer with both identification components, (2) records printed name, UTC time and
the meaning of the signature, (3) binds them to a hash of the exact versions, (4) shows the manifestation on every
human-readable copy, and (5) is invalidated by any later change. One approval created before this change is kept
as-is and is labelled in exports as a legacy approval without signature components. Even the new mechanism has the
gaps listed for 11.70 and 11.200(a)(3), and the organizational requirements of 11.10(j) and 11.100(b)-(c) are not met.
