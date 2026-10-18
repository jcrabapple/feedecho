# F4 — Feed discovery + preview (feed_parser.py)

## Goal

Feed-adding today requires pasting an exact RSS/Atom/JSON URL. Add HTML
`<link rel="alternate">` autodiscovery so pasting `https://example.com`
resolves to `https://example.com/feed.xml`. The API surface (routes,
feeds.html prefill, bookmarklet, auto-resolution in add_feed/edit_feed)
is wired by wave 2; this lane delivers the parser + its unit tests.

## Files

- Modify: `feed_parser.py` (ONLY this lane's file)
- Test (new): `tests/test_feed_discovery.py` (reuses the existing
  `tests/test_conditional_get.py` httpx mock seam — read that file first
  for the established MockTransport / ssrf_client pattern)

## New function in feed_parser.py

```python
def discover_feed_url(url: str, max_page_bytes: int = 2 * 1024 * 1024) -> dict:
```

Return shape (always a dict with these keys):
```python
{"found": bool,
 "url": str | None,        # best feed URL (input URL itself when input IS a feed)
 "title": str,             # page <title> or feed's own title ("" if unknown)
 "alternates": [str, ...]} # all valid alternates in document order; best first
```

### Rules

1. `validate_outbound_url(url)` first — SSRFError propagates unchanged.
2. Fetch with `ssrf_client([url])` + `_fetch_with_redirect_validation(...,
   max_page_bytes)` (same conditional-GET style as `fetch_feed`; pinned-IP
   transport; every redirect hop validated). The size cap applies; a body
   exceeding it is abandoned exactly like `fetch_feed`.
3. Content-type dispatch (read the final response content-type):
   - `text/html` → HTML branch (below).
   - anything else → non-HTML branch: run `feedparser.parse(content)` and
     if `parsed.feed.title` extracts, return it as `title`; the URL is
     returned UNCHANGED (`found` = True when the parser yields a feed
     title or >= 0 entries... simplify: `found = not parsed.bozo or bool(parsed.version)`);
     ITS OWN URL is echoed back (`url: url`).
4. HTML branch — parse `<link>` tags with a small local HTMLParser subclass
   mirroring the module's existing `_RichHTMLParser` pattern style (it is
   OK to use a feedparser-neutral regex if HTMLParser proves too
   tighten for the file's style; prefer HTMLParser):
   collect every tag's `rel`, `type`, `href` where `rel` (lowercased)
   == "alternate" AND `type` (lowercased) in
   `("application/rss+xml", "application/atom+xml", "application/feed+json")`.
   Also accept `type="application/json"` ONLY when the href ends `.json`
   (some sites publish JSON Feed that way).
5. For each collected link (document order), href is
   `urljoin(final_response_url, href)` then ABS-validated with
   `validate_outbound_url(resolved)`; SSRF-blocked candidates are SKIPPED
   silently (continue) — a page with one 127.0.0.1 link and one real link
   must resolve the real one, not 500.
6. `title`: extract the page's own `<title>` text (html.unescape'd) as
   the default; leave `""` if absent.
7. NO alternate links → return `{"found": False, "url": None, "title":
   page_title_or_empty, "alternates": []}`.
8. Duplicates: de-dup alternates by normalized URL string
   `rstrip("/")`; first occurrence wins.

Network errors (httpx.HTTPError) RAISE — the caller decides whether to
fall back (wave 2 falls back to the raw URL).

## Tests

`tests/test_feed_discovery.py`:

1. HTML with exactly one RSS alternates link → resolves.
2. HTML with 3 links (mixed rss/atom/json) → document order respected,
   first wins; alternates list contains all three (best first).
3. HTML with NO links → found=False, url=None, alternates=[], title
   extracted from <title>.
4. HTML with `rel=alternate` qq but no `type` attribute → skipped.
5. Relative href → resolved against the page URL; `urljoin` handles
   `../feed.xml`, full URL, and root-relative.
6. Entity-escaped href (`&amp;` in query string) → parsed correctly
   (HTMLParser gives decoded attr).
7. An alternate whose URL is SSRF-blocked (`http://127.0.0.1/feed.xml`)
   + one valid → skip blocked, use valid (no exception).
8. Input URL that itself points to a private IP → SSRFError propagates.
9. non-HTML content-type (e.g. `application/rss+xml`) → returned as-is
   (url unchanged), title extracted from feed when feedparser yields
   one, found=True.
10. fetch exceeding max_page_bytes → raises like fetch_feed does
    (ValueError-based cap). One light test.
11. Red-before-green evidence per coder rules.
