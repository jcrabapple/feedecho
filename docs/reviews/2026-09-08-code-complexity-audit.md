# FeedEcho — Code Complexity Audit

**Date:** 2026-09-08
**Scope:** All Python modules at commit `3dcf392` (v1.49.0), excluding `tests/`.
**Status:** reference snapshot, not yet actioned. `docs/reviews/2026-09-08-code-duplication-audit.md` (its §7) and `docs/reviews/2026-09-08-design-patterns-audit.md` (its adoption-outcome section) each record what was actually implemented from their own findings; several of those changes (e.g. the destination-dispatch registry, `scheduler.py`'s shared skeleton extraction) also reduce cyclomatic complexity reported here, but this document's line numbers and CC figures were not re-measured after those changes landed.
**Method:** Objective metrics computed with tooling, not estimated:
- **Cyclomatic complexity (CC) & Maintainability Index (MI):** [`radon`](https://radon.readthedocs.io/) 6.0.1, run against every file (`radon cc -s -n A -a`, `radon mi -s`).
- **Function/class line counts:** exact, via Python's `ast` module (`end_lineno - lineno + 1`), not line-diffing between `def`s.
- **Coupling (Ce/Ca/instability):** exact, via an `ast`-based import graph over all 25 local top-level modules (module names resolved against local filenames only; stdlib/third-party imports excluded).
- **Cognitive complexity, nesting depth, mixed-abstraction, and refactor snippets:** manual read of every function flagged by the objective metrics above (all line ranges below were read directly, not inferred from the CC number alone).

Companion document: `audits/CODE_DUPLICATION_AUDIT.md` (2026-09-08) — several findings below are the *complexity* side of duplication already documented there; cross-references are marked `[DUP §x.x]`.

**A note on reading CC scores in this report:** cyclomatic complexity counts decision points, and McCabe's original definition counts every `or`, `and`, and ternary in a boolean expression as its own branch. Several functions below have double-digit CC from a flat chain of independent `x or default` fallbacks or early-return guard clauses — genuinely easy to read despite the score. Those are explicitly called out as **"CC overstates difficulty"** so the score isn't applied mechanically. Radon grade bands used throughout: A (1-5), B (6-10), C (11-20), D (21-30), E (31-40), F (41+).

---

## Summary

| # | Finding | Metric | Location | Importance |
|---|---|---|---|---|
| 1 | `reader_page` — worst function in the codebase, ~half its CC is a duplicated query-builder | CC 76 (F), 349 lines | `app.py:2467` | **9/10** |
| 2 | 7 `_send_X` destination dispatchers in `scheduler.py` — deep image-pipeline nesting on the hot posting path | CC 41/33/27/24/18 | `scheduler.py` | **8/10** |
| 3 | `import_data` — 5 mixed concerns flattened into one function | CC 51 (F), 185 lines | `import_export.py:327` | **7/10** |
| 4 | `_flush_queue` — 5 mixed concerns + a real duplicated retry-backoff block | CC 33 (E), 243 lines | `scheduler.py:2280` | **7/10** |
| 5 | `reader_compose` — two full parallel request/response cycles in one function | CC 49 (F), 207 lines | `app.py:5115` | **7/10** |
| 6 | `add_echo` / `edit_echo` — ~90% duplicate functions, no destination-type registry | CC 29 (D) each | `app.py:5395`, `app.py:5528` | **6/10** |
| 7 | feed_parser.py image-extraction cluster — dedup fix also fixes a real behavioral divergence | CC 22/22/16/15/14/11 across 6 fns | `feed_parser.py:857-1136` | **6/10** |
| 8 | `app.py` is a 5925-line, 109-route god-file spanning ~15 unrelated resource domains | 5925 LOC, 109 routes, MI 0.00 (C) | `app.py` | **7/10** |
| 9 | Circular dependency between `app.py` and `auth.py`, worked around with a deferred import | 1 site | `auth.py:193` | **6/10** |
| 10 | `database.py`'s two schema-init functions are enormous but mechanically safe to split | 608 + 446 lines, CC 14 each | `database.py:293`, `database.py:967` | **5/10** |
| 11 | `_flush_digests` — untestable bin-packing algorithm buried inside I/O | CC 23 (D), 167 lines | `scheduler.py:2699` | **5/10** |
| 12 | bluesky.py's 4 HTTP-call functions share an undocumented identical shape (new finding, not in the DRY audit) | CC 15/12/12/12 | `bluesky.py` | **5/10** |
| 13 | `register_submit` — 5 concerns incl. a side-effecting email send inside validation | CC 20 (C), 151 lines | `auth.py:218` | **5/10** |
| 14 | `scheduler.py` has the highest efferent coupling in the codebase (imports 16 of 25 local modules) | Ce = 16 | `scheduler.py` | **5/10** |
| 15 | `login_submit` crams two unrelated auth mechanisms behind one branch | CC 11 (C), 81 lines | `auth.py:377` | **4/10** |
| 16 | `generate_alt_text` — 5 concerns incl. a retry loop wrapping dispatch+parse+validate | CC 18 (C), 113 lines | `alt_text.py:100` | **4/10** |
| 17 | `_check_feed_with_lease` — CC overstates difficulty; it's a flat guard-clause chain | CC 26 (D), 151 lines | `scheduler.py:361` | **3/10** |
| 18 | `reader_compose_preview` — CC overstates difficulty; it's null-coalescing chains | CC 20 (C), 56 lines | `app.py:5056` | **3/10** |
| 19 | `_store_feed_items` — CC overstates difficulty; flat field-by-field mapping, do not refactor | CC 18 (C), 67 lines | `scheduler.py:278` | **1/10** |
| 20 | `admin_email_save` / `oauth_callback` — CC from independent guard clauses, acceptable as-is | CC 18/16 (C) | `app.py:1551`, `app.py:5820` | **2/10** |
| 21 | `AuthMiddleware` — large but cohesive; one minor tail duplication between modes | 242 lines, 7 methods | `app.py:300` | **3/10** |
| 22 | `_validate_payload` — legitimately flat validation, low priority | CC 23 (D) | `import_export.py:154` | **2/10** |
| 23 | No class in the codebase exceeds 500 lines — the "classes over 500 lines" check is N/A here | max 242 lines | `app.py:300` (`AuthMiddleware`) | **N/A** |
| 24 | `feed_parser.py` and `settings.py` are the most depended-upon, most stable modules (Ce=0) — cited as a positive baseline, not a defect | Ca 11 / 13, I = 0.00 | `feed_parser.py`, `settings.py` | **N/A (positive)** |

---

## 1. Cyclomatic complexity

Full repo scan (excluding `tests/`): **2,609 blocks analyzed, average CC = A (3.47)**. The distribution is bimodal — the overwhelming majority of functions are simple (A/B grade), concentrated almost entirely in `app.py`, `scheduler.py`, `import_export.py`, `feed_parser.py`, `database.py`, `auth.py`, and `bluesky.py`. 82 functions across the codebase score C-grade (CC≥11) or worse; 12 score D or worse (CC≥21); 3 score F (CC≥41).

### 1.1 `reader_page` — `app.py:2467-2815` (CC 76, grade F, 349 lines) — Importance: 9/10

The single worst function in the codebase, and by a wide margin (next-worst is CC 51). Verified root cause: **roughly half of the 76 branches are a duplicate**. The saved-search unread-count block (`app.py:2699-2775`) reimplements the same filter-op dispatch (`is`/`feed`/`folder`/`in`), the same LIKE-escaping, and the same mute-keyword loop as the main query builder above it (`app.py:2532-2612`) — the mute-keyword loop specifically is duplicated verbatim at both `2604-2612` and `2754-2762`, and the `mutes` query itself runs twice (`2599-2603` and `2706-2710`) for no different input. Max nesting is 5 levels, at `2712-2762` (`with get_db()` → `if saved_searches` → `for s in saved_searches` → `for op, val in s_filters` → nested `if/elif`).

**Fix:**
```python
def _reader_where_clause(uid, feed_id, folder_id, q, view, after, mutes) -> tuple[list[str], list]:
    """WHERE/params shared by the main item list and every saved-search count."""
    where = ["f.user_id = ?", "f.read_enabled = 1", "f.deleted_at IS NULL"]
    params: list = [uid]
    # ... lines 2534-2612 move here verbatim, parameterized instead of closed over ...
    return where, params
```
Call it once for the main list and once per saved search (feed_id=folder_id=None, view driven by the search's own `is:` filter); fetch `mutes` once and pass it in both times.

**Effort: M.** **Post-refactor CC estimate: ~15-20** for the orchestrator once the where-builder (removes ~30), the delivery-badge join, and the saved-search-counts loop (now just calling the shared builder, removes ~15) are pulled out.

### 1.2 `import_data` — `import_export.py:327-511` (CC 51, grade F, 185 lines) — Importance: 7/10

Not deep nesting (max depth 3-4) — this is **breadth complexity**: 4 sequential phases (dedup-feeds `343-359`, dedup-accounts `365-378`, insert-feeds `394-412`, insert-accounts `415-435`, insert-echoes-with-validation `441-498`) each contributing many independent branches, all flattened into one function body. Abstraction mixing: read-only dedup queries sit next to raw INSERT SQL next to per-field validation/clamping next to summary-dict assembly.

**Fix (5 extractions):**
```python
def _dedupe_feeds(db, uid, feeds) -> tuple[dict, dict, dict, list]: ...      # lines 339-359
def _dedupe_accounts(db, uid, accounts_by_section) -> tuple[dict, dict, dict, dict]: ...  # 361-378
def _insert_new_feeds(db, uid, new_feed_urls, first_feed, feed_oldids, reader_allowed, feed_map) -> None: ...  # 394-412
def _insert_new_accounts(db, uid, new_account_keys, first_account, account_oldids, account_maps) -> None: ...  # 415-435
def _normalize_echo_fields(echo: dict) -> dict:
    """Clamp visibility/filter_mode/delivery_mode to their valid sets."""
    ...  # lines 468-476, the 3 near-identical if-not-in-valid-set blocks
def _insert_echoes(db, uid, echoes, feed_map, account_maps) -> tuple[int, int, int]: ...  # 438-498
```
`import_data` becomes: validate → dedupe_feeds → dedupe_accounts → (quota check if MULTI) → insert_new_feeds → insert_new_accounts → insert_echoes → build summary dict.

**Effort: M.** **Post-refactor CC estimate: ~5-6** for the orchestrator; `_insert_echoes` carries ~12-15 and is the next candidate if revisited.

### 1.3 `reader_compose` — `app.py:5115-5321` (CC 49, grade F, 207 lines) — Importance: 7/10

Max nesting only 3 levels (`5219-5233`). The complexity is **two full parallel request/response cycles living in one function**: the "enqueue" branch (`5219-5281`) and the "post now" branch (`5284-5321`) each independently validate destinations, build an echo row, and report results, sharing no code. The destination-validation loop (`5159-5174`) is itself a third independent reimplementation of "validate a `type:id` destination reference," a shape that also appears in `add_echo`/`edit_echo` (§1.4) — three call sites for one concept.

**Fix:**
```python
def _validate_destination_ref(db, uid: int, dk: str) -> tuple[str, int]:
    """Parse 'type:id', check it's a real destination type, and confirm ownership."""
    dest_type, sep, raw_id = dk.partition(":")
    if not sep or dest_type not in VALID_DEST_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid destination: {dk}")
    destination_id = _filter_int(raw_id)
    if destination_id is None:
        raise HTTPException(status_code=400, detail=f"Invalid destination: {dk}")
    dest_table = DESTINATION_TABLES_BY_TYPE[dest_type]
    if not db.execute(f"SELECT id FROM {dest_table} WHERE id = ? AND user_id = ?", (destination_id, uid)).fetchone():
        raise HTTPException(status_code=404, detail=f"Destination not found: {dk}")
    return dest_type, destination_id
```
Then split `reader_compose` itself into `_enqueue_compose(...)` (5219-5281) and `_dispatch_compose_now(...)` (5284-5321), with the parent function reduced to validation + calling one of the two.

**Effort: M.** **Post-refactor CC estimate:** orchestrator ~10-12; each extracted dispatch helper ~8-10.

### 1.4 `add_echo` (`app.py:5395-5507`, CC 29/D, 112 lines) and `edit_echo` (`app.py:5528-5638`, CC 29/D, 111 lines) — Importance: 6/10

These two functions are **~90% identical**: the same 7 validation guards (`5418-5430` vs `5550-5561`) and the exact same 7-arm `if destination_type == "mastodon": destination_id = account_id ... elif ...` chain resolving which form field holds the destination id (`5438-5469`, byte-for-byte reproduced at `5568-5599`). This elif chain alone accounts for ~14 of the 29 CC points in each function, and it's the same "no destination-type registry" root cause flagged in `[DUP §3.10]`. `edit_echo`'s only structural difference is loading the existing row first.

**Fix:**
```python
_DEST_FORM_FIELDS = {
    "mastodon": "account_id", "email": "email_account_id", "bluesky": "bluesky_account_id",
    "microblog": "microblog_account_id", "matrix": "matrix_account_id",
    "discord": "discord_account_id", "webhook": "webhook_account_id",
}

def _resolve_destination_id(destination_type: str, form_values: dict) -> int:
    field = _DEST_FORM_FIELDS[destination_type]  # KeyError impossible: type already validated
    destination_id = form_values.get(field)
    if not destination_id:
        raise HTTPException(status_code=400, detail=f"{field} required for {destination_type} destination")
    return destination_id
```
Also extract the shared validation prologue (`5418-5435` / `5550-5566`, identical) into `_validate_echo_form(destination_type, visibility, filter_mode, delivery_mode, drip_limit, template, uid, db) -> int` (returns the clamped drip_limit).

**Effort: M** (shared by both functions; do together). **Post-refactor CC estimate:** both drop from 29 to ~8-10.

### 1.5 The 7 `_send_X` destination dispatchers in `scheduler.py` — Importance: 8/10

`[DUP §3.1]` already identified that these 7 functions (mastodon `1126`/CC41, bluesky `1422`/CC27, microblog `1637`/CC18, matrix `1759`/CC24, discord `1964`, webhook `2056`, email `1286`) share a copy-pasted skeleton and proposed 5 shared helpers. Deep read confirms those helpers help but **undercount the real complexity source**: the image-attachment pipeline that each function inlines reaches **5-7 levels of nesting** over 60-100 lines (e.g. `_send_mastodon:1161-1242`, `_send_bluesky:1464-1540`) — `if attach_image → if image_url/raw_urls → if image_result → if type/size valid → if alt_description / elif alt_text.is_enabled → try/except generation`. The DRY audit's proposed `_resolve_alt_text` helper only covers the innermost 2 of those levels.

**Fix — extract one level higher than the DRY audit proposed:**
```python
def _attach_platform_image(item, echo, *, allowed_types=None, max_bytes=None) -> tuple[bytes | None, str, str]:
    """Fetch, validate, and resolve alt text for one image.
    Returns (blob_bytes_or_None, content_type, alt_text)."""
    ...
```
Bluesky's re-authentication-and-retry-once block (`_send_bluesky:1564-1622`, 3 nested except clauses) is Bluesky-specific — extract separately as `_post_with_reauth_retry(session, account, do_post)`, don't fold it into the shared skeleton.

**Effort: M** (touches the hottest path in the app — posting; needs a full SQLite+Postgres test run before merging, per the project's own review methodology). **Post-refactor CC estimates:** `_send_mastodon` 41→~14-16, `_send_bluesky` 27→~10-12, `_send_matrix` 24→~11-13 (matrix inherently posts text and image as two separate API events — that residual complexity is real domain behavior, not duplication, and shouldn't be eliminated), `_send_microblog` 18→~9-10.

### 1.6 `_flush_queue` — `scheduler.py:2280-2522` (CC 33, grade E, 243 lines) — Importance: 7/10

Max nesting depth 5, at `2476-2478`. Five mixed concerns in one function: SQL claim/lease logic (`2340-2360`), cross-tenant plan-cap math (`2366-2385`), a surprising, easy-to-miss row-migration that backfills a missing `echoes` row for legacy queue entries (`2392-2419`), item-dict hydration (`2421-2448`), and retry-backoff/status-transition bookkeeping (`2450-2522`). **A real bug-adjacent duplication was found here**: the retry-backoff computation `datetime.now(timezone.utc) + timedelta(minutes=new_attempt*10)` plus the same `UPDATE queued_posts SET status='queued'...` statement appears twice verbatim — once at `2479-2486` and again at `2506-2515` (success-then-marked-failed vs. exception branches) — a prime spot for the two copies to silently drift.

**Fix:**
```python
def _schedule_retry_or_terminal(db, qp_id, claim_token, row, err_msg: str) -> None:
    """Unifies the two identical backoff blocks at 2479-2486 and 2506-2515."""
    new_attempt = row["attempt_count"] + 1
    if new_attempt < 3:
        retry_at = (datetime.now(timezone.utc) + timedelta(minutes=new_attempt * 10)).strftime("%Y-%m-%d %H:%M:%S")
        db.execute(
            "UPDATE queued_posts SET status='queued', error_message=?, attempt_count=?,"
            " scheduled_at=?, claim_token=NULL, claimed_at=NULL WHERE id=? AND claim_token=?",
            (err_msg, new_attempt, retry_at, qp_id, claim_token),
        )
    else:
        db.execute(
            "UPDATE queued_posts SET status='failed', error_message=?, attempt_count=?"
            " WHERE id=? AND claim_token=?",
            (err_msg, new_attempt, qp_id, claim_token),
        )
```
Plus 6 more extractions for the other concerns (`_reclaim_stuck_sending`, `_fetch_due_queue_rows`, `_hourly_post_counts`, `_claim_queue_row`, `_hourly_cap_exceeded`, `_get_or_create_echo`, `_build_item_from_row`) — see full breakdown in the fork evidence; each is a straight-line extraction of an existing contiguous block.

**Effort: M.** **Post-refactor CC estimate: ~10-12.**

### 1.7 feed_parser.py image-extraction cluster — Importance: 6/10 (elevated from a pure-complexity finding — a real bug was found)

`[DUP §2.5]` proposed making the single-result extractors thin wrappers over the list extractors. Deep read **confirms this also fixes a real correctness bug, not just duplication**: `_extract_rss_image` (`932-964`, CC 14) is not actually a subset of `_extract_rss_images` (`857-929`, CC 22) — it independently reimplements the walk with different behavior (merges media_content/media_thumbnail into one loop instead of "thumbnail only if content is empty" per `901-904`, and skips the video/audio filter `_is_image_media` entirely, `879-887`). **A feed entry with a video in `media_content` and an image in `media_thumbnail` yields different results from the single-image extractor vs. the list extractor on the same input today.**

**Fix:** as proposed in `[DUP §2.5]` — make `_extract_rss_image`/`_extract_rss_image_alt`/`_extract_json_feed_image`/`_extract_json_feed_image_alt` thin wrappers (`imgs = _extract_x_images(entry); return imgs[0]["url"] if imgs else None`). This is not purely mechanical here — it changes `_extract_rss_image`'s behavior to match `_extract_rss_images`', which is the *correct* direction, but **needs a regression check**: run `tests/test_feed_parser.py` and `tests/test_feed_image_alt.py` before/after to confirm no test asserts the current (buggy) behavior.

**Effort: S-M.** **Post-refactor CC estimate:** the 4 wrapper functions drop from CC 22/14/15/11 to CC 2 each; total cluster CC drops from ~100 to ~45-50, concentrated entirely in the two canonical list functions (RSS and JSON Feed), which retain their CC because the priority-order walk is genuine, irreducible feature logic.

### 1.8 `database.py`'s two schema-init functions — `init_db_sqlite:293-903` (608 lines, CC 14) and `init_db_postgres:967-1414` (446 lines, CC 14) — Importance: 5/10

Confirmed via direct read: both are **genuinely flat** — 22 sequential `CREATE TABLE IF NOT EXISTS` statements with zero branching each. All of the CC 14 is concentrated in ~4 one-off migration blocks interspersed among the table definitions (a `username` backfill at `310-326`, unique-constraint table-rebuild guards for `email_accounts`/`bluesky_accounts` at `377`/`399`, a `settings.user_id` backfill guard at `439`). This is a low-risk, purely mechanical split.

**Fix:**
```python
def _create_accounts_table(db) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS accounts (...)""")  # lines 298-308

def _migrate_accounts_username(db) -> None:
    ...  # lines 310-326 — the one real migration branch for this table

# ... one pair (create + migrate-if-needed) per table, ~26 tiny functions total
```
`init_db_sqlite` becomes a flat call list. This is also a stepping stone toward `[DUP §3.2]`'s recommendation of a dialect-agnostic schema spec — splitting per-table first makes a later generator straightforward to build table-by-table.

**Effort: S**, despite the line count — it's copy-paste-and-wrap, not a logic change. **Post-refactor CC: 1** for each orchestrator; each extracted function is CC 1 (pure DDL) or CC 2-4 (migrations).

### 1.9 `register_submit` — `auth.py:218-368` (CC 20, grade C, 151 lines) — Importance: 5/10

Max nesting depth 5 at `321-327`. Five concerns in sequence: form validation (`226-236`), rate-limit check (`243-250`), a DB transaction with embedded early-return HTTP responses (`252-338`), a best-effort side-effect email send **outside** the transaction (`342-360`), and response/cookie construction (`365-368`).

**Fix:**
```python
def _validate_registration_form(email: str, password: str, confirm: str) -> list[str]:
    errors = []
    if not _EMAIL_RE.match(email):
        errors.append("Enter a valid email address.")
    if len(password) < _MIN_PASSWORD_LENGTH:
        errors.append(f"Password must be at least {_MIN_PASSWORD_LENGTH} characters.")
    if len(password) > _MAX_PASSWORD_LENGTH:
        errors.append(f"Password must be at most {_MAX_PASSWORD_LENGTH} characters.")
    if password != confirm:
        errors.append("Passwords do not match.")
    return errors

def _create_user_or_none(db, email, password, trial_end) -> dict | None:
    """Returns the new user row, or None on a duplicate-email race."""
    ...  # lines 286-312, catching IntegrityError/UniqueViolation -> return None

def _send_verification_email_best_effort(user_id: int, email: str) -> None:
    ...  # lines 342-360, already self-contained
```
**Effort: S.** **Post-refactor CC estimate: ~10-11** — the remaining branches (duplicate email, bad invite, throttled) are legitimate user-facing early returns, not smell.

### 1.10 `login_submit` — `auth.py:377-457` (CC 11, grade C, 81 lines) — Importance: 4/10

Low CC but **crams two entirely unrelated authentication mechanisms** behind one `if not settings.MULTI` at line 383: single-mode shared-secret token comparison (`383-418`) vs. multi-mode email+password+session lookup (`420-457`). This is mixed responsibility, not deep nesting (nesting is only 3 levels).

**Fix:**
```python
def _login_single_mode(request, token: str):
    ...  # lines 384-418, unchanged

def _login_multi_mode(request, email: str, password: str):
    ...  # lines 420-457, unchanged

def login_submit(request, email: str = Form(""), password: str = Form(""), token: str = Form("")):
    if not settings.MULTI:
        return _login_single_mode(request, token)
    return _login_multi_mode(request, email, password)
```
**Effort: S.** **Post-refactor CC: 2** for the dispatcher; ~6-7 for each mode handler.

### 1.11 `generate_alt_text` — `alt_text.py:100-212` (CC 18, grade C, 113 lines) — Importance: 4/10

Max nesting depth 4 at `187-194`. Five concerns wrapped in a retry loop: settings/config fetch+guard (`109-118`), request-payload construction (`120-136`), SSRF validation gated on `MULTI` (`148-153`), dual-path HTTP dispatch — pinned vs. unpinned client (`159-178`), and defensive response-shape unpacking (`182-194`).

**Fix:**
```python
def _build_vision_request(image_bytes: bytes, content_type: str) -> dict: ...   # lines 120-136

def _dispatch_vision_request(endpoint, headers, body):
    if app_settings.MULTI:
        return pinned_request("POST", endpoint, timeout=TIMEOUT_SECONDS, headers=headers, json=body)
    with unpinned_client(timeout=TIMEOUT_SECONDS) as client:
        return client.post(endpoint, headers=headers, json=body)

def _extract_alt_text(parsed) -> str: ...   # lines 187-194, returns "" on any shape mismatch
```
**Effort: S.** **Post-refactor CC estimate: ~9-10** — config guards, the SSRF branch, and retry/exception handling appropriately remain in the orchestrator.

### 1.12 bluesky.py's 4 HTTP-call functions — new finding, not in the DRY audit — Importance: 5/10

Reading `create_session` (`188-222`, CC 10), `refresh_session` (`225-...`, CC 12), `upload_blob` (`420-467`, CC 12), and `create_post` (`470-...`, CC 12) together surfaced a same-file duplication+complexity cluster the earlier DRY audit's destination-modules pass didn't catch (it looked at cross-file HTTP-status duplication in discord/matrix/webhook, not within bluesky.py). All 4 share the identical shape: POST via `pinned_request` in try/except → `BlueskyError`, a status-code ladder (401-or-token-error → `BlueskyAuthError`, ≥400 → `BlueskyError`), `response.json()` in try/except `ValueError`, then field-extraction+validation.

**Fix:**
```python
def _bluesky_call(method, url, *, error_context: str, auth_error_on_401=True, **kwargs) -> dict | None:
    try:
        response = pinned_request(method, url, **kwargs)
    except (httpx.RequestError, SSRFError) as exc:
        raise BlueskyError(f"Could not reach {error_context}") from exc
    detail = _error_detail(response)
    if auth_error_on_401 and (response.status_code == 401 or _is_token_error(detail)):
        raise BlueskyAuthError(f"{error_context} rejected{f' ({detail})' if detail else ''}")
    if response.status_code >= 400:
        raise BlueskyError(f"{error_context} failed (HTTP {response.status_code}){f' ({detail})' if detail else ''}")
    try:
        return response.json()
    except ValueError as exc:
        raise BlueskyError(f"{error_context} returned an invalid response") from exc
```
**Effort: S-M.** **Post-refactor CC estimates:** `create_session` 10→~4, `refresh_session` 12→~4, `upload_blob` 12→~5 (keeps type/size pre-checks), `create_post` 12→~5. **Also add this to `audits/CODE_DUPLICATION_AUDIT.md`** as a cross-reference — it was found during this pass, not the original DRY audit.

### 1.13 `resolve_pds` — `bluesky.py:97-167` (CC 15, grade C) — Importance: included in §1.12's area, not separately scored

Nesting depth 3 at `153-159`. Branches into two entirely different network protocols (did:web fetches a well-known JSON document; did:plc queries a directory service) inside one function.

**Fix:**
```python
def _fetch_did_document(did: str, handle: str) -> dict: ...      # lines 123-144
def _pds_from_did_document(doc: dict, handle: str) -> str: ...   # lines 150-161
```
**Effort: S.** **Post-refactor CC estimate: ~7-8.**

### 1.14 Functions where CC overstates real difficulty — do not prioritize refactoring these

- **`_store_feed_items`** (`scheduler.py:278-344`, CC 18) — Importance: **1/10**. Flat, linear; no nesting beyond one `if not item_id: continue`. The CC comes almost entirely from ~10 `item.get(x) or ""` fallback expressions. **Recommendation: do not refactor.** Kept in this report specifically as a worked example so the CC metric isn't applied mechanically elsewhere.
- **`reader_compose_preview`** (`app.py:5056-5110`, CC 20) — Importance: **3/10**. Only 3 real decision points; the rest is 14 `x or ""` null-coalesces and 2 ternaries inside two dict literals. The actual fix is the `_feed_item_to_dict` extraction already covered under §1.3 (this function builds the same dict shape as `reader_compose`); once that's shared, this function's own CC drops to ~4-5 as a side effect.
- **`_check_feed_with_lease`** (`scheduler.py:361-511`, CC 26) — Importance: **3/10**. Max nesting only 3 levels; most of the CC is 8 sequential, independent early-return guard clauses (paused, no-echoes, fetch-failed, no-items, cursor-uninitialized, etc.) — already about as simple as this branching can be. The one real extraction is the two structurally-identical delivery loops (`454-479` and `493-503`) into one `_deliver_items(feed_id, lease_token, echoes, items, feed_name, *, advance_cursor: bool)`. **Post-refactor estimate: ~16-18** (guard clauses stay).
- **`admin_email_save`** (`app.py:1551-1601`, CC 18) and **`oauth_callback`** (`app.py:5820-5898`, CC 16) — Importance: **2/10**. Both are flat chains of independent `if <bad input>: return error` guards, max nesting 2. Read top-to-bottom like a checklist; splitting further would hurt readability. `admin_email_save`'s CC will drop by ~4 automatically once `[DUP §3.4]`/`[DUP §3.6]` (admin-guard and error-render helpers) are applied — no standalone action needed here.
- **`_validate_payload`** (`import_export.py:154-203`, CC 23) — Importance: **2/10**. Three independent `for` loops each with one shallow `if...: raise` guard — legitimately flat validation. Marginal win (~23→~14) available by factoring into `_validate_records(items, kind, id_required=True)`, but low priority.

### 1.15 `_flush_digests` — `scheduler.py:2699-2865` (CC 23, grade D, 167 lines) — Importance: 5/10

Max nesting depth 3. Contains a genuine **bin-packing algorithm** building the digest body under a byte budget with an edge case for a single oversized item (`2738-2778` — the hardest part to read, due to budget arithmetic and a "degenerate case" comment at `2765-2770`), sandwiched between SQL fetch and two different bookkeeping blocks for failure vs. success.

**Fix:**
```python
def _pack_digest_body(echo_row, items) -> tuple[str, list, list]:
    """Pure function: builds (body, sent_items, held_items) under DIGEST_MAX_CHARS.
    Isolates the size-budget algorithm so it's unit-testable without a DB or send_email."""
    ...

def _finalize_digest_failure(db, echo_id, sent_items, exc) -> None: ...   # lines 2819-2833
def _finalize_digest_success(db, echo_id, sent_items) -> None: ...        # lines 2837-2856
```
**Effort: S-M.** **Post-refactor CC estimate: ~10-12.** This is the highest-value single extraction in the scheduler cluster after the `_send_X` functions — it turns an algorithm with no isolated test coverage today into a pure function that can get direct unit tests (e.g. "one item exceeds the budget alone" is exactly the kind of edge case that deserves a dedicated test and currently can't get one without spinning up the DB and email layers).

---

## 2. Cognitive complexity

Cyclomatic complexity counts branches; cognitive complexity is about how hard the *reader* has to work, which the manual reads above targeted directly. Cross-cutting cognitive-load patterns found across multiple functions:

- **Nesting depth 5+ recurs in exactly the same shape** (image-attachment pipelines) across `_send_mastodon`, `_send_bluesky`, `_send_matrix`, `_send_microblog` — once a reader learns to parse one, the other three are the same shape with different API calls. This is a case where fixing one function's readability (via extraction) effectively fixes four.
- **Mixed abstraction levels** — raw SQL sitting beside HTTP-response formatting beside business-rule validation in the same function body — is the dominant driver in `reader_page`, `import_data`, `_flush_queue`, `register_submit`, and `generate_alt_text`. In every one of these, the fix is the same shape: pull the SQL/HTTP/validation into named single-purpose helpers so the orchestrator reads as a list of steps.
- **No recursive functions were found** anywhere in the non-test codebase (confirmed via the AST walk used for the LOC metrics — no `FunctionDef` node calls its own name in its body across the 515 functions analyzed). Recursion is not a contributor to this codebase's complexity.
- **Guard-clause chains (flat, low cognitive load despite high CC)** appear in `_check_feed_with_lease`, `admin_email_save`, `oauth_callback`, and `_validate_payload` — these are the functions in §1.14 where the CC score should be discounted.
- **Two unrelated algorithms gated by one boolean, in the same function** — `login_submit`'s `if not settings.MULTI` split (§1.10) is the clearest example: single-mode and multi-mode auth share a function but not a code path, which is worse for a reader than two short functions plus a 2-line dispatcher.

---

## 3. Lines-of-code metrics

### 3.1 Files over 300 lines

| File | Lines | Note |
|---|---|---|
| `app.py` | 5,925 | God-file — see §5.1. MI grade **C (0.00)**, the worst in the repo. |
| `scheduler.py` | 2,983 | Large but cohesive (single responsibility: feed-poll + destination-dispatch pipeline) — see §5.2. MI grade **C (0.00)**. |
| `database.py` | 1,414 | Dominated by two enormous-but-flat schema functions (§1.8). MI grade **A (36.58)** — deceptively "acceptable" only because MI rewards low CC; the raw size is still a maintainability cost. |
| `feed_parser.py` | 1,213 | MI grade **C (8.96)**, second-worst in the repo — driven by the image-extraction cluster (§1.7) and several other C/D-grade functions (`get_backdated_items` CC14, `validate_outbound_url` CC13, `parse_json_feed` CC13). |
| `auth.py` | 628 | MI grade **A (43.07)** but low within its grade band — `register_submit`/`login_submit` are the drivers (§1.9, §1.10). |
| `bluesky.py` | 553 | MI grade **A (33.51)**, lowest "A" in the repo — driven by §1.12/§1.13. |
| `matrix.py` | 547 | MI grade **A (38.94)** — `normalize_room` (CC 11) is its only function over the C-grade threshold; not deep-dived in this pass (lower priority than the destination modules already covered). |
| `import_export.py` | 511 | MI grade **A (34.38)** — driven entirely by §1.2 and §1.14's `_validate_payload`. |
| `webhook.py` | 354 | MI grade **A (46.09)** — `parse_headers` (CC14), `build_payload` (CC13), `normalize_webhook_url` (CC11) are its complexity contributors; not deep-dived here (see `[DUP §3.3]` for the naming-collision finding on `normalize_webhook_url`). |
| `settings.py` | 347 | MI grade **A (59.94)** — `_load_plan_limits` (CC13) is its only notable function. |

Files under 300 lines are not flagged; none showed complexity metrics warranting inclusion.

### 3.2 Functions over 50 lines (69 functions found; top 20 shown, exact via AST)

| Lines | Function | Location |
|---|---|---|
| 608 | `init_db_sqlite` | `database.py:293` |
| 446 | `init_db_postgres` | `database.py:967` |
| 349 | `reader_page` | `app.py:2467` |
| 243 | `_flush_queue` | `scheduler.py:2280` |
| 213 | `_send_bluesky` | `scheduler.py:1422` |
| 207 | `reader_compose` | `app.py:5115` |
| 189 | `_send_matrix` | `scheduler.py:1759` |
| 185 | `import_data` | `import_export.py:327` |
| 167 | `_flush_digests` | `scheduler.py:2699` |
| 158 | `_send_mastodon` | `scheduler.py:1126` |
| 151 | `_check_feed_with_lease` | `scheduler.py:361` |
| 151 | `register_submit` | `auth.py:218` |
| 124 | `import_opml` | `app.py:4403` |
| 120 | `_send_microblog` | `scheduler.py:1637` |
| 118 | `queue_post_now` | `app.py:1892` |
| 117 | `_render_and_dispatch` | `scheduler.py:900` |
| 117 | `dashboard` | `app.py:1183` |
| 113 | `generate_alt_text` | `alt_text.py:100` |
| 112 | `add_echo` | `app.py:5395` |
| 111 | `edit_echo` | `app.py:5528` |

All functions above CC≥18 from §1 also appear in this list — length and branching are correlated here, as expected, with the two schema-init functions in `database.py` as the sole exception (huge but flat, §1.8).

**`queue_post_now`** (`app.py:1892-2009`, CC 22, 118 lines, not separately sectioned above): contains an exact internal duplication — the `INSERT INTO echoes (...)` block is copy-pasted verbatim at `1926-1932` and `1935-1941` (both branches of `if row["echo_id"] ... else` build an echo the same way when none exists). Fix:
```python
def _get_or_create_oneshot_echo(db, row, uid, now_utc) -> tuple[int, sqlite3.Row]:
    if row["echo_id"]:
        echo = db.execute("SELECT * FROM echoes WHERE id = ?", (row["echo_id"],)).fetchone()
        if echo:
            return echo["id"], echo
    echo_id = db.execute(
        """INSERT INTO echoes (feed_id, destination_type, destination_id, template,
                               visibility, attach_image, enabled, one_shot, deleted_at, user_id)
        VALUES (?, ?, ?, '{{ title }}', ?, ?, 0, 1, ?, ?) RETURNING id""",
        (row["feed_id"], row["destination_type"], row["destination_id"],
         row["visibility"] or "public", row["attach_image"], now_utc, uid),
    ).fetchone()["id"]
    echo = db.execute("SELECT * FROM echoes WHERE id = ?", (echo_id,)).fetchone()
    if not row["echo_id"]:
        db.execute("UPDATE queued_posts SET echo_id = ? WHERE id = ?", (echo_id, row["id"]))
    return echo_id, echo
```
Importance: **4/10**, Effort: **S**. Post-refactor CC estimate: ~14 (the surrounding claim/dispatch/status-update flow is sequential, not nested).

### 3.3 Classes over 500 lines

**None found.** The largest class in the codebase is `AuthMiddleware` (`app.py:300-541`) at 242 lines / 7 methods — well under the 500-line threshold. This codebase is predominantly procedural/functional (module-level functions, not classes), so the "classes over 500 lines" check is **N/A** as a category here. Full class inventory (all classes, largest first): `AuthMiddleware` (242, 7 methods), `RequestIdMiddleware` (59, 1 method), `PinningNetworkBackend` (48, 6 methods, `feed_parser.py:175`), `SecurityHeadersMiddleware` (33, 1 method), `_PinnedTransport` (28, 2 methods), `CappedSandbox` (27, 2 methods), `CSRFOriginMiddleware` (27, 2 methods), `_PgConnection` (17, 5 methods) — every other class in the repo is a small exception type (0-1 methods).

### 3.4 `AuthMiddleware` — `app.py:300-541` (242 lines, 7 methods) — Importance: 3/10

Read in full. **Cohesion is good** — all 7 methods (`dispatch`, `_path_is_routed`, `_token_matches`, `_single`, `_session_user`, `_pending_card`, `_multi`) serve one responsibility (deciding whether/how to admit a request), with comments explaining *why* specific branches exist (e.g. the documented 404-vs-login-redirect fix at `373-377`). Single-mode and multi-mode are genuinely different algorithms sharing helper primitives, not unrelated concerns bolted together — **splitting this into separate classes would relocate complexity, not reduce it.**

One real, minor duplication: `_single`'s tail (`446-456`: not-routed passthrough → Accept-header branch → redirect-or-401) is near-identical to `_multi`'s tail (`532-540`).
```python
@staticmethod
def _unauthenticated_response(request: Request):
    if not AuthMiddleware._path_is_routed(request):
        return None  # caller falls through to call_next
    if "text/html" in request.headers.get("accept", "") and request.method == "GET":
        return RedirectResponse(url="/login", status_code=302)
    return JSONResponse({"detail": "Authentication required"}, status_code=401)
```
Effort: **S**. Minor CC reduction (~2-3 each in `_single`/`_multi`); not a priority relative to §1's findings.

---

## 4. Coupling metrics

Computed from a full `ast`-based import graph over all 25 local top-level modules (afferent Ca = number of local modules that import this one; efferent Ce = number of local modules this one imports; instability I = Ce/(Ce+Ca), where 0 = maximally stable/depended-upon, 1 = maximally unstable/dependent).

| Module | Ce | Ca | Instability | Note |
|---|---|---|---|---|
| `app` | 21 | 1 | 0.95 | Entry point — see caveat below |
| `scheduler` | 16 | 1 | 0.94 | Highest *internal* efferent coupling — §4.2 |
| `auth` | 8 | 1 | 0.89 | Ca=1 only because of the circular-import workaround — §4.3 |
| `oauth` | 4 | 1 | 0.80 | |
| `import_export` | 3 | 1 | 0.75 | |
| `alt_text` | 4 | 2 | 0.67 | |
| `notify` | 2 | 1 | 0.67 | |
| `email_sender` | 4 | 4 | 0.50 | |
| `verification` | 2 | 2 | 0.50 | |
| `webhook` | 2 | 2 | 0.50 | |
| `logging_setup` | 1 | 1 | 0.50 | |
| `bluesky`/`discord`/`invites`/`mastodon`/`matrix`/`microblog` | 1 each | 2 each | 0.33 | Uniform shape — each destination module depends only on `feed_parser`, and is depended on only by `app`+`scheduler` |
| `database` | 2 | 8 | 0.20 | Stable — depended on by 8, depends on only `security`+`settings` |
| `plans` | 1 | 4 | 0.20 | Stable |
| `security` | 1 | 9 | 0.10 | Very stable — near the bottom of the dependency graph |
| `settings` | 0 | 13 | 0.00 | Maximally stable — the most depended-upon module, imports nothing local |
| `feed_parser` | 0 | 11 | 0.00 | Maximally stable |
| `template_engine` | 0 | 2 | 0.00 | |
| `filters` | 0 | 1 | 0.00 | |
| `utils` | 0 | 0 | 0.00 | New module from the duplication audit; not yet imported anywhere (by design — see `[DUP §6]`) |

**Positive finding (Importance: N/A):** `settings.py` (Ca=13, Ce=0) and `feed_parser.py` (Ca=11, Ce=0) sit at the stable base of the dependency graph and depend on nothing else locally — exactly where foundational modules should sit in a well-factored codebase. `security.py` (Ca=9, Ce=1) is nearly as stable. This is evidence the *low-level* module boundaries are sound even where the high-level ones (below) are not.

### 4.1 Instability caveat for `app.py`

`app.py`'s I=0.95 looks alarming by the textbook formula, but this is an artifact of it being the **process entry point** — nothing is expected to import it (Ca would be 0 in a typical Python web app), so the metric's usual interpretation ("high instability = risky to depend on this, but nothing depends on it so that's fine") actually holds here. **What Ca=1 actually is** is not a normal import — it's the circular-dependency workaround described next.

### 4.2 `scheduler.py` — highest genuine (non-entry-point) efferent coupling — Importance: 5/10

`scheduler.py` imports 16 of the other 24 local modules (`alt_text, bluesky, database, discord, email_sender, feed_parser, filters, mastodon, matrix, microblog, notify, plans, security, settings, template_engine, webhook`) — it is the "orchestrator hub" that knows about every destination type and every cross-cutting concern. This is architecturally consistent with `[DUP §3.1]`'s finding (the 7 `_send_X` functions are the reason for most of these imports — one per destination module) and with §1.5/§1.6 above (the same functions are also the complexity hot spot). **Reducing this coupling is not a separate task from the complexity fixes in §1.5-§1.6** — extracting the shared dispatch skeleton naturally reduces how much destination-specific knowledge lives directly in `scheduler.py`'s top-level namespace, though it will still need to import all 7 destination modules by nature of being the dispatcher.

### 4.3 Circular dependency: `app.py` ↔ `auth.py` — Importance: 6/10

Verified: `app.py` imports from `auth.py` at module load time (top-level, per the import graph). `auth.py` cannot import `app.py` at module level for the same reason, so it works around this with a **deferred, function-local import**:
```python
# auth.py:192-195
def _render_auth(request: Request, template: str, status_code: int = 200, **kwargs):
    from app import render
    return render(template, request, status_code=status_code, **kwargs)
```
This is the only such site in the codebase (confirmed via `grep -rn "from app import" *.py`). It works, but it's a real architectural smell: `render()` (the templating entry point) living in `app.py` rather than in a module both `app.py` and `auth.py` can import from top-level is *why* the cycle exists. It also means `auth.py` cannot be imported/tested in isolation without `app.py` successfully importing first (both modules load fully at process start today, so this isn't a live bug — but it constrains any future attempt to split `app.py`, which is exactly what §5.1 recommends).

**Fix:** move `render()` (and whatever template-environment setup it depends on) out of `app.py` into `template_engine.py` (which already exists and both `app.py` and `auth.py` could import from without a cycle — `template_engine.py` currently has Ce=0, Ca=2, so it's a safe place to add a dependency). Then `auth.py:193`'s deferred import becomes a normal top-level `from template_engine import render`.

**Effort: S-M** — moving `render()` is likely a small, mechanical change, but **needs verification**: confirm `render()`'s current implementation in `app.py` doesn't reach back into other `app.py`-local state (route table, middleware config) that would reintroduce the cycle. Grep `def render` in `app.py` to check before moving.

---

## 5. Cohesion analysis

### 5.1 `app.py` — low cohesion, a god-file — Importance: 7/10

`app.py` contains **109 route handlers**, **4 middleware classes**, and **159 top-level definitions** in one 5,925-line file. Grouping the 109 routes by top-level URL prefix shows at least 13 largely-independent resource domains living in one file: reader (12 `/api/reader*` routes + `reader_page`/`reader_compose`), feeds (11), settings (5), queue (5), folders (4), echoes (4), 7 separate destination-account CRUD groups (mastodon/email/bluesky/microblog/matrix/discord/webhook — 3 routes each), saved-searches (3), history (2), plus admin (11 routes), auth-adjacent pages (login/register/logout/forgot/reset — 9 routes), and static/legal pages (about/terms/privacy/pricing/howto — 5 routes). This is not "one big module with a clear purpose" (contrast with `scheduler.py`, §5.2) — it's **the union of everything that needs an HTTP route**, which is why it's also the single file responsible for 3 of this report's top-4 worst functions (`reader_page`, `reader_compose`, `add_echo`/`edit_echo`) and the sole file with MI grade C (0.00).

**Fix (structural, not a quick snippet):** split by resource domain into per-area route modules (e.g. `routes_reader.py`, `routes_accounts.py`, `routes_admin.py`, `routes_settings.py`) each exposing an `APIRouter`/route-registration function that `app.py` imports and mounts, keeping only app construction, middleware registration, and cross-cutting helpers (like `render()`, once moved per §4.3) in `app.py` itself. This is the single highest-leverage structural change available in this codebase, but it's large — **Effort: L**, and should be planned as its own project (with the per-function fixes in §1 done first, since a smaller `add_echo`/`edit_echo`/`reader_page` will be much easier to relocate than the current versions).

### 5.2 `scheduler.py` — large but cohesive — contrast case, no action needed

Despite being the second-largest file (2,983 lines, 53 top-level definitions), every one of `scheduler.py`'s functions serves the same responsibility: the scheduled feed-poll → item-store → destination-dispatch → retry/digest/drip pipeline. There is no unrelated concern mixed in (no HTTP routing, no admin logic, no auth). Its high efferent coupling (§4.2) is a *consequence* of legitimately needing to know about every destination type to do its one job, not a symptom of doing multiple jobs. **This module does not need to be split for cohesion reasons** — its problems are the individual function complexity issues in §1.5-§1.6 and §1.15, not its file-level organization.

### 5.3 Destination modules (`bluesky.py`, `discord.py`, `mastodon.py`, `matrix.py`, `microblog.py`, `webhook.py`) — cohesive, uniform, well-bounded

Each of these has an identical coupling shape (Ce=1 [only `feed_parser`], Ca=2 [only `app`+`scheduler`]) and each is focused solely on one platform's API integration. This is the intended design unit boundary in this codebase and it's working — no cohesion concerns found here; the complexity issues found within them (§1.12, §1.13, and `[DUP]`'s findings) are internal-to-the-module duplication/complexity, not misplaced responsibility.

### 5.4 Single-responsibility adherence — summary

Of the 8 largest files, 7 are internally cohesive (single clear responsibility, even where individual functions are too complex): `scheduler.py`, `database.py` (schema + query helpers), `feed_parser.py` (feed fetching/parsing/SSRF-safety), `auth.py` (authentication flows), `bluesky.py`/`matrix.py`/`import_export.py` (each one clear job). **`app.py` is the sole exception** and is respectively the largest file, the file with the worst 3 functions in the report, and the only file with the worst possible MI grade — all three facts point at the same root cause (§5.1), not three separate problems.

---

## Appendix: methodology notes and what was ruled out

- **Recursion:** exactly one recursive function in the non-test codebase, independently verified via an `ast`-based self-call check: the nested `walk(node, current_folder_id, depth)` inside `import_opml` (`app.py:4456-...`, part of the 124-line `import_opml` at `app.py:4403`), which recursively walks OPML `<outline>` nodes with an explicit `depth > 10: return` guard (`4458-4459`) and a `total_outlines > 1000` circuit-breaker (`4464-4465`). This is a legitimate, appropriately-bounded use of recursion for tree-shaped input (OPML folders can nest) — **not a complexity finding**, Importance N/A, no action recommended. Everywhere else in the codebase, "recursive calls" as a cognitive-complexity dimension is N/A.
- **Switch-statement complexity:** Python has no `switch`/`match`-statement usage in the codebase (the one `grep` hit for `match` is a variable named `match` holding a `re.search()` result at `feed_parser.py:1135`, not a `match` statement — confirmed false positive). This dimension maps onto the `if/elif` chain findings already covered (§1.4's 7-arm destination-type chain, §1.10's mode dispatch, etc.) rather than being a separate category.
- **radon-measured CC vs. hand-verified post-refactor estimates:** every "post-refactor CC estimate" in this report is a manual estimate from reading the proposed split, not a radon measurement of an actually-refactored function — confirming them precisely requires performing the extraction and re-running `radon cc` on the result. Flagged inline at each estimate rather than stated as measured fact.
- **Files not deep-dived in this pass** (flagged by radon but below the cutoff used to select fork targets): `matrix.py`'s `normalize_room` (CC11), `discover_base_url` (CC9, B-grade); `webhook.py`'s `parse_headers` (CC14), `build_payload` (CC13), `normalize_webhook_url` (CC11, and see `[DUP §3.3]`); `settings.py`'s `_load_plan_limits` (CC13); `security.py`'s `read_session` (CC13). **Needs verification** if a full sweep is wanted — these are all C-grade (CC 11-20), lower priority than the D/E/F-grade functions this report focused on, but not zero.
- **Test-file complexity** was excluded from scope (this audit targets production code); a handful of C/D-grade test functions exist (e.g. `tests/test_reader_pagination.py:48` at CC23/D) but are not included in the findings or summary table above.
