"""FeedEcho — self-hosted RSS feed cross-poster.

Routes feed items to Mastodon accounts or email addresses. Web UI for managing
feeds, accounts, echoes, settings, and viewing post history.
"""

import os
import re
import logging
import secrets as _secrets
from pathlib import Path
from datetime import datetime, timezone

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from database import get_db, init_db
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s: %(message)s")
logger = logging.getLogger("feedecho")

app = FastAPI(title="FeedEcho", version="1.10.1")

BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

jinja = Environment(
    loader=FileSystemLoader(BASE_DIR / "templates"),
    autoescape=select_autoescape(["html"]),
)

OAUTH_SESSION_COOKIE = "feedecho_oauth_session"
OAUTH_SESSION_MAX_AGE = 10 * 60

# ── Optional shared-secret auth ──────────────────────────────────────────────
# If FEEDCHO_AUTH_TOKEN is set, all requests must include it as either:
#   - Cookie: feedecho_auth=<token>   (set by the login page)
#   - X-Auth-Token: <token>           (for API/programmatic access)
# If the env var is unset, auth is disabled (original behavior).
_AUTH_TOKEN = os.environ.get("FEEDCHO_AUTH_TOKEN")

# Paths exempt from auth: health check + static files + OAuth callback.
# Only /oauth/callback must be reachable without a cookie (Mastodon redirects
# the user's browser here). /oauth/connect requires auth so unauthenticated
# users cannot trigger outbound requests to arbitrary instance URLs.
_AUTH_EXEMPT_PATHS = {"/healthz", "/favicon.svg", "/static", "/oauth/callback"}
_AUTH_EXEMPT_PREFIXES = ("/static",)


class AuthMiddleware(BaseHTTPMiddleware):
    """Shared-secret auth. If FEEDCHO_AUTH_TOKEN is unset, this is a no-op."""

    async def dispatch(self, request: Request, call_next):
        if not _AUTH_TOKEN:
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

        if token and _secrets.compare_digest(token, _AUTH_TOKEN):
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


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if not _AUTH_TOKEN:
        return RedirectResponse(url="/", status_code=302)
    return render("login.html", request)


@app.post("/login")
async def login_submit(request: Request, token: str = Form(...)):
    if not _AUTH_TOKEN:
        return RedirectResponse(url="/", status_code=302)
    if _secrets.compare_digest(token, _AUTH_TOKEN):
        response = RedirectResponse(url="/", status_code=302)
        response.set_cookie(
            key="feedecho_auth",
            value=token,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
        )
        return response
    return render("login.html", request, error="Invalid token")


app.add_middleware(AuthMiddleware)


def render(name: str, request: Request, status_code: int = 200, **kwargs) -> HTMLResponse:
    template = jinja.get_template(name)
    return HTMLResponse(template.render(request=request, **kwargs), status_code=status_code)


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
    init_db()
    _revalidate_stored_templates()
    start_scheduler()
    logger.info("FeedEcho started")
    yield
    stop_scheduler()


app.router.lifespan_context = lifespan


# ── Helpers ──────────────────────────────────────────────────────────────────

def _get_smtp_settings(mask_password: bool = False):
    """Load SMTP settings as a flat dict for templates.

    If mask_password is True, replaces the SMTP password with a placeholder
    so it's never sent to the browser. Used on settings/accounts pages.
    """
    with get_db() as db:
        rows = db.execute(
            "SELECT key, value FROM settings WHERE key LIKE 'smtp_%'"
        ).fetchall()
    if not rows:
        return {}
    settings = {row["key"]: row["value"] for row in rows}
    if mask_password and settings.get("smtp_password"):
        settings["smtp_password"] = "********"
    return settings


def _get_all_accounts():
    """Fetch Mastodon, email, and Bluesky accounts."""
    with get_db() as db:
        mastodon = db.execute(
            "SELECT id, name, username, instance, created_at FROM accounts ORDER BY name"
        ).fetchall()
        email = db.execute(
            "SELECT id, name, email, created_at FROM email_accounts ORDER BY name"
        ).fetchall()
        bluesky = db.execute(
            "SELECT id, name, handle, did, pds, created_at FROM bluesky_accounts ORDER BY handle"
        ).fetchall()
    return mastodon, email, bluesky


def _render_accounts_error(request: Request, message: str) -> HTMLResponse:
    """Render the accounts page with an error banner."""
    mastodon_accounts, email_accounts, bluesky_accounts = _get_all_accounts()
    smtp_settings = _get_smtp_settings(mask_password=True)
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
    with get_db() as db:
        mastodon_accounts = db.execute(
            "SELECT COUNT(*) as c FROM accounts"
        ).fetchone()["c"]
        email_accounts = db.execute(
            "SELECT COUNT(*) as c FROM email_accounts"
        ).fetchone()["c"]
        bluesky_accounts = db.execute(
            "SELECT COUNT(*) as c FROM bluesky_accounts"
        ).fetchone()["c"]
        feeds = db.execute(
            "SELECT * FROM feeds WHERE deleted_at IS NULL ORDER BY name"
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
            LEFT JOIN accounts a ON e.destination_type = 'mastodon' AND e.destination_id = a.id
            LEFT JOIN email_accounts ea ON e.destination_type = 'email' AND e.destination_id = ea.id
            LEFT JOIN bluesky_accounts b ON e.destination_type = 'bluesky' AND e.destination_id = b.id
            WHERE e.deleted_at IS NULL
            ORDER BY e.created_at DESC
        """).fetchall()
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
            LEFT JOIN accounts a ON e.destination_type = 'mastodon' AND e.destination_id = a.id
            LEFT JOIN email_accounts ea ON e.destination_type = 'email' AND e.destination_id = ea.id
            LEFT JOIN bluesky_accounts b ON e.destination_type = 'bluesky' AND e.destination_id = b.id
            ORDER BY pi.posted_at DESC
            LIMIT 20
        """).fetchall()
        stats = {
            "accounts": mastodon_accounts + email_accounts + bluesky_accounts,
            "feeds": len(feeds),
            "echoes": len(echoes),
            "active_echoes": sum(1 for e in echoes if e["enabled"]),
            "total_posts": db.execute("SELECT COUNT(*) FROM posted_items WHERE status = 'success'").fetchone()[0],
            "failed_posts": db.execute("SELECT COUNT(*) FROM posted_items WHERE status = 'failed'").fetchone()[0],
        }
    return render("dashboard.html", request, feeds=feeds, echoes=echoes,
                  recent_posts=recent_posts, stats=stats)


@app.get("/feeds", response_class=HTMLResponse)
async def feeds_page(request: Request):
    with get_db() as db:
        feeds = db.execute(
            "SELECT * FROM feeds WHERE deleted_at IS NULL ORDER BY name"
        ).fetchall()
        feed_echoes = {}
        for f in feeds:
            feed_echoes[f["id"]] = db.execute(
                "SELECT COUNT(*) as c FROM echoes WHERE feed_id = ? AND deleted_at IS NULL", (f["id"],)
            ).fetchone()["c"]
    return render("feeds.html", request, feeds=feeds, feed_echoes=feed_echoes)


@app.get("/accounts", response_class=HTMLResponse)
async def accounts_page(request: Request):
    mastodon_accounts, email_accounts, bluesky_accounts = _get_all_accounts()
    smtp_settings = _get_smtp_settings(mask_password=True)
    smtp_configured = bool(smtp_settings.get("smtp_host"))
    return render("accounts.html", request,
                  mastodon_accounts=mastodon_accounts,
                  email_accounts=email_accounts,
                  bluesky_accounts=bluesky_accounts,
                  smtp_configured=smtp_configured,
                  smtp_settings=smtp_settings)


@app.get("/echoes", response_class=HTMLResponse)
async def echoes_page(request: Request):
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
            LEFT JOIN accounts a ON e.destination_type = 'mastodon' AND e.destination_id = a.id
            LEFT JOIN email_accounts ea ON e.destination_type = 'email' AND e.destination_id = ea.id
            LEFT JOIN bluesky_accounts b ON e.destination_type = 'bluesky' AND e.destination_id = b.id
            WHERE e.deleted_at IS NULL
            ORDER BY e.created_at DESC
        """).fetchall()
        feeds = db.execute(
            "SELECT * FROM feeds WHERE deleted_at IS NULL ORDER BY name"
        ).fetchall()
        mastodon_accounts = db.execute(
            "SELECT id, name, username, instance FROM accounts ORDER BY name"
        ).fetchall()
        email_accounts = db.execute(
            "SELECT id, name, email FROM email_accounts ORDER BY name"
        ).fetchall()
        bluesky_accounts = db.execute(
            "SELECT id, name, handle FROM bluesky_accounts ORDER BY handle"
        ).fetchall()
    return render("echoes.html", request, echoes=echoes, feeds=feeds,
                  mastodon_accounts=mastodon_accounts,
                  email_accounts=email_accounts,
                  bluesky_accounts=bluesky_accounts,
                  template_vars=available_variables())


@app.get("/history", response_class=HTMLResponse)
async def history_page(request: Request):
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
            LEFT JOIN accounts a ON e.destination_type = 'mastodon' AND e.destination_id = a.id
            LEFT JOIN email_accounts ea ON e.destination_type = 'email' AND e.destination_id = ea.id
            LEFT JOIN bluesky_accounts b ON e.destination_type = 'bluesky' AND e.destination_id = b.id
            ORDER BY pi.posted_at DESC
            LIMIT 100
        """).fetchall()
    return render("history.html", request, posts=posts)


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    smtp_settings = _get_smtp_settings(mask_password=True)
    smtp_configured = bool(smtp_settings.get("smtp_host"))
    with get_db() as db:
        rows = db.execute(
            """SELECT key, value FROM settings
               WHERE key IN ('retry_max_attempts', 'retry_backoff_minutes',
                             'notify_failure_threshold', 'notify_email')"""
        ).fetchall()
        alt_rows = db.execute(
            """SELECT key, value FROM settings
               WHERE key IN ('alt_text_ai_enabled', 'alt_text_ai_base_url',
                             'alt_text_ai_model', 'alt_text_ai_api_key')"""
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
    return {"status": "ok"}


# ── API: Mastodon Accounts ──────────────────────────────────────────────────

@app.post("/api/accounts")
async def add_account(
    name: str = Form(...),
    username: str = Form(""),
    instance: str = Form(...),
    access_token: str = Form(...),
):
    instance = validate_url(instance)
    with get_db() as db:
        db.execute(
            "INSERT INTO accounts (name, username, instance, access_token) VALUES (?, ?, ?, ?)",
            (name, username or name, instance, access_token),
        )
    return RedirectResponse(url="/accounts", status_code=303)


@app.post("/api/accounts/{account_id}/test")
async def test_account(account_id: int):
    with get_db() as db:
        account = db.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    success, message = test_connection(account["instance"], account["access_token"])
    return {"success": success, "message": message}


@app.post("/api/accounts/{account_id}/delete")
async def delete_account(account_id: int):
    with get_db() as db:
        db.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    return RedirectResponse(url="/accounts", status_code=303)


# ── API: Email Accounts ─────────────────────────────────────────────────────

@app.post("/api/email-accounts")
async def add_email_account(
    name: str = Form(...),
    email: str = Form(...),
):
    with get_db() as db:
        db.execute(
            "INSERT OR REPLACE INTO email_accounts (name, email) VALUES (?, ?)",
            (name, email),
        )
    return RedirectResponse(url="/accounts?status=email_added", status_code=303)


@app.post("/api/email-accounts/{account_id}/delete")
async def delete_email_account(account_id: int):
    with get_db() as db:
        db.execute("DELETE FROM email_accounts WHERE id = ?", (account_id,))
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
        db.execute(
            """
            INSERT INTO bluesky_accounts (
                name, handle, app_password, did, pds,
                access_jwt, refresh_jwt, session_expires_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(handle) DO UPDATE SET
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
            ),
        )
    return RedirectResponse(url="/accounts?status=bluesky_connected", status_code=303)


@app.post("/api/bluesky-accounts/{account_id}/test")
def test_bluesky_account(account_id: int):
    with get_db() as db:
        account = db.execute(
            "SELECT * FROM bluesky_accounts WHERE id = ?", (account_id,)
        ).fetchone()
    if not account:
        raise HTTPException(status_code=404, detail="Bluesky account not found")
    success, message = test_bluesky_connection(
        account["handle"], account["app_password"]
    )
    return {"success": success, "message": message}


@app.post("/api/bluesky-accounts/{account_id}/delete")
def delete_bluesky_account(request: Request, account_id: int):
    with get_db() as db:
        dependent = db.execute(
            """
            SELECT COUNT(*) as c FROM echoes
             WHERE destination_type = 'bluesky'
               AND destination_id = ?
               AND deleted_at IS NULL
            """,
            (account_id,),
        ).fetchone()["c"]
    if dependent:
        return _render_accounts_error(
            request,
            "This Bluesky account is used by echoes. Delete or reassign those echoes first.",
        )
    with get_db() as db:
        db.execute("DELETE FROM bluesky_accounts WHERE id = ?", (account_id,))
    return RedirectResponse(url="/accounts?status=bluesky_deleted", status_code=303)


# ── API: Settings ───────────────────────────────────────────────────────────

@app.post("/api/settings/smtp")
async def save_smtp_settings(
    smtp_host: str = Form(...),
    smtp_port: int = Form(587),
    smtp_username: str = Form(""),
    smtp_password: str = Form(""),
    smtp_from_email: str = Form(""),
    smtp_from_name: str = Form("FeedEcho"),
    smtp_use_tls: str = Form("1"),
):
    settings = {
        "smtp_host": smtp_host,
        "smtp_port": str(smtp_port),
        "smtp_username": smtp_username,
        "smtp_from_email": smtp_from_email,
        "smtp_from_name": smtp_from_name,
        "smtp_use_tls": smtp_use_tls,
    }
    with get_db() as db:
        for key, value in settings.items():
            db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )
        # Only update password if it's not the mask placeholder
        if smtp_password and smtp_password != "********":
            db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                ("smtp_password", smtp_password),
            )
    return RedirectResponse(url="/settings?status=saved", status_code=303)


@app.post("/api/settings/smtp/test")
async def test_smtp(
    test_email: str = Form(""),
):
    success, message = test_smtp_connection(test_email if test_email else None)
    return {"success": success, "message": message}


@app.post("/api/settings/retry-notify")
async def save_retry_notify_settings(
    retry_max_attempts: int = Form(5),
    retry_backoff_minutes: int = Form(5),
    notify_failure_threshold: int = Form(3),
    notify_email: str = Form(""),
):
    values = {
        "retry_max_attempts": str(max(0, min(retry_max_attempts, 100))),
        "retry_backoff_minutes": str(max(1, min(retry_backoff_minutes, 1440))),
        "notify_failure_threshold": str(max(0, min(notify_failure_threshold, 100))),
        "notify_email": notify_email.strip(),
    }
    with get_db() as db:
        for key, value in values.items():
            db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )
    return RedirectResponse(url="/settings?status=saved", status_code=303)


@app.post("/api/settings/alt-text")
async def save_alt_text_settings(
    alt_text_ai_enabled: str = Form(""),
    alt_text_ai_base_url: str = Form(""),
    alt_text_ai_model: str = Form(""),
    alt_text_ai_api_key: str = Form(""),
):
    values = {
        "alt_text_ai_enabled": "1" if alt_text_ai_enabled else "0",
        "alt_text_ai_base_url": alt_text_ai_base_url.strip().rstrip("/"),
        "alt_text_ai_model": alt_text_ai_model.strip(),
    }
    with get_db() as db:
        for key, value in values.items():
            db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )
        # Only update API key if it's not the mask placeholder
        if alt_text_ai_api_key and alt_text_ai_api_key != "********":
            db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                ("alt_text_ai_api_key", alt_text_ai_api_key.strip()),
            )
    return RedirectResponse(url="/settings?status=saved", status_code=303)


@app.post("/api/settings/alt-text/test")
async def test_alt_text():
    """Test the vision API connection with a minimal request."""
    import alt_text
    if not alt_text.is_enabled():
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
    name: str = Form(...),
    url: str = Form(...),
    poll_interval: int = Form(15),
):
    url = validate_url(url)
    poll_interval = max(1, min(poll_interval, 1440))
    with get_db() as db:
        db.execute(
            "INSERT INTO feeds (name, url, poll_interval) VALUES (?, ?, ?)",
            (name, url, poll_interval),
        )
    return RedirectResponse(url="/feeds", status_code=303)


@app.post("/api/feeds/{feed_id}/delete")
async def delete_feed(feed_id: int):
    """Soft-delete a feed. Echoes and post history are preserved.

    A hard DELETE would cascade (echoes -> posted_items/digest_items) and wipe
    the cross-post audit trail, so feeds are only marked deleted_at. The feed
    disappears from listings and is skipped by the scheduler, but its echo
    config and history remain on the /echoes and /history pages.
    """
    with get_db() as db:
        db.execute(
            """
            UPDATE feeds
               SET deleted_at = datetime('now')
             WHERE id = ?
               AND deleted_at IS NULL
            """,
            (feed_id,),
        )
    return RedirectResponse(url="/feeds", status_code=303)


@app.post("/api/feeds/{feed_id}/test")
async def test_feed(feed_id: int):
    """Fetch a feed and return preview of items."""
    with get_db() as db:
        feed = db.execute(
            "SELECT * FROM feeds WHERE id = ? AND deleted_at IS NULL", (feed_id,)
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
async def init_feed(feed_id: int):
    """Initialize a feed's last_item_id so it only posts new items going forward."""
    with get_db() as db:
        feed = db.execute(
            "SELECT * FROM feeds WHERE id = ? AND deleted_at IS NULL", (feed_id,)
        ).fetchone()
        if not feed:
            raise HTTPException(status_code=404, detail="Feed not found")
        try:
            feed_data = fetch_feed(feed["url"])
            if feed_data["items"]:
                last_id = feed_data["items"][0]["id"]
                db.execute("UPDATE feeds SET last_item_id = ? WHERE id = ?", (last_id, feed_id))
                return {"success": True, "message": f"Initialized. Last item: {feed_data['items'][0]['title'][:60]}"}
            return {"success": True, "message": "Feed has no items"}
        except SSRFError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            return {"success": False, "error": str(e)}


@app.post("/api/feeds/{feed_id}/pause")
async def pause_feed(feed_id: int):
    with get_db() as db:
        feed = db.execute(
            "SELECT paused FROM feeds WHERE id = ? AND deleted_at IS NULL", (feed_id,)
        ).fetchone()
        if not feed:
            raise HTTPException(status_code=404, detail="Feed not found")
        new_val = 0 if feed["paused"] else 1
        db.execute("UPDATE feeds SET paused = ? WHERE id = ?", (new_val, feed_id))
    return {"success": True, "paused": bool(new_val)}


@app.post("/api/feeds/{feed_id}/fetch")
async def fetch_now(feed_id: int):
    """Trigger an immediate feed check."""
    try:
        check_feed(feed_id)
        return {"success": True, "message": "Feed checked"}
    except SSRFError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/history/{posted_id}/retry")
async def retry_post(posted_id: int):
    """Force a failed or gave_up row back to retryable: clears backoff and
    resets the attempt counter so the next feed check reprocesses it."""
    with get_db() as db:
        result = db.execute(
            """UPDATE posted_items
                  SET attempt_count = 0,
                      next_retry_at = NULL,
                      error_message = NULL
                WHERE id = ?
                  AND status IN ('failed', 'gave_up')""",
            (posted_id,),
        )
        if result.rowcount != 1:
            raise HTTPException(status_code=404, detail="No failed post with that id")
    return {"success": True, "message": "Post queued for retry on next feed check"}


@app.post("/api/history/{posted_id}/give-up")
async def give_up_post(posted_id: int):
    """Mark a failed row terminal so the feed cursor can advance past it."""
    with get_db() as db:
        result = db.execute(
            """UPDATE posted_items
                  SET status = 'gave_up',
                      next_retry_at = NULL
                WHERE id = ?
                  AND status = 'failed'""",
            (posted_id,),
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
        db.execute(
            """INSERT INTO echoes (feed_id, destination_type, destination_id, template, visibility,
                                   filter_keywords, filter_mode, content_warning, attach_image,
                                   delivery_mode, drip_limit, enabled)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (feed_id, destination_type, destination_id, template, visibility,
             filter_keywords.strip(), filter_mode, content_warning.strip(), is_attach_image,
             delivery_mode, drip_limit, is_enabled),
        )
    return RedirectResponse(url="/echoes", status_code=303)


@app.post("/api/echoes/{echo_id}/toggle")
async def toggle_echo(echo_id: int):
    with get_db() as db:
        echo = db.execute(
            "SELECT enabled FROM echoes WHERE id = ? AND deleted_at IS NULL", (echo_id,)
        ).fetchone()
        if not echo:
            raise HTTPException(status_code=404, detail="Echo not found")
        new_val = 0 if echo["enabled"] else 1
        db.execute("UPDATE echoes SET enabled = ? WHERE id = ?", (new_val, echo_id))
    return {"success": True, "enabled": bool(new_val)}


@app.post("/api/echoes/{echo_id}/edit")
async def edit_echo(
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
    with get_db() as db:
        echo = db.execute(
            "SELECT * FROM echoes WHERE id = ? AND deleted_at IS NULL", (echo_id,)
        ).fetchone()
        if not echo:
            raise HTTPException(status_code=404, detail="Echo not found")
        db.execute(
            """UPDATE echoes SET feed_id = ?, destination_type = ?, destination_id = ?,
               template = ?, visibility = ?, filter_keywords = ?, filter_mode = ?,
               content_warning = ?, attach_image = ?, delivery_mode = ?, drip_limit = ?,
               enabled = ?
               WHERE id = ?""",
            (feed_id, destination_type, destination_id, template, visibility,
             filter_keywords.strip(), filter_mode, content_warning.strip(), is_attach_image,
             delivery_mode, drip_limit, is_enabled, echo_id),
        )
    return RedirectResponse(url="/echoes", status_code=303)


@app.post("/api/echoes/{echo_id}/delete")
async def delete_echo(echo_id: int):
    """Soft-delete an echo so its posted-item history survives.

    A hard DELETE cascades to posted_items/digest_items and erases the
    cross-post audit trail. Marking deleted_at and disabling the echo removes
    it from listings and stops delivery while keeping its history intact.
    """
    with get_db() as db:
        db.execute(
            """
            UPDATE echoes
               SET deleted_at = datetime('now'),
                   enabled = 0
             WHERE id = ?
               AND deleted_at IS NULL
            """,
            (echo_id,),
        )
    return RedirectResponse(url="/echoes", status_code=303)


# ── API: Preview ────────────────────────────────────────────────────────────

@app.post("/api/preview")
def preview_template(
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

    with get_db() as db:
        feed = db.execute(
            "SELECT * FROM feeds WHERE id = ? AND deleted_at IS NULL", (feed_id,)
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
        auth_url = get_authorize_url(instance, oauth_session)
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
        instance = verify_state(state, oauth_session)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid, expired, already-used, or session-mismatched OAuth state.",
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
            """INSERT OR REPLACE INTO accounts (name, username, instance, access_token)
               VALUES (?, ?, ?, ?)""",
            (display_name, username, instance, access_token),
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
