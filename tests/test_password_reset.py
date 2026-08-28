"""Password reset: request flow, token peek/consume, password change."""

import time

import pytest
from fastapi.testclient import TestClient

import auth
import database
import security
import settings
import verification
from app import app

UID = 5


@pytest.fixture
def multi_env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MULTI", True)
    monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
    monkeypatch.setattr(settings, "BASE_URL", "http://testserver")
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "reset.db")
    database.init_db()
    auth._login_attempts.clear()
    auth._register_attempts.clear()
    with database.get_db() as db:
        db.execute(
            "INSERT INTO users (id, email, password_hash, email_verified)"
            " VALUES (?, 'reset@example.com', ?, 1)",
            (UID, security.hash_password("oldpassword")),
        )
    return settings


class TestForgotFlow:
    def test_request_sends_reset_link(self, multi_env, monkeypatch):
        sent = []
        monkeypatch.setattr(
            "email_sender.send_system_email",
            lambda to, subject, body: sent.append((to, subject, body)),
        )
        with TestClient(app) as c:
            resp = c.post(
                "/forgot-password",
                data={"email": "reset@example.com"},
                follow_redirects=False,
            )
        assert resp.status_code == 200
        for _ in range(100):  # email dispatch is a background thread
            if sent:
                break
            time.sleep(0.05)
        assert sent and sent[0][0] == "reset@example.com"
        body = sent[0][2]
        assert "/reset-password?token=" in body
        token = body.split("/reset-password?token=")[1].split("\n")[0]
        assert verification.peek_token(token, "reset") == UID

    def test_unknown_email_same_response(self, multi_env, monkeypatch):
        sent = []
        monkeypatch.setattr(
            "email_sender.send_system_email",
            lambda *a, **k: sent.append(1),
        )
        with TestClient(app) as c:
            resp = c.post(
                "/forgot-password",
                data={"email": "ghost@example.com"},
                follow_redirects=False,
            )
        assert resp.status_code == 200
        assert "reset link has been sent" in resp.text
        assert sent == []

    def test_reset_page_renders_form_for_valid_token(self, multi_env):
        token = verification.issue_token(UID, "reset")
        with TestClient(app) as c:
            resp = c.get(f"/reset-password?token={token}")
        assert resp.status_code == 200
        assert 'name="token"' in resp.text
        # Peek must not consume
        assert verification.peek_token(token, "reset") == UID

    def test_reset_page_rejects_bad_token(self, multi_env):
        with TestClient(app) as c:
            resp = c.get("/reset-password?token=bogus")
        assert resp.status_code == 200
        assert "invalid or has expired" in resp.text

    def test_single_mode_404(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "MULTI", False)
        monkeypatch.setattr(settings, "AUTH_TOKEN", None)
        monkeypatch.setattr(database, "DB_PATH", tmp_path / "single.db")
        database.init_db()
        with TestClient(app) as c:
            assert c.get("/forgot-password").status_code == 404
            assert c.get("/reset-password?token=x").status_code == 404
            assert c.post("/forgot-password", data={"email": "a@b.c"}).status_code == 404


class TestResetSubmit:
    def test_full_reset_flow_changes_password(self, multi_env):
        token = verification.issue_token(UID, "reset")
        with TestClient(app) as c:
            resp = c.post(
                "/reset-password",
                data={
                    "token": token,
                    "password": "newpassword1",
                    "confirm": "newpassword1",
                },
                follow_redirects=False,
            )
        assert resp.status_code == 302
        with database.get_db() as db:
            row = db.execute(
                "SELECT password_hash FROM users WHERE id = ?", (UID,)
            ).fetchone()
        assert security.verify_password("newpassword1", row["password_hash"])
        assert not security.verify_password("oldpassword", row["password_hash"])
        # Token is gone
        assert verification.consume_token(token, "reset") is None

    def test_mismatched_passwords_reissue_token(self, multi_env):
        token = verification.issue_token(UID, "reset")
        with TestClient(app) as c:
            resp = c.post(
                "/reset-password",
                data={"token": token, "password": "newpassword1", "confirm": "different1"},
                follow_redirects=False,
            )
        assert resp.status_code == 200
        assert "Passwords do not match" in resp.text
        # The SAME token is re-rendered (peek, not consume+reissue)
        assert f'name="token" value="{token}"' in resp.text
        assert verification.peek_token(token, "reset") == UID
        # Password unchanged
        with database.get_db() as db:
            row = db.execute(
                "SELECT password_hash FROM users WHERE id = ?", (UID,)
            ).fetchone()
        assert security.verify_password("oldpassword", row["password_hash"])

    def test_consumed_token_cannot_be_reused(self, multi_env):
        token = verification.issue_token(UID, "reset")
        with TestClient(app) as c:
            c.post(
                "/reset-password",
                data={"token": token, "password": "newpassword1", "confirm": "newpassword1"},
                follow_redirects=False,
            )
            resp = c.post(
                "/reset-password",
                data={"token": token, "password": "newpassword2", "confirm": "newpassword2"},
                follow_redirects=False,
            )
        assert resp.status_code == 200
        assert "invalid or has expired" in resp.text

    def test_short_password_rejected(self, multi_env):
        token = verification.issue_token(UID, "reset")
        with TestClient(app) as c:
            resp = c.post(
                "/reset-password",
                data={"token": token, "password": "short", "confirm": "short"},
                follow_redirects=False,
            )
        assert resp.status_code == 200
        assert "at least" in resp.text

    def test_login_with_new_password_works(self, multi_env):
        token = verification.issue_token(UID, "reset")
        with TestClient(app) as c:
            c.post(
                "/reset-password",
                data={"token": token, "password": "newpassword1", "confirm": "newpassword1"},
                follow_redirects=False,
            )
            resp = c.post(
                "/login",
                data={"email": "reset@example.com", "password": "newpassword1"},
                follow_redirects=False,
            )
        assert resp.status_code == 302


class TestForgotThrottle:
    def test_throttled_user_gets_no_fresh_link(self, multi_env, monkeypatch):
        sent = []
        monkeypatch.setattr(
            "email_sender.send_system_email",
            lambda *a, **k: sent.append(1),
        )
        for _ in range(verification.RESEND_LIMIT):
            verification.issue_token(UID, "reset")
        before = len(sent)
        with TestClient(app) as c:
            c.post("/forgot-password", data={"email": "reset@example.com"})
        time.sleep(0.3)  # allow any background dispatch to (not) happen
        assert len(sent) == before  # throttled: no send


    def test_reset_invalidates_existing_sessions(self, multi_env):
        with database.get_db() as db:
            db.execute("UPDATE users SET email_verified = 1 WHERE id = ?", (UID,))
        old_cookie = security.sign_session(UID, "reset@example.com", epoch=0)
        with TestClient(app) as c:
            c.cookies.set("feedecho_session", old_cookie)
            assert c.get("/").status_code == 200  # session valid pre-reset
        token = verification.issue_token(UID, "reset")
        with TestClient(app) as c:
            c.post(
                "/reset-password",
                data={"token": token, "password": "newpassword1", "confirm": "newpassword1"},
                follow_redirects=False,
            )
        with TestClient(app) as c:
            c.cookies.set("feedecho_session", old_cookie)
            resp = c.get("/feeds", follow_redirects=False)
        assert resp.status_code in (302, 303, 401)  # epoch bumped: dead session
