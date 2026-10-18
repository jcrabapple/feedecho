"""Tests for F3: clean_url / utm / hashtags template filters (template_engine.py).

Unit-level only: no database imports, pure render/filter calls.
"""

import pytest

import template_engine
from template_engine import (
    render_template,
    validate_template,
)


class TestCleanUrl:
    def test_strips_tracking_and_preserves_rest(self):
        url = ("https://example.com/post/a?utm_source=rss&keep=1&id=42"
               "&fbclid=abc#frag")
        out = render_template("{{ link | clean_url }}",
                              {"link": url})
        assert out == "https://example.com/post/a?keep=1&id=42#frag"

    def test_byte_identity_when_nothing_to_strip(self):
        url = "https://video.example/watch?v=dQw4w9WgXcQ&t=1m2s"
        out = render_template("{{ link | clean_url }}", {"link": url})
        assert out == url

    def test_extra_kwarg_comma_string(self):
        url = "https://example.com/x?ref=news&keep=1"
        out = render_template("{{ link | clean_url('ref,newsletter') }}",
                              {"link": url})
        assert out == "https://example.com/x?keep=1"

    def test_extra_kwarg_list(self):
        out = template_engine._filter_clean_url(
            "https://example.com/x?ref=news&keep=1", ["REF"])
        assert out == "https://example.com/x?keep=1"

    def test_utm_prefix_generic(self):
        url = "https://example.com/x?utm_foo=1&keep=1"
        out = render_template("{{ link | clean_url }}", {"link": url})
        assert out == "https://example.com/x?keep=1"

    def test_encoded_key_matches(self):
        # Percent-encoded key normalizes to utm_source before matching.
        out = template_engine._filter_clean_url(
            "https://example.com/x?%75tm_source=a&keep=1")
        assert out == "https://example.com/x?keep=1"

    def test_semicolon_separator_piece_preserved(self):
        url = "https://example.com/x?a=1;b=2&utm_source=rss"
        out = template_engine._filter_clean_url(url)
        assert out == "https://example.com/x?a=1;b=2"

    def test_empty_input_unchanged(self):
        assert template_engine._filter_clean_url("") == ""

    def test_garbage_string_unchanged(self):
        garbage = "not a url at all"
        assert template_engine._filter_clean_url(garbage) == garbage

    def test_relative_path_unchanged(self):
        assert template_engine._filter_clean_url("/docs/page?a=1") == \
            "/docs/page?a=1"

    def test_none_input_unchanged(self):
        assert template_engine._filter_clean_url(None) is None


class TestUtm:
    def test_positional_source_appends(self):
        url = "https://example.com/post"
        out = render_template("{{ link | utm('mastodon') }}", {"link": url})
        assert out == "https://example.com/post?utm_source=mastodon"

    def test_named_kwargs(self):
        url = "https://example.com/post"
        out = render_template(
            "{{ link | utm(source='m', medium='feedecho', campaign='blog',"
            " term='t', content='c') }}", {"link": url})
        assert out == ("https://example.com/post?utm_source=m"
                       "&utm_medium=feedecho&utm_campaign=blog"
                       "&utm_term=t&utm_content=c")

    def test_merges_over_existing_query(self):
        url = "https://example.com/p?utm_source=rss&keep=1#frag"
        out = template_engine._filter_utm(url, source="mastodon")
        assert out == "https://example.com/p?keep=1&utm_source=mastodon#frag"

    def test_untouched_others_keep_order_then_utms(self):
        url = "https://example.com/p?a=1&utm_medium=old&b=2"
        out = template_engine._filter_utm(url, source="m", medium="new")
        assert out == "https://example.com/p?a=1&b=2&utm_source=m&utm_medium=new"

    def test_no_kwargs_byte_identical(self):
        url = "https://example.com/p?a=1&utm_source=rss#f"
        out = render_template("{{ link | utm }}", {"link": url})
        assert out == url

    def test_empty_string_values_skipped(self):
        url = "https://example.com/p"
        out = template_engine._filter_utm(url, source="m", medium="")
        assert out == "https://example.com/p?utm_source=m"

    def test_garbage_unchanged(self):
        assert template_engine._filter_utm("not a url", source="m") == "not a url"

    def test_blank_value_query_preserved(self):
        url = "https://example.com/p?blank="
        out = template_engine._filter_utm(url, source="m")
        assert out == "https://example.com/p?blank=&utm_source=m"


class TestHashtagsFilter:
    def test_list_with_exclude_mixed_shapes(self):
        out = render_template(
            "{{ tags | hashtags(exclude='internal, heartbeat') }}",
            {"tags": ["release notes", "Internal", "posse", "open source"]})
        assert out == "#releasenotes #posse #opensource"

    def test_string_input(self):
        out = render_template("{{ tags | hashtags }}",
                              {"tags": "release notes, posse"})
        assert out == "#releasenotes #posse"

    def test_semicolon_and_space_separated(self):
        assert template_engine._filter_hashtags("a;b") == "#a #b"
        assert template_engine._filter_hashtags("a b c") == "#a #b #c"
        # With explicit separators, internal whitespace stays part of the
        # tag ("release notes" -> #releasenotes).
        assert template_engine._filter_hashtags("a; b c,d") == "#a #bc #d"

    def test_tuple_input(self):
        out = template_engine._filter_hashtags(("x", "y"))
        assert out == "#x #y"

    def test_empty_input_empty_string(self):
        assert template_engine._filter_hashtags("") == ""
        assert template_engine._filter_hashtags([]) == ""

    def test_unknown_type_empty_string(self):
        assert template_engine._filter_hashtags(42) == ""
        assert template_engine._filter_hashtags({"a": 1}) == ""

    def test_all_excluded_empty_string(self):
        assert template_engine._filter_hashtags(
            ["internal"], exclude="internal") == ""

    def test_dedupe_and_punct_via_format_hashtags(self):
        out = template_engine._filter_hashtags(["Open-Source", "open source"])
        # _format_hashtags dedupes case-insensitively, keeping first-seen form.
        assert out == "#OpenSource"


class TestContextHashtagsUnchanged:
    def test_context_var_still_works(self):
        item = {"tags": ["one", "two"]}
        out = render_template("{{ hashtags }}", item)
        assert out == "#one #two"

    def test_context_var_and_filter_coexist(self):
        item = {"tags": ["one", "two"]}
        out = render_template("{{ hashtags }}|{{ tags | hashtags(exclude='two') }}",
                              item)
        assert out == "#one #two|#one"


class TestRegistration:
    def test_filters_registered(self):
        for name in ("clean_url", "utm", "hashtags"):
            assert name in template_engine.env.filters

    def test_available_variables_rows(self):
        rows = template_engine.available_variables()
        vars_ = [r["var"] for r in rows]
        assert "{{ link | clean_url }}" in vars_
        assert "{{ link | utm('mastodon') }}" in vars_
        assert ("{{ item['tags'] | hashtags(exclude='internal,heartbeat') }}"
                in vars_)

    def test_templates_validate(self):
        validate_template("{{ link | clean_url }} {{ link | utm('m') }} "
                          "{{ tags | hashtags(exclude='x') }}")


class TestNoDatabase:
    def test_module_does_not_import_database(self):
        assert "database" not in template_engine.__dict__


def test_merges_over_existing_uppercase_utm_keys():
    """A URL already carrying ?UTM_SOURCE=... dedupes case-insensitively so
    both spellings cannot coexist after the merge (gate F-11)."""
    result = render_template(
        "{{ link | utm('mastodon') }}",
        {"link": "https://ex.com/p?UTM_SOURCE=newsletter&utm_medium=feed"},
    )
    assert result.count("utm_source=") + result.count("UTM_SOURCE=") == 1
    assert "utm_source=mastodon" in result
