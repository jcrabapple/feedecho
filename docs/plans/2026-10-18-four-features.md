# Four-Feature Batch: scope + implementation plan

Branch: `feat/four-features` (worktree off master). Current base: v1.75.1, commit 272c1bd.
Test baseline on HEAD at plan time: 2015 single + 99 multi + 52 pg collecting locally; CI green.
Release target after batch: 1.76.0 (bump via `scripts/bump_version.py` ONLY after gates + suites).

## The four features

1. **F1: Echo filter expansion** — field-scoped filter syntax (`tag:`, `author:`, `url:`/`link:`, `content:`) in `filters.py`, plus docs on the echo form and How-To. Bare-keyword behavior for existing users stays byte-identical.
2. **F2: Auto-queue + Review delivery modes** — `delivery_mode` gains `queue` and `review` for any destination, bridging scheduler echoes into `queued_posts` (curation queue). Reuses the Reader Compose enqueue machinery: one-shot echo at enqueue (idempotency key), `next_free_slot` slot math, `_flush_queue` consumer, post-now/cancel/edit/reorder endpoints.
3. **F3: Template URL hygiene + hashtag filters** — three new sandboxed Jinja filters in `template_engine.py`: `clean_url` (strip tracking params), `utm` (append UTM params), `hashtags` (exclude-list tag-to-hashtag converter). Surfaces in the echo variables table and docs.
4. **F4: Feed discovery, preview, and `?add=` deep link** — HTML `<link rel="alternate">` autodiscovery in `feed_parser.py`, `GET /api/feeds/discover`, `POST /api/feeds/preview`, auto-resolution inside `add_feed`/`edit_feed`, `?add=&name=` prefill, and a copyable bookmarklet on `/feeds`.

## Feature workflow (standing)

- Working tree is the feature branch; `systemctl --user restart feedecho` after each commit wave.
- Review gates after all features merge to the branch: BACKEND diff → Gemini 3.8 Flash via `agy` (headless, `--dangerously-skip-permissions`, from /tmp, `--print-timeout` with unit); FRONTEND (templates/app.js) diff → Kimi K3 via OpenRouter (max_tokens 60000 + reasoning cap). Triage every finding against source; **second smaller gate on the fix-delta** before closing.
- Full suites on the FINAL tree: sqlite suite + pg suite (`FEEDECHO_TEST_PG_URL=postgresql://feedecho:***@localhost:55432/feedecho`, `podman start fe-pg4` first). Suites run with `env -u PYTHONPATH .venv/bin/python -m pytest -q` (Hermes PYTHONPATH trap). Anything landing after a suite starts invalidates it; re-run.
- Merge feature branch → master as one `--no-ff` merge. Bump, tag `v1.76.0`, push via SSH remote, GHCR multi-arch, VPS deploy (overlay `~/feedecho-hosted`: BASE_TAG + compose image 1.76.0, `deploy:` + `docs:` Handoff entry, scp billing/ etc, on-box `docker build -f Dockerfile.hosted`), GitHub release after prod verification. Then Mastodon announcement via the release-announcements skill.

## Coder rules (all lanes)

- NO `git` commands at all — orchestrator owns the tree and history. NO `systemctl`, no full test suite, no other lanes' files.
- Red-before-green evidence: MUTATE the pre-change source in place with a greppable `# REDCHECK` marker (comment out the guard under test), run the new test, confirm the intended test fails ON the mutated code, then restore the original line and `grep -c REDCHECK` == 0. Do NOT use `git stash` for proofs.
- Run ONLY your own test file: `cd /home/jason/projects/feedecho && env -u PYTHONPATH .venv/bin/python -m pytest tests/<your_test>.py -q`. Failures in files you don't own: report, never fix.
- Repo is dual-dialect (sqlite + Postgres). No raw `cursor.rowcount` assumptions; use the `datetime`/`str` normalization helpers `timestamp_str` / `as_utc_naive` when reading TIMESTAMP columns back.
- No commits, no secrets, no `pyproject.toml` deps without approval; no jq/py packages. Keep diff tight: no drive-by cleanups outside the files listed below.
