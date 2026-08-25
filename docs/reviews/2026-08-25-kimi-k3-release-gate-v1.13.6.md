## Findings

### HIGH — Dockerfile (USER feedecho / uid 10001): upgrade breaks existing Docker deployments
**File:hunk:** `Dockerfile`, hunk adding `useradd --uid 10001 ... USER feedecho`.

What is wrong: every existing self-hoster's named volume or bind mount at `/app/data` is currently root-owned (the old image ran as root, and `VOLUME /app/data` preserves the initializing ownership). On upgrade, the container starts as uid 10001 and cannot open/write `feedecho.db` (sqlite also needs journal/wal write access in the directory). The image creates and chowns `/app/data` but that only fixes *fresh* volumes; Docker does not re-chown existing named volumes or bind mounts on image change.

Consequence: a routine `docker pull && up -d` upgrade leaves the service crash-looping with "unable to open database file" / permission denied. This is exactly the upgrade-breaking class the previous gate caught; the compose/docs mitigation is absent from this diff. Nothing in `docker-compose.multi.yml` or an entrypoint re-chowns.

Fix: add an entrypoint that chowns `/app/data` when running with sufficient privileges (start as root, `chown -R 10001:10001 /app/data`, `su-exec`/exec drop), or ship a documented mandatory migration step and call it out in release notes as breaking. As-is this should not be tagged.

### HIGH — `scheduler._discard_orphaned_digest_items` destroys a *paused* echo's backlog
**File:hunk:** `scheduler.py`, hunk adding `_discard_orphaned_digest_items`, predicate `OR e.enabled = 0`.

What is wrong: `enabled = 0` is the user-facing pause toggle (`toggleEcho` in `static/js/app.js`), not a deletion. The sweep treats a paused digest echo identically to a deleted one: it wipes `digest_items` and marks the `posted_items` rows `gave_up` with "no longer active". A user who pauses a digest echo for any reason and re-enables it later finds the queued items permanently gone; nothing else in the model (only `deleted_at`) distinguishes pause from deletion here.

Consequence: irrecoverable loss of queued digest content plus misleading history, triggered by a normal UI action. "Mirrors `_discard_drip_backlog`" makes this a repeated defect, not a justification.

Fix: restrict the sweep to genuinely-orphaned rows: `e.id IS NULL OR e.deleted_at IS NOT NULL` (and the feed/destination null/deleted legs). A disabled echo's backlog should sit until re-enabled, matching what a user expects from pause. If the drip discard has the same predicate, it needs the same fix; the new test `test_flush_digests_discards_items_for_a_deleted_feed` only covers `feed.deleted_at`, so it passes either way — add a paused-echo test.

### MEDIUM — single-mode login throttle permits permanent third-party lockout
**File:hunk:** `auth.py`, hunk in `login_submit` (single-mode branch).

What is wrong: while an IP's bucket is full, `_throttled(ip)` returns before the token is even compared, and the new test explicitly asserts that *the correct token is refused while throttled*. A third party who can reach the login endpoint (shared NAT egress, or anyone on the operator's network) can therefore keep the operator locked out indefinitely by continuously POSTing a wrong token — each failure re-arms the sliding window. The bucket is per-IP only, so behind NAT one user's failures block every user of that address.

Consequence: availability/DoS against the single-mode operator, exactly the credential path this batch was meant to protect.

Fix: on a successful `compare_digest`, bypass or clear throttle for that request (a correct credential should reset the bucket), and/or apply a flat per-IP cooldown that expires regardless of continuing failures. At minimum document the behavior. Separately (cannot be confirmed from the diff): if `_client_ip` honors an unverified `X-Forwarded-For`, the throttle is also trivially bypassed by header spoofing — confirm `_client_ip` only trusts XFF from configured proxies, otherwise the whole throttle is vacuous.

### MEDIUM — `test_body_over_the_cap_is_abandoned` is vacuous
**File:hunk:** `tests/test_security.py`, `TestSSRFRedirectProtection.test_body_over_the_cap_is_abandoned`.

What is wrong: the cap is 2048 with three 1024-byte chunks. Reading chunk 3 pushes the total to 3072 and raises; `read` is then `[1024,1024,1024]`. The assertion `sum(read) <= 3072` therefore also passes for a hypothetical implementation that buffers the entire body and rejects afterwards (read-all-then-raise yields the identical list). The test cannot distinguish "abandoned mid-stream" from "buffered everything, then rejected" — the exact regression the refactor targets.

Fix: assert on consumption, e.g. track chunks actually requested by `iter_bytes` and require the raise to happen before the third chunk is yielded (assert `len(read) <= 2`... in fact with the current code reading the 3rd chunk, the meaningful invariant is the generator being closed mid-iteration; assert `sum(read) == 3072` *and* that no 4th chunk exists, or use chunks of `max_bytes` and assert only the first is read). As written the test proves nothing about the streaming behavior it documents.

### LOW — claim-recheck TOCTOU acknowledgement + return-value semantics
**File:hunk:** `scheduler.py`, `_still_owns_claim` and `_send_mastodon`/`_send_email_echo` hunks.

Two items, neither blocking:
- The re-check narrows but cannot close the race: the window is only between the `SELECT` and the POST's network dispatch, which is inherent without a transactional outbox. Acceptable; the comment shouldn't imply the window is eliminated.
- On claim loss, `_send_mastodon` and `_send_email_echo` now `return False`. If the caller treats `False` like a delivery failure (calls `_fail_post`, `record_failure`, alert), a benign race produces a spurious failure/alert on the echo. The Bluesky path predates this with the same `return False`, so semantics are presumably safe — but the diff does not show the caller, and `_fail_post` with a stale token no-ops only if it token-scopes its UPDATE. Verify `_render_and_dispatch` treats claim-loss distinctly from send failure, or log-and-return in a way that cannot increment failure counters.

### LOW — destination-delete guard has a check-then-delete race
**File:hunk:** `app.py`, `delete_account` / `delete_email_account` hunks + `_dependent_echo_count`.

The count and the `DELETE` run in separate transactions; a concurrent echo creation between them still orphans an echo. Inherent TOCTOU; the realistic fix is a transaction with the echo-visibility check or a FK constraint, but for this app's concurrency profile the guard is a strict improvement. Note only — no change required to ship.

### LOW — `_safe_url` drops legitimate non-http(s) links and logs per-render
**File:hunk:** `app.py`, `_safe_url` hunk.

- Scheme allowlist handles the attack classes well (whitespace, case, `//`, `https:/\`, unicode, data URLs all correctly rejected). Two residual behavior notes: legitimate non-http schemes (e.g. `feed://` some publishers emit, magnet links in item URLs) now render linkless, and a hostile feed with many such URLs floods the log at WARNING on every history render. Consider `logger.debug`/rate-limiting and document the scheme allowlist in release notes.

### LOW — SMTP validation edge rejections + inconsistent error channel
**File:hunk:** `app.py`, `save_smtp_settings` hunk.

- Nothing is persisted on the failure path (validation precedes `get_db()`); CRLF and port checks are correct. Residuals: the from-address regex rejects technically valid addresses (`user@localhost`, address-literals, quoted local parts) — acceptable but worth noting for self-hosters pointing at a LAN relay; and a non-numeric `smtp_port` returns FastAPI's bare 422 while a >65535 port returns the styled 400 page — harmless inconsistency. `smtp_use_tls` is not CRLF-checked but is not a header field. Ship as-is.

### Residual confirmations (not defects in the diff)
- `feed_parser` stream conversion is sound: per-hop `validate_outbound_url` retained, `Location`-less redirect still raises, each `client.stream` context is closed on raise/continue, `iter_bytes()` applies content decoding so the cap binds *decompressed* bytes, and `raise_for_status` still raises `HTTPStatusError` inside the stream context. No cap bypass found. Callers' exception surface unchanged (ValueError same type).
- `settings.py` A12: all link-sending paths in the diff now gate on `_absolute_link()`; warning fires in `validate_config` only — confirm `validate_config()` is invoked at startup in both modes, otherwise the warning is never seen (import-time silence is a logging gap, not a correctness bug).
- Templates/JS: `previewTemplate` still resolves (`closest('form')` unaffected; `parentElement` becomes `.form-row`, to which the box is appended — same append parent in both static `echoes.html` and `editEcho` markup). New ids (`oauth-instance`, `masto-*`, `feed-*`, `login-*`, `smtp-test-email`) are unique per page. The echoes.html placeholder change fixes a real rendering bug.
- New SQL param counts match placeholders; `Row`/`dict_row` both support the `["c"]` access used.
- Docker gate job correctly serializes publish behind tests.
- Caddy env change is compatible for deployments that set the two vars in `.env` (compose substitution still reads `.env`); deployments that set neither now fail with a clear `:?` message — acceptable, but flag it in release notes.

## Verdict
Not safe to tag as-is. Two HIGH issues block: the non-root container breaks every existing Docker deployment on upgrade, and the digest sweep deletes data for a merely paused echo. Fix those two; the MEDIUM items (throttle DoS, vacuous streaming test) should land in the same batch; the rest are notes.