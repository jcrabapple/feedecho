"""Background scheduler and concurrency-safe feed delivery pipeline."""

from __future__ import annotations

import json
import logging
import secrets
import threading
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

import settings
from database import get_db
from email_sender import send_email
from feed_parser import fetch_feed, fetch_image, get_new_items, truncate
from filters import is_filtered
from mastodon import post_status, upload_media
from bluesky import (
    BLUESKY_IMAGE_TYPES,
    MAX_BLOB_BYTES,
    BlueskyAuthError,
    BlueskyError,
    build_facets,
    build_image_embed,
    create_post,
    create_session,
    refresh_session,
    resolve_pds,
    session_expiry,
    truncate_graphemes,
    upload_blob,
)
from notify import (
    max_attempts,
    next_retry_delay,
    record_failure,
    record_success,
)
from template_engine import render_template
import alt_text

logger = logging.getLogger("feedecho.scheduler")

scheduler: BackgroundScheduler | None = None

MASTODON_MAX_CHARS = 500
BLUESKY_MAX_GRAPHEMES = 300
PENDING_RECLAIM_SECONDS = 10 * 60
FEED_LEASE_SECONDS = 15 * 60
DRIP_QUEUE_CAP = 30
# Releases per flush on the unlimited (limit removed) path, to avoid
# dumping a stale backlog as one burst.
DRIP_DOWNGRADE_BATCH = 10

# Per-echo locks serializing drip rate checks across the feed-check and
# flush threads, so concurrent workers cannot exceed the hourly cap.
_drip_locks: dict[int, threading.Lock] = {}
_drip_locks_guard = threading.Lock()


def _drip_lock(echo_id: int) -> threading.Lock:
    with _drip_locks_guard:
        lock = _drip_locks.get(echo_id)
        if lock is None:
            lock = threading.Lock()
            _drip_locks[echo_id] = lock
        return lock


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _timestamp_after(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _acquire_feed_lease(feed_id: int, lease_token: str) -> bool:
    """Atomically acquire a per-feed lease, reclaiming expired leases only."""
    now = _now()
    expires_at = _timestamp_after(FEED_LEASE_SECONDS)

    with get_db() as db:
        result = db.execute(
            """
            UPDATE feeds
               SET lease_token = ?,
                   lease_expires_at = ?
             WHERE id = ?
               AND (
                    lease_token IS NULL
                    OR lease_expires_at IS NULL
                    OR lease_expires_at <= ?
               )
            """,
            (lease_token, expires_at, feed_id, now),
        )
        return result.rowcount == 1


def _renew_feed_lease(feed_id: int, lease_token: str) -> bool:
    """Extend a lease owned by this worker without taking another worker's lease."""
    with get_db() as db:
        result = db.execute(
            """
            UPDATE feeds
               SET lease_expires_at = ?
             WHERE id = ?
               AND lease_token = ?
            """,
            (_timestamp_after(FEED_LEASE_SECONDS), feed_id, lease_token),
        )
        return result.rowcount == 1


def _release_feed_lease(feed_id: int, lease_token: str) -> None:
    """Release only the lease held by this worker."""
    with get_db() as db:
        db.execute(
            """
            UPDATE feeds
               SET lease_token = NULL,
                   lease_expires_at = NULL
             WHERE id = ?
               AND lease_token = ?
            """,
            (feed_id, lease_token),
        )


def _update_last_fetched(feed_id: int, lease_token: str) -> None:
    with get_db() as db:
        db.execute(
            """
            UPDATE feeds
               SET last_fetched = ?
             WHERE id = ?
               AND lease_token = ?
            """,
            (_now(), feed_id, lease_token),
        )


def _update_cursor(feed_id: int, lease_token: str, cursor_id: str) -> bool:
    """Advance a cursor only while the current worker still owns the lease."""
    with get_db() as db:
        result = db.execute(
            """
            UPDATE feeds
               SET last_item_id = ?,
                   last_fetched = ?
             WHERE id = ?
               AND lease_token = ?
            """,
            (cursor_id, _now(), feed_id, lease_token),
        )
        return result.rowcount == 1


def check_feed(feed_id: int) -> None:
    """Fetch and deliver new feed items under an exclusive per-feed lease."""
    lease_token = secrets.token_urlsafe(32)

    if not _acquire_feed_lease(feed_id, lease_token):
        logger.info("Feed %s is already being checked; skipping", feed_id)
        return

    try:
        _check_feed_with_lease(feed_id, lease_token)
    finally:
        _release_feed_lease(feed_id, lease_token)


def _check_feed_with_lease(feed_id: int, lease_token: str) -> None:
    with get_db() as db:
        feed = db.execute(
            "SELECT * FROM feeds WHERE id = ? AND deleted_at IS NULL", (feed_id,)
        ).fetchone()
        if not feed:
            logger.warning("Feed %s not found", feed_id)
            return

        if feed["paused"]:
            logger.info("Feed %s (%s): paused; skipping", feed_id, feed["name"])
            _update_last_fetched(feed_id, lease_token)
            return

        echoes = db.execute(
            "SELECT * FROM echoes WHERE feed_id = ? AND enabled = 1",
            (feed_id,),
        ).fetchall()
        # Hosted mode gate: unverified owners' echoes are skipped until
        # the account email is verified (soft-block, silent in single mode
        # where every row belongs to user 1 whose email_verified is 0 —
        # hence the settings.MULTI guard).
        if settings.MULTI and echoes:
            verified = {
                r["id"]
                for r in db.execute(
                    "SELECT id FROM users WHERE email_verified = 1"
                ).fetchall()
            }
            echoes = [e for e in echoes if e["user_id"] in verified]

    feed_url = feed["url"]
    feed_name = feed["name"]
    last_seen_id = feed["last_item_id"]

    if not echoes:
        logger.info("Feed %s (%s): no enabled echoes", feed_id, feed_name)
        _update_last_fetched(feed_id, lease_token)
        return

    try:
        feed_data = fetch_feed(feed_url)
    except Exception:
        logger.exception("Feed %s (%s): fetch failed", feed_id, feed_name)
        _update_last_fetched(feed_id, lease_token)
        return

    items = feed_data.get("items") or []
    if not items:
        logger.info("Feed %s (%s): no items", feed_id, feed_name)
        _update_last_fetched(feed_id, lease_token)
        return

    if last_seen_id is None:
        _update_cursor(feed_id, lease_token, items[0]["id"])
        logger.info(
            "Feed %s (%s): initialized cursor to %s",
            feed_id,
            feed_name,
            items[0]["id"],
        )
        return

    new_items = get_new_items(items, last_seen_id)
    if not new_items:
        _update_last_fetched(feed_id, lease_token)
        return

    cursor_id = last_seen_id

    for item in new_items:
        if not _renew_feed_lease(feed_id, lease_token):
            logger.warning(
                "Feed %s (%s): lease was lost before processing item %s",
                feed_id,
                feed_name,
                item["id"],
            )
            return

        all_succeeded = True
        for echo in echoes:
            if not process_echo(echo, item, feed_name=feed_name):
                all_succeeded = False

        if not all_succeeded:
            # H-2: Never process or advance past a failed earlier item.
            logger.warning(
                "Feed %s (%s): delivery failed for item %s; stopping cursor advancement",
                feed_id,
                feed_name,
                item["id"],
            )
            break

        cursor_id = item["id"]

    if cursor_id != last_seen_id:
        if not _update_cursor(feed_id, lease_token, cursor_id):
            logger.warning("Feed %s: cursor was not updated because lease was lost", feed_id)
    else:
        _update_last_fetched(feed_id, lease_token)

    _retry_due_failures(feed_id, echoes, feed_name=feed_name)


def _retry_due_failures(feed_id: int, echoes, feed_name: str = "") -> None:
    """Reprocess failed rows whose backoff has elapsed, regardless of cursor.

    Normal cursor replay only covers items at-or-after the cursor. Failed rows
    can sit behind it (e.g. an item that failed, was manually reset, or was
    blocked while another echo's item gated advancement). This sweep gives
    them their scheduled retries without disturbing feed ordering guarantees.
    """
    if not echoes:
        return
    echo_ids = [e["id"] for e in echoes]
    placeholders = ",".join("?" for _ in echo_ids)

    with get_db() as db:
        due = db.execute(
            f"""
            SELECT id, echo_id, item_id FROM posted_items
             WHERE echo_id IN ({placeholders})
               AND status = 'failed'
               AND next_retry_at IS NOT NULL
               AND next_retry_at <= ?
             ORDER BY id
             LIMIT 25
            """,
            (*echo_ids, _now()),
        ).fetchall()

    if not due:
        return

    # We only have item_id stored, not the full item payload. Fetch the feed
    # once and match; items that have aged out of the feed are marked gave_up.
    try:
        feed_data = fetch_feed(db_feed_url(feed_id))
    except Exception:
        logger.exception("Feed %s: fetch failed during retry sweep", feed_id)
        return

    items_by_id = {it["id"]: it for it in (feed_data.get("items") or [])}
    echoes_by_id = {e["id"]: e for e in echoes}

    for row in due:
        item = items_by_id.get(row["item_id"])
        if item is None:
            with get_db() as db:
                db.execute(
                    """UPDATE posted_items SET status = 'gave_up',
                          error_message = 'Item no longer in feed; cannot retry',
                          next_retry_at = NULL
                        WHERE id = ? AND status = 'failed'""",
                    (row["id"],),
                )
            continue
        echo = echoes_by_id.get(row["echo_id"])
        if echo is not None:
            process_echo(echo, item, feed_name=feed_name)


def db_feed_url(feed_id: int) -> str:
    with get_db() as db:
        row = db.execute(
            "SELECT url FROM feeds WHERE id = ? AND deleted_at IS NULL", (feed_id,)
        ).fetchone()
    if not row:
        raise ValueError(f"Feed {feed_id} not found")
    return row["url"]


def _claim_post(echo_id: int, item: dict) -> tuple[int, str] | None:
    """Atomically claim an echo/item row.

    Returns ``(posted_item_id, claim_token)`` only for the worker that owns the
    pending attempt. Fresh pending rows cannot be claimed. Failed rows can be
    reclaimed once their backoff (next_retry_at) has elapsed, and pending rows
    abandoned for over ten minutes can be reclaimed.
    """
    item_id = item["id"]
    claim_token = secrets.token_urlsafe(32)
    now = _now()
    stale_before = _timestamp_after(-PENDING_RECLAIM_SECONDS)

    with get_db() as db:
        result = db.execute(
            """
            INSERT INTO posted_items (
                echo_id, item_id, item_title, item_url, status,
                claimed_at, claim_token, attempt_count, error_message
            )
            VALUES (?, ?, ?, ?, 'pending', ?, ?, 1, NULL)
            ON CONFLICT(echo_id, item_id) DO UPDATE SET
                item_title = excluded.item_title,
                item_url = excluded.item_url,
                status = 'pending',
                claimed_at = excluded.claimed_at,
                claim_token = excluded.claim_token,
                attempt_count = posted_items.attempt_count + 1,
                error_message = NULL
            WHERE posted_items.status = 'failed'
               AND (
                    posted_items.next_retry_at IS NULL
                    OR posted_items.next_retry_at <= ?
               )
               OR (
                    posted_items.status = 'pending'
                    AND (
                        posted_items.claimed_at IS NULL
                        OR posted_items.claimed_at <= ?
                    )
               )
            """,
            (
                echo_id,
                item_id,
                item.get("title", ""),
                item.get("link", ""),
                now,
                claim_token,
                now,
                stale_before,
            ),
        )

        if result.rowcount != 1:
            return None

        row = db.execute(
            """
            SELECT id, claim_token
              FROM posted_items
             WHERE echo_id = ?
               AND item_id = ?
            """,
            (echo_id, item_id),
        ).fetchone()

    if not row or row["claim_token"] != claim_token:
        return None

    return row["id"], claim_token


def _row_state(echo_id: int, item_id: str) -> str | None:
    with get_db() as db:
        row = db.execute(
            "SELECT status FROM posted_items WHERE echo_id = ? AND item_id = ?",
            (echo_id, item_id),
        ).fetchone()
    return row["status"] if row else None


def _post_succeeded(echo_id: int, item_id: str) -> bool:
    """True when the item is in a terminal state for this echo.

    'success', 'filtered', 'gave_up', and 'queued' all count as handled:
    they must not be retried and must not block cursor advancement.
    'queued' means the item is waiting for digest flush.
    A 'failed' row waiting out its retry backoff returns False here —
    it still gates the cursor — but process_echo distinguishes it from
    a fresh failure via _row_state so the run stops quietly instead of
    logging new failures.
    """
    return _row_state(echo_id, item_id) in ("success", "filtered", "gave_up", "queued")


def _record_filtered(echo_id: int, item: dict) -> None:
    """Record an item suppressed by the echo's keyword filter.

    Uses status 'filtered' so history shows what was dropped and the claim
    logic never retries it (only 'failed' and stale 'pending' rows are
    reclaimable).
    """
    with get_db() as db:
        db.execute(
            """
            INSERT INTO posted_items (
                echo_id, item_id, item_title, item_url, status,
                attempt_count, error_message
            )
            VALUES (?, ?, ?, ?, 'filtered', 0, NULL)
            ON CONFLICT(echo_id, item_id) DO NOTHING
            """,
            (echo_id, item["id"], item.get("title", ""), item.get("link", "")),
        )


def process_echo(echo, item: dict, feed_name: str = "") -> bool:
    """Deliver one item to one echo using an atomic pending-row claim.

    Items past a drip rate limit are held in the drip queue (status
    'queued') instead of dispatching immediately; flush_drips() releases
    them as the rate window allows.

    feed_name is optional context for template rendering ({{ feed_name }}).
    """
    echo_id = echo["id"]
    item_id = item["id"]

    # Keyword filter: suppressed items count as handled so the cursor
    # advances and they are never delivered or retried.
    # .get-style access keeps plain-dict fixtures in tests working.
    try:
        filter_kw = echo["filter_keywords"]
        filter_mode = echo["filter_mode"]
    except (KeyError, IndexError):
        filter_kw, filter_mode = None, None
    if is_filtered(item, filter_kw, filter_mode):
        _record_filtered(echo_id, item)
        logger.info("Echo %s: item %s suppressed by keyword filter", echo_id, item_id)
        return True

    claimed = _claim_post(echo_id, item)
    if claimed is None:
        # Terminal states (success/filtered/gave_up) count as handled. A fresh
        # pending row belongs to another worker, and a failed row waiting out
        # its retry backoff is deferred — both are "not done", so the cursor
        # stays put and the run stops at this item (H-2), quietly.
        return _post_succeeded(echo_id, item_id)

    posted_id, claim_token = claimed

    if _drip_applies(echo):
        # Serialize the rate check + dispatch per echo so the flush job
        # and feed checks cannot both see an open window and exceed the cap.
        with _drip_lock(echo_id):
            if _drip_rate(echo_id) >= _drip_limit(echo):
                return _queue_for_drip(echo, item, posted_id, claim_token)
            return _render_and_dispatch(echo, item, feed_name, posted_id, claim_token)

    return _render_and_dispatch(echo, item, feed_name, posted_id, claim_token)


def _drip_limit(echo) -> int:
    """The echo's drip rate limit (posts/hour); 0 disables drip."""
    try:
        return int(echo["drip_limit"] or 0)
    except (KeyError, IndexError, TypeError, ValueError):
        return 0


def _drip_applies(echo) -> bool:
    """Drip applies to rate-limited echoes with instant delivery only.

    Digest-mode echoes already batch on their own schedule, so a drip
    limit would double-throttle them.
    """
    if _drip_limit(echo) <= 0:
        return False
    try:
        delivery_mode = echo["delivery_mode"] or "instant"
    except (KeyError, IndexError):
        delivery_mode = "instant"
    return delivery_mode != "digest"


def _drip_rate(echo_id: int) -> int:
    """Successful posts by this echo within the sliding 60-minute window."""
    cutoff = _timestamp_after(-60 * 60)
    with get_db() as db:
        row = db.execute(
            """
            SELECT COUNT(*) AS n FROM posted_items
             WHERE echo_id = ?
               AND status = 'success'
               AND posted_at >= ?
            """,
            (echo_id, cutoff),
        ).fetchone()
    return row["n"] if row else 0


def _queue_for_drip(echo, item: dict, posted_id: int, claim_token: str) -> bool:
    """Hold an item until the drip window has room.

    Marks the posted row 'queued' (terminal for the cursor, same as
    digest mode) and stores the item payload for flush_drips(). When the
    queue is full the item is dropped as gave_up so an oversubscribed
    feed cannot pile up an unbounded backlog.
    """
    echo_id = echo["id"]
    item_id = item["id"]

    with get_db() as db:
        row = db.execute(
            "SELECT COUNT(*) AS n FROM drip_items WHERE echo_id = ?", (echo_id,)
        ).fetchone()
    if row["n"] >= DRIP_QUEUE_CAP:
        logger.warning(
            "Echo %s: drip queue full (%d); dropping item %s",
            echo_id,
            DRIP_QUEUE_CAP,
            item_id,
        )
        return _fail_post(
            posted_id,
            claim_token,
            echo_id,
            f"Drip queue full ({DRIP_QUEUE_CAP} pending); item dropped",
            permanent=True,
        )

    # 'raw' holds feedparser internals (struct_time etc.) that are not
    # JSON-serializable and never used for rendering.
    payload = {k: v for k, v in item.items() if k != "raw"}
    with get_db() as db:
        db.execute(
            """
            INSERT INTO drip_items (echo_id, item_id, item_json)
            VALUES (?, ?, ?)
            ON CONFLICT(echo_id, item_id) DO NOTHING
            """,
            (echo_id, item_id, json.dumps(payload)),
        )

    ok = _update_post(posted_id, claim_token, "queued")
    if ok:
        # Deliberately no record_success() here: queueing is not a
        # delivery, and clearing the failure-notify state on a hold would
        # suppress alerts for echoes that are not actually delivering.
        logger.info("Echo %s: item %s held for drip release", echo_id, item_id)
    return ok


def _reclaim_queued(echo_id: int, item_id: str) -> tuple[int, str] | None:
    """Transition a queued posted row back to pending with a fresh claim.

    Returns (posted_id, claim_token), or None if the row is no longer
    queued (already released or finalized by another worker).

    Deliberately does not bump attempt_count: release attempts are
    tracked on the drip_items row so failed releases can retry from the
    stored payload without burning the normal retry budget.
    """
    claim_token = secrets.token_urlsafe(32)
    with get_db() as db:
        row = db.execute(
            """
            SELECT id FROM posted_items
             WHERE echo_id = ? AND item_id = ? AND status = 'queued'
            """,
            (echo_id, item_id),
        ).fetchone()
        if not row:
            return None
        result = db.execute(
            """
            UPDATE posted_items
               SET status = 'pending',
                   claimed_at = ?,
                   claim_token = ?
             WHERE id = ? AND status = 'queued'
            """,
            (_now(), claim_token, row["id"]),
        )
        if result.rowcount != 1:
            return None
    return row["id"], claim_token


def _render_and_dispatch(
    echo,
    item: dict,
    feed_name: str,
    posted_id: int,
    claim_token: str,
) -> bool:
    """Render the echo template and dispatch to the destination sender.

    Shared by process_echo (fresh feed items) and flush_drips (released
    queue items) so both paths get identical rendering, failure handling,
    and image/alt-text/CW behavior.
    """
    echo_id = echo["id"]
    item_id = item["id"]

    try:
        content = render_template(echo["template"], item, feed_name=feed_name)
    except Exception as e:
        logger.exception("Echo %s: template render failed for item %s", echo_id, item_id)
        gave_up = _fail_post(
            posted_id,
            claim_token,
            echo_id,
            f"Template rendering failed: {e}",
        )
        return gave_up

    if not content.strip():
        gave_up = _fail_post(posted_id, claim_token, echo_id, "Rendered content was empty")
        return gave_up

    if echo["destination_type"] == "mastodon":
        return _send_mastodon(echo, item, content, echo["destination_id"], posted_id, claim_token)

    if echo["destination_type"] == "email":
        return _send_email_echo(
            echo,
            item,
            content,
            echo["destination_id"],
            posted_id,
            claim_token,
        )

    if echo["destination_type"] == "bluesky":
        return _send_bluesky(
            echo,
            item,
            content,
            echo["destination_id"],
            posted_id,
            claim_token,
        )

    gave_up = _fail_post(
        posted_id,
        claim_token,
        echo_id,
        f"Unknown destination type: {echo['destination_type']}",
    )
    return gave_up


def _update_post(
    posted_id: int,
    claim_token: str,
    status: str,
    error: str | None = None,
    post_url: str | None = None,
) -> bool:
    """Finalize only the pending row still owned by this claim token."""
    with get_db() as db:
        result = db.execute(
            """
            UPDATE posted_items
               SET status = ?,
                   error_message = ?,
                   post_url = COALESCE(?, post_url),
                   claimed_at = NULL,
                   claim_token = NULL,
                   posted_at = CURRENT_TIMESTAMP
             WHERE id = ?
               AND status = 'pending'
               AND claim_token = ?
            """,
            (status, error, post_url, posted_id, claim_token),
        )
        return result.rowcount == 1


def _fail_post(
    posted_id: int,
    claim_token: str,
    echo_id: int,
    error: str,
    permanent: bool = False,
) -> bool:
    """Mark a claimed row failed, scheduling the next automatic retry.

    If the row has exhausted retry_max_attempts, it is marked 'gave_up'
    (terminal) instead, which unblocks the feed cursor. Passing
    ``permanent=True`` skips straight to 'gave_up' for failures that no
    number of retries can fix (missing account, rejected credentials).
    Returns the final status written.
    """
    cap = max_attempts(echo_id)

    with get_db() as db:
        row = db.execute(
            "SELECT attempt_count FROM posted_items WHERE id = ?", (posted_id,)
        ).fetchone()
    attempts = row["attempt_count"] if row else 1

    if permanent or (cap > 0 and attempts >= cap):
        final = "gave_up"
        if permanent:
            error_out = error
        else:
            error_out = f"Gave up after {attempts} attempts. Last error: {error}"
        retry_at = None
    else:
        final = "failed"
        error_out = error
        retry_at = next_retry_delay(attempts, echo_id=echo_id)

    with get_db() as db:
        db.execute(
            """
            UPDATE posted_items
               SET status = ?,
                   error_message = ?,
                   next_retry_at = ?,
                   claimed_at = NULL,
                   claim_token = NULL,
                   posted_at = CURRENT_TIMESTAMP
             WHERE id = ?
               AND status = 'pending'
               AND claim_token = ?
            """,
            (final, error_out, retry_at, posted_id, claim_token),
        )

    record_failure(echo_id)
    if final == "gave_up":
        logger.error(
            "Echo %s: item gave up after %s attempts: %s", echo_id, attempts, error
        )
    return final == "gave_up"


def _send_mastodon(
    echo,
    item: dict,
    content: str,
    account_id: int,
    posted_id: int,
    claim_token: str,
) -> bool:
    with get_db() as db:
        account = db.execute(
            "SELECT * FROM accounts WHERE id = ?",
            (account_id,),
        ).fetchone()

    if not account:
        return _fail_post(
            posted_id, claim_token, echo["id"], f"Account {account_id} not found"
        )

    # Content warning: per-echo CW text, applied via Mastodon's spoiler_text
    try:
        cw_text = (echo["content_warning"] or "").strip()
    except (KeyError, IndexError):
        cw_text = ""
    sensitive = bool(cw_text)

    # Image attachment: if enabled, extract and upload the item's first image
    media_ids: list[str] = []
    try:
        attach_image = bool(echo["attach_image"])
    except (KeyError, IndexError):
        attach_image = False

    if attach_image:
        image_url = item.get("image_url", "")
        if image_url:
            image_result = fetch_image(image_url)
            if image_result:
                img_bytes, img_type = image_result
                description = ""
                if alt_text.is_enabled(user_id=echo["user_id"]):
                    try:
                        description = alt_text.generate_alt_text(
                            img_bytes, img_type, user_id=echo["user_id"]
                        )
                        if description:
                            logger.info(
                                "Echo %s: generated alt text for item %s (%d chars)",
                                echo["id"], item["id"], len(description),
                            )
                    except Exception:
                        logger.warning(
                            "Echo %s: alt text generation failed for item %s",
                            echo["id"], item["id"],
                            exc_info=True,
                        )
                uploaded = upload_media(
                    instance=account["instance"],
                    access_token=account["access_token"],
                    image_bytes=img_bytes,
                    content_type=img_type,
                    description=description,
                )
                if uploaded and uploaded.get("id"):
                    media_ids.append(str(uploaded["id"]))
                    logger.info(
                        "Echo %s: uploaded image %s for item %s",
                        echo["id"],
                        uploaded["id"],
                        item["id"],
                    )
                else:
                    logger.warning(
                        "Echo %s: image upload failed for item %s, posting text-only",
                        echo["id"],
                        item["id"],
                    )
            else:
                logger.info(
                    "Echo %s: image fetch failed or invalid for item %s, posting text-only",
                    echo["id"],
                    item["id"],
                )

    try:
        post_status(
            instance=account["instance"],
            access_token=account["access_token"],
            content=truncate(content, MASTODON_MAX_CHARS),
            visibility=echo["visibility"],
            sensitive=sensitive,
            spoiler_text=cw_text,
            media_ids=media_ids or None,
        )
    except Exception:
        logger.exception("Echo %s: Mastodon post failed", echo["id"])
        return _fail_post(posted_id, claim_token, echo["id"], "Mastodon delivery failed")

    ok = _update_post(posted_id, claim_token, "success")
    if ok:
        record_success(echo["id"])
    return ok


def _send_email_echo(
    echo,
    item: dict,
    content: str,
    email_account_id: int,
    posted_id: int,
    claim_token: str,
) -> bool:
    # Check if this echo is in digest mode — if so, queue for later sending
    try:
        delivery_mode = echo["delivery_mode"]
    except (KeyError, IndexError):
        delivery_mode = "instant"

    if delivery_mode == "digest":
        return _queue_for_digest(echo, item, content, posted_id, claim_token)

    # Instant mode: send immediately
    with get_db() as db:
        account = db.execute(
            "SELECT * FROM email_accounts WHERE id = ?",
            (email_account_id,),
        ).fetchone()

    if not account:
        return _fail_post(
            posted_id,
            claim_token,
            echo["id"],
            f"Email account {email_account_id} not found",
        )

    try:
        send_email(
            to_email=account["email"],
            subject=truncate(
                item.get("title") or item.get("link") or "FeedEcho: New Post",
                200,
            ),
            body=content,
            user_id=echo["user_id"],
        )
    except Exception:
        logger.exception("Echo %s: email delivery failed", echo["id"])
        return _fail_post(posted_id, claim_token, echo["id"], "Email delivery failed")

    ok = _update_post(posted_id, claim_token, "success")
    if ok:
        record_success(echo["id"])
    return ok


def _bsky_session(account) -> dict:
    """Return a usable Bluesky session, caching JWTs in the account row.

    Resolves the account's PDS when unknown, reuses a cached access JWT while
    unexpired, refreshes via the refresh JWT when possible, and falls back to
    a fresh app-password login otherwise. All network I/O happens with no DB
    connection held; only the short cache write opens one.
    """
    handle = account["handle"]
    app_password = account["app_password"]
    pds = (account["pds"] or "").strip()
    did = (account["did"] or "").strip()
    access_jwt = (account["access_jwt"] or "").strip()
    refresh_jwt = (account["refresh_jwt"] or "").strip()
    now = _now()

    resolved_did = None
    if not pds:
        resolved_did, pds = resolve_pds(handle)

    if access_jwt:
        expires_at = account["session_expires_at"] or ""
        if expires_at and expires_at > now:
            # Persist a late PDS/DID resolution without disturbing the session.
            if resolved_did:
                with get_db() as db:
                    db.execute(
                        "UPDATE bluesky_accounts SET did = ?, pds = ? WHERE id = ?",
                        (resolved_did, pds, account["id"]),
                    )
            return {
                "pds": pds,
                "did": did or resolved_did or "",
                "access_jwt": access_jwt,
            }

    session = None
    if refresh_jwt:
        try:
            session = refresh_session(pds, refresh_jwt)
        except BlueskyError:
            # Auth or network failure — fall through to a fresh login.
            session = None
    if session is None:
        session = create_session(pds, handle, app_password)

    with get_db() as db:
        db.execute(
            """
            UPDATE bluesky_accounts
               SET did = ?, pds = ?, access_jwt = ?, refresh_jwt = ?,
                   session_expires_at = ?
             WHERE id = ?
            """,
            (
                session["did"],
                pds,
                session["access_jwt"],
                session["refresh_jwt"],
                session_expiry(session["access_jwt"]),
                account["id"],
            ),
        )
    return {
        "pds": pds,
        "did": session["did"],
        "access_jwt": session["access_jwt"],
    }


def _send_bluesky(
    echo,
    item: dict,
    content: str,
    account_id: int,
    posted_id: int,
    claim_token: str,
) -> bool:
    with get_db() as db:
        account = db.execute(
            "SELECT * FROM bluesky_accounts WHERE id = ?",
            (account_id,),
        ).fetchone()

    if not account:
        return _fail_post(
            posted_id,
            claim_token,
            echo["id"],
            f"Bluesky account {account_id} not found",
            permanent=True,
        )

    # Content preparation is pure string work, but a bug here must not strand
    # the claimed row — finalize it as failed so the bounded retry owns it.
    try:
        text = truncate_graphemes(content or "", BLUESKY_MAX_GRAPHEMES)
        facets = build_facets(text)
    except Exception:
        logger.exception("Echo %s: Bluesky content preparation failed", echo["id"])
        return _fail_post(
            posted_id, claim_token, echo["id"], "Content preparation failed"
        )

    try:
        session = _bsky_session(account)
    except Exception:
        logger.exception("Echo %s: Bluesky session failed", echo["id"])
        return _fail_post(posted_id, claim_token, echo["id"], "Bluesky session failed")

    # Image attachment: optional, single image, with AI alt text when enabled.
    # Any failure in the pipeline degrades to a text-only post.
    try:
        attach_image = bool(echo["attach_image"])
    except (KeyError, IndexError):
        attach_image = False

    image_blob = None
    alt_description = ""
    try:
        if attach_image:
            image_url = item.get("image_url", "")
            if image_url:
                image_result = fetch_image(image_url)
                if image_result:
                    img_bytes, img_type = image_result
                    if img_type in BLUESKY_IMAGE_TYPES and len(img_bytes) <= MAX_BLOB_BYTES:
                        if alt_text.is_enabled(user_id=echo["user_id"]):
                            try:
                                alt_description = (
                                    alt_text.generate_alt_text(
                                        img_bytes, img_type, user_id=echo["user_id"]
                                    ) or ""
                                )
                            except Exception:
                                logger.warning(
                                    "Echo %s: alt text generation failed for item %s",
                                    echo["id"],
                                    item["id"],
                                    exc_info=True,
                                )
                        blob = upload_blob(
                            pds=session["pds"],
                            access_jwt=session["access_jwt"],
                            image_bytes=img_bytes,
                            content_type=img_type,
                        )
                        if blob:
                            image_blob = blob
                            logger.info(
                                "Echo %s: uploaded Bluesky image blob for item %s",
                                echo["id"],
                                item["id"],
                            )
                        else:
                            logger.warning(
                                "Echo %s: Bluesky image upload failed for item %s, posting text-only",
                                echo["id"],
                                item["id"],
                            )
                    else:
                        logger.info(
                            "Echo %s: image unsupported for Bluesky (type=%s, size=%d), posting text-only",
                            echo["id"],
                            img_type,
                            len(img_bytes),
                        )
                else:
                    logger.info(
                        "Echo %s: image fetch failed for item %s, posting text-only",
                        echo["id"],
                        item["id"],
                    )
    except Exception:
        logger.warning(
            "Echo %s: Bluesky image pipeline failed for item %s, posting text-only",
            echo["id"],
            item["id"],
            exc_info=True,
        )
        image_blob = None
        alt_description = ""

    embed = build_image_embed(image_blob, alt_description) if image_blob else None

    # Re-validate claim ownership immediately before the post: if the lease
    # lapsed and another worker reclaimed this row, posting would duplicate.
    with get_db() as db:
        owned = db.execute(
            """
            SELECT 1 FROM posted_items
             WHERE id = ? AND status = 'pending' AND claim_token = ?
            """,
            (posted_id, claim_token),
        ).fetchone()
    if not owned:
        logger.warning(
            "Echo %s: claim lost before Bluesky dispatch; skipping item %s",
            echo["id"],
            item["id"],
        )
        return False

    def _do_post(access_jwt: str, repo: str) -> dict:
        return create_post(
            pds=session["pds"],
            access_jwt=access_jwt,
            repo=repo,
            text=text,
            facets=facets or None,
            embed=embed,
        )

    try:
        result = _do_post(session["access_jwt"], session["did"])
    except BlueskyAuthError:
        # Token was rejected (expired or revoked mid-flight). Re-authenticate
        # once with the app password and retry the same payload.
        logger.warning(
            "Echo %s: Bluesky auth rejected for account %s, re-authenticating",
            echo["id"],
            account["handle"],
        )
        try:
            with get_db() as db:
                fresh = db.execute(
                    "SELECT handle, app_password FROM bluesky_accounts WHERE id = ?",
                    (account["id"],),
                ).fetchone()
            if not fresh:
                raise BlueskyError("Bluesky account was deleted during dispatch")
            refreshed = create_session(
                session["pds"], fresh["handle"], fresh["app_password"]
            )
            with get_db() as db:
                db.execute(
                    """
                    UPDATE bluesky_accounts
                       SET did = ?, access_jwt = ?, refresh_jwt = ?,
                           session_expires_at = ?
                     WHERE id = ?
                    """,
                    (
                        refreshed["did"],
                        refreshed["access_jwt"],
                        refreshed["refresh_jwt"],
                        session_expiry(refreshed["access_jwt"]),
                        account["id"],
                    ),
                )
            result = _do_post(refreshed["access_jwt"], refreshed["did"])
        except BlueskyAuthError:
            # Credentials themselves are bad/revoked — retries cannot help.
            logger.error(
                "Echo %s: Bluesky credentials rejected after re-auth; giving up",
                echo["id"],
            )
            return _fail_post(
                posted_id,
                claim_token,
                echo["id"],
                "Bluesky credentials rejected",
                permanent=True,
            )
        except Exception:
            logger.exception("Echo %s: Bluesky post failed after re-auth", echo["id"])
            return _fail_post(
                posted_id, claim_token, echo["id"], "Bluesky delivery failed"
            )
    except Exception:
        logger.exception("Echo %s: Bluesky post failed", echo["id"])
        return _fail_post(posted_id, claim_token, echo["id"], "Bluesky delivery failed")

    # Persist the post URL for auditing/duplicate detection.
    post_url = ""
    uri = result.get("uri", "")
    if uri:
        rkey = uri.rsplit("/", 1)[-1]
        post_url = f"https://bsky.app/profile/{session['did']}/post/{rkey}"

    ok = _update_post(posted_id, claim_token, "success", post_url=post_url)
    if ok:
        record_success(echo["id"])
    return ok


def _queue_for_digest(
    echo,
    item: dict,
    content: str,
    posted_id: int,
    claim_token: str,
) -> bool:
    """Queue an item for digest delivery instead of sending immediately.

    Inserts into digest_items and marks the posted_item as 'queued' so the
    cursor advances and the item isn't retried. The digest flush job sends
    all queued items for this echo as one email.
    """
    echo_id = echo["id"]
    item_id = item["id"]

    with get_db() as db:
        db.execute(
            """INSERT INTO digest_items (echo_id, item_id, item_title, item_url, rendered_content)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(echo_id, item_id) DO NOTHING""",
            (echo_id, item_id, item.get("title", ""), item.get("link", ""), content),
        )

    # Mark the posted_item as 'queued' — the digest job will finalize it
    ok = _update_post(posted_id, claim_token, "queued")
    if ok:
        record_success(echo_id)
        logger.info(
            "Echo %s: item %s queued for digest delivery",
            echo_id,
            item_id,
        )
    return ok


def _requeue_drip_failure(
    echo_id: int,
    item_id: str,
    item_json: str,
    attempts: int,
    posted_id: int,
) -> None:
    """Return a failed drip release to the queue, or gave_up at the cap.

    Retries run from the stored payload, so items that have rotated out
    of the feed are never lost the way a feed-based retry would lose
    them. Release attempts are counted on the drip_items row.
    """
    attempts += 1
    cap = max_attempts(echo_id)
    if cap > 0 and attempts >= cap:
        with get_db() as db:
            db.execute(
                """UPDATE posted_items
                      SET status = 'gave_up',
                          error_message = ?,
                          claimed_at = NULL,
                          claim_token = NULL,
                          posted_at = CURRENT_TIMESTAMP
                    WHERE id = ?""",
                (f"Drip release gave up after {attempts} attempts", posted_id),
            )
        record_failure(echo_id)
        logger.error(
            "Echo %s: drip release gave up for item %s after %s attempts",
            echo_id,
            item_id,
            attempts,
        )
        return

    with get_db() as db:
        result = db.execute(
            """UPDATE posted_items
                  SET status = 'queued',
                      claimed_at = NULL,
                      claim_token = NULL,
                      next_retry_at = NULL,
                      posted_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'failed'""",
            (posted_id,),
        )
        if result.rowcount != 1:
            # The row moved on (e.g. claimed by the retry sweep); the
            # normal path owns it now.
            return
        db.execute(
            """INSERT INTO drip_items (echo_id, item_id, item_json, attempts)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(echo_id, item_id) DO UPDATE SET attempts = excluded.attempts""",
            (echo_id, item_id, item_json, attempts),
        )
    logger.info(
        "Echo %s: dripped item %s release failed; re-queued (attempt %d)",
        echo_id,
        item_id,
        attempts,
    )


def _discard_drip_backlog(echo_id: int, reason: str) -> None:
    """Finalize an echo's queued drip rows as gave_up and clear the queue.

    A disabled or deleted echo must not hold items indefinitely: on
    re-enable they would dump as stale posts, and the queue cap would
    keep silently dropping new items meanwhile. History records what was
    discarded and why.
    """
    with get_db() as db:
        db.execute(
            """UPDATE posted_items
                  SET status = 'gave_up',
                      error_message = ?,
                      claimed_at = NULL,
                      claim_token = NULL,
                      posted_at = CURRENT_TIMESTAMP
                WHERE echo_id = ? AND status = 'queued'""",
            (reason, echo_id),
        )
        db.execute("DELETE FROM drip_items WHERE echo_id = ?", (echo_id,))
    logger.info("Echo %s: %s", echo_id, reason)


def flush_drips() -> None:
    """Release queued drip items whose rate window has room.

    Called by the drip scheduler job every 10 minutes. Each held item is
    re-rendered with the echo's current template and dispatched through
    the normal per-destination senders, so images, alt text, and content
    warnings behave exactly like instant posts. Failed releases return to
    the queue and retry from the stored payload. Disabled or deleted
    echoes have their backlog discarded as gave_up. Removing the limit
    entirely drains the queue in bounded batches per flush.
    """
    with get_db() as db:
        pending = db.execute(
            """
            SELECT d.id AS drip_id, d.item_id, d.item_json, d.attempts, e.*,
                   f.name AS feed_name, f.deleted_at AS feed_deleted_at
              FROM drip_items d
              JOIN echoes e ON d.echo_id = e.id
              JOIN feeds f ON e.feed_id = f.id
             ORDER BY d.id ASC
            """
        ).fetchall()

    discarded: set[int] = set()
    released: dict[int, int] = {}

    for row in pending:
        echo_id = row["id"]
        if echo_id in discarded:
            continue

        if row["deleted_at"] or row["feed_deleted_at"]:
            _discard_drip_backlog(echo_id, "Echo deleted; drip backlog discarded")
            discarded.add(echo_id)
            continue
        if not row["enabled"]:
            _discard_drip_backlog(echo_id, "Echo disabled; drip backlog discarded")
            discarded.add(echo_id)
            continue

        # Serialize with process_echo so concurrent workers cannot both
        # see an open window and exceed the hourly cap.
        with _drip_lock(echo_id):
            limit = _drip_limit(row)
            if limit > 0 and _drip_rate(echo_id) >= limit:
                continue
            if limit <= 0 and released.get(echo_id, 0) >= DRIP_DOWNGRADE_BATCH:
                # Limit removed: drain the stale backlog in batches
                # instead of one burst.
                continue

            try:
                item = json.loads(row["item_json"])
            except (json.JSONDecodeError, TypeError):
                logger.warning(
                    "Drip queue: unreadable payload %s for echo %s; dropping",
                    row["drip_id"],
                    echo_id,
                )
                with get_db() as db:
                    db.execute("DELETE FROM drip_items WHERE id = ?", (row["drip_id"],))
                continue

            claimed = _reclaim_queued(echo_id, row["item_id"])
            if claimed is None:
                # The posted row is no longer queued; remove the orphaned
                # queue entry so it is not reconsidered forever.
                with get_db() as db:
                    db.execute("DELETE FROM drip_items WHERE id = ?", (row["drip_id"],))
                continue

            posted_id, claim_token = claimed
            with get_db() as db:
                db.execute("DELETE FROM drip_items WHERE id = ?", (row["drip_id"],))

            logger.info(
                "Echo %s: releasing dripped item %s (%d in window)",
                echo_id,
                row["item_id"],
                _drip_rate(echo_id),
            )
            _render_and_dispatch(dict(row), item, row["feed_name"] or "", posted_id, claim_token)

            if _row_state(echo_id, row["item_id"]) == "failed":
                _requeue_drip_failure(
                    echo_id,
                    row["item_id"],
                    row["item_json"],
                    row["attempts"],
                    posted_id,
                )
            else:
                released[echo_id] = released.get(echo_id, 0) + 1


def flush_digests() -> None:
    """Send pending digest items as batched emails and finalize them.

    Called by the digest scheduler job at the configured interval.
    Groups digest_items by echo, sends one email per echo, then
    deletes the items from digest_items and updates posted_items
    from 'queued' to 'success'.
    """
    with get_db() as db:
        # Find all echoes that have pending digest items
        pending_echoes = db.execute("""
            SELECT DISTINCT d.echo_id, e.destination_id, e.feed_id, e.user_id,
                   f.name as feed_name, ea.email as to_email
              FROM digest_items d
              JOIN echoes e ON d.echo_id = e.id
              JOIN feeds f ON e.feed_id = f.id
              JOIN email_accounts ea ON e.destination_id = ea.id
             WHERE e.destination_type = 'email'
               AND e.delivery_mode = 'digest'
               AND e.enabled = 1
               AND f.deleted_at IS NULL
               AND e.deleted_at IS NULL
        """).fetchall()

    if not pending_echoes:
        return

    for echo_row in pending_echoes:
        echo_id = echo_row["echo_id"]
        with get_db() as db:
            items = db.execute(
                "SELECT * FROM digest_items WHERE echo_id = ? ORDER BY created_at ASC",
                (echo_id,),
            ).fetchall()

        if not items:
            continue

        # Build digest body
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        subject = f"FeedEcho Digest — {echo_row['feed_name']} — {date_str}"

        body_parts = [f"FeedEcho Digest for {echo_row['feed_name']}", f"Date: {date_str}", ""]

        for i, item in enumerate(items, 1):
            title = item["item_title"] or item["item_url"] or f"Item {i}"
            body_parts.append(f"{i}. {title}")
            body_parts.append(f"   {item['rendered_content']}")
            body_parts.append("")

        # Truncate overly long digests
        body = "\n".join(body_parts)
        if len(body) > 10000:  # reasonable email size cap
            body = body[:9950] + "\n\n... (truncated)"

        try:
            send_email(
                to_email=echo_row["to_email"],
                subject=subject,
                body=body,
                user_id=echo_row["user_id"],
            )
        except Exception:
            logger.exception(
                "Digest flush failed for echo %s (%s, %d items)",
                echo_id,
                echo_row["feed_name"],
                len(items),
            )
            # Don't mark as sent — leave items in digest_items for next run
            continue

        # Success — delete only the sent items and update posted_items to 'success'
        sent_item_ids = [item["item_id"] for item in items]
        with get_db() as db:
            for item in items:
                # Update posted_items from 'queued' to 'success'
                db.execute(
                    """UPDATE posted_items SET status = 'success', posted_at = CURRENT_TIMESTAMP
                       WHERE echo_id = ? AND item_id = ? AND status = 'queued'""",
                    (echo_id, item["item_id"]),
                )
            # Delete only the sent items, not any that may have arrived concurrently
            placeholders = ",".join("?" for _ in sent_item_ids)
            db.execute(
                f"DELETE FROM digest_items WHERE echo_id = ? AND item_id IN ({placeholders})",
                (echo_id, *sent_item_ids),
            )

        record_success(echo_id)
        logger.info(
            "Digest flushed for echo %s (%s): %d items sent",
            echo_id,
            echo_row["feed_name"],
            len(items),
        )


def check_all_feeds() -> None:
    now = datetime.now(timezone.utc)
    with get_db() as db:
        feeds = db.execute(
            "SELECT id, name, poll_interval, last_fetched FROM feeds WHERE deleted_at IS NULL"
        ).fetchall()

    due = []
    for feed in feeds:
        if not feed["last_fetched"]:
            due.append(feed)
            continue
        try:
            ts = feed["last_fetched"].replace("T", " ")[:19]
            last = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
        except (ValueError, TypeError):
            # Malformed timestamp: treat as due rather than skipping forever
            due.append(feed)
            continue
        # poll_interval is clamped to >=1 at creation; a malformed or NULL
        # value can only come from direct DB writes, so degrade to the
        # default instead of crashing the whole run or hot-looping the feed.
        try:
            interval = max(1, int(feed["poll_interval"] or 15))
        except (ValueError, TypeError):
            interval = 15
        if last <= now - timedelta(minutes=interval):
            due.append(feed)

    for feed in due:
        try:
            check_feed(feed["id"])
        except Exception:
            logger.exception("Error checking feed %s (%s)", feed["id"], feed["name"])


def start_scheduler() -> None:
    global scheduler
    if scheduler is not None:
        return

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        check_all_feeds,
        trigger=IntervalTrigger(minutes=2),
        id="check_all_feeds",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    scheduler.add_job(check_all_feeds, "date", id="startup_check", replace_existing=True)

    # Digest flush job — runs hourly, checks for pending digest items
    scheduler.add_job(
        flush_digests,
        trigger=IntervalTrigger(hours=1),
        id="flush_digests",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    # Run a digest flush shortly after startup
    scheduler.add_job(flush_digests, "date", id="startup_digest_flush", replace_existing=True)

    # Drip flush job — releases queued items as their rate windows allow
    scheduler.add_job(
        flush_drips,
        trigger=IntervalTrigger(minutes=10),
        id="flush_drips",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    # Run a drip flush shortly after startup
    scheduler.add_job(flush_drips, "date", id="startup_drip_flush", replace_existing=True)


def stop_scheduler() -> None:
    global scheduler
    if scheduler is not None:
        scheduler.shutdown(wait=False)
        scheduler = None