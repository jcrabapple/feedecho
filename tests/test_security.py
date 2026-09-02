"""Security and reliability tests for OAuth state and outbound URL controls."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
import socket

from database import get_db
from feed_parser import SSRFError, validate_feed_url, validate_outbound_url
from mastodon import post_status, verify_credentials
from oauth import _sign_state, _verify_state, get_or_create_app


class TestSSRFProtection:
    def test_blocks_localhost_ip(self):
        with pytest.raises(SSRFError, match="private"):
            validate_outbound_url("http://127.0.0.1/latest-meta-data/")

    def test_blocks_loopback_ipv6(self):
        with pytest.raises(SSRFError, match="private"):
            validate_outbound_url("http://[::1]/test")

    def test_blocks_private_10(self):
        with pytest.raises(SSRFError, match="private"):
            validate_outbound_url("http://10.0.0.1/internal")

    def test_blocks_private_192(self):
        with pytest.raises(SSRFError, match="private"):
            validate_outbound_url("http://192.168.1.1/admin")

    def test_blocks_private_172(self):
        with pytest.raises(SSRFError, match="private"):
            validate_outbound_url("http://172.16.0.1/")

    def test_blocks_link_local(self):
        with pytest.raises(SSRFError, match="private"):
            validate_outbound_url("http://169.254.169.254/latest-meta-data/")

    def test_blocks_file_scheme(self):
        with pytest.raises(SSRFError, match="not allowed"):
            validate_outbound_url("file:///etc/passwd")

    def test_blocks_gopher_scheme(self):
        with pytest.raises(SSRFError, match="not allowed"):
            validate_outbound_url("gopher://localhost/")

    def test_blocks_embedded_credentials(self):
        with pytest.raises(SSRFError, match="credentials"):
            validate_outbound_url("http://user:pass@example.com/feed")

    def test_blocks_hostname_resolving_to_private(self):
        with patch("socket.getaddrinfo") as mock_resolve:
            mock_resolve.return_value = [
                (socket.AF_INET, 0, 0, "", ("10.0.0.5", 0))
            ]
            with pytest.raises(SSRFError, match="resolves to"):
                validate_outbound_url("http://internal.example.com/secret")

    def test_allows_public_ip(self):
        assert validate_outbound_url("https://8.8.8.8/feed.xml") == "https://8.8.8.8/feed.xml"

    def test_validate_feed_url_alias_works(self):
        assert validate_feed_url("https://example.com/feed.xml") == "https://example.com/feed.xml"


class TestBlockedIpRanges:
    """The exact evasion-candidate set the blocklist must reject.

    Pinned per-address so a refactor that widens or narrows the blocklist
    fails loudly instead of silently shipping a hole. Includes 100.64.0.0/10
    (CGNAT — the range that motivated the explicit check; is_global would
    miss multicast/NAT64, so these stay explicit).
    """

    BLOCKED = [
        "127.0.0.1",          # loopback
        "::1",                # loopback v6
        "10.0.0.1",           # RFC1918
        "172.16.0.1",         # RFC1918
        "172.31.255.255",     # RFC1918 edge
        "192.168.1.1",        # RFC1918
        "169.254.169.254",    # link-local (cloud metadata)
        "100.64.0.1",         # CGNAT low edge (the gap this closes)
        "100.64.255.255",     # CGNAT high edge
        "0.0.0.0",            # unspecified
        "::",                 # unspecified v6
        "224.0.0.1",          # multicast
        "ff02::1",            # multicast v6
        "240.0.0.1",          # reserved (class E)
        "fe80::1",            # link-local v6
        "fc00::1",            # unique-local v6
        "::ffff:127.0.0.1",   # IPv4-mapped loopback
        "::ffff:10.0.0.1",    # IPv4-mapped private
        "64:ff9b::1",         # NAT64
        "198.18.0.1",         # benchmarking
        "192.0.2.1",          # TEST-NET-1 documentation
        "2001:db8::1",        # documentation v6
    ]

    ALLOWED = [
        "8.8.8.8",
        "93.184.216.34",
        "2606:2800:220:1::1",
        "2001:4860:4860::8888",
    ]

    def test_blocked_ranges(self):
        import ipaddress as _ipa
        from feed_parser import _is_blocked_ip

        for addr in self.BLOCKED:
            assert _is_blocked_ip(_ipa.ip_address(addr)), (
                f"{addr} must be blocked, but passed"
            )

    def test_allowed_ranges(self):
        import ipaddress as _ipa
        from feed_parser import _is_blocked_ip

        for addr in self.ALLOWED:
            assert not _is_blocked_ip(_ipa.ip_address(addr)), (
                f"{addr} must be allowed, but was blocked"
            )

    def test_cgnat_is_blocked_end_to_end(self):
        with pytest.raises(SSRFError, match="private"):
            validate_outbound_url("http://100.64.0.1/internal")


class TestSSRFRedirectProtection:
    """Every redirect hop is validated before it is followed.

    The fetch helper streams the body under a size cap, so the fakes below
    stand in for `client.stream(...)` context managers rather than
    `client.get(...)` responses.
    """

    @staticmethod
    def _stream_ctx(response):
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=response)
        ctx.__exit__ = MagicMock(return_value=False)
        return ctx

    def test_redirect_to_private_ip_is_blocked(self):
        from feed_parser import _fetch_with_redirect_validation

        redirect = MagicMock()
        redirect.is_redirect = True
        redirect.headers = {"location": "http://169.254.169.254/latest-meta-data/"}

        client = MagicMock()
        client.stream.return_value = self._stream_ctx(redirect)

        with pytest.raises(SSRFError, match="private"):
            _fetch_with_redirect_validation(client, "https://evil.example/feed.xml", {})

    def test_redirect_to_public_url_allowed(self):
        from feed_parser import _fetch_with_redirect_validation

        redirect = MagicMock()
        redirect.is_redirect = True
        redirect.headers = {"location": "https://8.8.8.8/feed.xml"}

        final = MagicMock()
        final.is_redirect = False
        final.status_code = 200
        final.headers = {"content-type": "application/rss+xml"}
        final.iter_bytes.return_value = [b"<rss></rss>"]

        client = MagicMock()
        client.stream.side_effect = [
            self._stream_ctx(redirect),
            self._stream_ctx(final),
        ]

        body, content_type = _fetch_with_redirect_validation(
            client,
            "https://8.8.8.8/feed.xml",
            {},
        )
        assert body == b"<rss></rss>"
        assert content_type == "application/rss+xml"

    def test_body_over_the_cap_is_abandoned(self):
        """The cap is enforced while streaming, not after buffering.

        Chunk sizes are chosen so the cap trips on the second chunk: a
        buffer-everything implementation would consume all three, so the
        assertion on how many chunks were pulled actually distinguishes the two.
        """
        from feed_parser import _fetch_with_redirect_validation

        final = MagicMock()
        final.is_redirect = False
        final.headers = {"content-type": "application/rss+xml"}
        chunks = [b"x" * 1024, b"x" * 1024, b"x" * 1024]
        read = []

        def _iter():
            for c in chunks:
                read.append(len(c))
                yield c

        final.iter_bytes.side_effect = _iter

        client = MagicMock()
        client.stream.return_value = self._stream_ctx(final)

        with pytest.raises(ValueError, match="too large"):
            _fetch_with_redirect_validation(
                client, "https://8.8.8.8/feed.xml", {}, max_bytes=1500
            )
        assert read == [1024, 1024], (
            "expected the read to stop as soon as the cap was passed; "
            f"chunks pulled: {read}"
        )

    def test_declared_content_length_over_the_cap_is_refused(self):
        from feed_parser import _fetch_with_redirect_validation

        final = MagicMock()
        final.is_redirect = False
        final.headers = {"content-type": "application/rss+xml", "content-length": "999999"}
        final.iter_bytes.side_effect = AssertionError("body should not be read at all")

        client = MagicMock()
        client.stream.return_value = self._stream_ctx(final)

        with pytest.raises(ValueError, match="too large"):
            _fetch_with_redirect_validation(
                client, "https://8.8.8.8/feed.xml", {}, max_bytes=2048
            )


class TestInstanceURLSSRF:
    def test_oauth_get_or_create_app_validates_instance(self):
        with pytest.raises(SSRFError):
            get_or_create_app("http://127.0.0.1:8000")

    def test_mastodon_post_status_validates_instance(self):
        with pytest.raises(SSRFError):
            post_status(
                instance="http://10.0.0.1",
                access_token="fake-token",
                content="test",
            )

    def test_mastodon_verify_credentials_validates_instance(self):
        with pytest.raises(SSRFError):
            verify_credentials(
                instance="http://169.254.169.254",
                access_token="fake-token",
            )


class TestOAuthStateSecurity:
    """State must be signed, server-side, expiry-bound, session-bound, and one-time."""

    @pytest.fixture(autouse=True)
    def clean_states(self):
        with get_db() as db:
            db.execute("DELETE FROM oauth_states")
        yield
        with get_db() as db:
            db.execute("DELETE FROM oauth_states")

    def test_sign_and_verify_roundtrip(self):
        session = "browser-session-a"
        token = _sign_state("https://dmv.community", session)
        assert _verify_state(token, session) == "https://dmv.community"

    def test_state_is_single_use(self):
        session = "browser-session-a"
        token = _sign_state("https://dmv.community", session)

        assert _verify_state(token, session) == "https://dmv.community"

        with pytest.raises(ValueError, match="already used|invalid"):
            _verify_state(token, session)

    def test_state_cannot_be_consumed_from_another_browser_session(self):
        token = _sign_state("https://dmv.community", "initiating-session")

        with pytest.raises(ValueError, match="session-mismatched|invalid"):
            _verify_state(token, "different-browser-session")

        # A failed mismatched attempt does not consume the legitimate state.
        assert _verify_state(token, "initiating-session") == "https://dmv.community"

    def test_expired_state_is_rejected(self):
        session = "browser-session-a"
        token = _sign_state("https://dmv.community", session)
        nonce = token.rsplit("|", 2)[0]

        expired = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).strftime("%Y-%m-%d %H:%M:%S")

        with get_db() as db:
            db.execute(
                "UPDATE oauth_states SET expires_at = ? WHERE nonce = ?",
                (expired, nonce),
            )

        with pytest.raises(ValueError, match="expired|invalid"):
            _verify_state(token, session)

    def test_verify_rejects_tampered_instance(self):
        session = "browser-session-a"
        token = _sign_state("https://dmv.community", session)
        parts = token.rsplit("|", 2)
        parts[1] = "https://evil.example"

        with pytest.raises(ValueError, match="Invalid state signature"):
            _verify_state("|".join(parts), session)

    def test_verify_rejects_tampered_full_length_signature(self):
        session = "browser-session-a"
        token = _sign_state("https://dmv.community", session)
        parts = token.rsplit("|", 2)
        parts[2] = "a" * 64

        with pytest.raises(ValueError, match="Invalid state signature"):
            _verify_state("|".join(parts), session)

    def test_signature_is_full_sha256_hex(self):
        token = _sign_state("https://dmv.community", "browser-session-a")
        signature = token.rsplit("|", 2)[2]

        assert len(signature) == 64
        assert all(char in "0123456789abcdef" for char in signature)

    def test_verify_requires_session_binding(self):
        token = _sign_state("https://dmv.community", "browser-session-a")

        with pytest.raises(ValueError, match="browser session"):
            _verify_state(token)

    def test_state_contains_unique_nonce(self):
        session = "browser-session-a"
        assert (
            _sign_state("https://dmv.community", session)
            != _sign_state("https://dmv.community", session)
        )