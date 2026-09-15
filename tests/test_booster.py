"""FeedBooster integration: accounts.booster_enabled column, the per-account
toggle route, the accounts-page UI gate, and the scheduler boost hook."""

import json

import pytest
from fastapi.testclient import TestClient

import auth
import database
import security
import settings
from app import app

BOOSTER_URL = "https://booster.example"
BOOSTER_TOKEN = "booster-secret"


@pytest.fixture()
def multi_client(monkeypatch, db_tmp):
    """Signed-in multi-mode TestClient over the temp DB."""
    monkeypatch.setattr(settings, "MULTI", True)
    monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
    monkeypatch.setattr(settings, "STATE_SECRET", "s" * 40)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
    monkeypatch.setattr(settings, "BOOSTER_URL", BOOSTER_URL)
    monkeypatch.setattr(settings, "BOOSTER_TOKEN", BOOSTER_TOKEN)
    auth._login_attempts.clear()
    auth._register_attempts.clear()

    UID = 5
    with database.get_db() as db:
        db.execute(
            "INSERT INTO users (id, email, password_hash, email_verified)"
            " VALUES (?, 'u@example.com', '', 1)",
            (UID,),
        )
        db.execute(
            "INSERT INTO accounts (id, name, username, instance, access_token, user_id)"
            " VALUES (1, 'acc', 'acc', 'https://m.example', 'x', ?)",
            (UID,),
        )
    client = TestClient(app)
    client.cookies.set("feedecho_session", security.sign_session(UID, "u@example.com"))
    return client


@pytest.fixture()
def unconfigured_client(multi_client, monkeypatch):
    monkeypatch.setattr(settings, "BOOSTER_URL", "")
    monkeypatch.setattr(settings, "BOOSTER_TOKEN", "")
    return multi_client


class TestColumn:
    def test_column_exists_on_fresh_db(self, db_tmp):
        with database.get_db() as db:
            db.execute("SELECT booster_enabled FROM accounts LIMIT 1")

    def test_default_is_zero(self, multi_client):
        with database.get_db() as db:
            row = db.execute("SELECT booster_enabled FROM accounts WHERE id = 1").fetchone()
        assert row["booster_enabled"] == 0


class TestToggleRoute:
    def test_toggle_on_then_off(self, multi_client):
        assert multi_client.post("/api/accounts/1/booster", follow_redirects=False).status_code == 303
        with database.get_db() as db:
            row = db.execute("SELECT booster_enabled FROM accounts WHERE id = 1").fetchone()
        assert row["booster_enabled"] == 1

        assert multi_client.post("/api/accounts/1/booster", follow_redirects=False).status_code == 303
        with database.get_db() as db:
            row = db.execute("SELECT booster_enabled FROM accounts WHERE id = 1").fetchone()
        assert row["booster_enabled"] == 0

    def test_other_users_account_404s(self, multi_client, db_tmp):
        with database.get_db() as db:
            db.execute(
                "INSERT INTO accounts (id, name, username, instance, access_token, user_id)"
                " VALUES (99, 'other', 'other', 'https://m.example', 'x', 777)"
            )
        r = multi_client.post("/api/accounts/99/booster", follow_redirects=False)
        assert r.status_code == 404

    def test_missing_account_404s(self, multi_client):
        assert multi_client.post("/api/accounts/12345/booster").status_code == 404

    def test_refuses_when_booster_unconfigured(self, unconfigured_client):
        r = unconfigured_client.post("/api/accounts/1/booster")
        assert r.status_code == 200  # rendered accounts page with the error banner
        with database.get_db() as db:
            row = db.execute("SELECT booster_enabled FROM accounts WHERE id = 1").fetchone()
        assert row["booster_enabled"] == 0


class TestAccountsPageUI:
    def test_toggle_hidden_when_unconfigured(self, unconfigured_client):
        r = unconfigured_client.get("/accounts")
        assert r.status_code == 200
        assert "booster" not in r.text.lower()

    def test_toggle_visible_when_configured(self, multi_client):
        r = multi_client.get("/accounts")
        assert r.status_code == 200
        assert '/api/accounts/1/booster' in r.text
        assert "Boost: Off" in r.text

    def test_toggle_shows_on_state(self, multi_client):
        multi_client.post("/api/accounts/1/booster", follow_redirects=False)
        r = multi_client.get("/accounts")
        assert "Boost: On" in r.text


class TestSchedulerHook:
    def test_no_call_when_disabled(self, monkeypatch):
        import scheduler

        calls = []
        monkeypatch.setattr(scheduler.httpx, "post", lambda *a, **k: calls.append(a))
        account = {"booster_enabled": 0}
        scheduler._maybe_boost(account, "https://m.example/@a/1", 1)
        assert not calls

    def test_no_call_when_unconfigured(self, monkeypatch):
        import scheduler

        monkeypatch.setattr(settings, "BOOSTER_URL", "")
        monkeypatch.setattr(settings, "BOOSTER_TOKEN", "")
        calls = []
        monkeypatch.setattr(scheduler.httpx, "post", lambda *a, **k: calls.append(a))
        scheduler._maybe_boost({"booster_enabled": 1}, "https://m.example/@a/1", 1)
        assert not calls

    def test_posts_to_booster_when_enabled(self, monkeypatch):
        import scheduler

        captured = {}

        class FakeResponse:
            status_code = 202

        def fake_post(url, json=None, headers=None, timeout=None):
            captured.update(url=url, json=json, headers=headers, timeout=timeout)
            return FakeResponse()

        monkeypatch.setattr(settings, "BOOSTER_URL", BOOSTER_URL)
        monkeypatch.setattr(settings, "BOOSTER_TOKEN", BOOSTER_TOKEN)
        monkeypatch.setattr(scheduler.httpx, "post", fake_post)
        scheduler._maybe_boost({"booster_enabled": 1}, "https://m.example/@a/1", 1)
        assert captured["url"] == f"{BOOSTER_URL}/internal/boost"
        assert captured["json"] == {"url": "https://m.example/@a/1"}
        assert captured["headers"]["authorization"] == f"Bearer {BOOSTER_TOKEN}"
        assert captured["timeout"] <= 10

    def test_booster_outage_never_raises(self, monkeypatch):
        import scheduler

        def fake_post(*a, **k):
            raise ConnectionError("booster down")

        monkeypatch.setattr(settings, "BOOSTER_URL", BOOSTER_URL)
        monkeypatch.setattr(settings, "BOOSTER_TOKEN", BOOSTER_TOKEN)
        monkeypatch.setattr(scheduler.httpx, "post", fake_post)
        scheduler._maybe_boost({"booster_enabled": 1}, "https://m.example/@a/1", 1)

    def test_missing_column_treated_as_disabled(self, monkeypatch):
        import scheduler

        calls = []
        monkeypatch.setattr(scheduler.httpx, "post", lambda *a, **k: calls.append(a))
        scheduler._maybe_boost({}, "https://m.example/@a/1", 1)
        assert not calls
