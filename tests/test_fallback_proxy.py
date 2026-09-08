"""Tests for automatic fallback proxy triggering on 403 / 429."""

from unittest import mock
import httpx
import pytest

import feed_parser
import settings


class TestFallbackProxy:
    @pytest.fixture(autouse=True)
    def _mock_dns(self, monkeypatch):
        # Allow fake hostnames in unit tests
        monkeypatch.setattr(feed_parser, "validate_outbound_url", lambda u: u)
        monkeypatch.setattr(
            feed_parser,
            "ssrf_client",
            lambda urls, timeout=30: (httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))), None),
        )

    def test_fetch_feed_falls_back_on_403(self, monkeypatch):
        monkeypatch.setattr(
            settings, "FALLBACK_PROXY_URL", "https://proxy.example.com"
        )
        monkeypatch.setattr(settings, "FALLBACK_PROXY_SECRET", "test-secret")

        direct_response = httpx.Response(403, request=httpx.Request("GET", "https://origin.example/rss"))
        proxy_feed_xml = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Proxied Feed</title><item><title>Item 1</title><link>https://origin.example/1</link></item></channel></rss>"""

        calls = []

        def _fake_fetch(client, url, headers, max_bytes=0, backend=None):
            calls.append(url)
            if "proxy.example.com" in url:
                assert headers.get("X-FeedEcho-Proxy-Secret") == "test-secret"
                return (proxy_feed_xml, "application/rss+xml")
            raise httpx.HTTPStatusError("Forbidden", request=direct_response.request, response=direct_response)

        monkeypatch.setattr(feed_parser, "_fetch_with_redirect_validation", _fake_fetch)

        result = feed_parser.fetch_feed("https://origin.example/rss")
        assert result["title"] == "Proxied Feed"
        assert len(result["items"]) == 1
        assert result["items"][0]["title"] == "Item 1"
        assert len(calls) == 2
        assert "origin.example" in calls[0]
        assert "proxy.example.com" in calls[1]

    def test_fetch_feed_falls_back_on_429(self, monkeypatch):
        monkeypatch.setattr(
            settings, "FALLBACK_PROXY_URL", "https://proxy.example.com"
        )
        monkeypatch.setattr(settings, "FALLBACK_PROXY_SECRET", "test-secret")

        direct_response = httpx.Response(429, request=httpx.Request("GET", "https://origin.example/rss"))
        proxy_feed_xml = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Proxied 429</title></channel></rss>"""

        def _fake_fetch(client, url, headers, max_bytes=0, backend=None):
            if "proxy.example.com" in url:
                return (proxy_feed_xml, "application/rss+xml")
            raise httpx.HTTPStatusError("Rate limited", request=direct_response.request, response=direct_response)

        monkeypatch.setattr(feed_parser, "_fetch_with_redirect_validation", _fake_fetch)

        result = feed_parser.fetch_feed("https://origin.example/rss")
        assert result["title"] == "Proxied 429"

    def test_fetch_feed_does_not_fallback_on_404_or_500(self, monkeypatch):
        monkeypatch.setattr(
            settings, "FALLBACK_PROXY_URL", "https://proxy.example.com"
        )
        direct_response = httpx.Response(404, request=httpx.Request("GET", "https://origin.example/rss"))

        calls = []

        def _fake_fetch(client, url, headers, max_bytes=0, backend=None):
            calls.append(url)
            raise httpx.HTTPStatusError("Not Found", request=direct_response.request, response=direct_response)

        monkeypatch.setattr(feed_parser, "_fetch_with_redirect_validation", _fake_fetch)

        with pytest.raises(httpx.HTTPStatusError):
            feed_parser.fetch_feed("https://origin.example/rss")
        assert len(calls) == 1  # No proxy call attempted

    def test_fetch_image_falls_back_on_403(self, monkeypatch):
        monkeypatch.setattr(
            settings, "FALLBACK_PROXY_URL", "https://proxy.example.com"
        )
        monkeypatch.setattr(settings, "FALLBACK_PROXY_SECRET", "test-secret")

        direct_response = httpx.Response(403, request=httpx.Request("GET", "https://origin.example/photo.jpg"))
        fake_jpeg = b"\xff\xd8\xff\xe0fakejpeg"

        def _fake_fetch(client, url, headers, max_bytes=0, backend=None):
            if "proxy.example.com" in url:
                return (fake_jpeg, "image/jpeg")
            raise httpx.HTTPStatusError("Forbidden", request=direct_response.request, response=direct_response)

        monkeypatch.setattr(feed_parser, "_fetch_with_redirect_validation", _fake_fetch)

        res = feed_parser.fetch_image("https://origin.example/photo.jpg")
        assert res is not None
        content, content_type = res
        assert content == fake_jpeg
        assert content_type == "image/jpeg"
