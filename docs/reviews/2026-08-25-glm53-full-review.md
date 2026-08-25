# GLM 5.3 full-repo review, 2026-08-25

Reviewer: `z-ai/glm-5.3` via OpenRouter (1M ctx), 5 parallel calls, all returned
`finish_reason: stop` (nothing truncated).

Scope: every tracked non-test source file, 8 web/auth modules, 4 data/scheduler
modules, 5 publisher modules, 17 templates + CSS + JS, 11 container/CI/Nix files.
Source was line-numbered in the prompt so findings are anchorable.

Usage: 144,409 prompt tokens / 107,521 completion tokens = **$0.68**.

Raw per-chunk output: `/tmp/glmrev/review-*.md`, payloads `/tmp/glmrev/payload-*.json`.

50 findings. Every one triaged against the source below; 6 are wrong or overstated.

---

## Triage verdicts

Legend: CONFIRMED = verified in source · PROVEN = verified by running code ·
FALSE POSITIVE = claim does not hold · OVERSTATED = real defect, wrong impact.

### Hits production right now (multi mode / Postgres)

| # | Finding | Verdict |
|---|---------|---------|
| B3 | `scheduler.py:969-970`, `session_expires_at` is a `TIMESTAMP`; psycopg returns `datetime`, compared with `>` against the `_now()` **string**. Raises `TypeError`, swallowed by the broad handler at 1052 and recorded as "Bluesky session failed". First post caches the expiry, every later post for that account fails → retries → gives up. | **PROVEN** |
| B4 | `scheduler.py:1564`, `feed["last_fetched"].replace("T", " ")` on a `datetime` raises `TypeError`, caught at 1568 and treated as "malformed → due". Every feed with a `last_fetched` is due on every 2-minute tick, so `poll_interval` is ignored entirely on Postgres. A 24h feed gets polled 720×/day. | **PROVEN** |
| A1 | Blocking network I/O on the event loop in `async def` handlers: `app.py:819, 1112, 1312, 1497, 1523, 1568, 1945, 2008, 2022`. `fetch_feed`/`check_feed`/`test_connection`/`exchange_code` are sync httpx with 30s timeouts. One "Fetch now" on a hanging feed stalls every request from every tenant. The codebase already states the rule at `app.py:1164-1168` and marks `add_bluesky_account` / `preview_template` / `delete_bluesky_account` as plain `def` for exactly this reason. | **CONFIRMED** |

`enabled`/`attach_image` are `INTEGER` on both dialects (verified), so the D4 claim
about `True`/`False` rendering does not apply, but B3/B4 prove the same class of bug
is live for `TIMESTAMP` columns. The pg suite renders pages; it never exercises a
second Bluesky post or a `last_fetched` comparison.

### Real, verified

| # | Finding | Verdict |
|---|---------|---------|
| B9 | `database.py:377` (sqlite) backfills only `is_admin`; the Postgres path at 847 backfills `session_epoch` too. Any sqlite DB created before `session_epoch` existed never gets the column. **The live local DB at `~/projects/feedecho/feedecho.db` is missing it**, confirmed by `PRAGMA table_info(users)`. Harmless in single mode; in multi mode `AuthMiddleware._multi` and `login_submit` both SELECT it → `no such column`. | **CONFIRMED (live)** |
| E5 | `docker-compose.yml:15`, `FEEDCHO_AUTH_TOKEN=${FEEDCHO_AUTH_TOKEN}` with no `:?` guard, unlike `POSTGRES_PASSWORD` in the multi compose. GLM rated this LOW and hedged on the impact; `app.py:211` settles it: empty `AUTH_TOKEN` makes `_single` a no-op, so a forgotten variable silently deploys a **completely unauthenticated instance**. Upgrade to HIGH. | **CONFIRMED, severity raised** |
| E1 | `.dockerignore` omits `.env` and `Dockerfile:18` is `COPY . .`. Both compose files tell you to create `.env` in the repo root, and single-mode compose uses `build: .` → session secret, DB password and auth token get baked into a readable image layer. Production pulls the GHCR image (clean CI checkout), so feedecho.net is unaffected; self-hosters who build locally are not. | **CONFIRMED** |
| C1 | `alt_text.py:110-119` POSTs to the per-tenant `alt_text_ai_base_url` with no `validate_outbound_url()`, while every other outbound hop in the repo validates. In multi mode a tenant points it at `169.254.169.254` or `127.0.0.1:<port>` and the server makes the request; timing gives a port-scan oracle. | **CONFIRMED** |
| C5 | `app.py:1386`, `alt_text.generate_alt_text(tiny_png, "image/png")` omits `user_id`, so it silently defaults to tenant 1. Line 1378 checks `is_enabled(current_user_id(request))`, then the actual call tests **tenant 1's** vision config and burns tenant 1's API key. Concrete instance of GLM's generic `user_id: int = 1` complaint; every other call site passes it explicitly. | **CONFIRMED** |
| A2 | `/api/settings/smtp` (`app.py:1270-1304`) stores host/username/from-name/from-email with no CRLF check, no email-format check and no port range check. `/admin/email` (760-792) enforces all three and documents why ("must fail here, not at send time"). Blast radius is limited, a tenant's own relay, no fallback to system SMTP, but the asymmetry is the bug. | **CONFIRMED** |
| A3 | `/oauth/callback` is in `_MULTI_EXEMPT_PATHS` (`app.py:192`) so `request.state.user_id` is never set; both error paths (1977, 2011) call `_render_accounts_error`, whose first line is `current_user_id()` (511) → raw 401 in multi mode. Denying authorization on Mastodon crashes the handler that exists to report it. `/oauth/connect` is fine (not exempt). | **CONFIRMED** |
| A4 | `retry_post` (`app.py:1582-1594`) accepts `status IN ('failed','gave_up')` but never sets `status` back to `'failed'`. The scheduler selects retryables on `status = 'failed'` (`scheduler.py:301, 380`), so retrying a `gave_up` row returns `{"success": true}` and nothing ever reprocesses it. One-word fix. | **CONFIRMED** |
| A5 | `auth.py:351-354`, `logout()` deletes only `feedecho_session`. Single mode authenticates on `feedecho_auth` (`app.py:222`), so logout is a no-op there. | **CONFIRMED** |
| A6 | `delete_account` (1116) and `delete_email_account` (1144) hard-DELETE with no dependent-echo check; `delete_bluesky_account` (1245-1259) refuses when live echoes reference the row. Deleting a Mastodon/email destination leaves echoes pointing at a dead `destination_id`. | **CONFIRMED** |
| B7 | `flush_digests` (`scheduler.py:1463-1475`) selects only `enabled = 1 AND deleted_at IS NULL` with an INNER JOIN on `email_accounts`, and there is no discard path, unlike drips, which have `_discard_drip_backlog`. Since `delete_echo` sets `enabled = 0`, pending digest items for a deleted echo are stranded forever: `posted_items` stuck at `queued`, `digest_items` rows never cleaned. | **CONFIRMED** |
| B8 | Only one claim-ownership re-check exists (`scheduler.py:1133-1149`, Bluesky). The Mastodon path does image fetch + upload + alt-text generation between `_claim_post` and `post_status` with no re-check, so a lapsed lease plus a reclaiming worker can double-post. Narrow window (needs >10 min of processing) but the guard exists next door for the same reason. | **CONFIRMED** |
| B2 | SSRF check is TOCTOU: `validate_outbound_url` resolves with `getaddrinfo` (`feed_parser.py:81`), then `client.get` resolves again (142). Low-TTL DNS rebinding gets past it on the initial URL and every redirect hop. Needs connection pinning to the validated IP to actually close. | **CONFIRMED** |
| B5 | `feed_parser.py:120` and `:363` check `len(response.content)` **after** a non-streaming `client.get`, so a hostile feed or image can force the worker to buffer the whole body before the 10 MB cap rejects it. | **CONFIRMED** |
| E3 | `docker-compose.multi.yml:72` gives the Caddy container the full `env_file`, though the Caddyfile only reads `ACME_EMAIL` and `FEEDECHO_DOMAIN`. The TLS terminator holds `FEEDCHO_SESSION_SECRET` (forges any tenant's session) and `POSTGRES_PASSWORD` for no reason. | **CONFIRMED** |
| E4 | `Dockerfile` has no `USER`; uvicorn parses untrusted remote feed content as root, and the data volume is created root-owned. | **CONFIRMED** |
| E2 | `nix/package.nix:18` ships `hash = lib.fakeHash` in a tagged release, and `nix/module.nix:21` falls back to that package for non-flake users. The documented non-flake NixOS path cannot build. | **CONFIRMED** |
| D3 | `href="{{ post.item_url }}"` / `post_url` / `feed.url` (`history.html:28,33`, `feeds.html:37`). Autoescaping does not neutralize a `javascript:` scheme, and nothing validates item link schemes on ingest. Hostile feed → one click → script in the authenticated origin. | **CONFIRMED** |
| D6 | Login, register, feeds, accounts and settings inputs are labelled by `placeholder` only (no `<label for>` / `aria-label`); `poll_interval` has only `title`. WCAG 1.3.1 / 3.3.2. `forgot_password.html` and `reset_password.html` already do it right. | **CONFIRMED** |
| D7 | Flash/status divs (`dashboard.html:19-22`, `accounts.html:10-24`, `login.html:12-14`, `admin_email_result.html:5-9`) carry no `role="status"` / `role="alert"`, and `previewTemplate` injects into a non-live region (`app.js:352,369`). WCAG 4.1.3, screen readers get nothing when an account connects or a preview renders. | **CONFIRMED** |
| D5 | `app.js:192-195` reads `.innerHTML` without `.trim()`, then branches on truthiness at 209-211. An empty `{% for %}` still yields whitespace → truthy → the edit form always offers Mastodon/Email/Bluesky even with zero accounts of that type. The create form guards correctly with `{% if mastodon_accounts %}`. | **CONFIRMED** |
| D9 | `toggleEcho` (`app.js:159-169`) has no `else` branch; a non-success response does nothing at all. Every sibling (`pauseFeed`, `retryPost`, `giveUpPost`) alerts. | **CONFIRMED** |
| D10 | `echoes.html:74` puts the Preview `<button>` inside the `<label>` wrapping the textarea (replicated in the JS edit form at `app.js:246`). Interactive content in `label` is invalid HTML and activating it also triggers label behaviour. | **CONFIRMED** |
| D8 | `echoes.html:73`, `placeholder="{{ title }} {{ link }}"` is evaluated as template variables, so the placeholder renders empty. The textarea default gets it right with quoting. | **CONFIRMED** |
| A7 | Single-mode token login (`auth.py:293-309`) never touches the throttle that the multi branch uses. Unlimited guesses against the one credential. Note the middleware accepts the same token per-request, so throttling `/login` alone is not a complete fix. | **CONFIRMED** |
| A8 | `app.py:1961` sets the OAuth state cookie with `secure=request.url.scheme == "https"` only, ignoring `FORCE_SECURE_COOKIE`, which exists precisely because the scheme reads `http` behind Caddy, and which `auth.py:172` honours for the session cookie. | **CONFIRMED** |
| A9 | `_forgot_throttled` (`auth.py:359-363`) always reassigns the bucket, so IP keys are never evicted (unlike `_prune`, which pops empty keys on purpose), and it mutates the dict without `_login_lock`. | **CONFIRMED** |
| A10 | `secrets.compare_digest` on `str` raises `TypeError` on non-ASCII input: `app.py:229`, `auth.py:299`, `oauth.py:106`. A cookie or `state` with a non-ASCII byte becomes a 500 instead of a clean rejection. | **CONFIRMED** |
| A12 | `validate_config()` enforces `DATABASE_URL` and `SESSION_SECRET` in multi mode but not `BASE_URL` (default `""`), so a deployment can start clean and then email verification/reset links as bare paths. | **CONFIRMED** |
| B6 | `posted_at` is written with `CURRENT_TIMESTAMP` (`scheduler.py:722, 776`) but compared against app-generated UTC strings in `_drip_rate` (539-548). sqlite's `CURRENT_TIMESTAMP` is UTC; Postgres converts to the session `TimeZone`, so on a non-UTC server the 60-minute drip window skews by the offset. Conditional on server TZ. | **CONFIRMED (conditional)** |
| B10 | `feed_parser.py:398`, `mktime()` treats feedparser's UTC `struct_time` as local wall time, then labels the result UTC. Every item date is shifted by the host offset. The host TZ here is America/New_York. Fix is `calendar.timegm`. | **CONFIRMED** |
| B11 | `database.py:851` calls `init_db()` at import time, so schema creation and the non-atomic rename-and-recreate migrations run in every process that imports the module, including concurrently under multiple workers. | **CONFIRMED** |
| B12 | `_queue_for_digest` calls `record_success(echo_id)` at merely queueing an item (`scheduler.py:1261`); `_queue_for_drip` deliberately does not, and says why at 598-600. A previously-alerting digest echo gets a false "recovered" email. | **CONFIRMED** |
| C3 | `bluesky.py:152-154` does not type-check `response.json()`, so a `did:web` document that is a JSON array raises `AttributeError`, which the `(httpx.HTTPError, ValueError)` handlers do not catch. The DID document host is tenant-controllable. | **CONFIRMED** |
| C4 | `bluesky.py:126` pastes everything after `did:web:` straight into the host, ignoring the spec's percent-encoded ports and colon-to-path-segment rules. Port-form DIDs resolve against the wrong host; path-form DIDs make httpx raise `InvalidURL`, which escapes every handler. Rare in practice (did:plc dominates). | **CONFIRMED** |
| C7 | `mastodon.py:50-52`, `response.json()` on a 200 with a non-JSON body raises `ValueError`, which the except tuple omits, breaking the documented "None on failure" contract. | **CONFIRMED** |
| C8 | `template_engine.py:74-78` rewrites legacy date tokens across the whole expression including quoted literals, so `{{ item["date:short"] }}` silently looks up `date_short`. The comment at 38-41 only holds for text outside `{{ }}`. | **CONFIRMED** |
| E6 | `nix/module.nix:122` hardcodes `StateDirectory = "feedecho"` while the DB path follows the configurable `dataDir` under `ProtectSystem = "strict"`, any non-default `dataDir` yields a service that cannot write its database. | **CONFIRMED** |
| E7 | `docker.yml` triggers on tag push with no `needs`/`workflow_run` gate on `tests.yml`, so a failing tag still publishes `latest` and the semver tag to GHCR. | **CONFIRMED** |
| E8 | `nix/package.nix:68` sets `mainProgram = "uvicorn"` although the adjacent comment states the wheel ships no console script, so `nix run` fails. | **CONFIRMED** |

### Wrong or overstated

| # | Claim | Why it fails |
|---|-------|--------------|
| B1 | "Soft-deleted echoes keep receiving new feed items indefinitely" (HIGH) | `delete_echo` (`app.py:1857-1867`) sets `enabled = 0` alongside `deleted_at`, and the scheduler filters `enabled = 1`. `toggle_echo` (1746) filters `deleted_at IS NULL`, so a deleted echo cannot be switched back on. **FALSE POSITIVE.** Adding `e.deleted_at IS NULL` is still reasonable defence in depth, the current safety depends on two columns being written together. |
| D1 | "Stored SMTP password and vision API key rendered into the HTML response" (HIGH) | Both are masked to `********` before render (`app.py:484` and `1066`), and both save handlers treat that sentinel as "unchanged" (`1298`, `1365`). **FALSE POSITIVE.** |
| D2 | "Feed-sourced and error text interpolated unescaped → stored XSS" (HIGH) | `app.py:55` builds the Jinja env with `select_autoescape(["html"])` and every template is `.html`, so autoescaping is on. GLM offered this as its own alternative branch. **FALSE POSITIVE**; the residual is redundant `| e` filters, which are harmless. |
| D4 | "`data-enabled` renders `True`/`False` on Postgres, silently disabling echoes" (MEDIUM) | `enabled` and `attach_image` are `INTEGER` in both schemas (`database.py:253/258` and `678/683`); psycopg returns `1`, which renders `"1"`. Verified against a live Postgres 17. **FALSE POSITIVE.** |
| C6 | "No size cap on images sent to the vision API" (MEDIUM) | `fetch_image` already rejects bodies over `MAX_IMAGE_SIZE` (10 MB) before the blob reaches `alt_text`. Bounded, if generously. **OVERSTATED.** |
| C2 | "`generate_alt_text` can raise … can kill the scheduler loop for all tenants" (HIGH) | The `IndexError`/`AttributeError` holes are real (`alt_text.py:124-136` vs the except tuple at 138), but both call sites wrap it in `except Exception` (`scheduler.py:842`, `app.py:1390`), so the effect is "alt text silently skipped". **OVERSTATED**, real bug, LOW. |
| A11 | "`verify_password` crashes on a NULL hash" (LOW) | `password_hash TEXT NOT NULL DEFAULT ''` in both schemas, so `None` cannot come from the DB; `''` hits the caught `ValueError`. Unreachable hardening nit. |

---

## Suggested order of work

1. **B3 + B4**, one-line dialect normalisations, both live on Postgres today. Add pg regression tests (second Bluesky post, `last_fetched` due calculation).
2. **E5**, add `:?` to `FEEDCHO_AUTH_TOKEN`, or refuse to start single mode with an empty token. An open instance from a typo is the worst failure mode in the list.
3. **A4, A5, A3**, three small correctness fixes with clear user-visible symptoms.
4. **B9**, add the `session_epoch` backfill to the sqlite path; the local DB proves the gap.
5. **A1**, flip the nine handlers to `def` (or `await asyncio.to_thread`). Mechanical, and it removes a trivially reachable stall.
6. **C1, C5, A2**, tenant-scoping and outbound-validation gaps in multi mode.
7. **D3, D6, D7**, the accessibility and `javascript:` items, batched as one front-end pass.
8. Everything else as cleanup.

---

## Raw reviews

Verbatim GLM 5.3 output follows, one section per chunk.


### Chunk A — HTTP layer, auth, authorization, tenant scoping (app.py, auth.py, security.py, oauth.py, verification.py, settings.py, filters.py, logging_setup.py)

### [HIGH] Blocking network/SMTP calls run on the event loop in `async def` routes
- **Where:** `app.py:1112`, `app.py:1312-1314`, `app.py:1497`, `app.py:1523`, `app.py:1568`, `app.py:1945`, `app.py:2008`, `app.py:2022` (also `app.py:647`, `app.py:819`)
- **What:** These handlers are `async def` yet call synchronous blocking I/O directly: `test_connection`, `fetch_feed`, `check_feed`, `test_smtp_connection`, `send_system_email`, `get_authorize_url`/`exchange_code`/`verify_credentials` (sync `httpx.Client`, 30 s timeout at `oauth.py:199`). The codebase itself states the rule at `app.py:1164-1168` ("Synchronous route (threadpool-offloaded): it performs blocking DNS, HTTPS, and SQLite work that must not run on the event loop") and marks `add_bluesky_account`/`preview_template` `def` for that reason.
- **Why it matters:** One user clicking "Test feed" on a slow/hanging feed — or "Fetch now", which runs a full `check_feed` poll plus posting — stalls the single event loop for up to 30 s+ per call. Every concurrent request from every tenant freezes; trivially repeatable DoS.
- **Fix:** Declare these handlers `def` instead of `async def` (FastAPI offloads sync handlers to the threadpool), or wrap each blocking call in `await asyncio.to_thread(...)`.

### [HIGH] User SMTP settings route skips the CRLF/port/email validation the admin route mandates
- **Where:** `app.py:1270-1304` (contrast `app.py:760-792`)
- **What:** `/api/settings/smtp` stores `smtp_host`, `smtp_username`, `smtp_from_email`, `smtp_from_name`, `smtp_port` per user with no checks: no `[\r\n]` rejection (admin does this at 788-792), no from-address format check (771-774), no port range check (763-769). The admin route's comment (760-761) states these are header fields that "must fail here, not at send time".
- **Why it matters:** Any authenticated tenant can store CR/LF in `smtp_from_name`/`smtp_from_email` and inject arbitrary SMTP headers (e.g. `Bcc:`) into emails the app later sends under that configuration; an out-of-range port is persisted and only explodes at send time — exactly the failure mode the admin route documents and prevents.
- **Fix:** Mirror the admin checks before the INSERT loop: reject `re.search(r"[\r\n]", value)` for host/username/from_name, validate `smtp_from_email` with the same regex, and require `1 <= port <= 65535`.

### [MEDIUM] OAuth callback error paths raise 401 in multi mode (exempt route has no `user_id`)
- **Where:** `app.py:1977`, `app.py:2011-2015` (mechanism: `app.py:192` + `app.py:511` + `auth.py:62-66`)
- **What:** `/oauth/callback` is in `_MULTI_EXEMPT_PATHS` (`app.py:192`), so `AuthMiddleware._multi` returns before setting `request.state.user_id`. Both error paths call `_render_accounts_error`, whose first line is `current_user_id(request)` (`app.py:511`), which raises `HTTPException(401)` in multi mode when `user_id` is absent.
- **Why it matters:** In multi mode, a user who denies authorization on Mastodon (`?error=access_denied`) or whose token exchange fails gets a raw 401 "Authentication required" JSON instead of the intended error page — the error handling crashes on exactly the paths it exists for.
- **Fix:** In `oauth_callback`, render a standalone error template (not `_render_accounts_error`, which needs the per-user accounts query), e.g. `render("error.html", request, status_code=400, code=400, message=...)`.

### [MEDIUM] "Retry" on a `gave_up` post is a silent no-op — status is never reset
- **Where:** `app.py:1582-1594`
- **What:** `retry_post`'s UPDATE sets `attempt_count = 0, next_retry_at = NULL, error_message = NULL` but never touches `status`, while its WHERE accepts `status IN ('failed', 'gave_up')`. `give_up_post` (`app.py:1597-1613`) makes a row terminal solely by setting `status = 'gave_up'` (its docstring: "Mark a failed row terminal").
- **Why it matters:** If the scheduler revived rows on `next_retry_at IS NULL` alone, `give_up` would itself be a no-op — so revival must be gated on status, and a `gave_up` row stays terminal. The endpoint returns `{"success": True, "Post queued for retry"}` while nothing will ever reprocess the row.
- **Fix:** Add `status = 'failed'` to the SET clause of the UPDATE.

### [MEDIUM] Logout deletes the wrong cookie in single mode
- **Where:** `auth.py:351-354` (route at `app.py:311-313`)
- **What:** `logout()` deletes only `COOKIE_NAME` (`feedecho_session`), but single mode authenticates via the `feedecho_auth` cookie (`auth.py:301-307`, checked at `app.py:222`).
- **Why it matters:** In single mode with `FEEDCHO_AUTH_TOKEN` set, an authenticated user's POST `/logout` clears a cookie that was never set; the shared-secret cookie survives and the user remains fully logged in. Logout is a no-op.
- **Fix:** Delete both cookies (or branch on `settings.MULTI`):
```python
response.delete_cookie(COOKIE_NAME)
response.delete_cookie("feedecho_auth")
```

### [MEDIUM] Mastodon/email account deletion leaves dangling live echoes (guard exists only for Bluesky)
- **Where:** `app.py:1116-1123` and `app.py:1144-1152` (contrast `app.py:1241-1259`)
- **What:** `delete_account` and `delete_email_account` hard-DELETE with no check for echoes still referencing the row, while `delete_bluesky_account` refuses when live echoes use the account (`app.py:1245-1259`).
- **Why it matters:** Deleting a Mastodon or email account targeted by enabled echoes leaves those echoes pointing at a nonexistent `destination_id`; delivery fails at run time with no warning at delete time — the exact breakage the Bluesky guard exists to prevent.
- **Fix:** Replicate the dependent-echo `COUNT(*)` check with `destination_type = 'mastodon'` / `'email'` before each DELETE.

### [MEDIUM] No rate limiting on single-mode token login
- **Where:** `auth.py:293-309`
- **What:** The single-mode branch of `login_submit` never calls `_throttled`/`_record_failure` (defined at `auth.py:131-157`); the throttle is only wired into the multi branch (`auth.py:311-318`).
- **Why it matters:** `POST /login` accepts unlimited shared-token guesses — the token is the sole credential in single mode, so a weak operator-chosen `FEEDCHO_AUTH_TOKEN` is brute-forceable with no lockout.
- **Fix:** Apply the same IP throttle in the single branch: check `_throttled(ip)` on entry and `_record_failure(ip)` on a mismatch.

### [LOW] OAuth session cookie ignores `FORCE_SECURE_COOKIE`
- **Where:** `app.py:1961` (contrast `auth.py:172`, `settings.py:48-53`)
- **What:** The cookie is set with `secure=request.url.scheme == "https"` only. `FORCE_SECURE_COOKIE` exists precisely because behind Caddy the scheme reads `http` (`settings.py:49-51`) and is applied to the session cookie at `auth.py:172` — but not here.
- **Why it matters:** In the stated production topology (TLS terminated at Caddy), the OAuth state-binding cookie is issued without `Secure` and will be sent over plaintext HTTP on any downgrade.
- **Fix:** `secure=request.url.scheme == "https" or settings.FORCE_SECURE_COOKIE`.

### [LOW] `_forgot_attempts` never evicts keys — unbounded memory growth
- **Where:** `auth.py:359-363`
- **What:** `_forgot_throttled` always assigns `_forgot_attempts[ip] = bucket`, including when the pruned bucket is empty, so keys are never removed — unlike `_prune` (`auth.py:121-128`), which deliberately pops empty keys "so the dict can't grow". It also mutates the dict without `_login_lock`.
- **Why it matters:** Every distinct IP that ever requests `/forgot-password` leaves a permanent entry; a client rotating source addresses grows the dict without bound.
- **Fix:** `if bucket: _forgot_attempts[ip] = bucket else: _forgot_attempts.pop(ip, None)`, under `_login_lock`.

### [LOW] `compare_digest` on raw request strings → unhandled TypeError (500) on non-ASCII input
- **Where:** `app.py:229`, `auth.py:299`, `oauth.py:106`
- **What:** `secrets.compare_digest`/`hmac.compare_digest` raise `TypeError` when a `str` argument contains non-ASCII characters. None of these call sites handle it — `app.py:1993` catches only `ValueError` around `verify_state`.
- **Why it matters:** A login attempt (or OAuth `state` parameter) containing non-ASCII characters produces an unhandled 500 instead of a clean rejection.
- **Fix:** Compare bytes (`token.encode("utf-8")` vs `settings.AUTH_TOKEN.encode("utf-8")`), or catch `TypeError` alongside `ValueError`.

### [LOW] `verify_password` crashes on a NULL hash instead of failing closed
- **Where:** `security.py:67`, `security.py:83`
- **What:** `stored.split("$")` raises `AttributeError` when `stored` is `None`, and the `except` clause only catches `(ValueError, TypeError)` — contradicting the docstring "False on any malformed or unknown-format input".
- **Why it matters:** A users row with a NULL `password_hash` turns a login attempt for that email into a 500 rather than a failed login.
- **Fix:** Add `if not isinstance(stored, str): return False` at the top (or add `AttributeError` to the except tuple).

### [LOW] `validate_config()` doesn't require `BASE_URL` in multi mode — verification/reset emails get relative links
- **Where:** `settings.py:63-81` (with `auth.py:264`, `auth.py:384`, `app.py:646`)
- **What:** `validate_config()` enforces `DATABASE_URL` and `SESSION_SECRET` in multi mode but not `BASE_URL`, whose default is `""` (`settings.py:20`). Links are built as `f"{settings.BASE_URL.rstrip('/')}/verify-email?token=..."`.
- **Why it matters:** A multi-mode deployment without `FEEDCHO_BASE_URL` starts cleanly and then sends verification and password-reset emails containing unusable relative links (`/verify-email?token=...`) — both flows silently broken despite the fail-fast intent of `validate_config`.
- **Fix:** In `validate_config()`: `if not BASE_URL: raise RuntimeError("FEEDCHO_BASE_URL must be set when FEEDCHO_MODE=multi")`.

No accessibility findings — the excerpt contains no HTML/CSS/JS source (templates or static assets) to review.


### Chunk B — Data layer, SQL dialect, scheduler (database.py, scheduler.py, feed_parser.py, notify.py)

### [HIGH] Soft-deleted echoes keep receiving new feed items
- **Where:** `scheduler.py:196-206` (both branches of the echoes query)
- **What:** The echo selection in `_check_feed_with_lease` filters only on `feed_id` and `enabled = 1`; it never checks `e.deleted_at IS NULL`. The same unfiltered list is then reused by `_retry_due_failures` (`scheduler.py:283-308`).
- **Why it matters:** Echoes are soft-deleted by design (`database.py:261`, and the flush paths prove the intent: `flush_drips` discards backlogs for `row["deleted_at"]` at `scheduler.py:1389-1396`, `flush_digests` filters `e.deleted_at IS NULL` at `scheduler.py:1474`). With this gap, a user who deletes an echo keeps getting new feed items cross-posted to their Mastodon/Bluesky account and email — indefinitely, in both single and multi mode.
- **Fix:** Add the filter to both branches (and consider `u.suspended = 0` in the MULTI branch, since `users.suspended` exists at `database.py:358/631`):
```sql
SELECT e.* FROM echoes e JOIN users u ON u.id = e.user_id
 WHERE e.feed_id = ? AND e.enabled = 1 AND e.deleted_at IS NULL AND u.email_verified = 1
```

### [HIGH] SSRF protection is bypassable via DNS rebinding (TOCTOU)
- **Where:** `feed_parser.py:81-95` (validation resolve) and `feed_parser.py:142` (actual request)
- **What:** `validate_outbound_url` resolves the hostname with `socket.getaddrinfo` and checks the result, but the subsequent `client.get(url)` performs its own, independent DNS resolution. The same applies to every redirect hop (`feed_parser.py:154`).
- **Why it matters:** In multi mode any tenant can register a feed (or serve a redirect / feed-embedded `image_url`) on a domain they control with low-TTL DNS: first resolution returns a public IP (passes the check), the second returns `127.0.0.1`, `169.254.169.254`, or an RFC1918 address — giving server-side access to internal services from the SaaS host.
- **Fix:** Resolve once and pin the connection to the validated IP (custom `httpx` transport that dials the checked address while preserving TLS SNI/Host), or re-validate inside a connection-time hook so the address actually dialed is the address checked.

### [HIGH] On Postgres, Bluesky delivery breaks after the first post (datetime vs str comparison)
- **Where:** `scheduler.py:969-970`
- **What:** `account["session_expires_at"]` is a `TIMESTAMP` column; on Postgres/psycopg it comes back as a `datetime` object, so `expires_at > now` (a `"YYYY-MM-DD HH:MM:SS"` string) raises `TypeError`. The exception is swallowed by the broad handler at `scheduler.py:1052-1056` and recorded as "Bluesky session failed".
- **Why it matters:** The first delivery caches `session_expires_at` (`scheduler.py:999`); every subsequent delivery for that account hits the `TypeError`, fails, retries, and eventually gives up — Bluesky is effectively unusable in multi (Postgres) mode.
- **Fix:** Normalize before comparing:
```python
expires_at = account["session_expires_at"]
if expires_at:
    expires_at = expires_at.strftime("%Y-%m-%d %H:%M:%S") if isinstance(expires_at, datetime) else str(expires_at)
if expires_at and expires_at > now:
```

### [HIGH] On Postgres, `poll_interval` is ignored — every feed is fetched every 2 minutes
- **Where:** `scheduler.py:1564`
- **What:** `feed["last_fetched"].replace("T", " ")` assumes a string. On Postgres the column is returned as a `datetime`, and `datetime.replace("T", " ")` raises `TypeError`, which is caught at `scheduler.py:1568` and treated as "malformed → due".
- **Why it matters:** In multi mode every feed with a `last_fetched` value is appended to `due` on every run of `check_all_feeds` (every 2 minutes, `scheduler.py:1597`), regardless of the user's `poll_interval` — a 24h feed gets hammered 720×/day, multiplying outbound load and risking bans by feed hosts.
- **Fix:**
```python
lf = feed["last_fetched"]
if isinstance(lf, datetime):
    last = lf.replace(tzinfo=timezone.utc)
else:
    last = datetime.strptime(str(lf).replace("T", " ")[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
```

### [MEDIUM] Response bodies fully buffered before the size cap is applied (memory DoS)
- **Where:** `feed_parser.py:115-121` (feeds) and `feed_parser.py:359-363` (images)
- **What:** `client.get(...)` (non-streaming) materializes the entire body in memory; `MAX_FEED_SIZE` / `MAX_IMAGE_SIZE` are only checked afterwards.
- **Why it matters:** Any registered feed or feed-embedded image URL can serve a multi-gigabyte body; the worker buffers it all before rejecting, so a single hostile URL (fetched every 2 minutes per the scheduler) can OOM the process — a tenant-reachable DoS in multi mode.
- **Fix:** Use `client.stream("GET", url)` and abort as soon as accumulated bytes exceed the cap (check `Content-Length` first, then enforce while iterating `response.aiter_bytes()`/`iter_bytes()`).

### [MEDIUM] `posted_at` written with DB `CURRENT_TIMESTAMP` but compared against app-generated UTC strings
- **Where:** `scheduler.py:722` and `scheduler.py:776` (also `_requeue_drip_failure:1293`, `_discard_drip_backlog:1350`, `flush_digests:1531`, and the column defaults at `database.py:479/778`)
- **What:** On Postgres, `CURRENT_TIMESTAMP` assigned to a `TIMESTAMP` (without tz) column is stored in the session's `TimeZone` local time, while `_drip_rate` compares `posted_at >= ?` against `_timestamp_after(-3600)`, a UTC string (`scheduler.py:539-548`). SQLite's `CURRENT_TIMESTAMP` is always UTC, so only Postgres is affected.
- **Why it matters:** On any non-UTC Postgres server the 60-minute drip window is skewed by the UTC offset: posts fall out of (or never enter) the window, so the user's drip rate limit either over-throttles or is bypassed entirely.
- **Fix:** Write `posted_at = ?` with `_now()` in these UPDATEs (and pass an explicit value on insert), or force the Postgres session `TimeZone` to UTC at connect time.

### [MEDIUM] Digest items for deleted/disabled echoes or missing email accounts are stuck forever
- **Where:** `scheduler.py:1463-1475`
- **What:** `flush_digests` only processes echoes matching `enabled = 1`, `e.deleted_at IS NULL`, `f.deleted_at IS NULL`, and an INNER JOIN on `email_accounts` (`scheduler.py:1469`). There is no discard path: unlike `flush_drips`, which explicitly finalizes backlogs of deleted/disabled echoes (`_discard_drip_backlog`, `scheduler.py:1335-1355`), digest rows that no longer match are never sent and never cleaned up.
- **Why it matters:** Deleting an echo (or its email account) with pending digest items leaves those `digest_items` rows and their `posted_items` rows (`status='queued'`) permanently stuck — silent non-delivery plus unbounded table growth (compounded by the missing `deleted_at` filter in `check_feed`, which keeps queueing new items for deleted echoes).
- **Fix:** Mirror the drip behavior: sweep `digest_items` whose echo is deleted/disabled or whose destination account is gone, mark the `posted_items` rows `gave_up` with a reason, and delete the `digest_items` rows.

### [MEDIUM] Mastodon/email dispatch skips the claim-ownership re-check that Bluesky performs
- **Where:** `scheduler.py:825-885` (image fetch + `upload_media` + `post_status` with no re-check) vs. the explicit guard at `scheduler.py:1133-1149`
- **What:** Between `_claim_post` and `post_status`, the Mastodon path performs slow network I/O (image fetch with up to 6 requests × 30s timeout, upload, optional alt-text generation) without re-validating that it still owns the claim. The Bluesky path re-checks precisely because "if the lease lapsed and another worker reclaimed this row, posting would duplicate" (`scheduler.py:1133-1134`).
- **Why it matters:** If processing exceeds `PENDING_RECLAIM_SECONDS` (10 min) and the feed lease (15 min, renewed only per item at `scheduler.py:248`) lapses, a second worker reclaims the pending row and posts; the first worker then posts too — a duplicate public post (the first worker's `_update_post` at `scheduler.py:890` silently no-ops).
- **Fix:** Replicate the ownership check from `scheduler.py:1135-1142` in `_send_mastodon` immediately before `post_status` (and in `_send_email_echo` before `send_email`), returning `False` when the claim is lost.

### [MEDIUM] SQLite migration path never backfills `users.session_epoch`
- **Where:** `database.py:377` vs. `database.py:847`
- **What:** `init_db_postgres` migrates `session_epoch` via `_add_column_if_missing` (with a comment saying it was added later), but `init_db_sqlite` only backfills `is_admin` — an existing SQLite database created before `session_epoch` existed keeps running without the column.
- **Why it matters:** Upgraded self-hosted installs lack `users.session_epoch`; any query selecting or updating it (the column exists in the CREATE at `database.py:360` and is documented as "bumped on password reset, invalidating all prior session cookies") fails with "no such column" on SQLite.
- **Fix:** Add to `init_db_sqlite` next to line 377:
```python
_add_column_if_missing(db, "users", "session_epoch", "INTEGER NOT NULL DEFAULT 0")
```

### [LOW] `mktime` misinterprets feedparser's UTC struct_time as local time
- **Where:** `feed_parser.py:398`
- **What:** `datetime.fromtimestamp(mktime(parsed), tz=timezone.utc)` — `mktime` treats the struct_time as local wall time, but feedparser's `published_parsed`/`updated_parsed` are UTC; the result is then labeled UTC.
- **Why it matters:** On any server not running UTC, every item date rendered via templates (`{{ date }}`) is shifted by the server's UTC offset.
- **Fix:** `datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)`.

### [LOW] `init_db()` runs as an import side effect
- **Where:** `database.py:851`
- **What:** Schema creation and the non-atomic rename-and-recreate migrations (`database.py:384-435`, `450-463`) execute at import time in every process that imports `database` (scheduler, notify, any script or test).
- **Why it matters:** Multi-worker deployments run these migrations concurrently; the `DROP IF EXISTS` → `RENAME` → `CREATE` → `INSERT SELECT` sequences are not concurrency-safe and can interleave (failed imports or duplicated rows), and merely importing the module mutates the production database.
- **Fix:** Remove the module-level call and invoke `init_db()` once from an explicit startup hook (app lifespan / entry point), guarded by an advisory lock for multi-worker setups.

### [LOW] `_queue_for_digest` records a "success" for merely queueing an item
- **Where:** `scheduler.py:1261` vs. the documented rationale at `scheduler.py:598-600`
- **What:** `_queue_for_digest` calls `record_success(echo_id)` when an item is only queued, while `_queue_for_drip` deliberately does not, with a comment explaining that clearing alert state on a hold "would suppress alerts for echoes that are not actually delivering."
- **Why it matters:** A previously-alerted digest echo gets a spurious "recovered" email and its alert state reset at queue time, so a genuinely broken SMTP path keeps failing silently until the threshold re-accumulates.
- **Fix:** Delete the `record_success(echo_id)` call at `scheduler.py:1261` (call it only in `flush_digests` after the email is actually sent, which already happens at `scheduler.py:1542`).

No accessibility findings: this excerpt contains no HTML, CSS, or JavaScript.


### Chunk C — Publishers and templating (bluesky.py, mastodon.py, email_sender.py, alt_text.py, template_engine.py)

### [HIGH] SSRF: user-controlled vision-API base URL is fetched without any outbound-URL validation
- **Where:** `alt_text.py:85` and `alt_text.py:110-119`
- **What:** `alt_text_ai_base_url` is a per-user setting (docstring lines 7-11, loaded per `user_id` at lines 54-61), and `endpoint = f"{base_url}/chat/completions"` is POSTed at line 119 with no `validate_outbound_url()` call — unlike every other outbound hop in this codebase (`bluesky.py:105,129,163`, `mastodon.py:28,82,108`).
- **Why it matters:** In MULTI mode a tenant can point `alt_text_ai_base_url` at internal hosts (cloud metadata, intranet services, `127.0.0.1` ports). The app server makes the request on their behalf; a 200 JSON response is reflected back as the generated alt text (lines 123-136), and success/failure timing gives a port-scan oracle. POSTing to internal endpoints that accept unauthenticated writes is also possible.
- **Fix:** Import and apply the same guard used elsewhere:
```python
from feed_parser import validate_outbound_url
...
endpoint = f"{base_url}/chat/completions"
validate_outbound_url(endpoint)
```

### [HIGH] `generate_alt_text` can raise despite its documented "never raises" contract
- **Where:** `alt_text.py:123-138` (specifically lines 124, 131, 136)
- **What:** `parsed.get("choices", [{}])[0]` raises `IndexError` when the API returns `"choices": []` (the default `[{}]` only applies when the key is absent); `.get("message", {})` raises `AttributeError` when `"message"` is `null`; `.strip()` at line 136 raises `AttributeError` if `content` is a list. The `except` tuple at line 138 is `(httpx.HTTPStatusError, httpx.RequestError, KeyError, ValueError)` — `IndexError` and `AttributeError` are not caught.
- **Why it matters:** Line 79 promises "Never raises — alt text is best-effort", so callers won't defend against it. An OpenAI-compatible endpoint that returns empty `choices` (content-filtered responses, some proxies do this) throws an unhandled exception into the posting pipeline, which per project context can kill the scheduler loop for all tenants.
- **Fix:**
```python
choices = parsed.get("choices") or []
if not isinstance(choices, list) or not choices:
    return ""
message = choices[0].get("message") or {}
if not isinstance(message, dict):
    return ""
content = message.get("content") or message.get("reasoning_content")
return content.strip() if isinstance(content, str) else ""
```
and add `IndexError, AttributeError` to the except tuple as a backstop.

### [MEDIUM] `resolve_pds` crashes with uncaught `AttributeError` on non-dict DID documents
- **Where:** `bluesky.py:152-154` (also `bluesky.py:120`, `bluesky.py:136`, `bluesky.py:147`)
- **What:** `doc = response.json()` is not type-checked; `doc.get("service")` (line 152) raises `AttributeError` if the DID document is a JSON array/string, and `service.get("serviceEndpoint")` (line 153) raises if a service entry is not a dict. The handlers at lines 117, 137, 148 catch only `(httpx.HTTPError, ValueError)`, and `test_connection` (lines 550-554) catches only `ValueError`/`BlueskyError` subclasses.
- **Why it matters:** For `did:web` handles the DID document is fetched from a tenant-controllable host (lines 126-134): in MULTI mode a tenant with a custom-domain handle can serve `[]` and crash the shared connection-test request or posting loop for everyone.
- **Fix:** After each `response.json()`, add `if not isinstance(doc, dict): raise BlueskyError("Invalid DID document")`, and skip non-dict service entries in the loop at line 152.

### [MEDIUM] `did:web` URL construction violates the did:web method spec (ports and path segments)
- **Where:** `bluesky.py:126`
- **What:** The spec encodes ports percent-encoded (`did:web:example.com%3A8443` → `https://example.com:8443/...`) and treats extra colon segments as path components (`did:web:example.com:users:alice` → `https://example.com/users/alice/did.json`). The code pastes everything after `did:web:` into the host verbatim, producing `https://example.com%3A8443/.well-known/did.json` (wrong host) and `https://example.com:users:alice/.well-known/did.json` (invalid port).
- **Why it matters:** Port-form DIDs silently resolve against the wrong host; path-form DIDs make httpx raise `InvalidURL`, which is not an `httpx.HTTPError` or `ValueError`, so it escapes the `except` at line 137 and every handler in `test_connection` — an unhandled exception from a merely unusual (but valid) DID.
- **Fix:** Percent-decode the host portion, split remaining `:` segments into a URL path, and use the spec's no-`.well-known` path when segments exist; wrap URL building so any failure raises `BlueskyError`.

### [MEDIUM] `user_id` defaults to tenant 1 — silent cross-tenant credential fallback in MULTI mode
- **Where:** `email_sender.py:15`, `email_sender.py:95`, `email_sender.py:113`; `alt_text.py:51`, `alt_text.py:64`, `alt_text.py:75`
- **What:** Every settings-loading and send function defaults `user_id: int = 1`. In MULTI mode, any call site that omits the argument silently loads tenant 1's SMTP password/credentials or tenant 1's vision API key instead of failing loudly.
- **Why it matters:** Project context rates tenant-scoping defects as critical: mail for tenant N would be sent through tenant 1's SMTP account (wrong origin, tenant 1's quota/reputation), and tenant 1's paid API key would be consumed by other tenants' alt-text calls — with no error to surface the mistake.
- **Fix:** Make `user_id` a required keyword-only argument in multi mode, e.g. `def get_smtp_settings(*, user_id: int) -> dict | None:`, or `assert not settings.MULTI` when the default is used.

### [MEDIUM] No size cap on images sent to the vision API
- **Where:** `alt_text.py:92-93` and `alt_text.py:118-119`
- **What:** `image_bytes` is base64-encoded (≈1.33× memory) and POSTed with no length check — in contrast to the explicit 1 MB cap enforced for Bluesky blobs (`bluesky.py:436-437`).
- **Why it matters:** Feed images are externally sized; a malicious or huge feed image causes a large memory allocation plus a multi-megabyte upload on the worker for every retry attempt (line 116), a memory/bandwidth DoS vector and unbounded API cost.
- **Fix:** Add an early bound, e.g. `MAX_ALT_IMAGE_BYTES = 10_000_000; if len(image_bytes) > MAX_ALT_IMAGE_BYTES: return ""`.

### [LOW] `upload_media` lets `ValueError` escape its "None on failure" contract
- **Where:** `mastodon.py:50-52`
- **What:** `response.json()` at line 50 raises `ValueError` on a 200 response with a non-JSON body, but the `except` at line 51 catches only `(httpx.HTTPStatusError, httpx.RequestError)`, contradicting the documented "Dict … on success, None on failure" behavior (line 25).
- **Why it matters:** An instance or intercepting proxy that returns HTML with status 200 crashes the caller instead of the intended "skip the image" path.
- **Fix:** Add `ValueError` to the except tuple at line 51, or wrap the JSON parse in its own try/except returning `None`.

### [LOW] `_normalize` rewrites legacy date tokens inside string literals, contradicting its own comment
- **Where:** `template_engine.py:74-78`
- **What:** `_fix_expression` runs `inner.replace(old, new)` over the entire expression text, including quoted string literals. The comment at lines 38-41 claims literals are left untouched, but `{{ "date:iso" }}` renders as `date_iso`, and `{{ item["date:short"] }}` is rewritten to look up the key `date_short` instead.
- **Why it matters:** Templates whose literals legitimately contain the token text silently render wrong output with no error.
- **Fix:** Skip rewriting when the expression contains quotes, e.g. `if '"' in inner or "'" in inner: return match.group(0)` at the top of `_fix_expression`.

No accessibility defects — this excerpt contains no HTML, CSS, or JavaScript.


### Chunk D — Front end: accessibility, JS, injection (17 templates, style.css, app.js)

### [HIGH] Stored SMTP password and AI API key rendered into the HTML response
- **Where:** `templates/settings.html:30`, `templates/settings.html:113`
- **What:** `value="{{ smtp_settings.smtp_password or '' }}"` and `value="{{ alt_text_settings.get('alt_text_ai_api_key', '') }}"` pre-fill password inputs with the stored secrets. `type="password"` only masks display; the plaintext secret is in the response body, page source, and devtools.
- **Why it matters:** Anyone with access to the browser (shared machine, screen-share, browser sync/backup of the DOM, XSS elsewhere) reads the SMTP credential and the paid vision-API key. `templates/admin.html:99` does this correctly (blank value + "leave blank to keep" placeholder), proving the intended pattern.
- **Fix:** Render blank values with a "stored — leave blank to keep" placeholder, as in admin.html: `<input type="password" name="smtp_password" value="" placeholder="{% if smtp_settings.smtp_password %}Stored — leave blank to keep{% endif %}">`, and treat an empty POST field as "unchanged" server-side.

### [HIGH] HTML escaping applied inconsistently; feed-sourced and error text interpolated unescaped
- **Where:** `templates/history.html:28-30`, `templates/history.html:52`, `templates/feeds.html:34`, `templates/feeds.html:37`, `templates/echoes.html:164-179`, `templates/login.html:13`, `templates/base.html:48` (vs. the explicit `| e` at `templates/feeds.html:30-31` and `templates/echoes.html:155-159`)
- **What:** Data-* attributes are explicitly escaped with `| e`, which only makes sense if the engine does not autoescape — yet the same user/externally-sourced values are interpolated raw in text and attribute contexts elsewhere: `{{ post.item_title[:60] }}` (arbitrary feed content), `{{ post.error_message }}` (remote API error text), `{{ feed.name }}`, `{{ error }}` (can reflect submitted email), `title="{{ current_user_email }}"`.
- **Why it matters:** If the engine does not autoescape (as the deliberate `| e` usage implies), a malicious feed publisher gets stored XSS against the FeedEcho account owner via item titles/error strings — script runs with the victim's session and can rewrite SMTP settings or exfiltrate stored tokens. If the engine *does* autoescape, the `| e` calls are dead code and the inconsistency is still a maintenance trap.
- **Fix:** Apply `| e` (or verified autoescape) uniformly to every interpolation of persistent/external data: `{{ post.item_title[:60] | e }}`, `{{ post.error_message | e }}`, `{{ feed.name | e }}`, `title="{{ current_user_email | e }}"`, etc.

### [MEDIUM] External URLs rendered into `href` with no scheme validation
- **Where:** `templates/history.html:28`, `templates/history.html:33`, `templates/feeds.html:37`
- **What:** `href="{{ post.item_url }}"`, `href="{{ post.post_url }}"`, and `href="{{ feed.url }}"` output third-party/feed-publisher-controlled URLs verbatim; HTML escaping does not neutralize a `javascript:` scheme.
- **Why it matters:** A hostile feed can supply `item_url = "javascript:..."`; when the user clicks the item link (or "view post ↗") in History, script executes in the authenticated origin. Nothing in the templates restricts to `http(s):`.
- **Fix:** Validate scheme server-side before storing/rendering, and/or render defensively: `{% if post.item_url and post.item_url.startswith(('http://', 'https://')) %}<a href="{{ post.item_url }}">…</a>{% else %}…{% endif %}`.

### [MEDIUM] Edit form boolean state breaks when values render as `True`/`False` instead of `1`/`0`
- **Where:** `templates/echoes.html:160`, `templates/echoes.html:163` with `static/js/app.js:187` and `static/js/app.js:190`
- **What:** The template writes `data-attach-image="{{ echo.attach_image }}"` / `data-enabled="{{ echo.enabled }}"`, and the JS requires the exact string `'1'` (`row.dataset.attachImage === '1'`, `row.dataset.enabled === '1'`). On Postgres, BOOLEAN columns come back as Python `True`/`False`, which render as `"True"`/`"False"`.
- **Why it matters:** Dialect-dependent silent data loss: on the Postgres deployment, opening "Edit" on an enabled echo or one with image attachment shows both checkboxes unchecked; saving then disables the echo / drops image attachment without the user intending it. (Works only by accident on sqlite 0/1 integers.)
- **Fix:** Normalize in the template: `data-enabled="{{ 1 if echo.enabled else 0 }}"` and `data-attach-image="{{ 1 if echo.attach_image else 0 }}"`.

### [MEDIUM] Destination-type options always shown in edit form because whitespace-only `innerHTML` is truthy
- **Where:** `static/js/app.js:193-195` and `static/js/app.js:209-211`, fed by `templates/echoes.html:216-227`
- **What:** `const mastoOpts = document.getElementById('mastodon-options').innerHTML;` returns the template's newlines/indentation even when the `{% for %}` loop renders zero options, so `mastoOpts ? '<option value="mastodon"...'` always emits the Mastodon/Email/Bluesky options. The create form correctly uses `{% if mastodon_accounts %}` (echoes.html:19-27); the JS edit path does not.
- **Why it matters:** Editing an echo offers destination types the user has no accounts for, with empty account `<select>`s; switching to one submits no/first account id and can silently re-point or break the echo.
- **Fix:** Trim and test for content: `const mastoOpts = document.getElementById('mastodon-options').innerHTML.trim();` (and same for email/bluesky/feed lists), or emit a `data-count` attribute and branch on that.

### [MEDIUM] Form inputs rely on placeholder text alone — no programmatic labels
- **Where:** `templates/login.html:18-23`, `templates/login.html:31-32`, `templates/register.html:13-22`, `templates/feeds.html:7-9`, `templates/accounts.html:30`, `templates/accounts.html:39-42`, `templates/accounts.html:52-53`, `templates/accounts.html:62-64`, `templates/settings.html:53`
- **What:** These required inputs (email, password, token, feed name/URL, instance, access token, app password, test email…) have no `<label>`/`aria-label`; the only "label" is `placeholder`, which vanishes on input and is unreliable for screen readers. `poll_interval` (feeds.html:9) has only a `title`.
- **Why it matters:** WCAG 2.1 AA failure (1.3.1 / 3.3.2): screen-magnifier and screen-reader users cannot determine field purpose once typing, and password managers often can't map fields. `forgot_password.html:13-14` and `reset_password.html:15-18` show the correct pattern already in use.
- **Fix:** Add labels, e.g. `<label for="login-email">Email</label><input id="login-email" type="email" name="email" …>` (or `aria-label="Email"` where layout can't change).

### [MEDIUM] Status/flash messages and preview results are not announced to assistive tech
- **Where:** `templates/dashboard.html:19-22`, `templates/accounts.html:10-24`, `templates/login.html:12-14`, `templates/admin_email_result.html:5-9`, `static/js/app.js:352`, `static/js/app.js:369-371`
- **What:** Success/error flashes ("Email verified. Thanks!", "Mastodon account connected successfully.") are plain `<div>`s with no `role="status"`/`role="alert"`/`aria-live`, and `previewTemplate` injects results via `box.innerHTML` into a non-live region.
- **Why it matters:** WCAG 2.1 AA 4.1.3 (Status Messages): screen-reader users get no announcement that an account was connected, verification succeeded, or a template preview rendered — the page appears unchanged to them.
- **Fix:** Add `role="status"` to informational flashes and `role="alert"` to errors, and give the preview container `aria-live="polite"`.

### [LOW] Template placeholder attribute evaluates `title`/`link` as variables instead of literal text
- **Where:** `templates/echoes.html:73`
- **What:** `placeholder="{{ title }} {{ link }}"` is parsed as template output of (undefined) variables `title` and `link`, not the intended literal `{{ title }} {{ link }}` — unlike the textarea body, which correctly uses `'{{ title }} {{ link }}'`, and `howto.html:48` which escapes properly.
- **Why it matters:** The placeholder renders empty (or raises on undefined variables in a strict engine). Dead/misleading UI; if the engine is strict-undefined the whole Echoes page 500s.
- **Fix:** `placeholder="{{ '{{ title }} {{ link }}' }}"` — though note the placeholder never shows while the textarea has default content; consider removing the default value or the placeholder.

### [LOW] `toggleEcho` fails silently on error responses
- **Where:** `static/js/app.js:159-169`
- **What:** If the response is OK but `data.success` is falsy (or the API returns an error payload with 200), the function does nothing — no alert, no reload. Every sibling (`pauseFeed` at 70-82, `retryPost`, `giveUpPost`) reports failures.
- **Why it matters:** The user clicks "Toggle", nothing visibly happens, and they can't tell whether the echo was enabled or disabled — leading to echoes left in the wrong state.
- **Fix:** Add an `else { alert(data.detail || 'Failed to toggle echo'); }` branch mirroring `pauseFeed`.

### [LOW] Interactive `<button>` nested inside `<label>` — invalid HTML with side effects
- **Where:** `templates/echoes.html:74` (and replicated in the generated edit form at `static/js/app.js:246`)
- **What:** The "Preview" button sits inside the `<label>` wrapping the template `<textarea>`. HTML spec forbids interactive content inside `label`; activating the button also triggers label behavior toward the first labelable descendant (the textarea).
- **Why it matters:** Clicking Preview can additionally focus/scroll the textarea, and AT may announce an unexpected nesting; it's a spec violation that browsers handle inconsistently.
- **Fix:** Move the button (and the hint span) outside the label: `<label>Template <textarea …></textarea></label><button type="button" …>Preview</button>`.

No defects found in: `templates/about.html`, `templates/howto.html`, `templates/404.html`, `templates/error.html` (beyond the escaping issue noted above), `templates/forgot_password.html`, `templates/reset_password.html`, `templates/admin.html`, `static/css/style.css`.


### Chunk E — Container, CI and deployment config (Dockerfile, compose, Caddyfile, workflows, nix)

### [HIGH] `.env` is not dockerignored — secrets get baked into image layers
- **Where:** `.dockerignore:1-14` (omission), `Dockerfile:18`
- **What:** `.dockerignore` excludes `.git`, `data/`, `feedecho.db`, etc., but not `.env`. `COPY . .` therefore copies the project-root `.env` into the image at `/app/.env`.
- **Why it matters:** The documented workflows guarantee a `.env` exists in the build context: `docker-compose.multi.yml:4` instructs `cp .env.example.multi .env` (containing `FEEDCHO_SESSION_SECRET` and `POSTGRES_PASSWORD`, per `.env.example.multi:13,20`), and single-mode `docker-compose.yml:15` reads `FEEDCHO_AUTH_TOKEN` from `.env`. Anyone who then runs `docker compose up` (which does `build: .`) bakes the session-signing secret — which forges session cookies for every tenant in multi mode — plus the DB password and auth token into a readable image layer.
- **Fix:** Add to `.dockerignore`:
  ```
  .env
  .env.*
  !.env.example.multi
  ```

### [HIGH] `lib.fakeHash` makes the non-flake Nix package unbuildable
- **Where:** `nix/package.nix:18`
- **What:** `hash = lib.fakeHash;` is a deliberately invalid placeholder hash shipped in a tagged release. `nix/module.nix:21` falls back to `pkgs.callPackage ./nix/package.nix { }` for non-flake users, so the module's default package always fails with a hash mismatch when fetching `v1.13.4`.
- **Why it matters:** The entire documented non-flake NixOS deployment path (`services.feedecho.enable = true` without the flake) cannot build; every `nixos-rebuild` fails at the fetch step.
- **Fix:** Prefetch and pin the real hash:
  ```nix
  hash = "sha256-<result of nix-prefetch-url --unpack https://github.com/jcrabapple/feedecho/archive/v1.13.4.tar.gz>";
  ```

### [MEDIUM] Caddy container receives the entire `.env`, including the session secret and DB password
- **Where:** `docker-compose.multi.yml:72-73`
- **What:** The `caddy` service loads the full `.env` via `env_file`, but the Caddyfile only consumes `{$ACME_EMAIL}` and `{$FEEDECHO_DOMAIN}` (`Caddyfile:7,10`).
- **Why it matters:** The internet-facing TLS terminator — the component most exposed to request-parsing bugs — holds `FEEDCHO_SESSION_SECRET` and `POSTGRES_PASSWORD` in its environment. Code execution in the Caddy container (or `docker inspect` access) yields the secret that signs all multi-tenant session cookies, i.e. full auth bypass, plus DB credentials.
- **Fix:** Pass only the two variables Caddy needs:
  ```yaml
  environment:
    - ACME_EMAIL=${ACME_EMAIL}
    - FEEDECHO_DOMAIN=${FEEDECHO_DOMAIN}
  ```
  and drop `env_file` from the caddy service.

### [MEDIUM] Container runs as root
- **Where:** `Dockerfile:2-26` (no `USER` directive; `CMD` at line 26)
- **What:** The image never switches away from UID 0, so uvicorn — an app that fetches and parses untrusted remote feed content — runs as root inside the container, and the `feedecho-data` volume is created root-owned.
- **Why it matters:** Any file-write or code-execution bug in the app or a dependency runs with root privileges in the container, and the SQLite volume ends up owned by root, blocking any later migration to a non-root user without manual chown.
- **Fix:** Add before `CMD`:
  ```dockerfile
  RUN useradd --system --uid 1000 feedecho && chown -R feedecho /app
  USER feedecho
  ```

### [LOW] `FEEDCHO_AUTH_TOKEN` is not enforced at compose level
- **Where:** `docker-compose.yml:15`
- **What:** `${FEEDCHO_AUTH_TOKEN}` has no `:?` guard, so if the variable is unset, compose substitutes an empty string and starts the container with `FEEDCHO_AUTH_TOKEN=""` — despite the comment calling it "required". This is inconsistent with the `:?` enforcement used for `POSTGRES_PASSWORD` in `docker-compose.multi.yml:19,46`.
- **Why it matters:** A forgotten token silently deploys with an empty credential value instead of failing fast at `up` time; whether that yields an open instance depends on app-side handling of the empty string.
- **Fix:** `- FEEDCHO_AUTH_TOKEN=${FEEDCHO_AUTH_TOKEN:?set FEEDCHO_AUTH_TOKEN in .env}`

### [LOW] `StateDirectory` is hardcoded while `dataDir` is configurable, under `ProtectSystem=strict`
- **Where:** `nix/module.nix:122` (with `83-87` and `137`)
- **What:** `StateDirectory = "feedecho"` always makes `/var/lib/feedecho` the only writable path, but the DB location follows the user-settable `dataDir` (`FEEDCHO_DB_PATH = "${cfg.dataDir}/feedecho.db"`, line 113) while the filesystem is made read-only by `ProtectSystem = "strict"` (line 137).
- **Why it matters:** Any user who sets `services.feedecho.dataDir` to anything other than `/var/lib/feedecho` gets a service that cannot write its SQLite database and fails at runtime.
- **Fix:** Derive the writable path from `dataDir`, e.g. `StateDirectory = lib.removePrefix "/var/lib/" cfg.dataDir;` (when under `/var/lib`) or add `ReadWritePaths = [ cfg.dataDir ];`.

### [LOW] Release images are published without waiting on the test suite
- **Where:** `.github/workflows/docker.yml:3-7`
- **What:** Tag pushes trigger the docker publish workflow and the Tests workflow (`tests.yml:3-5`) in parallel; there is no `needs`/`workflow_run` gate, so `build-push` (line 45) pushes `latest` and semver tags to GHCR even if the test jobs are failing on that same tag.
- **Why it matters:** A broken or regressed release tag gets published as the image that `docker-compose.multi.yml:33` pins for production and that self-hosters pull as `latest`.
- **Fix:** Gate the publish job on the tests workflow (e.g. trigger on `workflow_run` for `Tests` with `conclusion: success`, or make the docker workflow call the test jobs first via `needs`).

### [LOW] `meta.mainProgram` points to a binary the package does not ship
- **Where:** `nix/package.nix:68`
- **What:** `mainProgram = "uvicorn"` is set, but as the adjacent comment (lines 66-67) states, the wheel installs no console script, so `$out/bin/uvicorn` does not exist in this derivation.
- **Why it matters:** `nix run` on this package fails with "program 'uvicorn' not found" instead of doing anything useful.
- **Fix:** Remove `mainProgram` (or point it at a real wrapper script added via `postInstall`).

No HTML, CSS, or JavaScript files appear in this excerpt, so no accessibility findings.
