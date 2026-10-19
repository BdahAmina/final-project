"""Password hashing, password policy, lockout, MFA and session-expiry settings.

Passwords: salted PBKDF2-HMAC-SHA256 (600,000 iterations by default), compared in constant time.
Session tokens: 256-bit random, stored only as SHA-256. All limits can be tightened through
environment variables; they are read at import time.
"""
import hashlib
import hmac
import os
import re
import secrets

PBKDF2_ITERATIONS = 600_000
PASSWORD_MIN_LENGTH = int(os.getenv("PASSWORD_MIN_LENGTH", "12"))
MAX_FAILED_LOGINS = int(os.getenv("MAX_FAILED_LOGINS", "5"))
LOCKOUT_MINUTES = int(os.getenv("LOCKOUT_MINUTES", "15"))
SESSION_IDLE_MINUTES = int(os.getenv("SESSION_IDLE_MINUTES", "15"))
SESSION_MAX_HOURS = int(os.getenv("SESSION_MAX_HOURS", "8"))
PASSWORD_MAX_AGE_DAYS = int(os.getenv("PASSWORD_MAX_AGE_DAYS", "180"))
# Roles that must use a one-time code (MFA) at every login and every signature. Patients may enrol voluntarily.
MFA_REQUIRED_ROLES = {r.strip() for r in os.getenv("MFA_REQUIRED_ROLES", "admin,doctor").split(",") if r.strip()}
USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{2,39}$")
COMMON = {"password", "password123", "123456789012", "qwertyuiop12", "letmein12345", "welcome12345", "admin1234567"}


def hash_password(password: str, salt: bytes = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt, digest = stored.split("$")
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(candidate.hex(), digest)


DUMMY_HASH = hash_password(secrets.token_hex(8))  # equalises login time for unknown usernames


def password_problem(password: str, username: str = "") -> str:
    """Hebrew error message, or '' if the password meets the policy."""
    password = password or ""
    if len(password) < PASSWORD_MIN_LENGTH:
        return f"הסיסמה חייבת להכיל לפחות {PASSWORD_MIN_LENGTH} תווים."
    if username and username.lower() in password.lower():
        return "הסיסמה אינה יכולה לכלול את שם המשתמש."
    if password.lower() in COMMON or len(set(password)) < 4:
        return "הסיסמה נפוצה או פשוטה מדי."
    return ""


def username_problem(username: str) -> str:
    if not USERNAME.match(username or ""):
        return "שם משתמש: 3–40 תווים – אותיות אנגליות קטנות, ספרות, נקודה, מקף או קו תחתון."
    return ""


def new_token() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    return token, token_hash(token)


def token_hash(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()
