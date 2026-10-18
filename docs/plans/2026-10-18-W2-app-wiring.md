# Wave 2 — App wiring spec (single sequential lane)

You are the integrator lane. Wave 1 (filters.py / scheduler.py /
template_engine.py / feed_parser.py lanes) has been committed. Your job is
everything that touches `app.py` (multiple regions), templates, app.js, and
the import clamp — the four features together, integrated.

**Owned files:** `app.py`, `import_export.py`, `templates/echoes.html`,
`templates/feeds.html`, `templates/queue.html`, `templates/history.html`,
`templates/dashboard.html`, `templates/howto.html`, `templates/base.html`,
`static/js/app.js`, `README.md`, and ONE new test file
`tests/test_delivery_modes.py`. Nothing else. NO commits (orchestrator
commits).

## Read first

- `docs/plans/2026-10-18-four-features.md`, and each feature's design doc
  `2026-10-18-F{1,2,3,4}-*.md` (they list every surface contract).
- All four wave-1 implementation files (their final committed shape is
  the source of truth for the helpers you call).

## F1 surfaces (filter field syntax)

1. `templates/echoes.html` create-form filter input `hint` span text: extend
   to mention optional field scoping:
   `Comma-separated, case-insensitive. Plain text matches title and summary; prefixes `tag:`, `author:`, `url:` (or `link:`), and `content:` match that field.`
   Verify no Jinja unclosed braces.
2. `static/js/app.js` — editEcho's inline edit form has a
   `filter_keywords` input; update its `placeholder` to
   `e.g. spoiler, tag:posse, author:jason` (keep the rest of the row as-is —
   no hint in the edit form).
3. `templates/howto.html` section 3 "Optional rules" Filter keywords bullet —
   add the field-prefix explanation (same words as #1).
4. `README.md` — the `filter_keywords` bullet in the features list and
   anywhere filters are described: add field prefixes. The exact sections
   live at the bottom of `/echoes` in the feature list under
   "Optional rules" / filter lines; keep the README's overall style
   (bold lead phrases, `code blocks` inline).

## F2 surfaces (delivery modes queue/review)

5. `app.py` `add_echo` + `edit_echo`: replace the validation line
   `if delivery_mode not in ("instant", "digest"): raise ...` with the
   real `VALID_DELIVERY_MODES = ("instant", "digest", "queue", "review")`.
   Digest mode stays email-only (unchanged rule). Add to `edit_echo`,
   right after the validation, the same behavior for non-Mastodon
   destinations: nothing extra (queue/review apply to every destination).
   NO plan-cap change at the route (scheduler handles cap at enqueue).
6. `app.py` `queue_page`: include status `'draft'` in the SELECT
   `WHERE q.user_id = ? AND q.status IN ('queued', 'sending', 'failed')` →
   add `'draft'`. Also the template badge conditions.
7. `app.py` `queue_cancel`: allow canceling draft rows — update the
   status check `if row["status"] not in ("queued", "failed")` → add
   `"draft"`; the DELETE (echo + posted rows) works fine, but now the
   DELETE also applies to a canceled draft. Note in the docstring? Keep
   the curl style minimal.
8. `app.py` `queue_edit`: allow editing drafts the same way — include
   `'draft'` next to `'queued'` in the status guard.
9. `app.py` `delete_feed`: the queued-cleanup UPDATE
   `WHERE feed_id = ? AND status = 'queued'` → add draft rows too
   (`IN ('queued','draft')`).
10. `import_export.py` `_VALID_DELIVERY_MODES` → widen to
    `("instant", "digest", "queue", "review")`.
11. `templates/echoes.html` build form: the delivery-mode select currently
    lives in the `#digest-fields` email-only row. Replace that row with an
    ALWAYS-VISIBLE row showing a select with the four modes; the
    digest option is disabled unless the currently selected destination
    type is `email` (JS wires it). Rename the row id (e.g.
    `delivery-mode-fields`). Update `toggleDestFields` JS to toggle the
    digest-option disabled flag and to hide the `drip-fields` row unless
    `delivery_mode === 'instant'`.
12. `static/js/app.js` editEcho: the inline edit form's delivery-mode
    select gets the same 4 options + disabled-digest logic via the
    `toggleEditDest` path (it already keys off the select). Keep the
    data-carrying attribute `data-delivery-mode`.
13. `templates/echoes.html` row badges: add badges for the new modes
    (like DIGEST/DRIP today):
    `AUTO-QUEUE` badge when `delivery_mode == 'queue'` and
    `REVIEW` badge when `delivery_mode == 'review'`.
14. `templates/queue.html` badge: add `draft` → badge-info in the status
    enum (same style line).
15. `templates/history.html` status text: add `draft` → same color ---
    history treats queued as info today; do the same for draft. Check
    dashboard.html's queue count/statuses — if it hardcodes an IN list
    over statuses it is wave-2 to widen.
16. How-To + README: add bullets for the new modes alongside the existing
    Drip/Digest bullets ("Auto-queue", "Review-first").

## F3 surfaces (template filters)

17. `template_engine.available_variables()` was already extended in F3's
    wave (its OWN lane edits available_variables). Do not re-edit it.
    Wave-1's template lane already does the extension
    (docs/plans/2026-10-18-F3-url-filters.md item 4). Confirm the
    function includes those 3 rows; if it did not, apply them yourself
    (note it in your summary).
18. `README.md` template table: add `| \`{{ link | clean_url }}\` |`,
    `| \`{{ link | utm(...) }}\` |`, and the hashtags-with-exclude row in
    the vars table with descriptions matching
    `template_engine.available_variables()`.
19. `templates/howto.html` — the Template docs section (section 3's hints
    or a separate "Link UTM / clean URL" bullet) mentions the three new
    filters; if you add literal `{{ ... }}` in this file, remember the
    document is a Jinja template and needs `{% raw %}` wrapping.
20. `templates/echoes.html` "Available template variables" table is
    server-rendered from `available_variables()` — no edit needed there.

## F4 surfaces (discovery / preview / ?add=)

21. `app.py` — reuse the discovered URL in `add_feed`: for a NEW feed
    (not a re-add upsert), call `feed_parser.discover_feed_url` with the
    pasted URL. On success (found), overwrite the stored `url` value.
    On ANY failure (network, 404, SSRFError, ValueError from feedparser),
    fall back to using the pasted URL unchanged. Because the feature
    runs at add-time only (not every poll), put the discovery call
    INSIDE the existing `with get_db() as db` block right before the
    INSERT (it's HTTP-bound; the synchronous route is fine).
    IMPORTANT: validate the RESOLVED url with `validate_url` (app.py's own
    accepts the same rules; the discovered URL has already been validated
    by the parser with SSRFError, but treat it the same — pass it through
    app.validate_url for scheme/host consistency).
22. `app.py` `edit_feed`: same auto-discovery when the URL CHANGEs (url !=
    feed["url"]). Reuse the same small helper.
23. New route `@app.get("/api/feeds/discover")`:
    ```python
    @app.get("/api/feeds/discover")
    def discover_feed_api(request: Request, url: str = Query(...)):
        uid = current_user_id(request)
        url = validate_url(url)
        try:
            info = discover_feed_url(url)
        except (httpx.HTTPError, SSRFError) as e:
            raise HTTPException(status_code=400, detail=f"Could not discover feeds for {url}: {e}")
        return {"found": bool(info.get("url")), "url": info.get("url"),
                "title": info.get("title") or "", "alternates": info.get("alternates") or []}
    ```
    Use the project's conventions (check how other routes import things —
    the `feed_parser` import already exists; you may need
    `from feed_parser import SSRFError` if not already imported at module
    scope, or reach through `feed_parser.SSRFError`).
24. New route `@app.post("/api/feeds/preview")`:
    `def test_feed_preview(request, url: str = Form(...))`: validates url,
    calls `feed_parser.fetch_feed(url)` directly (no plans cap, no DB,
    no feed row needed), returns the same JSON shape as
    `POST /api/feeds/{feed_id}/test` (read that route's return shape and
    reuse its item-list builder; if that route builds a dict inline,
    extract a small local helper `_preview_payload(feed_data)` used by
    both routes).
    Note: this is an auth-protected route (the token/session middleware
    gate); no extra auth logic needed.
25. `app.py` `feeds_page`: accept `?add=<raw url>` and `?name=<raw text>`
    query params. Pass them, pre-fill the form via the page template
    (`feeds.html`) and a small JS bootstrap that runs on DOMContentLoaded:
    ```js
    const addUrl = "{{ add_url | urlencode }}";  // from feeds_page context
    ```
    The JS (`static/js/app.js`, add functions `discoverFeed(btn)` and a
    `DOMContentLoaded` wire-up): call `GET /api/feeds/discover?url=<encoded url>`,
    then fill the `#feed-url` input with the resolved `url` and the
    `#feed-name` input with `title` (only when the user left the name
    empty or came from `?name=`). Render a status line in an existing
    alert element. Failures degrade to no prefacing (stays a no-op). This
    does not add a new route path — it calls the API from #23.
26. STARTING A BOOKMARKLET: `templates/feeds.html` gets a small
    `<details>` (above the Add form) titled "Add from any page" with a
    copyable `javascript:`-free code block (a SHORT, escaped snippet)
    that opens `/feeds?add=<page url>`. Because a bookmarklet needs the
    right base URL at runtime, TEMPLATE the snippet: render
    `{{ request.url.scheme }}://{{ request.url.netloc }}` inline in the
    code block (the hostname the user actually opened this page on).
    Full JS inside `{% raw %}` to survive Jinja. Example shape:
    `<a href="javascript:(function(){var u=location.href;if(u.indexOf('?add=')<0){u=u.split('#')[0];}location.href='/feeds?add='+encodeURIComponent(u);})()">+ Subscribe</a>`
    (+ full styling); keep it SHORT and manual (the `<pre>` form needs
    to copy). Test the unimaginable in manual browser pass; tests only
    assert the code block is PRESENT in html.
27. `templates/base.html` cache-bust: `<script src="/static/js/app.js?v=57">`
    → v=58.
28. `tests/test_ux_p2_fixes.py` line ~381 pins the version: change the
    `app.js?v=57` pin to `v=58` IN THE SAME commit (otherwise CI fails).
    Keep the style.css pin untouched.
29. `README.md` F4 bullets: "Add a feed" section bullet about HTML link
    autodiscovery. How-To section 1 bullet tip about auto-discovery too
    (one sentence: paste a site URL; FeedEcho resolves the feed link
    automatically). The bookmarklet itself is `/feeds`-page visible, not
    README; skip README mention of the bookmarklet.

## Tests (new file only)

`tests/test_delivery_modes.py`:
1. POST `/api/echoes` with `delivery_mode=queue` and a mastodon echo
   creates the echo row with the right delivery_mode (route-level, use
   the TestClient single-mode fixture pattern like other test files;
   find an existing echo-adding test for the exact fixture pattern and
   follow it).
2. Same with `review` (assert row's `delivery_mode`).
3. `delivery_mode=digest` on a mastodon destination STILL 400s (the
   existing email-only rule for digest is intact).
4. `import_export` round-trip: an export from an echo with
   `delivery_mode=queue` re-imports with the same mode (small, single
   record, reuse the existing Import/Export test pattern).
5. Route-level queue-page: with a draft post in queued_posts the
   `/queue` HTML includes its row (uses existing single-mode fixture
   styling).
6. `/api/feeds/discover` route-level: monkeypatch
   `feed_parser.discover_feed_url` (or `discovery` module-local shim) to
   return a dictionary and assert the JSON shape. For 400-path, raise
   httpx.HTTPError and assert 400.
7. NO test asserts the bookmarklet (JS-only); the test asserts the
   `/feeds` page contains the new `<details>` + snippet code block.
8. Red-before-green evidence per coder rules.
