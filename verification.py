"""Email verification: single-use token issuance, consumption, resend throttle.

Tokens are stored hashed (SHA-256; they carry 256 bits of entropy so a
plain digest is sufficient). One email_tokens table serves both the
verification flow (purpose='verify') and password reset (purpose='reset').
"""

import logging
from datetime import datetime, timedelta, timezone

from database import get_db
from security import new_token, token_hash

logger = logging.getLogger(__name__)

TOKEN_TTL_HOURS = 24
RESEND_LIMIT = 3  # per user, per purpose, per 24h

_TS = "%Y-%m-%d %H:%M:%S"


def _now_str() -> str:
    """Explicit UTC timestamp string. On PG the SQL NOW-ish default resolves
    in the session time zone while every reader assumes UTC, so this module
    binds UTC params everywhere instead (same rule as invites._now_str)."""
    return datetime.now(timezone.utc).strftime(_TS)


def issue_token(user_id: int, purpose: str) -> str:
    """Create a fresh token, invalidating prior unconsumed tokens of the
    same purpose (a new verification/reset supersedes the old link).

    A partial unique index (one unconsumed token per user+purpose)
    serializes concurrent issuers; on conflict the issuance retries once
    so exactly one live token survives.
    """
    token = new_token()
    expires = (
        datetime.now(timezone.utc) + timedelta(hours=TOKEN_TTL_HOURS)
    ).strftime(_TS)
    for _attempt in (1, 2):
        try:
            with get_db() as db:
                # Consume (not delete) the prior token so issuance history
                # stays countable for the resend throttle.
                db.execute(
                    "UPDATE email_tokens SET consumed_at = ?"
                    " WHERE user_id = ? AND purpose = ? AND consumed_at IS NULL",
                    (_now_str(), user_id, purpose),
                )
                db.execute(
                    "INSERT INTO email_tokens"
                    " (user_id, token_hash, purpose, expires_at)"
                    " VALUES (?, ?, ?, ?)",
                    (user_id, token_hash(token), purpose, expires),
                )
            return token
        except Exception as exc:  # noqa: BLE001
            if exc.__class__.__name__ not in ("IntegrityError", "UniqueViolation"):
                raise
            # Concurrent issuer won the race; clear the live row and retry.
            with get_db() as db:
                db.execute(
                    "DELETE FROM email_tokens"
                    " WHERE user_id = ? AND purpose = ? AND consumed_at IS NULL",
                    (user_id, purpose),
                )
    raise RuntimeError("email token issuance failed after retry")


def resend_allowed(user_id: int, purpose: str) -> bool:
    """Whether another token may be issued (anti-spam throttle)."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=24)
    ).strftime(_TS)
    with get_db() as db:
        row = db.execute(
            "SELECT COUNT(*) AS n FROM email_tokens"
            " WHERE user_id = ? AND purpose = ? AND created_at >= ?",
            (user_id, purpose, cutoff),
        ).fetchone()
    return (row["n"] or 0) < RESEND_LIMIT


def consume_token(token: str, purpose: str) -> int | None:
    """Atomically consume a token; returns the owning user_id or None.

    None for: unknown hash, wrong purpose, consumed, expired, or a
    concurrent consumer winning the race. Single-use and expiry are both
    enforced inside the conditional UPDATE (SQL-side, dialect-neutral)
    and confirmed via rowcount.
    """
    digest = token_hash(token)
    with get_db() as db:
        row = db.execute(
            "SELECT id, user_id, consumed_at, expires_at FROM email_tokens"
            " WHERE token_hash = ? AND purpose = ?",
            (digest, purpose),
        ).fetchone()
        if not row or row["consumed_at"]:
            return None
        cur = db.execute(
            "UPDATE email_tokens SET consumed_at = ?"
            " WHERE id = ? AND consumed_at IS NULL"
            " AND expires_at > ?",
            (_now_str(), row["id"], _now_str()),
        )
        if cur.rowcount != 1:
            # Concurrent consumer won, or the token expired.
            return None
    return row["user_id"]


def peek_token(token: str, purpose: str) -> int | None:
    """Validate a token WITHOUT consuming it (for rendering reset forms).

    Returns the owning user_id when the token is known, unconsumed, and
    unexpired; None otherwise. Consumption still happens on submit, so
    opening the link repeatedly cannot invalidate it.
    """
    digest = token_hash(token)
    with get_db() as db:
        row = db.execute(
            "SELECT user_id FROM email_tokens"
            " WHERE token_hash = ? AND purpose = ?"
            " AND consumed_at IS NULL AND expires_at > ?",
            (digest, purpose, _now_str()),
        ).fetchone()
    return row["user_id"] if row else None
