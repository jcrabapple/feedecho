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
