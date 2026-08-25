"""Soft-deletion preserves cross-post history (issue #2).

Deleting a feed (or an echo) must not wipe posted_items/digest_items —
those rows are the immutable audit trail of what was cross-posted.
"""

import tempfile
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

    # Shared-secret auth must not leak in from the ambient environment —
    # otherwise a FEEDCHO_AUTH_TOKEN in the shell breaks every request here.
    monkeypatch.setattr(app_module.settings, "AUTH_TOKEN", None)
    return TestClient(app_module.app)


def _seed():
    with get_db() as db:
        db.execute(
            "INSERT INTO feeds (name, url) VALUES (?, ?)",
            ("Test Feed", "https://example.com/feed.xml"),
        )
        db.execute(
            "INSERT INTO accounts (name, username, instance, access_token) "
            "VALUES (?, ?, ?, ?)",
            ("Test", "test", "https://example.com", "token"),
        )
        db.execute(
            "INSERT INTO echoes (feed_id, destination_type, destination_id, template) "
            "VALUES (?, ?, ?, ?)",
            (1, "mastodon", 1, "{{ title }}"),
        )
        db.execute(
            "INSERT INTO posted_items (echo_id, item_id, item_title, item_url, status) "
            "VALUES (?, ?, ?, ?, ?)",
            (1, "item-1", "First Post", "https://example.com/1", "success"),
        )
        db.execute(
            "INSERT INTO digest_items "
            "(echo_id, item_id, item_title, item_url, rendered_content) "
            "VALUES (?, ?, ?, ?, ?)",
            (1, "item-2", "Digest Item", "https://example.com/2", "content"),
        )


class TestFeedSoftDelete:
    def test_feed_delete_preserves_history(self, client, temp_db):
        _seed()
        resp = client.post("/api/feeds/1/delete")
        assert resp.status_code == 200
        with get_db() as db:
            feed = db.execute("SELECT * FROM feeds WHERE id = 1").fetchone()
            assert feed is not None
            assert feed["deleted_at"] is not None
            echoes = db.execute(
                "SELECT COUNT(*) as c FROM echoes WHERE feed_id = 1"
            ).fetchone()["c"]
            posts = db.execute("SELECT COUNT(*) as c FROM posted_items").fetchone()["c"]
            digests = db.execute(
                "SELECT COUNT(*) as c FROM digest_items"
            ).fetchone()["c"]
            assert echoes == 1
            assert posts == 1
            assert digests == 1

    def test_deleted_feed_hidden_from_feeds_but_history_remains(self, client, temp_db):
        _seed()
        client.post("/api/feeds/1/delete")
        feeds_page = client.get("/feeds").text
        assert "Test Feed" not in feeds_page
        history = client.get("/history").text
        assert "First Post" in history

    def test_feed_delete_is_idempotent(self, client, temp_db):
        _seed()
        client.post("/api/feeds/1/delete")
        client.post("/api/feeds/1/delete")
        with get_db() as db:
            posts = db.execute("SELECT COUNT(*) as c FROM posted_items").fetchone()["c"]
            assert posts == 1

    def test_actions_on_deleted_feed_404(self, client, temp_db):
        _seed()
        client.post("/api/feeds/1/delete")
        assert client.post("/api/feeds/1/pause").status_code == 404
        assert client.post("/api/feeds/1/test").status_code == 404
        assert client.post("/api/feeds/1/init").status_code == 404

    def test_scheduler_skips_deleted_feed(self, client, temp_db, monkeypatch):
        _seed()
        client.post("/api/feeds/1/delete")
        from scheduler import check_all_feeds

        called = []
        monkeypatch.setattr("scheduler.check_feed", lambda feed_id: called.append(feed_id))
        check_all_feeds()
        assert called == []

    def test_flush_digests_discards_items_for_a_deleted_feed(self, client, temp_db):
        """A deleted feed's queued digest items are finalized, not sent, and not
        kept forever.

        Previously flush_digests simply could not see them (its query INNER
        JOINs on a live feed and enabled echo), so the rows sat in digest_items
        indefinitely with their posted_items stuck at 'queued'. The drip path
        always discarded a deleted/disabled echo's backlog with an audit trail;
        digests now behave the same way. What must never happen is sending
        items from a feed the user deleted.
        """
        _seed()
        with get_db() as db:
            db.execute(
                "INSERT INTO email_accounts (name, email) VALUES (?, ?)",
                ("Digest User", "digest@example.com"),
            )
            db.execute(
                "UPDATE echoes SET destination_type = 'email', "
                "destination_id = 1, delivery_mode = 'digest' WHERE id = 1",
            )
            db.execute(
                "INSERT INTO posted_items (echo_id, item_id, status)"
                " VALUES (1, 'digest-item-1', 'queued')"
            )
        client.post("/api/feeds/1/delete")
        from scheduler import flush_digests

        flush_digests()
        with get_db() as db:
            assert (
                db.execute("SELECT COUNT(*) as c FROM digest_items").fetchone()["c"] == 0
            ), "queued digest rows for a deleted feed must not be retained"
            row = db.execute(
                "SELECT status, error_message FROM posted_items WHERE item_id = 'digest-item-1'"
            ).fetchone()
        # Finalized with a reason, so history explains it instead of showing a
        # row stuck at 'queued' forever.
        assert row["status"] == "gave_up"
        assert "no longer active" in row["error_message"]


class TestEchoSoftDelete:
    def test_echo_delete_preserves_posted_items(self, client, temp_db):
        _seed()
        resp = client.post("/api/echoes/1/delete")
        assert resp.status_code == 200
        with get_db() as db:
            echo = db.execute("SELECT * FROM echoes WHERE id = 1").fetchone()
            assert echo is not None
            assert echo["deleted_at"] is not None
            assert echo["enabled"] == 0
            posts = db.execute("SELECT COUNT(*) as c FROM posted_items").fetchone()["c"]
            digests = db.execute(
                "SELECT COUNT(*) as c FROM digest_items"
            ).fetchone()["c"]
            assert posts == 1
            assert digests == 1

    def test_deleted_echo_hidden_from_listings(self, client, temp_db):
        _seed()
        client.post("/api/echoes/1/delete")
        client.post("/api/feeds/1/delete")
        echoes_page = client.get("/echoes").text
        assert "Test Feed" not in echoes_page
        # History survives both deletions.
        assert "First Post" in client.get("/history").text

    def test_deleted_echo_cannot_toggle_or_edit(self, client, temp_db):
        _seed()
        client.post("/api/echoes/1/delete")
        assert client.post("/api/echoes/1/toggle").status_code == 404
        assert client.post(
            "/api/echoes/1/edit",
            data={"feed_id": 1, "destination_type": "mastodon", "account_id": 1},
        ).status_code == 404
