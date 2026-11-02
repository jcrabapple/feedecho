# GitHub destination — implementation plan

Branch: `feat/github-destination` (base `master` @ `e5f1046`, v1.76.2)
Issue: [#49](https://github.com/jcrabapple/feedecho/issues/49) — member request via support ticket 13

## Goal

Ship **GitHub as the ninth destination type**: each echo writes one file into a
repository on a branch, so a user can back up a site, a microblog, or a link log
as markdown. Parity target is EchoFeed's GitHub service (token + repo + branch +
committer, with the file path and commit message defined per echo), which is
exactly what the requesting member used before EchoFeed shut down.

Non-goals for v1: no GitHub Enterprise / API base override, no PR flow (commits
go straight to the branch), no file deletion, no multi-file or batched commits,
no image upload, no HTML→markdown converter, no digest delivery for github
echoes.

## Product decisions

- **Body = the echo template, verbatim.** The template is the file. A user writes
  front matter and markdown directly in it. Existing variables cover it:
  `{{ title }}`, `{{ link }}`, `{{ date_short }}`, `{{ tags }}`, `{{ author }}`,
  `{{ content }}` (HTML-stripped text) and `{{ content_html }}` (sanitized HTML —
  inline HTML is valid markdown and preserves links/emphasis, so this is the
  fidelity option, not a bug).
- **Path and commit message are per echo, not per account.** EchoFeed put
  `filePath` in the echo's format; FeedEcho echoes already own the per-item
  output, and one repo commonly receives several sections (`posts/`, `notes/`)
  from different feeds. Account-level path templates would force duplicate
  accounts holding the same token.
- **Overwrite, don't skip.** An existing file at the rendered path is updated
  via the contents API `sha` (re-echos and corrections land). Collisions are the
  user's naming choice; documented.
- **New `| slug` template filter.** A path like `posts/{{ title }}.md` breaks on
  any title containing `/` or `:`. `{{ title | slug }}` gives a safe filename
  component. Small addition to `template_engine.py`, documented on `/howto` and
  the README, and the default path template uses it.
- **No digest.** `delivery_mode` is clamped to `instant`/`drip` for github
  echoes (a digest has no single item to derive a path from).

## Data model (both dialects in `database.py`)

`github_accounts` — one connected repo:

| column | type | notes |
|---|---|---|
| id | PK | |
| name | TEXT NOT NULL | display label |
| repo | TEXT NOT NULL | normalized `owner/repo`, lowercase |
| branch | TEXT NOT NULL DEFAULT 'main' | |
| token | TEXT NOT NULL | `security.encrypt_secret`; never rendered back, never logged |
| committer_name | TEXT DEFAULT '' | optional; blank = token's own user |
| committer_email | TEXT DEFAULT '' | optional |
| user_id | INTEGER NOT NULL DEFAULT 1 | in the `user_id` backfill loop |
| created_at | TIMESTAMP | |

Unique index on `(user_id, repo, branch)` so reconnecting the same target updates
in place. Add `github` → `github_accounts` to `DESTINATION_TABLES_BY_TYPE` in
`app.py` (this is what makes the type valid, counts toward plan caps, and gets
swept by the delete-all loops).

`echoes` — two new columns via `_add_column_if_missing` in BOTH dialect branches:

- `github_path TEXT NOT NULL DEFAULT ''` — path template, required when the echo
  is github, cleared when the destination changes away from github.
- `github_commit TEXT NOT NULL DEFAULT ''` — commit message template, optional;
  blank means the default `Add {{ title }}`.

## `github.py` (new module, mirrors `webhook.py` shape)

Fixed host `https://api.github.com` — no user-supplied base URL, so no new SSRF
surface. All calls sync `httpx` with `DEFAULT_REQUEST_TIMEOUT`.

- `parse_repo(raw)` → normalized `owner/repo`. Accepts `owner/repo`,
  `https://github.com/owner/repo`, with or without `.git`/trailing slash.
  Rejects anything else with a user-facing `ValueError`.
- `normalize_token(raw)` — strip; require ≥20 chars and no whitespace. The live
  call is the real check.
- `connect(token, repo, branch)` → `{full_name, private, default_branch, branch, permissions}`.
  `GET /repos/{repo}` (proves token + visibility; require `permissions.push` true
  when the field is present) then `GET /repos/{repo}/branches/{branch}`. 401 →
  `ValueError("GitHub rejected that token.")`; 404 repo → "not found or this
  token cannot see it"; missing branch → message naming the repo's default
  branch. Connect-time validation is mandatory: a token that saves but cannot
  write fails at 6am inside the scheduler where nobody sees it.
- `test_connection(account)` → re-runs the branch read, returns the head sha.
- `render_path(template, item, feed_name)` → render through
  `template_engine.render_template`, then `normalize_path`.
- `normalize_path(rendered)` → raises `ValueError` on: empty result, absolute
  path (`/...`), any `.`/`..` segment, control chars, a segment >255 chars or a
  total >1024. Normalizes `\` → `/`, collapses `//`, strips segments. Appends
  `.md` when the last segment has no extension. Rejection (not silent repair) is
  deliberate: a path that reaches a `..` came from a bad template.
- `build_commit_message(template, item, feed_name)` → rendered, whitespace
  collapsed, capped at 200 chars, falls back to `Add {{ title }}` value or the
  path.
- `send_file(token, repo, branch, path, content, message, committer=None)`:
  1. `GET /repos/{repo}/contents/{quoted path}?ref={branch}` → 200 gives the
     blob `sha` (update), 404 means create.
  2. `PUT /repos/{repo}/contents/{quoted path}` with
     `{"message", "content": base64, "branch", "sha"?, "committer"?}`.
  3. Returns `{"sha", "html_url", "created"}`. The route's history link is the
     file's `html_url`.
  - Path must be percent-encoded per segment; `quote(path, safe='/')` is not
    enough for `#`/`?`, so encode each segment with `quote(segment, safe='')`.
- Size cap `MAX_FILE_BYTES = 1_000_000` on the encoded body: over the cap is a
  **permanent** rejection with a clear message (truncating a backup file
  silently corrupts it).
- Errors: `GitHubError(DestinationError)`,
  `GitHubAuthError(DestinationAuthError)` (401, and 403 that is not a rate
  limit — message names the "Contents: Read and write" permission),
  `GitHubNotFoundError(DestinationNotFoundError)` (404 repo/branch),
  `GitHubRateLimitError(DestinationRateLimitError)` (403/429 with
  `x-ratelimit-remaining: 0` or a Retry-After; transient),
  `GitHubRejectedError(DestinationError)` (409/422, permanent),
  everything else 5xx/3xx → `GitHubError` (transient). Error strings are static
  and never include the response body's echo of the request or the token.

## Dispatch (`scheduler.py`)

- `_send_github(echo, item, content, account_id, posted_id, claim_token)` — a
  plain `def`, registered in `_DESTINATION_HANDLERS["github"]`.
- Account via `_destination_account("github_accounts", ..., user_scoped=True)`;
  token via `security.decrypt_secret`.
- Path/commit render + `normalize_path` failures are **permanent**
  (`_fail_post(..., permanent=True)`): a broken stored template can never heal
  on retry. Same for the size cap.
- `_guard_claim(...)` immediately before the `PUT` (the GET is idempotent, the
  PUT is the irreversible step).
- Success → `_mark_posted(..., post_url=file html_url)`.
- Rate-limit and 5xx errors ride the existing transient retry path.
- `content_html` needs no special handling: `render_template` already inlines the
  sanitized HTML when the template uses it.
- Echo-route clamps (silent, no 400): `attach_image=0`, `render_html=0`,
  `use_markdown=0` unless mastodon, `delivery_mode` digest → instant for github,
  and `github_path`/`github_commit` stored only for github echoes (cleared
  otherwise) so a destination switch cannot leave a stale path on a mastodon row.

## `app.py` registration points (grep each, in order)

1. import block from `github`.
2. `DESTINATION_TABLES_BY_TYPE` (+ `DESTINATION_LIMITS["github"] = None`).
3. `_get_all_accounts()` — SELECT, return tuple, and **every unpack site** (the
   function's tuple is destructured in more than one place; grep the call).
4. Accounts page: context dict, per-type count query, `discord:`-style option
   label builder (`github:<id>` → `GitHub · owner/repo`).
5. Three history/dashboard query blocks that `CASE`/`JOIN` each destination
   (the ones containing `LEFT JOIN discord_accounts`): name, target column, and
   row renderer.
6. Connect route `POST /api/github-accounts` (validate via `connect()`, encrypt,
   upsert on the unique index) and `POST /api/github-accounts/{id}/test`.
7. Delete route `POST /api/github-accounts/{id}/delete` with the
   dependent-echo guard, plus the delete-table list around the `/accounts`
   handler if that list is explicit.
8. Echo create + edit routes: new `Form("")` params, validation (github requires
   a non-empty path template that renders safely against a sample item), INSERT
   and UPDATE column lists.
9. Destination-options endpoint feeding the echo form.

## Templates

- `accounts.html`: connect form (repo, branch, token as `type=password`,
  committer name/email optional), accounts table (name · repo · branch, Test,
  Delete), token never rendered, JS test handler.
- `echoes.html`: destination `<option value="github">`, `github-fields` block
  (`github_path`, `github_commit`), `github-options` script tag, toggle in the
  destination-switch JS (**disable hidden inputs so a hidden block cannot submit
  over another destination's saved values**), visibility dash for the new type,
  history destination label.
- `app.js` `?v=` cache-bust bump + its pin test if any JS lives there.
- `howto.html`: new section; every literal `{{ ... }}` example wrapped in
  `{% raw %}...{% endraw %}` including inside `placeholder=` attributes.
- `README.md`: destination list + GitHub section (PAT scope, front matter
  example, path template with `| slug`, overwrite semantics).
- `template_engine.available_variables()` + `/howto` variable table: add `| slug`.

## `import_export.py`

- Add `("github", "github_accounts", ["name","repo","branch","token","committer_name","committer_email"], ("repo","branch"))`
  to the account sections, add `"token"` to the credential-key set, and
  re-validate on import (`parse_repo`, branch non-empty). Echo export/import
  field lists get `github_path` and `github_commit`; a github echo missing its
  path is rejected at import, not silently created broken.

## Tests (new `tests/test_github.py` + updates)

1. `parse_repo`: bare, URL, `.git`, trailing slash, uppercase, invalid shapes.
2. `normalize_path`: absolute, `..`, `.`, control chars, empty, >1024 total,
   >255 segment, `\` normalization, `//` collapse, `.md` appended, existing
   extension kept.
3. `build_commit_message`: default, custom, 200-char cap, empty title.
4. `connect`: success, 401, 404, missing branch (message names default), 200
   with `permissions.push` false → refused.
5. `send_file`: create (404 → PUT without sha), update (GET sha → PUT with sha),
   409 → rejected/permanent, 422 → rejected, 403 rate-limited → transient,
   403 scope → auth, 5xx → transient, per-segment path encoding (`#`, `?`, space).
6. Size cap: no network call happens, error is permanent.
7. Scheduler: success marks success with the file URL; render failure permanent;
   missing account permanent; `_guard_claim` runs before the PUT (recording
   fakes); rate-limit goes to transient retry.
8. Echo routes: github requires path; path cleared when switching destination;
   flags clamped (attach_image/render_html/digest).
9. Plan quota: trial destination cap counts github accounts; delete the last one
   frees the slot.
10. Delete guard refuses while a github echo references the account.
11. Import/export round-trip; import of a bad repo or a github echo without a
    path is rejected.
12. Token hygiene: token absent from rendered accounts page, from
    `posted_items.error_message`, and from log records on failure.
13. `| slug` filter: spaces, punctuation, unicode, case, empty, long input.
14. `tests/test_pg_dialect.py::TestPostgresInit` picks up the new table.
15. Red checks (mutate the production line, watch the test fail, restore): path
    traversal rejection, the sha-carrying update PUT, and the size cap.

## Acceptance criteria

- Both suites green, in the documented invocations (single-mode sqlite, multi,
  pg) with `env -u PYTHONPATH`.
- A live end-to-end send against a scratch repo (token supplied via env, never
  printed): one file created with the rendered path/body, a second send updates
  the same path, history row carries the file URL.
- Account connect form and echo form render for a github account; a github echo
  rejected without a path.
- README + `/howto` describe the shipped field names.

## Verification commands

```
unset $(env | grep -oE '^(FEEDCHO|FEEDECHO)_[A-Z_0-9]*' | tr '\n' ' ')
env -u PYTHONPATH .venv/bin/python -m pytest -q tests/test_github.py
env -u PYTHONPATH .venv/bin/python -m pytest -q            # single
env -u PYTHONPATH FEEDECHO_MODE=multi ... -m multi
FEEDECHO_TEST_PG_URL=... .venv/bin/python -m pytest -q -m pg
```

## Risks

- Fine-grained PAT with read-only contents passes `connect()` (a read is all it
  proves) and fails on the first PUT with 403 — mapped to a permanent auth error
  whose message names the required permission.
- Contents API round trip is 2 requests per item against the 5000/hr token
  budget; fine at FeedEcho volumes.
- Files over the cap are refused rather than truncated.
- A repo whose default branch is protected will 409 on PUT; surfaced as a
  permanent rejection with the API's message.
