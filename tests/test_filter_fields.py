"""Tests for F1: field-scoped echo filter clauses (tag:, author:, url:/link:, content:)."""

import pytest


def _item(**overrides):
    item = {
        "id": "item-1",
        "title": "Weekly giveaway roundup",
        "link": "https://example.com/post/1",
        "summary": "Our weekly roundup of giveaway deals.",
        "author": "jason",
        "tags": ["release notes", "posse"],
        "categories": {"audience": "internal", "kind": "notes"},
        "content": "<p>Deep dive content</p>",
        "content_text": "Deep dive content text",
    }
    item.update(overrides)
    return item


def test_prefix_parse_mixed():
    from filters import parse_keywords, split_clause

    parts = parse_keywords("tag:posse, giveaway")
    fields = [split_clause(p) for p in parts]
    assert fields == [("tag", "posse"), (None, "giveaway")]


def test_exclude_tag_matches_tags_list():
    from filters import is_filtered

    assert is_filtered(_item(), "tag:posse", "exclude") is True
    assert is_filtered(_item(tags=["release notes"]), "tag:posse", "exclude") is False


def test_exclude_tag_matches_categories_dict_values():
    from filters import is_filtered

    assert is_filtered(_item(tags=[]), "tag:internal", "exclude") is True


def test_exclude_author_scoped_to_author_field():
    from filters import is_filtered

    assert is_filtered(_item(), "author:jason", "exclude") is True
    # summary mentions 'jason' but author field does not
    assert is_filtered(_item(author="someone", summary="jason wrote this"), "author:jason", "exclude") is False


def test_exclude_url_matches_link_substring():
    from filters import is_filtered

    assert is_filtered(_item(), "url:/post/1", "exclude") is True
    assert is_filtered(_item(link="https://example.com/other"), "url:/post/1", "exclude") is False


def test_link_alias_for_url():
    from filters import is_filtered

    assert is_filtered(_item(), "link:/post/1", "exclude") is True
    assert is_filtered(_item(), "link:/post/1", "exclude") == is_filtered(_item(), "url:/post/1", "exclude")


def test_exclude_content_scoped():
    from filters import is_filtered

    assert is_filtered(_item(), "content:deep dive", "exclude") is True
    assert is_filtered(_item(), "content:content text", "exclude") is True
    # title-only text is NOT searched by content:
    assert is_filtered(_item(summary="", content="", content_text=""), "content:giveaway", "exclude") is False
    # content_html alone is deliberately not searched
    assert is_filtered(_item(summary="", content="", content_text=""), "content:deep", "exclude") is False


def test_exclude_tags_as_string_not_iterated():
    """tags as raw string matches as one tag, not per-character tokens."""
    from filters import is_filtered, split_clause

    assert split_clause("tag:release notes")[0] == "tag"
    assert is_filtered(_item(tags="release notes", categories=None), "tag:release notes", "exclude") is True
    assert is_filtered(_item(tags="release notes"), "tag:onent", "exclude") is False


def test_unknown_prefix_stays_bare():
    from filters import is_filtered, split_clause

    item = _item(title="Version release:2 notes", summary="", tags=[])
    # formerly a real keyword shape: plain substring over title/summary/description
    assert is_filtered(item, "release:2", "exclude") is True
    assert is_filtered(item, "release:9", "exclude") is False
    assert split_clause("release:2") == (None, "release:2")


def test_empty_value_falls_back_to_bare():
    from filters import is_filtered, split_clause

    # 'tag:' with nothing after behaves as the bare keyword "tag:"
    item = _item(title="tag: talk", summary="", tags=[])
    assert is_filtered(item, "tag:", "exclude") is True
    item2 = _item(title="no match here", summary="", tags=[])
    assert is_filtered(item2, "tag:", "exclude") is False
    assert split_clause("tag:") == (None, "tag:")


def test_include_mode_with_field_clause():
    from filters import is_filtered

    assert is_filtered(_item(), "tag:posse", "include") is False  # kept
    assert is_filtered(_item(tags=["other"]), "tag:posse", "include") is True  # dropped


def test_include_miss_reason_exact():
    from filters import match_reason

    assert match_reason(_item(tags=["other"]), "tag:posse", "include") == "no include keyword matched"


def test_exclude_hit_reason_strings():
    from filters import match_reason

    assert match_reason(_item(), "giveaway", "exclude") == 'matched "giveaway"'
    assert match_reason(_item(), "tag:posse", "exclude") == 'matched tag "posse"'


def test_combined_clauses_any_match_suppresses():
    from filters import is_filtered

    assert is_filtered(_item(), "tag:posse, author:nobody", "exclude") is True
    assert is_filtered(_item(), "tag:nope, author:jason", "exclude") is True
    assert is_filtered(_item(), "tag:nope, author:nobody", "exclude") is False
