"""Portable import/export of feeds, accounts, and echoes.

The export is a versioned JSON document, not a SQL dump: a single-mode SQLite
instance and the hosted Postgres service speak the same format, so migration
runs in both directions. Credentials are included on purpose — an export that
omitted access tokens would be useless for moving an account — so the routes
that produce/consume this document must gate on authentication and warn the
operator that the file contains secrets.

Record identity on import is by natural key (feed url; account email / handle /
uid / homeserver+room / webhook url / instance+username), so re-importing the
same document is idempotent rather than duplicating. ``echoes`` reference feeds
and accounts by their source ``id``; import rebuilds ``{old_id: new_id}`` maps
and rewrites those references so an imported echo points at the freshly
inserted feed/account, never at a stale id. Echoes are themselves deduplicated
on ``(feed_id, destination_type, destination_id)``.

Quotas are enforced on import in multi mode only (``settings.MULTI``): the
number of *new* rows an import would add — after dedup against what already
exists — is checked against the plan's ``max_feeds`` / ``max_destinations``
caps, and ``poll_interval`` / ``drip_limit`` are clamped to the plan's floor
and ceiling. Single mode never consults any of this.
"""

from __future__ import annotations

from datetime import datetime, timezone

import plans
import settings
from _version import __version__ as APP_VERSION
from security import decrypt_secret, encrypt_secret, hash_secret

FORMAT = "feedecho-export"
VERSION = 1

# (export section key, table, insert columns, natural-key columns for dedup).
# Column order is the insert order; `id` and `user_id` are appended by the
# importer, and `created_at` is deliberately dropped so the target instance
# stamps its own.
_ACCOUNTS = [
    ("mastodon", "accounts", ["name", "username", "instance", "access_token"], ("instance", "username")),
    ("email", "email_accounts", ["name", "email"], ("email",)),
    ("bluesky", "bluesky_accounts", [
        "name", "handle", "app_password", "did", "pds",
        "access_jwt", "refresh_jwt", "session_expires_at",
    ], ("handle",)),
    ("microblog", "microblog_accounts", ["name", "uid", "token"], ("uid",)),
    ("matrix", "matrix_accounts", [
        "name", "homeserver", "base_url", "access_token",
        "matrix_user_id", "room_id", "room_alias",
    ], ("homeserver", "room_id")),
    ("discord", "discord_accounts", ["name", "webhook_url", "channel_id"], ("webhook_url",)),
    ("webhook", "webhook_accounts", ["name", "url", "headers"], ("url",)),
]

ACCOUNT_TYPES = tuple(section for section, *_ in _ACCOUNTS)
ACCOUNT_TABLES = {section: table for section, table, _, _ in _ACCOUNTS}
_KEY_COLS = {section: keys for section, _, _, keys in _ACCOUNTS}

# Columns that hold third-party credentials and are encrypted at rest in multi
# mode. Export decrypts them (the export document carries plaintext, matching
# the "credentials included by design" contract); import re-encrypts them.
_CREDENTIAL_COLS = {
    "mastodon": {"access_token"},
    "bluesky": {"app_password", "access_jwt", "refresh_jwt"},
    "microblog": {"token"},
    "matrix": {"access_token"},
    "discord": {"webhook_url"},
}

_FEED_COLS = ["name", "url", "feed_type", "poll_interval", "last_item_id", "paused", "read_enabled"]

_ECHO_COLS = [
    "feed_id", "destination_type", "destination_id", "template", "visibility",
    "enabled", "filter_keywords", "filter_mode", "content_warning",
    "attach_image", "delivery_mode", "drip_limit",
]

_VALID_VISIBILITY = ("public", "unlisted", "private", "direct")
_VALID_FILTER_MODES = ("exclude", "include")
_VALID_DELIVERY_MODES = ("instant", "digest")


class ExportError(ValueError):
    """The import document is malformed or would exceed plan limits."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jsonable(value):
    """Normalize a DB value to a JSON-serializable Python value.

    PG reads TIMESTAMP columns back as ``datetime``; sqlite as ``str``.
    ``json.dumps`` cannot serialize a ``datetime``, so normalize to the same
    ``YYYY-MM-DD HH:MM:SS`` UTC string the app stores everywhere else.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return value


def _rows_to_dicts(rows) -> list[dict]:
    return [{k: _jsonable(v) for k, v in dict(r).items()} for r in rows]


def build_export(db, uid: int) -> dict:
    """Read one user's feeds, accounts and echoes into the export document."""
    feeds = _rows_to_dicts(db.execute(
        f"SELECT id, {', '.join(_FEED_COLS)} FROM feeds"
        " WHERE user_id = ? AND deleted_at IS NULL ORDER BY id",
        (uid,),
    ).fetchall())

    accounts = {}
    for section, table, cols, _ in _ACCOUNTS:
        rows = _rows_to_dicts(db.execute(
            f"SELECT id, {', '.join(cols)} FROM {table}"
            " WHERE user_id = ? ORDER BY id",
            (uid,),
        ).fetchall())
        for row in rows:
            for col in _CREDENTIAL_COLS.get(section, frozenset()):
                if row.get(col):
                    row[col] = decrypt_secret(row[col])
        accounts[section] = rows

    # Echoes reference feeds by feed_id; only emit echoes whose feed survives
    # the export (a soft-deleted feed is not exported, and an echo pointing at
    # it would otherwise import with a dangling feed_id).
    echoes = _rows_to_dicts(db.execute(
        f"SELECT id, {', '.join(_ECHO_COLS)} FROM echoes"
        " WHERE user_id = ? AND deleted_at IS NULL"
        " AND feed_id IN (SELECT id FROM feeds WHERE user_id = ? AND deleted_at IS NULL)"
        " ORDER BY id",
        (uid, uid),
    ).fetchall())

    return {
        "format": FORMAT,
        "version": VERSION,
        "exported_at": _now_iso(),
        "app_version": APP_VERSION,
        "feeds": feeds,
        "accounts": accounts,
        "echoes": echoes,
    }


def _validate_payload(payload) -> dict:
    """Validate the document envelope and normalize its sections.

    Returns a dict with ``feeds``/``echoes`` as lists and ``accounts`` as a
    dict keyed by account type whose values are always lists. Raises
    ``ExportError`` (never a bare KeyError/TypeError) on anything malformed,
    so the import route renders a clean error instead of a 500.
    """
    if not isinstance(payload, dict):
        raise ExportError("Import file is not a valid FeedEcho export.")
    if payload.get("format") != FORMAT:
        raise ExportError("This file is not a FeedEcho export.")
    version = payload.get("version")
    if version != VERSION:
        if isinstance(version, int) and version > VERSION:
            raise ExportError(
                f"This export was created by a newer FeedEcho (version {version}). "
                "Upgrade FeedEcho to import it."
            )
        raise ExportError(f"Unsupported export version: {version!r}.")

    feeds = payload.get("feeds") or []
    accounts_raw = payload.get("accounts") or {}
    echoes = payload.get("echoes") or []
    if (
        not isinstance(feeds, list)
        or not isinstance(accounts_raw, dict)
        or not isinstance(echoes, list)
    ):
        raise ExportError("Import file has an unexpected structure.")

    for feed in feeds:
        if not isinstance(feed, dict) or "id" not in feed:
            raise ExportError("Feed record is malformed (missing id).")

    accounts: dict[str, list] = {}
    for section in ACCOUNT_TYPES:
        section_val = accounts_raw.get(section) or []
        if not isinstance(section_val, list):
            raise ExportError("Import file has an unexpected structure.")
        for account in section_val:
            if not isinstance(account, dict) or "id" not in account:
                raise ExportError("Account record is malformed (missing id).")
        accounts[section] = section_val

    for echo in echoes:
        if not isinstance(echo, dict):
            raise ExportError("Import file has an unexpected structure.")

    return {"feeds": feeds, "accounts": accounts, "echoes": echoes}


def _try_int(value):
    """``int(value)`` or None — never raises."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _record_id(record: dict, label: str) -> int:
    value = _try_int(record.get("id"))
    if value is None:
        raise ExportError(f"{label} record is malformed (invalid id).")
    return value


def _user_plan(db, uid: int) -> str:
    row = db.execute("SELECT plan FROM users WHERE id = ?", (uid,)).fetchone()
    return (row["plan"] if row else None) or "trial"


def _account_key(section: str, account: dict) -> tuple:
    """The natural-key values for an account, for dedup lookups.

    Mastodon's stored ``username`` mirrors the create path (``username or
    name``), so an export that somehow carries an empty username still
    dedups by the display name.
    """
    values = []
    for col in _KEY_COLS[section]:
        value = account.get(col)
        if col == "username" and not value:
            value = account.get("name")
        values.append(str(value or "").strip())
    return tuple(values)


def _normalize_account(section: str, account: dict) -> None:
    """Normalize an imported account dict in place before dedup/insert.

    Keeps the dedup natural key and the stored row in agreement: string
    fields are stripped (so ``url`` with surrounding whitespace still matches
    an existing row), and Mastodon's ``username`` falls back to ``name`` the
    same way the create handler does.
    """
    for key, value in list(account.items()):
        if isinstance(value, str):
            account[key] = value.strip()
    if section == "mastodon" and not account.get("username"):
        account["username"] = account.get("name") or ""


def _existing_account_id(db, uid: int, section: str, account: dict):
    table = ACCOUNT_TABLES[section]
    key_cols = _KEY_COLS[section]
    # A credential that is also a natural key (Discord webhook_url) is stored
    # encrypted, so equality can't be a SQL match — decrypt-and-compare. A user
    # has at most a handful of accounts, so this is cheap.
    if _CREDENTIAL_COLS.get(section, frozenset()) & set(key_cols):
        want = _account_key(section, account)
        rows = db.execute(
            f"SELECT id, {', '.join(key_cols)} FROM {table} WHERE user_id = ?",
            (uid,),
        ).fetchall()
        for row in rows:
            got = tuple(
                str(decrypt_secret(row[c] or "")).strip() for c in key_cols
            )
            if got == want:
                return row
        return None
    key_vals = _account_key(section, account)
    clause = " AND ".join(f"{c} = ?" for c in key_cols)
    return db.execute(
        f"SELECT id FROM {table} WHERE user_id = ? AND {clause}",
        (uid, *key_vals),
    ).fetchone()


def _destination_count(db, uid: int) -> int:
    return sum(
        db.execute(
            f"SELECT COUNT(*) AS c FROM {table} WHERE user_id = ?", (uid,)
        ).fetchone()["c"]
        for table in ACCOUNT_TABLES.values()
    )


def _enforce_quotas(db, uid: int, new_feeds: int, new_destinations: int) -> None:
    """Reject the import up front if it would exceed the plan's caps.

    Counts NEW rows only: re-importing data that already exists never trips
    the cap (the same "cap counts new rows" rule the create handlers use).
    """
    plan = _user_plan(db, uid)

    feed_cap = plans.limit_for(plan, "max_feeds")
    if feed_cap and new_feeds:
        current = db.execute(
            "SELECT COUNT(*) AS c FROM feeds WHERE user_id = ? AND deleted_at IS NULL",
            (uid,),
        ).fetchone()["c"]
        if current + new_feeds > feed_cap:
            raise ExportError(
                f"Import would add {new_feeds} feed(s), exceeding your plan's "
                f"{feed_cap}-feed limit ({current} already present). Upgrade or "
                "remove feeds to import them all."
            )

    dest_cap = plans.limit_for(plan, "max_destinations")
    if dest_cap and new_destinations:
        current = _destination_count(db, uid)
        if current + new_destinations > dest_cap:
            raise ExportError(
                f"Import would add {new_destinations} connected account(s), "
                f"exceeding your plan's {dest_cap}-account limit ({current} already "
                "present). Upgrade or disconnect accounts to import them all."
            )


def _clamp_poll(db, uid: int, poll_interval) -> int:
    poll = max(1, min(_try_int(poll_interval) or 15, 1440))
    if settings.MULTI:
        poll = plans.clamp_poll_interval(poll, _user_plan(db, uid))
    return poll


def _clamp_drip(db, uid: int, drip_limit) -> int:
    drip = max(0, min(_try_int(drip_limit) or 0, 1000))
    if settings.MULTI:
        drip = plans.clamp_drip_limit(drip, _user_plan(db, uid))
    return drip


def import_data(db, uid: int, payload: dict) -> dict:
    """Import an export document for one user, returning a summary.

    Raises ``ExportError`` on a malformed document or a quota overrun, before
    any row is written, so the caller's transaction rolls back cleanly.
    """
    doc = _validate_payload(payload)

    # ── Dedup + quota pre-check (read-only) ─────────────────────────────────
    # First occurrence of each natural key wins; later occurrences (within the
    # same document) map to the same new id, so a file that itself contains a
    # duplicate key can never split an echo across two rows.
    feed_map: dict[int, int] = {}
    first_feed: dict[str, dict] = {}
    feed_oldids: dict[str, list[int]] = {}
    new_feed_urls: list[str] = []
    for feed in doc["feeds"]:
        old_id = _record_id(feed, "Feed")
        url = str(feed.get("url") or "").strip()
        if not url:
            raise ExportError("Feed is missing a URL.")
        existing = db.execute(
            "SELECT id FROM feeds WHERE user_id = ? AND url = ? AND deleted_at IS NULL",
            (uid, url),
        ).fetchone()
        if existing:
            feed_map[old_id] = existing["id"]
            continue
        if url not in first_feed:
            first_feed[url] = feed
            feed_oldids[url] = []
            new_feed_urls.append(url)
        feed_oldids[url].append(old_id)

    account_maps: dict[str, dict[int, int]] = {s: {} for s in ACCOUNT_TYPES}
    first_account: dict[str, dict] = {s: {} for s in ACCOUNT_TYPES}
    account_oldids: dict[str, dict] = {s: {} for s in ACCOUNT_TYPES}
    new_account_keys: dict[str, list] = {s: [] for s in ACCOUNT_TYPES}
    for section in ACCOUNT_TYPES:
        for account in doc["accounts"][section]:
            old_id = _record_id(account, "Account")
            _normalize_account(section, account)
            existing = _existing_account_id(db, uid, section, account)
            if existing:
                account_maps[section][old_id] = existing["id"]
                continue
            key = _account_key(section, account)
            if key not in first_account[section]:
                first_account[section][key] = account
                account_oldids[section][key] = []
                new_account_keys[section].append(key)
            account_oldids[section][key].append(old_id)

    if settings.MULTI:
        _enforce_quotas(
            db,
            uid,
            len(new_feed_urls),
            sum(len(v) for v in new_account_keys.values()),
        )

    # The reader is a paid capability in multi mode, so a feed's read_enabled
    # flag must not be imported by a plan that doesn't include it (single mode
    # is always allowed). Same clamp-on-import philosophy as poll/drip below.
    reader_allowed = not settings.MULTI or plans.reader_enabled(_user_plan(db, uid))

    # ── Insert feeds ────────────────────────────────────────────────────────
    for url in new_feed_urls:
        feed = first_feed[url]
        poll = _clamp_poll(db, uid, feed.get("poll_interval"))
        row = db.execute(
            "INSERT INTO feeds (name, url, feed_type, poll_interval, last_item_id,"
            " paused, read_enabled, user_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (
                str(feed.get("name") or "").strip(),
                url,
                str(feed.get("feed_type") or "rss"),
                poll,
                feed.get("last_item_id"),
                1 if feed.get("paused") else 0,
                1 if (feed.get("read_enabled") and reader_allowed) else 0,
                uid,
            ),
        ).fetchone()
        for old_id in feed_oldids[url]:
            feed_map[old_id] = row["id"]

    # ── Insert accounts ─────────────────────────────────────────────────────
    for section, table, cols, _ in _ACCOUNTS:
        for key in new_account_keys[section]:
            account = first_account[section][key]
            # Encrypt credentials at rest (multi mode); no-op in single mode.
            for col in _CREDENTIAL_COLS.get(section, frozenset()):
                if account.get(col):
                    account[col] = encrypt_secret(account[col])
            values = [account.get(col) for col in cols]
            placeholders = ", ".join("?" for _ in cols)
            column_list = ", ".join(cols)
            row = db.execute(
                f"INSERT INTO {table} ({column_list}, user_id)"
                f" VALUES ({placeholders}, ?) RETURNING id",
                (*values, uid),
            ).fetchone()
            for old_id in account_oldids[section][key]:
                account_maps[section][old_id] = row["id"]

    # ── Insert echoes (dedup on feed+destination) ───────────────────────────
    added_echoes = 0
    existing_echoes = 0
    skipped_echoes = 0
    for echo in doc["echoes"]:
        dest_type = echo.get("destination_type")
        if dest_type not in ACCOUNT_TYPES:
            skipped_echoes += 1
            continue
        feed_old = _try_int(echo.get("feed_id"))
        dest_old = _try_int(echo.get("destination_id"))
        if (
            feed_old is None
            or dest_old is None
            or feed_old not in feed_map
            or dest_old not in account_maps[dest_type]
        ):
            skipped_echoes += 1
            continue
        new_feed = feed_map[feed_old]
        new_dest = account_maps[dest_type][dest_old]
        existing = db.execute(
            "SELECT id FROM echoes WHERE user_id = ? AND feed_id = ?"
            " AND destination_type = ? AND destination_id = ? AND deleted_at IS NULL",
            (uid, new_feed, dest_type, new_dest),
        ).fetchone()
        if existing:
            existing_echoes += 1
            continue

        drip = _clamp_drip(db, uid, echo.get("drip_limit"))
        visibility = echo.get("visibility") or "public"
        if visibility not in _VALID_VISIBILITY:
            visibility = "public"
        filter_mode = echo.get("filter_mode") or "exclude"
        if filter_mode not in _VALID_FILTER_MODES:
            filter_mode = "exclude"
        delivery_mode = echo.get("delivery_mode") or "instant"
        if delivery_mode not in _VALID_DELIVERY_MODES:
            delivery_mode = "instant"
        db.execute(
            "INSERT INTO echoes (feed_id, destination_type, destination_id, template,"
            " visibility, enabled, filter_keywords, filter_mode, content_warning,"
            " attach_image, delivery_mode, drip_limit, user_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new_feed,
                dest_type,
                new_dest,
                str(echo.get("template") or "{{ title }} {{ link }}"),
                visibility,
                1 if echo.get("enabled", 1) else 0,
                str(echo.get("filter_keywords") or ""),
                filter_mode,
                str(echo.get("content_warning") or ""),
                1 if echo.get("attach_image") else 0,
                delivery_mode,
                drip,
                uid,
            ),
        )
        added_echoes += 1

    return {
        "added_feeds": len(new_feed_urls),
        "added_accounts": sum(len(v) for v in new_account_keys.values()),
        "added_echoes": added_echoes,
        "existing_feeds": len(doc["feeds"]) - len(new_feed_urls),
        "existing_accounts": sum(
            len(doc["accounts"][s]) - len(new_account_keys[s])
            for s in ACCOUNT_TYPES
        ),
        "existing_echoes": existing_echoes,
        "skipped_echoes": skipped_echoes,
    }
