"""Password hashing and signed session tokens — stdlib only.

Deliberately dependency-free: scrypt (hashlib) for passwords, HMAC-signed
stateless cookies for sessions. No argon2/itsdangerous so self-hosters
never need extra wheels, and the same code runs identically in single
and multi mode.

Session tokens use a purpose-derived HMAC key (separate from the OAuth
state key) and carry an ``aud`` claim, so tokens cannot be confused with
any other signed artifact under the same secret.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time

import settings

# scrypt cost parameters: N=2**17, r=8, p=1 (OWASP-recommended minimum,
# ~128 MiB per hash). verify_password only accepts hashes made with
# EXACTLY these parameters — attacker-supplied work factors are rejected
# before any computation, which closes the login-DoS vector.
_SCRYPT_N = 2**17
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_MAXMEM = 256 * 1024 * 1024

SESSION_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days

_SESSION_PURPOSE = b"feedecho-session:v1"
_SESSION_AUD = "feedecho-session"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str) -> str:
    """scrypt-hash a password. Format: scrypt$N$r$p$salt_b64$digest_b64."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        maxmem=_SCRYPT_MAXMEM,
    )
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
        actual = hashlib.scrypt(
            password.encode(),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=_SCRYPT_MAXMEM,
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def session_secret() -> bytes:
    """The master HMAC key for session cookies.

    Multi mode requires FEEDCHO_SESSION_SECRET explicitly — the gate
    fires even when FEEDCHO_AUTH_TOKEN is set, so a carried-over
    single-mode env can never mint multi-tenant sessions. Single mode
    falls back to FEEDCHO_AUTH_TOKEN (sessions are unused there).
    """
    if settings.MULTI and not settings.SESSION_SECRET:
        raise RuntimeError(
            "FEEDCHO_SESSION_SECRET must be set when FEEDCHO_MODE=multi"
        )
    key = settings.SESSION_SECRET or (settings.AUTH_TOKEN or "")
    return key.encode()


def _session_key() -> bytes:
    """Purpose-derived HMAC key so session tokens share no key material
    with any other signed artifact (OAuth state, etc.)."""
    return hmac.new(
        session_secret(), _SESSION_PURPOSE, hashlib.sha256
    ).digest()


def sign_session(user_id: int, email: str) -> str:
    """Return a signed session token: base64url(claims).hexsig."""
    payload = _b64(
        json.dumps(
            {
                "aud": _SESSION_AUD,
                "uid": user_id,
                "email": email,
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
        return {"user_id": uid, "email": str(data.get("email", ""))}
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        return None
