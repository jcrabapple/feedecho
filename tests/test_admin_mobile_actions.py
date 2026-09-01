"""Admin mobile action-cell layout regression tests.

On phones the admin page's per-user action forms (Suspend / Make admin /
plan select + Set plan / days input + Extend trial) rendered side by side
inside the ACTIONS card row and collided at the right edge: "Make admin"
clipped under the plan dropdown, "Extend trial" cut off. The <=640px
stylesheet now stacks one action cluster per line.

Selector gotcha pinned here: the admin template's Actions cells carry no
.action-cell class (that class only exists on accounts/feeds/echoes/history),
so the rule must target td[data-label="Actions"].
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STYLE_CSS = (REPO_ROOT / "static" / "css" / "style.css").read_text(encoding="utf-8")
ADMIN_HTML = (REPO_ROOT / "templates" / "admin.html").read_text(encoding="utf-8")


def _admin_block() -> str:
    for m in re.finditer(r"@media \(max-width: 640px\) \{", STYLE_CSS):
        start = m.end()
        depth, i = 1, start
        while depth and i < len(STYLE_CSS):
            if STYLE_CSS[i] == "{":
                depth += 1
            elif STYLE_CSS[i] == "}":
                depth -= 1
            i += 1
        body = STYLE_CSS[start:i - 1]
        if 'td[data-label="Actions"]' in body and "inline-form" in body:
            return body
    raise AssertionError("mobile admin action-cell block not found")


class TestAdminMobileActions:
    def test_actions_cell_targets_data_label_not_action_cell(self):
        block = _admin_block()
        assert 'td[data-label="Actions"]' in block
        assert ".data-table td.action-cell {" not in block, (
            "the admin Actions cells have no .action-cell class; a class-based "
            "selector silently matches nothing"
        )

    def test_actions_cell_stacks(self):
        block = _admin_block()
        rule = re.search(r'\.data-table td\[data-label="Actions"\]\s*\{([^}]*)\}', block)
        assert rule, "actions cell rule missing"
        body = rule.group(1)
        assert "display: block" in body, "forms must stack vertically"
        assert "white-space: normal" in body, "nowrap clips button labels"

    def test_form_clusters_stay_on_one_line_internally(self):
        """Each .inline-form keeps select+button side by side (flex row)."""
        block = _admin_block()
        rule = re.search(
            r'\.data-table td\[data-label="Actions"\] \.inline-form\s*\{([^}]*)\}', block
        )
        assert rule and "display: flex" in rule.group(1)

    def test_selects_and_inputs_capped(self):
        block = _admin_block()
        rule = re.search(
            r'\.data-table td\[data-label="Actions"\] \.inline-form select,[^{]*\{([^}]*)\}',
            block,
        )
        assert rule, "select/input sizing rule missing"
        body = rule.group(1)
        assert "min-width: 0" in body and "max-width" in body, (
            "the plan dropdown and days input must shrink to fit but never overlap"
        )
        assert "min-height: 44px" in body, "touch target"

    def test_admin_template_uses_data_label_actions(self):
        assert ADMIN_HTML.count('data-label="Actions"') == 2, (
            "admin.html user rows + invite rows must keep the data-label the mobile CSS hooks into"
        )

    def test_cache_buster_bumped(self):
        base = (REPO_ROOT / "templates" / "base.html").read_text(encoding="utf-8")
        assert 'style.css?v=35' in base
