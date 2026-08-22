# Multi-Tenant Refactor + VPS Deployment — Implementation Plan

> **For Hermes:** Execute task-by-task with TDD. Each task ends with the full suite green and a commit.
> Related design: `~/Documents/Obsidian Vault/Business/FeedEcho Hosted SaaS Plan.md` (Section 4.0).

**Goal:** Make FeedEcho run in two modes from one repo: `FEEDCHO_MODE=single` (today's self-hosted app, unchanged behavior, SQLite) and `FEEDCHO_MODE=multi` (hosted product: users, sessions, Postgres), then deploy multi mode to the VPS at 107.150.1.236 serving feedecho.net.

**Architecture:** Mode flag + user_id singleton trick. Every owned table gains `user_id`; single mode hardcodes 1. `get_db()` returns sqlite3 or psycopg connections behind the same row-mapping interface, with `?`→`%s` placeholder translation. Auth in multi mode is email/password + HMAC-signed session cookie (stdlib only — no argon2/itsdangerous deps). Raw SQL stays; date math already lives in Python.

**Tech Stack:** Python 3.12+, FastAPI, raw SQL (sqlite3 / psycopg3), stdlib `hashlib.scrypt` + `hmac`, Jinja2, APScheduler, Docker Compose (postgres:17-alpine + ghcr.io/jcrabapple/feedecho).

**Current state:** v1.11.1, 295 tests green, live single-tenant instance on feedecho.snakepit.us (port 8453). Env reads scattered: `database.py` (FEEDCHO_DB_PATH), `oauth.py` (FEEDCHO_CALLBACK_URL/FEEDCHO_STATE_SECRET/FEEDCHO_AUTH_TOKEN), `app.py` (FEEDCHO_AUTH_TOKEN).

---

## Task 1: settings.py + FEEDCHO_MODE flag

**Objective:** One settings module; scattered `os.environ` reads move into it. No behavior change.

**Files:**
- Create: `settings.py`
- Modify: `database.py:9`, `oauth.py:17-28`, `app.py:58`

**Step 1: Write failing tests**

`tests/test_settings.py`:
```python
def test_defaults_are_single_mode(monkeypatch):
    for key in list(os.environ):
        if key.startswith("FEEDCHO_"):
            monkeypatch.delenv(key)
    import importlib, settings
    importlib.reload(settings)
    assert settings.MODE == "single"
    assert settings.MULTI is False

def test_multi_mode_flag(monkeypatch):
    monkeypatch.setenv("FEEDCHO_MODE", "multi")
    import importlib, settings
    importlib.reload(settings)
    assert settings.MODE == "multi"
    assert settings.MULTI is True
```

**Step 2: Run** — `pytest tests/test_settings.py -v` — expected: FAIL (no module).

**Step 3: Implement**

`settings.py`:
```python
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

MODE = os.environ.get("FEEDCHO_MODE", "single")
if MODE not in ("single", "multi"):
    raise ValueError(f"FEEDCHO_MODE must be 'single' or 'multi', got {MODE!r}")
MULTI = MODE == "multi"

DB_PATH = Path(os.environ.get("FEEDCHO_DB_PATH", BASE_DIR / "feedecho.db"))
DATABASE_URL = os.environ.get("FEEDCHO_DATABASE_URL", "")
AUTH_TOKEN = os.environ.get("FEEDCHO_AUTH_TOKEN", "")
CALLBACK_URL = os.environ.get("FEEDCHO_CALLBACK_URL", "")
STATE_SECRET = os.environ.get("FEEDCHO_STATE_SECRET", "")
BASE_URL = os.environ.get("FEEDCHO_BASE_URL", "")
```

Then in `database.py` replace line 9 with `from settings import DB_PATH`. In `oauth.py` replace the three `os.environ.get` blocks with `from settings import CALLBACK_URL, AUTH_TOKEN, STATE_SECRET`. In `app.py` replace `_AUTH_TOKEN = os.environ.get("FEEDCHO_AUTH_TOKEN")` with `from settings import AUTH_TOKEN as _AUTH_TOKEN`.

**Step 4: Run** — full suite `pytest -q` — expected: 295 + 2 new pass.

**Step 5: Commit** — `git add -A && git commit -m "Add settings.py with FEEDCHO_MODE flag"`

---

## Task 2: users table + user_id columns (SQLite), singleton migration

**Objective:** Schema gains `users` and `user_id` on owned tables; existing single-tenant DBs migrate with user_id=1 and a singleton user row.

**Files:**
- Modify: `database.py` (init_db)
- Create: `tests/test_users_schema.py`

**Step 1: Write failing tests**

```python
def test_users_table_exists(temp_db):  # fixture like tests/test_database.py
    with get_db() as db:
        cols = {r["name"] for r in db.execute("PRAGMA table_info(users)")}
    assert {"id", "email", "password_hash", "plan", "created_at"} <= cols

def test_user_id_columns(temp_db):
    with get_db() as db:
        for table in ("accounts", "feeds", "echoes", "email_accounts", "bluesky_accounts", "settings"):
            cols = {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}
            assert "user_id" in cols, table

def test_singleton_user_created(temp_db):
    with get_db() as db:
        row = db.execute("SELECT id, email FROM users WHERE id = 1").fetchone()
    assert row is not None

def test_existing_rows_backfilled_to_user_1(temp_db):
    with get_db() as db:
        db.execute("INSERT INTO accounts (name, instance, access_token) VALUES (?,?,?)", ("T", "https://x", "tok"))
    # simulate pre-migration state? Instead: init_db twice and assert user_id=1
    with get_db() as db:
        row = db.execute("SELECT user_id FROM accounts").fetchone()
        assert row["user_id"] == 1
```

**Step 2: Run** — FAIL (no users table).

**Step 3: Implement** in `init_db()`:

```sql
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL DEFAULT '',
    plan TEXT NOT NULL DEFAULT 'trial',
    trial_ends_at TIMESTAMP,
    email_verified INTEGER NOT NULL DEFAULT 0,
    suspended INTEGER NOT NULL DEFAULT 0,
    stripe_customer_id TEXT DEFAULT '',
    stripe_subscription_id TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
```

- `user_id` columns on accounts/feeds/echoes/email_accounts/bluesky_accounts: `_add_column_if_missing(db, table, "user_id", "INTEGER NOT NULL DEFAULT 1")`. For `settings`: drop and recreate as `(user_id, key)` composite PK — but only if the table has no user_id column yet (migration: `CREATE TABLE settings_new (...); INSERT INTO settings_new (user_id, key, value) SELECT 1, key, value FROM settings; DROP TABLE settings; ALTER TABLE settings_new RENAME TO settings`). Guard with the same `_column_names` check.
- Singleton: `INSERT OR IGNORE INTO users (id, email) VALUES (1, 'local')` (single-tenant placeholder).
- Backfill: existing rows get DEFAULT 1 automatically via ADD COLUMN DEFAULT.

**Step 4: Run** — full suite — all 295 + new pass (existing INSERTs unaffected because new columns default).

**Step 5: Commit** — `git commit -m "Add users table and user_id columns with singleton migration"`

---

## Task 3: DB dialect layer (placeholder translation + row mapping + PG schema)

**Objective:** `get_db()` yields a connection that behaves the same on sqlite3 and psycopg3; queries written with `?` work on both.

**Files:**
- Modify: `database.py`
- Modify: `pyproject.toml` (add `postgres = ["psycopg[binary]>=3.2"]` extra)
- Create: `tests/test_dialect.py`

**Step 1: Tests** (pure unit, no live postgres needed for most):

```python
def test_qmark_translation():
    assert qmark("SELECT * FROM feeds WHERE id = ? AND deleted_at IS NULL") == \
        "SELECT * FROM feeds WHERE id = %s AND deleted_at IS NULL"

def test_placeholder_scan_falls_back_to_passthrough():
    # qmark() must return sql unchanged when not postgres mode
    monkeypatch.setattr(database, "DIALECT", "sqlite")
    assert qmark("WHERE id = ?") == "WHERE id = ?"
```

**Step 2: Implement**

- `DIALECT` computed in `database.py`: `"postgres"` when `settings.MULTI and settings.DATABASE_URL` else `"sqlite"`.
- `def qmark(sql: str) -> str`: if DIALECT is postgres, replace `?` with `%s` using a regex that skips `?` inside string literals (`re.sub(r"\?", "%s", sql)` is acceptable — audit queries for `?` inside literals; none today; add a comment + a test that greps the codebase: `search` for `\?` inside SQL strings with apostrophes is overkill — instead add a lint-style test iterating `SQL_LITERAL_RE`).
- `get_db()`: when postgres — `psycopg.connect(DATABASE_URL, row_factory=dict_row)` wrapped in a tiny adapter class `PgAdapter` exposing `.execute(sql, params)` that calls `conn.execute(qmark(sql), params)`, `.commit()`, `.rollback()`, `.close()`, and `__enter__/__exit__`; sqlite path unchanged. Callers use `with get_db() as db: db.execute(...)` — both support it.
- `_column_names(db, table)`: postgres branch via `information_schema.columns`.
- `init_db()`: route to `init_db_sqlite()` (current body) or `init_db_postgres()` — a parallel schema function with the same tables; differences: `BIGSERIAL PRIMARY KEY` instead of `INTEGER PRIMARY KEY AUTOINCREMENT`, no PRAGMA lines, `NOW() - INTERVAL '1 day'` in the oauth cleanup, `CREATE TABLE IF NOT EXISTS` works on PG, `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` for the idempotent migrations (PG supports it; sqlite does not — so `_add_column_if_missing` branches).
- psycopg import must be lazy (only in postgres mode) so self-hosters never need it. `try: import psycopg` inside the postgres branch; raise a clear error if missing.

**Step 3: Run** — full suite green on sqlite (postgres path not exercised locally; CI adds it in Task 12).

**Step 4: Commit.**

---

## Task 4: Wire DATABASE_URL + mode into startup

**Objective:** `lifespan` uses the right backend per mode; `init_db()` runs against Postgres in multi mode.

**Files:** Modify `app.py` (lifespan), `database.py`.

- `init_db()` becomes: `init_db_postgres() if DIALECT == "postgres" else init_db_sqlite()`.
- `lifespan` unchanged otherwise.
- Verify locally: `FEEDCHO_MODE=multi` with no DATABASE_URL still runs on sqlite (DIALECT falls back to sqlite when URL empty) — tests cover this: `test_multi_mode_without_url_uses_sqlite`.

---

## Task 5: Auth primitives — scrypt + signed session cookie (stdlib)

**Objective:** Password hashing and stateless signed cookies with zero new dependencies.

**Files:**
- Create: `authlib.py` (name TBD: `security.py`)
- Create: `tests/test_security.py` (extend existing security tests file)

**Functions:**
```python
def hash_password(password: str) -> str            # hashlib.scrypt, salt=secrets.token_bytes(16), format "scrypt$n$r$p$salt_b64$hash_b64"
def verify_password(password: str, stored: str) -> bool
def sign_session(user_id: int, email: str) -> str # hmac-sha256 over "user_id|email|expiry" with SECRET_KEY, b64
def read_session(token: str) -> dict | None       # verifies signature + expiry, returns claims
```
- `SECRET_KEY` = `settings.STATE_SECRET` or `FEEDCHO_SESSION_SECRET` env, falling back to a generated key persisted in `~/.feedecho_session_secret` only for dev convenience (in production multi mode, REQUIRE the env var — raise on startup if unset).
- Tests: roundtrip, wrong password, tampered signature, expired token.

---

## Task 6: Multi-mode auth — register/login/logout + middleware

**Objective:** In multi mode, email/password accounts replace shared-secret auth. Single mode keeps today's behavior bit-for-bit.

**Files:**
- Create: `auth.py` (routes: GET/POST /register, GET/POST /login (multi-mode branch), POST /logout)
- Modify: `app.py` (middleware split), `templates/login.html` (+ new `templates/register.html`), `static/css/style.css`

**Behavior:**
- Single mode: `AuthMiddleware` unchanged (cookie = shared token).
- Multi mode: new `SessionMiddleware` — exempt paths `/healthz /static /favicon.svg /oauth/callback /register /login`; reads `feedecho_session` cookie → `read_session` → stash `request.state.user_id`; browser requests without a valid session redirect to /login, API requests get 401.
- `current_user_id(request) -> int` helper: multi mode returns `request.state.user_id` (middleware guarantees it), single mode returns 1. All route handlers will use this (Task 7).
- Register: email + password + confirm, scrypt hash, plan='trial', trial_ends_at=now+14d, email_verified=0 for now (verification mail lands with Resend in a later task; record the decision in code comment).
- Login: sets signed cookie (httponly, samesite=lax, secure when https).
- **Rate limiting note:** login attempts get a tiny in-memory throttle (Task/Phase 4 does the real work; here a 5-attempts-per-5-min per-IP dict — boring and cheap).

**Step: tests** — `tests/test_auth_multi.py`: register→login→protected page, wrong password rejected, tampered cookie rejected, register duplicate email, single-mode suite untouched.

---

## Task 7: User scoping through app routes

**Objective:** Every page/API query scoped by `current_user_id()` in multi mode; single mode passes user_id=1.

**Files:** Modify `app.py` (all handlers), `notify.py` (settings getters gain user_id param defaulting to 1), `scheduler.py` unchanged (system-level jobs operate across users; per-user scoping is not needed in the pipeline).

**Mechanics:**
- `SELECT ... WHERE user_id = ?` added to every query against accounts/feeds/echoes/email_accounts/bluesky_accounts/settings.
- POST handlers: insert explicit `user_id` from `current_user_id(request)`.
- `get_setting_int`/`get_setting` in notify.py: add `user_id: int = 1` param; scheduler call sites pass `echo/feed user_id` or default (settings remain per-user only where they're user-facing; retry/backoff settings stay GLOBAL in single mode and are per-user in multi — decision: keep retry settings global for now, note in code).
- OAuth callback (Task 8) is the one cross-cutting piece; land in the same task if the diff stays reviewable, else split.

**Step: tests** — `tests/test_scoping.py`: user A cannot see/modify user B's feeds/echoes via API (seed two users, assert 404/empty + FK-consistency), single-mode tests untouched.

---

## Task 8: Mastodon OAuth binding in multi mode

**Objective:** OAuth state rows bind to the right user.

**Files:** Modify `oauth.py`, `app.py` (oauth routes).

- `session_binding` = the raw session token (single: shared token value; multi: signed session cookie value). `/oauth/connect` stores it; `/oauth/callback` looks it up, derives user via `read_session` in multi mode, `1` in single, and writes the resulting account row with that user_id.
- Test: multi-mode connect→callback (mocked Mastodon) lands account under the session's user.

---

## Task 9: Scheduler + pipeline audit

**Objective:** Prove the posting pipeline is untouched and mode-agnostic.

- Grep `scheduler.py` for any bare user-facing reads that need scoping (there should be none — it processes all users' feeds deliberately).
- Add regression tests: multi-mode DB with two users' feeds → `check_all_feeds` (mocked post_status) posts both correctly, drip/digest flushes work with `get_db()` in both dialects (sqlite locally).
- No production code changes expected; tests + maybe `feed_name` threading already present.

---

## Task 10: UI — mode flag in templates, register/login/logout chrome

**Objective:** Hosted users see auth chrome; self-hosters see nothing new.

**Files:** Modify `templates/base.html`, `render()` in `app.py` (inject `MULTI` + `current_user_email`), `templates/login.html`, create `templates/register.html`.

- `render()` gains `MULTI=settings.MULTI` always.
- base.html: `{% if MULTI %}` nav shows email + Logout button; dashboard shows a trial/billing banner (placeholder for Stripe task).
- Bump `app.js?v=15` → v=16 only if JS changes (likely not; skip).

---

## Task 11: Test matrix + CI

**Objective:** Single-mode suite (295) + multi-mode suite (new) run in CI; local dev runs both on sqlite.

**Files:** Modify `.github/workflows/*` (find existing), `pytest.ini`/`pyproject.toml` markers.

- Mark multi-mode tests `@pytest.mark.multi`; CI job runs `pytest -m multi` with `FEEDCHO_MODE=multi` env and a temp sqlite DB, plus the default suite.
- Document: Postgres-specific tests (Task 12) are CI-gated.

---

## Task 12: Postgres CI matrix + dialect tests

**Objective:** The PG path is actually exercised.

**Files:** `.github/workflows/tests.yml` (add service container `postgres:17-alpine`), `tests/test_pg_dialect.py`.

- Tests: `init_db()` against real PG, `get_db()` insert/select roundtrip with `?` placeholders, `_add_column_if_missing` on PG, migration idempotency (run init twice).
- Gated with `@pytest.mark.pg`; skipped locally when `FEEDCHO_TEST_PG_URL` unset.

---

## Task 13: Deploy artifacts — compose, Caddy, env

**Objective:** One command brings the hosted stack up on the VPS.

**Files:** Create `docker-compose.multi.yml`, `Caddyfile`, `.env.example.multi`; modify `Dockerfile` (install `.[postgres]`), `README.md` (hosted mode section).

- compose: `postgres:17-alpine` (volume `pgdata`, healthcheck `pg_isready`), `feedecho` (ghcr image, env from `.env`, depends_on healthy, `FEEDCHO_MODE=multi`, `FEEDCHO_DATABASE_URL=postgresql://feedecho:${POSTGRES_PASSWORD}@postgres:5432/feedecho`, `FEEDCHO_SESSION_SECRET`, `FEEDCHO_BASE_URL=https://feedecho.net`), `caddy:2` (volumes Caddyfile, ports 80/443, `FEEDCHO_AUTH_TOKEN` no longer needed in multi).
- Dockerfile: `RUN pip install ".[postgres]"` — keep it unconditional (image is shared; psycopg binary wheel is small).
- Local validation: `docker compose -f docker-compose.multi.yml config` parses; cannot run locally without image — note it.

---

## Task 14: Release v1.12.0 + deploy + DNS

**Objective:** Live multi-tenant app on the VPS at feedecho.net.

- Bump version across inventory (pyproject, app.py, flake.nix, nix/package.nix, nix/README.md) per `feed-syndication-service` skill; run full suite; commit; tag `v1.12.0`; push; GHCR builds on tag.
- **Kimi K3 code review before release** (Jason's standing gate for this repo) on the multi-tenant diff.
- VPS: `scp` compose + Caddyfile + .env (secrets from Infisical into `.env`, chmod 600, never committed); `docker compose up -d`; verify `curl https://feedecho.net/healthz` and register the first real account.
- DNS: A record `feedecho.net → 107.150.1.236` (Porkbun API `dns/create`), wait for propagation. Decide Cloudflare-proxy vs direct at this step (plan default: direct first, Cloudflare proxy later when WAF matters).
- Verify backups story is next (Phase 4).

---

## Pitfalls

- **`?` inside SQL string literals**: `qmark` regex replacement is naive; audit for literals containing `?` (none today, but the audit test in Task 3 must fail loudly if one appears).
- **psycopg row factory**: must be `dict_row` so `row["col"]` works identically to sqlite's Row.
- **`PRAGMA foreign_keys=ON`** is sqlite-only; PG enforces FKs by default — don't emit PRAGMA on PG.
- **`ALTER TABLE ADD COLUMN IF NOT EXISTS`** works on PG but NOT sqlite; `_add_column_if_missing` must branch on dialect (sqlite: check via table_info; PG: use IF NOT EXISTS).
- **settings composite PK migration**: recreate table; do it in a transaction; backfill `SELECT 1, key, value`.
- **Auth middleware order**: existing `AuthMiddleware` must remain the single-mode path; do not wrap both (choose middleware at startup based on MODE).
- **Existing 295 tests**: any new global state (settings import) must not break auth-proof fixtures; keep `FEEDCHO_AUTH_TOKEN` handling identical in single mode.
- **Never commit `.env`** for the VPS; ship `.env.example.multi` with placeholder values.
- **Do not put the VPS IP or feedecho.net in public README beyond what's already public** (Jason's standing preference: live infra hostnames stay out of public docs except the product domain).

## Verification (end-to-end)

- Full suite (295 + new) green in single and multi modes locally.
- On VPS: `curl -s http://localhost:80/healthz` inside caddy or `curl https://feedecho.net/healthz` → `{"status":"ok"}`; register account; create feed + echo; item posts to Mastodon (test account); disable account → posting stops (later Phase 4).
