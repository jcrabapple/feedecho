"""Tests for the feed parser state tracking and item parsing."""

import pytest
from feed_parser import (
    get_new_items,
    clean_text,
    strip_html,
    truncate,
    _extract_rss_image,
    _extract_rss_image_alt,
    _extract_rss_images,
)


class TestGetNewItems:
    def test_no_last_seen_returns_empty(self):
        """First run should return empty list — don't post backlog."""
        items = [{"id": "1"}, {"id": "2"}, {"id": "3"}]
        assert get_new_items(items, None) == []

    def test_finds_new_items(self):
        items = [{"id": "3"}, {"id": "2"}, {"id": "1"}]
        new = get_new_items(items, "1")
        assert len(new) == 2
        assert new[0]["id"] == "2"
        assert new[1]["id"] == "3"

    def test_no_new_items(self):
        """When last_seen is the newest item, nothing is new."""
        items = [{"id": "2"}, {"id": "1"}]
        new = get_new_items(items, "2")
        assert new == []

    def test_last_seen_not_in_feed_returns_newest_only(self):
        """If cursor scrolled off feed, only post newest item to prevent spam."""
        items = [{"id": "5"}, {"id": "4"}, {"id": "3"}, {"id": "2"}, {"id": "1"}]
        new = get_new_items(items, "old_id")
        assert len(new) == 1
        assert new[0]["id"] == "5"

    def test_empty_items(self):
        assert get_new_items([], "last") == []

    def test_single_item_feed(self):
        items = [{"id": "1"}]
        new = get_new_items(items, "1")
        assert new == []

    def test_new_items_returned_oldest_first(self):
        """Items should be posted oldest-first (reversed from feed order)."""
        items = [{"id": "5"}, {"id": "4"}, {"id": "3"}, {"id": "2"}, {"id": "1"}]
        new = get_new_items(items, "1")
        assert [item["id"] for item in new] == ["2", "3", "4", "5"]


class TestCleanText:
    def test_strips_html_tags(self):
        assert clean_text("<p>Hello world</p>") == "Hello world"

    def test_strips_nested_tags(self):
        assert clean_text("<div><p>Hello <b>world</b></p></div>") == "Hello world"

    def test_normalizes_whitespace(self):
        assert clean_text("Hello   world\n\n  test") == "Hello world test"

    def test_empty_string(self):
        assert clean_text("") == ""

    def test_none_returns_empty(self):
        assert clean_text(None) == ""

    def test_plain_text_unchanged(self):
        assert clean_text("Just text") == "Just text"

    def test_decodes_entities(self):
        assert clean_text("Tom &amp; Jerry") == "Tom & Jerry"

    def test_decodes_numeric_entities(self):
        assert clean_text("&#8217;") == "\u2019"


class TestStripHtml:
    def test_strips_scripts(self):
        html = "<p>Hello</p><script>alert('xss')</script><p>World</p>"
        result = strip_html(html)
        assert "alert" not in result
        assert "Hello" in result
        assert "World" in result

    def test_strips_styles(self):
        html = "<style>body { color: red; }</style><p>Content</p>"
        result = strip_html(html)
        assert "color" not in result
        assert "Content" in result

    def test_empty_string(self):
        assert strip_html("") == ""

    def test_strips_all_tags(self):
        html = "<p>Hello <a href='link'>world</a></p>"
        result = strip_html(html)
        assert "Hello" in result
        assert "world" in result
        assert "<" not in result

    def test_decodes_entities(self):
        assert strip_html("&lt;b&gt;text&lt;/b&gt;") == "<b>text</b>"


class TestTruncate:
    def test_short_text_unchanged(self):
        assert truncate("short text") == "short text"

    def test_truncates_long_text(self):
        long_text = "x" * 600
        result = truncate(long_text, 500)
        assert len(result) == 500
        assert result.endswith("…")

    def test_custom_max_len(self):
        result = truncate("hello world", 5)
        assert len(result) == 5


class TestExtractRssImageMediaTypeFilter:
    """A video media:content entry must never be picked as the item's image.

    Regression coverage for the primary-image picker (_extract_rss_image /
    _extract_rss_image_alt) not filtering media_content/media_thumbnail by
    medium/type, unlike the sibling _extract_rss_images which already does.
    """

    def test_skips_leading_video_and_picks_image(self):
        entry = {
            "media_content": [
                {
                    "url": "https://example.com/clip.mp4",
                    "medium": "video",
                    "type": "video/mp4",
                },
                {
                    "url": "https://example.com/photo.jpg",
                    "medium": "image",
                    "type": "image/jpeg",
                },
            ]
        }
        assert _extract_rss_image(entry) == "https://example.com/photo.jpg"
        assert _extract_rss_images(entry) == [
            {"url": "https://example.com/photo.jpg", "alt": ""}
        ]

    def test_alt_text_belongs_to_the_image_not_the_video(self):
        entry = {
            "media_content": [
                {
                    "url": "https://example.com/clip.mp4",
                    "medium": "video",
                    "type": "video/mp4",
                    "media_text": [{"text": "video caption"}],
                },
                {
                    "url": "https://example.com/photo.jpg",
                    "medium": "image",
                    "type": "image/jpeg",
                    "media_text": [{"text": "photo caption"}],
                },
            ]
        }
        assert _extract_rss_image(entry) == "https://example.com/photo.jpg"
        assert _extract_rss_image_alt(entry) == "photo caption"

    def test_all_video_media_yields_no_image(self):
        """Consistent with _extract_rss_images: no fallback to a video URL."""
        entry = {
            "media_content": [
                {
                    "url": "https://example.com/clip.mp4",
                    "medium": "video",
                    "type": "video/mp4",
                },
            ]
        }
        assert _extract_rss_image(entry) == ""
        assert _extract_rss_image_alt(entry) == ""
        assert _extract_rss_images(entry) == []

    def test_video_by_type_only_is_also_skipped(self):
        """type= is enough to disqualify even without an explicit medium=video."""
        entry = {
            "media_content": [
                {"url": "https://example.com/clip.mp4", "type": "video/mp4"},
                {"url": "https://example.com/photo.jpg", "type": "image/jpeg"},
            ]
        }
        assert _extract_rss_image(entry) == "https://example.com/photo.jpg"

    def test_media_thumbnail_also_filters_video(self):
        entry = {
            "media_thumbnail": [
                {
                    "url": "https://example.com/clip-thumb.mp4",
                    "medium": "video",
                    "type": "video/mp4",
                },
                {
                    "url": "https://example.com/thumb.jpg",
                    "medium": "image",
                    "type": "image/jpeg",
                },
            ]
        }
        assert _extract_rss_image(entry) == "https://example.com/thumb.jpg"
