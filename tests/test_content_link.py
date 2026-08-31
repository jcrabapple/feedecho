"""First outbound link in content ({{ content_link }}).

Link-blogs (pika.page, Micro.blog, etc.) wrap the headline in an <a>
pointing at the external article. strip_html() drops that href while
keeping the anchor text, so {{ content }} lost the link. The parser now
recovers the first content link as content_link, exposed to templates
and the webhook payload.
"""

import feed_parser
from template_engine import available_variables, render_template


class TestExtractFirstLink:
    def test_extracts_first_anchor_href(self):
        html_str = (
            '<p><a href="https://www.wsj.com/politics/foo">Headline</a></p>'
            "<p>Of course the one person would be George Santos…</p>"
        )
        assert feed_parser._extract_first_link(html_str) == "https://www.wsj.com/politics/foo"

    def test_href_not_first_attribute(self):
        html_str = '<a class="x" rel="nofollow" href="https://e.com/a">t</a>'
        assert feed_parser._extract_first_link(html_str) == "https://e.com/a"

    def test_no_link_returns_empty(self):
        assert feed_parser._extract_first_link("<p>just text</p>") == ""

    def test_empty_input(self):
        assert feed_parser._extract_first_link("") == ""
        assert feed_parser._extract_first_link(None) == ""

    def test_html_entity_in_href(self):
        html_str = '<a href="https://e.com/?a=1&amp;b=2">t</a>'
        assert feed_parser._extract_first_link(html_str) == "https://e.com/?a=1&b=2"

    def test_protocol_relative_href(self):
        assert feed_parser._extract_first_link('<a href="//e.com/a">t</a>') == "https://e.com/a"

    def test_relative_href_resolved_against_base(self):
        html_str = '<a href="article-123">t</a>'
        assert (
            feed_parser._extract_first_link(html_str, base_url="https://e.com/posts/foo")
            == "https://e.com/posts/article-123"
        )

    def test_first_link_wins(self):
        html_str = '<a href="https://e.com/1">one</a> <a href="https://e.com/2">two</a>'
        assert feed_parser._extract_first_link(html_str) == "https://e.com/1"

    def test_data_href_suffix_not_mistaken_for_href(self):
        html_str = '<a data-href="https://tracker.example/x" href="https://real.example/y">t</a>'
        assert feed_parser._extract_first_link(html_str) == "https://real.example/y"


class TestParseCarriesContentLink:
    def test_rss_link_post(self):
        parsed = {
            "feed": {"title": "T"},
            "entries": [
                {
                    "id": "1",
                    "title": "Ex-Congressman George Santos Receives Kalshi's First-Ever Lifetime Ban...",
                    "link": "https://itsbrian.lol/posts/foo",
                    "content": [
                        {
                            "value": (
                                '<p><a href="https://www.wsj.com/politics/foo">Headline</a></p>'
                                "<p>Of course the one person would be George Santos…</p>"
                            )
                        }
                    ],
                }
            ],
        }
        items = feed_parser.parse_rss_feed(parsed, "https://itsbrian.lol/posts_feed")["items"]
        assert items[0]["content_link"] == "https://www.wsj.com/politics/foo"
        # content keeps the anchor text (the href is dropped from the body)
        assert "Headline" in items[0]["content"]
        assert "Of course the one person" in items[0]["content"]
        assert "wsj.com" not in items[0]["content"]

    def test_rss_no_content_link(self):
        parsed = {
            "feed": {"title": "T"},
            "entries": [
                {
                    "id": "1",
                    "title": "e1",
                    "link": "https://e.com/1",
                    "content": [{"value": "<p>no links here</p>"}],
                }
            ],
        }
        items = feed_parser.parse_rss_feed(parsed, "https://e.com/rss")["items"]
        assert items[0]["content_link"] == ""

    def test_rss_content_missing_uses_summary(self):
        # No content element: content falls back to summary, content_link is "".
        parsed = {
            "feed": {"title": "T"},
            "entries": [
                {"id": "1", "title": "e1", "link": "https://e.com/1", "summary": "Just a summary"}
            ],
        }
        items = feed_parser.parse_rss_feed(parsed, "https://e.com/rss")["items"]
        assert items[0]["content"] == "Just a summary"
        assert items[0]["content_link"] == ""

    def test_json_feed_link_post(self):
        data = {
            "items": [
                {
                    "id": "1",
                    "title": "e1",
                    "url": "https://itsbrian.lol/posts/foo",
                    "content_html": (
                        '<p><a href="https://www.wsj.com/politics/foo">Headline</a></p>'
                        "<p>Comment</p>"
                    ),
                }
            ]
        }
        items = feed_parser.parse_json_feed(data)["items"]
        assert items[0]["content_link"] == "https://www.wsj.com/politics/foo"


class TestTemplateRendersContentLink:
    def test_content_link_variable(self):
        item = {
            "title": "T",
            "content": "Headline Comment",
            "content_link": "https://www.wsj.com/politics/foo",
        }
        assert (
            render_template("{{ content }} {{ content_link }}", item)
            == "Headline Comment https://www.wsj.com/politics/foo"
        )

    def test_content_link_empty_when_missing(self):
        assert render_template("{{ content_link }}", {"title": "T"}) == ""

    def test_available_variables_lists_content_link(self):
        assert any(v["var"] == "{{ content_link }}" for v in available_variables())
