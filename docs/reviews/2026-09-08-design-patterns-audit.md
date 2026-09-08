# FeedEcho — Design Pattern Usage Audit

**Date:** 2026-09-08
**Scope:** All Python modules at commit `3dcf392` (v1.49.0), excluding `tests/`. Findings below (including line numbers) are a snapshot as of that commit — see [§ Adoption outcome](#adoption-outcome-2026-09-08-post-audit) at the end for what has since landed on master and how line numbers have shifted.
**Method:** Direct reads of every module discussed below plus targeted greps to confirm/deny pattern presence (searched for `@dataclass`/`NamedTuple`/`BaseModel`/`TypedDict`, `Protocol`/`ABC`/`abstractmethod`, `lru_cache`/module-level cache dicts, middleware registration, and exception-class import graphs). No pattern below is asserted without a cited line range.
**Companion documents:** `docs/reviews/2026-09-08-code-duplication-audit.md` and `docs/reviews/2026-09-08-code-complexity-audit.md` (both 2026-09-08) — several findings here are the *pattern* lens on issues those reports already found from the duplication/complexity angle; cross-referenced as `[DUP §x.x]` / `[CX §x.x]`. The duplication audit's own adoption record (its §7) is required reading before acting on any finding below that overlaps its scope — several of its "rejected after full read" entries directly bear on findings here (see the outcome section).

**How to read this report:** GoF/architectural patterns are tools, not obligations — a pattern's *absence* is only a finding when the code pays a real, cited cost for not having it. Several sections below conclude "absent, and that's correct" (Observer, Builder-as-a-class, a payment Strategy). Those are marked **N/A (appropriate)** and are not refactoring recommendations.

---

## Summary

| # | Finding | Category | Verdict | Importance |
|---|---|---|---|---|
| 1 | No Strategy/Factory registry for the 7 destination types — dispatch is a hand-copied if/elif chain in 5+ places | Creational + Behavioral | Missing, cheap to add | **9/10** |
| 2 | No Repository layer — 243 raw `db.execute()` calls directly in `app.py` route handlers, 80 more in `scheduler.py` | Domain | Missing | **8/10** |
| 3 | 6 destination modules define independent exception hierarchies with no shared base class | Structural (Adapter) | Incomplete | **6/10** |
| 4 | No DTO/value objects anywhere — zero `@dataclass`/`NamedTuple`/`TypedDict`/Pydantic model in production code | Domain | Missing | **5/10** |
| 5 | Service layer exists for auth/plans/invites/verification/notify, but is 100% absent for feeds/echoes/7 account types/folders/saved-searches/queue/settings | Domain | Inconsistent | **5/10** |
| 6 | `_saved_search_counts_cache` (Proxy/cache pattern) invalidated manually at 9 separate call sites | Structural (Proxy) | Fragile implementation | **5/10** |
| 7 | `test_connection()` is implemented 6 times with the same name/return type but incompatible per-platform argument lists — an informal, not formal, Adapter | Structural (Adapter) | Incomplete | **4/10** |
| 8 | Single-mode vs. multi-mode auth is a Strategy-shaped problem implemented as inline branching, not swappable strategies | Behavioral (Strategy) | Works, could be cleaner | **4/10** |
| 9 | No DB connection pooling — a legitimate place for a Proxy pattern | Creational/Structural | Missing (perf, not correctness) | **3/10** |
| 10 | `_PgConnection` — correctly implemented, self-documented Adapter | Structural (Adapter) | **Correct, no action** | N/A |
| 11 | Logger configuration — correctly implemented idempotent Singleton | Creational (Singleton) | **Correct, no action** | N/A |
| 12 | No singleton DB connection — each `get_db()` call opens a fresh connection | Creational (Singleton) | **Correctly absent** | N/A |
| 13 | Starlette middleware stack — correctly implemented Decorator **and** Chain of Responsibility, deliberately ordered with an explanatory comment | Structural/Behavioral | **Correct, no action** | N/A |
| 14 | `_route_index()` — well-implemented memoizing Proxy | Structural (Proxy) | **Correct, no action** | N/A |
| 15 | `queued_posts`/`digest_items`/`drip_items` tables are an implicit, appropriately-simple Command pattern (persisted, deferred-execution work items) | Behavioral (Command) | **Appropriate, no action** | N/A |
| 16 | No Observer/pub-sub system | Behavioral (Observer) | **Appropriately absent** | N/A |
| 17 | No payment/Strategy pattern for billing | Behavioral (Strategy) | **Out of repo scope by design** | N/A |
| 18 | No GoF Builder class anywhere; simple builder *functions* (`build_payload`, `build_embed`, `build_facets`) are used instead | Creational (Builder) | **Appropriate, no action** | N/A |

---

## 1. Creational patterns

### 1.1 Singleton — database connections: correctly **absent** (Importance: N/A, positive finding)

`database.py:140-168`'s `get_db()` is a `@contextmanager` that opens a **fresh connection on every call** — SQLite: `sqlite3.connect(...)` at line 157; Postgres: `_pg_connect()` at line 149 — and closes it in a `finally` block. There is no global/singleton connection object anywhere in the codebase (confirmed: no `global` connection variable, no module-level connection instance).

**This is the correct choice, not a missing pattern.** A singleton shared connection would be actively wrong here: FastAPI/Starlette handles concurrent requests on a thread pool (sync routes) and an event loop (async routes), and SQLite/psycopg connections are not safe to share across concurrent transactions without external locking. If you evaluated this codebase against "should there be a Singleton connection?" the answer is no — confirm this understanding is correct before ever "fixing" it.

**Related, distinct performance concern (not a Singleton problem):** each Postgres connection is opened and torn down per request with no pooling — already flagged as LOW in the project's own `docs/reviews/2026-08-28-kimi-full-repo-review.md` item #33 and again in `[CX summary]`. See §9 below — the correct pattern to add here is a connection-pool **Proxy**, not a Singleton.

### 1.2 Singleton — logger: correctly implemented (Importance: N/A, positive finding)

`logging_setup.py:38-71`'s `setup_logging()` is a textbook idempotent-initialization Singleton: a module-level `_configured` flag (`logging_setup.py:29`) guards against re-configuring the root logger, with an explicit docstring explaining *why* idempotency matters ("repeated TestClient startups in one pytest process don't stack handlers," lines 39-45). It also correctly delegates the actual singleton *registry* to Python's own `logging.getLogger()` machinery rather than reinventing one. No action needed.

### 1.3 Factory / Strategy — missing destination-type registry — Importance: 9/10 (the highest-value finding in this report)

There is no Factory or Strategy object for "the thing that knows how to post to a destination type." Instead, the same 7-way enumeration (`mastodon`/`email`/`bluesky`/`microblog`/`matrix`/`discord`/`webhook`) is hand-copied as **at least 5 independent if/elif or lookup structures**, each already documented as a duplication/complexity problem in its own right, but never previously framed as "this is what a Strategy registry is for":

1. `scheduler.py:946-1008` (`_render_and_dispatch`) — a 62-line if/elif chain dispatching to `_send_mastodon`/`_send_email_echo`/`_send_bluesky`/`_send_microblog`/`_send_matrix`/`_send_discord`/`_send_webhook`. `[CX §1.5]` already flagged this function's CC.
2. `app.py:5438-5469` and `app.py:5568-5599` (`add_echo`/`edit_echo`) — the same 7-arm elif chain resolving which form field holds the destination id. `[CX §1.4]` already flagged this as ~90% duplicate between the two functions.
3. `templates/echoes.html:223-235` — a 7-branch Jinja `{% if %}/{% elif %}` mirroring the same enumeration. `[DUP §3.10]`.
4. `app.py:162-171` (`DESTINATION_TABLES_BY_TYPE`) — a **data-only** registry (type → table name), proving the team already reaches for a dict here; it just never grew into a **behavior** registry (type → handler).
5. `import_export.py:41-55` (`_ACCOUNTS`) — a separately-maintained fourth enumeration of the same 7 types. `[DUP §3.10]`.

**This is unusually cheap to fix**, because the 7 `_send_X` functions in `scheduler.py` already have (with one exception) **identical signatures** — verified directly:
```
_send_mastodon(echo, item, content, account_id, posted_id, claim_token) -> bool
_send_email_echo(echo, item, content, email_account_id, posted_id, claim_token) -> bool
_send_bluesky(echo, item, content, account_id, posted_id, claim_token) -> bool
_send_microblog(echo, item, content, account_id, posted_id, claim_token) -> bool
_send_matrix(echo, item, content, account_id, posted_id, claim_token) -> bool
_send_discord(echo, item, content, account_id, posted_id, claim_token) -> bool
_send_webhook(echo, item, content, feed_name, account_id, posted_id, claim_token) -> bool  # one extra positional arg
```

**Fix:**
```python
# scheduler.py, near the top-level constants
_DESTINATION_HANDLERS = {
    "mastodon": _send_mastodon,
    "email": _send_email_echo,
    "bluesky": _send_bluesky,
    "microblog": _send_microblog,
    "matrix": _send_matrix,
    "discord": _send_discord,
}

def _render_and_dispatch(echo, item, content, feed_name, posted_id, claim_token, echo_id):
    ...  # existing setup (lines 900-945) unchanged
    handler = _DESTINATION_HANDLERS.get(echo["destination_type"])
    if handler is None:
        if echo["destination_type"] == "webhook":
            return _send_webhook(echo, item, content, feed_name, echo["destination_id"], posted_id, claim_token)
        return _fail_post(posted_id, claim_token, echo_id, f"Unknown destination type: {echo['destination_type']}")
    return handler(echo, item, content, echo["destination_id"], posted_id, claim_token)
```
(`_send_webhook`'s extra `feed_name` parameter is the one signature outlier — either special-case it as above, or — cleaner — reorder its signature to match the other six, moving `feed_name` out of the positional list into a keyword-only param so all 7 fit one calling convention: `handler(echo, item, content, echo["destination_id"], posted_id, claim_token, feed_name=feed_name)` with the other 6 functions accepting `**_ignored` or an explicit unused `feed_name=None` — recommend the signature-alignment approach since it removes the special case entirely.)

Once `_DESTINATION_HANDLERS` exists as the canonical type→behavior registry, it can also back `DESTINATION_TABLES_BY_TYPE` (already exists, just needs handlers added as a second field) and give `import_export.py`'s `_ACCOUNTS` and the `add_echo`/`edit_echo` elif chains one place to import from instead of five independent lists to keep in sync.

**Effort: M** (the registry itself is a 10-line addition; the payoff comes from also migrating `add_echo`/`edit_echo` and `import_export.py` onto it, which `[CX §1.4]` and `[DUP §3.10]` already scoped as separate M-effort tickets — this finding says "do them as one unified registry, not four independent fixes").

### 1.4 Builder — no GoF Builder class; simple builder *functions* used instead (Importance: N/A, appropriate)

The codebase constructs several non-trivial objects — Discord embeds (`discord.py`'s `build_embed`, CC 7 per `[CX]`), webhook JSON payloads (`webhook.py`'s `build_payload`, CC 13), Bluesky rich-text facets (`bluesky.py`'s `build_facets`, CC 8), and digest email bodies (the size-budget algorithm inside `_flush_digests`, `scheduler.py:2699-2865`, which `[CX §1.15]` recommends extracting as a pure `_pack_digest_body` function). None of these use a stateful, chainable Builder class (`.with_x().with_y().build()`).

**This is the right call, not a gap.** Every one of these objects is built from a fixed, known set of inputs in one pass — there's no scenario in this codebase where construction steps are optional, reorderable, or need to be assembled incrementally across multiple call sites. A GoF Builder earns its complexity when a class has many optional constructor parameters or when the same construction process needs to yield different representations; none of that applies here. A plain function that takes the needed inputs and returns the finished object is the simpler, correct choice. **No refactoring recommended.**

---

## 2. Structural patterns

### 2.1 Adapter — `_PgConnection` (`database.py:108-124`) — correctly implemented (Importance: N/A, positive finding)

Self-documented in its own docstring: *"Thin adapter over psycopg3 so callers keep writing `db.execute(sql, ?)`."* It translates psycopg3's connection interface to look like sqlite3's (`execute`/`commit`/`rollback`/`close`), including running SQL text through `qmark()` (`database.py:25`) to convert `?`-style placeholders — the one thing sqlite3 and psycopg don't agree on. This is exactly what an Adapter is for, correctly scoped to the one interface gap that actually exists between the two drivers. No action needed.

### 2.2 Adapter — destination modules: informal, not formal (Importance: 4/10)

Six destination modules (`bluesky.py`, `discord.py`, `mastodon.py`, `matrix.py`, `microblog.py`, `webhook.py`) each expose a `test_connection(...)` function with the same name and the same return contract (`tuple[bool, str]`), confirmed by direct grep:
```
bluesky.py:536   def test_connection(handle: str, app_password: str) -> tuple[bool, str]:
discord.py:290   def test_connection(webhook_url: str) -> tuple[bool, str]:
mastodon.py:123  def test_connection(instance: str, access_token: str) -> tuple[bool, str]:
matrix.py:523    def test_connection(...) -> tuple[bool, str]:
microblog.py:209 def test_connection(token: str) -> tuple[bool, str]:
webhook.py:317   def test_connection(url: str, headers: dict[str, str]) -> tuple[bool, str]:
```
This is an Adapter pattern **by naming convention only** — each function takes different, credential-shape-specific positional arguments, so nothing can call `test_connection` uniformly across destination types without already knowing, per type, which fields to unpack from the account row. Contrast with `scheduler.py`'s `_send_X` functions (§1.3), which already *are* uniform on `(echo, item, content, account_id, posted_id, claim_token)` — the inconsistency is specifically in the `test_connection` family, which is exactly the set of functions `app.py`'s 6 `test_*_account` routes call (`[DUP §1.4]` cluster 6 already found these routes ~85% duplicate for this reason).

**Fix — align on the account-row shape already used elsewhere:**
```python
# each module, e.g. discord.py:
def test_connection(account: dict) -> tuple[bool, str]:
    return _test_connection_impl(account["webhook_url"])
```
Or, more directly useful given `zero formal interface exists today`, declare the shared contract explicitly with `typing.Protocol` (confirmed via grep: `Protocol`/`ABC`/`abstractmethod` do not appear anywhere in production code — the one `Protocol` grep hit in `bluesky.py` is the AT Protocol, not `typing.Protocol`):
```python
# a new module, e.g. destinations.py, or utils.py
from typing import Protocol

class DestinationAdapter(Protocol):
    def test_connection(self, account: dict) -> tuple[bool, str]: ...
    def send(self, echo, item: dict, content: str, account_id: int, posted_id: int, claim_token: str) -> bool: ...
```
Modules don't need to literally implement a class for `Protocol` to add value — it's structural typing, so the existing module-level functions can be wrapped in a small adapter object per destination that satisfies the Protocol, giving `app.py`'s test-account routes and `scheduler.py`'s dispatch (§1.3) one typed contract instead of two separately-inconsistent conventions.

**Effort: M** — touches 6 modules' public signatures plus the `app.py` call sites already identified in `[DUP §1.4]` cluster 6.

### 2.3 Adapter completeness — no shared exception base class across destination modules (Importance: 6/10)

Each destination module defines its own independent exception hierarchy: `BlueskyError`/`BlueskyAuthError` (`bluesky.py:38,42`), `MicroblogError`/`MicroblogAuthError` (`microblog.py:31,35`), `MatrixError`/`MatrixAuthError`/`MatrixPermissionError` (`matrix.py:69,73,77`), `DiscordError`/`DiscordAuthError`/`DiscordNotFoundError`/`DiscordBadRequestError` (`discord.py:61,80,84,88`), `WebhookError`/`WebhookAuthError`/`WebhookNotFoundError`/`WebhookRejectedError` (`webhook.py:62,77,81,85`). The naming *convention* is consistent (translating each platform's raw errors into an Auth/NotFound/RateLimit/Rejected vocabulary is exactly what an Adapter should do) — but **there is no shared base class**, confirmed by reading `scheduler.py`'s imports (`scheduler.py:29-77`): it imports all ~14 of these exception names individually, with no common ancestor to catch generically.

This is precisely why `[DUP §3.1]`'s "3-tier exception ladder (AuthError→permanent fail, DestinationError→fail with message, bare Exception→generic fail)" has to be **re-implemented per destination module** instead of written once — Python's `except` can't catch "any destination's auth error" without a shared base to catch.

**Fix:**
```python
# a new module, e.g. destination_errors.py, or add to utils.py
class DestinationError(Exception):
    """Base for all destination-adapter failures; catch this for the generic fail path."""

class DestinationAuthError(DestinationError):
    """Credentials rejected — permanent failure, no point retrying."""

class DestinationNotFoundError(DestinationError):
    """Target (channel/room/blog/webhook) no longer exists."""

class DestinationRateLimitError(DestinationError):
    """Caller should back off; carries retry_after where known."""
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after
```
Then each module's existing exception becomes `class BlueskyError(DestinationError): ...`, `class BlueskyAuthError(BlueskyError, DestinationAuthError): ...` (multiple inheritance keeps each module's own catch sites working unchanged while adding the shared ancestor). `scheduler.py`'s 3-tier exception ladder can then be written once as a shared helper instead of once per `_send_X` function.

**Effort: M** — the exception class changes themselves are small and backward-compatible (multiple inheritance means existing `except BlueskyAuthError` call sites keep working), but validating the ladder consolidation needs the full test suite (SQLite + Postgres) per the project's own methodology.

### 2.4 Facade — `_render_and_dispatch` is an implicit Facade; quality follows §1.3's fix

`scheduler.py:900-1018` (`_render_and_dispatch`) is the single entry point every caller uses to post an item without knowing which of the 7 destination modules is involved — that's a Facade, and it's the right shape (one function, hides 7 subsystems). Its internals are exactly the if/elif chain from §1.3; fixing that dispatch mechanism does not change the Facade's boundary, only its implementation. No separate action beyond §1.3.

### 2.5 Facade absent between HTTP routes and the database — see §4.1 (Domain: Repository)

There is no Facade/Service layer standing between `app.py`'s route handlers and raw SQL for the CRUD-heavy domains (feeds, echoes, the 7 account types, folders, saved searches, queue, settings). This is really a Repository/Service-layer gap rather than a Facade gap in the classic sense (a Facade simplifies a complex subsystem; here there's no subsystem at all standing between the route and the table) — covered in full at §4.1/§4.2 to avoid double-counting the same evidence under two names.

### 2.6 Decorator (and Chain of Responsibility) — Starlette middleware stack: correctly implemented (Importance: N/A, positive finding)

`app.py:728-733` registers 4 middleware classes:
```python
app.add_middleware(AuthMiddleware)
app.add_middleware(CSRFOriginMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
# Outermost: request-id threading + access logging wraps everything.
app.add_middleware(RequestIdMiddleware)
```
Each middleware (`BaseHTTPMiddleware` subclass) wraps `call_next` and can short-circuit or pass through — textbook Decorator, and simultaneously textbook Chain of Responsibility (each link decides whether to handle the request itself or defer to the next). Starlette's `add_middleware` builds the stack in reverse call order (last-added = outermost), and the comment at line 732 shows the team understands and relies on this: request flow is `RequestIdMiddleware → SecurityHeadersMiddleware → CSRFOriginMiddleware → AuthMiddleware → route`. That ordering is sound — request-id is assigned before anything else can log, security headers wrap every response including ones from inner failures, and origin/CSRF checks happen before spending effort on authentication. **No action needed**, cited here only because the task asked to evaluate Decorator/middleware explicitly and it deserves a clean bill of health.

### 2.7 Proxy — `_route_index()` (`app.py:207-236`) — well-implemented memoizing Proxy (Importance: N/A, positive finding)

Caches the split of literal vs. parameterized routes, keyed by `id(app)` and invalidated by comparing `len(routes)` against the cached length (`app.py:214`) — a correct, self-contained invalidation check with no external call sites needing to remember to bust it. Well-commented on *why* (`app.py:202-206`): route-table changes only happen once at startup (`billing.mount`), so length-based invalidation is sufficient and cheap. **No action needed** — cited as the positive contrast to §2.8.

### 2.8 Proxy — `_saved_search_counts_cache` (`app.py:2394`) — functionally correct, fragile invalidation (Importance: 5/10)

A 60-second, `max_item_id`-keyed cache for saved-search unread counts (`app.py:2701-2775`, confirmed by direct read): `if cached and cached[0] == max_item_id and (now_time - cached[1]) < 60: use cached`. The invalidation *logic* is sound (a cheap version-stamp plus a time bound), but unlike §2.7, invalidation is **not self-contained** — it additionally relies on `_saved_search_counts_cache.pop(uid, None)` being called at every mutation site, and that call is duplicated **9 times** across the file: `app.py:3061, 4069, 4104, 4127, 4785, 4802, 4858, 4886, 4914` (confirmed by grep). Any future route that mutates saved searches, feeds, or read-state and forgets to add a 10th `.pop()` call produces stale counts silently — not a crash, just wrong numbers, which is a worse failure mode because nothing will flag it.

**Fix:** wrap the cache in a tiny class so invalidation is one method instead of a copy-pasted literal:
```python
class _SavedSearchCountsCache:
    def __init__(self):
        self._store: dict[int, tuple[int, float, dict[int, int]]] = {}

    def get(self, uid: int, max_item_id: int, now: float, ttl: int = 60):
        cached = self._store.get(uid)
        if cached and cached[0] == max_item_id and (now - cached[1]) < ttl:
            return cached[2]
        return None

    def set(self, uid: int, max_item_id: int, now: float, counts: dict[int, int]) -> None:
        self._store[uid] = (max_item_id, now, counts)

    def invalidate(self, uid: int) -> None:
        self._store.pop(uid, None)

_saved_search_counts_cache = _SavedSearchCountsCache()
```
This doesn't reduce the 9 call sites that must remember to invalidate — that's inherent to write-invalidated caching — but it does mean each call site reads `_saved_search_counts_cache.invalidate(uid)` (one obvious name to grep for when adding a new mutation route) instead of a bare `.pop(uid, None)` that looks like ordinary dict housekeeping and is easy to miss when reviewing a diff.

**Effort: S.**

### 2.9 Proxy — no database connection pooling (Importance: 3/10)

Every `get_db()` call opens and tears down a connection (§1.1). For SQLite this is cheap (local file, WAL mode); for the hosted Postgres path this is a real per-request latency cost, already flagged as LOW in the project's prior review. A connection pool (e.g. `psycopg_pool.ConnectionPool`) is a legitimate Proxy pattern application here — it would sit behind `_pg_connect()` and hand out pooled connections transparently, with `get_db()`'s call sites unchanged. **Not urgent** (correctness is unaffected; this is a latency/scale concern), but flagged since the audit explicitly asks about Proxy/caching and this is the one clear gap in that category with an established, low-risk library solution. **Effort: S-M**, needs a decision on pool sizing and idle-connection lifetime before deploying, not just a code change.

---

## 3. Behavioral patterns

### 3.1 Strategy — destination dispatch — see §1.3 (Creational and Behavioral overlap)

The `_render_and_dispatch` if/elif chain (§1.3) is simultaneously a missing Factory (creating the right handler) and a missing Strategy (selecting the right posting algorithm) — GoF treats these as separate patterns but the fix is the same dict-based registry either way. Not re-scored here to avoid double-counting; see §1.3 for the importance rating and fix.

### 3.2 Strategy — auth mode (single vs. multi-tenant) — Importance: 4/10

`auth.py:377-457` (`login_submit`) and `app.py:300-541` (`AuthMiddleware`, specifically `_single` at line 426 and `_multi` at line 505) both branch on `settings.MULTI` to run one of two **entirely different authentication algorithms** (shared-secret token comparison vs. email+password+session) inside one function/class rather than as two swappable strategy implementations. `[CX §1.10]` already recommends splitting `login_submit` into `_login_single_mode`/`_login_multi_mode` behind a 2-line dispatcher — that fix is exactly "extract the two strategies out of the branch," so this finding is the pattern-language framing of the same fix already scoped there, not an additional one. `AuthMiddleware`'s `_single`/`_multi` split (`[CX §3.4]`) is **already implemented as separate methods**, closer to Strategy already — the remaining gap is that `login_submit` (the route handler) hasn't been split the same way its middleware counterpart has.

**Note on whether formal Strategy classes are warranted:** given there are only ever two modes, selected once at process startup from a config flag (not swapped at runtime per-request), function-level extraction (as `[CX §1.10]` recommends) is sufficient — introducing `AuthStrategy` classes with a common interface would be over-engineering for a boolean that never changes after startup. **Recommendation: apply `[CX §1.10]`'s fix; no additional class hierarchy needed.**

### 3.3 Chain of Responsibility — middleware — see §2.6 (positive, no action)

### 3.4 Chain of Responsibility — guard-clause validation chains — appropriate as-is (Importance: N/A)

`[CX §1.14]` already identified that `admin_email_save`, `oauth_callback`, and `_check_feed_with_lease` implement validation as flat chains of independent early-return guard clauses. This is Chain-of-Responsibility *in spirit* (each check either rejects or passes through) implemented as straight-line code rather than as a linked list of handler objects — which is the right call at this scale (a handful of fixed, non-reorderable checks per function). A real CoR object chain would only pay for itself if the set of checks needed to be dynamically composed or reordered at runtime, which none of these do. **No action needed** — cited to close out the CoR evaluation the task asked for, distinct from the middleware finding.

### 3.5 Observer — absent, and appropriately so (Importance: N/A)

`notify.py`'s `record_failure`/`record_success` (`notify.py`, imported into `scheduler.py:78-83`) are called directly and imperatively wherever a post succeeds or fails — there is no event bus, no subscriber list, no `Observable`/`EventEmitter` abstraction anywhere in the codebase (confirmed: no such class or pattern found). Every "thing that happens when a post fails" (record the failure count, maybe send a notification email) is a fixed, small, known set of side effects called directly from `_fail_post`/`_update_post`. **Introducing a pub/sub Observer here would be premature abstraction** — it would pay off if third-party plugins or a growing list of unrelated side effects needed to hook into post-success/failure without `scheduler.py` knowing about them in advance, but nothing in the current codebase or its documented roadma indicates that need. **No action recommended.**

### 3.6 Command — the queue tables are an appropriate, implicit Command pattern (Importance: N/A, positive finding)

`queued_posts`, `digest_items`, and `drip_items` (schema in `database.py`, populated by `scheduler.py`'s `_queue_for_digest`/`_queue_for_drip`/queue-related functions) are, in effect, serialized Command objects: each row captures "post this item to this destination" as data, to be executed later by `_flush_queue`/`_flush_digests`/`_flush_drips`. This achieves everything a formal `Command` class hierarchy (`execute()`, `undo()`, command history) would provide for this use case — deferred execution, retry, and persistence across process restarts — without the overhead of an object hierarchy, because the "commands" here are homogeneous (always "deliver this item") rather than needing polymorphic `execute()` implementations. **This is the right level of abstraction for a single-process background scheduler** (this is not Celery/a distributed task queue with heterogeneous job types, where a formal Command class would earn its keep). **No action recommended.**

### 3.7 Payment/billing Strategy — out of repo scope by design (Importance: N/A)

Verified via `settings.py:231-239`: *"The actual payment routes (`/api/billing/*`) are NOT part of this repo — the hosted deployment mounts them from its private billing module — so this flag only toggles the front-end seam and never implies Stripe here."* `BILLING_ENABLED` (`settings.py:239`) is a pure feature-flag seam, not a payment Strategy implementation, and isn't meant to be one. **There is nothing to evaluate here** — the task's mention of "payment processing" as a Strategy-pattern example doesn't apply to this codebase; note this explicitly rather than inventing a finding to fill the category.

---

## 4. Domain patterns

### 4.1 Repository pattern — absent — Importance: 8/10

Quantified via direct grep of `.execute(` call sites: `app.py` contains **243** raw SQL execute calls directly inside route handlers; `scheduler.py` contains 80; `import_export.py` 12; `notify.py` 9; `verification.py` 7; `invites.py`/`oauth.py` 5 each; `auth.py` 8. `database.py` — the one module that could plausibly be a Repository layer — exposes **no entity-level CRUD functions** (no `get_feed_by_id`, `create_echo`, `update_account`, etc.); its actual top-level functions are `dialect()`, `qmark()`, `as_utc_naive()`/`timestamp_str()`, `get_db()`, schema/migration helpers (`_column_names`, `_has_unique_on`, `_add_column_if_missing`, `_dedupe_discord_webhook_hashes`), one specific utility (`prune_feed_items`), and the two schema-init functions. Every route handler in `app.py` writes and executes its own SQL directly against `feeds`/`echoes`/`accounts`/`bluesky_accounts`/etc.

This is the domain-layer restatement of `[CX §5.1]`'s "app.py is a 5,925-line god-file" finding — the *reason* every route handler is long and tangled with SQL is that there is no layer absorbing that SQL on their behalf.

**Fix (illustrative — this is a large, structural change, not a snippet-sized one):**
```python
# a new module, e.g. repositories/feeds.py
def get_feed(db, uid: int, feed_id: int) -> dict | None:
    return db.execute(
        "SELECT * FROM feeds WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
        (feed_id, uid),
    ).fetchone()

def list_feeds(db, uid: int) -> list[dict]:
    return db.execute(
        "SELECT * FROM feeds WHERE user_id = ? AND deleted_at IS NULL ORDER BY name",
        (uid,),
    ).fetchall()

def soft_delete_feed(db, uid: int, feed_id: int, now: str) -> None:
    db.execute(
        "UPDATE feeds SET deleted_at = ? WHERE id = ? AND user_id = ?",
        (now, feed_id, uid),
    )
```
Route handlers then call `feeds.get_feed(db, uid, feed_id)` instead of inlining the SQL. This also directly resolves the exact-duplicate SQL bug `[DUP §1.1]` found (Bluesky/Micro.blog delete routes hand-rolling `_dependent_echo_count`'s query) — with a Repository layer, that duplication is structurally impossible because there's only one place the query could live.

**Effort: L.** This is the single largest structural recommendation across all three audits and should be planned as its own project, most naturally *after* `[CX §5.1]`'s route-module split (a smaller, already-modularized `app.py` makes it much easier to introduce a repository per domain incrementally, one route module at a time, rather than as one big-bang rewrite).

### 4.2 Service layer — present for some domains, absent for others — Importance: 5/10

`auth.py`, `plans.py`, `invites.py`, `verification.py`, and `notify.py` already function as thin service modules: they encapsulate business rules (password validation, plan-limit checks, invite consumption, token verification, failure-notification thresholds) separately from `app.py`'s routes, and `app.py` calls into them rather than reimplementing the logic inline. This is a real, working partial Service Layer.

**The gap:** every CRUD-heavy domain — feeds, echoes, the 7 destination-account types, folders, saved searches, the post queue, and per-user settings — has **zero** service-layer separation; all of their business logic (validation, ownership checks, cap enforcement) is inlined directly in the `app.py` route handlers alongside the SQL (§4.1). This inconsistency means a reader has to know, module by module, whether a given piece of logic lives in a service function or is inlined in the route — there's no single rule to follow.

**Fix:** as domains are migrated onto the Repository layer (§4.1), pull their validation/business-rule code into a matching service function in the same pass (e.g. `services/echoes.py` housing `_validate_destination_ref`/`_resolve_destination_id` from `[CX §1.3]`/`[CX §1.4]`, calling into `repositories/echoes.py` for persistence) — don't do the repository and service extractions as two separate passes over the same code.

**Effort: L** (bundled with §4.1's effort, not additive).

### 4.3 DTO / value objects — absent — Importance: 5/10

Confirmed via repo-wide grep: **zero** occurrences of `@dataclass`, `NamedTuple`, `TypedDict`, or `BaseModel` (Pydantic) in production code. Every domain concept — an echo, a feed, an account, a feed item — is passed around as a raw `sqlite3.Row` (or `dict` for `_PgConnection`'s `dict_row` factory) with no static shape guarantee. This is felt most acutely at exactly the boundary §1.3 recommends formalizing: `_send_X(echo, item, content, account_id, posted_id, claim_token)` — a reader (or a static type checker) has no way to know what keys `echo`/`item` are expected to carry without reading the function body or tracing back to the `SELECT *` that produced the row.

**This is not a blanket "add dataclasses everywhere" recommendation** — for a codebase this size, converting every `SELECT` result to a full domain class would be a large mechanical change for a benefit that's mostly about IDE/type-checker ergonomics rather than fixing a live bug. The **targeted** recommendation is to add lightweight `TypedDict`s exactly where cross-function/cross-module contracts already exist informally, starting with the dispatch boundary from §1.3:
```python
from typing import TypedDict

class FeedItem(TypedDict, total=False):
    id: str
    title: str
    content: str
    summary: str
    published_at: str
    image_url: str
    # ... remaining known keys from feed_parser.py's item dicts
```
`TypedDict` costs nothing at runtime (pure static-typing sugar — no behavior change, no migration risk) and can be introduced incrementally, function-signature by function-signature, without touching how rows are actually fetched. **Effort: S per boundary** (start with the `_send_X`/`_render_and_dispatch` boundary alongside §1.3's registry fix, since both changes touch the same function signatures at the same time).

### 4.4 Domain model — anemic, and mostly appropriately so — Importance: not separately scored (folds into §4.1-§4.3)

Business rules (`plans.py`'s limit checks, `scheduler.py`'s `_drip_limit`/`_drip_applies`/`_drip_rate`, the digest-packing algorithm inside `_flush_digests`) are free functions operating on raw dicts/Rows rather than methods on domain objects (an "Echo" class with a `.drip_limit()` method, etc.). For a codebase of this size and shape (a handful of business rules, not a rich domain with dozens of interacting entities), an anemic-model-plus-free-functions design is a **reasonable, appropriate choice** — introducing full DDD-style entities would be over-engineering unless the team has concrete plans to grow the business-rule surface significantly. The parts of this that are worth improving are already captured more precisely above: give the data flowing between functions a typed shape (§4.3) and give the persistence a proper seam (§4.1) — those two changes get most of the practical benefit of "a domain model" without requiring a rewrite into classes.

---

## Appendix: what was checked and ruled out without a separate section

- **Prototype pattern:** not applicable to this codebase — no object cloning/copying use case was found (checked via grep for `copy.deepcopy`/`__copy__`/`clone` — none found in production code beyond routine dict copies). Not scored as a finding; there is no evidence this pattern is needed anywhere.
- **Visitor pattern:** not applicable — no heterogeneous object-tree traversal exists that would benefit from double-dispatch (the closest candidate, OPML import's recursive `walk()` at `app.py:4456`, per `[CX Appendix]`, operates on a single homogeneous node shape and doesn't need it).
- **Mediator pattern:** `_render_and_dispatch` (§2.4) already serves as a lightweight mediator between `scheduler.py`'s flush functions and the 7 destination modules; not a gap once §1.3's registry fix lands.
- **Interpreter pattern:** the saved-search query mini-language (`_parse_reader_query`, referenced in `[CX §1.1]`'s `reader_page` analysis) is a plausible Interpreter-pattern candidate, but a shallow read shows it's a simple token-splitter, not a grammar needing recursive interpretation — **needs verification** if a deeper look is wanted; not read in full for this audit.

---

## Adoption outcome (2026-09-08, post-audit)

Between this audit's initial pass and implementation, master moved ~30 commits (v1.49.0 → v1.50.1) on an unrelated, independently-run adoption of the companion duplication audit (`docs/reviews/2026-09-08-code-duplication-audit.md`, its own §7). That work already extracted `scheduler.py`'s shared destination-dispatch *skeleton* (`_destination_account`/`_echo_attach_image`/`_resolve_alt_text`/`_guard_claim`/`_finalize_success`) — re-verified before starting this work that it did **not** touch the if/elif dispatch chain itself (§1.3 here remained live) or add any exception base classes (§2.3 remained live). All findings below were re-confirmed against current file content (not assumed from the original snapshot) before implementation.

**Landed (this PR):**
- **§1.3** (destination-dispatch registry): `scheduler.py` — `_DESTINATION_HANDLERS` dict maps 6 of 7 destination types to their `_send_X` function (all share the same signature); `webhook` stays a special case ahead of the dict lookup since it alone needs `feed_name`. `_render_and_dispatch`'s 62-line if/elif chain is now ~10 lines. Zero behavior change — same functions, same call order, same fallback to `_fail_post` for an unknown type.
- **§2.3** (shared exception base classes): added `DestinationError`/`DestinationAuthError`/`DestinationNotFoundError`/`DestinationRateLimitError` to `utils.py`. Each of the 5 destination modules' existing exception classes (bluesky/discord/matrix/microblog/webhook) now additionally inherit the matching shared base via multiple inheritance — verified every resulting MRO resolves cleanly and that instance attributes set in existing custom `__init__`s (e.g. `DiscordRateLimitError.retry_after`) are unaffected. Every pre-existing `except BlueskyAuthError`-style call site keeps working unchanged; this is purely additive. One deliberate judgment call beyond the audit's original text: `MatrixPermissionError` was folded into `DestinationAuthError` (not left orphaned) after confirming in `scheduler.py` that it's already handled identically to `MatrixAuthError` (both `permanent=True`, non-retryable) — `DestinationAuthError`'s docstring was broadened from "credentials rejected" to "rejected credentials or insufficient permission" to make that inclusion honest.
- **§4.3** (TypedDict at the dispatch boundary): added `FeedItem` (`TypedDict, total=False`) to `utils.py`, built from `feed_parser.py`'s actual `parse_rss_feed`/`parse_json_feed` item-dict keys (verified directly, not the field names guessed in this document's original §4.3 snippet — e.g. the real key is `date`, not `published_at`). Applied as the type of every `item: dict` parameter in `scheduler.py` (14 sites). `total=False` and a docstring caveat because a backdated/drip-redelivered item reconstructed from the `feed_items` table is not verified to carry every key the fresh parse-time dict does. Zero runtime behavior change (`from __future__ import annotations` means annotations are never evaluated at runtime; `item["key"]` access is identical for `dict` and `TypedDict`).
- **§2.8** (`_saved_search_counts_cache`): wrapped the bare module-level dict in a small `_SavedSearchCountsCache` class (`.get(uid, max_item_id, now)` / `.set(...)` / `.invalidate(uid)`) in `app.py`, preserving the exact same version-stamp-plus-60s-TTL invalidation logic. All 9 `.pop(uid, None)` call sites (confirmed still 9, unchanged from the original audit's count) now read `.invalidate(uid)`.
- **§3.2** (auth-mode Strategy split, `login_submit`): split into `_login_single_mode(request, token)` and `_login_multi_mode(request, email, password)` behind a 2-line dispatcher in `auth.py`, exactly as this audit and `[CX §1.10]` both recommended. The public `login_submit(request, email, password, token)` signature is unchanged, so `app.py`'s route wrapper (which calls it by keyword) required no changes.

**Reconsidered, not implemented:**
- **§2.2** (`test_connection` signature alignment across bluesky/discord/mastodon/matrix/microblog/webhook): re-scoped out during implementation. Aligning these 6 functions onto one `test_connection(account: dict)` shape would touch 6 production modules' public signatures plus 12 call sites (6 in `app.py`, 6 in tests) for a purely architectural/typing benefit — no bug fix, no duplication reduction. The duplication audit's own adoption record already rejected the closely-related §2.7 finding (merging these same 6 functions' *internal* try/except skeleton) with the same reasoning: "the mechanical savings don't justify" the churn. Applying that same bar here, the signature-alignment version of the idea doesn't clear it either. Left as a documented, deliberately-skipped recommendation rather than silently dropped.

**Not attempted in this PR (out of scope, large L-effort structural work):**
- **§4.1** (Repository pattern) and **§4.2** (Service layer): confirmed still absent (243 raw `db.execute()` calls in `app.py` as of this snapshot; not re-counted post-adoption since neither this nor the duplication-audit adoption touched that surface). These remain the correct next large project — see §4.1's own text for why they should be sequenced after `[CX §5.1]`'s route-module split, not attempted as one big-bang change.
- **§9** (DB connection pooling): not in this PR's scope; unchanged from the audit's original recommendation.

**Verification:** full test suite (`pytest`, SQLite) run before any change (1497 passed, 36 skipped) and after all five landed changes (1497 passed, 36 skipped, identical) — no regressions. Targeted destination-module, scheduler, reader/saved-search, and auth test files were also run individually after each change before the final full-suite pass. Postgres-dialect tests were not run locally (no local Postgres instance); none of the five changes touch dialect-specific code paths (`database.py`'s SQLite/Postgres branches were not modified).
