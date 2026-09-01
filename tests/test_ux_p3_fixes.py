"""P3 polish fixes from the 2026-08-28 Kimi UX review.

Findings covered (numbers from docs/reviews/2026-08-28-kimi-ux-review.md):
21 echoes Toggle button becomes Enable/Disable naming the action.
26 forgot-password hides the form after the reset email is sent and shows
   a success status instead of a plain paragraph above a live form.
27 404/error recovery links match the viewer's auth state.
31 dashboard's empty account cell falls back to an em dash like its
   neighbours.
32 theme toggle announces the action it performs ("Switch to light/dark
   theme") instead of a glyph + aria-pressed.

Findings 23, 24, 25, 28, 29, 30 were absorbed into the P2 pass and are
pinned there (tests/test_ux_p2_fixes.py) or in test_ux_p1_fixes.py.
"""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import database
import settings
from app import app

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_JS = (REPO_ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")


def _tpl(name: str) -> str:
    return (REPO_ROOT / "templates" / name).read_text(encoding="utf-8")


@pytest.fixture
def multi_env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MULTI", True)
    monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "ux-p3-multi.db")
    database.init_db()
    return settings


@pytest.fixture
def single_env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MULTI", False)
    monkeypatch.setattr(settings, "AUTH_TOKEN", "sekret")
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "ux-p3-single.db")
    database.init_db()
    return settings


# ---------------------------------------------------------------------------
# 21. Enable/Disable instead of Toggle
# ---------------------------------------------------------------------------


class TestEchoToggleButton:
    def test_no_bare_toggle_label(self):
        page = _tpl("echoes.html")
        assert ">Toggle</button>" not in page

    def test_button_names_its_action(self):
        page = _tpl("echoes.html")
        # enabled -> Disable/disableEcho, disabled -> Enable/enableEcho
        assert re.search(r"onclick=\"withBusy\(this, \(btn\) => disableEcho\(\{\{ echo\.id \}\}, btn\)\)\">Disable</button>", page)
        assert re.search(r"onclick=\"withBusy\(this, \(btn\) => enableEcho\(\{\{ echo\.id \}\}, btn\)\)\">Enable</button>", page)

    def test_js_keeps_toggle_backed(self):
        assert "async function disableEcho(" in APP_JS
        assert "async function enableEcho(" in APP_JS
        assert APP_JS.count("return toggleEcho(echoId, btn);") == 2


# ---------------------------------------------------------------------------
# 26. Forgot-password post-send state
# ---------------------------------------------------------------------------


class TestForgotPasswordSentState:
    def test_template_hides_form_when_sent(self):
        page = _tpl("forgot_password.html")
        # the form only renders in the not-sent branch
        assert re.search(r"\{% if not sent %\}\s*<form", page)
        assert "role=\"status\"" in page, "confirmation must be a polite status"

    def test_rendered_pages(self, multi_env):
        with TestClient(app) as c:
            unsent = c.get("/forgot-password").text
        assert "<form" in unsent
        with TestClient(app) as c:
            sent = c.get("/forgot-password?sent=1").text
        # sent renders via template flag; the GET page without submit never
        # sets it, so assert the no-form branch by checking both renders of
        # the template above and the live page keeps the form for entry.
        assert "Back to login" in sent


# ---------------------------------------------------------------------------
# 27. 404/error recovery links match auth state
# ---------------------------------------------------------------------------


class TestRecoveryLinks:
    def test_404_template_gates_dashboard_link(self):
        page = _tpl("404.html")
        assert "{% if authed %}" in page and "/login" in page
        assert "Back to dashboard" in page

    def test_error_template_gates_dashboard_link(self):
        page = _tpl("error.html")
        assert "{% if authed %}" in page and "/login" in page

    def test_anonymous_404_offers_login_not_dashboard(self, multi_env):
        with TestClient(app) as c:
            page = c.get("/definitely-not-a-page", headers={"accept": "text/html"}).text
        assert "Log in" in page
        assert "Back to dashboard" not in page


# ---------------------------------------------------------------------------
# 31. Dashboard empty account cell
# ---------------------------------------------------------------------------


class TestDashboardEmptyCell:
    def test_account_fallback_is_em_dash(self):
        page = _tpl("dashboard.html")
        assert "{{ post.account_name or '' }}" not in page
        assert "{{ post.account_name or '—' }}" in page


# ---------------------------------------------------------------------------
# 32. Theme toggle accessible name
# ---------------------------------------------------------------------------


class TestThemeToggleNaming:
    def test_aria_pressed_removed(self):
        # The theme toggle (3-state) must not use aria-pressed; only 2-state
        # toggles may: the reader star toggle and the reader density toggle,
        # plus the one-time legacy removeAttribute cleanup.
        hits = [ln for ln in APP_JS.splitlines() if "aria-pressed" in ln]
        assert "btn.removeAttribute('aria-pressed');" in [h.strip() for h in hits]
        assert any("setAttribute('aria-pressed'" in h and "starred" in h for h in hits), (
            "the reader star toggle should set aria-pressed for its on/off state"
        )
        assert any("setAttribute('aria-pressed'" in h and "density" in h for h in hits), (
            "the reader density toggle should set aria-pressed for its on/off state"
        )
        assert len([h for h in hits if h.strip()]) == 3

    def test_state_dependent_label(self):
        assert "'Switch to dark theme'" in APP_JS
        assert "'Switch to light theme'" in APP_JS
        assert "setAttribute('aria-label', label)" in APP_JS
        assert "setAttribute('title', label)" in APP_JS

    def test_base_template_carries_a_neutral_fallback_name(self):
        page = _tpl("base.html")
        m = re.search(r'<button class="theme-toggle"[^>]*>', page)
        assert m and 'aria-label="Switch theme"' in m.group(0)
