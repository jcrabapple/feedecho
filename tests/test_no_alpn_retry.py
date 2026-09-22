"""fetch_feed retries with a no-ALPN client when the edge drops the connection.

Tumblr's custom-domain edge proxy drops TLS connections that advertise
ALPN "http/1.1" (which httpcore always does) — the server closes without
sending a response — and its HTTP/2 resets streams. It serves plain
HTTP/1.1 when the ClientHello omits ALPN entirely, so fetch_feed retries
once with ssrf_client(no_alpn=True).
"""

import httpx
import pytest
from unittest import mock

import feed_parser
from feed_parser import fetch_feed

MINIMAL_RSS = (
    "<?xml version='1.0'?><rss version='2.0'><channel>"
    "<title>t</title><link>https://example.com/</link>"
    "<description>d</description>"
    "<item><title>i1</title><guid>g1</guid>"
    "<pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate></item>"
    "</channel></rss>"
).encode()

CONTENT = (MINIMAL_RSS, "application/xml", {"status": 200, "etag": None, "last_modified": None})


class _FakeClientFactory:
    """Stands in for ssrf_client: records calls, hands out mock clients."""

    def __init__(self, fetch_results):
        self.calls = []
        self.closed = []
        self._fetch_results = list(fetch_results)

    def __call__(self, urls, *, timeout=30, no_alpn=False):
        self.calls.append({"urls": list(urls), "no_alpn": no_alpn})
        client = mock.MagicMock()
        client.close.side_effect = lambda: self.closed.append(no_alpn)
        return client, mock.MagicMock()

    def fetch(self, *args, **kwargs):
        result = self._fetch_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class TestNoAlpnSslContext:
    """The no-ALPN context must differ from the normal one ONLY in that
    httpcore's set_alpn_protocols call becomes a no-op."""

    def test_no_alpn_context_ignores_alpn_setting(self):
        ctx = feed_parser._ssl_context(no_alpn=True)
        # httpcore calls this unconditionally before wrap_socket. The real
        # OpenSSL implementation validates its argument and would raise on
        # garbage; the subclass must swallow even that, proving the call
        # never reaches the TLS layer.
        assert ctx.set_alpn_protocols(["http/1.1"]) is None
        assert ctx.set_alpn_protocols([12345, "not-a-protocol"]) is None  # type: ignore[list-item]

    def test_normal_context_still_enforces_alpn_setting(self):
        ctx = feed_parser._ssl_context(no_alpn=False)
        with pytest.raises((TypeError, ValueError)):
            ctx.set_alpn_protocols([12345, "not-a-protocol"])  # type: ignore[list-item]

    def test_no_alpn_context_matches_normal_context_security_settings(self):
        normal = feed_parser._ssl_context(no_alpn=False)
        no_alpn = feed_parser._ssl_context(no_alpn=True)
        assert no_alpn.verify_flags == normal.verify_flags
        assert no_alpn.verify_mode == normal.verify_mode
        assert no_alpn.check_hostname == normal.check_hostname
        assert no_alpn.get_ca_certs() != []  # trust store carried over

    def test_no_alpn_context_is_an_ssl_context(self):
        import ssl

        assert isinstance(feed_parser._ssl_context(no_alpn=True), ssl.SSLContext)


class TestNoAlpnRetry:
    def test_retries_with_no_alpn_after_remote_disconnect(self):
        pair = _FakeClientFactory(
            [
                httpx.RemoteProtocolError(
                    "Server disconnected without sending a response."
                ),
                CONTENT,
            ]
        )
        with mock.patch.object(feed_parser, "ssrf_client", pair), \
                mock.patch.object(feed_parser, "_fetch_with_redirect_validation", pair.fetch):
            result = fetch_feed("https://binarydigit.art/rss")

        assert [c["no_alpn"] for c in pair.calls] == [False, True]
        assert pair.closed == [False, True]
        assert result["title"] == "t"
        assert len(result["items"]) == 1

    def test_no_retry_when_first_attempt_succeeds(self):
        pair = _FakeClientFactory([CONTENT])
        with mock.patch.object(feed_parser, "ssrf_client", pair), \
                mock.patch.object(feed_parser, "_fetch_with_redirect_validation", pair.fetch):
            fetch_feed("https://example.com/feed.xml")
        assert [c["no_alpn"] for c in pair.calls] == [False]

    def test_raises_when_no_alpn_retry_also_fails(self):
        pair = _FakeClientFactory(
            [
                httpx.RemoteProtocolError(
                    "Server disconnected without sending a response."
                ),
                httpx.RemoteProtocolError(
                    "Server disconnected without sending a response."
                ),
            ]
        )
        with mock.patch.object(feed_parser, "ssrf_client", pair), \
                mock.patch.object(feed_parser, "_fetch_with_redirect_validation", pair.fetch):
            with pytest.raises(httpx.RemoteProtocolError):
                fetch_feed("https://binarydigit.art/rss")
        assert [c["no_alpn"] for c in pair.calls] == [False, True]
