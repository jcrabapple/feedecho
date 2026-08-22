// FeedEcho — client-side interactions

// Store original row HTML for cancelEdit; avoids XSS from inline HTML serialization
const editState = new Map();

async function testAccount(accountId) {
    try {
        const resp = await fetch(`/api/accounts/${accountId}/test`, { method: 'POST' });
        const data = await resp.json();
        if (!resp.ok) {
            alert('Test failed: ' + (data.detail || resp.statusText));
            return;
        }
        alert(data.message || (data.success ? 'OK' : 'Failed'));
    } catch (e) {
        alert('Request failed: ' + e.message);
    }
}

async function testBlueskyAccount(accountId) {
    try {
        const resp = await fetch(`/api/bluesky-accounts/${accountId}/test`, { method: 'POST' });
        const data = await resp.json();
        if (!resp.ok) {
            alert('Test failed: ' + (data.detail || resp.statusText));
            return;
        }
        alert(data.message || (data.success ? 'OK' : 'Failed'));
    } catch (e) {
        alert('Request failed: ' + e.message);
    }
}

async function testFeed(feedId) {
    try {
        const resp = await fetch(`/api/feeds/${feedId}/test`, { method: 'POST' });
        const data = await resp.json();
        if (data.success) {
            const p = data.preview;
            alert(`Feed: ${p.title}\nType: ${p.type}\nItems: ${p.item_count}\n\nLatest: ${p.items[0]?.title || 'none'}`);
        } else {
            alert('Feed test failed: ' + data.error);
        }
    } catch (e) {
        alert('Request failed: ' + e.message);
    }
}

async function initFeed(feedId) {
    if (!confirm('Initialize feed? This sets the last seen item so only new posts going forward will be cross-posted.')) return;
    try {
        const resp = await fetch(`/api/feeds/${feedId}/init`, { method: 'POST' });
        const data = await resp.json();
        alert(data.message || (data.success ? 'OK' : 'Failed'));
    } catch (e) {
        alert('Request failed: ' + e.message);
    }
}

async function fetchNow(feedId) {
    try {
        const resp = await fetch(`/api/feeds/${feedId}/fetch`, { method: 'POST' });
        const data = await resp.json();
        alert(data.message || (data.success ? 'OK' : 'Failed'));
    } catch (e) {
        alert('Request failed: ' + e.message);
    }
}

async function pauseFeed(feedId) {
    try {
        const resp = await fetch(`/api/feeds/${feedId}/pause`, { method: 'POST' });
        const data = await resp.json();
        if (data.success) {
            location.reload();
        } else {
            alert(data.detail || 'Failed to toggle pause');
        }
    } catch (e) {
        alert('Request failed: ' + e.message);
    }
}

async function retryPost(postedId) {
    try {
        const resp = await fetch(`/api/history/${postedId}/retry`, { method: 'POST' });
        const data = await resp.json();
        alert(data.message || data.detail || 'Done');
        if (data.success) location.reload();
    } catch (e) {
        alert('Request failed: ' + e.message);
    }
}

async function testAltText() {
    const btn = event.target;
    const originalText = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Testing...';
    try {
        const resp = await fetch('/api/settings/alt-text/test', { method: 'POST' });
        const data = await resp.json();
        alert(data.message || (data.success ? 'OK' : 'Failed'));
    } catch (e) {
        alert('Request failed: ' + e.message);
    } finally {
        btn.disabled = false;
        btn.textContent = originalText;
    }
}

async function giveUpPost(postedId) {
    if (!confirm('Give up on this item? The feed will move past it and it will not be delivered.')) return;
    try {
        const resp = await fetch(`/api/history/${postedId}/give-up`, { method: 'POST' });
        const data = await resp.json();
        alert(data.message || data.detail || 'Done');
        if (data.success) location.reload();
    } catch (e) {
        alert('Request failed: ' + e.message);
    }
}

async function toggleEcho(echoId) {
    try {
        const resp = await fetch(`/api/echoes/${echoId}/toggle`, { method: 'POST' });
        const data = await resp.json();
        if (data.success) {
            location.reload();
        }
    } catch (e) {
        alert('Request failed: ' + e.message);
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
    const enabled = row.dataset.enabled === '1';

    const feedOpts = document.getElementById('feed-options').innerHTML;
    const mastoOpts = document.getElementById('mastodon-options').innerHTML;
    const emailOpts = document.getElementById('email-options').innerHTML;
    const blueskyOpts = document.getElementById('bluesky-options').innerHTML;

    const mastoStyle = destType === 'mastodon' ? '' : 'display:none';
    const emailStyle = destType === 'email' ? '' : 'display:none';
    const blueskyStyle = destType === 'bluesky' ? '' : 'display:none';

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
            <div class="form-row">
                <label>Enabled
                    <input type="checkbox" name="enabled" value="true"${enabled ? ' checked' : ''}>
                </label>
            </div>
            <div class="form-row">
                <label>Template
                    <textarea name="template" rows="3">${escapeHTML(template)}</textarea>
                    <button type="button" class="btn-sm template-preview-btn" onclick="previewTemplate(this)">Preview</button>
                </label>
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
                    <select name="delivery_mode">
                        <option value="instant"${deliveryMode === 'instant' ? ' selected' : ''}>Instant (one email per item)</option>
                        <option value="digest"${deliveryMode === 'digest' ? ' selected' : ''}>Digest (batch items into one email, sent hourly)</option>
                    </select>
                </label>
            </div>
            <div class="form-row edit-actions">
                <button type="submit" class="btn-sm">Save</button>
                <button type="button" class="btn-sm btn-danger" onclick="cancelEdit(${echoId})">Cancel</button>
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
}

function toggleEditDest(echoId) {
    const destType = document.getElementById(`edit-dest-type-${echoId}`).value;
    document.getElementById(`edit-mastodon-fields-${echoId}`).style.display = destType === 'mastodon' ? '' : 'none';
    document.getElementById(`edit-email-fields-${echoId}`).style.display = destType === 'email' ? '' : 'none';
    document.getElementById(`edit-bluesky-fields-${echoId}`).style.display = destType === 'bluesky' ? '' : 'none';
    const digestFields = document.getElementById(`edit-digest-fields-${echoId}`);
    if (digestFields) digestFields.style.display = destType === 'email' ? '' : 'none';
}

function cancelEdit(echoId) {
    const row = document.getElementById(`echo-row-${echoId}`);
    if (row && editState.has(echoId)) {
        row.innerHTML = editState.get(echoId);
        editState.delete(echoId);
    }
}

async function previewTemplate(btn) {
    const form = btn.closest('form');
    if (!form) return;

    const templateField = form.querySelector('textarea[name="template"]');
    const feedSelect = form.querySelector('select[name="feed_id"]');
    if (!templateField) return;
    if (!feedSelect) {
        alert('Preview needs a feed selector on this form.');
        return;
    }
    if (!feedSelect.value) {
        alert('Select a feed first, then preview.');
        return;
    }

    let box = btn.parentElement.querySelector('.template-preview');
    if (!box) {
        box = document.createElement('div');
        box.className = 'template-preview';
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
    return div.innerHTML;
}

/* Theme toggle — persists to localStorage, falls back to system preference */
(function () {
    const btn = document.getElementById('theme-toggle');
    if (!btn) return;

    function currentTheme() {
        return document.documentElement.getAttribute('data-theme') || 'dark';
    }

    function render(theme) {
        btn.textContent = theme === 'light' ? '☀' : '☾';
        btn.setAttribute('aria-pressed', theme === 'light' ? 'true' : 'false');
    }

    render(currentTheme());

    btn.addEventListener('click', function () {
        const next = currentTheme() === 'light' ? 'dark' : 'light';
        document.documentElement.setAttribute('data-theme', next);
        try { localStorage.setItem('feedecho-theme', next); } catch (e) {}
        render(next);
    });

    // Follow system preference changes when the user hasn't chosen explicitly
    window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', function (e) {
        let stored = null;
        try { stored = localStorage.getItem('feedecho-theme'); } catch (err) {}
        if (stored) return;
        const theme = e.matches ? 'light' : 'dark';
        document.documentElement.setAttribute('data-theme', theme);
        render(theme);
    });
})();
