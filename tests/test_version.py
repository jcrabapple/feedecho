"""Version reporting (issue #8).

Two halves:

- ``TestVersionConsistency`` pins the hardcoded copies of the version in the
  deploy artifacts to ``_version.__version__``, and scans the tracked tree for
  version-shaped strings (image tags, tag refs, nix ``version``/``rev``) that
  drifted. A partial bump used to be invisible until someone read the Nix
  expression or pulled the wrong image tag; now it fails here.
- ``TestVersionFooter*`` covers the UI: the running version shows in the footer
  for the person who can actually upgrade the deployment (the self-hoster in
  single mode, an admin on the hosted service) and nobody else.
"""

import re
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import auth
import database
import security
import settings
from _version import __version__ as VERSION
from app import app

ROOT = Path(__file__).resolve().parent.parent

# Files that deliberately name the version some past change shipped in.
# History, not configuration — kept in sync with scripts/bump_version.py.
HISTORICAL = {
    "Dockerfile",
    "docker-entrypoint.sh",
    "tests/test_review_fixes.py",
    "tests/test_version.py",
}


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class TestVersionConsistency:
    def test_version_is_semver(self):
        assert re.fullmatch(r"\d+\.\d+\.\d+", VERSION), VERSION

    def test_fastapi_reports_it(self):
        # /openapi.json and the docs page both read this.
        assert app.version == VERSION

    def test_pyproject_defers_to_version_module(self):
        pyproject = _read("pyproject.toml")
        assert 'dynamic = [ "version" ]' in pyproject
        assert 'path = "_version.py"' in pyproject
        # No second static copy left behind in [project].
        assert not re.search(r'^version = "', pyproject, re.MULTILINE)

    def test_flake_matches(self):
        assert f'version = "{VERSION}"' in _read("flake.nix")

    def test_nix_package_matches(self):
        pkg = _read("nix/package.nix")
        assert f'version = "{VERSION}"' in pkg
        assert f'rev = "v{VERSION}"' in pkg
        # The nix-prefetch-url example in the hash comment.
        assert f"v{VERSION}.tar.gz" in pkg

    def test_nix_readme_matches(self):
        readme = _read("nix/README.md")
        assert f'rev = "v{VERSION}"' in readme
        assert f"v{VERSION}.tar.gz" in readme

    def test_no_multi_compose_file_in_oss_repo(self):
        """The hosted compose file lives in the private feedecho-hosted
        overlay (product-split, 2026-08-27). Its version-pinned image tag is
        synced manually at deploy time, not asserted here."""
        assert not Path("docker-compose.multi.yml").exists()
        assert not Path("Caddyfile").exists()
        assert not Path(".env.example.multi").exists()
        assert not Path("HANDOFF.md").exists()

    def test_no_stale_version_anywhere_in_the_tree(self):
        """Catch a *new* hardcoded copy the per-file asserts above don't know
        about (a version pin added to the README, a workflow, an example
        compose file...). Only shapes that carry this project's own version
        are scanned, so dependency pins and timestamps are not false hits."""
        shapes = (
            re.compile(r"ghcr\.io/jcrabapple/feedecho:(\d+\.\d+\.\d+)"),
            re.compile(r"feedecho/archive/refs/tags/v(\d+\.\d+\.\d+)"),
            re.compile(r'\brev = "v(\d+\.\d+\.\d+)"'),
            re.compile(r'^\s*version = "(\d+\.\d+\.\d+)";', re.MULTILINE),
        )
        tracked = subprocess.run(
            ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.split()
        stale = []
        for rel in tracked:
            if rel in HISTORICAL or rel.startswith("docs/") or rel.endswith(".lock"):
                continue
            path = ROOT / rel
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for shape in shapes:
                for found in shape.findall(text):
                    if found != VERSION:
                        stale.append(f"{rel}: {found}")
        assert not stale, f"version drift (expected {VERSION}): {stale}"


@pytest.fixture
def single_env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MULTI", False)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "version-single.db")
    database.init_db()
    return settings


class TestVersionFooterSingleMode:
    def test_footer_shows_version_and_release_link(self, single_env):
        with TestClient(app) as c:
            page = c.get("/").text
        assert "site-footer" in page
        assert f"v{VERSION}" in page
        assert f'href="{settings.PROJECT_URL}/releases"' in page
        # The new-tab link has to say so for screen readers; the title
        # attribute is not reliably announced.
        assert "opens in a new tab" in page

    def test_footer_present_on_every_page(self, single_env):
        with TestClient(app) as c:
            for path in ("/", "/feeds", "/accounts", "/echoes", "/history", "/settings"):
                page = c.get(path).text
                assert f"v{VERSION}" in page, path

    def test_no_about_link_when_self_hosted(self, single_env):
        # /about is a hosted-service disclosure page and 404s here.
        with TestClient(app) as c:
            page = c.get("/").text
        assert 'href="/about"' not in page
        assert 'href="/howto"' in page

    def test_no_footer_at_all_on_unauthenticated_login_page(self, single_env, monkeypatch):
        # Nothing in the footer is reachable without the token, so it is not
        # rendered: /howto would bounce straight back to /login.
        monkeypatch.setattr(settings, "AUTH_TOKEN", "sekret")
        with TestClient(app) as c:
            page = c.get("/login").text
        assert f"v{VERSION}" not in page
        assert "site-footer" not in page

    def test_shown_once_token_accepted(self, single_env, monkeypatch):
        monkeypatch.setattr(settings, "AUTH_TOKEN", "sekret")
        with TestClient(app) as c:
            c.cookies.set(auth.AUTH_COOKIE_NAME, "sekret")
            page = c.get("/").text
        assert f"v{VERSION}" in page

    def test_hidden_when_token_wrong(self, single_env, monkeypatch):
        monkeypatch.setattr(settings, "AUTH_TOKEN", "sekret")
        with TestClient(app) as c:
            c.cookies.set(auth.AUTH_COOKIE_NAME, "wrong")
            page = c.get("/", follow_redirects=True).text
        assert f"v{VERSION}" not in page


TENANT_ID = 7
ADMIN_ID = 8


@pytest.fixture
def multi_env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MULTI", True)
    monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "version-multi.db")
    database.init_db()
    with database.get_db() as db:
        db.execute(
            "INSERT INTO users (id, email, password_hash, plan, is_admin)"
            " VALUES (?, ?, '', 'trial', 0)",
            (TENANT_ID, "tenant@example.com"),
        )
        db.execute(
            "INSERT INTO users (id, email, password_hash, plan, is_admin)"
            " VALUES (?, ?, '', 'paid', 1)",
            (ADMIN_ID, "admin@example.com"),
        )
    return settings


def _as(client, uid, email):
    client.cookies.set("feedecho_session", security.sign_session(uid, email))
    return client


def _footer(page: str) -> str:
    """Just the footer. The top nav links /howto to anonymous visitors too
    (pre-existing), so a whole-page assertion cannot tell them apart."""
    m = re.search(r"<footer.*?</footer>", page, re.S)
    return m.group(0) if m else ""


@pytest.mark.multi
class TestVersionFooterMultiMode:
    def test_withheld_from_public_pages(self, multi_env):
        with TestClient(app) as c:
            for path in ("/login", "/register", "/about"):
                page = c.get(path).text
                assert f"v{VERSION}" not in page, path
                # The footer itself still renders, just without the version.
                assert "site-footer" in page, path

    def test_withheld_from_a_signed_in_tenant(self, multi_env):
        # /register is public: gating on "logged in" would hand the version
        # to anyone willing to sign up, and a tenant cannot upgrade the
        # service anyway.
        with TestClient(app) as c:
            page = _as(c, TENANT_ID, "tenant@example.com").get("/").text
        assert f"v{VERSION}" not in page
        assert "site-footer" in page

    def test_shown_to_an_admin(self, multi_env):
        with TestClient(app) as c:
            page = _as(c, ADMIN_ID, "admin@example.com").get("/").text
        assert f"v{VERSION}" in page
        assert f'href="{settings.PROJECT_URL}/releases"' in page

    def test_about_link_kept_for_anonymous_visitors(self, multi_env):
        with TestClient(app) as c:
            page = c.get("/login").text
        footer = _footer(page)
        assert 'href="/about"' in footer
        # ...but not the auth-gated How To link.
        assert 'href="/howto"' not in footer
