"""About page: public hosted-service disclosure (multi only)."""

import pytest
from fastapi.testclient import TestClient

import database
import settings
from app import app


class TestAboutPage:
    @pytest.fixture(autouse=True)
    def multi_env(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "MULTI", True)
        monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
        monkeypatch.setattr(settings, "AUTH_TOKEN", None)
        monkeypatch.setattr(settings, "DATABASE_URL", "")
        monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
        monkeypatch.setattr(database, "DB_PATH", tmp_path / "about.db")
        database.init_db()

    def test_public_without_session(self):
        with TestClient(app) as c:
            resp = c.get("/about")
        assert resp.status_code == 200
        for section in (
            "What we store",
            "Security practices",
            "Third parties",
            "Account deletion",
        ):
            assert section in resp.text

    def test_discloses_key_practices(self):
        with TestClient(app) as c:
            resp = c.get("/about")
        assert "scrypt" in resp.text
        assert "single-use" in resp.text
        assert "sandboxed" in resp.text
        assert "not sold" in resp.text

    def test_claims_match_reality(self):
        # No fabricated capabilities: billing does not exist yet.
        with TestClient(app) as c:
            resp = c.get("/about")
        assert "will be handled by a payment processor" in resp.text

    def test_footer_links_appear_in_multi_mode(self):
        with TestClient(app) as c:
            resp = c.get("/login")
        assert 'href="/about"' in resp.text

    def test_404_in_single_mode(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "MULTI", False)
        monkeypatch.setattr(settings, "AUTH_TOKEN", None)
        monkeypatch.setattr(database, "DB_PATH", tmp_path / "single.db")
        database.init_db()
        with TestClient(app) as c:
            resp = c.get("/about")
        assert resp.status_code == 404

    def test_no_footer_in_single_mode(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "MULTI", False)
        monkeypatch.setattr(settings, "AUTH_TOKEN", None)
        monkeypatch.setattr(database, "DB_PATH", tmp_path / "single2.db")
        database.init_db()
        with TestClient(app) as c:
            resp = c.get("/")
        assert "site-footer" not in resp.text
