"""Phase 3: reader page + read/star actions (issue #11)."""

import pytest
from fastapi.testclient import TestClient

import database
import security
import settings
from app import app


@pytest.fixture
def single_env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MULTI", False)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "reader-single.db")
    database.init_db()
    import scheduler
    monkeypatch.setattr(scheduler, "fetch_feed", lambda url: {"items": []})
    with database.get_db() as db:
        db.execute(
            "INSERT INTO feeds (name, url, read_enabled) VALUES (?, ?, 1)",
            ("F", "https://example.com/feed"),
        )
        db.execute(
            "INSERT INTO feed_items (feed_id, item_id, title, is_read)"
            " VALUES (1, 'a', 'Item A', 0)"
        )
        db.execute(
            "INSERT INTO feed_items (feed_id, item_id, title, is_read)"
            " VALUES (1, 'b', 'Item B', 1)"
        )
    return settings


class TestReaderPageSingle:
    def test_renders_reader(self, single_env):
        with TestClient(app) as c:
            resp = c.get("/reader")
        assert resp.status_code == 200
        assert "Reader" in resp.text
        assert "Item A" in resp.text

    def test_unread_view_hides_read_items(self, single_env):
        with TestClient(app) as c:
            resp = c.get("/reader?view=unread")
        assert "Item A" in resp.text
        assert "Item B" not in resp.text

    def test_all_view_shows_everything(self, single_env):
        with TestClient(app) as c:
            resp = c.get("/reader?view=all")
        assert "Item A" in resp.text and "Item B" in resp.text

    def test_reader_hides_read_disabled_feeds(self, single_env):
        with database.get_db() as db:
            db.execute(
                "INSERT INTO feeds (name, url, read_enabled) VALUES (?, ?, 0)",
                ("Hidden", "https://example.com/hidden"),
            )
            db.execute(
                "INSERT INTO feed_items (feed_id, item_id, title) VALUES (2, 'h', 'Hidden Item')"
            )
        with TestClient(app) as c:
            resp = c.get("/reader?view=all")
        assert "Hidden Item" not in resp.text
        assert "Hidden" not in resp.text

    def test_toggle_read(self, single_env):
        with TestClient(app) as c:
            assert c.post("/api/reader/1/read").json()["is_read"] is True
            assert c.post("/api/reader/1/read").json()["is_read"] is False

    def test_toggle_star(self, single_env):
        with TestClient(app) as c:
            assert c.post("/api/reader/1/star").json()["starred"] is True

    def test_mark_all_read(self, single_env):
        with TestClient(app) as c:
            assert c.post("/api/reader/mark-all-read").status_code == 200
        with database.get_db() as db:
            unread = db.execute(
                "SELECT COUNT(*) AS c FROM feed_items WHERE is_read = 0"
            ).fetchone()["c"]
        assert unread == 0

    def test_reader_toggle(self, single_env):
        with TestClient(app) as c:
            assert c.post("/api/feeds/1/reader-toggle").json()["read_enabled"] is False
            assert c.post("/api/feeds/1/reader-toggle").json()["read_enabled"] is True


@pytest.fixture
def multi_env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MULTI", True)
    monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "reader-multi.db")
    database.init_db()
    import scheduler
    monkeypatch.setattr(scheduler, "fetch_feed", lambda url: {"items": []})
    with database.get_db() as db:
        for uid, email in ((11, "a@example.com"), (12, "b@example.com")):
            db.execute(
                "INSERT INTO users (id, email, password_hash, plan) VALUES (?, ?, '', 'paid')",
                (uid, email),
            )
        db.execute("INSERT INTO feeds (name, url, user_id) VALUES (?, ?, ?)", ("A feed", "u", 11))
        db.execute("INSERT INTO feeds (name, url, user_id) VALUES (?, ?, ?)", ("B feed", "u", 12))
        db.execute("INSERT INTO feed_items (feed_id, item_id, title) VALUES (1, 'a', 'A item')")
        db.execute("INSERT INTO feed_items (feed_id, item_id, title) VALUES (2, 'b', 'B item')")
    return settings


def _as(client, uid, email):
    client.cookies.set("feedecho_session", security.sign_session(uid, email))
    return client


class TestReaderScoping:
    def test_cannot_toggle_another_users_item(self, multi_env):
        with TestClient(app) as c:
            _as(c, 11, "a@example.com")
            assert c.post("/api/reader/2/read").status_code == 404
            assert c.post("/api/reader/2/star").status_code == 404

    def test_mark_all_read_scoped_to_owner(self, multi_env):
        with TestClient(app) as c:
            _as(c, 11, "a@example.com")
            assert c.post("/api/reader/mark-all-read").status_code == 200
        with database.get_db() as db:
            a = db.execute("SELECT is_read FROM feed_items WHERE id = 1").fetchone()["is_read"]
            b = db.execute("SELECT is_read FROM feed_items WHERE id = 2").fetchone()["is_read"]
        assert a == 1 and b == 0
