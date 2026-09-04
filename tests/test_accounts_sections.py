"""Tests for the accounts page's per-destination collapsible sections."""

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

import auth
import database
import security
import settings
from app import app


@pytest.fixture()
def db_tmp(monkeypatch):
    """Point the DB layer at a fresh temp file per test."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)

    monkeypatch.setattr(database, "DB_PATH", database.Path(path))
    database.init_db()

    yield database

    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except OSError:
            pass


@pytest.fixture()
def multi_client(monkeypatch, db_tmp):
    """Signed-in multi-mode TestClient over the temp DB."""
    monkeypatch.setattr(settings, "MULTI", True)
    monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
    auth._login_attempts.clear()
    auth._register_attempts.clear()

    UID = 5
    with database.get_db() as db:
        db.execute(
            "INSERT INTO users (id, email, password_hash, email_verified)"
            " VALUES (?, 'u@example.com', '', 1)",
            (UID,),
        )
    client = TestClient(app)
    client.cookies.set("feedecho_session", security.sign_session(UID, "u@example.com"))
    return client


class TestAccountsPageSections:
    def test_one_section_per_destination_type(self, multi_client):
        r = multi_client.get("/accounts")
        assert r.status_code == 200
        body = r.text
        assert body.count('<details class="account-section') == 7
        for title in ("Mastodon", "Email", "Bluesky", "Micro.blog", "Matrix", "Discord", "Webhook"):
            assert f'class="account-section-title">{title}</span>' in body

    def test_sections_collapsed_when_no_accounts(self, multi_client):
        r = multi_client.get("/accounts")
        assert r.status_code == 200
        # Every section closed: none may carry the `open` attribute.
        assert '<details class="account-section" open' not in r.text
        assert "Not connected" in r.text
        assert "Expand all" in r.text and "Collapse all" in r.text

    def test_connected_section_open_with_count_badge(self, multi_client):
        with database.get_db() as db:
            db.execute(
                "INSERT INTO webhook_accounts (name, url, headers, user_id)"
                " VALUES ('A', 'https://hooks.example.com/a', '{}', 5)"
            )
            db.execute(
                "INSERT INTO webhook_accounts (name, url, headers, user_id)"
                " VALUES ('B', 'https://hooks.example.com/b', '{}', 5)"
            )
        r = multi_client.get("/accounts")
        assert r.status_code == 200
        # The webhook section itself is open and counts two connected
        # endpoints; an empty type (Discord) stays closed.
        webhook_title = r.text.find('account-section-title">Webhook</span>')
        assert webhook_title != -1
        tag_start = r.text.rfind("<details", 0, webhook_title)
        webhook_tag = r.text[tag_start:webhook_title]
        assert " open" in webhook_tag
        assert ">2 connected<" in r.text
        assert "Webhooks" in r.text
        # Only one section carries the open attribute.
        assert r.text.count('<details class="account-section" open') == 1

    def test_empty_state_still_shown_when_nothing_connected(self, multi_client):
        r = multi_client.get("/accounts")
        assert "No accounts yet." in r.text
        assert "Connect a Mastodon instance" in r.text

    def test_connected_account_renders_inside_its_section(self, multi_client):
        # Discord is used because a section follows it (Webhook): the row must
        # appear before the next section opens, proving it rendered inside the
        # Discord section and not after some stray closing </details>.
        with database.get_db() as db:
            db.execute(
                "INSERT INTO discord_accounts (name, webhook_url, channel_id, user_id)"
                " VALUES ('Receiver', 'https://discord.com/api/webhooks/1/token', '1', 5)"
            )
        r = multi_client.get("/accounts")
        body = r.text
        section_start = body.find('account-section-title">Discord</span>')
        assert section_start != -1
        receiver_at = body.find("Receiver", section_start)
        assert receiver_at != -1
        next_section = body.find(
            '<details class="account-section', section_start + 10
        )
        assert next_section != -1
        assert receiver_at < next_section

    def test_connect_forms_stack_fields_on_own_lines(self, multi_client):
        # Multi-field account forms (Bluesky: name + handle + app password,
        # Matrix: server + room + token, etc.) must not render inline, where
        # flex squeezes each input to an unreadable sliver. Every connect form
        # carries the `stacked` modifier so labels sit above full-width inputs.
        r = multi_client.get("/accounts")
        assert r.status_code == 200
        assert r.text.count('class="inline-form stacked"') == 8
        # No connect form may remain plain inline.
        assert 'class="inline-form"' not in r.text

    def test_stacked_css_rule_exists(self):
        from pathlib import Path
        css = (Path(__file__).resolve().parent.parent / "static" / "css" / "style.css").read_text(encoding="utf-8")
        import re
        m = re.search(r"\.inline-form\.stacked\s*\{([^}]*)\}", css)
        assert m, ".inline-form.stacked rule missing from style.css"
        assert "flex-direction: column" in m.group(1), "stacked forms must use a column layout"
        assert "align-items: stretch" in m.group(1), "stacked inputs must stretch full width"
