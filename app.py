"""FeedEcho — self-hosted RSS feed cross-poster.

Routes feed items to Mastodon accounts or email addresses. Web UI for managing
feeds, accounts, echoes, settings, and viewing post history.
"""

import os
import re
import logging
import time
import uuid
import secrets as _secrets
from pathlib import Path
from datetime import datetime, timezone

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from database import get_db, init_db
import auth
from auth import current_user_id
import security
from feed_parser import fetch_feed, SSRFError, validate_outbound_url
from mastodon import test_connection, post_status, verify_credentials
from bluesky import (
    BlueskyAuthError,
    BlueskyError,
    create_session as bluesky_create_session,
    normalize_handle as bluesky_normalize_handle,
    resolve_pds as bluesky_resolve_pds,
    session_expiry as bluesky_session_expiry,
    test_connection as test_bluesky_connection,
)
from template_engine import render_template, available_variables, validate_template
from scheduler import start_scheduler, stop_scheduler, check_feed
from oauth import get_authorize_url, exchange_code, verify_state
from email_sender import get_smtp_settings, test_smtp_connection

import logging_setup

logging_setup.setup_logging()
logger = logging.getLogger("feedecho")
access_logger = logging.getLogger("feedecho.access")

app = FastAPI(title="FeedEcho", version="1.13.3")

BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

jinja = Environment(
    loader=FileSystemLoader(BASE_DIR / "templates"),
    autoescape=select_autoescape(["html"]),
)


def _as_utc_naive(value) -> datetime | None:
    """Normalize a stored timestamp (sqlite TEXT or psycopg datetime) to
    a naive UTC datetime, or None if unparseable/empty.

    Invariant: every writer stores UTC in these TIMESTAMP columns (app code
    generates UTC strings via scheduler._now(); sqlite CURRENT_TIMESTAMP is
    UTC; the stock Postgres container runs with timezone UTC). Naive values
    are therefore treated as UTC here — do not add writers that store
    local time.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _iso_utc(value) -> str:
    """ISO-8601 UTC ('2026-08-24T06:46:00Z') for the browser-side
    local-time conversion; '' when the value is missing/unparseable."""
    dt = _as_utc_naive(value)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else ""


def _utc_text(value) -> str:
    """Plain 'YYYY-MM-DD HH:MM:SS' UTC fallback text (shown when JS is off)."""
    dt = _as_utc_naive(value)
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""


jinja.filters["iso_utc"] = _iso_utc
jinja.filters["utc_text"] = _utc_text

OAUTH_SESSION_COOKIE = "feedecho_oauth_session"
OAUTH_SESSION_MAX_AGE = 10 * 60

# ── Optional shared-secret auth ──────────────────────────────────────────────
# If FEEDCHO_AUTH_TOKEN is set, all requests must include it as either:
#   - Cookie: feedecho_auth=<token>   (set by the login page)
#   - X-Auth-Token: <token>           (for API/programmatic access)
# If the env var is unset, auth is disabled (original behavior).
# (Single-tenant mode only; multi mode replaces this with sessions.)
import settings
from settings import AUTH_TOKEN  # noqa: F401  (re-exported for tests/legacy)

# Paths exempt from auth: health check + static files + OAuth callback.
# Only /oauth/callback must be reachable without a cookie (Mastodon redirects
# the user's browser here). /oauth/connect requires auth so unauthenticated
# users cannot trigger outbound requests to arbitrary instance URLs.
_AUTH_EXEMPT_PATHS = {"/healthz", "/favicon.svg", "/static", "/oauth/callback"}
_AUTH_EXEMPT_PREFIXES = ("/static",)


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Request-id threading + structured access log.

    Outermost middleware: accepts a client X-Request-ID (validated against
    a conservative charset + length cap; anything else is replaced with a
    generated uuid) and attaches it to every log record emitted while the
    request is handled, to the response header, and to one access-log line
    per request.
    """

    _VALID_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

    async def dispatch(self, request: Request, call_next):
        raw = request.headers.get("x-request-id", "")
        request_id = raw if self._VALID_ID.fullmatch(raw) else uuid.uuid4().hex
        token = logging_setup.set_request_id(request_id)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # Log a 500 access line while the id is still attached, then
            # reset scope-safely and re-raise (ServerErrorMiddleware logs
            # the traceback, and its record will carry the id).
            duration_ms = round((time.perf_counter() - start) * 1000, 1)
            peer = request.client.host if request.client else "-"
            uid = getattr(request.state, "user_id", None)
            access_logger.error(
                "%s %s 500 %sms peer=%s%s (unhandled exception)",
                request.method,
                request.url.path,
                duration_ms,
                peer,
                f" user={uid}" if uid is not None else "",
            )
            logging_setup.reset_request_id(token)
            raise
        # Access log emitted while the request id is still attached to the
        # context, so its own record carries the id too.
        response.headers["X-Request-ID"] = request_id
        duration_ms = round((time.perf_counter() - start) * 1000, 1)
        peer = request.client.host if request.client else "-"
        uid = getattr(request.state, "user_id", None)
        path = request.url.path
        log = (
            access_logger.debug
            if path == "/healthz"
            else access_logger.info
        )
        log(
            "%s %s %s %sms peer=%s%s",
            request.method,
            path,
            response.status_code,
            duration_ms,
            peer,
            f" user={uid}" if uid is not None else "",
        )
        logging_setup.reset_request_id(token)
        return response


class AuthMiddleware(BaseHTTPMiddleware):
    """Shared-secret auth (single mode) or session auth (multi mode).

    Single mode: FEEDCHO_AUTH_TOKEN unset → no-op; set → cookie/header
    token required. Multi mode: feedecho_session cookie required on all
    paths except the public set. Mode is read per request so tests can
    flip settings.MULTI without reloading the app.
    """

    _MULTI_EXEMPT_PATHS = {
        "/healthz",
        "/favicon.svg",
        "/oauth/callback",
        "/register",
        "/login",
        "/logout",
        # Email links are opened unauthenticated.
        "/verify-email",
        "/forgot-password",
        "/reset-password",
        # Public hosted-service disclosure page.
        "/about",
    }
    _MULTI_EXEMPT_PREFIXES = ("/static",)

    async def dispatch(self, request: Request, call_next):
        if settings.MULTI:
            return await self._multi(request, call_next)
        return await self._single(request, call_next)

    async def _single(self, request: Request, call_next):
        if not settings.AUTH_TOKEN:
            return await call_next(request)

        path = request.url.path

        # Allow health check and static files without auth
        if path in _AUTH_EXEMPT_PATHS or path.startswith(tuple(_AUTH_EXEMPT_PREFIXES)):
            return await call_next(request)

        # Check cookie or header
        token = (
            request.cookies.get("feedecho_auth")
            or request.headers.get("x-auth-token")
        )

        if (
            token
            and settings.AUTH_TOKEN
            and _secrets.compare_digest(token, settings.AUTH_TOKEN)
        ):
            return await call_next(request)

        # If this is the login endpoint, let it through
        if path == "/login":
            return await call_next(request)

        # Redirect browser requests to login, 401 for API/JSON
        accept = request.headers.get("accept", "")
        if "text/html" in accept and request.method == "GET":
            return RedirectResponse(url="/login", status_code=302)
        return JSONResponse(
            {"detail": "Authentication required"}, status_code=401
        )

    async def _multi(self, request: Request, call_next):
        path = request.url.path
        if path in self._MULTI_EXEMPT_PATHS or path.startswith(
            tuple(self._MULTI_EXEMPT_PREFIXES)
        ):
            return await call_next(request)

        token = request.cookies.get("feedecho_session")
        claims = security.read_session(token) if token else None
        if claims:
            # Suspension and session-epoch are enforced per request, not
            # just at login: a valid HMAC session for a suspended account,
            # or one issued before the last password reset, is rejected.
            with get_db() as db:
                row = db.execute(
                    "SELECT suspended, session_epoch FROM users WHERE id = ?",
                    (claims["user_id"],),
                ).fetchone()
            if (
                row
                and not row["suspended"]
                and row["session_epoch"] == claims.get("epoch", 0)
            ):
                request.state.user_id = claims["user_id"]
                return await call_next(request)

        accept = request.headers.get("accept", "")
        if "text/html" in accept and request.method == "GET":
            return RedirectResponse(url="/login", status_code=302)
        return JSONResponse(
            {"detail": "Authentication required"}, status_code=401
        )


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return auth.login_page(request)


@app.post("/login")
async def login_submit(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    token: str = Form(""),
):
    return auth.login_submit(request, email=email, password=password, token=token)


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return auth.register_page(request)


@app.post("/register")
async def register_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    confirm: str = Form(...),
):
    return auth.register_submit(
        request, email=email, password=password, confirm=confirm
    )


@app.post("/logout")
async def logout():
    return auth.logout()


@app.get("/forgot-password", response_class=HTMLResponse)
async def forgot_page(request: Request):
    return auth.forgot_page(request)


@app.post("/forgot-password")
async def forgot_submit(request: Request, email: str = Form("")):
    return auth.forgot_submit(request, email=email)


@app.get("/reset-password", response_class=HTMLResponse)
async def reset_page(request: Request, token: str = ""):
    return auth.reset_page(request, token=token)


@app.post("/reset-password")
async def reset_submit(
    request: Request,
    token: str = Form(""),
    password: str = Form(""),
    confirm: str = Form(""),
):
    return auth.reset_submit(
        request, token=token, password=password, confirm=confirm
    )


app.add_middleware(AuthMiddleware)
# Outermost: request-id threading + access logging wraps auth.
app.add_middleware(RequestIdMiddleware)


def _trial_context(request: Request) -> dict:
    """Multi-mode template context: current user's email, plan, trial state.

    Returns {} in single mode or when no authenticated user is present
    (exempt routes like /login and /register).
    """
    if not settings.MULTI:
        return {}
    uid = getattr(request.state, "user_id", None)
    if uid is None:
        return {}
    with get_db() as db:
        row = db.execute(
            "SELECT email, plan, trial_ends_at, is_admin, email_verified"
            " FROM users WHERE id = ?",
            (uid,),
        ).fetchone()
    if not row:
        return {}
    ctx = {
        "current_user_email": row["email"],
        "plan": row["plan"] or "trial",
        "is_admin": bool(row["is_admin"]),
        "email_verified": bool(row["email_verified"]),
    }
    ends = row["trial_ends_at"]
    if ends:
        try:
            end = datetime.fromisoformat(str(ends).replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            if end <= now:
                ctx["trial_expired"] = True
            else:
                ctx["trial_days_left"] = max(1, (end - now).days)
                # ISO date for a <time class="local-time"> element; the
                # browser renders it in the viewer's locale (issue #6).
                ctx["trial_ends_date"] = end.strftime("%Y-%m-%d")
        except ValueError:
            logger.warning("Unparseable trial_ends_at for user %s: %r", uid, ends)
    return ctx


def render(name: str, request: Request, status_code: int = 200, **kwargs) -> HTMLResponse:
    template = jinja.get_template(name)
    context = {"MULTI": settings.MULTI}
    if settings.MULTI:
        context.update(_trial_context(request))
    context.update(kwargs)
    return HTMLResponse(
        template.render(request=request, **context), status_code=status_code
    )


def validate_url(url: str) -> str:
    """Validate a URL has http or https scheme and is safe for outbound requests.

    Combines scheme check with SSRF protection (blocks private IPs,
    internal hostnames, non-http schemes, embedded credentials).
    """
    if not re.match(r"^https?://", url):
        raise HTTPException(status_code=400, detail="URL must start with http:// or https://")
    try:
        validate_outbound_url(url)
    except SSRFError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return url.rstrip("/")


from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.validate_config()
    init_db()
    _bootstrap_admin()
    _revalidate_stored_templates()
    start_scheduler()
    logger.info("FeedEcho started")
    yield
    stop_scheduler()


def _bootstrap_admin() -> None:
    """Promote the user named by FEEDCHO_ADMIN_EMAIL (idempotent).

    The hosted deployment has no other bootstrap path: the first admin is
    promoted from the environment, then manages the rest from the admin
    dashboard. No-op when unset or in single mode. Email comparison is
    case-insensitive (registration normalizes to lowercase). Note: while
    the env var is set, the named account is re-promoted on every startup
    — an intentional recovery hatch, so a dashboard demotion of that
    account is only permanent after removing the env var.
    """
    if not settings.MULTI or not settings.ADMIN_EMAIL:
        return
    with get_db() as db:
        row = db.execute(
            "SELECT id, is_admin FROM users WHERE LOWER(email) = LOWER(?)",
            (settings.ADMIN_EMAIL,),
        ).fetchone()
        if row is None:
            logger.warning(
                "FEEDCHO_ADMIN_EMAIL matches no registered user yet"
            )
            return
        if row["is_admin"]:
            return
        db.execute(
            "UPDATE users SET is_admin = 1 WHERE id = ?", (row["id"],)
        )
    logger.info("Promoted FEEDCHO_ADMIN_EMAIL user (id %s) to admin", row["id"])


app.router.lifespan_context = lifespan


# ── Helpers ──────────────────────────────────────────────────────────────────

def _get_smtp_settings(mask_password: bool = False, user_id: int = 1):
    """Load SMTP settings as a flat dict for templates.

    If mask_password is True, replaces the SMTP password with a placeholder
    so it's never sent to the browser. Used on settings/accounts pages.
    """
    with get_db() as db:
        rows = db.execute(
            "SELECT key, value FROM settings WHERE key LIKE 'smtp_%' AND user_id = ?",
            (user_id,),
        ).fetchall()
    if not rows:
        return {}
    smtp = {row["key"]: row["value"] for row in rows}
    if mask_password and smtp.get("smtp_password"):
        smtp["smtp_password"] = "********"
    return smtp


def _get_all_accounts(user_id: int = 1):
    """Fetch Mastodon, email, and Bluesky accounts for one user."""
    with get_db() as db:
        mastodon = db.execute(
            "SELECT id, name, username, instance, created_at FROM accounts"
            " WHERE user_id = ? ORDER BY name",
            (user_id,),
        ).fetchall()
        email = db.execute(
            "SELECT id, name, email, created_at FROM email_accounts"
            " WHERE user_id = ? ORDER BY name",
            (user_id,),
        ).fetchall()
        bluesky = db.execute(
            "SELECT id, name, handle, did, pds, created_at FROM bluesky_accounts"
            " WHERE user_id = ? ORDER BY handle",
            (user_id,),
        ).fetchall()
    return mastodon, email, bluesky


def _render_accounts_error(request: Request, message: str) -> HTMLResponse:
    """Render the accounts page with an error banner."""
    uid = current_user_id(request)
    mastodon_accounts, email_accounts, bluesky_accounts = _get_all_accounts(uid)
    smtp_settings = _get_smtp_settings(mask_password=True, user_id=uid)
    return render(
        "accounts.html",
        request,
        mastodon_accounts=mastodon_accounts,
        email_accounts=email_accounts,
        bluesky_accounts=bluesky_accounts,
        smtp_configured=bool(smtp_settings.get("smtp_host")),
        smtp_settings=smtp_settings,
        error=message,
    )


# ── Pages ───────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    uid = current_user_id(request)
    with get_db() as db:
        mastodon_accounts = db.execute(
            "SELECT COUNT(*) as c FROM accounts WHERE user_id = ?", (uid,)
        ).fetchone()["c"]
        email_accounts = db.execute(
            "SELECT COUNT(*) as c FROM email_accounts WHERE user_id = ?", (uid,)
        ).fetchone()["c"]
        bluesky_accounts = db.execute(
            "SELECT COUNT(*) as c FROM bluesky_accounts WHERE user_id = ?", (uid,)
        ).fetchone()["c"]
        feeds = db.execute(
            "SELECT * FROM feeds WHERE deleted_at IS NULL AND user_id = ? ORDER BY name",
            (uid,),
        ).fetchall()
        echoes = db.execute("""
            SELECT e.*, f.name as feed_name,
                   CASE
                     WHEN e.destination_type = 'mastodon' THEN '@' || a.username || '@' || REPLACE(a.instance, 'https://', '')
                     WHEN e.destination_type = 'email' THEN ea.name || ' (' || ea.email || ')'
                     WHEN e.destination_type = 'bluesky' THEN '@' || b.handle
                   END as destination_name
            FROM echoes e
            JOIN feeds f ON e.feed_id = f.id
            LEFT JOIN accounts a ON e.destination_type = 'mastodon' AND e.destination_id = a.id AND a.user_id = e.user_id
            LEFT JOIN email_accounts ea ON e.destination_type = 'email' AND e.destination_id = ea.id AND ea.user_id = e.user_id
            LEFT JOIN bluesky_accounts b ON e.destination_type = 'bluesky' AND e.destination_id = b.id AND b.user_id = e.user_id
            WHERE e.deleted_at IS NULL AND e.user_id = ?
            ORDER BY e.created_at DESC
        """, (uid,)).fetchall()
        recent_posts = db.execute("""
            SELECT pi.*, f.name as feed_name,
                   CASE
                     WHEN e.destination_type = 'mastodon' THEN '@' || a.username || '@' || REPLACE(a.instance, 'https://', '')
                     WHEN e.destination_type = 'email' THEN ea.name || ' (' || ea.email || ')'
                     WHEN e.destination_type = 'bluesky' THEN '@' || b.handle
                   END as account_name,
                   CASE
                     WHEN e.destination_type = 'mastodon' THEN a.instance
                     WHEN e.destination_type = 'email' THEN ea.email
                     WHEN e.destination_type = 'bluesky' THEN b.pds
                   END as instance
            FROM posted_items pi
            JOIN echoes e ON pi.echo_id = e.id
            JOIN feeds f ON e.feed_id = f.id
            LEFT JOIN accounts a ON e.destination_type = 'mastodon' AND e.destination_id = a.id AND a.user_id = e.user_id
            LEFT JOIN email_accounts ea ON e.destination_type = 'email' AND e.destination_id = ea.id AND ea.user_id = e.user_id
            LEFT JOIN bluesky_accounts b ON e.destination_type = 'bluesky' AND e.destination_id = b.id AND b.user_id = e.user_id
            WHERE e.user_id = ?
            ORDER BY pi.posted_at DESC
            LIMIT 20
        """, (uid,)).fetchall()
        stats = {
            "accounts": mastodon_accounts + email_accounts + bluesky_accounts,
            "feeds": len(feeds),
            "echoes": len(echoes),
            "active_echoes": sum(1 for e in echoes if e["enabled"]),
            "total_posts": db.execute(
                "SELECT COUNT(*) AS n FROM posted_items pi JOIN echoes e ON pi.echo_id = e.id"
                " WHERE pi.status = 'success' AND e.user_id = ?",
                (uid,),
            ).fetchone()["n"],
            "failed_posts": db.execute(
                "SELECT COUNT(*) AS n FROM posted_items pi JOIN echoes e ON pi.echo_id = e.id"
                " WHERE pi.status = 'failed' AND e.user_id = ?",
                (uid,),
            ).fetchone()["n"],
        }
    return render("dashboard.html", request, feeds=feeds, echoes=echoes,
                  recent_posts=recent_posts, stats=stats)


# ── Email verification ────────────────────────────────────────────────────────

@app.get("/verify-email", response_class=HTMLResponse)
async def verify_email(token: str = "", request: Request = None):
    from auth import _require_multi
    from verification import consume_token

    _require_multi()
    uid = consume_token(token, "verify") if token else None
    if uid is None:
        return render(
            "error.html", request, status_code=400, code=400,
            message="This verification link is invalid or has expired.",
        )
    with get_db() as db:
        db.execute(
            "UPDATE users SET email_verified = 1 WHERE id = ?", (uid,)
        )
    logger.info("User %s verified their email", uid)
    return RedirectResponse(url="/?verified=1", status_code=302)


@app.post("/resend-verification")
async def resend_verification(request: Request):
    from auth import _require_multi
    from verification import issue_token, resend_allowed

    _require_multi()
    uid = current_user_id(request)
    with get_db() as db:
        row = db.execute(
            "SELECT email, email_verified FROM users WHERE id = ?", (uid,)
        ).fetchone()
    if not row or row["email_verified"]:
        return RedirectResponse(url="/", status_code=302)
    if not resend_allowed(uid, "verify"):
        return render(
            "error.html", request, status_code=400, code=400,
            message="Too many verification emails. Wait a while before trying again.",
        )
    try:
        from email_sender import send_system_email

        token = issue_token(uid, "verify")
        link = f"{settings.BASE_URL.rstrip('/')}/verify-email?token={token}"
        send_system_email(
            row["email"],
            "Verify your FeedEcho account",
            f"Verify your email address by opening this link:\n\n{link}\n\n"
            "This link expires in 24 hours.",
        )
    except Exception as exc:  # noqa: BLE001 — resend failure surfaces in the banner
        logger.warning("Resend verification email for user %s failed: %s", uid, exc)
        return render(
            "error.html", request, status_code=400, code=400,
            message="System email is not configured yet. Try again later.",
        )
    return RedirectResponse(url="/?verification_sent=1", status_code=302)


# ── Admin ─────────────────────────────────────────────────────────────────────

def _admin_uid_or_none(request: Request) -> int | None:
    """The admin's user id, or None when the caller is not an admin.

    404 in single mode (admin routes don't exist there); None in multi
    mode when the authenticated user lacks the role.
    """
    from auth import _require_multi

    _require_multi()
    uid = current_user_id(request)
    return uid if auth.is_admin(uid) else None


def _admin_stats(db) -> dict:
    rows = db.execute(
        "SELECT COUNT(*) AS n,"
        " SUM(CASE WHEN is_admin = 1 THEN 1 ELSE 0 END) AS admins,"
        " SUM(CASE WHEN suspended = 1 THEN 1 ELSE 0 END) AS suspended,"
        " SUM(CASE WHEN email_verified = 1 THEN 1 ELSE 0 END) AS verified,"
        " SUM(CASE WHEN plan = 'trial' AND trial_ends_at > CURRENT_TIMESTAMP"
        " THEN 1 ELSE 0 END) AS active_trials"
        " FROM users WHERE email != 'local'"
    ).fetchone()
    return {k: (rows[k] or 0) for k in ("n", "admins", "suspended", "verified", "active_trials")}


def _admin_guard_last_admin(db, user_id: int, column: str) -> str | None:
    """Error message if an action would leave zero admins, else None.

    column is 'suspended' (guard: last ACTIVE admin) or 'is_admin'
    (guard: last admin bit). Single-connection check + write keeps this
    race-free per request transaction.
    """
    if column == "is_admin":
        # Demoting: preserve at least one admin bit, whatever its state.
        count = db.execute(
            "SELECT COUNT(*) AS n FROM users WHERE is_admin = 1"
        ).fetchone()["n"]
        if (count or 0) <= 1:
            return "Cannot demote the last admin account"
    else:
        # Suspending an admin: keep at least one ACTIVE admin.
        row = db.execute(
            "SELECT is_admin, suspended FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if row and row["is_admin"] and not row["suspended"]:
            active = db.execute(
                "SELECT COUNT(*) AS n FROM users"
                " WHERE is_admin = 1 AND suspended = 0"
            ).fetchone()["n"]
            if (active or 0) <= 1:
                return "Cannot suspend the last active admin account"
    return None


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    uid = _admin_uid_or_none(request)
    if uid is None:
        return render("error.html", request, status_code=403,
                      code=403, message="Admin access required")
    with get_db() as db:
        users = db.execute("""
            SELECT id, email, plan, trial_ends_at, email_verified,
                   suspended, is_admin, created_at
              FROM users
             WHERE email != 'local'
             ORDER BY created_at DESC
        """).fetchall()
        stats = _admin_stats(db)
        smtp_rows = db.execute(
            "SELECT key, value FROM system_settings WHERE key LIKE 'smtp_%'"
        ).fetchall()
    smtp = {row["key"]: row["value"] for row in smtp_rows}
    if "smtp_password" in smtp and smtp["smtp_password"]:
        smtp["smtp_password"] = "\u2022\u2022\u2022\u2022\u2022\u2022 (stored)"
    else:
        smtp["smtp_password"] = ""
    smtp["configured"] = bool(smtp.get("smtp_host") and smtp.get("smtp_port"))
    return render("admin.html", request, users=users, stats=stats, smtp=smtp)


_SMTP_FORM_KEYS = (
    "smtp_host", "smtp_port", "smtp_username", "smtp_password",
    "smtp_from_email", "smtp_from_name", "smtp_use_tls",
)


@app.post("/admin/email")
async def admin_email_save(request: Request):
    uid = _admin_uid_or_none(request)
    if uid is None:
        return render("error.html", request, status_code=403,
                      code=403, message="Admin access required")
    form = await request.form()

    # Validate before storing anything: a bad port or control characters
    # in header fields must fail here, not at send time.
    port_raw = (form.get("smtp_port") or "").strip()
    try:
        port = int(port_raw)
        if not 1 <= port <= 65535:
            raise ValueError
    except (ValueError, TypeError):
        return render("error.html", request, status_code=400,
                      code=400, message="SMTP port must be a number between 1 and 65535")

    from_email = (form.get("smtp_from_email") or "").strip()
    if from_email and not re.match(r"^[^@\s\r\n]+@[^@\s\r\n]+\.[^@\s\r\n]+$", from_email):
        return render("error.html", request, status_code=400,
                      code=400, message="From address is not a valid email address")

    updates: dict[str, str] = {}
    for key in _SMTP_FORM_KEYS:
        if key == "smtp_password":
            pw = form.get(key) or ""
            # No strip: SMTP passwords may legitimately contain leading or
            # trailing whitespace. Blank (or the masked sentinel, which the
            # page never actually renders into an input) keeps the stored
            # value.
            if pw and pw != "\u2022\u2022\u2022\u2022\u2022\u2022 (stored)":
                updates[key] = pw
            continue
        value = (form.get(key) or "").strip()
        if key in ("smtp_host", "smtp_username", "smtp_from_name") and re.search(
            r"[\r\n]", value
        ):
            return render("error.html", request, status_code=400,
                          code=400, message=f"Invalid characters in {key}")
        updates[key] = value
    updates["smtp_use_tls"] = "1" if form.get("smtp_use_tls") else "0"
    with get_db() as db:
        for key, value in updates.items():
            db.execute(
                "INSERT INTO system_settings (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
    logger.info("Admin %s updated system email settings", uid)
    return RedirectResponse(url="/admin#email", status_code=302)


@app.post("/admin/email/test")
async def admin_email_test(request: Request):
    uid = _admin_uid_or_none(request)
    if uid is None:
        return render("error.html", request, status_code=403,
                      code=403, message="Admin access required")
    with get_db() as db:
        row = db.execute(
            "SELECT email FROM users WHERE id = ?", (uid,)
        ).fetchone()
    target = row["email"] if row else ""
    from email_sender import test_system_smtp_connection

    ok, message = test_system_smtp_connection(to_email=target)
    logger.info("Admin %s system email test: %s (%s)", uid, "ok" if ok else "failed", message)
    return render(
        "admin_email_result.html", request,
        ok=ok, message=message, target=target,
    )


@app.post("/admin/users/{user_id}/suspend")
async def admin_suspend(user_id: int, request: Request):
    """Set suspended=1 (atomic target state, idempotent)."""
    uid = _admin_uid_or_none(request)
    if uid is None:
        return render("error.html", request, status_code=403,
                      code=403, message="Admin access required")
    if user_id == uid:
        return render("error.html", request, status_code=400,
                      code=400, message="You cannot suspend your own account")
    with get_db() as db:
        row = db.execute(
            "SELECT id FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return render("error.html", request, status_code=404,
                          code=404, message="User not found")
        guard = _admin_guard_last_admin(db, user_id, "suspended")
        if guard:
            return render("error.html", request, status_code=400,
                          code=400, message=guard)
        db.execute(
            "UPDATE users SET suspended = 1 WHERE id = ? AND suspended = 0",
            (user_id,),
        )
    logger.info("Admin %s suspended user %s", uid, user_id)
    return RedirectResponse(url="/admin", status_code=302)


@app.post("/admin/users/{user_id}/unsuspend")
async def admin_unsuspend(user_id: int, request: Request):
    uid = _admin_uid_or_none(request)
    if uid is None:
        return render("error.html", request, status_code=403,
                      code=403, message="Admin access required")
    with get_db() as db:
        row = db.execute(
            "SELECT id FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return render("error.html", request, status_code=404,
                          code=404, message="User not found")
        db.execute(
            "UPDATE users SET suspended = 0 WHERE id = ? AND suspended = 1",
            (user_id,),
        )
    logger.info("Admin %s unsuspended user %s", uid, user_id)
    return RedirectResponse(url="/admin", status_code=302)


@app.post("/admin/users/{user_id}/promote")
async def admin_promote(user_id: int, request: Request):
    uid = _admin_uid_or_none(request)
    if uid is None:
        return render("error.html", request, status_code=403,
                      code=403, message="Admin access required")
    with get_db() as db:
        row = db.execute(
            "SELECT id FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return render("error.html", request, status_code=404,
                          code=404, message="User not found")
        db.execute(
            "UPDATE users SET is_admin = 1 WHERE id = ? AND is_admin = 0",
            (user_id,),
        )
    logger.info("Admin %s promoted user %s", uid, user_id)
    return RedirectResponse(url="/admin", status_code=302)


@app.post("/admin/users/{user_id}/demote")
async def admin_demote(user_id: int, request: Request):
    uid = _admin_uid_or_none(request)
    if uid is None:
        return render("error.html", request, status_code=403,
                      code=403, message="Admin access required")
    if user_id == uid:
        return render("error.html", request, status_code=400,
                      code=400, message="You cannot demote your own account")
    with get_db() as db:
        row = db.execute(
            "SELECT id FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return render("error.html", request, status_code=404,
                          code=404, message="User not found")
        guard = _admin_guard_last_admin(db, user_id, "is_admin")
        if guard:
            return render("error.html", request, status_code=400,
                          code=400, message=guard)
        db.execute(
            "UPDATE users SET is_admin = 0 WHERE id = ? AND is_admin = 1",
            (user_id,),
        )
    logger.info("Admin %s demoted user %s", uid, user_id)
    return RedirectResponse(url="/admin", status_code=302)


@app.get("/feeds", response_class=HTMLResponse)
async def feeds_page(request: Request):
    uid = current_user_id(request)
    with get_db() as db:
        feeds = db.execute(
            "SELECT * FROM feeds WHERE deleted_at IS NULL AND user_id = ? ORDER BY name",
            (uid,),
        ).fetchall()
        feed_echoes = {}
        for f in feeds:
            feed_echoes[f["id"]] = db.execute(
                "SELECT COUNT(*) as c FROM echoes WHERE feed_id = ? AND deleted_at IS NULL AND user_id = ?",
                (f["id"], uid),
            ).fetchone()["c"]
    return render("feeds.html", request, feeds=feeds, feed_echoes=feed_echoes)


@app.get("/accounts", response_class=HTMLResponse)
async def accounts_page(request: Request):
    uid = current_user_id(request)
    mastodon_accounts, email_accounts, bluesky_accounts = _get_all_accounts(uid)
    smtp_settings = _get_smtp_settings(mask_password=True, user_id=uid)
    smtp_configured = bool(smtp_settings.get("smtp_host"))
    return render("accounts.html", request,
                  mastodon_accounts=mastodon_accounts,
                  email_accounts=email_accounts,
                  bluesky_accounts=bluesky_accounts,
                  smtp_configured=smtp_configured,
                  smtp_settings=smtp_settings)


@app.get("/echoes", response_class=HTMLResponse)
async def echoes_page(request: Request):
    uid = current_user_id(request)
    with get_db() as db:
        echoes = db.execute("""
            SELECT e.*, f.name as feed_name,
                   CASE
                     WHEN e.destination_type = 'mastodon' THEN '@' || a.username || '@' || REPLACE(a.instance, 'https://', '')
                     WHEN e.destination_type = 'email' THEN ea.name || ' (' || ea.email || ')'
                     WHEN e.destination_type = 'bluesky' THEN '@' || b.handle
                   END as destination_name
            FROM echoes e
            JOIN feeds f ON e.feed_id = f.id
            LEFT JOIN accounts a ON e.destination_type = 'mastodon' AND e.destination_id = a.id AND a.user_id = e.user_id
            LEFT JOIN email_accounts ea ON e.destination_type = 'email' AND e.destination_id = ea.id AND ea.user_id = e.user_id
            LEFT JOIN bluesky_accounts b ON e.destination_type = 'bluesky' AND e.destination_id = b.id AND b.user_id = e.user_id
            WHERE e.deleted_at IS NULL AND e.user_id = ?
            ORDER BY e.created_at DESC
        """, (uid,)).fetchall()
        feeds = db.execute(
            "SELECT * FROM feeds WHERE deleted_at IS NULL AND user_id = ? ORDER BY name",
            (uid,),
        ).fetchall()
        mastodon_accounts = db.execute(
            "SELECT id, name, username, instance FROM accounts WHERE user_id = ? ORDER BY name",
            (uid,),
        ).fetchall()
        email_accounts = db.execute(
            "SELECT id, name, email FROM email_accounts WHERE user_id = ? ORDER BY name",
            (uid,),
        ).fetchall()
        bluesky_accounts = db.execute(
            "SELECT id, name, handle FROM bluesky_accounts WHERE user_id = ? ORDER BY handle",
            (uid,),
        ).fetchall()
    return render("echoes.html", request, echoes=echoes, feeds=feeds,
                  mastodon_accounts=mastodon_accounts,
                  email_accounts=email_accounts,
                  bluesky_accounts=bluesky_accounts,
                  template_vars=available_variables())


@app.get("/history", response_class=HTMLResponse)
async def history_page(request: Request):
    uid = current_user_id(request)
    with get_db() as db:
        posts = db.execute("""
            SELECT pi.*, f.name as feed_name,
                   CASE
                     WHEN e.destination_type = 'mastodon' THEN '@' || a.username || '@' || REPLACE(a.instance, 'https://', '')
                     WHEN e.destination_type = 'email' THEN ea.name
                     WHEN e.destination_type = 'bluesky' THEN '@' || b.handle
                   END as account_name,
                   CASE
                     WHEN e.destination_type = 'mastodon' THEN a.instance
                     WHEN e.destination_type = 'email' THEN ea.email
                     WHEN e.destination_type = 'bluesky' THEN b.pds
                   END as instance
            FROM posted_items pi
            JOIN echoes e ON pi.echo_id = e.id
            JOIN feeds f ON e.feed_id = f.id
            LEFT JOIN accounts a ON e.destination_type = 'mastodon' AND e.destination_id = a.id AND a.user_id = e.user_id
            LEFT JOIN email_accounts ea ON e.destination_type = 'email' AND e.destination_id = ea.id AND ea.user_id = e.user_id
            LEFT JOIN bluesky_accounts b ON e.destination_type = 'bluesky' AND e.destination_id = b.id AND b.user_id = e.user_id
            WHERE e.user_id = ?
            ORDER BY pi.posted_at DESC
            LIMIT 100
        """, (uid,)).fetchall()
    return render("history.html", request, posts=posts)


@app.get("/howto", response_class=HTMLResponse)
async def howto_page(request: Request):
    return render("howto.html", request)


@app.get("/about", response_class=HTMLResponse)
async def about_page(request: Request):
    from auth import _require_multi

    _require_multi()  # hosted-service disclosure; 404 in self-hosted mode
    return render("about.html", request)


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    uid = current_user_id(request)
    smtp_settings = _get_smtp_settings(mask_password=True, user_id=uid)
    smtp_configured = bool(smtp_settings.get("smtp_host"))
    with get_db() as db:
        rows = db.execute(
            """SELECT key, value FROM settings
               WHERE key IN ('retry_max_attempts', 'retry_backoff_minutes',
                             'notify_failure_threshold', 'notify_email')
                 AND user_id = ?""",
            (uid,),
        ).fetchall()
        alt_rows = db.execute(
            """SELECT key, value FROM settings
               WHERE key IN ('alt_text_ai_enabled', 'alt_text_ai_base_url',
                             'alt_text_ai_model', 'alt_text_ai_api_key')
                 AND user_id = ?""",
            (uid,),
        ).fetchall()
    retry_notify = {r["key"]: r["value"] for r in rows}
    alt_text_settings = {r["key"]: r["value"] for r in alt_rows}
    alt_text_configured = bool(alt_text_settings.get("alt_text_ai_base_url"))
    # Mask the API key before sending to the browser
    if alt_text_settings.get("alt_text_ai_api_key"):
        alt_text_settings["alt_text_ai_api_key"] = "********"
    return render("settings.html", request,
                  smtp_settings=smtp_settings,
                  smtp_configured=smtp_configured,
                  retry_notify=retry_notify,
                  alt_text_settings=alt_text_settings,
                  alt_text_configured=alt_text_configured)


@app.get("/healthz")
async def healthz():
    logger.debug("health check")
    return {"status": "ok"}


# ── API: Mastodon Accounts ──────────────────────────────────────────────────

@app.post("/api/accounts")
async def add_account(
    request: Request,
    name: str = Form(...),
    username: str = Form(""),
    instance: str = Form(...),
    access_token: str = Form(...),
):
    uid = current_user_id(request)
    instance = validate_url(instance)
    with get_db() as db:
        db.execute(
            "INSERT INTO accounts (name, username, instance, access_token, user_id)"
            " VALUES (?, ?, ?, ?, ?)",
            (name, username or name, instance, access_token, uid),
        )
    return RedirectResponse(url="/accounts", status_code=303)


@app.post("/api/accounts/{account_id}/test")
async def test_account(request: Request, account_id: int):
    uid = current_user_id(request)
    with get_db() as db:
        account = db.execute(
            "SELECT * FROM accounts WHERE id = ? AND user_id = ?",
            (account_id, uid),
        ).fetchone()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    success, message = test_connection(account["instance"], account["access_token"])
    return {"success": success, "message": message}


@app.post("/api/accounts/{account_id}/delete")
async def delete_account(request: Request, account_id: int):
    uid = current_user_id(request)
    with get_db() as db:
        db.execute(
            "DELETE FROM accounts WHERE id = ? AND user_id = ?", (account_id, uid)
        )
    return RedirectResponse(url="/accounts", status_code=303)


# ── API: Email Accounts ─────────────────────────────────────────────────────

@app.post("/api/email-accounts")
async def add_email_account(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
):
    uid = current_user_id(request)
    with get_db() as db:
        db.execute(
            "INSERT INTO email_accounts (name, email, user_id) VALUES (?, ?, ?)"
            " ON CONFLICT(user_id, email) DO UPDATE SET name = excluded.name",
            (name, email, uid),
        )
    return RedirectResponse(url="/accounts?status=email_added", status_code=303)


@app.post("/api/email-accounts/{account_id}/delete")
async def delete_email_account(request: Request, account_id: int):
    uid = current_user_id(request)
    with get_db() as db:
        db.execute(
            "DELETE FROM email_accounts WHERE id = ? AND user_id = ?",
            (account_id, uid),
        )
    return RedirectResponse(url="/accounts", status_code=303)


# ── API: Bluesky Accounts ───────────────────────────────────────────────────

@app.post("/api/bluesky-accounts")
def add_bluesky_account(
    request: Request,
    name: str = Form(...),
    handle: str = Form(...),
    app_password: str = Form(...),
):
    """Resolve the handle, verify the app password, and store the account.

    Synchronous route (threadpool-offloaded): it performs blocking DNS,
    HTTPS, and SQLite work that must not run on the event loop.
    """
    try:
        handle = bluesky_normalize_handle(handle)
    except ValueError as e:
        return _render_accounts_error(request, str(e))

    display_name = name.strip()[:100] or handle

    try:
        did, pds = bluesky_resolve_pds(handle)
        session = bluesky_create_session(pds, handle, app_password)
    except BlueskyAuthError:
        return _render_accounts_error(
            request,
            "Bluesky rejected the app password. Check the handle and app password and try again.",
        )
    except BlueskyError as e:
        return _render_accounts_error(request, str(e))
    except Exception:
        logger.exception("Bluesky account verification failed for %s", handle)
        return _render_accounts_error(
            request, "Could not verify the Bluesky account. Try again."
        )

    with get_db() as db:
        uid = current_user_id(request)
        db.execute(
            """
            INSERT INTO bluesky_accounts (
                name, handle, app_password, did, pds,
                access_jwt, refresh_jwt, session_expires_at, user_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, handle) DO UPDATE SET
                name = excluded.name,
                app_password = excluded.app_password,
                did = excluded.did,
                pds = excluded.pds,
                access_jwt = excluded.access_jwt,
                refresh_jwt = excluded.refresh_jwt,
                session_expires_at = excluded.session_expires_at
            """,
            (
                display_name,
                handle,
                app_password,
                session["did"],
                pds,
                session["access_jwt"],
                session["refresh_jwt"],
                bluesky_session_expiry(session["access_jwt"]),
                uid,
            ),
        )
    return RedirectResponse(url="/accounts?status=bluesky_connected", status_code=303)


@app.post("/api/bluesky-accounts/{account_id}/test")
def test_bluesky_account(request: Request, account_id: int):
    uid = current_user_id(request)
    with get_db() as db:
        account = db.execute(
            "SELECT * FROM bluesky_accounts WHERE id = ? AND user_id = ?",
            (account_id, uid),
        ).fetchone()
    if not account:
        raise HTTPException(status_code=404, detail="Bluesky account not found")
    success, message = test_bluesky_connection(
        account["handle"], account["app_password"]
    )
    return {"success": success, "message": message}


@app.post("/api/bluesky-accounts/{account_id}/delete")
def delete_bluesky_account(request: Request, account_id: int):
    uid = current_user_id(request)
    with get_db() as db:
        dependent = db.execute(
            """
            SELECT COUNT(*) as c FROM echoes
             WHERE destination_type = 'bluesky'
               AND destination_id = ?
               AND deleted_at IS NULL
               AND user_id = ?
            """,
            (account_id, uid),
        ).fetchone()["c"]
    if dependent:
        return _render_accounts_error(
            request,
            "This Bluesky account is used by echoes. Delete or reassign those echoes first.",
        )
    with get_db() as db:
        db.execute(
            "DELETE FROM bluesky_accounts WHERE id = ? AND user_id = ?",
            (account_id, uid),
        )
    return RedirectResponse(url="/accounts?status=bluesky_deleted", status_code=303)


# ── API: Settings ───────────────────────────────────────────────────────────

@app.post("/api/settings/smtp")
async def save_smtp_settings(
    request: Request,
    smtp_host: str = Form(...),
    smtp_port: int = Form(587),
    smtp_username: str = Form(""),
    smtp_password: str = Form(""),
    smtp_from_email: str = Form(""),
    smtp_from_name: str = Form("FeedEcho"),
    smtp_use_tls: str = Form("1"),
):
    uid = current_user_id(request)
    values = {
        "smtp_host": smtp_host,
        "smtp_port": str(smtp_port),
        "smtp_username": smtp_username,
        "smtp_from_email": smtp_from_email,
        "smtp_from_name": smtp_from_name,
        "smtp_use_tls": smtp_use_tls,
    }
    with get_db() as db:
        for key, value in values.items():
            db.execute(
                "INSERT INTO settings (user_id, key, value) VALUES (?, ?, ?)"
                " ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
                (uid, key, value),
            )
        # Only update password if it's not the mask placeholder
        if smtp_password and smtp_password != "********":
            db.execute(
                "INSERT INTO settings (user_id, key, value) VALUES (?, ?, ?)"
                " ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
                (uid, "smtp_password", smtp_password),
            )
    return RedirectResponse(url="/settings?status=saved", status_code=303)


@app.post("/api/settings/smtp/test")
async def test_smtp(
    request: Request,
    test_email: str = Form(""),
):
    success, message = test_smtp_connection(
        test_email, user_id=current_user_id(request)
    )
    return {"success": success, "message": message}


@app.post("/api/settings/retry-notify")
async def save_retry_notify_settings(
    request: Request,
    retry_max_attempts: int = Form(5),
    retry_backoff_minutes: int = Form(5),
    notify_failure_threshold: int = Form(3),
    notify_email: str = Form(""),
):
    uid = current_user_id(request)
    values = {
        "retry_max_attempts": str(max(0, min(retry_max_attempts, 100))),
        "retry_backoff_minutes": str(max(1, min(retry_backoff_minutes, 1440))),
        "notify_failure_threshold": str(max(0, min(notify_failure_threshold, 100))),
        "notify_email": notify_email.strip(),
    }
    with get_db() as db:
        for key, value in values.items():
            db.execute(
                "INSERT INTO settings (user_id, key, value) VALUES (?, ?, ?)"
                " ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
                (uid, key, value),
            )
    return RedirectResponse(url="/settings?status=saved", status_code=303)


@app.post("/api/settings/alt-text")
async def save_alt_text_settings(
    request: Request,
    alt_text_ai_enabled: str = Form(""),
    alt_text_ai_base_url: str = Form(""),
    alt_text_ai_model: str = Form(""),
    alt_text_ai_api_key: str = Form(""),
):
    uid = current_user_id(request)
    values = {
        "alt_text_ai_enabled": "1" if alt_text_ai_enabled else "0",
        "alt_text_ai_base_url": alt_text_ai_base_url.strip().rstrip("/"),
        "alt_text_ai_model": alt_text_ai_model.strip(),
    }
    with get_db() as db:
        for key, value in values.items():
            db.execute(
                "INSERT INTO settings (user_id, key, value) VALUES (?, ?, ?)"
                " ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
                (uid, key, value),
            )
        # Only update API key if it's not the mask placeholder
        if alt_text_ai_api_key and alt_text_ai_api_key != "********":
            db.execute(
                "INSERT INTO settings (user_id, key, value) VALUES (?, ?, ?)"
                " ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
                (uid, "alt_text_ai_api_key", alt_text_ai_api_key.strip()),
            )
    return RedirectResponse(url="/settings?status=saved", status_code=303)


@app.post("/api/settings/alt-text/test")
async def test_alt_text(request: Request):
    """Test the vision API connection with a minimal request."""
    import alt_text
    if not alt_text.is_enabled(user_id=current_user_id(request)):
        return {"success": False, "message": "AI alt text is not configured"}
    try:
        # Use a tiny 1x1 PNG to test the API connection
        import base64
        tiny_png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg=="
        )
        result = alt_text.generate_alt_text(tiny_png, "image/png")
        if result:
            return {"success": True, "message": f"API working. Response: {result[:100]}"}
        return {"success": True, "message": "API reachable (empty response to test image)"}
    except Exception as e:
        return {"success": False, "message": f"API test failed: {e}"}


# ── API: Feeds ──────────────────────────────────────────────────────────────

@app.post("/api/feeds")
async def add_feed(
    request: Request,
    name: str = Form(...),
    url: str = Form(...),
    poll_interval: int = Form(15),
):
    uid = current_user_id(request)
    url = validate_url(url)
    poll_interval = max(1, min(poll_interval, 1440))
    with get_db() as db:
        db.execute(
            "INSERT INTO feeds (name, url, poll_interval, user_id) VALUES (?, ?, ?, ?)",
            (name, url, poll_interval, uid),
        )
    return RedirectResponse(url="/feeds", status_code=303)


@app.post("/api/feeds/{feed_id}/edit")
async def edit_feed(
    request: Request,
    feed_id: int,
    name: str = Form(...),
    url: str = Form(...),
    poll_interval: int = Form(15),
):
    """Update a feed's name, URL, or poll interval in place (issue #3).

    Changing the URL invalidates the cursor: last_item_id belonged to the
    old feed, and comparing it against the new feed's item IDs could
    silently skip (or mis-dedupe) items. Resetting it means the next poll
    re-initializes to the newest item — the same no-backfill behaviour as
    adding a fresh feed. Renames and interval changes keep the cursor.
    """
    uid = current_user_id(request)
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Feed name is required")
    url = validate_url(url)
    poll_interval = max(1, min(poll_interval, 1440))
    with get_db() as db:
        feed = db.execute(
            "SELECT url FROM feeds WHERE id = ? AND deleted_at IS NULL AND user_id = ?",
            (feed_id, uid),
        ).fetchone()
        if not feed:
            raise HTTPException(status_code=404, detail="Feed not found")
        if feed["url"] != url:
            db.execute(
                """
                UPDATE feeds
                   SET name = ?, url = ?, poll_interval = ?, last_item_id = NULL
                 WHERE id = ? AND deleted_at IS NULL AND user_id = ?
                """,
                (name, url, poll_interval, feed_id, uid),
            )
        else:
            db.execute(
                "UPDATE feeds SET name = ?, url = ?, poll_interval = ? "
                "WHERE id = ? AND deleted_at IS NULL AND user_id = ?",
                (name, url, poll_interval, feed_id, uid),
            )
    return RedirectResponse(url="/feeds", status_code=303)


@app.post("/api/feeds/{feed_id}/delete")
async def delete_feed(request: Request, feed_id: int):
    """Soft-delete a feed. Echoes and post history are preserved.

    A hard DELETE would cascade (echoes -> posted_items/digest_items) and wipe
    the cross-post audit trail, so feeds are only marked deleted_at. The feed
    disappears from listings and is skipped by the scheduler, but its echo
    config and history remain on the /echoes and /history pages.
    """
    uid = current_user_id(request)
    with get_db() as db:
        db.execute(
            """
            UPDATE feeds
               SET deleted_at = CURRENT_TIMESTAMP
             WHERE id = ?
               AND deleted_at IS NULL
               AND user_id = ?
            """,
            (feed_id, uid),
        )
    return RedirectResponse(url="/feeds", status_code=303)


@app.post("/api/feeds/{feed_id}/test")
async def test_feed(request: Request, feed_id: int):
    """Fetch a feed and return preview of items."""
    uid = current_user_id(request)
    with get_db() as db:
        feed = db.execute(
            "SELECT * FROM feeds WHERE id = ? AND deleted_at IS NULL AND user_id = ?",
            (feed_id, uid),
        ).fetchone()
    if not feed:
        raise HTTPException(status_code=404, detail="Feed not found")
    try:
        feed_data = fetch_feed(feed["url"])
        preview = {
            "title": feed_data["title"],
            "type": feed_data["type"],
            "item_count": len(feed_data["items"]),
            "items": feed_data["items"][:5],
        }
        return {"success": True, "preview": preview}
    except SSRFError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/feeds/{feed_id}/init")
async def init_feed(request: Request, feed_id: int):
    """Initialize a feed's last_item_id so it only posts new items going forward."""
    uid = current_user_id(request)
    with get_db() as db:
        feed = db.execute(
            "SELECT * FROM feeds WHERE id = ? AND deleted_at IS NULL AND user_id = ?",
            (feed_id, uid),
        ).fetchone()
        if not feed:
            raise HTTPException(status_code=404, detail="Feed not found")
        try:
            feed_data = fetch_feed(feed["url"])
            if feed_data["items"]:
                last_id = feed_data["items"][0]["id"]
                db.execute(
                    "UPDATE feeds SET last_item_id = ? WHERE id = ? AND user_id = ?",
                    (last_id, feed_id, uid),
                )
                return {"success": True, "message": f"Initialized. Last item: {feed_data['items'][0]['title'][:60]}"}
            return {"success": True, "message": "Feed has no items"}
        except SSRFError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            return {"success": False, "error": str(e)}


@app.post("/api/feeds/{feed_id}/pause")
async def pause_feed(request: Request, feed_id: int):
    uid = current_user_id(request)
    with get_db() as db:
        feed = db.execute(
            "SELECT paused FROM feeds WHERE id = ? AND deleted_at IS NULL AND user_id = ?",
            (feed_id, uid),
        ).fetchone()
        if not feed:
            raise HTTPException(status_code=404, detail="Feed not found")
        new_val = 0 if feed["paused"] else 1
        db.execute(
            "UPDATE feeds SET paused = ? WHERE id = ? AND user_id = ?",
            (new_val, feed_id, uid),
        )
    return {"success": True, "paused": bool(new_val)}


@app.post("/api/feeds/{feed_id}/fetch")
async def fetch_now(request: Request, feed_id: int):
    """Trigger an immediate feed check."""
    uid = current_user_id(request)
    with get_db() as db:
        feed = db.execute(
            "SELECT id FROM feeds WHERE id = ? AND deleted_at IS NULL AND user_id = ?",
            (feed_id, uid),
        ).fetchone()
    if not feed:
        raise HTTPException(status_code=404, detail="Feed not found")
    try:
        check_feed(feed_id)
        return {"success": True, "message": "Feed checked"}
    except SSRFError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/history/{posted_id}/retry")
async def retry_post(request: Request, posted_id: int):
    """Force a failed or gave_up row back to retryable: clears backoff and
    resets the attempt counter so the next feed check reprocesses it."""
    uid = current_user_id(request)
    with get_db() as db:
        result = db.execute(
            """UPDATE posted_items
                  SET attempt_count = 0,
                      next_retry_at = NULL,
                      error_message = NULL
                WHERE id = ?
                  AND status IN ('failed', 'gave_up')
                  AND echo_id IN (SELECT id FROM echoes WHERE user_id = ?)""",
            (posted_id, uid),
        )
        if result.rowcount != 1:
            raise HTTPException(status_code=404, detail="No failed post with that id")
    return {"success": True, "message": "Post queued for retry on next feed check"}


@app.post("/api/history/{posted_id}/give-up")
async def give_up_post(request: Request, posted_id: int):
    """Mark a failed row terminal so the feed cursor can advance past it."""
    uid = current_user_id(request)
    with get_db() as db:
        result = db.execute(
            """UPDATE posted_items
                  SET status = 'gave_up',
                      next_retry_at = NULL
                WHERE id = ?
                  AND status = 'failed'
                  AND echo_id IN (SELECT id FROM echoes WHERE user_id = ?)""",
            (posted_id, uid),
        )
        if result.rowcount != 1:
            raise HTTPException(status_code=404, detail="No failed post with that id")
    return {"success": True, "message": "Post marked as given up"}


# ── API: Echoes ─────────────────────────────────────────────────────────────

VALID_VISIBILITY = {"public", "unlisted", "private", "direct"}
VALID_DEST_TYPES = {"mastodon", "email", "bluesky"}
VALID_FILTER_MODES = {"exclude", "include"}


def _revalidate_stored_templates() -> None:
    """Log stored templates that no longer parse under the Jinja2 engine.

    Templates saved by the old regex engine could contain literal ``{%``
    or ``{#`` text that is now real Jinja2 syntax. Save-time validation
    only protects new edits, so revalidate everything at startup and
    warn instead of letting those echoes silently gave_up at render time.
    """
    with get_db() as db:
        rows = db.execute(
            "SELECT id, feed_id, template FROM echoes WHERE deleted_at IS NULL"
        ).fetchall()
    for row in rows:
        try:
            validate_template(row["template"])
        except Exception as e:
            logger.warning(
                "Echo %s: stored template no longer parses and will give up at "
                "render time (edit the echo to fix): %s",
                row["id"],
                e,
            )


def _validate_echo_template(template: str) -> None:
    """Reject templates the Jinja2 engine cannot parse at save time.

    Without this, a syntax error only surfaces later as a gave_up post.
    """
    try:
        validate_template(template)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Template error: {e}")


@app.post("/api/echoes")
async def add_echo(
    request: Request,
    feed_id: int = Form(...),
    destination_type: str = Form("mastodon"),
    account_id: int = Form(None),
    email_account_id: int = Form(None),
    bluesky_account_id: int = Form(None),
    template: str = Form("{{ title }} {{ link }}"),
    visibility: str = Form("public"),
    filter_keywords: str = Form(""),
    filter_mode: str = Form("exclude"),
    content_warning: str = Form(""),
    attach_image: str = Form(""),
    delivery_mode: str = Form("instant"),
    drip_limit: int = Form(0),
    enabled: str = Form(""),
):
    uid = current_user_id(request)
    if destination_type not in VALID_DEST_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid destination type")
    if visibility not in VALID_VISIBILITY:
        raise HTTPException(status_code=400, detail=f"Invalid visibility")
    if filter_mode not in VALID_FILTER_MODES:
        raise HTTPException(status_code=400, detail=f"Invalid filter mode")
    if delivery_mode not in ("instant", "digest"):
        raise HTTPException(status_code=400, detail="Invalid delivery mode")
    # Digest mode is email-only
    if delivery_mode == "digest" and destination_type != "email":
        raise HTTPException(status_code=400, detail="Digest mode is only available for email destinations")
    if drip_limit < 0 or drip_limit > 1000:
        raise HTTPException(status_code=400, detail="Drip limit must be between 0 and 1000")

    _validate_echo_template(template)

    # Resolve destination_id based on type
    if destination_type == "mastodon":
        destination_id = account_id
        if not destination_id:
            raise HTTPException(status_code=400, detail="account_id required for mastodon destination")
    elif destination_type == "email":
        destination_id = email_account_id
        if not destination_id:
            raise HTTPException(status_code=400, detail="email_account_id required for email destination")
    else:
        destination_id = bluesky_account_id
        if not destination_id:
            raise HTTPException(status_code=400, detail="bluesky_account_id required for bluesky destination")

    is_enabled = 1 if enabled else 0
    is_attach_image = 1 if attach_image else 0
    with get_db() as db:
        # Ownership: the feed and the destination must belong to this user.
        feed = db.execute(
            "SELECT id FROM feeds WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
            (feed_id, uid),
        ).fetchone()
        if not feed:
            raise HTTPException(status_code=404, detail="Feed not found")
        dest_table = {
            "mastodon": "accounts",
            "email": "email_accounts",
            "bluesky": "bluesky_accounts",
        }[destination_type]
        dest = db.execute(
            f"SELECT id FROM {dest_table} WHERE id = ? AND user_id = ?",
            (destination_id, uid),
        ).fetchone()
        if not dest:
            raise HTTPException(status_code=404, detail="Destination not found")

        db.execute(
            """INSERT INTO echoes (feed_id, destination_type, destination_id, template, visibility,
                                   filter_keywords, filter_mode, content_warning, attach_image,
                                   delivery_mode, drip_limit, enabled, user_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (feed_id, destination_type, destination_id, template, visibility,
             filter_keywords.strip(), filter_mode, content_warning.strip(), is_attach_image,
             delivery_mode, drip_limit, is_enabled, uid),
        )
    return RedirectResponse(url="/echoes", status_code=303)


@app.post("/api/echoes/{echo_id}/toggle")
async def toggle_echo(request: Request, echo_id: int):
    uid = current_user_id(request)
    with get_db() as db:
        echo = db.execute(
            "SELECT enabled FROM echoes WHERE id = ? AND deleted_at IS NULL AND user_id = ?",
            (echo_id, uid),
        ).fetchone()
        if not echo:
            raise HTTPException(status_code=404, detail="Echo not found")
        new_val = 0 if echo["enabled"] else 1
        db.execute(
            "UPDATE echoes SET enabled = ? WHERE id = ? AND user_id = ?",
            (new_val, echo_id, uid),
        )
    return {"success": True, "enabled": bool(new_val)}


@app.post("/api/echoes/{echo_id}/edit")
async def edit_echo(
    request: Request,
    echo_id: int,
    feed_id: int = Form(...),
    destination_type: str = Form("mastodon"),
    account_id: int = Form(None),
    email_account_id: int = Form(None),
    bluesky_account_id: int = Form(None),
    template: str = Form("{{ title }} {{ link }}"),
    visibility: str = Form("public"),
    filter_keywords: str = Form(""),
    filter_mode: str = Form("exclude"),
    content_warning: str = Form(""),
    attach_image: str = Form(""),
    delivery_mode: str = Form("instant"),
    drip_limit: int = Form(0),
    enabled: str = Form(""),
):
    if destination_type not in VALID_DEST_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid destination type")
    if visibility not in VALID_VISIBILITY:
        raise HTTPException(status_code=400, detail=f"Invalid visibility")
    if filter_mode not in VALID_FILTER_MODES:
        raise HTTPException(status_code=400, detail=f"Invalid filter mode")
    if delivery_mode not in ("instant", "digest"):
        raise HTTPException(status_code=400, detail="Invalid delivery mode")
    if delivery_mode == "digest" and destination_type != "email":
        raise HTTPException(status_code=400, detail="Digest mode is only available for email destinations")
    if drip_limit < 0 or drip_limit > 1000:
        raise HTTPException(status_code=400, detail="Drip limit must be between 0 and 1000")

    _validate_echo_template(template)

    if destination_type == "mastodon":
        destination_id = account_id
        if not destination_id:
            raise HTTPException(status_code=400, detail="account_id required for mastodon destination")
    elif destination_type == "email":
        destination_id = email_account_id
        if not destination_id:
            raise HTTPException(status_code=400, detail="email_account_id required for email destination")
    else:
        destination_id = bluesky_account_id
        if not destination_id:
            raise HTTPException(status_code=400, detail="bluesky_account_id required for bluesky destination")

    is_enabled = 1 if enabled else 0
    is_attach_image = 1 if attach_image else 0
    uid = current_user_id(request)
    with get_db() as db:
        echo = db.execute(
            "SELECT * FROM echoes WHERE id = ? AND deleted_at IS NULL AND user_id = ?",
            (echo_id, uid),
        ).fetchone()
        if not echo:
            raise HTTPException(status_code=404, detail="Echo not found")
        # Ownership: the new feed and destination must belong to this user.
        feed = db.execute(
            "SELECT id FROM feeds WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
            (feed_id, uid),
        ).fetchone()
        if not feed:
            raise HTTPException(status_code=404, detail="Feed not found")
        dest_table = {
            "mastodon": "accounts",
            "email": "email_accounts",
            "bluesky": "bluesky_accounts",
        }[destination_type]
        dest = db.execute(
            f"SELECT id FROM {dest_table} WHERE id = ? AND user_id = ?",
            (destination_id, uid),
        ).fetchone()
        if not dest:
            raise HTTPException(status_code=404, detail="Destination not found")
        db.execute(
            """UPDATE echoes SET feed_id = ?, destination_type = ?, destination_id = ?,
               template = ?, visibility = ?, filter_keywords = ?, filter_mode = ?,
               content_warning = ?, attach_image = ?, delivery_mode = ?, drip_limit = ?,
               enabled = ?
               WHERE id = ? AND user_id = ?""",
            (feed_id, destination_type, destination_id, template, visibility,
             filter_keywords.strip(), filter_mode, content_warning.strip(), is_attach_image,
             delivery_mode, drip_limit, is_enabled, echo_id, uid),
        )
    return RedirectResponse(url="/echoes", status_code=303)


@app.post("/api/echoes/{echo_id}/delete")
async def delete_echo(request: Request, echo_id: int):
    """Soft-delete an echo so its posted-item history survives.

    A hard DELETE cascades to posted_items/digest_items and erases the
    cross-post audit trail. Marking deleted_at and disabling the echo removes
    it from listings and stops delivery while keeping its history intact.
    """
    uid = current_user_id(request)
    with get_db() as db:
        db.execute(
            """
            UPDATE echoes
               SET deleted_at = CURRENT_TIMESTAMP,
                   enabled = 0
             WHERE id = ?
               AND deleted_at IS NULL
               AND user_id = ?
            """,
            (echo_id, uid),
        )
    return RedirectResponse(url="/echoes", status_code=303)


# ── API: Preview ────────────────────────────────────────────────────────────

@app.post("/api/preview")
def preview_template(
    request: Request,
    template: str = Form(...),
    feed_id: int = Form(...),
):
    """Preview a template rendered against the feed's most recent items.

    Renders the given template against up to 3 recent feed items so users
    can check output before saving an echo. The template is validated
    first; syntax errors return 400 with the Jinja2 error message.
    """
    try:
        validate_template(template)
    except Exception as e:
        return JSONResponse(
            {"success": False, "error": f"Template syntax error: {e}"},
            status_code=400,
        )

    uid = current_user_id(request)
    with get_db() as db:
        feed = db.execute(
            "SELECT * FROM feeds WHERE id = ? AND deleted_at IS NULL AND user_id = ?",
            (feed_id, uid),
        ).fetchone()
    if not feed:
        raise HTTPException(status_code=404, detail="Feed not found")

    try:
        feed_data = fetch_feed(feed["url"])
    except SSRFError as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)
    except Exception:
        logger.warning("Template preview: feed %s fetch failed", feed_id, exc_info=True)
        return JSONResponse(
            {"success": False, "error": "Could not fetch the feed"}, status_code=502
        )

    items = (feed_data.get("items") or [])[:3]
    previews = []
    for item in items:
        try:
            rendered = render_template(template, item, feed_name=feed["name"])
        except Exception as e:
            return JSONResponse(
                {"success": False, "error": f"Render error: {e}"}, status_code=400
            )
        previews.append(
            {"title": item.get("title") or "(untitled)", "rendered": rendered}
        )

    return {"success": True, "items": previews}


# ── API: OAuth ───────────────────────────────────────────────────────────────

@app.get("/oauth/connect")
async def oauth_connect(request: Request, instance: str = ""):
    """Start a session-bound Mastodon OAuth authorization flow."""
    if not instance:
        raise HTTPException(status_code=400, detail="Instance URL is required")

    instance = validate_url(instance)

    # This cookie is independent from shared-secret auth. It ties the OAuth
    # callback to the browser session that initiated the flow.
    oauth_session = request.cookies.get(OAUTH_SESSION_COOKIE)
    if not oauth_session:
        oauth_session = _secrets.token_urlsafe(32)

    try:
        auth_url = get_authorize_url(
            instance, oauth_session, user_id=current_user_id(request)
        )
    except Exception:
        logger.exception("OAuth connect failed for %s", instance)
        return _render_accounts_error(
            request,
            "Failed to start OAuth authorization. Verify the instance URL and try again.",
        )

    response = RedirectResponse(url=auth_url, status_code=302)
    response.set_cookie(
        key=OAUTH_SESSION_COOKIE,
        value=oauth_session,
        max_age=OAUTH_SESSION_MAX_AGE,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        path="/oauth",
    )
    return response


@app.get("/oauth/callback")
async def oauth_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
):
    """Handle a one-time, session-bound Mastodon OAuth callback."""
    if error:
        return _render_accounts_error(
            request, "Authorization was denied by the OAuth provider."
        )

    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state parameter")

    oauth_session = request.cookies.get(OAUTH_SESSION_COOKIE)
    if not oauth_session:
        raise HTTPException(
            status_code=400,
            detail="OAuth session is missing or expired. Start the connection again.",
        )

    try:
        instance, state_user_id = verify_state(state, oauth_session)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid, expired, already-used, or session-mismatched OAuth state.",
        )

    # In multi mode the state must be bound to a user. A NULL user_id
    # (legacy row) would otherwise attribute the account to tenant 1.
    if settings.MULTI and state_user_id is None:
        raise HTTPException(
            status_code=400,
            detail="OAuth session is not bound to a user. Start the connection again.",
        )

    try:
        token_data = exchange_code(instance, code)
    except Exception:
        logger.exception("OAuth token exchange failed for %s", instance)
        response = _render_accounts_error(
            request, "OAuth token exchange failed. Please try connecting again."
        )
        response.delete_cookie(OAUTH_SESSION_COOKIE, path="/oauth")
        return response

    access_token = token_data.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise HTTPException(status_code=502, detail="OAuth provider returned no access token")

    try:
        creds = verify_credentials(instance, access_token)
        display_name = creds.get("display_name") or creds.get("username", "Unknown")
        username = creds.get("username", "unknown")
    except Exception:
        logger.exception("Could not verify OAuth credentials for %s", instance)
        display_name = "Unknown"
        username = "unknown"

    with get_db() as db:
        db.execute(
            """INSERT INTO accounts (name, username, instance, access_token, user_id)
               VALUES (?, ?, ?, ?, ?)""",
            (display_name, username, instance, access_token, state_user_id or 1),
        )

    response = RedirectResponse(url="/accounts?status=connected", status_code=303)
    response.delete_cookie(OAUTH_SESSION_COOKIE, path="/oauth")
    return response


# ── Misc ─────────────────────────────────────────────────────────────────────

@app.exception_handler(404)
async def not_found_handler(request: Request, exc: HTTPException):
    if request.headers.get("accept", "").startswith("application/json"):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    return render("404.html", request, status_code=404)


@app.get("/favicon.svg", include_in_schema=False)
async def favicon():
    return Response(
        content="""<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><rect width='64' height='64' rx='14' fill='#2d5a9e'/><path d='M32 16 L48 32 L32 48 L16 32 Z' fill='#fff'/><circle cx='32' cy='32' r='6' fill='#2d5a9e'/></svg>""",
        media_type="image/svg+xml",
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8453)
