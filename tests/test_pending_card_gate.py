"""The card-pending gate: an account that registered but never collected a
card must not use the app.

Registration with billing on writes the TRIAL_PENDING sentinel as
trial_ends_at; the Stripe subscription webhook replaces it once the card is
collected. Until then the account is gated off every app route except the
checkout/settings/logout/delete paths, so a bot that abandons Checkout cannot
poke at feeds, echoes, or accounts. Posting was already paused; this closes
the "still get in and click around" loophole.
"""

import pytest
from fastapi.testclient import TestClient

import database
import plans
import security
import settings
from app import app

USER_ID = 222


def _client(monkeypatch, tmp_path, *, billing, trial_ends_at, plan="trial"):
    monkeypatch.setattr(settings, "MULTI", True)
    monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
    monkeypatch.setattr(settings, "BILLING_ENABLED", billing)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "gate.db")
    import app as app_module

    monkeypatch.setattr(app_module, "_assert_billing_mounted", lambda app: None)
    database.init_db()
    with database.get_db() as db:
        db.execute(
            "INSERT OR IGNORE INTO users"
            " (id, email, password_hash, plan, is_admin, suspended, trial_ends_at)"
            " VALUES (?, 'pending@example.com', '', ?, 0, 0, ?)",
            (USER_ID, plan, trial_ends_at),
        )
    token = security.sign_session(USER_ID, "pending@example.com", 0)
    client = TestClient(app)
    client.cookies.set("feedecho_session", token)
    return client


def test_pending_user_bounced_from_dashboard(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path, billing=True, trial_ends_at=plans.TRIAL_PENDING)
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/settings#billing"


def test_pending_user_bounced_from_app_routes(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path, billing=True, trial_ends_at=plans.TRIAL_PENDING)
    for path in ("/feeds", "/accounts", "/echoes", "/history", "/reader"):
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 302, path
        assert r.headers["location"] == "/settings#billing", path


def test_pending_user_can_reach_settings(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path, billing=True, trial_ends_at=plans.TRIAL_PENDING)
    assert c.get("/settings").status_code == 200


def test_pending_user_checkout_not_redirected(monkeypatch, tmp_path):
    # The gate must not bounce the checkout route itself. In the OSS test app
    # the route isn't registered, so it 404s — but crucially NOT a /settings
    # redirect, proving the allow-list let it through.
    c = _client(monkeypatch, tmp_path, billing=True, trial_ends_at=plans.TRIAL_PENDING)
    r = c.get("/api/billing/checkout", follow_redirects=False)
    assert r.status_code != 302


def test_active_trial_not_gated(monkeypatch, tmp_path):
    c = _client(
        monkeypatch, tmp_path, billing=True, trial_ends_at="2030-01-01 00:00:00"
    )
    assert c.get("/").status_code == 200


def test_paid_user_not_gated(monkeypatch, tmp_path):
    c = _client(
        monkeypatch, tmp_path, billing=True, trial_ends_at=None, plan="paid"
    )
    assert c.get("/").status_code == 200


def test_no_billing_sentinel_not_gated(monkeypatch, tmp_path):
    # Billing off + sentinel value must NOT gate: self-hosters may have any
    # value in the column and must never be locked out of their own instance.
    c = _client(monkeypatch, tmp_path, billing=False, trial_ends_at=plans.TRIAL_PENDING)
    assert c.get("/").status_code == 200
