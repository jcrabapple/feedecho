"""Multi-mode authentication: register, login, logout, and session setup.

Single mode never registers these routes' behavior — every handler
short-circuits with 404 unless FEEDCHO_MODE=multi. Sessions are stateless
HMAC tokens from security.py; the shared-secret AuthMiddleware in app.py
routes to this flow only when settings.MULTI is set.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import settings
from database import get_db
from security import SESSION_TTL_SECONDS, hash_password, sign_session, verify_password

# Minimal in-memory login throttle: 5 failed attempts per IP per 5 minutes.
# Deliberately cheap and boring; real abuse controls (per-user caps, IP
# reputation) land with the billing/abuse phase.
_MAX_LOGIN_ATTEMPTS = 5
_LOGIN_WINDOW_SECONDS = 5 * 60
_login_attempts: dict[str, list[float]] = {}
_login_lock = threading.Lock()

# Registration gets its own, more generous bucket: scrypt is expensive and
# the route is public, so cap valid-form submissions per IP.
_MAX_REGISTER_ATTEMPTS = 10
_REGISTER_WINDOW_SECONDS = 10 * 60
_register_attempts: dict[str, list[float]] = {}

_EMAIL_RE = re.compile(r"^[^@\s\r\n]+@[^@\s\r\n]+\.[^@\s\r\n]+$")


# Forgot-password IP throttle: 5 requests per IP per 10 minutes. Keeps an
# unauthenticated email-sending endpoint from rotating across the whole
# address space (per-user token throttle alone doesn't bound total sends).
_forgot_attempts: dict[str, list[float]] = {}
_FORGOT_LIMIT = 5
_FORGOT_WINDOW = 600
_MIN_PASSWORD_LENGTH = 8
_MAX_PASSWORD_LENGTH = 1024
_TRIAL_DAYS = 14

COOKIE_NAME = "feedecho_session"

# Precomputed once so unknown-email login attempts pay the same scrypt
# cost as known-email attempts (no timing-based user enumeration).
_DUMMY_HASH = hash_password("feedecho-timing-equalizer")


def current_user_id(request: Request) -> int:
    """The authenticated user for this request (multi mode), or the
    singleton user 1 in single mode."""
    if settings.MULTI:
        uid = getattr(request.state, "user_id", None)
        if uid is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        return uid
    return 1


def _require_multi() -> None:
    if not settings.MULTI:
        raise HTTPException(status_code=404, detail="Not found")


def is_admin(uid: int) -> bool:
    """Whether the given user is an admin.

    Read fresh from the database on every call — never from session
    claims — so demotions take effect immediately even with a valid
    session token. Non-existent users are not admins.
    """
    with get_db() as db:
        row = db.execute(
            "SELECT is_admin FROM users WHERE id = ?", (uid,)
        ).fetchone()
    return bool(row and row["is_admin"])


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _trial_end() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=_TRIAL_DAYS)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _client_ip(request: Request) -> str:
    """The client IP for rate limiting.

    Behind a trusted reverse proxy (settings.TRUSTED_PROXIES), the TCP
    peer is the proxy, so the rightmost X-Forwarded-For entry is used.
    Without trusted proxies configured, X-Forwarded-For is ignored
    entirely (it is trivially spoofable).
    """
    peer = request.client.host if request.client else "unknown"
    if not settings.TRUSTED_PROXIES:
        return peer
    try:
        peer_ip = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    if not any(peer_ip in ipaddress.ip_network(c) for c in settings.TRUSTED_PROXIES):
        return peer
    forwarded = request.headers.get("x-forwarded-for", "")
    entries = [e.strip() for e in forwarded.split(",") if e.strip()]
    return entries[-1] if entries else peer


def _prune(bucket: dict[str, list[float]], key: str, window: float) -> list[float]:
    now = time.monotonic()
    attempts = [t for t in bucket.get(key, []) if now - t < window]
    if attempts:
        bucket[key] = attempts
    else:
        bucket.pop(key, None)  # evict empty keys so the dict can't grow
    return attempts


def _throttled(ip: str) -> bool:
    with _login_lock:
        return len(_prune(_login_attempts, ip, _LOGIN_WINDOW_SECONDS)) >= _MAX_LOGIN_ATTEMPTS


def _register_throttled(ip: str) -> bool:
    with _login_lock:
        return (
            len(_prune(_register_attempts, ip, _REGISTER_WINDOW_SECONDS))
            >= _MAX_REGISTER_ATTEMPTS
        )


def _record_failure(ip: str) -> None:
    with _login_lock:
        _login_attempts.setdefault(ip, []).append(time.monotonic())


def _record_register(ip: str) -> None:
    with _login_lock:
        _register_attempts.setdefault(ip, []).append(time.monotonic())


def _clear_failures(ip: str) -> None:
    """A successful login resets the failure bucket for that IP."""
    with _login_lock:
        _login_attempts.pop(ip, None)


def _set_session_cookie(
    response: RedirectResponse,
    user_id: int,
    email: str,
    request: Request,
    epoch: int = 0,
) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=sign_session(user_id, email, epoch),
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https" or settings.FORCE_SECURE_COOKIE,
        max_age=SESSION_TTL_SECONDS,
    )


def _render_auth(request: Request, template: str, status_code: int = 200, **kwargs):
    from app import render

    return render(template, request, status_code=status_code, **kwargs)


def register_page(request: Request):
    _require_multi()
    return _render_auth(request, "register.html")


def register_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    confirm: str = Form(...),
):
    _require_multi()
    email = email.strip().lower()
    errors: list[str] = []
    if not _EMAIL_RE.match(email):
        errors.append("Enter a valid email address.")
    if len(password) < _MIN_PASSWORD_LENGTH:
        errors.append(f"Password must be at least {_MIN_PASSWORD_LENGTH} characters.")
    if len(password) > _MAX_PASSWORD_LENGTH:
        errors.append(f"Password must be at most {_MAX_PASSWORD_LENGTH} characters.")
    if password != confirm:
        errors.append("Passwords do not match.")
    if errors:
        return _render_auth(
            request, "register.html", error=" ".join(errors), email=email
        )

    ip = _client_ip(request)
    if _register_throttled(ip):
        return _render_auth(
            request,
            "register.html",
            error="Too many signup attempts from this address. Try again later.",
            email=email,
        )

    with get_db() as db:
        existing = db.execute(
            "SELECT id FROM users WHERE email = ?", (email,)
        ).fetchone()
        if existing:
            return _render_auth(
                request,
                "register.html",
                error="An account with that email already exists.",
                email=email,
            )
        _record_register(ip)
        try:
            db.execute(
                """
                INSERT INTO users (email, password_hash, plan, trial_ends_at, email_verified)
                VALUES (?, ?, 'trial', ?, 0)
                """,
                (email, hash_password(password), _trial_end()),
            )
        except Exception as e:
            # Duplicate-email race between SELECT and INSERT: the UNIQUE
            # constraint fires as IntegrityError (sqlite) / UniqueViolation
            # (psycopg). Render the same friendly message as the SELECT path.
            if e.__class__.__name__ in ("IntegrityError", "UniqueViolation"):
                return _render_auth(
                    request,
                    "register.html",
                    error="An account with that email already exists.",
                    email=email,
                )
            raise
        user = db.execute(
            "SELECT id FROM users WHERE email = ?", (email,)
        ).fetchone()

    # Verification email: best-effort. Signup succeeds even if system SMTP
    # is unconfigured or the send fails; the dashboard banner offers resend.
    try:
        import logging

        import verification
        from email_sender import send_system_email

        token = verification.issue_token(user["id"], "verify")
        link = f"{settings.BASE_URL.rstrip('/')}/verify-email?token={token}"
        send_system_email(
            email,
            "Verify your FeedEcho account",
            f"Welcome to FeedEcho. Verify your email address by opening this link:\n\n{link}\n\n"
            "This link expires in 24 hours. If you did not create this account, you can ignore this email.",
        )
    except Exception as exc:  # noqa: BLE001 — verification must not block signup
        logging.getLogger("feedecho").warning(
            "Verification email for %s failed: %s", email, exc
        )

    response = RedirectResponse(url="/", status_code=302)
    _set_session_cookie(response, user["id"], email, request)
    return response


def login_page(request: Request):
    if not settings.MULTI:
        return _render_auth(request, "login.html")
    return _render_auth(request, "login.html", multi=True)


def login_submit(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    token: str = Form(""),
):
    if not settings.MULTI:
        # Single mode: shared-secret token (original behavior).
        import secrets as _secrets

        if not settings.AUTH_TOKEN:
            return RedirectResponse(url="/", status_code=302)
        if token and _secrets.compare_digest(token, settings.AUTH_TOKEN):
            response = RedirectResponse(url="/", status_code=302)
            response.set_cookie(
                key="feedecho_auth",
                value=token,
                httponly=True,
                samesite="lax",
                secure=request.url.scheme == "https" or settings.FORCE_SECURE_COOKIE,
            )
            return response
        return _render_auth(request, "login.html", error="Invalid token")

    ip = _client_ip(request)
    if _throttled(ip):
        return _render_auth(
            request,
            "login.html",
            multi=True,
            error="Too many failed attempts. Try again in a few minutes.",
        )

    email = email.strip().lower()
    with get_db() as db:
        user = db.execute(
            "SELECT id, email, password_hash, suspended, session_epoch"
            " FROM users WHERE email = ?",
            (email,),
        ).fetchone()
    if user is None:
        # Pay the same scrypt cost as a known-email attempt so response
        # timing does not reveal whether an account exists.
        verify_password(password, _DUMMY_HASH)
        _record_failure(ip)
        return _render_auth(
            request, "login.html", multi=True, error="Invalid email or password"
        )
    if not verify_password(password, user["password_hash"]):
        _record_failure(ip)
        return _render_auth(
            request, "login.html", multi=True, error="Invalid email or password"
        )
    _clear_failures(ip)
    if user["suspended"]:
        return _render_auth(
            request, "login.html", multi=True, error="This account is suspended."
        )

    response = RedirectResponse(url="/", status_code=302)
    _set_session_cookie(response, user["id"], user["email"], request, user["session_epoch"])
    return response


def logout():
    response = RedirectResponse(url="/login", status_code=302)
    # Both cookies: multi mode authenticates on the signed session, single
    # mode on the shared-secret cookie. Clearing only one made logout a
    # no-op in the other mode.
    response.delete_cookie(COOKIE_NAME)
    response.delete_cookie("feedecho_auth")
    return response


# ── Password reset ────────────────────────────────────────────────────────────

def _forgot_throttled(ip: str) -> bool:
    now = time.time()
    bucket = [t for t in _forgot_attempts.get(ip, []) if now - t < _FORGOT_WINDOW]
    _forgot_attempts[ip] = bucket
    return len(bucket) >= _FORGOT_LIMIT


def forgot_page(request: Request):
    _require_multi()
    return _render_auth(request, "forgot_password.html")


def _send_reset_email(user_id: int, email: str) -> None:
    """Issue a reset token and email the link (runs in a worker thread).

    Called fire-and-forget so the HTTP response never waits on SMTP —
    this also removes the timing oracle between known/unknown addresses.
    """
    import verification
    from email_sender import send_system_email

    try:
        if not verification.resend_allowed(user_id, "reset"):
            return
        token = verification.issue_token(user_id, "reset")
        link = f"{settings.BASE_URL.rstrip('/')}/reset-password?token={token}"
        send_system_email(
            email,
            "Reset your FeedEcho password",
            f"Reset your FeedEcho password by opening this link:\n\n{link}\n\n"
            "This link expires in 24 hours. If you did not request a reset, you can ignore this email.",
        )
    except Exception:  # noqa: BLE001 — enumeration-safe; ERROR level so a
        logging.getLogger("feedecho").error(  # broken mailer is detectable
            "Password reset email failed for user %s", user_id, exc_info=True
        )


def forgot_submit(request: Request, email: str = Form("")):
    _require_multi()
    email = email.strip().lower()
    # Uniform response: never reveal whether an account exists. The email
    # dispatch happens in a background thread (see _send_reset_email), so
    # the response time is identical for known and unknown addresses.
    ip = _client_ip(request)
    if not _forgot_throttled(ip):
        with get_db() as db:
            user = db.execute(
                "SELECT id, email FROM users WHERE email = ?", (email,)
            ).fetchone()
        if user is not None:
            _forgot_attempts.setdefault(ip, []).append(time.time())
            import asyncio

            asyncio.create_task(
                asyncio.to_thread(_send_reset_email, user["id"], user["email"])
            )
    return _render_auth(request, "forgot_password.html", sent=True)


def reset_page(request: Request, token: str = ""):
    _require_multi()
    if not token:
        return _render_auth(request, "reset_password.html", error="Missing reset token.")
    import verification

    if verification.peek_token(token, "reset") is None:
        return _render_auth(
            request, "reset_password.html",
            error="This reset link is invalid or has expired.",
        )
    return _render_auth(request, "reset_password.html", token=token)


def reset_submit(
    request: Request,
    token: str = Form(""),
    password: str = Form(""),
    confirm: str = Form(""),
):
    _require_multi()
    import verification

    # Peek first: validation errors re-render with the SAME token (no
    # reissue, no throttle bypass, no reset-window extension). The token
    # is consumed only after the new password passes validation.
    uid = verification.peek_token(token, "reset") if token else None
    if uid is None:
        return _render_auth(
            request, "reset_password.html",
            error="This reset link is invalid or has expired.",
        )
    errors: list[str] = []
    if len(password) < _MIN_PASSWORD_LENGTH:
        errors.append(f"Password must be at least {_MIN_PASSWORD_LENGTH} characters.")
    if len(password) > _MAX_PASSWORD_LENGTH:
        errors.append(f"Password must be at most {_MAX_PASSWORD_LENGTH} characters.")
    if password != confirm:
        errors.append("Passwords do not match.")
    if errors:
        return _render_auth(
            request, "reset_password.html",
            error=" ".join(errors), token=token,
        )

    if verification.consume_token(token, "reset") is None:
        return _render_auth(
            request, "reset_password.html",
            error="This reset link is invalid or has expired.",
        )

    with get_db() as db:
        # Bump the session epoch: every previously issued session cookie
        # for this user dies with the password change.
        db.execute(
            "UPDATE users SET password_hash = ?,"
            " session_epoch = session_epoch + 1 WHERE id = ?",
            (hash_password(password), uid),
        )
    logging.getLogger("feedecho").info("User %s reset their password", uid)
    return RedirectResponse(url="/login?reset=1", status_code=302)
