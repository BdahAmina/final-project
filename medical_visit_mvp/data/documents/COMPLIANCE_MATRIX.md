# HIPAA and 21 CFR Part 11 — compliance statement and requirements matrix

## Compliance statement

The Medical Visit system is built to comply with:

- **HIPAA Security Rule** (45 CFR Part 164 Subpart C): every technical safeguard of §164.312 is implemented in the
  software, and the administrative, physical and documentation standards of §§164.308, 164.310 and 164.316, together
  with the Breach Notification Rule (§§164.400–414), are covered by the written policies in
  [`compliance/`](compliance/).
- **21 CFR Part 11** (electronic records; electronic signatures): every control of §§11.10, 11.50, 11.70, 11.100,
  11.200 and 11.300 is implemented in the software, and the procedural controls (§11.10(i)–(k), §11.100(b)–(c)) are
  covered by the signature policy and the validation package in [`compliance/`](compliance/).

Compliance belongs to the organization that operates the system, not to software on its own. A deployment complies
when the operating organization does the following:

1. Adopts the policies in `compliance/SECURITY_POLICIES.md` and `compliance/SIGNATURE_POLICY.md` and names its
   security official.
2. Reviews and signs the risk analysis in `compliance/RISK_ANALYSIS.md` for its own environment.
3. Executes and signs the validation in `compliance/VALIDATION_PLAN.md` (IQ/OQ: `python -m tools.validate`; PQ:
   scripted scenarios).
4. Collects a signed acknowledgement from every signer and sends the §11.100(c) certification letter to FDA when
   Part 11 applies.

Sources (official text, retrieved 2026-10-16/18): 45 CFR 164.302–318 and 21 CFR Part 11 via eCFR (up to date as of
2026-10-14); FDA guidance *Part 11 — Scope and Application* (2003); FDA *Electronic Systems, Electronic Records, and
Electronic Signatures in Clinical Investigations: Q&A* (Oct 2024, Rev. 1); HHS *HIPAA Security Rule NPRM*
(90 FR 898, 2025-01-06; the proposed MFA, encryption and testing requirements are also met here).

Status legend: **Met (software)**: implemented in code and covered by automated tests. **Met (policy)**: covered
by a written procedure in `compliance/` that takes effect when the organization adopts it. **N/A**: not applicable,
with the reason given.

## 1. HIPAA Security Rule — technical safeguards (45 CFR 164.312)

| Provision | Status | Control | Evidence (tests) |
|---|---|---|---|
| (a)(1) Access control | Met (software) | Roles (admin/doctor/patient) plus a per-record check in `Repository._visit`. A doctor must be the visit doctor and hold an active care assignment. A patient sees only their own approved content. The admin sees no clinical content. Denials are audited. | `test_storage.AccessTests.*`, `test_api.test_role_restrictions_admin_has_no_clinical_access`, `test_api.test_cross_user_access_and_protected_exports` |
| (a)(2)(i) Unique user identification | Met (software) | Usernames are permanent primary keys. They are never deleted or reassigned. There are no shared accounts and no self-registration. | `test_storage.AccountTests.test_only_admin_manages_users_and_roles_no_self_elevation` |
| (a)(2)(ii) Emergency access procedure | Met (software + policy) | An admin grants a care assignment with a mandatory reason. It is audited and revocable. Procedure: SECURITY_POLICIES.md §7. | `test_storage.AccessTests.test_revoking_care_assignment_removes_doctor_access` |
| (a)(2)(iii) Automatic logoff | Met (software) | Server-side sessions end after 15 minutes idle and after 8 hours at most. | `test_storage.AccountTests.test_sessions_expire_idle_and_absolute_and_logout` |
| (a)(2)(iv) Encryption and decryption | Met (software) | AES-256-GCM encryption at rest covers every clinical column, every personal name and the pseudonym mappings (`backend/crypto.py`). The 256-bit data key is protected with Windows DPAPI or held in an environment variable. It is escrowed offline (SECURITY_POLICIES.md §9). Backups stay encrypted. | `test_security.EncryptionAtRestTests.*`, `test_security.MigrationEncryptsExistingDataTests` |
| (b) Audit controls | Met (software) | An append-only `audit_events` trail records logins, MFA, sessions, account, role and assignment changes, clinical access, edits, signatures, invalidations, exports, key events and deletions. It holds no passwords, codes or clinical text. Admins have a viewer, CSV/JSON export and a live security-alert panel. | `test_storage.AuditTests.*`, `test_api.test_admin_audit_viewer_filters_paginates_and_exports` |
| (c)(1) Integrity | Met (software) | Every record version is append-only. Approvals are revoked in the same transaction as any change. Database triggers block UPDATE and DELETE on the audit trail. Authenticated encryption (GCM) detects any change to encrypted values. | `test_storage.VersionTests.*`, `test_security.test_wrong_key_cannot_read_and_altered_ciphertext_is_detected` |
| (c)(2) Mechanism to authenticate ePHI | Met (software) | Each signed set of versions carries a SHA-256 hash, recomputed on every read and export, and an Ed25519 signature. The audit chain is HMAC-SHA-256 and is always keyed. GCM tags cover every encrypted value. | `test_security.CryptographicSignatureTests.*`, `test_storage.AuditTests.test_chain_is_always_keyed_and_needs_the_key` |
| (d) Person or entity authentication | Met (software) | Passwords use PBKDF2-SHA256 with 600,000 iterations, a password policy, lockout after 5 failures and password aging. **Multi-factor authentication** (RFC 6238 one-time codes) is required for administrators and doctors at every login and every signature. Codes are single use, and wrong codes count toward the lockout. | `test_security.MfaTests.*`, `test_security.MfaInterfaceTests`, `test_storage.AccountTests.*` |
| (e)(1) Transmission security; (e)(2)(i) integrity controls; (e)(2)(ii) encryption | Met (software + policy) | The processes listen only on 127.0.0.1, so no ePHI crosses a network. Any network deployment must go through the HTTPS reverse proxy (`deployment/Caddyfile.example`, TLS 1.2+). This is mandated in SECURITY_POLICIES.md §10. All models run locally, so there is no cloud transmission. | `test_pipeline.LocalOnlyTests.*` |

## 2. HIPAA — administrative, physical and organizational standards

| Provision | Status | Where |
|---|---|---|
| 164.308(a)(1)(ii)(A)–(B) Risk analysis and risk management | Met (policy) | `compliance/RISK_ANALYSIS.md`: assets, threats, likelihood and impact, controls, residual risk, review cycle |
| 164.308(a)(1)(ii)(C) Sanction policy | Met (policy) | SECURITY_POLICIES.md §3 |
| 164.308(a)(1)(ii)(D) Information system activity review | Met (software + policy) | Security alerts on every admin screen; weekly audit review (SECURITY_POLICIES.md §5) |
| 164.308(a)(2) Assigned security responsibility | Met (policy) | SECURITY_POLICIES.md §1 (named security official) |
| 164.308(a)(3) Workforce security; (a)(4) information access management | Met (software + policy) | Admin-managed roles and care assignments, deactivation ends all sessions; authorization, supervision and termination steps in SECURITY_POLICIES.md §2 |
| 164.308(a)(5) Security awareness and training | Met (policy) | SECURITY_POLICIES.md §4 (training before access, yearly refresher, records kept) |
| 164.308(a)(6) Security incident procedures | Met (software + policy) | Lockouts and failed or denied events surface as alerts; incident response in SECURITY_POLICIES.md §6 |
| 164.308(a)(7) Contingency plan | Met (software + policy) | `manage backup` and `manage verify-backup` (encrypted copies, integrity check, test decryption, audit-chain check); daily schedule, off-site copy, key escrow, recovery test (SECURITY_POLICIES.md §9) |
| 164.308(a)(8) Evaluation | Met (policy) | Yearly re-evaluation and after every significant change (SECURITY_POLICIES.md §11; validation re-run) |
| 164.308(b) Business associate contracts | Met (policy) / N/A as built | No third party processes ePHI: all processing is local. If one is ever added, a BAA is required first (SECURITY_POLICIES.md §12). |
| 164.310 Physical safeguards (facility, workstation, device and media) | Met (policy) | SECURITY_POLICIES.md §8: screen lock, full-disk encryption, media disposal |
| 164.316 Policies, procedures and documentation; 6-year retention | Met (policy) | This folder is under version control (Git); retention and review in SECURITY_POLICIES.md §11 |
| 164.400–414 Breach notification | Met (policy) | SECURITY_POLICIES.md §6.3 (risk assessment, 60-day notification) |
| 164.502(b), 164.514(d) Minimum necessary | Met (software) | Role-scoped views: admin has no clinical access, patients see only approved content, the assistant sees only the bound visit |
| 164.524 Right of access | Met (software + policy) | Patients export TXT, PDF or JSON of their approved record; full-record requests go through SECURITY_POLICIES.md §13 |

Pseudonymization is not de-identification: the redaction step replaces identifiers before any model sees the text,
but pseudonymized transcripts are still treated, encrypted and protected as ePHI. The system never relies on
§164.514(b) de-identification.

## 3. 21 CFR Part 11

| Provision | Status | Control | Evidence |
|---|---|---|---|
| 11.10(a) Validation | Met (software + policy) | `compliance/VALIDATION_PLAN.md`: requirements, risk assessment, traceability matrix, IQ/OQ run by `python -m tools.validate` (dated report in `data/validation/`), PQ protocol, change control | `tools/validate.py`, all tests |
| 11.10(b) Accurate and complete copies | Met (software) | PDF/TXT of the approved record with versions, UTC times, SHA-256 and the full signature manifestation; JSON export of every version, signature, signed manifest and the public signing keys needed to verify them | `test_api.test_cross_user_access_and_protected_exports`, `test_security.test_signing_requires_mfa_code_and_produces_a_verifiable_ed25519_signature` |
| 11.10(c) Protection of records for the retention period | Met (software + policy) | No deletion path for records or audit events; encryption at rest; encrypted, verified daily backups; retention schedule (SECURITY_POLICIES.md §9, §11) | `test_storage.MigrationTests`, `manage verify-backup` |
| 11.10(d) Limiting system access | Met (software) | See HIPAA (a)(1) and (d) | as above |
| 11.10(e) Audit trail | Met (software) | Computer-generated, time-stamped (UTC), append-only, HMAC-chained trail; earlier versions are never overwritten; the trail is kept for the record's life and can be exported for review. **Signatures carry a trusted-time check** (SNTP); a clock off by more than 2 minutes blocks signing. | `test_storage.AuditTests.*`, `test_security.test_clock_skew_blocks_signing` |
| 11.10(f) Operational system checks | Met (software) | Enforced order: roles confirmed, then redaction reviewed, then extraction, then patient summary, then signature on current versions only | `test_components.PipelineTests.test_end_to_end_with_review_gates_and_patient_view` |
| 11.10(g) Authority checks | Met (software) | Role and assignment checks on every lookup; only the signer's own credentials and code can sign | `test_storage.AccessTests.*`, `test_storage.VersionTests.test_signature_requires_own_credentials_and_exact_version` |
| 11.10(h) Device checks | N/A (justified) | FDA expects device checks where only certain devices may be legitimate sources of data or commands. This is a closed, single-site system with no instruments or terminals feeding records. Workstation rules are in SECURITY_POLICIES.md §8. | — |
| 11.10(i) Education, training and experience | Met (policy) | SECURITY_POLICIES.md §4; SIGNATURE_POLICY.md §2 | — |
| 11.10(j) Written policy holding signers accountable | Met (policy) | SIGNATURE_POLICY.md §1 plus a signed acknowledgement form per signer | — |
| 11.10(k) Systems documentation controls | Met (policy) | Documentation and code in Git; change control in VALIDATION_PLAN.md §7 | — |
| 11.30 Open systems | N/A (closed system) | Access is controlled by the organization; if exposed over a network, TLS is mandatory (SECURITY_POLICIES.md §10) and records already carry Ed25519 signatures | — |
| 11.50 Signature manifestation | Met (software) | Printed name, UTC date and time and meaning on screen, in TXT, PDF and JSON, together with algorithm, key fingerprint, MFA and time source | `test_storage.VersionTests.test_signature_requires_own_credentials_and_exact_version` |
| 11.70 Signature/record linking | Met (software) | Each signature is an **Ed25519 digital signature** over a manifest containing the SHA-256 of the exact versions. The private key can be opened only with the signer's password. Any alteration of the record, manifest or signature voids the approval on every read and export. | `test_security.CryptographicSignatureTests.*` |
| 11.100(a) Unique, never reused or reassigned | Met (software) | Permanent usernames | `test_storage.AccountTests.*` |
| 11.100(b) Identity verified before issuing | Met (policy) | SIGNATURE_POLICY.md §3 (identity-proofing checklist) | — |
| 11.100(c) Certification to FDA | Met (policy) | Letter template in SIGNATURE_POLICY.md §4, sent before first use when Part 11 applies | — |
| 11.200(a)(1) Two distinct identification components | Met (software) | Every signing requires re-entering username, password and a one-time code | `test_security.test_signing_requires_mfa_code_and_produces_a_verifiable_ed25519_signature` |
| 11.200(a)(2)–(3) Used only by the owner; misuse needs two or more people | Met (software) | An admin password reset cannot take over an account: the user's MFA device is still required, and the reset retires the signing key. Resetting MFA requires a **second administrator's** password and code. | `test_security.test_mfa_reset_needs_a_second_administrator`, `test_security.test_key_follows_password_change_and_admin_reset_retires_it` |
| 11.300(a) Unique ID/password combination | Met (software) | Unique usernames | as above |
| 11.300(b) Periodic checking and revision | Met (software + policy) | Password aging (180 days); quarterly account review (SECURITY_POLICIES.md §2) | `test_storage.AccountTests.test_password_aging_forces_change` |
| 11.300(c) Loss management | Met (software + policy) | Deactivation, password reset and two-person MFA reset all end sessions; lost-device procedure in SECURITY_POLICIES.md §2.4 | `test_security.MfaTests.*` |
| 11.300(d) Detect and urgently report unauthorized attempts | Met (software + policy) | Lockouts and failed or denied logins, codes and signatures appear immediately on every admin screen; escalation in SECURITY_POLICIES.md §6 | `test_security.test_mfa_reset_needs_a_second_administrator` (alerts) |
| 11.300(e) Testing of tokens and cards | Met (software) | One-time codes follow RFC 6238 and are verified against the standard's reference values | `test_security.TotpTests.test_rfc6238_reference_value` |

## 4. Related determinations

| Topic | Determination |
|---|---|
| FDA device status of the clinical assistant | Designed to the non-device CDS criteria of FD&C Act §520(o)(1)(E) (2026 CDS guidance): doctor-only, shows its basis, the doctor decides. See `CLINICAL_ASSISTANT.md`. |
| Israeli law (Privacy Protection Law 5741-1981, Data Security Regulations 5777-2017, Patient's Rights Law 5756-1996) | The same controls serve these regulations; a local legal review is still recommended before use with real patients. |
