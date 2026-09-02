"""B2 — DNS-rebinding protection: validated-IP pinning for outbound fetches.

validate_outbound_url approves a URL's DNS answer at call time; without
pinning, httpx re-resolves at connect time and every redirect hop too, so a
low-TTL or attacker-controlled DNS answer can route the fetch to an internal
address after validation passed. These tests pin the seams:

- validate_outbound_url still blocks private ranges (existing suite covers it)
- ssrf_client pins each hostname to the address approved for it
- connect_tcp dials the pinned address (observed via the inner backend)
- redirect hops are validated AND pinned before being followed
- a hostname that resolves to a private range is refused at pin time

Network-adjacent pieces use fakes; only the pin-bookkeeping logic is real.
"""

import re
import socket
from pathlib import Path
from unittest import mock

import httpx
import pytest

import feed_parser
from feed_parser import (
    PinningNetworkBackend,
    SSRFError,
    _hostname_pin_key,
    _pins_for_urls,
    _fetch_with_redirect_validation,
    pinned_request,
    ssrf_client,
    unpinned_client,
)


# ── Pin bookkeeping ─────────────────────────────────────────────────────────


class TestHostnamePinKey:
    def test_ascii_hostname_is_lowercased_identity(self):
        assert _hostname_pin_key("Example.COM") == "example.com"

    def test_unicode_hostname_uses_punycode_form(self):
        # httpcore dials the IDNA form; the pin key must match it.
        assert _hostname_pin_key("BÜCHER.example.com") == (
            "xn--bcher-kva.example.com"
        )


class TestPickPinnedAddr:
    def test_prefers_ipv4_over_ipv6(self):
        addrs = ["2001:db8::1", "93.184.216.34"]
        assert feed_parser._pick_pinned_addr(addrs) == "93.184.216.34"

    def test_falls_back_to_first_when_only_ipv6(self):
        addrs = ["2001:db8::1", "2001:db8::2"]
        assert feed_parser._pick_pinned_addr(addrs) == "2001:db8::1"


class TestPinsForUrls:
    def test_refuses_hostname_resolving_to_private_range(self):
        with mock.patch.object(
            feed_parser.socket,
            "getaddrinfo",
            return_value=[(socket.AF_INET, 0, 0, "", ("10.0.0.5", 0))],
        ):
            with pytest.raises(SSRFError, match="private/reserved"):
                _pins_for_urls(["https://internal.example.com/feed"])

    def test_refuses_unresolvable_hostname(self):
        with mock.patch.object(
            feed_parser.socket, "getaddrinfo", side_effect=socket.gaierror(1, "nope")
        ):
            with pytest.raises(SSRFError, match="cannot resolve"):
                _pins_for_urls(["https://gone.example/feed"])

    def test_literal_ip_pins_itself(self):
        pins = _pins_for_urls(["https://8.8.8.8/feed"])
        assert pins == {"8.8.8.8": "8.8.8.8"}

    def test_hostname_pins_validated_address(self):
        with mock.patch.object(
            feed_parser.socket,
            "getaddrinfo",
            return_value=[
                (socket.AF_INET, 0, 0, "", ("93.184.216.34", 0)),
                (socket.AF_INET6, 0, 0, "", ("2606:2800:220:1::1", 0, 0, 0)),
            ],
        ):
            pins = _pins_for_urls(["https://example.com/feed"])
        assert pins == {"example.com": "93.184.216.34"}


# ── The backend dials the pinned address ────────────────────────────────────


class _RecordingInner:
    """Stands in for httpcore.SyncBackend; records what we dial."""

    def __init__(self):
        self.dialed = []

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.dialed.append((host, port))
        return object()  # NetworkStream stand-in; never used for I/O here

    def connect_unix_socket(self, *args, **kwargs):
        raise AssertionError("not used by these tests")

    def sleep(self, seconds):
        pass


class _ExposedBackend(PinningNetworkBackend):
    """PinningNetworkBackend with a replaceable inner, for observation."""

    def __init__(self):
        super().__init__()
        self.inner = _RecordingInner()
        # PinningNetworkBackend keeps the real inner in self._inner
        self._inner = self.inner


class TestPinningBackend:
    def test_connect_tcp_dials_pinned_address(self):
        backend = _ExposedBackend()
        backend.set_pin("example.com", "93.184.216.34")
        backend.connect_tcp("example.com", 443)
        assert backend.inner.dialed == [("93.184.216.34", 443)]

    def test_unpinned_host_fails_closed(self):
        """A pin miss must NOT fall through to live DNS (the TOCTOU hole)."""
        backend = _ExposedBackend()
        with pytest.raises(ConnectionError, match="pin miss"):
            backend.connect_tcp("unpinned.example", 443)
        assert backend.inner.dialed == []

    def test_pin_key_is_idna_normalized(self):
        backend = _ExposedBackend()
        backend.set_pin("BÜCHER.example.com", "93.184.216.34")
        backend.connect_tcp("xn--bcher-kva.example.com", 443)
        assert backend.inner.dialed == [("93.184.216.34", 443)]

    def test_clear_pins_causes_fail_closed(self):
        """After clear_pins, the host has no pin -> connect must fail closed."""
        backend = _ExposedBackend()
        backend.set_pin("example.com", "93.184.216.34")
        backend.clear_pins()
        with pytest.raises(ConnectionError, match="pin miss"):
            backend.connect_tcp("example.com", 443)


# ── ssrf_client builds a pinned client ──────────────────────────────────────


class TestSsrfClient:
    def test_rejects_private_url_before_building_client(self):
        with pytest.raises(SSRFError):
            ssrf_client(["http://127.0.0.1:8000/healthz"])

    def test_client_and_backend_are_bound_together(self):
        client, backend = ssrf_client(["https://example.com/"])
        try:
            assert isinstance(client, httpx.Client)
            assert isinstance(backend, PinningNetworkBackend)
            # The pin minted for the entry URL is in the backend's map.
            assert backend._pins.get("example.com")
        finally:
            client.close()


# ── Redirect hops are validated AND pinned ──────────────────────────────────


class _StreamCtx:
    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return self._response

    def __exit__(self, *exc):
        return False


def _redirect_response(location):
    resp = mock.MagicMock()
    resp.is_redirect = True
    resp.headers = {"location": location}
    return resp


def _final_response(body=b"<rss></rss>"):
    resp = mock.MagicMock()
    resp.is_redirect = False
    resp.status_code = 200
    resp.headers = {"content-type": "application/rss+xml"}
    resp.iter_bytes.return_value = iter([body])
    return resp


class TestRedirectHopsPinned:
    def test_redirect_hop_is_pinned_after_validation(self):
        """The hop that passes validation gets a pin before the next request."""
        client = mock.MagicMock()
        backend = _ExposedBackend()
        final = _final_response()
        client.stream.side_effect = [
            _StreamCtx(_redirect_response("https://cdn.example/feed.xml")),
            _StreamCtx(final),
        ]

        with mock.patch.object(
            feed_parser.socket,
            "getaddrinfo",
            return_value=[(socket.AF_INET, 0, 0, "", ("93.184.216.34", 0))],
        ):
            _fetch_with_redirect_validation(
                client,
                "https://origin.example/feed.xml",
                {},
                backend=backend,
            )

        assert backend._pins.get("cdn.example") == "93.184.216.34"
        # And the entry URL's pin was minted too? No: _fetch_with_redirect_
        # validation only pins hops AFTER the first; the entry URL was pinned
        # by ssrf_client before this helper ran.
        assert "origin.example" not in backend._pins

    def test_redirect_to_private_ip_never_gets_a_pin(self):
        client = mock.MagicMock()
        backend = _ExposedBackend()
        client.stream.return_value = _StreamCtx(
            _redirect_response("http://169.254.169.254/latest/meta-data/")
        )

        with pytest.raises(SSRFError, match="private"):
            _fetch_with_redirect_validation(
                client, "https://origin.example/feed.xml", {}, backend=backend
            )
        assert not backend._pins

    def test_no_backend_argument_still_validates_hops(self):
        """Back-compat: callers without a backend keep the old guarantee."""
        client = mock.MagicMock()
        client.stream.return_value = _StreamCtx(
            _redirect_response("http://10.0.0.9/feed.xml")
        )

        with pytest.raises(SSRFError, match="private"):
            _fetch_with_redirect_validation(client, "https://8.8.8.8/feed.xml", {})


# ── fetch_feed / fetch_image go through the pinned path ─────────────────────


class TestFetchersUsePinnedClient:
    @staticmethod
    def _poison_fetch(client, url, headers, max_bytes=0, backend=None):
        raise AssertionError("network disabled in unit test")

    def test_fetch_feed_uses_ssrf_client(self):
        with mock.patch.object(
            feed_parser, "ssrf_client", wraps=feed_parser.ssrf_client
        ) as spy, mock.patch.object(
            feed_parser, "_fetch_with_redirect_validation", self._poison_fetch
        ):
            with pytest.raises(AssertionError, match="network disabled"):
                feed_parser.fetch_feed("https://example.com/feed.xml")
        assert spy.called

    def test_fetch_image_uses_ssrf_client(self):
        with mock.patch.object(
            feed_parser, "ssrf_client", wraps=feed_parser.ssrf_client
        ) as spy:
            feed_parser.fetch_image("http://127.0.0.1:9/x.png")
        # Loopback is rejected inside validate_outbound_url before ssrf_client
        assert not spy.called

        # fetch_image swallows all exceptions by contract (returns None on
        # failure), so assert the poisoned helper ran by the marker it raises.
        marker = RuntimeError("network-disabled-marker")
        with mock.patch.object(
            feed_parser, "ssrf_client", wraps=feed_parser.ssrf_client
        ) as spy2, mock.patch.object(
            feed_parser, "_fetch_with_redirect_validation", side_effect=marker
        ):
            assert feed_parser.fetch_image("https://example.com/img.png") is None
        assert spy2.called


class TestRealPinnedTransport:
    """Exercises the real _PinnedTransport path (not a mock client)."""

    def test_real_pinned_transport_fetches_a_public_feed(self):
        """Send bytes through the hand-constructed ConnectionPool + transport.

        This is the only test that actually drives the real _PinnedTransport
        (the riskiest code in the B2 change). Skipped when offline.
        """
        import socket

        try:
            socket.getaddrinfo("example.com", 443)
        except socket.gaierror:
            pytest.skip("offline — cannot reach example.com")
        client, backend = ssrf_client(["https://example.com/"])
        try:
            r = client.get("https://example.com/")
            assert r.status_code == 200
            assert "example.com" in backend._pins
        finally:
            client.close()


# ── pinned_request: the one-call entry point modules now use ────────────────


class TestPinnedRequest:
    def _redirect(self, location, status=302):
        resp = mock.MagicMock()
        resp.is_redirect = True
        resp.status_code = status
        resp.headers = {"location": location}
        return resp

    def test_redirect_to_private_ip_is_refused(self):
        """S1: a redirect hop to an internal address must not be dialed."""
        fake_client = mock.MagicMock()
        backend = _ExposedBackend()
        fake_client.request.return_value = self._redirect(
            "http://169.254.169.254/latest/meta-data/"
        )

        with mock.patch.object(
            feed_parser, "ssrf_client", return_value=(fake_client, backend)
        ):
            with pytest.raises(SSRFError, match="private"):
                pinned_request(
                    "GET", "https://origin.example/feed", follow_redirects=True
                )
        assert not backend._pins

    def test_redirect_to_public_ip_is_validated_and_pinned(self):
        fake_client = mock.MagicMock()
        backend = _ExposedBackend()
        final = mock.MagicMock()
        final.is_redirect = False
        fake_client.request.side_effect = [
            self._redirect("https://cdn.example/feed.xml"),
            final,
        ]

        with mock.patch.object(
            feed_parser.socket,
            "getaddrinfo",
            return_value=[(socket.AF_INET, 0, 0, "", ("93.184.216.34", 0))],
        ), mock.patch.object(
            feed_parser, "ssrf_client", return_value=(fake_client, backend)
        ):
            resp = pinned_request(
                "GET", "https://origin.example/feed", follow_redirects=True
            )

        assert resp is final
        assert backend._pins.get("cdn.example") == "93.184.216.34"

    def test_redirects_not_followed_by_default(self):
        fake_client = mock.MagicMock()
        backend = _ExposedBackend()
        redirect = self._redirect("http://169.254.169.254/latest/meta-data/")
        fake_client.request.return_value = redirect

        with mock.patch.object(
            feed_parser, "ssrf_client", return_value=(fake_client, backend)
        ):
            resp = pinned_request("GET", "https://origin.example/feed")

        # follow_redirects defaults False: the redirect comes back unvalidated,
        # exactly like a bare httpx.Client(follow_redirects=False) would.
        assert resp is redirect
        assert not backend._pins

    def test_cross_origin_redirect_strips_authorization(self):
        """A hop to another origin must not carry the tenant's credentials."""
        fake_client = mock.MagicMock()
        backend = _ExposedBackend()
        final = mock.MagicMock()
        final.is_redirect = False
        fake_client.request.side_effect = [
            self._redirect("https://other.example/collect", 307),
            final,
        ]

        with mock.patch.object(
            feed_parser.socket,
            "getaddrinfo",
            return_value=[(socket.AF_INET, 0, 0, "", ("93.184.216.34", 0))],
        ), mock.patch.object(
            feed_parser, "ssrf_client", return_value=(fake_client, backend)
        ):
            pinned_request(
                "GET",
                "https://origin.example/api",
                headers={"Authorization": "Bearer tok"},
                follow_redirects=True,
            )

        second = fake_client.request.call_args_list[1]
        assert "Authorization" not in second.kwargs["headers"]

    def test_same_origin_redirect_keeps_authorization(self):
        fake_client = mock.MagicMock()
        backend = _ExposedBackend()
        final = mock.MagicMock()
        final.is_redirect = False
        fake_client.request.side_effect = [
            self._redirect("https://origin.example/other", 307),
            final,
        ]

        with mock.patch.object(
            feed_parser.socket,
            "getaddrinfo",
            return_value=[(socket.AF_INET, 0, 0, "", ("93.184.216.34", 0))],
        ), mock.patch.object(
            feed_parser, "ssrf_client", return_value=(fake_client, backend)
        ):
            pinned_request(
                "GET",
                "https://origin.example/api",
                headers={"Authorization": "Bearer tok"},
                follow_redirects=True,
            )

        second = fake_client.request.call_args_list[1]
        assert second.kwargs["headers"]["Authorization"] == "Bearer tok"

    def test_307_preserves_method_and_body(self):
        fake_client = mock.MagicMock()
        backend = _ExposedBackend()
        final = mock.MagicMock()
        final.is_redirect = False
        fake_client.request.side_effect = [
            self._redirect("https://origin.example/next", 307),
            final,
        ]

        with mock.patch.object(
            feed_parser.socket,
            "getaddrinfo",
            return_value=[(socket.AF_INET, 0, 0, "", ("93.184.216.34", 0))],
        ), mock.patch.object(
            feed_parser, "ssrf_client", return_value=(fake_client, backend)
        ):
            pinned_request(
                "POST",
                "https://origin.example/api",
                json={"a": 1},
                follow_redirects=True,
            )

        second = fake_client.request.call_args_list[1]
        assert second.args[0] == "POST"
        assert second.kwargs.get("json") == {"a": 1}

    def test_302_after_post_drops_body_and_entity_headers(self):
        fake_client = mock.MagicMock()
        backend = _ExposedBackend()
        final = mock.MagicMock()
        final.is_redirect = False
        fake_client.request.side_effect = [
            self._redirect("https://origin.example/next", 302),
            final,
        ]

        with mock.patch.object(
            feed_parser.socket,
            "getaddrinfo",
            return_value=[(socket.AF_INET, 0, 0, "", ("93.184.216.34", 0))],
        ), mock.patch.object(
            feed_parser, "ssrf_client", return_value=(fake_client, backend)
        ):
            pinned_request(
                "POST",
                "https://origin.example/api",
                headers={
                    "Authorization": "Bearer tok",
                    "Content-Type": "application/json",
                },
                json={"a": 1},
                follow_redirects=True,
            )

        second = fake_client.request.call_args_list[1]
        assert second.args[0] == "GET"
        assert "json" not in second.kwargs
        headers = second.kwargs["headers"]
        assert "Content-Type" not in headers
        assert headers.get("Authorization") == "Bearer tok"

    def test_too_many_redirects_raises(self):
        fake_client = mock.MagicMock()
        backend = _ExposedBackend()
        fake_client.request.return_value = self._redirect(
            "https://origin.example/loop", 302
        )

        with mock.patch.object(
            feed_parser.socket,
            "getaddrinfo",
            return_value=[(socket.AF_INET, 0, 0, "", ("93.184.216.34", 0))],
        ), mock.patch.object(
            feed_parser, "ssrf_client", return_value=(fake_client, backend)
        ):
            with pytest.raises(SSRFError, match="Too many redirects"):
                pinned_request(
                    "GET", "https://origin.example/feed", follow_redirects=True
                )


class TestClientFactoriesDisableRedirects:
    def test_unpinned_client_disables_redirects(self):
        client = unpinned_client(timeout=5)
        try:
            assert client.follow_redirects is False
        finally:
            client.close()

    def test_ssrf_client_disables_redirects(self):
        client, backend = ssrf_client(["https://example.com/"], timeout=5)
        try:
            assert client.follow_redirects is False
        finally:
            client.close()


# ── S2 enforcement: no bare httpx.Client outside the machinery ──────────────


class TestNoBareHttpxClientOutsideFeedParser:
    """Outbound modules must route through pinned_request/ssrf_client/
    unpinned_client — never construct httpx.Client themselves.

    A bare httpx.Client re-resolves the hostname at connect time, silently
    re-opening the DNS-rebinding TOCTOU window that the pinned transport
    closes. This is the outbound-HTTP sibling of
    test_no_async_route_performs_blocking_io.

    Matches every evasion spelling: ``httpx.Client(``, ``httpx.AsyncClient(``,
    and ``from httpx import Client`` (which lets a module build a bare client
    under a short alias). Scans the whole repo (rglob), excluding tests/.
    """

    CONSTRUCTION = re.compile(r"\bhttpx\.(?:Async)?Client\s*\(")
    IMPORTED = re.compile(r"\bfrom\s+httpx\s+import\s+[^\n]*\b(?:Async)?Client\b")

    def _offenders(self) -> dict:
        repo = Path(__file__).resolve().parent.parent
        offenders = {}
        for py in repo.rglob("*.py"):
            rel = py.relative_to(repo)
            # Skip the test tree, hidden dirs (.venv, .git, ...), and the
            # SSRF machinery itself.
            if py.name == "feed_parser.py":
                continue
            if rel.parts and (rel.parts[0] == "tests" or any(
                part.startswith(".") for part in rel.parts
            )):
                continue
            lines = sorted(
                {
                    i + 1
                    for i, line in enumerate(py.read_text().splitlines())
                    if self.CONSTRUCTION.search(line) or self.IMPORTED.search(line)
                }
            )
            if lines:
                offenders[str(rel)] = lines
        return offenders

    def test_bare_httpx_client_only_in_feed_parser(self):
        offenders = self._offenders()
        assert offenders == {}, (
            "bare httpx.Client/AsyncClient (or `from httpx import Client`) found"
            " outside feed_parser.py — route outbound requests through"
            " pinned_request/ssrf_client/unpinned_client. "
            f"Offenders: {offenders}"
        )
