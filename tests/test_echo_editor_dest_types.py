"""Fixes from docs/reviews/2026-09-09-bug-review.md findings #2 and #23.

#2  (Critical): editEcho()/toggleEditDest() in static/js/app.js built
    destination-type <option> lists and field-visibility toggles for
    mastodon/email/bluesky/microblog/matrix only. Discord and Webhook
    echoes had no branch at all, so opening "Edit" on one of them
    silently fell back to whatever destination type happened to be
    first in the list (typically mastodon) — saving the edit then
    either reassigned the echo to the wrong destination or failed with
    a 400 "Invalid destination type".

#23 (Low): readerLoadMore() called a nonexistent `hydrateLocalTimes`
    function (guarded by `typeof ... === 'function'`, so it silently
    no-opped every time). The real function is `formatLocalTimes`.
    Items appended via the Reader's "Load more" button never got their
    <time class="local-time"> elements converted to the viewer's local
    time/locale.
"""

import re
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient

import auth
import database
import security
import settings
from app import app

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_JS = (REPO_ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")


def _tpl(name: str) -> str:
    return (REPO_ROOT / "templates" / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# #2. editEcho()/toggleEditDest() must handle discord and webhook
# ---------------------------------------------------------------------------


class TestEditEchoDiscordWebhookSupport:
    def test_no_hydrateLocalTimes_references_left(self):
        assert "hydrateLocalTimes" not in APP_JS

    def test_edit_echo_reads_discord_and_webhook_option_templates(self):
        # Mirrors mastoOpts/emailOpts/... pulled from the server-rendered
        # #<type>-options <script> blocks in templates/echoes.html.
        assert "document.getElementById('discord-options')" in APP_JS
        assert "document.getElementById('webhook-options')" in APP_JS

    def test_edit_echo_builds_discord_and_webhook_select_options(self):
        assert '<option value="discord"' in APP_JS
        assert '<option value="webhook"' in APP_JS
        assert "Discord Channel</option>" in APP_JS
        assert ">Webhook</option>" in APP_JS

    def test_edit_echo_has_discord_and_webhook_field_containers(self):
        assert 'id="edit-discord-fields-${echoId}"' in APP_JS
        assert 'id="edit-webhook-fields-${echoId}"' in APP_JS
        assert 'name="discord_account_id"' in APP_JS
        assert 'name="webhook_account_id"' in APP_JS

    def test_edit_echo_sets_selected_value_for_discord_and_webhook(self):
        assert 'row.querySelector(\'select[name="discord_account_id"]\')' in APP_JS
        assert 'row.querySelector(\'select[name="webhook_account_id"]\')' in APP_JS

    def test_toggle_edit_dest_shows_hides_discord_and_webhook_fields(self):
        m = re.search(r"function toggleEditDest\(echoId\) \{.*?\n\}", APP_JS, re.S)
        assert m, "toggleEditDest() not found"
        body = m.group(0)
        assert "edit-discord-fields-${echoId}`).style.display = destType === 'discord'" in body
        assert "edit-webhook-fields-${echoId}`).style.display = destType === 'webhook'" in body

    def test_readerLoadMore_calls_the_real_formatLocalTimes(self):
        m = re.search(r"async function readerLoadMore\(btn\) \{.*?\n\}", APP_JS, re.S)
        assert m, "readerLoadMore() not found"
        body = m.group(0)
        assert "formatLocalTimes()" in body


# ---------------------------------------------------------------------------
# Live route coverage: editing a discord/webhook echo round-trips cleanly.
# ---------------------------------------------------------------------------

DISCORD_WEBHOOK_URL = (
    "https://discord.com/api/webhooks/1234567890123456789/"
    "token_abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
)


@pytest.fixture()
def multi_client(monkeypatch, db_tmp):
    """Signed-in multi-mode TestClient over the temp DB (mirrors test_discord.py)."""
    monkeypatch.setattr(settings, "MULTI", True)
    monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
    monkeypatch.setattr(settings, "STATE_SECRET", "s" * 40)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
    auth._login_attempts.clear()
    auth._register_attempts.clear()

    uid = 7
    with database.get_db() as db:
        db.execute(
            "INSERT INTO users (id, email, password_hash, email_verified)"
            " VALUES (?, 'edituser@example.com', '', 1)",
            (uid,),
        )
    client = TestClient(app)
    client.cookies.set("feedecho_session", security.sign_session(uid, "edituser@example.com"))
    return client


class TestEditingDiscordEchoServerSide:
    def test_echoes_page_serves_discord_and_webhook_option_templates(self, multi_client):
        with mock.patch(
            "app.discord_connect",
            return_value={"webhook_url": DISCORD_WEBHOOK_URL, "name": "Feed Bot", "channel_id": "1"},
        ):
            multi_client.post(
                "/api/discord-accounts",
                data={"webhook_url": DISCORD_WEBHOOK_URL},
                follow_redirects=False,
            )
        with database.get_db() as db:
            db.execute(
                "INSERT INTO feeds (id, name, url, user_id) VALUES (1, 'f', 'https://example.com/feed', 7)"
            )
            db.execute(
                "INSERT INTO echoes (id, feed_id, destination_type, destination_id, template, user_id)"
                " VALUES (1, 1, 'discord', 1, '{{ title }}', 7)"
            )
        r = multi_client.get("/echoes")
        assert r.status_code == 200
        # The edit-dropdown option template the fixed editEcho() now reads.
        assert 'id="discord-options"' in r.text
        assert 'id="webhook-options"' in r.text
        assert "Feed Bot" in r.text

    def test_edit_discord_echo_keeps_discord_destination(self, multi_client):
        """Regression for finding #2: saving an edited discord echo (with the
        destination_type the client would now correctly keep selected) must
        not be rejected and must not silently reassign it to another type.
        """
        with mock.patch(
            "app.discord_connect",
            return_value={"webhook_url": DISCORD_WEBHOOK_URL, "name": "Feed Bot", "channel_id": "1"},
        ):
            multi_client.post(
                "/api/discord-accounts",
                data={"webhook_url": DISCORD_WEBHOOK_URL},
                follow_redirects=False,
            )
        with database.get_db() as db:
            db.execute(
                "INSERT INTO feeds (id, name, url, user_id) VALUES (1, 'f', 'https://example.com/feed', 7)"
            )
            db.execute(
                "INSERT INTO echoes (id, feed_id, destination_type, destination_id, template, user_id)"
                " VALUES (1, 1, 'discord', 1, '{{ title }}', 7)"
            )
        r = multi_client.post(
            "/api/echoes/1/edit",
            data={
                "feed_id": "1",
                "destination_type": "discord",
                "discord_account_id": "1",
                "template": "{{ title }} {{ link }}",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        with database.get_db() as db:
            row = db.execute(
                "SELECT destination_type, destination_id FROM echoes WHERE id = 1"
            ).fetchone()
        assert row["destination_type"] == "discord"
        assert row["destination_id"] == 1
