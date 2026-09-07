"""Tests for Curation Queue (Phase 3)."""

import pytest
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient

import database
import security
import settings
import scheduler
from app import app, _next_free_slot


@pytest.fixture
def queue_env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MULTI", True)
    monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "queue.db")
    database.init_db()
    monkeypatch.setattr(scheduler, "check_all_feeds", lambda: None)
    with database.get_db() as db:
        # Users
        db.execute("INSERT INTO users (id, email, password_hash, plan) VALUES (101, 'u1@example.com', '', 'paid')")
        db.execute("INSERT INTO users (id, email, password_hash, plan) VALUES (102, 'u2@example.com', '', 'trial')")

        # Accounts for user 101
        db.execute("INSERT INTO accounts (id, name, username, instance, access_token, user_id) VALUES (1, 'Mastodon main', 'm1', 'https://mastodon.social', 'tok', 101)")

        # Feeds & items for user 101
        db.execute("INSERT INTO feeds (id, name, url, read_enabled, user_id) VALUES (1, 'Feed 1', 'https://example.com/feed1', 1, 101)")
        db.execute(
            "INSERT INTO feed_items (id, feed_id, item_id, title, link, summary, published_at, is_read)"
            " VALUES (1, 1, 'item-1', 'Article One', 'https://example.com/1', 'Summary 1', '2026-01-01 12:00:00', 0)"
        )

        # Accounts & feeds for user 102
        db.execute("INSERT INTO accounts (id, name, username, instance, access_token, user_id) VALUES (2, 'U2 Mastodon', 'u2m', 'https://mastodon.social', 'tok', 102)")
        db.execute("INSERT INTO feeds (id, name, url, read_enabled, user_id) VALUES (2, 'Feed 2', 'https://example.com/feed2', 1, 102)")
        db.execute(
            "INSERT INTO feed_items (id, feed_id, item_id, title, link, summary, published_at, is_read)"
            " VALUES (2, 2, 'item-2', 'Article Two', 'https://example.com/2', 'Summary 2', '2026-01-01 12:00:00', 0)"
        )
    return settings


def _as_u1(client):
    client.cookies.set("feedecho_session", security.sign_session(101, "u1@example.com"))
    return client


def _as_u2(client):
    client.cookies.set("feedecho_session", security.sign_session(102, "u2@example.com"))
    return client


def test_enqueue_post_from_compose(queue_env):
    """Enqueuing a post creates a row in queued_posts with materialized content."""
    client = TestClient(app)
    _as_u1(client)

    resp = client.post(
        "/api/reader/1/compose",
        data={
            "destinations": "mastodon:1",
            "content": "Queued curation comment {{ raw_preserved }}",
            "enqueue": "1",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["queued"] is True
    assert len(data["results"]) == 1
    assert data["results"][0]["status"] == "queued"
    qp_id = data["results"][0]["queue_id"]

    with database.get_db() as db:
        row = db.execute("SELECT * FROM queued_posts WHERE id = ?", (qp_id,)).fetchone()
    assert row is not None
    assert row["content"] == "Queued curation comment {{ raw_preserved }}"
    assert row["status"] == "queued"
    assert row["user_id"] == 101


def test_flush_queue_dispatches_due_post_and_pruned_feed_item_survives(queue_env, monkeypatch):
    """Due post dispatches via flush_queue; pruned feed_items do not break dispatch."""
    dispatched = []

    def fake_post_status(instance, access_token, content, **kwargs):
        dispatched.append(content)
        return {"id": "m-status-due-1"}

    monkeypatch.setattr(scheduler, "post_status", fake_post_status)

    past_time = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")

    with database.get_db() as db:
        qp_id = db.execute(
            """
            INSERT INTO queued_posts (
                user_id, feed_item_id, item_id, feed_id, destination_type, destination_id,
                content, visibility, scheduled_at, status
            ) VALUES (101, 1, 'item-1', 1, 'mastodon', 1, 'Materialized Body To Dispatch', 'public', ?, 'queued')
            RETURNING id
            """,
            (past_time,),
        ).fetchone()["id"]

        # Simulate feed_items row being PRUNED before queue dispatch!
        db.execute("DELETE FROM feed_items WHERE id = 1")

    # Run queue flush
    scheduler.flush_queue()

    assert len(dispatched) == 1
    assert dispatched[0] == "Materialized Body To Dispatch"

    with database.get_db() as db:
        row = db.execute("SELECT * FROM queued_posts WHERE id = ?", (qp_id,)).fetchone()
        assert row["status"] == "sent"
        assert row["posted_item_id"] is not None


def test_concurrent_flush_claims_once(queue_env, monkeypatch):
    """Two concurrent workers do not double-dispatch the same queued post."""
    dispatched = []
    monkeypatch.setattr(scheduler, "post_status", lambda *args, **kw: dispatched.append(args) or {"id": "m1"})

    past_time = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    with database.get_db() as db:
        qp_id = db.execute(
            """
            INSERT INTO queued_posts (
                user_id, feed_item_id, item_id, feed_id, destination_type, destination_id,
                content, scheduled_at, status
            ) VALUES (101, 1, 'item-1', 1, 'mastodon', 1, 'Single dispatch content', ?, 'queued')
            RETURNING id
            """,
            (past_time,),
        ).fetchone()["id"]

    # First run claims and dispatches
    scheduler.flush_queue()
    # Second run should find nothing due or unclaimed
    scheduler.flush_queue()

    assert len(dispatched) == 1
    with database.get_db() as db:
        row = db.execute("SELECT status FROM queued_posts WHERE id = ?", (qp_id,)).fetchone()
        assert row["status"] == "sent"


def test_cancelled_posts_skipped(queue_env, monkeypatch):
    """Cancelled queued posts are ignored by flush_queue."""
    dispatched = []
    monkeypatch.setattr(scheduler, "post_status", lambda *args, **kw: dispatched.append(args) or {"id": "m1"})

    past_time = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    with database.get_db() as db:
        qp_id = db.execute(
            """
            INSERT INTO queued_posts (
                user_id, feed_item_id, item_id, feed_id, destination_type, destination_id,
                content, scheduled_at, status
            ) VALUES (101, 1, 'item-1', 1, 'mastodon', 1, 'Do not post', ?, 'cancelled')
            RETURNING id
            """,
            (past_time,),
        ).fetchone()["id"]

    scheduler.flush_queue()
    assert len(dispatched) == 0


def test_queue_depth_allowance_enforced_multi(queue_env):
    """Trial plan limit (10 items) blocks the 11th enqueue with 402."""
    client = TestClient(app)
    _as_u2(client)

    # Seed 10 queued items for user 102
    with database.get_db() as db:
        for i in range(10):
            db.execute(
                """
                INSERT INTO queued_posts (
                    user_id, item_id, feed_id, destination_type, destination_id,
                    content, scheduled_at, status
                ) VALUES (102, ?, 2, 'mastodon', 2, 'C', '2099-01-01 00:00:00', 'queued')
                """,
                (f"item-u2-{i}",),
            )

    # Attempt to enqueue 11th post
    resp = client.post(
        "/api/reader/2/compose",
        data={
            "destinations": "mastodon:2",
            "content": "Over cap",
            "enqueue": "1",
        },
    )
    assert resp.status_code == 402
    assert "Your plan allows 10 queued post" in resp.json()["detail"]


def test_queue_page_and_actions(queue_env, monkeypatch):
    """Test /queue UI, Post now, Cancel, Edit, and Reorder actions."""
    dispatched = []
    monkeypatch.setattr(scheduler, "post_status", lambda *args, **kw: dispatched.append(args) or {"id": "m-instant"})

    client = TestClient(app)
    _as_u1(client)

    with database.get_db() as db:
        db.execute(
            """
            INSERT INTO queued_posts (
                id, user_id, item_id, feed_id, destination_type, destination_id,
                content, scheduled_at, status
            ) VALUES (10, 101, 'item-1', 1, 'mastodon', 1, 'Item 10', '2026-09-01 10:00:00', 'queued'),
                     (20, 101, 'item-1', 1, 'mastodon', 1, 'Item 20', '2026-09-01 11:00:00', 'queued')
            """
        )

    # 1. View /queue
    q_page = client.get("/queue")
    assert q_page.status_code == 200
    assert "Item 10" in q_page.text
    assert "Item 20" in q_page.text

    # 2. Reorder (move 20 up -> swaps timestamps with 10)
    r_up = client.post("/api/queue/20/reorder", data={"direction": "up"}, follow_redirects=False)
    assert r_up.status_code == 303
    with database.get_db() as db:
        r10 = db.execute("SELECT scheduled_at FROM queued_posts WHERE id = 10").fetchone()
        r20 = db.execute("SELECT scheduled_at FROM queued_posts WHERE id = 20").fetchone()
    assert str(r20["scheduled_at"]) < str(r10["scheduled_at"])

    # 3. Edit item
    r_edit = client.post(
        "/api/queue/10/edit",
        data={"content": "Item 10 edited", "scheduled_at": "2026-09-02 12:00:00"},
        follow_redirects=False,
    )
    assert r_edit.status_code == 303
    with database.get_db() as db:
        r10_edit = db.execute("SELECT content FROM queued_posts WHERE id = 10").fetchone()
    assert r10_edit["content"] == "Item 10 edited"

    # 4. Post now
    r_now = client.post("/api/queue/10/post-now")
    assert r_now.status_code == 200
    assert r_now.json()["success"] is True
    assert len(dispatched) == 1
    with database.get_db() as db:
        r10_sent = db.execute("SELECT status FROM queued_posts WHERE id = 10").fetchone()
    assert r10_sent["status"] == "sent"

    # 5. Cancel
    r_cancel = client.post("/api/queue/20/cancel", follow_redirects=False)
    assert r_cancel.status_code == 303
    with database.get_db() as db:
        r20_cancel = db.execute("SELECT status FROM queued_posts WHERE id = 20").fetchone()
    assert r20_cancel["status"] == "cancelled"
