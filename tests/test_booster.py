"""FeedBooster integration: the per-account toggle (accounts.booster_enabled),
the per-echo toggle (echoes.booster_enabled) and its mutual exclusivity with
the account setting, the echoes-page UI gate, and the scheduler boost hook."""

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


@pytest.fixture()
def single_client(monkeypatch, db_tmp):
    """Single-mode TestClient: shared-secret cookie auth, operator uid 1."""
    monkeypatch.setattr(settings, "MULTI", False)
    monkeypatch.setattr(settings, "AUTH_TOKEN", "single-token")
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
    monkeypatch.setattr(settings, "BOOSTER_URL", BOOSTER_URL)
    monkeypatch.setattr(settings, "BOOSTER_TOKEN", BOOSTER_TOKEN)
    client = TestClient(app)
    client.cookies.set(auth.AUTH_COOKIE_NAME, "single-token")
    return client


@pytest.fixture()
def seeded_client(multi_client):
    """multi_client plus feed 1 + Mastodon echo 1, both owned by user 5."""
    with database.get_db() as db:
        db.execute(
            "INSERT INTO feeds (id, name, url, user_id)"
            " VALUES (1, 'f', 'https://f.example/rss', 5)"
        )
        db.execute(
            "INSERT INTO echoes (id, feed_id, destination_type, destination_id, user_id)"
            " VALUES (1, 1, 'mastodon', 1, 5)"
        )
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


class TestEchoColumn:
    def test_column_exists_on_fresh_db(self, db_tmp):
        with database.get_db() as db:
            db.execute("SELECT booster_enabled FROM echoes LIMIT 1")

    def test_default_is_zero(self, seeded_client):
        with database.get_db() as db:
            row = db.execute("SELECT booster_enabled FROM echoes WHERE id = 1").fetchone()
        assert row["booster_enabled"] == 0


class TestEchoToggleRoute:
    def test_toggle_on_then_off(self, seeded_client):
        r = seeded_client.post("/api/echoes/1/booster", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/echoes"
        with database.get_db() as db:
            row = db.execute("SELECT booster_enabled FROM echoes WHERE id = 1").fetchone()
        assert row["booster_enabled"] == 1

        r = seeded_client.post("/api/echoes/1/booster", follow_redirects=False)
        assert r.status_code == 303
        with database.get_db() as db:
            row = db.execute("SELECT booster_enabled FROM echoes WHERE id = 1").fetchone()
        assert row["booster_enabled"] == 0

    def test_other_users_echo_404s(self, multi_client, db_tmp):
        with database.get_db() as db:
            db.execute(
                "INSERT INTO feeds (id, name, url, user_id)"
                " VALUES (99, 'other', 'https://o.example/rss', 777)"
            )
            db.execute(
                "INSERT INTO echoes (id, feed_id, destination_type, destination_id, user_id)"
                " VALUES (99, 99, 'mastodon', 99, 777)"
            )
        r = multi_client.post("/api/echoes/99/booster", follow_redirects=False)
        assert r.status_code == 404

    def test_missing_echo_404s(self, seeded_client):
        assert seeded_client.post("/api/echoes/12345/booster").status_code == 404

    def test_deleted_echo_404s(self, seeded_client, db_tmp):
        with database.get_db() as db:
            db.execute("UPDATE echoes SET deleted_at = CURRENT_TIMESTAMP WHERE id = 1")
        assert seeded_client.post("/api/echoes/1/booster").status_code == 404

    def test_refuses_when_booster_unconfigured(self, unconfigured_client):
        # Checked before the echo lookup: the banner renders regardless.
        r = unconfigured_client.post("/api/echoes/1/booster")
        assert r.status_code == 200
        assert "FeedBooster is not configured on this server." in r.text

    def test_refuses_for_non_mastodon_echo(self, seeded_client, db_tmp):
        with database.get_db() as db:
            db.execute(
                "INSERT INTO echoes (id, feed_id, destination_type, destination_id, user_id)"
                " VALUES (2, 1, 'bluesky', 1, 5)"
            )
        r = seeded_client.post("/api/echoes/2/booster", follow_redirects=False)
        assert r.status_code == 200
        assert "only available for echoes to Mastodon" in r.text
        with database.get_db() as db:
            row = db.execute("SELECT booster_enabled FROM echoes WHERE id = 2").fetchone()
        assert row["booster_enabled"] == 0

    def test_refused_while_account_boost_on_then_allowed_after(self, seeded_client):
        # Account-level boost on: the per-echo toggle refuses and flips nothing.
        assert seeded_client.post("/api/accounts/1/booster", follow_redirects=False).status_code == 303
        r = seeded_client.post("/api/echoes/1/booster", follow_redirects=False)
        assert r.status_code == 200
        assert "already enabled for this Mastodon account" in r.text
        with database.get_db() as db:
            row = db.execute("SELECT booster_enabled FROM echoes WHERE id = 1").fetchone()
        assert row["booster_enabled"] == 0

        # Account-level boost off again: the per-echo toggle works.
        assert seeded_client.post("/api/accounts/1/booster", follow_redirects=False).status_code == 303
        r2 = seeded_client.post("/api/echoes/1/booster", follow_redirects=False)
        assert r2.status_code == 303
        with database.get_db() as db:
            row = db.execute("SELECT booster_enabled FROM echoes WHERE id = 1").fetchone()
        assert row["booster_enabled"] == 1

    def test_account_boost_supersedes_then_echo_flag_persists(self, seeded_client):
        # Approved semantics: while the account setting is on, both flags may
        # be set in the database (the account supersedes per-echo, and the UI
        # shows the locked badge instead of the toggle). Turning the account
        # setting back off does NOT clear per-echo flags — flags set prior to
        # enabling the account setting resume boosting. (The toggle route
        # refuses NEW enables while the account setting is on, so a flag can
        # only be set while it is off; import/export deliberately does not
        # carry the flag.)
        assert seeded_client.post("/api/echoes/1/booster", follow_redirects=False).status_code == 303
        assert seeded_client.post("/api/accounts/1/booster", follow_redirects=False).status_code == 303
        r = seeded_client.get("/echoes")
        assert "Boost: Account" in r.text
        with database.get_db() as db:
            acct = db.execute("SELECT booster_enabled FROM accounts WHERE id = 1").fetchone()
            echo = db.execute("SELECT booster_enabled FROM echoes WHERE id = 1").fetchone()
        assert acct["booster_enabled"] == 1
        assert echo["booster_enabled"] == 1

        assert seeded_client.post("/api/accounts/1/booster", follow_redirects=False).status_code == 303
        with database.get_db() as db:
            echo = db.execute("SELECT booster_enabled FROM echoes WHERE id = 1").fetchone()
        assert echo["booster_enabled"] == 1

    def test_edit_destination_change_clears_echo_boost(self, seeded_client, db_tmp):
        with database.get_db() as db:
            db.execute(
                "INSERT INTO accounts (id, name, username, instance, access_token, user_id)"
                " VALUES (2, 'acc2', 'acc2', 'https://m.example', 'x', 5)"
            )
        assert seeded_client.post("/api/echoes/1/booster", follow_redirects=False).status_code == 303
        r = seeded_client.post(
            "/api/echoes/1/edit",
            data={"feed_id": 1, "destination_type": "mastodon", "account_id": 2, "enabled": "true"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        with database.get_db() as db:
            echo = db.execute(
                "SELECT destination_id, booster_enabled FROM echoes WHERE id = 1"
            ).fetchone()
        assert echo["destination_id"] == 2
        assert echo["booster_enabled"] == 0

    def test_edit_without_destination_change_preserves_echo_boost(self, seeded_client):
        assert seeded_client.post("/api/echoes/1/booster", follow_redirects=False).status_code == 303
        r = seeded_client.post(
            "/api/echoes/1/edit",
            data={
                "feed_id": 1, "destination_type": "mastodon", "account_id": 1,
                "template": "{{ title }}", "enabled": "true",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        with database.get_db() as db:
            echo = db.execute(
                "SELECT template, booster_enabled FROM echoes WHERE id = 1"
            ).fetchone()
        assert echo["booster_enabled"] == 1
        assert echo["template"] == "{{ title }}"


class TestEchoesPageUI:
    def test_toggle_hidden_when_unconfigured(self, unconfigured_client):
        r = unconfigured_client.get("/echoes")
        assert r.status_code == 200
        assert "boost" not in r.text.lower()

    def test_error_banner_renders_on_refusal(self, unconfigured_client):
        r = unconfigured_client.post("/api/echoes/1/booster")
        assert r.status_code == 200
        assert "FeedBooster is not configured on this server." in r.text

    def test_toggle_visible_when_configured(self, seeded_client):
        r = seeded_client.get("/echoes")
        assert r.status_code == 200
        assert '/api/echoes/1/booster' in r.text
        assert "Boost: Off" in r.text
        # Off state never carries the active styling (strict pin for F8).
        assert "btn-success" not in r.text

    def test_toggle_shows_on_state(self, seeded_client):
        assert seeded_client.post("/api/echoes/1/booster", follow_redirects=False).status_code == 303
        r = seeded_client.get("/echoes")
        assert "Boost: On" in r.text
        assert "btn-success" in r.text
        # Strict pin: one state marker per echo, so a revert of the flip or
        # the template state logic shows up here.
        assert "Boost: Off" not in r.text

    def test_locked_when_account_boost_on(self, seeded_client):
        assert seeded_client.post("/api/accounts/1/booster", follow_redirects=False).status_code == 303
        r = seeded_client.get("/echoes")
        assert "Boost: Account" in r.text
        # No per-echo toggle form while the account setting owns the state.
        assert '/api/echoes/1/booster' not in r.text

    def test_no_toggle_for_non_mastodon_echo(self, seeded_client, db_tmp):
        with database.get_db() as db:
            db.execute(
                "INSERT INTO echoes (id, feed_id, destination_type, destination_id, user_id)"
                " VALUES (2, 1, 'bluesky', 1, 5)"
            )
        r = seeded_client.get("/echoes")
        assert r.status_code == 200
        assert '/api/echoes/2/booster' not in r.text


class TestSchedulerHook:
    def test_no_call_when_disabled(self, monkeypatch):
        import scheduler

        calls = []
        monkeypatch.setattr(scheduler.httpx, "post", lambda *a, **k: calls.append(a))
        scheduler._maybe_boost(
            {"booster_enabled": 0}, {"id": 1, "booster_enabled": 0}, "https://m.example/@a/1"
        )
        assert not calls

    def test_no_call_when_both_flags_missing(self, monkeypatch):
        import scheduler

        calls = []
        monkeypatch.setattr(scheduler.httpx, "post", lambda *a, **k: calls.append(a))
        scheduler._maybe_boost({}, {"id": 1}, "https://m.example/@a/1")
        assert not calls

    def test_no_call_for_private_or_direct(self, monkeypatch):
        # HIGH-gate fix: followers-only and direct echoes must never be sent
        # to the booster, even with both toggles on.
        import scheduler

        calls = []
        monkeypatch.setattr(scheduler.httpx, "post", lambda *a, **k: calls.append(a))
        monkeypatch.setattr(settings, "BOOSTER_URL", BOOSTER_URL)
        monkeypatch.setattr(settings, "BOOSTER_TOKEN", BOOSTER_TOKEN)
        for visibility in ("private", "direct"):
            scheduler._maybe_boost(
                {"booster_enabled": 1},
                {"id": 1, "booster_enabled": 1},
                "https://m.example/@a/1",
                visibility,
            )
        assert not calls

    def test_unlisted_is_boosted(self, monkeypatch):
        import scheduler

        captured = {}

        class FakeResponse:
            status_code = 202

        monkeypatch.setattr(settings, "BOOSTER_URL", BOOSTER_URL)
        monkeypatch.setattr(settings, "BOOSTER_TOKEN", BOOSTER_TOKEN)
        monkeypatch.setattr(
            scheduler.httpx,
            "post",
            lambda url, json=None, headers=None, timeout=None: captured.update(url=url)
            or FakeResponse(),
        )
        scheduler._maybe_boost(
            {"booster_enabled": 0},
            {"id": 1, "booster_enabled": 1},
            "https://m.example/@a/1",
            "unlisted",
        )
        assert captured["url"] == f"{BOOSTER_URL}/internal/boost"

    def test_error_page_keeps_booster_buttons(self, multi_client, monkeypatch):
        # MEDIUM-gate fix: _render_accounts_error must pass booster_configured
        # so the Boost buttons don't vanish on error renders. Deleting an
        # account that still has dependent echoes hits the error path.
        with database.get_db() as db:
            db.execute(
                "INSERT INTO feeds (id, name, url, user_id)"
                " VALUES (1, 'f', 'https://f.example/rss', 5)"
            )
            db.execute(
                "INSERT INTO echoes (feed_id, destination_type, destination_id, user_id)"
                " VALUES (1, 'mastodon', 1, 5)"
            )
        monkeypatch.setattr(settings, "BOOSTER_URL", "")
        monkeypatch.setattr(settings, "BOOSTER_TOKEN", "")
        r = multi_client.post("/api/accounts/1/delete")
        assert r.status_code == 200
        assert "booster" not in r.text.lower()

        # And for a configured booster the buttons survive the error render.
        monkeypatch.setattr(settings, "BOOSTER_URL", BOOSTER_URL)
        monkeypatch.setattr(settings, "BOOSTER_TOKEN", BOOSTER_TOKEN)
        r2 = multi_client.post("/api/accounts/1/delete")
        assert r2.status_code == 200
        assert "/api/accounts/1/booster" in r2.text

    def test_no_call_when_unconfigured(self, monkeypatch):
        import scheduler

        monkeypatch.setattr(settings, "BOOSTER_URL", "")
        monkeypatch.setattr(settings, "BOOSTER_TOKEN", "")
        calls = []
        monkeypatch.setattr(scheduler.httpx, "post", lambda *a, **k: calls.append(a))
        scheduler._maybe_boost(
            {"booster_enabled": 1}, {"id": 1, "booster_enabled": 1}, "https://m.example/@a/1"
        )
        assert not calls

    def test_posts_to_booster_when_account_enabled(self, monkeypatch):
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
        scheduler._maybe_boost(
            {"booster_enabled": 1}, {"id": 1, "booster_enabled": 0}, "https://m.example/@a/1"
        )
        assert captured["url"] == f"{BOOSTER_URL}/internal/boost"
        assert captured["json"] == {"url": "https://m.example/@a/1"}
        assert captured["headers"]["authorization"] == f"Bearer {BOOSTER_TOKEN}"
        assert captured["timeout"] <= 10

    def test_posts_to_booster_when_only_echo_enabled(self, monkeypatch):
        # The per-echo toggle alone must drive the boost when the account
        # setting is off.
        import scheduler

        captured = {}

        class FakeResponse:
            status_code = 202

        def fake_post(url, json=None, headers=None, timeout=None):
            captured.update(url=url)
            return FakeResponse()

        monkeypatch.setattr(settings, "BOOSTER_URL", BOOSTER_URL)
        monkeypatch.setattr(settings, "BOOSTER_TOKEN", BOOSTER_TOKEN)
        monkeypatch.setattr(scheduler.httpx, "post", fake_post)
        scheduler._maybe_boost(
            {"booster_enabled": 0}, {"id": 1, "booster_enabled": 1}, "https://m.example/@a/1"
        )
        assert captured["url"] == f"{BOOSTER_URL}/internal/boost"

    def test_booster_outage_never_raises(self, monkeypatch):
        import scheduler

        def fake_post(*a, **k):
            raise ConnectionError("booster down")

        monkeypatch.setattr(settings, "BOOSTER_URL", BOOSTER_URL)
        monkeypatch.setattr(settings, "BOOSTER_TOKEN", BOOSTER_TOKEN)
        monkeypatch.setattr(scheduler.httpx, "post", fake_post)
        scheduler._maybe_boost(
            {"booster_enabled": 0}, {"id": 1, "booster_enabled": 1}, "https://m.example/@a/1"
        )

    def test_missing_columns_treated_as_disabled(self, monkeypatch):
        import scheduler

        calls = []
        monkeypatch.setattr(scheduler.httpx, "post", lambda *a, **k: calls.append(a))
        scheduler._maybe_boost({}, {}, "https://m.example/@a/1")
        assert not calls

    def test_none_inputs_do_not_raise(self, monkeypatch):
        import scheduler

        calls = []
        monkeypatch.setattr(scheduler.httpx, "post", lambda *a, **k: calls.append(a))
        scheduler._maybe_boost(None, None, "https://m.example/@a/1")
        assert not calls


class TestSingleMode:
    def _seed_operator_echo(self):
        with database.get_db() as db:
            db.execute(
                "INSERT INTO accounts (id, name, username, instance, access_token, user_id)"
                " VALUES (1, 'acc', 'acc', 'https://m.example', 'x', 1)"
            )
            db.execute(
                "INSERT INTO feeds (id, name, url, user_id)"
                " VALUES (1, 'f', 'https://f.example/rss', 1)"
            )
            db.execute(
                "INSERT INTO echoes (id, feed_id, destination_type, destination_id, user_id)"
                " VALUES (1, 1, 'mastodon', 1, 1)"
            )

    def test_echo_boost_toggle_roundtrip(self, single_client):
        self._seed_operator_echo()
        r = single_client.post("/api/echoes/1/booster", follow_redirects=False)
        assert r.status_code == 303
        with database.get_db() as db:
            row = db.execute("SELECT booster_enabled FROM echoes WHERE id = 1").fetchone()
        assert row["booster_enabled"] == 1

        # Toggle back off: a true roundtrip.
        r2 = single_client.post("/api/echoes/1/booster", follow_redirects=False)
        assert r2.status_code == 303
        with database.get_db() as db:
            row = db.execute("SELECT booster_enabled FROM echoes WHERE id = 1").fetchone()
        assert row["booster_enabled"] == 0

    def test_exclusivity_refusal_in_single_mode(self, single_client):
        self._seed_operator_echo()
        assert single_client.post("/api/accounts/1/booster", follow_redirects=False).status_code == 303
        r = single_client.post("/api/echoes/1/booster", follow_redirects=False)
        assert r.status_code == 200
        assert "already enabled for this Mastodon account" in r.text
        with database.get_db() as db:
            row = db.execute("SELECT booster_enabled FROM echoes WHERE id = 1").fetchone()
        assert row["booster_enabled"] == 0

    def test_page_renders_with_toggle(self, single_client):
        self._seed_operator_echo()
        r = single_client.get("/echoes")
        assert r.status_code == 200
        assert "/api/echoes/1/booster" in r.text
        assert "Boost: Off" in r.text
