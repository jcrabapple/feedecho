"""Tier 1 reader features: search operators + mark-all-read undo (issue #11)."""

import pytest
from fastapi.testclient import TestClient

import database
import scheduler
import settings
from app import app, _parse_reader_query


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MULTI", False)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "tier1.db")
    database.init_db()
    monkeypatch.setattr(scheduler, "check_all_feeds", lambda: None)
    with database.get_db() as db:
        db.execute(
            "INSERT INTO feeds (name, url, read_enabled) VALUES (?, ?, 1)",
            ("Alpha", "https://example.com/a"),
        )
        db.execute(
            "INSERT INTO feeds (name, url, read_enabled) VALUES (?, ?, 1)",
            ("Beta", "https://example.com/b"),
        )
        # Alpha: a1 unread, a2 read, a3 unread+starred. Beta: b1 unread.
        db.execute(
            "INSERT INTO feed_items (feed_id, item_id, title, content, is_read, starred)"
            " VALUES (1, 'a1', 'Apple', 'fruit', 0, 0)"
        )
        db.execute(
            "INSERT INTO feed_items (feed_id, item_id, title, content, is_read, starred)"
            " VALUES (1, 'a2', 'Avocado', 'green fruit', 1, 0)"
        )
        db.execute(
            "INSERT INTO feed_items (feed_id, item_id, title, content, is_read, starred)"
            " VALUES (1, 'a3', 'Aardvark', 'animal', 0, 1)"
        )
        db.execute(
            "INSERT INTO feed_items (feed_id, item_id, title, content, is_read, starred)"
            " VALUES (2, 'b1', 'Banana', 'fruit', 0, 0)"
        )
    return settings


class TestParseReaderQuery:
    def test_bare_terms_and_operators(self):
        filters, terms = _parse_reader_query("hello is:starred feed:alpha world")
        assert ("is", "starred") in filters
        assert ("feed", "alpha") in filters
        assert terms == ["hello", "world"]

    def test_unknown_operator_is_bare_term(self):
        filters, terms = _parse_reader_query("foo:bar")
        assert filters == []
        assert terms == ["foo:bar"]

    def test_in_scope(self):
        filters, terms = _parse_reader_query("in:title fruit")
        assert ("in", "title") in filters
        assert terms == ["fruit"]


class TestSearchOperators:
    def test_is_starred(self, env):
        with TestClient(app) as c:
            page = c.get("/reader", params={"q": "is:starred", "view": "all"}).text
        assert "Aardvark" in page
        assert "Apple" not in page
        assert "Banana" not in page

    def test_is_unread(self, env):
        with TestClient(app) as c:
            page = c.get("/reader", params={"q": "is:unread", "view": "all"}).text
        assert "Apple" in page
        assert "Aardvark" in page
        assert "Banana" in page
        assert "Avocado" not in page  # already read

    def test_feed_filter(self, env):
        with TestClient(app) as c:
            page = c.get("/reader", params={"q": "feed:beta", "view": "all"}).text
        assert "Banana" in page
        assert "Apple" not in page

    def test_in_title_scopes_terms(self, env):
        with TestClient(app) as c:
            page = c.get("/reader", params={"q": "in:title banana", "view": "all"}).text
        assert "Banana" in page
        assert "Apple" not in page

    def test_bare_term_still_searches_title_and_body(self, env):
        with TestClient(app) as c:
            page = c.get("/reader", params={"q": "fruit", "view": "all"}).text
        assert "Apple" in page
        assert "Avocado" in page
        assert "Banana" in page
        assert "Aardvark" not in page  # content is "animal", not "fruit"


class TestMarkAllReadUndo:
    def test_mark_all_read_returns_ids(self, env):
        with TestClient(app) as c:
            r = c.post("/api/reader/mark-all-read")
        data = r.json()
        assert data["success"] is True
        assert data["count"] == 3  # a1, a3, b1 are unread
        assert set(data["ids"]) == {1, 3, 4}

    def test_mark_unread_reverts(self, env):
        with TestClient(app) as c:
            c.post("/api/reader/mark-all-read")
            r = c.post("/api/reader/mark-unread", data={"ids": "1,3,4"})
            assert r.json()["count"] == 3
        with database.get_db() as db:
            n = db.execute("SELECT COUNT(*) AS c FROM feed_items WHERE is_read = 0").fetchone()["c"]
        assert n == 3

    def test_mark_unread_empty_ids_is_400(self, env):
        with TestClient(app) as c:
            assert c.post("/api/reader/mark-unread", data={"ids": ""}).status_code == 400

    def test_mark_unread_nonexistent_id_counts_zero(self, env):
        with TestClient(app) as c:
            r = c.post("/api/reader/mark-unread", data={"ids": "9999,abc"})
            assert r.json()["count"] == 0
