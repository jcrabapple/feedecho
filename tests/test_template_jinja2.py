"""Tests for the Jinja2 post template engine (conditionals, filters, sandbox)."""

import pytest
from jinja2 import TemplateSyntaxError
from jinja2.exceptions import SecurityError

from template_engine import render_template, validate_template


ITEM = {
    "id": "abc123",
    "title": "A Post Title",
    "link": "https://example.com/post",
    "summary": "A short summary sentence that goes on for a while so truncation is testable.",
    "content": "Full content",
    "author": "Jane Doe",
    "date": "2024-01-15T09:30:00Z",
    "tags": ["python", "web"],
    "image_url": "https://example.com/img.jpg",
}


class TestJinja2Features:
    def test_conditional_when_summary_present(self):
        template = (
            "{% if summary %}{{ title }} - {{ summary | truncate(20) }}"
            "{% else %}{{ title }} {{ link }}{% endif %}"
        )
        result = render_template(template, ITEM)
        assert result.startswith("A Post Title - A short summary")
        assert "https://example.com/post" not in result

    def test_conditional_else_branch(self):
        template = "{% if author == 'Nobody' %}A{% else %}{{ title }}{% endif %}"
        assert render_template(template, ITEM) == "A Post Title"

    def test_truncate_filter(self):
        result = render_template("{{ title | truncate(5) }}", ITEM)
        assert result == "A..."

    def test_default_filter_on_missing(self):
        result = render_template("{{ missing_var | default('fallback') }}", ITEM)
        assert result == "fallback"

    def test_default_filter_on_empty_with_boolean(self):
        # Jinja2's default() only replaces undefined unless boolean=true.
        item = dict(ITEM, author="")
        result = render_template("{{ author | default('Anonymous', true) }}", item)
        assert result == "Anonymous"

    def test_item_dict_dot_access(self):
        assert render_template("{{ item.title }}", ITEM) == "A Post Title"

    def test_item_dict_subscript_access(self):
        assert render_template("{{ item['link'] }}", ITEM) == "https://example.com/post"

    def test_item_raw_list_index(self):
        result = render_template("{{ item['tags'][0] }}", ITEM)
        assert result == "python"

    def test_feed_name_variable(self):
        result = render_template("{{ feed_name }}: {{ title }}", ITEM, feed_name="My Feed")
        assert result == "My Feed: A Post Title"

    def test_feed_name_empty_by_default(self):
        assert render_template("{{ feed_name }}", ITEM) == ""

    def test_date_iso_variable(self):
        assert render_template("{{ date_iso }}", ITEM) == "2024-01-15T09:30:00"

    def test_date_short_variable(self):
        assert render_template("{{ date_short }}", ITEM) == "2024-01-15"

    def test_tags_variable(self):
        assert render_template("{{ tags }}", ITEM) == "['python', 'web']"

    def test_legacy_colon_syntax_in_conditional(self):
        template = "{% if date_short %}{{ date:short }}{% endif %}"
        assert render_template(template, ITEM) == "2024-01-15"

    def test_loop_over_tags(self):
        template = "{% for tag in tags %}#{{ tag }} {% endfor %}"
        assert render_template(template, ITEM) == "#python #web "


class TestLegacyCompatibility:
    """The old regex engine's documented syntax must keep rendering."""

    def test_legacy_date_iso_token(self):
        assert render_template("{{ date:iso }}", ITEM) == "2024-01-15T09:30:00"

    def test_legacy_date_short_token(self):
        assert render_template("{{ date:short }}", ITEM) == "2024-01-15"

    def test_legacy_combined(self):
        template = "{{ title }} {{ link }} ({{ date:short }})"
        assert render_template(template, ITEM) == (
            "A Post Title https://example.com/post (2024-01-15)"
        )

    def test_unknown_variable_renders_empty(self):
        assert render_template("{{ nope }}", ITEM) == ""

    def test_whitespace_insensitive(self):
        assert render_template("{{   title   }}", ITEM) == "A Post Title"

    def test_literal_prose_with_token_text_preserved(self):
        # "date:iso" outside {{ }} is plain text and must not be rewritten.
        template = "Format date:iso is the spec. {{ title }} {{ date:iso }}"
        assert render_template(template, ITEM) == (
            "Format date:iso is the spec. A Post Title 2024-01-15T09:30:00"
        )

    def test_normalize_handles_multiple_expressions(self):
        template = "{{ title }} | {{ date:short }} | {{ date:iso }}"
        assert render_template(template, ITEM) == (
            "A Post Title | 2024-01-15 | 2024-01-15T09:30:00"
        )

    def test_first_filter_on_empty_tags(self):
        item = dict(ITEM, tags=[])
        assert render_template("{{ item['tags'] | first }}", item) == ""

    def test_first_filter_on_present_tags(self):
        assert render_template("{{ item['tags'] | first }}", ITEM) == "python"


class TestValidation:
    def test_valid_template_passes(self):
        validate_template("{{ title }} {{ link }}")

    def test_unclosed_tag_raises(self):
        with pytest.raises(TemplateSyntaxError):
            validate_template("{% if summary %}oops")

    def test_unclosed_variable_raises(self):
        with pytest.raises(TemplateSyntaxError):
            validate_template("{{ title")

    def test_render_malformed_raises(self):
        with pytest.raises(TemplateSyntaxError):
            render_template("{% if summary %}", ITEM)


class TestSandbox:
    def test_dunder_access_renders_empty(self):
        # Jinja2 3.1.6 turns unsafe attribute access into an undefined that
        # renders empty; the protection is that chaining off it raises.
        assert render_template("{{ ''.__class__ }}", ITEM) == ""

    def test_mro_chain_blocked(self):
        with pytest.raises(SecurityError):
            render_template("{{ item.__class__.__mro__ }}", ITEM)

    def test_rce_chain_blocked(self):
        with pytest.raises(SecurityError):
            render_template("{{ [].__class__.__base__.__subclasses__() }}", ITEM)

    def test_mro_index_chain_blocked(self):
        with pytest.raises(SecurityError):
            render_template("{{ item.__class__.__mro__[1].__subclasses__() }}", ITEM)

    def test_plain_braces_not_an_expression(self):
        assert render_template("Plain { text } here", ITEM) == "Plain { text } here"

    def test_repeat_oversized_refused(self):
        # `{{ 'A' * 50000000 }}` rendered a 50 MB string in ~0.2s before the
        # cap; the `*` operator must be bounded before it allocates.
        with pytest.raises(SecurityError):
            render_template("{{ 'A' * 50000000 }}", ITEM)

    def test_nested_repeat_refused(self):
        # `{% set a = 'A' * 100000 %}{{ a * 100000 }}` requested ~10 GB.
        with pytest.raises(SecurityError):
            render_template("{% set a = 'A' * 100000 %}{{ a * 100000 }}", ITEM)

    def test_small_repeat_allowed(self):
        # The cap must not break legitimate repetition.
        assert render_template("{{ '-' * 5 }}", ITEM) == "-----"

    def test_output_backstop_refused(self):
        # 100000 iterations * 11 chars = 1.1M > the 1M output backstop,
        # exercised independent of the `*` operator.
        with pytest.raises(SecurityError):
            render_template(
                "{% for i in range(100000) %}xxxxxxxxxxx{% endfor %}", ITEM
            )
