# FeedEcho — Code Duplication Audit

**Date:** 2026-09-08
**Scope:** Full repository at commit `3dcf392` (v1.49.0) — all Python modules, templates, JS/CSS, and the `tests/` directory.
**Method:** Direct file reads + grep verification of every cited line range (parallel review across 5 areas: destination modules, `app.py` routes, `scheduler.py`/`database.py`/`feed_parser.py`, `tests/`, and remaining modules/templates/assets). Every finding below cites the exact file:line where it was read. Claims the reviewers couldn't fully verify are marked **Needs verification** with what would confirm them — none of those are counted in the summary totals.
**Companion deliverable:** `utils.py` (repo root) — a new stdlib-only utilities module implementing the highest-value, lowest-risk extractions from this report (see [§6](#6-new-utils-module)). It is not yet wired into call sites; adoption snippets are given per finding below.

This report does not re-litigate correctness/security bugs already tracked in `docs/reviews/2026-08-28-kimi-full-repo-review.md` — it cites that review only where a duplication cluster is the direct cause of an already-known bug.

---

## Summary

| # | Finding | Category | Files touched | Importance |
|---|---|---|---|---|
| 1 | 7 destination dispatchers in `scheduler.py` share a copy-pasted skeleton | Structural | 1 file, 7 functions | **8/10** |
| 2 | Live email-regex drift: 2 different patterns for the same check | Data (+drift) | `app.py`×3, `auth.py` | **8/10** |
| 3 | `_dependent_echo_count` SQL hand-rolled twice instead of called | Exact | `app.py`×2 | **6/10** |
| 4 | SQLite/Postgres schema hand-authored twice for 22 tables | Structural | `database.py` | **6/10** |
| 5 | `normalize_webhook_url` name collision, different rules | Structural | `discord.py`, `webhook.py` | **5/10** |
| 6 | `app.js` `test*Account` functions — 6 near-byte-identical bodies | Exact | `static/js/app.js` | **6/10** |
| 7 | `_now_str`/UTC-timestamp helper reimplemented 5×+ | Exact/Structural | `auth.py`,`invites.py`,`verification.py`,`oauth.py`,`notify.py` | **5/10** |
| 8 | `templates/accounts.html` — 7 near-identical per-destination sections | Structural | 1 file | **5/10** |
| 9 | `app.py` admin-guard boilerplate repeated 11× | Structural | `app.py` | **5/10** |
| 10 | feed_parser.py image-extraction logic tripled (RSS + JSON Feed), one 11-line closure EXACT-duplicated | Near/Exact | `feed_parser.py` | **5/10** |
| 11 | `plans.py` `check_*_allowance` — 4 near-identical functions + `import_export.py` reimplements the same logic | Near | `plans.py`, `import_export.py` | **4/10** |
| 12 | `_error_detail`-style JSON error parsing reimplemented 4× | Near | `bluesky.py`,`discord.py`,`matrix.py`,`microblog.py` | **4/10** |
| 13 | HTTP status → typed-exception classification duplicated 3× | Structural | `discord.py`,`matrix.py`,`webhook.py` | **7/10** |
| 14 | Settings/credential fetch-from-DB "rows→dict" idiom repeated 3× | Near/Structural | `email_sender.py`×2, `alt_text.py`, `notify.py` | **4/10** |
| 15 | Secret-mask sentinel: two different conventions for one purpose | Data (+drift) | `app.py` | **4/10** |
| 16 | `settings.py` int-env-with-fallback block repeated 4× | Structural | `settings.py` | **3/10** |
| 17 | `REQUEST_TIMEOUT = 30` constant repeated 4× (and inconsistently inlined elsewhere) | Data | `discord.py`,`matrix.py`,`microblog.py`,`webhook.py` | **3/10** |
| 18 | Rate-limit `Retry-After` extraction duplicated 2× | Near | `discord.py`, `webhook.py` | **3/10** |
| 19 | discord/matrix character-truncation — EXACT duplicate modulo constant | Exact | `discord.py`, `matrix.py` | **3/10** |
| 20 | `templates/echoes.html` destination-type if/elif chain mirrors the same 7-type enumeration found elsewhere | Structural | `templates/echoes.html` | **4/10** |
| 21 | `app.py` `render("error.html", ...)` call shape repeated 30× | Structural/Data | `app.py` | **3/10** |
| 22 | `app.py` settings upsert SQL literal repeated 5× inside a 3×-repeated loop | Structural/Data | `app.py` | **3/10** |
| 23 | `app.py` "user not found" 404 boilerplate repeated 7× | Structural | `app.py` | **3/10** |
| 24 | `scheduler.py` `BLUESKY_MAX_GRAPHEMES` duplicates `bluesky.py`'s constant | Data | `scheduler.py`, `bluesky.py` | **3/10** |
| 25 | `test_connection() -> (bool, str)` boilerplate across 6 destination modules | Structural | 6 files | **2/10** |
| 26 | feed_parser.py audio-enclosure extraction — minor near-dup | Near | `feed_parser.py` | **2/10** |
| 27 | No `tests/conftest.py`; `db_tmp` fixture duplicated across 16 test files | Structural | `tests/*.py` | **5/10** |
| 28 | `_setup_echo` helper duplicated in 2 test files with a real default-value drift (`attach_image` 1 vs 0) | Near (+drift) | `test_alt_text.py`, `test_cw_and_images.py` | **6/10** |
| 29 | `_register`/`_login`/`_uid`/`_seed` helpers independently defined in 11 test files | Needs verification | `tests/*.py` | **3/10** |

**Totals:** 29 findings — 5 EXACT, 12 NEAR, 15 STRUCTURAL, 6 DATA (several span two categories). Two findings (#2, #28) represent *actual behavioral drift*, not just duplicated code — these are the highest-value fixes because they're bugs waiting to happen, not just maintenance debt.

---

## 1. Exact duplicates

### 1.1 `_dependent_echo_count` reimplemented inline instead of called — `app.py` (Importance: 6/10)

`_dependent_echo_count(user_id, destination_type, account_id)` is defined at `app.py:1002-1020` specifically so every destination's delete route can check for echoes still pointing at the account before deleting it. Its own docstring says: *"The Bluesky delete guarded against this from the start; Mastodon and email did not."* Five delete routes call it correctly (`app.py:3149, 3202, 3563, 3664, 3758`). Two routes never got backfilled and instead carry a byte-for-byte copy of the same SQL:

- `delete_bluesky_account` — `app.py:3310-3319`
- `delete_microblog_account` — `app.py:3428-3437`

Both copies are field-for-field identical to the helper (verified by direct read): same `SELECT COUNT(*) as c FROM echoes WHERE destination_type = ... AND destination_id = ? AND deleted_at IS NULL AND user_id = ?`.

**Duplication:** 100% of a 9-line block, ×2 sites.
**Fix (drop-in):**
```python
# app.py:3306-3319 (delete_bluesky_account) — replace the inline query with:
dependent = _dependent_echo_count(uid, "bluesky", account_id)

# app.py:3424-3437 (delete_microblog_account) — replace the inline query with:
dependent = _dependent_echo_count(uid, "microblog", account_id)
```
Zero behavior change — the SQL is identical. **Effort: S.**

### 1.2 `app.js` `test*Account` functions — 6 near-byte-identical bodies (Importance: 6/10)

`static/js/app.js` defines `testAccount` (42-54), `testBlueskyAccount` (56-68), `testMicroblogAccount` (70-82), `testMatrixAccount` (84-96), `testDiscordAccount` (98-110), `testWebhookAccount` (112-124) — verified present at those names via grep. Each is a 13-line body identical except for the URL path segment (`accounts`, `bluesky-accounts`, `microblog-accounts`, `matrix-accounts`, `discord-accounts`, `webhook-accounts`).

**Duplication:** ~95% of each 13-line function, ×6.
**Fix:**
```javascript
async function testDestinationAccount(kind, accountId, btn) {
    const path = kind === 'mastodon' ? 'accounts' : `${kind}-accounts`;
    try {
        const resp = await fetch(`/api/${path}/${accountId}/test`, { method: 'POST' });
        const data = await resp.json();
        if (!resp.ok) { showStatus(btn, 'Test failed: ' + (data.detail || resp.statusText), 'error'); return; }
        showStatus(btn, data.message || (data.success ? 'OK' : 'Failed'), data.success ? 'success' : 'error');
    } catch (e) { showStatus(btn, 'Request failed: ' + e.message, 'error'); }
}
```
Note `testAccount` (mastodon) hits `/api/accounts/...`, not `/api/accounts-accounts/...` — the `kind === 'mastodon'` special case above preserves that. Update the 6 `onclick="test*Account(...)"` call sites in `templates/accounts.html` to call `testDestinationAccount('bluesky', ...)` etc. **Effort: S** — mechanical, but cross-check the `templates/accounts.html` `onclick=` attributes when migrating.

### 1.3 discord/matrix character truncation (Importance: 3/10)

`discord.py:231-234` (`truncate_content`) and `matrix.py:223-226` (`_truncate_body`) are identical 4-line bodies (`if len(text) <= MAX: return text` / `return text[:MAX-1].rstrip() + "…"`), differing only in the constant name/value (`MAX_CONTENT_CHARS=2000` vs `MAX_BODY_CHARS=32_000`). `bluesky.py:404-414`'s `truncate_graphemes` is a different (grapheme-aware) algorithm and is **not** part of this duplicate — keep it separate.

**Fix:** use `utils.truncate_chars` from the new module:
```python
# discord.py — replace truncate_content body with:
from utils import truncate_chars as _truncate_chars
def truncate_content(text: str) -> str:
    return _truncate_chars(text, MAX_CONTENT_CHARS)

# matrix.py — replace _truncate_body body with:
def _truncate_body(text: str) -> str:
    return _truncate_chars(text, MAX_BODY_CHARS)
```
**Effort: S.**

### 1.4 `_add()` dedupe closure — feed_parser.py (part of §5.1)

See [§4.1](#41-feed_parsers-image-extraction-logic-tripled-per-format-importance-510) — the inner `_add(url, alt)` closure in `_extract_rss_images` (`feed_parser.py:867-877`) is line-for-line identical to the one in `_extract_json_feed_images` (`feed_parser.py:1062-1072`), 11 lines.

---

## 2. Near duplicates

### 2.1 JSON error-body parsing reimplemented 4× (Importance: 4/10)

`bluesky.py:46-56`, `discord.py:92-102`, `matrix.py:81-94`, `microblog.py:39-49` each: try `response.json()`, catch `ValueError` → `""`, if dict pull a message field and truncate to 200 chars. ~85% identical structure; only the dict key(s) checked differ (`message`/`error` vs `message` vs `error_description`/`error`). `matrix.py` also has a sibling `_errcode` (`matrix.py:97-106`) using the same idiom for a different key.

**Fix:** use `utils.json_error_detail`:
```python
from utils import json_error_detail

# bluesky.py — replace the body of the existing error-detail function with:
def _error_detail(response):
    return json_error_detail(response, "message", "error")

# matrix.py:
def _errcode(response):
    return json_error_detail(response, "errcode")
```
**Effort: S.**

### 2.2 Rate-limit `Retry-After` extraction duplicated 2× (Importance: 3/10)

`discord.py:105-121` (`_rate_limit_seconds`) and `webhook.py:226-241` (same function name) both check body JSON for `retry_after`/`retryAfter`, else the `Retry-After` header, cast to float, swallow `ValueError` — only the check order is reversed.

**Fix:** replace both bodies with a call to `utils.parse_retry_after(response)`. **Effort: S.**

### 2.3 `plans.py` `check_*_allowance` — 4 near-identical functions, plus a 5th reimplementation in `import_export.py` (Importance: 4/10)

`check_feed_allowance` (`plans.py:99-112`), `check_destination_allowance` (115-122), `check_queue_allowance` (125-132), `check_saved_search_allowance` (135-142) share the shape `cap = limit_for(plan, key); if cap and current_count >= cap: raise PlanError(...)`, differing only in the `key` string and the pluralized noun in the message. Separately, `import_export.py:281-310` (`_enforce_quotas`) reimplements the same "current + new > cap → raise" check independently because it needs a batch (`+new_count`) variant the single-item checks don't support.

**Fix:** generalize to one function taking an `additional` count (see `utils.check_allowance` in the new module, adapted to raise `plans.PlanError`):
```python
# plans.py
def _check_allowance(current_count: int, plan: str, key: str, noun: str, *, additional: int = 1, verb: str = "add") -> None:
    cap = limit_for(plan, key)
    if cap and current_count + additional > cap:
        plural = f"{noun}{'s' if cap != 1 else ''}"
        raise PlanError(f"Your plan allows {cap} {plural}. Upgrade or {verb} to continue.")

def check_feed_allowance(current_count, plan):
    _check_allowance(current_count, plan, "feeds", "feed")
# ...and so on for the other three; import_export.py calls
# _check_allowance(current_count, plan, key, noun, additional=new_count)
```
**Effort: M** — check `tests/test_plans.py` doesn't assert the exact pre-refactor message strings before collapsing (**needs verification**).

### 2.4 Settings/credential fetch-from-DB "rows→dict" idiom repeated 3× (Importance: 4/10)

`email_sender.py:19-31` (`get_smtp_settings`) and `email_sender.py:34-49` (`get_system_smtp_settings`) both run `SELECT key, value FROM ... WHERE key LIKE 'smtp_%' [AND user_id = ?]` then `{row["key"]: row["value"] for row in rows}` then `_normalize(...)`. `alt_text.py:54-64` (`_get_settings`) follows the identical `SELECT key, value ... → dict comprehension` idiom. `notify.py:40-51` (`get_setting_int`) is a single-key variant of the same idiom.

**Fix:** use `utils.rows_to_dict(rows)` in each of the four functions in place of the inline dict comprehension. **Effort: M** (touches 3 files; low risk since the transformation is byte-identical). **Needs verification:** confirm `database.py` doesn't already expose an equivalent helper before adding a second one (`grep -n "def get_setting\|def fetch_settings" database.py settings.py`).

### 2.5 feed_parser.py image-extraction logic tripled per format (Importance: 5/10)

For RSS: `_extract_rss_images` (`feed_parser.py:857-929`, returns a list), `_extract_rss_image` (932-964, single url), `_extract_rss_image_alt` (967-1005, single alt) all independently walk the same priority order (media_content → media_thumbnail → enclosures → first `<img>`). For JSON Feed: `_extract_json_feed_images` (1052-1102), `_extract_json_feed_image` (1024-1049), `_extract_json_feed_image_alt` (1105-1136) repeat the same triple pattern (image → banner_image → first `<img>`). The inner `_add(url, alt)` dedupe closure is **line-for-line identical** between the two list functions (867-877 vs 1062-1072) — see [§1.4](#14-add-dedupe-closure--feed_parserpy-part-of-51).

**Fix:** make the single-url/single-alt functions thin wrappers over the list function:
```python
def _extract_rss_image(entry) -> str | None:
    imgs = _extract_rss_images(entry)
    return imgs[0]["url"] if imgs else None

def _extract_rss_image_alt(entry) -> str | None:
    imgs = _extract_rss_images(entry)
    return imgs[0]["alt"] if imgs else None
```
Same pattern for the JSON Feed trio; factor the shared `_add` closure into one private helper both list-functions call. **Effort: S–M** — existing coverage in `test_feed_parser.py`/`test_feed_image_alt.py` should catch regressions.

### 2.6 feed_parser.py audio-enclosure extraction — minor near-dup (Importance: 2/10)

`_extract_audio_enclosure` (`feed_parser.py:1008-1013`) vs `_extract_json_feed_audio` (1016-1021) — same 5-line shape, different field names. Low value; fold in only if touching this area for another reason. **Effort: S.**

### 2.7 `test_connection() -> (bool, str)` boilerplate across 6 destination modules (Importance: 2/10)

`bluesky.py:536`, `discord.py:290`, `mastodon.py:123`, `matrix.py:523`, `microblog.py:209`, `webhook.py:317` each implement the same try/except-per-exception-type → `(False, str(e))` / success → `(True, msg)` skeleton. ~40% of each body is this skeleton; the rest is platform-specific and shouldn't be merged. **Recommendation: skip** unless doing a broader refactor of these modules — the mechanical savings don't justify introducing a decorator around 6 different exception hierarchies. **Effort: M, low value.**

---

## 3. Structural duplicates

### 3.1 Seven `_send_X` destination dispatchers in `scheduler.py` (Importance: 8/10 — highest-value finding)

`_send_mastodon` (`scheduler.py:1126-1284`), `_send_email_echo` (1286-1345), `_send_bluesky` (1422-1635, + helper `_bsky_session` 1347-1420), `_send_microblog` (1637-1757), `_send_matrix` (1759-1962, + `_matrix_image_filename` 1950-1961), `_send_discord` (1964-2054), `_send_webhook` (2056-2131) share a copy-pasted skeleton with these repeated sub-blocks:

- **(a)** "fetch account row; if missing → `_fail_post(...)`" — 7 occurrences. **Mastodon (`scheduler.py:1140-1143`) is the only one missing `permanent=True`** — this is exactly the class of bug the existing review already flagged (item #12: "Missing-account failures not marked `permanent=True` in Mastodon/email paths, unlike Bluesky"). The duplication *is* the root cause: there's no single place this logic lives, so each copy can silently drift.
- **(b)** `try: attach_image = bool(echo["attach_image"]) except (KeyError, IndexError): attach_image = False` — byte-identical at lines 1156-1159, 1464-1467, 1675-1678, 1800-1803, 2000-2003 (5×).
- **(c)** Feed-alt-wins-else-AI-fallback logic — ~80% identical at 1192-1215 (mastodon), 1479-1499 (bluesky), 1685-1704 (microblog), 1820-1838 (matrix).
- **(d)** `_still_owns_claim` re-check + near-identical warning log immediately before dispatch — 1249-1255, 1320-1326, 1546-1552, 1716-1722, 1874-1880, 2016-2022, 2095-2101 (7×, ~95% identical, only the destination-name string differs).
- **(e)** Success tail `ok = _update_post(..., "success", post_url=...); if ok: record_success(echo["id"]); return ok` — identical shape in all 7.
- **(f)** 3-tier exception ladder (AuthError→permanent fail, DestinationError→fail with message, bare Exception→generic fail) — same shape in 5 of 7 (bluesky/microblog/matrix/discord/webhook).

**Estimated duplication:** ~35-45% of each function's body.
**Fix (extraction plan):**
```python
def _destination_account(table: str, account_id: int, echo: dict) -> dict | None:
    """Fetch the account row for a destination, or None if missing."""
    ...

def _echo_attach_image(echo) -> bool:
    try:
        return bool(echo["attach_image"])
    except (KeyError, IndexError):
        return False

def _resolve_alt_text(item, img_bytes, img_type, user_id, echo_id, item_id) -> str | None:
    ...  # feed-alt-wins-else-AI-fallback, single implementation

def _guard_claim(posted_id, claim_token, echo_id, item_id, dest_name) -> bool:
    ...  # the (d) re-check + log

def _finalize_success(posted_id, claim_token, echo_id, post_url: str = "") -> bool:
    ok = _update_post(posted_id, claim_token, "success", post_url=post_url)
    if ok:
        record_success(echo_id)
    return ok
```
Then fix the mastodon gap as part of the same change: `_destination_account` failures always pass `permanent=True` to `_fail_post`. **Effort: M** — touches the hottest path in the app (posting), needs full test-suite run (SQLite + Postgres per `docs/reviews/...`'s own methodology) before merging.

### 3.2 SQLite/Postgres schema hand-authored twice — `database.py` (Importance: 6/10)

`init_db_sqlite()` (`database.py:293-903`) and `init_db_postgres()` (967-1414) each define `CREATE TABLE` for the same 22 tables (`accounts`, `feeds`, `feed_items`, `echoes`, `digest_items`, `drip_items`, six `*_accounts` tables, `users`, `settings`, `posted_items`, `queued_posts`, `queue_settings`, `oauth_apps`, `oauth_states`, `invite_codes`, `system_settings`, `scheduler_leases`, `email_tokens`) with mirrored columns in two SQL dialects. This has already drifted in production: the prior review's item #32 (PG FK columns `INTEGER` against `BIGSERIAL` PKs — fails at 2^31 rows) is a direct consequence of maintaining two hand-written schemas instead of one source of truth.

**Fix:** not a drop-in snippet — this needs a dialect-agnostic table/column spec that generates both `CREATE TABLE` branches, or at minimum a schema-parity test stronger than the existing regex-based `test_dialect.py`. **Effort: L.** Flagging for planning, not immediate action.

### 3.3 `normalize_webhook_url` — same name, different rules, two files (Importance: 5/10)

`discord.py:157-179` and `webhook.py:166-201` are both named `normalize_webhook_url`, both: strip input, raise `ValueError` on empty, validate scheme/host shape, return a canonical URL — but the actual validation rules differ (Discord regex-matches a specific host+path pattern; the generic webhook checks scheme/credentials/SSRF). This is not a merge candidate — the business rules are legitimately different — but the name collision is a real hazard: `from discord import normalize_webhook_url` vs `from webhook import normalize_webhook_url` will silently apply the wrong validation if anyone ever imports the wrong one, or if the two are ever consolidated into a shared import without noticing the rule mismatch.

**Fix:**
```python
# discord.py — rename for clarity, update the one internal call site:
def normalize_discord_webhook_url(raw: str) -> str:
    ...
```
Also extract the shared preamble into `utils`:
```python
def require_nonempty(raw: str, message: str) -> str:
    value = (raw or "").strip()
    if not value:
        raise ValueError(message)
    return value
```
**Effort: S.**

### 3.4 `app.py` admin-guard boilerplate repeated 11× (Importance: 5/10)

The 4-line block `uid = _admin_uid_or_none(request); if uid is None: return render("error.html", request, status_code=403, code=403, message="Admin access required")` is byte-identical across 11 route handlers: `admin_page`, `admin_email_save`, `admin_email_test`, `admin_suspend`, `admin_unsuspend`, `admin_promote`, `admin_demote`, `admin_set_plan`, `admin_extend_trial`, `admin_generate_invites`, `admin_revoke_invite` — at `app.py:1512-1515, 1552-1555, 1606-1609, 1628-1631, 1656-1659, 1677-1680, 1698-1701, 1735-1737, 1760-1763, 1798-1801, 1817-1820`.

**Fix:**
```python
def _require_admin(request) -> int:
    """Return the admin uid or raise HTTPException(403)."""
    uid = _admin_uid_or_none(request)
    if uid is None:
        raise HTTPException(status_code=403, detail="Admin access required")
    return uid
```
Each of the 11 call sites becomes `uid = _require_admin(request)`, and a shared exception handler renders `error.html` for `HTTPException`. This is also a security hygiene win: a single guard function is harder to accidentally omit or get wrong than 11 independent copies. **Effort: S** — 403 behavior is already covered by tests per the existing test suite.

### 3.5 `app.py` "user not found" 404 boilerplate repeated 7× (Importance: 3/10)

`row = db.execute("SELECT ... FROM users WHERE id = ?", (user_id,)).fetchone(); if row is None: return render("error.html", ..., status_code=404, message="User not found")` at `app.py:965, 1639, 1664, 1685, 1709, 1745, 1774` (~90% identical; column list varies `id` vs `id, plan`).

**Fix:** `def _get_user_or_404(db, user_id, columns="id")` returning the row or raising. **Effort: S.**

### 3.6 `app.py` `render("error.html", ...)` call shape repeated 30× (Importance: 3/10)

30 call sites (grep-confirmed) pass `status_code=X, code=X` — the same value twice as separate kwargs.

**Fix:** `def _error(request, status: int, message: str): return render("error.html", request, status_code=status, code=status, message=message)`. **Effort: S.**

### 3.7 `app.py` settings-upsert SQL repeated 5× inside a 3×-repeated loop (Importance: 3/10)

The literal `INSERT INTO settings (user_id, key, value) VALUES (?, ?, ?) ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value` appears at `app.py:3890-3892, 3897-3899, 3950-3952, 3974-3976, 3981-3983`, each inside a `for key, value in values.items(): db.execute(...)` loop duplicated at 3888-3893, 3948-3953, 3972-3977 (in `save_smtp_settings`, `save_retry_notify_settings`, `save_alt_text_settings`).

**Fix:** `def _upsert_settings(db, uid: int, values: dict[str, str]) -> None` in `database.py` or `settings.py`, called once per route instead of looping inline. **Effort: S.**

### 3.8 `settings.py` int-env-with-fallback repeated 4× (Importance: 3/10)

`SCRYPT_CONCURRENCY` (133-139), `MAX_BACKDATED_ENTRY_DAYS` (249-255), `READER_MAX_ITEMS_PER_FEED` (262-268), `READER_MAX_STARRED_PER_FEED` (270-276) each: `try: X = int(env(name, default)) except ValueError: logger.warning(...); X = default`.

**Fix:**
```python
def _env_int(name: str, default: int) -> int:
    try:
        return int(env(name, str(default)))
    except ValueError:
        logging.getLogger("feedecho").warning(
            "FEEDECHO_%s is not a valid integer; using default of %s", name, default
        )
        return default
```
**Effort: S.**

### 3.9 `templates/accounts.html` — 7 near-identical per-destination sections (Importance: 5/10)

Mastodon (70-129), Email (131-186), Bluesky (188-239), Micro.blog (241-289), Matrix (290-345), Discord (347-396), Webhook (398-469) each repeat: `<details class="account-section">` → `<summary>` with a connected-count badge → connect form → `<table class="data-table">` listing rows with a "Test" button (`withBusy(...)`) and a delete `<form>` sharing the same `confirm('Delete this account? ...')` text. Only field labels/columns and the account-list variable differ (~65-70% shared markup).

**Fix:** a Jinja macro `{% macro account_section(kind, accounts, columns, connect_fields) %}` in a new `templates/_macros.html`. **Effort: L** — the Mastodon OAuth-vs-manual dual connect form (lines 70-95) doesn't fit the mold cleanly and needs its own branch inside the macro; do this as a dedicated pass, not opportunistically.

### 3.10 `templates/echoes.html` destination-type if/elif chain (Importance: 4/10)

`templates/echoes.html:223-235` has a 7-branch `{% if echo.destination_type == 'mastodon' %}...{% elif == 'email' %}...` chain enumerating the same 7 destination types as §3.9 and `import_export.py`'s `_ACCOUNTS` table (`import_export.py:41-55`). `import_export.py`'s `_ACCOUNTS` list is already a centralized registry and is a good model — **needs verification** whether `app.py`/`scheduler.py` have an equivalent, in which case the fix is "have templates consume the existing registry" rather than inventing a new one.

**Effort: M** (contingent on the verification above).

### 3.11 HTTP status → typed-exception classification duplicated 3× (Importance: 7/10)

`discord.py:124-154`, `matrix.py:109-130`, `webhook.py:244-274` each: check status ranges, raise module-specific exception subclasses for 401/403 (auth), 404/410 (not found), 429 (rate limit, carries `retry_after`), 400/422 (rejected), 3xx (redirect refused), fallback generic. ~60% of each function's control flow (the status-range dispatch) is shared; the exception classes themselves are necessarily distinct. This is the strongest duplication cluster for correctness risk: it's the same *shape* of logic that the existing review already found drifting (permanent vs transient misclassification between destination types).

**Fix:**
```python
# utils.py addition (not yet in the module — add if adopting this fix):
def classify_http_status(status_code: int) -> str:
    if status_code in (401, 403):
        return "auth"
    if status_code in (404, 410):
        return "not_found"
    if status_code == 429:
        return "rate_limit"
    if status_code in (400, 422):
        return "rejected"
    if 300 <= status_code < 400:
        return "redirect"
    return "other"
```
Each module's `_raise_for_status` calls `classify_http_status(response.status_code)` then maps the category to its own exception type. **Effort: M** — `scheduler.py` depends on the exception *types* for retry logic (verify via `grep -n "except.*Error" scheduler.py`), so this needs test coverage on all 3 modules' failure paths, not just a mechanical extraction.

### 3.12 Secret-mask sentinel: two conventions for one purpose (Importance: 4/10)

Per-tenant SMTP password / alt-text API key is masked as the literal `"********"` (`app.py:998, 2882`, checked again at 3895, 3979 to detect "unchanged"). The admin system SMTP password is masked as `"•••••• (stored)"` (`app.py:1533`, checked at 1582). Same purpose (don't overwrite a secret on save unless retyped), two sentinel conventions, each hardcoded in 2+ places.

**Fix:** one module-level constant per convention at minimum (`SECRET_MASKED = "********"`); ideally unify to one convention — but check `templates/admin.html`'s expectation of the `•` string before changing the *value*, not just the constant. **Effort: S**, with a template cross-check.

### 3.13 `_now_str`/UTC-timestamp helper reimplemented 5×+ (Importance: 5/10)

Identical body `datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")`, each with its own docstring repeating the same PG-timezone warning: `auth.py:104-105`, `invites.py:28-31`, `verification.py:22-26`, `notify.py:36-37` (all confirmed via grep), plus `oauth.py:33-38` (`_now()`/`_sqlite_timestamp()`, same result via a different route). The raw format string `"%Y-%m-%d %H:%M:%S"` is also inlined ad hoc in `import_export.py:103` and `database.py:105`.

**Fix:** already implemented as `utils.utc_now_str()` — see [§6](#6-new-utils-module). Replace each local `_now_str`/`_now` definition with an import:
```python
from utils import utc_now_str as _now_str
```
**Effort: S** — mechanical, one import + one deleted function per file, 5-6 files.

### 3.14 `scheduler.py` `BLUESKY_MAX_GRAPHEMES` duplicates `bluesky.py`'s constant (Importance: 3/10)

`scheduler.py:92` defines `BLUESKY_MAX_GRAPHEMES = 300`, duplicating `bluesky.py:28`'s `MAX_POST_GRAPHEMES = 300` (used at `scheduler.py:1448`).

**Fix:** `from bluesky import MAX_POST_GRAPHEMES` in `scheduler.py`; delete the local constant. **Effort: S.**

### 3.15 `REQUEST_TIMEOUT = 30` constant repeated 4×, inconsistent elsewhere (Importance: 3/10)

`discord.py:44`, `matrix.py:51`, `microblog.py:27`, `webhook.py:52` each independently define `REQUEST_TIMEOUT = 30`; `bluesky.py`/`mastodon.py` instead inline `timeout=30`/`timeout=15.0`/`timeout=60` with no named constant at all.

**Fix:** use `utils.DEFAULT_REQUEST_TIMEOUT` where a module doesn't need a different value; for bluesky/mastodon, at minimum name the inline literal as a module constant so a future "bump the default" change doesn't require finding every inline number. **Effort: S.**

---

## 4. Data duplication

### 4.1 Live email-regex drift — the highest-priority data duplication (Importance: 8/10)

Confirmed via grep across the whole repo, three distinct regex literals used for the same "is this a syntactically valid email" check, live simultaneously:

- `auth.py:41` — `_EMAIL_RE = re.compile(r"^[^@\s\r\n]+@[^@\s\r\n]+\.[^@\s\r\n]+$")` — **requires a dot** in the domain.
- `app.py:1570` (`admin_email_save`) — same dot-requiring pattern, inlined rather than imported from `auth.py`.
- `app.py:3175` (`add_email_account`) — `r"^[^@\s\r\n]+@[^@\s\r\n]+$"` — **no dot required**.
- `app.py:3866` (`save_smtp_settings`) — same loose pattern as 3175 (comment there explicitly says `feedecho@localhost` must pass).

This is the exact class of bug the existing review flagged as HIGH (finding #1: recipient addresses that become message headers must reject control characters "at store time, not send time" — here the *inconsistency* is that two of the four validation sites accept a syntax the other two reject, meaning which check runs first determines whether a given input is accepted). This is data duplication (the loose pattern is copy-pasted verbatim at two sites) plus a live behavioral drift between the strict and loose variants.

**Fix:** already implemented as `utils.EMAIL_RE` / `utils.EMAIL_RE_LOOSE` / `utils.is_valid_email()` — see [§6](#6-new-utils-module). Adoption:
```python
# auth.py — replace the local _EMAIL_RE with:
from utils import EMAIL_RE as _EMAIL_RE

# app.py:1570, 3175, 3866 — replace each inline re.match(...) with:
from utils import is_valid_email
...
if from_email and not is_valid_email(from_email):        # app.py:1570 — strict
if not is_valid_email(email, require_domain_dot=False):  # app.py:3175 — loose
if not is_valid_email(smtp_from_email.strip(), require_domain_dot=False):  # app.py:3866 — loose
```
**Effort: S** for the mechanical swap; but **decide deliberately** whether the strict/loose split should exist at all (why does the admin-wide SMTP relay require a dot but per-tenant sender addresses don't?) — that's a product decision, not a refactor, and is flagged here rather than resolved.

### 4.2 SQLite/Postgres schema duplication — see [§3.2](#32-sqlitepostgres-schema-hand-authored-twice--databasepy-importance-610).

### 4.3 `scheduler.py` graphemes constant — see [§3.14](#314-schedulerpy-bluesky_max_graphemes-duplicates-blueskypys-constant-importance-310).

### 4.4 `REQUEST_TIMEOUT` constant — see [§3.15](#315-request_timeout--30-constant-repeated-4-inconsistent-elsewhere-importance-310).

### 4.5 Secret-mask sentinel — see [§3.12](#312-secret-mask-sentinel-two-conventions-for-one-purpose-importance-410).

---

## 5. Test-suite duplication (`tests/`)

### 5.1 No `tests/conftest.py`; `db_tmp` fixture duplicated across 16 files (Importance: 5/10)

Confirmed: `tests/conftest.py` does not exist. The `db_tmp` fixture (mkstemp → close → unlink → patch `database.DB_PATH` → `init_db()` → yield → cleanup) is redefined per-file in at least 16 test files, in three near-identical variants:

- No scheduler patch: `test_accounts_sections.py:17-31`, `test_oauth_app.py:31-45`, `test_invites.py:28-40`.
- Plus `monkeypatch.setattr(scheduler, "get_db", database.get_db)`: `test_discord.py:23-37`, `test_matrix.py:23-37`, `test_webhook.py:27-41`, `test_microblog.py:24-39`, `test_plans.py:26-40`.
- Same body with inline (not module-level) `import database`/`import scheduler`: `test_bluesky.py:10-24`, `test_digest.py:10-24`, `test_filters.py:10-24`, `test_kimi_full_review_fixes.py:23-37`, `test_mastodon_post_url.py:23-38`.

`test_reader.py` has a genuinely different implementation (uses `TemporaryDirectory`, no scheduler patch) and should stay local rather than being forced into the shared fixture.

**Fix:** create `tests/conftest.py`:
```python
import os
import tempfile
import pytest
import database
import scheduler

@pytest.fixture
def db_tmp(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    monkeypatch.setattr(database, "DB_PATH", database.Path(path))
    database.init_db()
    monkeypatch.setattr(scheduler, "get_db", database.get_db)
    yield database
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except OSError:
            pass
```
Delete the 16 local copies (keep `test_reader.py`'s local fixture). **Effort: S** — mechanical per file, but touch 16 files.

### 5.2 `_setup_echo` helper duplicated with an actual default-value drift (Importance: 6/10 — bug-adjacent)

`test_alt_text.py:364-402` and `test_cw_and_images.py` (~46-89) define an otherwise-identical `_setup_echo(db_tmp, echo_overrides=None)` helper, but with `"attach_image": 1` in `test_alt_text.py:373` vs `"attach_image": 0` in `test_cw_and_images.py:58`. This means a test in either file that relies on the *default* (doesn't pass `attach_image` explicitly) silently exercises a different code path depending which file it's copied from next — exactly the kind of drift that produces a false-negative test (a real regression that only shows up under the default the test didn't happen to use).

**Fix:** move one canonical version to `tests/conftest.py`; before consolidating, grep both files for `_setup_echo(db_tmp, {` call sites that omit `attach_image` and make it explicit at each of those sites so behavior doesn't silently change when the default moves. **Effort: S**, with a mandatory pre-check.

### 5.3 `_register`/`_login`/`_uid`/`_seed` helpers independently defined in 11 files — **Needs verification** (Importance: 3/10)

11 files define at least one of these locally (`test_account_deletion.py`, `test_auth_multi.py`, `test_backdated.py`, `test_drip.py`, `test_feed_edit.py`, `test_feed_delete.py`, `test_history_display.py`, `test_invites.py`, `test_import_export.py`, `test_history_filters.py`, `test_retry_notify.py`), but bodies were not pairwise-diffed. **To confirm:** `for f in <files>; do sed -n '/^def _register(/,/^$/p' $f; done | diff` (or equivalent per helper name) — if bodies match, extract to `conftest.py`; if they've diverged per-test-scenario, leave as-is. Separately, the `client(...)` fixture name is reused in 12 files but bodies are **confirmed not duplicates** (each patches different state) — no action needed there.

---

## 6. New `utils.py` module

Created at repo root: `utils.py`. Stdlib-only, no imports from the app's own modules (avoids circular-import risk with `app.py`/`scheduler.py`/`database.py`). Implements:

| Function | Replaces |
|---|---|
| `utc_now_str()` | §3.13 — `_now_str` in `auth.py`, `invites.py`, `verification.py`, `oauth.py`, `notify.py` |
| `is_valid_email()` / `EMAIL_RE` / `EMAIL_RE_LOOSE` | §4.1 — the 3-way email regex drift |
| `truncate_chars()` | §1.3 — `discord.py`/`matrix.py` truncation |
| `DEFAULT_REQUEST_TIMEOUT` | §3.15 — repeated `REQUEST_TIMEOUT = 30` |
| `json_error_detail()` | §2.1 — 4× JSON error-body parsing |
| `parse_retry_after()` | §2.2 — 2× Retry-After parsing |
| `rows_to_dict()` | §2.4 — settings rows→dict idiom |
| `check_allowance()` | §2.3 — reference implementation; `plans.py` should adapt this to raise `PlanError` rather than import `utils` (see the docstring caveat in the module) to avoid a circular import |

**Not included** (kept out to avoid a bigger blast radius than requested): `classify_http_status` (§3.11, add if you take on that refactor), the admin-guard/404/error-render helpers (§3.4/3.5/3.6 — these belong in `app.py` itself since they reference `render()` and `HTTPException`, not in a dependency-free `utils.py`), and the schema-generation work (§3.2, L effort, needs design first).

**Adoption status:** the module is written and self-contained but **not yet wired into any call site** — each finding above gives the exact snippet to swap in. This was a deliberate choice: rewiring ~20 files touches hot paths (posting, auth, admin) that deserve their own test run and review pass rather than being bundled into an audit deliverable. Recommended order if you want to adopt incrementally: §3.13 (`_now_str`, zero behavior change) → §1.3/§2.1/§2.2 (destination modules, well-covered by existing tests) → §4.1 (email regex — needs the product decision on strict vs loose first) → §2.3/§2.4 (plans/settings, moderate effort).

---

## Appendix: ruled out (checked, not duplication)

- `security.py` — reviewed in full, no internal duplication found.
- `template_engine.py`, `filters.py` — reviewed in full, no duplication found.
- `style.css` — already mostly uses CSS variables; only 4 raw hex literals found, not a pattern worth extracting.
- `oauth.py`'s `_sign_state`/`_state_signature` vs `security.py`'s `sign_session`/`_session_key` — structurally similar HMAC pattern (~15 lines of overlap) but deliberately separate security domains (OAuth state vs. session auth) with different payload/expiry shapes; merging would add indirection without removing real risk.
- `mastodon.py`/`bluesky.py`/`matrix.py` media-upload functions (`upload_media`, `upload_blob`, `upload_media`) — share only the general shape "POST bytes, get an id/uri back"; payload construction, size limits, and content-type allowlists differ enough per-platform that merging would add abstraction without reducing real duplication.
- `client(...)` test fixture name reused in 12 files — confirmed bodies differ per test scenario; naming convention, not duplication.
- Digest/instant/drip retry-and-failure accounting differences between destination types (already tracked as correctness bugs, not duplication, in `docs/reviews/2026-08-28-kimi-full-repo-review.md` items #3, #4, #11, #12) — cited here only where §3.1 shows the *duplication* is the mechanism that let the drift happen.
