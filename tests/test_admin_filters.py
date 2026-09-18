"""Admin user-list filters: plan (trial/beta/paid) + verified/unverified.

GET /admin accepts `plan` and `verified` query params; the per-row action
forms carry them as hidden inputs so a POST redirect lands back on the
filtered view.
"""

import os

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


class TestFilterRedirectsAllActions:
    """Every per-row action handler must re-apply the posted filters."""

    CASES = [
        ("/admin/users/101/unsuspend", {}),
        ("/admin/users/101/promote", {}),
        # demote omitted: with only one admin in the fixture the pre-existing
        # last-admin guard 400s before the redirect; its redirect line is
        # the identical _admin_filter_qs_from_form one-liner.
        ("/admin/users/101/plan", {"plan": "paid"}),
        ("/admin/users/101/extend-trial", {"days": "14"}),
        ("/admin/users/101/delete", {"confirm_text": "DELETE"}),
    ]

    @pytest.mark.parametrize("url,data", CASES)
    def test_action_redirect_preserves_filters(self, multi_env, url, data):
        payload = {**data, "filter_plan": "trial", "filter_verified": "yes"}
        with _client(ADMIN_ID, "admin@example.com") as c:
            resp = c.post(url, data=payload, follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/admin?plan=trial&verified=yes"


class TestFilterNormalization:
    def test_multipart_file_field_does_not_500(self, multi_env):
        # A stray file upload under a filter field must fall back to
        # "no filter", not raise AttributeError inside the redirect helper.
        with _client(ADMIN_ID, "admin@example.com") as c:
            resp = c.post(
                "/admin/users/102/suspend",
                data={},
                files={"filter_plan": ("f.txt", b"trial")},
                follow_redirects=False,
            )
        assert resp.status_code == 302
        assert resp.headers["location"] == "/admin"

    def test_plan_keys_are_case_sensitive(self, multi_env):
        resp = _get_admin("?plan=TRIAL")
        assert resp.status_code == 200
        for _, email, _, _ in USERS:
            assert email in resp.text

    def test_custom_plan_key_filters(self, multi_env, monkeypatch):
        monkeypatch.setattr(
            settings, "PLAN_LIMITS",
            {**settings.PLAN_LIMITS, "Enterprise": dict(settings.PLAN_LIMITS["paid"])},
        )
        with database.get_db() as db:
            db.execute(
                "INSERT INTO users (id, email, password_hash, plan,"
                " email_verified) VALUES (106, 'enterprise@example.com', '',"
                " 'Enterprise', 1)"
            )
        resp = _get_admin("?plan=Enterprise")
        assert "enterprise@example.com" in resp.text
        assert "trial-verified@example.com" not in resp.text


@pytest.fixture
def pg_env(monkeypatch):
    """Module-local copy of the established PG full-app fixture pattern
    (tests/test_admin_delete_user.py): real multi mode over PG, fresh
    Fernet key, idempotent init_db."""
    from cryptography.fernet import Fernet

    monkeypatch.setattr(settings, "MULTI", True)
    monkeypatch.setattr(settings, "DATABASE_URL", os.environ["FEEDECHO_TEST_PG_URL"])
    monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", False)
    monkeypatch.setattr(settings, "CREDENTIAL_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
    monkeypatch.setattr(settings, "STATE_SECRET", "s" * 40)
    auth._login_attempts.clear()
    auth._register_attempts.clear()
    database.init_db()
    return settings


@pytest.mark.pg
@pytest.mark.skipif(
    not os.environ.get("FEEDECHO_TEST_PG_URL"),
    reason="FEEDECHO_TEST_PG_URL not set; PG tests are CI-gated",
)
class TestAdminFiltersPG:
    """The filtered admin query must behave identically on Postgres."""

    def test_pg_filter_plan_and_verified(self, pg_env):
        with database.get_db() as db:
            db.execute(
                "INSERT INTO users (id, email, password_hash, is_admin)"
                " VALUES (300, 'pgadmin@example.com', '', 1)"
                " ON CONFLICT (id) DO UPDATE SET email = excluded.email"
            )
            db.execute(
                "INSERT INTO users (id, email, password_hash, plan,"
                " email_verified) VALUES (301, 'pg-paid@example.com', '',"
                " 'paid', 1) ON CONFLICT (id) DO UPDATE SET"
                " email = excluded.email, plan = excluded.plan,"
                " email_verified = excluded.email_verified"
            )
            db.execute(
                "INSERT INTO users (id, email, password_hash, plan,"
                " email_verified) VALUES (302, 'pg-trial@example.com', '',"
                " 'trial', 0) ON CONFLICT (id) DO UPDATE SET"
                " email = excluded.email, plan = excluded.plan,"
                " email_verified = excluded.email_verified"
            )
        with _client(300, "pgadmin@example.com") as c:
            resp = c.get("/admin?plan=paid&verified=yes")
        assert resp.status_code == 200
        assert "pg-paid@example.com" in resp.text
        assert "pg-trial@example.com" not in resp.text
        # the unfiltered page shows everyone
        resp = c.get("/admin")
        assert "pg-paid@example.com" in resp.text
        assert "pg-trial@example.com" in resp.text
