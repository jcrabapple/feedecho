"""UX review fixes (2026-08-28): visible focus, register labels, inline status, busy state.

Covers the four P1 findings from docs/reviews/2026-08-28-kimi-ux-review.md:

1. style.css replaced every input's focus outline with a 1px border-color
   shift. All three ``outline: none`` rules must now carry a visible outline.
2. register.html had zero labelled inputs (login.html labels all of its).
3. static/js/app.js reported every async outcome through blocking alert();
   buttons now render feedback into a polite live region instead.
4. Async action buttons showed no in-progress state; every firing site now
   goes through the withBusy() wrapper, which disables the button while the
   request is in flight.

The JS fixes are pinned as source-level checks (the behaviours live in the
browser, outside the TestClient's reach); the template and CSS fixes are
pinned against the rendered pages and the stylesheet text.
"""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import database
import settings
from app import app

REPO_ROOT = Path(__file__).resolve().parent.parent
STYLE_CSS = (REPO_ROOT / "static" / "css" / "style.css").read_text(encoding="utf-8")
APP_JS = (REPO_ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")

client = TestClient(app)


@pytest.fixture
def multi_env(monkeypatch, tmp_path):
    """/register is a multi-mode page; render it the way test_nav_gating does."""
    monkeypatch.setattr(settings, "MULTI", True)
    monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "ux-p1-multi.db")
    database.init_db()
    return settings


# ---------------------------------------------------------------------------
# 1. Visible focus indicators
# ---------------------------------------------------------------------------


class TestFocusVisibility:
    def test_no_outline_none_left_in_stylesheet(self):
        """The review found `outline: none` at three focus rules; none may return."""
        assert "outline: none" not in STYLE_CSS
        assert "outline:none" not in STYLE_CSS

    @pytest.mark.parametrize(
        ("selector", "line"),
        [
            ("inline-form", ".inline-form input:focus {"),
            ("form-row", ".form-row input:focus, .form-row select:focus, .form-row textarea:focus {"),
            ("auth-input", ".auth-input:focus {"),
        ],
        ids=["inline-form", "form-row", "auth-input"],
    )
    def test_each_focus_rule_has_visible_outline(self, selector, line):
        """Each input focus rule carries a 2px outline, not a colour-only cue."""
        pattern = re.escape(line) + r"(.*?)\}"  # rule body up to the closing brace
        rule = ""
        for m in re.finditer(pattern, STYLE_CSS, re.S):
            rule = m.group(0)
            break
        assert rule, f"{selector} focus rule not found in style.css"
        assert "outline: 2px solid" in rule, (
            f"{selector}:focus must set a visible outline (outline: 2px solid ...)"
        )


# ---------------------------------------------------------------------------
# 2. Register form labels
# ---------------------------------------------------------------------------


class TestRegisterLabels:
    def _register_page(self, multi_env) -> str:
        with TestClient(app) as c:
            resp = c.get("/register")
        assert resp.status_code == 200
        return resp.text

    def test_every_register_input_has_a_label_for(self, multi_env):
        page = self._register_page(multi_env)
        inputs = re.findall(r'<input\b[^>]*name="([^"]+)"[^>]*id="([^"]+)"[^>]*>', page)
        assert inputs, "register page should render inputs with ids"
        for name, input_id in inputs:
            assert re.search(rf'<label\b[^>]*for="{re.escape(input_id)}"', page), (
                f'input "{name}" (id={input_id}) has no <label for="{input_id}">'
            )

    def test_no_unlabelled_input_remains(self, multi_env):
        page = self._register_page(multi_env)
        for name in ("email", "password", "confirm", "invite_code"):
            if f'name="{name}"' not in page:
                continue  # conditional field not rendered (e.g. invite_code off)
            input_tag = re.search(rf'<input\b[^>]*name="{name}"[^>]*>', page)
            assert input_tag, f'{name} input missing from register page'
            assert 'id="' in input_tag.group(0), f'{name} input has no id to label against'


# ---------------------------------------------------------------------------
# 3. Inline status instead of alert()
# ---------------------------------------------------------------------------


class TestInlineStatus:
    def test_no_alert_calls_remain_in_app_js(self):
        alerts = [
            ln for ln in APP_JS.splitlines()
            if re.search(r"\balert\(", ln) and not ln.lstrip().startswith("//")
        ]
        assert alerts == [], "app.js must not report through blocking alert(): " + "\n".join(alerts)

    def test_showstatus_uses_live_region_and_textcontent(self):
        assert "function showStatus(" in APP_JS
        m = re.search(r"function showStatus\(.*?\n}", APP_JS, re.S)
        assert m, "showStatus body not found"
        body = m.group(0)
        assert "role" in body and "status" in body, "status box must be a live region"
        assert "aria-live" in body and "polite" in body
        assert "textContent" in body, "server strings must go through textContent, not innerHTML"
        assert not re.search(r"\.\s*innerHTML", body), (
            "showStatus must never write innerHTML (untrusted server strings)"
        )

    def test_async_actions_render_inline_status(self):
        for fn in ("testAccount", "testFeed", "fetchNow", "retryPost", "giveUpPost",
                   "toggleEcho", "pauseFeed"):
            m = re.search(rf"async function {fn}\(.*?\n}}", APP_JS, re.S)
            assert m, f"{fn} not found"
            assert "showStatus(" in m.group(0), f"{fn} must report through showStatus"


# ---------------------------------------------------------------------------
# 4. Busy state on async action buttons
# ---------------------------------------------------------------------------


class TestBusyState:
    def test_withbusy_disables_button_and_restores_it(self):
        m = re.search(r"async function withBusy\(.*?\n}", APP_JS, re.S)
        assert m, "withBusy wrapper missing"
        body = m.group(0)
        assert "btn.disabled = true" in body and "btn.disabled = false" in body
        assert "finally" in body, "button must be restored even when the action throws"

    def test_all_firing_sites_wrap_their_buttons(self):
        """Every template onclick that fires an async action goes through withBusy."""
        wrappers = {
            "accounts.html": ["testAccount", "testBlueskyAccount", "testMicroblogAccount"],
            "feeds.html": ["testFeed", "fetchNow", "pauseFeed"],
            "history.html": ["retryPost", "giveUpPost"],
            "echoes.html": ["disableEcho", "enableEcho"],
        }
        for template, fns in wrappers.items():
            page = (REPO_ROOT / "templates" / template).read_text(encoding="utf-8")
            for fn in fns:
                # a bare onclick="fn(...)" would skip the busy wrapper
                bare = re.search(rf'onclick="{fn}\(', page)
                assert bare is None, (
                    f"{template}: {fn} onclick must use withBusy(this, (btn) => {fn}(...))"
                )
                wrapped = re.search(rf'withBusy\(this, \(btn\) => {fn}\(', page)
                assert wrapped, f"{template}: no withBusy call site for {fn}"

    def test_settings_test_alt_text_passes_button(self):
        page = (REPO_ROOT / "templates" / "settings.html").read_text(encoding="utf-8")
        assert 'onclick="testAltText(this)"' in page, (
            "testAltText needs the button element to toggle its busy state"
        )
