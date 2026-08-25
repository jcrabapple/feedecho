"""OAuth app registration: the website Mastodon shows on every post.

Regression coverage for issue #7 — the registration hardcoded
https://feedecho.example.com, so posts from any install (self-hosted or
hosted) linked the application name at a domain that does not exist.
"""

import importlib
import os
import tempfile

import pytest

import oauth
import settings


@pytest.fixture
def restore_settings():
    """Re-read settings from the real env after tests that reload it.

    Autouse-free but requested by every reload test. It is torn down after
    monkeypatch has already restored the environment, so the reload leaves
    the module in the state the rest of the suite expects.
    """
    yield
    importlib.reload(settings)


@pytest.fixture
def db_tmp(monkeypatch):
    """Point the DB layer at a fresh temp file per test."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)

    import database

    monkeypatch.setattr(database, "DB_PATH", database.Path(path))
    database.init_db()

    yield database

    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except OSError:
            pass


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeClient:
    """Records every /api/v1/apps registration payload."""

    calls: list = []

    def __init__(self, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, data=None, **kw):
        type(self).calls.append((url, data))
        return _FakeResponse(
            {"client_id": "cid-%d" % len(type(self).calls), "client_secret": "csec"}
        )


@pytest.fixture
def fake_http(monkeypatch):
    _FakeClient.calls = []
    monkeypatch.setattr(oauth.httpx, "Client", _FakeClient)
    # Registration targets a fake host; skip the DNS/SSRF round trip.
    monkeypatch.setattr(oauth, "validate_outbound_url", lambda url: None)
    return _FakeClient


class TestAppWebsiteResolution:
    def test_defaults_to_project_repo_not_placeholder(
        self, monkeypatch, restore_settings
    ):
        for key in list(os.environ):
            if key.startswith("FEEDCHO_"):
                monkeypatch.delenv(key)
        s = importlib.reload(settings)
        assert s.APP_WEBSITE == "https://github.com/jcrabapple/feedecho"
        assert "example.com" not in s.APP_WEBSITE

    def test_base_url_wins_over_repo_fallback(self, monkeypatch, restore_settings):
        monkeypatch.setenv("FEEDCHO_BASE_URL", "https://echo.abhinavsarkar.net/")
        monkeypatch.delenv("FEEDCHO_APP_WEBSITE", raising=False)
        s = importlib.reload(settings)
        assert s.APP_WEBSITE == "https://echo.abhinavsarkar.net"

    def test_explicit_override_wins_over_base_url(self, monkeypatch, restore_settings):
        monkeypatch.setenv("FEEDCHO_BASE_URL", "https://echo.example.org")
        monkeypatch.setenv("FEEDCHO_APP_WEBSITE", "https://abhinavsarkar.net/feedecho")
        s = importlib.reload(settings)
        assert s.APP_WEBSITE == "https://abhinavsarkar.net/feedecho"

    def test_callback_url_derives_from_base_url(self, monkeypatch, restore_settings):
        monkeypatch.setenv("FEEDCHO_BASE_URL", "https://echo.abhinavsarkar.net/")
        monkeypatch.delenv("FEEDCHO_CALLBACK_URL", raising=False)
        s = importlib.reload(settings)
        assert s.CALLBACK_URL == "https://echo.abhinavsarkar.net/oauth/callback"

    def test_whitespace_base_url_is_ignored(self, monkeypatch, restore_settings):
        """A whitespace-only base URL is truthy; it must not become the link."""
        for key in list(os.environ):
            if key.startswith("FEEDCHO_"):
                monkeypatch.delenv(key)
        monkeypatch.setenv("FEEDCHO_BASE_URL", "   ")
        s = importlib.reload(settings)
        assert s.APP_WEBSITE == "https://github.com/jcrabapple/feedecho"
        assert s.CALLBACK_URL == "https://feedecho.example.com/oauth/callback"

    def test_helper_never_returns_empty(self, monkeypatch):
        monkeypatch.setattr(settings, "APP_WEBSITE", "")
        assert oauth.app_website() == settings.PROJECT_URL

    def test_fallback_literal_matches_project_url(self):
        """Guard against the two copies of the repo link drifting apart."""
        assert oauth.FALLBACK_WEBSITE == settings.PROJECT_URL


class TestRegistrationPayload:
    def test_registration_sends_configured_website(
        self, db_tmp, fake_http, monkeypatch
    ):
        monkeypatch.setattr(settings, "APP_WEBSITE", "https://echo.example.org")
        oauth.get_or_create_app("https://mastodon.social")

        url, data = fake_http.calls[0]
        assert url == "https://mastodon.social/api/v1/apps"
        assert data["website"] == "https://echo.example.org"
        assert data["client_name"] == "FeedEcho"

    def test_registration_never_sends_placeholder_domain(
        self, db_tmp, fake_http, monkeypatch
    ):
        monkeypatch.setattr(settings, "APP_WEBSITE", "")
        oauth.get_or_create_app("https://mastodon.social")

        _, data = fake_http.calls[0]
        assert "feedecho.example.com" not in data["website"]
        assert data["website"] == "https://github.com/jcrabapple/feedecho"

    def test_website_is_persisted(self, db_tmp, fake_http, monkeypatch):
        monkeypatch.setattr(settings, "APP_WEBSITE", "https://echo.example.org")
        monkeypatch.setattr(
            settings, "CALLBACK_URL", "https://echo.example.org/oauth/callback"
        )
        oauth.get_or_create_app("https://mastodon.social")

        with db_tmp.get_db() as db:
            row = db.execute(
                "SELECT website, redirect_uris FROM oauth_apps WHERE instance = ?",
                ("https://mastodon.social",),
            ).fetchone()
        assert row["website"] == "https://echo.example.org"
        assert row["redirect_uris"] == "https://echo.example.org/oauth/callback"

    def test_registration_sends_configured_callback(
        self, db_tmp, fake_http, monkeypatch
    ):
        monkeypatch.setattr(
            settings, "CALLBACK_URL", "https://echo.example.org/oauth/callback"
        )
        oauth.get_or_create_app("https://mastodon.social")

        _, data = fake_http.calls[0]
        assert data["redirect_uris"] == "https://echo.example.org/oauth/callback"


class TestCacheInvalidation:
    def test_matching_website_reuses_cached_credentials(
        self, db_tmp, fake_http, monkeypatch
    ):
        monkeypatch.setattr(settings, "APP_WEBSITE", "https://echo.example.org")
        first = oauth.get_or_create_app("https://mastodon.social")
        second = oauth.get_or_create_app("https://mastodon.social")

        assert first == second
        assert len(fake_http.calls) == 1, "cached app should not re-register"

    def test_changed_website_forces_reregistration(
        self, db_tmp, fake_http, monkeypatch
    ):
        monkeypatch.setattr(settings, "APP_WEBSITE", "https://old.example.org")
        first = oauth.get_or_create_app("https://mastodon.social")

        monkeypatch.setattr(settings, "APP_WEBSITE", "https://new.example.org")
        second = oauth.get_or_create_app("https://mastodon.social")

        assert len(fake_http.calls) == 2
        assert fake_http.calls[1][1]["website"] == "https://new.example.org"
        assert second["client_id"] != first["client_id"]

        with db_tmp.get_db() as db:
            rows = db.execute(
                "SELECT client_id, website FROM oauth_apps WHERE instance = ?",
                ("https://mastodon.social",),
            ).fetchall()
        assert len(rows) == 1, "re-registration must upsert, not duplicate"
        assert rows[0]["website"] == "https://new.example.org"
        assert rows[0]["client_id"] == second["client_id"]

    def test_legacy_row_without_website_reregisters(
        self, db_tmp, fake_http, monkeypatch
    ):
        """Installs predating this fix have NULL website and a bad post link."""
        with db_tmp.get_db() as db:
            db.execute(
                "INSERT INTO oauth_apps (instance, client_id, client_secret) "
                "VALUES (?, ?, ?)",
                ("https://mastodon.social", "legacy-cid", "legacy-secret"),
            )

        monkeypatch.setattr(settings, "APP_WEBSITE", "https://echo.example.org")
        result = oauth.get_or_create_app("https://mastodon.social")

        assert len(fake_http.calls) == 1
        assert result["client_id"] != "legacy-cid"
        with db_tmp.get_db() as db:
            row = db.execute(
                "SELECT website FROM oauth_apps WHERE instance = ?",
                ("https://mastodon.social",),
            ).fetchone()
        assert row["website"] == "https://echo.example.org"

    def test_no_refresh_pins_cached_client(self, db_tmp, fake_http, monkeypatch):
        """The token exchange must reuse the client that issued the code.

        Re-registering mid-flow would hand Mastodon a client_id that never
        saw the authorization code, so allow_refresh=False stays on the
        cached row even when the configured website has drifted.
        """
        with db_tmp.get_db() as db:
            db.execute(
                "INSERT INTO oauth_apps"
                " (instance, client_id, client_secret, website, redirect_uris)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    "https://mastodon.social",
                    "issued-cid",
                    "issued-secret",
                    "https://old.example.org",
                    "https://old.example.org/oauth/callback",
                ),
            )

        monkeypatch.setattr(settings, "APP_WEBSITE", "https://new.example.org")
        pinned = oauth.get_or_create_app(
            "https://mastodon.social", allow_refresh=False
        )

        assert pinned == {"client_id": "issued-cid", "client_secret": "issued-secret"}
        assert fake_http.calls == [], "token exchange must not re-register"

    def test_changed_callback_forces_reregistration(
        self, db_tmp, fake_http, monkeypatch
    ):
        """A drifted callback URL is rejected by the instance as a redirect
        mismatch, so the cached registration must not survive it."""
        monkeypatch.setattr(settings, "APP_WEBSITE", "https://echo.example.org")
        monkeypatch.setattr(
            settings, "CALLBACK_URL", "http://localhost:8453/oauth/callback"
        )
        oauth.get_or_create_app("https://mastodon.social")

        monkeypatch.setattr(
            settings, "CALLBACK_URL", "https://echo.example.org/oauth/callback"
        )
        oauth.get_or_create_app("https://mastodon.social")

        assert len(fake_http.calls) == 2
        assert (
            fake_http.calls[1][1]["redirect_uris"]
            == "https://echo.example.org/oauth/callback"
        )
        with db_tmp.get_db() as db:
            rows = db.execute("SELECT redirect_uris FROM oauth_apps").fetchall()
        assert len(rows) == 1
        assert rows[0]["redirect_uris"] == "https://echo.example.org/oauth/callback"

    def test_no_refresh_still_registers_when_uncached(
        self, db_tmp, fake_http, monkeypatch
    ):
        """allow_refresh=False only skips refresh, it is not a hard no-op."""
        monkeypatch.setattr(settings, "APP_WEBSITE", "https://echo.example.org")
        result = oauth.get_or_create_app(
            "https://mastodon.social", allow_refresh=False
        )

        assert len(fake_http.calls) == 1
        assert result["client_id"] == "cid-1"
