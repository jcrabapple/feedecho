"""Tests for the users table, user_id columns, and singleton migration."""

import tempfile
from pathlib import Path

import pytest

from database import _has_unique_on, get_db, init_db


@pytest.fixture
def temp_db(monkeypatch):
    """Point the DB layer at a fresh temp file per test."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        monkeypatch.setattr("database.DB_PATH", db_path)
        init_db()
        yield db_path


def _cols(db, table):
    return {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}


class TestUsersTable:
    def test_users_table_exists(self, temp_db):
        with get_db() as db:
            cols = _cols(db, "users")
        assert {
            "id",
            "email",
            "password_hash",
            "plan",
            "trial_ends_at",
            "email_verified",
            "suspended",
            "stripe_customer_id",
            "stripe_subscription_id",
            "created_at",
        } <= cols

    def test_singleton_user_created(self, temp_db):
        with get_db() as db:
            row = db.execute("SELECT id, email FROM users WHERE id = 1").fetchone()
        assert row is not None
        assert row["email"] == "local"


class TestUserScopedTables:
    @pytest.mark.parametrize(
        "table",
        ["accounts", "feeds", "echoes", "email_accounts", "bluesky_accounts"],
    )
    def test_user_id_column_exists(self, temp_db, table):
        with get_db() as db:
            cols = _cols(db, table)
        assert "user_id" in cols

    def test_new_rows_default_to_user_1(self, temp_db):
        with get_db() as db:
            db.execute(
                "INSERT INTO accounts (name, instance, access_token) VALUES (?, ?, ?)",
                ("Test", "https://example.com", "tok"),
            )
            row = db.execute("SELECT user_id FROM accounts").fetchone()
        assert row["user_id"] == 1


class TestSettingsCompositeKey:
    def test_settings_has_user_id_in_pk(self, temp_db):
        with get_db() as db:
            cols = _cols(db, "settings")
            pk = db.execute("PRAGMA table_info(settings)").fetchall()
        assert "user_id" in cols
        pk_cols = [r["name"] for r in pk if r["pk"] > 0]
        assert set(pk_cols) == {"user_id", "key"}

    def test_existing_settings_backfilled_to_user_1(self, temp_db):
        # Simulate a pre-migration settings table (single key PK, no user_id)
        with get_db() as db:
            db.execute("DROP TABLE settings")
            db.execute(
                "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)"
            )
            db.execute(
                "INSERT INTO settings (key, value) VALUES ('smtp_host', 'smtp.example.com')"
            )

        init_db()

        with get_db() as db:
            cols = _cols(db, "settings")
            row = db.execute(
                "SELECT user_id, key, value FROM settings WHERE key = 'smtp_host'"
            ).fetchone()
        assert "user_id" in cols
        assert row["user_id"] == 1
        assert row["value"] == "smtp.example.com"

    def test_settings_migration_survives_leftover_legacy_table(self, temp_db):
        # A partial migration or backup restore may leave settings_legacy
        # behind; init_db() must still start and migrate cleanly.
        with get_db() as db:
            db.execute("DROP TABLE settings")
            db.execute(
                "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)"
            )
            db.execute(
                "INSERT INTO settings (key, value) VALUES ('keep', 'v1')"
            )
            db.execute(
                "CREATE TABLE settings_legacy (key TEXT PRIMARY KEY, value TEXT)"
            )
            db.execute(
                "INSERT INTO settings_legacy (key, value) VALUES ('junk', 'x')"
            )

        init_db()

        with get_db() as db:
            row = db.execute(
                "SELECT value FROM settings WHERE key = 'keep'"
            ).fetchone()
        assert row["value"] == "v1"


class TestCompositeUniqueMigration:
    def test_email_accounts_migrated_from_legacy_single_unique(self, temp_db):
        with get_db() as db:
            db.execute("DROP TABLE email_accounts")
            db.execute(
                """
                CREATE TABLE email_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    email TEXT NOT NULL UNIQUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            db.execute(
                "INSERT INTO email_accounts (name, email) VALUES ('T', 'me@example.com')"
            )

        init_db()

        with get_db() as db:
            row = db.execute(
                "SELECT user_id, email FROM email_accounts"
            ).fetchone()
            assert row["user_id"] == 1
            assert row["email"] == "me@example.com"
            assert _has_unique_on(db, "email_accounts", {"user_id", "email"})
            assert not _has_unique_on(db, "email_accounts", {"email"})

    def test_bluesky_accounts_migrated_from_legacy_single_unique(self, temp_db):
        with get_db() as db:
            db.execute("DROP TABLE bluesky_accounts")
            db.execute(
                """
                CREATE TABLE bluesky_accounts (
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
                """
            )
            db.execute(
                "INSERT INTO bluesky_accounts (name, handle, app_password)"
                " VALUES ('B', 'user.bsky.social', 'pw')"
            )

        init_db()

        with get_db() as db:
            row = db.execute(
                "SELECT user_id, handle FROM bluesky_accounts"
            ).fetchone()
            assert row["user_id"] == 1
            assert _has_unique_on(db, "bluesky_accounts", {"user_id", "handle"})
            assert not _has_unique_on(db, "bluesky_accounts", {"handle"})

    def test_two_users_can_share_destination_email(self, temp_db):
        with get_db() as db:
            db.execute(
                "INSERT INTO email_accounts (name, email, user_id) VALUES"
                " ('A', 'shared@example.com', 1)"
            )
            db.execute(
                "INSERT INTO email_accounts (name, email, user_id) VALUES"
                " ('B', 'shared@example.com', 2)"
            )
        with get_db() as db:
            count = db.execute(
                "SELECT COUNT(*) AS c FROM email_accounts"
            ).fetchone()["c"]
        assert count == 2

    def test_same_user_still_cannot_duplicate_email(self, temp_db):
        import sqlite3

        with get_db() as db:
            db.execute(
                "INSERT INTO email_accounts (name, email, user_id) VALUES"
                " ('A', 'dup@example.com', 1)"
            )
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(
                    "INSERT INTO email_accounts (name, email, user_id) VALUES"
                    " ('B', 'dup@example.com', 1)"
                )
