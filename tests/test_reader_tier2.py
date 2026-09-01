"""Tier 2 reader features: OPML, mutes, today view, bulk mark-read (issue #11)."""

from datetime import datetime, timedelta, timezone

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
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "tier2.db")
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


class TestOpml:
    def test_export(self, env):
        with TestClient(app) as c:
            r = c.get("/api/feeds/opml")
        assert r.status_code == 200
        assert "text/x-opml" in r.headers.get("content-type", "")
        assert 'xmlUrl="https://example.com/a"' in r.text
        assert 'text="Alpha"' in r.text

    def test_import(self, env):
        opml = (
            '<?xml version="1.0"?><opml version="2.0"><body>'
            '<outline text="News" xmlUrl="https://example.com/news.xml"/>'
            '<outline text="Dup" xmlUrl="https://example.com/a"/>'
            '<outline text="Bad" xmlUrl="ftp://bad.example.com/x"/>'
            "</body></opml>"
        )
        with TestClient(app) as c:
            r = c.post("/api/feeds/opml", data={"opml": opml}, follow_redirects=False)
        assert r.status_code == 303
        with database.get_db() as db:
            urls = {row["url"] for row in db.execute("SELECT url FROM feeds").fetchall()}
            row = db.execute("SELECT read_enabled FROM feeds WHERE url = 'https://example.com/news.xml'").fetchone()
        assert "https://example.com/news.xml" in urls
        assert len(urls) == 3  # news added; dup + ftp skipped
        assert row["read_enabled"] == 1  # single mode: reading on by default

    def test_import_rejects_doctype(self, env):
        opml = '<!DOCTYPE opml [<!ENTITY a "x">]><opml><body></body></opml>'
        with TestClient(app) as c:
            assert c.post("/api/feeds/opml", data={"opml": opml}).status_code == 400

    def test_import_invalid_xml_is_400(self, env):
        with TestClient(app) as c:
            r = c.post("/api/feeds/opml", data={"opml": "not xml at all"})
        assert r.status_code == 400


class TestMuteKeywords:
    def test_muted_items_hidden(self, env):
        with database.get_db() as db:
            db.execute("UPDATE feeds SET mute_keywords = 'spam' WHERE id = 1")
            db.execute(
                "INSERT INTO feed_items (feed_id, item_id, title, content, is_read)"
                " VALUES (1, 'm1', 'Spammy deal', 'spam content', 0)"
            )
        with TestClient(app) as c:
            page = c.get("/reader", params={"view": "all"}).text
        assert "Spammy deal" not in page
        assert "Apple" in page  # non-muted item still shows

    def test_edit_feed_persists_mute_keywords(self, env):
        with TestClient(app) as c:
            r = c.post(
                "/api/feeds/1/edit",
                data={"name": "Alpha", "url": "https://example.com/a",
                      "poll_interval": "15", "mute_keywords": "spam, ads"},
                follow_redirects=False,
            )
        assert r.status_code == 303
        with database.get_db() as db:
            row = db.execute("SELECT mute_keywords FROM feeds WHERE id = 1").fetchone()
        assert row["mute_keywords"] == "spam, ads"


class TestTodayView:
    def test_shows_only_last_24h(self, env):
        now = datetime.now(timezone.utc)
        with database.get_db() as db:
            db.execute(
                "INSERT INTO feed_items (feed_id, item_id, title, published_at)"
                " VALUES (1, 'recent', 'Fresh', ?)",
                (now.strftime("%Y-%m-%d %H:%M:%S"),),
            )
            old = (now - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
            db.execute(
                "INSERT INTO feed_items (feed_id, item_id, title, published_at)"
                " VALUES (1, 'old', 'Stale', ?)",
                (old,),
            )
        with TestClient(app) as c:
            page = c.get("/reader", params={"view": "today"}).text
        assert "Fresh" in page
        assert "Stale" not in page
        # the fixture's undated items are excluded from Today too
        assert "Apple" not in page


class TestBulkMarkRead:
    def test_mark_read_sets_only_unread(self, env):
        with TestClient(app) as c:
            r = c.post("/api/reader/mark-read", data={"ids": "1,3,9999"})
            assert r.json()["count"] == 2  # ids 1 and 3 were unread; 9999 doesn't exist
        with database.get_db() as db:
            n = db.execute("SELECT COUNT(*) AS c FROM feed_items WHERE is_read = 0").fetchone()["c"]
        assert n == 1  # only b1 (id 4) remains unread

    def test_mark_read_empty_is_noop(self, env):
        with TestClient(app) as c:
            r = c.post("/api/reader/mark-read", data={"ids": ""})
            assert r.json() == {"success": True, "count": 0}
