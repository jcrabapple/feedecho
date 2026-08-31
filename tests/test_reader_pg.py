"""Postgres coverage for the reader storage path (Phase 2)."""

import os

import pytest

import database
import settings

TEST_PG_URL = os.environ.get("FEEDECHO_TEST_PG_URL", "")

pytestmark = pytest.mark.pg

requires_pg = pytest.mark.skipif(
    not TEST_PG_URL, reason="FEEDECHO_TEST_PG_URL not set; PG tests are CI-gated"
)


@pytest.fixture
def pg_env(monkeypatch):
    monkeypatch.setattr(settings, "MULTI", True)
    monkeypatch.setattr(settings, "DATABASE_URL", TEST_PG_URL)
    monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", False)
    return settings


@pytest.fixture(autouse=True)
def fresh_schema(pg_env):
    with database.get_db() as db:
        db.execute("DROP SCHEMA public CASCADE")
        db.execute("CREATE SCHEMA public")
        db.execute("GRANT ALL ON SCHEMA public TO public")


def _items():
    return [
        {"id": "a", "title": "t-a", "link": "l", "summary": "", "content": "",
         "date": "2026-01-01T00:00:00+00:00"},
        {"id": "b", "title": "t-b", "link": "l", "summary": "", "content": "",
         "date": "2026-01-02T00:00:00+00:00"},
        {"id": "null", "title": "t-null", "link": "l", "date": None},
    ]


@requires_pg
class TestReaderStoragePg:
    def test_store_feed_items_dedupe_and_prune(self, pg_env):
        import scheduler

        database.init_db()
        with database.get_db() as db:
            db.execute(
                "INSERT INTO feeds (name, url, read_enabled, user_id)"
                " VALUES (?, ?, 1, 1)",
                ("f", "https://example.com/feed"),
            )
        scheduler._store_feed_items(1, _items())
        scheduler._store_feed_items(1, _items())  # idempotent
        with database.get_db() as db:
            rows = db.execute(
                "SELECT item_id FROM feed_items WHERE feed_id = 1 ORDER BY item_id"
            ).fetchall()
            assert [r["item_id"] for r in rows] == ["a", "b", "null"]

        with database.get_db() as db:
            database.prune_feed_items(db, 1, limit=2)
            ids = [
                r["item_id"]
                for r in db.execute(
                    "SELECT item_id FROM feed_items WHERE feed_id = 1 ORDER BY item_id"
                ).fetchall()
            ]
        # The NULL-published item is pruned first (portable NULL-last order).
        assert ids == ["a", "b"]
