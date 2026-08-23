"""Password hashing and signed session tokens — stdlib only.

Deliberately dependency-free: scrypt (hashlib) for passwords, HMAC-signed
stateless cookies for sessions. No argon2/itsdangerous so self-hosters
never need extra wheels, and the same code runs identically in single
and multi mode.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time

import settings

# scrypt cost parameters: N=2**14, r=8, p=1 (~16 MiB, tens of ms per hash).
# Deliberately below OWASP's high-security recommendation to keep login
# latency low on a 1-vCPU box; raise N later if needed.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1

SESSION_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days


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
    )
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _b64(salt), _b64(digest)
    )


def verify_password(password: str, stored: str) -> bool:
    """Constant-time-ish verification; False on any malformed input."""
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = _unb64(salt_b64)
        expected = _unb64(digest_b64)
        actual = hashlib.scrypt(
            password.encode(),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def session_secret() -> bytes:
    """The HMAC key for session cookies.

    Multi mode requires FEEDCHO_SESSION_SECRET explicitly. Single mode
    falls back to FEEDCHO_AUTH_TOKEN (sessions aren't used there; the
    value keeps oauth state signing consistent).
    """
    key = settings.SESSION_SECRET or (settings.AUTH_TOKEN or "")
    if not key and settings.MULTI:
        raise RuntimeError(
            "FEEDCHO_SESSION_SECRET must be set when FEEDCHO_MODE=multi"
        )
    return key.encode()


def sign_session(user_id: int, email: str) -> str:
    """Return a signed session token: base64url(claims).hexsig."""
    payload = _b64(
        json.dumps(
            {
                "uid": user_id,
                "email": email,
                "exp": int(time.time()) + SESSION_TTL_SECONDS,
            }
        ).encode()
    ).rstrip("=")
    sig = hmac.new(
        session_secret(), payload.encode(), hashlib.sha256
    ).hexdigest()
    return f"{payload}.{sig}"


def read_session(token: str) -> dict | None:
    """Verify a signed session token; return claims or None."""
    if not token or "." not in token:
        return None
    payload, sig = token.rsplit(".", 1)
    expected = hmac.new(
        session_secret(), payload.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        data = json.loads(_unb64(payload))
        if not isinstance(data, dict):
            return None
        if int(data.get("exp", 0)) < time.time():
            return None
        return {"user_id": int(data["uid"]), "email": str(data.get("email", ""))}
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None
