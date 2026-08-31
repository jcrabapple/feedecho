"""Feed parser — fetch and parse RSS, Atom, and JSON feeds.

Returns normalized feed items with: id, title, link, summary, content,
content_link, author, date, and raw data for template access.
"""

import re
import calendar
import hashlib
import html
import ipaddress
import json
import socket
import threading
import httpx
import feedparser
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlparse


USER_AGENT = "feedecho/1.0 (+https://github.com/yourusername/feedecho)"
MAX_FEED_SIZE = 10 * 1024 * 1024  # 10 MB cap
MAX_REDIRECTS = 5
MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10 MB cap, matches Mastodon's default limits
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/avif"}


class SSRFError(ValueError):
    """Raised when a URL points to a private or internal address."""


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Check if an IP address is private, loopback, link-local, or reserved."""
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def validate_outbound_url(url: str) -> str:
    """Validate that a URL is safe for server-side fetching (SSRF protection).

    Blocks:
    - Non-http(s) schemes (file://, gopher://, etc.)
    - Userinfo in URL (user:pass@host)
    - Hostnames that resolve to private/internal IPs
    - Direct IP addresses in private ranges (10.x, 172.16-31.x, 192.168.x,
      127.x, 169.254.x, ::1, fc00::, fe80::)

    Raises SSRFError if the URL is unsafe. Returns the URL if safe.

    NOTE: validation alone is TOCTOU-vulnerable (the DNS answer checked here
    is not the connection httpx later makes). Callers that actually fetch
    must go through ``ssrf_client()`` so the validated IP is pinned; see
    ``PinningNetworkBackend``.
    """
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise SSRFError(f"Blocked: URL scheme '{parsed.scheme}' is not allowed")

    if parsed.username or parsed.password:
        raise SSRFError("Blocked: URLs with embedded credentials are not allowed")

    hostname = parsed.hostname
    if not hostname:
        raise SSRFError("Blocked: URL has no hostname")

    # If it's a literal IP, check directly
    is_ip = False
    ip = None
    try:
        ip = ipaddress.ip_address(hostname)
        is_ip = True
    except ValueError:
        pass

    if is_ip and ip is not None:
        if _is_blocked_ip(ip):
            raise SSRFError(f"Blocked: IP address {ip} is in a private/reserved range")
    else:
        # Not a literal IP — resolve the hostname and check all results
        try:
            infos = socket.getaddrinfo(hostname, None)
        except socket.gaierror:
            raise SSRFError(f"Blocked: cannot resolve hostname '{hostname}'")

        for family, _, _, _, sockaddr in infos:
            addr_str = sockaddr[0]
            try:
                ip = ipaddress.ip_address(addr_str)
            except ValueError:
                continue
            if _is_blocked_ip(ip):
                raise SSRFError(
                    f"Blocked: hostname '{hostname}' resolves to "
                    f"private/reserved IP {ip}"
                )

    return url


# ── DNS-rebinding protection: validated-IP pinning (B2) ─────────────────────
#
# validate_outbound_url checks the DNS answer at call time, but httpx resolves
# the hostname AGAIN when it opens the connection. A low-TTL DNS answer (or an
# attacker-controlled authoritative server) can hand back a different IP
# between the two lookups, and every redirect hop re-resolves too. That gap
# turns the validator into theatre on hostile DNS.
#
# The fix: validate_outbound_url returns the addresses it approved, and the
# fetch path connects to one of those exact addresses. The backend below pins
# per-hostname; TLS is unaffected because httpcore derives SNI and the
# certificate identity from the URL's hostname, not from the IP we dial — a
# pinned-but-hostile IP simply fails certificate verification, and a
# same-CDN address swap to another site behind the same cert is the residual
# risk documented in HANDOFF.

def _hostname_pin_key(hostname: str) -> str:
    """The hostname form httpcore passes to NetworkBackend.connect_tcp.

    httpcore dials the IDNA/punycode form of the URL host, while
    urlparse().hostname yields the unicode form. Keying the pin map on the
    punycode form makes both sides agree (ASCII hostnames are their own
    punycode form, so this is a no-op for them).
    """
    return hostname.encode("idna").decode("ascii").lower()


def _resolve_addrs(hostname: str) -> list[str]:
    """Resolve a hostname to its IP addresses (both families, best effort)."""
    addrs: list[str] = []
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return addrs
    for family, _, _, _, sockaddr in infos:
        try:
            addrs.append(str(ipaddress.ip_address(sockaddr[0])))
        except ValueError:
            continue
    return addrs


def _pick_pinned_addr(addrs: list[str]) -> str:
    """Choose one address to dial: the first IPv4, else the first entry.

    Preferring IPv4 keeps the door open for hosts that publish an A record
    and an unreachable AAAA record; when only IPv6 exists we take the first.
    """
    for addr in addrs:
        if isinstance(ipaddress.ip_address(addr), ipaddress.IPv4Address):
            return addr
    return addrs[0]


class PinningNetworkBackend:
    """httpcore NetworkBackend that dials pre-validated addresses.

    Wraps httpcore's default SyncBackend. ``pins`` maps the IDNA hostname to
    the exact IP approved by validate_outbound_url; connect_tcp dials the
    pinned address instead of resolving DNS again. Unknown hosts (e.g. from
    code paths that bypass ssrf_client) fall through to normal resolution —
    this backend is defense-in-depth for our fetches, not a system-wide VPN.
    """

    def __init__(self) -> None:
        import httpcore

        self._inner = httpcore.SyncBackend()
        self._pins: dict[str, str] = {}
        self._lock = threading.Lock()

    def set_pin(self, hostname: str, addr: str) -> None:
        key = _hostname_pin_key(hostname)
        with self._lock:
            self._pins[key] = addr

    def clear_pins(self) -> None:
        with self._lock:
            self._pins.clear()

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        with self._lock:
            pinned = self._pins.get(host)
        if not pinned:
            # Fail closed: an SSRF guard that silently falls through to live
            # DNS on a pin miss is no guard at all. Every outbound host must
            # be explicitly pinned via ssrf_client before it is dialed.
            raise ConnectionError(
                f"SSRF pin miss for {host!r} — host was not pre-validated"
            )
        # Dial the pinned address; SNI and cert identity still come from the
        # URL hostname because httpcore passes them independently of this arg.
        return self._inner.connect_tcp(
            pinned, port, timeout=timeout,
            local_address=local_address, socket_options=socket_options,
        )

    def connect_unix_socket(self, *args, **kwargs):
        return self._inner.connect_unix_socket(*args, **kwargs)

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(seconds)


def _pins_for_urls(urls: list[str]) -> dict[str, str]:
    """Validate each URL and return {pin_key: dial_addr} for all of them.

    Idempotent per URL, so callers can pre-validate the whole redirect set
    they are willing to follow, or just the first URL.
    """
    pins: dict[str, str] = {}
    for url in urls:
        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            continue
        try:
            ipaddress.ip_address(hostname)
            key = hostname.lower()
        except ValueError:
            key = _hostname_pin_key(hostname)
        addrs = _resolve_addrs(hostname)
        if not addrs:
            raise SSRFError(f"Blocked: cannot resolve hostname '{hostname}'")
        # _pins_for_urls is the SOLE authority on which address gets pinned:
        # it resolves and checks independently of validate_outbound_url. Do
        # NOT refactor it to trust the validator's result — that reopens the
        # TOCTOU window (two independent resolutions, one for validation and
        # one for pinning).
        for addr in addrs:
            if _is_blocked_ip(ipaddress.ip_address(addr)):
                raise SSRFError(
                    f"Blocked: hostname '{hostname}' resolves to "
                    f"private/reserved IP {addr}"
                )
        pins[key] = _pick_pinned_addr(addrs)
    return pins


def ssrf_client(
    urls: list[str],
    *,
    timeout: float = 30,
) -> tuple[httpx.Client, PinningNetworkBackend]:
    """Build an httpx.Client whose connections are pinned to validated IPs.

    Every URL in ``urls`` is validated and its address pinned for the life
    of the client. Returns the client and its pinning backend, so callers
    (the redirect-validating fetch helper) can pin additional hops as they
    validate them. Callers MUST close the client when done (try/finally);
    that releases the pool and its connections.
    """
    import httpcore
    from httpx._config import create_ssl_context

    pins = _pins_for_urls(urls)
    backend = PinningNetworkBackend()
    for key, addr in pins.items():
        backend.set_pin(key, addr)
    pool = httpcore.ConnectionPool(
        ssl_context=create_ssl_context(verify=True, cert=None, trust_env=True),
        network_backend=backend,
    )

    class _PinnedTransport(httpx.BaseTransport):
        def handle_request(self, request):
            req = httpcore.Request(
                method=request.method,
                url=httpcore.URL(
                    scheme=request.url.raw_scheme,
                    host=request.url.raw_host,
                    port=request.url.port,
                    target=request.url.raw_path,
                ),
                headers=request.headers.raw,
                content=request.stream,
                extensions=request.extensions,
            )
            with httpx._transports.default.map_httpcore_exceptions():
                resp = pool.handle_request(req)
            return httpx.Response(
                resp.status,
                headers=resp.headers,
                stream=httpx._transports.default.ResponseStream(resp.stream),
                extensions=resp.extensions,
            )

        def close(self):
            # httpx.BaseTransport has no close hook; without this the
            # ConnectionPool (and its keep-alive sockets/FDs) leak on every
            # client.close() in the long-running scheduler worker.
            pool.close()

    client = httpx.Client(
        transport=_PinnedTransport(),
        follow_redirects=False,
        timeout=timeout,
    )
    return client, backend


# Backwards-compatible alias
validate_feed_url = validate_outbound_url


def fetch_feed(url: str) -> dict:
    """Fetch and parse a feed URL. Returns dict with feed metadata and items.

    Validates the initial URL and every redirect hop for SSRF protection,
    pinning each hop's connection to the IP validated for that hop (B2: the
    second DNS lookup a naive fetch performs is the rebinding window).
    Raises SSRFError if any URL (initial or redirect) points to a
    private/internal address.
    Raises httpx.HTTPError on network failure.
    """
    validate_outbound_url(url)

    headers = {"User-Agent": USER_AGENT}
    client, backend = ssrf_client([url])
    try:
        content, content_type = _fetch_with_redirect_validation(
            client, url, headers, MAX_FEED_SIZE, backend=backend
        )
    finally:
        client.close()

    # JSON Feed. The path (not the full URL) decides: query strings like
    # /feed.json?token=... are common on private feeds, and such URLs often
    # also omit a JSON content-type, so both signals must use the path.
    if "json" in content_type or urlparse(url).path.endswith(".json"):
        return parse_json_feed(json.loads(content))

    # RSS/Atom via feedparser
    parsed = feedparser.parse(content)
    return parse_rss_feed(parsed, url)


def _fetch_with_redirect_validation(
    client: httpx.Client,
    url: str,
    headers: dict,
    max_bytes: int = MAX_FEED_SIZE,
    backend: "PinningNetworkBackend | None" = None,
) -> tuple[bytes, str]:
    """Fetch a URL with a hard size cap, validating every redirect hop.

    Prevents SSRF via redirect: an attacker can host a public feed that
    redirects to an internal IP. This function validates every Location
    header before following it, and (when a pinning backend is supplied)
    pins each validated hop's hostname to its validated address, closing
    the rebinding window on every hop, not just the first.

    The body is streamed and abandoned as soon as it exceeds ``max_bytes``.
    Checking the size after a buffered read (the previous behaviour) meant a
    hostile feed or image could make the worker hold the whole body in memory
    before the cap rejected it.

    Returns (body, content-type). Raises ValueError when the cap is exceeded.
    """
    for _ in range(MAX_REDIRECTS + 1):
        with client.stream("GET", url, headers=headers) as response:
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise ValueError("Redirect response had no Location header")
                # Resolve relative redirects against the current URL
                next_url = str(httpx.URL(url).join(location))
                validate_outbound_url(next_url)
                if backend is not None:
                    for key, addr in _pins_for_urls([next_url]).items():
                        backend.set_pin(key, addr)
                url = next_url
                continue

            response.raise_for_status()
            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > max_bytes:
                raise ValueError(
                    f"Response too large: {declared} bytes (max {max_bytes})"
                )
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(
                        f"Response too large: exceeded {max_bytes} bytes"
                    )
                chunks.append(chunk)
            return b"".join(chunks), response.headers.get("content-type", "")

    raise ValueError(f"Too many redirects (max {MAX_REDIRECTS})")


def parse_rss_feed(parsed: feedparser.FeedParserDict, url: str) -> dict:
    """Parse an RSS/Atom feed from feedparser output."""
    feed_info = parsed.get("feed", {})

    items = []
    for entry in parsed.get("entries", []):
        # Content HTML is read once and reused: {{ content }} is the
        # stripped text, {{ content_link }} is the first outbound link
        # inside it (recovered before strip_html drops the href).
        content_html = entry.get("content", [{}])[0].get("value", "") if entry.get("content") else ""
        item = {
            "id": _get_item_id(entry),
            "title": clean_text(entry.get("title", "")),
            "link": entry.get("link", ""),
            "summary": clean_text(entry.get("summary", "")),
            "content": strip_html(content_html) if entry.get("content") else clean_text(entry.get("summary", "")),
            "content_link": _extract_first_link(content_html, base_url=entry.get("link", "")),
            "author": entry.get("author", ""),
            "date": _parse_date_struct(entry),
            "tags": [tag.get("term", "") for tag in entry.get("tags", []) if tag.get("term")],
            "image_url": _extract_rss_image(entry),
            "image_alt": _extract_rss_image_alt(entry),
            "raw": {k: v for k, v in entry.items()},
        }
        items.append(item)

    return {
        "title": feed_info.get("title", url),
        "url": url,
        "type": "rss",
        "items": items,
    }


def parse_json_feed(data: dict) -> dict:
    """Parse a JSON Feed (https://jsonfeed.org/)."""
    items = []
    for entry in data.get("items", []):
        # JSON Feed 1.1 uses "authors" (array), 1.0 uses "author" (object)
        author_name = ""
        authors = entry.get("authors") or []
        if authors and isinstance(authors, list):
            author_name = authors[0].get("name", "") if isinstance(authors[0], dict) else ""
        elif entry.get("author") and isinstance(entry.get("author"), dict):
            author_name = entry["author"].get("name", "")

        content_html = entry.get("content_html") or ""
        item = {
            "id": _get_json_item_id(entry),
            "title": entry.get("title", ""),
            "link": entry.get("url", ""),
            "summary": entry.get("summary", ""),
            "content": strip_html(entry.get("content_html") or entry.get("content_text", "")),
            "content_link": _extract_first_link(content_html, base_url=entry.get("url", "")),
            "author": author_name,
            "date": _parse_iso_date(entry.get("date_published") or entry.get("date_modified")),
            "tags": entry.get("tags", []),
            "image_url": _extract_json_feed_image(entry),
            "image_alt": _extract_json_feed_image_alt(entry),
            "raw": entry,
        }
        items.append(item)

    return {
        "title": data.get("title", "Unknown"),
        "url": data.get("feed_url", ""),
        "type": "json",
        "items": items,
    }


def get_new_items(items: list[dict], last_seen_id: str | None) -> list[dict]:
    """Return only items newer than last_seen_id.

    If last_seen_id is None, returns empty list (first run — don't post backlog).
    If last_seen_id is not found in the feed (scrolled off), returns the newest
    item only to prevent backlog spam.
    """
    if last_seen_id is None:
        return []
    if not items:
        return []

    new_items = []
    found_seen = False
    for item in items:
        if item["id"] == last_seen_id:
            found_seen = True
            break
        new_items.append(item)

    if not found_seen:
        # Cursor scrolled off the feed — only post the newest item to avoid spam
        return items[:1] if items else []

    # Items come newest-first in most feeds. Reverse so oldest-new-item posts first.
    new_items.reverse()
    return new_items


def get_backdated_items(
    items: list[dict],
    last_seen_id: str | None,
    max_days: int,
    now: datetime | None = None,
    limit: int = 20,
) -> list[dict]:
    """Return items past the cursor whose publish date is within *max_days*.

    These are entries that appear positionally OLDER than ``last_seen_id`` (so
    :func:`get_new_items` skips them) but carry a publish date inside the
    allowed window — e.g. a blog that backdated a post after FeedEcho already
    saw a newer one.

    Only items **after** the cursor in the feed list are considered. Items
    before the cursor (positionally newer) are returned by
    :func:`get_new_items` and must never be returned here.

    Deduplication is handled downstream by ``_claim_post``'s
    ``ON CONFLICT(echo_id, item_id)`` upsert, so already-posted items are safe
    to return here — they will be silently skipped at delivery time.

    A small forward tolerance (up to 1 day ahead of *now*) absorbs publish-date
    clock skew between the feed server and this host; anything further in the
    future is treated as a broken feed and excluded.

    Returns oldest-first (matching ``get_new_items`` ordering within the
    backdated subset) so a caller posting chronologically within this set
    gets the right order. Positional items from ``get_new_items`` are all
    newer than the cursor; backdated items are all older — the two sets
    are not interleaved, so concatenating them is not chronologically
    monotonic.
    """
    if last_seen_id is None or not items or max_days <= 0:
        return []

    if now is None:
        now = datetime.now(timezone.utc)

    # Locate the cursor; everything *after* it in the list is positionally
    # older and a candidate for backdated delivery.
    cursor_idx = None
    for i, item in enumerate(items):
        if item.get("id") == last_seen_id:
            cursor_idx = i
            break

    if cursor_idx is None:
        # Cursor scrolled off the feed. get_new_items returns items[:1] (the
        # newest) to prevent backlog spam; the backdated scan must not
        # double-handle that item, so start past it.
        cursor_idx = 0  # skip items[0]

    after_cursor = items[cursor_idx + 1:]

    cutoff = now - timedelta(days=max_days)
    future_tolerance = now + timedelta(days=1)

    backdated: list[dict] = []
    for item in after_cursor:
        date_str = item.get("date")
        if not date_str:
            continue
        dt = _parse_item_date(date_str)
        if dt is None:
            continue
        if dt < cutoff or dt > future_tolerance:
            continue
        backdated.append(item)

    if not backdated:
        return []

    # Take the newest *limit* items within the window, then reverse to
    # oldest-first so the caller posts chronologically.
    backdated.sort(key=lambda it: _parse_item_date(it["date"]), reverse=True)
    backdated = backdated[:limit]
    backdated.reverse()
    return backdated


def _parse_item_date(date_str) -> datetime | None:
    """Parse an item date string (ISO 8601 or RFC 822) to an aware UTC datetime.

    Returns None on unparseable or non-string input. Naive datetimes (no tz
    suffix) are assumed to be UTC — feed timestamps are almost always
    server-local and assuming UTC is safer than treating them as local wall
    time.
    """
    if not isinstance(date_str, str):
        return None

    # ISO 8601 (JSON Feed, most modern RSS/Atom).
    try:
        if date_str.endswith("Z"):
            date_str = date_str[:-1] + "+00:00"
        dt = datetime.fromisoformat(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        pass

    # RFC 822 (legacy RSS pubDate).
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(date_str)
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError, OverflowError):
        pass

    return None


def clean_text(text: str) -> str:
    """Strip HTML tags, decode entities, and normalize whitespace."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def strip_html(html_str: str) -> str:
    """Strip all HTML for plain-text output (Mastodon statuses are plain text)."""
    if not html_str:
        return ""
    # Remove script/style blocks
    html_str = re.sub(r"<script[^>]*>.*?</script>", "", html_str, flags=re.DOTALL | re.IGNORECASE)
    html_str = re.sub(r"<style[^>]*>.*?</style>", "", html_str, flags=re.DOTALL | re.IGNORECASE)
    # Strip all tags
    html_str = re.sub(r"<[^>]+>", "", html_str)
    # Decode entities
    html_str = html.unescape(html_str)
    # Normalize whitespace
    html_str = re.sub(r"\s+", " ", html_str).strip()
    return html_str


def clean_html(html_str: str) -> str:
    """Light-clean HTML: kept for backwards compat. Use strip_html for Mastodon output."""
    return strip_html(html_str)


def truncate(text: str, max_len: int = 500) -> str:
    """Truncate text to max_len chars with ellipsis."""
    if len(text) <= max_len:
        return text
    return text[:max_len - 1].rstrip() + "…"


def _extract_first_link(html_str: str, base_url: str = "") -> str:
    """First outbound <a href> inside the content HTML, resolved to absolute.

    Link-blogs (pika.page, Micro.blog, etc.) wrap the headline in an <a>
    pointing at the external article. ``strip_html()`` drops that href while
    keeping the anchor text, so ``{{ content }}`` loses the link even though
    the headline survives. This recovers it for ``{{ content_link }}``.

    Returns the absolute URL, or "" when the content has no link. Relative
    hrefs are resolved against ``base_url`` (the item's own link).
    """
    if not html_str:
        return ""
    # Negative lookbehind on the href name so attribute suffixes like
    # `data-href` / `x-href` are not mistaken for a real `href`.
    match = re.search(r'<a\b[^>]*?(?<![\w-])href\s*=\s*["\']([^"\']+)["\']', html_str, re.IGNORECASE)
    if not match:
        return ""
    href = html.unescape(match.group(1)).strip()
    if not href:
        return ""
    if "://" in href:
        return href
    if href.startswith("//"):
        return "https:" + href
    if base_url:
        return urljoin(base_url, href)
    return href


def _extract_rss_image(entry: dict) -> str:
    """Extract first image URL from an RSS/Atom entry.

    Priority: media_content > media_thumbnail > enclosures > first <img> in content/summary.
    Returns "" if no image found.
    """
    # media_content / media_thumbnail (Media RSS)
    for key in ("media_content", "media_thumbnail"):
        media_list = entry.get(key, [])
        if media_list and isinstance(media_list, list):
            url = media_list[0].get("url", "") if isinstance(media_list[0], dict) else ""
            if url:
                return url

    # RSS enclosures
    for enc in entry.get("enclosures", []):
        if isinstance(enc, dict):
            ctype = enc.get("type", "")
            if ctype.startswith("image/") and enc.get("href"):
                return enc["href"]

    # First <img> in content or summary HTML
    html_content = ""
    if entry.get("content"):
        html_content = entry.get("content", [{}])[0].get("value", "")
    if not html_content:
        html_content = entry.get("summary", "")
    if html_content:
        match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', html_content, re.IGNORECASE)
        if match:
            return match.group(1)

    return ""


def _extract_rss_image_alt(entry: dict) -> str:
    """Alt text for the image _extract_rss_image picked, or "".

    Mirrors its priority order so the caption always belongs to the URL we
    chose: Media RSS media:text on the same media object, enclosure alt, or
    the alt attribute of the content <img> whose src was chosen.
    """
    for key in ("media_content", "media_thumbnail"):
        media_list = entry.get(key, [])
        if media_list and isinstance(media_list, list) and isinstance(media_list[0], dict):
            if media_list[0].get("url", ""):
                text = media_list[0].get("media_text", "")
                if isinstance(text, list) and text:
                    text = text[0].get("text", "") if isinstance(text[0], dict) else ""
                if isinstance(text, str) and text.strip():
                    return text.strip()
                return ""

    for enc in entry.get("enclosures", []):
        if isinstance(enc, dict) and enc.get("type", "").startswith("image/") and enc.get("href"):
            return enc.get("alt", "") or ""

    html_content = ""
    if entry.get("content"):
        html_content = entry.get("content", [{}])[0].get("value", "")
    if not html_content:
        html_content = entry.get("summary", "")
    if not html_content:
        return ""
    # The URL extractor takes the FIRST <img src>; only its own alt applies.
    match = re.search(r"<img\b[^>]*>", html_content, re.IGNORECASE)
    if not match:
        return ""
    tag = match.group(0)
    src = re.search(r'\bsrc=["\']([^"\']+)["\']', tag, re.IGNORECASE)
    if not src:
        return ""
    alt = re.search(r'\balt=["\']([^"\']*)["\']', tag, re.IGNORECASE)
    return alt.group(1).strip() if alt else ""


def _extract_json_feed_image(entry: dict) -> str:
    """Extract first image URL from a JSON Feed entry.

    Priority: image > banner_image > first <img> in content_html.
    JSON Feed 1.1 allows "image" as an object {url, caption} (or a string).
    Returns "" if no image found.
    """
    image = entry.get("image")
    if isinstance(image, dict) and image.get("url"):
        return image["url"]
    if isinstance(image, str) and image:
        return image

    banner = entry.get("banner_image")
    if isinstance(banner, dict) and banner.get("url"):
        return banner["url"]
    if isinstance(banner, str) and banner:
        return banner

    html_content = entry.get("content_html", "")
    if html_content:
        match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', html_content, re.IGNORECASE)
        if match:
            return match.group(1)

    return ""


def _extract_json_feed_image_alt(entry: dict) -> str:
    """Alt text for the image _extract_json_feed_image picked, or "".

    JSON Feed 1.1 allows "image" as an object {url, caption} (or a string);
    caption is the official alt-text slot. Falls back to the content <img>
    alt attribute when the URL came from content_html.
    """
    image = entry.get("image")
    if isinstance(image, dict) and image.get("url"):
        return (image.get("caption") or "").strip()
    if isinstance(image, str) and image:
        return ""

    banner = entry.get("banner_image")
    if isinstance(banner, dict) and banner.get("url"):
        return (banner.get("caption") or "").strip()
    if isinstance(banner, str) and banner:
        return ""

    html_content = entry.get("content_html", "")
    if not html_content:
        return ""
    # Mirror the URL extractor: the FIRST <img> with a src is the chosen one,
    # so only its own alt applies.
    match = re.search(r"<img\b[^>]*>", html_content, re.IGNORECASE)
    while match:
        tag = match.group(0)
        if re.search(r'\bsrc=["\']([^"\']+)["\']', tag, re.IGNORECASE):
            alt = re.search(r'\balt=["\']([^"\']*)["\']', tag, re.IGNORECASE)
            return alt.group(1).strip() if alt else ""
        match = re.search(r"<img\b[^>]*>", html_content[match.end():], re.IGNORECASE)
    return ""


def fetch_image(url: str) -> tuple[bytes, str] | None:
    """Fetch an image for Mastodon media upload.

    Validates URL for SSRF, caps size, and returns (content_bytes, content_type).
    Returns None if the fetch fails, the URL is unsafe, or the content is not an image.
    """
    try:
        validate_outbound_url(url)
    except SSRFError:
        return None

    headers = {"User-Agent": USER_AGENT}
    try:
        client, backend = ssrf_client([url])
        try:
            content, raw_type = _fetch_with_redirect_validation(
                client, url, headers, MAX_IMAGE_SIZE, backend=backend
            )
        finally:
            client.close()
        content_type = raw_type.split(";")[0].strip()

        if content_type not in ALLOWED_IMAGE_TYPES:
            return None

        return content, content_type
    except Exception:
        return None


def _get_item_id(entry: dict) -> str:
    """Get a stable item ID, synthesizing one if the feed lacks guid/link."""
    item_id = entry.get("id") or entry.get("guid") or entry.get("link", "")
    if item_id:
        return item_id
    # Synthesize from title + date + content hash
    seed = (entry.get("title", "") + entry.get("published", "") + entry.get("summary", "")).encode()
    return hashlib.sha256(seed).hexdigest()[:16]


def _get_json_item_id(entry: dict) -> str:
    """Get a stable item ID for JSON Feed entries."""
    item_id = entry.get("id") or entry.get("url", "")
    if item_id:
        return item_id
    seed = (entry.get("title", "") + entry.get("date_published", "") + entry.get("content_text", "")).encode()
    return hashlib.sha256(seed).hexdigest()[:16]


def _parse_date_struct(entry: dict) -> str | None:
    """Parse date from feedparser's struct_time fields (already parsed)."""
    for field in ("published_parsed", "updated_parsed"):
        parsed = entry.get(field)
        if parsed:
            try:
                # timegm, not mktime: feedparser's *_parsed struct_times are
                # already UTC, and mktime interprets them as local wall time,
                # so every item date was shifted by the host's UTC offset.
                dt = datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)
                return dt.isoformat()
            except (ValueError, OverflowError):
                pass
    # Fallback to raw string
    return entry.get("published") or entry.get("updated")


def _parse_iso_date(date_str: str | None) -> str | None:
    """Parse an ISO 8601 date string."""
    if not date_str:
        return None
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.isoformat()
    except (ValueError, TypeError):
        return date_str
