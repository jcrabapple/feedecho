"""Admin email settings: system SMTP save/test, isolation from per-user SMTP."""

import pytest
from fastapi.testclient import TestClient

import auth
import database
import email_sender
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
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "email.db")
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


def _save_smtp(c, **overrides):
    data = {
        "smtp_host": "smtp.example.com",
        "smtp_port": "587",
        "smtp_username": "mailer",
        "smtp_password": "secret123",
        "smtp_from_email": "no-reply@example.com",
        "smtp_from_name": "FeedEcho",
        "smtp_use_tls": "1",
    }
    data.update(overrides)
    return c.post("/admin/email", data=data, follow_redirects=False)


class TestAdminEmailSettings:
    def test_admin_can_save_system_smtp(self, multi_env):
        with _client(ADMIN_ID, "admin@example.com") as c:
            resp = _save_smtp(c)
            assert resp.status_code == 302
        cfg = email_sender.get_system_smtp_settings()
        assert cfg["host"] == "smtp.example.com"
        assert cfg["port"] == 587
        assert cfg["username"] == "mailer"
        assert cfg["password"] == "secret123"
        assert cfg["from_email"] == "no-reply@example.com"
        assert cfg["use_tls"] is True

    def test_blank_password_keeps_stored_value(self, multi_env):
        with _client(ADMIN_ID, "admin@example.com") as c:
            _save_smtp(c)
            # Save again with the password field blank: must keep secret123
            _save_smtp(c, smtp_password="")
        cfg = email_sender.get_system_smtp_settings()
        assert cfg["password"] == "secret123"

    def test_stored_password_never_rendered(self, multi_env):
        with _client(ADMIN_ID, "admin@example.com") as c:
            _save_smtp(c)
            page = c.get("/admin").text
        assert "secret123" not in page

    def test_non_admin_cannot_save(self, multi_env):
        with _client(USER_ID, "user@example.com") as c:
            resp = _save_smtp(c)
        assert resp.status_code == 403
        assert email_sender.get_system_smtp_settings() is None

    def test_single_mode_404(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "MULTI", False)
        monkeypatch.setattr(settings, "AUTH_TOKEN", None)
        monkeypatch.setattr(database, "DB_PATH", tmp_path / "single.db")
        database.init_db()
        with TestClient(app) as c:
            resp = c.post("/admin/email", data={"smtp_host": "h"})
        assert resp.status_code == 404

    def test_test_email_uses_system_smtp(self, multi_env, monkeypatch):
        sent = []
        monkeypatch.setattr(
            email_sender, "test_system_smtp_connection",
            lambda to_email="": (sent.append(to_email) or (True, "ok")),
        )
        with _client(ADMIN_ID, "admin@example.com") as c:
            resp = c.post("/admin/email/test", follow_redirects=False)
        assert resp.status_code == 200
        assert sent == ["admin@example.com"]
        assert "ok" in resp.text

    def test_test_email_when_unconfigured_reports_failure(self, multi_env):
        with _client(ADMIN_ID, "admin@example.com") as c:
            resp = c.post("/admin/email/test", follow_redirects=False)
        assert resp.status_code == 200
        assert "not configured" in resp.text.lower()

    def test_junk_port_rejected_at_save(self, multi_env):
        with _client(ADMIN_ID, "admin@example.com") as c:
            resp = _save_smtp(c, smtp_port="abc")
        assert resp.status_code == 400
        assert email_sender.get_system_smtp_settings() is None
        with _client(ADMIN_ID, "admin@example.com") as c:
            resp = _save_smtp(c, smtp_port="99999")
        assert resp.status_code == 400

    def test_control_characters_rejected(self, multi_env):
        with _client(ADMIN_ID, "admin@example.com") as c:
            resp = _save_smtp(c, smtp_host="evil\r\nBcc: x")
        assert resp.status_code == 400
        assert email_sender.get_system_smtp_settings() is None

    def test_invalid_from_email_rejected(self, multi_env):
        with _client(ADMIN_ID, "admin@example.com") as c:
            resp = _save_smtp(c, smtp_from_email="not-an-email")
        assert resp.status_code == 400

    def test_password_whitespace_preserved(self, multi_env):
        with _client(ADMIN_ID, "admin@example.com") as c:
            _save_smtp(c, smtp_password="  padded pw  ")
        assert email_sender.get_system_smtp_settings()["password"] == "  padded pw  "


class TestSystemVsPerUserSmtp:
    def test_per_user_smtp_unaffected_by_system_settings(self, multi_env):
        with database.get_db() as db:
            db.execute(
                "INSERT INTO settings (user_id, key, value)"
                " VALUES (?, 'smtp_host', 'userhost.example.com')",
                (USER_ID,),
            )
            db.execute(
                "INSERT INTO settings (user_id, key, value)"
                " VALUES (?, 'smtp_port', '465')",
                (USER_ID,),
            )
        with _client(ADMIN_ID, "admin@example.com") as c:
            _save_smtp(c)
        assert email_sender.get_smtp_settings(user_id=USER_ID)["host"] == "userhost.example.com"
        assert email_sender.get_system_smtp_settings()["host"] == "smtp.example.com"

    def test_send_system_email_uses_system_settings(self, multi_env, monkeypatch):
        with _client(ADMIN_ID, "admin@example.com") as c:
            _save_smtp(c)
        calls = []
        monkeypatch.setattr(email_sender, "_send_via", lambda cfg, to, subj, body: calls.append((cfg, to, subj)))
        email_sender.send_system_email("target@example.com", "Subject", "Body")
        assert calls and calls[0][0]["host"] == "smtp.example.com"
        assert calls[0][1] == "target@example.com"

    def test_send_system_email_raises_when_unconfigured(self, multi_env):
        with pytest.raises(ValueError):
            email_sender.send_system_email("a@example.com", "S", "B")
