# FeedEcho — UX Interface & Navigation Audit

**Date:** 2026-09-08
**Scope:** Live interaction review of the running multi-mode app (registration, dashboard, Feeds, Accounts, Echoes, Reader, mobile viewport) at commit `8a3a5ed` (v1.51.1), using a real browser session (Chrome via automated interaction), not just template/CSS reading.
**Method:** Launched the app locally (`FEEDECHO_MODE=multi`, SQLite fallback), registered a fresh account, and walked the primary flows a new user would hit first: landing → sign up → dashboard → Feeds → Accounts → Echoes → Reader, plus a resize to a 390px mobile viewport. Every finding below was confirmed with either a screenshot, a DOM/computed-style check (`getComputedStyle`, element geometry), or a source read — not assumed from templates alone. See [§ Adoption outcome](#adoption-outcome-2026-09-08-post-audit) for what was implemented.

---

## Findings, prioritized by impact

### 1. The brand's "primary" color reads as an error/danger signal — everywhere, starting at signup (Importance: 9/10)

**Problem:** `--primary` derives from `--hue-coral: 35` (`static/css/style.css`), used for the Sign Up button, every input's focus ring, the active nav-tab underline, the trial-status banner border, and the dashboard's "Accounts" stat number. `--danger` sits at hue 25 — only 10° away in the same warm red-orange band, not reliably distinguishable on a neutral background, especially for red-green color vision deficiency.

Confirmed directly: on `/register`, the autofocused (empty) email field showed a thick coral ring and the submit button rendered dark coral — before any interaction. Verified via `getComputedStyle` that this was the deliberate `--primary` focus/button color (not a native `:invalid` state — the ring persisted after typing a syntactically valid email, `validity.valid: true`). Verified via the dashboard that "Accounts: 0" renders in the same coral (`oklch(0.46 0.19 35)`) while "Feeds: 0" — an equally benign zero — renders in calm teal (`oklch(0.47 0.12 195)`), with no apparent logic for the difference. The trial-status banner's border is the same coral (`.trial-banner { border: 1px solid oklch(0.46 0.19 35) }`), giving a routine "13 days left" notice the same visual weight as an actual warning.

**Why it matters:** A brand-new user's very first interaction with the product (signup) visually reads as "something's wrong," and the same signal recurs at nearly every important, benign moment afterward (account status, active nav tab, a zero-state stat).

### 2. On mobile, three of nine nav destinations were invisible with no hint they existed (Importance: 8/10)

**Problem:** Below 640px, `.nav-links` scrolled horizontally with the scrollbar hidden (`scrollbar-width: none`). At a 390px viewport, "Dashboard Feeds Reader Queue Accounts Echoes" exactly filled the visible strip; History, Settings, and How To were pushed off-screen (measured "History" rendering at x=494 in a 500px-wide viewport) with zero edge fade, arrow, or partial-item peek indicating more tabs existed. Confirmed reachable only by an undiscoverable swipe (scripted `scrollLeft` revealed them).

**Why it matters:** Three entire sections — including Settings, where SMTP/API keys/plan info live — had no visible path to them on mobile, while the nav visually looked "complete."

### 3. The Echoes page showed blank, unlabeled dropdowns with no guidance when prerequisites weren't met (Importance: 7/10)

**Problem:** `/echoes` requires a feed and a connected destination first, but if neither existed, the "Feed" and "Destination" `<select>` elements rendered with zero `<option>` tags (confirmed via DOM query: `options: []`) — a blank box, no placeholder. Feeds and Accounts pages both handle their own empty state with a clear message ("No feeds yet. Add one above to get started." / "No accounts yet. Connect a Mastodon instance..."); Echoes — the "put it all together" step in the product's own onboarding pitch — didn't.

### 4. A working "manual connect" option was invisible — styled as plain text (Importance: 6/10)

**Problem:** On Accounts → Mastodon, "Or add Mastodon manually with a token" is a functional `<details>/<summary>` toggle (confirmed by clicking it — it expanded a full second form), but rendered with no color, no underline, no marker — `color: var(--text-muted)` and no chevron. Only `cursor: pointer` hinted it was interactive, which isn't visible in a static view.

### 5. One dropdown rendered as a raw, unstyled native `<select>` (Importance: 5/10)

**Problem:** The Feeds page's "Folder" dropdown (`<select name="folder_id" id="feed-folder">` in `templates/feeds.html`, no CSS class) fell back to the OS-native control appearance — a solid dark pill next to light, custom-styled text inputs — because `.inline-form` (the form's class) styled `input`/`button` but not `select` (`static/css/style.css:426`, confirmed by grep). Every select checked elsewhere in the app (Echoes' Feed/Destination pickers) was correctly styled — an isolated gap in one form, not a systemic issue.

### 6. Placeholder text meant to show the URL format was cut off (Importance: 4/10)

**Problem:** The feed URL field's placeholder (`https://example.com/feed.xml`) showed only `https://example.com/fee` — confirmed at both 390px and 1400px widths, so it's the field's fixed proportion within the row (`.inline-form input { flex: 1 }`, giving Name/URL/Poll-interval equal width regardless of content length), not a small-screen artifact.

### 7. Native browser validation errors don't match the app's design language (Importance: 3/10 — noted, not fixed)

**Problem:** Submitting an invalid feed URL triggers Chrome's built-in validation bubble ("Please enter a URL.") — OS-styled, generic copy, dropped into an otherwise fully custom UI.

### 8. Two adjacent toggle-button groups on the Reader page used different "selected" visual languages (Importance: 3/10)

**Problem:** The view-filter tabs (Unread/All/Starred/Today) showed the active one with a solid coral fill (`.reader-tab.active { background: var(--primary) }`); the display-option toggles right below (Compact/Auto-read/Full text) showed "on" as a wash-tint outline instead (`.btn-sm[aria-pressed="true"] { background: var(--primary-wash); border-color: var(--primary) }`) — same row, same concept, two conventions.

### Positive: the Reader's keyboard-shortcuts modal is genuinely well designed

The `?` button opens a clean, well-organized shortcuts + search-operator reference (`j`/`k` navigation, `is:starred`, `feed:name`, etc.) — exactly the kind of feature a design-literate, power-user audience appreciates. Its only weakness is discoverability: reachable only via a small, unlabeled `?` button with no other hint (onboarding, empty states) that it exists. Not changed in this PR — a "Press `?` for shortcuts" hint somewhere on first Reader visit would be a reasonable low-cost follow-up, but is a content/onboarding decision more than a bug fix.

---

## Adoption outcome (2026-09-08, post-audit)

**Landed (this PR):**
- **§2** (mobile nav discoverability): added `.nav-links-wrap`, a non-scrolling wrapper around `.nav-links` that positions a fixed-edge fade (`::after` gradient into `var(--surface)`) at the visible right edge of the tab strip — stays put regardless of the inner strip's scroll offset, giving a permanent "more content" cue. `.nav-links-wrap { display: contents }` by default so desktop's `.nav-links { margin-left: auto }` still works as a direct flex child of `.navbar`; the mobile media query gives the wrapper its own box (`order: 4`, `flex: 1 1 100%`, `position: relative`) and moves `margin-left: 0` onto `.nav-links` itself. Cache-buster bumped `style.css?v=56` → `?v=57` (and every test asserting that literal string updated to match, per the project's existing convention of pinning it). This is a static fade (doesn't disappear once scrolled to the end) — a scroll-position-aware version is a reasonable follow-up, noted in the CSS comment.
- **§3** (Echoes empty-state guidance): `templates/echoes.html` now computes `has_destination` from the seven per-platform account lists already passed into the template (no route/Python change needed) and, when `not feeds or not has_destination`, replaces the create-echo form with a message naming exactly what's missing ("at least one feed and one connected destination" / "at least one feed" / "a connected destination") plus links to `/feeds` and/or `/accounts`. Also fixed a redundant-message bug this introduced: the pre-existing "No echoes yet. Create one above." empty state (for when the form IS showing but no echoes exist yet) is now gated on `feeds and has_destination` too, so it no longer says "above" when the form isn't there.
- **§4** (manual-connect affordance): `.manual-section summary` now gets a chevron (`::after { content: "▾" }`, rotated when closed, matching the pattern already used one level up for the account-type accordions themselves), an underline on hover, and a visible focus ring — same treatment as every other disclosure control in the app, applied for consistency rather than invented fresh.
- **§5** (unstyled select): added `select` to the `.inline-form` input/button/focus selector list (`static/css/style.css:426`) — the `form-row` equivalent rule already included `select` (confirmed by reading the existing focus-visibility test), so this brings `.inline-form` in line with the project's own established convention rather than introducing a new one.
- **§6** (truncated placeholder): `#feed-url { flex: 2.5 }` gives the URL field a larger share of the row than poll-interval/folder need, instead of every field getting an equal `flex: 1`.
- **§8** (Reader toggle consistency): `.reader-toolbar-right .btn-sm[aria-pressed="true"]` now uses solid `var(--primary)` fill + `var(--primary-contrast)` text (matching `.reader-tab.active`) instead of the wash-tint outline.
- **§1** (color hue), **partially, flagged for review**: shifted `--hue-coral` from `35` to `50` — roughly midway between `--hue-red` (25) and `--hue-gold` (85), so it no longer sits adjacent to either. This is a **brand-color judgment call, not a bug fix** — implemented as a single, isolated, easily-tunable CSS custom property (one line) specifically so it's simple to adjust or revert if a different hue is wanted; not treated as a settled decision. **Please review this one specifically.**

**Not implemented:**
- **§7** (native validation styling): left as-is. Replacing browser-native validation UI risks an accessibility regression (screen readers handle native `:invalid`/validation messages well; a custom replacement needs its own `aria-live` and focus-management work to not be a net loss) that's a bigger, more careful effort than this PR's other fixes warrant. Noted as a real but lower-priority (3/10) finding for a future, dedicated pass.
- Reader shortcuts discoverability (mentioned under the positive finding above): a content/onboarding decision, not a bug — left for product judgment.

**Verification:**
- Full test suite: 1502 passed / 36 skipped before any change; 1503 passed / 36 skipped after (one new test added: `test_nav_links_wrap_has_scroll_edge_fade`).
- Four pre-existing tests initially broke from the mobile-nav restructuring and the `.inline-form` selector change (`test_mobile_nav.py`'s order/overflow assertions, `test_ux_p1_fixes.py`'s focus-rule text match, and four separate cache-buster version-string assertions across `test_admin_mobile_actions.py`/`test_mobile_form_layout.py`/`test_trial_limits_visibility.py`/`test_ux_p2_fixes.py`) — all updated to match the new, deliberate structure rather than reverting the fix to avoid touching tests.
- CSS verified brace-balanced (no syntax errors) after all edits.
- All nine primary routes (`/`, `/feeds`, `/accounts`, `/echoes`, `/reader`, `/settings`, `/history`, `/queue`, `/howto`) verified to return 200 with no server-error markers in the response body, via a scripted register → browse session against a live local instance.
- The Echoes empty-state logic specifically verified across two states (no feed + no destination; feed added, no destination) by adding a real feed via the API and re-checking the rendered message text and links updated correctly.
- **Not re-verified visually**: the Chrome browser session used for the initial audit disconnected mid-session and did not reconnect, so the CSS/template fixes above were validated functionally (tests, curl, computed-style checks earlier in the audit) but not re-screenshotted after implementation. Flagging this explicitly rather than claiming a visual check that didn't happen — worth a manual look before merging, particularly the color-hue change (§1) and the nav scroll-fade (§2), since both are visual-only effects that don't fail a functional test if wrong.
