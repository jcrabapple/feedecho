"""Terms and Privacy pages: hosted-only legal pages.

Pins the route contract added with the legal pages:
- /terms and /privacy are public (anonymous-accessible) in multi mode and
  render nav chrome when signed in — they are decision pages for people
  considering signup, like /register and /about.
- They 404 in single mode (the OSS product has no hosted contract).
- The register page links them (the "signing up means agreeing" pointer).
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
    monkeypatch.setattr(database, "DB_PATH", Path(tmp_path / "legal.db"))
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


def _client(user_id=None, email="u@example.com"):
    c = TestClient(app)
    if user_id is not None:
        c.cookies.set("feedecho_session", security.sign_session(user_id, email))
    return c


class TestLegalPagesMulti:
    def test_terms_renders_anonymously(self, multi_env):
        c = _client(None)
        r = c.get("/terms")
        assert r.status_code == 200
        assert "Terms of Service" in r.text
        # anonymous chrome: login funnel present, no app links
        assert "Log in" in r.text

    def test_privacy_renders_anonymously(self, multi_env):
        c = _client(None)
        r = c.get("/privacy")
        assert r.status_code == 200
        assert "Privacy Policy" in r.text
        assert "scrypt" in r.text  # the data-practices table is real content

    def test_terms_render_when_signed_in(self, multi_env):
        c = _client(UID, "u@example.com")
        r = c.get("/terms")
        assert r.status_code == 200
        # signed-in chrome present (nav app links)
        assert "/logout" in r.text

    def test_privacy_renders_when_signed_in(self, multi_env):
        c = _client(UID, "u@example.com")
        r = c.get("/privacy")
        assert r.status_code == 200
        assert "Privacy Policy" in r.text

    def test_footer_links_terms_and_privacy_when_signed_in(self, multi_env):
        c = _client(UID, "u@example.com")
        r = c.get("/about")
        assert 'href="/terms"' in r.text
        assert 'href="/privacy"' in r.text

    def test_footer_links_visible_anonymously_on_register(self, multi_env):
        c = _client(None)
        r = c.get("/register")
        assert 'href="/terms"' in r.text
        assert 'href="/privacy"' in r.text

    def test_register_page_points_to_legal(self, multi_env):
        c = _client(None)
        r = c.get("/register")
        assert r.status_code == 200
        assert "Signing up means agreeing" in r.text
        assert 'href="/terms"' in r.text
        assert 'href="/privacy"' in r.text


class TestLegalPagesSingleMode:
    def test_single_mode_404s_legal_pages(self, single_env):
        """Self-hosted mode has no hosted contract: /terms and /privacy 404."""
        c = TestClient(app)
        c.headers["X-Auth-Token"] = "tok"
        assert c.get("/terms").status_code == 404
        assert c.get("/privacy").status_code == 404

    def test_invites_required_is_false_in_single_mode(self, single_env):
        import invites

        assert invites.invites_required() is False
