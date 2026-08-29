"""Tests for the settings module and FEEDECHO_MODE flag."""

import os
import importlib

import settings


def _reload_settings():
    return importlib.reload(settings)


def _clean_env(monkeypatch):
    # Both spellings: an ambient legacy FEEDCHO_* variable is still honoured
    # by settings.env() (issue #15), so leaving one set would defeat every
    # "unset means default" assertion below.
    for key in list(os.environ):
        if key.startswith(("FEEDECHO_", "FEEDCHO_")):
            monkeypatch.delenv(key)


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

    def test_single_mode_never_raises(self, monkeypatch):
        monkeypatch.setattr(settings, "MULTI", False)
        settings.validate_config()  # must not raise

    def test_multi_without_url_raises(self, monkeypatch):
        self._set_multi(monkeypatch, secret="x" * 40)
        import pytest

        with pytest.raises(RuntimeError, match="FEEDECHO_DATABASE_URL"):
            settings.validate_config()

    def test_multi_without_url_allowed_via_fallback_flag(self, monkeypatch):
        self._set_multi(monkeypatch, fallback=True, secret="x" * 40)
        settings.validate_config()  # must not raise

    def test_multi_without_session_secret_raises(self, monkeypatch):
        self._set_multi(monkeypatch, url="postgresql://x/x")
        import pytest

        with pytest.raises(RuntimeError, match="FEEDECHO_SESSION_SECRET"):
            settings.validate_config()

    def test_multi_with_short_session_secret_raises(self, monkeypatch):
        self._set_multi(monkeypatch, url="postgresql://x/x", secret="short")
        import pytest

        with pytest.raises(RuntimeError, match="at least 32"):
            settings.validate_config()

    def test_multi_fully_configured_passes(self, monkeypatch):
        self._set_multi(monkeypatch, url="postgresql://x/x", secret="s" * 32)
        settings.validate_config()  # must not raise
