# Full-repo Kimi K3 review — 2026-08-28

**Scope:** entire OSS tree at commit `9a86668` (master, v1.25.0) — 107 tracked files, ~23K
LOC Python plus frontend, templates, deploy, Nix, and all 41 test files.
**Method:** 41 parallel Kimi K3 (`moonshotai/kimi-k3` via OpenRouter) batch reviews, each
file delivered line-numbered with sha256 digests. 379K prompt / 146K completion tokens,
~$3.12. Raw output: 102 findings + 40 context notes across two passes.
**Triage:** every HIGH/MEDIUM verified against the actual code (and empirically, where the
claim was testable). The list below is post-triage; counts of what was discarded and why
are at the bottom.

## Verified real findings

### HIGH

1. **Email recipient stored with no validation** — `app.py:1531-1554`
   `add_email_account` stores the recipient address raw, while the SMTP settings route
   (`app.py:1818-1825`) validates addresses with `r"^[^@\s\r\n]+@[^@\s\r\n]+$"` and
   comments that such fields "become message headers, so control characters must fail here
   rather than at send time." A CRLF-bearing recipient is a header-injection vector at
   send time. Fix: apply the same regex at `add_email_account` (and in micro.blog
   connect paths if any field feeds headers).

### MEDIUM

2. **Micro.blog plan-cap counts upsert rows as new** — `app.py:1725-1741`
   `current_total + len(blogs)` is checked before an `ON CONFLICT ... DO UPDATE` upsert,
   so a user at their cap re-connecting the same token (rotation) is wrongly rejected.
   The email route explicitly skips the cap for existing rows ("Cap counts NEW rows
   only"); micro.blog connect should count only blogs not already present.

3. **Truncated digest marks dropped content as delivered** — `scheduler.py:1775-1813`
   The assembled body is hard-truncated at 10,000 chars, but every queued item is then
   marked success and deleted from `digest_items`. Content past the cut is permanently
   lost while reported as sent. Fix: finalize only the items that fit (leave the rest
   queued for the next flush), or split into multiple emails.

4. **Digest send failures never call `record_failure`** — `scheduler.py:1788-1796`
   A failed `send_email` in `flush_digests` is logged and the loop continues; unlike the
   instant/drip paths, the failure counter never increments. A permanently broken SMTP
   config retries hourly forever without ever reaching the failure-notification
   threshold that the digest-queue comment promises.

5. **Last-admin guard is a check-then-act race across requests** — `app.py:941-967` with
   callers at 1106/1176 — `_admin_guard_last_admin` counts, then the UPDATE runs in the
   caller; two concurrent requests can both pass and leave zero admins. The in-code
   docstring's "race-free per request transaction" claim covers intra-request ordering
   only. (The destination-cap and feed-cap TOCTOU variants of this pattern are
   documented-accepted in `plans.py:84-88` and were excluded from findings.)

6. **`verification.py:39-41, 96-99` — CURRENT_TIMESTAMP vs Python-UTC mixed clocks.**
   `expires_at` is written as a Python UTC string, but `consume_token` compares
   `expires_at > CURRENT_TIMESTAMP` and stamps `consumed_at` with CURRENT_TIMESTAMP,
   which on Postgres resolves in the session time zone (the hazard the codebase itself
   documents at `invites.py:28-31` and works around elsewhere). Tokens can be rejected
   early or accepted past TTL depending on session TZ. Bind a UTC cutoff parameter
   instead (and stamp consumed_at client-side).

7. **JSON feed detection fails for `.json` URLs with query strings** —
   `feed_parser.py:334`: `url.endswith(".json")` misses `feed.json?token=abc`; such URLs
   also commonly ship `text/plain`, so both detection branches miss and `feedparser`
   yields zero entries — silent empty feed. Use `urlparse(url).path.endswith(".json")`.

8. **`templates/settings.html:100-102` — alt-text checkbox value/checked mismatch.**
   The checkbox submits `value="true"` but the checked test compares to `'1'`; the save
   route (`app.py:1906`) persists `"1"/"0"`, so the box never renders checked. Use
   `value="1"`. (Second pass even flagged this as a possible HIGH before triage; it's a
   one-word fix.)

9. **`oauth.py:26` — OAuth state HMAC prefers `AUTH_TOKEN` over `STATE_SECRET`.**
   `_STATE_SECRET = (AUTH_TOKEN or STATE_SECRET or ...)` — in multi mode a carried-over
   single-mode token silently keys OAuth state, the exact scenario
   `security.session_secret()` guards against for sessions. Blunted by the server-side
   `oauth_states` check; reverse the precedence and fail closed in multi mode.

### LOW (product code)

10. `_drip_locks` dict grows unboundedly across echo lifetimes (`scheduler.py:66-77`) —
    prune deleted echoes or bound it.
11. `_fail_post` runs `record_failure` even when its claim-guarded UPDATE no-ops
    (`scheduler.py:839`) — check `rowcount` first.
12. Missing-account failures not marked `permanent=True` in Mastodon/email paths, unlike
    Bluesky (`scheduler.py:861-864, 997-1003` vs 1129) — burns the retry budget.
13. `_admin_stats` uses `CURRENT_TIMESTAMP` for active-trials (`app.py:933-934`),
    violating the documented bind-UTC-params invariant (same class as #6).
14. `oauth_callback` inserts duplicate `accounts` rows on re-connect — no
    `(instance, username)` dedup/upsert (`app.py:2646-2650`).
15. `register_submit` sends verification email synchronously in-request
    (`auth.py:336-354`), unlike the forgot flow's thread dispatch — slow SMTP delays
    signup; also the inline `send_system_email` path is the reason the forgot-flow
    HIGH-turned-LOW task-retention nit exists.
16. Register throttle never counts early-return submissions (`auth.py:253-279`) —
    duplicate-email/invite probing is unbounded by the 10-per-10-min bucket.
17. `validate_template` checks syntax only, not sandbox safety
    (`template_engine.py:128-134`) — SecurityError surfaces at render time instead of
    save time. (Sandbox IS enforced at render — SandboxedEnvironment at line 24 — so
    this is a UX/contract nit, not SSTI.)
18. `_format_hashtags` strips non-ASCII (`template_engine.py:55-64`) — mangles accented
    tags ("café" → "#caf").
19. `email_sender.py:54` — non-numeric stored `smtp_port` raises ValueError inside
    `_normalize` instead of the friendly unconfigured path.
20. `mastodon.py:115-129` — `verify_credentials` doesn't guard non-dict 200 JSON (e.g.
    HTML-returning proxy) — `AttributeError` escapes `test_connection`'s catches. The
    three analogous bluesky.py claims were FALSE POSITIVES: all three sites already
    guard `isinstance`/`ValueError`.
21. `testAltText` relies on implicit `window.event` (`app.js:144-148`).
22. Edit form drip-limit field visible on initial render for digest mode
    (`app.js:305-309`).
23. `toggleDestFields()` hides drip fields whenever mode is digest, ignoring
    destination switches (`templates/echoes.html:254-260`).
24. Visibility column shows "public" for email echoes (`templates/echoes.html:205`).
25. Focus outline removed on inputs, weak keyboard indicator
    (`style.css:361, 396-399, 647-650`) + `color-scheme: light dark` on `:root` despite
    hard data-theme switching (`style.css:12-13`).
26. `USER_AGENT` still carries the placeholder `yourusername` repo URL
    (`feed_parser.py:21`) — visible to every feed operator.
27. Nix/doc trio: `flake.nix:68` re-declares the `mainProgram` that `nix/package.nix`
    deliberately removed (breaks `nix run`); `nix/module.nix` description references a
    nonexistent `services.feedecho.authToken` option; README flake example omits the
    `nixpkgs` input it uses.
28. `invites.revoke()` accepts `admin_uid` but never records it (`invites.py:112-124`).
29. `plans.trial_state` vs `limit_for` disagree on unknown-plan fallback
    (`plans.py:28-31` vs `62-63`).
30. `settings.py:119-123` — plan-limit override accepts booleans as ints (`true` → 1).
31. `_column_names` PG branch doesn't scope `information_schema` to current schema
    (`database.py:171-177`).
32. PG FK columns are INTEGER against BIGSERIAL PKs (`database.py:744-764`) — fails at
    2^31.
33. Fresh psycopg connection per `get_db()` call, no pooling (`database.py:126-136`) —
    latency cost in hosted mode.

### LOW (tests)

34. `test_version.py:79-82` — hosted-file-absence asserts use cwd-relative paths;
    vacuously pass when pytest runs from elsewhere. Anchor to ROOT. (There is no
    `tests/conftest.py` chdir, so this is live.)
35. `test_version.py:95-106` — `git ls-files` output split on whitespace.
36. `test_oauth_app.py:18-27` — `restore_settings` fixture ordering contradicts its
    docstring; patched env can leak into the reloaded settings module.
37. Blocking-I/O AST guard scans only `app.py` with a hand-maintained function list
    (`test_review_fixes.py:233-277`) — this is exactly why #15 (register_submit's sync
    SMTP call inside an `async def` wrapper) is invisible to it.
38. Vacuous/weak assertions: `"2" in r.text` for the micro.blog count
    (`test_microblog.py:661-672`), `"handle" in resp.text`
    (`test_bluesky.py:966-973`), negative-only dashboard check
    (`test_landing.py:83-90`).
39. Nondeterministic `ORDER BY created_at` (`test_digest.py:183-190`) — add `, id`.
40. `test_ssrf_pinning.py:158-167` — real-DNS test without the offline skip guard used
    elsewhere in the same file.
41. Duplicated `db_tmp` fixtures across test files (test_plans/test_invites byte-identical;
    test_microblog/test_mastodon_post_url; test_alt_text/test_cw_and_images with actual
    drift: `attach_image` default 1 vs 0) — move to `tests/conftest.py`.
42. Misc: hardcoded retry-cap literal (`test_scheduler_scoping.py:115-116`), token
    scraping by newline-split (`test_password_reset.py:58-61` + email_verification), fixed
    sleep in throttle test (`test_password_reset.py:192-204`), inconsistent send_email
    patching (`test_retry_notify.py:244-261`), schema-parity regex coupled to source
    formatting (`test_dialect.py:135-153`), stub import-style dependency
    (`test_bluesky.py:423-433`).

### Defense-in-depth (LOW, optional)

43. CSRF surface: every auth cookie is set `samesite="lax"` unconditionally (app.py:2561,
    auth.py:181, 393) and all mutations are POST, which neutralizes cross-site form
    posting. No CSRF tokens exist anywhere, so the protection is single-layer. Origin/
    Referer checking or tokens would add a second layer if cookie flags are ever
    relaxed.
44. `security.session_secret()` returns an empty HMAC key in single mode with neither
    env set (sessions are documented-unused there, but fail-closed would be cheaper
    than the assumption).
45. `_sign_state(session_binding=None)` mints an unverifiable token
    (`oauth.py:61-62`) — dead-parameter footgun, callers always pass it.
46. Digest items for suspended/expired users freeze in the queue (excluded from flush,
    not swept) and dump as one oversized email on reactivation — compounds #3.
47. `notify_alerted_echo_{id}` settings rows orphaned when an alerted echo is deleted
    (`notify.py:130-150` + delete paths).
48. Dockerfile dependency list hand-mirrors pyproject.toml (divergence risk);
    `alt_text.py` retries non-retryable 4xx with a fixed sleep.

## False positives and deliberate-design detections (not real)

- **"asyncio.create_task in sync handler 500s every reset" (the original HIGH)** —
  `auth.forgot_submit` is wrapped by an `async def` route at app.py:405, runs on the
  event loop; empirically fine. Residual task-retention nit folded into #15.
- **"IPv4-mapped IPv6 literals bypass `_is_blocked_ip`"** — premise false on Python
  3.14: `::ffff:127.0.0.1` tests `is_private=True` and is blocked; empirically verified
  on the repo's own venv.
- **"Stored SMTP password / vision API key rendered into page HTML" (escalated to HIGH
  on a second pass)** — both GET paths mask server-side before render
  (`app.py:610-611`, `app.py:1458-1459`); the template never sees the real secret.
- **CSRF "attacker can suspend users via victim's browser"** — SameSite=Lax on every
  cookie blocks cross-site POST (see #43).
- **"OAuth callback destination-cap race"** — documented-accepted TOCTOU
  (`plans.py:84-88`).
- **SSTI concern** — SandboxedEnvironment confirmed at template_engine.py:24.
- **`int Form(None)` 422s on empty strings** — empirically coerces to None.
- **Oversize-feed `ValueError` escapes callers** — every caller catches `Exception`;
  docstring nit only.
- **bluesky.py non-dict JSON ×3** — all sites already guarded.
- **micro.blog Bearer leak on cross-host redirects** — httpx strips `Authorization` on
  every cross-host redirect (verified against installed httpx source; the HTTPS-redirect
  exception requires same host, which is not cross-host).
- **"echoes migration DROP TABLE loses data"** — the pre-`destination_type` schema never
  shipped: commit 73d9e4c (2026-08-07) is an ancestor of v1.0.0.
- **"digest flush skips disabled echoes but items linger forever"** — contradicts
  `feed_parser`/scheduler sweep behavior actually present; second pass dropped it.
- Also dropped: `add_feed` holding DB connection (SQLite-only callers, accepted);
  request-id reset ordering (traceback does carry the id through the same
  ServerErrorMiddleware pass — comment/implementation actually agree once the wrapper
  chain is read); flake `mkNixosModule` dead helper (kept real Nix items in #27);
  `test_retry_notify` sweep finding (behavior is deliberately designed, test docstring
  says so).

## Context notes from Kimi (unverified, plausible)

- Digest items queued before suspension freeze (see #46); scheduled-user gating
  (`suspended = 0` exclusions) bounds the backlog.
- `_fail_post` docstring says "Returns the final status written" but returns a bool.
- `recent_posts` dashboard query deliberately omits `deleted_at` on echoes (history
  preserved by design).
- `_client_ip` takes the rightmost X-Forwarded-For entry (spooof-resistant given the
  deployment's single proxy).
- `oauth_callback` inconsistently deletes the OAUTH_SESSION_COOKIE across error paths.
- Privacy/terms pages claim "Last updated: August 27, 2026" — matches the v1.15.0-era
  review date; fine unless policy text changed since.

## Cost

41 batches + 1 retry, 379K prompt tokens, 146K completion tokens, ~$3.12 via OpenRouter.

## Disposition (2026-08-28)

All 9 verified findings fixed on master (commit `cac3473`): the HIGH plus 8 MEDIUMs,
with 24 new regression tests in `tests/test_kimi_full_review_fixes.py`. A Kimi K3 gate
pass on the fix diff caught 3 defects in the fixes themselves (all verified real, fixed,
and pinned): zero-new-row reconnects blocked when over cap, success-after-failure
leaving posted_items stranded at 'failed' while the queue row was deleted (silent
loss), and the degenerate oversized-item path sending title-only. SQLite 820 passed /
20 skipped; PG 20 passed. Remaining LOWs (#10-48) and defense-in-depth notes
(#43-48) are unfixed backlog.
