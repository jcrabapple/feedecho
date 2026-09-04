"""Pricing page: public hosted-service page (multi only)."""

import pytest
from fastapi.testclient import TestClient

import database
import settings
from app import app


class TestPricingPage:
    @pytest.fixture(autouse=True)
    def multi_env(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "MULTI", True)
        monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
        monkeypatch.setattr(settings, "AUTH_TOKEN", None)
        monkeypatch.setattr(settings, "DATABASE_URL", "")
        monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
        monkeypatch.setattr(database, "DB_PATH", tmp_path / "pricing.db")
        database.init_db()

    def test_public_without_session(self):
        with TestClient(app) as c:
            resp = c.get("/pricing")
        assert resp.status_code == 200
        assert "Pricing" in resp.text

    def test_states_the_current_prices_and_trial(self):
        with TestClient(app) as c:
            resp = c.get("/pricing")
        assert "$4" in resp.text
        assert "$40" in resp.text
        assert "14 days" in resp.text
        assert "30-day" in resp.text  # refund

    def test_states_the_trial_and_paid_limits(self):
        with TestClient(app) as c:
            resp = c.get("/pricing")
        # Trial caps (settings.DEFAULT_PLAN_LIMITS["trial"]).
        assert "5 feeds" in resp.text
        assert "5 connected accounts" in resp.text
        # Paid caps (settings.DEFAULT_PLAN_LIMITS["paid"]).
        assert "50 feeds" in resp.text
        assert "50 connected accounts" in resp.text

    def test_footer_link_appears_in_multi_mode(self):
        with TestClient(app) as c:
            resp = c.get("/login")
        assert 'href="/pricing"' in resp.text

    def test_404_in_single_mode(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "MULTI", False)
        monkeypatch.setattr(settings, "AUTH_TOKEN", None)
        monkeypatch.setattr(database, "DB_PATH", tmp_path / "single.db")
        database.init_db()
        with TestClient(app) as c:
            resp = c.get("/pricing")
        assert resp.status_code == 404

    def test_limits_match_the_code(self):
        """The copy must not drift from settings.DEFAULT_PLAN_LIMITS."""
        trial = settings.DEFAULT_PLAN_LIMITS["trial"]
        paid = settings.DEFAULT_PLAN_LIMITS["paid"]
        assert trial["max_feeds"] == 5
        assert trial["max_destinations"] == 5
        assert paid["max_feeds"] == 50
        assert paid["max_destinations"] == 50
