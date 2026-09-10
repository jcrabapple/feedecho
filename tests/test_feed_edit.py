"""In-place feed editing (issue #3): name, URL, and poll interval.

Changing a feed's URL must invalidate the last-seen cursor, since item
IDs from the old feed are meaningless against the new one. Renames and
interval-only edits must preserve the cursor.
"""

import re
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

    # Shared-secret auth must not leak in from the ambient environment.
    monkeypatch.setattr(app_module.settings, "AUTH_TOKEN", None)
    return TestClient(app_module.app)


def _seed(cursor=None):
    with get_db() as db:
        db.execute(
            "INSERT INTO feeds (name, url, poll_interval, last_item_id) "
            "VALUES (?, ?, ?, ?)",
            ("Test Feed", "https://example.com/feed.xml", 15, cursor),
        )


def _get_feed():
    with get_db() as db:
        return db.execute("SELECT * FROM feeds WHERE id = 1").fetchone()


class TestFeedEdit:
    def test_edit_updates_name_url_interval(self, client, temp_db):
        _seed()
        resp = client.post(
            "/api/feeds/1/edit",
            data={
                "name": "Renamed",
                "url": "https://example.com/new.xml",
                "poll_interval": "60",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/feeds"
        feed = _get_feed()
        assert feed["name"] == "Renamed"
        assert feed["url"] == "https://example.com/new.xml"
        assert feed["poll_interval"] == 60
        # Rendered on the feeds page after redirect
        page = client.get("/feeds").text
        assert "Renamed" in page
        assert "example.com/new.xml" in page

    def test_url_change_resets_cursor(self, client, temp_db):
        _seed(cursor="item-5")
        client.post(
            "/api/feeds/1/edit",
            data={
                "name": "Test Feed",
                "url": "https://example.com/different.xml",
                "poll_interval": "15",
            },
        )
        assert _get_feed()["last_item_id"] is None

    def test_same_url_preserves_cursor(self, client, temp_db):
        _seed(cursor="item-5")
        client.post(
            "/api/feeds/1/edit",
            data={
                "name": "Renamed Only",
                "url": "https://example.com/feed.xml",
                "poll_interval": "15",
            },
        )
        assert _get_feed()["last_item_id"] == "item-5"

    def test_poll_interval_clamped(self, client, temp_db):
        _seed()
        client.post(
            "/api/feeds/1/edit",
            data={
                "name": "Test Feed",
                "url": "https://example.com/feed.xml",
                "poll_interval": "0",
            },
        )
        assert _get_feed()["poll_interval"] == 1
        client.post(
            "/api/feeds/1/edit",
            data={
                "name": "Test Feed",
                "url": "https://example.com/feed.xml",
                "poll_interval": "999999",
            },
        )
        assert _get_feed()["poll_interval"] == 1440

    def test_invalid_url_rejected(self, client, temp_db):
        _seed()
        resp = client.post(
            "/api/feeds/1/edit",
            data={
                "name": "Test Feed",
                "url": "ftp://example.com/feed.xml",
                "poll_interval": "15",
            },
        )
        assert resp.status_code == 400
        assert _get_feed()["url"] == "https://example.com/feed.xml"

    def test_blank_name_rejected(self, client, temp_db):
        _seed()
        resp = client.post(
            "/api/feeds/1/edit",
            data={
                "name": "   ",
                "url": "https://example.com/feed.xml",
                "poll_interval": "15",
            },
        )
        assert resp.status_code == 400
        assert _get_feed()["name"] == "Test Feed"

    def test_name_is_trimmed(self, client, temp_db):
        _seed()
        client.post(
            "/api/feeds/1/edit",
            data={
                "name": "  Padded Name  ",
                "url": "https://example.com/feed.xml",
                "poll_interval": "15",
            },
        )
        assert _get_feed()["name"] == "Padded Name"

    def test_edit_unknown_feed_404(self, client, temp_db):
        _seed()
        resp = client.post(
            "/api/feeds/999/edit",
            data={
                "name": "X",
                "url": "https://example.com/feed.xml",
                "poll_interval": "15",
            },
        )
        assert resp.status_code == 404

    def test_edit_deleted_feed_404(self, client, temp_db):
        _seed()
        client.post("/api/feeds/1/delete")
        resp = client.post(
            "/api/feeds/1/edit",
            data={
                "name": "Zombie",
                "url": "https://example.com/feed.xml",
                "poll_interval": "15",
            },
        )
        assert resp.status_code == 404
        assert _get_feed()["name"] == "Test Feed"

    def test_edit_preserves_echoes_and_pause_state(self, client, temp_db):
        _seed()
        with get_db() as db:
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
            db.execute("UPDATE feeds SET paused = 1 WHERE id = 1")
        client.post(
            "/api/feeds/1/edit",
            data={
                "name": "Renamed",
                "url": "https://example.com/new.xml",
                "poll_interval": "30",
            },
        )
        feed = _get_feed()
        assert feed["paused"] == 1
        with get_db() as db:
            echoes = db.execute(
                "SELECT COUNT(*) as c FROM echoes WHERE feed_id = 1"
            ).fetchone()["c"]
        assert echoes == 1

    def test_hostile_name_escaped_on_page(self, client, temp_db):
        _seed()
        client.post(
            "/api/feeds/1/edit",
            data={
                "name": "<script>alert(1)</script>",
                "url": "https://example.com/feed.xml",
                "poll_interval": "15",
            },
        )
        page = client.get("/feeds").text
        assert "<script>alert(1)</script>" not in page
        assert "&lt;script&gt;" in page


class TestFeedEditInvalidatesSavedSearchCache:
    """Bug review 2026-09-09 finding #14: mute_keywords (and folder_id) are
    direct inputs to the saved-search unread-counts query, so edit_feed
    must invalidate the cache, same as reader_mute/delete_feed do."""

    def test_editing_mute_keywords_invalidates_cache(self, client, temp_db):
        import app as app_module

        with get_db() as db:
            db.execute(
                "INSERT INTO feeds (id, name, url, poll_interval, read_enabled, user_id) "
                "VALUES (1, 'Test Feed', 'https://example.com/feed.xml', 15, 1, 1)"
            )
            db.execute(
                "INSERT INTO feed_items (feed_id, item_id, title, content, is_read) "
                "VALUES (1, 'it-1', 'Breaking widget news', 'widget content', 0)"
            )

        s_id = client.post(
            "/api/saved-searches",
            data={"name": "Widgets", "query": "widget"},
            headers={"Accept": "application/json"},
        ).json()["id"]

        def _saved_search_badge(html: str, search_id: int) -> int:
            m = re.search(
                r'href="/reader\?saved=%d(?:&amp;fulltext=1)?"' % search_id,
                html,
                re.S,
            )
            assert m, f"saved search {search_id} link not found in page"
            anchor = html[m.start(): html.find("</a>", m.end()) + 4]
            badge = re.search(
                r'class="(?:reader-search-count|reader-feed-unread)"[^>]*>(\d+)</span>',
                anchor,
            )
            return int(badge.group(1)) if badge else 0

        # Populate the cache: unmuted, "widget" matches the one unread item.
        r1 = client.get(f"/reader?saved={s_id}")
        assert _saved_search_badge(r1.text, s_id) == 1
        assert 1 in app_module._saved_search_counts_cache._store

        # Mute "widget" on this feed via edit_feed.
        client.post(
            "/api/feeds/1/edit",
            data={
                "name": "Test Feed",
                "url": "https://example.com/feed.xml",
                "poll_interval": "15",
                "mute_keywords": "widget",
            },
        )

        # Cache must be invalidated immediately, not stale until TTL expiry.
        assert 1 not in app_module._saved_search_counts_cache._store
        r2 = client.get(f"/reader?saved={s_id}")
        assert _saved_search_badge(r2.text, s_id) == 0
        assert app_module._saved_search_counts_cache._store[1][2][s_id] == 0
