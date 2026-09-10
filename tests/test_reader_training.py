"""Tests for Reader Phase 4: Delivery Badges, Filter Reasons, and Reader Mute Training."""

import pytest
from fastapi.testclient import TestClient

import database
import security
import settings
from app import app
import scheduler
from filters import match_reason, is_filtered


@pytest.fixture()
def p4_env(monkeypatch, tmp_path):
    db_file = tmp_path / "test_p4.db"
    monkeypatch.setattr(database, "DB_PATH", db_file)
    monkeypatch.setattr(settings, "MULTI", True)
    monkeypatch.setattr(settings, "SESSION_SECRET", "x" * 40)
    monkeypatch.setattr(settings, "STATE_SECRET", "x" * 40)
    monkeypatch.setattr(scheduler, "check_all_feeds", lambda: None)
    database.init_db()

    with database.get_db() as db:
        # Create users
        db.execute(
            "INSERT INTO users (id, email, password_hash, plan) VALUES (?, ?, ?, ?)",
            (101, "u1@example.com", "fake", "paid"),
        )
        db.execute(
            "INSERT INTO users (id, email, password_hash, plan) VALUES (?, ?, ?, ?)",
            (102, "u2@example.com", "fake", "paid"),
        )
        # Create feeds
        db.execute(
            "INSERT INTO feeds (id, user_id, name, url, read_enabled, mute_keywords) VALUES (?, ?, ?, ?, ?, ?)",
            (1, 101, "Tech Feed", "https://tech.example/feed.xml", 1, "sponsor"),
        )
        db.execute(
            "INSERT INTO feeds (id, user_id, name, url, read_enabled, mute_keywords) VALUES (?, ?, ?, ?, ?, ?)",
            (2, 101, "News Feed", "https://news.example/feed.xml", 1, ""),
        )
        db.execute(
            "INSERT INTO feeds (id, user_id, name, url, read_enabled, mute_keywords) VALUES (?, ?, ?, ?, ?, ?)",
            (3, 102, "Other User Feed", "https://other.example/feed.xml", 1, ""),
        )
        # Create feed items
        db.execute(
            """
            INSERT INTO feed_items (id, feed_id, item_id, title, link, summary, published_at, is_read, starred)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0)
            """,
            (1, 1, "tech-1", "Sponsor Special Announcement", "https://tech.example/1", "Special sponsor deal", "2026-09-01 12:00:00"),
        )
        db.execute(
            """
            INSERT INTO feed_items (id, feed_id, item_id, title, link, summary, published_at, is_read, starred)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0)
            """,
            (2, 1, "tech-2", "Open Source Breakthrough", "https://tech.example/2", "Great news for developers", "2026-09-01 13:00:00"),
        )
        db.execute(
            """
            INSERT INTO feed_items (id, feed_id, item_id, title, link, summary, published_at, is_read, starred)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0)
            """,
            (3, 2, "news-1", "General News Headline", "https://news.example/1", "World update today", "2026-09-01 14:00:00"),
        )
        db.execute(
            """
            INSERT INTO feed_items (id, feed_id, item_id, title, link, summary, published_at, is_read, starred)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0)
            """,
            (4, 3, "other-1", "Other User Item", "https://other.example/1", "Not visible to u1", "2026-09-01 15:00:00"),
        )
        # Create an echo and some posted_items for delivery badges
        db.execute(
            """
            INSERT INTO echoes (id, feed_id, destination_type, destination_id, template, enabled, one_shot, user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (1, 1, "mastodon", 1, "{{ title }}", 1, 0, 101),
        )
        db.execute(
            """
            INSERT INTO posted_items (echo_id, item_id, item_title, item_url, status, error_message)
            VALUES (?, ?, ?, ?, 'success', NULL)
            """,
            (1, "tech-2", "Open Source Breakthrough", "https://tech.example/2"),
        )
        db.execute(
            """
            INSERT INTO posted_items (echo_id, item_id, item_title, item_url, status, error_message)
            VALUES (?, ?, ?, ?, 'filtered', 'matched "sponsor"')
            """,
            (1, "tech-1", "Sponsor Special Announcement", "https://tech.example/1"),
        )

    client = TestClient(app)
    client.cookies.set("feedecho_session", security.sign_session(101, "u1@example.com"))
    return client


def test_match_reason():
    """Verify match_reason returns human-readable filter explanation."""
    item = {"id": "1", "title": "Cool Product (Sponsored)", "summary": "Buy now"}
    assert match_reason(item, "sponsored, ad", "exclude") == 'matched "sponsored"'
    assert match_reason(item, "crypto", "exclude") is None

    # Include mode
    assert match_reason(item, "tech, software", "include") == "no include keyword matched"
    assert match_reason(item, "sponsored, tech", "include") is None
    assert match_reason(item, "", "include") is None


def test_delivery_badges_in_reader(p4_env):
    """Reader displays delivery badges for echoed, filtered, etc."""
    resp = p4_env.get("/reader")
    assert resp.status_code == 200
    html = resp.text

    # tech-2 should have Echoed badge linking to /history?feed=1&item_id=tech-2
    assert "Echoed" in html
    assert 'href="/history?feed=1&amp;item_id=tech-2"' in html or 'href="/history?feed=1&item_id=tech-2"' in html
    # tech-1 has 'sponsor' in mute_keywords on feed 1, so by default reader filters it out
    # Let's verify tech-2 shows Echoed badge
    assert "Delivered to 1 destination" in html


def test_history_item_id_filter(p4_env):
    """History page supports item_id filter parameter."""
    resp = p4_env.get("/history", params={"feed": "1", "item_id": "tech-2"})
    assert resp.status_code == 200
    assert "Open Source Breakthrough" in resp.text
    assert "Sponsor Special Announcement" not in resp.text


def test_reader_mute_feed_scope(p4_env):
    """POST /api/reader/{item_id}/mute with scope=feed affects only that item's feed."""
    resp = p4_env.post(
        "/api/reader/2/mute",
        data={"phrase": "breakthrough", "scope": "feed"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["affected_feed_ids"] == [1]

    with database.get_db() as db:
        f1 = db.execute("SELECT mute_keywords FROM feeds WHERE id = 1").fetchone()
        f2 = db.execute("SELECT mute_keywords FROM feeds WHERE id = 2").fetchone()
    assert "breakthrough" in f1["mute_keywords"]
    assert "breakthrough" not in (f2["mute_keywords"] or "")

    # Item should now be hidden in reader
    r_reader = p4_env.get("/reader")
    assert "Open Source Breakthrough" not in r_reader.text


def test_reader_mute_all_scope(p4_env):
    """POST /api/reader/{item_id}/mute with scope=all hits all user's read-enabled feeds."""
    resp = p4_env.post(
        "/api/reader/3/mute",
        data={"phrase": "Headline", "scope": "all"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert set(data["affected_feed_ids"]) == {1, 2}

    with database.get_db() as db:
        f1 = db.execute("SELECT mute_keywords FROM feeds WHERE id = 1").fetchone()
        f2 = db.execute("SELECT mute_keywords FROM feeds WHERE id = 2").fetchone()
        f3 = db.execute("SELECT mute_keywords FROM feeds WHERE id = 3").fetchone()

    assert "Headline" in f1["mute_keywords"]
    assert "Headline" in f2["mute_keywords"]
    # Other user's feed unaffected
    assert "Headline" not in (f3["mute_keywords"] or "")


def test_reader_unmute(p4_env):
    """POST /api/reader/unmute removes phrase from feeds."""
    # First mute
    p4_env.post("/api/reader/2/mute", data={"phrase": "deal", "scope": "feed"})
    with database.get_db() as db:
        f1 = db.execute("SELECT mute_keywords FROM feeds WHERE id = 1").fetchone()
    assert "deal" in f1["mute_keywords"]

    # Now unmute with feed_ids
    r_unmute = p4_env.post("/api/reader/unmute", data={"phrase": "deal", "feed_ids": "1"})
    assert r_unmute.status_code == 200
    with database.get_db() as db:
        f1_after = db.execute("SELECT mute_keywords FROM feeds WHERE id = 1").fetchone()
    assert "deal" not in f1_after["mute_keywords"]


def test_reader_mute_comma_rejected(p4_env):
    """Commas in phrase are rejected to prevent CSV corruption."""
    resp = p4_env.post(
        "/api/reader/2/mute",
        data={"phrase": "deal, discount", "scope": "feed"},
    )
    assert resp.status_code == 400
    assert "commas" in resp.json()["detail"]


def test_reader_mute_authz_isolation(p4_env):
    """User cannot mute an item belonging to another user."""
    # Item 4 belongs to feed 3 (user 102)
    resp = p4_env.post("/api/reader/4/mute", data={"phrase": "hack", "scope": "feed"})
    assert resp.status_code == 404
