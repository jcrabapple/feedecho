"""Mastodon OAuth registration, authorization state, and token exchange."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

import settings
from database import get_db
from feed_parser import SSRFError, validate_outbound_url
from settings import AUTH_TOKEN, STATE_SECRET

SCOPES = "read write"
STATE_TTL_SECONDS = 10 * 60

# Last-resort website for the app registration. Kept in sync with
# settings.PROJECT_URL by a test, so drift fails the suite instead of
# silently shipping two different links.
FALLBACK_WEBSITE = "https://github.com/jcrabapple/feedecho"

# STATE_SECRET wins: in multi mode a carried-over single-mode AUTH_TOKEN must
# never silently key OAuth-state HMACs (mirrors security.session_secret()'s
# gate, which exists for exactly this scenario).
_STATE_SECRET = (STATE_SECRET or AUTH_TOKEN or secrets.token_urlsafe(32)).encode("utf-8")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sqlite_timestamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _hash_session_binding(session_binding: str) -> str:
    """Avoid storing a browser session secret in plaintext."""
    return hashlib.sha256(session_binding.encode("utf-8")).hexdigest()


def _state_signature(nonce: str, instance: str) -> str:
    payload = f"{nonce}|{instance}".encode("utf-8")
    return hmac.new(_STATE_SECRET, payload, hashlib.sha256).hexdigest()


def _sign_state(
    instance: str, session_binding: str | None = None, user_id: int | None = None
) -> str:
    """Create and persist a one-time OAuth state token.

    `session_binding` must be a cryptographically random browser session value.
    It is stored hashed and must be presented again when consuming state.
    `user_id` records which tenant initiated the flow (multi mode); it is
    recovered when the state is consumed so the resulting account row is
    attributed to the right user.

    The optional defaults are retained only for compatibility with direct
    callers; production callers must provide a browser-session binding.
    """
    if session_binding is None:
        session_binding = secrets.token_urlsafe(32)

    instance = instance.rstrip("/")
    nonce = secrets.token_urlsafe(32)
    signature = _state_signature(nonce, instance)
    expires_at = _sqlite_timestamp(_now() + timedelta(seconds=STATE_TTL_SECONDS))

    with get_db() as db:
        db.execute(
            """
            INSERT INTO oauth_states (nonce, instance, session_binding, user_id, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (nonce, instance, _hash_session_binding(session_binding), user_id, expires_at),
        )

    return f"{nonce}|{instance}|{signature}"


def _verify_state(state: str, session_binding: str | None = None) -> str:
    """Validate and atomically consume an OAuth state token.

    A state token is valid only when all of these conditions hold:

    * its full SHA-256 HMAC is valid;
    * its server-side record exists;
    * it is bound to the initiating browser session;
    * it has not expired;
    * it has not been consumed previously.

    The atomic UPDATE is the single-use guarantee.
    """
    if session_binding is None:
        raise ValueError("OAuth state is not bound to a browser session")

    parts = state.rsplit("|", 2)
    if len(parts) != 3:
        raise ValueError("Invalid state parameter")

    nonce, instance, signature = parts
    if not nonce or not instance or not signature:
        raise ValueError("Invalid state parameter")

    expected_signature = _state_signature(nonce, instance)
    if not hmac.compare_digest(signature, expected_signature):
        raise ValueError("Invalid state signature")

    binding_hash = _hash_session_binding(session_binding)
    now = _sqlite_timestamp(_now())

    with get_db() as db:
        result = db.execute(
            """
            UPDATE oauth_states
               SET consumed_at = ?
             WHERE nonce = ?
               AND instance = ?
               AND session_binding = ?
               AND consumed_at IS NULL
               AND expires_at > ?
            """,
            (now, nonce, instance, binding_hash, now),
        )
        if result.rowcount != 1:
            raise ValueError("OAuth state is invalid, expired, already used, or session-mismatched")

    return instance


def app_website() -> str:
    """The website Mastodon shows behind the application name on posts.

    Read per call (not frozen at import) so a config change takes effect on
    the next registration, and so tests can monkeypatch settings.
    """
    return (
        getattr(settings, "APP_WEBSITE", "")
        or getattr(settings, "PROJECT_URL", "")
        or FALLBACK_WEBSITE
    )


def callback_url() -> str:
    """The redirect URI sent to the instance. Read per call, same reason."""
    return getattr(settings, "CALLBACK_URL", "")


def get_or_create_app(instance: str, allow_refresh: bool = True) -> dict:
    """Register an OAuth app on an instance, or return cached credentials.

    Cached credentials are reused only while the values frozen into the
    registration still match the configuration: the website (the link
    Mastodon shows on posts) and the redirect URI (which the instance
    rejects the authorize request over if it drifted). A mismatch — or a
    legacy row from before these were recorded — re-registers the app,
    since Mastodon's API cannot edit an existing registration.

    `allow_refresh=False` pins the call to whatever is cached. The token
    exchange must use the same client that issued the authorization code,
    so it never re-registers mid-flow.

    Concurrency note: this is SELECT-then-register-then-upsert, so two
    connects to the same instance overlapping on a stale row can both
    register. The upsert keeps one row (last writer wins) and the loser's
    in-flight authorization fails; retrying the connect succeeds against
    the now-matching row. Pre-existing shape, left as-is deliberately.
    """
    instance = instance.rstrip("/")
    validate_outbound_url(instance)
    website = app_website()
    redirect_uris = callback_url()

    with get_db() as db:
        row = db.execute(
            "SELECT client_id, client_secret, website, redirect_uris"
            " FROM oauth_apps WHERE instance = ?",
            (instance,),
        ).fetchone()
        if row:
            unchanged = (row["website"] or "") == website and (
                row["redirect_uris"] or ""
            ) == redirect_uris
            if unchanged or not allow_refresh:
                return {
                    "client_id": row["client_id"],
                    "client_secret": row["client_secret"],
                }

    data = {
        "client_name": "FeedEcho",
        "redirect_uris": redirect_uris,
        "scopes": SCOPES,
        "website": website,
    }

    try:
        with httpx.Client(
            timeout=httpx.Timeout(30.0),
            follow_redirects=False,
        ) as client:
            response = client.post(f"{instance}/api/v1/apps", data=data)
            response.raise_for_status()
            result = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise RuntimeError("Unable to register OAuth application with instance") from exc

    client_id = result.get("client_id")
    client_secret = result.get("client_secret")
    if not isinstance(client_id, str) or not isinstance(client_secret, str):
        raise RuntimeError("Instance returned an invalid OAuth application response")

    with get_db() as db:
        db.execute(
            """
            INSERT INTO oauth_apps
                (instance, client_id, client_secret, website, redirect_uris)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(instance) DO UPDATE SET
                client_id = excluded.client_id,
                client_secret = excluded.client_secret,
                website = excluded.website,
                redirect_uris = excluded.redirect_uris
            """,
            (instance, client_id, client_secret, website, redirect_uris),
        )

    return {"client_id": client_id, "client_secret": client_secret}


def get_authorize_url(
    instance: str, session_binding: str, user_id: int | None = None
) -> str:
    """Build a session-bound Mastodon authorization URL."""
    if not session_binding:
        raise ValueError("A browser session binding is required")

    instance = instance.rstrip("/")
    validate_outbound_url(instance)
    app = get_or_create_app(instance)
    state_token = _sign_state(instance, session_binding, user_id=user_id)

    query = urlencode(
        {
            "client_id": app["client_id"],
            "redirect_uri": callback_url(),
            "response_type": "code",
            "scope": SCOPES,
            "state": state_token,
        }
    )
    return f"{instance}/oauth/authorize?{query}"


def exchange_code(instance: str, code: str) -> dict:
    """Exchange an authorization code for an access token."""
    instance = instance.rstrip("/")
    validate_outbound_url(instance)
    # Pinned to the cached client: the code was issued to it, and a
    # re-registration here would invalidate the exchange.
    app = get_or_create_app(instance, allow_refresh=False)

    data = {
        "client_id": app["client_id"],
        "client_secret": app["client_secret"],
        "redirect_uri": callback_url(),
        "grant_type": "authorization_code",
        "code": code,
        "scope": SCOPES,
    }

    try:
        with httpx.Client(
            timeout=httpx.Timeout(30.0),
            follow_redirects=False,
        ) as client:
            response = client.post(f"{instance}/oauth/token", data=data)
            response.raise_for_status()
            result = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise RuntimeError("OAuth token exchange failed") from exc

    if not isinstance(result, dict):
        raise RuntimeError("Instance returned an invalid OAuth token response")
    return result


def verify_state(state: str, session_binding: str) -> tuple[str, int | None]:
    """Verify and consume a server-side, one-time OAuth state token.

    Returns (instance, user_id); user_id is None in single mode.

    The user_id re-read happens after consumption; this is safe because
    consumed rows are only marked (consumed_at), never deleted, and no
    concurrent path removes them. A concurrent duplicate-callback attempt
    fails inside _verify_state before reaching this SELECT.
    """
    instance = _verify_state(state, session_binding)
    with get_db() as db:
        nonce = state.rsplit("|", 2)[0]
        row = db.execute(
            "SELECT user_id FROM oauth_states WHERE nonce = ?", (nonce,)
        ).fetchone()
    return instance, (row["user_id"] if row else None)