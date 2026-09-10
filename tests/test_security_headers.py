"""S8 + S9: security headers and CSRF origin check."""

import pathlib
import tempfile

import pytest
from fastapi.testclient import TestClient

import app as app_module
import database
import settings


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
    assert h.get("Referrer-Policy") == "strict-origin-when-cross-origin"
    assert "camera=()" in h.get("Permissions-Policy", "")
    csp = h.get("Content-Security-Policy", "")
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp
    assert "base-uri 'self'" in csp
    assert "form-action 'self'" in csp


def test_csp_stays_strict_when_billing_disabled(client, monkeypatch):
    # Self-hosted: no billing seam is mounted, so no form submission beyond
    # the Mastodon connect form ever needs to leave the origin. Default
    # pages keep the strict directive — pinned as an exact token set, not a
    # substring: "form-action 'self'" is a prefix of the wide
    # "form-action 'self' https:" and a bare `in` passes vacuously (gate
    # finding, 2026-09-10).
    monkeypatch.setattr(settings, "BILLING_ENABLED", False)
    csp = client.get("/healthz").headers.get("Content-Security-Policy", "")
    directives = {p.strip() for p in csp.split(";") if p.strip()}
    assert "form-action 'self'" in directives
    assert "form-action 'self' https:" not in directives
    assert "stripe.com" not in csp


def test_csp_form_action_wide_only_on_connect_form_pages(client):
    # /accounts renders the connect form whose redirect chain leaves the
    # origin, so its document needs form-action 'self' https:. Credential
    # pages (login, register, admin) do NOT — pin the difference: the same
    # directive on both would mean the scoping silently regressed.
    def form_action_of(path):
        csp = client.get(path).headers.get("Content-Security-Policy", "")
        return next(
            p.strip() for p in csp.split(";") if p.strip().startswith("form-action")
        )

    assert form_action_of("/accounts") == "form-action 'self' https:"
    assert form_action_of("/login") == "form-action 'self'"


def test_csp_wide_pages_include_stripe_when_billing_enabled(client, monkeypatch):
    monkeypatch.setattr(settings, "BILLING_ENABLED", True)
    csp = client.get("/accounts").headers.get("Content-Security-Policy", "")
    assert (
        "form-action 'self' https: https://checkout.stripe.com https://billing.stripe.com"
        in csp
    )


def test_csp_allows_stripe_redirects_when_billing_enabled(client, monkeypatch):
    # Hosted billing: the register and Subscribe forms POST to
    # /api/billing/checkout (and portal), which 303-redirect to
    # checkout.stripe.com / billing.stripe.com. The browser enforces
    # form-action on EVERY hop of a form submission's redirect chain, so
    # both origins must be listed or the redirect is silently killed and
    # the customer never reaches the payment page (the v1.42.0 regression,
    # fixed 2026-09-10). Pinned as a full directive: an exact-match here is
    # what a future CSP edit must consciously update. Non-connect pages
    # keep Stripe WITHOUT the wide scheme: 'self' https: would make the
    # pinned assertion vacuous.
    monkeypatch.setattr(settings, "BILLING_ENABLED", True)
    csp = client.get("/healthz").headers.get("Content-Security-Policy", "")
    assert (
        "form-action 'self' https://checkout.stripe.com https://billing.stripe.com"
        in csp
    )


def test_csp_form_action_allows_https_redirect_hops(client):
    # The accounts-page connect form is a form submission (method="get" is
    # still a form submission for CSP) whose redirect chain lands on the
    # user-chosen Mastodon instance's /oauth/authorize. form-action is
    # enforced on EVERY hop, and the instance origin is unbounded, so the
    # only honest allowance is scheme-level: 'self' plus https:. Without
    # it, connecting any instance was blocked client-side with only a
    # console error (reported by a beta user 2026-09-10, same defect class
    # as the Stripe form-action regression).
    csp = client.get("/accounts").headers.get("Content-Security-Policy", "")
    assert "form-action 'self' https:" in csp


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
    # JSON request: branded detail body, not a bare empty 403 (error-handling
    # audit finding 1.4).
    assert r.json() == {"detail": "Cross-origin request rejected"}


def test_csrf_rejects_null_origin(client):
    # A bare "Origin: null" with no same-origin evidence and no Referer fails
    # closed (this is what a sandboxed/opaque context forger sends).
    r = client.post(
        "/api/settings/smtp",
        headers={"Origin": "null", "Host": "testserver"},
        data={"smtp_host": "smtp.example.com", "smtp_port": "587"},
    )
    assert r.status_code == 403


def test_csrf_allows_null_origin_with_same_origin_fetch_metadata(client):
    # Browsers send "Origin: null" on a same-origin form POST when a referrer
    # policy hides the referrer (Firefox/Chromium, WHATWG Fetch). Fetch
    # Metadata (Sec-Fetch-Site: same-origin) proves it is genuinely same-origin.
    r = client.post(
        "/api/settings/smtp",
        headers={"Origin": "null", "Sec-Fetch-Site": "same-origin", "Host": "testserver"},
        data={"smtp_host": "smtp.example.com", "smtp_port": "587"},
    )
    assert r.status_code != 403


def test_csrf_rejects_null_origin_with_cross_site_fetch_metadata(client):
    # A null Origin from a cross-site context is still rejected.
    r = client.post(
        "/api/settings/smtp",
        headers={"Origin": "null", "Sec-Fetch-Site": "cross-site", "Host": "testserver"},
        data={"smtp_host": "smtp.example.com", "smtp_port": "587"},
    )
    assert r.status_code == 403


def test_csrf_allows_null_origin_with_matching_referer(client):
    # No Fetch Metadata (older client) but a same-origin Referer still passes.
    r = client.post(
        "/api/settings/smtp",
        headers={"Origin": "null", "Referer": "http://testserver/login", "Host": "testserver"},
        data={"smtp_host": "smtp.example.com", "smtp_port": "587"},
    )
    assert r.status_code != 403


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