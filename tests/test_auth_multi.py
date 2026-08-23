"""Tests for multi-mode authentication: register, login, logout, sessions."""

import pytest
from fastapi.testclient import TestClient

import auth
import database
import settings
from app import app


@pytest.fixture
def multi_env(monkeypatch, tmp_path):
    """Multi-mode environment on a fresh temp SQLite DB."""
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


def _user_row(email):
    with database.get_db() as db:
        return db.execute(
            "SELECT * FROM users WHERE email = ?", (email,)
        ).fetchone()


class TestRegister:
    def test_register_creates_user_and_signs_in(self, client):
        resp = _register(client)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/"
        assert "feedecho_session" in resp.cookies

        row = _user_row("new@example.com")
        assert row is not None
        assert row["plan"] == "trial"
        assert row["email_verified"] == 0
        assert row["trial_ends_at"] is not None
        assert row["password_hash"].startswith("scrypt$")

    def test_register_normalizes_email(self, client):
        _register(client, email="  MixedCase@Example.COM ")
        assert _user_row("mixedcase@example.com") is not None

    def test_register_duplicate_email_shows_error(self, client):
        _register(client)
        resp = _register(client)
        assert resp.status_code == 200
        assert "already" in resp.text

    def test_register_short_password_rejected(self, client):
        resp = _register(client, password="short")
        assert resp.status_code == 200
        assert "8 characters" in resp.text
        assert _user_row("new@example.com") is None

    def test_register_password_mismatch_rejected(self, client):
        resp = client.post(
            "/register",
            data={
                "email": "a@example.com",
                "password": "hunter2hunter2",
                "confirm": "different",
            },
        )
        assert resp.status_code == 200
        assert _user_row("a@example.com") is None

    def test_register_invalid_email_rejected(self, client):
        resp = _register(client, email="not-an-email")
        assert resp.status_code == 200
        assert _user_row("not-an-email") is None


class TestLogin:
    def test_login_with_valid_credentials(self, client):
        _register(client, password="correct-horse-9")
        client.post("/logout")
        resp = client.post(
            "/login",
            data={"email": "new@example.com", "password": "correct-horse-9"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "feedecho_session" in resp.cookies

    def test_login_wrong_password(self, client):
        _register(client, password="correct-horse-9")
        resp = client.post(
            "/login",
            data={"email": "new@example.com", "password": "wrong"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert "Invalid email or password" in resp.text
        assert "feedecho_session" not in resp.cookies

    def test_login_unknown_email_same_error(self, client):
        resp = client.post(
            "/login",
            data={"email": "nobody@example.com", "password": "whatever"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        # Same message as wrong-password: no user enumeration.
        assert "Invalid email or password" in resp.text

    def test_login_ratelimited_after_five_attempts(self, client):
        for _ in range(5):
            client.post(
                "/login",
                data={"email": "x@example.com", "password": "wrong"},
            )
        resp = client.post(
            "/login",
            data={"email": "x@example.com", "password": "wrong"},
        )
        assert resp.status_code == 200
        assert "Too many" in resp.text

    def test_successful_login_clears_failure_bucket(self, client):
        _register(client, password="correct-horse-9")
        client.post("/logout")
        for _ in range(4):
            client.post(
                "/login",
                data={"email": "new@example.com", "password": "wrong"},
            )
        # Correct login must not be blocked by the earlier failures
        resp = client.post(
            "/login",
            data={"email": "new@example.com", "password": "correct-horse-9"},
            follow_redirects=False,
        )
        assert resp.status_code == 302


class TestRegisterAbuseControls:
    def test_register_throttled_after_ten_attempts(self, client):
        for i in range(10):
            client.post(
                "/register",
                data={
                    "email": f"u{i}@example.com",
                    "password": "hunter2hunter2",
                    "confirm": "hunter2hunter2",
                },
            )
        resp = client.post(
            "/register",
            data={
                "email": "one-more@example.com",
                "password": "hunter2hunter2",
                "confirm": "hunter2hunter2",
            },
        )
        assert resp.status_code == 200
        assert "Too many signup attempts" in resp.text
        assert _user_row("one-more@example.com") is None

    def test_overlong_password_rejected(self, client):
        resp = _register(client, password="x" * 1025)
        assert resp.status_code == 200
        assert "at most" in resp.text


class TestCookieSecurity:
    def test_secure_flag_forced_behind_proxy(self, client, monkeypatch):
        monkeypatch.setattr(settings, "FORCE_SECURE_COOKIE", True)
        resp = _register(client)
        assert "Secure" in resp.headers.get("set-cookie", "")

    def test_secure_flag_absent_by_default_over_http(self, client):
        resp = _register(client)
        assert "Secure" not in resp.headers.get("set-cookie", "")


class TestClientIp:
    def test_xff_ignored_without_trusted_proxies(self, monkeypatch):
        from starlette.requests import Request as SR

        monkeypatch.setattr(settings, "TRUSTED_PROXIES", ())
        scope = {
            "type": "http",
            "client": ("1.2.3.4", 12345),
            "headers": [(b"x-forwarded-for", b"9.9.9.9")],
        }
        assert auth._client_ip(SR(scope)) == "1.2.3.4"

    def test_xff_rightmost_used_behind_trusted_proxy(self, monkeypatch):
        from starlette.requests import Request as SR

        monkeypatch.setattr(settings, "TRUSTED_PROXIES", ("10.0.0.0/8",))
        scope = {
            "type": "http",
            "client": ("10.0.0.5", 12345),
            "headers": [(b"x-forwarded-for", b"spoofed.example, 9.9.9.9")],
        }
        assert auth._client_ip(SR(scope)) == "9.9.9.9"

    def test_xff_ignored_from_untrusted_peer(self, monkeypatch):
        from starlette.requests import Request as SR

        monkeypatch.setattr(settings, "TRUSTED_PROXIES", ("10.0.0.0/8",))
        scope = {
            "type": "http",
            "client": ("203.0.113.9", 12345),
            "headers": [(b"x-forwarded-for", b"9.9.9.9")],
        }
        assert auth._client_ip(SR(scope)) == "203.0.113.9"


class TestSessionEnforcement:
    BROWSER = {"Accept": "text/html"}

    def test_protected_page_redirects_to_login_without_session(self, client):
        resp = client.get("/", follow_redirects=False, headers=self.BROWSER)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/login"

    def test_protected_page_served_with_session(self, client):
        _register(client)
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 200

    def test_api_request_without_session_returns_401(self, client):
        resp = client.post("/api/feeds", data={"name": "F", "url": "https://x"})
        assert resp.status_code == 401

    def test_tampered_cookie_redirects_to_login(self, client):
        client.cookies.set("feedecho_session", "garbage.garbage")
        resp = client.get("/", follow_redirects=False, headers=self.BROWSER)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/login"

    def test_logout_clears_session(self, client):
        _register(client)
        resp = client.post("/logout", follow_redirects=False)
        assert resp.status_code == 302
        # Cookie cleared
        assert resp.headers.get("set-cookie", "").startswith(
            "feedecho_session="
        )
        after = client.get("/", follow_redirects=False, headers=self.BROWSER)
        assert after.status_code == 302

    def test_healthz_exempt(self, client):
        resp = client.get("/healthz")
        assert resp.status_code == 200


class TestSingleModeUnaffected:
    def test_register_404s_in_single_mode(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "MULTI", False)
        monkeypatch.setattr(settings, "AUTH_TOKEN", None)
        monkeypatch.setattr(database, "DB_PATH", tmp_path / "single.db")
        database.init_db()
        auth._login_attempts.clear()
        with TestClient(app) as c:
            resp = c.post(
                "/register",
                data={
                    "email": "a@example.com",
                    "password": "hunter2hunter2",
                    "confirm": "hunter2hunter2",
                },
            )
        assert resp.status_code == 404

    def test_single_mode_login_page_unchanged(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "MULTI", False)
        monkeypatch.setattr(settings, "AUTH_TOKEN", "sekret")
        monkeypatch.setattr(database, "DB_PATH", tmp_path / "single.db")
        database.init_db()
        with TestClient(app) as c:
            resp = c.get("/login")
        assert "Access token" in resp.text
