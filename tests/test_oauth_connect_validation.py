"""OAuth connect flow validation and CSP form-action coverage.

Regression tests for the bug Alex Pardoe reported on 2026-09-10:

1. The accounts-page connect form is <form method="get" action="/oauth/connect">,
   and the route 302s to the user-chosen instance's /oauth/authorize. Browsers
   enforce CSP form-action on EVERY hop of a form submission's redirect chain,
   so the document rendering the form needs form-action 'self' https: — a
   fixed-origin whitelist can never cover user-chosen Mastodon instances
   (social.lol was blocked client-side with only a console error). The wide
   directive is scoped to the connect-form pages (/accounts, /oauth/connect);
   other pages keep strict 'self'.

2. Entering the instance without a scheme ("social.lol") raised a 400
   HTTPException that no HTML handler covers, so the browser showed raw JSON
   after a normal form submission. The route now defaults scheme-less input to
   https://, reduces the input to its origin, enforces https-only, and renders
   the accounts page with a friendly banner when the URL fails validation
   (including malformed URLs that previously escaped as unhandled 500s).
"""

import pathlib
import tempfile

import pytest
from fastapi.testclient import TestClient

import app as app_module
import database
import oauth as oauth_module


@pytest.fixture()
def client(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setattr(database, "DB_PATH", str(pathlib.Path(tmp) / "test.db"))
    # Single mode is the default (FEEDECHO_MODE unset); AUTH_TOKEN unset makes
    # AuthMiddleware a no-op, so the routes under test are reachable.
    with TestClient(app_module.app) as c:
        yield c


@pytest.fixture()
def fake_instance_app(monkeypatch):
    """Keep get_authorize_url real (it builds the actual redirect URL) but
    stub the instance app registration so no network call happens."""
    monkeypatch.setattr(
        oauth_module,
        "get_or_create_app",
        lambda instance, allow_refresh=True: {
            "client_id": "test-client-id",
            "client_secret": "test-client-secret",
        },
    )


class TestConnectInstanceNormalization:
    def test_bare_domain_defaults_to_https(self, client, fake_instance_app):
        r = client.get(
            "/oauth/connect",
            params={"instance": "social.lol"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert r.headers["location"].startswith(
            "https://social.lol/oauth/authorize?"
        )

    def test_whitespace_and_trailing_slash_are_tolerated(
        self, client, fake_instance_app
    ):
        r = client.get(
            "/oauth/connect",
            params={"instance": "  social.lol/  "},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert r.headers["location"].startswith(
            "https://social.lol/oauth/authorize?"
        )

    def test_explicit_scheme_is_preserved(self, client, fake_instance_app):
        r = client.get(
            "/oauth/connect",
            params={"instance": "https://social.lol"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert r.headers["location"].startswith(
            "https://social.lol/oauth/authorize?"
        )

    def test_path_and_query_are_stripped_to_origin(self, client, fake_instance_app):
        # social.lol/web?utm_source=x must not corrupt the authorize URL
        # (https://social.lol?utm_source=x/oauth/authorize) or the oauth_apps
        # cache key. (Review-gate finding, 2026-09-10.)
        r = client.get(
            "/oauth/connect",
            params={"instance": "https://social.lol/web?utm_source=x"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert r.headers["location"].startswith(
            "https://social.lol/oauth/authorize?"
        )


class TestConnectValidationErrors:
    def test_unusable_instance_renders_banner_not_raw_json(
        self, client, fake_instance_app
    ):
        # A URL that still fails validation after normalization (no host here)
        # used to raise a bare 400 HTTPException; browsers showed raw JSON.
        r = client.get(
            "/oauth/connect", params={"instance": ":bad"}, follow_redirects=False
        )
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "isn&#39;t a usable Mastodon instance" in r.text
        # The accounts page rendered (its connect form is present), so the
        # user can correct the input without hitting back.
        assert "/oauth/connect" in r.text

    def test_malformed_ipv6_bracket_renders_banner_not_500(
        self, client, fake_instance_app
    ):
        # urllib.parse.urlsplit raises ValueError("Invalid IPv6 URL") on
        # unclosed brackets; that used to escape as an unhandled 500.
        r = client.get(
            "/oauth/connect",
            params={"instance": "https://[::1"},
            follow_redirects=False,
        )
        assert r.status_code == 200
        assert "isn&#39;t a usable Mastodon instance" in r.text

    def test_ssrf_guarded_host_renders_banner(self, client, fake_instance_app):
        r = client.get(
            "/oauth/connect",
            params={"instance": "http://127.0.0.1:8453"},
            follow_redirects=False,
        )
        assert r.status_code == 200
        assert "isn&#39;t a usable Mastodon instance" in r.text

    def test_plain_http_instance_is_rejected_with_banner(
        self, client, fake_instance_app, monkeypatch
    ):
        # http:// would sail through validate_url and then be killed by the
        # page's own CSP form-action ('self' https:) a hop later — reject it
        # up front with a readable message. (RFC 6749 3.1.3: TLS required.)
        # Bypass only the DNS-based SSRF half (public DNS is not a test
        # dependency); the http rejection under test happens after it.
        monkeypatch.setattr(
            app_module, "validate_outbound_url", lambda url: url
        )
        r = client.get(
            "/oauth/connect",
            params={"instance": "http://insecure.example.com"},
            follow_redirects=False,
        )
        assert r.status_code == 200
        assert "must be reachable over https" in r.text

    def test_userinfo_password_is_not_echoed_in_banner(
        self, client, fake_instance_app
    ):
        # Embedded credentials are an SSRF-rejection; the banner must not
        # print the password back into the page. (Review-gate finding.)
        r = client.get(
            "/oauth/connect",
            params={"instance": "https://user:sekrit@127.0.0.1"},
            follow_redirects=False,
        )
        assert r.status_code == 200
        assert "sekrit" not in r.text

    def test_missing_instance_still_400(self, client):
        # Direct URL hit with no instance parameter: the form's `required`
        # attribute guards the browser path, and API callers get JSON.
        r = client.get("/oauth/connect", follow_redirects=False)
        assert r.status_code == 400


class TestConnectFormPageCSP:
    def test_accounts_page_carries_wide_form_action(self, client):
        csp = client.get("/accounts").headers.get("Content-Security-Policy", "")
        assert "form-action 'self' https:" in csp

    def test_connect_error_page_carries_wide_form_action(
        self, client, fake_instance_app
    ):
        # The error banner renders from /oauth/connect itself; its document
        # hosts the same form, so it needs the same directive.
        r = client.get(
            "/oauth/connect", params={"instance": ":bad"}, follow_redirects=False
        )
        csp = r.headers.get("Content-Security-Policy", "")
        assert "form-action 'self' https:" in csp

    def test_healthz_keeps_strict_form_action(self, client):
        csp = client.get("/healthz").headers.get("Content-Security-Policy", "")
        directives = {p.strip() for p in csp.split(";") if p.strip()}
        assert "form-action 'self'" in directives
        assert "form-action 'self' https:" not in directives
