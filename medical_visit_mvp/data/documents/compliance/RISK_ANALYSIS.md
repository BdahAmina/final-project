# Security risk analysis (45 CFR 164.308(a)(1)(ii)(A)–(B))

Method: NIST SP 800-30 style.
- Each risk is the likelihood (1–3) × impact (1–3) of a threat against an asset, before and after controls.
- Residual risk of 4 or more needs a mitigation plan.
- Reviewed yearly and after every significant change.

The operating organization reviews this analysis for its own site and signs it at the end.

## 1. Scope and ePHI inventory

| Asset | Where | ePHI | Protection |
|---|---|---|---|
| Visit database `data/database/visits.sqlite` | Server disk | Transcripts, summaries, plans, patient context, assistant exchanges, names | AES-256-GCM per value, access control, audit |
| Pseudonym database `data/database/pseudonyms.sqlite` | Server disk | Original identifiers behind placeholders | AES-256-GCM, deleted after 24 h or at session end |
| Temporary audio `data/runtime/tmp_audio/` | Server disk | Voice recordings | Deleted after transcription and after at most 24 h |
| Backups | Backup folder and off-site drive | Copies of the above | Remain encrypted; verified on creation |
| Data key `data/keys/data.key` and escrow copy | Server (DPAPI) and safe | Key material | DPAPI (Windows account), offline escrow with two custodians |
| Exports (PDF/TXT/JSON) | User's device after download | Approved records | User responsibility, policy §8 |
| Browser session | Workstation | Displayed records | 15-minute idle logoff, 8-hour maximum |

Data flow: microphone or upload → local transcription (faster-whisper) → review and redaction → local model (Ollama)
→ doctor review → Ed25519 signature → patient view and export. No ePHI leaves the server. The time check sends
only an SNTP request.

## 2. Risk register

L = likelihood, I = impact, R = risk (L × I). The third column gives L/I/R before controls.

| # | Threat → asset | Before controls (L/I/R) | Controls | Residual (L/I/R) |
|---|---|---|---|---|
| 1 | Stolen or copied database file | 2/3/6 | Encryption at rest (key outside the file), full-disk encryption, access-controlled server | 1/2/2 |
| 2 | Stolen password (phishing, reuse) | 3/3/9 | MFA for admins and doctors, lockout, password policy, training | 1/3/3 |
| 3 | Insider browsing records without need | 2/3/6 | Role and care-assignment checks, admin has no clinical access, audit of every access, weekly review, sanctions | 1/3/3 |
| 4 | Signature forged or record changed after signing | 2/3/6 | Ed25519 signature with password-wrapped key, MFA at signing, hash recomputed on every read, approval voided on any change | 1/3/3 |
| 5 | Administrator takes over a doctor's account | 2/3/6 | Password reset keeps MFA and retires the signing key; MFA reset needs two administrators; all audited | 1/3/3 |
| 6 | Audit trail tampered with | 2/3/6 | Append-only triggers, keyed HMAC chain with the key outside the database, monthly verification | 1/2/2 |
| 7 | Data loss (disk failure, ransomware) | 2/3/6 | Daily verified backups, weekly off-site copy, key escrow, yearly restore test | 1/2/2 |
| 8 | Loss of the encryption key | 1/3/3 | Offline escrow with two custodians, `key-info` check in validation | 1/3/3 |
| 9 | Network interception | 2/3/6 | Localhost only; HTTPS proxy mandatory for network access | 1/2/2 |
| 10 | Model output inserts wrong clinical facts | 3/2/6 | Quote verification, blocking issues, doctor review and signature; the assistant never writes to the record | 1/2/2 |
| 11 | Wrong signature time (clock drift) | 2/2/4 | SNTP check at signing (more than 2 minutes off blocks signing), UTC timestamps | 1/1/1 |
| 12 | Session left open on a shared workstation | 2/2/4 | Idle logoff after 15 minutes, screen lock after 5 minutes, visible logout button | 1/2/2 |
| 13 | Lost phone with the authenticator app | 2/2/4 | Lost-device procedure; codes are useless without the password; two-person MFA reset | 1/1/1 |
| 14 | Re-identification from pseudonymized text | 2/2/4 | Pseudonymized text is treated as ePHI (encrypted, access controlled); mappings deleted after 24 hours | 1/2/2 |

All residual risks are below 4. Risk 8 is accepted at 3 because losing the key makes data unrecoverable; it is
mitigated by dual-custody escrow and the yearly restore test.

## 3. Risk management plan

| Action | Owner | When |
|---|---|---|
| Daily backup task and off-site rotation (policy §9) | Security official | At installation |
| Key escrow (policy §9.3) | Security official and deputy | At installation |
| Full-disk encryption and screen lock on the server (policy §8) | Security official | At installation |
| HTTPS proxy before any network access (policy §10) | Security official | Before network use |
| Training and signer acknowledgements | Security official | Before accounts are issued |
| Yearly review of this analysis | Security official | Yearly |

## 4. Approval

Reviewed for site ____________________ by ____________________ (security official) Date __________ Signature __________
