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
    # v1.46.0: validate_config() requires a Fernet key in real multi mode.
    from cryptography.fernet import Fernet

    monkeypatch.setattr(settings, "CREDENTIAL_KEY", Fernet.generate_key().decode())
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

    def test_prune_starred_items_exempt_pg(self, pg_env):
        import scheduler

        database.init_db()
        with database.get_db() as db:
            db.execute(
                "INSERT INTO feeds (name, url, read_enabled, user_id)"
                " VALUES (?, ?, 1, 1)",
                ("f", "https://example.com/feed"),
            )
        scheduler._store_feed_items(1, _items())
        with database.get_db() as db:
            # Star item 'null' (which would otherwise be pruned first)
            db.execute("UPDATE feed_items SET starred = 1 WHERE feed_id = 1 AND item_id = 'null'")
            database.prune_feed_items(db, 1, limit=1)
            rows = db.execute(
                "SELECT item_id, starred FROM feed_items WHERE feed_id = 1 ORDER BY item_id"
            ).fetchall()
            ids = [r["item_id"] for r in rows]
            # 'null' is starred so it survives; of 'a' and 'b', 'b' is newer so 1 unstarred kept is 'b'
            assert ids == ["b", "null"]

    def test_reader_keyset_pagination_pg(self, pg_env, monkeypatch):
        import auth as auth_mod
        import security
        import scheduler
        from fastapi.testclient import TestClient
        from app import app

        monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
        monkeypatch.setattr(scheduler, "check_all_feeds", lambda: None)
        auth_mod._login_attempts.clear()
        auth_mod._register_attempts.clear()
        database.init_db()
        with database.get_db() as db:
            db.execute(
                "INSERT INTO users (id, email, password_hash, plan) VALUES (?, ?, '', 'paid')",
                (101, "pg-page@example.com"),
            )
            db.execute(
                "INSERT INTO feeds (id, name, url, read_enabled, user_id)"
                " VALUES (10, 'PgFeed', 'https://example.com/pgfeed', 1, 101)"
            )
            for i in range(55):
                db.execute(
                    "INSERT INTO feed_items (feed_id, item_id, title, published_at, is_read)"
                    " VALUES (10, ?, ?, ?, 0)",
                    (f"pg-item-{i:02d}", f"Pg Item {i}", f"2026-01-01 00:{i:02d}:00"),
                )

        client = TestClient(app)
        client.cookies.set("feedecho_session", security.sign_session(101, "pg-page@example.com"))

        resp = client.get("/reader?view=all")
        assert resp.status_code == 200
        assert 'data-next-cursor="' in resp.text
        # Page 1 has 50 items
        import re

        page1_ids = re.findall(r'<li id="item-(\d+)" class="reader-entry"', resp.text)
        assert len(page1_ids) == 50

        # Extract next cursor and fetch page 2
        cursor_match = re.search(r'data-next-cursor="([^"]+)"', resp.text)
        assert cursor_match is not None
        cursor = cursor_match.group(1)
        resp2 = client.get(f"/reader?view=all&after={cursor}")
        assert resp2.status_code == 200
        page2_ids = re.findall(r'<li id="item-(\d+)" class="reader-entry"', resp2.text)
        assert len(page2_ids) == 5
        # No overlap between page 1 and page 2
        assert not set(page1_ids).intersection(set(page2_ids))


@requires_pg
class TestReaderPagePg:
    def test_reader_page_renders_against_pg(self, pg_env, monkeypatch):
        import auth as auth_mod
        import security
        import scheduler
        from fastapi.testclient import TestClient
        from app import app

        monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
        # The lifespan starts the scheduler; no-op its feed sweep so the
        # background thread does no DB work that could race monkeypatch
        # teardown (which flips the dialect back to sqlite mid-query).
        monkeypatch.setattr(scheduler, "check_all_feeds", lambda: None)
        auth_mod._login_attempts.clear()
        auth_mod._register_attempts.clear()
        database.init_db()
        with database.get_db() as db:
            db.execute(
                "INSERT INTO users (id, email, password_hash, plan) VALUES (?, ?, '', 'paid')",
                (11, "r@example.com"),
            )
            db.execute(
                "INSERT INTO feeds (name, url, read_enabled, user_id) VALUES (?, ?, 1, ?)",
                ("F", "https://example.com/feed", 11),
            )
            db.execute(
                "INSERT INTO feed_items (feed_id, item_id, title, is_read) VALUES (1, 'a', 'PG Item', 0)"
            )

        with TestClient(app) as c:
            c.cookies.set("feedecho_session", security.sign_session(11, "r@example.com"))
            resp = c.get("/reader")
        assert resp.status_code == 200
        assert "PG Item" in resp.text


@requires_pg
class TestReaderTier2Pg:
    def test_mutes_and_bulk_mark_read_unread_against_pg(self, pg_env, monkeypatch):
        import auth as auth_mod
        import security
        import scheduler
        from fastapi.testclient import TestClient
        from app import app

        monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
        monkeypatch.setattr(scheduler, "check_all_feeds", lambda: None)
        auth_mod._login_attempts.clear()
        auth_mod._register_attempts.clear()
        database.init_db()
        with database.get_db() as db:
            db.execute(
                "INSERT INTO users (id, email, password_hash, plan) VALUES (?, ?, '', 'paid')",
                (11, "r@example.com"),
            )
            db.execute(
                "INSERT INTO feeds (name, url, read_enabled, mute_keywords, user_id)"
                " VALUES (?, ?, 1, ?, ?)",
                ("F", "https://example.com/feed", "noise", 11),
            )
            db.execute(
                "INSERT INTO feed_items (feed_id, item_id, title, content, is_read)"
                " VALUES (1, 'a', 'Good', 'clean', 0)"
            )
            db.execute(
                "INSERT INTO feed_items (feed_id, item_id, title, content, is_read)"
                " VALUES (1, 'b', 'Bad', 'noise here', 0)"
            )

        with TestClient(app) as c:
            c.cookies.set("feedecho_session", security.sign_session(11, "r@example.com"))
            # mute hides the "noise" item (COALESCE keeps NULL-safe on PG)
            page = c.get("/reader", params={"view": "all"}).text
            assert "Good" in page
            assert "Bad" not in page
            # bulk mark-read + mark-unread roundtrip via IN placeholders
            assert c.post("/api/reader/mark-read", data={"ids": "1,2"}).json()["count"] == 2
            assert c.post("/api/reader/mark-unread", data={"ids": "1,2"}).json()["count"] == 2
            # today view (items have no published_at, so empty but 200)
            assert c.get("/reader", params={"view": "today"}).status_code == 200

