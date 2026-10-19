# Electronic signature policy (21 CFR Part 11)

Covers 11.10(i)–(j), 11.50, 11.100(b)–(c) and 11.200. It takes effect together with SECURITY_POLICIES.md.

## 1. Accountability (11.10(j))

An electronic signature in this system is the legally binding equivalent of the signer's handwritten signature.

**When it is used:** a doctor signs to approve the visit record:
- the clinician summary;
- the patient summary;
- the doctor's plan.

The recorded meaning is "Reviewed and approved".

**What signing requires:**
1. Re-entering the doctor's username and password.
2. Entering a fresh one-time code from the doctor's own authenticator app.

**What is recorded:**
- the printed name;
- the UTC date and time, checked against a trusted time source;
- the meaning;
- the Ed25519 digital signature over the exact signed versions.

**Signers must:**
- never share their password, authenticator device or codes;
- never let another person sign for them;
- review the content shown under "What will be signed" before signing;
- report a lost device or a suspected compromise at once (SECURITY_POLICIES.md §2.4).

**Changes after signing:** any change to a signed record automatically voids the signature, and the record must be
reviewed and signed again.

## 2. Training (11.10(i))

Before their first signature, each doctor is trained on this policy, the signing screen and the meaning of the
signature. The training is recorded under SECURITY_POLICIES.md §4.

## 3. Identity verification before issuing a signing account (11.100(b))

Checklist, completed by the administrator who creates the account. Keep it for 6 years.

| Item | Done |
|---|---|
| Official photo ID seen in person or by live video (type and last 4 digits) | ☐ |
| Professional licence number checked against the licensing registry | ☐ |
| Temporary password handed over personally | ☐ |
| MFA set up by the person themself at first login (not by the administrator) | ☐ |
| Signed acknowledgement (§5) received before the first signature | ☐ |

Administrator: __________________ Date: __________

## 4. Certification to FDA (11.100(c))

When Part 11 applies (for example, the system is a source system in an FDA-regulated clinical investigation), send
the following letter before or at the time of first use. Send the paper original with a traditional handwritten
signature to the FDA office named on FDA's current "Letters of non-repudiation agreement" page (search fda.gov for
that phrase; check the address at the time of sending, since FDA offices have been reorganized). Keep a copy.

> Pursuant to Section 11.100 of Title 21 of the Code of Federal Regulations, this is to certify that
> [organization name], [address], intends that all electronic signatures executed by our employees, agents, or
> representatives, located anywhere in the world, on or after [date], are the legally binding equivalent of
> traditional handwritten signatures.
>
> [Name, title, handwritten signature, date]

## 5. Signer acknowledgement form

> I, ______________________ (username ________), acknowledge that my electronic signature in the Medical Visit
> system is the legally binding equivalent of my handwritten signature. I will not share my password, authenticator
> device or codes, will not let anyone sign for me, will review records before signing, and will report any loss
> or suspected misuse immediately. I have received training on this policy.
>
> Signature (handwritten): ______________________ Date: __________
