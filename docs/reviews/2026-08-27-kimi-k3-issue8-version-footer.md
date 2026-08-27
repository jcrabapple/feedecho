# Review: issue #8 — version in footer

## Findings

**1. LOW — `app.py` `render()` (~line 421) + `templates/base.html` footer: the anon-withholding control is defeated by public `/register`.**
The comment says the version is withheld because "an exact version handed to anonymous visitors is a free list of which fixed bugs to try" — but multi mode has an open, unauthenticated `/register` (it's in `_MULTI_EXEMPT_PATHS`). Any anonymous attacker can register, get a session, pass the gate, and receive the version. So the control costs complexity and buys nothing against its own stated threat model — it only protects against truly passive scrapers. Minimal fix, pick one: (a) accept that the version is not secret (it's also trivially fingerprintable via static asset hashes) and render it unconditionally, dropping the `authed` gate; or (b) if you genuinely want it withheld, gate on `is_admin` rather than `authed` — which also matches the issue text ("so that the **admin** knows"). As written, the hardest role to obtain on the hosted service (registered user) still sees it.

**2. LOW — `app.py` `AuthMiddleware._multi` (exempt branch, ~line 276) and `_single` exempt branch (~line 231): credentialed viewers on exempt paths are treated as anonymous.**
Because exempt paths short-circuit *before* session/token parsing, a logged-in admin viewing the public `/about` page in multi mode — which is exactly where the footer now intentionally appears — loses the version. Same shape in single mode with `AUTH_TOKEN` set: any page in `_AUTH_EXEMPT_PATHS` ignores a perfectly valid cookie. The direction is safe (erring toward hiding), but it's inconsistent: the same person sees the version flicker in/out depending on which page they're on. Minimal fix: on the exempt branch, best-effort parse the cookie/token when present and set `authed` before falling through (or accept and document the behavior).

**3. LOW (a11y/UX) — `templates/base.html` version link (the `target="_blank"` anchor, ~line 64).**
The link opens a new tab but nothing in the accessible name announces that; the `title` attribute is not reliably exposed to screen readers and is invisible to keyboard/touch users. Minimal fix: add `aria-label="FeedEcho v{{ app_version }} — release notes on GitHub (opens in new tab)"` or a visually-hidden "(opens in new tab)" span. Also note the link text "v1.13.6" alone is a weak link-purpose cue (WCAG 2.4.4); the surrounding "FeedEcho" text mitigates this only partially.

**4. LOW — `templates/base.html` footer on the single-mode-with-token `/login` page: `How To` is a dead-end link.**
Single mode previously had no footer, so this is new: on the public login page (AUTH_TOKEN set), the footer offers `/howto`, which is not auth-exempt and 302s straight back to `/login`. In multi mode the same loop pre-existed, so I'm not assigning blame there — but the diff exports the defect to single mode. Minimal fix: either add `/howto` to the single-mode exempt set (it's documentation, on par with `/about` in multi) or suppress footer links unless `authed`.

**5. LOW — `tests/test_version.py` `TestVersionConsistency`: the "every remaining copy" guarantee is only as good as the hardcoded file list.**
The four pinned files (flake.nix, nix/package.nix, nix/README.md, docker-compose.multi.yml) are enforced, but nothing prevents a *fifth* hardcoded version sneaking into Dockerfile, root README, a workflow, CHANGELOG, etc. — CI would stay green. The docstrings in `_version.py` and the test overclaim ("pins every one of them"). Minimal fix: replace/augment the per-file asserts with a repo scan that finds every `\d+\.\d+\.\d+` occurrence outside an allowlist (`_version.py`, lockfiles, tests) and asserts each equals `__version__`. Until then, the test is non-vacuous for what it names — the rest of the suite's footer/trial tests behave, I found no test that passes vacuously — but the completeness claim is unfalsifiable.

## Self-hoster upgrade breakage

**None found.** The dynamic-version move is sound for them: hatchling resolves `1.13.6` from `_version.py` (verified per your facts, and the test suite would fail on a resolution mismatch via `test_fastapi_reports_it`), the Dockerfile copies source rather than pip-installing so top-level `import _version` resolves in-image, the Nix pin keeps `package.nix`/`flake.nix` coherent via the new consistency tests, and the CSS cache-buster was bumped (`?v=12` → `?v=13`) so the new footer styling won't be served stale. Behavior-wise, a self-hoster with no `FEEDCHO_AUTH_TOKEN` gets `authed = True` for every request, which is exactly the documented "no auth = operator" semantics — that is not a leak.

## Answers to your challenged areas

1. **The `authed` flag** is set on exactly the paths that pass auth and never on a wrong-token/register-anonymous path — the short-circuits are all in the "safely withheld" direction. The only wrinkle is the exempt-path asymmetry in finding 2.
2. **Footer/context dependencies:** only `tests/test_about.py` encoded "footer is multi-only", and it was flipped accordingly. No other reader of `context` keys is affected; `kwargs` still wins over the new defaults, and `{% if app_version %}` is safe under the default undefined.
3. **Packaging:** per your verified facts, working; I have no justification to contradict them.
4. **Tests:** non-vacuous, with the completeness caveat in finding 5.
5. **Template:** findings 3 and 4 cover the real (small) issues; markup/escaping is otherwise fine — the URL is a hardcoded constant behind autoescape.
---

## Triage (applied 2026-08-27)

**1. Anon-withholding defeated by public /register — REAL, fixed as suggested (b).**
Kimi is right that `authed` was the wrong line: `/register` is in
`_MULTI_EXEMPT_PATHS`, so anyone could sign up and read the version. The gate is
now "the person who can actually upgrade this deployment": single mode shows the
version to any viewer past the auth gate (they are the operator), multi mode
shows it only to `is_admin`. That also matches the issue's own wording. Covered
by `test_withheld_from_a_signed_in_tenant` / `test_shown_to_an_admin`.

**2. Credentialed viewers on exempt paths look anonymous — REAL, accepted here,
FIXED in the following release** (see
`docs/reviews/2026-08-27-kimi-k3-nav-gating.md`: the middleware now identifies
the viewer on /login, /register, /about and /verify-email, so an admin does get
the version footer on /about).

Original reasoning for deferring it: Making the exempt branch parse the session would also
start rendering the multi-mode nav chrome (email + logout) on `/login`, `/about`
and `/register` for logged-in users — a wider behaviour change than a footer
feature should carry. The asymmetry is now stated in the `render()` comment: the
public pages never show a version, every other page does.

**3. New-tab link not announced — REAL, already applied before the review
landed.** The anchor carries `<span class="sr-only"> — release notes on GitHub,
opens in a new tab</span>`, which also fixes the weak WCAG 2.4.4 link purpose
(the accessible name is no longer just "v1.13.6"). Asserted in
`test_footer_shows_version_and_release_link`.

**4. `/howto` is a dead-end link for anonymous viewers — REAL, fixed for the
footer.** The footer only offers `/howto` when `authed`, and renders at all only
when `MULTI or authed`, so single mode's login page has no footer instead of a
footer full of redirects. Note for a future change: the **top nav** has linked
`/howto` (and Dashboard/Feeds/Settings) to anonymous viewers since long before
this diff — same bounce, wider blast radius, left alone deliberately.

**5. Consistency test's completeness claim was unfalsifiable — REAL, fixed.**
Added `test_no_stale_version_anywhere_in_the_tree`: it walks `git ls-files` and
matches the shapes that actually carry this project's version (GHCR image tag,
`archive/refs/tags/vX`, nix `rev`/`version`), asserting each equals
`__version__`, with an allowlist for files that deliberately reference a past
release. The `_version.py` docstring no longer overclaims.

Not a finding, verified separately: `pip install -e ".[dev]"` on Python 3.12
resolves the dynamic version (`importlib.metadata.version("feedecho")` ->
1.13.6) and both CI job commands pass against that editable install, so the
hatchling change cannot break the CI matrix.
