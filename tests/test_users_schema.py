"""Tests for the users table, user_id columns, and singleton migration."""

import tempfile
from pathlib import Path

import pytest

from database import get_db, init_db


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
