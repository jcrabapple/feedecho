# F2 — Auto-queue + Review delivery modes (scheduler.py)

## Goal

`echoes.delivery_mode` gains two values beyond today's `instant`/`digest`:

- `queue` — "Auto-queue": feed items are rendered into the curation queue and
  released automatically at the destination's next free slot (existing
  `queue_settings` interval + active-hours math). IF a review step is wanted
  instead, use `review`.
- `review` — "Review-first": same queue row but stored with status `draft`;
  nothing auto-sends until the operator acts from `POST /queue`.

This bridges automated echoes into the already-shipped `queued_posts`
plumbing (Reader Compose desk).

## Operator-visible behavior

- Every destination type can use queue/review (digest stays email-only).
- Drip limit does not buffer queue-mode or review-mode items (they leave the
  instant pipeline at a different point); `drip_limit` stays stored but inert.
- Multi mode: the queue-depth plan cap enforced like the Compose desk —
  `plans.check_queue_allowance(count + 1, plan)`; on overflow the item FAILS
  LOUDLY with `_fail_post(..., msg)` where msg names the cap (never silently
  dropped). Single mode never consults plans.
- Auto-modes NEVER dispatch via the destination sender directly. They
  `INSERT` one row in `queued_posts`, mark the `posted_items` row `'queued'`
  so the feed cursor advances (same terminal-hold semantics drip uses), and
  the existing `_flush_queue` / post-now machinery handles delivery.

## Files

- Modify: `scheduler.py` (ONLY this lane's file; app/templates are wave-2)
- Test (new): `tests/test_scheduler_queue_mode.py`

allowed values for validation (`import_export._VALID_DELIVERY_MODES`,
echo routes) are wave-2 edits; this lane assumes scheduler-side behavior
only and must tolerate any stored value gracefully (fallback on unknown =
instant, same defensive pattern `_drip_applies` already uses).

## scheduler.py changes

### 1. `_drip_applies`

Current body ends with `return delivery_mode != "digest"`.
Change to `return delivery_mode == "instant"` (only instant echoes drip; the
docstring should say queue/review/digest buffer elsewhere).

### 2. `process_echo` branch

After the `filter` check and after `_claim_post` succeeds (right before
`if _drip_applies(echo):`), insert:

```python
try:
    delivery_mode = echo["delivery_mode"] or "instant"
except (KeyError, IndexError):
    delivery_mode = "instant"
if delivery_mode in ("queue", "review"):
    return _enqueue_for_queue_mode(echo, item, posted_id, claim_token, delivery_mode)
```

### 3. New function `_enqueue_for_queue_mode(echo, item, posted_id, claim_token, mode) -> bool`

Sequence (one `with get_db() as db:` block is fine except where noted):

1. PLAN CAP (MULTI only):
   ```python
   if settings.MULTI:
       plan = echo.get("plan") if hasattr(echo, "get") else (echo["plan"] if ... else "trial")
   ```
   Practical: mirrors `_drip_applies` style — keys may be missing on test
   fixtures. Use dict-style accessor guarded by try/except (KeyError,
   IndexError, TypeError) with fallback `"trial"`.
   Count rows: `SELECT COUNT(*) FROM queued_posts WHERE user_id = ? AND status IN ('queued','sending','draft')`
   (echo["user_id"]), then call
   `plans.check_queue_allowance(count + 1, plan)` and catch the exception
   (its type per plans.py — find by reading plans.py; use broad except
   where the raised error type is ambiguous OR import the specific class).
   On overflow: `_fail_post(posted_id, claim_token, echo["id"], "<cap message>")`
   with a permanent=False default and message:
   `"Echo auto-queue is at your plan's queue limit; free space or switch this echo back to Instant"`.
   Return the error-path result.
2. RENDER the stored echo template into materialized text:
   `content = render_template(echo["template"], item, feed_name=item.get("feed_name") or "")`
   — feed_name is NOT a field on item; pass "" for this lane's function
   (no feed_name kwarg yet). Keep the same try/except the instant path has:
   render errors -> `_fail_post(..., f"Template rendering failed: {e}")`.
   Empty render -> `_fail_post(..., "Rendered content was empty")`.
   Both MATCH `_render_and_dispatch`'s failure strings exactly so the error
   history is consistent.
3. ONE-SHOT ECHO at enqueue (idempotency key), mirroring the Compose desk's
   insert (app.py ~5966) — read that code for the canonical columns:
   ```sql
   INSERT INTO echoes (feed_id, destination_type, destination_id, template,
                       visibility, attach_image, enabled, one_shot, deleted_at, user_id)
   VALUES (?, ?, ?, '{{ title }}', ?, ?, 0, 1, ?, ?) RETURNING id
   ```
   (template literal `'{{ title }}'`; the stored content carries the real
   text; the echoed row is inert for re-rendering because the queued row's
   content wins.)
4. SCHEDULE slot:
   - `mode == "queue"`: `target_time = next_free_slot(db, echo["user_id"], echo["destination_type"], echo["destination_id"])`
   - `mode == "review"`: `target_time = _now()`
5. INSERT queued row with the Compose shape (see app.py enqueue for exact
   columns):
   ```
   user_id, feed_item_id, item_id, feed_id, destination_type, destination_id,
   content, visibility, attach_image, image_alt, scheduled_at, status, echo_id
   ```
   - `feed_item_id`: pass None (compute path has no feed_items row id).
     `_flush_queue` already tolerates NULL there.
   - `status`: `'draft'` for review, `'queued'` for auto-queue.
   - `image_alt`: `item.get("image_alt") or ""`.
   - `attach_image`: reuse `_echo_attach_image(echo)` (same helper the
     instant path uses).
6. MARK the posted_items row 'queued' (cursor-terminal, alert-state safe):
   `_update_post(posted_id, claim_token, "queued")` — same call as
   `_queue_for_drip`/digest path. Do NOT `record_success` (queueing is not a
   delivery), matching drip semantics.
7. Log: `logger.info("Echo %s: item %s enqueued for %s at %s", ...)`.

`_post_succeeded(echo_id, item_id)` already counts 'queued' as terminal — the
existing retry sweep and cursor logic require NO changes.

### 4. Move `next_free_slot` INTO scheduler.py

Copy the function body currently in `app.py` at ~line 4767 (`_next_free_slot(db, uid, dest_type, dest_id)`) into `scheduler.py`, renamed to PUBLIC `next_free_slot(db, uid, dest_type, dest_id)` — byte-identical logic (queue_settings interval + active-hours snapping, `_filter_int`-free — it takes ints directly). Do NOT modify app.py (orchestrator removes that copy in wave 2 and imports from scheduler). Keep a module-level alias `__all__ = ["next_free_slot"]` not needed; just the function.

## Tests

`tests/test_scheduler_queue_mode.py`, sqlite suite style:

1. `delivery_mode='queue'` mastodon echo (accounts row + feed row +
   enabled echo, seeded like `tests/test_filters.py::TestSchedulerFiltering`):
   `scheduler.process_echo(echo, item_with("tags"))` -> returns True;
   `post_status` NEVER called (monkeypatched);
   `queued_posts` has exactly 1 row: status='queued', destination ids match,
   content == rendered template output, echo_id set and `echoes` row
   `one_shot=1`, `enabled=0`;
   posted_items row status == 'queued' (not 'success').
2. `delivery_mode='review'` -> same shape but status == 'draft'.
3. Review-mode echo: `_flush_queue` path would not fire; assert via the
   `_flush_queue` SELECT that the draft row is excluded — light check: run
   the scheduler helper's SELECT directly in the test and assert draft row's
   id not among due rows.
4. Drip is bypassed: an echo with `drip_limit=3` AND `delivery_mode='queue'`
   creates a queued row, and `drip_items` stays EMPTY.
5. Cursor semantics: `_post_succeeded(echo_id, item_id)` is True after
   queueing.
6. `next_free_slot` unit test: move-check — default settings (no
   queue_settings row) yields ~now + 240min; with a queue_settings row at
   interval=60 the slot is now+60; with active_hours 09:00-21:00 UTC
   producing an out-of-window candidate, it snaps to 09:00 next day.
   (Use tz-naive UTC comparisons; the function isTravel-unaware.)
7. Plan-cap guard (multi): monkeypatch `settings.MULTI=True` and
   `plans.check_queue_allowance` to the real function but insert
   queue rows so count+1 exceeds cap — expect `_fail_post` path resulting in
   posted_items 'failed' with an error_message mentioning the queue limit,
   and NO queued row inserted.
8. Red-before-green evidence per coder rules.
