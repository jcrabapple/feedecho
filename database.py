"""Database layer — SQLite (single mode) or PostgreSQL (multi mode).

Every query in the codebase is written with ``?`` placeholders and runs
unchanged on both backends: :func:`qmark` translates ``?`` to ``%s`` when
the dialect is postgres, and ``get_db`` yields a connection whose
``execute`` performs that translation. Rows support ``row["col"]`` on both
backends (sqlite3.Row / psycopg dict_row).
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

import settings
from settings import DB_PATH


def dialect() -> str:
    """'postgres' when running multi-tenant against DATABASE_URL, else 'sqlite'."""
    return "postgres" if (settings.MULTI and settings.DATABASE_URL) else "sqlite"


def qmark(sql: str) -> str:
    """Translate ``?`` placeholders to ``%s`` for Postgres.

    Single-quoted string literals are skipped: ``?`` inside them is left
    alone, ``%`` is escaped to ``%%`` (psycopg3 treats bare ``%`` in
    literals as a placeholder), and SQL-standard doubled quotes
    (``'it''s'``) are handled so a ``?`` after an escaped quote stays
    inside the literal. No-op when the dialect is sqlite.

    Deliberately out of scope: ``E'...'`` backslash escapes and
    dollar-quoted strings — FeedEcho's SQL never uses them.
    """
    if dialect() != "postgres":
        return sql
    out = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch != "'":
            out.append("%s" if ch == "?" else ch)
            i += 1
            continue
        # Enter a string literal: copy verbatim until the closing quote.
        out.append(ch)
        i += 1
        while i < n:
            if sql[i] == "'":
                if i + 1 < n and sql[i + 1] == "'":  # doubled quote escape
                    out.append("''")
                    i += 2
                    continue
                out.append("'")
                i += 1
                break
            out.append("%%" if sql[i] == "%" else sql[i])
            i += 1
    return "".join(out)


class _PgConnection:
    """Thin adapter over psycopg3 so callers keep writing db.execute(sql, ?)."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        return self._conn.execute(qmark(sql), params)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


def _pg_connect():
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError(
            "PostgreSQL mode requires the psycopg package: "
            "pip install 'feedecho[postgres]'"
        ) from exc
    return psycopg.connect(
        settings.DATABASE_URL, row_factory=psycopg.rows.dict_row
    )


@contextmanager
def get_db():
    """Yield a connection, committing on clean exit, rolling back on error.

    Postgres connections commit/rollback via psycopg's transaction
    context manager; SQLite connections use WAL mode (set once during
    init_db) with busy_timeout=30s for write contention.
    """
    if dialect() == "postgres":
        conn = _pg_connect()
        try:
            with conn:  # commits on success, rolls back on exception
                yield _PgConnection(conn)
        finally:
            conn.close()
        return

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


def _column_names(db, table_name: str) -> set[str]:
    if dialect() == "postgres":
        rows = db.execute(
            "SELECT column_name AS name FROM information_schema.columns"
            " WHERE table_name = ?",
            (table_name,),
        ).fetchall()
        return {row["name"] for row in rows}
    return {row["name"] for row in db.execute(f"PRAGMA table_info({table_name})")}


def _has_unique_on(db, table_name: str, columns: set[str]) -> bool:
    """True if the table has a UNIQUE index covering exactly `columns`.

    SQLite-only helper: detects inline UNIQUE constraints (autoindexes)
    so migrations can be idempotent across restarts.
    """
    for idx in db.execute(f"PRAGMA index_list({table_name})"):
        if not idx["unique"]:
            continue
        cols = {r["name"] for r in db.execute(f"PRAGMA index_info({idx['name']})")}
        if cols == columns:
            return True
    return False


def _add_column_if_missing(
    db,
    table_name: str,
    column_name: str,
    column_definition: str,
) -> None:
    if dialect() == "postgres":
        db.execute(
            f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS "
            f"{column_name} {column_definition}"
        )
        return
    if column_name not in _column_names(db, table_name):
        db.execute(
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}"
        )


def init_db() -> None:
    """Create and migrate the schema for the active dialect."""
    if dialect() == "postgres":
        init_db_postgres()
    else:
        init_db_sqlite()


def init_db_sqlite() -> None:
    """Create and migrate the SQLite application schema."""
    with get_db() as db:
        db.execute("PRAGMA journal_mode=WAL")

        db.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                username TEXT DEFAULT '',
                instance TEXT NOT NULL,
                access_token TEXT NOT NULL,
                user_id INTEGER NOT NULL DEFAULT 1,
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
                paused INTEGER NOT NULL DEFAULT 0,
                deleted_at TIMESTAMP,
                user_id INTEGER NOT NULL DEFAULT 1,
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
                user_id INTEGER NOT NULL DEFAULT 1,
                filter_keywords TEXT DEFAULT '',
                filter_mode TEXT NOT NULL DEFAULT 'exclude',
                content_warning TEXT DEFAULT '',
                attach_image INTEGER NOT NULL DEFAULT 0,
                delivery_mode TEXT NOT NULL DEFAULT 'instant',
                drip_limit INTEGER NOT NULL DEFAULT 0,
                deleted_at TIMESTAMP,
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
                email TEXT NOT NULL,
                user_id INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, email)
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS bluesky_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                handle TEXT NOT NULL,
                app_password TEXT NOT NULL,
                did TEXT DEFAULT '',
                pds TEXT DEFAULT '',
                access_jwt TEXT DEFAULT '',
                refresh_jwt TEXT DEFAULT '',
                session_expires_at TIMESTAMP,
                user_id INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, handle)
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
                is_admin INTEGER NOT NULL DEFAULT 0,
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
        # Admin flag for hosted mode (A2); never auto-granted.
        _add_column_if_missing(db, "users", "is_admin", "INTEGER NOT NULL DEFAULT 0")

        # Migrate single-column UNIQUE constraints to per-user composite
        # constraints so different tenants can use the same destination
        # email/handle. Identical behavior in single mode (user_id is
        # always 1). Recreate-table pattern, guarded by _has_unique_on so
        # it is idempotent across restarts.
        if not _has_unique_on(db, "email_accounts", {"user_id", "email"}):
            db.execute("DROP TABLE IF EXISTS email_accounts_legacy")
            db.execute(
                "ALTER TABLE email_accounts RENAME TO email_accounts_legacy"
            )
            db.execute("""
                CREATE TABLE email_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    email TEXT NOT NULL,
                    user_id INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, email)
                )
            """)
            db.execute(
                "INSERT INTO email_accounts (id, name, email, user_id, created_at)"
                " SELECT id, name, email, user_id, created_at"
                " FROM email_accounts_legacy"
            )
            db.execute("DROP TABLE email_accounts_legacy")

        if not _has_unique_on(db, "bluesky_accounts", {"user_id", "handle"}):
            db.execute("DROP TABLE IF EXISTS bluesky_accounts_legacy")
            db.execute(
                "ALTER TABLE bluesky_accounts RENAME TO bluesky_accounts_legacy"
            )
            db.execute("""
                CREATE TABLE bluesky_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    handle TEXT NOT NULL,
                    app_password TEXT NOT NULL,
                    did TEXT DEFAULT '',
                    pds TEXT DEFAULT '',
                    access_jwt TEXT DEFAULT '',
                    refresh_jwt TEXT DEFAULT '',
                    session_expires_at TIMESTAMP,
                    user_id INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, handle)
                )
            """)
            db.execute(
                "INSERT INTO bluesky_accounts (id, name, handle, app_password,"
                " did, pds, access_jwt, refresh_jwt, session_expires_at,"
                " user_id, created_at)"
                " SELECT id, name, handle, app_password, did, pds, access_jwt,"
                " refresh_jwt, session_expires_at, user_id, created_at"
                " FROM bluesky_accounts_legacy"
            )
            db.execute("DROP TABLE bluesky_accounts_legacy")

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
            # The guard keeps the app startable if a partial migration or
            # backup restore left a settings_legacy table behind.
            db.execute("DROP TABLE IF EXISTS settings_legacy")
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
                post_url TEXT,
                next_retry_at TIMESTAMP,
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
                user_id INTEGER,
                consumed_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        _add_column_if_missing(db, "oauth_states", "user_id", "INTEGER")

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

        _init_shared_tables(db)


def _init_shared_tables(db) -> None:
    """Deployment-wide tables, identical on both dialects.

    system_settings holds admin email/SMTP config; email_tokens holds
    single-use email-flow tokens (verification, password reset).

    Deliberately a separate table from per-user `settings`: system values
    (verification/reset SMTP) belong to the deployment, not to any tenant,
    and must be unreadable/unwritable by tenant-scoped settings routes.
    """
    db.execute("""
        CREATE TABLE IF NOT EXISTS system_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    if dialect() == "postgres":
        db.execute("""
            CREATE TABLE IF NOT EXISTS email_tokens (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                token_hash TEXT NOT NULL,
                purpose TEXT NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                consumed_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    else:
        db.execute("""
            CREATE TABLE IF NOT EXISTS email_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                token_hash TEXT NOT NULL,
                purpose TEXT NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                consumed_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_email_tokens_user
        ON email_tokens(user_id, purpose)
    """)
    # At most ONE unconsumed token per (user, purpose): the partial unique
    # index serializes concurrent issue_token calls at the DB level on both
    # dialects (the issuer retries once on conflict).
    db.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_email_tokens_active
        ON email_tokens(user_id, purpose)
        WHERE consumed_at IS NULL
    """)


def init_db_postgres() -> None:
    """Create the PostgreSQL application schema (multi-tenant).

    Mirrors init_db_sqlite at the post-migration state: fresh Postgres
    installs get the final schema directly. No singleton user row — real
    accounts come from /register. Keep the table set in sync with
    init_db_sqlite; the schema-parity test enforces it.
    """
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id BIGSERIAL PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL DEFAULT '',
                plan TEXT NOT NULL DEFAULT 'trial',
                trial_ends_at TIMESTAMP,
                email_verified INTEGER NOT NULL DEFAULT 0,
                suspended INTEGER NOT NULL DEFAULT 0,
                is_admin INTEGER NOT NULL DEFAULT 0,
                stripe_customer_id TEXT DEFAULT '',
                stripe_subscription_id TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id BIGSERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                username TEXT DEFAULT '',
                instance TEXT NOT NULL,
                access_token TEXT NOT NULL,
                user_id BIGINT NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS feeds (
                id BIGSERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                feed_type TEXT DEFAULT 'rss',
                poll_interval INTEGER DEFAULT 15,
                last_fetched TIMESTAMP,
                last_item_id TEXT,
                lease_token TEXT,
                lease_expires_at TIMESTAMP,
                paused INTEGER NOT NULL DEFAULT 0,
                deleted_at TIMESTAMP,
                user_id BIGINT NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS echoes (
                id BIGSERIAL PRIMARY KEY,
                feed_id INTEGER NOT NULL,
                destination_type TEXT NOT NULL DEFAULT 'mastodon',
                destination_id INTEGER NOT NULL,
                template TEXT NOT NULL DEFAULT '{{ title }} {{ link }}',
                visibility TEXT DEFAULT 'public',
                enabled INTEGER DEFAULT 1,
                user_id BIGINT NOT NULL DEFAULT 1,
                filter_keywords TEXT DEFAULT '',
                filter_mode TEXT NOT NULL DEFAULT 'exclude',
                content_warning TEXT DEFAULT '',
                attach_image INTEGER NOT NULL DEFAULT 0,
                delivery_mode TEXT NOT NULL DEFAULT 'instant',
                drip_limit INTEGER NOT NULL DEFAULT 0,
                deleted_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (feed_id) REFERENCES feeds(id) ON DELETE CASCADE
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS digest_items (
                id BIGSERIAL PRIMARY KEY,
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
                id BIGSERIAL PRIMARY KEY,
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

        db.execute("""
            CREATE TABLE IF NOT EXISTS email_accounts (
                id BIGSERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                user_id BIGINT NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, email)
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS bluesky_accounts (
                id BIGSERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                handle TEXT NOT NULL,
                app_password TEXT NOT NULL,
                did TEXT DEFAULT '',
                pds TEXT DEFAULT '',
                access_jwt TEXT DEFAULT '',
                refresh_jwt TEXT DEFAULT '',
                session_expires_at TIMESTAMP,
                user_id BIGINT NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, handle)
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                user_id BIGINT NOT NULL DEFAULT 1,
                key TEXT NOT NULL,
                value TEXT,
                PRIMARY KEY (user_id, key)
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS posted_items (
                id BIGSERIAL PRIMARY KEY,
                echo_id INTEGER NOT NULL,
                item_id TEXT NOT NULL,
                item_title TEXT,
                item_url TEXT,
                status TEXT NOT NULL,
                error_message TEXT,
                claimed_at TIMESTAMP,
                claim_token TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                post_url TEXT,
                next_retry_at TIMESTAMP,
                posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (echo_id) REFERENCES echoes(id) ON DELETE CASCADE
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS oauth_apps (
                id BIGSERIAL PRIMARY KEY,
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
                user_id BIGINT,
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
               OR expires_at <= NOW() - INTERVAL '1 day'
        """)

        # Admin flag for hosted mode (A2); never auto-granted.
        _add_column_if_missing(db, "users", "is_admin", "INTEGER NOT NULL DEFAULT 0")

        _init_shared_tables(db)

init_db()
