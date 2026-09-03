"""S8 + S9: security headers and CSRF origin check."""

import pathlib
import tempfile

import pytest
from fastapi.testclient import TestClient

import app as app_module
import database


@pytest.fixture()
def client(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setattr(database, "DB_PATH", str(pathlib.Path(tmp) / "test.db"))
    # Single mode is the default (FEEDECHO_MODE unset); AUTH_TOKEN unset makes
    # AuthMiddleware a no-op, so the routes under test are reachable.
    with TestClient(app_module.app) as c:
        yield c


def test_security_headers_present_on_html(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    h = r.headers
    assert h.get("X-Content-Type-Options") == "nosniff"
    assert h.get("X-Frame-Options") == "DENY"
    assert h.get("Referrer-Policy") == "no-referrer"
    assert "camera=()" in h.get("Permissions-Policy", "")
    csp = h.get("Content-Security-Policy", "")
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp
    assert "base-uri 'self'" in csp
    assert "form-action 'self'" in csp


def test_hsts_emitted_unconditionally(client):
    # HSTS is emitted even over plain HTTP: browsers ignore the header there
    # (RFC 6797), and gating on scheme would silently drop it behind a
    # TLS-terminating reverse proxy. Caddy sets the hosted service's own HSTS.
    r = client.get("/healthz")
    assert r.headers.get("Strict-Transport-Security") == "max-age=31536000"


def test_csrf_rejects_cross_origin_post(client):
    # A forged POST carrying an attacker Origin is rejected before auth.
    r = client.post(
        "/api/settings/smtp",
        headers={"Origin": "https://evil.com", "Host": "testserver"},
        data={"smtp_host": "smtp.example.com", "smtp_port": "587"},
    )
    assert r.status_code == 403


def test_csrf_rejects_null_origin(client):
    r = client.post(
        "/api/settings/smtp",
        headers={"Origin": "null", "Host": "testserver"},
        data={"smtp_host": "smtp.example.com", "smtp_port": "587"},
    )
    assert r.status_code == 403


def test_csrf_allows_same_origin_post(client):
    # Same-origin POST passes the CSRF check (auth/validation decides next).
    r = client.post(
        "/api/settings/smtp",
        headers={"Origin": "http://testserver", "Host": "testserver"},
        data={"smtp_host": "smtp.example.com", "smtp_port": "587"},
    )
    assert r.status_code != 403


def test_csrf_allows_no_origin_no_referer(client):
    # Non-browser client (curl, script, webhook) sends neither header.
    r = client.post(
        "/api/settings/smtp",
        data={"smtp_host": "smtp.example.com", "smtp_port": "587"},
    )
    assert r.status_code != 403


def test_csrf_rejects_cross_origin_referer_fallback(client):
    # No Origin but a cross-site Referer must also be rejected.
    r = client.post(
        "/api/settings/smtp",
        headers={"Referer": "https://evil.com/login", "Host": "testserver"},
        data={"smtp_host": "smtp.example.com", "smtp_port": "587"},
    )
    assert r.status_code == 403


def test_csrf_get_not_subject_to_origin_check(client):
    # GET is safe: an Origin header (even cross-site) must not 403 a GET.
    r = client.get("/healthz", headers={"Origin": "https://evil.com"})
    assert r.status_code == 200