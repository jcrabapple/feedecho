// FeedEcho — client-side interactions

// Store original row HTML for cancelEdit; avoids XSS from inline HTML serialization
const editState = new Map();

// Inline status feedback (replaces blocking alert() dialogs): renders the
// outcome next to the triggering button in a polite live region so screen
// readers announce it, and the text stays re-readable and styleable.
// kind: 'success' | 'error' | 'info'
function showStatus(btn, text, kind) {
    if (!btn) return;
    const host = btn.parentElement;
    if (!host) return;
    let box = host.querySelector('.action-status');
    if (!box) {
        box = document.createElement('div');
        box.className = 'action-status';
        box.setAttribute('role', 'status');
        box.setAttribute('aria-live', 'polite');
        host.appendChild(box);
    }
    box.className = 'action-status action-status-' + (kind || 'info');
    box.textContent = text; // textContent, never innerHTML: server strings are untrusted
}

// Busy-state wrapper: disables the clicked button and swaps its label for the
// duration of the async action so double-clicks cannot fire duplicate POSTs.
// fn receives no arguments; wrap it (e.g. `onclick="withBusy(this, () => testAccount(3))"`).
async function withBusy(btn, fn) {
    if (btn.disabled) return;
    const originalText = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Working...';
    try {
        await fn();
    } finally {
        btn.disabled = false;
        btn.textContent = originalText;
    }
}

async function testAccount(accountId, btn) {
    try {
        const resp = await fetch(`/api/accounts/${accountId}/test`, { method: 'POST' });
        const data = await resp.json();
        if (!resp.ok) {
            showStatus(btn, 'Test failed: ' + (data.detail || resp.statusText), 'error');
            return;
        }
        showStatus(btn, data.message || (data.success ? 'OK' : 'Failed'), data.success ? 'success' : 'error');
    } catch (e) {
        showStatus(btn, 'Request failed: ' + e.message, 'error');
    }
}

async function testBlueskyAccount(accountId, btn) {
    try {
        const resp = await fetch(`/api/bluesky-accounts/${accountId}/test`, { method: 'POST' });
        const data = await resp.json();
        if (!resp.ok) {
            showStatus(btn, 'Test failed: ' + (data.detail || resp.statusText), 'error');
            return;
        }
        showStatus(btn, data.message || (data.success ? 'OK' : 'Failed'), data.success ? 'success' : 'error');
    } catch (e) {
        showStatus(btn, 'Request failed: ' + e.message, 'error');
    }
}

async function testMicroblogAccount(accountId, btn) {
    try {
        const resp = await fetch(`/api/microblog-accounts/${accountId}/test`, { method: 'POST' });
        const data = await resp.json();
        if (!resp.ok) {
            showStatus(btn, 'Test failed: ' + (data.detail || resp.statusText), 'error');
            return;
        }
        showStatus(btn, data.message || (data.success ? 'OK' : 'Failed'), data.success ? 'success' : 'error');
    } catch (e) {
        showStatus(btn, 'Request failed: ' + e.message, 'error');
    }
}

async function testFeed(feedId, btn) {
    try {
        const resp = await fetch(`/api/feeds/${feedId}/test`, { method: 'POST' });
        const data = await resp.json();
        if (data.success) {
            const p = data.preview;
            showStatus(btn, `Feed: ${p.title} · Type: ${p.type} · Items: ${p.item_count} · Latest: ${p.items[0]?.title || 'none'}`, 'success');
        } else {
            showStatus(btn, 'Feed test failed: ' + data.error, 'error');
        }
    } catch (e) {
        showStatus(btn, 'Request failed: ' + e.message, 'error');
    }
}

async function initFeed(feedId, btn) {
    if (!confirm('Initialize feed? This sets the last seen item so only new posts going forward will be cross-posted.')) return;
    try {
        const resp = await fetch(`/api/feeds/${feedId}/init`, { method: 'POST' });
        const data = await resp.json();
        showStatus(btn, data.message || (data.success ? 'OK' : 'Failed'), data.success ? 'success' : 'error');
    } catch (e) {
        showStatus(btn, 'Request failed: ' + e.message, 'error');
    }
}

async function fetchNow(feedId, btn) {
    try {
        const resp = await fetch(`/api/feeds/${feedId}/fetch`, { method: 'POST' });
        const data = await resp.json();
        showStatus(btn, data.message || (data.success ? 'OK' : 'Failed'), data.success ? 'success' : 'error');
    } catch (e) {
        showStatus(btn, 'Request failed: ' + e.message, 'error');
    }
}

// Restore scroll position across the full reloads that table-row actions
// trigger (pause/retry/give-up/toggle). Dumping the user back to the top of
// a long history list after every action is exactly the friction this avoids.
function reloadPreservingScroll() {
    try {
        sessionStorage.setItem('feedecho-scroll-y', String(window.scrollY));
    } catch (e) {}
    location.reload();
}
(function () {
    let y = null;
    try { y = Number(sessionStorage.getItem('feedecho-scroll-y')); } catch (e) {}
    if (y) {
        try { sessionStorage.removeItem('feedecho-scroll-y'); } catch (e) {}
        window.addEventListener('load', () => window.scrollTo(0, y), { once: true });
    }
})();

async function pauseFeed(feedId, btn) {
    try {
        const resp = await fetch(`/api/feeds/${feedId}/pause`, { method: 'POST' });
        const data = await resp.json();
        if (data.success) {
            reloadPreservingScroll();
        } else {
            showStatus(btn, data.detail || 'Failed to toggle pause', 'error');
        }
    } catch (e) {
        showStatus(btn, 'Request failed: ' + e.message, 'error');
    }
}

function editFeed(feedId) {
    const row = document.getElementById(`feed-row-${feedId}`);
    if (!row) return;

    // Namespaced key: editState is shared with the echo editor, whose keys
    // are bare echo ids. 'feed-N' can never collide with a bare number.
    const key = 'feed-' + feedId;
    editState.set(key, row.innerHTML);

    const name = row.dataset.name;
    const url = row.dataset.url;
    const pollInterval = row.dataset.pollInterval || '15';

    row.innerHTML = `<td colspan="7">
        <form method="post" action="/api/feeds/${feedId}/edit" class="echo-edit-form">
            <div class="form-row">
                <label>Name
                    <input type="text" name="name" value="${escapeHTML(name)}" required>
                </label>
                <label>Feed URL
                    <input type="url" name="url" value="${escapeHTML(url)}" required>
                </label>
                <label>Poll interval (min)
                    <input type="number" name="poll_interval" min="1" max="1440" value="${pollInterval}">
                </label>
            </div>
            <p class="hint">Changing the URL resets the last-seen cursor, so the next check re-initializes against the new feed without back-posting old items.</p>
            <div class="form-row edit-actions">
                <button type="submit" class="btn-sm">Save</button>
                <button type="button" class="btn-sm" onclick="cancelFeedEdit(${feedId})">Cancel</button>
            </div>
        </form>
    </td>`;

    // innerHTML replacement above destroyed the focused Edit button; drop
    // focus into the form it opened instead of leaving it on <body>.
    row.querySelector('input, select, textarea')?.focus();
}

async function retryPost(postedId, btn) {
    try {
        const resp = await fetch(`/api/history/${postedId}/retry`, { method: 'POST' });
        const data = await resp.json();
        showStatus(btn, data.message || data.detail || 'Done', data.success ? 'success' : 'error');
        if (data.success) reloadPreservingScroll();
    } catch (e) {
        showStatus(btn, 'Request failed: ' + e.message, 'error');
    }
}

async function testAltText(btn) {
    const originalText = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Testing...';
    try {
        const resp = await fetch('/api/settings/alt-text/test', { method: 'POST' });
        const data = await resp.json();
        showStatus(btn, data.message || (data.success ? 'OK' : 'Failed'), data.success ? 'success' : 'error');
    } catch (e) {
        showStatus(btn, 'Request failed: ' + e.message, 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = originalText;
    }
}

async function giveUpPost(postedId, btn) {
    if (!confirm('Give up on this item? The feed will move past it and it will not be delivered.')) return;
    try {
        const resp = await fetch(`/api/history/${postedId}/give-up`, { method: 'POST' });
        const data = await resp.json();
        showStatus(btn, data.message || data.detail || 'Done', data.success ? 'success' : 'error');
        if (data.success) reloadPreservingScroll();
    } catch (e) {
        showStatus(btn, 'Request failed: ' + e.message, 'error');
    }
}

async function disableEcho(echoId, btn) {
    return toggleEcho(echoId, btn);
}

async function enableEcho(echoId, btn) {
    return toggleEcho(echoId, btn);
}

async function toggleEcho(echoId, btn) {
    try {
        const resp = await fetch(`/api/echoes/${echoId}/toggle`, { method: 'POST' });
        const data = await resp.json();
        if (data.success) {
            reloadPreservingScroll();
        } else {
            showStatus(btn, data.detail || data.error || 'Failed to toggle echo', 'error');
        }
    } catch (e) {
        showStatus(btn, 'Request failed: ' + e.message, 'error');
    }
}

function editEcho(echoId) {
    const row = document.getElementById(`echo-row-${echoId}`);
    if (!row) return;

    // Store original HTML to restore on cancel (in memory, not in DOM attribute)
    const originalHTML = row.innerHTML;
    editState.set(echoId, originalHTML);

    const feedId = row.dataset.feedId;
    const destType = row.dataset.destinationType;
    const destId = row.dataset.destinationId;
    const template = row.dataset.template;
    const visibility = row.dataset.visibility;
    const filterKeywords = row.dataset.filterKeywords || '';
    const filterMode = row.dataset.filterMode || 'exclude';
    const contentWarning = row.dataset.contentWarning || '';
    const attachImage = row.dataset.attachImage === '1';
    const deliveryMode = row.dataset.deliveryMode || 'instant';
    const dripLimit = row.dataset.dripLimit || '0';
    const enabled = row.dataset.enabled === '1';

    // .trim(): an empty {% for %} still leaves newlines and indentation, which
    // are truthy, so every destination type was offered even with zero accounts
    // of that type. The create form guards with {% if accounts %} instead.
    const feedOpts = document.getElementById('feed-options').innerHTML.trim();
    const mastoOpts = document.getElementById('mastodon-options').innerHTML.trim();
    const emailOpts = document.getElementById('email-options').innerHTML.trim();
    const blueskyOpts = document.getElementById('bluesky-options').innerHTML.trim();
    const microblogOpts = document.getElementById('microblog-options').innerHTML.trim();

    const mastoStyle = destType === 'mastodon' ? '' : 'display:none';
    const emailStyle = destType === 'email' ? '' : 'display:none';
    const blueskyStyle = destType === 'bluesky' ? '' : 'display:none';
    const microblogStyle = destType === 'microblog' ? '' : 'display:none';

    row.innerHTML = `<td colspan="5">
        <form method="post" action="/api/echoes/${echoId}/edit" class="echo-edit-form">
            <div class="form-row">
                <label>Feed
                    <select name="feed_id" required>${feedOpts}</select>
                </label>
                <label>Destination
                    <select name="destination_type" id="edit-dest-type-${echoId}" onchange="toggleEditDest(${echoId})">
                        ${mastoOpts ? '<option value="mastodon"' + (destType === 'mastodon' ? ' selected' : '') + '>Mastodon Account</option>' : ''}
                        ${emailOpts ? '<option value="email"' + (destType === 'email' ? ' selected' : '') + '>Email Address</option>' : ''}
                        ${blueskyOpts ? '<option value="bluesky"' + (destType === 'bluesky' ? ' selected' : '') + '>Bluesky Account</option>' : ''}
                        ${microblogOpts ? '<option value="microblog"' + (destType === 'microblog' ? ' selected' : '') + '>Micro.blog Blog</option>' : ''}
                    </select>
                </label>
            </div>
            <div class="form-row" id="edit-mastodon-fields-${echoId}" style="${mastoStyle}">
                <label>Mastodon Account
                    <select name="account_id">${mastoOpts}</select>
                </label>
                <label>Visibility
                    <select name="visibility">
                        <option value="public"${visibility === 'public' ? ' selected' : ''}>Public</option>
                        <option value="unlisted"${visibility === 'unlisted' ? ' selected' : ''}>Unlisted</option>
                        <option value="private"${visibility === 'private' ? ' selected' : ''}>Private (followers only)</option>
                        <option value="direct"${visibility === 'direct' ? ' selected' : ''}>Direct</option>
                    </select>
                </label>
            </div>
            <div class="form-row" id="edit-email-fields-${echoId}" style="${emailStyle}">
                <label>Email Address
                    <select name="email_account_id">${emailOpts}</select>
                </label>
            </div>
            <div class="form-row" id="edit-bluesky-fields-${echoId}" style="${blueskyStyle}">
                <label>Bluesky Account
                    <select name="bluesky_account_id">${blueskyOpts}</select>
                </label>
            </div>
            <div class="form-row" id="edit-microblog-fields-${echoId}" style="${microblogStyle}">
                <label>Micro.blog Blog
                    <select name="microblog_account_id">${microblogOpts}</select>
                </label>
            </div>
            <div class="form-row">
                <label>Enabled
                    <input type="checkbox" name="enabled" value="true"${enabled ? ' checked' : ''}>
                </label>
            </div>
            <div class="form-row">
                <label>Template
                    <textarea name="template" rows="3">${escapeHTML(template)}</textarea>
                </label>
                <!-- Button outside the label: interactive content inside <label>
                     is invalid HTML and activating it also targets the textarea. -->
                <button type="button" class="btn-sm template-preview-btn" onclick="previewTemplate(this)">Preview</button>
            </div>
            <div class="form-row">
                <label>Keyword filter
                    <input type="text" name="filter_keywords" value="${escapeHTML(filterKeywords)}" placeholder="e.g. spoiler, giveaway, nsfw">
                </label>
                <label>Filter mode
                    <select name="filter_mode">
                        <option value="exclude"${filterMode === 'exclude' ? ' selected' : ''}>Exclude matching items</option>
                        <option value="include"${filterMode === 'include' ? ' selected' : ''}>Only include matching items</option>
                    </select>
                </label>
            </div>
            <div class="form-row">
                <label>Content warning
                    <input type="text" name="content_warning" value="${escapeHTML(contentWarning)}" placeholder="e.g. Spoilers, US Politics" maxlength="500">
                </label>
                <label>Attach image
                    <input type="checkbox" name="attach_image" value="true"${attachImage ? ' checked' : ''}>
                </label>
            </div>
            <div class="form-row" id="edit-digest-fields-${echoId}" style="${emailStyle}">
                <label>Delivery mode
                    <select name="delivery_mode" onchange="toggleEditDest(${echoId})">
                        <option value="instant"${deliveryMode === 'instant' ? ' selected' : ''}>Instant (one email per item)</option>
                        <option value="digest"${deliveryMode === 'digest' ? ' selected' : ''}>Digest (batch items into one email, sent hourly)</option>
                    </select>
                </label>
            </div>
            <div class="form-row" id="edit-drip-fields-${echoId}">
                <label>Max posts per hour
                    <input type="number" name="drip_limit" min="0" max="1000" value="${dripLimit}">
                </label>
            </div>
            <div class="form-row edit-actions">
                <button type="submit" class="btn-sm">Save</button>
                <button type="button" class="btn-sm" onclick="cancelEdit(${echoId})">Cancel</button>
            </div>
        </form>
    </td>`;

    // Set selected values in dropdowns
    const feedSelect = row.querySelector('select[name="feed_id"]');
    if (feedSelect) feedSelect.value = feedId;
    const mastoSelect = row.querySelector('select[name="account_id"]');
    if (mastoSelect) mastoSelect.value = destId;
    const emailSelect = row.querySelector('select[name="email_account_id"]');
    if (emailSelect) emailSelect.value = destId;
    const blueskySelect = row.querySelector('select[name="bluesky_account_id"]');
    if (blueskySelect) blueskySelect.value = destId;
    const microblogSelect = row.querySelector('select[name="microblog_account_id"]');
    if (microblogSelect) microblogSelect.value = destId;

    // Sync conditional rows to the echo's current delivery mode: without this
    // a digest echo opens showing both "batch into one email" and "Max posts
    // per hour", and the drip field only hides after the next onchange.
    toggleEditDest(echoId);

    // innerHTML replacement above destroyed the focused Edit button; drop
    // focus into the form it opened instead of leaving it on <body>.
    row.querySelector('input, select, textarea')?.focus();
}

function toggleEditDest(echoId) {
    const destType = document.getElementById(`edit-dest-type-${echoId}`).value;
    document.getElementById(`edit-mastodon-fields-${echoId}`).style.display = destType === 'mastodon' ? '' : 'none';
    document.getElementById(`edit-email-fields-${echoId}`).style.display = destType === 'email' ? '' : 'none';
    document.getElementById(`edit-bluesky-fields-${echoId}`).style.display = destType === 'bluesky' ? '' : 'none';
    document.getElementById(`edit-microblog-fields-${echoId}`).style.display = destType === 'microblog' ? '' : 'none';
    const digestFields = document.getElementById(`edit-digest-fields-${echoId}`);
    if (digestFields) digestFields.style.display = destType === 'email' ? '' : 'none';
    const dripFields = document.getElementById(`edit-drip-fields-${echoId}`);
    if (dripFields) {
        const deliveryMode = document.querySelector(`#edit-digest-fields-${echoId} select[name="delivery_mode"]`);
        dripFields.style.display = (deliveryMode && deliveryMode.value === 'digest') ? 'none' : '';
    }
}

function cancelEdit(echoId) {
    const row = document.getElementById(`echo-row-${echoId}`);
    if (row && editState.has(echoId)) {
        row.innerHTML = editState.get(echoId);
        editState.delete(echoId);
        // The editor form vanished with the focused control inside it; return
        // focus to the row's Edit button so keyboard users are not dumped to
        // the top of the document.
        row.querySelector('button')?.focus();
    }
}

function cancelFeedEdit(feedId) {
    const key = 'feed-' + feedId;
    const row = document.getElementById(`feed-row-${feedId}`);
    if (row && editState.has(key)) {
        row.innerHTML = editState.get(key);
        editState.delete(key);
        // Same focus-return contract as cancelEdit.
        row.querySelector('button')?.focus();
    }
}

async function previewTemplate(btn) {
    const form = btn.closest('form');
    if (!form) return;

    const templateField = form.querySelector('textarea[name="template"]');
    const feedSelect = form.querySelector('select[name="feed_id"]');
    if (!templateField) return;
    if (!feedSelect) {
        showStatus(btn, 'Preview needs a feed selector on this form.', 'error');
        return;
    }
    if (!feedSelect.value) {
        showStatus(btn, 'Select a feed first, then preview.', 'error');
        return;
    }

    let box = btn.parentElement.querySelector('.template-preview');
    if (!box) {
        box = document.createElement('div');
        box.className = 'template-preview';
        // Injected asynchronously: without a live region a screen reader never
        // learns the preview rendered.
        box.setAttribute('role', 'status');
        box.setAttribute('aria-live', 'polite');
        btn.parentElement.appendChild(box);
    }
    box.innerHTML = '<p class="hint">Rendering against latest feed items...</p>';

    const body = new FormData();
    body.append('template', templateField.value);
    body.append('feed_id', feedSelect.value);

    try {
        const resp = await fetch('/api/preview', { method: 'POST', body });
        const data = await resp.json();
        if (!resp.ok || !data.success) {
            box.innerHTML = `<p class="preview-error">${escapeHTML(data.error || resp.statusText)}</p>`;
            return;
        }
        if (!data.items || data.items.length === 0) {
            box.innerHTML = '<p class="hint">This feed has no items to preview.</p>';
            return;
        }
        box.innerHTML = data.items.map((it) =>
            `<div class="preview-item"><p class="preview-title">${escapeHTML(it.title)}</p><pre>${escapeHTML(it.rendered)}</pre></div>`
        ).join('');
    } catch (e) {
        box.innerHTML = `<p class="preview-error">${escapeHTML('Request failed: ' + e.message)}</p>`;
    }
}

function escapeHTML(str) {
    const div = document.createElement('div');
    div.appendChild(document.createTextNode(str));
    // innerHTML serialization escapes & < > but NOT double quotes; these
    // helpers interpolate into double-quoted attributes, so escape them too.
    return div.innerHTML.replace(/"/g, '&quot;');
}

// Convert server UTC timestamps to the viewer's local timezone (issue #4)
// and locale (issue #6). Passing `navigator.languages` (the user's ordered
// browser language preferences) explicitly makes the output follow the
// locale the user configured, not just the browser UI language.
// <time class="local-time" datetime="2026-08-24T06:46:00Z">fallback</time>
function formatLocalTimes() {
    const locales = (navigator.languages && navigator.languages.length) ? navigator.languages : undefined;
    document.querySelectorAll('time.local-time').forEach((el) => {
        const raw = el.getAttribute('datetime');
        if (!raw) return;
        const dateOnly = /^\d{4}-\d{2}-\d{2}$/.test(raw);
        let d;
        if (dateOnly) {
            // Date-only strings parse as UTC midnight; building the Date
            // from parts keeps it on the local calendar day (no off-by-one).
            const parts = raw.split('-');
            d = new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]));
        } else {
            d = new Date(raw);
        }
        if (isNaN(d.getTime())) return; // keep the UTC fallback text
        el.textContent = dateOnly ? d.toLocaleDateString(locales) : d.toLocaleString(locales);
    });
}

/* Theme toggle — 3-state cycle: auto (follow device) → light → dark.
   'auto' is the escape hatch from the old two-state toggle: once any click
   wrote a stored value, the device preference was overridden forever with
   no way back (reported: "light/dark mode does not follow the device"). */
(function () {
    const btn = document.getElementById('theme-toggle');
    if (!btn) return;

    const LIVE = document.getElementById('theme-mode-live');
    const mqLight = window.matchMedia('(prefers-color-scheme: light)');
    const ORDER = ['auto', 'light', 'dark'];

    function readStorage() {
        try {
            const stored = localStorage.getItem('feedecho-theme');
            return (stored === 'light' || stored === 'dark') ? stored : 'auto';
        } catch (e) {
            return null; // storage unavailable: caller falls back to the closure copy
        }
    }

    // Closure copy of the current mode. localStorage can throw on EVERY
    // access (Safari private mode with cookies blocked): readStorage() then
    // returns null and the click cycle would dead-end on 'light'. The click
    // handler keeps this variable in sync and storedMode() prefers it.
    let mode = readStorage() || 'auto';

    function storedMode() {
        return mode && ORDER.includes(mode) ? mode : 'auto';
    }

    function applyTheme(mode) {
        // 'auto' resolves through the live media query each time so the
        // rendered theme is always current, even mid-session OS switches.
        const theme = mode === 'auto'
            ? (mqLight.matches ? 'light' : 'dark')
            : mode;
        document.documentElement.setAttribute('data-theme', theme);
        document.documentElement.setAttribute('data-theme-mode', mode);
        return theme;
    }

    function render(mode, announce) {
        // Show what the NEXT click does; 'auto' is announced as such so the
        // user can tell an OS-following state from a pinned one.
        const next = ORDER[(ORDER.indexOf(mode) + 1) % ORDER.length];
        const label = next === 'auto' ? 'Switch to auto theme (follow device)'
            : next === 'light' ? 'Switch to light theme' : 'Switch to dark theme';
        btn.setAttribute('aria-label', label);
        btn.setAttribute('title', label);
        btn.removeAttribute('aria-pressed');
        // Glyph per mode: half-moon = auto (follows device), sun = pinned
        // light, moon = pinned dark.
        btn.textContent = mode === 'auto' ? '◐' : (mode === 'light' ? '☀' : '☾');
        // Only user/system-initiated changes speak; the startup render must
        // stay silent or every navigation announces the theme (aria-live
        // regions announce on content CHANGE, and set on load counts).
        if (announce && LIVE) {
            LIVE.textContent = mode === 'auto'
                ? 'Theme follows device settings'
                : 'Theme set to ' + mode + ' manually';
        }
    }

    applyTheme(storedMode());
    render(mode, false);

    btn.addEventListener('click', function () {
        const next = ORDER[(ORDER.indexOf(storedMode()) + 1) % ORDER.length];
        try {
            if (next === 'auto') localStorage.removeItem('feedecho-theme');
            else localStorage.setItem('feedecho-theme', next);
        } catch (e) {}
        mode = next;
        applyTheme(next);
        render(next, true);
    });

    // While in auto, follow device changes live.
    // Legacy WebKit (< iOS 14) only has the addListener form; the guard is
    // cheap and Brian's report came from an iOS Safari.
    const onSystemChange = function () {
        if (storedMode() !== 'auto') return;
        applyTheme('auto');
        render('auto', true);
    };
    if (mqLight.addEventListener) mqLight.addEventListener('change', onSystemChange);
    else if (mqLight.addListener) mqLight.addListener(onSystemChange);
})();

// app.js is a classic script at the end of <body>, so the DOM is parsed.
formatLocalTimes();

// Mobile table semantics: the <=640px card layout sets thead and all table
// elements to display:block, which strips table semantics, and leaves each
// cell labelled only by ::before content from data-label that many screen
// readers never announce. Hydrate the header text into every cell as real
// (visually hidden) text; CSS keeps the ::before for sighted narrow layouts.
// role="cell" keeps the value from being double-announced with the sr-only
// copy present.
function hydrateTableHeaders() {
    document.querySelectorAll('table.data-table').forEach((table) => {
        const headers = Array.from(table.querySelectorAll('thead th')).map((th) => th.textContent.trim());
        if (!headers.length) return;
        table.querySelectorAll('tbody tr').forEach((tr) => {
            tr.querySelectorAll('td[data-label]').forEach((td) => {
                if (td.querySelector('.sr-only.table-label')) return;
                const label = document.createElement('span');
                label.className = 'sr-only table-label';
                label.textContent = td.dataset.label;
                td.setAttribute('role', 'cell');
                td.insertBefore(label, td.firstChild);
            });
        });
    });
}
hydrateTableHeaders();
