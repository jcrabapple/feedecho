"""Starred-items export (CSV/JSON) for the reader."""

import csv
import io
import json
import os

import pytest
from fastapi.testclient import TestClient

import database
import security
import settings
from app import app

TEST_PG_URL = os.environ.get("FEEDECHO_TEST_PG_URL", "")

requires_pg = pytest.mark.skipif(
    not TEST_PG_URL, reason="FEEDECHO_TEST_PG_URL not set; PG tests are CI-gated"
)


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MULTI", False)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "starred-export.db")
    database.init_db()
    import scheduler

    monkeypatch.setattr(scheduler, "check_all_feeds", lambda: None)
    with database.get_db() as db:
        db.execute(
            "INSERT INTO feeds (name, url, read_enabled) VALUES (?, ?, 1)",
            ("Alpha", "https://example.com/a"),
        )
        db.execute(
            "INSERT INTO feeds (name, url, read_enabled) VALUES (?, ?, 1)",
            ("Beta", "https://example.com/b"),
        )
        # 1: starred, newest. 2: starred, older. 3: unstarred (excluded).
        db.execute(
            "INSERT INTO feed_items (feed_id, item_id, title, author, link, summary,"
            " is_read, starred, published_at)"
            " VALUES (1, 'i1', '=Cookie Recipe', 'Ada', 'https://ex.com/1', 'Tasty', 1, 1,"
            " '2026-09-10 12:00:00')"
        )
        db.execute(
            "INSERT INTO feed_items (feed_id, item_id, title, author, link, summary,"
            " is_read, starred, published_at)"
            " VALUES (2, 'i2', 'Older Post', 'Bob', 'https://ex.com/2', 'Fine', 0, 1,"
            " '2026-09-01 08:30:00')"
        )
        db.execute(
            "INSERT INTO feed_items (feed_id, item_id, title, link, is_read, starred,"
            " published_at)"
            " VALUES (1, 'i3', 'Unstarred', 'https://ex.com/3', 0, 0, '2026-09-15 09:00:00')"
        )
    return settings


def _rows(text: str):
    return list(csv.DictReader(io.StringIO(text)))


class TestStarredExportCSV:
    def test_csv_download(self, env):
        with TestClient(app) as c:
            r = c.get("/api/reader/starred/export?format=csv")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/csv")
        assert "attachment" in r.headers["content-disposition"]
        assert "feedecho-starred.csv" in r.headers["content-disposition"]
        rows = _rows(r.text)
        assert len(rows) == 2
        assert rows[0]["title"] == "'=Cookie Recipe"  # published_at DESC
        assert rows[0]["feed"] == "Alpha"
        assert rows[1]["title"] == "Older Post"
        assert rows[1]["feed"] == "Beta"
        assert rows[1]["link"] == "https://ex.com/2"
        assert rows[1]["published_at"] == "2026-09-01 08:30:00"

    def test_default_format_is_csv(self, env):
        with TestClient(app) as c:
            r = c.get("/api/reader/starred/export")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/csv")

    def test_formula_prefix_neutralized(self, env):
        with TestClient(app) as c:
            r = c.get("/api/reader/starred/export?format=csv")
        rows = _rows(r.text)
        # '=Cookie Recipe' must not survive as a spreadsheet formula.
        assert rows[0]["title"] == "'=Cookie Recipe"

    def test_formula_prefix_after_leading_whitespace(self, env):
        with database.get_db() as db:
            db.execute(
                "INSERT INTO feed_items (feed_id, item_id, title, starred, published_at)"
                " VALUES (1, 'i5', ' \t=cmd|calc!A0', 1, '2026-09-12 10:00:00')"
            )
        with TestClient(app) as c:
            r = c.get("/api/reader/starred/export?format=csv")
        rows = _rows(r.text)
        # Excel strips leading whitespace before formula parsing, so the
        # guard must check past it.
        assert rows[0]["title"] == "' \t=cmd|calc!A0"

    def test_unstarred_and_soft_deleted_excluded(self, env):
        with database.get_db() as db:
            db.execute(
                "INSERT INTO feed_items (feed_id, item_id, title, starred, published_at)"
                " VALUES (1, 'i4', 'Ghost Item', 1, '2026-09-14 10:00:00')"
            )
            db.execute("UPDATE feeds SET deleted_at = '2026-09-15 10:00:00' WHERE id = 1")
        with TestClient(app) as c:
            r = c.get("/api/reader/starred/export?format=csv")
        rows = _rows(r.text)
        titles = [row["title"] for row in rows]
        assert "Ghost Item" not in titles  # feed soft-deleted
        assert "Unstarred" not in titles
        assert len(rows) == 1


class TestStarredExportJSON:
    def test_json_download(self, env):
        with TestClient(app) as c:
            r = c.get("/api/reader/starred/export?format=json")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")
        assert "feedecho-starred.json" in r.headers["content-disposition"]
        data = json.loads(r.text)
        assert data["format"] == "feedecho-starred-export"
        assert data["version"] == 1
        assert data["count"] == 2
        items = data["items"]
        assert items[0]["title"] == "=Cookie Recipe"  # JSON is not formula-guarded
        assert items[0]["item_id"] == "i1"
        assert items[0]["feed"] == "Alpha"
        assert items[0]["author"] == "Ada"
        assert items[0]["is_read"] is True
        assert items[0]["summary"] == "Tasty"
        assert items[0]["published_at"] == "2026-09-10 12:00:00"


class TestStarredExportErrors:
    def test_unknown_format_400(self, env):
        with TestClient(app) as c:
            r = c.get("/api/reader/starred/export?format=xml")
        assert r.status_code == 400


class TestStarredExportMultiMode:
    @pytest.fixture
    def multi_env(self, env, monkeypatch):
        monkeypatch.setattr(settings, "MULTI", True)
        monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
        monkeypatch.setattr(settings, "STATE_SECRET", "s" * 40)
        monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
        with database.get_db() as db:
            db.execute(
                "INSERT INTO users (id, email, password_hash, plan) VALUES (?, ?, '', 'paid')",
                (12, "paid@example.com"),
            )
            db.execute(
                "INSERT INTO users (id, email, password_hash, plan) VALUES (?, ?, '', 'paid')",
                (13, "other@example.com"),
            )
            db.execute("UPDATE feeds SET user_id = 12")
            # A second tenant's starred item must never leak into user 12's export.
            db.execute(
                "INSERT INTO feeds (name, url, read_enabled, user_id)"
                " VALUES ('Other', 'https://example.com/other', 1, 13)"
            )
            db.execute(
                "INSERT INTO feed_items (feed_id, item_id, title, starred, published_at)"
                " VALUES (3, 'x1', 'Other User Post', 1, '2026-09-11 10:00:00')"
            )
        return settings

    def test_paid_user_exports(self, multi_env):
        with TestClient(app) as c:
            c.cookies.set("feedecho_session", security.sign_session(12, "paid@example.com"))
            r = c.get("/api/reader/starred/export?format=json")
        assert r.status_code == 200
        data = json.loads(r.text)
        assert data["count"] == 2
        assert all(it["feed"] != "Other" for it in data["items"])

    def test_non_reader_plan_402(self, multi_env):
        with database.get_db() as db:
            db.execute("UPDATE users SET plan = 'unknown-plan' WHERE id = 12")
        with TestClient(app) as c:
            c.cookies.set("feedecho_session", security.sign_session(12, "paid@example.com"))
            r = c.get("/api/reader/starred/export?format=json")
        assert r.status_code == 402


class TestStarredExportUI:
    def test_export_links_on_starred_view_only(self, env):
        with TestClient(app) as c:
            starred = c.get("/reader?view=starred").text
            all_view = c.get("/reader?view=all").text
        assert "/api/reader/starred/export?format=csv" in starred
        assert "/api/reader/starred/export?format=json" in starred
        assert "/api/reader/starred/export" not in all_view

    def test_export_links_hidden_during_search(self, env):
        # Export ignores the search query, so the links hide while one is active.
        with TestClient(app) as c:
            searched = c.get("/reader?view=starred&q=post").text
        assert "/api/reader/starred/export" not in searched


@requires_pg
@pytest.mark.pg
class TestStarredExportPg:
    """Dialect coverage: NULL-ordering rule + timestamp normalization on PG."""

    def test_json_roundtrip_orders_nulls_last(self, monkeypatch):
        from cryptography.fernet import Fernet

        monkeypatch.setattr(settings, "MULTI", True)
        monkeypatch.setattr(settings, "DATABASE_URL", TEST_PG_URL)
        monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", False)
        monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
        monkeypatch.setattr(settings, "STATE_SECRET", "s" * 40)
        monkeypatch.setattr(settings, "CREDENTIAL_KEY", Fernet.generate_key().decode())
        with database.get_db() as db:
            db.execute("DROP SCHEMA public CASCADE")
            db.execute("CREATE SCHEMA public")
            db.execute("GRANT ALL ON SCHEMA public TO public")
        database.init_db()
        with database.get_db() as db:
            db.execute(
                "INSERT INTO users (id, email, password_hash, plan)"
                " VALUES (12, 'paid@example.com', '', 'paid')"
            )
            db.execute(
                "INSERT INTO feeds (name, url, read_enabled, user_id)"
                " VALUES ('F', 'u', 1, 12)"
            )
            db.execute(
                "INSERT INTO feed_items (feed_id, item_id, title, starred, published_at)"
                " VALUES (1, 'a', 'Newest', 1, '2026-09-10 12:00:00')"
            )
            db.execute(
                "INSERT INTO feed_items (feed_id, item_id, title, starred, published_at)"
                " VALUES (1, 'b', 'Null Date', 1, NULL)"
            )
        with TestClient(app) as c:
            c.cookies.set("feedecho_session", security.sign_session(12, "paid@example.com"))
            r = c.get("/api/reader/starred/export?format=json")
        assert r.status_code == 200
        data = json.loads(r.text)
        assert data["count"] == 2
        # PG reads TIMESTAMP back as datetime; ORDER BY col IS NULL keeps NULLs
        # last on BOTH dialects; timestamp_str normalizes to the UTC string.
        assert data["items"][0]["title"] == "Newest"
        assert data["items"][0]["published_at"] == "2026-09-10 12:00:00"
        assert data["items"][1]["title"] == "Null Date"
        assert data["items"][1]["published_at"] == ""
