"""Tests for the settings module and FEEDCHO_MODE flag."""

import os
import importlib

import settings


def _reload_settings():
    return importlib.reload(settings)


def _clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("FEEDCHO_"):
            monkeypatch.delenv(key)


class TestModeFlag:
    def test_defaults_to_single_mode(self, monkeypatch):
        _clean_env(monkeypatch)
        s = _reload_settings()
        assert s.MODE == "single"
        assert s.MULTI is False

    def test_multi_mode_flag(self, monkeypatch):
        monkeypatch.setenv("FEEDCHO_MODE", "multi")
        s = _reload_settings()
        assert s.MODE == "multi"
        assert s.MULTI is True

    def test_invalid_mode_raises(self, monkeypatch):
        monkeypatch.setenv("FEEDCHO_MODE", "bogus")
        import pytest

        with pytest.raises(ValueError):
            _reload_settings()

    def test_defaults_are_sane(self, monkeypatch):
        _clean_env(monkeypatch)
        s = _reload_settings()
        assert s.AUTH_TOKEN == ""
        assert s.DATABASE_URL == ""
        assert s.CALLBACK_URL == ""
        assert s.DB_PATH.name == "feedecho.db"


class TestEnvPassthrough:
    def test_db_path_env(self, monkeypatch):
        monkeypatch.setenv("FEEDCHO_DB_PATH", "/tmp/custom-feedecho.db")
        s = _reload_settings()
        assert str(s.DB_PATH) == "/tmp/custom-feedecho.db"

    def test_auth_token_env(self, monkeypatch):
        monkeypatch.setenv("FEEDCHO_AUTH_TOKEN", "sekret")
        s = _reload_settings()
        assert s.AUTH_TOKEN == "sekret"
