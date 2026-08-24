"""UI gating: multi-mode chrome (email + logout, trial banner) appears only
for authenticated hosted users; single mode stays unchanged."""

import re

import pytest
from fastapi.testclient import TestClient

import auth
import database
import security
import settings
from app import app

A_ID = 2


@pytest.fixture
def multi_env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MULTI", True)
    monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "ui.db")
    database.init_db()
    auth._login_attempts.clear()
    auth._register_attempts.clear()
    with database.get_db() as db:
        db.execute(
            "INSERT INTO users (id, email, password_hash, plan, trial_ends_at)"
            " VALUES (?, ?, '', 'trial', datetime('now', '+10 days'))",
            (A_ID, "trial@example.com"),
        )
    return settings


@pytest.fixture
def client(multi_env):
    with TestClient(app) as c:
        c.cookies.set(
            "feedecho_session", security.sign_session(A_ID, "trial@example.com")
        )
        yield c


@pytest.mark.multi
class TestMultiChrome:
    def test_dashboard_shows_email_and_logout(self, client):
        page = client.get("/").text
        assert "trial@example.com" in page
        assert 'action="/logout"' in page

    def test_trial_banner_on_dashboard(self, client):
        page = client.get("/").text
        assert "trial-banner" in page
        assert "free trial ends" in page
        # Issue #6: the date must be an ISO date inside a local-time element
        # so the browser renders it in the viewer's locale, and it must match
        # the stored trial_ends_at (no timezone/day drift).
        m = re.search(
            r'free trial ends <time class="local-time" datetime="(\d{4}-\d{2}-\d{2})">',
            page,
        )
        assert m, "trial end date should be an ISO date in a local-time element"
        with database.get_db() as db:
            row = db.execute(
                "SELECT trial_ends_at FROM users WHERE id = ?", (A_ID,)
            ).fetchone()
        assert m.group(1) == str(row["trial_ends_at"])[:10]

    def test_no_trial_banner_for_paid_plan(self, multi_env, client):
        with database.get_db() as db:
            db.execute("UPDATE users SET plan = 'paid' WHERE id = ?", (A_ID,))
        page = client.get("/").text
        assert "trial-banner" not in page

    def test_other_pages_have_chrome_no_banner(self, client):
        page = client.get("/feeds").text
        assert "trial@example.com" in page
        assert "trial-banner" not in page

    def test_login_page_has_no_account_chrome(self, multi_env):
        with TestClient(app) as c:
            page = c.get("/login").text
        assert "trial@example.com" not in page
        assert 'action="/logout"' not in page


class TestSingleModeUnchanged:
    @pytest.fixture(autouse=True)
    def single_env(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "MULTI", False)
        monkeypatch.setattr(settings, "AUTH_TOKEN", None)
        monkeypatch.setattr(database, "DB_PATH", tmp_path / "single.db")
        database.init_db()

    def test_dashboard_has_no_multi_chrome(self):
        with TestClient(app) as c:
            page = c.get("/").text
        assert "trial-banner" not in page
        assert 'action="/logout"' not in page
        assert "nav-account" not in page
