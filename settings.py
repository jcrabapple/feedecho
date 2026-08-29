"""Central configuration — the single source of truth for environment-driven settings."""

import logging
import os
from pathlib import Path
from typing import TypeVar, overload

_T = TypeVar("_T")

BASE_DIR = Path(__file__).resolve().parent

# ── Environment variable names ───────────────────────────────────────────────
#
# Every setting is read as FEEDECHO_*. The prefix shipped misspelled as
# FEEDCHO_ (one E) through v1.31.0 — issue #15 — so the legacy spelling is
# still honoured: the correct name wins when both are set, and each legacy
# name that actually supplied a value is recorded in LEGACY_ENV_IN_USE and
# named once by warn_legacy_env() at startup. That keeps an upgrade from
# silently un-configuring a running deployment (an unauthenticated UI, a
# database written to a different path) just because the operator's .env,
# systemd unit, or Nix module still uses the old name.
ENV_PREFIX = "FEEDECHO_"
LEGACY_ENV_PREFIX = "FEEDCHO_"

# (legacy name, canonical name) for every legacy variable read this process.
LEGACY_ENV_IN_USE: list[tuple[str, str]] = []


@overload
def env(name: str) -> str | None: ...


@overload
def env(name: str, default: _T) -> str | _T: ...


def env(name: str, default=None):
    """Read ``FEEDECHO_<name>``, falling back to the legacy ``FEEDCHO_<name>``.

    ``name`` is the suffix, e.g. ``env("AUTH_TOKEN")``. The default is returned
    unchanged (not coerced), so callers keep their existing contracts — an
    unset AUTH_TOKEN is still None, an unset DB_PATH is still a Path.
    """
    canonical = ENV_PREFIX + name
    legacy = LEGACY_ENV_PREFIX + name
    if canonical in os.environ:
        return os.environ[canonical]
    if legacy in os.environ:
        if (legacy, canonical) not in LEGACY_ENV_IN_USE:
            LEGACY_ENV_IN_USE.append((legacy, canonical))
        return os.environ[legacy]
    return default


def warn_legacy_env() -> None:
    """Warn once, naming every deprecated FEEDCHO_ variable this process read.

    Called from validate_config() at app startup rather than at import: the
    logging configuration is in place by then, so the warning actually lands
    in the operator's logs instead of on the logging lastResort handler.
    """
    if not LEGACY_ENV_IN_USE:
        return
    logging.getLogger("feedecho").warning(
        "Deprecated environment variable name(s) in use: %s. The %s prefix was "
        "a misspelling (issue #15); rename to the %s form. The old names still "
        "work but will be removed in a future release.",
        ", ".join(f"{old} -> {new}" for old, new in LEGACY_ENV_IN_USE),
        LEGACY_ENV_PREFIX,
        ENV_PREFIX,
    )


MODE = env("MODE", "single")
if MODE not in ("single", "multi"):
    raise ValueError(f"FEEDECHO_MODE must be 'single' or 'multi', got {MODE!r}")
MULTI = MODE == "multi"

DB_PATH = Path(env("DB_PATH", BASE_DIR / "feedecho.db"))
DATABASE_URL = env("DATABASE_URL", "")
# None when unset, matching the pre-refactor semantics exactly (all
# consumers use truthiness checks; empty string would be equivalent but
# None preserves the original contract).
AUTH_TOKEN = env("AUTH_TOKEN")
STATE_SECRET = env("STATE_SECRET", "")
BASE_URL = env("BASE_URL", "").strip()

# Where the source lives. Used as the last-resort website for the Mastodon
# OAuth app registration so posts never advertise a placeholder domain.
PROJECT_URL = "https://github.com/jcrabapple/feedecho"

# An unset callback URL derives from BASE_URL when one is configured; the
# example.com placeholder is only used when the deployment URL is unknown
# (where OAuth would fail loudly on redirect mismatch anyway).
CALLBACK_URL = env("CALLBACK_URL", "").strip() or (
    f"{BASE_URL.rstrip('/')}/oauth/callback"
    if BASE_URL
    else "https://feedecho.example.com/oauth/callback"
)

# The "website" sent with the OAuth app registration. Mastodon renders it as
# the link behind the application name on every post, so it must resolve:
# an explicit override wins, then this deployment's own URL, then the repo.
APP_WEBSITE = (
    env("APP_WEBSITE", "").strip()
    or BASE_URL.rstrip("/")
    or PROJECT_URL
)
ADMIN_EMAIL = env("ADMIN_EMAIL", "")
SESSION_SECRET = env("SESSION_SECRET", "")
ALLOW_SQLITE_FALLBACK = env("ALLOW_SQLITE_FALLBACK", "") == "1"
# Force the Secure flag on session cookies when TLS terminates in front
# of the app (Caddy/nginx proxy): the request scheme then reads http
# even though the client connection is https.
FORCE_SECURE_COOKIE = env("FORCE_SECURE_COOKIE", "") == "1"
# Comma-separated CIDR list of trusted reverse proxies. When the direct
# peer is inside this list, client IPs are derived from X-Forwarded-For
# (rightmost entry) instead of the TCP peer, so rate limits see real
# client addresses instead of one shared proxy IP.
TRUSTED_PROXIES = tuple(
    c.strip() for c in env("TRUSTED_PROXIES", "").split(",") if c.strip()
)

# ── Plan limits (hosted mode) ────────────────────────────────────────────────
#
# Per-plan resource caps. JSON via FEEDECHO_PLAN_LIMITS overrides defaults;
# unknown keys are kept, so adding a plan in env does not require a code
# change. Single mode is exempt from all of this (limits are only consulted
# through plans.limit_for(), which callers gate on settings.MULTI).
#
# Keys per plan:
#   max_feeds           — feeds the plan may have (0 = unlimited)
#   min_poll_interval   — lowest poll_interval in minutes the plan may set;
#                         the scheduler CLAMPS down-scoped values rather than
#                         rejecting, so plan downgrades never break existing
#                         feeds, they just poll less often
#   max_destinations    — total connected accounts across all types (0 = unlimited)
#   max_posts_per_hour  — drip ceiling for the plan (0 = unlimited)
DEFAULT_PLAN_LIMITS = {
    "trial": {
        "max_feeds": 5,
        "min_poll_interval": 15,
        "max_destinations": 5,
        "max_posts_per_hour": 60,
    },
    "beta": {
        "max_feeds": 25,
        "min_poll_interval": 5,
        "max_destinations": 15,
        "max_posts_per_hour": 240,
    },
    "paid": {
        "max_feeds": 100,
        "min_poll_interval": 1,
        "max_destinations": 50,
        "max_posts_per_hour": 1000,
    },
}


def _load_plan_limits() -> dict:
    import json

    raw = env("PLAN_LIMITS", "").strip()
    if not raw:
        return {k: dict(v) for k, v in DEFAULT_PLAN_LIMITS.items()}
    try:
        overrides = json.loads(raw)
    except ValueError:
        logging.getLogger("feedecho").warning(
            "FEEDECHO_PLAN_LIMITS is not valid JSON; using default plan limits"
        )
        return {k: dict(v) for k, v in DEFAULT_PLAN_LIMITS.items()}
    if not isinstance(overrides, dict):
        logging.getLogger("feedecho").warning(
            "FEEDECHO_PLAN_LIMITS must be a JSON object; using default plan limits"
        )
        return {k: dict(v) for k, v in DEFAULT_PLAN_LIMITS.items()}
    merged = {k: dict(v) for k, v in DEFAULT_PLAN_LIMITS.items()}
    for plan, limits in overrides.items():
        if isinstance(limits, dict):
            merged.setdefault(plan, {}).update(
                {k: v for k, v in limits.items() if isinstance(v, int) and v >= 0}
            )
    return merged


PLAN_LIMITS = _load_plan_limits()

# Trial handling: the plan column also carries 'single' semantics implicitly
# (single mode never checks). A trial whose trial_ends_at has passed is
# PAUSED by the scheduler (feeds skipped, cursor frozen — nothing is deleted)
# until the operator moves the user to 'beta'/'paid' or extends trial_ends_at.
TRIAL_GRACE_NOTE = env("TRIAL_GRACE_NOTE", "")

# ── Invite codes (hosted beta gate) ─────────────────────────────────────────
#
# When True, /register requires a valid unused invite code. Default is False
# so an upgrade never locks self-hosters out of their own instance (single
# mode ignores this entirely); the hosted deployment sets FEEDECHO_INVITES_REQUIRED=1.
INVITES_REQUIRED = env("INVITES_REQUIRED", "") == "1"


# ── Backdated entries ─────────────────────────────────────────────────────────
#
# When True, feed items that appear positionally OLDER than the cursor (so the
# position-based scan skips them) but carry a publish date within
# MAX_BACKDATED_ENTRY_DAYS of now are still delivered. Off by default so
# existing behaviour is unchanged; self-hosters who backdate posts can opt in.
ALLOW_BACKDATED_ENTRIES = env("ALLOW_BACKDATED_ENTRIES", "") == "1"
try:
    MAX_BACKDATED_ENTRY_DAYS = int(env("MAX_BACKDATED_ENTRY_DAYS", "3"))
except ValueError:
    logging.getLogger("feedecho").warning(
        "FEEDECHO_MAX_BACKDATED_ENTRY_DAYS is not a valid integer; using default of 3"
    )
    MAX_BACKDATED_ENTRY_DAYS = 3


def validate_config() -> None:
    """Fail fast on misconfigured multi mode. Called from app startup.

    Single mode never raises — it keeps the original permissive behavior.
    """
    # Runs in both modes: a self-hoster on the legacy names deserves the
    # rename notice even though nothing else here applies to single mode.
    warn_legacy_env()
    if not MULTI:
        return
    if not DATABASE_URL and not ALLOW_SQLITE_FALLBACK:
        raise RuntimeError(
            "FEEDECHO_DATABASE_URL must be set when FEEDECHO_MODE=multi "
            "(or set FEEDECHO_ALLOW_SQLITE_FALLBACK=1 for local development)"
        )
    if not SESSION_SECRET:
        raise RuntimeError(
            "FEEDECHO_SESSION_SECRET must be set when FEEDECHO_MODE=multi"
        )
    if len(SESSION_SECRET) < 32:
        raise RuntimeError(
            "FEEDECHO_SESSION_SECRET must be at least 32 characters in multi mode"
        )
    if not BASE_URL:
        # Deliberately a warning, not a raise: unlike DATABASE_URL and
        # SESSION_SECRET, an unset BASE_URL degrades one feature (the links in
        # verification and reset email) rather than risking data or auth, and
        # raising here would turn an upgrade into an outage for any existing
        # multi-mode self-hoster running without it. auth.py refuses to send
        # those emails rather than mailing a relative link nobody can open.
        logging.getLogger("feedecho").warning(
            "FEEDECHO_BASE_URL is not set: account verification and password "
            "reset emails cannot be sent (their links would be relative)."
        )
