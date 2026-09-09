# FeedEcho — Error Handling Audit

**Date:** 2026-09-08
**Scope:** All Python modules at commit `1624c68` (master, post-PR #17), excluding `tests/`. See [§ Adoption outcome](#adoption-outcome-2026-09-08-post-audit) at the end for what was implemented from this report.
**Stack note:** FeedEcho is a synchronous/async-hybrid FastAPI (Starlette) app with an in-process APScheduler background scheduler — there is no Node.js/JS runtime in the request path, so two categories in the brief map onto this stack differently than their Express/Node framing suggests: "unhandled promise rejections" → unhandled `asyncio` Task exceptions, and "event emitter error handling" → APScheduler job-execution error handling (there is no `EventEmitter` idiom here). Both are evaluated under their Python-equivalent below rather than skipped.
**Method:** direct reads of every cited location plus repo-wide greps (`HTTPException(status_code=`, `exception_handler`, `except Exception`, `asyncio.create_task`, `logger.exception`/`exc_info=True`) and one heuristic scan (flagging `except Exception` blocks with neither a traceback-capturing log call nor a `raise` in the following 10 lines) — every heuristic hit was individually read and reclassified below; several were false positives (the heuristic's window missed a `raise` a few lines further down) and are noted as such rather than reported as findings.

---

## Summary

| # | Finding | Category | Importance |
|---|---|---|---|
| 1 | No handler for unhandled exceptions (500) — falls through to Starlette's bare, unbranded default page | Consistency / Categories | **7/10** |
| 2 | `PlanError` and other domain exceptions are converted to HTTP responses at 11 separate call sites with no shared translation layer (though the varying response shapes turn out to be individually justified — see finding detail) | Consistency | **3/10** |
| 3 | `forbidden_handler` (403) hardcodes "Admin access required," discarding `exc.detail` | Consistency / Information | **4/10** |
| 4 | `CSRFOriginMiddleware` returns a bare `Response(status_code=403)` — empty body, bypasses the 403 handler and all branding | Consistency / Information | **5/10** |
| 5 | 401/429/502 use raw `Response`/`JSONResponse` in places rather than `raise HTTPException` — two parallel response-construction mechanisms exist, though the one seemingly-odd JSON shape found is confirmed intentional (matches a JS call site), not drift | Consistency | **2/10** |
| 6 | Inconsistent logging completeness for the same failure category: registration verification-email failure (WARNING, no traceback) vs. password-reset email failure (ERROR + full traceback) | Information | **5/10** |
| 7 | Image-fetch helper in `feed_parser.py` swallows every exception with zero logging | Information | **5/10** |
| 8 | `test_*_connection`-family functions swallow exceptions into a user-facing string with inconsistent breadth (some catch broadly and mask real bugs, some catch narrowly) | Information / Consistency | **4/10** |
| 9 | `alt_text.py`'s retry loop retries non-retryable 4xx (e.g. a bad API key) identically to transient errors, with a fixed (not exponential) delay | Recovery | **3/10** |
| 10 | `asyncio.create_task(...)` fire-and-forget email dispatch doesn't hold a reference to the task | Async | **2/10** |
| 11 | A benign unhandled-thread-exception warning appears during scheduler shutdown in tests | Async | **2/10** |
| 12 | Per-feed/per-item exception isolation in the background scheduler, with full traceback logging | Async / Recovery — **positive** | N/A |
| 13 | Retry-with-backoff + consecutive-failure-threshold alerting + recovery notification in `notify.py` | Recovery — **positive** | N/A |
| 14 | Fallback-proxy pattern for feed and image fetches on HTTP 403/429 | Recovery — **positive** | N/A |
| 15 | No `debug=True` anywhere — production never risks leaking tracebacks via Starlette's `ServerErrorMiddleware` | Information — **positive** | N/A |
| 16 | Circuit breaker pattern absent | Recovery — **appropriately absent** | N/A |
| 17 | Custom exception hierarchies (per-destination `DestinationError` family, `PlanError`, `SSRFError`, etc.) are well-named and consistently structured | Categories — **positive** | N/A |

---

## 1. Error handling consistency

### 1.1 No centralized handler for unhandled exceptions (500) — Importance: 7/10

`app.py` registers exactly two exception handlers:
```python
# app.py:5882-5894
@app.exception_handler(404)
async def not_found_handler(request: Request, exc: HTTPException): ...

@app.exception_handler(403)
async def forbidden_handler(request: Request, exc: HTTPException): ...
```
There is no `@app.exception_handler(500)` and no `@app.exception_handler(Exception)` (confirmed via `grep -n "exception_handler" app.py` — only these two matches exist). The `FastAPI()` app is constructed with no `debug` argument (`app.py:99`: `app = FastAPI(title="FeedEcho", version=APP_VERSION)`), so Starlette's `ServerErrorMiddleware` runs in its default (non-debug) mode for any exception that isn't a registered `HTTPException`/handler — which returns Starlette's built-in **plain-text** `Internal Server Error` response, not the app's own `templates/error.html` used for 403/404, and not JSON for API clients. Every other error page in the app (`404.html`, `error.html`) is branded and offers a "back to dashboard" / "log in" link (`templates/error.html:1-11`); a 500 gets none of that.

This is not a security gap (see finding 15 — no traceback leaks either way) — it's purely a consistency/UX gap: the one error category most likely to actually happen in production (an unexpected bug) is the one category with no branded handling.

**Fix:**
```python
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # RequestIdMiddleware (app.py:259-275) already logs this exception with
    # full context (method, path, duration, peer, user id) before re-raising,
    # so this handler's job is only to shape the response, not to log again.
    if request.headers.get("accept", "").startswith("application/json"):
        return JSONResponse({"detail": "Internal server error"}, status_code=500)
    return render("error.html", request, status_code=500,
                  code=500, message="Something went wrong. Please try again.")
```
Registering this does **not** disable the existing traceback logging: `RequestIdMiddleware.dispatch` (`app.py:257-275`) already catches, logs, and re-raises every exception before it reaches this handler, so the fix is additive — it only changes what the client sees, not what gets logged. **Effort: S.**

### 1.2 Domain-exception-to-HTTP translation is manual at every call site, but this turns out to be justified — Importance: 3/10 (downgraded after reading the call sites)

`PlanError` (raised by `plans.py`) is caught at **11 separate sites** in `app.py` (confirmed via `grep -n "except PlanError" app.py`: lines 3136, 3203, 3271, 3505, 3623, 3716, 4025, 4315, 4463, 5164, 5867). At first glance this looks like a missing `@app.exception_handler(PlanError)` — but reading the sites shows the response shape genuinely varies by caller and a single global handler couldn't produce all three correctly:
- Some sites re-render a form-error partial: `except PlanError as e: return _render_accounts_error(request, str(e))` (e.g. `app.py:3136`, `3271`) or `_render_oauth_error` (`app.py:5867`).
- Some sites convert to a JSON 402: `except PlanError as e: raise HTTPException(status_code=402, detail=str(e))` (`app.py:4025-4026`, `4315-4316`, `5164-5165` — these three are byte-for-byte identical).
- One site (`app.py:4463`, inside the OPML-import loop) silently increments a `capped` counter and `continue`s — correct behavior for a bulk import where one over-cap item shouldn't abort the whole batch.

**This is good, context-appropriate design, not a gap** — a form submission needs a re-rendered form, a JSON API route needs a 402, and a bulk-import loop needs a per-item skip; a centralized handler could only pick one shape. The one real, minor finding is the 3-way exact duplication of `except PlanError as e: raise HTTPException(status_code=402, detail=str(e))`, which is small enough (3 lines × 3 sites) that it's a duplication-audit item, not an architecture gap:
```python
# a small local helper, not a global handler:
def _plan_error_to_http(e: PlanError) -> HTTPException:
    return HTTPException(status_code=402, detail=str(e))
```
**Effort: S**, low priority — noted for completeness, not because it's costing anything today.

### 1.3 `forbidden_handler` hardcodes its message instead of using `exc.detail` — Importance: 4/10

```python
# app.py:5889-5894
@app.exception_handler(403)
async def forbidden_handler(request: Request, exc: HTTPException):
    if request.headers.get("accept", "").startswith("application/json"):
        return JSONResponse({"detail": "Admin access required"}, status_code=403)
    return render("error.html", request, status_code=403,
                  code=403, message="Admin access required")
```
`exc` is accepted but never read. Today this is harmless by coincidence: the only production call site that raises `HTTPException(status_code=403, ...)` is `_require_admin` (`app.py:1391`, `raise HTTPException(status_code=403, detail="Admin access required")`), whose detail happens to match the handler's hardcoded string exactly (confirmed via `grep -n "status_code=403" app.py auth.py oauth.py security.py` — no other `HTTPException(403)` site exists). But this is a landmine: the moment anyone adds a second 403 raise site with a different reason, the handler will silently discard it and always show "Admin access required," misinforming the user about why they were blocked.

**Fix:**
```python
@app.exception_handler(403)
async def forbidden_handler(request: Request, exc: HTTPException):
    message = exc.detail if isinstance(exc.detail, str) else "Forbidden"
    if request.headers.get("accept", "").startswith("application/json"):
        return JSONResponse({"detail": message}, status_code=403)
    return render("error.html", request, status_code=403, code=403, message=message)
```
**Effort: S.**

### 1.4 `CSRFOriginMiddleware` bypasses the 403 handler and all branding entirely — Importance: 5/10

```python
# app.py:687-695
class CSRFOriginMiddleware(BaseHTTPMiddleware):
    _UNSAFE = frozenset({"POST", "PUT", "PATCH", "DELETE"})

    async def dispatch(self, request: Request, call_next):
        if request.method in self._UNSAFE and not self._same_origin(request):
            return Response(status_code=403)
        return await call_next(request)
```
This returns a bare `Response(status_code=403)` — no body, no `Content-Type`, no `detail` key, no rendered page. Because it's a `Response` object returned directly from middleware (not a raised `HTTPException`), it never reaches `forbidden_handler` (finding 1.3) at all — this is a *third*, even more minimal, code path for producing a 403, on top of the `HTTPException`-based one. A legitimate user whose form submission gets cross-origin-rejected (the middleware's own comment at `app.py:676-685` describes real false-positive-prone heuristics — Referrer-Policy-hidden headers, privacy tools) sees a totally blank page with no explanation.

**Fix:**
```python
async def dispatch(self, request: Request, call_next):
    if request.method in self._UNSAFE and not self._same_origin(request):
        if "text/html" in request.headers.get("accept", ""):
            return render("error.html", request, status_code=403, code=403,
                          message="Your request could not be verified. Please try again.")
        return JSONResponse({"detail": "Cross-origin request rejected"}, status_code=403)
    return await call_next(request)
```
Requires `render` to be resolvable at the point `CSRFOriginMiddleware` is defined (`app.py:687`) — `render` is defined earlier in `app.py` (used throughout the file), so this is a same-module call, not a new import. **Effort: S.**

### 1.5 401/429/502 partly use raw `Response`/`JSONResponse` instead of `raise HTTPException` — Importance: 2/10

`AuthMiddleware` returns 401 directly: `JSONResponse({"detail": "Authentication required"}, status_code=401)` (`app.py:455-457`, and identically at `541`), and `preview_template`'s rate limit returns 429 directly: `JSONResponse({"success": False, "error": "..."}, status_code=429)` (`app.py:5709-5712` — note this response also uses a different JSON key, `error` instead of `detail`, from every other error response in the app). Meanwhile every 400/402/404/409/502 in the app (117 sites total, confirmed via `grep -oE "HTTPException\(status_code=[0-9]+" app.py | sort | uniq -c`) goes through `raise HTTPException(...)`.

This isn't broken — both mechanisms work and the 401 shape (`{"detail": ...}`) accidentally matches FastAPI's default `HTTPException` JSON shape. The `{"success": False, "error": ...}` shape at `app.py:5711` initially looks like a one-off inconsistency, but is **confirmed intentional, not a bug**: `static/js/app.js:495` explicitly reads `data.error` from the preview endpoint's response (`box.innerHTML = ...${escapeHTML(data.error || resp.statusText)}...`), and the sibling 502 site in the same `/api/preview` handler (`app.py:5737`, §2's table) uses the identical `{"success": False, "error": ...}` shape — this is a deliberate, scoped contract for that one feature's JS integration, not drift. **Do not change this key** — doing so would break the preview UI's error rendering. The only remaining, genuinely low-value observation is that two parallel response-construction mechanisms exist in the codebase (raised `HTTPException` vs. directly-returned `Response`/`JSONResponse`) for a future maintainer to be aware of; no code change is recommended here. **Effort: N/A — no fix needed; downgrade this finding to informational.**

---

## 2. Error categories

Verified via `grep -oE "HTTPException\(status_code=[0-9]+" app.py | sort | uniq -c`:

| Code | Count | Use | Assessment |
|---|---|---|---|
| 400 | 65 | Validation / bad input | Correctly used throughout |
| 404 | 41 | Not found | Correctly used throughout; has a branded handler (§1.1's positive contrast) |
| 409 | 5 | Conflict (duplicate resource) | Correctly used |
| 402 | 4 | Plan-limit exceeded | An unusual but semantically well-fitting choice ("Payment Required" for a plan cap) — see §1.2 |
| 403 | 1 (`_require_admin`) | Admin-only access | See §1.3/§1.4 for handler gaps around this code specifically |
| 502 | 2 (1 `HTTPException`, 1 raw `JSONResponse`) | Upstream/proxy failure | Correctly used at both sites: `app.py:5853` (OAuth provider returned no token) and `app.py:5737` (upstream feed fetch failed during template preview). The preview site logs via `logger.warning(..., exc_info=True)` before responding — good practice — but reuses the `{"success": False, "error": ...}` shape instead of `{"detail": ...}`, the same off-pattern shape as the 429 site in §1.5; both belong to the same `/api/preview` feature and may be an intentional, scoped contract for that one JS call site rather than an oversight — **needs verification against `static/js/app.js`'s preview-fetch handler** before changing either key name. |
| 401 | 3 (2 raw `JSONResponse`, 1 `HTTPException` at `auth.py:69`) | Authentication required | See §1.5 |
| 429 | 1 (raw `JSONResponse`) | Rate limiting | See §1.5 |
| 500 | 0 (never explicitly raised; relies on unhandled-exception fallthrough) | Server error | See §1.1 |

**Overall:** categorization onto the *correct* status code is solid — no evidence of e.g. a validation failure returning 500 or a not-found returning 400. The gap is entirely in **handling uniformity across codes**, covered in Section 1, not in **choosing** the wrong code.

**Unable to verify:** the 502 site's context (one occurrence, not read in this audit pass). Confirm with `grep -n -B5 "status_code=502" app.py`.

---

## 3. Async error handling

### 3.1 Fire-and-forget `asyncio.create_task` doesn't hold a task reference — Importance: 2/10

The only `asyncio.create_task` site in production code:
```python
# auth.py:571-575 (forgot_submit)
import asyncio
asyncio.create_task(
    asyncio.to_thread(_send_reset_email, user["id"], user["email"])
)
```
The Python `asyncio` docs explicitly warn: *"Save a reference to the result of this function, to avoid a task disappearing mid-execution."* Nothing here holds that reference. In practice this is low-risk for two reasons verified directly: (1) `_send_reset_email` (`auth.py:529-554`) wraps its **entire body** in `try/except Exception`, logging at `ERROR` with `exc_info=True` (`auth.py:551-554`) — so even if the task *were* garbage-collected mid-flight, no exception would propagate unhandled, because the exception is caught inside the thread function itself, not at the task-await level; (2) this is the only such call site in the codebase (confirmed via `grep -rn "asyncio.create_task" *.py`).

**Fix (hygiene, not a live bug):**
```python
_background_tasks: set = set()
...
task = asyncio.create_task(asyncio.to_thread(_send_reset_email, user["id"], user["email"]))
_background_tasks.add(task)
task.add_done_callback(_background_tasks.discard)
```
**Effort: S.**

### 3.2 Background scheduler: per-item exception isolation — verified positive

`check_all_feeds` (`scheduler.py:2882-2886`) wraps each feed's processing individually:
```python
for feed in due:
    try:
        check_feed(feed["id"])
    except Exception:
        logger.exception("Error checking feed %s (%s)", feed["id"], feed["name"])
```
`_flush_queue` does the same for each queued post (`scheduler.py`, confirmed via direct read: the per-row dispatch is wrapped in `try: ... except Exception as exc: logger.exception("Queue flush: error processing queued post %s", qp_id); ...` with backoff bookkeeping in the except block). `logger.exception` captures the full traceback automatically. **This is correct, deliberate isolation** — one bad feed or one bad queued post cannot abort the batch or crash the scheduler's background thread. `_flush_drips`/`_flush_digests` were confirmed structurally identical in the prior complexity audit's read of these functions (same lease-acquire → per-item-try/except shape); re-verify with `grep -n "except Exception" scheduler.py` around their line ranges if a from-scratch check is wanted. **No action needed.**

### 3.3 A benign unhandled-thread-exception warning during scheduler shutdown — Importance: 2/10

Running the test suite surfaces `PytestUnhandledThreadExceptionWarning` during `TestAdminBootstrap` tests: `apscheduler.jobstores.base.JobLookupError: 'No job by the id of startup_queue_flush was found'`, raised inside APScheduler's own internal `_process_jobs` thread. `stop_scheduler()` (`scheduler.py:2942-2946`) calls `scheduler.shutdown(wait=False)`; `startup_queue_flush`/`startup_drip_flush` (`scheduler.py:2928`, `2939`) are one-shot `"date"`-trigger jobs that APScheduler auto-removes once they fire. The warning appears to be a benign race between that auto-removal and shutdown's own internal bookkeeping — it occurs inside APScheduler's library code, not FeedEcho's call stack, and doesn't crash the app or lose data. **Unable to fully verify root cause without instrumenting APScheduler directly** — what would confirm it: reproduce outside pytest (a standalone script that calls `start_scheduler()` then `stop_scheduler()` twice) and check whether the same traceback appears on stderr; if so, it's an upstream APScheduler quirk, not fixable from FeedEcho's side beyond avoiding `wait=False` (which would change shutdown latency — a tradeoff, not a fix). **Effort: Unable to verify without further investigation; low priority given no observed production impact.**

### 3.4 "Event emitter" error handling — not applicable to this stack

There is no `EventEmitter`/pub-sub idiom in this codebase (confirmed in the prior design-patterns audit: no `Observable`/`EventEmitter` class exists, and that absence was assessed as appropriate for the app's scale). The closest analogs — APScheduler job execution (§3.2) and `notify.py`'s failure/success recording (§4.1) — are covered under Async and Recovery above. Nothing further to evaluate under this heading for a Python/FastAPI stack.

---

## 4. Error recovery

### 4.1 Retry + backoff + failure-threshold alerting — verified positive

`notify.py` implements a complete, per-tenant-configurable recovery pipeline: `next_retry_delay` (`notify.py:65-87`, doubling backoff, configurable via `retry_backoff_minutes`) and `max_attempts` (`notify.py:89-97`, configurable via `retry_max_attempts`) bound how long a failing delivery is retried; `record_failure` (`notify.py:179-...`) counts consecutive failures and sends one alert email once a configurable threshold is crossed, gated so it fires only once (`_notify_state` check at `notify.py:193`); `record_success` (`notify.py:225-...`) sends one all-clear email and resets state on recovery. This is graceful degradation done well — bounded retry cost, no alert spam, explicit recovery notification. **No action needed.**

### 4.2 Fallback-proxy pattern for outbound fetches — verified positive

`feed_parser.py`'s `fetch_feed` (`feed_parser.py:433-458`) and the image-fetch helper (`feed_parser.py:1183-1211`) both catch `httpx.HTTPStatusError` specifically for `403`/`429` responses and retry through `_fetch_via_fallback_proxy` when `settings.FALLBACK_PROXY_URL` is configured, re-raising unchanged otherwise:
```python
except httpx.HTTPStatusError as exc:
    if exc.response.status_code in (403, 429) and settings.FALLBACK_PROXY_URL:
        content, content_type = _fetch_via_fallback_proxy(url, headers, MAX_FEED_SIZE)
    else:
        raise
```
This is a well-scoped fallback: it only engages for the two status codes that indicate bot-blocking (not for genuine 404s or 500s, which correctly propagate), and only when an operator has opted in by configuring a proxy. **No action needed.**

### 4.3 `alt_text.py` retries non-retryable errors identically to transient ones — Importance: 3/10 (pre-existing, previously flagged as LOW in `docs/reviews/2026-08-28-kimi-full-repo-review.md` item #48; still live)

```python
# alt_text.py:196-211
except (
    httpx.HTTPStatusError,
    httpx.RequestError,
    KeyError,
    ValueError,
    IndexError,
    AttributeError,
) as e:
    logger.warning("Alt text API call failed (attempt %d/%d): %s", attempt, MAX_RETRIES, e)
    if attempt < MAX_RETRIES:
        time.sleep(RETRY_DELAY)
```
`MAX_RETRIES = 2`, `RETRY_DELAY = 2` (`alt_text.py:32-33`) — a fixed 2-second delay, not exponential backoff, applied uniformly whether the failure was a transient network blip or a permanent `401` (invalid API key) / `400` (malformed request) that will fail identically on every retry. Real-world cost is small (one extra 2-second-delayed attempt per generation), which is why this stays LOW rather than MEDIUM.

**Fix:**
```python
except httpx.HTTPStatusError as e:
    if e.response.status_code in (401, 403, 400, 404):
        logger.warning("Alt text API call failed permanently (HTTP %s): %s", e.response.status_code, e)
        return ""  # don't burn retries on a config error
    logger.warning("Alt text API call failed (attempt %d/%d): %s", attempt, MAX_RETRIES, e)
    if attempt < MAX_RETRIES:
        time.sleep(RETRY_DELAY * attempt)  # exponential-ish backoff instead of fixed
except (httpx.RequestError, KeyError, ValueError, IndexError, AttributeError) as e:
    logger.warning("Alt text API call failed (attempt %d/%d): %s", attempt, MAX_RETRIES, e)
    if attempt < MAX_RETRIES:
        time.sleep(RETRY_DELAY * attempt)
```
**Effort: S.**

### 4.4 Circuit breaker — absent, and appropriately so

No circuit-breaker pattern exists anywhere in the codebase (no failure-rate tracking that trips a destination "off" for a cooldown window independent of per-item retry counts). Given each destination account already has an independent per-echo `attempt_count`/`max_attempts` cap (§4.1) that bounds the cost of a persistently-failing destination without needing cross-request state, and given this is a single-process scheduler (not a high-QPS service where a failing downstream could cascade into thread-pool exhaustion), a circuit breaker would be meaningful complexity added for a failure mode the existing retry caps already bound. **No action recommended.**

---

## 5. Error information

### 5.1 No debug-mode traceback leakage — verified positive

`FastAPI(title="FeedEcho", version=APP_VERSION)` (`app.py:99`) never sets `debug=True`, and no environment-conditional override was found (`grep -n "DEBUG\|debug=" app.py settings.py` returns nothing). Starlette's `ServerErrorMiddleware` therefore always runs in its safe default mode: an unhandled exception never renders an HTML traceback page to the client, in dev or prod alike. The tradeoff (a uniform, unhelpful page even during local development) is the flip side of finding 1.1 — fixing 1.1 improves both environments at once without weakening this safety property, since the fix only changes the *branding* of the 500 response, not whether internals are exposed.

### 5.2 Inconsistent logging completeness for equivalent failure categories — Importance: 5/10

Two near-identical "send a system email, and it might fail" code paths are logged completely differently:
```python
# auth.py:551-554 (_send_reset_email — password reset)
except Exception:  # noqa: BLE001
    logging.getLogger("feedecho").error(
        "Password reset email failed for user %s", user_id, exc_info=True
    )
```
```python
# auth.py:351-354 (register_submit — verification email)
except Exception as exc:  # noqa: BLE001
    logging.getLogger("feedecho").warning(
        "Verification email for %s failed: %s", email, exc
    )
```
The reset-email path logs at `ERROR` with `exc_info=True` (full traceback captured); the verification-email path logs at `WARNING` with only `str(exc)` (no traceback). If verification-email failures ever spike due to an actual bug (not just "SMTP unconfigured," which the comment at `auth.py:334-335` says is an expected, tolerated case), there will be no stack trace anywhere to diagnose why — only a one-line message repeated in the logs.

**Fix:**
```python
except Exception as exc:  # noqa: BLE001 — verification must not block signup
    logging.getLogger("feedecho").error(
        "Verification email for %s failed: %s", email, exc, exc_info=True
    )
```
(Keep it non-blocking — signup still succeeds either way, per the existing comment; only the log level/detail changes.) **Effort: S.**

### 5.3 Silent exception swallowing with zero logging — Importance: 5/10 (image fetch), 4/10 (test-connection family)

Found via a repo-wide heuristic scan (`except Exception` blocks with neither a traceback-log nor a `raise` in the following 10 lines), then individually verified. Two categories of genuine finding (several other heuristic hits were false positives — e.g. `auth.py:288` and `settings.py:334` both do handle/re-raise, just further down than the scan's window; not reported as findings):

**a) `feed_parser.py`'s image-fetch helper — Importance 5/10:**
```python
# feed_parser.py:1190-1211
try:
    try:
        client, backend = ssrf_client([url])
        ...
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (403, 429) and settings.FALLBACK_PROXY_URL:
            content, raw_type = _fetch_via_fallback_proxy(url, headers, MAX_IMAGE_SIZE)
        else:
            raise
    content_type = raw_type.split(";")[0].strip()
    if content_type not in ALLOWED_IMAGE_TYPES:
        return None
    return content, content_type
except Exception:
    return None
```
The outer `except Exception: return None` catches everything — network errors, redirect-loop `ValueError`s, oversized-response `ValueError`s, the fallback proxy itself failing — with **zero logging**. If image attachment starts silently failing for many items (e.g. a misconfigured `FALLBACK_PROXY_URL`, or a network-policy change blocking outbound fetches entirely), there is no log line anywhere to point at why — echoes just stop having images, with no diagnostic trail.

**Fix:**
```python
except Exception:
    logger.debug("Image fetch failed for %s", url, exc_info=True)
    return None
```
(`DEBUG` rather than `WARNING`/`ERROR` because a single item's image failing is expected/routine — dead links, blocked hosts — but the trace should exist for when it's investigated, and a systemic failure will show up as a burst of these at `DEBUG` if someone turns the level up.) **Effort: S.**

**b) `test_*_connection` family — Importance 4/10, and inconsistent in an interesting way:**
```python
# email_sender.py:203-207 and :225-... (test_smtp_connection, test_system_smtp_connection)
except Exception as e:
    return False, str(e)
```
This catches **any** exception — including a genuine bug in the function itself, not just a bad SMTP config — and converts it into a user-facing "connection failed: <message>" string with no server-side log at all. Contrast with `bluesky.py`'s `test_connection` (`bluesky.py:529-...`), which catches only the exceptions it actually expects (`ValueError`, `BlueskyAuthError`, `BlueskyError`) — an unexpected exception type there would propagate as a real 500 (via finding 1.1's fallthrough), which is actually the *better* failure mode for surfacing an actual bug, at the cost of a worse one-off UX if it happens. The two styles are inconsistent with each other, and the broader-catching style (`email_sender.py`) risks masking bugs as user-facing "your settings are wrong" messages.

**Fix (align on typed catches + a diagnostic log for the broad case):**
```python
except (smtplib.SMTPException, OSError, TimeoutError) as e:
    return False, str(e)
except Exception as e:
    logging.getLogger("feedecho").error("Unexpected error testing SMTP connection", exc_info=True)
    return False, f"Unexpected error: {e}"
```
**Effort: S**, but verify the exact exception types `smtplib`/the underlying send path can raise before narrowing (`grep -n "^import smtplib\|smtplib\." email_sender.py`) so the narrowed catch doesn't accidentally miss a real, expected failure mode.

### 5.4 User-friendly messages — spot-checked, adequate

`templates/error.html` (`templates/error.html:1-11`) is a clean, branded page with a contextual link (dashboard if authenticated, login/signup if not) — better than a bare status code, and consistent for the two codes that use it (403, 404). The gap is coverage (§1.1, §1.4), not quality of the template itself.

---

## Appendix: positive findings not otherwise detailed above

- **Custom exception hierarchies are well-structured and consistently named.** Every destination module (`bluesky.py`, `discord.py`, `matrix.py`, `microblog.py`, `webhook.py`) defines its own `<Platform>Error`/`<Platform>AuthError` family, and — per the 2026-09-08 design-patterns audit's adoption — these now all additionally inherit a shared `DestinationError`/`DestinationAuthError`/`DestinationNotFoundError`/`DestinationRateLimitError` base from `utils.py`, so generic "any destination auth failure" catching is possible without importing all five modules. Domain-specific exceptions elsewhere (`PlanError`, `SSRFError`, `InviteError`, `AccountDeletionAbort`) follow the same one-clear-purpose-per-class convention. This is a solid foundation; the gaps in this report are about the *edges* of that system (where it meets HTTP responses and logging), not the exception classes themselves.
- **`RequestIdMiddleware` logs every unhandled exception with full request context before re-raising** (`app.py:257-275`) — method, path, duration, peer IP, user id, all tagged with the same request id that appears on every other log line for that request and echoed back in the `X-Request-ID` response header. This is exactly the kind of logging completeness the rest of this report is asking for elsewhere (§5.2, §5.3) — it's already the standard the codebase sets for itself at the top of the middleware stack; the gaps found are in modules that fall below that standard, not evidence the standard doesn't exist.

---

## Adoption outcome (2026-09-08, post-audit)

**Landed (PR against master, this session):**
- **§1.1** — `@app.exception_handler(Exception)` added in `app.py`, giving unhandled exceptions the same branded `error.html`/JSON treatment as every other error category. Verified safe: Starlette resolves handlers by walking the raised exception's MRO, so a registered `HTTPException` handler (built-in or the 403/404 overrides) is always matched first — this handler only ever receives genuinely non-`HTTPException` exceptions. Manually verified end-to-end with a synthetic route raising `ValueError`: JSON requests get `{"detail": "Internal server error"}` at 500, HTML requests get the branded page, and `RequestIdMiddleware`'s existing traceback logging still fires unchanged. Confirmed no test relies on an unhandled exception propagating through the app's `TestClient` (`raise_server_exceptions` is only set to `False` in one existing test, which already expects a response rather than a raised exception).
- **§1.3** — `forbidden_handler` now reads `exc.detail` instead of hardcoding "Admin access required".
- **§1.4** — `CSRFOriginMiddleware` now returns a branded `error.html` (HTML requests) or `{"detail": "Cross-origin request rejected"}` (JSON) instead of a bare empty `Response(status_code=403)`. Existing CSRF tests (`test_security_headers.py`) only assert on status code, not body, so this required no test changes.
- **§4.3** — `alt_text.py`'s retry loop now short-circuits on a permanent client error (400/401/403/404) instead of burning the retry budget and sleeping on it; added a regression test (`tests/test_alt_text.py::test_permanent_client_error_does_not_retry`) asserting exactly one call and zero `time.sleep` invocations for a 401. Also switched the *transient*-error backoff from a fixed `RETRY_DELAY` to `RETRY_DELAY * attempt`. Handles `e.response is None` defensively (falls through to the normal retry path) since the existing `test_returns_empty_on_http_error` test constructs `HTTPStatusError` without a response object.
- **§5.2** — `register_submit`'s verification-email failure now logs at `ERROR` with `exc_info=True`, matching `_send_reset_email`'s level and traceback capture for the same failure category.
- **§5.3a** — `feed_parser.py`'s image-fetch helper (`fetch_image`) now logs at `DEBUG` with `exc_info=True` before returning `None`, instead of swallowing every exception silently. Added a module-level `logger` (none existed in this file before).
- **§5.3b** — `email_sender.py`'s `test_smtp_connection`/`test_system_smtp_connection` now log at `ERROR` with `exc_info=True` before converting an exception to the `(False, message)` return shape, so an actual bug in this path (as opposed to a user's SMTP misconfiguration) leaves a stack trace. Kept the broad `except Exception` rather than narrowing to specific `smtplib` exception types, since narrowing risks silently missing a real (if unusual) failure mode this function is supposed to catch — the fix here is purely additive logging, no behavior change to the returned tuple.
- **§3.1** — the one `asyncio.create_task` fire-and-forget site (`auth.py`'s `forgot_submit`) now holds a reference in a module-level `_background_tasks` set with a done-callback to discard it, per the asyncio docs' own guidance. Confirmed this was hygiene, not a live bug: `_send_reset_email` already wraps its entire body in `except Exception`, so no exception could reach the task boundary unhandled either way.

**Not implemented (out of scope, per the audit's own reasoning):**
- **§1.2** (`PlanError`'s 11 call sites) — read in full during the audit and found to be context-appropriate (form re-render vs. JSON 402 vs. bulk-import skip-and-count genuinely differ by caller); the one exact 3-way duplication identified is low-value enough that the audit itself recommended against prioritizing it.
- **§1.5** (401/429/502 response-construction mechanism) — downgraded to informational during the audit itself after confirming the one seemingly-odd JSON shape (`{"success": False, "error": ...}` at `app.py:5711`/`5737`) is intentional and matches `static/js/app.js:495`'s handler; changing it would have broken the preview UI.
- **§3.3** (benign `JobLookupError` warning during scheduler shutdown) — the audit explicitly marked this "unable to verify without further investigation" with no concrete fix proposed; no further investigation was done in this PR.

**Verification:** full test suite run before any change (1497 passed, 36 skipped) and after all eight changes (1498 passed — the one new regression test — 36 skipped, otherwise identical). Targeted test runs after each individual change (security/CSRF, alt_text, email_sender, feed_parser, auth/password-reset) before the final full-suite pass. One fix (§1.1) was additionally verified with a manual synthetic-exception test against a live `TestClient` instance, not just the existing suite, since no existing test exercises an unhandled non-`HTTPException` route.
