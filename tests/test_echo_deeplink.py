"""Phase 5: Echo deep-link from the reader (issue #11)."""

import pytest
from fastapi.testclient import TestClient

import database
import scheduler
import settings
from app import app


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MULTI", False)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "deeplink.db")
    database.init_db()
    monkeypatch.setattr(scheduler, "check_all_feeds", lambda: None)
    with database.get_db() as db:
        db.execute(
            "INSERT INTO accounts (name, username, instance, access_token)"
            " VALUES (?, ?, ?, ?)",
            ("main", "user", "https://mastodon.social", "tok"),
        )
        db.execute("INSERT INTO feeds (name, url, read_enabled) VALUES (?, ?, 1)", ("F", "https://example.com/feed"))
        db.execute("INSERT INTO feed_items (feed_id, item_id, title) VALUES (1, 'a', 'Item A')")
    return settings


class TestEchoDeepLink:
    def test_echoes_page_preselects_feed(self, env):
        with TestClient(app) as c:
            page = c.get("/echoes?feed=1&from_=reader").text
        assert 'name="return_to" value="/reader"' in page
        assert '<option value="1" selected>' in page

    def test_add_echo_redirects_to_reader(self, env):
        with TestClient(app) as c:
            r = c.post(
                "/api/echoes",
                data={"feed_id": "1", "destination_type": "mastodon",
                      "account_id": "1", "return_to": "/reader"},
                follow_redirects=False,
            )
        assert r.status_code == 303
        assert r.headers["location"] == "/reader"

    def test_add_echo_default_return_is_echoes(self, env):
        with TestClient(app) as c:
            r = c.post(
                "/api/echoes",
                data={"feed_id": "1", "destination_type": "mastodon", "account_id": "1"},
                follow_redirects=False,
            )
        assert r.status_code == 303
        assert r.headers["location"] == "/echoes"

    def test_add_echo_rejects_open_redirect(self, env):
        with TestClient(app) as c:
            for redirect_target in ("//evil.com", "/\\evil.com", "https://evil.com"):
                r = c.post(
                    "/api/echoes",
                    data={"feed_id": "1", "destination_type": "mastodon",
                          "account_id": "1", "return_to": redirect_target},
                    follow_redirects=False,
                )
                assert r.status_code == 303
                assert r.headers["location"] == "/echoes"

    def test_reader_shows_echo_link(self, env):
        with TestClient(app) as c:
            page = c.get("/reader").text
        assert "/echoes?feed=1&from_=reader" in page
