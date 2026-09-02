"""Trial limits + usage visibility on the dashboard banner.

Trial users could only discover their plan limits by hitting them (the caps
appeared solely in error text), and the silent clamps (poll interval, drip
rate) were undiscoverable entirely. The dashboard trial banner now states
the plan limits and where the user stands against the countable ones
(feeds, connected accounts).

Signing note: the session cookie must be signed AFTER the fixture patches
settings.SESSION_SECRET — a token minted under the import-time secret fails
verification.
"""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import database
import settings
from app import app
import security

USER_ID = 111


def _setup(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MULTI", True)
    monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "trial-limits.db")
    database.init_db()
    with database.get_db() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, email, password_hash, plan, is_admin, suspended)"
            " VALUES (?, 'trial@example.com', '', 'trial', 0, 0)",
            (USER_ID,),
        )


def _dashboard(monkeypatch, tmp_path) -> str:
    """Set up a trial user and render their dashboard."""
    _setup(monkeypatch, tmp_path)
    token = security.sign_session(USER_ID, "trial@example.com", 0)
    with TestClient(app) as c:
        c.cookies.set("feedecho_session", token)
        return c.get("/").text


def _usage_text(page: str) -> str | None:
    m = re.search(r'class="trial-usage"\s*>(.*?)</p>', page, re.S)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else None


class TestTrialLimitsOnDashboard:
    def test_banner_states_limits_and_usage(self, monkeypatch, tmp_path):
        page = _dashboard(monkeypatch, tmp_path)
        text = _usage_text(page)
        assert text, "trial-usage line missing from the banner"
        assert "0 of 5 feeds" in text
        assert "0 of 5 connected accounts" in text
        assert "15 min" in text, "poll floor must be stated (it clamps silently)"
        assert "60 posts/hour" in text

    def test_usage_counts_reflect_rows(self, monkeypatch, tmp_path):
        _setup(monkeypatch, tmp_path)
        with database.get_db() as db:
            db.execute(
                "INSERT INTO feeds (name, url, user_id) VALUES ('f1', 'https://e.com/1', ?)",
                (USER_ID,),
            )
            db.execute(
                "INSERT INTO feeds (name, url, user_id, deleted_at)"
                " VALUES ('f2', 'https://e.com/2', ?, '2026-01-01 00:00:00')",
                (USER_ID,),
            )
            db.execute(
                "INSERT INTO bluesky_accounts (handle, name, app_password, user_id)"
                " VALUES ('me.bsky.social', 'me', 'x', ?)",
                (USER_ID,),
            )
        page = _dashboard(monkeypatch, tmp_path)
        text = _usage_text(page)
        assert text and "1 of 5 feeds" in text, "soft-deleted feeds must not count"
        assert "1 of 5 connected accounts" in text

    def test_paid_user_sees_no_usage_line(self, monkeypatch, tmp_path):
        _setup(monkeypatch, tmp_path)
        with database.get_db() as db:
            db.execute("UPDATE users SET plan = 'paid' WHERE id = ?", (USER_ID,))
        page = _dashboard(monkeypatch, tmp_path)
        assert "trial-usage" not in page
        assert "Plan limits:" not in page

    def test_non_trial_banner_unchanged(self, monkeypatch, tmp_path):
        _setup(monkeypatch, tmp_path)
        with database.get_db() as db:
            db.execute("UPDATE users SET plan = 'paid' WHERE id = ?", (USER_ID,))
        page = _dashboard(monkeypatch, tmp_path)
        assert "trial-banner" not in page

    def test_expired_trial_still_shows_limits(self, monkeypatch, tmp_path):
        _setup(monkeypatch, tmp_path)
        with database.get_db() as db:
            db.execute(
                "UPDATE users SET trial_ends_at = '2020-01-01 00:00:00' WHERE id = ?",
                (USER_ID,),
            )
        page = _dashboard(monkeypatch, tmp_path)
        assert "Trial ended" in page
        text = _usage_text(page)
        assert text and "0 of 5 feeds" in text, (
            "an expired-trial user deciding whether to pay still needs the limits visible"
        )

    def test_banner_paragraphs_styled(self):
        style = (Path(__file__).resolve().parent.parent / "static" / "css" / "style.css").read_text(
            encoding="utf-8"
        )
        assert ".trial-banner p { margin:" in style
        assert ".trial-usage" in style

    def test_cache_buster_bumped(self):
        base = (Path(__file__).resolve().parent.parent / "templates" / "base.html").read_text(
            encoding="utf-8"
        )
        assert 'style.css?v=47' in base


class TestBillingCtaSeam:
    """The billing UI is gated on settings.BILLING_ENABLED (set by the hosted
    deployment). Disabled = the "launch soon" copy; enabled = a Subscribe CTA."""

    def test_subscribe_cta_when_enabled(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "BILLING_ENABLED", True)
        page = _dashboard(monkeypatch, tmp_path)
        assert "Subscribe" in page

    def test_launch_soon_copy_when_disabled(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "BILLING_ENABLED", False)
        page = _dashboard(monkeypatch, tmp_path)
        assert "Paid plans launch soon" in page

    def test_settings_billing_section_when_enabled(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "BILLING_ENABLED", True)
        _setup(monkeypatch, tmp_path)
        with TestClient(app) as c:
            c.cookies.set("feedecho_session", security.sign_session(USER_ID, "trial@example.com", 0))
            page = c.get("/settings").text
        assert 'id="billing"' in page
        assert "$4/month" in page
        assert "$40/year" in page

    def test_settings_billing_section_hidden_when_disabled(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "BILLING_ENABLED", False)
        _setup(monkeypatch, tmp_path)
        with TestClient(app) as c:
            c.cookies.set("feedecho_session", security.sign_session(USER_ID, "trial@example.com", 0))
            page = c.get("/settings").text
        assert 'id="billing"' not in page
