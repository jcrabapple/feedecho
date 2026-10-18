# F3 — Template URL / hashtag filters (template_engine.py)

## Goal

Three new sandboxed-registered Jinja filters for echo templates:
`clean_url`, `utm`, and `hashtags` (exclude-aware list form). Pure
functions; no new deps.

## Files

- Modify: `template_engine.py` (add filters + register + extend
  `available_variables()`)
- Test (new): `tests/test_url_filters.py`

## 1. `clean_url` — strip tracking params

Signature: `{{ link | clean_url }}` (no args regular usage), optional
second arg `extra` (a comma-separated STRING is easiest from Jinja —
e.g. `{{ link | clean_url('ref,fbclid') }}`) — accept a `str` or a list
and normalize to a set.

```python
_DEFAULT_TRACKING = ("utm_source", "utm_medium", "utm_campaign", "utm_term",
                     "utm_content", "fbclid", "gclid", "msclkid", "dclid",
                     "mc_cid", "mc_eid", "igshid", "twclid", "vero_id", "spm")
```
- Any param whose lowered key STARTSWITH `utm_` is stripped (covers new
  utm addons without enumerating).
- Additionally keys in `_DEFAULT_TRACKING` are stripped.
- `extra` keys (lowered) are ALSO stripped.
- PRESERVATION: everything else must survive byte-for-byte — use
  `urllib.parse.urlsplit`, rebuild `query` by iterating `query.split("&")`
  and dropping only pieces whose normalized key (`urllib.parse.unquote(keypart).lower()`)
  is in the strip set, then `urlunsplit`. This keeps `?v=dQw4w9WgXcQ`,
  order, spaces encoding, `;` separators in odd receivers, and fragments
  (`#start-of-post`) untouched.
- When the query is preserved as-is (no pieces dropped), the returned
  string must be BYTE-IDENTICAL to the input (assert in tests).
- Non-URLs, empty values, or `urlsplit` ValueError: return the input
  unchanged (defensive; filters must never raise — templates post to real
  social accounts).

## 2. `utm` — append UTM params

Signature: `{{ link | utm('mastodon') }}` positional source, or named:
`{{ link | utm(source='mastodon', medium='feedecho', campaign='blog', term='x', content='y') }}`.
- Only the five `utm_*` keys are produced; skip keys whose value is
  None/empty-string.
- MERGE semantics: parse existing query with
  `parse_qsl(query, keep_blank_values=True)`; NEW/utmspecified keys
  override same-named existing entries; untouched others keep their
  original relative order first, then the utm additions. Rebuild with
  `urlencode(params, doseq=True)`.
- No utm args at all (all None) -> return input UNCHANGED byte-for-byte.
- urlsplit ValueError / non-string / empty -> return input unchanged.

## 3. `hashtags` — exclude-aware tag-to-hashtag filter

- Name: register on `env.filters["hashtags"]` (the context var `{{ hashtags }}`
  and this filter live in separate Jinja namespaces; document).
- Value shape: a list/tuple of tags (typical `{{ item['tags'] }}`), OR a
  string of comma/space/`;`-separated tags. Normalize strings into a list
  (split on `,`, `;`, whitespace; strip empties).
- Optional kwarg `exclude` (same flexible shape): tags matching after
  normalization + NFKC/casefold comparison are dropped BEFORE
  hashtagging.
- Reuse `_format_hashtags(...)` for the cleaned output — dedupe,
  punctuation-strip, unicode-aware — but CAREFULLY: `_format_hashtags`
  is the existing public helper this module already exposes to the
  context builder; call it (don't reimplement).
- Non-list non-string input or empty -> return "".
- The context var `{{ hashtags }}` (existing built from the feed's
  full tag list) behavior MUST NOT change — verify via render_template
  with an item with tags; still works.

## 4. `available_variables()` additions

Append rows:
```python
{"var": "{{ link | clean_url }}", "desc": "Strip UTM/tracking params from a URL (utm_*, fbclid, gclid, ...); everything else is preserved"},
{"var": "{{ link | utm('mastodon') }}", "desc": "Append tracking params to a URL: utm(source, medium=, campaign=, term=, content=)"},
{"var": "{{ item['tags'] | hashtags(exclude='internal,heartbeat') }}", "desc": "Convert a tag list to #hashtags, dropping the listed ones (comma/whitespace separated)"},
```

## Tests

`tests/test_url_filters.py`:

1. clean_url: strips utm_* + default list params, preserves non-tracking
   query params + fragment + path/host/scheme.
2. clean_url byte-identity when nothing to strip (URL with `?v=` etc).
3. clean_url extra kwarg via comma string.
4. clean_url with EMPTY input, with a garbage string, with a bare
   relative path — unchanged, no exception.
5. utm: appends over an existing query; merges (existing utm_source
   overridden); keeps others + fragment; byte-identical when no kwargs.
6. utm on garbage -> unchanged.
7. hashtags filter: list input with exclude list (mixed list/string),
   string input "release notes, posse" works, empty input -> "",
   unknown input type -> "".
8. context var `{{ hashtags }}` from an item with multiple tags still
   yields `#one #two` (unchanged behavior pin).
9. "unit-level only" — none of these hit database.py; pure preview calls.
10. Red-before-green evidence per coder rules.
