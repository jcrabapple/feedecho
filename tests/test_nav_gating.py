"""Chrome gating: an anonymous viewer only gets links they can actually use.

The nav used to render Dashboard/Feeds/Accounts/Echoes/History/Settings/How To
to everyone, so every one of them bounced a logged-out visitor straight back to
/login. The nav now keys off ``request.state.authed``, which the middleware also
records on the public pages (/login, /register, /about, /verify-email) so a
signed-in user does not get anonymous chrome there — and an authenticated GET of
/login or /register redirects into the app instead of showing a dead form.
"""

import re

import pytest
from fastapi.testclient import TestClient

import app as app_module
import auth
import database
import security
import settings
from _version import __version__ as VERSION
from app import app

APP_LINKS = ('href="/feeds"', 'href="/reader"', 'href="/accounts"', 'href="/echoes"',
             'href="/history"', 'href="/settings"', 'href="/howto"')


def _nav(page: str) -> str:
    m = re.search(r"<nav.*?</nav>", page, re.S)
    return m.group(0) if m else ""


def _nav_links(page: str) -> str:
    """Only the links div. The brand anchor also carries href="/login" for
    anonymous viewers, so a whole-nav assertion cannot tell the funnel link
    from the logo. The theme toggle and account area now sit after the div
    (mobile two-row header), so the match must stop at the div's own close,
    not at </nav>."""
    m = re.search(r'<div class="nav-links">.*?</div>', page, re.S)
    return m.group(0) if m else ""


# ── single mode ──────────────────────────────────────────────────────────────

@pytest.fixture
def single_env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MULTI", False)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "nav-single.db")
    database.init_db()
    return settings


class TestSingleMode:
    def test_operator_gets_the_full_nav(self, single_env):
        with TestClient(app) as c:
            nav = _nav(c.get("/").text)
        for link in APP_LINKS:
            assert link in nav, link
        assert 'href="/" class="nav-brand"' in nav

    def test_login_page_offers_no_app_links(self, single_env, monkeypatch):
        monkeypatch.setattr(settings, "AUTH_TOKEN", "sekret")
        with TestClient(app) as c:
            page = c.get("/login").text
        nav = _nav(page)
        for link in APP_LINKS:
            assert link not in nav, link
        # Single mode has no landing page: the brand points at / (the app).
        assert 'href="/" class="nav-brand"' in nav
        # The form is still there, and the theme toggle still works logged out.
        assert "Access token" in page
        assert "theme-toggle" in nav

    def test_full_nav_returns_with_the_token(self, single_env, monkeypatch):
        monkeypatch.setattr(settings, "AUTH_TOKEN", "sekret")
        with TestClient(app) as c:
            c.cookies.set(auth.AUTH_COOKIE_NAME, "sekret")
            nav = _nav(c.get("/").text)
        for link in APP_LINKS:
            assert link in nav, link

    def test_authenticated_login_get_redirects_into_the_app(self, single_env, monkeypatch):
        monkeypatch.setattr(settings, "AUTH_TOKEN", "sekret")
        with TestClient(app) as c:
            c.cookies.set(auth.AUTH_COOKIE_NAME, "sekret")
            resp = c.get("/login", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/"

    def test_login_get_redirects_when_no_token_is_configured(self, single_env):
        # Nothing to log into: POST /login already behaved this way.
        with TestClient(app) as c:
            resp = c.get("/login", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/"

    def test_wrong_token_still_gets_the_anonymous_login_page(self, single_env, monkeypatch):
        monkeypatch.setattr(settings, "AUTH_TOKEN", "sekret")
        with TestClient(app) as c:
            c.cookies.set(auth.AUTH_COOKIE_NAME, "nope")
            resp = c.get("/login")
        assert resp.status_code == 200
        assert 'href="/feeds"' not in _nav(resp.text)

    def test_api_without_token_is_still_401(self, single_env, monkeypatch):
        monkeypatch.setattr(settings, "AUTH_TOKEN", "sekret")
        with TestClient(app) as c:
            resp = c.post("/api/feeds", data={"name": "x", "url": "https://e.com/f"})
        assert resp.status_code == 401

    def test_html_get_without_token_still_redirects_to_login(self, single_env, monkeypatch):
        monkeypatch.setattr(settings, "AUTH_TOKEN", "sekret")
        with TestClient(app) as c:
            resp = c.get("/feeds", headers={"accept": "text/html"}, follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/login"

    def test_unknown_path_is_a_real_404_not_a_login_redirect(self, single_env, monkeypatch):
        monkeypatch.setattr(settings, "AUTH_TOKEN", "sekret")
        with TestClient(app) as c:
            resp = c.get("/no-such-page-xyz", headers={"accept": "text/html"},
                         follow_redirects=False)
        assert resp.status_code == 404
        assert "location" not in resp.headers
        assert "Page not found" in resp.text

    def test_unknown_path_is_a_real_404_for_the_operator_too(self, single_env, monkeypatch):
        monkeypatch.setattr(settings, "AUTH_TOKEN", "sekret")
        with TestClient(app) as c:
            c.cookies.set(auth.AUTH_COOKIE_NAME, "sekret")
            resp = c.get("/no-such-page-xyz", headers={"accept": "text/html"},
                         follow_redirects=False)
        assert resp.status_code == 404
        assert "Page not found" in resp.text
        assert 'href="/"' in resp.text  # operator chrome: back to dashboard


# ── multi mode ───────────────────────────────────────────────────────────────

TENANT_ID = 11
ADMIN_ID = 12
SUSPENDED_ID = 13


@pytest.fixture
def multi_env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MULTI", True)
    monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "nav-multi.db")
    database.init_db()
    with database.get_db() as db:
        for uid, email, admin, susp in (
            (TENANT_ID, "tenant@example.com", 0, 0),
            (ADMIN_ID, "admin@example.com", 1, 0),
            (SUSPENDED_ID, "banned@example.com", 0, 1),
        ):
            db.execute(
                "INSERT INTO users (id, email, password_hash, plan, is_admin, suspended)"
                " VALUES (?, ?, '', 'trial', ?, ?)",
                (uid, email, admin, susp),
            )
    return settings


def _as(client, uid, email):
    client.cookies.set("feedecho_session", security.sign_session(uid, email))
    return client


@pytest.mark.multi
class TestMultiMode:
    def test_anonymous_gets_a_sign_in_funnel(self, multi_env):
        with TestClient(app) as c:
            for path in ("/login", "/register", "/about"):
                page = c.get(path).text
                nav = _nav(page)
                links = _nav_links(page)
                for link in APP_LINKS:
                    assert link not in nav, f"{path}: {link}"
                assert ">Log in</a>" in links, path
                assert ">Sign up</a>" in links, path
                assert 'href="/login"' in links, path
                assert 'href="/register"' in links, path
                # Anonymous brand points at / (the hosted landing page).
                assert 'href="/" class="nav-brand"' in nav, path

    def test_anonymous_unknown_path_is_a_real_404(self, multi_env):
        with TestClient(app) as c:
            resp = c.get("/no-such-page-xyz", headers={"accept": "text/html"},
                         follow_redirects=False)
        assert resp.status_code == 404
        assert "location" not in resp.headers
        assert "Page not found" in resp.text

    def test_signed_in_unknown_path_is_a_real_404(self, multi_env):
        with TestClient(app) as c:
            _as(c, TENANT_ID, "tenant@example.com")
            resp = c.get("/no-such-page-xyz", headers={"accept": "text/html"},
                         follow_redirects=False)
        assert resp.status_code == 404
        assert "Page not found" in resp.text

    @pytest.mark.parametrize("path", ["/forgot-password", "/reset-password"])
    def test_excluded_public_paths_stay_anonymous(self, multi_env, path):
        # These are for people who cannot get in; the middleware deliberately
        # does not identify the viewer there. Pinned so a future edit cannot
        # widen identification to the whole exempt set unnoticed.
        with TestClient(app) as c:
            page = _as(c, TENANT_ID, "tenant@example.com").get(path).text
        nav = _nav(page)
        assert "tenant@example.com" not in nav
        assert 'href="/feeds"' not in nav

    def test_verify_email_keys_off_the_token_not_the_session(self, multi_env):
        """/verify-email is a state-changing GET opened from an email link and
        is one of the newly-identified paths. It must verify the token's user
        and nobody else, even when a *different* user is signed in."""
        from verification import issue_token

        token = issue_token(ADMIN_ID, "verify")
        with TestClient(app) as c:
            resp = _as(c, TENANT_ID, "tenant@example.com").get(
                f"/verify-email?token={token}", follow_redirects=False
            )
        assert resp.status_code == 302
        with database.get_db() as db:
            rows = {
                r["id"]: r["email_verified"]
                for r in db.execute(
                    "SELECT id, email_verified FROM users WHERE id IN (?, ?)",
                    (TENANT_ID, ADMIN_ID),
                ).fetchall()
            }
        assert rows[ADMIN_ID] == 1, "the token's user should be verified"
        assert not rows[TENANT_ID], "the signed-in user must not be verified"

    def test_verify_email_still_works_for_an_anonymous_clicker(self, multi_env):
        from verification import issue_token

        token = issue_token(TENANT_ID, "verify")
        with TestClient(app) as c:
            resp = c.get(f"/verify-email?token={token}", follow_redirects=False)
        assert resp.status_code == 302
        with database.get_db() as db:
            row = db.execute(
                "SELECT email_verified FROM users WHERE id = ?", (TENANT_ID,)
            ).fetchone()
        assert row["email_verified"] == 1

    def test_verify_email_with_a_bad_token_renders_anonymous_chrome(self, multi_env):
        # The error page is rendered by the same render() path; an anonymous
        # clicker must not be offered app links from it.
        with TestClient(app) as c:
            resp = c.get("/verify-email?token=nonsense")
        assert resp.status_code == 400
        assert 'href="/feeds"' not in _nav(resp.text)

    def test_signed_in_user_keeps_their_chrome_on_a_public_page(self, multi_env):
        # The whole point of the middleware change: /about is public, but a
        # logged-in reader still needs a way back into the app.
        with TestClient(app) as c:
            page = _as(c, TENANT_ID, "tenant@example.com").get("/about").text
        nav = _nav(page)
        for link in APP_LINKS:
            assert link in nav, link
        assert "tenant@example.com" in nav
        assert 'action="/logout"' in nav
        # ...but a tenant is still not shown the version.
        assert f"v{VERSION}" not in page

    def test_admin_sees_the_version_on_a_public_page(self, multi_env):
        with TestClient(app) as c:
            page = _as(c, ADMIN_ID, "admin@example.com").get("/about").text
        assert f"v{VERSION}" in page
        assert 'href="/admin"' in _nav(page)

    def test_suspended_session_is_treated_as_anonymous_on_public_pages(self, multi_env):
        with TestClient(app) as c:
            nav = _nav(_as(c, SUSPENDED_ID, "banned@example.com").get("/about").text)
        assert 'href="/feeds"' not in nav
        assert "banned@example.com" not in nav

    def test_stale_epoch_session_is_treated_as_anonymous_on_public_pages(self, multi_env):
        # A password reset bumps session_epoch; old cookies must not identify.
        with database.get_db() as db:
            db.execute(
                "UPDATE users SET session_epoch = 5 WHERE id = ?", (TENANT_ID,)
            )
        with TestClient(app) as c:
            nav = _nav(_as(c, TENANT_ID, "tenant@example.com").get("/about").text)
        assert 'href="/feeds"' not in nav
        assert "tenant@example.com" not in nav

    @pytest.mark.parametrize("path", ["/login", "/register"])
    def test_authenticated_get_redirects_into_the_app(self, multi_env, path):
        with TestClient(app) as c:
            resp = _as(c, TENANT_ID, "tenant@example.com").get(
                path, follow_redirects=False
            )
        assert resp.status_code == 302
        assert resp.headers["location"] == "/"

    def test_anonymous_login_page_still_renders(self, multi_env):
        with TestClient(app) as c:
            resp = c.get("/login")
        assert resp.status_code == 200
        assert 'action="/login"' in resp.text

    def test_login_post_is_not_redirected(self, multi_env):
        # Only GET is short-circuited; posting credentials must still work.
        with TestClient(app) as c:
            resp = _as(c, TENANT_ID, "tenant@example.com").post(
                "/login", data={"email": "tenant@example.com", "password": "wrong"},
                follow_redirects=False,
            )
        assert resp.status_code == 200  # re-renders with an error, not a 302

    def test_public_assets_do_not_hit_the_database(self, multi_env, monkeypatch):
        # The public-page identification must not make /static and /healthz pay
        # for a query on every request.
        calls = []
        real = app_module.get_db

        def counting_get_db(*a, **kw):
            calls.append(1)
            return real(*a, **kw)

        monkeypatch.setattr(app_module, "get_db", counting_get_db)
        with TestClient(app) as c:
            _as(c, TENANT_ID, "tenant@example.com")
            # Startup (_bootstrap_admin, _revalidate_stored_templates) queries
            # too; only per-request reads are being measured here.
            calls.clear()
            c.get("/static/css/style.css")
            c.get("/healthz")
            assert calls == []
            c.get("/about")
            assert calls, "a public page should identify the viewer"
