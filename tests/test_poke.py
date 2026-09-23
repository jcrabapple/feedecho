"""Per-feed poke endpoint (Tier 1-2).

A feed can expose a secret, unauthenticated poke URL. Hitting it triggers an
immediate fetch of that feed instead of waiting out the poll interval. The
owner reveals/regenerates the token through an authenticated endpoint; the
poke route itself never requires a session and never exposes the feed id.

The poke is throttled: a second poke within the cooldown window is a no-op,
so a leaked token can't be turned into a fetch hammer against the feed's
origin server.
"""

import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from database import get_db, init_db


@pytest.fixture
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        monkeypatch.setattr("database.DB_PATH", db_path)
        init_db()
        yield db_path


@pytest.fixture
def client(temp_db, monkeypatch):
    import app as app_module

    monkeypatch.setattr(app_module.settings, "AUTH_TOKEN", None)
    return TestClient(app_module.app)


def _seed(poke_token=None, last_poked_at=None):
    with get_db() as db:
        db.execute(
            "INSERT INTO feeds (name, url, poll_interval, poke_token, last_poked_at)"
            " VALUES (?, ?, ?, ?, ?)",
            ("Test Feed", "https://example.com/feed.xml", 15, poke_token, last_poked_at),
        )


def _get_feed():
    with get_db() as db:
        return db.execute("SELECT * FROM feeds WHERE id = 1").fetchone()


class TestPokeTokenManagement:
    def test_generates_token_on_first_request(self, client, temp_db):
        _seed()
        resp = client.post("/api/feeds/1/poke-token", data={})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        token = data["poke_token"]
        assert token and len(token) >= 32
        assert data["poke_url"].endswith(f"/poke/{token}")
        assert _get_feed()["poke_token"] == token

    def test_reveal_is_idempotent(self, client, temp_db):
        _seed()
        t1 = client.post("/api/feeds/1/poke-token", data={}).json()["poke_token"]
        t2 = client.post("/api/feeds/1/poke-token", data={}).json()["poke_token"]
        assert t1 == t2

    def test_regenerate_rotates_token(self, client, temp_db):
        _seed()
        t1 = client.post("/api/feeds/1/poke-token", data={}).json()["poke_token"]
        t2 = client.post(
            "/api/feeds/1/poke-token", data={"regenerate": "1"}
        ).json()["poke_token"]
        assert t1 != t2
        assert _get_feed()["poke_token"] == t2

    def test_unknown_feed_404(self, client, temp_db):
        resp = client.post("/api/feeds/999/poke-token", data={})
        assert resp.status_code == 404

    def test_deleted_feed_404(self, client, temp_db):
        _seed()
        client.post("/api/feeds/1/delete")
        resp = client.post("/api/feeds/1/poke-token", data={})
        assert resp.status_code == 404


class TestPokeEndpoint:
    def test_valid_token_triggers_fetch(self, client, temp_db, monkeypatch):
        import app as app_module

        calls = []
        monkeypatch.setattr(
            app_module, "check_feed", lambda feed_id: calls.append(feed_id)
        )
        _seed(poke_token="tok123")
        resp = client.get("/poke/tok123")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        assert calls == [1]

    def test_unknown_token_404(self, client, temp_db):
        _seed(poke_token="tok123")
        resp = client.get("/poke/nope")
        assert resp.status_code == 404

    def test_deleted_feed_token_404(self, client, temp_db, monkeypatch):
        import app as app_module

        monkeypatch.setattr(app_module, "check_feed", lambda feed_id: None)
        _seed(poke_token="tok123")
        client.post("/api/feeds/1/delete")
        resp = client.get("/poke/tok123")
        assert resp.status_code == 404

    def test_requires_no_session(self, client, temp_db, monkeypatch):
        # The poke route is the unauthenticated webhook: it must not redirect
        # or 401 when no user is logged in.
        import app as app_module

        monkeypatch.setattr(app_module, "check_feed", lambda feed_id: None)
        _seed(poke_token="tok123")
        resp = client.get("/poke/tok123", follow_redirects=False)
        assert resp.status_code == 200

    def test_recent_poke_is_throttled(self, client, temp_db, monkeypatch):
        import app as app_module

        calls = []
        monkeypatch.setattr(
            app_module, "check_feed", lambda feed_id: calls.append(feed_id)
        )
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        _seed(poke_token="tok123", last_poked_at=now)
        resp = client.get("/poke/tok123")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert resp.json().get("skipped") == "recently_poked"
        assert calls == [], "a poke inside the cooldown window must not refetch"

    def test_poke_reachable_when_shared_secret_configured(self, client, temp_db, monkeypatch):
        # A real single-mode deploy sets FEEDECHO_AUTH_TOKEN; the poke route is
        # the one unauthenticated webhook and must stay reachable WITHOUT that
        # shared secret (it's token-gated by the poke token instead).
        import app as app_module

        monkeypatch.setattr(app_module, "check_feed", lambda feed_id: None)
        monkeypatch.setattr(app_module.settings, "AUTH_TOKEN", "sekret-token")
        _seed(poke_token="tok123")
        resp = client.get("/poke/tok123", follow_redirects=False)
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_poke_paused_feed_is_skipped(self, client, temp_db, monkeypatch):
        import app as app_module

        calls = []
        monkeypatch.setattr(
            app_module, "check_feed", lambda feed_id: calls.append(feed_id)
        )
        with get_db() as db:
            db.execute(
                "INSERT INTO feeds (name, url, poll_interval, poke_token, paused)"
                " VALUES (?, ?, ?, ?, 1)",
                ("Paused Feed", "https://example.com/feed.xml", 15, "tok-paused"),
            )
        resp = client.get("/poke/tok-paused")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "skipped": "feed_paused"}
        assert calls == [], "a paused feed must not be fetched on poke"

    def test_poke_accepts_post_method_and_sets_robots_header(self, client, temp_db, monkeypatch):
        import app as app_module

        calls = []
        monkeypatch.setattr(
            app_module, "check_feed", lambda feed_id: calls.append(feed_id)
        )
        _seed(poke_token="tok-post")
        resp = client.post("/poke/tok-post")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        assert resp.headers.get("x-robots-tag") == "noindex, nofollow"
        assert calls == [1]

    def test_poke_token_tenant_scoped(self, client, temp_db, monkeypatch):
        import app as app_module

        monkeypatch.setattr(app_module.settings, "MULTI", True)
        monkeypatch.setattr(app_module.AuthMiddleware, "_session_user", staticmethod(lambda req: 2))
        with get_db() as db:
            db.execute(
                "INSERT INTO feeds (name, url, user_id, poke_token)"
                " VALUES (?, ?, 1, 'tok-u1')",
                ("User1 Feed", "https://example.com/u1.xml"),
            )
        # User 2 tries to reveal or regenerate User 1's poke token:
        resp = client.post("/api/feeds/1/poke-token", data={})
        assert resp.status_code == 404

    def test_idx_feeds_poke_token_created(self, temp_db):
        with get_db() as db:
            indexes = {
                row["name"]
                for row in db.execute("PRAGMA index_list('feeds')").fetchall()
            }
        assert "idx_feeds_poke_token" in indexes

