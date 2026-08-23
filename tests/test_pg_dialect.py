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
