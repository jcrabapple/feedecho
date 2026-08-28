"""Feed-provided image alt text (Brian's request).

Feeds carry alt text for their images — Media RSS media:text, RSS enclosure
alt, JSON Feed 1.1 image caption, plain <img alt=""> in content — but the
parser only kept the URL, so posters either burned AI vision credits or
shipped empty descriptions. Now the parser extracts image_alt and the three
poster paths prefer it over AI generation.
"""

import pytest

import feed_parser


class TestRssAltExtraction:
    def test_media_rss_media_text(self):
        entry = {
            "media_content": [
                {"url": "https://e.com/pic.jpg", "media_text": [{"text": "A sunset over the harbor"}]}
            ],
        }
        assert feed_parser._extract_rss_image(entry) == "https://e.com/pic.jpg"
        assert feed_parser._extract_rss_image_alt(entry) == "A sunset over the harbor"

    def test_media_thumbnail_media_text(self):
        entry = {
            "media_thumbnail": [{"url": "https://e.com/thumb.jpg"}],
            "media_text": [{"text": "Thumbnail caption"}],
        }
        # media_text is entry-level in some feeds; the URL from media_thumbnail
        # has no same-object text, so alt is empty (never mismatches).
        assert feed_parser._extract_rss_image_alt(entry) == ""

    def test_media_object_without_text_yields_empty_not_next_source(self):
        """A media URL with no caption must not steal the content img's alt."""
        entry = {
            "media_content": [{"url": "https://e.com/pic.jpg"}],
            "summary": '<img src="https://e.com/other.jpg" alt="other image">',
        }
        assert feed_parser._extract_rss_image(entry) == "https://e.com/pic.jpg"
        assert feed_parser._extract_rss_image_alt(entry) == ""

    def test_content_img_alt(self):
        entry = {
            "summary": '<p>hi</p><img src="https://e.com/a.png" alt="A red barn in snow">',
        }
        assert feed_parser._extract_rss_image(entry) == "https://e.com/a.png"
        assert feed_parser._extract_rss_image_alt(entry) == "A red barn in snow"

    def test_img_without_alt_then_img_with_alt(self):
        """URL priority is first-img-wins; alt belongs to that same img."""
        entry = {
            "content": [{"value": '<img src="https://e.com/1.jpg"><img src="https://e.com/2.jpg" alt="Second image caption">'}],
        }
        assert feed_parser._extract_rss_image(entry) == "https://e.com/1.jpg"
        # The chosen img has no alt, so alt stays empty — a later image's
        # caption must never be attached to the earlier image's URL.
        assert feed_parser._extract_rss_image_alt(entry) == ""

    def test_alt_must_belong_to_chosen_url(self):
        """If the chosen img has no alt, no later caption is used for it."""
        entry = {
            "summary": '<img src="https://e.com/1.jpg"><img src="https://e.com/2.jpg" alt="caption for two">',
        }
        assert feed_parser._extract_rss_image(entry) == "https://e.com/1.jpg"
        assert feed_parser._extract_rss_image_alt(entry) == ""


class TestJsonFeedAltExtraction:
    def test_image_object_caption(self):
        entry = {"image": {"url": "https://e.com/cover.jpg", "caption": "Cover art for episode 12"}}
        assert feed_parser._extract_json_feed_image(entry) == "https://e.com/cover.jpg"
        assert feed_parser._extract_json_feed_image_alt(entry) == "Cover art for episode 12"

    def test_image_string_has_no_alt(self):
        entry = {"image": "https://e.com/cover.jpg"}
        assert feed_parser._extract_json_feed_image_alt(entry) == ""

    def test_banner_object_caption(self):
        entry = {"banner_image": {"url": "https://e.com/banner.png", "caption": "Banner: city skyline"}}
        assert feed_parser._extract_json_feed_image_alt(entry) == "Banner: city skyline"

    def test_content_html_img_alt(self):
        entry = {"content_html": '<img src="https://e.com/x.jpg" alt="Chart of results">'}
        assert feed_parser._extract_json_feed_image_alt(entry) == "Chart of results"

    def test_content_html_alt_belongs_to_chosen_img(self):
        """First img wins the URL; a later captioned img must not be used."""
        entry = {
            "content_html": '<img src="https://e.com/1.jpg"><img src="https://e.com/2.jpg" alt="two">',
        }
        assert feed_parser._extract_json_feed_image(entry) == "https://e.com/1.jpg"
        assert feed_parser._extract_json_feed_image_alt(entry) == ""

    def test_content_html_skips_srcless_imgs(self):
        """An <img> without src is skipped for URL; its alt never applies."""
        entry = {
            "content_html": '<img alt="no source"><img src="https://e.com/real.jpg" alt="the real one">',
        }
        assert feed_parser._extract_json_feed_image(entry) == "https://e.com/real.jpg"
        assert feed_parser._extract_json_feed_image_alt(entry) == "the real one"


class TestParseIncludesImageAlt:
    def test_rss_items_carry_image_alt(self):
        parsed = {
            "feed": {"title": "T"},
            "entries": [
                {"id": "1", "title": "e1", "link": "https://e.com/1",
                 "summary": '<img src="https://e.com/a.jpg" alt="Alt words here">'}
            ],
        }
        items = feed_parser.parse_rss_feed(parsed, "https://e.com/rss")["items"]
        assert items[0]["image_alt"] == "Alt words here"

    def test_json_items_carry_image_alt(self):
        data = {"items": [{"id": "1", "title": "e1", "url": "https://e.com/1",
                           "image": {"url": "https://e.com/i.jpg", "caption": "Episode cover"}}]}
        items = feed_parser.parse_json_feed(data)["items"]
        assert items[0]["image_alt"] == "Episode cover"


class TestSchedulerPrefersFeedAlt:
    """The three poster paths seed their description from item['image_alt']."""

    @pytest.fixture()
    def src(self):
        return (REPO := __import__("pathlib").Path(__file__).resolve().parent.parent) / "scheduler.py"

    def test_mastodon_path_seeds_from_image_alt(self, src):
        text = src.read_text(encoding="utf-8")
        m = re.search(r"description = \(item\.get\(\"image_alt\"\)", text)
        assert m, "Mastodon path must seed description from item['image_alt']"
        # AI is the fallback branch, not the default
        assert re.search(r'elif alt_text\.is_enabled\([^\n]*\n\s*try:\n\s*description = alt_text\.generate_alt_text', text)

    def test_bluesky_path_seeds_from_image_alt(self, src):
        text = src.read_text(encoding="utf-8")
        assert 'alt_description = (item.get("image_alt") or "").strip()' in text

    def test_microblog_path_seeds_from_image_alt(self, src):
        text = src.read_text(encoding="utf-8")
        assert 'photo_alt = (item.get("image_alt") or "").strip()' in text
        # AI generation is skipped entirely when the feed supplied alt text
        assert "if not photo_alt and alt_text.is_enabled(" in text


import re  # noqa: E402  (used by the source-level checks above)
