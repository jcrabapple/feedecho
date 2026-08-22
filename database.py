"""Database layer — SQLite with WAL mode and concurrency-safe migrations."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from settings import DB_PATH


@contextmanager
def get_db():
    """Yield a SQLite connection, closing it when done.

    Uses busy_timeout=30s for write contention. WAL mode is set once
    during init_db, not per-connection.
    """
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _column_names(db: sqlite3.Connection, table_name: str) -> set[str]:
    return {row["name"] for row in db.execute(f"PRAGMA table_info({table_name})")}


def _add_column_if_missing(
    db: sqlite3.Connection,
    table_name: str,
    column_name: str,
    column_definition: str,
) -> None:
    if column_name not in _column_names(db, table_name):
        db.execute(
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}"
        )


def init_db() -> None:
    """Create and migrate the application schema."""
    with get_db() as db:
        db.execute("PRAGMA journal_mode=WAL")

        db.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                username TEXT DEFAULT '',
                instance TEXT NOT NULL,
                access_token TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        account_columns = _column_names(db, "accounts")
        if "username" not in account_columns:
            db.execute("ALTER TABLE accounts ADD COLUMN username TEXT DEFAULT ''")
            rows = db.execute("SELECT id, name FROM accounts").fetchall()
            import re

            for row in rows:
                match = re.search(r"\(([^)]+)\)$", row["name"] or "")
                username = (
                    match.group(1)
                    if match
                    else (row["name"] or "unknown")
                )
                db.execute(
                    "UPDATE accounts SET username = ? WHERE id = ?",
                    (username, row["id"]),
                )

        db.execute("""
            CREATE TABLE IF NOT EXISTS feeds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                feed_type TEXT DEFAULT 'rss',
                poll_interval INTEGER DEFAULT 15,
                last_fetched TIMESTAMP,
                last_item_id TEXT,
                lease_token TEXT,
                lease_expires_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        _add_column_if_missing(db, "feeds", "lease_token", "TEXT")
        _add_column_if_missing(db, "feeds", "lease_expires_at", "TIMESTAMP")
        _add_column_if_missing(db, "feeds", "paused", "INTEGER NOT NULL DEFAULT 0")
        # Soft-delete marker: feeds are never hard-deleted by the app so that
        # echo configuration and posted-item history survive as an audit trail.
        _add_column_if_missing(db, "feeds", "deleted_at", "TIMESTAMP")

        echo_columns = _column_names(db, "echoes")
        if (
            echo_columns
            and "account_id" in echo_columns
            and "destination_type" not in echo_columns
        ):
            db.execute("DROP TABLE echoes")

        db.execute("""
            CREATE TABLE IF NOT EXISTS echoes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                feed_id INTEGER NOT NULL,
                destination_type TEXT NOT NULL DEFAULT 'mastodon',
                destination_id INTEGER NOT NULL,
                template TEXT NOT NULL DEFAULT '{{ title }} {{ link }}',
                visibility TEXT DEFAULT 'public',
                enabled INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (feed_id) REFERENCES feeds(id) ON DELETE CASCADE
            )
        """)
        _add_column_if_missing(db, "echoes", "filter_keywords", "TEXT DEFAULT ''")
        _add_column_if_missing(
            db, "echoes", "filter_mode", "TEXT NOT NULL DEFAULT 'exclude'"
        )
        _add_column_if_missing(db, "echoes", "content_warning", "TEXT DEFAULT ''")
        _add_column_if_missing(
            db, "echoes", "attach_image", "INTEGER NOT NULL DEFAULT 0"
        )
        _add_column_if_missing(
            db, "echoes", "delivery_mode", "TEXT NOT NULL DEFAULT 'instant'"
        )
        _add_column_if_missing(
            db, "echoes", "drip_limit", "INTEGER NOT NULL DEFAULT 0"
        )
        _add_column_if_missing(db, "echoes", "deleted_at", "TIMESTAMP")

        db.execute("""
            CREATE TABLE IF NOT EXISTS digest_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                echo_id INTEGER NOT NULL,
                item_id TEXT NOT NULL,
                item_title TEXT,
                item_url TEXT,
                rendered_content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(echo_id, item_id),
                FOREIGN KEY (echo_id) REFERENCES echoes(id) ON DELETE CASCADE
            )
        """)
        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_digest_items_echo
            ON digest_items(echo_id)
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS drip_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                echo_id INTEGER NOT NULL,
                item_id TEXT NOT NULL,
                item_json TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                queued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(echo_id, item_id),
                FOREIGN KEY (echo_id) REFERENCES echoes(id) ON DELETE CASCADE
            )
        """)
        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_drip_items_echo
            ON drip_items(echo_id)
        """)
        # Migrate drip_items tables created by the initial 1.11.0 schema
        # draft, which predated the attempts column.
        _add_column_if_missing(
            db, "drip_items", "attempts", "INTEGER NOT NULL DEFAULT 0"
        )

        db.execute("""
            CREATE TABLE IF NOT EXISTS email_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS bluesky_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                handle TEXT NOT NULL UNIQUE,
                app_password TEXT NOT NULL,
                did TEXT DEFAULT '',
                pds TEXT DEFAULT '',
                access_jwt TEXT DEFAULT '',
                refresh_jwt TEXT DEFAULT '',
                session_expires_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL DEFAULT '',
                plan TEXT NOT NULL DEFAULT 'trial',
                trial_ends_at TIMESTAMP,
                email_verified INTEGER NOT NULL DEFAULT 0,
                suspended INTEGER NOT NULL DEFAULT 0,
                stripe_customer_id TEXT DEFAULT '',
                stripe_subscription_id TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Single-tenant placeholder: all data belongs to user 1 when the
        # app runs without accounts (FEEDCHO_MODE=single).
        db.execute("INSERT OR IGNORE INTO users (id, email) VALUES (1, 'local')")

        # Owned tables carry user_id. Existing single-tenant databases
        # backfill to user 1 via the column default.
        for table in ("accounts", "feeds", "echoes", "email_accounts", "bluesky_accounts"):
            _add_column_if_missing(
                db, table, "user_id", "INTEGER NOT NULL DEFAULT 1"
            )

        db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                user_id INTEGER NOT NULL DEFAULT 1,
                key TEXT NOT NULL,
                value TEXT,
                PRIMARY KEY (user_id, key)
            )
        """)
        if "user_id" not in _column_names(db, "settings"):
            # Migrate legacy single-key settings tables: backfill to user 1.
            db.execute("ALTER TABLE settings RENAME TO settings_legacy")
            db.execute("""
                CREATE TABLE settings (
                    user_id INTEGER NOT NULL DEFAULT 1,
                    key TEXT NOT NULL,
                    value TEXT,
                    PRIMARY KEY (user_id, key)
                )
            """)
            db.execute(
                "INSERT INTO settings (user_id, key, value) SELECT 1, key, value FROM settings_legacy"
            )
            db.execute("DROP TABLE settings_legacy")

        db.execute("""
            CREATE TABLE IF NOT EXISTS posted_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                echo_id INTEGER NOT NULL,
                item_id TEXT NOT NULL,
                item_title TEXT,
                item_url TEXT,
                status TEXT NOT NULL,
                error_message TEXT,
                claimed_at TIMESTAMP,
                claim_token TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (echo_id) REFERENCES echoes(id) ON DELETE CASCADE
            )
        """)
        _add_column_if_missing(db, "posted_items", "claimed_at", "TIMESTAMP")
        _add_column_if_missing(db, "posted_items", "claim_token", "TEXT")
        _add_column_if_missing(db, "posted_items", "post_url", "TEXT")
        _add_column_if_missing(
            db,
            "posted_items",
            "attempt_count",
            "INTEGER NOT NULL DEFAULT 0",
        )
        _add_column_if_missing(db, "posted_items", "next_retry_at", "TIMESTAMP")

        db.execute("""
            CREATE TABLE IF NOT EXISTS oauth_apps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instance TEXT NOT NULL UNIQUE,
                client_id TEXT NOT NULL,
                client_secret TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS oauth_states (
                nonce TEXT PRIMARY KEY,
                instance TEXT NOT NULL,
                session_binding TEXT NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                consumed_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_posted_items_echo
            ON posted_items(echo_id, posted_at DESC)
        """)
        db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_posted_items_echo_item
            ON posted_items(echo_id, item_id)
        """)
        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_posted_items_reclaim
            ON posted_items(status, claimed_at)
        """)
        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_echoes_feed
            ON echoes(feed_id)
        """)
        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_feeds_lease
            ON feeds(lease_expires_at)
        """)
        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_oauth_states_expiry
            ON oauth_states(expires_at)
        """)

        # Best-effort cleanup of expired/consumed state rows.
        db.execute("""
            DELETE FROM oauth_states
            WHERE consumed_at IS NOT NULL
               OR expires_at <= datetime('now', '-1 day')
        """)


init_db()
