# Review: FeedEcho pre-release diff

## Findings

### F1 — HIGH — `alt_text.py` hunk (`validate_outbound_url(endpoint)`): unconditional SSRF guard breaks single-mode self-hosters

The guard runs in both modes. Self-hosters overwhelmingly point the vision endpoint at a LAN/loopback service (Ollama, llama.cpp, LocalAI on `192.168.x.x`, `10.x`, `localhost:11434`); `validate_outbound_url` demonstrably blocks link-local (the new test blocks `169.254.169.254`) and by design blocks private/loopback ranges. The context explicitly requires single mode to stay behaviorally unchanged; this silently disables alt text for exactly those deployments. Consequence: regression for a legitimate single-mode configuration, `generate_alt_text` returns `""` and posts go out without alt text, with only a log line. Fix: apply the guard only when `settings.MULTI` is true (the threat is tenant-supplied URLs in hosted mode), or gate it behind an explicit `allow_private` opt-in per user setting. If you deliberately want it everywhere, that violates the stated constraint and needs a release note plus an env override.

### F2 — MEDIUM — `app.py` `test_alt_text` (hunk at line ~1380) + `alt_text.py` guard: blocked URL is reported as success

When the SSRF guard (or any misconfiguration) makes `generate_alt_text` return `""`, the endpoint returns `{"success": True, "message": "API reachable (empty response to test image)"}`. After this diff, the most common failure mode in multi mode (URL rejected by the guard) reports *success*, and combined with F1 a self-hoster's blocked LAN endpoint also reports success — while real posts get no alt text. Consequence: operators cannot distinguish "endpoint works" from "endpoint refused / blocked / misconfigured". Fix: surface the rejection — e.g. have `generate_alt_text` signal "blocked/config error" distinctly (return `None` vs `""`, or log and have the test endpoint call `validate_outbound_url` itself and return `success: False` with the reason).

### F3 — MEDIUM — `scheduler.py` `posted_at` hunks: historical rows keep the session-TZ skew

The fix only changes new writes. Every `posted_at` written before this deploy via `CURRENT_TIMESTAMP` on a non-UTC Postgres session remains offset, and `_drip_rate`'s window compares those against UTC strings — so the drip window is still wrong for pre-existing rows until they age out of the window. The new PG test even demonstrates the skew is possible. Consequence: up to one drip-window of incorrect rate limiting post-upgrade on affected deployments. Fix: either a migration normalizing existing `posted_at` rows, or accept and document (likely acceptable given window size — but it's a real residual, not zero).

Also note the incomplete fix surface: the docstring in `database.py` says "prefer binding explicit `_now()`," but schema defaults (`created_at`, `trial_ends_at`, etc.) still use `CURRENT_TIMESTAMP`. Within this diff all read sites are normalized, but any *other* Python-side comparison of those columns against UTC strings elsewhere in the repo (not visible here) is the same bug class, still live. Verify before tagging.

### F4 — MEDIUM — `auth.py` `logout` hunk: `delete_cookie` attribute parity unverified, and a hard-coded second cookie name

`delete_cookie(COOKIE_NAME)` / `delete_cookie("feedecho_auth")` use Starlette defaults (path `/`, no domain). Deletion only takes if the original `set_cookie` used the same path/domain; if either cookie is set with an explicit `domain` (subdomain deployments) or non-default path, logout is still a no-op for that mode — the exact bug being fixed, preserved. The passing tests use TestClient defaults and cannot detect this. Fix: verify both cookies' set sites use defaults, or mirror set attributes; also replace the literal `"feedecho_auth"` with whatever constant the middleware uses (the OAuth cookie uses a constant — this one should too, or a future rename re-breaks single-mode logout silently).

### F5 — LOW — `app.py` async→sync hunks: cannot fully verify from diff; two residual risks

- A leftover `await` in a converted body would be a `SyntaxError` and would fail the whole suite, so given 558 passing, no literal `await` remains. What the diff cannot show (bodies of `resend_verification`, `admin_email_test`, `test_account`, `init_feed` are not pictured): whether a handler *needs* to be async — e.g. uses `request.stream()`, returns a streaming response, or reads the body via `await request.form()` which would have had to be rewritten to `Form(...)` params. `test_smtp` uses `Form`, suggesting the pattern, but verify each converted handler's body-read path.
- The new AST test guards only the forward direction (async handler + blocking call). It cannot catch a sync handler that should be async. Minor.
- Threadpool: nine more endpoints now consume anyio worker threads; a tenant hammering "test/fetch" endpoints can starve the pool (default 40). Pre-existing pattern, but wider now. Acceptable, note it.

### F6 — LOW — `tests/test_pg_dialect.py` new tests: hard-coded IDs (`users.id = 7`, account/feed/echo ids = 1) assume `pg_env` resets schema per test

If `pg_env` doesn't recreate/truncate tables per test, these collide across tests and across reruns against the same service container (`INSERT id=7` twice → unique violation → flaky-before-it-starts). The existing tests in the file presumably established that fixture; verify isolation, because these tests will also be run against a reused CI service. The vacuity guards (`isinstance(..., datetime)`, `current_setting('TimeZone')`) are the right pattern and keep them honest. The `delta < 60` bound is coarse but stable.

Also in `test_private_base_url_is_refused_before_any_request`: monkeypatching `alt_text.httpx.Client` mutates the shared real `httpx` module attribute for the duration of the test; monkeypatch restores it, but any other test running concurrently in the same process would see it — fine under pytest's serial default, fragile if you adopt `pytest-xdist`-in-process or asyncio test groups. Note only.

### F7 — LOW — `.dockerignore` / `docker-compose.yml`

- `.env`/`.env.*` exclusion is correct against secret-baking and doesn't affect compose interpolation or `--env-file`. Residual: if the Dockerfile's `COPY . /app` plus a runtime `dotenv.load_dotenv()` was ever the config path for a bare `docker build && docker run` user (no compose env), that path is now broken. Verify no runtime dependency on a baked `.env`. The `!.env.example.multi` re-include is fine; any sibling like `.env.example.single` is now excluded too — check docs don't tell users to read it from inside the image.
- The `:?` guard is a deliberate behavior change (hard fail for anyone previously running unauthenticated) — correctly fail-closed, but it's a breaking change for exactly the self-host cohort the context protects. Confirm it's called out in release notes. (`FEEDCHO_AUTH_TOKEN` naming is pre-existing, not introduced here.)

## Item-by-item audit answers

1. **Placeholder counts**: audited every edited statement — all match. `_update_post` 6/6, `_fail_post` 6/6, `_requeue_drip_failure` (gave-up) 3/3, `_requeue_drip_failure` (requeue) 2/2, `_discard_drip_backlog` 3/3, `flush_digests` 3/3, `retry_post` unchanged tuple with one fewer placeholder set (`status='failed'` is a literal). Clean.
2. **Remaining raw TIMESTAMP compares/parses in the diff**: none found — `session_expires_at` and `last_fetched` now go through `timestamp_str`; writers bind `_now()`. Unparseable → `""` → `strptime` raises `ValueError`, which must already be caught by the existing "malformed → due" except in `check_all_feeds` (sqlite required this pre-diff); verify that except clause covers `ValueError` before tagging. Residual class risk: schema-default `CURRENT_TIMESTAMP` columns (F3).
3. **async→sync**: see F5. Safe given suite passes; bodies of four handlers unseen are the verify list.
4. **Targeted changes**: logout — see F4; `retry_post` status reset is correct with a real regression test matching the scheduler predicate; `_render_oauth_error` — OK only if `render()`/`error.html` never call `current_user_id()` internally (the multi-mode test asserting 400 + no "Authentication required" passing implies it doesn't — confirm); SSRF guard — F1/F2; additionally the guard only validates the initial URL — if the `httpx.Client` used here enables `follow_redirects`, a redirect hop bypasses it (check the client construction, out of frame).
5. **Helpers**: `as_utc_naive` handles empty/None, psycopg datetime (aware → UTC-converted, naive → assumed UTC per documented invariant), sqlite TEXT, `"Z"` suffix. Edge: bare falsy values short-circuit before parse — fine. `timestamp_str` truncates microseconds consistently on both sides of comparisons — fine. `app._as_utc_naive` delegation is behavior-identical (same logic, moved). No defect.
6. **Tests**: see F6; otherwise the new tests are non-vacuous (explicit guards), assert the right predicates (scheduler sweep predicate, Set-Cookie rendering matching Starlette's `Max-Age=0` output), and the AST test is a real structural gate for the forward direction.
7. **Docker**: see F7.

## Verdict

Not safe to ship as-is, on one point: **F1** violates the stated hard requirement (single-mode behavior unchanged) — gate the SSRF guard to multi mode or ship an override. F2 (misleading success) should ship with the same change. F3 and F4 are verify/document items, not blockers. Placeholder audit, writer normalization, retry/status fix, OAuth error path, and the helper consolidation are correct, with tests that genuinely constrain them.