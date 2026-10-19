"""Encryption at rest, electronic-signature keys, one-time codes (MFA) and the trusted-time check.

Encryption at rest (45 CFR 164.312(a)(2)(iv), (c)(2)): clinical columns and personal names are stored as
AES-256-GCM ciphertext ("enc1:" + base64(nonce | ciphertext)). The 256-bit data key comes from
DATA_ENCRYPTION_KEY (base64) or from a key file (default data/keys/data.key) that is created on first use and,
on Windows, protected with DPAPI so that only the Windows account that created it can read it. Keep an offline
escrow copy of the key (python -m backend.manage export-key): without it the encrypted data cannot be recovered.
Values written before encryption was introduced are read as-is and re-encrypted by migration 0003.

Signatures (21 CFR 11.70, 11.200): each signer has an Ed25519 key pair. The private key is stored only wrapped
with a key derived from the signer's password (PBKDF2-HMAC-SHA256 + AES-256-GCM), so it can be used only when the
signer re-enters the password; the public key is kept with every signature so it can be verified forever.

MFA (164.312(d), 11.300): RFC 6238 time-based one-time codes (6 digits, 30 s), compatible with authenticator apps.

Trusted time (11.10(e)): signing compares the local clock with an SNTP server (TIME_SERVER, default
pool.ntp.org; empty disables the check). Only a time request is sent, never data.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import socket
import struct
import sys
import time
from pathlib import Path
from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes
from sqlalchemy.types import Text, TypeDecorator

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "enc1:"
TIME_SERVER = os.getenv("TIME_SERVER", "pool.ntp.org")
MAX_CLOCK_SKEW_SECONDS = int(os.getenv("MAX_CLOCK_SKEW_SECONDS", "120"))
KEY_WRAP_ITERATIONS = 600_000
_cache = {}


class CryptoError(RuntimeError):
    pass


# ---- data key ------------------------------------------------------------------------------------------------
def _dpapi(data: bytes, protect: bool) -> bytes:
    """Windows DPAPI (current-user scope) through ctypes; identity elsewhere (the file is chmod 600 instead)."""
    if sys.platform != "win32":
        return data
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    src, out = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), Blob()
    crypt32 = ctypes.windll.crypt32
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    args = (ctypes.byref(src), "medical-visit-data-key" if protect else None, None, None, None, 0, ctypes.byref(out)) if protect \
        else (ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out))
    if not fn(*args):
        raise CryptoError("DPAPI could not " + ("protect" if protect else "read") + " the data key (different Windows account?)")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def key_file() -> Path:
    return Path(os.getenv("DATA_KEY_FILE", str(ROOT / "data" / "keys" / "data.key")))


def data_key() -> bytes:
    if "data" in _cache:
        return _cache["data"]
    raw = os.getenv("DATA_ENCRYPTION_KEY", "").strip()
    if raw:
        key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    else:
        path = key_file()
        if path.exists():
            key = _dpapi(path.read_bytes(), protect=False)
        else:
            key = secrets.token_bytes(32)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(_dpapi(key, protect=True))
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
    if len(key) != 32:
        raise CryptoError("The data encryption key must be 32 bytes (base64).")
    _cache["data"] = key
    return key


def key_protection() -> str:
    if os.getenv("DATA_ENCRYPTION_KEY", "").strip():
        return "environment variable DATA_ENCRYPTION_KEY"
    return f"key file {key_file()} ({'Windows DPAPI, current user' if sys.platform == 'win32' else 'file permissions 600'})"


def export_key() -> str:
    return base64.urlsafe_b64encode(data_key()).decode()


def subkey(purpose: str) -> bytes:
    """Independent key for another purpose (e.g. the audit HMAC), derived from the data key with HKDF."""
    if purpose not in _cache:
        _cache[purpose] = HKDF(hashes.SHA256(), 32, salt=None, info=purpose.encode()).derive(data_key())
    return _cache[purpose]


def reset_cache():
    _cache.clear()


# ---- field encryption ------------------------------------------------------------------------------------------
def encrypt(text: str) -> str:
    nonce = secrets.token_bytes(12)
    return PREFIX + base64.b64encode(nonce + AESGCM(data_key()).encrypt(nonce, text.encode(), None)).decode()


def decrypt(value: str) -> str:
    if not isinstance(value, str) or not value.startswith(PREFIX):
        return value  # written before encryption at rest; migration 0003 encrypts such rows
    blob = base64.b64decode(value[len(PREFIX):])
    try:
        return AESGCM(data_key()).decrypt(blob[:12], blob[12:], None).decode()
    except InvalidTag as exc:
        raise CryptoError("Encrypted value cannot be decrypted with this key (wrong key or altered data).") from exc


def is_encrypted(value) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


# ---- whole-file encryption (backups and database files written before encryption at rest) -------------------------
FILE_MAGIC = b"MVENC1\n"


def seal_file(src: Path) -> Path:
    """Encrypt `src` into `src` + '.enc', check that it decrypts to the same bytes, then delete `src`."""
    src = Path(src)
    data = src.read_bytes()
    nonce = secrets.token_bytes(12)
    dst = src.with_name(src.name + ".enc")
    dst.write_bytes(FILE_MAGIC + nonce + AESGCM(data_key()).encrypt(nonce, data, src.name.encode()))
    if unseal_bytes(dst) != data:
        dst.unlink()
        raise CryptoError(f"verification of {dst.name} failed; the original was kept")
    src.unlink()
    return dst


def unseal_bytes(path: Path) -> bytes:
    path = Path(path)
    blob = path.read_bytes()
    if not blob.startswith(FILE_MAGIC):
        raise CryptoError(f"{path.name} is not an encrypted file")
    blob = blob[len(FILE_MAGIC):]
    try:
        return AESGCM(data_key()).decrypt(blob[:12], blob[12:], path.name[:-len(".enc")].encode())
    except InvalidTag as exc:
        raise CryptoError(f"{path.name} cannot be decrypted with this key (wrong key or altered file)") from exc


class EncryptedText(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else encrypt(str(value))

    def process_result_value(self, value, dialect):
        return None if value is None else decrypt(value)


class EncryptedJSON(TypeDecorator):
    """JSON value stored encrypted. Unencrypted legacy values (plain JSON text) are still readable."""
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else encrypt(json.dumps(value, ensure_ascii=False))

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return json.loads(decrypt(value)) if isinstance(value, str) else value


# ---- signing keys -----------------------------------------------------------------------------------------------
def _wrap_key(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)


def new_signing_key(password: str, iterations: int = None) -> tuple[str, str]:
    """(public key b64, wrapped private key) for a new Ed25519 key pair, the private key wrapped with the password."""
    private = Ed25519PrivateKey.generate()
    raw = private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    return public_key_b64(private), wrap_private(raw, password, iterations)


def wrap_private(raw: bytes, password: str, iterations: int = None) -> str:
    iterations = iterations or KEY_WRAP_ITERATIONS
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    ct = AESGCM(_wrap_key(password, salt, iterations)).encrypt(nonce, raw, b"signing-key")
    return f"w1${iterations}${base64.b64encode(salt + nonce + ct).decode()}"


def unwrap_private(wrapped: str, password: str) -> Ed25519PrivateKey:
    """The private key, or CryptoError if the password does not open it."""
    try:
        _, iterations, blob = wrapped.split("$")
        blob = base64.b64decode(blob)
        raw = AESGCM(_wrap_key(password, blob[:16], int(iterations))).decrypt(blob[16:28], blob[28:], b"signing-key")
    except (ValueError, InvalidTag, AttributeError) as exc:
        raise CryptoError("signing key cannot be opened with this password") from exc
    return Ed25519PrivateKey.from_private_bytes(raw)


def rewrap(wrapped: str, old_password: str, new_password: str, iterations: int = None) -> str:
    private = unwrap_private(wrapped, old_password)
    raw = private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    return wrap_private(raw, new_password, iterations)


def public_key_b64(private: Ed25519PrivateKey) -> str:
    return base64.b64encode(private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()


def fingerprint(public_b64: str) -> str:
    return hashlib.sha256(base64.b64decode(public_b64)).hexdigest()[:32]


def canonical(manifest: dict) -> bytes:
    return json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def sign(private: Ed25519PrivateKey, manifest: dict) -> str:
    return base64.b64encode(private.sign(canonical(manifest))).decode()


def verify(public_b64: str, manifest: dict, signature_b64: str) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public_b64)).verify(base64.b64decode(signature_b64), canonical(manifest))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


# ---- one-time codes (RFC 6238) ------------------------------------------------------------------------------------
def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp(secret: str, step: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    return f"{(struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def totp_match(secret: str, code: str, last_step: int = None, now: float = None, window: int = 1):
    """The time step the code belongs to (±window steps for clock drift), or None. Steps at or before
    `last_step` are refused so a code cannot be replayed."""
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(code) != 6:
        return None
    current = int((now or time.time()) // 30)
    for step in range(current - window, current + window + 1):
        if (last_step is None or step > last_step) and hmac.compare_digest(totp(secret, step), code):
            return step
    return None


def provisioning_uri(secret: str, username: str, issuer: str = "Medical Visit") -> str:
    from urllib.parse import quote
    return f"otpauth://totp/{quote(issuer)}:{quote(username)}?secret={secret}&issuer={quote(issuer)}&digits=6&period=30"


def qr_png(uri: str):
    """PNG bytes of a QR code for the authenticator app, or None if the qrcode package is missing."""
    try:
        import io
        import qrcode
    except ImportError:
        return None
    buf = io.BytesIO()
    qrcode.make(uri, box_size=6, border=2).save(buf, format="PNG")
    return buf.getvalue()


# ---- trusted time ----------------------------------------------------------------------------------------------
def clock_check(server: str = None, timeout: float = 2.0) -> dict:
    """{'source', 'offset_seconds', 'verified'}: SNTP offset of the local clock. Unreachable server -> unverified."""
    server = TIME_SERVER if server is None else server
    if not server:
        return {"source": "local clock (time check disabled)", "offset_seconds": None, "verified": False}
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sent = time.time()
            sock.sendto(b"\x1b" + 47 * b"\0", (server, 123))
            data, _ = sock.recvfrom(48)
            received = time.time()
        secs, frac = struct.unpack("!II", data[40:48])
        server_time = secs - 2208988800 + frac / 2 ** 32
        offset = server_time - (sent + received) / 2
        return {"source": f"SNTP {server}", "offset_seconds": round(offset, 3), "verified": True}
    except OSError:
        return {"source": f"local clock ({server} unreachable)", "offset_seconds": None, "verified": False}
