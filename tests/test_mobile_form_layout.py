"""Mobile form-layout regression tests (scrunched inputs on phones).

The <=640px stylesheet must put every form label on its own line with a
full-width control beneath it: the natural .inline-form flex layout squeezes
`flex: 1` inputs down to the leftover space after the label text (a "Name"
input rendered ~30px wide), making contents unreadable on phones.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STYLE_CSS = (REPO_ROOT / "static" / "css" / "style.css").read_text(encoding="utf-8")


def _mobile_block() -> str:
    """Return the last max-width:640px media block (the form-layout one)."""
    blocks = re.findall(r"@media \(max-width: 640px\) \{", STYLE_CSS)
    assert blocks, "no mobile media block found"
    # split on blocks and take the one containing the inline-form label rule
    for m in re.finditer(r"@media \(max-width: 640px\) \{", STYLE_CSS):
        start = m.end()
        depth = 1
        i = start
        while depth and i < len(STYLE_CSS):
            if STYLE_CSS[i] == "{":
                depth += 1
            elif STYLE_CSS[i] == "}":
                depth -= 1
            i += 1
        block = STYLE_CSS[start:i - 1]
        if ".inline-form label" in block:
            return block
    raise AssertionError("mobile media block with .inline-form label rules not found")


class TestMobileFormLayout:
    def test_inline_form_labels_claim_full_line(self):
        block = _mobile_block()
        assert re.search(r"\.inline-form label\s*\{[^}]*width: 100%", block, re.S), (
            "mobile: .inline-form labels must take a full line so inputs drop below them"
        )
        assert "display: block" in re.search(r"\.inline-form label\s*\{[^}]*\}", block, re.S).group(0)

    def test_inline_form_inputs_go_full_width(self):
        block = _mobile_block()
        input_rule = re.search(r"\.inline-form input\s*\{([^}]*)\}", block)
        assert input_rule, "mobile .inline-form input rule missing"
        body = input_rule.group(1)
        assert "100%" in body, "inputs must fill the line (flex-basis or width 100%)"
        assert "min-width: 0" in body, "inputs must stay shrinkable without overflowing"

    def test_number_inputs_keep_usable_width(self):
        block = _mobile_block()
        m = re.search(r"\.inline-form input\[type=\"number\"\]\s*\{([^}]*)\}", block)
        assert m and "min-width" in m.group(1), (
            "number inputs (poll interval, ports) must keep a usable minimum width"
        )

    def test_form_row_controls_fill_width(self):
        blocks = re.findall(r"@media \(max-width: 640px\) \{((?:[^{}]|\{[^{}]*\})*?)\}\s*\n\s*\.template-vars", STYLE_CSS, re.S)
        found = False
        for m in re.finditer(r"@media \(max-width: 640px\) \{", STYLE_CSS):
            start = m.end()
            depth, i = 1, start
            while depth and i < len(STYLE_CSS):
                if STYLE_CSS[i] == "{":
                    depth += 1
                elif STYLE_CSS[i] == "}":
                    depth -= 1
                i += 1
            block = STYLE_CSS[start:i - 1]
            if ".form-row" in block and "input, .form-row select" in block:
                assert "width: 100%" in block, ".form-row controls must fill the column"
                found = True
        assert found, "no .form-row full-width rule in the mobile block"

    def test_submit_buttons_do_not_stretch(self):
        block = _mobile_block()
        assert re.search(r"\.inline-form button\s*\{[^}]*flex: 0 0 auto", block, re.S), (
            "submit buttons must keep their natural width on mobile"
        )

    def test_cache_buster_bumped(self):
        base = (REPO_ROOT / "templates" / "base.html").read_text(encoding="utf-8")
        assert 'style.css?v=37' in base, "CSS changed; bump the cache-buster so phones pick it up"

    def test_smtp_test_email_label_is_visible(self):
        page = (REPO_ROOT / "templates" / "settings.html").read_text(encoding="utf-8")
        m = re.search(r'<label[^>]*for="smtp-test-email"[^>]*>', page)
        assert m and "sr-only" not in m.group(0), (
            "the SMTP test-recipient input needs a visible label now that labels render on their own line"
        )
