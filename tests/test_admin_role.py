"""Admin role plumbing: is_admin column, migration, and role checks."""

import pytest

import auth
import database
import settings


@pytest.fixture
def db_env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MULTI", False)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "admin.db")
    database.init_db()
    return database


class TestRegisterPath:
    """The real signup flow must never create admins."""

    @pytest.fixture
    def multi_client(self, monkeypatch, tmp_path):
        from fastapi.testclient import TestClient

        from app import app

        monkeypatch.setattr(settings, "MULTI", True)
        monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
        monkeypatch.setattr(settings, "AUTH_TOKEN", None)
        monkeypatch.setattr(settings, "DATABASE_URL", "")
        monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
        monkeypatch.setattr(database, "DB_PATH", tmp_path / "reg.db")
        database.init_db()
        auth._login_attempts.clear()
        auth._register_attempts.clear()
        with TestClient(app) as c:
            yield c

    def test_register_creates_non_admin(self, multi_client):
        resp = multi_client.post(
            "/register",
            data={
                "email": "fresh@example.com",
                "password": "hunter2hunter2",
                "confirm": "hunter2hunter2",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302
        with database.get_db() as db:
            row = db.execute(
                "SELECT id, is_admin FROM users WHERE email = 'fresh@example.com'"
            ).fetchone()
        assert row["is_admin"] == 0
        assert auth.is_admin(row["id"]) is False


class TestAdminRole:
    def test_fresh_init_single_mode_user_is_not_admin(self, db_env):
        # Fresh init_db_sqlite INSERT OR IGNOREs user 1 ('local'); that
        # placeholder must never be an admin in a role system.
        assert auth.is_admin(1) is False

    def test_new_users_are_not_admins(self, db_env):
        with database.get_db() as db:
            db.execute(
                "INSERT INTO users (id, email, password_hash)"
                " VALUES (2, 'plain@example.com', 'h')"
            )
            row = db.execute(
                "SELECT is_admin FROM users WHERE id = 2"
            ).fetchone()
        assert row["is_admin"] == 0
        assert auth.is_admin(2) is False

    def test_promoted_user_is_admin(self, db_env):
        with database.get_db() as db:
            db.execute(
                "INSERT INTO users (id, email, password_hash, is_admin)"
                " VALUES (3, 'boss@example.com', 'h', 1)"
            )
        assert auth.is_admin(3) is True

    def test_missing_user_is_not_admin(self, db_env):
        assert auth.is_admin(9999) is False

    def test_demotion_takes_effect_immediately(self, db_env):
        with database.get_db() as db:
            db.execute(
                "INSERT INTO users (id, email, password_hash, is_admin)"
                " VALUES (4, 'temp@example.com', 'h', 1)"
            )
        assert auth.is_admin(4) is True
        with database.get_db() as db:
            db.execute("UPDATE users SET is_admin = 0 WHERE id = 4")
        assert auth.is_admin(4) is False

    def test_migration_adds_column_to_legacy_users_table(self, monkeypatch, tmp_path):
        # Simulate a pre-is_admin database: create the users table without
        # the column, then run init_db and confirm the migration adds it.
        import sqlite3

        path = tmp_path / "legacy.db"
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE,"
            " password_hash TEXT NOT NULL DEFAULT '', plan TEXT NOT NULL DEFAULT 'trial',"
            " trial_ends_at TIMESTAMP, email_verified INTEGER NOT NULL DEFAULT 0,"
            " suspended INTEGER NOT NULL DEFAULT 0, stripe_customer_id TEXT DEFAULT '',"
            " stripe_subscription_id TEXT DEFAULT '',"
            " created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute("INSERT INTO users (id, email) VALUES (1, 'local')")
        conn.commit()
        conn.close()

        monkeypatch.setattr(settings, "MULTI", False)
        monkeypatch.setattr(settings, "AUTH_TOKEN", None)
        monkeypatch.setattr(database, "DB_PATH", path)
        database.init_db()
        with database.get_db() as db:
            row = db.execute("SELECT is_admin FROM users WHERE id = 1").fetchone()
        assert row["is_admin"] == 0
