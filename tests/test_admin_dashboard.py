"""Admin dashboard: gating, user management actions, nav visibility."""

import pytest
from fastapi.testclient import TestClient

import auth
import database
import security
import settings
from app import app

ADMIN_ID = 10
USER_ID = 11


@pytest.fixture
def multi_env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MULTI", True)
    monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "admin.db")
    database.init_db()
    auth._login_attempts.clear()
    auth._register_attempts.clear()
    with database.get_db() as db:
        db.execute(
            "INSERT INTO users (id, email, password_hash, is_admin)"
            " VALUES (?, 'admin@example.com', '', 1)",
            (ADMIN_ID,),
        )
        db.execute(
            "INSERT INTO users (id, email, password_hash, is_admin)"
            " VALUES (?, 'user@example.com', '', 0)",
            (USER_ID,),
        )
    return settings


def _client(uid, email):
    c = TestClient(app)
    c.cookies.set("feedecho_session", security.sign_session(uid, email))
    return c


class TestAdminGating:
    def test_admin_page_for_admin(self, multi_env):
        with _client(ADMIN_ID, "admin@example.com") as c:
            resp = c.get("/admin")
        assert resp.status_code == 200
        assert "admin@example.com" in resp.text
        assert "user@example.com" in resp.text

    def test_admin_page_forbidden_for_regular_user(self, multi_env):
        with _client(USER_ID, "user@example.com") as c:
            resp = c.get("/admin")
        assert resp.status_code == 403
        assert "Admin access required" in resp.text

    def test_admin_page_redirects_unauthenticated(self, multi_env):
        with TestClient(app) as c:
            resp = c.get(
                "/admin", headers={"accept": "text/html"}, follow_redirects=False
            )
        assert resp.status_code in (302, 303)

    def test_admin_page_404_in_single_mode(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "MULTI", False)
        monkeypatch.setattr(settings, "AUTH_TOKEN", None)
        monkeypatch.setattr(database, "DB_PATH", tmp_path / "single.db")
        database.init_db()
        with TestClient(app) as c:
            resp = c.get("/admin")
        assert resp.status_code == 404

    def test_nav_links_show_admin_only_for_admins(self, multi_env):
        with _client(ADMIN_ID, "admin@example.com") as c:
            assert 'href="/admin"' in c.get("/").text
        with _client(USER_ID, "user@example.com") as c:
            assert 'href="/admin"' not in c.get("/").text


class TestAdminActions:
    def test_suspend_and_unsuspend(self, multi_env):
        with _client(ADMIN_ID, "admin@example.com") as c:
            resp = c.post(f"/admin/users/{USER_ID}/suspend", follow_redirects=False)
            assert resp.status_code == 302
        with database.get_db() as db:
            row = db.execute(
                "SELECT suspended FROM users WHERE id = ?", (USER_ID,)
            ).fetchone()
        assert row["suspended"] == 1
        # Suspend is idempotent (atomic target state)
        with _client(ADMIN_ID, "admin@example.com") as c:
            c.post(f"/admin/users/{USER_ID}/suspend", follow_redirects=False)
        with database.get_db() as db:
            row = db.execute(
                "SELECT suspended FROM users WHERE id = ?", (USER_ID,)
            ).fetchone()
        assert row["suspended"] == 1
        with _client(ADMIN_ID, "admin@example.com") as c:
            c.post(f"/admin/users/{USER_ID}/unsuspend", follow_redirects=False)
        with database.get_db() as db:
            row = db.execute(
                "SELECT suspended FROM users WHERE id = ?", (USER_ID,)
            ).fetchone()
        assert row["suspended"] == 0

    def test_suspended_session_is_rejected_per_request(self, multi_env):
        # A suspended user with a valid HMAC session must lose access
        # immediately, not at cookie expiry.
        with database.get_db() as db:
            db.execute("UPDATE users SET suspended = 1 WHERE id = ?", (USER_ID,))
        with _client(USER_ID, "user@example.com") as c:
            resp = c.get("/", follow_redirects=False)
        assert resp.status_code in (302, 303, 401)
        # Unsuspend restores the same session
        with database.get_db() as db:
            db.execute("UPDATE users SET suspended = 0 WHERE id = ?", (USER_ID,))
        with _client(USER_ID, "user@example.com") as c:
            resp = c.get("/", follow_redirects=False)
        assert resp.status_code == 200

    def test_admin_cannot_suspend_self(self, multi_env):
        with _client(ADMIN_ID, "admin@example.com") as c:
            resp = c.post(f"/admin/users/{ADMIN_ID}/suspend", follow_redirects=False)
        assert resp.status_code == 400

    def test_promote_and_demote(self, multi_env):
        with _client(ADMIN_ID, "admin@example.com") as c:
            c.post(f"/admin/users/{USER_ID}/promote", follow_redirects=False)
        with database.get_db() as db:
            row = db.execute(
                "SELECT is_admin FROM users WHERE id = ?", (USER_ID,)
            ).fetchone()
        assert row["is_admin"] == 1
        # Now demote again
        with _client(ADMIN_ID, "admin@example.com") as c:
            c.post(f"/admin/users/{USER_ID}/demote", follow_redirects=False)
        with database.get_db() as db:
            row = db.execute(
                "SELECT is_admin FROM users WHERE id = ?", (USER_ID,)
            ).fetchone()
        assert row["is_admin"] == 0

    def test_admin_cannot_demote_self(self, multi_env):
        with _client(ADMIN_ID, "admin@example.com") as c:
            resp = c.post(f"/admin/users/{ADMIN_ID}/demote", follow_redirects=False)
        assert resp.status_code == 400

    def test_non_admin_cannot_act(self, multi_env):
        with _client(USER_ID, "user@example.com") as c:
            resp = c.post(f"/admin/users/{USER_ID}/promote", follow_redirects=False)
        assert resp.status_code == 403
        with database.get_db() as db:
            row = db.execute(
                "SELECT is_admin FROM users WHERE id = ?", (USER_ID,)
            ).fetchone()
        assert row["is_admin"] == 0

    def test_missing_user_404(self, multi_env):
        with _client(ADMIN_ID, "admin@example.com") as c:
            resp = c.post("/admin/users/999/suspend", follow_redirects=False)
        assert resp.status_code == 404


class TestLastAdminGuard:
    """Unit tests for _admin_guard_last_admin: through the UI the self-guards
    fire first, so the guard is defense-in-depth for future bulk actions."""

    def test_demote_last_admin_bit_returns_error(self, multi_env):
        from app import _admin_guard_last_admin

        with database.get_db() as db:
            db.execute("UPDATE users SET is_admin = 0 WHERE id = ?", (ADMIN_ID,))
            db.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (USER_ID,))
            err = _admin_guard_last_admin(db, USER_ID, "is_admin")
        assert err is not None

    def test_demote_with_second_admin_passes(self, multi_env):
        from app import _admin_guard_last_admin

        with database.get_db() as db:
            db.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (USER_ID,))
            err = _admin_guard_last_admin(db, USER_ID, "is_admin")
        assert err is None

    def test_suspend_last_active_admin_returns_error(self, multi_env):
        from app import _admin_guard_last_admin

        with database.get_db() as db:
            db.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (USER_ID,))
            db.execute("UPDATE users SET suspended = 1 WHERE id = ?", (USER_ID,))
            # Only ACTIVE admin is ADMIN_ID.
            err = _admin_guard_last_admin(db, ADMIN_ID, "suspended")
        assert err is not None

    def test_suspend_admin_with_another_active_admin_passes(self, multi_env):
        from app import _admin_guard_last_admin

        with database.get_db() as db:
            db.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (USER_ID,))
            err = _admin_guard_last_admin(db, ADMIN_ID, "suspended")
        assert err is None

    def test_suspend_non_admin_never_guarded(self, multi_env):
        from app import _admin_guard_last_admin

        with database.get_db() as db:
            err = _admin_guard_last_admin(db, USER_ID, "suspended")
        assert err is None


class TestAdminBootstrap:
    def test_admin_email_env_promotes_on_startup(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "MULTI", True)
        monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
        monkeypatch.setattr(settings, "AUTH_TOKEN", None)
        monkeypatch.setattr(settings, "DATABASE_URL", "")
        monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
        monkeypatch.setattr(settings, "ADMIN_EMAIL", "jason@example.com")
        monkeypatch.setattr(database, "DB_PATH", tmp_path / "boot.db")
        database.init_db()
        with database.get_db() as db:
            db.execute(
                "INSERT INTO users (id, email, password_hash)"
                " VALUES (50, 'jason@example.com', '')"
            )
        with TestClient(app):
            pass  # lifespan runs _bootstrap_admin
        assert auth.is_admin(50) is True

    def test_bootstrap_is_idempotent_and_ignores_other_users(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "MULTI", True)
        monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
        monkeypatch.setattr(settings, "AUTH_TOKEN", None)
        monkeypatch.setattr(settings, "DATABASE_URL", "")
        monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
        monkeypatch.setattr(settings, "ADMIN_EMAIL", "jason@example.com")
        monkeypatch.setattr(database, "DB_PATH", tmp_path / "boot2.db")
        database.init_db()
        with database.get_db() as db:
            db.execute(
                "INSERT INTO users (id, email, password_hash)"
                " VALUES (51, 'jason@example.com', '')"
            )
            db.execute(
                "INSERT INTO users (id, email, password_hash)"
                " VALUES (52, 'other@example.com', '')"
            )
        with TestClient(app):
            pass
        with TestClient(app):
            pass  # second startup must not raise or change anything
        assert auth.is_admin(51) is True
        assert auth.is_admin(52) is False
