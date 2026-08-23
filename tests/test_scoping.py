"""Cross-tenant isolation tests: user A's data must be invisible and
immutable to user B in multi mode."""

import pytest
from fastapi.testclient import TestClient

import auth
import database
import security
import settings
from app import app

A_ID, B_ID = 2, 3


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MULTI", True)
    monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "multi.db")
    database.init_db()
    auth._login_attempts.clear()
    with database.get_db() as db:
        db.execute(
            "INSERT INTO users (id, email, password_hash) VALUES (?, ?, '')",
            (A_ID, "a@example.com"),
        )
        db.execute(
            "INSERT INTO users (id, email, password_hash) VALUES (?, ?, '')",
            (B_ID, "b@example.com"),
        )
    return settings


def _client(env, user_id, email):
    c = TestClient(app)
    c.cookies.set("feedecho_session", security.sign_session(user_id, email))
    return c


@pytest.fixture
def client_a(env):
    with _client(env, A_ID, "a@example.com") as c:
        yield c


@pytest.fixture
def client_b(env):
    with _client(env, B_ID, "b@example.com") as c:
        yield c


def _add_feed(client, name="My Feed"):
    return client.post(
        "/api/feeds",
        data={"name": name, "url": "https://example.com/feed.xml"},
        follow_redirects=False,
    )


def _seed_feed_for_a(env):
    with database.get_db() as db:
        db.execute(
            "INSERT INTO feeds (id, name, url, user_id) VALUES (1, 'A feed', 'https://example.com/a', ?)",
            (A_ID,),
        )


class TestFeedIsolation:
    def test_a_sees_own_feed_b_does_not(self, env, client_a, client_b):
        _add_feed(client_a, "A private feed")
        assert "A private feed" in client_a.get("/feeds").text
        assert "A private feed" not in client_b.get("/feeds").text

    def test_b_cannot_delete_as_feed(self, env, client_a, client_b):
        _seed_feed_for_a(env)
        client_b.post("/api/feeds/1/delete")
        with database.get_db() as db:
            row = db.execute(
                "SELECT deleted_at FROM feeds WHERE id = 1"
            ).fetchone()
        assert row["deleted_at"] is None

    def test_b_cannot_pause_as_feed(self, env, client_b):
        _seed_feed_for_a(env)
        resp = client_b.post("/api/feeds/1/pause")
        assert resp.status_code == 404

    def test_b_cannot_fetch_as_feed(self, env, client_b):
        _seed_feed_for_a(env)
        assert client_b.post("/api/feeds/1/fetch").status_code == 404


class TestEchoIsolation:
    def test_b_cannot_attach_echo_to_as_feed(self, env, client_b):
        _seed_feed_for_a(env)
        resp = client_b.post(
            "/api/echoes",
            data={
                "feed_id": 1,
                "destination_type": "email",
                "email_account_id": 999,
            },
        )
        # Feed ownership check fires before destination resolution matters
        assert resp.status_code == 404

    def test_b_cannot_attach_echo_to_as_destination(self, env, client_b):
        _seed_feed_for_a(env)
        # Give B a feed, but the email destination belongs to A.
        with database.get_db() as db:
            db.execute(
                "INSERT INTO feeds (id, name, url, user_id) VALUES (2, 'B feed', 'https://example.com/b', ?)",
                (B_ID,),
            )
            db.execute(
                "INSERT INTO email_accounts (id, name, email, user_id) VALUES (1, 'A email', 'a@example.com', ?)",
                (A_ID,),
            )
        resp = client_b.post(
            "/api/echoes",
            data={
                "feed_id": 2,
                "destination_type": "email",
                "email_account_id": 1,
            },
        )
        assert resp.status_code == 404

    def test_b_cannot_toggle_as_echo(self, env, client_b):
        _seed_feed_for_a(env)
        with database.get_db() as db:
            db.execute(
                "INSERT INTO echoes (id, feed_id, destination_type, destination_id, user_id) VALUES (1, 1, 'email', 1, ?)",
                (A_ID,),
            )
        assert client_b.post("/api/echoes/1/toggle").status_code == 404

    def test_b_cannot_delete_as_echo(self, env, client_b):
        _seed_feed_for_a(env)
        with database.get_db() as db:
            db.execute(
                "INSERT INTO echoes (id, feed_id, destination_type, destination_id, user_id) VALUES (1, 1, 'email', 1, ?)",
                (A_ID,),
            )
        client_b.post("/api/echoes/1/delete")
        with database.get_db() as db:
            row = db.execute("SELECT deleted_at FROM echoes WHERE id = 1").fetchone()
        assert row["deleted_at"] is None


class TestHistoryIsolation:
    def test_b_cannot_retry_as_posted_item(self, env, client_b):
        _seed_feed_for_a(env)
        with database.get_db() as db:
            db.execute(
                "INSERT INTO echoes (id, feed_id, destination_type, destination_id, user_id) VALUES (1, 1, 'email', 1, ?)",
                (A_ID,),
            )
            db.execute(
                "INSERT INTO posted_items (id, echo_id, item_id, status) VALUES (1, 1, 'i1', 'failed')"
            )
        assert client_b.post("/api/history/1/retry").status_code == 404


class TestSettingsIsolation:
    def test_smtp_settings_are_per_user(self, env, client_a, client_b):
        client_a.post(
            "/api/settings/smtp",
            data={
                "smtp_host": "smtp.a.example.com",
                "smtp_port": 587,
                "smtp_from_email": "a@example.com",
                "smtp_from_name": "A",
                "smtp_use_tls": "1",
            },
        )
        # B's settings page does not see A's SMTP host
        assert "smtp.a.example.com" not in client_b.get("/settings").text
        with database.get_db() as db:
            rows = db.execute(
                "SELECT user_id, key, value FROM settings WHERE key = 'smtp_host'"
            ).fetchall()
        assert [(r["user_id"], r["value"]) for r in rows] == [
            (A_ID, "smtp.a.example.com")
        ]
