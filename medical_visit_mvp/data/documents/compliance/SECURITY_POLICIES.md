# Security policies and procedures

Covers HIPAA 45 CFR 164.308, 164.310, 164.316 and 164.400–414, and the procedural parts of 21 CFR 11.10 and 11.300.
These policies take effect when the operating organization fills in the names below and the security official
signs the adoption record at the end. Every change to this document goes through Git and is approved by the
security official.

| Field | Value |
|---|---|
| Organization | ____________________ |
| Security official (164.308(a)(2)) | ____________________ |
| Deputy (second administrator) | ____________________ |
| Effective date | ____________________ |
| Next review (at most 12 months) | ____________________ |

## 1. Roles and responsibility

- **Security official**: owns these policies, the risk analysis, incident handling, the yearly evaluation and the
  key escrow. Holds an administrator account.
- **Second administrator** (deputy): required for MFA resets (two-person rule) and for key-escrow access.
- **Doctors**: sign records; accountable under SIGNATURE_POLICY.md.
- **Patients**: read and export only their own approved records.
- Administrator accounts see no clinical content; this is enforced by the software.

## 2. Workforce security and access management (164.308(a)(3)–(4); 11.300(b)–(c))

1. **Authorization.** Before an account is created, the security official approves the role and, for doctors, the
   patients to assign. Patients may also be added by their treating doctor; the patient is then assigned only to
   that doctor.
2. **Identity check.** Identity is verified in person or by video with an official ID before an account is issued
   (SIGNATURE_POLICY.md §3). The temporary password is handed over personally and must be changed at first login.
3. **MFA.** Administrators and doctors set up an authenticator app at first login. It is required at every login and
   signature.
4. **Lost or stolen phone or password.** The user tells the security official at once. Steps: deactivate or reset the
   password; the user's sessions end. For a lost authenticator, two administrators perform the MFA reset in the admin
   console. With only one administrator, use `python -m backend.manage reset-mfa` on the server, and record it in the
   incident log.
5. **Quarterly review.** Every quarter, the security official reviews all accounts, roles and care assignments in the
   admin console, deactivates accounts no longer needed and revokes stale assignments. The review is recorded in the
   audit trail, through the console actions, and in the review log.
6. **Termination.** On the last working day the account is deactivated: all sessions end, and the username is kept and
   never reused.

## 3. Sanctions (164.308(a)(1)(ii)(C))

Violations such as sharing credentials, signing for another person, accessing records without a care need or
disabling controls lead to sanctions proportional to severity, from retraining to dismissal and reporting to the
licensing body. Sanctions are documented and kept for 6 years.

## 4. Training (164.308(a)(5); 11.10(i))

Before receiving an account, and yearly after that, each user completes training on:
- this policy;
- phishing and password hygiene;
- MFA use;
- reporting incidents;
- the meaning and legal weight of the electronic signature (doctors).

Training records (name, date, topics, trainer) are kept for 6 years.

## 5. Information system activity review (164.308(a)(1)(ii)(D))

- **Daily:** an administrator opens the admin console. The security-alert panel lists every failed or denied login,
  one-time code, signature or MFA reset and every account lockout of the last 24 hours. Each alert is either explained
  or opened as an incident (§6).
- **Weekly:** the security official reviews the audit trail (filters: outcome = denied or failure; action = export,
  mfa_reset, password_reset) and runs "verify audit chain integrity".
- **Monthly:** `python -m backend.manage verify-audit` on the server, and a restore test of the latest backup (§9).

## 6. Security incidents and breach notification (164.308(a)(6); 164.400–414; 11.300(d))

1. **Report:** anyone who suspects an incident tells the security official immediately. Examples: unexplained
   lockouts, a lost device, a suspected shared password, a failed audit-chain check, or a failed backup verification.
2. **Contain:** deactivate the affected accounts, reset passwords and MFA, and revoke care assignments as needed.
   Preserve the audit trail by exporting it as CSV or JSON.
3. **Breach assessment:** the security official applies the 4-factor risk assessment of §164.402: nature of the
   PHI, who obtained it, whether it was actually acquired or viewed, and how far the risk was mitigated. Data
   encrypted at rest whose key was not compromised is "secured PHI" under HHS guidance and is not a reportable
   breach.
4. **Notify:**
   - Individuals: without unreasonable delay and no later than 60 days after discovery.
   - HHS: within 60 days for 500 or more individuals; otherwise in the yearly log.
   - Media: when more than 500 residents of one state or jurisdiction are affected.
   - Under Israeli law: the Privacy Protection Authority, as required by the Data Security Regulations.
5. **Document:** keep the incident log, the assessment and the notifications for 6 years.

## 7. Emergency access (164.312(a)(2)(ii))

When a patient's treating doctor is unavailable and care requires access:
1. An administrator assigns another doctor with reason "emergency access: <short reason>". The reason is mandatory
   and audited.
2. After the emergency, the assignment is revoked.
3. The security official reviews each emergency access within 7 days.

## 8. Workstation, device and media (164.310; 11.10(h) justification)

- The server computer has full-disk encryption (BitLocker), a screen lock after at most 5 minutes, current
  operating-system updates and antivirus.
- Only authorized staff can log in to it.
- No clinical exports are stored on removable media unless the media is encrypted.
- Disposal of disks or devices: secure wipe or physical destruction, recorded in the media log.
- The system takes no input from instruments or terminals. Records are entered only by authenticated users through
  the application, so device checks under 11.10(h) are not applicable.

## 9. Contingency plan: backup, recovery and key escrow (164.308(a)(7); 11.10(c))

1. **Daily backup** (Windows Task Scheduler, outside working hours):
   `..\venv\Scripts\python.exe -m backend.manage backup --out <backup folder>`.
   - It copies both databases consistently and checks each copy: integrity, test decryption and the audit chain.
   - Copies stay encrypted.
   - One copy per week goes off-site, for example to an encrypted external drive kept in another building.
2. **Retention:** daily copies for 30 days, weekly copies for 1 year. The records themselves are never deleted
   from the database.
3. **Key escrow:**
   - Right after installation, the security official runs `python -m backend.manage export-key --out <file>`.
   - The key is stored offline (printed or on an encrypted USB drive) in a safe, with the deputy as second custodian.
   - The file is then deleted from the computer.
   - **Without the key, encrypted data and backups cannot be recovered.**
4. **Restore test:** monthly, run `python -m backend.manage verify-backup <latest copy>`. Yearly, do a full restore
   on a spare computer with `DATA_ENCRYPTION_KEY` set to the escrowed key. Record the results.
5. **Emergency mode:** if the server is lost, restore the latest copy on a replacement computer (deployment/README.md),
   set `DATA_ENCRYPTION_KEY`, run `manage verify-audit`, and resume. Paper notes taken in the meantime are entered
   afterwards as new versions.

## 10. Transmission security (164.312(e))

- The application listens only on 127.0.0.1.
- Any access from another computer must go through the HTTPS reverse proxy (`deployment/Caddyfile.example`; TLS 1.2
  or higher, valid certificate). Plain HTTP over a network is prohibited.
- No ePHI is sent to cloud services: transcription and models run locally. The time check sends only an SNTP
  request.

## 11. Documentation, evaluation and change control (164.308(a)(8); 164.316; 11.10(k))

- Policies, the risk analysis, validation reports, training records, incident logs and review logs are kept for
  6 years from creation or last effective date.
- Source code and documentation are under Git; every change follows VALIDATION_PLAN.md §7 (change control).
- Yearly, and after every significant change, the security official re-evaluates these policies and the risk
  analysis and re-runs the validation.

## 12. Business associates (164.308(b))

The system uses no third party that creates, receives, maintains or transmits ePHI. Before any such service is added
(cloud hosting, remote backup, external support with data access), a business associate agreement is signed and this
section is updated.

## 13. Right of access (164.524)

Patients download their approved summary and plan (TXT, PDF, JSON) in the app. A request for the full designated
record set is answered within 30 days. The treating doctor exports the complete record as JSON, and the security
official reviews it before release.

## Adoption

Adopted by the security official: name ____________________ signature ____________________ date __________
