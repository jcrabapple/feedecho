"""Central configuration — the single source of truth for environment-driven settings."""

import logging
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

MODE = os.environ.get("FEEDCHO_MODE", "single")
if MODE not in ("single", "multi"):
    raise ValueError(f"FEEDCHO_MODE must be 'single' or 'multi', got {MODE!r}")
MULTI = MODE == "multi"

DB_PATH = Path(os.environ.get("FEEDCHO_DB_PATH", BASE_DIR / "feedecho.db"))
DATABASE_URL = os.environ.get("FEEDCHO_DATABASE_URL", "")
# None when unset, matching the pre-refactor semantics exactly (all
# consumers use truthiness checks; empty string would be equivalent but
# None preserves the original contract).
AUTH_TOKEN = os.environ.get("FEEDCHO_AUTH_TOKEN")
STATE_SECRET = os.environ.get("FEEDCHO_STATE_SECRET", "")
BASE_URL = os.environ.get("FEEDCHO_BASE_URL", "").strip()

# Where the source lives. Used as the last-resort website for the Mastodon
# OAuth app registration so posts never advertise a placeholder domain.
PROJECT_URL = "https://github.com/jcrabapple/feedecho"

# An unset callback URL derives from BASE_URL when one is configured; the
# example.com placeholder is only used when the deployment URL is unknown
# (where OAuth would fail loudly on redirect mismatch anyway).
CALLBACK_URL = os.environ.get("FEEDCHO_CALLBACK_URL", "").strip() or (
    f"{BASE_URL.rstrip('/')}/oauth/callback"
    if BASE_URL
    else "https://feedecho.example.com/oauth/callback"
)

# The "website" sent with the OAuth app registration. Mastodon renders it as
# the link behind the application name on every post, so it must resolve:
# an explicit override wins, then this deployment's own URL, then the repo.
APP_WEBSITE = (
    os.environ.get("FEEDCHO_APP_WEBSITE", "").strip()
    or BASE_URL.rstrip("/")
    or PROJECT_URL
)
ADMIN_EMAIL = os.environ.get("FEEDCHO_ADMIN_EMAIL", "")
SESSION_SECRET = os.environ.get("FEEDCHO_SESSION_SECRET", "")
ALLOW_SQLITE_FALLBACK = (
    os.environ.get("FEEDCHO_ALLOW_SQLITE_FALLBACK", "") == "1"
)
# Force the Secure flag on session cookies when TLS terminates in front
# of the app (Caddy/nginx proxy): the request scheme then reads http
# even though the client connection is https.
FORCE_SECURE_COOKIE = (
    os.environ.get("FEEDCHO_FORCE_SECURE_COOKIE", "") == "1"
)
# Comma-separated CIDR list of trusted reverse proxies. When the direct
# peer is inside this list, client IPs are derived from X-Forwarded-For
# (rightmost entry) instead of the TCP peer, so rate limits see real
# client addresses instead of one shared proxy IP.
TRUSTED_PROXIES = tuple(
    c.strip() for c in os.environ.get("FEEDCHO_TRUSTED_PROXIES", "").split(",") if c.strip()
)


def validate_config() -> None:
    """Fail fast on misconfigured multi mode. Called from app startup.

    Single mode never raises — it keeps the original permissive behavior.
    """
    if not MULTI:
        return
    if not DATABASE_URL and not ALLOW_SQLITE_FALLBACK:
        raise RuntimeError(
            "FEEDCHO_DATABASE_URL must be set when FEEDCHO_MODE=multi "
            "(or set FEEDCHO_ALLOW_SQLITE_FALLBACK=1 for local development)"
        )
    if not SESSION_SECRET:
        raise RuntimeError(
            "FEEDCHO_SESSION_SECRET must be set when FEEDCHO_MODE=multi"
        )
    if len(SESSION_SECRET) < 32:
        raise RuntimeError(
            "FEEDCHO_SESSION_SECRET must be at least 32 characters in multi mode"
        )
    if not BASE_URL:
        # Deliberately a warning, not a raise: unlike DATABASE_URL and
        # SESSION_SECRET, an unset BASE_URL degrades one feature (the links in
        # verification and reset email) rather than risking data or auth, and
        # raising here would turn an upgrade into an outage for any existing
        # multi-mode self-hoster running without it. auth.py refuses to send
        # those emails rather than mailing a relative link nobody can open.
        logging.getLogger("feedecho").warning(
            "FEEDCHO_BASE_URL is not set: account verification and password "
            "reset emails cannot be sent (their links would be relative)."
        )
