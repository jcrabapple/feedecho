"""Tests for the settings module and FEEDECHO_MODE flag."""

import os
import importlib

import pytest
from cryptography.fernet import Fernet

import settings

_VALID_KEY = Fernet.generate_key().decode()


def _reload_settings():
    return importlib.reload(settings)


def _clean_env(monkeypatch):
    # Both spellings: an ambient legacy FEEDCHO_* variable is still honoured
    # by settings.env() (issue #15), so leaving one set would defeat every
    # "unset means default" assertion below.
    for key in list(os.environ):
        if key.startswith(("FEEDECHO_", "FEEDCHO_")):
            monkeypatch.delenv(key)
    # One-click host URLs feed the BASE_URL default, so a test run on a
    # Render/Railway box must not inherit them either.
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN", raising=False)


class TestModeFlag:
    def test_defaults_to_single_mode(self, monkeypatch):
        _clean_env(monkeypatch)
        s = _reload_settings()
        assert s.MODE == "single"
        assert s.MULTI is False

    def test_multi_mode_flag(self, monkeypatch):
        monkeypatch.setenv("FEEDECHO_MODE", "multi")
        s = _reload_settings()
        assert s.MODE == "multi"
        assert s.MULTI is True

    def test_invalid_mode_raises(self, monkeypatch):
        monkeypatch.setenv("FEEDECHO_MODE", "bogus")
        import pytest

        with pytest.raises(ValueError):
            _reload_settings()

    def test_defaults_are_sane(self, monkeypatch):
        _clean_env(monkeypatch)
        s = _reload_settings()
        assert s.AUTH_TOKEN is None
        assert s.DATABASE_URL == ""
        assert s.CALLBACK_URL == "https://feedecho.example.com/oauth/callback"
        assert s.DB_PATH.name == "feedecho.db"


class TestEnvPassthrough:
    def test_db_path_env(self, monkeypatch):
        monkeypatch.setenv("FEEDECHO_DB_PATH", "/tmp/custom-feedecho.db")
        s = _reload_settings()
        assert str(s.DB_PATH) == "/tmp/custom-feedecho.db"

    def test_auth_token_env(self, monkeypatch):
        monkeypatch.setenv("FEEDECHO_AUTH_TOKEN", "sekret")
        s = _reload_settings()
        assert s.AUTH_TOKEN == "sekret"

    def test_backdated_defaults_off(self, monkeypatch):
        _clean_env(monkeypatch)
        s = _reload_settings()
        assert s.ALLOW_BACKDATED_ENTRIES is False
        assert s.MAX_BACKDATED_ENTRY_DAYS == 3

    def test_backdated_enabled(self, monkeypatch):
        _clean_env(monkeypatch)
        monkeypatch.setenv("FEEDECHO_ALLOW_BACKDATED_ENTRIES", "1")
        s = _reload_settings()
        assert s.ALLOW_BACKDATED_ENTRIES is True

    def test_backdated_custom_days(self, monkeypatch):
        _clean_env(monkeypatch)
        monkeypatch.setenv("FEEDECHO_MAX_BACKDATED_ENTRY_DAYS", "7")
        s = _reload_settings()
        assert s.MAX_BACKDATED_ENTRY_DAYS == 7

    def test_fallback_proxy_settings(self, monkeypatch):
        _clean_env(monkeypatch)
        monkeypatch.setenv("FEEDECHO_FALLBACK_PROXY_URL", "https://proxy.example.com")
        monkeypatch.setenv("FEEDECHO_FALLBACK_PROXY_SECRET", "sekret")
        s = _reload_settings()
        assert s.FALLBACK_PROXY_URL == "https://proxy.example.com"
        assert s.FALLBACK_PROXY_SECRET == "sekret"


class TestPlatformBaseUrl:
    """One-click hosts (render.yaml, the Railway template) publish the
    service's public URL in their own variables; single mode uses it when
    FEEDECHO_BASE_URL is unset."""

    @pytest.fixture(autouse=True)
    def _restore_settings(self, monkeypatch):
        # These tests reload the settings module with platform env set;
        # restore the env first, then reload, so later tests do not inherit
        # a forced Secure cookie or a platform BASE_URL.
        yield
        monkeypatch.undo()
        _reload_settings()

    def test_no_platform_no_base_url(self, monkeypatch):
        _clean_env(monkeypatch)
        s = _reload_settings()
        assert s.BASE_URL == ""
        assert s.PLATFORM_BASE_URL == ""
        assert s.FORCE_SECURE_COOKIE is False

    def test_render_external_url(self, monkeypatch):
        _clean_env(monkeypatch)
        monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://fe-abc.onrender.com")
        s = _reload_settings()
        assert s.BASE_URL == "https://fe-abc.onrender.com"
        assert s.CALLBACK_URL == "https://fe-abc.onrender.com/oauth/callback"
        assert s.APP_WEBSITE == "https://fe-abc.onrender.com"
        # Render terminates TLS; the app sees http, so the cookie flag
        # has to be forced or the login cookie ships without Secure.
        assert s.FORCE_SECURE_COOKIE is True

    def test_railway_public_domain_gets_https(self, monkeypatch):
        _clean_env(monkeypatch)
        monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "fe-production.up.railway.app")
        s = _reload_settings()
        assert s.BASE_URL == "https://fe-production.up.railway.app"
        assert s.CALLBACK_URL == "https://fe-production.up.railway.app/oauth/callback"
        assert s.FORCE_SECURE_COOKIE is True

    def test_render_wins_over_railway(self, monkeypatch):
        _clean_env(monkeypatch)
        monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://r.onrender.com")
        monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "x.up.railway.app")
        s = _reload_settings()
        assert s.BASE_URL == "https://r.onrender.com"

    def test_explicit_base_url_wins(self, monkeypatch):
        # A custom domain in front of the platform service.
        _clean_env(monkeypatch)
        monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://fe.onrender.com")
        monkeypatch.setenv("FEEDECHO_BASE_URL", "https://feeds.example.org")
        s = _reload_settings()
        assert s.BASE_URL == "https://feeds.example.org"
        assert s.CALLBACK_URL == "https://feeds.example.org/oauth/callback"

    def test_explicit_callback_url_wins(self, monkeypatch):
        _clean_env(monkeypatch)
        monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "fe.up.railway.app")
        monkeypatch.setenv(
            "FEEDECHO_CALLBACK_URL", "https://other.example.org/oauth/callback"
        )
        s = _reload_settings()
        assert s.CALLBACK_URL == "https://other.example.org/oauth/callback"

    def test_blank_platform_values_ignored(self, monkeypatch):
        _clean_env(monkeypatch)
        monkeypatch.setenv("RENDER_EXTERNAL_URL", "  ")
        monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "")
        s = _reload_settings()
        assert s.BASE_URL == ""
        assert s.FORCE_SECURE_COOKIE is False

    def test_multi_mode_ignores_platform_url(self, monkeypatch):
        _clean_env(monkeypatch)
        monkeypatch.setenv("FEEDECHO_MODE", "multi")
        monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://fe.onrender.com")
        s = _reload_settings()
        assert s.BASE_URL == ""
        assert s.PLATFORM_BASE_URL == ""
        assert s.FORCE_SECURE_COOKIE is False

    def test_force_secure_cookie_explicit_off(self, monkeypatch):
        _clean_env(monkeypatch)
        monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://fe.onrender.com")
        monkeypatch.setenv("FEEDECHO_FORCE_SECURE_COOKIE", "0")
        s = _reload_settings()
        assert s.FORCE_SECURE_COOKIE is False

    def test_force_secure_cookie_explicit_on_without_platform(self, monkeypatch):
        _clean_env(monkeypatch)
        monkeypatch.setenv("FEEDECHO_FORCE_SECURE_COOKIE", "1")
        s = _reload_settings()
        assert s.FORCE_SECURE_COOKIE is True


class TestValidateConfig:
    def _set_multi(self, monkeypatch, **kwargs):
        monkeypatch.setattr(settings, "MULTI", True)
        monkeypatch.setattr(settings, "DATABASE_URL", kwargs.get("url", ""))
        monkeypatch.setattr(
            settings, "ALLOW_SQLITE_FALLBACK", kwargs.get("fallback", False)
        )
        monkeypatch.setattr(
            settings, "SESSION_SECRET", kwargs.get("secret", "")
        )
        # Defaults to a valid value: tests targeting a later check (e.g.
        # CREDENTIAL_KEY) shouldn't also have to think about this one.
        # Tests that specifically target the STATE_SECRET check pass
        # state="" (or a short value) explicitly.
        monkeypatch.setattr(
            settings, "STATE_SECRET", kwargs.get("state", "s" * 32)
        )
        monkeypatch.setattr(
            settings, "CREDENTIAL_KEY", kwargs.get("key", "")
        )

    def test_single_mode_without_token_raises(self, monkeypatch):
        # The startup auth gate (v1.69.0): a bare `docker run` used to boot
        # fully unauthenticated on 0.0.0.0. Now it fails closed.
        import pytest

        monkeypatch.setattr(settings, "MULTI", False)
        monkeypatch.setattr(settings, "AUTH_TOKEN", None)
        monkeypatch.setattr(settings, "ALLOW_INSECURE", False)
        with pytest.raises(RuntimeError, match="FEEDECHO_AUTH_TOKEN"):
            settings.validate_config()

    def test_single_mode_with_token_passes(self, monkeypatch):
        monkeypatch.setattr(settings, "MULTI", False)
        monkeypatch.setattr(settings, "AUTH_TOKEN", "tok")
        monkeypatch.setattr(settings, "ALLOW_INSECURE", False)
        settings.validate_config()  # must not raise

    def test_single_mode_insecure_opt_out_passes_and_warns(
        self, monkeypatch, caplog
    ):
        monkeypatch.setattr(settings, "MULTI", False)
        monkeypatch.setattr(settings, "AUTH_TOKEN", None)
        monkeypatch.setattr(settings, "ALLOW_INSECURE", True)
        with caplog.at_level("WARNING", logger="feedecho"):
            settings.validate_config()  # must not raise
        assert any(
            "authentication is DISABLED" in r.message for r in caplog.records
        )

    def test_lifespan_gate_refuses_boot_end_to_end(self, monkeypatch):
        # SEC-02: prove the gate is wired into the ASGI lifespan, not just
        # the validate_config unit — a TestClient boot must raise.
        import pytest
        from fastapi.testclient import TestClient

        import app as app_module

        monkeypatch.setattr(settings, "MULTI", False)
        monkeypatch.setattr(settings, "AUTH_TOKEN", None)
        monkeypatch.setattr(settings, "ALLOW_INSECURE", False)
        with pytest.raises(RuntimeError, match="FEEDECHO_AUTH_TOKEN"):
            with TestClient(app_module.app):
                pass

    def test_middleware_fail_closed_without_lifespan(self, monkeypatch):
        # SEC-01: with lifespan events skipped (no validate_config), a
        # no-token + no-flag request must 500, not sail through as operator.
        from fastapi.testclient import TestClient

        import app as app_module

        monkeypatch.setattr(settings, "MULTI", False)
        monkeypatch.setattr(settings, "AUTH_TOKEN", None)
        monkeypatch.setattr(settings, "ALLOW_INSECURE", False)
        c = TestClient(app_module.app)  # no `with` = no lifespan run
        r = c.get("/")
        assert r.status_code == 500
        assert "FEEDECHO_AUTH_TOKEN" in r.text

    def test_single_mode_invalid_credential_key_raises(self, monkeypatch):
        # The Fernet format check runs in both modes (hoisted out of the
        # multi branch in v1.69.0, when single mode gained optional
        # credential encryption).
        import pytest

        monkeypatch.setattr(settings, "MULTI", False)
        monkeypatch.setattr(settings, "AUTH_TOKEN", "tok")
        monkeypatch.setattr(settings, "ALLOW_INSECURE", False)
        monkeypatch.setattr(settings, "CREDENTIAL_KEY", "not-a-valid-key")
        with pytest.raises(RuntimeError, match="not a valid Fernet key"):
            settings.validate_config()

    def test_multi_without_url_raises(self, monkeypatch):
        self._set_multi(monkeypatch, secret="x" * 40, key=_VALID_KEY)
        import pytest

        with pytest.raises(RuntimeError, match="FEEDECHO_DATABASE_URL"):
            settings.validate_config()

    def test_multi_without_url_allowed_via_fallback_flag(self, monkeypatch):
        self._set_multi(monkeypatch, fallback=True, secret="x" * 40)
        settings.validate_config()  # must not raise

    def test_multi_without_session_secret_raises(self, monkeypatch):
        self._set_multi(monkeypatch, url="postgresql://x/x", key=_VALID_KEY)
        import pytest

        with pytest.raises(RuntimeError, match="FEEDECHO_SESSION_SECRET"):
            settings.validate_config()

    def test_multi_with_short_session_secret_raises(self, monkeypatch):
        self._set_multi(
            monkeypatch, url="postgresql://x/x", secret="short", key=_VALID_KEY
        )
        import pytest

        with pytest.raises(RuntimeError, match="at least 32"):
            settings.validate_config()

    def test_multi_without_state_secret_raises(self, monkeypatch):
        self._set_multi(
            monkeypatch, url="postgresql://x/x", secret="s" * 32, state="",
            key=_VALID_KEY,
        )
        import pytest

        with pytest.raises(RuntimeError, match="FEEDECHO_STATE_SECRET"):
            settings.validate_config()

    def test_multi_with_short_state_secret_raises(self, monkeypatch):
        self._set_multi(
            monkeypatch, url="postgresql://x/x", secret="s" * 32,
            state="short", key=_VALID_KEY,
        )
        import pytest

        with pytest.raises(RuntimeError, match="at least 32"):
            settings.validate_config()

    def test_multi_fully_configured_passes(self, monkeypatch):
        self._set_multi(
            monkeypatch, url="postgresql://x/x", secret="s" * 32, key=_VALID_KEY
        )
        settings.validate_config()  # must not raise

    def test_multi_without_credential_key_raises(self, monkeypatch):
        # S3 (credential encryption) is silently undone when a real multi
        # deployment boots without a key — fail closed instead (Opus D1.5).
        self._set_multi(monkeypatch, url="postgresql://x/x", secret="s" * 32)
        import pytest

        with pytest.raises(RuntimeError, match="FEEDECHO_CREDENTIAL_KEY"):
            settings.validate_config()

    def test_multi_without_credential_key_allowed_via_fallback(self, monkeypatch):
        # Local development (sqlite fallback) keeps the old warn-not-raise
        # behaviour, mirroring the DATABASE_URL carve-out.
        self._set_multi(monkeypatch, fallback=True, secret="s" * 32)
        settings.validate_config()  # must not raise
