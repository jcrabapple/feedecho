"""Phase 2: reader ingestion — store items on poll, decoupled from echoes."""

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture()
def env(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)

    import database

    monkeypatch.setattr(database, "DB_PATH", database.Path(path))
    database.init_db()

    import scheduler
    import notify

    monkeypatch.setattr(scheduler, "get_db", database.get_db)
    monkeypatch.setattr(notify, "get_db", database.get_db)

    yield database, scheduler

    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except OSError:
            pass


def _mk_item(id, days_ago=0.0):
    date = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    return {"id": id, "title": f"t-{id}", "link": f"https://example.com/{id}",
            "summary": "", "content": f"<p>{id}</p>", "date": date}


class TestStoreFeedItems:
    def test_inserts_and_dedupes(self, env):
        database, scheduler = env
        with database.get_db() as db:
            db.execute("INSERT INTO feeds (name, url) VALUES (?, ?)", ("f", "u"))
        items = [_mk_item("a"), _mk_item("b")]
        scheduler._store_feed_items(1, items)
        scheduler._store_feed_items(1, items)  # idempotent
        with database.get_db() as db:
            rows = db.execute(
                "SELECT item_id, title FROM feed_items ORDER BY item_id"
            ).fetchall()
        assert [(r["item_id"], r["title"]) for r in rows] == [("a", "t-a"), ("b", "t-b")]

    def test_skips_missing_id(self, env):
        database, scheduler = env
        with database.get_db() as db:
            db.execute("INSERT INTO feeds (name, url) VALUES (?, ?)", ("f", "u"))
        scheduler._store_feed_items(1, [{"title": "no id"}])
        with database.get_db() as db:
            n = db.execute("SELECT COUNT(*) AS c FROM feed_items").fetchone()["c"]
        assert n == 0

    def test_normalizes_date_to_canonical_utc_string(self, env):
        database, scheduler = env
        with database.get_db() as db:
            db.execute("INSERT INTO feeds (name, url) VALUES (?, ?)", ("f", "u"))
        scheduler._store_feed_items(1, [_mk_item("a")])
        with database.get_db() as db:
            row = db.execute(
                "SELECT published_at FROM feed_items WHERE item_id = 'a'"
            ).fetchone()
        assert "T" not in row["published_at"]
        assert row["published_at"].count(":") == 2


class TestCheckFeedReaderIngestion:
    def test_read_enabled_feed_without_echoes_is_stored(self, env, monkeypatch):
        database, scheduler = env
        with database.get_db() as db:
            db.execute(
                "INSERT INTO feeds (name, url, read_enabled) VALUES (?, ?, 1)",
                ("f", "u"),
            )
        items = [_mk_item("a"), _mk_item("b")]
        monkeypatch.setattr(scheduler, "fetch_feed", lambda url: {"items": items})
        scheduler.check_feed(1)
        with database.get_db() as db:
            rows = db.execute(
                "SELECT item_id FROM feed_items ORDER BY item_id"
            ).fetchall()
        assert [r["item_id"] for r in rows] == ["a", "b"]

    def test_read_disabled_feed_without_echoes_is_not_fetched(self, env, monkeypatch):
        database, scheduler = env
        with database.get_db() as db:
            db.execute("INSERT INTO feeds (name, url) VALUES (?, ?)", ("f", "u"))

        def boom(url):
            raise AssertionError("must not fetch")

        monkeypatch.setattr(scheduler, "fetch_feed", boom)
        scheduler.check_feed(1)  # must not raise
        with database.get_db() as db:
            n = db.execute("SELECT COUNT(*) AS c FROM feed_items").fetchone()["c"]
        assert n == 0

    def test_read_enabled_feed_with_echoes_stores_and_delivers(self, env, monkeypatch):
        database, scheduler = env
        with database.get_db() as db:
            db.execute(
                "INSERT INTO accounts (name, username, instance, access_token)"
                " VALUES (?, ?, ?, ?)",
                ("main", "user", "https://mastodon.social", "tok"),
            )
            db.execute(
                "INSERT INTO feeds (name, url, read_enabled) VALUES (?, ?, 1)",
                ("f", "u"),
            )
            db.execute(
                """INSERT INTO echoes (feed_id, destination_type, destination_id,
                                       template, visibility, enabled)
                   VALUES (1, 'mastodon', 1, '{{ title }}', 'public', 1)"""
            )
            db.execute("UPDATE feeds SET last_item_id = 'cursor' WHERE id = 1")
        # Newest-first: two new items, then the cursor item the feed has
        # already seen. get_new_items must deliver the two, and reading must
        # still store all three.
        items = [_mk_item("a"), _mk_item("b"), _mk_item("cursor")]
        posted = []
        monkeypatch.setattr(scheduler, "fetch_feed", lambda url: {"items": items})
        monkeypatch.setattr(
            scheduler, "post_status",
            lambda **kw: posted.append(kw["content"]) or {"id": "p1"},
        )
        scheduler.check_feed(1)
        with database.get_db() as db:
            rows = db.execute(
                "SELECT item_id FROM feed_items ORDER BY item_id"
            ).fetchall()
        stored = [r["item_id"] for r in rows]
        assert "a" in stored and "b" in stored
        assert posted, "echo must still deliver when reading is also enabled"
