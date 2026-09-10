"""Tests for database layer."""

import pytest
import os
import tempfile
from pathlib import Path
from database import get_db, init_db


@pytest.fixture
def temp_db(monkeypatch):
    """Use a temp database for each test."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        monkeypatch.setattr("database.DB_PATH", db_path)
        init_db()
        yield db_path


class TestDatabaseInit:
    def test_creates_all_tables(self, temp_db):
        with get_db() as db:
            tables = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            table_names = [t["name"] for t in tables]
            assert "accounts" in table_names
            assert "feeds" in table_names
            assert "echoes" in table_names
            assert "posted_items" in table_names


class TestAccounts:
    def test_insert_and_query(self, temp_db):
        with get_db() as db:
            db.execute(
                "INSERT INTO accounts (name, instance, access_token) VALUES (?, ?, ?)",
                ("Test", "https://example.com", "token123"),
            )
            rows = db.execute("SELECT * FROM accounts").fetchall()
            assert len(rows) == 1
            assert rows[0]["name"] == "Test"
            assert rows[0]["instance"] == "https://example.com"
            assert rows[0]["access_token"] == "token123"


class TestDripItemsMigration:
    def test_attempts_column_added_to_legacy_table(self, temp_db):
        # Simulate a DB created by the first 1.11.0 schema draft, which
        # shipped drip_items without the attempts column.
        with get_db() as db:
            db.execute("DROP TABLE drip_items")
            db.execute(
                """
                CREATE TABLE drip_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    echo_id INTEGER NOT NULL,
                    item_id TEXT NOT NULL,
                    item_json TEXT NOT NULL,
                    queued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(echo_id, item_id),
                    FOREIGN KEY (echo_id) REFERENCES echoes(id) ON DELETE CASCADE
                )
                """
            )

        init_db()

        with get_db() as db:
            cols = {row["name"] for row in db.execute("PRAGMA table_info(drip_items)")}
        assert "attempts" in cols


class TestEchoes:
    def test_cascade_delete_with_feed(self, temp_db):
        with get_db() as db:
            # Create feed, account, echo
            db.execute(
                "INSERT INTO feeds (name, url) VALUES (?, ?)",
                ("Test Feed", "https://example.com/feed.xml"),
            )
            db.execute(
                "INSERT INTO accounts (name, instance, access_token) VALUES (?, ?, ?)",
                ("Test", "https://example.com", "token"),
            )
            db.execute(
                "INSERT INTO echoes (feed_id, destination_type, destination_id, template) VALUES (?, ?, ?, ?)",
                (1, "mastodon", 1, "{{ title }}"),
            )
            # Direct SQL DELETE still cascades (schema-level safety net);
            # the app itself soft-deletes feeds to preserve history.
            db.execute("DELETE FROM feeds WHERE id = 1")
            echoes = db.execute("SELECT * FROM echoes").fetchall()
            assert len(echoes) == 0

    def test_email_account_crud(self, temp_db):
        with get_db() as db:
            db.execute(
                "INSERT INTO email_accounts (name, email) VALUES (?, ?)",
                ("Test User", "user@example.com"),
            )
            rows = db.execute("SELECT * FROM email_accounts").fetchall()
            assert len(rows) == 1
            assert rows[0]["email"] == "user@example.com"

    def test_settings_crud(self, temp_db):
        with get_db() as db:
            db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                ("smtp_host", "smtp.example.com"),
            )
            row = db.execute("SELECT value FROM settings WHERE key = 'smtp_host'").fetchone()
            assert row["value"] == "smtp.example.com"


class TestAccountsUsernameMigration:
    def test_username_backfilled_on_legacy_table(self, temp_db):
        # Simulate a database created before `username` shipped on the
        # accounts table (the same shape a pre-existing hosted Postgres
        # database would have before finding #1's fix).
        with get_db() as db:
            db.execute("DROP TABLE accounts")
            db.execute(
                """
                CREATE TABLE accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    instance TEXT NOT NULL,
                    access_token TEXT NOT NULL,
                    user_id INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            db.execute(
                "INSERT INTO accounts (name, instance, access_token) VALUES (?, ?, ?)",
                ("Alice (alice)", "https://example.com", "token"),
            )
            db.execute(
                "INSERT INTO accounts (name, instance, access_token) VALUES (?, ?, ?)",
                ("Bob", "https://other.example.com", "token2"),
            )

        init_db()

        with get_db() as db:
            rows = db.execute(
                "SELECT name, username FROM accounts ORDER BY id"
            ).fetchall()
        assert rows[0]["username"] == "alice"
        assert rows[1]["username"] == "Bob"


class TestFeedsUniqueConstraint:
    def test_duplicate_user_url_rejected(self, temp_db):
        import sqlite3

        with get_db() as db:
            db.execute(
                "INSERT INTO feeds (name, url, user_id) VALUES (?, ?, ?)",
                ("Feed", "https://example.com/feed.xml", 1),
            )
        with pytest.raises(sqlite3.IntegrityError):
            with get_db() as db:
                db.execute(
                    "INSERT INTO feeds (name, url, user_id) VALUES (?, ?, ?)",
                    ("Feed dup", "https://example.com/feed.xml", 1),
                )

    def test_same_url_different_user_allowed(self, temp_db):
        with get_db() as db:
            db.execute(
                "INSERT INTO feeds (name, url, user_id) VALUES (?, ?, ?)",
                ("Feed", "https://example.com/feed.xml", 1),
            )
            db.execute(
                "INSERT INTO feeds (name, url, user_id) VALUES (?, ?, ?)",
                ("Feed", "https://example.com/feed.xml", 2),
            )
            rows = db.execute("SELECT * FROM feeds").fetchall()
        assert len(rows) == 2

    def test_readd_after_soft_delete_allowed(self, temp_db):
        """The unique index is partial (WHERE deleted_at IS NULL): feeds are
        soft-deleted and never purged, and import_export.py's own dedup
        lookup already ignores soft-deleted rows, so re-adding the same URL
        after a delete must not be permanently blocked."""
        with get_db() as db:
            db.execute(
                "INSERT INTO feeds (name, url, user_id) VALUES (?, ?, ?)",
                ("Feed", "https://example.com/feed.xml", 1),
            )
            db.execute(
                "UPDATE feeds SET deleted_at = CURRENT_TIMESTAMP"
                " WHERE url = ? AND user_id = ?",
                ("https://example.com/feed.xml", 1),
            )
            db.execute(
                "INSERT INTO feeds (name, url, user_id) VALUES (?, ?, ?)",
                ("Feed again", "https://example.com/feed.xml", 1),
            )
            rows = db.execute(
                "SELECT * FROM feeds WHERE url = ? AND user_id = ?",
                ("https://example.com/feed.xml", 1),
            ).fetchall()
        assert len(rows) == 2


class TestAccountsUniqueConstraint:
    def test_duplicate_user_instance_username_rejected(self, temp_db):
        import sqlite3

        with get_db() as db:
            db.execute(
                "INSERT INTO accounts (name, username, instance, access_token, user_id)"
                " VALUES (?, ?, ?, ?, ?)",
                ("Alice", "alice", "https://example.com", "token", 1),
            )
        with pytest.raises(sqlite3.IntegrityError):
            with get_db() as db:
                db.execute(
                    "INSERT INTO accounts"
                    " (name, username, instance, access_token, user_id)"
                    " VALUES (?, ?, ?, ?, ?)",
                    ("Alice again", "alice", "https://example.com", "token2", 1),
                )


class TestUpgradeDedupe:
    """Pre-upgrade databases can already hold duplicate rows the new unique
    indexes would reject: oauth_callback/add_account were plain INSERTs on
    every reconnect. init_db must dedupe them BEFORE building the indexes,
    or the upgrade crashes at boot (v1.39.0 Discord-hash failure class)."""

    def _make_legacy_duplicates(self, db):
        db.execute("DROP INDEX IF EXISTS idx_feeds_user_url")
        db.execute("DROP INDEX IF EXISTS idx_accounts_user_instance_username")
        # Two accounts rows for the same logical account (user, instance,
        # username) — the reconnect created a second row; echoes point at
        # the OLD one.
        db.execute(
            "INSERT INTO accounts (name, username, instance, access_token, user_id)"
            " VALUES ('Alice', 'alice', 'https://ex.com', 'old-token', 1)"
        )
        db.execute(
            "INSERT INTO accounts (name, username, instance, access_token, user_id)"
            " VALUES ('Alice new', 'alice', 'https://ex.com', 'new-token', 1)"
        )
        # Two ACTIVE feeds rows with the same (user_id, url) — feeds first,
        # the echoes below carry an FK on feed_id.
        db.execute(
            "INSERT INTO feeds (name, url, user_id) VALUES ('F', 'https://ex.com/rss', 1)"
        )
        db.execute(
            "INSERT INTO feeds (name, url, user_id) VALUES ('F2', 'https://ex.com/rss', 1)"
        )
        db.execute(
            "INSERT INTO echoes (feed_id, destination_type, destination_id,"
            " template, user_id) VALUES (1, 'mastodon', 1, 't', 1)"
        )
        db.execute(
            "INSERT INTO queued_posts (user_id, item_id, feed_id,"
            " destination_type, destination_id, content, scheduled_at)"
            " VALUES (1, 'i1', 1, 'mastodon', 1, 'c', '2026-01-01 00:00:00')"
        )
        # Items on both rows: one collides with the kept feed's items, one
        # must be repointed.
        db.execute(
            "INSERT INTO feed_items (feed_id, item_id, title) VALUES (1, 'i1', 'A')"
        )
        db.execute(
            "INSERT INTO feed_items (feed_id, item_id, title) VALUES (2, 'i1', 'dup')"
        )
        db.execute(
            "INSERT INTO feed_items (feed_id, item_id, title) VALUES (2, 'i2', 'keep')"
        )

    def test_init_db_survives_duplicate_rows_and_dedupes(self, temp_db):
        with get_db() as db:
            self._make_legacy_duplicates(db)

        # Pre-fix, this raised IntegrityError from CREATE UNIQUE INDEX.
        init_db()

        with get_db() as db:
            accounts = db.execute(
                "SELECT id, username, access_token FROM accounts"
            ).fetchall()
            assert len(accounts) == 1
            # MAX(id) survives: the freshest reconnect wins.
            assert accounts[0]["username"] == "alice"
            assert accounts[0]["access_token"] == "new-token"
            echo = db.execute(
                "SELECT destination_id FROM echoes"
            ).fetchone()
            assert echo["destination_id"] == accounts[0]["id"]
            qp = db.execute(
                "SELECT destination_id FROM queued_posts"
            ).fetchone()
            assert qp["destination_id"] == accounts[0]["id"]
            active = db.execute(
                "SELECT id, deleted_at FROM feeds ORDER BY id"
            ).fetchall()
            # MIN(id) survives; the duplicate is soft-deleted, never
            # hard-deleted (posted_items history keeps pointing at it).
            assert len(active) == 2
            assert active[0]["deleted_at"] is None
            assert active[1]["deleted_at"] is not None
            items = db.execute(
                "SELECT feed_id, item_id FROM feed_items ORDER BY item_id"
            ).fetchall()
            # Colliding item dropped, non-colliding item repointed to the
            # kept feed.
            assert [(r["feed_id"], r["item_id"]) for r in items] == [
                (active[0]["id"], "i1"),
                (active[0]["id"], "i2"),
            ]
            idx = db.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
                " AND name IN ('idx_feeds_user_url',"
                " 'idx_accounts_user_instance_username')"
            ).fetchall()
            assert len(idx) == 2

    def test_conflict_do_nothing_insert_reports_rowcount_zero(self, temp_db):
        """The exact INSERT the OPML import route relies on: a conflicting
        insert must be skipped silently and report rowcount 0, not raise."""
        with get_db() as db:
            db.execute(
                "INSERT INTO feeds (name, url, user_id) VALUES ('F', 'https://x/rss', 1)"
            )
            cur = db.execute(
                "INSERT INTO feeds (name, url, user_id)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT(user_id, url) WHERE deleted_at IS NULL DO NOTHING",
                ("F2", "https://x/rss", 1),
            )
            assert cur.rowcount == 0
            cur2 = db.execute(
                "INSERT INTO feeds (name, url, user_id)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT(user_id, url) WHERE deleted_at IS NULL DO NOTHING",
                ("F3", "https://x/other", 1),
            )
            assert cur2.rowcount == 1
