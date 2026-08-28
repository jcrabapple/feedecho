"""Hosted landing page at / (anonymous marketing surface).

Pins the product-split behavior:
- Multi mode, anonymous: / serves the landing (hero, tagline, CTA buttons).
- Multi mode, signed in: / serves the dashboard (redirect from login/register
  still lands there).
- Single mode: / is the app dashboard, exactly as before — no marketing page.
- The landing links Log in + Sign up (top CTA) and the legal pages.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import auth
import database
import security
import settings
from app import app

UID = 5


@pytest.fixture()
def multi_env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MULTI", True)
    monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
    monkeypatch.setattr(database, "DB_PATH", Path(tmp_path / "landing.db"))
    database.init_db()
    auth._login_attempts.clear()
    auth._register_attempts.clear()
    with database.get_db() as db:
        db.execute(
            "INSERT INTO users (id, email, password_hash, email_verified)"
            " VALUES (?, 'u@example.com', '', 1)",
            (UID,),
        )
    return database


@pytest.fixture()
def single_env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MULTI", False)
    monkeypatch.setattr(settings, "AUTH_TOKEN", "tok")
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
    monkeypatch.setattr(database, "DB_PATH", Path(tmp_path / "single.db"))
    database.init_db()
    return database


class TestLandingMultiMode:
    def test_anonymous_root_is_landing(self, multi_env):
        c = TestClient(app)
        r = c.get("/")
        assert r.status_code == 200
        assert "echoed everywhere" in r.text
        assert "Start your free trial" in r.text
        assert "How it works" in r.text

    def test_landing_has_login_and_signup_ctas(self, multi_env):
        c = TestClient(app)
        r = c.get("/")
        assert 'href="/register"' in r.text
        assert 'href="/login"' in r.text

    def test_landing_links_legal_pages(self, multi_env):
        c = TestClient(app)
        r = c.get("/")
        assert 'href="/terms"' in r.text
        assert 'href="/privacy"' in r.text

    def test_landing_hero_image_present(self, multi_env):
        c = TestClient(app)
        r = c.get("/")
        assert 'src="/static/img/landing-hero.png"' in r.text
        assert c.get("/static/img/landing-hero.png").status_code == 200

    def test_signed_in_root_is_dashboard(self, multi_env):
        c = TestClient(app)
        c.cookies.set("feedecho_session", security.sign_session(UID, "u@example.com"))
        r = c.get("/")
        assert r.status_code == 200
        # Dashboard, not landing: the stats grid heading
        assert "landing-hero" not in r.text

    def test_login_and_register_still_redirect_authed_to_dashboard(self, multi_env):
        c = TestClient(app)
        c.cookies.set("feedecho_session", security.sign_session(UID, "u@example.com"))
        for path in ("/login", "/register"):
            r = c.get(path, follow_redirects=False)
            assert r.status_code == 302, path
            assert r.headers["location"] == "/"

    def test_nav_brand_points_at_landing_for_anonymous(self, multi_env):
        c = TestClient(app)
        r = c.get("/")
        assert 'href="/" class="nav-brand"' in r.text


class TestLandingSingleMode:
    def test_single_mode_root_is_dashboard_not_landing(self, single_env):
        """Self-hosted mode has no marketing page: / is the app dashboard."""
        c = TestClient(app)
        c.headers["X-Auth-Token"] = "tok"
        r = c.get("/")
        assert r.status_code == 200
        assert "landing-hero" not in r.text
        assert "echoed everywhere" not in r.text

    def test_single_mode_has_no_register_cta_leak(self, single_env):
        c = TestClient(app)
        c.headers["X-Auth-Token"] = "tok"
        r = c.get("/")
        # The landing's trial CTA must not leak into the self-hosted app page.
        assert "Start your free trial" not in r.text
