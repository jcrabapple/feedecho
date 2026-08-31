"""Phase 4: Shout — one-time echo of a reader item (issue #11)."""

import pytest
from fastapi.testclient import TestClient

import database
import scheduler
import security
import settings
from app import app


@pytest.fixture
def single_env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MULTI", False)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "shout-single.db")
    database.init_db()
    monkeypatch.setattr(scheduler, "check_all_feeds", lambda: None)
    with database.get_db() as db:
        db.execute(
            "INSERT INTO accounts (name, username, instance, access_token)"
            " VALUES (?, ?, ?, ?)",
            ("main", "user", "https://mastodon.social", "tok"),
        )
        db.execute("INSERT INTO feeds (name, url) VALUES (?, ?)", ("F", "https://example.com/feed"))
        db.execute(
            "INSERT INTO feed_items (feed_id, item_id, title, link, summary, content, author, published_at)"
            " VALUES (1, 'a', 'Hello', 'https://example.com/a', 'sum', 'body', 'Auth', '2026-08-31 12:00:00')"
        )
    return settings


class TestShout:
    def test_shout_posts_once_and_records_history(self, single_env, monkeypatch):
        posted = []
        monkeypatch.setattr(
            scheduler, "post_status",
            lambda **kw: posted.append(kw["content"]) or {"id": "p1"},
        )
        with TestClient(app) as c:
            r = c.post(
                "/api/reader/1/shout",
                data={"destination": "mastodon:1", "template": "{{ title }} {{ link }}"},
            )
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is True
        assert data["status"] == "success"
        assert posted == ["Hello https://example.com/a"]
        with database.get_db() as db:
            echo = db.execute("SELECT * FROM echoes WHERE one_shot = 1").fetchone()
            assert echo is not None
            assert echo["enabled"] == 0
            assert echo["deleted_at"] is not None
            pi = db.execute(
                "SELECT status, item_title FROM posted_items WHERE echo_id = ?",
                (echo["id"],),
            ).fetchone()
            assert pi["status"] == "success"
            assert pi["item_title"] == "Hello"

    def test_one_shot_echo_hidden_from_listing(self, single_env, monkeypatch):
        monkeypatch.setattr(scheduler, "post_status", lambda **kw: {"id": "p1"})
        with TestClient(app) as c:
            c.post("/api/reader/1/shout", data={"destination": "mastodon:1"})
        with database.get_db() as db:
            # /echoes and the scheduler both filter these out: soft-deleted AND disabled.
            active = db.execute(
                "SELECT COUNT(*) AS c FROM echoes WHERE deleted_at IS NULL"
            ).fetchone()["c"]
            oneshots = db.execute(
                "SELECT COUNT(*) AS c FROM echoes WHERE one_shot = 1"
            ).fetchone()["c"]
        assert active == 0
        assert oneshots == 1

    def test_invalid_destination_is_400(self, single_env, monkeypatch):
        monkeypatch.setattr(scheduler, "post_status", lambda **kw: {"id": "p1"})
        with TestClient(app) as c:
            assert c.post("/api/reader/1/shout", data={"destination": "bogus"}).status_code == 400
            assert c.post("/api/reader/1/shout", data={"destination": "mastodon:notanint"}).status_code == 400

    def test_missing_item_is_404(self, single_env, monkeypatch):
        monkeypatch.setattr(scheduler, "post_status", lambda **kw: {"id": "p1"})
        with TestClient(app) as c:
            assert c.post("/api/reader/999/shout", data={"destination": "mastodon:1"}).status_code == 404


@pytest.fixture
def multi_env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MULTI", True)
    monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "shout-multi.db")
    database.init_db()
    monkeypatch.setattr(scheduler, "check_all_feeds", lambda: None)
    with database.get_db() as db:
        for uid, email in ((11, "a@example.com"), (12, "b@example.com")):
            db.execute(
                "INSERT INTO users (id, email, password_hash, plan) VALUES (?, ?, '', 'paid')",
                (uid, email),
            )
        db.execute(
            "INSERT INTO accounts (name, username, instance, access_token, user_id)"
            " VALUES (?, ?, ?, ?, ?)",
            ("A account", "a", "https://mastodon.social", "tok", 11),
        )
        db.execute("INSERT INTO feeds (name, url, user_id) VALUES (?, ?, ?)", ("A feed", "u", 11))
        db.execute("INSERT INTO feeds (name, url, user_id) VALUES (?, ?, ?)", ("B feed", "u", 12))
        db.execute(
            "INSERT INTO feed_items (feed_id, item_id, title) VALUES (1, 'a', 'A item')"
        )
        db.execute(
            "INSERT INTO feed_items (feed_id, item_id, title) VALUES (2, 'b', 'B item')"
        )
    return settings


def _as(client, uid, email):
    client.cookies.set("feedecho_session", security.sign_session(uid, email))
    return client


class TestShoutScoping:
    def test_cannot_shout_another_users_item(self, multi_env, monkeypatch):
        monkeypatch.setattr(scheduler, "post_status", lambda **kw: {"id": "p1"})
        with TestClient(app) as c:
            _as(c, 12, "b@example.com")
            # item 1 belongs to user 11's feed
            resp = c.post("/api/reader/1/shout", data={"destination": "mastodon:1"})
        assert resp.status_code == 404

    def test_cannot_shout_to_another_users_account(self, multi_env, monkeypatch):
        monkeypatch.setattr(scheduler, "post_status", lambda **kw: {"id": "p1"})
        with TestClient(app) as c:
            _as(c, 12, "b@example.com")
            # user 12 shouts their OWN item (id 2) but into user 11's account
            resp = c.post("/api/reader/2/shout", data={"destination": "mastodon:1"})
        assert resp.status_code == 404
