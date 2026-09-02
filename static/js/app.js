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

async function testMatrixAccount(accountId, btn) {
    try {
        const resp = await fetch(`/api/matrix-accounts/${accountId}/test`, { method: 'POST' });
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

async function testDiscordAccount(accountId, btn) {
    try {
        const resp = await fetch(`/api/discord-accounts/${accountId}/test`, { method: 'POST' });
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

async function testWebhookAccount(accountId, btn) {
    try {
        const resp = await fetch(`/api/webhook-accounts/${accountId}/test`, { method: 'POST' });
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
    const muteKeywords = row.dataset.muteKeywords || '';

    row.innerHTML = `<td colspan="6">
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
                <label>Mute keywords (comma-separated)
                    <input type="text" name="mute_keywords" value="${escapeHTML(muteKeywords)}" placeholder="e.g. sponsored, press release">
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
    const matrixOpts = document.getElementById('matrix-options').innerHTML.trim();

    const mastoStyle = destType === 'mastodon' ? '' : 'display:none';
    const emailStyle = destType === 'email' ? '' : 'display:none';
    const blueskyStyle = destType === 'bluesky' ? '' : 'display:none';
    const microblogStyle = destType === 'microblog' ? '' : 'display:none';
    const matrixStyle = destType === 'matrix' ? '' : 'display:none';

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
                        ${matrixOpts ? '<option value="matrix"' + (destType === 'matrix' ? ' selected' : '') + '>Matrix Room</option>' : ''}
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
            <div class="form-row" id="edit-matrix-fields-${echoId}" style="${matrixStyle}">
                <label>Matrix Room
                    <select name="matrix_account_id">${matrixOpts}</select>
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
    const matrixSelect = row.querySelector('select[name="matrix_account_id"]');
    if (matrixSelect) matrixSelect.value = destId;

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
    document.getElementById(`edit-matrix-fields-${echoId}`).style.display = destType === 'matrix' ? '' : 'none';
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

// ── Reader (issue #11) ─────────────────────────────────────────────────────
function readerNextEntry(entry) {
    let n = entry.nextElementSibling;
    while (n && !n.classList.contains('reader-entry')) n = n.nextElementSibling;
    return n;
}

function readerList(entry) {
    return entry.closest('.reader-item-list');
}

function readerApplyStar(btn, starred) {
    btn.classList.toggle('is-starred', starred);
    const svg = btn.querySelector('.action-btn__star svg');
    if (svg) svg.setAttribute('fill', starred ? 'currentColor' : 'none');
    btn.setAttribute('aria-pressed', starred ? 'true' : 'false');
    btn.setAttribute('aria-label', starred ? 'Unstar' : 'Star');
}

function readerAdjustUnread(feedId, delta) {
    if (!feedId || !delta) return;
    const feed = document.querySelector(`li.reader-feed[data-feed-id="${feedId}"]`);
    if (!feed) return;
    const badge = feed.querySelector('.reader-feed-unread');
    if (!badge) {
        if (delta > 0) {
            const meta = feed.querySelector('.reader-feed-meta');
            const span = document.createElement('span');
            span.className = 'reader-feed-unread';
            span.textContent = '1';
            (meta || feed).appendChild(span);
        }
        return;
    }
    const n = parseInt(badge.textContent, 10) + delta;
    if (n <= 0) badge.remove();
    else badge.textContent = String(n);
}

// Sticky-header offset: keep the next card just below the navbar.
const READER_HEADER_OFFSET = 72;

function readerRemoveAndAdvance(entry) {
    const list = readerList(entry);
    const next = readerNextEntry(entry);
    const wasCurrent = entry.classList.contains('reader-current');
    entry.remove();
    if (next) {
        // Keep keyboard focus flowing: if the removed card was the "current"
        // one, the next card inherits the highlight.
        if (wasCurrent) next.classList.add('reader-current');
        // The next card slides up into the removed one's place. Only nudge the
        // viewport if its top ended up hidden under the sticky header.
        const top = next.getBoundingClientRect().top;
        if (top < READER_HEADER_OFFSET) {
            scrollBy({ top: top - READER_HEADER_OFFSET, behavior: 'smooth' });
        }
        next.querySelector('summary')?.focus({ preventScroll: true });
    } else if (list && !list.querySelector('.reader-entry')) {
        reloadPreservingScroll(); // list is empty -> reload to surface the empty state
    }
}

async function readerToggleRead(itemId, btn) {
    if (btn.disabled) return;
    const entry = btn.closest('.reader-entry');
    btn.disabled = true;
    try {
        const resp = await fetch(`/api/reader/${itemId}/read`, { method: 'POST' });
        const data = await resp.json();
        if (!resp.ok) { showStatus(btn, data.detail || 'Failed', 'error'); return; }
        readerAdjustUnread(entry.dataset.feedId, data.is_read ? -1 : 1);
        const list = readerList(entry);
        if (list && list.dataset.view === 'unread' && data.is_read) {
            readerRemoveAndAdvance(entry);
        } else {
            const details = entry.querySelector('.reader-item');
            details.classList.toggle('unread', !data.is_read);
            btn.textContent = data.is_read ? 'Mark unread' : 'Mark read';
        }
    } catch (e) { showStatus(btn, 'Request failed: ' + e.message, 'error'); }
    finally { btn.disabled = false; }
}

async function readerToggleStar(itemId, btn) {
    if (btn.disabled) return;
    const entry = btn.closest('.reader-entry');
    btn.disabled = true;
    try {
        const resp = await fetch(`/api/reader/${itemId}/star`, { method: 'POST' });
        const data = await resp.json();
        if (!resp.ok) { showStatus(btn, data.detail || 'Failed', 'error'); return; }
        const list = readerList(entry);
        if (list && list.dataset.view === 'starred' && !data.starred) {
            readerRemoveAndAdvance(entry);
        } else {
            readerApplyStar(btn, data.starred);
        }
    } catch (e) { showStatus(btn, 'Request failed: ' + e.message, 'error'); }
    finally { btn.disabled = false; }
}

async function readerToggleFeed(feedId, btn) {
    if (btn.disabled) return;
    btn.disabled = true;
    try {
        const resp = await fetch(`/api/feeds/${feedId}/reader-toggle`, { method: 'POST' });
        const data = await resp.json();
        if (!resp.ok) { showStatus(btn, data.detail || 'Failed', 'error'); return; }
        reloadPreservingScroll();
    } catch (e) { showStatus(btn, 'Request failed: ' + e.message, 'error'); }
    finally { btn.disabled = false; }
}

async function readerMarkAllRead(btn) {
    if (btn.disabled) return;
    btn.disabled = true;
    const params = new URLSearchParams(window.location.search);
    const feedId = params.get('feed');
    const body = feedId ? new URLSearchParams({ feed_id: feedId }) : new URLSearchParams();
    try {
        const resp = await fetch('/api/reader/mark-all-read', { method: 'POST', body });
        const data = await resp.json();
        if (!resp.ok) { showStatus(btn, data.detail || 'Failed', 'error'); return; }
        if (data.count > 0 && Array.isArray(data.ids) && data.ids.length) {
            sessionStorage.setItem('feedecho-reader-undo', JSON.stringify({ ids: data.ids, at: Date.now() }));
        }
        reloadPreservingScroll();
    } catch (e) { showStatus(btn, 'Request failed: ' + e.message, 'error'); }
    finally { btn.disabled = false; }
}

function readerOpenShout(itemId) {
    const dlg = document.getElementById('shout-' + itemId);
    if (!dlg) return;
    // Every open starts from the defaults; Cancel/Esc therefore discards edits.
    const form = dlg.querySelector('form');
    if (form) form.reset();
    dlg.showModal();
}

// Toggle the variables tooltip (tap/click/Enter on the ⓘ trigger). Desktop
// hover still reveals it via CSS; this makes it usable on touch devices.
function readerToggleVars(btn) {
    const open = btn.classList.toggle('is-open');
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
}

// Transient toast (role=status) for overlay actions whose dialog has closed.
function readerToast(text) {
    document.querySelector('.reader-toast')?.remove();
    const toast = document.createElement('div');
    toast.className = 'reader-toast';
    toast.setAttribute('role', 'status');
    toast.textContent = text;
    document.body.appendChild(toast);
    setTimeout(() => toast.remove(), 4000);
}

async function readerShout(itemId, form) {
    const btn = form.querySelector('button[type="submit"]');
    if (btn.disabled) return false;
    const data = new URLSearchParams(new FormData(form));
    btn.disabled = true;
    const dlg = form.closest('dialog');
    try {
        const resp = await fetch(`/api/reader/${itemId}/shout`, { method: 'POST', body: data });
        const result = await resp.json();
        if (!resp.ok) { showStatus(btn, result.detail || 'Shout failed', 'error'); return false; }
        if (result.success) {
            const box = form.querySelector('.action-status');
            if (box) box.remove();
            form.reset();
            if (dlg) dlg.close();
            readerToast('Shouted' + (result.post_url ? ' — view in History' : ''));
        } else {
            showStatus(btn, result.error_message || 'Shout failed', 'error');
        }
    } catch (e) { showStatus(btn, 'Request failed: ' + e.message, 'error'); }
    finally { btn.disabled = false; }
    return false;
}

// ── Reader keyboard shortcuts ────────────────────────────────────────────────
function readerCurrent() {
    return document.querySelector('.reader-entry.reader-current') || document.querySelector('.reader-entry') || null;
}

function readerMove(dir) {
    const entries = Array.from(document.querySelectorAll('.reader-entry'));
    if (!entries.length) return;
    const cur = document.querySelector('.reader-entry.reader-current');
    const idx = cur ? entries.indexOf(cur) : -1;
    const next = entries[idx + dir];
    if (!next) return;
    if (cur) cur.classList.remove('reader-current');
    next.classList.add('reader-current');
    next.scrollIntoView({ block: 'start', behavior: 'smooth' });
    next.querySelector('summary')?.focus({ preventScroll: true });
}

function readerShowShortcuts() {
    const d = document.getElementById('reader-shortcuts');
    if (!d) return;
    if (d.open) d.close(); else d.showModal();
}

document.addEventListener('keydown', (e) => {
    if (!document.querySelector('.reader-entry')) return;
    const tag = (e.target.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select' || e.target.isContentEditable) return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === '?') { e.preventDefault(); readerShowShortcuts(); return; }
    const k = e.key.toLowerCase();
    if (k === 'j') { readerMove(1); }
    else if (k === 'k') { readerMove(-1); }
    else if (k === 'o' || k === 'enter') {
        const cur = readerCurrent(); if (!cur) return;
        const d = cur.querySelector('details.reader-item');
        if (d) { e.preventDefault(); d.open = !d.open; }
    } else if (k === 'm') {
        const cur = readerCurrent(); if (!cur) return;
        const btn = cur.querySelector('button[onclick*="readerToggleRead"]');
        if (btn) { e.preventDefault(); btn.click(); }
    } else if (k === 's') {
        const cur = readerCurrent(); if (!cur) return;
        const btn = cur.querySelector('button[onclick*="readerToggleStar"]');
        if (btn) { e.preventDefault(); btn.click(); }
    }
});

// ── Reader density toggle ────────────────────────────────────────────────────
function readerApplyDensity() {
    const container = document.querySelector('.reader');
    if (!container) return;
    if (localStorage.getItem('feedecho-reader-density') === 'compact') {
        container.dataset.density = 'compact';
    }
    const btn = document.getElementById('reader-density-btn');
    if (btn) {
        btn.setAttribute('aria-pressed', container.dataset.density === 'compact' ? 'true' : 'false');
    }
}

function readerToggleDensity() {
    const container = document.querySelector('.reader');
    if (!container) return;
    const compact = container.dataset.density === 'compact';
    container.dataset.density = compact ? 'comfortable' : 'compact';
    localStorage.setItem('feedecho-reader-density', compact ? 'comfortable' : 'compact');
    readerApplyDensity();
}

// ── Reader mark-all-read undo ────────────────────────────────────────────────
function readerUndoToast(ids) {
    const toast = document.createElement('div');
    toast.className = 'reader-undo-toast';
    toast.setAttribute('role', 'status');
    toast.appendChild(document.createTextNode('Marked as read'));
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = 'Undo';
    btn.onclick = () => readerUndo(ids);
    toast.appendChild(btn);
    document.body.appendChild(toast);
    setTimeout(() => { toast.remove(); sessionStorage.removeItem('feedecho-reader-undo'); }, 5000);
}

async function readerUndo(ids) {
    sessionStorage.removeItem('feedecho-reader-undo');
    if (!Array.isArray(ids) || !ids.length) { reloadPreservingScroll(); return; }
    const body = new URLSearchParams({ ids: ids.join(',') });
    try {
        await fetch('/api/reader/mark-unread', { method: 'POST', body });
    } catch (e) { /* ignore; the reload below reflects whatever state took */ }
    location.reload();
}

// ── Reader auto-read-on-scroll ───────────────────────────────────────────────
let readerAutoReadActive = false;
let readerAutoReadQueue = new Set();
let readerAutoReadTimer = null;
let readerAutoScrollTick = false;

function readerAutoReadFlush() {
    readerAutoReadTimer = null;
    const ids = Array.from(readerAutoReadQueue);
    readerAutoReadQueue.clear();
    if (!ids.length) return;
    // On failure, reload to resync — the DOM was already mutated optimistically.
    fetch('/api/reader/mark-read', { method: 'POST', body: new URLSearchParams({ ids: ids.join(',') }) })
        .catch(() => { reloadPreservingScroll(); });
}

function readerAutoReadMark(entry) {
    const details = entry.querySelector('details.reader-item');
    if (!details || !details.classList.contains('unread')) return;
    entry.dataset.autoDone = '1';
    if (entry.dataset.itemId) readerAutoReadQueue.add(entry.dataset.itemId);
    // Mark read only — never remove the card (layout stays anchored).
    details.classList.remove('unread');
    details.classList.add('reader-auto-read');
    const btn = entry.querySelector('button[onclick*="readerToggleRead"]');
    if (btn) btn.textContent = 'Mark unread';
    readerAdjustUnread(entry.dataset.feedId, -1);
}

function readerAutoReadScroll() {
    if (readerAutoScrollTick) return;
    readerAutoScrollTick = true;
    requestAnimationFrame(() => {
        readerAutoScrollTick = false;
        // Mark any item whose TOP has crossed the header. This also covers tall
        // expanded articles — their top crosses while still on screen, which the
        // IntersectionObserver enter/leave model could not detect.
        document.querySelectorAll('.reader-entry').forEach((entry) => {
            if (entry.dataset.autoDone === '1') return;
            if (entry.getBoundingClientRect().top > READER_HEADER_OFFSET) return;
            readerAutoReadMark(entry);
        });
        if (readerAutoReadQueue.size && !readerAutoReadTimer) {
            readerAutoReadTimer = setTimeout(readerAutoReadFlush, 1500);
        }
    });
}

function readerEnableAutoRead() {
    if (readerAutoReadActive) return;
    const list = document.querySelector('.reader-item-list');
    const view = list && list.dataset.view;
    // Not in Starred view — that's a curated list, not a triage queue.
    if (view === 'starred') return;
    readerAutoReadActive = true;
    window.addEventListener('scroll', readerAutoReadScroll, { passive: true });
    window.addEventListener('resize', readerAutoReadScroll, { passive: true });
    readerAutoReadScroll(); // mark anything already past the header
}

function readerDisableAutoRead() {
    if (!readerAutoReadActive) return;
    readerAutoReadActive = false;
    window.removeEventListener('scroll', readerAutoReadScroll);
    window.removeEventListener('resize', readerAutoReadScroll);
    if (readerAutoReadTimer) { clearTimeout(readerAutoReadTimer); readerAutoReadTimer = null; }
    readerAutoReadQueue.clear();
}

function readerApplyAutoRead() {
    const btn = document.getElementById('reader-autoread-btn');
    const on = localStorage.getItem('feedecho-reader-autoread') === '1';
    if (btn) {
        btn.setAttribute('aria-pressed', on ? 'true' : 'false');
        btn.textContent = on ? 'Auto-read ✓' : 'Auto-read';
    }
    if (on) readerEnableAutoRead();
}

function readerToggleAutoRead() {
    const on = localStorage.getItem('feedecho-reader-autoread') === '1';
    localStorage.setItem('feedecho-reader-autoread', on ? '0' : '1');
    if (on) readerDisableAutoRead(); else readerEnableAutoRead();
    readerApplyAutoRead();
}

function readerToggleFullText() {
    // Full-text view is server-rendered via the ?fulltext=1 param (the page
    // reloads with full article bodies inline). Toggle the param and reload.
    const url = new URL(window.location);
    if (url.searchParams.get('fulltext')) {
        url.searchParams.delete('fulltext');
    } else {
        url.searchParams.set('fulltext', '1');
    }
    window.location = url.toString();
}

// ── Reader lazy body + feeds drawer + poll pill ──────────────────────────────
let readerMaxItemId = 0;
let readerPollTimer = null;

function readerLoadBody(details) {
    const content = details.querySelector('.reader-item-content');
    if (!content || content.dataset.loaded === '1') return;
    const itemId = content.dataset.itemId;
    if (!itemId) return;
    content.dataset.loaded = '1';
    content.classList.add('is-loading');
    fetch(`/api/reader/${itemId}/body`)
        .then((r) => (r.ok ? r.json() : Promise.reject()))
        .then((d) => { content.textContent = d.content || ''; content.classList.remove('is-loading'); })
        .catch(() => { content.dataset.loaded = '0'; content.classList.remove('is-loading'); });
}

document.addEventListener('toggle', (e) => {
    const details = e.target;
    if (details && details.matches && details.matches('details.reader-item') && details.open) {
        readerLoadBody(details);
    }
});

function readerToggleFeeds() {
    const feeds = document.querySelector('.reader-feeds');
    if (feeds) feeds.classList.toggle('open');
}

function readerStartPolling() {
    // Baseline comes from the server (data-max-item-id on the .reader
    // container) = the newest item id across ALL read-enabled feeds, not the
    // subset currently on screen. Otherwise the "N new" count is inflated by
    // already-read items that the current view filters out.
    const container = document.querySelector('.reader');
    readerMaxItemId = container ? (parseInt(container.dataset.maxItemId, 10) || 0) : 0;
    if (readerMaxItemId === 0) return;
    if (readerPollTimer) clearInterval(readerPollTimer);
    readerPollTimer = setInterval(readerPoll, 60000);
}

async function readerPoll() {
    try {
        const resp = await fetch(`/api/reader/new-count?since_id=${readerMaxItemId}`);
        const data = await resp.json();
        if (data.count > 0) readerShowNewPill(data.count);
    } catch (e) { /* network hiccup; next tick retries */ }
}

function readerShowNewPill(n) {
    let pill = document.getElementById('reader-new-pill');
    if (!pill) {
        pill = document.createElement('button');
        pill.id = 'reader-new-pill';
        pill.className = 'reader-new-pill';
        pill.type = 'button';
        pill.addEventListener('click', () => reloadPreservingScroll());
        document.body.appendChild(pill);
    }
    pill.textContent = `${n} new — Load`;
}

// ── Reader page init ─────────────────────────────────────────────────────────
(function () {
    if (!document.querySelector('.reader')) return;
    readerApplyDensity();
    readerApplyAutoRead();
    readerStartPolling();
    const raw = sessionStorage.getItem('feedecho-reader-undo');
    if (raw) {
        try {
            const undo = JSON.parse(raw);
            if (Date.now() - undo.at > 5000) {
                sessionStorage.removeItem('feedecho-reader-undo');
            } else {
                readerUndoToast(undo.ids);
            }
        } catch (e) {
            sessionStorage.removeItem('feedecho-reader-undo');
        }
    }
})();
