# FeedEcho Reader — Implementation Plan

Handoff document for the implementing agent. Target branch: `claude/feedecho-rss-architecture-srfbxw`
(or one branch per phase, in the order given).

**Product thesis.** FeedEcho should not compete with Miniflux/FreshRSS on lean reading or with
Feedly on ML ranking. Its unique asset is a first-class *output* side — seven destination types,
a sandboxed Jinja template engine, per-echo filters, drip, digest, vision-generated alt text, and
delivery history. The reader becomes a **curation desk**: read → select → annotate → publish or
queue. Every phase below is ordered by how much it serves that thesis, with correctness debt first.

---

## Ground rules (apply to every phase)

These are existing invariants in this codebase. Violating them is a bug, not a style choice.

1. **Dual dialect.** Every schema change goes in BOTH branches of `database.py::init_db()` — the
   sqlite branch (~line 307) and the postgres branch (~line 914) — plus an
   `_add_column_if_missing()` call for existing installs. `INTEGER PRIMARY KEY AUTOINCREMENT`
   ↔ `BIGSERIAL PRIMARY KEY`.
2. **`?` placeholders only.** `database.qmark()` translates to `%s` for Postgres. Never write `%s`
   directly, never f-string user values into SQL. Identifiers interpolated into f-strings must come
   from module-level constants only (see `DESTINATION_TABLES_BY_TYPE` usage).
3. **NULL ordering.** Postgres and sqlite disagree on NULL sort position. Always write
   `ORDER BY (col IS NULL), col DESC` as the existing reader query and `prune_feed_items` do.
4. **Ownership on every query.** Reader rows are reached via `feed_items JOIN feeds` with
   `f.user_id = ?` and `f.deleted_at IS NULL`. There is no per-item user_id column; do not add one.
5. **Entitlement check.** Every new reader route calls `_require_reader(db, uid)` (`app.py:829`).
6. **Outbound HTTP.** No bare `httpx.Client` outside `feed_parser.py` — a test enforces this. Use
   `ssrf_client()` / `pinned_request()`; `unpinned_client()` is single-mode only.
7. **CSRF posture.** Session cookie is `SameSite=Lax` (`auth.py:186`) and there is no CSRF token
   scheme. New state-changing endpoints must be `POST` (never `GET`), matching `app.py:662`.
8. **Tests live in `tests/`,** one file per feature area, using the `env` fixture pattern from
   `tests/test_reader_tier1.py` (monkeypatch `settings.MULTI`, `database.DB_PATH`, stub
   `scheduler.check_all_feeds`). Postgres-shaped assertions go in a `*_pg.py` file mirroring
   `tests/test_reader_pg.py`.

---

## Phase 0 — Correctness debt (ship first, small)

Two defects make the current reader unsafe to build on.

### 0.1 Starred items are silently deleted by retention

**Bug.** `database.py::prune_feed_items()` (line 237) deletes every row past
`READER_MAX_ITEMS_PER_FEED` (default 200) ordered only by date. Starring an item does not protect
it. `scheduler.py:341` calls this on every poll of a read-enabled feed, so a starred item on a busy
feed is gone within days. "Star to save" is currently a false promise.

**Fix.** Starred rows are never deleted and never count against the cap:

```sql
DELETE FROM feed_items
 WHERE feed_id = ?
   AND starred = 0
   AND id NOT IN (
       SELECT id FROM feed_items
        WHERE feed_id = ? AND starred = 0
        ORDER BY published_at IS NULL, published_at DESC, id DESC
        LIMIT ?
   )
```

- Keep the signature `prune_feed_items(db, feed_id, limit=None)`.
- Update the docstring to state the starred exemption.
- Add `settings.READER_MAX_STARRED_PER_FEED` (default `0` = unlimited) as a safety valve for
  hosted mode; when non-zero, a second delete trims starred rows beyond that cap. Wire it into
  `plans.py` later (Phase 6) rather than now.

**Tests** (`tests/test_reader.py`, `TestPrune`): insert 5 items with cap 2, star the oldest, assert
the starred row survives and exactly 2 unstarred remain. Add the same case to `test_reader_pg.py`.

### 0.2 The reader can only ever show 100 items

**Bug.** `reader_page` (`app.py:2116`) ends in a hard `LIMIT 100` with no offset or cursor. Items
101+ in any view are unreachable — there is no "next page" in the UI at all. With a 200-item
per-feed cap and multiple feeds, most stored content is invisible.

**Fix — keyset pagination** (not OFFSET: it drifts as new items arrive mid-scroll).

Sort key is the existing triple: `(published_at IS NULL) ASC, published_at DESC, id DESC`.
Mixed directions rule out a row-value comparison, so add an explicit helper in `app.py`:

```python
READER_PAGE_SIZE = 50

def _reader_keyset(cursor: str | None) -> tuple[str, list]:
    """SQL predicate + params selecting rows strictly AFTER `cursor` in reader order.

    Cursor format: "<published_at or ''>|<id>". Two cases, because NULL-dated
    rows sort as one block after all dated rows:
      - cursor row HAS a date: dated rows older than it, or same date + lower id,
        plus every NULL-dated row.
      - cursor row has NO date: only NULL-dated rows with a lower id.
    """
```

Case A (`pub` non-empty):
```sql
( i.published_at IS NULL
  OR i.published_at < ?
  OR (i.published_at = ? AND i.id < ?) )
```
Case B (`pub` empty):
```sql
( i.published_at IS NULL AND i.id < ? )
```

- Query `LIMIT READER_PAGE_SIZE + 1`; if the extra row comes back, pop it and emit
  `next_cursor = f"{last.published_at or ''}|{last.id}"`.
- Accept `?after=<cursor>` on `GET /reader`. Validate with a strict regex
  (`^[0-9 :.\-]*\|\d+$`); a malformed cursor is ignored (first page), never a 500.
- Template: render a `Load more` anchor carrying every current query param plus `after=`,
  so it works without JS. Progressive enhancement in `static/js/app.js`
  (`readerLoadMore(btn)`): fetch the same URL with `X-Requested-With: fetch`, and have the
  route return only the `<li>` fragment list when that header is present (add a small
  `reader_items.html` partial included by `reader.html` so both paths render identical markup).
- Rebind keyboard nav after append: `readerMove()` reads `readerList()` live, so appending
  to the same `<ul>` is sufficient; verify `data-max-item-id` is NOT changed by a load-more
  (the new-count pill baseline must stay the page-load value).
- Auto-read-on-scroll (`readerAutoReadScroll`) must observe appended entries — it queries the
  list on each scroll, confirm no cached NodeList.

**Tests** (`tests/test_reader_pagination.py`): 120 items with mixed NULL/non-NULL dates and
duplicate timestamps; walk pages via `after=`; assert (a) no duplicates across pages, (b) no
gaps, (c) full set recovered, (d) NULL-dated block lands last, (e) garbage cursor → page 1.

**Acceptance for Phase 0:** starring survives 10x the cap of new items; every stored item is
reachable by paging; existing reader tests green.

---

## Phase 1 — Organization: folders + OPML

A flat, alphabetical feed list is the reader's most visible weakness against
FreshRSS/Inoreader. OPML already exists but is folder-blind and paste-only, so the two land
together.

### 1.1 Folders

**Schema** (both dialects + migration):

```sql
CREATE TABLE IF NOT EXISTS folders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,   -- BIGSERIAL on pg
    user_id INTEGER NOT NULL DEFAULT 1,
    name TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_folders_user_name ON folders(user_id, LOWER(name));
```
Plus `_add_column_if_missing(db, "feeds", "folder_id", "INTEGER")` — nullable, no FK constraint
on sqlite migration path (matches how `feeds` handles other added columns); deleting a folder
sets member feeds to `folder_id = NULL` in application code, not via cascade.

**Routes** (`app.py`, near the other feed APIs):
- `POST /api/folders` — `name` (1..60 chars, trimmed, collapse whitespace). 409 on duplicate.
- `POST /api/folders/{id}/rename` — same validation.
- `POST /api/folders/{id}/delete` — nulls out `feeds.folder_id` for that user's feeds first.
- `POST /api/feeds/{feed_id}/folder` — `folder_id` (empty = uncategorized). Verify BOTH the feed
  and the folder belong to `uid` before the update.
- Optional `POST /api/folders/reorder` — comma-separated ids → `position`.

**Reader integration:**
- `reader_page` gains `folder: str = ""`; when set, `where.append("f.folder_id = ?")`.
- Sidebar (`templates/reader.html`): group `feeds` by folder, folder header row with summed
  unread count and a "mark folder read" button; uncategorized feeds in a trailing group.
  Collapse state per folder id in `localStorage` (follow the existing `readerApplyDensity`
  pattern for persisted UI prefs).
- `_parse_reader_query` gains `folder:NAME` → `LOWER(fo.name) LIKE ? ESCAPE '!'`, which requires
  a `LEFT JOIN folders fo ON fo.id = f.folder_id` in the items query. Add the operator to the
  shortcuts dialog table in `reader.html`.
- `reader_mark_all_read` gains an optional `folder_id` form field (mutually exclusive with
  `feed_id`), scoped by ownership, returning ids for the existing undo toast.
- Feeds page (`templates/feeds.html`): a folder `<select>` per feed row plus a "New folder" input.

**Tests** (`tests/test_reader_folders.py`): folder CRUD authz (another user's folder → 404),
duplicate name → 409, delete leaves feeds intact with NULL folder, `folder=` filter, `folder:`
operator, mark-folder-read affects only that folder's items.

### 1.2 OPML — extend what exists

**Already built** (do not rewrite): `GET /api/feeds/opml` exports OPML 2.0 (`app.py:3484`),
`POST /api/feeds/opml` imports it (`app.py:3513`), UI in `templates/feeds.html` under
"Import / export subscriptions". Import already validates each URL through `validate_url`,
skips duplicates, enforces `check_feed_allowance`, sets `read_enabled` from the plan, and
rejects DOCTYPE/ENTITY declarations by string check before `ElementTree.fromstring`.

**Four real gaps:**

1. **No file upload.** Import is a textarea: the user must open the `.opml` file in an editor and
   paste it. Every other reader hands you a file. Add an `UploadFile` field to the same route
   (accept either `opml` text or an uploaded file; if both, prefer the file), keep the textarea as
   a fallback. Read at most 2 MB from the upload and reject larger with a 400 — the current code
   has no size bound at all.
2. **Folders are dropped on import.** `root.iter("outline")` flattens the tree, so nested
   outlines lose their parent title. Replace with a recursive walk that tracks the nearest
   ancestor outline lacking `xmlUrl` as the folder name, and create/reuse folders (Phase 1.1) by
   name. Depth-cap the recursion (say 10) and cap total outlines (1000).
3. **Folders are dropped on export.** The exporter emits a flat outline list. Once `folder_id`
   exists, group by folder and nest feed outlines inside a container
   `<outline text="Folder">`, with uncategorized feeds at top level. Keep using `quoteattr` for
   escaping — it is already correct.
4. **Import feedback is thin.** The redirect carries `imported`/`skipped` but the template only
   renders `imported`, and "skipped" conflates duplicate, invalid URL, and hit-the-plan-cap.
   Split into `imported`, `duplicate`, `invalid`, `capped` and render all four, so a user whose
   import silently stopped at the plan cap can tell.

**Optional hardening:** the DOCTYPE/ENTITY string check is crude — it rejects a document with the
literal text `<!DOCTYPE` inside an attribute or CDATA, and passes nothing dangerous either way.
Adding `defusedxml` (small, pure Python; add to `pyproject.toml` and refresh `uv.lock`) and using
`defusedxml.ElementTree.fromstring` is a cleaner guarantee. Low priority — the current check is
not wrong, just blunt. Keep the size and outline-count caps regardless.

**Tests** (extend the existing OPML tests — check `tests/` for current coverage first): nested
folders round-trip export→import→export; file upload path; oversize upload rejected; outline-count
cap; the four-way import summary counts.

**Acceptance for Phase 1:** a 60-feed Inoreader OPML with folders imports in one action, groups
correctly in the sidebar, and exports back to an equivalent file.

---

## Phase 2 — Compose (the differentiator)

Today's Shout (`app.py:3948`, dialog in `reader.html`) is a destination `<select>` and a raw Jinja
template textarea, rendered once per item — 100 `<dialog>` elements on a full page. It works but
it is a developer's UI. Compose turns it into the reason someone chooses FeedEcho.

### 2.1 Restructure the dialog

One shared `<dialog id="reader-compose">` at page level. Each item's action button carries
`data-item-id`; `readerOpenCompose(id)` fetches state and populates. Removes ~100 duplicated
subtrees per page.

### 2.2 New endpoint: preview

`GET /api/reader/{item_id}/compose` → JSON:

```json
{
  "item": {"title": "...", "link": "...", "summary": "...", "image_url": "...", "image_alt": "..."},
  "destinations": [{"value": "mastodon:3", "label": "Mastodon · main", "limit": 500}],
  "default_template": "{{ title }} {{ link }}",
  "rendered": {"mastodon:3": "Post text as it would be sent"}
}
```

- Per-destination limits from one new module-level map (single source of truth, referenced by
  both the API and the JS counter): mastodon 500, bluesky `bluesky.MAX_POST_GRAPHEMES` (300),
  discord 2000, matrix/email/microblog/webhook `None`.
- Bluesky counting must use `bluesky._grapheme_clusters` semantics, not `len()`, or the counter
  lies about exactly the platform that truncates.
- Render previews through `template_engine.render_template` with the item dict assembled the same
  way `reader_shout` assembles it, so preview == sent.

### 2.3 Post an edited body

Compose must send *the text the user edited*, including free-form commentary. Do **not** round-trip
that text through Jinja — braces or `{%` in user prose would be interpreted, and it needlessly
puts user text through the sandbox.

Thread an override through the delivery path:

- `scheduler.process_echo(echo, item, feed_name="", override_content=None)`
- `scheduler._render_and_dispatch(echo, item, feed_name, posted_id, claim_token, override_content=None)`
  — when `override_content` is not None, skip `render_template` entirely; keep the
  `if not content.strip(): _fail_post(...)` guard; apply a hard length cap (reuse
  `template_engine._MAX_OUTPUT`) before dispatch.
- Every existing call site keeps working unchanged (default `None`).

`POST /api/reader/{item_id}/compose` (replaces `/shout`; keep `/shout` as a thin alias for one
release so nothing breaks):

| Field | Notes |
|---|---|
| `destinations` | comma-separated `type:id`, 1..5 entries, each ownership-checked |
| `content` | the final body; if omitted, `template` is rendered as today |
| `template` | still accepted for the "use my template" path; `_validate_echo_template` as now |
| `visibility` | unchanged, `VALID_VISIBILITY` |
| `attach_image` | 0/1 |
| `image_alt` | optional override; blank → existing alt → `alt_text.generate_alt_text` if enabled |

Behavior: one one-shot echo per destination (existing materialization pattern —
`enabled=0`, `one_shot=1`, `deleted_at=now`, so neither `/echoes` nor the scheduler picks it up),
each dispatched via `process_echo(..., override_content=content)`. Return an array of per-destination
results (`status`, `post_url`, `error_message`) and have the JS render per-destination success/failure
rather than a single toast — partial failure across destinations is the normal case and must be visible.

### 2.4 Dialog UX

- Editable body textarea prefilled from the rendered template; live per-destination character
  counters that turn red past the limit (Bluesky counted in graphemes).
- A separate **commentary** field prepended to the body on insert (a single "Add comment" affordance
  writes into the same textarea — keep one source of truth for what gets sent).
- Multi-select destinations (checkbox list, not `<select>`), remembering the last used set in
  `localStorage`.
- Image: thumbnail of `item.image_url` with an "attach" toggle and an editable alt-text field
  pre-filled from `image_alt`. Show a hint when alt is auto-generated. This is a genuine
  differentiator — most cross-posters ship images with no alt text at all.
- Buttons: **Post now** / **Add to queue** (queue disabled until Phase 3) / Cancel.

**Tests** (`tests/test_reader_compose.py`): override content is sent verbatim, including a body
containing `{{ title }}` (must NOT be expanded); empty body rejected; multi-destination returns
one result per destination and a partial failure is reported as such; destination belonging to
another user → 404; oversize body rejected; `/shout` alias still works.

**Acceptance for Phase 2:** select an item, add a sentence of commentary, see accurate character
counts for Mastodon and Bluesky, post to both in one action with alt text attached.

---

## Phase 3 — The queue ("Buffer for RSS")

The drip (`drip_items`, `flush_drips`) and digest (`digest_items`, `flush_digests`) machinery
already exists but is reachable only by automatic echoes. Human curation should feed the same
release valve.

**Schema:**

```sql
CREATE TABLE IF NOT EXISTS queued_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,      -- BIGSERIAL on pg
    user_id INTEGER NOT NULL DEFAULT 1,
    feed_item_id INTEGER,                      -- nullable: item may be pruned later
    item_id TEXT NOT NULL,                     -- publisher id, for dedupe/history join
    feed_id INTEGER NOT NULL,
    destination_type TEXT NOT NULL,
    destination_id INTEGER NOT NULL,
    content TEXT NOT NULL,                     -- final body (Compose output)
    visibility TEXT DEFAULT 'public',
    attach_image INTEGER NOT NULL DEFAULT 0,
    image_alt TEXT,
    scheduled_at TIMESTAMP NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',     -- queued|sending|sent|failed|cancelled
    claim_token TEXT,
    claimed_at TIMESTAMP,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    posted_item_id INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_queued_posts_due ON queued_posts(status, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_queued_posts_user ON queued_posts(user_id, scheduled_at);
```

**Scheduler job** `flush_queue()` in `scheduler.py`, registered in `start_scheduler()` on an
`IntervalTrigger(minutes=5)` with `max_instances=1, coalesce=True`, wrapped in
`_acquire_job_lease("flush_queue", FLUSH_QUEUE_LEASE_SECONDS)` / `_release_job_lease` in a
`finally` — copy the `flush_drips` structure exactly.

Claiming must be atomic and replica-safe, mirroring `_acquire_feed_lease`:

```sql
UPDATE queued_posts
   SET status = 'sending', claim_token = ?, claimed_at = ?
 WHERE id = ?
   AND status = 'queued'
   AND scheduled_at <= ?
```
`rowcount == 1` wins the row. Re-claim rows stuck in `sending` past a TTL (crash recovery).

Dispatch reuses Phase 2: materialize a one-shot echo, call
`process_echo(echo, item, feed_name, override_content=row["content"])`, then write back
`status`, `posted_item_id`, `error_message`. Failures retry with backoff up to N attempts, then
`failed` with a notification through the existing `notify.py` path.

**Slots.** v1 keeps this simple and explicit:
- Per (user, destination) setting: `queue_interval_minutes` (default 240) and optional
  `queue_active_hours` ("09:00-21:00" in the user's TZ) stored in a `queue_settings` table.
- `next_free_slot(user_id, destination)` = max(now, last scheduled_at for that destination) +
  interval, snapped into active hours. Compose's "Add to queue" uses it; the queue page lets the
  user override with an explicit datetime.
- Do NOT build a full drag-and-drop calendar in v1. Reordering = recomputing `scheduled_at`.

**UI** — new `/queue` page (`templates/queue.html`):
list grouped by destination, ordered by `scheduled_at`, each row showing time, destination, body
preview, and Edit / Post now / Remove. Reorder via up/down buttons (recompute slots) before any
drag-and-drop. Sidebar link with a pending count.

**Entitlements** (`settings.DEFAULT_PLAN_LIMITS`): `queue_depth` (trial 10, beta 100, paid 500;
0 = unlimited) enforced in `plans.check_queue_allowance()` at enqueue time, following the
`check_feed_allowance` shape.

**Tests** (`tests/test_queue.py`): enqueue → due → dispatched once; two concurrent `flush_queue`
runs dispatch exactly once (simulate by claiming with two tokens); cancelled rows are skipped;
depth cap enforced in multi mode; a pruned `feed_item_id` does not break dispatch (content is
already materialized on the row — assert this explicitly, it's the reason `content` is stored
rather than re-rendered).

---

## Phase 4 — Show the cross-poster's decisions in the reader

No other reader can do this, because no other reader posts. This is the feature that makes the
reader and the cross-poster feel like one product instead of two tabs.

### 4.1 Per-item delivery status

`reader_page` already computes a `shouted` flag via a correlated subquery over `posted_items` for
`one_shot` echoes. Generalize: one subquery (or a single grouped join, preferred for a 50-row page)
returning per-item aggregate state across ALL of that feed's echoes:

```sql
LEFT JOIN (
    SELECT p.item_id, e.feed_id,
           SUM(CASE WHEN p.status = 'success'  THEN 1 ELSE 0 END) AS n_sent,
           SUM(CASE WHEN p.status = 'filtered' THEN 1 ELSE 0 END) AS n_filtered,
           SUM(CASE WHEN p.status IN ('failed','gave_up') THEN 1 ELSE 0 END) AS n_failed,
           SUM(CASE WHEN p.status = 'queued'   THEN 1 ELSE 0 END) AS n_queued
      FROM posted_items p JOIN echoes e ON e.id = p.echo_id
     WHERE e.user_id = ?
     GROUP BY p.item_id, e.feed_id
) d ON d.item_id = i.item_id AND d.feed_id = i.feed_id
```

Render badges in the item meta line: *Echoed*, *Filtered*, *Failed*, *Queued*, each linking to
`/history` filtered to that item. Verify the query plan on Postgres with a realistic row count
before shipping; if it regresses, denormalize a small `feed_items.delivery_state` column updated
by `_update_post`.

### 4.2 Say *why* it was filtered

`_record_filtered` (`scheduler.py:675`) writes `error_message = NULL`. Change it to record the
matched keyword(s): extend `filters.py::is_filtered` into
`match_reason(item, keywords, mode) -> str | None` (returning e.g. `matched "sponsored"` or
`no include keyword matched`), keep `is_filtered` as a thin wrapper so existing callers and tests
are untouched, and store the reason. Surface it in the reader badge tooltip and in `/history`.

### 4.3 Item-level training

Muting is currently a comma-separated text box on the feed edit page — the wrong place. The
judgment happens while reading.

- `POST /api/reader/{item_id}/mute` — form fields `phrase` (1..60 chars) and `scope`
  (`feed` | `all`). Appends to `feeds.mute_keywords` for that feed (or every read-enabled feed of
  the user), deduping case-insensitively. Returns the updated keyword list.
- UI: select text in an item body → a small floating "Mute this" affordance; plus a
  "Mute…" action button that prefills from the selection or the title.
- Undo toast reusing `readerUndoToast`, calling a matching `unmute` endpoint.
- The mute list must be visible and editable somewhere — add a "Muted words" section to the feed
  edit page listing them as removable chips, since the reader can now write to it.

**Tests** (`tests/test_reader_training.py`): mute writes the phrase, dedupes, scope=all hits every
read-enabled feed and nothing else; muted items disappear from the reader query; unmute restores;
filter reason is recorded and rendered.

---

## Phase 5 — Saved searches as smart feeds

The search parser (`_parse_reader_query`) already supports `is:`/`feed:`/`in:` (plus `folder:`
from Phase 1). Make a query a first-class object.

```sql
CREATE TABLE IF NOT EXISTS saved_searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,   -- BIGSERIAL on pg
    user_id INTEGER NOT NULL DEFAULT 1,
    name TEXT NOT NULL,
    query TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_saved_searches_user_name
    ON saved_searches(user_id, LOWER(name));
```

- `POST /api/saved-searches` (name + query), delete, rename. `GET /reader?saved=<id>` runs the
  stored query through the existing parser.
- Sidebar section above Feeds, each with an unread count. **Perf caveat:** counts run the same
  `LIKE` scan per saved search on every page load. Cap saved searches per plan (trial 3, paid 20),
  compute counts in one pass, and cache them for 60s in-process keyed by
  `(user_id, max_item_id)`. If this is still slow on Postgres, that is the point at which
  full-text search (`tsvector` + GIN on pg, FTS5 on sqlite) becomes justified — not before.
- "Save this search" button appears in the toolbar whenever `q` is non-empty.

**Deferred (Phase 6, design note only):** echoing a saved search. `echoes.feed_id` is
`NOT NULL` and the scheduler is feed-driven, so this needs `echoes.saved_search_id` (nullable,
with a CHECK that exactly one of feed_id/saved_search_id is set), a cursor that is per-search
rather than per-feed (`feed_items.id` high-water mark), and a `check_saved_searches()` sweep after
each feed poll. Real work — do not fold it into Phase 5.

---

## Sequencing and sizing

| Phase | Content | Size | Gate |
|---|---|---|---|
| 0 | Starred pruning, pagination | S | none |
| 1 | Folders, OPML nesting + upload | M | 0 |
| 2 | Compose | M | 0 |
| 3 | Queue | L | 2 |
| 4 | Delivery visibility, training | M | 2 |
| 5 | Saved searches | M | 1 |

One PR per phase. Phase 0 and Phase 1 can land in parallel with Phase 2 if branches are kept
disjoint (Phase 2 touches `scheduler.py` + the compose dialog; Phase 1 touches the sidebar and
`database.py` — expect a conflict in `reader.html` and `init_db`; land Phase 1 first if unsure).

## Self-hosted vs hosted

Everything above ships in both builds. The reader is what earns goodwill; scale is what gets paid
for. Hosted-only fences, all expressed as `settings.DEFAULT_PLAN_LIMITS` keys read through
`plans.limit_for()` (single mode never consults them):

- `queue_depth` — pending queued posts
- `queue_horizon_days` — how far ahead scheduling may reach
- `saved_searches` — count
- `compose_max_destinations` — 1 on trial, 5 on paid
- `reader_retention_items` — overrides `READER_MAX_ITEMS_PER_FEED` per plan
- `READER_MAX_STARRED_PER_FEED` — unlimited self-hosted, capped hosted

Do **not** fence single-destination Compose, folders, OPML, or the reader itself behind a plan
beyond the existing `reader` entitlement.

## Explicitly out of scope

- AI summaries / relevance ranking as a headline feature — recurring cost, and it puts FeedEcho
  head-to-head with Feedly's actual moat.
- Social/shared reading (NewsBlur blurblogs) — needs scale that does not exist yet.
- Native mobile apps — add a PWA manifest and swipe gestures on the existing pages instead.
- Full-text article extraction — worthwhile later, but it widens the outbound surface to arbitrary
  article URLs and must go through the pinned SSRF path in hosted mode. Separate plan.
