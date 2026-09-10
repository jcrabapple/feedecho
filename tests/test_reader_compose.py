"""Tests for Reader Compose (Phase 2)."""

import pytest
from fastapi.testclient import TestClient

import database
import security
import settings
import scheduler
from app import app, DESTINATION_LIMITS


@pytest.fixture
def compose_env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MULTI", True)
    monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
    monkeypatch.setattr(settings, "STATE_SECRET", "s" * 40)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "compose.db")
    database.init_db()
    monkeypatch.setattr(scheduler, "check_all_feeds", lambda: None)
    with database.get_db() as db:
        # Users (IDs > 100 to avoid collision with default user 1)
        db.execute("INSERT INTO users (id, email, password_hash, plan) VALUES (101, 'u1@example.com', '', 'paid')")
        db.execute("INSERT INTO users (id, email, password_hash, plan) VALUES (102, 'u2@example.com', '', 'paid')")

        # Accounts for user 101
        db.execute("INSERT INTO accounts (id, name, username, instance, access_token, user_id) VALUES (1, 'Mastodon main', 'm1', 'https://mastodon.social', 'tok', 101)")
        db.execute("INSERT INTO bluesky_accounts (id, name, handle, app_password, did, access_jwt, refresh_jwt, pds, session_expires_at, user_id) VALUES (1, 'Bluesky main', 'b1', 'pass', 'did:plc:1', 'tok', 'ref', 'https://bsky.social', '2099-01-01 00:00:00', 101)")
        db.execute("INSERT INTO discord_accounts (id, name, webhook_url, user_id) VALUES (1, 'Discord channel', 'https://discord.com/api/webhooks/1/tok', 101)")

        # Accounts for user 102
        db.execute("INSERT INTO accounts (id, name, username, instance, access_token, user_id) VALUES (2, 'U2 Mastodon', 'u2m', 'https://mastodon.social', 'tok', 102)")

        # Feeds & items for user 101
        db.execute("INSERT INTO feeds (id, name, url, read_enabled, user_id) VALUES (1, 'Feed 1', 'https://example.com/feed1', 1, 101)")
        db.execute(
            "INSERT INTO feed_items (id, feed_id, item_id, title, link, summary, content, image_url, image_alt, published_at, is_read)"
            " VALUES (1, 1, 'item-1', 'Article One', 'https://example.com/1', 'Summary 1', 'Full body 1', 'https://example.com/pic.jpg', 'Feed Alt Text', '2026-01-01 12:00:00', 0)"
        )

        # Feed & item for user 102
        db.execute("INSERT INTO feeds (id, name, url, read_enabled, user_id) VALUES (2, 'Feed 2', 'https://example.com/feed2', 1, 102)")
        db.execute(
            "INSERT INTO feed_items (id, feed_id, item_id, title, link, summary, published_at, is_read)"
            " VALUES (2, 2, 'item-2', 'Article Two', 'https://example.com/2', 'Summary 2', '2026-01-01 12:00:00', 0)"
        )
    return settings


def _as_u1(client):
    client.cookies.set("feedecho_session", security.sign_session(101, "u1@example.com"))
    return client


def _as_u2(client):
    client.cookies.set("feedecho_session", security.sign_session(102, "u2@example.com"))
    return client


def test_compose_preview_endpoint(compose_env):
    client = TestClient(app)
    _as_u1(client)

    resp = client.get("/api/reader/1/compose")
    assert resp.status_code == 200
    data = resp.json()

    assert data["item"]["title"] == "Article One"
    assert data["item"]["image_url"] == "https://example.com/pic.jpg"
    assert data["item"]["image_alt"] == "Feed Alt Text"
    assert len(data["destinations"]) == 3
    # Destination limits mapped correctly
    d_map = {d["value"]: d for d in data["destinations"]}
    assert d_map["mastodon:1"]["limit"] == 500
    assert d_map["bluesky:1"]["limit"] == 300
    assert d_map["discord:1"]["limit"] == 2000
    assert "mastodon:1" in data["rendered"]
    assert "Article One https://example.com/1" in data["rendered"]["mastodon:1"]


def test_compose_override_content_sent_verbatim(compose_env, monkeypatch):
    """User-edited commentary and braces are NOT expanded by Jinja."""
    dispatched = []

    def fake_post_status(instance, access_token, content, **kwargs):
        dispatched.append(content)
        return {"id": "m-status-1"}

    monkeypatch.setattr(scheduler, "post_status", fake_post_status)

    client = TestClient(app)
    _as_u1(client)

    custom_text = "Check this out: {{ title }} {% if unclosed brace"
    resp = client.post(
        "/api/reader/1/compose",
        data={
            "destinations": "mastodon:1",
            "content": custom_text,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    # Verbatim content sent
    assert len(dispatched) == 1
    assert dispatched[0] == custom_text


def test_compose_multi_destination_partial_reporting(compose_env, monkeypatch):
    """Posting to multiple destinations reports per-destination status and handles partial failure."""
    def fake_post_status(instance, access_token, content, **kwargs):
        return {"id": "m-ok"}

    def fake_create_post(pds, access_jwt, repo, text, **kwargs):
        raise scheduler.BlueskyError("Bluesky rate limited")

    monkeypatch.setattr(scheduler, "post_status", fake_post_status)
    monkeypatch.setattr(scheduler, "create_post", fake_create_post)

    client = TestClient(app)
    _as_u1(client)

    resp = client.post(
        "/api/reader/1/compose",
        data={
            "destinations": "mastodon:1,bluesky:1",
            "content": "Multi-post test",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False  # Partial failure
    assert len(data["results"]) == 2

    res_map = {r["destination"]: r for r in data["results"]}
    assert res_map["mastodon:1"]["success"] is True
    assert res_map["mastodon:1"]["status"] == "success"
    assert res_map["bluesky:1"]["success"] is False
    assert res_map["bluesky:1"]["error_message"] is not None


def test_compose_destination_ownership_isolation(compose_env):
    """Cannot compose to an account belonging to another user."""
    client = TestClient(app, raise_server_exceptions=False)
    _as_u1(client)

    # user 2's mastodon account id is 2
    resp = client.post(
        "/api/reader/1/compose",
        data={
            "destinations": "mastodon:2",
            "content": "Hack attempt",
        },
    )
    assert resp.status_code == 404
    # In multi mode, HTTPException(404) is caught by Starlette error handlers
    assert "not found" in resp.text.lower()


def test_compose_destination_permanent_failure_reports_failed(compose_env, monkeypatch):
    """Permanent delivery failure (gave_up) reports success=False in compose results."""
    def fake_discord_send(*args, **kwargs):
        raise scheduler.DiscordAuthError("Invalid Webhook Token")

    monkeypatch.setattr(scheduler, "discord_send_webhook", fake_discord_send)

    client = TestClient(app)
    _as_u1(client)

    resp = client.post(
        "/api/reader/1/compose",
        data={
            "destinations": "discord:1",
            "content": "Testing failure report",
        },
    )
    assert resp.status_code == 200
    res = resp.json()
    assert res["success"] is False
    assert res["results"][0]["success"] is False
    assert res["results"][0]["status"] in ("failed", "gave_up")


def test_compose_empty_body_rejected(compose_env):
    client = TestClient(app)
    _as_u1(client)

    # Empty content and template that renders empty
    resp = client.post(
        "/api/reader/1/compose",
        data={
            "destinations": "mastodon:1",
            "content": "   ",
            "template": "",
        },
    )
    assert resp.status_code == 400


def test_compose_oversize_body_rejected(compose_env, monkeypatch):
    """Hard output cap rejects massive payloads."""
    client = TestClient(app)
    _as_u1(client)

    huge = "A" * (1_000_001)
    resp = client.post(
        "/api/reader/1/compose",
        data={
            "destinations": "mastodon:1",
            "content": huge,
        },
    )
    assert resp.status_code == 200
    res = resp.json()
    assert res["success"] is False
    assert "exceeds" in res["results"][0]["error_message"]


def test_shout_alias_still_works(compose_env, monkeypatch):
    """POST /api/reader/{id}/shout continues to work as a thin alias."""
    monkeypatch.setattr(scheduler, "post_status", lambda **kw: {"id": "m1"})

    client = TestClient(app)
    _as_u1(client)

    resp = client.post(
        "/api/reader/1/shout",
        data={"destination": "mastodon:1", "template": "{{ title }}"},
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert resp.json()["status"] == "success"
