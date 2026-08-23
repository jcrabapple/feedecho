"""Email verification: token lifecycle, endpoint behavior, posting gate."""

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
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "verify.db")
    database.init_db()
    auth._login_attempts.clear()
    auth._register_attempts.clear()
    with database.get_db() as db:
        db.execute(
            "INSERT INTO users (id, email, password_hash)"
            " VALUES (?, 'verify@example.com', '')",
            (UID,),
        )
    return settings


def _client(uid=UID, email="verify@example.com"):
    c = TestClient(app)
    c.cookies.set("feedecho_session", security.sign_session(uid, email))
    return c


class TestTokenLifecycle:
    def test_issue_and_consume(self, multi_env):
        token = verification.issue_token(UID, "verify")
        assert verification.consume_token(token, "verify") == UID
        # Single use
        assert verification.consume_token(token, "verify") is None

    def test_wrong_purpose_rejected(self, multi_env):
        token = verification.issue_token(UID, "verify")
        assert verification.consume_token(token, "reset") is None

    def test_unknown_token_rejected(self, multi_env):
        assert verification.consume_token("bogus", "verify") is None

    def test_expired_token_rejected(self, multi_env):
        token = verification.issue_token(UID, "verify")
        with database.get_db() as db:
            db.execute(
                "UPDATE email_tokens SET expires_at = '2000-01-01 00:00:00'"
                " WHERE user_id = ?",
                (UID,),
            )
        assert verification.consume_token(token, "verify") is None

    def test_new_token_invalidates_old(self, multi_env):
        old = verification.issue_token(UID, "verify")
        new = verification.issue_token(UID, "verify")
        assert verification.consume_token(old, "verify") is None
        assert verification.consume_token(new, "verify") == UID

    def test_resend_throttle(self, multi_env):
        for _ in range(verification.RESEND_LIMIT):
            verification.issue_token(UID, "verify")
        assert verification.resend_allowed(UID, "verify") is False


class TestVerifyEndpoint:
    def test_valid_link_verifies_and_redirects(self, multi_env):
        token = verification.issue_token(UID, "verify")
        with TestClient(app) as c:
            resp = c.get(f"/verify-email?token={token}", follow_redirects=False)
        assert resp.status_code == 302
        with database.get_db() as db:
            row = db.execute(
                "SELECT email_verified FROM users WHERE id = ?", (UID,)
            ).fetchone()
        assert row["email_verified"] == 1

    def test_invalid_link_renders_error(self, multi_env):
        with TestClient(app) as c:
            resp = c.get("/verify-email?token=nope", follow_redirects=False)
        assert resp.status_code == 400

    def test_404_in_single_mode(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "MULTI", False)
        monkeypatch.setattr(settings, "AUTH_TOKEN", None)
        monkeypatch.setattr(database, "DB_PATH", tmp_path / "single.db")
        database.init_db()
        with TestClient(app) as c:
            assert c.get("/verify-email?token=x").status_code == 404
            assert c.post("/resend-verification").status_code == 404


class TestSignupIntegration:
    def test_register_issues_token_and_sends_email(self, multi_env, monkeypatch):
        sent = []
        monkeypatch.setattr(
            "email_sender.send_system_email",
            lambda to, subject, body: sent.append((to, subject, body)),
        )
        with TestClient(app) as c:
            resp = c.post(
                "/register",
                data={
                    "email": "new@example.com",
                    "password": "hunter2hunter2",
                    "confirm": "hunter2hunter2",
                },
                follow_redirects=False,
            )
        assert resp.status_code == 302
        assert len(sent) == 1
        to, subject, body = sent[0]
        assert to == "new@example.com"
        assert "/verify-email?token=" in body
        token = body.split("/verify-email?token=")[1].split("\n")[0]
        assert verification.consume_token(token, "verify") is not None

    def test_register_succeeds_even_when_send_fails(self, multi_env, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("smtp down")

        monkeypatch.setattr("email_sender.send_system_email", boom)
        with TestClient(app) as c:
            resp = c.post(
                "/register",
                data={
                    "email": "fail@example.com",
                    "password": "hunter2hunter2",
                    "confirm": "hunter2hunter2",
                },
                follow_redirects=False,
            )
        assert resp.status_code == 302


class TestResendEndpoint:
    def test_resend_issues_fresh_token(self, multi_env, monkeypatch):
        sent = []
        monkeypatch.setattr(
            "email_sender.send_system_email",
            lambda to, subject, body: sent.append(body),
        )
        old = verification.issue_token(UID, "verify")
        with _client() as c:
            resp = c.post("/resend-verification", follow_redirects=False)
        assert resp.status_code == 302
        assert sent and "/verify-email?token=" in sent[0]
        new_token = sent[0].split("/verify-email?token=")[1].split("\n")[0]
        assert verification.consume_token(old, "verify") is None
        assert verification.consume_token(new_token, "verify") == UID

    def test_resend_refused_when_throttled(self, multi_env):
        for _ in range(verification.RESEND_LIMIT):
            verification.issue_token(UID, "verify")
        with _client() as c:
            resp = c.post("/resend-verification", follow_redirects=False)
        assert resp.status_code == 400

    def test_resend_noop_when_verified(self, multi_env, monkeypatch):
        sent = []
        monkeypatch.setattr(
            "email_sender.send_system_email",
            lambda *a, **k: sent.append(1),
        )
        with database.get_db() as db:
            db.execute(
                "UPDATE users SET email_verified = 1 WHERE id = ?", (UID,)
            )
        with _client() as c:
            resp = c.post("/resend-verification", follow_redirects=False)
        assert resp.status_code == 302
        assert sent == []


class TestBannerAndGating:
    def test_unverified_user_sees_banner(self, multi_env):
        with _client() as c:
            page = c.get("/").text
        assert "Verify your email" in page
        assert 'action="/resend-verification"' in page

    def test_verified_user_sees_no_banner(self, multi_env):
        with database.get_db() as db:
            db.execute(
                "UPDATE users SET email_verified = 1 WHERE id = ?", (UID,)
            )
        with _client() as c:
            page = c.get("/").text
        assert "Verify your email" not in page

    def test_verified_flash_after_link(self, multi_env):
        with _client() as c:
            page = c.get("/?verified=1").text
        assert "Email verified" in page
