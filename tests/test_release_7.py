"""Regression tests for the Release 7 cleanup batch (S10b, S10d, D3, A1-A4)."""

import pathlib

import pytest
from fastapi.testclient import TestClient

import app as app_module
import database
import settings


@pytest.fixture
def multi_client(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MULTI", True)
    monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "multi.db")
    database.init_db()
    app_module.auth._login_attempts.clear()
    app_module.auth._register_attempts.clear()
    app_module._preview_throttle_attempts.clear()
    with TestClient(app_module.app) as c:
        yield c


def test_preview_throttle_returns_429(multi_client, monkeypatch):
    monkeypatch.setattr(app_module, "_PREVIEW_THROTTLE_LIMIT", 3)
    monkeypatch.setattr(app_module, "_PREVIEW_THROTTLE_WINDOW", 600)
    monkeypatch.setattr(app_module, "_preview_throttle_attempts", {})
    # Register so the session cookie is set — preview requires auth.
    multi_client.post(
        "/register",
        data={"email": "p@example.com", "password": "hunter2hunter2", "confirm": "hunter2hunter2"},
        follow_redirects=False,
    )
    for _ in range(3):
        r = multi_client.post("/api/preview", data={"template": "{{ title }}", "feed_id": "1"})
        assert r.status_code != 429, "expected allowed before throttle limit"
    r = multi_client.post("/api/preview", data={"template": "{{ title }}", "feed_id": "1"})
    assert r.status_code == 429


def test_no_google_fonts_in_base_html():
    base = (pathlib.Path(__file__).resolve().parent.parent / "templates" / "base.html").read_text(
        encoding="utf-8"
    )
    assert "fonts.googleapis.com" not in base
    assert "fonts.gstatic.com" not in base


def test_self_hosted_font_file_exists():
    font = (
        pathlib.Path(__file__).resolve().parent.parent
        / "static" / "fonts" / "inter-latin.woff2"
    )
    assert font.is_file(), f"Expected font file at {font}"


def test_csp_no_longer_references_google_fonts():
    csp = app_module._CSP_HEADER
    assert "fonts.googleapis.com" not in csp
    assert "fonts.gstatic.com" not in csp