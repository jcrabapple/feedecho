"""Tests for the generic webhook destination: client, dispatch, and routes.

All network calls are monkeypatched — no live webhook traffic.
"""

import json
import os
import tempfile
from unittest import mock

import pytest
from fastapi.testclient import TestClient

import auth
import database
import scheduler
import security
import settings
import webhook
from app import app

HOOK_URL = "https://hooks.example.com/feedecho"
HOOK_URL_SECRET = "https://hooks.example.com/feedecho?token=SUPERSECRETTOKEN"


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
        "content": "The content.",
        "content_link": "https://example.com/article",
        "author": "Alice",
        "date": "2026-08-30T10:00:00",
        "tags": ["news", "tech"],
        "image_url": "https://example.com/img.png",
        "image_alt": "A picture",
    }
    item.update(overrides)
    return item


def _setup_webhook_echo(db_tmp, echo_overrides=None, headers=None):
    """Create a webhook account, feed, and echo. Returns the echo row."""
    echo_kwargs = {
        "destination_type": "webhook",
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

    headers_json = json.dumps(headers or {"Authorization": "Bearer sekrit"})
    with db_tmp.get_db() as db:
        db.execute(
            "INSERT INTO webhook_accounts (name, url, headers)"
            " VALUES (?, ?, ?)",
            ("My Receiver", HOOK_URL, headers_json),
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


# ── Client: parse_headers ───────────────────────────────────────────────────


class TestParseHeaders:
    def test_empty_returns_empty(self):
        assert webhook.parse_headers("") == {}
        assert webhook.parse_headers("   \n  ") == {}

    def test_single(self):
        assert webhook.parse_headers("Authorization: Bearer xyz") == {
            "Authorization": "Bearer xyz"
        }

    def test_multiple_and_colon_in_value(self):
        out = webhook.parse_headers(
            "Authorization: Bearer xyz\nX-Hook-Url: https://example.com/a:b\nX-One: 1"
        )
        assert out == {
            "Authorization": "Bearer xyz",
            "X-Hook-Url": "https://example.com/a:b",
            "X-One": "1",
        }

    def test_missing_colon_raises_with_line(self):
        with pytest.raises(ValueError, match="line 2"):
            webhook.parse_headers("Authorization: Bearer xyz\nno-colon-here")

    def test_invalid_name_raises(self):
        with pytest.raises(ValueError, match="invalid header name"):
            webhook.parse_headers("Bad Header: value")

    def test_duplicate_name_raises(self):
        with pytest.raises(ValueError, match="repeats Authorization"):
            webhook.parse_headers("Authorization: one\nAuthorization: two")

    def test_too_many_raises(self):
        lines = "\n".join(f"X-H-{i}: v" for i in range(webhook.MAX_HEADER_COUNT + 1))
        with pytest.raises(ValueError, match="Too many headers"):
            webhook.parse_headers(lines)

    def test_crlf_accepted(self):
        assert webhook.parse_headers("X-A: 1\r\nX-B: 2") == {"X-A": "1", "X-B": "2"}

    def test_cr_in_value_rejected(self):
        # A bare \r must not silently become a second header line.
        with pytest.raises(ValueError, match="control character"):
            webhook.parse_headers("X-A: ok\rInjected: v")

    def test_overlong_rejected(self):
        with pytest.raises(ValueError, match="too long"):
            webhook.parse_headers("X-A: " + "v" * (webhook.MAX_HEADERS_TEXT + 1))


# ── Client: normalize_webhook_url ───────────────────────────────────────────


class TestNormalizeWebhookURL:
    def test_https_passes(self):
        assert webhook.normalize_webhook_url(HOOK_URL) == HOOK_URL

    def test_http_passes(self):
        assert webhook.normalize_webhook_url("http://hooks.example.com/x") == "http://hooks.example.com/x"

    def test_whitespace_stripped(self):
        assert webhook.normalize_webhook_url(f"  {HOOK_URL}  ") == HOOK_URL

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            webhook.normalize_webhook_url("")

    def test_non_http_scheme_rejected(self):
        with pytest.raises(ValueError, match="http"):
            webhook.normalize_webhook_url("file:///etc/passwd")

    def test_userinfo_rejected(self):
        with pytest.raises(ValueError, match="credentials"):
            webhook.normalize_webhook_url("https://user:pass@hooks.example.com/x")

    def test_multi_mode_blocks_private_ip(self, monkeypatch):
        monkeypatch.setattr(settings, "MULTI", True)
        with pytest.raises(ValueError, match="Blocked"):
            webhook.normalize_webhook_url("https://127.0.0.1:8443/hook")

    def test_multi_mode_requires_https(self, monkeypatch):
        monkeypatch.setattr(settings, "MULTI", True)
        with pytest.raises(ValueError, match="https"):
            webhook.normalize_webhook_url("http://hooks.example.com/x")

    def test_multi_mode_blocks_lan_ip(self, monkeypatch):
        monkeypatch.setattr(settings, "MULTI", True)
        with pytest.raises(ValueError, match="Blocked"):
            webhook.normalize_webhook_url("https://192.168.1.10/hook")

    def test_single_mode_allows_private_ip(self, monkeypatch):
        monkeypatch.setattr(settings, "MULTI", False)
        assert (
            webhook.normalize_webhook_url("http://127.0.0.1:8080/hook")
            == "http://127.0.0.1:8080/hook"
        )


# ── Client: build_payload / load_headers ────────────────────────────────────


class TestBuildPayload:
    def test_full_shape(self):
        item = _item()
        payload = webhook.build_payload(item, "TEXT", feed_name="My Feed")
        assert payload == {
            "text": "TEXT",
            "id": "item-1",
            "title": "Test Post",
            "link": "https://example.com/post/1",
            "summary": "A summary of the post.",
            "content": "The content.",
            "content_link": "https://example.com/article",
            "author": "Alice",
            "published": "2026-08-30T10:00:00",
            "tags": ["news", "tech"],
            "image_url": "https://example.com/img.png",
            "image_alt": "A picture",
            "feed_name": "My Feed",
        }

    def test_missing_fields_become_empty(self):
        payload = webhook.build_payload({"id": "x"}, "TEXT")
        assert payload["title"] == ""
        assert payload["tags"] == []
        assert payload["feed_name"] == ""

    def test_wrong_typed_tags_coerced(self):
        assert webhook.build_payload({"id": "x", "tags": "news"}, "T")["tags"] == []
        assert webhook.build_payload({"id": "x", "tags": None}, "T")["tags"] == []
        assert webhook.build_payload({"id": "x", "date": None}, "T")["published"] == ""


class TestLoadHeaders:
    def test_roundtrip(self):
        headers = {"Authorization": "Bearer xyz"}
        assert webhook.load_headers(webhook.dump_headers(headers)) == headers

    def test_garbage_is_empty(self):
        assert webhook.load_headers("not json") == {}
        assert webhook.load_headers(None) == {}

    def test_invalid_name_dropped(self):
        assert webhook.load_headers('{"Bad Name": "v", "X-Ok": "1"}') == {"X-Ok": "1"}

    def test_control_char_value_dropped(self):
        assert webhook.load_headers('{"X-A": "a\\rb"}') == {}


# ── Client: send_webhook ────────────────────────────────────────────────────


class TestSendWebhook:
    @pytest.fixture(autouse=True)
    def _single_mode(self, monkeypatch):
        monkeypatch.setattr(settings, "MULTI", False)

    def test_posts_json_with_headers(self):
        with mock.patch.object(webhook.httpx, "Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            client.post.return_value = _resp({}, 204)
            webhook.send_webhook(
                HOOK_URL, {"Authorization": "Bearer xyz"}, {"text": "hi"}
            )
        _, kwargs = client.post.call_args
        assert kwargs["json"] == {"text": "hi"}
        assert kwargs["headers"] == {"Authorization": "Bearer xyz"}
        # Redirects are disabled on the client itself.
        assert client_cls.call_args.kwargs["follow_redirects"] is False

    def test_2xx_success(self):
        with mock.patch.object(webhook.httpx, "Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            client.post.return_value = _resp({}, 200)
            webhook.send_webhook(HOOK_URL, {}, {"text": "hi"})  # no raise

    def test_3xx_is_error(self):
        with mock.patch.object(webhook.httpx, "Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            client.post.return_value = _resp({}, 302)
            with pytest.raises(webhook.WebhookError):
                webhook.send_webhook(HOOK_URL, {}, {"text": "hi"})

    def test_401_is_auth_error(self):
        with mock.patch.object(webhook.httpx, "Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            client.post.return_value = _resp({}, 401)
            with pytest.raises(webhook.WebhookAuthError):
                webhook.send_webhook(HOOK_URL, {}, {"text": "hi"})

    def test_403_is_auth_error(self):
        with mock.patch.object(webhook.httpx, "Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            client.post.return_value = _resp({}, 403)
            with pytest.raises(webhook.WebhookAuthError):
                webhook.send_webhook(HOOK_URL, {}, {"text": "hi"})

    def test_404_is_not_found(self):
        with mock.patch.object(webhook.httpx, "Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            client.post.return_value = _resp({}, 404)
            with pytest.raises(webhook.WebhookNotFoundError):
                webhook.send_webhook(HOOK_URL, {}, {"text": "hi"})

    def test_400_is_rejected(self):
        with mock.patch.object(webhook.httpx, "Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            client.post.return_value = _resp({}, 400)
            with pytest.raises(webhook.WebhookRejectedError):
                webhook.send_webhook(HOOK_URL, {}, {"text": "hi"})

    def test_other_4xx_is_permanent(self):
        with mock.patch.object(webhook.httpx, "Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            client.post.return_value = _resp({}, 405)
            with pytest.raises(webhook.WebhookRejectedError):
                webhook.send_webhook(HOOK_URL, {}, {"text": "hi"})

    def test_429_carries_retry_after(self):
        with mock.patch.object(webhook.httpx, "Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            client.post.return_value = _resp({}, 429, headers={"Retry-After": "45"})
            with pytest.raises(webhook.WebhookRateLimitError) as exc_info:
                webhook.send_webhook(HOOK_URL, {}, {"text": "hi"})
        assert exc_info.value.retry_after == 45.0

    def test_429_malformed_retry_after_is_none(self):
        with mock.patch.object(webhook.httpx, "Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            client.post.return_value = _resp({}, 429, headers={"Retry-After": "never"})
            with pytest.raises(webhook.WebhookRateLimitError) as exc_info:
                webhook.send_webhook(HOOK_URL, {}, {"text": "hi"})
        assert exc_info.value.retry_after is None

    def test_network_error_is_scrubbed(self):
        import httpx as _httpx

        with mock.patch.object(webhook.httpx, "Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            client.post.side_effect = _httpx.ConnectError("boom", request=mock.Mock())
            with pytest.raises(webhook.WebhookError) as exc_info:
                webhook.send_webhook(HOOK_URL, {}, {"text": "hi"})
        assert "ConnectError" in str(exc_info.value)
        assert HOOK_URL not in str(exc_info.value)

    def test_malformed_stored_url_is_rejected(self):
        with pytest.raises(webhook.WebhookRejectedError):
            webhook.send_webhook("ftp://nope/", {}, {"text": "hi"})

    def test_multi_mode_uses_pinned_client(self, monkeypatch):
        monkeypatch.setattr(settings, "MULTI", True)
        order = []

        def guard(u):
            order.append("guard")
            return u

        def fake_ssrf(urls, timeout):
            order.append("ssrf")
            assert urls == [HOOK_URL]
            fake = mock.Mock()
            fake.post.return_value = _resp({}, 204)
            return fake, mock.Mock()

        monkeypatch.setattr(webhook, "validate_outbound_url", guard)
        monkeypatch.setattr(webhook, "ssrf_client", fake_ssrf)
        webhook.send_webhook(HOOK_URL, {}, {"text": "hi"})
        # Validation must precede pinning: the pinned transport is only
        # meaningful for a URL the guard already approved.
        assert order == ["guard", "ssrf"]


class TestTestConnection:
    def test_success(self, monkeypatch):
        monkeypatch.setattr(settings, "MULTI", False)
        with mock.patch.object(webhook, "send_webhook") as send:
            ok, msg = webhook.test_connection(HOOK_URL, {})
        assert ok is True
        assert "accepted the test delivery" in msg

    def test_failure_maps_to_false(self, monkeypatch):
        monkeypatch.setattr(settings, "MULTI", False)
        with mock.patch.object(
            webhook, "send_webhook", side_effect=webhook.WebhookAuthError("bad")
        ):
            ok, msg = webhook.test_connection(HOOK_URL, {})
        assert ok is False
        assert "bad" in msg


# ── Scheduler dispatch ──────────────────────────────────────────────────────


class TestSendWebhookDispatch:
    def test_success(self, db_tmp, monkeypatch):
        echo = _setup_webhook_echo(db_tmp)
        item = _item()

        sent = []
        monkeypatch.setattr(
            scheduler,
            "webhook_send_webhook",
            lambda *a, **kw: sent.append(a) or None,
        )

        ok = scheduler.process_echo(echo, item, feed_name="f")
        assert ok is True
        assert len(sent) == 1
        url, headers, payload = sent[0]
        assert url == HOOK_URL
        assert headers == {"Authorization": "Bearer sekrit"}
        assert payload["text"] == "Test Post https://example.com/post/1"
        assert payload["feed_name"] == "f"
        assert payload["title"] == "Test Post"
        with db_tmp.get_db() as db:
            row = db.execute(
                "SELECT status, post_url FROM posted_items WHERE echo_id = 1"
            ).fetchone()
        assert row["status"] == "success"
        assert row["post_url"] == ""

    def test_auth_error_is_permanent(self, db_tmp, monkeypatch):
        echo = _setup_webhook_echo(db_tmp)
        item = _item()

        def fail(*a, **kw):
            raise webhook.WebhookAuthError("bad credentials")

        monkeypatch.setattr(scheduler, "webhook_send_webhook", fail)
        gave_up = scheduler.process_echo(echo, item)
        assert gave_up is True
        with db_tmp.get_db() as db:
            row = db.execute(
                "SELECT status FROM posted_items WHERE echo_id = 1"
            ).fetchone()
        assert row["status"] == "gave_up"

    def test_rejected_payload_is_permanent(self, db_tmp, monkeypatch):
        echo = _setup_webhook_echo(db_tmp)
        item = _item()

        def fail(*a, **kw):
            raise webhook.WebhookRejectedError("bad shape")

        monkeypatch.setattr(scheduler, "webhook_send_webhook", fail)
        assert scheduler.process_echo(echo, item) is True

    def test_generic_error_is_retryable(self, db_tmp, monkeypatch):
        echo = _setup_webhook_echo(db_tmp)
        item = _item()

        def fail(*a, **kw):
            raise webhook.WebhookError("HTTP 500")

        monkeypatch.setattr(scheduler, "webhook_send_webhook", fail)
        gave_up = scheduler.process_echo(echo, item)
        assert not gave_up
        with db_tmp.get_db() as db:
            row = db.execute(
                "SELECT status, attempt_count FROM posted_items WHERE echo_id = 1"
            ).fetchone()
        assert row["status"] == "failed"
        assert row["attempt_count"] == 1

    def test_rate_limit_is_retryable(self, db_tmp, monkeypatch):
        echo = _setup_webhook_echo(db_tmp)
        item = _item()

        def fail(*a, **kw):
            raise webhook.WebhookRateLimitError(retry_after=30)

        monkeypatch.setattr(scheduler, "webhook_send_webhook", fail)
        assert not scheduler.process_echo(echo, item)

    def test_error_text_never_contains_header_value(self, db_tmp, monkeypatch):
        echo = _setup_webhook_echo(db_tmp)
        item = _item()

        def fail(*a, **kw):
            # Simulates a transport failure; the stored message must never
            # carry the account's header values (they are credentials).
            raise webhook.WebhookError("Webhook delivery failed (HTTP 500)")

        monkeypatch.setattr(scheduler, "webhook_send_webhook", fail)
        scheduler.process_echo(echo, item)
        with db_tmp.get_db() as db:
            row = db.execute(
                "SELECT error_message FROM posted_items WHERE echo_id = 1"
            ).fetchone()
        assert "sekrit" not in row["error_message"]
        assert "Authorization" not in row["error_message"]

    def test_missing_account_is_permanent(self, db_tmp, monkeypatch):
        echo = _setup_webhook_echo(db_tmp, echo_overrides={"destination_id": 999})
        item = _item()

        sent = []
        monkeypatch.setattr(
            scheduler,
            "webhook_send_webhook",
            lambda *a, **kw: sent.append(a) or None,
        )
        gave_up = scheduler.process_echo(echo, item)
        assert gave_up is True
        assert sent == []


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


class TestWebhookRoutes:
    @pytest.fixture(autouse=True)
    def _bypass_dns(self, monkeypatch):
        # The route-level tests POST example.com URLs; the real SSRF guard
        # would resolve them (live DNS in the suite). The private-IP test
        # restores the real guard, which blocks IP literals without any DNS.
        from feed_parser import validate_outbound_url as real_guard

        monkeypatch.setattr(webhook, "validate_outbound_url", lambda u: u)
        self._real_guard = real_guard

    def test_connect_creates_row(self, multi_client):
        r = multi_client.post(
            "/api/webhook-accounts",
            data={
                "url": HOOK_URL,
                "name": "Receiver",
                "headers_text": "Authorization: Bearer sekrit\nX-Extra: 1",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert "webhook_connected" in r.headers["location"]
        with database.get_db() as db:
            row = db.execute(
                "SELECT * FROM webhook_accounts WHERE user_id = 5"
            ).fetchone()
        assert row["url"] == HOOK_URL
        assert row["name"] == "Receiver"
        stored = webhook.load_headers(row["headers"])
        assert stored == {"Authorization": "Bearer sekrit", "X-Extra": "1"}

    def test_connect_blank_url_rejected(self, multi_client):
        r = multi_client.post("/api/webhook-accounts", data={"url": "  "})
        assert r.status_code == 200
        assert "Enter the webhook URL" in r.text

    def test_connect_bad_headers_rejected(self, multi_client):
        r = multi_client.post(
            "/api/webhook-accounts",
            data={"url": HOOK_URL, "headers_text": "no colon here"},
        )
        assert r.status_code == 200
        assert "missing a colon" in r.text
        with database.get_db() as db:
            count = db.execute(
                "SELECT COUNT(*) AS c FROM webhook_accounts"
            ).fetchone()["c"]
        assert count == 0

    def test_connect_private_ip_blocked_in_multi(self, multi_client, monkeypatch):
        monkeypatch.setattr(webhook, "validate_outbound_url", self._real_guard)
        r = multi_client.post(
            "/api/webhook-accounts", data={"url": "https://127.0.0.1:8443/hook"}
        )
        assert r.status_code == 200
        assert "Blocked" in r.text
        with database.get_db() as db:
            count = db.execute(
                "SELECT COUNT(*) AS c FROM webhook_accounts"
            ).fetchone()["c"]
        assert count == 0

    def test_connect_calls_ssrf_guard(self, multi_client, monkeypatch):
        calls = []
        monkeypatch.setattr(
            webhook, "validate_outbound_url", lambda u: calls.append(u) or u
        )
        multi_client.post(
            "/api/webhook-accounts",
            data={"url": HOOK_URL},
            follow_redirects=False,
        )
        assert HOOK_URL in calls

    def test_reconnect_updates_headers(self, multi_client):
        multi_client.post(
            "/api/webhook-accounts",
            data={"url": HOOK_URL, "name": "Old", "headers_text": "Authorization: one"},
            follow_redirects=False,
        )
        multi_client.post(
            "/api/webhook-accounts",
            data={"url": HOOK_URL, "name": "New", "headers_text": "Authorization: two"},
            follow_redirects=False,
        )
        with database.get_db() as db:
            rows = db.execute(
                "SELECT name, headers FROM webhook_accounts WHERE user_id = 5"
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["name"] == "New"
        assert webhook.load_headers(rows[0]["headers"]) == {"Authorization": "two"}

    def test_test_endpoint(self, multi_client):
        multi_client.post(
            "/api/webhook-accounts",
            data={"url": HOOK_URL},
            follow_redirects=False,
        )
        with mock.patch(
            "app.test_webhook_connection", return_value=(True, "Webhook accepted the test delivery")
        ):
            r = multi_client.post("/api/webhook-accounts/1/test")
        assert r.status_code == 200
        assert r.json() == {"success": True, "message": "Webhook accepted the test delivery"}

    def test_delete_blocked_by_dependent_echo(self, multi_client):
        multi_client.post(
            "/api/webhook-accounts",
            data={"url": HOOK_URL},
            follow_redirects=False,
        )
        with database.get_db() as db:
            db.execute(
                "INSERT INTO feeds (id, name, url, user_id) VALUES (1, 'f', 'https://example.com/feed', 5)"
            )
            db.execute(
                "INSERT INTO echoes (feed_id, destination_type, destination_id, template, user_id)"
                " VALUES (1, 'webhook', 1, '{{ title }}', 5)"
            )
        r = multi_client.post("/api/webhook-accounts/1/delete", follow_redirects=False)
        assert r.status_code == 200
        assert "used by echoes" in r.text
        with database.get_db() as db:
            count = db.execute(
                "SELECT COUNT(*) AS c FROM webhook_accounts"
            ).fetchone()["c"]
        assert count == 1

    def test_delete_succeeds_without_echoes(self, multi_client):
        multi_client.post(
            "/api/webhook-accounts",
            data={"url": HOOK_URL},
            follow_redirects=False,
        )
        r = multi_client.post("/api/webhook-accounts/1/delete", follow_redirects=False)
        assert r.status_code == 303
        assert "webhook_deleted" in r.headers["location"]

    def test_accounts_page_masks_credentials(self, multi_client):
        multi_client.post(
            "/api/webhook-accounts",
            data={
                "url": HOOK_URL_SECRET,
                "name": "Secret Hook",
                "headers_text": "Authorization: Bearer SUPERSECRET",
            },
            follow_redirects=False,
        )
        # Slack-style URL: the token lives in the PATH.
        multi_client.post(
            "/api/webhook-accounts",
            data={
                "url": "https://hooks.slack.com/services/T000/B000/XXXXPATHTOKEN",
                "name": "Slack Hook",
            },
            follow_redirects=False,
        )
        r = multi_client.get("/accounts")
        assert r.status_code == 200
        assert "Webhooks" in r.text
        assert "Secret Hook" in r.text
        assert "Slack Hook" in r.text
        assert "Authorization" in r.text  # header NAME is fine
        # Credentials must never render: header value, query token, path token.
        assert "SUPERSECRET" not in r.text
        assert "SUPERSECRETTOKEN" not in r.text
        assert "XXXXPATHTOKEN" not in r.text
        # Only the origin renders, not the path.
        assert "https://hooks.slack.com" in r.text
        assert "services/T000/B000" not in r.text

    def test_echoes_page_offers_webhook_destination(self, multi_client):
        multi_client.post(
            "/api/webhook-accounts",
            data={"url": HOOK_URL},
            follow_redirects=False,
        )
        with database.get_db() as db:
            db.execute(
                "INSERT INTO feeds (id, name, url, user_id) VALUES (1, 'f', 'https://example.com/feed', 5)"
            )
        r = multi_client.get("/echoes")
        assert r.status_code == 200
        assert 'value="webhook"' in r.text
        assert 'id="webhook-fields"' in r.text

    def test_add_echo_with_webhook_destination(self, multi_client):
        multi_client.post(
            "/api/webhook-accounts",
            data={"url": HOOK_URL},
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
                "destination_type": "webhook",
                "webhook_account_id": "1",
                "template": "{{ title }} {{ link }}",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        with database.get_db() as db:
            row = db.execute(
                "SELECT destination_type, destination_id FROM echoes WHERE user_id = 5"
            ).fetchone()
        assert row["destination_type"] == "webhook"
        assert row["destination_id"] == 1

    def test_add_echo_requires_webhook_account(self, multi_client):
        with database.get_db() as db:
            db.execute(
                "INSERT INTO feeds (id, name, url, user_id) VALUES (1, 'f', 'https://example.com/feed', 5)"
            )
        r = multi_client.post(
            "/api/echoes",
            data={"feed_id": "1", "destination_type": "webhook", "template": "{{ title }}"},
        )
        assert r.status_code == 400
        assert "webhook_account_id required" in r.json()["detail"]

    def test_add_echo_rejects_foreign_webhook_account(self, multi_client):
        with database.get_db() as db:
            db.execute(
                "INSERT INTO feeds (id, name, url, user_id) VALUES (1, 'f', 'https://example.com/feed', 5)"
            )
            db.execute(
                "INSERT INTO webhook_accounts (name, url, headers, user_id)"
                " VALUES ('A', ?, '{}', 999)",
                (HOOK_URL,),
            )
        r = multi_client.post(
            "/api/echoes",
            data={
                "feed_id": "1",
                "destination_type": "webhook",
                "webhook_account_id": "1",
                "template": "{{ title }}",
            },
        )
        assert r.status_code == 404
        with database.get_db() as db:
            count = db.execute(
                "SELECT COUNT(*) AS c FROM echoes WHERE user_id = 5"
            ).fetchone()["c"]
        assert count == 0

    def test_cross_tenant_upsert_is_isolated(self, multi_client, monkeypatch, db_tmp):
        multi_client.post(
            "/api/webhook-accounts",
            data={"url": HOOK_URL, "name": "A", "headers_text": "Authorization: a-secret"},
            follow_redirects=False,
        )
        # A second tenant connecting the SAME URL must create their own row,
        # never overwrite the first tenant's.
        with database.get_db() as db:
            db.execute(
                "INSERT INTO users (id, email, password_hash, email_verified)"
                " VALUES (6, 'v@example.com', '', 1)"
            )
        client_b = TestClient(app)
        client_b.cookies.set("feedecho_session", security.sign_session(6, "v@example.com"))
        r = client_b.post(
            "/api/webhook-accounts",
            data={"url": HOOK_URL, "name": "B", "headers_text": "Authorization: b-secret"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        with database.get_db() as db:
            rows = db.execute(
                "SELECT user_id, name, headers FROM webhook_accounts ORDER BY user_id"
            ).fetchall()
        assert len(rows) == 2
        by_user = {row["user_id"]: row for row in rows}
        assert by_user[5]["name"] == "A"
        assert webhook.load_headers(by_user[5]["headers"]) == {"Authorization": "a-secret"}
        assert by_user[6]["name"] == "B"
        assert webhook.load_headers(by_user[6]["headers"]) == {"Authorization": "b-secret"}

    def test_history_masks_query_token(self, multi_client):
        multi_client.post(
            "/api/webhook-accounts",
            data={"url": HOOK_URL_SECRET, "name": "Secret Hook"},
            follow_redirects=False,
        )
        with database.get_db() as db:
            db.execute(
                "INSERT INTO feeds (id, name, url, user_id) VALUES (1, 'f', 'https://example.com/feed', 5)"
            )
            db.execute(
                "INSERT INTO echoes (id, feed_id, destination_type, destination_id, template, user_id)"
                " VALUES (1, 1, 'webhook', 1, '{{ title }}', 5)"
            )
            db.execute(
                "INSERT INTO posted_items (echo_id, item_id, item_title, item_url, status)"
                " VALUES (1, 'item-1', 'T', 'https://example.com', 'success')"
            )
        r = multi_client.get("/history")
        assert r.status_code == 200
        assert "Secret Hook" in r.text
        assert "SUPERSECRETTOKEN" not in r.text
