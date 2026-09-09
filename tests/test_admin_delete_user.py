"""Admin user deletion (v1.53.0): spam-cleanup counterpart to Suspend.

POST /admin/users/{user_id}/delete hard-deletes the user and every owned
row by reusing the self-serve `_hard_delete_user` machinery (D4), with:

- admin gating (_require_admin; single mode 404s via _require_admin)
- self-delete refusal
- last-active-admin guard (same contract as suspend/demote)
- typed-email confirmation (confirm_email must match the account email,
  case-insensitive) — an irreversible bulk-data delete needs a deliberate
  second step beyond one click
- billing deletion hooks run first and may VETO (AccountDeletionAbort) so
  a paying customer is never deleted with a live subscription
- every owned row gone afterwards (feeds, echoes, destinations, queued
  posts, posted history, settings, invite claims, the user row itself)
"""

import os

import pytest
from fastapi.testclient import TestClient

import auth
import database
import security
import settings
from app import app

ADMIN_ID = 10
SPAM_ID = 11
OTHER_ADMIN_ID = 12


@pytest.fixture
def multi_env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MULTI", True)
    monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "admin-delete.db")
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
            " VALUES (?, 'spam@uberip.com', '', 0)",
            (SPAM_ID,),
        )
        db.execute(
            "INSERT INTO users (id, email, password_hash, is_admin)"
            " VALUES (?, 'second-admin@example.com', '', 1)",
            (OTHER_ADMIN_ID,),
        )
    return settings


def _client(uid, email):
    c = TestClient(app)
    c.cookies.set("feedecho_session", security.sign_session(uid, email))
    return c


def _seed_spam_data(uid):
    """Give the spam account owned rows so the delete provably cascades."""
    with database.get_db() as db:
        db.execute(
            "INSERT INTO feeds (id, user_id, name, url) VALUES (900, ?, 'f', 'https://x.example/rss')",
            (uid,),
        )
        db.execute(
            "INSERT INTO echoes (id, feed_id, user_id, destination_type, destination_id,"
            " template, visibility, filter_keywords, filter_mode, content_warning,"
            " attach_image, enabled)"
            " VALUES (900, 900, ?, 'mastodon', 1, 't', 'public', '', 'exclude', '', 0, 1)",
            (uid,),
        )
        db.execute(
            "INSERT INTO settings (user_id, key, value) VALUES (?, 'k', 'v')",
            (uid,),
        )


class TestAdminDeleteUser:
    def test_delete_removes_user_and_all_owned_rows(self, multi_env):
        _seed_spam_data(SPAM_ID)
        with _client(ADMIN_ID, "admin@example.com") as c:
            resp = c.post(
                f"/admin/users/{SPAM_ID}/delete",
                data={"confirm_email": "spam@uberip.com"},
                follow_redirects=False,
            )
        assert resp.status_code == 302
        with database.get_db() as db:
            assert db.execute("SELECT COUNT(*) c FROM users WHERE id = ?", (SPAM_ID,)).fetchone()["c"] == 0
            assert db.execute("SELECT COUNT(*) c FROM feeds WHERE id = 900").fetchone()["c"] == 0
            assert db.execute("SELECT COUNT(*) c FROM echoes WHERE id = 900").fetchone()["c"] == 0
            assert db.execute("SELECT COUNT(*) c FROM settings WHERE user_id = ?", (SPAM_ID,)).fetchone()["c"] == 0

    def test_confirmation_email_is_required_and_case_insensitive(self, multi_env):
        with _client(ADMIN_ID, "admin@example.com") as c:
            wrong = c.post(
                f"/admin/users/{SPAM_ID}/delete",
                data={"confirm_email": "wrong@example.com"},
                follow_redirects=False,
            )
            assert wrong.status_code == 400
            missing = c.post(
                f"/admin/users/{SPAM_ID}/delete", data={}, follow_redirects=False
            )
            assert missing.status_code == 400
            # Case-insensitive match passes (deliberate typing convenience)
            ok = c.post(
                f"/admin/users/{SPAM_ID}/delete",
                data={"confirm_email": "  SPAM@UBERIP.com  "},
                follow_redirects=False,
            )
            assert ok.status_code == 302
        with database.get_db() as db:
            assert db.execute("SELECT COUNT(*) c FROM users WHERE id = ?", (SPAM_ID,)).fetchone()["c"] == 0

    def test_cannot_delete_self(self, multi_env):
        with _client(ADMIN_ID, "admin@example.com") as c:
            resp = c.post(
                f"/admin/users/{ADMIN_ID}/delete",
                data={"confirm_email": "admin@example.com"},
                follow_redirects=False,
            )
        assert resp.status_code == 400
        assert "own account" in resp.text
        with database.get_db() as db:
            assert db.execute("SELECT COUNT(*) c FROM users WHERE id = ?", (ADMIN_ID,)).fetchone()["c"] == 1

    def test_last_admin_cannot_self_delete(self, multi_env):
        """The last-admin protection for DELETE is the self-delete refusal:
        the caller is an admin, so deleting ANOTHER admin always leaves at
        least one admin (themselves). Deleting the final admin is therefore
        only ever a self-delete, which this pins."""
        with database.get_db() as db:
            db.execute("DELETE FROM users WHERE id = ?", (ADMIN_ID,))
            # OTHER_ADMIN_ID is now the only admin
        with _client(OTHER_ADMIN_ID, "second-admin@example.com") as c:
            resp = c.post(
                f"/admin/users/{OTHER_ADMIN_ID}/delete",
                data={"confirm_email": "second-admin@example.com"},
                follow_redirects=False,
            )
        assert resp.status_code == 400
        assert "own account" in resp.text
        with database.get_db() as db:
            assert db.execute("SELECT COUNT(*) c FROM users WHERE id = ?", (OTHER_ADMIN_ID,)).fetchone()["c"] == 1

    def test_admin_can_delete_another_admin_when_not_last(self, multi_env):
        with _client(ADMIN_ID, "admin@example.com") as c:
            resp = c.post(
                f"/admin/users/{OTHER_ADMIN_ID}/delete",
                data={"confirm_email": "second-admin@example.com"},
                follow_redirects=False,
            )
        assert resp.status_code == 302
        with database.get_db() as db:
            assert db.execute("SELECT COUNT(*) c FROM users WHERE id = ?", (OTHER_ADMIN_ID,)).fetchone()["c"] == 0

    def test_gating_regular_user_forbidden_and_anonymous_redirects(self, multi_env):
        with database.get_db() as db:
            db.execute(
                "INSERT INTO users (id, email, password_hash, is_admin) VALUES (99, 'u99@example.com', '', 0)",
            )
        with _client(99, "u99@example.com") as c:
            resp = c.post(
                "/admin/users/99/delete",
                data={"confirm_email": "u99@example.com"},
                follow_redirects=False,
            )
        assert resp.status_code == 403
        # Anonymous admin-action POSTs are 401 (no session at all —
        # AuthMiddleware rejects before _require_admin's 403 branch).
        with TestClient(app) as c:
            resp = c.post(
                "/admin/users/99/delete",
                data={"confirm_email": "u99@example.com"},
                follow_redirects=False,
            )
        assert resp.status_code == 401
        with database.get_db() as db:
            assert db.execute("SELECT COUNT(*) c FROM users WHERE id = 99").fetchone()["c"] == 1

    def test_unknown_user_404(self, multi_env):
        with _client(ADMIN_ID, "admin@example.com") as c:
            resp = c.post(
                "/admin/users/424242/delete",
                data={"confirm_email": "ghost@example.com"},
                follow_redirects=False,
            )
        assert resp.status_code == 404

    def test_billing_veto_aborts_deletion(self, multi_env, monkeypatch):
        """The deletion hooks run before the delete; an AccountDeletionAbort
        (Stripe unreachable, live subscription) must fail closed."""
        import app as app_module

        vetoed = []

        def _veto(uid):
            vetoed.append(uid)
            raise app_module.AccountDeletionAbort("Stripe is unreachable; not deleting a paying customer")

        monkeypatch.setitem(app_module.__dict__, "_account_deletion_hooks", [_veto])
        with database.get_db() as db:
            db.execute(
                "UPDATE users SET plan='paid', stripe_customer_id='cus_X', stripe_subscription_id='sub_X' WHERE id = ?",
                (SPAM_ID,),
            )
        with _client(ADMIN_ID, "admin@example.com") as c:
            resp = c.post(
                f"/admin/users/{SPAM_ID}/delete",
                data={"confirm_email": "spam@uberip.com"},
                follow_redirects=False,
            )
        assert resp.status_code == 400
        assert "not deleting" in resp.text
        assert vetoed == [SPAM_ID]
        with database.get_db() as db:
            assert db.execute("SELECT COUNT(*) c FROM users WHERE id = ?", (SPAM_ID,)).fetchone()["c"] == 1

    def test_deletion_hook_failure_does_not_block_delete(self, multi_env, monkeypatch):
        """A best-effort hook raising a non-abort exception must not strand
        the spam row (same contract as the self-serve path)."""
        import app as app_module

        calls = []

        def _boom(uid):
            calls.append(uid)
            raise RuntimeError("webhook flaked")

        monkeypatch.setitem(app_module.__dict__, "_account_deletion_hooks", [_boom])
        with _client(ADMIN_ID, "admin@example.com") as c:
            resp = c.post(
                f"/admin/users/{SPAM_ID}/delete",
                data={"confirm_email": "spam@uberip.com"},
                follow_redirects=False,
            )
        assert resp.status_code == 302
        assert calls == [SPAM_ID]
        with database.get_db() as db:
            assert db.execute("SELECT COUNT(*) c FROM users WHERE id = ?", (SPAM_ID,)).fetchone()["c"] == 0

    def test_admin_page_renders_delete_form_with_confirm_input(self, multi_env):
        with _client(ADMIN_ID, "admin@example.com") as c:
            resp = c.get("/admin")
        assert resp.status_code == 200
        assert f'/admin/users/{SPAM_ID}/delete' in resp.text
        assert 'name="confirm_email"' in resp.text
        assert "adminConfirmDelete" in resp.text

    def test_cannot_delete_last_admin_account(self, multi_env, monkeypatch):
        """When an admin account is the last remaining admin, attempting to
        delete it (e.g. via a concurrent demotion of the caller) must be
        refused by _admin_guard_last_admin."""
        import app as app_module

        monkeypatch.setattr(app_module, "_require_admin", lambda req: ADMIN_ID)
        with database.get_db() as db:
            db.execute("UPDATE users SET is_admin = 0 WHERE id = ?", (ADMIN_ID,))
        with _client(ADMIN_ID, "admin@example.com") as c:
            resp = c.post(
                f"/admin/users/{OTHER_ADMIN_ID}/delete",
                data={"confirm_email": "second-admin@example.com"},
                follow_redirects=False,
            )
        assert resp.status_code == 400
        assert "last admin" in resp.text
        with database.get_db() as db:
            assert db.execute("SELECT COUNT(*) c FROM users WHERE id = ?", (OTHER_ADMIN_ID,)).fetchone()["c"] == 1

    def test_single_mode_404s(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "MULTI", False)
        monkeypatch.setattr(settings, "AUTH_TOKEN", None)
        monkeypatch.setattr(database, "DB_PATH", tmp_path / "single.db")
        database.init_db()
        with TestClient(app) as c:
            resp = c.post("/admin/users/1/delete", data={"confirm_email": "x"})
        assert resp.status_code == 404


@pytest.fixture
def pg_env(monkeypatch):
    """Module-local copy of the established PG full-app fixture pattern
    (tests/test_pg_dialect.py): real multi mode over PG, fresh Fernet key,
    fresh schema per test."""
    from cryptography.fernet import Fernet

    monkeypatch.setattr(settings, "MULTI", True)
    monkeypatch.setattr(settings, "DATABASE_URL", os.environ["FEEDECHO_TEST_PG_URL"])
    monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", False)
    monkeypatch.setattr(settings, "CREDENTIAL_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
    auth._login_attempts.clear()
    auth._register_attempts.clear()
    database.init_db()
    return settings


@pytest.mark.skipif(
    not os.environ.get("FEEDECHO_TEST_PG_URL"),
    reason="FEEDECHO_TEST_PG_URL not set; PG tests are CI-gated",
)
class TestAdminDeleteUserPG:
    """The admin delete sweep must run cleanly against Postgres."""

    def test_pg_admin_delete_sweeps_user(self, pg_env, monkeypatch):
        # A scheduler job started by the app lifespan could race the
        # monkeypatch teardown (test_pg_dialect's lesson); no TestClient
        # lifespan jobs touch users, but keep the test honest anyway.
        with database.get_db() as db:
            # High fixed ids + ON CONFLICT DO NOTHING: local re-runs against
            # a persistent Postgres cannot collide with leftovers (and this
            # file's sqlite fixtures run on a different engine entirely).
            db.execute(
                "INSERT INTO users (id, email, password_hash, is_admin)"
                " VALUES (9, 'pgboss@example.com', '', 1)"
                " ON CONFLICT (id) DO UPDATE SET email = excluded.email,"
                " is_admin = excluded.is_admin"
            )
            db.execute(
                "INSERT INTO users (id, email, password_hash, is_admin)"
                " VALUES (10, 'pgspam@example.com', '', 0)"
                " ON CONFLICT (id) DO UPDATE SET email = excluded.email,"
                " is_admin = excluded.is_admin"
            )
            db.execute(
                "INSERT INTO feeds (id, user_id, name, url)"
                " VALUES (900, 10, 'f', 'https://x.example/rss')"
                " ON CONFLICT (id) DO NOTHING"
            )
        with _client(9, "pgboss@example.com") as c:
            resp = c.post(
                "/admin/users/10/delete",
                data={"confirm_email": "pgspam@example.com"},
                follow_redirects=False,
            )
        assert resp.status_code == 302
        with database.get_db() as db:
            assert db.execute("SELECT COUNT(*) c FROM users WHERE id = 10").fetchone()["c"] == 0
            assert db.execute("SELECT COUNT(*) c FROM feeds WHERE id = 900").fetchone()["c"] == 0
