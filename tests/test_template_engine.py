"""Tests for the template engine."""

import pytest
from template_engine import render_template, available_variables


class TestRenderTemplate:
    def test_basic_title_link(self):
        template = "{{ title }} {{ link }}"
        item = {"title": "My Post", "link": "https://example.com/post"}
        result = render_template(template, item)
        assert result == "My Post https://example.com/post"

    def test_summary(self):
        template = "{{ title }}\n\n{{ summary }}"
        item = {"title": "My Post", "summary": "A short summary."}
        result = render_template(template, item)
        assert result == "My Post\n\nA short summary."

    def test_missing_field(self):
        template = "{{ title }} {{ author }}"
        item = {"title": "My Post"}
        result = render_template(template, item)
        assert result == "My Post "

    def test_date_iso(self):
        template = "{{ date:iso }}"
        item = {"date": "2024-01-15T09:30:00Z"}
        result = render_template(template, item)
        assert "2024-01-15" in result
        assert "09:30:00" in result

    def test_date_short(self):
        template = "{{ date:short }}"
        item = {"date": "2024-01-15T09:30:00Z"}
        result = render_template(template, item)
        assert result == "2024-01-15"

    def test_hashtags(self):
        template = "{{ title }} {{ hashtags }}"
        item = {"title": "My Post", "tags": ["python", "indieweb"]}
        result = render_template(template, item)
        assert result == "My Post #python #indieweb"

    def test_hashtags_strips_special_chars(self):
        template = "{{ hashtags }}"
        item = {"tags": ["C++ Programming", "web-dev!"]}
        result = render_template(template, item)
        assert "#CProgramming" in result
        assert "#webdev" in result

    def test_hashtags_dedupes_equivalent_tags(self):
        template = "{{ hashtags }}"
        item = {"tags": ["open source", "open-source", "AI", "ai", "AI/ML"]}
        result = render_template(template, item)
        assert result == "#opensource #AI #AIML"

    def test_hashtags_keeps_accented_letters(self):
        template = "{{ hashtags }}"
        item = {"tags": ["Attualità", "Économie", "Über"]}
        result = render_template(template, item)
        assert result == "#Attualità #Économie #Über"

    def test_hashtags_nfd_input_normalizes(self):
        # Decomposed form: "e" + combining acute accent (U+0301)
        template = "{{ hashtags }}"
        item = {"tags": ["e\u0301conomie"]}
        result = render_template(template, item)
        assert result == "#économie"
        assert "\u0301" not in result

    def test_hashtags_dedupes_accented_case_insensitively(self):
        template = "{{ hashtags }}"
        item = {"tags": ["Attualità", "ATTUALITÀ"]}
        result = render_template(template, item)
        assert result == "#Attualità"

    def test_hashtags_still_strips_punctuation_and_emoji(self):
        template = "{{ hashtags }}"
        item = {"tags": ["Café ☕ Time!", "naïve—café"]}
        result = render_template(template, item)
        assert result == "#CaféTime #naïvecafé"

    def test_hashtags_keeps_indic_and_thai_marks(self):
        template = "{{ hashtags }}"
        item = {"tags": ["नमस्ते", "เชียงใหม่"]}
        result = render_template(template, item)
        assert result == "#नमस्ते #เชียงใหม่"

    def test_hashtags_leading_mark_dropped(self):
        # A combining mark with no base character cannot start a hashtag
        template = "{{ hashtags }}"
        item = {"tags": ["\u0301abc"]}
        result = render_template(template, item)
        assert result == "#abc"

    def test_hashtags_dedupes_nfd_against_nfc(self):
        template = "{{ hashtags }}"
        item = {"tags": ["e\u0301conomie", "économie"]}
        result = render_template(template, item)
        assert result == "#économie"

    def test_no_hashtags_when_empty(self):
        template = "{{ title }} {{ hashtags }}"
        item = {"title": "My Post", "tags": []}
        result = render_template(template, item)
        assert result == "My Post "

    def test_unknown_variable(self):
        template = "{{ unknown_var }}"
        item = {"title": "My Post"}
        result = render_template(template, item)
        assert result == ""

    def test_whitespace_in_variable(self):
        template = "{{   title   }} {{link}}"
        item = {"title": "Test", "link": "https://example.com"}
        result = render_template(template, item)
        assert result == "Test https://example.com"

    def test_multiple_variables(self):
        template = "{{ author }}: {{ title }} ({{ date:short }})\n{{ link }}"
        item = {
            "author": "Jane",
            "title": "Hello World",
            "date": "2024-06-15T12:00:00Z",
            "link": "https://example.com/hello",
        }
        result = render_template(template, item)
        assert "Jane: Hello World (2024-06-15)" in result
        assert "https://example.com/hello" in result

    def test_empty_template(self):
        result = render_template("", {"title": "Test"})
        assert result == ""

    def test_no_variables(self):
        template = "Just plain text"
        result = render_template(template, {"title": "Test"})
        assert result == "Just plain text"


class TestAvailableVariables:
    def test_returns_list(self):
        variables = available_variables()
        assert isinstance(variables, list)
        assert len(variables) > 0

    def test_includes_title(self):
        variables = available_variables()
        assert any(v["var"] == "{{ title }}" for v in variables)

    def test_includes_link(self):
        variables = available_variables()
        assert any(v["var"] == "{{ link }}" for v in variables)

    def test_includes_date_iso(self):
        variables = available_variables()
        assert any(v["var"] == "{{ date_iso }}" for v in variables)


class TestDateFilters:
    def test_date_short_filter_iso(self):
        assert render_template(
            "{{ item.stamp | date_short }}", {"stamp": "2024-01-15T09:30:00Z"}
        ) == "2024-01-15"

    def test_date_iso_filter_iso(self):
        assert render_template(
            "{{ item.stamp | date_iso }}", {"stamp": "2024-01-15T09:30:00Z"}
        ) == "2024-01-15T09:30:00"

    def test_date_short_filter_rfc822(self):
        # Classic RSS pubDate / lastBuildDate shape
        assert render_template(
            "{{ item.stamp | date_short }}",
            {"stamp": "Thu, 31 Dec 2026 00:00:00 +0000"},
        ) == "2026-12-31"

    def test_date_iso_filter_rfc822(self):
        assert render_template(
            "{{ item.stamp | date_iso }}",
            {"stamp": "Sat, 26 Jul 2025 00:00:00 +0000"},
        ) == "2025-07-26T00:00:00"

    def test_unparseable_passthrough(self):
        assert render_template(
            "{{ item.stamp | date_short }}", {"stamp": "not-a-date"}
        ) == "not-a-date"

    def test_empty_input(self):
        assert render_template("{{ item.stamp | date_short }}", {"stamp": ""}) == ""
        assert render_template("{{ date | date_short }}", {}) == ""

    def test_filter_on_item_date_variable(self):
        # Context vars like date also work with the filter.
        assert render_template(
            "{{ date | date_short }}", {"date": "Thu, 31 Dec 2026 00:00:00 +0000"}
        ) == "2026-12-31"
