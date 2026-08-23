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
  FEEDCHO_ADMIN_PASSWORD in Infisical, dev env). 411 tests green (local sqlite
  suite), 9/9 pg green.
- NOT STARTED (new scope, Jason 2026-08-22): structured logging, admin user
  role (is_admin), admin dashboard, admin email settings (verification +
  password-reset emails), email verification in signup flow, user password
  reset. Docs: basic How To page (add feeds, add accounts, create echoes)
  for BOTH self-hosted and hosted modes; hosted-only About landing page
  disclosing privacy + security practices. Phase 4 (billing): Stripe
  card-gated trial, B2 backups, Cloudflare proxy decision. Private beta
  targeted months 3-4.

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
