"""Mobile navbar tests (two-row header; wrapped tab rows since v1.69.1).

Pattern: row 1 = brand + theme toggle + avatar account menu; the tabs then
wrap onto as many full-width rows as needed (44px touch targets) so every
destination is visible at first paint — the v1.26.3 scroll strip + edge
fade read as clipped (external review, 2026-09). The account email/logout
collapse into a zero-JS <details> menu on mobile; desktop keeps the inline
email + logout. Sticky positioning is retained deliberately.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STYLE_CSS = (REPO_ROOT / "static" / "css" / "style.css").read_text(encoding="utf-8")
BASE_HTML = (REPO_ROOT / "templates" / "base.html").read_text(encoding="utf-8")


def _blocks():
    """Yield (start_offset, body) for every max-width:640px media block."""
    for m in re.finditer(r"@media \(max-width: 640px\) \{", STYLE_CSS):
        start = m.end()
        depth, i = 1, start
        while depth and i < len(STYLE_CSS):
            if STYLE_CSS[i] == "{":
                depth += 1
            elif STYLE_CSS[i] == "}":
                depth -= 1
            i += 1
        yield m.start(), STYLE_CSS[start:i - 1]


def _nav_block() -> str:
    for _, body in _blocks():
        if ".nav-links-wrap" in body:
            return body
    raise AssertionError("mobile navbar block not found")


class TestMobileNavStructure:
    def test_two_row_layout_via_order(self):
        block = _nav_block()
        assert re.search(r"\.nav-brand\s*\{[^}]*order: 1", block, re.S)
        assert re.search(r"\.theme-toggle\s*\{[^}]*order: 2", block, re.S)
        assert re.search(r"\.nav-account\s*\{[^}]*order: 3", block, re.S)
        # the tab strip's row is .nav-links-wrap (a non-scrolling positioning
        # context for the edge-fade cue); .nav-links itself is the inner
        # scrolling strip and no longer carries order/flex-basis directly.
        assert re.search(r"\.nav-links-wrap\s*\{[^}]*order: 4", block, re.S)
        assert re.search(r"\.nav-links-wrap\s*\{[^}]*flex: 1 1 100%", block, re.S)

    def test_tabs_wrap_instead_of_scrolling(self):
        """External review (2026-09): the scroll strip still read as clipped —
        History/Settings/How To sat behind an undiscoverable swipe. Tabs now
        wrap to a second row so every destination is visible at first paint."""
        block = _nav_block()
        links_rule = re.search(r"\.nav-links\s*\{([^}]*)\}", block).group(1)
        assert "flex-wrap: wrap" in links_rule
        assert "nowrap" not in links_rule
        assert "overflow-x: auto" not in links_rule

    def test_no_scroll_edge_fade_remains(self):
        """The fade was a cue for hidden scroll content; with wrapping there
        is no scroll, so the cue (and its always-on false-positive paint,
        the deferred v1.51.2 LOW) is gone with it."""
        block = _nav_block()
        assert ".nav-links-wrap::after" not in block

    def test_touch_targets_are_44px(self):
        block = _nav_block()
        tab_rule = re.search(r"\.nav-links a\s*\{([^}]*)\}", block).group(1)
        assert "min-height: 44px" in tab_rule
        assert "min-height: 44px" in re.search(r"\.theme-toggle\s*\{[^}]*\}", block, re.S).group(0)
        assert "min-height: 44px" in re.search(r"\.nav-account-menu summary\s*\{[^}]*\}", block, re.S).group(0)

    def test_email_collapses_to_details_menu_on_mobile(self):
        block = _nav_block()
        assert re.search(r"\.nav-account > \.nav-email[^{]*\{[^}]*display: none", block, re.S)
        assert ".nav-account-menu" in block and ".nav-avatar" in block

    def test_sticky_retained_and_safe_area_padded(self):
        # .navbar keeps position: sticky at desktop scale (the design keeps it)
        navbar_rule = re.search(r"\.navbar\s*\{([^}]*)\}", STYLE_CSS).group(1)
        assert "position: sticky" in navbar_rule
        block = _nav_block()
        assert "safe-area-inset-top" in block, "viewport-fit=cover ships; the sticky header must pad for it"

    def test_desktop_hides_the_mobile_menu(self):
        assert re.search(
            r"@media \(min-width: 641px\) \{[^}]*\.nav-account-menu\s*\{[^}]*display: none",
            STYLE_CSS, re.S,
        ), "the <details> menu must be mobile-only"

    def test_400px_shrink_no_longer_breaks_touch_targets(self):
        m = re.search(r"@media \(max-width: 400px\) \{((?:[^{}]|\{[^{}]*\})*?)\}", STYLE_CSS, re.S)
        body = m.group(1)
        assert ".nav-links a" not in body, (
            "the 400px font/padding shrink would drop tab targets below 44px"
        )


class TestNavTemplate:
    def test_theme_toggle_hoisted_out_of_nav_links(self):
        # the toggle must be a direct child of .navbar now (row composition)
        m = re.search(r'</div>\s*<button class="theme-toggle"', BASE_HTML)
        assert m, "theme toggle must sit outside .nav-links for row ordering"

    def test_details_menu_present_with_avatar(self):
        assert 'class="nav-account-menu"' in BASE_HTML
        assert 'class="nav-avatar"' in BASE_HTML
        assert "{{ current_user_email[0] | upper }}" in BASE_HTML
        assert 'aria-label="Account menu ({{ current_user_email }})"' in BASE_HTML

    def test_both_copies_of_logout_present(self):
        # desktop inline copy + mobile details copy; display:none per
        # breakpoint means AT/tab order only ever sees one
        assert BASE_HTML.count('action="/logout"') == 2

    def test_cache_buster_bumped(self):
        assert 'style.css?v=60' in BASE_HTML
