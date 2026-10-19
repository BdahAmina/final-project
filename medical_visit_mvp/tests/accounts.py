"""Test-only accounts with fictional names. Production hashing uses 600,000 PBKDF2 rounds; tests lower it.

Mirrors real setup: the admin is bootstrapped (as `python -m backend.manage create-admin` does), then the
admin creates doctors and patients and assigns doctor_a -> patient_a and doctor_b -> patient_b.
"""
import os
os.environ.setdefault("DATA_ENCRYPTION_KEY", "dGVzdC1vbmx5LWRhdGEta2V5LTMyLWJ5dGVzLWxvbmc=")  # fixed test key, never the real one
from backend import crypto, security  # noqa: E402

security.PBKDF2_ITERATIONS = 1_000
security.MFA_REQUIRED_ROLES = set()  # MFA is exercised explicitly in test_security.py
crypto.TIME_SERVER = ""  # no network time check in tests
PASSWORD = "correct-horse-battery-1"
ADMIN = {"id": "admin_root", "role": "admin"}
DOC = {"id": "doctor_a", "role": "doctor"}
OTHER_DOC = {"id": "doctor_b", "role": "doctor"}
PAT = {"id": "patient_a", "role": "patient"}
OTHER_PAT = {"id": "patient_b", "role": "patient"}


def add_accounts(repo):
    repo.bootstrap_admin(ADMIN["id"], "מנהל/ת בדיקה", PASSWORD)
    for user, name in ((DOC, "ד״ר בדיונית א"), (OTHER_DOC, "ד״ר בדיוני ב"), (PAT, "מטופלת בדיונית א"), (OTHER_PAT, "מטופל בדיוני ב")):
        repo.create_user(ADMIN, user["id"], user["role"], name, PASSWORD)
    repo.assign(ADMIN, DOC["id"], PAT["id"], "test fixture")
    repo.assign(ADMIN, OTHER_DOC["id"], OTHER_PAT["id"], "test fixture")
