"""Tests for v1.48.1 Opus 5 review fixes: retry clears posted_items, error_message
renders on /queue, cancel cleans up echo, scheduling horizon cap, auto-retry
with backoff."""

import pytest
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient

import database
import security
import settings
from app import app
import scheduler


@pytest.fixture()
def fix_env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MULTI", True)
    monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "fix.db")
    database.init_db()
    monkeypatch.setattr(scheduler, "check_all_feeds", lambda: None)

    with database.get_db() as db:
        db.execute("INSERT INTO users (id, email, password_hash, plan) VALUES (601, 'fix@example.com', '', 'paid')")
        db.execute("INSERT INTO accounts (id, name, username, instance, access_token, user_id) VALUES (1, 'M', 'm', 'https://m.social', 'tok', 601)")
        db.execute("INSERT INTO feeds (id, name, url, read_enabled, user_id) VALUES (1, 'F1', 'https://e.com/f', 1, 601)")
        db.execute("INSERT INTO feed_items (id, feed_id, item_id, title, link, published_at, is_read) VALUES (1, 1, 'item-1', 'Title', 'https://e.com/1', '2026-01-01 00:00:00', 0)")

    client = TestClient(app)
    client.cookies.set("feedecho_session", security.sign_session(601, "fix@example.com"))
    return client


def test_retry_clears_stale_posted_items(fix_env, monkeypatch):
    """Queue retry must delete the stale posted_items row so _claim_post can
    create a fresh pending row on the next dispatch attempt."""
    client = fix_env

    # Enqueue a post (creates echo_id at enqueue time)
    r_enq = client.post("/api/reader/1/compose", data={
        "destinations": "mastodon:1", "content": "Test post", "enqueue": "1",
    })
    assert r_enq.status_code == 200
    qp_id = r_enq.json()["results"][0]["queue_id"]

    with database.get_db() as db:
        qp = db.execute("SELECT echo_id FROM queued_posts WHERE id = ?", (qp_id,)).fetchone()
        echo_id = qp["echo_id"]
        assert echo_id is not None

    # Simulate a failed dispatch: insert a posted_items row with status='gave_up'
    with database.get_db() as db:
        db.execute(
            "INSERT INTO posted_items (echo_id, item_id, item_title, item_url, status, error_message)"
            " VALUES (?, 'item-1', 'Title', 'https://e.com/1', 'gave_up', 'permanent failure')",
            (echo_id,),
        )
        db.execute("UPDATE queued_posts SET status = 'failed', error_message = 'permanent failure', attempt_count = 3 WHERE id = ?", (qp_id,))

    # Retry: should clear the stale posted_items row
    r_retry = client.post(f"/api/queue/{qp_id}/retry", follow_redirects=False)
    assert r_retry.status_code == 303

    with database.get_db() as db:
        # posted_items row should be gone
        pi = db.execute("SELECT * FROM posted_items WHERE echo_id = ?", (echo_id,)).fetchone()
        assert pi is None
        # queued_posts should be back to queued
        qp = db.execute("SELECT status, attempt_count FROM queued_posts WHERE id = ?", (qp_id,)).fetchone()
        assert qp["status"] == "queued"
        assert qp["attempt_count"] == 0


def test_queue_page_shows_error_message(fix_env, monkeypatch):
    """The /queue page must render the error_message for failed posts."""
    client = fix_env

    # Enqueue and then simulate failure
    r_enq = client.post("/api/reader/1/compose", data={
        "destinations": "mastodon:1", "content": "Fail test", "enqueue": "1",
    })
    qp_id = r_enq.json()["results"][0]["queue_id"]

    with database.get_db() as db:
        db.execute("UPDATE queued_posts SET status = 'failed', error_message = 'Connection timeout' WHERE id = ?", (qp_id,))

    # /queue page should show the error message
    r_page = client.get("/queue")
    assert r_page.status_code == 200
    assert "Connection timeout" in r_page.text


def test_cancel_cleans_up_orphaned_echo(fix_env):
    """Cancelling a queued post should delete the echo created at enqueue time."""
    client = fix_env

    r_enq = client.post("/api/reader/1/compose", data={
        "destinations": "mastodon:1", "content": "Cancel me", "enqueue": "1",
    })
    qp_id = r_enq.json()["results"][0]["queue_id"]

    with database.get_db() as db:
        qp = db.execute("SELECT echo_id FROM queued_posts WHERE id = ?", (qp_id,)).fetchone()
        echo_id = qp["echo_id"]
        assert echo_id is not None
        # Echo exists
        e = db.execute("SELECT id FROM echoes WHERE id = ?", (echo_id,)).fetchone()
        assert e is not None

    # Cancel
    r_cancel = client.post(f"/api/queue/{qp_id}/cancel", follow_redirects=False)
    assert r_cancel.status_code == 303

    with database.get_db() as db:
        # Echo should be deleted
        e = db.execute("SELECT id FROM echoes WHERE id = ?", (echo_id,)).fetchone()
        assert e is None
        qp = db.execute("SELECT status FROM queued_posts WHERE id = ?", (qp_id,)).fetchone()
        assert qp["status"] == "cancelled"


def test_scheduling_horizon_cap(fix_env):
    """Enqueue with a scheduled_at >365 days out should be rejected."""
    client = fix_env

    far_future = (datetime.now(timezone.utc) + timedelta(days=9999)).strftime("%Y-%m-%d %H:%M:%S")
    r = client.post("/api/reader/1/compose", data={
        "destinations": "mastodon:1", "content": "Far future", "enqueue": "1",
        "scheduled_at": far_future,
    })
    assert r.status_code == 400
    assert "365 days" in r.json()["detail"]


def test_auto_retry_with_backoff(fix_env, monkeypatch):
    """A transient failure should auto-retry with backoff instead of going
    straight to terminal 'failed'."""
    client = fix_env

    # Enqueue
    r_enq = client.post("/api/reader/1/compose", data={
        "destinations": "mastodon:1", "content": "Auto-retry test", "enqueue": "1",
    })
    qp_id = r_enq.json()["results"][0]["queue_id"]

    # Set scheduled_at to the past so it's due
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    with database.get_db() as db:
        db.execute("UPDATE queued_posts SET scheduled_at = ? WHERE id = ?", (past, qp_id))

    # Mock post_status to fail
    call_count = [0]
    def failing_post_status(instance, access_token, content, **kw):
        call_count[0] += 1
        raise Exception("503 Service Unavailable")

    monkeypatch.setattr(scheduler, "post_status", failing_post_status)

    # Run flush_queue
    scheduler.flush_queue()

    with database.get_db() as db:
        qp = db.execute("SELECT status, attempt_count FROM queued_posts WHERE id = ?", (qp_id,)).fetchone()
        # First failure: attempt_count goes to 1, status should be 'queued' (auto-retry)
        assert qp["status"] == "queued"
        assert qp["attempt_count"] == 1

    # Run again (after backoff time, but we force it by setting scheduled_at to past)
    with database.get_db() as db:
        db.execute("UPDATE queued_posts SET scheduled_at = ? WHERE id = ?", (past, qp_id))

    scheduler.flush_queue()

    with database.get_db() as db:
        qp = db.execute("SELECT status, attempt_count FROM queued_posts WHERE id = ?", (qp_id,)).fetchone()
        # Second failure: attempt_count goes to 2, still auto-retry
        assert qp["status"] == "queued"
        assert qp["attempt_count"] == 2

    # Third attempt: terminal failure
    with database.get_db() as db:
        db.execute("UPDATE queued_posts SET scheduled_at = ? WHERE id = ?", (past, qp_id))

    scheduler.flush_queue()

    with database.get_db() as db:
        qp = db.execute("SELECT status, attempt_count, error_message FROM queued_posts WHERE id = ?", (qp_id,)).fetchone()
        assert qp["status"] == "failed"
        assert qp["attempt_count"] == 3
        assert qp["error_message"] is not None

        # The terminal failure must leave a posted_items row visible in history
        echo_id = db.execute("SELECT echo_id FROM queued_posts WHERE id = ?", (qp_id,)).fetchone()["echo_id"]
        pi = db.execute("SELECT status, error_message FROM posted_items WHERE echo_id = ?", (echo_id,)).fetchone()
        assert pi is not None
        assert pi["status"] in ("failed", "gave_up")


def test_cap_enforcement_increments_within_tick(fix_env, monkeypatch):
    """The hoisted max_posts_per_hour count must be incremented as posts
    dispatch within the same tick, not just snapshotted once."""

    # Create a paid user (queue_depth=500, max_posts_per_hour=500) and override the cap to 5
    with database.get_db() as db:
        db.execute("INSERT INTO users (id, email, password_hash, plan) VALUES (701, 'cap@example.com', '', 'paid')")
        db.execute("INSERT INTO accounts (id, name, username, instance, access_token, user_id) VALUES (2, 'M2', 'm2', 'https://m.social', 'tok', 701)")
        db.execute("INSERT INTO feeds (id, name, url, read_enabled, user_id) VALUES (2, 'F2', 'https://e.com/f2', 1, 701)")
        for i in range(10):
            db.execute(
                "INSERT INTO feed_items (id, feed_id, item_id, title, link, published_at, is_read)"
                " VALUES (?, 2, ?, ?, ?, '2026-01-01 00:00:00', 0)",
                (100 + i, f"cap-item-{i}", f"Title {i}", f"https://e.com/{i}"),
            )

    # Override the cap for this test: paid's max_posts_per_hour = 5
    monkeypatch.setattr(settings, "PLAN_LIMITS", {
        "paid": {**settings.DEFAULT_PLAN_LIMITS["paid"], "max_posts_per_hour": 5},
        "trial": settings.DEFAULT_PLAN_LIMITS["trial"],
        "beta": settings.DEFAULT_PLAN_LIMITS["beta"],
    })

    client = TestClient(app)
    client.cookies.set("feedecho_session", security.sign_session(701, "cap@example.com"))

    # Enqueue 10 posts (overridden cap is 5)
    for i in range(10):
        r = client.post("/api/reader/" + str(100 + i) + "/compose", data={
            "destinations": "mastodon:2", "content": f"Post {i}", "enqueue": "1",
        })
        assert r.status_code == 200

    # Set all to due
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    with database.get_db() as db:
        db.execute("UPDATE queued_posts SET scheduled_at = ? WHERE user_id = 701", (past,))

    # Mock successful dispatch
    monkeypatch.setattr(scheduler, "post_status", lambda instance, access_token, content, **kw: {"id": "m-cap"})

    scheduler.flush_queue()

    with database.get_db() as db:
        sent = db.execute(
            "SELECT COUNT(*) AS c FROM queued_posts WHERE user_id = 701 AND status = 'sent'"
        ).fetchone()["c"]
        queued = db.execute(
            "SELECT COUNT(*) AS c FROM queued_posts WHERE user_id = 701 AND status = 'queued'"
        ).fetchone()["c"]
    # Cap is 5, so exactly 5 should be sent and 5 remain queued
    assert sent == 5
    assert queued == 5
