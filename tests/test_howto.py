"""How To page: renders for both modes with the three core flows."""

import pytest
from fastapi.testclient import TestClient

import auth
import database
import security
import settings
from app import app

UID = 5


class TestHowToPage:
    @pytest.fixture(autouse=True)
    def env(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "MULTI", True)
        monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
        monkeypatch.setattr(settings, "AUTH_TOKEN", None)
        monkeypatch.setattr(settings, "DATABASE_URL", "")
        monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
        monkeypatch.setattr(database, "DB_PATH", tmp_path / "howto.db")
        database.init_db()
        auth._login_attempts.clear()
        auth._register_attempts.clear()
        with database.get_db() as db:
            db.execute(
                "INSERT INTO users (id, email, password_hash, email_verified)"
                " VALUES (?, 'u@example.com', '', 1)",
                (UID,),
            )

    def _client(self):
        c = TestClient(app)
        c.cookies.set("feedecho_session", security.sign_session(UID, "u@example.com"))
        return c

    def test_renders_for_authenticated_multi_user(self):
        with self._client() as c:
            resp = c.get("/howto")
        assert resp.status_code == 200
        for heading in ("Add a feed", "Add an account", "Create an echo"):
            assert heading in resp.text

    def test_nav_link_present(self):
        with self._client() as c:
            assert 'href="/howto"' in c.get("/").text

    def test_redirects_unauthenticated(self):
        with TestClient(app) as c:
            resp = c.get("/howto", headers={"accept": "text/html"}, follow_redirects=False)
        assert resp.status_code in (302, 303)

    def test_renders_in_single_mode(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "MULTI", False)
        monkeypatch.setattr(settings, "AUTH_TOKEN", None)
        monkeypatch.setattr(database, "DB_PATH", tmp_path / "single.db")
        database.init_db()
        with TestClient(app) as c:
            resp = c.get("/howto")
        assert resp.status_code == 200
        assert "Add a feed" in resp.text

    def test_no_multi_only_chrome_on_single_mode_page(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "MULTI", False)
        monkeypatch.setattr(settings, "AUTH_TOKEN", None)
        monkeypatch.setattr(database, "DB_PATH", tmp_path / "single2.db")
        database.init_db()
        with TestClient(app) as c:
            resp = c.get("/howto")
        assert "Admin" not in resp.text.split("How To")[0]  # nav has no admin link
