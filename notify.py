"""Failure notifications and bounded retry for echo delivery.

Settings (stored in the settings table, editable on /settings):

- retry_max_attempts: max delivery attempts per item before giving up.
  0 = retry forever (pre-cap behavior). Default 5.
- retry_backoff_minutes: base delay between automatic retries; doubles per
  attempt (5m, 10m, 20m...). Default 5.
- notify_failure_threshold: consecutive delivery failures on one echo that
  trigger a notification email. 0 = notifications off. Default 3.
- notify_email: address to notify. Falls back to the first email account.

Notifications use the existing SMTP settings; if SMTP isn't configured,
notifications are silently skipped.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from database import get_db
from email_sender import get_smtp_settings, send_email

logger = logging.getLogger("feedecho.notify")

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BACKOFF_MINUTES = 5
DEFAULT_NOTIFY_THRESHOLD = 3


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_str() -> str:
    return _now().strftime("%Y-%m-%d %H:%M:%S")


def get_setting_int(key: str, default: int, user_id: int = 1) -> int:
    with get_db() as db:
        row = db.execute(
            "SELECT value FROM settings WHERE key = ? AND user_id = ?",
            (key, user_id),
        ).fetchone()
    if not row or row["value"] in (None, ""):
        return default
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return default


def next_retry_delay(attempt_count: int) -> str:
    """Backoff timestamp for the next automatic retry of a failed row."""
    base = max(1, get_setting_int("retry_backoff_minutes", DEFAULT_BACKOFF_MINUTES))
    delay = base * (2 ** max(0, attempt_count - 1))
    delay = min(delay, 24 * 60)  # cap at 1 day
    return (_now() + timedelta(minutes=delay)).strftime("%Y-%m-%d %H:%M:%S")


def max_attempts() -> int:
    return get_setting_int("retry_max_attempts", DEFAULT_MAX_ATTEMPTS)


def _consecutive_failures(echo_id: int) -> int:
    """Consecutive failed delivery attempts at the tail of an echo's history.

    Counts attempts (not rows): a single item failing repeatedly still
    accumulates toward the notification threshold via its attempt_count.
    """
    with get_db() as db:
        rows = db.execute(
            """
            SELECT status, attempt_count FROM posted_items
             WHERE echo_id = ?
             ORDER BY id DESC LIMIT 50
            """,
            (echo_id,),
        ).fetchall()
    count = 0
    for row in rows:
        if row["status"] == "failed":
            count += max(1, row["attempt_count"])
        else:
            break
    return count


def _notify_state(echo_id: int) -> str | None:
    with get_db() as db:
        row = db.execute(
            "SELECT value FROM settings WHERE key = ?", (f"notify_alerted_echo_{echo_id}",)
        ).fetchone()
    return row["value"] if row else None


def _set_notify_state(echo_id: int, value: str | None) -> None:
    key = f"notify_alerted_echo_{echo_id}"
    with get_db() as db:
        if value is None:
            db.execute("DELETE FROM settings WHERE key = ?", (key,))
        else:
            # user_id is pinned to 1 here: notify state is single-tenant
            # bookkeeping; multi-tenant scoping lands in the auth work.
            db.execute(
                "INSERT INTO settings (user_id, key, value) VALUES (1, ?, ?) "
                "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
                (key, value),
            )


def _notify_address() -> str | None:
    with get_db() as db:
        row = db.execute(
            "SELECT value FROM settings WHERE key = 'notify_email'"
        ).fetchone()
        if row and row["value"]:
            return row["value"]
        row = db.execute("SELECT email FROM email_accounts ORDER BY id LIMIT 1").fetchone()
    return row["email"] if row else None


def _echo_label(echo_id: int) -> str:
    with get_db() as db:
        row = db.execute(
            """
            SELECT f.name AS feed_name, e.destination_type
              FROM echoes e JOIN feeds f ON f.id = e.feed_id
             WHERE e.id = ?
            """,
            (echo_id,),
        ).fetchone()
    if not row:
        return f"echo #{echo_id}"
    return f"echo #{echo_id} ({row['feed_name']} → {row['destination_type']})"


def record_failure(echo_id: int) -> None:
    """Track a delivery failure; send an alert email at the threshold."""
    threshold = get_setting_int("notify_failure_threshold", DEFAULT_NOTIFY_THRESHOLD)
    if threshold <= 0:
        return

    failures = _consecutive_failures(echo_id)
    if failures < threshold or _notify_state(echo_id):
        return

    if not get_smtp_settings():
        logger.warning("Failure notification suppressed for echo %s: SMTP not configured", echo_id)
        return

    address = _notify_address()
    if not address:
        return

    label = _echo_label(echo_id)
    try:
        send_email(
            to_email=address,
            subject=f"FeedEcho: {label} failing",
            body=(
                f"{label} has failed {failures} consecutive deliveries.\n\n"
                f"Check History in FeedEcho for the error details. Common causes:\n"
                f"- expired or revoked Mastodon token\n"
                f"- SMTP credentials changed\n"
                f"- destination unreachable\n\n"
                f"You'll get one follow-up email when it recovers."
            ),
        )
        _set_notify_state(echo_id, _now_str())
        logger.info("Failure notification sent for echo %s (%s failures)", echo_id, failures)
    except Exception:
        logger.exception("Failed to send failure notification for echo %s", echo_id)


def record_success(echo_id: int) -> None:
    """On recovery after an alert, send one all-clear email and reset state."""
    alerted_at = _notify_state(echo_id)
    if not alerted_at:
        return

    _set_notify_state(echo_id, None)

    if not get_smtp_settings():
        return
    address = _notify_address()
    if not address:
        return

    label = _echo_label(echo_id)
    try:
        send_email(
            to_email=address,
            subject=f"FeedEcho: {label} recovered",
            body=(
                f"{label} is delivering again (alert was raised at {alerted_at} UTC).\n"
                f"No action needed."
            ),
        )
        logger.info("Recovery notification sent for echo %s", echo_id)
    except Exception:
        logger.exception("Failed to send recovery notification for echo %s", echo_id)
