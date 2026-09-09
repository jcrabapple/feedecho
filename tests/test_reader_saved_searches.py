"""Tests for Reader Phase 5: Saved Searches as Smart Feeds."""

import re

import pytest
from fastapi.testclient import TestClient

import database
import security
import settings
from app import app
import scheduler


def _saved_search_badge(html: str, s_id: int) -> int:
    """Extract the sidebar unread-count badge for one saved search's link.

    Both the per-feed unread badge and the saved-search badge share the
    ``reader-feed-unread`` class, so a plain substring/regex search over
    the whole page can accidentally match the wrong badge when the two
    counts collide. Scope the search to the saved search's own <a> block.
    """
    m = re.search(
        r'href="/reader\?saved=%d(?:&amp;fulltext=1)?".*?</a>' % s_id,
        html,
        re.S,
    )
    assert m, f"saved search {s_id} link not found in page"
    badge = re.search(r'reader-feed-unread">(\d+)</span>', m.group(0))
    return int(badge.group(1)) if badge else 0


@pytest.fixture()
def p5_env(monkeypatch, tmp_path):
    db_file = tmp_path / "test_p5.db"
    monkeypatch.setattr(database, "DB_PATH", db_file)
    monkeypatch.setattr(settings, "MULTI", True)
    monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
    monkeypatch.setattr(scheduler, "check_all_feeds", lambda: None)
    database.init_db()

    with database.get_db() as db:
        # Create users (one trial, one paid)
        db.execute(
            "INSERT INTO users (id, email, password_hash, plan) VALUES (?, ?, ?, ?)",
            (501, "u1@example.com", "fake", "trial"),
        )
        db.execute(
            "INSERT INTO users (id, email, password_hash, plan) VALUES (?, ?, ?, ?)",
            (502, "u2@example.com", "fake", "paid"),
        )

        # Create feeds
        db.execute(
            "INSERT INTO feeds (id, user_id, name, url, read_enabled) VALUES (?, ?, ?, ?, ?)",
            (10, 501, "Tech Feed", "https://tech.example/rss", 1),
        )
        db.execute(
            "INSERT INTO feeds (id, user_id, name, url, read_enabled) VALUES (?, ?, ?, ?, ?)",
            (20, 502, "Other Feed", "https://other.example/rss", 1),
        )

        # Create feed items
        db.execute(
            """
            INSERT INTO feed_items (id, feed_id, item_id, title, content, summary, is_read, starred, published_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (1, 10, "it-1", "AI Breakthrough announced", "Artificial intelligence models release", "", 0, 1, "2026-09-01 10:00:00"),
        )
        db.execute(
            """
            INSERT INTO feed_items (id, feed_id, item_id, title, content, summary, is_read, starred, published_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (2, 10, "it-2", "General Technology Update", "Standard tech news", "", 0, 0, "2026-09-01 11:00:00"),
        )
        db.execute(
            """
            INSERT INTO feed_items (id, feed_id, item_id, title, content, summary, is_read, starred, published_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (3, 10, "it-3", "Old AI Paper", "Historical research on AI", "", 1, 0, "2026-09-01 12:00:00"),
        )

    client = TestClient(app)
    client.cookies.set("feedecho_session", security.sign_session(501, "u1@example.com"))
    return client


def test_saved_search_crud_and_tenant_isolation(p5_env):
    """Create, rename, and delete saved searches with tenant isolation."""
    # 1. Create saved search
    r_create = p5_env.post(
        "/api/saved-searches",
        data={"name": "AI News", "query": "AI is:unread"},
        headers={"Accept": "application/json"},
    )
    assert r_create.status_code == 200
    data = r_create.json()
    assert data["success"] is True
    s_id = data["id"]
    assert data["name"] == "AI News"
    assert data["query"] == "AI is:unread"

    # Duplicate name should 409
    r_dup = p5_env.post(
        "/api/saved-searches",
        data={"name": "AI News", "query": "something else"},
        headers={"Accept": "application/json"},
    )
    assert r_dup.status_code == 409

    # 2. Rename saved search
    r_rename = p5_env.post(
        f"/api/saved-searches/{s_id}/rename",
        data={"name": "All AI Items"},
        headers={"Accept": "application/json"},
    )
    assert r_rename.status_code == 200
    assert r_rename.json()["name"] == "All AI Items"

    # 3. Isolation: user 502 cannot rename or delete user 501's saved search
    c2 = TestClient(app)
    c2.cookies.set("feedecho_session", security.sign_session(502, "u2@example.com"))
    assert c2.post(f"/api/saved-searches/{s_id}/rename", data={"name": "Hacked"}).status_code == 404
    assert c2.post(f"/api/saved-searches/{s_id}/delete").status_code == 404

    # 4. Delete saved search
    r_del = p5_env.post(f"/api/saved-searches/{s_id}/delete", headers={"Accept": "application/json"})
    assert r_del.status_code == 200
    with database.get_db() as db:
        assert db.execute("SELECT * FROM saved_searches WHERE id = ?", (s_id,)).fetchone() is None


def test_reader_saved_search_view_and_counts(p5_env):
    """GET /reader?saved=<id> runs saved query and sidebar displays unread counts."""
    # Create saved search
    r_create = p5_env.post(
        "/api/saved-searches",
        data={"name": "AI Unread", "query": "AI is:unread"},
        headers={"Accept": "application/json"},
    )
    s_id = r_create.json()["id"]

    # View reader with saved parameter
    r_page = p5_env.get(f"/reader?saved={s_id}")
    assert r_page.status_code == 200
    html = r_page.text

    # Sidebar contains saved search section and unread badge
    assert "Saved Searches" in html
    assert "AI Unread" in html
    # it-1 is unread and contains AI; it-3 is read; it-2 has no AI. So count is 1.
    assert '<span class="reader-feed-unread">1</span>' in html
    # The item list should show "AI Breakthrough announced"
    assert "AI Breakthrough announced" in html
    # and should NOT show "Old AI Paper" because of is:unread
    assert "Old AI Paper" not in html


def test_saved_search_badge_matches_page_when_no_is_operator(p5_env):
    """A saved search with no is: operator must show a sidebar badge count
    equal to the number of items the saved search page itself displays
    (all matches, read or not) — not an unread-only count. Bug review
    2026-09-09 finding #13.
    """
    r_create = p5_env.post(
        "/api/saved-searches",
        data={"name": "AI Everything", "query": "AI"},
        headers={"Accept": "application/json"},
    )
    s_id = r_create.json()["id"]

    r_page = p5_env.get(f"/reader?saved={s_id}")
    assert r_page.status_code == 200
    html = r_page.text

    # "AI" matches it-1 (unread, starred) and it-3 (read); it-2 has no "AI".
    # The page itself shows both, regardless of read state.
    assert "AI Breakthrough announced" in html
    assert "Old AI Paper" in html
    assert "General Technology Update" not in html

    # The sidebar badge must agree with that: 2, not the unread-only count (1).
    assert _saved_search_badge(html, s_id) == 2


def test_saved_searches_plan_allowance_enforced(p5_env):
    """Trial plan limit (3 saved searches) is enforced."""
    # User 501 is on trial (cap = 3)
    p5_env.post("/api/saved-searches", data={"name": "S1", "query": "q1"}, headers={"Accept": "application/json"})
    p5_env.post("/api/saved-searches", data={"name": "S2", "query": "q2"}, headers={"Accept": "application/json"})
    p5_env.post("/api/saved-searches", data={"name": "S3", "query": "q3"}, headers={"Accept": "application/json"})

    # 4th should return 402
    r4 = p5_env.post(
        "/api/saved-searches",
        data={"name": "S4", "query": "q4"},
        headers={"Accept": "application/json"},
    )
    assert r4.status_code == 402
    assert "allows 3 saved searches" in r4.json()["detail"]
