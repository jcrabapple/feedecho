"""Wave-2 integration tests: delivery modes (queue/review), import/export
round-trip, draft rows on the queue page, and the F4 discovery API."""

import json

import pytest
from fastapi.testclient import TestClient

import app as app_module
import database
import import_export
import settings
from app import app


@pytest.fixture
def queue_env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MULTI", True)
    monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
    monkeypatch.setattr(settings, "STATE_SECRET", "s" * 40)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "modes.db")
    database.init_db()
    with database.get_db() as db:
        db.execute("INSERT INTO users (id, email, password_hash, plan) VALUES (101, 'u1@example.com', '', 'paid')")
        db.execute("INSERT INTO accounts (id, name, username, instance, access_token, user_id) VALUES (1, 'Mastodon main', 'm1', 'https://mastodon.social', 'tok', 101)")
        db.execute("INSERT INTO feeds (id, name, url, read_enabled, user_id) VALUES (1, 'Feed 1', 'https://example.com/feed1', 1, 101)")
    return settings


def _client():
    c = TestClient(app)
    c.cookies.set("feedecho_session", __import__("security").sign_session(101, "u1@example.com"))
    return c


def _echo_form(mode):
    return {
        "feed_id": "1",
        "destination_type": "mastodon",
        "account_id": "1",
        "template": "{{ title }} {{ link }}",
        "delivery_mode": mode,
    }


def _last_echo():
    with database.get_db() as db:
        return db.execute("SELECT * FROM echoes ORDER BY id DESC LIMIT 1").fetchone()


class TestDeliveryModeRoutes:
    def test_add_echo_queue_mode(self, queue_env):
        client = _client()
        resp = client.post("/api/echoes", data=_echo_form("queue"), follow_redirects=False)
        assert resp.status_code == 303
        assert _last_echo()["delivery_mode"] == "queue"

    def test_add_echo_review_mode(self, queue_env):
        client = _client()
        resp = client.post("/api/echoes", data=_echo_form("review"), follow_redirects=False)
        assert resp.status_code == 303
        assert _last_echo()["delivery_mode"] == "review"

    def test_edit_echo_queue_mode(self, queue_env):
        client = _client()
        assert client.post("/api/echoes", data=_echo_form("instant"), follow_redirects=False).status_code == 303
        echo = _last_echo()
        resp = client.post(
            f"/api/echoes/{echo['id']}/edit",
            data=_echo_form("review"),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert _last_echo()["delivery_mode"] == "review"

    def test_digest_on_mastodon_still_rejected(self, queue_env):
        client = _client()
        resp = client.post("/api/echoes", data=_echo_form("digest"), follow_redirects=False)
        assert resp.status_code == 400
        assert "email" in resp.json()["detail"]


class TestDraftRowsOnQueuePage:
    def _draft_row(self):
        with database.get_db() as db:
            return db.execute(
                """
                INSERT INTO queued_posts (
                    user_id, feed_item_id, item_id, feed_id, destination_type, destination_id,
                    content, scheduled_at, status
                ) VALUES (101, 1, 'item-1', 1, 'mastodon', 1, 'Draft awaiting review', ?, 'draft')
                RETURNING id
                """,
                ("2026-10-18 12:00:00",),
            ).fetchone()["id"]

    def test_queue_page_shows_draft(self, queue_env):
        qp_id = self._draft_row()
        client = _client()
        page = client.get("/queue")
        assert page.status_code == 200
        assert "Draft awaiting review" in page.text
        assert "draft" in page.text

    def test_queue_cancel_accepts_draft(self, queue_env):
        qp_id = self._draft_row()
        client = _client()
        resp = client.post(f"/api/queue/{qp_id}/cancel", follow_redirects=False)
        assert resp.status_code == 303
        with database.get_db() as db:
            row = db.execute("SELECT status FROM queued_posts WHERE id = ?", (qp_id,)).fetchone()
        assert row["status"] == "cancelled"

    def test_queue_edit_accepts_draft(self, queue_env):
        qp_id = self._draft_row()
        client = _client()
        resp = client.post(
            f"/api/queue/{qp_id}/edit",
            data={"content": "Edited draft", "scheduled_at": "2026-10-18 13:00:00"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        with database.get_db() as db:
            row = db.execute("SELECT content, status FROM queued_posts WHERE id = ?", (qp_id,)).fetchone()
        assert row["content"] == "Edited draft"
        assert row["status"] == "draft"

    def test_delete_feed_finalizes_drafts(self, queue_env):
        qp_id = self._draft_row()
        client = _client()
        resp = client.post("/api/feeds/1/delete")
        assert resp.status_code == 200
        with database.get_db() as db:
            row = db.execute("SELECT status, error_message FROM queued_posts WHERE id = ?", (qp_id,)).fetchone()
        assert row["status"] == "failed"
        assert row["error_message"] == "Feed deleted"


class TestImportExportQueueMode:
    def test_round_trip_preserves_queue_mode(self, queue_env, monkeypatch, tmp_path):
        client = _client()
        assert client.post("/api/echoes", data=_echo_form("queue"), follow_redirects=False).status_code == 303
        with database.get_db() as db:
            payload = import_export.build_export(db, 101)

        # Import into a fresh database and confirm the mode survives.
        monkeypatch.setattr(database, "DB_PATH", tmp_path / "dst.db")
        database.init_db()
        with database.get_db() as db:
            db.execute("INSERT INTO users (id, email, password_hash, plan) VALUES (201, 'dst@example.com', '', 'paid')")
            summary = import_export.import_data(db, 201, payload)
        assert summary["added_echoes"] == 1
        with database.get_db() as db:
            row = db.execute("SELECT delivery_mode FROM echoes ORDER BY id DESC LIMIT 1").fetchone()
        assert row["delivery_mode"] == "queue"


class TestDiscoverApi:
    def test_discover_returns_shape(self, queue_env, monkeypatch):
        monkeypatch.setattr(
            app_module,
            "discover_feed_url",
            lambda url: {"found": True, "url": "https://example.com/feed.xml", "title": "Example", "alternates": ["https://example.com/feed.xml"]},
        )
        client = _client()
        resp = client.get("/api/feeds/discover", params={"url": "https://example.com/blog"})
        assert resp.status_code == 200
        data = resp.json()
        assert data == {
            "found": True,
            "url": "https://example.com/feed.xml",
            "title": "Example",
            "alternates": ["https://example.com/feed.xml"],
        }

    def test_discover_network_error_is_400(self, queue_env, monkeypatch):
        import httpx

        def boom(url):
            raise httpx.ConnectError("nope")

        monkeypatch.setattr(app_module, "discover_feed_url", boom)
        client = _client()
        resp = client.get("/api/feeds/discover", params={"url": "https://example.com/blog"})
        assert resp.status_code == 400
        assert "Could not discover feeds" in resp.json()["detail"]


class TestPageRenders:
    def test_echoes_page_renders_new_badges_and_hint(self, queue_env):
        client = _client()
        assert client.post("/api/echoes", data=_echo_form("review"), follow_redirects=False).status_code == 303
        page = client.get("/echoes")
        assert page.status_code == 200
        assert "REVIEW" in page.text
        assert "content:" in page.text

    def test_howto_renders(self, queue_env):
        client = _client()
        page = client.get("/howto")
        assert page.status_code == 200
        assert "Auto-queue" in page.text


class TestFeedsPageBookmarklet:
    def test_bookmarklet_details_present(self, queue_env):
        client = _client()
        page = client.get("/feeds")
        assert page.status_code == 200
        assert "bookmarklet-details" in page.text
        assert "Add from any page" in page.text
        assert "+ Subscribe" in page.text
        assert "?add=" in page.text

    def test_add_deep_link_prefills_form(self, queue_env):
        client = _client()
        page = client.get("/feeds", params={"add": "https://example.com/blog", "name": "Blog"})
        assert page.status_code == 200
        assert 'data-add-url="https://example.com/blog"' in page.text
        assert 'data-add-name="Blog"' in page.text
