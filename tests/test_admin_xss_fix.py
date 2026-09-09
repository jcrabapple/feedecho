"""Regressions for the inline-JS string-breakout XSS fix in admin/folder
confirm dialogs (docs/reviews/2026-09-09-bug-review.md findings #1, #20, #21).

Before the fix, several `onsubmit="return confirm('... {{ u.email }} ...')"`
attributes interpolated untrusted, Jinja-autoescaped user data directly into
an inline JS string. Because the browser HTML-decodes attribute values
*before* compiling the inline handler as JS, a quote/apostrophe in the email
(or folder name) could break out of the JS string literal and execute
arbitrary JS in the viewer's session. The fix moves the untrusted value into
a `data-*` attribute (which Jinja autoescape does make safe there) and reads
it back with `element.dataset` in static/js/app.js, building the confirm()
text via plain string concatenation instead of template interpolation.

These tests assert the *shape* of the rendered HTML (data-attribute present,
vulnerable inline-interpolation pattern absent) rather than executing JS in
a browser, matching how this repo tests template output elsewhere.
"""

import re

import pytest
from fastapi.testclient import TestClient

import database
import security
import settings
from app import app

ADMIN_ID = 20
EVIL_ID = 21

EVIL_EMAIL = "x'-alert(1)-'@a.co"


@pytest.fixture
def admin_env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MULTI", True)
    monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "admin-xss.db")
    database.init_db()
    with database.get_db() as db:
        db.execute(
            "INSERT INTO users (id, email, password_hash, is_admin)"
            " VALUES (?, 'admin@example.com', '', 1)",
            (ADMIN_ID,),
        )
        # Registration only rejects '@', whitespace, and CR/LF in the local
        # part (utils.EMAIL_RE) -- quotes and parens are allowed through, so
        # this is a realistic attacker-controlled value, inserted directly
        # here the same way test_admin_dashboard.py seeds users.
        db.execute(
            "INSERT INTO users (id, email, password_hash, is_admin, plan)"
            " VALUES (?, ?, '', 0, 'trial')",
            (EVIL_ID, EVIL_EMAIL),
        )
    return settings


def _admin_client():
    c = TestClient(app)
    c.cookies.set("feedecho_session", security.sign_session(ADMIN_ID, "admin@example.com"))
    return c


class TestAdminEmailXss:
    def test_evil_email_never_appears_inside_inline_onsubmit(self, admin_env):
        with _admin_client() as c:
            resp = c.get("/admin")
        assert resp.status_code == 200
        html = resp.text

        # The raw email must never sit inside an onsubmit="...confirm(...)"
        # attribute anywhere in the page -- that was the vulnerable pattern.
        for m in re.finditer(r'onsubmit="([^"]*)"', html):
            assert EVIL_EMAIL not in m.group(1), (
                "attacker email leaked into an inline onsubmit handler: "
                + m.group(1)
            )
            # Also guard against the raw apostrophe/quote fragments landing
            # there even if the rest of the email were stripped somehow.
            assert "'-alert(1)-'" not in m.group(1)

        # It must instead show up only as a data-email attribute value,
        # where Jinja autoescaping keeps it a safe, inert string.
        assert 'data-email="x&#39;-alert(1)-&#39;@a.co"' in html or (
            'data-email="' in html and EVIL_EMAIL.replace("'", "&#39;") in html
        )

    def test_admin_action_forms_use_data_email_and_js_helpers(self, admin_env):
        with _admin_client() as c:
            resp = c.get("/admin")
        html = resp.text

        # Each of the five forms named in the finding must have moved to
        # the data-attribute + dedicated-JS-function pattern.
        for fn in (
            "adminConfirmSuspend",
            "adminConfirmRemoveAdmin",
            "adminConfirmMakeAdmin",
            "adminConfirmSetPlan",
            "adminConfirmExtendTrial",
        ):
            assert f"onsubmit=\"return {fn}(this)\"" in html

        # None of the old Jinja-interpolated-into-JS-string patterns remain.
        assert "onsubmit=\"return confirm('Suspend " not in html
        assert "onsubmit=\"return confirm('Remove admin access from " not in html
        assert "onsubmit=\"return confirm('Make " not in html
        assert "onsubmit=\"return confirm('Set plan for " not in html
        assert "onsubmit=\"return confirm('Extend " not in html

    def test_extend_trial_js_has_no_bare_unescaped_apostrophe(self):
        # Finding #2: the *static* template text itself had a bare
        # apostrophe ("{{ u.email }}'s trial") that broke the JS string on
        # every render, regardless of email content. Assert the app.js
        # helper builds that text via safe concatenation instead.
        with open("static/js/app.js", encoding="utf-8") as f:
            js = f.read()
        m = re.search(
            r"function adminConfirmExtendTrial\(form\)\s*\{(.*?)\n\}",
            js,
            re.S,
        )
        assert m, "adminConfirmExtendTrial helper not found in app.js"
        body = m.group(1)
        # The apostrophe must appear only inside a JS string literal built
        # by concatenation (immediately preceded by a `+ '...` piece), not
        # embedded raw in a template-interpolated identifier.
        assert "trial by " in body
        assert "\\'s trial by" in body or "'s trial by" in body
        # Sanity: the helper is syntactically well-formed JS -- balanced
        # quotes on the confirm() line once escaped apostrophes (\') are
        # discounted, since those are literal characters inside a string,
        # not delimiters.
        confirm_line = next(line for line in body.splitlines() if "confirm(" in line)
        unescaped = confirm_line.replace("\\'", "")
        assert unescaped.count("'") % 2 == 0


class TestFolderNameXss:
    @pytest.fixture
    def single_env(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "MULTI", False)
        monkeypatch.setattr(settings, "AUTH_TOKEN", None)
        monkeypatch.setattr(settings, "DATABASE_URL", "")
        monkeypatch.setattr(database, "DB_PATH", tmp_path / "folder-xss.db")
        database.init_db()
        import scheduler

        monkeypatch.setattr(scheduler, "fetch_feed", lambda url: {"items": []})
        return settings

    def test_evil_folder_name_never_appears_inside_inline_onsubmit(self, single_env):
        client = TestClient(app)
        evil_name = "a'-alert(1)-'"
        r = client.post("/api/folders", data={"name": evil_name}, follow_redirects=False)
        assert r.status_code == 303

        resp = client.get("/feeds")
        assert resp.status_code == 200
        html = resp.text

        for m in re.finditer(r'onsubmit="([^"]*)"', html):
            assert evil_name not in m.group(1)
            assert "'-alert(1)-'" not in m.group(1)

        assert "data-folder-name=" in html
        assert 'onsubmit="return folderConfirmRename(this)"' in html
        assert 'onsubmit="return folderConfirmDelete(this)"' in html
        # Old vulnerable patterns are gone.
        assert "var n = prompt('Rename folder:', '" not in html
        assert "confirm('Delete folder \\'" not in html
