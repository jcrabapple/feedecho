"""Tests for self-serve account deletion (D4)."""

import os

import pytest
from fastapi.testclient import TestClient

import auth
import database
import settings
from app import _account_deletion_hooks, app


@pytest.fixture
def multi_env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MULTI", True)
    monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "multi.db")
    database.init_db()
    auth._login_attempts.clear()
    auth._register_attempts.clear()
    return settings


@pytest.fixture
def client(multi_env):
    with TestClient(app) as c:
        yield c


def _register(client, email="new@example.com", password="hunter2hunter2"):
    return client.post(
        "/register",
        data={"email": email, "password": password, "confirm": password},
        follow_redirects=False,
    )


def _uid(email):
    with database.get_db() as db:
        row = db.execute(
            "SELECT id FROM users WHERE email = ?", (email,)
        ).fetchone()
    return row["id"] if row else None


def _seed(uid):
    """Insert one of each owned table so the test proves a full sweep."""
    with database.get_db() as db:
        db.execute(
            "INSERT INTO feeds (name, url, user_id) VALUES ('F', 'https://e.com/r', ?)",
            (uid,),
        )
        feed_id = db.execute(
            "SELECT id FROM feeds WHERE user_id = ?", (uid,)
        ).fetchone()["id"]
        db.execute(
            "INSERT INTO feed_items (feed_id, item_id, title) VALUES (?, 'i1', 't')",
            (feed_id,),
        )
        db.execute(
            "INSERT INTO echoes (feed_id, destination_type, destination_id, user_id)"
            " VALUES (?, 'mastodon', 1, ?)",
            (feed_id, uid),
        )
        echo_id = db.execute(
            "SELECT id FROM echoes WHERE user_id = ?", (uid,)
        ).fetchone()["id"]
        db.execute(
            "INSERT INTO digest_items (echo_id, item_id, rendered_content)"
            " VALUES (?, 'i1', 'x')",
            (echo_id,),
        )
        db.execute(
            "INSERT INTO drip_items (echo_id, item_id, item_json)"
            " VALUES (?, 'i1', '{}')",
            (echo_id,),
        )
        db.execute(
            "INSERT INTO posted_items (echo_id, item_id, status)"
            " VALUES (?, 'i1', 'success')",
            (echo_id,),
        )
        db.execute(
            "INSERT INTO accounts (name, instance, access_token, user_id)"
            " VALUES ('M', 'https://m.example', 'tok', ?)",
            (uid,),
        )
        db.execute(
            "INSERT INTO email_accounts (name, email, user_id) VALUES ('E', 'e@x.com', ?)",
            (uid,),
        )
        db.execute(
            "INSERT INTO settings (user_id, key, value) VALUES (?, 'retry_max_attempts', '9')",
            (uid,),
        )
        db.execute(
            "INSERT INTO oauth_states (nonce, instance, session_binding, expires_at, user_id)"
            " VALUES ('n1', 'https://m.example', 's', '2099-01-01 00:00:00', ?)",
            (uid,),
        )
        db.execute(
            "INSERT INTO email_tokens (user_id, token_hash, purpose, expires_at)"
            " VALUES (?, 'h', 'reset', '2099-01-01 00:00:00')",
            (uid,),
        )


def _user_scoped_counts(uid):
    with database.get_db() as db:
        out = {}
        for table in ("feeds", "echoes", "accounts", "email_accounts", "settings"):
            out[table] = db.execute(
                f"SELECT COUNT(*) AS c FROM {table} WHERE user_id = ?", (uid,)
            ).fetchone()["c"]
        out["users"] = db.execute(
            "SELECT COUNT(*) AS c FROM users WHERE id = ?", (uid,)
        ).fetchone()["c"]
        out["oauth_states"] = db.execute(
            "SELECT COUNT(*) AS c FROM oauth_states WHERE user_id = ?", (uid,)
        ).fetchone()["c"]
        out["email_tokens"] = db.execute(
            "SELECT COUNT(*) AS c FROM email_tokens WHERE user_id = ?", (uid,)
        ).fetchone()["c"]
        return out


@pytest.mark.multi
class TestAccountDeletion:
    def test_wrong_password_keeps_everything(self, client):
        _register(client)
        uid = _uid("new@example.com")
        _seed(uid)
        resp = client.post(
            "/settings/delete-account",
            data={"password": "wrong-password-1"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert "Incorrect password" in resp.text
        counts = _user_scoped_counts(uid)
        assert counts["users"] == 1
        assert counts["feeds"] == 1
        assert counts["echoes"] == 1

    def test_correct_password_deletes_everything(self, client):
        _register(client)
        uid = _uid("new@example.com")
        _seed(uid)
        resp = client.post(
            "/settings/delete-account",
            data={"password": "hunter2hunter2"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login?deleted=1"
        assert "feedecho_session" not in resp.cookies
        counts = _user_scoped_counts(uid)
        assert counts["users"] == 0
        assert counts["feeds"] == 0
        assert counts["echoes"] == 0
        assert counts["accounts"] == 0
        assert counts["email_accounts"] == 0
        assert counts["settings"] == 0
        assert counts["oauth_states"] == 0
        assert counts["email_tokens"] == 0
        # Child tables (feed_items, digest_items, drip_items, posted_items) were
        # seeded only for this user, so after the sweep they must be empty.
        with database.get_db() as db:
            for child in ("feed_items", "digest_items", "drip_items", "posted_items"):
                n = db.execute(f"SELECT COUNT(*) AS c FROM {child}").fetchone()["c"]
                assert n == 0, f"{child} still has rows after deletion"

    def test_registered_hook_runs_before_delete(self, client):
        called = []
        _account_deletion_hooks.append(lambda uid: called.append(uid))
        try:
            _register(client)
            uid = _uid("new@example.com")
            client.post(
                "/settings/delete-account",
                data={"password": "hunter2hunter2"},
                follow_redirects=False,
            )
            assert called == [uid]
        finally:
            _account_deletion_hooks.clear()

    def test_single_mode_404s(self, client, monkeypatch):
        monkeypatch.setattr(settings, "MULTI", False)
        resp = client.post(
            "/settings/delete-account", data={"password": "x"}, follow_redirects=False
        )
        assert resp.status_code == 404


@pytest.mark.pg
@pytest.mark.skipif(
    "not os.environ.get('FEEDECHO_TEST_PG_URL')",
    reason="FEEDECHO_TEST_PG_URL not set",
)
class TestAccountDeletionPG:
    """The sweep's `IN (SELECT ...)` subqueries must translate on Postgres."""

    def test_pg_hard_delete_sweeps_every_table(self, client):
        _register(client)
        uid = _uid("new@example.com")
        _seed(uid)
        resp = client.post(
            "/settings/delete-account",
            data={"password": "hunter2hunter2"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        counts = _user_scoped_counts(uid)
        assert counts["users"] == 0
        assert counts["feeds"] == 0
        assert counts["echoes"] == 0
        with database.get_db() as db:
            for child in ("feed_items", "digest_items", "drip_items", "posted_items"):
                n = db.execute(f"SELECT COUNT(*) AS c FROM {child}").fetchone()["c"]
                assert n == 0, f"{child} still has rows after deletion"
