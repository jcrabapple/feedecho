# F1 — Field-scoped echo filters (filters.py)

## Goal

Echo keyword filters currently match only `title + summary + description`.
POSSE users need to filter by feed **tags**, **author**, or **URL** (e.g. only
cross-post items tagged `#posse`). Extend `filters.py` with optional
field-scoped clauses while keeping existing bare-keyword behavior
byte-identical.

## Behavior spec

`filter_keywords` stays a comma-separated string. After the existing
trim-and-split, each part is parsed:

```
^(tag|author|url|link|content):(.+)$   ->  field-scoped clause
anything else                          ->  bare keyword (unchanged semantics)
```

Rules:
- Unknown prefixes (e.g. `release:2`) stay BARE keywords — a legacy keyword
  containing a colon keeps behaving as a plain substring token.
- A recognized prefix with an empty value (`tag:` with nothing after) falls
  back to a bare keyword of the whole part (legacy safety).
- Case-insensitive matching throughout (same `.casefold()` pattern as now).
- Bare keyword haystack stays EXACTLY: `title`, `summary`, `description`
  (same as today). Do NOT widen it with new fields.

Field haystacks:
- `tag:` → `" ".join(item.get("tags") or [])` + category dict VALUES (when
  `categories` is a dict, join its string values), all joined by space,
  casefolded.
- `author:` → `item.get("author") or ""`.
- `url:` and `link:` → `item.get("link") or ""`.
- `content:` → `item.get("content") or ""` + `item.get("content_text") or ""`.
  `content_html` is deliberately NOT searched (sanitized markup can
  false-positive on tags); document that.

Mode semantics (unchanged structure, now over clauses):
- exclude (default): item suppressed when ANY clause matches.
- include: item suppressed unless at least one clause matches.
- `match_reason()` returns:
  - include miss: the existing exact string `"no include keyword matched"`
  - exclude hit: `matched tag "posse"` for a field clause, and
    `matched "giveaway"` for a bare clause (prefix word present only when
    scoped). Keep the bare-case format identical to today's
    `f'matched "{kw}"'` so existing history texts don't change shape.

Nothing outside `filters.py` changes: `process_echo` already plumbs
`filter_keywords` + `filter_mode` and records `match_reason` output.

## Files

- Modify: `filters.py` (only file)
- Test (new): `tests/test_filter_fields.py`

## Tests

1. Prefix parse: mixed `tag:posse, giveaway` -> 1 field clause + 1 bare.
2. exclude `tag:posse` matches tags list `["release notes", "posse"]`.
3. exclude `tag:internal` matches via categories dict values.
4. exclude `author:jason` matches author field, NOT summary mention.
5. exclude `url:/notes/` matches item link substring.
6. `link:` is an alias for `url:` (same result).
7. exclude `content:keyword` matches content/content_text but NOT title-only
   text.
8. Unknown prefix `release:2` still works as a bare substring keyword
   (formerly a real keyword shape).
9. `tag:` with no value falls back to bare keyword behavior.
10. include mode: keeps item when a tag clause matches, drops otherwise.
11. include miss reason string is exactly `"no include keyword matched"`.
12. exclude hit reason for a bare clause is exactly `matched "<kw>"`, and for
    a tag clause is `matched tag "<kw>"` (assert `match_reason`).
13. Combined clauses: any one match suppresses in exclude mode.
14. Existing `tests/test_filters.py` still passes unchanged (do not edit it).

## Docs (wave 2 wiring, NOT in this lane's files)

Echo form hint, How-To, README mention `tag:`, `author:`, `url:`,
`content:` prefixes.
