# FeedEcho — Correctness Bug Review

**Date:** 2026-09-09
**Scope:** Entire tracked source tree at commit `7cd5ad8` (master/`fix/ux-navigation-gaps`, v1.51.1) — all 26 top-level Python modules, `templates/`, `static/js/app.js`.
**Method:** 9 parallel reviews, each assigned a set of files, instructed to find correctness bugs only — not the code-quality/complexity/duplication/design-pattern/UX issues already covered by the five 2026-09-08 audits and the 2026-08-28 Kimi reviews in this directory. Each finding below was verified by direct code read (several were confirmed by executing the actual logic against realistic inputs, e.g. the RSS media-type bug, the admin-panel XSS, the SMTP header bug). Known findings from the 2026-08-28 Kimi review were re-checked against current code; most (email CRLF validation on `add_email_account`, micro.blog plan-cap counting, masked-secret sentinel handling) are now fixed and are not repeated here — only genuinely new/remaining bugs are listed.

Every deployment currently runs SQLite (per project context); the two Postgres-only findings (#4, #5, and the related note in #18) are latent — they will surface the moment a Postgres deployment exists, not before.

## Summary

| # | Finding | Location | Severity |
|---|---|---|---|
| 1 | Stored XSS in admin panel via inline JS handlers built from an unescaped-for-JS-context user email | `templates/admin.html` | **Critical** |
| 2 | Editing a Discord or Webhook echo silently reassigns its destination or fails | `static/js/app.js` | **Critical** |
| 3 | `mastodon.py` has no permanent/transient error classification — a revoked token is retried forever as if transient | `mastodon.py`, `scheduler.py` | **High** |
| 4 | `accounts.username` has a SQLite backfill migration but no Postgres equivalent | `database.py` | **High** |
| 5 | Postgres `CURRENT_TIMESTAMP`/`NOW()` is session-timezone-dependent, violating the app's UTC invariant | `database.py` | **High** |
| 6 | Account import has no field validation — malformed import crashes with a raw DB exception | `import_export.py` | **High** |
| 7 | Digest/drip flush loops have no per-item exception isolation — one bad row stalls the whole batch | `scheduler.py` | **High** |
| 8 | Reconnecting a webhook account silently wipes its existing headers | `app.py` | **High** |
| 9 | RSS image picker doesn't filter by media type — can return a video URL as an item's "image" | `feed_parser.py` | **High** |
| 10 | Bluesky truncation doesn't enforce the 3000-byte cap and mis-implements ZWJ grapheme clustering | `bluesky.py` | Medium |
| 11 | Email Subject header not sanitized against embedded CRLF — permanently breaks delivery for that item | `email_sender.py`, `scheduler.py` | Medium |
| 12 | Dashboard "Failed" stat excludes `gave_up` posts | `app.py` | Medium |
| 13 | Saved-search unread badge doesn't match what the saved search shows when opened | `app.py` | Medium |
| 14 | `edit_feed`/folder rename/delete/`set_feed_folder` don't invalidate the saved-search counts cache | `app.py` | Medium |
| 15 | Email sender re-validates its claim lease before slow image-fetch I/O instead of after | `scheduler.py` | Medium |
| 16 | Only Matrix has an idempotency key against crash-then-reclaim duplicate posts | `scheduler.py` | Medium |
| 17 | OAuth `STATE_SECRET` fallback ignores the multi-mode gate its own comment says it mirrors | `oauth.py` | Medium |
| 18 | Other backfilled SQLite columns likely also missing from `init_db_postgres` | `database.py` | Medium |
| 19 | `feeds`/`accounts` tables have no DB-level UNIQUE constraint backing import's dedup check | `database.py` | Medium |
| 20 | "Extend trial" confirmation dialog never appears — unconditional JS syntax error | `templates/admin.html` | Medium |
| 21 | Folder rename/delete has the same inline-JS string-breakout as #1, but self-XSS only | `templates/feeds.html` | Medium |
| 22 | TOCTOU race between dependent-echo check and destination delete | `app.py` | Low |
| 23 | `readerLoadMore()` calls a nonexistent function — new items never get localized timestamps | `static/js/app.js` | Low |
| 24 | Matrix can linkify a URL that truncation cut in half | `matrix.py` | Low |
| 25 | Verification-token race can delete an already-issued, already-sent token | `verification.py` | Low |
| 26 | `poll_interval: 0` on import is treated as missing, not clamped to the real floor | `import_export.py` | Low |
| 27 | Discord `webhook_url_hash` not recomputed when `webhook_url` is empty on import | `import_export.py` | Low |

---

## Critical

### 1. Stored XSS in the admin panel via inline JS handlers built from a user's email
`templates/admin.html:68,74,79,84,95`

`u.email` is interpolated into `onsubmit="return confirm('... {{ u.email }} ...')"` inline event-handler attributes on the Suspend/Promote/Demote/Set-plan/Extend-trial forms. Jinja's autoescape HTML-escapes quotes, but the browser HTML-*decodes* attribute entities before compiling the inline handler as JS — so escaping does not prevent breaking out of the JS string.

`utils.EMAIL_RE = r"^[^@\s\r\n]+@[^@\s\r\n]+\.[^@\s\r\n]+$"` (confirmed by direct read) only excludes `@`, whitespace, and CR/LF from the local part — it does not exclude quotes or parens. An email like `x'-alert(document.domain)-'@a.co` passes registration validation.

**Failure scenario:** register with that email. When any admin opens `/admin` and clicks Suspend/Promote/Demote/etc. on that row, the crafted JS runs in the admin's authenticated session — session-cookie theft, or a same-origin `fetch()` to self-promote the attacker to admin (no CSRF token gates these admin POSTs).

**Fix:** never interpolate user data into inline event-handler attributes. Move to `data-*` attributes read by an external JS handler (as `editFeed`/`editEcho` already do), or build the confirm text via `textContent`.

### 2. Editing a Discord or Webhook echo silently reassigns its destination or fails
`static/js/app.js:258-434` (`editEcho`, `toggleEditDest`)

`editEcho()` builds destination-specific option lists/field visibility only for `mastodon`, `email`, `bluesky`, `microblog`, `matrix` — it has no branch at all for `discord`/`webhook`, even though the create form and the server (`app.py:5526-5586`, `VALID_DEST_TYPES`) fully support both.

**Failure scenario:** a user with a Discord or Webhook echo clicks Edit. The destination-type select never contains a Discord/Webhook option, so it silently falls back to whatever option is first. Saving reassigns the echo to an unrelated destination/account with no warning — content meant for one place (e.g. a private webhook) can start going somewhere else entirely. If the user has no other destination type, Save 400s.

**Fix:** add `discord-fields`/`webhook-fields` blocks and corresponding `<option>`/visibility handling in `editEcho()`/`toggleEditDest()`, mirroring the other five types.

---

## High

### 3. `mastodon.py` has no permanent/transient error classification
`mastodon.py` (`post_status`, `upload_media`, `verify_credentials`), consumed at `scheduler.py:1305-1317`

Every other destination module (bluesky, matrix, discord, webhook, microblog) defines an `*AuthError`/permanent-failure exception, and the scheduler passes `permanent=True` on it. `mastodon.py` has no exception hierarchy — `raise_for_status()` produces the same generic `httpx.HTTPStatusError` for a 401 as a 429 or a 500, and `_send_mastodon`'s bare `except Exception` always retries with backoff, never marking the post `gave_up` immediately.

**Failure scenario:** a user revokes/regenerates their Mastodon token. Every post attempt gets HTTP 401 but is retried as if transient — wasting the retry budget, or (with `retry_max_attempts=0`, "retry forever") retrying indefinitely and never surfacing "reconnect your account."

**Fix:** add a `MastodonAuthError` on 401/403 and pass `permanent=True` for it in `_send_mastodon`, matching the other five senders.

### 4. `accounts.username` has a SQLite backfill migration but no Postgres equivalent
`database.py:310-326` (SQLite) vs. `database.py:967-1015` (`init_db_postgres`)

SQLite detects a pre-migration `accounts` table and `ALTER TABLE ... ADD COLUMN username` with a regex backfill from `name`. `init_db_postgres` has no equivalent `_add_column_if_missing` call — `CREATE TABLE IF NOT EXISTS` is a no-op on an existing table.

**Failure scenario:** a hosted Postgres database created before `username` existed never gets the column. Every query touching `accounts.username` (including `import_export.py:42`'s account column list) raises `column "username" does not exist` and crashes.

**Fix:** add the same `_add_column_if_missing(db, "accounts", "username", ...)` + backfill to `init_db_postgres`.

### 5. Postgres `CURRENT_TIMESTAMP`/`NOW()` is session-timezone-dependent
`database.py:76-81` (documents the UTC invariant) vs. every `TIMESTAMP DEFAULT CURRENT_TIMESTAMP` column and `database.py:1400-1404`

SQLite's `CURRENT_TIMESTAMP` is always UTC; Postgres's resolves in the session/server timezone before being cast to a naive `TIMESTAMP` column. `_pg_connect()` never pins the session timezone to UTC.

**Failure scenario:** Postgres server timezone set to e.g. `America/New_York` → a row inserted at 15:00 UTC is stored as 10:00 (naive, local wall-clock), but the app's `as_utc_naive()`/`timestamp_str()` treat all naive values as UTC — a 5-hour skew propagates into every retention/ordering/expiry comparison against `scheduler._now()`.

**Fix:** bind an explicit UTC value from Python instead of `DEFAULT CURRENT_TIMESTAMP`/`NOW()`, or force the Postgres session timezone to UTC on connect.

### 6. Account import has no field validation
`import_export.py:154-203` (`_validate_payload` only checks `"id" in account`), `:415-433` (insert loop)

Unlike feeds (which validate `url` is non-empty), accounts are inserted with `account.get(col)` for every column, no presence/type check, against tables with `NOT NULL` columns and no default (`accounts.access_token`/`instance`, `bluesky_accounts.handle`/`app_password`, `matrix_accounts.homeserver`/`room_id`/`access_token`, etc.).

**Failure scenario:** an import document like `{"accounts": {"mastodon": [{"id": 1}]}}` (missing `access_token`/`instance`) binds `NULL` into a `NOT NULL` column and raises an unhandled DB exception — not the `ExportError` the module's own docstring promises ("never a bare KeyError/TypeError").

**Fix:** validate each account's NOT-NULL columns are present and correctly typed before insert, raising `ExportError` otherwise.

### 7. Digest/drip flush loops have no per-item exception isolation
`scheduler.py:2689-2827` (`_flush_digests`), `:2506-2589` (`_flush_drips`)

Both iterate over unrelated echoes/tenants but only wrap a narrow inner call in try/except — unlike `_flush_queue` (`scheduler.py:2412-2484`), which correctly wraps the *entire* per-row dispatch+finalize sequence.

**Failure scenario:** an uncaught exception on one row (a transient DB error during finalize, an unexpected data shape) propagates out of the whole function. The job-lease `finally` prevents a hang, but every other tenant's pending digest/drip items in that batch are silently skipped for the tick. A row that reliably reproduces the exception is a permanent poison pill blocking all users scheduled after it.

**Fix:** wrap each row's processing in its own try/except, mirroring `_flush_queue`.

### 8. Reconnecting a webhook account silently wipes its existing headers
`app.py:3685-3740` (`add_webhook_account`)

This route is the only way to rename or update a webhook (no separate rename endpoint). `ON CONFLICT ... DO UPDATE SET headers = excluded.headers` unconditionally overwrites `headers` with the submitted `headers_text`, which defaults to `""` and — per the route's own docstring — is never pre-filled with the existing value ("stored like credentials and never shown again").

**Failure scenario:** a user resubmits the connect form with the same URL just to fix the display name, leaving Headers blank as the UI naturally invites. The row's headers become `{}`. The next delivery to an endpoint that required `Authorization: Bearer ...` is rejected with no indication the cause was the earlier "rename."

**Fix:** only overwrite `headers` when `headers_text` is non-empty (`CASE WHEN excluded.headers != '{}' THEN excluded.headers ELSE webhook_accounts.headers END`), or split rename from reconnect into separate routes.

### 9. RSS image picker doesn't filter by media type
`feed_parser.py:979-985` (`_extract_rss_image`), `:1015-1024` (`_extract_rss_image_alt`)

`_extract_rss_image` loops over `media_content`/`media_thumbnail` and returns the first entry's URL with no check that the medium is actually an image — unlike its sibling `_extract_rss_images` (used for `image_urls`), which correctly filters via `_is_image_media`. Confirmed by running:
```python
entry = {"media_content": [
    {"url": ".../clip.mp4", "medium": "video", "type": "video/mp4"},
    {"url": ".../photo.jpg", "medium": "image", "type": "image/jpeg"},
]}
_extract_rss_image(entry)   # -> the video URL (wrong)
_extract_rss_images(entry)  # -> the photo (correct)
```

**Failure scenario:** any Media RSS feed listing a video alongside a real poster/thumbnail (common for video/podcast feeds) gets a video URL as its `image_url` — broken in any client rendering it directly, or silently no-image if it later fails a content-type check.

**Fix:** apply `_is_image_media()` inside `_extract_rss_image`'s and `_extract_rss_image_alt`'s loops, same as `_extract_rss_images`.

---

## Medium

### 10. Bluesky truncation doesn't enforce the 3000-byte cap and mis-implements ZWJ grapheme clustering
`bluesky.py:360-407` (`_grapheme_clusters`, `truncate_graphemes`)

The AT Protocol caps post `text` at both 300 graphemes *and* 3000 UTF-8 bytes independently; only the grapheme count is enforced here. Separately, the ZWJ "glue in both directions" rule merges any character following a ZWJ into the previous cluster, not just emoji (Extended_Pictographic) as UAX #29 requires — confirmed against Python's `regex` module that `"c‍d"` is 2 true clusters but this code counts 1, meaning under-counting here lets *more* true graphemes through than truncation intends (the opposite of the module's own "can only under-count, so truncation never exceeds limits" docstring claim).

**Failure scenario:** heavy-diacritic/zalgo text can have few grapheme clusters but tens of thousands of bytes, passing untruncated and getting a generic 400 from the PDS; or ZWJ-using non-emoji script text retains more than 300 true graphemes after "truncation," same result. Because the rejection isn't `ExpiredToken`/`InvalidToken`, the scheduler treats it as transient and retries the same doomed content until the retry cap exhausts.

**Fix:** also enforce `len(text.encode("utf-8")) <= 3000`; restrict the ZWJ glue-forward rule to Extended_Pictographic follow characters only.

### 11. Email Subject header not sanitized against embedded CRLF
`email_sender.py:135` (`root["Subject"] = subject`), fed from `scheduler.py:1392-1395` (raw feed item title)

Every other header-bound field (SMTP host/username/from-name/from-email, the `To` address) is explicitly validated against `[\r\n]` at save time. The email Subject is built per-send directly from an untrusted feed item title with no sanitization anywhere.

**Failure scenario:** a feed item title containing a raw `\r`/`\n` reaches `_send_via`, which connects and authenticates to the SMTP server, then raises `HeaderParseError`/`HeaderWriteError` serializing the message. Since the title never changes, every retry fails identically — permanently breaking delivery for that item with a generic "Email delivery failed," no hint the actual cause is a malformed title.

**Fix:** strip/replace `[\r\n]+` in `subject` before assigning it to the header.

### 12. Dashboard "Failed" stat excludes `gave_up` posts
`app.py:1305-1309`

The dashboard counts only `pi.status = 'failed'`, while the admin usage view correctly counts `status IN ('failed', 'gave_up')` for the same metric. `gave_up` is the terminal status for posts that exhausted their retry budget (`scheduler.py:1027-1075`).

**Failure scenario:** a post exhausts retries and becomes `gave_up` — the user-facing "Failed" count on their own dashboard doesn't reflect it, understating the most significant failure class (permanent, not transient) on the primary at-a-glance stat.

**Fix:** `WHERE pi.status IN ('failed', 'gave_up')`, matching `_admin_usage`.

### 13. Saved-search unread badge doesn't match what the saved search shows when opened
`app.py:2566-2606` (item query), `:2738-2801` (badge count)

The sidebar badge defaults to unread-only unless the saved query text contains `is:read`/`is:starred`. But the actual `/reader?saved=<id>` item query only adds `is_read = 0` via an `elif view == "unread"` branch that's unreachable whenever `q` is non-empty — which it always is for a saved search. No implicit read-state filter applies there unless the query text itself has an `is:` operator.

**Failure scenario:** a saved search `feed:security` (no `is:` operator) shows "3" in the sidebar (unread-only count) but opens to show all 15 matching items regardless of read state — a silently wrong badge for every saved search that doesn't explicitly scope on read state (the common case).

**Fix:** make the two code paths use the same read-state semantics — either drop the sidebar's implicit unread default, or always apply it in both places.

### 14. `edit_feed`/folder rename/delete/`set_feed_folder` don't invalidate the saved-search counts cache
`app.py:4520-4586` (`edit_feed`), `:4150-4220` (`rename_folder`/`delete_folder`/`set_feed_folder`)

`edit_feed` can change `mute_keywords`/`folder_id`, both direct inputs to the cached count query; the two mute routes explicitly invalidate the cache with a comment explaining why, `edit_feed` doesn't. Similarly, folder rename/delete/reassignment change `folder:`-scoped saved-search membership but never call `.invalidate(uid)`, unlike `delete_feed`/`toggle_reader_feed`.

**Failure scenario:** user edits a feed's mute keywords, or renames/moves a folder that a saved search filters on. Displayed unread counts stay stale for up to 60 seconds (the cache TTL).

**Fix:** add `_saved_search_counts_cache.invalidate(uid)` at the end of all four routes, consistent with the rest of the codebase.

### 15. Email sender re-validates its claim lease before slow image-fetch I/O instead of after
`scheduler.py:1357-1387` (`_send_email_echo`)

Every other destination sender calls `_guard_claim` immediately before the irreversible send, *after* any slow image-fetch/alt-text pipeline — specifically because that pipeline is what can let the 10-minute reclaim window elapse. `_send_email_echo` calls `_guard_claim` first, then runs up to 4 image fetches, then sends with no re-check in between.

**Failure scenario:** an echo with image attachment has a slow/hanging image URL; cumulative fetch time exceeds `PENDING_RECLAIM_SECONDS`. A second worker reclaims the row and sends its own copy. The first worker, having already passed its guard check, proceeds to send too — a duplicate email.

**Fix:** move `_guard_claim` to immediately before `send_email(...)`, after the image-embedding block, matching the other five senders.

### 16. Only Matrix has an idempotency key against crash-then-reclaim duplicate posts
`scheduler.py:590-660` (`_claim_post`), `:1867-1869` (Matrix's `matrix_transaction_id`)

`_claim_post` reclaims a stale `'pending'` row after 10 minutes regardless of *why* it's stale, including a worker that crashed after successfully posting but before finalizing. Matrix alone derives a deterministic transaction ID so a homeserver-side retry dedupes. Mastodon, Bluesky, micro.blog, Discord, webhook, and email have no equivalent.

**Failure scenario:** a worker posts to Mastodon, then the process is killed before `_finalize_success` runs. Over 10 minutes later the row is reclaimed and reposted — a genuine duplicate on the destination.

**Fix:** apply the same idempotency-key pattern to the other senders where the destination API supports it, or add a way to distinguish "still running" from "crashed" more safely.

### 17. OAuth `STATE_SECRET` fallback ignores the multi-mode gate its own comment says it mirrors
`oauth.py:30`

The comment claims this mirrors `security.session_secret()`, which explicitly `raise`s if `settings.MULTI and not settings.SESSION_SECRET`. The actual code unconditionally falls back to `AUTH_TOKEN` (then a random value) whenever `STATE_SECRET` is empty, with no mode check, and `validate_config()` never enforces `STATE_SECRET` the way it enforces `SESSION_SECRET`.

**Failure scenario:** a multi-mode operator sets `SESSION_SECRET`/`CREDENTIAL_KEY` but forgets `FEEDECHO_STATE_SECRET` (nothing warns), while `FEEDECHO_AUTH_TOKEN` is still present from a carried-over single-mode config. The OAuth-state HMAC key silently becomes a possibly low-entropy value with no minimum-length enforcement — exactly the scenario the code's own comment says must never happen (blast radius is reduced by `_verify_state` also requiring a matching server-side row, so this isn't independently exploitable, but the invariant is unenforced).

**Fix:** gate the fallback exactly like `session_secret()`; add a `validate_config()` check requiring `FEEDECHO_STATE_SECRET` in multi mode.

### 18. Other backfilled SQLite columns likely also missing from `init_db_postgres`
`database.py:378-387` (feeds), `:461-476` (echoes), `:768-777` (posted_items) vs. `init_db_postgres` (`:1069-1138`, `:1269-1285`, which only backfills a handful of columns)

Same failure mode as #4 — lower confidence since there's no explicit evidence these predate Postgres support, but any that do will crash any already-deployed Postgres install the moment that column is touched, with no self-heal.

**Fix:** audit deployment history for these columns; add `_add_column_if_missing` to `init_db_postgres` for any that could predate a hosted install.

### 19. `feeds`/`accounts` tables have no DB-level UNIQUE constraint backing import's dedup check
`database.py` (feeds/accounts `CREATE TABLE`) vs. `import_export.py:348-359`, `:261-269`/`:365-378` (SELECT-then-INSERT dedup)

Every other destination table has `UNIQUE(user_id, ...)` enforced at the DB level; `feeds` and `accounts` don't, and import's dedup is pure check-then-insert.

**Failure scenario:** two concurrent imports (or an import racing a manual "add feed"/"connect account" request) for the same user and URL/instance both pass the SELECT check before either commits, producing duplicate rows.

**Fix:** add `UNIQUE(user_id, url)` on `feeds` and `UNIQUE(user_id, instance, username)` on `accounts`.

### 20. "Extend trial" confirmation dialog never appears
`templates/admin.html:95`

`onsubmit="return confirm('Extend {{ u.email }}'s trial by ...')"` contains a literal, unescaped apostrophe in the *static template text itself* ("`}}'s trial`"), which prematurely terminates the JS string regardless of `u.email`'s content — a JS syntax error on every render.

**Failure scenario:** admin clicks "Extend trial" for any user; the malformed handler fails silently, `confirm()` never runs, and the form submits immediately with no confirmation at all.

**Fix:** `onsubmit="return confirm('Extend ' + {{ u.email | tojson }} + '\'s trial by ' + this.days.value + ' day(s)?')"`, or move the handler out of the inline attribute (do this alongside fixing #1, same root cause).

### 21. Folder rename/delete has the same inline-JS string-breakout as #1, but self-XSS only
`templates/feeds.html:37,41`

Same pattern as the admin XSS: `folder.name | e` interpolated into an inline `onsubmit="var n = prompt('Rename folder:', '{{ folder.name | e }}'); ..."`/`confirm(...)`. Folder names are free text with no character restriction (60-char max).

**Failure scenario:** a folder named `a'-alert(1)-'` executes attacker JS when its owner clicks Rename or Delete. Scoped to the folder owner's own session (folders aren't shown to other users), so self-XSS rather than cross-user — lower severity than #1, but same fix.

**Fix:** same as #1.

---

## Low

### 22. TOCTOU race between dependent-echo check and destination delete
`app.py` — all 7 destination-delete routes (~lines 3172-3774)

Each route checks `_dependent_echo_count` in one connection, then deletes in a separate one. A concurrent request creating an echo against that destination between the check and the delete leaves the new echo pointing at a row that no longer exists — the exact dangling-reference scenario `_dependent_echo_count` exists to prevent.

**Fix:** perform the check and the delete in the same transaction.

### 23. `readerLoadMore()` calls a nonexistent function
`static/js/app.js:1583`

`if (typeof hydrateLocalTimes === 'function') hydrateLocalTimes();` — no such function exists; the real one is `formatLocalTimes` (line 523). The guard silently no-ops every time, so items appended via "Load more" never get their timestamps localized.

**Fix:** call `formatLocalTimes()`.

### 24. Matrix can linkify a URL that truncation cut in half
`matrix.py:204-221`

`_truncate_body` is a plain character-count cut with no URL-awareness; `html_body`'s regex then still linkifies whatever partial URL remains, unlike Bluesky's `build_facets`, which explicitly drops link facets whose byte range was cut mid-URL.

**Fix:** port Bluesky's "drop the match if it was cut" check into `html_body`.

### 25. Verification-token race can delete an already-issued, already-sent token
`verification.py:35-61` (`issue_token`)

Two concurrent `issue_token` calls for the same user+purpose (e.g. a double-clicked "resend" or two tabs hitting forgot-password) can interleave so that the second call's unique-violation handler deletes the *first* call's already-committed, already-emailed row instead of its own failed insert.

**Failure scenario:** the user clicks the link from the first email and gets "This link is invalid or has expired," because a race reissued a different token under them.

**Fix:** on unique-violation retry, don't unconditionally delete the current live row — re-select and return the winning token instead of always minting a fresh one.

### 26. `poll_interval: 0` on import is treated as missing, not clamped to the real floor
`import_export.py:313-314`

`max(1, min(_try_int(poll_interval) or 15, 1440))` — `_try_int(0) or 15` evaluates to `15` because `0` is falsy in Python, even though `0` was successfully parsed. The surrounding `max(1, ...)` implies the intended floor is 1.

**Fix:** `parsed = _try_int(poll_interval); poll = max(1, min(parsed if parsed is not None else 15, 1440))`.

### 27. Discord `webhook_url_hash` not recomputed when `webhook_url` is empty on import
`import_export.py:253-258`

The hash is only recomputed `if section == "discord" and account.get("webhook_url")`. An import record with an empty `webhook_url` but a non-empty `webhook_url_hash` passes the stale hash through unchanged and it's used as the dedup key — corrupting that user's own dedup key going forward (no cross-tenant impact).

**Fix:** always recompute `webhook_url_hash` from `webhook_url`, or reject a hash supplied without a matching URL.

---

## Areas checked with no bugs found above Low severity

- `filters.py` — casefold-based matching, correct include/exclude semantics, no regex bypass.
- `alt_text.py`'s retry logic — permanent-4xx handling and linear backoff are correct and complete (commit `683cd59`).
- `plans.py`'s `check_*_allowance` family — comparison direction and offset math verified correct against real call sites.
- `settings.py`'s env-var precedence and `validate_config()` — matches documented behavior and test suite.
- `template_engine.py` — sandboxed Jinja2 environment, no unsafe globals, repeat-size DoS guard verified.
- `email_sender._render_html_body` — correctly HTML-escapes body and alt text; no XSS in the HTML part.
- Password hashing (scrypt + `hmac.compare_digest`), session-epoch invalidation, invite-code atomic consumption, rate-limit IP derivation, Fernet credential encryption — traced and found correctly implemented.
- Scheduler feed-level and job-level leases — atomic and correctly scoped.
- `_flush_queue`'s own retry/backoff counting — internally consistent.
- The 6 destination `test_connection()` implementations and the `normalize_webhook_url` divergence between discord.py/webhook.py — both intentional and correct for their respective threat models, not bugs.
- `app.js` fetch error handling, `escapeHTML` usage, busy-state double-submit guards, reader star/read/mute/compose flows — correct.
