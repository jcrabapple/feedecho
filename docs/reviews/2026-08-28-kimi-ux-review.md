# FeedEcho UX / Design / Accessibility Review: Kimi K3

**Date:** 2026-08-28 · **Reviewer:** Kimi K3 via OpenRouter (5 parallel batches, 5/5 clean finish) · **Code state:** commit 1530dda (v1.25.1)
**Scope:** UX/interaction design, accessibility, visual/interaction patterns for a design-literate audience. Security, backend correctness, and code style explicitly out of scope.
**Triage:** All 34 findings below were verified against the actual source (grep/template checks) before inclusion. Zero false positives.

Priorities reflect likely impact on user experience, not implementation complexity.

---

## P1: Blocks or badly hurts a core workflow / excludes users

### 1. Form fields have no visible focus indicator (a11y): `static/css/style.css`
- **Evidence:** `.inline-form input:focus { border-color: var(--primary); outline: none; }` (L361), `.form-row input:focus … outline: none` (L398), `.auth-input:focus { outline: none; … }` (L648)
- **Problem:** Every text input, select, and textarea removes the focus outline and replaces it with only a subtle 1px border-color change (neutral → coral).
- **Why it matters:** Keyboard and low-vision users cannot reliably see which field has focus; a 1px color shift fails WCAG 2.4.7, especially in dark theme where `--border` (L 0.33) and `--primary` (L 0.72) are hard to distinguish at a glance.
- **Fix:** Keep the color change but add `outline: 2px solid var(--primary); outline-offset: 1px;` (or a 2px ring via box-shadow) to all three focus rules.

### 2. Registration form has zero labeled inputs (a11y): `templates/register.html`
- **Evidence:** `<input type="email" name="email" placeholder="Email" required … autocomplete="email" autofocus class="auth-input">` plus password/confirm/invite_code inputs. `grep -c "<label" register.html` = 0 (login.html has 3).
- **Problem:** None of the four registration inputs has a `<label>` or accessible name; placeholders are the only cue.
- **Why it matters:** Screen reader users hear unnamed fields and must guess. Placeholders disappear once typed, so sighted users lose context when correcting errors: especially between "Password (8+ characters)" and "Confirm password".
- **Fix:** Add visible `<label for>` elements with matching ids, as login.html already does.

### 3. All async feedback is delivered via blocking `alert()` (ux): `static/js/app.js`
- **Evidence:** 28 `alert(` calls: test results (`testAccount`, `testBlueskyAccount`, `testMicroblogAccount`, `testFeed`), `initFeed`, `fetchNow`, `retryPost`, `giveUpPost`, `toggleEcho`.
- **Problem:** Every async action's outcome: including multi-line feed preview data (`Feed: … Type: … Items: …`): is crammed into a modal dialog.
- **Why it matters:** Alerts block the page, vanish on dismiss (users can't re-read a preview or error detail), steal focus, and feel jarring to a design-focused audience.
- **Fix:** Render results inline next to the triggering button in a `role="status"` region with the existing `.alert` success/error styling (the `previewTemplate` pattern already exists); reserve dialogs for confirmations.

### 4. Action buttons show no in-progress state (ux): `static/js/app.js`
- **Evidence:** Only `testAltText` does `btn.disabled = true; btn.textContent = 'Testing...'` (L147/156). `testAccount`, `testFeed`, `fetchNow`, `pauseFeed`, `toggleEcho`, `retryPost`, `giveUpPost` fire fetch with no disabled state or spinner.
- **Problem:** On a slow connection nothing visibly happens after clicking.
- **Why it matters:** Users double-click (firing duplicate POSTs) or assume the app is broken. The correct pattern exists in the same file but wasn't applied anywhere else.
- **Fix:** Extract a shared helper that disables the clicked button, swaps in a "Working…" label, and restores it in `finally`; apply to every async handler.

---

## P2: Friction and confusion that slows people down

### 5. "Init" is an unexplained prerequisite for the core workflow (ux): `templates/feeds.html`
- **Evidence:** `<th>Item ID Set</th>` (L26), `<span class="badge badge-warning">No: click Init</span>` (L48), `<button class="btn-sm" onclick="initFeed({{ feed.id }})">Init</button>` (L55)
- **Problem:** Internal jargon leaks into the UI: an unexplained "Init" action gates whether a feed tracks items, and the column header means nothing to users.
- **Why it matters:** A core workflow depends on understanding an unexplained action; skipping it apparently leaves the feed not working, with no hint why.
- **Fix:** Rename the column to "Tracking", label the button "Initialize tracking", give the badge a plain state ("Not initialized"), and add one line of helper text explaining what it does.

### 6. Mobile tables lose all header semantics (a11y): `static/css/style.css`
- **Evidence:** `@media (max-width: 640px) { .data-table thead { display: none; } … content: attr(data-label); }` (L289–311)
- **Problem:** `display: block` on all table elements strips table semantics, and column headers become visual-only `::before` pseudo-content.
- **Why it matters:** Generated content is not exposed as a header association by many screen-reader/browser combinations: narrow-viewport users get bare values with no meaning.
- **Fix:** Keep the card layout visually but include visually-hidden real text (sr-only header copy or `aria-label` on cells) instead of relying on `attr()` pseudo-content.

### 7. Inline edit destroys focus (a11y): `static/js/app.js`
- **Evidence:** `editFeed` L111 and `editEcho` L222 replace `row.innerHTML`, destroying the focused Edit button; cancel/save restore HTML without returning focus.
- **Problem:** Focus drops to `<body>` when the edit form opens; keyboard/AT users must re-navigate from the top of the page to the form they just opened, with no announcement it appeared.
- **Why it matters:** Standard keyboard trap-and-dump; makes inline editing effectively unusable without a mouse.
- **Fix:** After replacing innerHTML, `row.querySelector('input, select, textarea')?.focus()`; on cancel/save, restore focus to the row's Edit button.

### 8. Cancel buttons styled as destructive (ux): `static/js/app.js`
- **Evidence:** L127 `<button type="button" class="btn-sm btn-danger" onclick="cancelFeedEdit(${feedId})">Cancel</button>` and L312 same for `cancelEdit`.
- **Problem:** Cancel: the safe action: wears solid-red danger styling identical to delete.
- **Why it matters:** Red signals data destruction; users hesitate on the safe option, and the visual hierarchy implies Cancel is worse than Save. Exactly the inverted affordance a design-literate audience notices.
- **Fix:** Style Cancel as a neutral secondary `.btn-sm`; reserve `.btn-danger` for delete/give-up.

### 9. Digest echoes open their edit form showing the drip-rate field (ux): `static/js/app.js`
- **Evidence:** `editEcho` (L187) renders `#edit-drip-fields-${echoId}` with no initial style; `toggleEditDest` (L330) only runs `onchange` of the selects (L229, L299).
- **Problem:** For a digest echo, "Max posts per hour" is visible on first open alongside the digest explanation ("batch items into one email, sent hourly"): contradictory settings shown together.
- **Why it matters:** Users can't tell which setting governs delivery cadence.
- **Fix:** Call `toggleEditDest(echoId)` once at the end of `editEcho` (or compute the initial display from `deliveryMode`, mirroring `mastoStyle`/`emailStyle`).

### 10. SMTP TLS select and port input actively contradict each other (ux): `templates/settings.html`
- **Evidence:** `<option value="1">STARTTLS (port 587)</option>` / `<option value="0">SSL/TLS (port 465)</option>` (L44) beside a free-editable `<input type="number" name="smtp_port" …>` (L22) that never syncs.
- **Problem:** Selecting "SSL/TLS (port 465)" leaves the Port field at 587.
- **Why it matters:** The UI invites an inconsistent host/port/security combo; the failure surfaces later as a mysterious email-delivery error.
- **Fix:** Auto-update the port when TLS mode changes (small script), or drop port numbers from option labels and list typical ports in helper text.

### 11. Mastodon fields flash on every page load, and are permanently wrong without JS (ux): `templates/echoes.html`
- **Evidence:** `<div class="form-row" id="mastodon-fields">` (L34) has no `display:none`, unlike `email-fields` (L51), `bluesky-fields` (L60), `microblog-fields` (L103), `digest-fields` (L112); visibility is only fixed by `toggleDestFields()` at L250.
- **Problem:** Server-rendered HTML shows irrelevant Mastodon fields whenever Mastodon isn't the first destination option; with JS blocked the form is permanently wrong.
- **Why it matters:** A flash of wrong fields on every load plus invalid submissions possible without JS.
- **Fix:** Render the initial hidden state server-side based on the first `#destination-type` option; keep `toggleDestFields()` for changes only.

### 12. Content-warning field shown for all destinations but documented Mastodon-only (ux): `templates/echoes.html`
- **Evidence:** L94–97: the CW row sits outside `#mastodon-fields`, is never toggled, and its hint says "Applied as Mastodon spoiler text".
- **Problem:** Users creating email/Bluesky/micro.blog echoes can't tell if the warning does anything.
- **Why it matters:** Silent no-op settings break trust; users either set it expecting behavior they won't get, or avoid a useful field.
- **Fix:** Move the row into `#mastodon-fields`, or extend the hint: "Applied as spoiler text on Mastodon; ignored for other destinations."

### 13. Destructive admin actions need no confirmation (ux): `templates/admin.html`
- **Evidence:** Suspend `/admin/users/{{ u.id }}/suspend` (btn-danger), `/demote`, and `/admin/invites/revoke` forms all submit on a single click; `grep "confirm(" admin.html` returns nothing.
- **Problem:** One misclick in a dense table of small buttons suspends a user, strips an admin, or kills an invite code.
- **Why it matters:** Recovery requires noticing and manually reversing; blast radius is instant.
- **Fix:** Add `onsubmit="return confirm('Suspend this user?')"` (or a proper dialog) to Suspend/Demote/Revoke; keep trivially reversible actions one-click.

### 14. Persistent config banners use `role="alert"` (a11y): `templates/settings.html`
- **Evidence:** "SMTP not configured…" (L13) and "Vision API not configured…" (L95) are static state banners marked `role="alert"`.
- **Problem:** Screen readers announce them assertively as fresh errors on every page load.
- **Why it matters:** Alarm fatigue: habituated users learn alerts here don't mean real problems.
- **Fix:** `role="status"` (or no live role) for persistent state; reserve `role="alert"` for new errors.

### 15. Success banners reappear on every refresh (ux): `templates/accounts.html`
- **Evidence:** L10–32: banners keyed off `request.query_params.get('status')` with no dismissal and no URL cleanup.
- **Problem:** A stale "connected successfully" shows again on refresh, reload, or when a bookmarked URL is opened.
- **Why it matters:** The message implies an action just happened when it didn't.
- **Fix:** `history.replaceState` to strip the parameter after render, or POST-redirect-GET to the clean `/accounts` URL.

### 16. Current-page nav state is color-only, with no `aria-current` (a11y): `templates/base.html` + `static/css/style.css`
- **Evidence:** `{% if request.url.path == '/' %}active{% endif %}` (base.html); `.nav-links a.active { color: var(--primary); }` (L135); no `aria-current` anywhere; no `scope` of other non-color cue.
- **Problem:** The only "you are here" signal is a muted-gray → coral color shift; `--text-muted` (L 0.68) vs `--primary` (L 0.72) is nearly invisible in dark theme.
- **Why it matters:** Fails WCAG 1.4.1 (use of color); screen readers get no programmatic current-page indication.
- **Fix:** Render `aria-current="page"` on the active link and add a non-color cue (`font-weight: 600; box-shadow: inset 0 -2px 0 var(--primary);`).

### 17. Placeholder-only labeling across forms (a11y/ux): `templates/feeds.html`, `templates/accounts.html`
- **Evidence:** Feeds add-form: `<input … placeholder="Feed name" required>` with `class="sr-only"` labels; accounts: `oauth-instance` input (L39) and inline forms at L49–55, 66–68, 78–82, 92.
- **Problem:** Visible labels are placeholders alone; once filled, there's no persistent label (e.g. distinguishing "Display name" vs "Username" in the manual Mastodon form).
- **Why it matters:** Placeholder-as-label is a known anti-pattern; hurts re-checking and low-vision/zoomed users, and reads as a polish deficit to a design-literate audience.
- **Fix:** Visible (or persistent floating) labels, keeping sr-only only as a supplement.

### 18. Every pause/retry/give-up/toggle triggers a full page reload (ux): `static/js/app.js`
- **Evidence:** `location.reload()` at L89, 138, 167, 178.
- **Problem:** Success = whole page flash; scroll position in long history/feeds lists is lost after every action.
- **Why it matters:** Real friction when triaging many failed items: dumped back to the top after each one.
- **Fix:** Update the affected row in place (swap badge, disable button); if reload stays, preserve scroll via `sessionStorage`.

### 19. Landing page heading order is broken (a11y): `templates/landing.html`
- **Evidence:** `<h1>` (L6) → six `<h3>` features (L26–46) → `<h2>` "How it works" (L52).
- **Problem:** h1→h3 skips h2; the h2 then appears as a child of h3-level content.
- **Why it matters:** Screen-reader heading navigation produces a nonsensical outline.
- **Fix:** Add a visually-hidden h2 "Features" above the grid (features stay h3), or promote feature titles to h2.

### 20. Reset-password errors are silent to screen readers (a11y): `templates/reset_password.html`
- **Evidence:** L8 `<p class="auth-error">{{ error }}</p>` vs login.html L14 `<div class="alert alert-error" role="alert">`.
- **Problem:** Invalid/expired token errors aren't announced.
- **Why it matters:** Users may fill the whole form on a dead token before discovering failure.
- **Fix:** Use the same `role="alert"` alert div as the other auth pages.

---

## P3: Polish

### 21. "Toggle" names a mechanism, not an action (ux): `templates/echoes.html` L215
- **Problem:** Button says "Toggle"; users must cross-reference the Status column to predict the result. **Fix:** `{{ 'Disable' if echo.enabled else 'Enable' }}`.

### 22. Delete-account confirm says nothing about blast radius (ux): `templates/accounts.html` L120, 150, 181, 212
- **Problem:** `confirm('Delete this account?')` doesn't say whether dependent echoes break, get disabled, or get deleted; the last-chance warning misses its purpose. **Fix:** "Echoes using it will stop working.": ideally show the affected-echo count.

### 23. Asymmetric role-toggle labels (ux): `templates/admin.html` L73/77
- **Problem:** "Make admin" vs "Demote". **Fix:** Symmetric pair ("Promote"/"Demote" or "Make admin"/"Remove admin").

### 24. Table headers lack `scope="col"` (a11y): `templates/admin.html`
- **Problem:** Both tables' `<th>` cells declare no scope; header association is less reliable in some AT. **Fix:** Add `scope="col"`.

### 25. Stored-password state lives only in a placeholder (ux): `templates/admin.html` L202
- **Problem:** `placeholder="Stored: leave blank to keep"` (empty attribute when nothing stored); placeholders vanish on typing and aren't reliably announced, so admins may unknowingly clear the credential. **Fix:** Conditional persistent hint `<p class="hint">` and omit the placeholder when empty.

### 26. Forgot-password form stays interactive after the email is sent (ux): `templates/forgot_password.html` L7–16
- **Problem:** Confirmation paragraph renders above the still-live form; looks like a failed submission, inviting repeat sends. **Fix:** When `sent`, render `role="status"` success and hide/disable the form, keeping only "Back to login".

### 27. 404 recovery link mislabeled for anonymous users (ux): `templates/404.html`
- **Problem:** "Back to dashboard" bounces logged-out visitors through a redirect to /login; error.html same. **Fix:** "Back to home" plus Log in / Sign up links when unauthenticated.

### 28. No skip link; nav landmark unnamed (a11y): `templates/base.html` L30
- **Problem:** Keyboard users tab through the entire nav on every page. **Fix:** Visually-hidden "Skip to main content" link as first focusable element targeting `<main id="main">`; `aria-label="Primary"` on the nav.

### 29. Hint text inside `<label>` pollutes accessible names (a11y): `templates/settings.html` L66–69, 76–79, 100–103
- **Problem:** e.g. the checkbox's name becomes "Enable AI alt text Generate alt text before uploading images to Mastodon."; "0 = retry forever" is read as the field name. **Fix:** Move hints outside the label and reference via `aria-describedby`.

### 30. Alt-text hint names "Mastodon" in a Bluesky-first product (ux): `templates/settings.html` L102
- **Problem:** "Generate alt text before uploading images to Mastodon" makes Bluesky users unsure the feature applies to them. **Fix:** "…before posting to your connected accounts."

### 31. Empty account cell reads as a rendering bug (ux): `templates/dashboard.html` L111
- **Problem:** `{{ post.account_name or '' }}` renders a literally blank cell while neighboring columns use "-" and "Unknown". **Fix:** `{{ post.account_name or '-' }}`.

### 32. Theme toggle's accessible name is a glyph, `aria-pressed` conflates state (a11y): `static/js/app.js` L456–457
- **Problem:** `btn.textContent = '☀' / '☾'` with `aria-pressed` meaning "light is active": announced as "crescent moon, toggle button, pressed". **Fix:** State-dependent `aria-label` ("Switch to light/dark theme") + `title`; drop `aria-pressed`.

---

## Coverage notes

- Batches: A auth/public templates, B core app (dashboard/feeds/history/settings), C echoes/accounts, D admin/howto, E shared CSS+JS. ~117K chars of source reviewed.
- howto.html produced no findings (documentation page; clean).
- Verification: each finding checked against source at commit 1530dda before inclusion (grep line checks for labels, confirms, outline rules, hidden divs, alert counts, aria attributes, reload sites). Zero false positives dropped.
