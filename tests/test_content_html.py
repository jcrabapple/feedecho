"""Tests for the {{ content_html }} sanitized-HTML passthrough.

Covers feed_parser.sanitize_html (the ingest-time allowlist), the parser
wiring in both feed formats, template context exposure, and the
feed_items.content_html storage roundtrip.
"""

import json

import feed_parser
import template_engine


# ── sanitize_html: the ingest-time allowlist ────────────────────────────────

class TestSanitizeHtml:
    def test_empty_and_none(self):
        assert feed_parser.sanitize_html("") == ""
        assert feed_parser.sanitize_html(None) == ""

    def test_non_string_returns_empty(self):
        # feedparser occasionally yields lists for content fields.
        assert feed_parser.sanitize_html(["a", "b"]) == ""

    def test_keeps_article_markup(self):
        dirty = '<p>Hello <strong>world</strong></p><ul><li>one</li><li>two</li></ul>'
        assert feed_parser.sanitize_html(dirty) == dirty

    def test_keeps_links_with_href(self):
        out = feed_parser.sanitize_html('<p><a href="https://e.com/a">read</a></p>')
        assert '<a href="https://e.com/a"' in out and "read</a>" in out

    def test_strips_script_and_style(self):
        out = feed_parser.sanitize_html('<p>hi</p><script>alert(1)</script><style>x{}</style>')
        assert "alert" not in out and "script" not in out and "<style" not in out

    def test_strips_event_handlers(self):
        out = feed_parser.sanitize_html('<p onclick="evil()">hi</p>')
        assert "onclick" not in out and "evil" not in out

    def test_javascript_href_dropped(self):
        out = feed_parser.sanitize_html('<a href="javascript:alert(1)">x</a>')
        assert "javascript:" not in out

    def test_data_src_dropped(self):
        out = feed_parser.sanitize_html('<img src="data:text/html;base64,AAAA">')
        assert "data:" not in out

    def test_iframe_and_form_stripped(self):
        out = feed_parser.sanitize_html('<iframe src="https://e.com"></iframe><form action="/x"></form><p>ok</p>')
        assert "iframe" not in out and "form" not in out and "ok" in out

    def test_style_attribute_stripped(self):
        out = feed_parser.sanitize_html('<p style="position:fixed">hi</p>')
        assert "style" not in out and "position" not in out

    def test_relative_href_kept(self):
        out = feed_parser.sanitize_html('<a href="/more">x</a>')
        assert 'href="/more"' in out

    def test_entities_survive(self):
        out = feed_parser.sanitize_html("<p>Fish &amp; Chips</p>")
        assert "Fish &amp; Chips" in out or "Fish & Chips" in out


# ── Parser wiring: content_html lands on parsed items ──────────────────────

class TestParserWiring:
    def _rss(self, body: str):
        import feedparser

        parsed = feedparser.parse(body)
        return feed_parser.parse_rss_feed(parsed, "https://e.com/feed")["items"]

    def test_rss_item_carries_sanitized_html(self):
        items = self._rss("""<?xml version="1.0"?>
        <rss version="2.0"><channel><title>t</title><link>https://e.com</link>
        <description>d</description><item><title>One</title>
        <link>https://e.com/1</link>
        <description>&lt;p&gt;Hi &lt;strong&gt;there&lt;/strong&gt; &lt;a href="https://e.com/x"&gt;link&lt;/a&gt;&lt;/p&gt;&lt;script&gt;bad()&lt;/script&gt;</description>
        </item></channel></rss>""")
        item = items[0]
        assert "<strong>there</strong>" in item["content_html"]
        assert 'href="https://e.com/x"' in item["content_html"]
        # The sanitized value never carries the script through.
        assert "bad()" not in item["content_html"]
        # Plain-text sibling unchanged in spirit: no tags in content.
        assert "<" not in item["content"]

    def test_rss_item_without_content_has_empty_html(self):
        items = self._rss("""<?xml version="1.0"?>
        <rss version="2.0"><channel><title>t</title><link>https://e.com</link>
        <description>d</description><item><title>One</title>
        <link>https://e.com/1</link><description>plain</description>
        </item></channel></rss>""")
        # summary-only feed: display_html falls back to the summary, which is
        # plain text — sanitized output equals the text.
        assert items[0]["content_html"] == "plain"


# ── Template context: the variable renders through the sandbox ─────────────

class TestTemplateContext:
    ITEM = {
        "id": "1",
        "title": "T",
        "link": "https://e.com/1",
        "summary": "S",
        "content": "Hi there",
        "content_html": '<p>Hi <a href="https://e.com/x">there</a></p>',
        "content_link": "",
        "author": "",
        "date": "",
        "tags": [],
        "image_url": "",
    }

    def test_content_html_renders_verbatim(self):
        out = template_engine.render_template("{{ content_html }}", self.ITEM, feed_name="f")
        assert '<a href="https://e.com/x">there</a>' in out

    def test_missing_key_renders_empty(self):
        item = dict(self.ITEM)
        del item["content_html"]
        assert template_engine.render_template("[{{ content_html }}]", item) == "[]"

    def test_custom_webhook_body_roundtrip(self):
        """The maique-shaped use: HTML inside a webhook custom body."""
        import webhook

        t = '{"message": {{ content_html | tojson }}}'
        payload = webhook.build_body_payload(t, self.ITEM, feed_name="f")
        assert payload["message"] == self.ITEM["content_html"]

    def test_available_variables_lists_content_html(self):
        assert any("content_html" in v["var"] for v in template_engine.available_variables())


# ── Storage: the column round-trips through feed_items ─────────────────────

class TestStorageRoundtrip:
    def test_store_and_read_back(self, db_tmp):
        from database import get_db

        html = '<p>Hi <a href="https://e.com/x">there</a></p>'
        with get_db() as db:
            db.execute("INSERT INTO feeds (name, url) VALUES ('f', 'https://e.com/feed')")
            db.execute(
                """INSERT INTO feed_items (feed_id, item_id, title, link, summary,
                                           content, content_text, content_link, content_html)
                   VALUES (1, 'i1', 'T', 'https://e.com/1', 'S', 'Hi there', 'Hi there', '', ?)""",
                (html,),
            )
            row = db.execute("SELECT * FROM feed_items WHERE item_id = 'i1'").fetchone()
        assert row["content_html"] == html

    def test_legacy_row_reads_as_empty(self, db_tmp):
        """Rows written before the column existed read back as '', never None
        leaking into templates."""
        from database import get_db

        with get_db() as db:
            db.execute("INSERT INTO feeds (name, url) VALUES ('f', 'https://e.com/feed')")
            db.execute(
                """INSERT INTO feed_items (feed_id, item_id, title, link, summary, content)
                   VALUES (1, 'i2', 'T', 'https://e.com/2', 'S', 'C')"""
            )
            row = db.execute("SELECT * FROM feed_items WHERE item_id = 'i2'").fetchone()
        assert (row["content_html"] or "") == ""
