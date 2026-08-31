"""Tests for the RSS reader storage foundation (issue #11)."""

import tempfile
from pathlib import Path

import pytest

from database import get_db, init_db, prune_feed_items


@pytest.fixture
def db_tmp(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setattr("database.DB_PATH", Path(tmpdir) / "test.db")
        init_db()
        yield


class TestReaderSchema:
    def test_feed_items_table_created(self, db_tmp):
        with get_db() as db:
            names = {
                r["name"]
                for r in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        assert "feed_items" in names

    def test_feed_read_enabled_defaults_to_zero(self, db_tmp):
        with get_db() as db:
            db.execute(
                "INSERT INTO feeds (name, url, user_id) VALUES (?, ?, ?)",
                ("F", "https://example.com/feed", 1),
            )
            row = db.execute("SELECT read_enabled FROM feeds").fetchone()
        assert row["read_enabled"] == 0

    def test_feed_items_unique_per_feed_and_item(self, db_tmp):
        with get_db() as db:
            db.execute(
                "INSERT INTO feeds (name, url, user_id) VALUES (?, ?, ?)",
                ("F", "https://example.com/feed", 1),
            )
            db.execute(
                "INSERT INTO feed_items (feed_id, item_id) VALUES (?, ?)", (1, "a")
            )
            with pytest.raises(Exception):
                db.execute(
                    "INSERT INTO feed_items (feed_id, item_id) VALUES (?, ?)",
                    (1, "a"),
                )


class TestPruneFeedItems:
    def _seed(self, n):
        with get_db() as db:
            db.execute(
                "INSERT INTO feeds (name, url, user_id) VALUES (?, ?, ?)",
                ("F", "https://example.com/feed", 1),
            )
            feed_id = db.execute("SELECT id FROM feeds").fetchone()["id"]
            for i in range(n):
                db.execute(
                    "INSERT INTO feed_items (feed_id, item_id, published_at)"
                    " VALUES (?, ?, ?)",
                    (feed_id, f"item-{i:03d}", f"2026-01-01 00:{i:02d}:00"),
                )
        return feed_id

    def test_prunes_oldest_beyond_limit(self, db_tmp):
        feed_id = self._seed(5)
        with get_db() as db:
            prune_feed_items(db, feed_id, limit=3)
            ids = [
                r["item_id"]
                for r in db.execute(
                    "SELECT item_id FROM feed_items WHERE feed_id = ?"
                    " ORDER BY published_at",
                    (feed_id,),
                ).fetchall()
            ]
        assert ids == ["item-002", "item-003", "item-004"]

    def test_prunes_null_published_first(self, db_tmp):
        feed_id = self._seed(2)
        with get_db() as db:
            db.execute(
                "INSERT INTO feed_items (feed_id, item_id, published_at)"
                " VALUES (?, ?, ?)",
                (feed_id, "null-item", None),
            )
            prune_feed_items(db, feed_id, limit=2)
            ids = [
                r["item_id"]
                for r in db.execute(
                    "SELECT item_id FROM feed_items WHERE feed_id = ?",
                    (feed_id,),
                ).fetchall()
            ]
        assert "null-item" not in ids

    def test_zero_limit_disables_pruning(self, db_tmp):
        feed_id = self._seed(3)
        with get_db() as db:
            prune_feed_items(db, feed_id, limit=0)
            count = db.execute(
                "SELECT COUNT(*) AS c FROM feed_items WHERE feed_id = ?", (feed_id,)
            ).fetchone()["c"]
        assert count == 3
