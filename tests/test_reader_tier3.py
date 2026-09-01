"""Tier 3 reader features: lazy body, new-count, enclosure storage (issue #11)."""

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
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "tier3.db")
    database.init_db()
    monkeypatch.setattr(scheduler, "check_all_feeds", lambda: None)
    with database.get_db() as db:
        db.execute("INSERT INTO feeds (name, url, read_enabled) VALUES (?, ?, 1)", ("Alpha", "https://example.com/a"))
        db.execute("INSERT INTO feeds (name, url, read_enabled) VALUES (?, ?, 1)", ("Beta", "https://example.com/b"))
        db.execute("INSERT INTO feed_items (feed_id, item_id, title, content, is_read) VALUES (1, 'a1', 'Apple', 'fruit', 0)")
        db.execute("INSERT INTO feed_items (feed_id, item_id, title, content, is_read) VALUES (1, 'a2', 'Avocado', 'green fruit', 1)")
        db.execute("INSERT INTO feed_items (feed_id, item_id, title, content, is_read) VALUES (1, 'a3', 'Aardvark', 'animal', 0)")
        db.execute("INSERT INTO feed_items (feed_id, item_id, title, content, is_read) VALUES (2, 'b1', 'Banana', 'fruit', 0)")
    return settings


class TestItemBody:
    def test_returns_display_content(self, env):
        with TestClient(app) as c:
            r = c.get("/api/reader/1/body")
        assert r.status_code == 200
        assert r.json()["content"] == "fruit"

    def test_404_for_missing_item(self, env):
        with TestClient(app) as c:
            assert c.get("/api/reader/9999/body").status_code == 404


class TestNewCount:
    def test_counts_newer_than_since_id(self, env):
        with TestClient(app) as c:
            r = c.get("/api/reader/new-count", params={"since_id": "2"})
        assert r.json()["count"] == 2  # ids 3 and 4


class TestEnclosureStorage:
    def test_stores_image_and_enclosure(self, env):
        item = {
            "id": "x",
            "title": "Pod",
            "image_url": "https://example.com/img.jpg",
            "image_alt": "pic",
            "enclosure_url": "https://example.com/ep.mp3",
        }
        scheduler._store_feed_items(2, [item])
        with database.get_db() as db:
            row = db.execute(
                "SELECT image_url, image_alt, enclosure_url FROM feed_items WHERE item_id = 'x'"
            ).fetchone()
        assert row["image_url"] == "https://example.com/img.jpg"
        assert row["image_alt"] == "pic"
        assert row["enclosure_url"] == "https://example.com/ep.mp3"


class TestSummaryOnlyContentText:
    def test_falls_back_to_summary_html_for_structure(self):
        # Feeds without <content:encoded> (plain WordPress <description>) ship
        # the full body in summary; content_text must stay structured, not empty.
        from feed_parser import parse_rss_feed

        entry = {
            "id": "x",
            "title": "Emo piece",
            "link": "https://example.com/emo",
            "summary": (
                "<div>Opening paragraph.</div>"
                "<h2>What emo is</h2>"
                "<ul><li>First wave</li><li>Second wave</li></ul>"
            ),
            # no "content" key
        }
        result = parse_rss_feed({"feed": {}, "entries": [entry]}, "https://example.com/feed")
        item = result["items"][0]
        assert "\n" in item["content_text"]  # structured, not a flat run
        assert "What emo is" in item["content_text"]
        assert "• First wave" in item["content_text"]  # bullets preserved


class TestFullTextView:
    def test_fulltext_renders_full_body_inline(self, env):
        with database.get_db() as db:
            db.execute(
                "INSERT INTO feed_items (feed_id, item_id, title, summary, content_text, is_read) "
                "VALUES (1, 'ft1', 'Full text item', 'Short teaser.', 'Full article body with paragraph breaks.', 0)"
            )
        with TestClient(app) as c:
            page = c.get("/reader", params={"view": "all", "fulltext": "1"}).text
        assert "Full article body" in page          # full text rendered inline
        assert "Short teaser." not in page          # teaser replaced, not shown
        assert 'data-loaded="1"' in page            # lazy-load bypassed

    def test_teaser_by_default(self, env):
        with database.get_db() as db:
            db.execute(
                "INSERT INTO feed_items (feed_id, item_id, title, summary, content_text, is_read) "
                "VALUES (1, 'ft2', 'Teaser item', 'Short teaser.', 'Full article body.', 0)"
            )
        with TestClient(app) as c:
            page = c.get("/reader", params={"view": "all"}).text
        assert "Short teaser." in page              # teaser shown
        assert "Full article body." not in page     # full text not inline by default
