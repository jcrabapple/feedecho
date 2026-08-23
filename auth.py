"""Multi-mode authentication: register, login, logout, and session setup.

Single mode never registers these routes' behavior — every handler
short-circuits with 404 unless FEEDCHO_MODE=multi. Sessions are stateless
HMAC tokens from security.py; the shared-secret AuthMiddleware in app.py
routes to this flow only when settings.MULTI is set.
"""

from __future__ import annotations

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

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MIN_PASSWORD_LENGTH = 8
_TRIAL_DAYS = 14

COOKIE_NAME = "feedecho_session"


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


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _trial_end() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=_TRIAL_DAYS)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _throttled(ip: str) -> bool:
    now = time.monotonic()
    with _login_lock:
        attempts = [
            t for t in _login_attempts.get(ip, []) if now - t < _LOGIN_WINDOW_SECONDS
        ]
        _login_attempts[ip] = attempts
        return len(attempts) >= _MAX_LOGIN_ATTEMPTS


def _record_failure(ip: str) -> None:
    with _login_lock:
        _login_attempts.setdefault(ip, []).append(time.monotonic())


def _set_session_cookie(response: RedirectResponse, user_id: int, email: str, request: Request) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=sign_session(user_id, email),
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
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
    if password != confirm:
        errors.append("Passwords do not match.")
    if errors:
        return _render_auth(
            request, "register.html", error=" ".join(errors), email=email
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
        db.execute(
            """
            INSERT INTO users (email, password_hash, plan, trial_ends_at, email_verified)
            VALUES (?, ?, 'trial', ?, 0)
            """,
            (email, hash_password(password), _trial_end()),
        )
        user = db.execute(
            "SELECT id FROM users WHERE email = ?", (email,)
        ).fetchone()

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

        from app import _AUTH_TOKEN

        if not _AUTH_TOKEN:
            return RedirectResponse(url="/", status_code=302)
        if token and _secrets.compare_digest(token, _AUTH_TOKEN):
            response = RedirectResponse(url="/", status_code=302)
            response.set_cookie(
                key="feedecho_auth",
                value=token,
                httponly=True,
                samesite="lax",
                secure=request.url.scheme == "https",
            )
            return response
        return _render_auth(request, "login.html", error="Invalid token")

    ip = request.client.host if request.client else "unknown"
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
            "SELECT id, email, password_hash, suspended FROM users WHERE email = ?",
            (email,),
        ).fetchone()
    if user is None or not verify_password(password, user["password_hash"]):
        _record_failure(ip)
        return _render_auth(
            request, "login.html", multi=True, error="Invalid email or password"
        )
    if user["suspended"]:
        return _render_auth(
            request, "login.html", multi=True, error="This account is suspended."
        )

    response = RedirectResponse(url="/", status_code=302)
    _set_session_cookie(response, user["id"], user["email"], request)
    return response


def logout():
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(COOKIE_NAME)
    return response
