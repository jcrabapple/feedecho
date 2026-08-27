# HANDOFF: FeedEcho multi-tenant refactor → live hosted v1.12.1
Generated: 2026-08-22 ~22:15 EDT · Session focus: 14-task multi-tenant plan completed, hosted service live

## 1. Goal

FeedEcho (RSS → Mastodon/Bluesky/email cross-poster) runs as a paid hosted
multi-tenant SaaS at feedecho.net (Postgres, per-user accounts) from the SAME
repo as the unchanged single-tenant OSS self-hosted mode. Targeting EchoFeed's
shut-down user base. Paid-only: $36/yr or $4/mo, card-gated 14-day trial.
Jason's time budget: 1-2 hrs/week, launch in 4-6 months.

## 2. Background / Context

EchoFeed (Robb Knight) announced shutdown 2026-08-06 — market window. Jason
approved a 14-task plan (docs/plans/2026-08-22-multi-tenant-refactor.md).
Hard constraints locked in the plan: stdlib-only auth (scrypt + HMAC/secrets,
no new runtime deps), psycopg[binary] as the only optional extra, single-mode
behavior bit-for-bit unchanged, same repo no fork, secrets only in Infisical
(dev env, project af3b8a09-35ab-4acc-b0ea-c4ef2201eb29), key-only SSH to the
VPS, Kimi K3 review gate before every release (Jason's standing rule).

## 3. Current State

- DONE: All 14 plan tasks. Auth (register/login/sessions, throttle), tenant
  scoping across every route and the scheduler (retry caps, notify, SMTP,
  alt-text all per-owner), OAuth state account-binding, UI gating (nav chrome,
  trial banner), CI matrix (single/multi/pg jobs in .github/workflows/tests.yml),
  PG dialect tests (tests/test_pg_dialect.py, 9 tests incl. full dashboard
  render against real PG), deploy artifacts (docker-compose.multi.yml,
  Caddyfile, .env.example.multi), v1.12.0 + v1.12.1 released.
- DONE: LIVE at https://feedecho.net — VPS 107.150.1.236 (/opt/feedecho,
  docker compose: postgres + feedecho + caddy, all healthy), DNS A record via
  Porkbun (id 577733457, TTL 600, direct — no Cloudflare proxy), Caddy TLS.
  Jason's account jcrabapple@fastmail.us registered (password:
  FEEDCHO_ADMIN_PASSWORD in Infisical, dev env).
- DONE (next-phase items 1-8, each with a Kimi K3 review closed): structured
  logging (logging_setup.py LogRecordFactory + RequestIdMiddleware +
  FEEDCHO_LOG_LEVEL); admin role (users.is_admin + auth.is_admin fresh-read +
  FEEDCHO_ADMIN_EMAIL bootstrap); admin dashboard (/admin users/stats +
  suspend/unsuspend/promote/demote with self-guards + last-admin guards);
  admin email settings (system_settings table + system SMTP with masking +
  validation); email verification (email_tokens, verify/resend, scheduler
  gate for unverified owners); password reset (forgot/reset with peek-consume,
  per-IP + per-user throttles, session_epoch invalidation); How To page
  (both modes); About page (hosted-only public disclosure). 501 sqlite +
  13 pg tests green.
- DONE: v1.13.0 released and LIVE at https://feedecho.net (all 8 next-phase
  items deployed; FEEDCHO_ADMIN_EMAIL bootstrapped Jason's account to admin).
  Local self-hosted service also on v1.13.0.
- DONE (issue #3, Kimi-reviewed): feed editing — POST /api/feeds/{id}/edit
  updates name/URL/poll interval in place (inline row editor mirroring the
  echo pattern). URL changes reset last_item_id (old cursor is meaningless
  against a new feed); same-URL edits preserve it. 404 on deleted/other-
  tenant feeds; name trimmed + required; poll interval clamped 1-1440.
  513 sqlite + 14 pg tests green. Kimi findings applied: cancelFeedEdit
  (cancelEdit hardcodes echo-row prefix), quote-escaping in escapeHTML,
  deleted_at re-check in UPDATEs, blank-name validation, pg test id capture.
- DONE (issues #4 + #5, two Kimi passes): history page timestamps and
  drip-status display. #4: all UI timestamps (history, dashboard, feeds,
  accounts, admin) render as <time class="local-time" datetime="...Z">
  converted to the viewer's timezone by formatLocalTimes() in app.js,
  with a UTC-labelled fallback when JS is off. New Jinja filters
  iso_utc/utc_text in app.py handle BOTH sqlite TEXT and psycopg datetime
  (the inline .replace(' ','T') approach 500s on PG — caught by the pg
  suite, now regression-tested). #5: 'queued' (drip-held) and 'pending'
  rows show Queued/Pending badges instead of red Failed on history AND
  dashboard. NULL-posted_at guards everywhere. 523 sqlite + 15 pg green.
- DONE (issue #6, Kimi-reviewed): dates respect the browser locale.
  formatLocalTimes() passes navigator.languages to the Intl formatters
  (previously no locales arg = UI language only, ignoring configured
  content languages). Dashboard trial banner moved from server-rendered
  "%b %d, %Y" to an ISO date in a local-time element; date-only values
  parsed as local midnight (no UTC off-by-one). app.js cache-buster
  bumped (v=17). Regression test asserts banner date == stored
  trial_ends_at. 523 sqlite + 15 pg green.
- DONE (issue #7, Kimi-reviewed): the Mastodon app registration no longer
  hardcodes website="https://feedecho.example.com", the dead link Mastodon
  rendered behind the "FeedEcho" application name on every post. Resolution
  is FEEDCHO_APP_WEBSITE -> FEEDCHO_BASE_URL -> settings.PROJECT_URL (repo);
  CALLBACK_URL derives from BASE_URL when unset instead of always falling
  back to the placeholder; BASE_URL is .strip()ed. oauth_apps gained website
  + redirect_uris (both dialects + migrations); cached credentials are reused
  only while BOTH still match config, since Mastodon cannot edit an existing
  registration and a drifted callback fails as a redirect mismatch. Legacy
  NULL rows re-register once. exchange_code passes allow_refresh=False so the
  token exchange stays pinned to the client the code was issued to. Known and
  documented: SELECT-then-register-then-upsert TOCTOU on concurrent connects
  to the same instance (pre-existing; loser retries). ALREADY-CONNECTED
  ACCOUNTS KEEP THE OLD LINK — the token is bound to the old registration, so
  the account must be reconnected. Local instance: drop-in
  ~/.config/systemd/user/feedecho.service.d/callback-url.conf sets
  FEEDCHO_CALLBACK_URL=https://feedecho.snakepit.us/oauth/callback and
  deliberately leaves APP_WEBSITE unset, so the public post link is the repo
  and not Jason's personal hostname. 540 sqlite + 16 pg green.
- NOT STARTED: Phase 4 (billing): Stripe card-gated trial, B2 backups,
  Cloudflare proxy decision. Private beta targeted months 3-4. Note: the
  hosted account's verification banner is active until system SMTP is
  configured in /admin and the link is clicked.
- DONE (issue #8, Kimi-reviewed, v1.14.0 released + deployed): the running
  version shows in the site footer, linked to the GitHub releases page.
  `_version.py` is the single source of truth for the Python side — `app.py`
  imports it for `FastAPI(version=)` and `pyproject.toml` declares
  `dynamic = ["version"]` with `[tool.hatch.version] path = "_version.py"`, so
  a bump rewrites 8 literals across 5 files (was 9 across 6) via
  `scripts/bump_version.py X.Y.Z`, which asserts a per-file count.
  `tests/test_version.py` pins every copy AND scans `git ls-files` for
  version-shaped strings (GHCR tag, `archive/refs/tags/vX`, nix `rev`/
  `version`) that drifted, so a partial bump fails CI instead of shipping a
  footer that lies. Editable-install path verified: `pip install -e ".[dev]"`
  on 3.12 resolves the dynamic version and both CI job commands pass.
  Visibility rule: single mode shows the version to any viewer past the auth
  gate (no token = operator); multi mode shows it to `is_admin` only, because
  `/register` is public and a tenant cannot upgrade the service.
  `AuthMiddleware` now records `request.state.authed`; the public paths
  short-circuit before session parsing, so /login, /register and /about never
  show a version even to an admin (deliberate, documented in `render()`).
  The footer also renders in single mode for the first time, only links pages
  the viewer can reach (/howto when authed, /about in multi), and is omitted
  entirely when neither applies. Version link carries an `.sr-only`
  "opens in a new tab" cue. style.css cache-buster v=13. 606 sqlite + 20 pg
  green, CI 3/3 on the tag. Verified in production as admin (footer shows
  v1.14.0) and anonymously (no version on /login or /about), local instance on
  1.14.0 across 7 pages, and rendered in Chrome at 1280px + 390px.
  KNOWN, NOT FIXED (pre-existing, wider than this change): the top nav links
  /howto and the app pages to anonymous viewers on /login, which bounce back
  to the login page.
- DONE (Kimi-reviewed, v1.15.0 released + deployed): that nav defect is fixed.
  `base.html` renders the app links only when `authed`; anonymous multi-mode
  viewers get Log in + Sign up (current page marked `active`), single mode's
  login page is brand + theme toggle, and the brand href points at /login for
  anonymous viewers instead of / (which redirected back). An authenticated GET
  of /login (both modes) or /register (multi) now 302s to / instead of
  rendering a dead form — POST untouched, and single mode redirects even with
  no token configured, matching what POST already did.
  To make `authed` correct on public pages, `AuthMiddleware` grew
  `_token_matches()` (single, pure compare, now runs before the exempt
  short-circuit) and `_session_user()` (multi, same suspension +
  session_epoch enforcement as the old inline code), and runs the session read
  best-effort on `_MULTI_PUBLIC_PAGES` = /login, /register, /about,
  /verify-email. Deliberately excluded: /oauth/callback (its handler
  authorizes on the signed OAuth state and documents user_id as unset),
  /logout, and /static + /healthz + /favicon.svg (must not pay for a DB read —
  pinned by a get_db call-counting test). /forgot-password and /reset-password
  stay anonymous on purpose (they are for people who cannot get in) and that is
  pinned too. Consequence: an admin now sees the version footer on /about,
  which closes the finding deferred in the v1.14.0 review.
  Kimi passed the gate with 3 LOW findings, all in the new tests' coverage, all
  fixed: funnel assertions were partly satisfied by the brand anchor;
  /verify-email had no coverage despite being the riskiest newly-identified
  path (verified it keys off `consume_token`, never `current_user_id`, now
  pinned by a cross-account test where a signed-in user clicks a token issued
  to someone else); the deliberate exclusions were unpinned.
  629 sqlite + 20 pg green (23 new in tests/test_nav_gating.py, 13 confirmed
  RED against the old behaviour), CI 3/3 on the tag. Verified in production
  anonymously (no app links on /login, /register, /about; no version) and as
  admin (full nav + Admin + email + version on / and /about; /login and
  /register both 302 to /), plus the local single-mode instance. Rendered in
  Chrome: single-mode login, multi-mode anonymous login, /about as tenant and
  as admin.
- DONE (Kimi-reviewed, UNRELEASED as of 2026-08-27): micro.blog destination.
  A fourth destination type 'microblog' posts feed items to Micro.blog via
  its Micropub API (form-encoded POST https://micro.blog/micropub,
  h=entry&content=&mp-destination=&photo=&mp-photo-alt=). Connect flow takes
  an app token (micro.blog/account/apps), calls q=config, and stores ONE ROW
  PER BLOG the token can post to (microblog_accounts: name/uid/token/user_id,
  UNIQUE(user_id, uid), upsert on reconnect refreshes name+token). Echoes
  target a specific blog; scheduler _send_microblog sends content plus the
  item's image URL as photo= when attach_image is set (Micro.blog fetches and
  hosts it — no blob upload), with AI alt text in mp-photo-alt when enabled;
  degrade-to-text-only on image failure, same as Mastodon/Bluesky. 401/403 →
  permanent gave_up; other errors bounded-retry; claim re-checked before the
  irreversible post. Seams touched: database DDL (BOTH dialects), app.py
  account routes + echo CRUD + 4 SQL CASE/JOIN sites + dashboard stats,
  templates (accounts, echoes incl. edit-row JS options, howto), app.js
  (testMicroblogAccount, editEcho microblog options, toggleEditDest) with
  cache-bust v=19, BLOCKING set + list_destinations. Tests: 34 new in
  tests/test_microblog.py (all network monkeypatched). Kimi gate: 1 MEDIUM
  (sender account fetch now scoped AND user_id = echo.user_id), 2 LOW (dead
  MAX_POST_LENGTH removed; auth classification now status-code-only so a 500
  body mentioning "unauthorized" stays retryable) — all fixed with new pinning
  tests. 663 sqlite + 20 pg green. KNOWN PARITY GAP (pre-existing, not this
  feature): _send_mastodon and _send_bluesky still fetch their account rows
  without AND user_id = ?; worth folding into a future cleanup.

## 4. Key Decisions (and why)

- Multi mode gated on FEEDCHO_MODE=multi + FEEDCHO_DATABASE_URL; validate_config
  hard-fails at startup without DATABASE_URL (or explicit
  FEEDCHO_ALLOW_SQLITE_FALLBACK=1) or a >=32-char FEEDCHO_SESSION_SECRET.
- settings table composite PK (user_id, key); single mode = user_id 1.
- AUTH_TOKEN is read from settings per REQUEST (an import-time copy once broke
  every single-mode test because infisical-env exported it into the shell).
- Deploy: dedicated compose network 172.28.0.0/16, TRUSTED_PROXIES scoped to it;
  _client_ip uses rightmost XFF (Caddy appends the real client IP, unspoofable
  through it); postgres never published to the host; image pinned to 1.12.1.
- Same repo, no fork: multi-mode features ship in the OSS tree; single mode
  renders no multi chrome.

## 5. Traps & Dead Ends

- sqlite Row allows row[0]; psycopg dict_row raises KeyError. Any positional
  row access is a latent PG bug — the TestAppOnPostgres pattern (full request
  against real PG) catches these; extend it when adding routes.
- scrypt hashes contain $N$r$p — never pass them through shell double-quoted
  strings (shell expands $1...); do hash+verify inside the container via
  docker exec -e / PYTHONPATH=/app.
- GH PAT lacks the workflow scope: HTTPS push of new workflow files is rejected.
  Push via SSH (git@github.com:jcrabapple/feedecho.git). GHCR package is public.
- infisical CLI: `secrets set KEY=value` (not positional); CLI login expires —
  `eval "$(infisical-login)"` refreshes it (infisical-env alone can't write).
- Compose env placeholders in the Caddyfile are evaluated from the CADDY
  container's env — env_file passthrough is required, not optional.
- Kimi review convention: verify every finding against real code; explicitly
  triage false positives in the commit message.
- New test files can be silently zeroed by gateway interruptions — verify
  collected test counts after any interrupted write (409 vs 403 mismatch
  caught test_ui_multi.py at 0 bytes once).

## 6. Relevant Files & Pointers

- docs/plans/2026-08-22-multi-tenant-refactor.md — the approved 14-task plan
- app.py — routes, render() (MULTI + trial context injection), AuthMiddleware
- auth.py — register/login/logout, _client_ip, throttling, _trial_end
- security.py — scrypt hashing, HMAC sessions (sign/read)
- notify.py — _echo_owner + per-owner retry/alert/SMTP settings
- scheduler.py — check_all_feeds Python due-filter (dialect-safe), dispatch
- database.py — dialect layer (qmark, dict_row, _add_column_if_missing)
- docker-compose.multi.yml / Caddyfile / .env.example.multi — hosted stack
- tests/test_pg_dialect.py — PG-gated suite incl. TestAppOnPostgres
- skills: feed-syndication-service (release flow + version inventory), kimi-k3-openrouter
- Infisical dev project af3b8a09-35ab-4acc-b0ea-c4ef2201eb29: FEEDCHO_ADMIN_PASSWORD,
  FEEDCHO_VPS_IP/USER, PORKBUN keys, OPENROUTER_API_KEY, GITHUB_TOKEN

## 7. Open Work (state + dependencies)

- Logging: ad-hoc logger calls only; no structured/request-id logging.
- Admin role: users table has no is_admin column yet; no admin routes.
- Email verification: SMTP plumbing exists per-user (email_sender.py) but
  signup sends nothing; no verification tokens/resend endpoints.
- Password reset: no token generation, reset routes, or email template.
- Docs pages: no How To page (feeds/accounts/echoes walkthrough) for either
  mode; no hosted About page (privacy + security disclosure). Both are new.
- Billing: no Stripe integration; trial banner is a placeholder.
- Backups: B2 not set up; pgdata volume unbacked.
- Cloudflare proxy vs direct DNS: undecided (plan default: direct for now).

## 8. Environment & Setup

- Repo /home/jason/projects/feedecho (branch master), venv .venv
- Tests: `.venv/bin/python -m pytest` (411), `-m multi`, `-m pg` (needs
  FEEDCHO_TEST_PG_URL; spin up `podman run -d --rm -p 55432:5432 -e
  POSTGRES_USER=feedecho -e POSTGRES_PASSWORD=feedecho -e POSTGRES_DB=feedecho
  docker.io/library/postgres:17-alpine` then wait for pg_isready)
- VPS: ssh root@107.150.1.236 (id_ed25519, key-only), stack at /opt/feedecho,
  `docker compose -f docker-compose.multi.yml up -d`, logs via docker logs feedecho
- Release flow per feed-syndication-service skill (version inventory sweep,
  tag, SSH push, gh release create, GHCR ~2min, VPS scp + compose up)
- Local self-hosted service: systemctl --user restart feedecho.service after pull

---

## Prompt for the Fresh Agent

FeedEcho v1.12.1 is released and live in hosted multi-tenant mode at
feedecho.net; the 14-task refactor plan is complete with 411 sqlite tests and
9 PG tests green. New scope approved by Jason for the next phase: structured
logging, an admin user role and admin dashboard, admin-configurable email
settings for verification and password-reset mail, email verification during
signup, and user password reset. Billing (Stripe), backups (B2), and the
Cloudflare proxy decision are separate Phase-4 items.

Before responding, read every file listed under "Relevant Files & Pointers" above.
Do not summarize, paraphrase, or claim you already have context — actually read each
file. Treat every claim in this handoff as context to verify against the code, not
facts to trust blindly. Then wait for my instructions before taking any action.
