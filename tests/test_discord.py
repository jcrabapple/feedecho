"""Tests for the Discord destination: client, dispatch, and routes.

All network calls are monkeypatched — no live Discord traffic.
"""

import os
import tempfile
from unittest import mock

import pytest
from fastapi.testclient import TestClient

import auth
import database
import discord
import scheduler
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

    monkeypatch.setattr(scheduler, "get_db", database.get_db)

    yield database

    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except OSError:
            pass


def _item(**overrides):
    item = {
        "id": "item-1",
        "title": "Test Post",
        "link": "https://example.com/post/1",
        "summary": "A summary of the post.",
        "image_url": "",
    }
    item.update(overrides)
    return item


WEBHOOK_URL = (
    "https://discord.com/api/webhooks/1234567890123456789/"
    "token_abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
)


def _setup_discord_echo(db_tmp, echo_overrides=None):
    """Create a Discord account, feed, and echo. Returns the echo row."""
    echo_kwargs = {
        "destination_type": "discord",
        "destination_id": 1,
        "template": "{{ title }} {{ link }}",
        "visibility": "public",
        "filter_keywords": "",
        "filter_mode": "exclude",
        "content_warning": "",
        "attach_image": 0,
        "enabled": 1,
    }
    if echo_overrides:
        echo_kwargs.update(echo_overrides)

    with db_tmp.get_db() as db:
        db.execute(
            "INSERT INTO discord_accounts (name, webhook_url, channel_id)"
            " VALUES (?, ?, ?)",
            ("Announcements", WEBHOOK_URL, "1112223334445556667"),
        )
        db.execute(
            "INSERT INTO feeds (name, url) VALUES (?, ?)",
            ("f", "https://example.com/feed"),
        )
        db.execute(
            """INSERT INTO echoes (feed_id, destination_type, destination_id, template,
                                   visibility, filter_keywords, filter_mode,
                                   content_warning, attach_image, enabled)
               VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                echo_kwargs["destination_type"],
                echo_kwargs["destination_id"],
                echo_kwargs["template"],
                echo_kwargs["visibility"],
                echo_kwargs["filter_keywords"],
                echo_kwargs["filter_mode"],
                echo_kwargs["content_warning"],
                echo_kwargs["attach_image"],
                echo_kwargs["enabled"],
            ),
        )
    with db_tmp.get_db() as db:
        return db.execute("SELECT * FROM echoes WHERE id = 1").fetchone()


def _resp(payload, status_code=200, headers=None):
    r = mock.Mock()
    r.status_code = status_code
    r.json.return_value = payload
    r.headers = headers or {}
    return r


# ── Client: normalize_webhook_url ───────────────────────────────────────────


class TestNormalizeWebhookURL:
    def test_valid_url_passes_through(self):
        assert discord.normalize_webhook_url(WEBHOOK_URL) == WEBHOOK_URL

    def test_discordapp_com_canonicalized(self):
        raw = "https://discordapp.com" + WEBHOOK_URL[len("https://discord.com") :]
        assert discord.normalize_webhook_url(raw) == WEBHOOK_URL

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            discord.normalize_webhook_url("")

    def test_http_rejected(self):
        with pytest.raises(ValueError):
            discord.normalize_webhook_url(WEBHOOK_URL.replace("https://", "http://"))

    def test_wrong_host_rejected(self):
        raw = WEBHOOK_URL.replace("discord.com", "discord.com.evil.example")
        with pytest.raises(ValueError):
            discord.normalize_webhook_url(raw)

    def test_non_webhook_path_rejected(self):
        with pytest.raises(ValueError):
            discord.normalize_webhook_url(
                "https://discord.com/channels/1234567890123456789/1112223334445556667"
            )

    def test_garbage_rejected(self):
        with pytest.raises(ValueError):
            discord.normalize_webhook_url("just some text")


# ── Client: inspect_webhook / connect / send ────────────────────────────────


class TestInspectWebhook:
    def test_returns_name_and_channel(self):
        payload = {
            "type": 1,
            "id": "1234567890123456789",
            "name": "Feed Bot",
            "channel_id": "1112223334445556667",
            "guild_id": "9998887776665554443",
        }
        with mock.patch.object(discord.httpx, "Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            client.get.return_value = _resp(payload)
            info = discord.inspect_webhook(WEBHOOK_URL)
        assert info == {"name": "Feed Bot", "channel_id": "1112223334445556667"}

    def test_missing_name_is_empty_string(self):
        with mock.patch.object(discord.httpx, "Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            client.get.return_value = _resp({"channel_id": "1"})
            info = discord.inspect_webhook(WEBHOOK_URL)
        assert info == {"name": "", "channel_id": "1"}

    def test_401_is_auth_error(self):
        with mock.patch.object(discord.httpx, "Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            client.get.return_value = _resp(
                {"message": "Invalid Webhook Token", "code": 50027}, 401
            )
            with pytest.raises(discord.DiscordAuthError):
                discord.inspect_webhook(WEBHOOK_URL)

    def test_404_is_not_found(self):
        with mock.patch.object(discord.httpx, "Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            client.get.return_value = _resp(
                {"message": "Unknown Webhook", "code": 10015}, 404
            )
            with pytest.raises(discord.DiscordNotFoundError):
                discord.inspect_webhook(WEBHOOK_URL)

    def test_network_error_is_discord_error(self):
        import httpx as _httpx

        with mock.patch.object(discord.httpx, "Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            client.get.side_effect = _httpx.ConnectError("boom")
            with pytest.raises(discord.DiscordError):
                discord.inspect_webhook(WEBHOOK_URL)


class TestConnect:
    def test_returns_normalized_url_and_metadata(self):
        info = {"name": "Feed Bot", "channel_id": "111"}
        with mock.patch.object(discord, "inspect_webhook", return_value=info):
            result = discord.connect(WEBHOOK_URL)
        assert result == {
            "webhook_url": WEBHOOK_URL,
            "name": "Feed Bot",
            "channel_id": "111",
        }

    def test_malformed_url_raises_value_error(self):
        with pytest.raises(ValueError):
            discord.connect("not a url")


class TestSendWebhook:
    def test_posts_content(self):
        with mock.patch.object(discord.httpx, "Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            client.post.return_value = _resp({}, 204)
            discord.send_webhook(WEBHOOK_URL, "Hello Discord")
        _, kwargs = client.post.call_args
        assert kwargs["json"] == {"content": "Hello Discord"}

    def test_posts_embed_when_given(self):
        embed = {"title": "T", "url": "https://example.com"}
        with mock.patch.object(discord.httpx, "Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            client.post.return_value = _resp({}, 204)
            discord.send_webhook(WEBHOOK_URL, "Hello", embed=embed)
        _, kwargs = client.post.call_args
        assert kwargs["json"] == {"content": "Hello", "embeds": [embed]}

    def test_content_truncated_to_2000(self):
        long_text = "x" * 5000
        with mock.patch.object(discord.httpx, "Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            client.post.return_value = _resp({}, 204)
            discord.send_webhook(WEBHOOK_URL, long_text)
        _, kwargs = client.post.call_args
        sent = kwargs["json"]["content"]
        assert len(sent) <= discord.MAX_CONTENT_CHARS
        assert sent.endswith("…")

    def test_empty_content_is_bad_request(self):
        with pytest.raises(discord.DiscordBadRequestError):
            discord.send_webhook(WEBHOOK_URL, "   ")

    def test_tampered_stored_url_is_bad_request(self):
        # A tampered DB row must not be POSTed to; fail permanently.
        with pytest.raises(discord.DiscordBadRequestError):
            discord.send_webhook("http://127.0.0.1:8080/", "hi")

    def test_401_is_auth_error(self):
        with mock.patch.object(discord.httpx, "Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            client.post.return_value = _resp({"message": "Invalid Webhook Token"}, 401)
            with pytest.raises(discord.DiscordAuthError):
                discord.send_webhook(WEBHOOK_URL, "hi")

    def test_404_is_not_found(self):
        with mock.patch.object(discord.httpx, "Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            client.post.return_value = _resp({"message": "Unknown Webhook"}, 404)
            with pytest.raises(discord.DiscordNotFoundError):
                discord.send_webhook(WEBHOOK_URL, "hi")

    def test_400_is_bad_request(self):
        with mock.patch.object(discord.httpx, "Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            client.post.return_value = _resp({"message": "Bad payload"}, 400)
            with pytest.raises(discord.DiscordBadRequestError):
                discord.send_webhook(WEBHOOK_URL, "hi")

    def test_redirect_is_error(self):
        # Webhook endpoints never redirect; a 3xx must not be followed.
        with mock.patch.object(discord.httpx, "Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            client.post.return_value = _resp({}, 307)
            with pytest.raises(discord.DiscordError):
                discord.send_webhook(WEBHOOK_URL, "hi")

    def test_429_carries_retry_after_from_body(self):
        with mock.patch.object(discord.httpx, "Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            client.post.return_value = _resp(
                {"message": "You are being rate limited.", "retry_after": 100}, 429
            )
            with pytest.raises(discord.DiscordRateLimitError) as exc_info:
                discord.send_webhook(WEBHOOK_URL, "hi")
        assert exc_info.value.retry_after == 100.0

    def test_429_carries_retry_after_from_header(self):
        with mock.patch.object(discord.httpx, "Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            client.post.return_value = _resp(
                {"message": "You are being rate limited."}, 429,
                headers={"Retry-After": "60"},
            )
            with pytest.raises(discord.DiscordRateLimitError) as exc_info:
                discord.send_webhook(WEBHOOK_URL, "hi")
        assert exc_info.value.retry_after == 60.0


class TestBuildEmbed:
    def test_all_fields(self):
        embed = discord.build_embed(
            "Title", "https://example.com/post", "https://example.com/img.png"
        )
        assert embed == {
            "title": "Title",
            "url": "https://example.com/post",
            "image": {"url": "https://example.com/img.png"},
        }

    def test_title_truncated_to_256(self):
        embed = discord.build_embed("t" * 500, "https://example.com", "")
        assert len(embed["title"]) == discord.MAX_EMBED_TITLE_CHARS
        assert "url" in embed
        assert "image" not in embed

    def test_no_title_but_image_and_url(self):
        embed = discord.build_embed("", "https://example.com", "https://example.com/i.png")
        assert embed == {"url": "https://example.com", "image": {"url": "https://example.com/i.png"}}

    def test_non_http_image_dropped(self):
        embed = discord.build_embed("T", "https://example.com", "javascript:alert(1)")
        assert "image" not in embed

    def test_non_http_url_dropped(self):
        embed = discord.build_embed("T", "javascript:alert(1)", "https://example.com/i.png")
        assert "url" not in embed
        assert "image" in embed

    def test_nothing_fits_returns_none(self):
        assert discord.build_embed("", "", "") is None
        assert discord.build_embed("", "javascript:alert(1)", "") is None


class TestTestConnection:
    def test_ok(self):
        with mock.patch.object(
            discord, "inspect_webhook", return_value={"name": "Bot", "channel_id": "1"}
        ):
            ok, msg = discord.test_connection(WEBHOOK_URL)
        assert ok is True
        assert "Webhook OK" in msg

    def test_auth_error(self):
        with mock.patch.object(
            discord, "inspect_webhook", side_effect=discord.DiscordAuthError("bad")
        ):
            ok, msg = discord.test_connection(WEBHOOK_URL)
        assert ok is False
        assert "bad" in msg


# ── Scheduler dispatch ──────────────────────────────────────────────────────


class TestSendDiscordDispatch:
    def test_success(self, db_tmp, monkeypatch):
        echo = _setup_discord_echo(db_tmp)
        item = _item()

        sent = []
        monkeypatch.setattr(
            scheduler,
            "discord_send_webhook",
            lambda *a, **kw: sent.append(a) or None,
        )

        ok = scheduler.process_echo(echo, item)
        assert ok is True
        assert len(sent) == 1
        # (webhook_url, content, embed)
        assert sent[0][1] == "Test Post https://example.com/post/1"
        assert sent[0][2] is None
        with db_tmp.get_db() as db:
            row = db.execute(
                "SELECT status, post_url FROM posted_items WHERE echo_id = 1"
            ).fetchone()
        assert row["status"] == "success"
        assert row["post_url"] == ""

    def test_auth_error_is_permanent(self, db_tmp, monkeypatch):
        echo = _setup_discord_echo(db_tmp)
        item = _item()

        def fail(*a, **kw):
            raise discord.DiscordAuthError("invalid token")

        monkeypatch.setattr(scheduler, "discord_send_webhook", fail)
        gave_up = scheduler.process_echo(echo, item)
        assert gave_up is True
        with db_tmp.get_db() as db:
            row = db.execute(
                "SELECT status FROM posted_items WHERE echo_id = 1"
            ).fetchone()
        assert row["status"] == "gave_up"

    def test_not_found_is_permanent(self, db_tmp, monkeypatch):
        echo = _setup_discord_echo(db_tmp)
        item = _item()

        def fail(*a, **kw):
            raise discord.DiscordNotFoundError("deleted")

        monkeypatch.setattr(scheduler, "discord_send_webhook", fail)
        gave_up = scheduler.process_echo(echo, item)
        assert gave_up is True

    def test_generic_error_is_retryable(self, db_tmp, monkeypatch):
        echo = _setup_discord_echo(db_tmp)
        item = _item()

        def fail(*a, **kw):
            raise discord.DiscordError("HTTP 500")

        monkeypatch.setattr(scheduler, "discord_send_webhook", fail)
        gave_up = scheduler.process_echo(echo, item)
        assert not gave_up
        with db_tmp.get_db() as db:
            row = db.execute(
                "SELECT status, attempt_count FROM posted_items WHERE echo_id = 1"
            ).fetchone()
        assert row["status"] == "failed"
        assert row["attempt_count"] == 1

    def test_rate_limit_is_retryable(self, db_tmp, monkeypatch):
        echo = _setup_discord_echo(db_tmp)
        item = _item()

        def fail(*a, **kw):
            raise discord.DiscordRateLimitError(retry_after=30)

        monkeypatch.setattr(scheduler, "discord_send_webhook", fail)
        gave_up = scheduler.process_echo(echo, item)
        assert not gave_up
        with db_tmp.get_db() as db:
            row = db.execute(
                "SELECT status, attempt_count FROM posted_items WHERE echo_id = 1"
            ).fetchone()
        assert row["status"] == "failed"
        assert row["attempt_count"] == 1

    def test_error_text_never_contains_webhook_url(self, db_tmp, monkeypatch):
        echo = _setup_discord_echo(db_tmp)
        item = _item()

        def fail(*a, **kw):
            # Simulates an httpx-layer failure; the message must carry no URL.
            raise discord.DiscordError("Could not reach Discord (ConnectError)")

        monkeypatch.setattr(scheduler, "discord_send_webhook", fail)
        scheduler.process_echo(echo, item)
        with db_tmp.get_db() as db:
            row = db.execute(
                "SELECT error_message FROM posted_items WHERE echo_id = 1"
            ).fetchone()
        assert WEBHOOK_URL not in row["error_message"]

    def test_missing_account_is_permanent(self, db_tmp, monkeypatch):
        echo = _setup_discord_echo(db_tmp, echo_overrides={"destination_id": 999})
        item = _item()

        sent = []
        monkeypatch.setattr(
            scheduler,
            "discord_send_webhook",
            lambda *a, **kw: sent.append(kw) or None,
        )
        gave_up = scheduler.process_echo(echo, item)
        assert gave_up is True
        assert sent == []

    def test_embed_built_when_image_attached(self, db_tmp, monkeypatch):
        echo = _setup_discord_echo(db_tmp, echo_overrides={"attach_image": 1})
        item = _item(image_url="https://example.com/img.png")

        sent = []
        monkeypatch.setattr(
            scheduler,
            "discord_send_webhook",
            lambda *a, **kw: sent.append(a) or None,
        )
        ok = scheduler.process_echo(echo, item)
        assert ok is True
        assert sent[0][2] == {
            "title": "Test Post",
            "url": "https://example.com/post/1",
            "image": {"url": "https://example.com/img.png"},
        }

    def test_no_embed_when_no_image(self, db_tmp, monkeypatch):
        echo = _setup_discord_echo(db_tmp, echo_overrides={"attach_image": 1})
        item = _item()  # image_url is ""

        sent = []
        monkeypatch.setattr(
            scheduler,
            "discord_send_webhook",
            lambda *a, **kw: sent.append(a) or None,
        )
        ok = scheduler.process_echo(echo, item)
        assert ok is True
        # The embed keeps the title + link card; only the image is omitted.
        assert sent[0][2] == {
            "title": "Test Post",
            "url": "https://example.com/post/1",
        }


# ── Routes ──────────────────────────────────────────────────────────────────


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


CONNECT_INFO = {
    "webhook_url": WEBHOOK_URL,
    "name": "Feed Bot",
    "channel_id": "1112223334445556667",
}


class TestDiscordRoutes:
    def test_connect_creates_row(self, multi_client):
        with mock.patch("app.discord_connect", return_value=CONNECT_INFO):
            r = multi_client.post(
                "/api/discord-accounts",
                data={"webhook_url": WEBHOOK_URL},
                follow_redirects=False,
            )
        assert r.status_code == 303
        assert "discord_connected" in r.headers["location"]
        with database.get_db() as db:
            row = db.execute(
                "SELECT * FROM discord_accounts WHERE user_id = 5"
            ).fetchone()
        assert row["webhook_url"] == WEBHOOK_URL
        assert row["name"] == "Feed Bot"
        assert row["channel_id"] == "1112223334445556667"

    def test_connect_blank_url_rejected(self, multi_client):
        r = multi_client.post("/api/discord-accounts", data={"webhook_url": "  "})
        assert r.status_code == 200
        assert "Paste the Discord webhook URL" in r.text

    def test_connect_bad_url_renders_message(self, multi_client):
        with mock.patch(
            "app.discord_connect", side_effect=ValueError("not a webhook URL")
        ):
            r = multi_client.post(
                "/api/discord-accounts", data={"webhook_url": "https://discord.com"}
            )
        assert r.status_code == 200
        assert "not a webhook URL" in r.text
        with database.get_db() as db:
            count = db.execute(
                "SELECT COUNT(*) AS c FROM discord_accounts"
            ).fetchone()["c"]
        assert count == 0

    def test_connect_auth_error_renders_message(self, multi_client):
        with mock.patch(
            "app.discord_connect", side_effect=discord.DiscordAuthError("invalid token")
        ):
            r = multi_client.post(
                "/api/discord-accounts", data={"webhook_url": WEBHOOK_URL}
            )
        assert r.status_code == 200
        assert "invalid token" in r.text
        with database.get_db() as db:
            count = db.execute(
                "SELECT COUNT(*) AS c FROM discord_accounts"
            ).fetchone()["c"]
        assert count == 0

    def test_reconnect_updates_name_in_place(self, multi_client):
        with mock.patch("app.discord_connect", return_value=CONNECT_INFO):
            multi_client.post(
                "/api/discord-accounts",
                data={"webhook_url": WEBHOOK_URL, "name": "Old"},
                follow_redirects=False,
            )
            multi_client.post(
                "/api/discord-accounts",
                data={"webhook_url": WEBHOOK_URL, "name": "New"},
                follow_redirects=False,
            )
        with database.get_db() as db:
            rows = db.execute(
                "SELECT name FROM discord_accounts WHERE user_id = 5"
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["name"] == "New"

    def test_discordapp_alias_dedupes_to_same_row(self, multi_client):
        alias = "https://discordapp.com" + WEBHOOK_URL[len("https://discord.com") :]
        info = dict(CONNECT_INFO)
        with mock.patch("app.discord_connect", return_value=info):
            multi_client.post(
                "/api/discord-accounts", data={"webhook_url": WEBHOOK_URL},
                follow_redirects=False,
            )
            multi_client.post(
                "/api/discord-accounts", data={"webhook_url": alias},
                follow_redirects=False,
            )
        with database.get_db() as db:
            count = db.execute(
                "SELECT COUNT(*) AS c FROM discord_accounts WHERE user_id = 5"
            ).fetchone()["c"]
        assert count == 1

    def test_test_endpoint(self, multi_client):
        with mock.patch("app.discord_connect", return_value=CONNECT_INFO):
            multi_client.post(
                "/api/discord-accounts",
                data={"webhook_url": WEBHOOK_URL},
                follow_redirects=False,
            )
        with mock.patch(
            "app.test_discord_connection", return_value=(True, "Webhook OK")
        ):
            r = multi_client.post("/api/discord-accounts/1/test")
        assert r.status_code == 200
        assert r.json() == {"success": True, "message": "Webhook OK"}

    def test_delete_blocked_by_dependent_echo(self, multi_client):
        with mock.patch("app.discord_connect", return_value=CONNECT_INFO):
            multi_client.post(
                "/api/discord-accounts",
                data={"webhook_url": WEBHOOK_URL},
                follow_redirects=False,
            )
        with database.get_db() as db:
            db.execute(
                "INSERT INTO feeds (id, name, url, user_id) VALUES (1, 'f', 'https://example.com/feed', 5)"
            )
            db.execute(
                "INSERT INTO echoes (feed_id, destination_type, destination_id, template, user_id)"
                " VALUES (1, 'discord', 1, '{{ title }}', 5)"
            )
        r = multi_client.post("/api/discord-accounts/1/delete", follow_redirects=False)
        assert r.status_code == 200
        assert "used by echoes" in r.text
        with database.get_db() as db:
            count = db.execute(
                "SELECT COUNT(*) AS c FROM discord_accounts"
            ).fetchone()["c"]
        assert count == 1

    def test_delete_succeeds_without_echoes(self, multi_client):
        with mock.patch("app.discord_connect", return_value=CONNECT_INFO):
            multi_client.post(
                "/api/discord-accounts",
                data={"webhook_url": WEBHOOK_URL},
                follow_redirects=False,
            )
        r = multi_client.post("/api/discord-accounts/1/delete", follow_redirects=False)
        assert r.status_code == 303
        assert "discord_deleted" in r.headers["location"]

    def test_accounts_page_lists_webhooks_without_url(self, multi_client):
        with mock.patch("app.discord_connect", return_value=CONNECT_INFO):
            multi_client.post(
                "/api/discord-accounts",
                data={"webhook_url": WEBHOOK_URL},
                follow_redirects=False,
            )
        r = multi_client.get("/accounts")
        assert r.status_code == 200
        assert "Discord Webhooks" in r.text
        assert "Feed Bot" in r.text
        # The credential itself must never be rendered back.
        assert WEBHOOK_URL not in r.text

    def test_echoes_and_history_never_render_webhook_url(self, multi_client):
        with mock.patch("app.discord_connect", return_value=CONNECT_INFO):
            multi_client.post(
                "/api/discord-accounts",
                data={"webhook_url": WEBHOOK_URL},
                follow_redirects=False,
            )
        with database.get_db() as db:
            db.execute(
                "INSERT INTO feeds (id, name, url, user_id) VALUES (1, 'f', 'https://example.com/feed', 5)"
            )
            db.execute(
                "INSERT INTO echoes (id, feed_id, destination_type, destination_id, template, user_id)"
                " VALUES (1, 1, 'discord', 1, '{{ title }}', 5)"
            )
            db.execute(
                "INSERT INTO posted_items (echo_id, item_id, item_title, item_url, status)"
                " VALUES (1, 'item-1', 'T', 'https://example.com', 'success')"
            )
        for path in ("/echoes", "/history"):
            r = multi_client.get(path)
            assert r.status_code == 200
            assert WEBHOOK_URL not in r.text, f"webhook URL leaked on {path}"

    def test_echoes_page_offers_discord_destination(self, multi_client):
        with mock.patch("app.discord_connect", return_value=CONNECT_INFO):
            multi_client.post(
                "/api/discord-accounts",
                data={"webhook_url": WEBHOOK_URL},
                follow_redirects=False,
            )
        with database.get_db() as db:
            db.execute(
                "INSERT INTO feeds (id, name, url, user_id) VALUES (1, 'f', 'https://example.com/feed', 5)"
            )
        r = multi_client.get("/echoes")
        assert r.status_code == 200
        assert 'value="discord"' in r.text
        assert 'id="discord-fields"' in r.text

    def test_add_echo_with_discord_destination(self, multi_client):
        with mock.patch("app.discord_connect", return_value=CONNECT_INFO):
            multi_client.post(
                "/api/discord-accounts",
                data={"webhook_url": WEBHOOK_URL},
                follow_redirects=False,
            )
        with database.get_db() as db:
            db.execute(
                "INSERT INTO feeds (id, name, url, user_id) VALUES (1, 'f', 'https://example.com/feed', 5)"
            )
        r = multi_client.post(
            "/api/echoes",
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
                "SELECT destination_type, destination_id FROM echoes WHERE user_id = 5"
            ).fetchone()
        assert row["destination_type"] == "discord"
        assert row["destination_id"] == 1

    def test_add_echo_requires_discord_account(self, multi_client):
        with database.get_db() as db:
            db.execute(
                "INSERT INTO feeds (id, name, url, user_id) VALUES (1, 'f', 'https://example.com/feed', 5)"
            )
        r = multi_client.post(
            "/api/echoes",
            data={"feed_id": "1", "destination_type": "discord", "template": "{{ title }}"},
        )
        assert r.status_code == 400
        assert "discord_account_id required" in r.json()["detail"]

    def test_add_echo_rejects_foreign_discord_account(self, multi_client):
        with database.get_db() as db:
            db.execute(
                "INSERT INTO feeds (id, name, url, user_id) VALUES (1, 'f', 'https://example.com/feed', 5)"
            )
            db.execute(
                "INSERT INTO discord_accounts (name, webhook_url, channel_id, user_id)"
                " VALUES ('A', ?, '', 999)",
                (WEBHOOK_URL,),
            )
        r = multi_client.post(
            "/api/echoes",
            data={
                "feed_id": "1",
                "destination_type": "discord",
                "discord_account_id": "1",
                "template": "{{ title }}",
            },
        )
        assert r.status_code == 404

    def test_history_renders_discord_rows(self, multi_client):
        with mock.patch("app.discord_connect", return_value=CONNECT_INFO):
            multi_client.post(
                "/api/discord-accounts",
                data={"webhook_url": WEBHOOK_URL},
                follow_redirects=False,
            )
        with database.get_db() as db:
            db.execute(
                "INSERT INTO feeds (id, name, url, user_id) VALUES (1, 'f', 'https://example.com/feed', 5)"
            )
            db.execute(
                "INSERT INTO echoes (id, feed_id, destination_type, destination_id, template, user_id)"
                " VALUES (1, 1, 'discord', 1, '{{ title }}', 5)"
            )
            db.execute(
                "INSERT INTO posted_items (echo_id, item_id, item_title, item_url, status)"
                " VALUES (1, 'item-1', 'T', 'https://example.com', 'success')"
            )
        r = multi_client.get("/history")
        assert r.status_code == 200
        assert "Feed Bot" in r.text
