"""Feed autodiscovery (F4): <link rel="alternate"> resolution in feed_parser.

Reuses the test_conditional_get.py seam: monkeypatch ssrf_client so no real
DNS/socket happens, and monkeypatch _fetch_with_redirect_validation to hand
back canned (body, content-type, meta) tuples.
"""

from unittest import mock

import httpx
import pytest

import feed_parser

PAGE_ONE_LINK = (
    b"<html><head><title>Example</title>"
    b'<link rel="alternate" type="application/rss+xml" href="/feed.xml">'
    b"</head><body></body></html>"
)

PAGE_THREE_LINKS = (
    b"<html><head>"
    b'<link rel="alternate" type="application/atom+xml" href="/atom.xml">'
    b'<link rel="alternate" type="application/feed+json" href="/feed.json">'
    b'<link rel="alternate" type="application/rss+xml" href="/rss.xml">'
    b"</head></html>"
)

PAGE_NO_LINKS = b"<html><head><title>My Blog</title></head><body>hi</body></html>"

RSS_BODY = (
    b'<?xml version="1.0"?><rss version="2.0"><channel><title>RSS Title</title>'
    b"<item><title>i1</title></item></channel></rss>"
)


def _install(monkeypatch, body, content_type="text/html"):
    """Patch the network seam so discover_feed_url() gets canned bytes."""
    meta = {"status": 200, "etag": None, "last_modified": None, "final_url": "https://example.com/"}

    def fake_fetch(client, url, headers, max_bytes=0, backend=None):
        return body, content_type, meta

    monkeypatch.setattr(
        feed_parser, "ssrf_client", lambda urls, **kw: (mock.MagicMock(), mock.MagicMock())
    )
    monkeypatch.setattr(feed_parser, "_fetch_with_redirect_validation", fake_fetch)


class TestHTMLDiscovery:
    def test_single_rss_link_resolves(self, monkeypatch):
        _install(monkeypatch, PAGE_ONE_LINK, "text/html; charset=utf-8")
        result = feed_parser.discover_feed_url("https://example.com/")
        assert result["found"] is True
        assert result["url"] == "https://example.com/feed.xml"
        assert result["alternates"] == ["https://example.com/feed.xml"]
        assert result["title"] == "Example"

    def test_three_links_document_order_best_first(self, monkeypatch):
        _install(monkeypatch, PAGE_THREE_LINKS, "text/html")
        result = feed_parser.discover_feed_url("https://example.com/")
        assert result["url"] == "https://example.com/atom.xml"
        assert result["alternates"] == [
            "https://example.com/atom.xml",
            "https://example.com/feed.json",
            "https://example.com/rss.xml",
        ]

    def test_no_links_returns_not_found_with_page_title(self, monkeypatch):
        _install(monkeypatch, PAGE_NO_LINKS, "text/html")
        result = feed_parser.discover_feed_url("https://example.com/")
        assert result == {
            "found": False,
            "url": None,
            "title": "My Blog",
            "alternates": [],
        }

    def test_alternate_without_type_skipped_and_non_alternate_rel_skipped(self, monkeypatch):
        page = (
            b"<html><head>"
            b'<link rel="alternate" href="/alternate-feed">'
            b'<link rel="stylesheet" type="application/rss+xml" href="/not-a-feed.xml">'
            b'<link rel="alternate" type="application/rss+xml" href="/rss.xml">'
            b"</head></html>"
        )
        _install(monkeypatch, page, "text/html")
        result = feed_parser.discover_feed_url("https://example.com/")
        assert result["alternates"] == ["https://example.com/rss.xml"]

    def test_relative_hrefs_resolved_against_page_url(self, monkeypatch):
        page = (
            b"<html><head>"
            b'<link rel="alternate" type="application/rss+xml" href="../feed.xml">'
            b'<link rel="alternate" type="application/atom+xml" '
            b'href="https://example.com/atom">'
            b'<link rel="alternate" type="application/feed+json" href="/root.json">'
            b"</head></html>"
        )
        _install(monkeypatch, page, "text/html")
        result = feed_parser.discover_feed_url("https://example.com/blog/")
        assert result["alternates"] == [
            "https://example.com/feed.xml",
            "https://example.com/atom",
            "https://example.com/root.json",
        ]

    def test_entity_escaped_href_parsed(self, monkeypatch):
        page = (
            b"<html><head>"
            b'<link rel="alternate" type="application/rss+xml" '
            b'href="/feed.xml?utm=1&amp;token=a%2Bb">'
            b"</head></html>"
        )
        _install(monkeypatch, page, "text/html")
        result = feed_parser.discover_feed_url("https://example.com/")
        # convert_charrefs decodes &amp; in attribute values
        assert result["url"] == "https://example.com/feed.xml?utm=1&token=a%2Bb"

    def test_duplicate_hrefs_deduped_first_wins(self, monkeypatch):
        page = (
            b"<html><head>"
            b'<link rel="alternate" type="application/rss+xml" href="/feed.xml">'
            b'<link rel="alternate" type="application/rss+xml" href="/feed.xml/">'
            b"</head></html>"
        )
        _install(monkeypatch, page, "text/html")
        result = feed_parser.discover_feed_url("https://example.com/")
        assert result["alternates"] == ["https://example.com/feed.xml"]

    def test_ssrf_blocked_alternate_skipped_valid_used(self, monkeypatch):
        page = (
            b"<html><head>"
            b'<link rel="alternate" type="application/rss+xml" '
            b'href="http://127.0.0.1/feed.xml">'
            b'<link rel="alternate" type="application/rss+xml" href="/safe.xml">'
            b"</head></html>"
        )
        _install(monkeypatch, page, "text/html")
        result = feed_parser.discover_feed_url("https://example.com/")
        assert result["found"] is True
        assert result["url"] == "https://example.com/safe.xml"
        assert result["alternates"] == ["https://example.com/safe.xml"]

    def test_private_input_url_raises_ssrferror(self, monkeypatch):
        with pytest.raises(feed_parser.SSRFError):
            feed_parser.discover_feed_url("http://127.0.0.1/feed.xml")

    def test_non_html_feed_returned_unchanged(self, monkeypatch):
        _install(monkeypatch, RSS_BODY, "application/rss+xml")
        result = feed_parser.discover_feed_url("https://example.com/feed.xml")
        assert result["found"] is True
        assert result["url"] == "https://example.com/feed.xml"
        assert result["title"] == "RSS Title"
        assert result["alternates"] == []


class TestSizeCap:
    def test_oversized_content_length_raises_valueerror(self):
        resp = httpx.Response(
            200,
            headers={"content-type": "text/html", "content-length": str(10 * 1024 * 1024)},
            request=httpx.Request("GET", "https://example.com/"),
        )
        client = mock.MagicMock()
        client.stream.return_value = _Ctx(resp)

        with pytest.raises(ValueError, match="too large"):
            feed_parser._fetch_with_redirect_validation(
                client, "https://example.com/", {}, max_bytes=1024
            )


class _Ctx:
    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return self._response

    def __exit__(self, *exc):
        return False


def test_link_type_with_mime_parameters_still_matches():
    """<link type="application/rss+xml; charset=utf-8"> is a valid feed link
    (gate F-06): MIME parameters are stripped before the type check."""
    from feed_parser import _DiscoveryHTMLParser

    parser = _DiscoveryHTMLParser()
    parser.feed(
        '<!DOCTYPE html><html><head>'
        '<title>The Page</title>'
        '<link rel="alternate" type="application/rss+xml; charset=utf-8" href="/feed.xml">'
        '<link rel="alternate" type="application/atom+xml; CHARSET=UTF-8" href="/atom.xml">'
        '</head></html>'
    )
    mimes = [t for _, t, _ in parser.links]
    assert any(m.strip().lower().startswith("application/rss+xml") for m in mimes)
    assert any(m.strip().lower().startswith("application/atom+xml") for m in mimes)
    assert "The Page" == parser.title.strip()


def test_later_title_elements_do_not_pollute_page_title():
    """Only the first <title> in the document is the page title; inline SVG
    titles must not append to it (gate F-07)."""
    from feed_parser import _DiscoveryHTMLParser

    parser = _DiscoveryHTMLParser()
    parser.feed(
        "<html><head><title>The Page</title></head>"
        "<body><svg><title>GitHub logo</title></svg>"
        "<svg><title>Another icon</title></svg></body></html>"
    )
    assert parser.title == "The Page"
