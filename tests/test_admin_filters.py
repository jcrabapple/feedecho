"""Admin user-list filters: plan (trial/beta/paid) + verified/unverified.

GET /admin accepts `plan` and `verified` query params; the per-row action
forms carry them as hidden inputs so a POST redirect lands back on the
filtered view.
"""

import pytest
from fastapi.testclient import TestClient

import auth
import database
import security
import settings
from app import app

ADMIN_ID = 50
USERS = [
    (101, "trial-verified@example.com", "trial", 1),
    (102, "trial-unverified@example.com", "trial", 0),
    (103, "paid-verified@example.com", "paid", 1),
    (104, "paid-unverified@example.com", "paid", 0),
    (105, "beta-verified@example.com", "beta", 1),
]


@pytest.fixture
def multi_env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MULTI", True)
    monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
    monkeypatch.setattr(settings, "STATE_SECRET", "s" * 40)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "admin-filters.db")
    database.init_db()
    auth._login_attempts.clear()
    auth._register_attempts.clear()
    with database.get_db() as db:
        db.execute(
            "INSERT INTO users (id, email, password_hash, is_admin)"
            " VALUES (?, 'admin@example.com', '', 1)",
            (ADMIN_ID,),
        )
        for uid, email, plan, verified in USERS:
            db.execute(
                "INSERT INTO users (id, email, password_hash, plan,"
                " email_verified) VALUES (?, ?, '', ?, ?)",
                (uid, email, plan, verified),
            )
    return settings


def _client(uid, email):
    c = TestClient(app)
    c.cookies.set("feedecho_session", security.sign_session(uid, email))
    return c


def _get_admin(query=""):
    with _client(ADMIN_ID, "admin@example.com") as c:
        return c.get("/admin" + query)


class TestPlanFilter:
    def test_default_shows_all_plans(self, multi_env):
        resp = _get_admin()
        assert resp.status_code == 200
        for _, email, _, _ in USERS:
            assert email in resp.text
        assert "Filter active" not in resp.text

    def test_filter_plan_trial(self, multi_env):
        resp = _get_admin("?plan=trial")
        assert "trial-verified@example.com" in resp.text
        assert "trial-unverified@example.com" in resp.text
        assert "paid-verified@example.com" not in resp.text
        assert "beta-verified@example.com" not in resp.text

    def test_filter_plan_paid(self, multi_env):
        resp = _get_admin("?plan=paid")
        assert "paid-verified@example.com" in resp.text
        assert "paid-unverified@example.com" in resp.text
        assert "trial-verified@example.com" not in resp.text

    def test_filter_plan_beta(self, multi_env):
        resp = _get_admin("?plan=beta")
        assert "beta-verified@example.com" in resp.text
        assert "trial-verified@example.com" not in resp.text

    def test_invalid_plan_value_is_ignored(self, multi_env):
        resp = _get_admin("?plan=superuser")
        assert resp.status_code == 200
        for _, email, _, _ in USERS:
            assert email in resp.text

    def test_filter_form_marks_selected_plan(self, multi_env):
        resp = _get_admin("?plan=trial")
        assert '<option value="trial" selected>' in resp.text


class TestVerifiedFilter:
    def test_filter_verified_yes(self, multi_env):
        resp = _get_admin("?verified=yes")
        assert "trial-verified@example.com" in resp.text
        assert "paid-verified@example.com" in resp.text
        assert "trial-unverified@example.com" not in resp.text
        assert "paid-unverified@example.com" not in resp.text

    def test_filter_verified_no(self, multi_env):
        resp = _get_admin("?verified=no")
        assert "trial-unverified@example.com" in resp.text
        assert "paid-unverified@example.com" in resp.text
        assert "trial-verified@example.com" not in resp.text
        assert "beta-verified@example.com" not in resp.text

    def test_invalid_verified_value_is_ignored(self, multi_env):
        resp = _get_admin("?verified=maybe")
        assert resp.status_code == 200
        for _, email, _, _ in USERS:
            assert email in resp.text

    def test_combined_plan_and_verified(self, multi_env):
        resp = _get_admin("?plan=paid&verified=no")
        assert "paid-unverified@example.com" in resp.text
        for uid, email, _, _ in USERS:
            if email != "paid-unverified@example.com":
                assert email not in resp.text

    def test_filter_hint_shows_count(self, multi_env):
        resp = _get_admin("?plan=paid&verified=no")
        assert "Filter active" in resp.text
        assert "showing 1 of" in resp.text


class TestFilterRedirects:
    def test_suspend_redirect_preserves_filters(self, multi_env):
        with _client(ADMIN_ID, "admin@example.com") as c:
            resp = c.post(
                "/admin/users/101/suspend",
                data={"filter_plan": "trial", "filter_verified": "no"},
                follow_redirects=False,
            )
        assert resp.status_code == 302
        assert resp.headers["location"] == "/admin?plan=trial&verified=no"

    def test_suspend_redirect_without_filters_stays_plain(self, multi_env):
        with _client(ADMIN_ID, "admin@example.com") as c:
            resp = c.post(
                "/admin/users/102/suspend",
                data={},
                follow_redirects=False,
            )
        assert resp.headers["location"] == "/admin"

    def test_invalid_hidden_filter_values_are_dropped(self, multi_env):
        with _client(ADMIN_ID, "admin@example.com") as c:
            resp = c.post(
                "/admin/users/101/suspend",
                data={"filter_plan": "paid'; DROP TABLE users;--",
                      "filter_verified": "../etc/passwd"},
                follow_redirects=False,
            )
        assert resp.headers["location"] == "/admin"

    def test_filtered_page_after_action_still_filtered(self, multi_env):
        with _client(ADMIN_ID, "admin@example.com") as c:
            c.post("/admin/users/101/suspend",
                   data={"filter_plan": "trial", "filter_verified": ""})
            page = c.get("/admin?plan=trial")
        assert "trial-unverified@example.com" in page.text
        # the suspended row still matches the filter (plan/verified unchanged)
        assert "trial-verified@example.com" in page.text
        assert "paid-verified@example.com" not in page.text


class TestFilterGating:
    def test_non_admin_with_filter_params_is_403(self, multi_env):
        with _client(104, "paid-unverified@example.com") as c:
            resp = c.get("/admin?plan=trial&verified=yes")
        assert resp.status_code == 403
