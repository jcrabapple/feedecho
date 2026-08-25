"""Postgres dialect tests: exercise the real PG path.

Gated on FEEDCHO_TEST_PG_URL (a postgres:// URL). Skipped locally and in
the single/multi CI jobs; runs in the dedicated PG CI job with a
postgres:17-alpine service container.
"""

import os

import pytest

import database
import settings

TEST_PG_URL = os.environ.get("FEEDCHO_TEST_PG_URL", "")

pytestmark = pytest.mark.pg

requires_pg = pytest.mark.skipif(
    not TEST_PG_URL, reason="FEEDCHO_TEST_PG_URL not set; PG tests are CI-gated"
)


@pytest.fixture
def pg_env(monkeypatch):
    monkeypatch.setattr(settings, "MULTI", True)
    monkeypatch.setattr(settings, "DATABASE_URL", TEST_PG_URL)
    monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", False)
    return settings


@pytest.fixture(autouse=True)
def fresh_schema(pg_env):
    """Reset the public schema before each test.

    The CI job runs against a disposable service container, but this
    makes local re-runs against a persistent Postgres safe too: fixed
    entity ids in tests cannot collide with rows from a previous run.
    """
    with database.get_db() as db:
        db.execute("DROP SCHEMA public CASCADE")
        db.execute("CREATE SCHEMA public")
        db.execute("GRANT ALL ON SCHEMA public TO public")


@requires_pg
class TestPostgresInit:
    def test_init_db_creates_schema(self, pg_env):
        database.init_db()
        with database.get_db() as db:
            tables = db.execute(
                """
                SELECT table_name FROM information_schema.tables
                 WHERE table_schema = 'public'
                """
            ).fetchall()
        names = {row["table_name"] for row in tables}
        for expected in (
            "feeds",
            "echoes",
            "accounts",
            "email_accounts",
            "bluesky_accounts",
            "settings",
            "users",
            "posted_items",
            "oauth_apps",
            "oauth_states",
            "digest_items",
            "drip_items",
        ):
            assert expected in names, f"missing table {expected}"

    def test_init_db_is_idempotent(self, pg_env):
        database.init_db()
        database.init_db()  # second run must not raise

    def test_settings_composite_pk(self, pg_env):
        database.init_db()
        with database.get_db() as db:
            db.execute(
                "INSERT INTO users (id, email, password_hash) VALUES (1, 'u1@example.com', '')"
            )
            db.execute(
                "INSERT INTO users (id, email, password_hash) VALUES (2, 'u2@example.com', '')"
            )
            db.execute(
                "INSERT INTO settings (user_id, key, value) VALUES (1, 'k', 'v1')"
            )
            db.execute(
                "INSERT INTO settings (user_id, key, value) VALUES (2, 'k', 'v2')"
            )
            rows = db.execute(
                "SELECT user_id, value FROM settings WHERE key = 'k' ORDER BY user_id"
            ).fetchall()
        assert [(r["user_id"], r["value"]) for r in rows] == [(1, "v1"), (2, "v2")]


@requires_pg
class TestPostgresRoundtrip:
    def test_qmark_placeholder_translation(self, pg_env):
        """`?` placeholders must work through the dialect layer on PG."""
        database.init_db()
        with database.get_db() as db:
            db.execute(
                "INSERT INTO users (id, email, password_hash) VALUES (?, ?, ?)",
                (3, "roundtrip@example.com", "hash"),
            )
            row = db.execute(
                "SELECT email FROM users WHERE id = ?", (3,)
            ).fetchone()
        assert row["email"] == "roundtrip@example.com"

    def test_upsert_on_conflict(self, pg_env):
        database.init_db()
        with database.get_db() as db:
            db.execute(
                "INSERT INTO users (id, email, password_hash) VALUES (4, 'u4@example.com', 'h')"
            )
            db.execute(
                "INSERT INTO settings (user_id, key, value) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
                (4, "smtp_host", "smtp.example.com"),
            )
            db.execute(
                "INSERT INTO settings (user_id, key, value) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
                (4, "smtp_host", "smtp2.example.com"),
            )
            row = db.execute(
                "SELECT value FROM settings WHERE user_id = ? AND key = ?",
                (4, "smtp_host"),
            ).fetchone()
        assert row["value"] == "smtp2.example.com"

    def test_soft_delete_uses_current_timestamp(self, pg_env):
        database.init_db()
        with database.get_db() as db:
            db.execute(
                "INSERT INTO users (id, email, password_hash) VALUES (5, 'u5@example.com', 'h')"
            )
            db.execute(
                "INSERT INTO feeds (id, name, url, user_id) VALUES (1, 'F', 'https://x', 5)"
            )
            db.execute(
                "UPDATE feeds SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?", (1,)
            )
            row = db.execute("SELECT deleted_at FROM feeds WHERE id = ?", (1,)).fetchone()
        assert row["deleted_at"] is not None


@requires_pg
class TestPostgresMigration:
    def test_add_column_if_missing(self, pg_env):
        database.init_db()
        with database.get_db() as db:
            # Add a column the schema doesn't have, twice: both must succeed
            database._add_column_if_missing(db, "feeds", "probe_col", "TEXT")
            database._add_column_if_missing(db, "feeds", "probe_col", "TEXT")
            columns = db.execute(
                """
                SELECT column_name FROM information_schema.columns
                 WHERE table_name = 'feeds'
                """
            ).fetchall()
        assert "probe_col" in {c["column_name"] for c in columns}

    def test_oauth_states_has_user_id_column(self, pg_env):
        database.init_db()
        with database.get_db() as db:
            columns = db.execute(
                """
                SELECT column_name FROM information_schema.columns
                 WHERE table_name = 'oauth_states'
                """
            ).fetchall()
        assert "user_id" in {c["column_name"] for c in columns}

    def test_oauth_apps_website_upsert(self, pg_env, monkeypatch):
        """Issue #7: the website column must exist on PG and the registration
        upsert (4 columns, ON CONFLICT) must round-trip through dict_row."""
        import oauth
        import settings as settings_mod

        database.init_db()
        with database.get_db() as db:
            columns = db.execute(
                """
                SELECT column_name FROM information_schema.columns
                 WHERE table_name = 'oauth_apps'
                """
            ).fetchall()
        assert "website" in {c["column_name"] for c in columns}

        calls = []

        class _Resp:
            def __init__(self, n):
                self._n = n

            def raise_for_status(self):
                pass

            def json(self):
                return {"client_id": f"pg-cid-{self._n}", "client_secret": "s"}

        class _Client:
            def __init__(self, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, data=None, **kw):
                calls.append(data)
                return _Resp(len(calls))

        monkeypatch.setattr(oauth.httpx, "Client", _Client)
        monkeypatch.setattr(oauth, "validate_outbound_url", lambda url: None)
        monkeypatch.setattr(settings_mod, "APP_WEBSITE", "https://pg.example.org")

        first = oauth.get_or_create_app("https://pg.mastodon.example")
        assert calls[0]["website"] == "https://pg.example.org"
        # Cached path: reads row["website"] off a psycopg dict_row.
        assert oauth.get_or_create_app("https://pg.mastodon.example") == first
        assert len(calls) == 1

        # Changed config re-registers through the ON CONFLICT branch.
        monkeypatch.setattr(settings_mod, "APP_WEBSITE", "https://pg2.example.org")
        second = oauth.get_or_create_app("https://pg.mastodon.example")
        assert len(calls) == 2
        assert second["client_id"] != first["client_id"]
        with database.get_db() as db:
            rows = db.execute("SELECT client_id, website FROM oauth_apps").fetchall()
        assert len(rows) == 1
        assert rows[0]["website"] == "https://pg2.example.org"
        assert rows[0]["client_id"] == second["client_id"]

    def test_users_has_is_admin_column(self, pg_env):
        database.init_db()
        with database.get_db() as db:
            columns = db.execute(
                """
                SELECT column_name FROM information_schema.columns
                 WHERE table_name = 'users'
                """
            ).fetchall()
            assert "is_admin" in {c["column_name"] for c in columns}
            db.execute(
                "INSERT INTO users (id, email, password_hash) VALUES (1, 'local', '')"
            )
            row = db.execute(
                "SELECT is_admin FROM users WHERE id = 1"
            ).fetchone()
        assert row["is_admin"] == 0
        # Exercise the qmark placeholder path of auth.is_admin on PG.
        import auth as auth_mod

        assert auth_mod.is_admin(1) is False
        with database.get_db() as db:
            db.execute("UPDATE users SET is_admin = 1 WHERE id = 1")
        assert auth_mod.is_admin(1) is True

    def test_system_settings_table_and_upsert(self, pg_env):
        database.init_db()
        with database.get_db() as db:
            db.execute(
                "INSERT INTO system_settings (key, value) VALUES ('smtp_host', 'a.example.com')"
            )
            db.execute(
                "INSERT INTO system_settings (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("smtp_host", "b.example.com"),
            )
            rows = db.execute(
                "SELECT key, value FROM system_settings WHERE key = 'smtp_host'"
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["value"] == "b.example.com"

    def test_email_tokens_roundtrip_on_pg(self, pg_env):
        import verification

        database.init_db()
        with database.get_db() as db:
            db.execute(
                "INSERT INTO users (id, email, password_hash)"
                " VALUES (77, 'tok@example.com', '')"
            )
        token = verification.issue_token(77, "verify")
        assert verification.consume_token(token, "verify") == 77
        # Single use on PG too
        assert verification.consume_token(token, "verify") is None


@requires_pg
class TestAppOnPostgres:
    """Full app request against PG — catches dialect leaks that schema-only
    tests can't (e.g. positional row indexing that works on sqlite Row but
    raises KeyError on psycopg dict rows)."""

    def test_dashboard_renders_on_pg(self, pg_env, monkeypatch):
        from fastapi.testclient import TestClient

        import auth as auth_mod
        import security
        from app import app

        monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
        auth_mod._login_attempts.clear()
        auth_mod._register_attempts.clear()
        database.init_db()
        with database.get_db() as db:
            db.execute(
                "INSERT INTO users (id, email, password_hash, plan, trial_ends_at)"
                " VALUES (9, 'pg@example.com', '', 'trial', NULL)"
            )
        with TestClient(app) as c:
            c.cookies.set("feedecho_session", security.sign_session(9, "pg@example.com"))
            resp = c.get("/")
        assert resp.status_code == 200
        assert "Dashboard" in resp.text

    def test_admin_page_renders_on_pg(self, pg_env, monkeypatch):
        from fastapi.testclient import TestClient

        import auth as auth_mod
        import security
        from app import app

        monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
        auth_mod._login_attempts.clear()
        auth_mod._register_attempts.clear()
        database.init_db()
        with database.get_db() as db:
            db.execute(
                "INSERT INTO users (id, email, password_hash, is_admin)"
                " VALUES (9, 'boss@example.com', '', 1)"
            )
            db.execute(
                "INSERT INTO users (id, email, password_hash)"
                " VALUES (10, 'minion@example.com', '')"
            )
        with TestClient(app) as c:
            c.cookies.set("feedecho_session", security.sign_session(9, "boss@example.com"))
            resp = c.get("/admin")
        assert resp.status_code == 200
        assert "boss@example.com" in resp.text
        assert "minion@example.com" in resp.text

    def test_feed_edit_roundtrip_on_pg(self, pg_env, monkeypatch):
        from fastapi.testclient import TestClient

        import auth as auth_mod
        import security
        from app import app

        monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
        auth_mod._login_attempts.clear()
        auth_mod._register_attempts.clear()
        database.init_db()
        with database.get_db() as db:
            db.execute(
                "INSERT INTO users (id, email, password_hash)"
                " VALUES (9, 'pg@example.com', '')"
            )
            db.execute(
                "INSERT INTO feeds (name, url, poll_interval, last_item_id, user_id)"
                " VALUES ('PG Feed', 'https://example.com/feed.xml', 15, 'item-1', 9)"
            )
            feed_id = db.execute(
                "SELECT id FROM feeds WHERE user_id = 9"
            ).fetchone()["id"]
        with TestClient(app) as c:
            c.cookies.set("feedecho_session", security.sign_session(9, "pg@example.com"))
            edit = c.post(
                f"/api/feeds/{feed_id}/edit",
                data={
                    "name": "PG Renamed",
                    "url": "https://example.com/other.xml",
                    "poll_interval": "45",
                },
                follow_redirects=False,
            )
            page = c.get("/feeds")
        assert edit.status_code == 303
        assert page.status_code == 200
        assert "PG Renamed" in page.text
        with database.get_db() as db:
            row = db.execute(
                "SELECT name, url, poll_interval, last_item_id FROM feeds"
                f" WHERE id = {feed_id}"
            ).fetchone()
        assert row["name"] == "PG Renamed"
        assert row["url"] == "https://example.com/other.xml"
        assert row["poll_interval"] == 45
        assert row["last_item_id"] is None

    def test_history_page_renders_on_pg(self, pg_env, monkeypatch):
        """Timestamps come back as datetime objects on PG; the iso_utc /
        utc_text filters must render them without calling string methods."""
        from fastapi.testclient import TestClient

        import auth as auth_mod
        import security
        from app import app

        monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
        auth_mod._login_attempts.clear()
        auth_mod._register_attempts.clear()
        database.init_db()
        with database.get_db() as db:
            db.execute(
                "INSERT INTO users (id, email, password_hash)"
                " VALUES (9, 'pg@example.com', '')"
            )
            db.execute(
                "INSERT INTO feeds (id, name, url, user_id)"
                " VALUES (1, 'PG Feed', 'https://example.com/feed.xml', 9)"
            )
            db.execute(
                "INSERT INTO echoes (id, feed_id, destination_type, destination_id,"
                " template, user_id)"
                " VALUES (1, 1, 'mastodon', 1, '{{ title }}', 9)"
            )
            db.execute(
                "INSERT INTO posted_items (echo_id, item_id, item_title, status)"
                " VALUES (1, 'pg-item-1', 'PG Item', 'queued')"
            )
        with TestClient(app) as c:
            c.cookies.set("feedecho_session", security.sign_session(9, "pg@example.com"))
            page = c.get("/history")
            dash = c.get("/")
        assert page.status_code == 200
        assert "Queued" in page.text
        assert "held for drip rate limit" in page.text
        assert '<span class="badge badge-danger">Failed</span>' not in page.text
        # The filter received a psycopg datetime and rendered a valid ISO
        # attribute (this is the crash path the test exists to cover).
        import re

        assert re.search(
            r'datetime="\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"', page.text
        )
        assert dash.status_code == 200
        assert "Queued" in dash.text
