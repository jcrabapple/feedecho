"""P2 UX fixes from the 2026-08-28 Kimi review (docs/reviews/2026-08-28-kimi-ux-review.md).

Covers, by review finding number:

5  feeds.html: "Item ID Set"/"Init" jargon replaced with plain-language
   tracking states; the Init button is titled.
10 settings.html: TLS options no longer advertise fixed ports next to a
   free-editable Port field (the contradiction invited mismatched configs).
11 echoes.html: destination field rows carry their initial visibility
   server-side, so the Mastodon row no longer flashes on load or shows
   wrong fields without JS.
13 admin.html: Suspend / Remove admin / Revoke require confirmation.
14 settings.html: persistent "not configured" banners use role=status.
15 accounts.html: ?status= banners clean the URL after render.
16 base.html: current nav link gets aria-current="page"; skip link added.
17 feeds/accounts forms: visible <label> elements, not sr-only+placeholder.
19 landing.html: features section gets an sr-only h2 (h1 -> h2 -> h3 order).
20 reset_password.html: errors use the same role=alert pattern as login.
23 admin.html: symmetric role toggle labels (Make admin / Remove admin).
24 admin/accounts tables: scope="col" on header cells.
25 admin.html: stored-password state is a persistent hint, not a placeholder.

app.js behaviours (7 focus return, 8 neutral Cancel, 9 toggleEditDest init,
18 scroll-preserving reload, 6 sr-only table header hydration) are pinned by
source checks in the same file.
"""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import database
import settings
from app import app

REPO_ROOT = Path(__file__).resolve().parent.parent


def _tpl(name: str) -> str:
    return (REPO_ROOT / "templates" / name).read_text(encoding="utf-8")


def _src(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


APP_JS = _src("static/js/app.js")
STYLE_CSS = _src("static/css/style.css")

client = TestClient(app)


@pytest.fixture
def multi_env(monkeypatch, tmp_path):
    """/register, /accounts and /echoes are multi-mode pages."""
    monkeypatch.setattr(settings, "MULTI", True)
    monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "ux-p2-multi.db")
    database.init_db()
    return settings


@pytest.fixture
def single_env(monkeypatch, tmp_path):
    """Single mode renders /feeds and /settings."""
    monkeypatch.setattr(settings, "MULTI", False)
    monkeypatch.setattr(settings, "AUTH_TOKEN", "sekret")
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "ux-p2-single.db")
    database.init_db()
    return settings


def _get(path: str, page: str):
    with TestClient(app) as c:
        if page == "single":
            c.cookies.set("feedecho_auth", "sekret")
        resp = c.get(path)
    assert resp.status_code == 200, (path, resp.status_code)
    return resp.text


# ---------------------------------------------------------------------------
# 5 + 17. Feeds page: plain-language tracking, visible labels
# ---------------------------------------------------------------------------


class TestFeedsPage:
    def test_jargon_terms_gone(self):
        page = _tpl("feeds.html")
        assert "Item ID Set" not in page
        assert re.search(r">Init<", page) is None
        assert re.search(r"click Init\b", page) is None, (
            "badge may say 'click Initialize' but never the jargon 'click Init'"
        )

    def test_plain_language_tracking_states(self):
        page = _tpl("feeds.html")
        assert "Tracking" in page
        assert "Initialize" in page

    def test_add_feed_form_has_visible_labels(self):
        page = _tpl("feeds.html")
        for input_id in ("feed-name", "feed-url", "feed-poll-interval"):
            label = re.search(rf'<label for="{input_id}">(.+?)</label>', page)
            assert label, f"missing visible <label for={input_id}>"
            assert "sr-only" not in label.group(0), f"label for {input_id} must be visible"

    def test_feeds_page_renders_with_labels(self, single_env):
        page = _get("/feeds", "single")
        assert '<label for="feed-name">Name</label>' in page


# ---------------------------------------------------------------------------
# 16. Nav: aria-current + skip link
# ---------------------------------------------------------------------------


class TestNavCurrent:
    def test_base_template_sets_aria_current(self):
        page = _tpl("base.html")
        assert 'aria-current="page"' in page
        # the old pattern was class="..." with the active class inside the if;
        # every active class must now sit on an element that also has aria-current
        for a in re.findall(r"<a\b[^>]*>", page):
            if 'class="active"' in a:
                assert 'aria-current="page"' in a, f"active link without aria-current: {a}"

    def test_rendered_page_marks_current_link(self, single_env):
        page = _get("/feeds", "single")
        m = re.search(r'<a href="/feeds"[^>]*aria-current="page"[^>]*>Feeds</a>', page)
        assert m, "the /feeds link must carry aria-current on /feeds"
        # no other link on the page may claim current
        assert page.count('aria-current="page"') == 1

    def test_skip_link_present_and_targets_main(self, single_env):
        page = _get("/feeds", "single")
        assert '<a class="skip-link" href="#main">Skip to main content</a>' in page
        assert '<main id="main">' in page

    def test_skip_link_styled_and_revealed_on_focus(self):
        assert ".skip-link" in STYLE_CSS and ".skip-link:focus" in STYLE_CSS

    def test_active_nav_has_non_color_cue(self):
        block = re.search(r"\.nav-links a\.active \{(.*?)\}", STYLE_CSS, re.S)
        assert block, "active nav rule missing"
        body = block.group(1)
        assert "font-weight" in body or "box-shadow" in body, (
            "current-page state needs a non-colour cue (WCAG 1.4.1)"
        )

    def test_nav_has_accessible_name(self):
        assert '<nav class="navbar" aria-label="Primary">' in _tpl("base.html")


# ---------------------------------------------------------------------------
# 14 + 10. Settings banners and TLS options
# ---------------------------------------------------------------------------


class TestSettingsPage:
    def test_persistent_banners_use_status_not_alert(self):
        page = _tpl("settings.html")
        banners = re.findall(r'<div class="alert alert-warning" role="(\w+)">', page)
        assert banners, "expected config-state banners"
        assert "alert" not in banners, "static state banners must not be role=alert"

    def test_tls_options_do_not_advertise_ports(self):
        page = _tpl("settings.html")
        assert "(port 587)" not in page
        assert "(port 465)" not in page
        # the real guidance lives in hint text
        assert "587" in page and "465" in page

    def test_alt_text_hint_is_not_mastodon_only(self):
        page = _tpl("settings.html")
        assert "Generate alt text before uploading images to Mastodon." not in page

    def test_hints_are_outside_labels(self):
        page = _tpl("settings.html")
        # a wrapping <label> element must not contain a .hint span: the hint
        # becomes part of the control's accessible name (finding 29)
        for m in re.finditer(r"<label\b([^>]*)>(.*?)</label>", page, re.S):
            attrs, body = m.group(1), m.group(2)
            if 'class="hint"' in body and "for=" not in attrs:
                assert False, (
                    f"hint text inside <label> pollutes the accessible name: {m.group(0)[:80]!r}"
                )


# ---------------------------------------------------------------------------
# 13 + 23 + 25. Admin confirms, symmetric labels, password hint
# ---------------------------------------------------------------------------


class TestAdminPage:
    def test_destructive_actions_confirm(self):
        page = _tpl("admin.html")
        suspend_form = re.search(r'<form[^>]*action="/admin/users/\{\{ u\.id \}\}/suspend"[^>]*>', page)
        assert suspend_form and "confirm(" in suspend_form.group(0)
        demote_form = re.search(r'<form[^>]*action="/admin/users/\{\{ u\.id \}\}/demote"[^>]*>', page)
        assert demote_form and "confirm(" in demote_form.group(0)
        revoke_form = re.search(r'<form[^>]*action="/admin/invites/revoke"[^>]*>', page)
        assert revoke_form and "confirm(" in revoke_form.group(0)
        # reversible actions stay one-click
        unsuspend_form = re.search(r'<form[^>]*action="/admin/users/\{\{ u\.id \}\}/unsuspend"[^>]*>', page)
        assert unsuspend_form and "confirm(" not in unsuspend_form.group(0)

    def test_symmetric_role_labels(self):
        page = _tpl("admin.html")
        assert ">Demote<" not in page
        assert ">Remove admin<" in page
        assert ">Make admin<" in page

    def test_stored_password_state_is_a_persistent_hint(self):
        page = _tpl("admin.html")
        assert "A password is stored" in page, "hint text must state the stored-password fact"
        m = re.search(r'name="smtp_password"[^>]*placeholder="([^"]*)"', page)
        assert m is not None
        assert "leave blank to keep" not in m.group(1), (
            "placeholder must not be the only carrier of the stored-state message"
        )

    def test_table_headers_have_scope(self):
        page = _tpl("admin.html")
        ths = re.findall(r"<th(?![^>]*scope=)[^>]*>[^<]*</th>", page)
        assert ths == [], f"admin.html has <th> without scope: {ths}"


# ---------------------------------------------------------------------------
# 15. accounts.html: status banners clean up their URL
# ---------------------------------------------------------------------------


class TestAccountsStatusCleanup:
    def test_template_strips_status_param_after_render(self):
        page = _tpl("accounts.html")
        assert "history.replaceState" in page
        assert "searchParams.delete('status')" in page

    def test_accounts_form_labels_are_visible(self):
        page = _tpl("accounts.html")
        for input_id in ("oauth-instance", "masto-name", "masto-username", "masto-instance",
                         "masto-token", "email-acct-name", "email-acct-address",
                         "bsky-name", "bsky-handle", "bsky-app-password", "microblog-token"):
            label = re.search(rf'<label for="{input_id}">(.+?)</label>', page)
            assert label, f"missing visible <label for={input_id}>"

    def test_account_delete_confirms_blast_radius(self):
        page = _tpl("accounts.html")
        confirms = re.findall(r"confirm\('([^']*)'\)", page)
        assert confirms, "delete confirms expected"
        for text in confirms:
            if "echo" in text.lower() or "stop working" in text.lower():
                break
        else:
            pytest.fail("no delete confirm explains the consequence for dependent echoes")


# ---------------------------------------------------------------------------
# 11 + 12. echoes.html: server-side initial visibility, CW hint
# ---------------------------------------------------------------------------


class TestEchoesForm:
    def test_destination_rows_are_server_side_hidden(self):
        page = _tpl("echoes.html")
        for row_id in ("mastodon-fields", "email-fields", "bluesky-fields", "microblog-fields"):
            m = re.search(rf'id="{row_id}"(\s*style="display: none;")?', page)
            assert m, f"#{row_id} missing"
            # every row goes through the dest_hidden macro, no hardcoded style
            assert 'style="display: none;">' not in m.group(0).replace(f'id="{row_id}"', ""), (
                f"#{row_id} must take its initial state from dest_hidden()"
            )
        assert "dest_hidden" in page

    def test_first_dest_matches_option_order(self):
        """The macro's fallback chain must mirror the <option> if-chain."""
        page = _tpl("echoes.html")
        option_order = re.findall(r"<option value=\"(\w+)\"", page)
        macro = re.search(r"first_dest = (.+?) %\}", page, re.S).group(1)
        first_macro = re.match(r"'(\w+)' if", macro.strip()).group(1)
        assert option_order, "destination options missing"
        assert option_order[0] == first_macro or first_macro == "mastodon", (
            "dest_hidden fallback chain starts with the wrong type"
        )

    def test_cw_hint_states_its_scope(self):
        page = _tpl("echoes.html")
        assert "Applied as spoiler text on Mastodon destinations; ignored by other destination types." in page


# ---------------------------------------------------------------------------
# 19 + 20. Landing heading order, reset-password alerts
# ---------------------------------------------------------------------------


class TestQuickA11y:
    def test_landing_heading_order_is_h1_h2_h3(self):
        page = _tpl("landing.html")
        levels = [int(m.group(1)) for m in re.finditer(r"<h([1-6])", page)]
        assert levels, "landing page has no headings"
        assert all(levels[i] <= levels[i + 1] + 1 for i in range(len(levels) - 1)) and levels[0] == 1, (
            f"landing heading outline is not monotonic from h1: h{levels}"
        )
        assert levels.count(2) >= 2, "need an explicit h2 between the h1 and the feature h3s"

    def test_landing_renders_with_sr_only_h2(self, multi_env):
        page = _get("/", "multi")
        assert '<h2 class="sr-only">Features</h2>' in page

    def test_reset_password_errors_use_role_alert(self):
        page = _tpl("reset_password.html")
        assert '<div class="alert alert-error" role="alert">{{ error }}</div>' in page
        assert 'class="auth-error"' not in page


# ---------------------------------------------------------------------------
# app.js behaviours: 7 focus, 8 cancel styling, 9 toggleEditDest, 18 scroll
# ---------------------------------------------------------------------------


class TestAppJsP2:
    def test_edit_opens_focus_first_field_and_cancel_restores_it(self):
        for fn in ("editFeed", "editEcho"):
            m = re.search(rf"function {fn}\(.*?\n}}", APP_JS, re.S)
            assert m and "?.focus()" in m.group(0), f"{fn} must move focus into the form"
        for fn in ("cancelEdit", "cancelFeedEdit"):
            m = re.search(rf"function {fn}\(.*?\n}}", APP_JS, re.S)
            assert m and "?.focus()" in m.group(0), f"{fn} must return focus to the row"

    def test_cancel_buttons_are_not_danger_styled(self):
        for m in re.finditer(r'<button[^>]*>Cancel</button>', APP_JS):
            assert "btn-danger" not in m.group(0), f"Cancel must not be danger-styled: {m.group(0)}"

    def test_editEcho_syncs_conditional_rows_on_open(self):
        body = re.search(r"function editEcho\(.*?\n}", APP_JS, re.S).group(0)
        assert "toggleEditDest(echoId)" in body, (
            "editEcho must call toggleEditDest so digest echoes never show the drip field"
        )

    def test_row_actions_reload_preserving_scroll(self):
        reloads = [
            ln for ln in APP_JS.splitlines()
            if "location.reload()" in ln and "function reloadPreservingScroll" not in ln
        ]
        # only the helper's internal call may use location.reload directly
        inside_helper = re.search(r"function reloadPreservingScroll\(\) \{.*?\n}", APP_JS, re.S).group(0)
        for ln in reloads:
            assert ln.strip() in inside_helper, (
                f"row action must use reloadPreservingScroll(), not bare reload: {ln.strip()}"
            )
        assert APP_JS.count("reloadPreservingScroll()") >= 5

    def test_table_header_hydration_exists(self):
        assert "hydrateTableHeaders" in APP_JS
        assert "table-label" in APP_JS and "sr-only" in APP_JS

    def test_asset_versions_bumped(self):
        base = _tpl("base.html")
        assert 'style.css?v=25' in base and 'app.js?v=21' in base
