"""Password hashing, signed session tokens, and credential encryption.

Passwords use scrypt (hashlib), session cookies are HMAC-signed stateless
tokens, and third-party credentials (Mastodon/Bluesky/Matrix/micro.blog/
Discord tokens, SMTP passwords, vision API keys, OAuth client secrets) are
encrypted at rest with Fernet (cryptography) in multi mode only. Single mode
stores credentials plaintext: the operator owns the database and gains nothing
from encrypting against themselves.
"""

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time

import settings

try:
    from cryptography.fernet import Fernet
except ImportError:  # pragma: no cover - cryptography is a declared dependency
    Fernet = None

# scrypt cost parameters: N=2**17, r=8, p=1 (OWASP-recommended minimum,
# ~128 MiB per hash). verify_password only accepts hashes made with
# EXACTLY these parameters — attacker-supplied work factors are rejected
# before any computation, which closes the login-DoS vector.
_SCRYPT_N = 2**17
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_MAXMEM = 256 * 1024 * 1024

# scrypt releases the GIL, so N concurrent hashes genuinely run in parallel and
# stack N × ~128 MiB. Bound concurrency so a login/register/reset burst cannot
# OOM the box. Default 4 (≈512 MiB peak); tune via FEEDECHO_SCRYPT_CONCURRENCY.
_scrypt_semaphore = threading.BoundedSemaphore(settings.SCRYPT_CONCURRENCY)

SESSION_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days

_SESSION_PURPOSE = b"feedecho-session:v1"
_SESSION_AUD = "feedecho-session"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _scrypt(password: bytes, salt: bytes, n: int, r: int, p: int) -> bytes:
    """Run hashlib.scrypt under the global concurrency cap.

    scrypt releases the GIL, so concurrent calls genuinely run in parallel and
    each holds ~128 MiB. The semaphore bounds total simultaneous hashes.
    """
    with _scrypt_semaphore:
        return hashlib.scrypt(
            password, salt=salt, n=n, r=r, p=p, maxmem=_SCRYPT_MAXMEM
        )


def hash_password(password: str) -> str:
    """scrypt-hash a password. Format: scrypt$N$r$p$salt_b64$digest_b64."""
    salt = secrets.token_bytes(16)
    digest = _scrypt(password.encode(), salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P)
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _b64(salt), _b64(digest)
    )


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verification; False on any malformed or unknown-format
    input. Stored work factors must match the current constants exactly —
    anything else (forged, rotated, or corrupt) fails closed without
    allocating attacker-chosen memory."""
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        if (int(n), int(r), int(p)) != (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P):
            return False
        salt = _unb64(salt_b64)
        expected = _unb64(digest_b64)
        actual = _scrypt(password.encode(), salt, int(n), int(r), int(p))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def new_token() -> str:
    """A high-entropy single-use token for email flows (verification, reset)."""
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    """Hash a token for at-rest storage.

    Tokens are high-entropy (256 bits), so a plain SHA-256 digest is
    sufficient — scrypt would add nothing against offline attacks on a
    token with this much entropy.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def session_secret() -> bytes:
    """The master HMAC key for session cookies.

    Multi mode requires FEEDECHO_SESSION_SECRET explicitly — the gate
    fires even when FEEDECHO_AUTH_TOKEN is set, so a carried-over
    single-mode env can never mint multi-tenant sessions. Single mode
    falls back to FEEDECHO_AUTH_TOKEN (sessions are unused there).
    """
    if settings.MULTI and not settings.SESSION_SECRET:
        raise RuntimeError(
            "FEEDECHO_SESSION_SECRET must be set when FEEDECHO_MODE=multi"
        )
    key = settings.SESSION_SECRET or (settings.AUTH_TOKEN or "")
    return key.encode()


def _session_key() -> bytes:
    """Purpose-derived HMAC key so session tokens share no key material
    with any other signed artifact (OAuth state, etc.)."""
    return hmac.new(
        session_secret(), _SESSION_PURPOSE, hashlib.sha256
    ).digest()


def sign_session(user_id: int, email: str, epoch: int = 0) -> str:
    """Return a signed session token: base64url(claims).hexsig."""
    payload = _b64(
        json.dumps(
            {
                "aud": _SESSION_AUD,
                "uid": user_id,
                "email": email,
                "ep": epoch,
                "exp": int(time.time()) + SESSION_TTL_SECONDS,
            }
        ).encode()
    ).rstrip("=")
    sig = hmac.new(_session_key(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def read_session(token: str | bytes) -> dict | None:
    """Verify a signed session token; return claims or None.

    Returns None for EVERY malformed input — never raises: garbage
    payloads, non-ASCII or truncated signatures, bytes tokens, wrong
    audience, expired tokens, and coerced claim types all fail closed.
    """
    try:
        if not isinstance(token, str) or "." not in token:
            return None
        payload, sig = token.rsplit(".", 1)
        sig_bytes = sig.encode("ascii")
        expected = hmac.new(
            _session_key(), payload.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig_bytes, expected.encode("ascii")):
            return None
        data = json.loads(_unb64(payload))
        if not isinstance(data, dict):
            return None
        if data.get("aud") != _SESSION_AUD:
            return None
        if int(data.get("exp", 0)) < time.time():
            return None
        uid = data.get("uid")
        if isinstance(uid, bool) or not isinstance(uid, int):
            return None
        epoch = data.get("ep", 0)
        if isinstance(epoch, bool) or not isinstance(epoch, int):
            return None
        return {
            "user_id": uid,
            "email": str(data.get("email", "")),
            "epoch": epoch,
        }
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        return None


# ── Credential encryption (Fernet, multi mode only) ──────────────────────────


_credential_fernet = None
_credential_fernet_key = None


def _fernet():
    """Lazy-initialised Fernet instance, keyed from settings.CREDENTIAL_KEY.

    Returns None when the key is unset or cryptography is not installed,
    so callers silently fall back to plaintext.
    """
    global _credential_fernet, _credential_fernet_key
    key = settings.CREDENTIAL_KEY or ""
    if _credential_fernet is not None and key == _credential_fernet_key:
        return _credential_fernet
    _credential_fernet_key = key
    if not key or Fernet is None:
        _credential_fernet = None
    else:
        _credential_fernet = Fernet(key.encode("utf-8"))
    return _credential_fernet


def encrypt_secret(value: str) -> str:
    """Encrypt a third-party credential for at-rest storage.

    Only encrypts in multi mode with FEEDECHO_CREDENTIAL_KEY set. Single
    mode returns the value unchanged (the operator owns the DB). An unset
    key in multi mode returns the value unchanged (a startup warning fires
    through settings.validate_config).
    """
    if not value:
        return value
    if not settings.MULTI:
        return value
    fernet = _fernet()
    if fernet is None:
        return value
    token = fernet.encrypt(value.encode("utf-8"))
    return token.decode("ascii")


def decrypt_secret(value: str) -> str:
    """Decrypt a stored credential, tolerating legacy plaintext rows.

    Returns the plaintext credential. Values that are not valid Fernet
    tokens (rows written before encryption shipped, or a key rotation that
    renders the token unreadable) are returned unchanged, so a mixed
    plaintext/encrypted DB keeps working. Re-encrypts on the next write.
    """
    if not value:
        return value
    if not settings.MULTI:
        return value
    fernet = _fernet()
    if fernet is None:
        return value
    try:
        return fernet.decrypt(value.encode("ascii")).decode("utf-8")
    except Exception:
        if value.startswith("gAAAA"):
            # A well-formed Fernet token that failed to decrypt is a wrong key
            # or tampered value — NOT a legacy plaintext row. Returning the
            # ciphertext as a "credential" would hand garbage to Mastodon/etc.
            # and surface as a confusing 401. Fail loud instead.
            raise ValueError(
                "credential decryption failed — FEEDECHO_CREDENTIAL_KEY is "
                "wrong or the stored value was tampered with"
            )
        # Legacy plaintext (written before encryption shipped): return as-is.
        return value


def hash_secret(value: str) -> str:
    """Deterministic SHA-256 digest of a secret, for dedup of an encrypted
    credential that also serves as a row's natural key (Discord webhook URL).

    Fernet is non-deterministic, so an encrypted column can't be an
    ON CONFLICT / WHERE-equality key; the digest of the plaintext can.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
