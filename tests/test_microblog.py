"""Tests for the micro.blog (Micropub) destination: client, dispatch, and routes.

All network calls are monkeypatched — no live micro.blog traffic.
"""

import json
import os
import tempfile
from unittest import mock

import pytest
from fastapi.testclient import TestClient

import auth
import database
import microblog
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


def _setup_microblog_echo(db_tmp, echo_overrides=None):
    """Create a micro.blog account, feed, and echo. Returns the echo row."""
    echo_kwargs = {
        "destination_type": "microblog",
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
            "INSERT INTO microblog_accounts (name, uid, token) VALUES (?, ?, ?)",
            ("My Blog", "https://myblog.micro.blog/", "token-123"),
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


# ── Client: list_destinations / fetch_config ───────────────────────────────


def _config_response(payload, status_code=200):
    resp = mock.Mock()
    resp.status_code = status_code
    resp.json.return_value = payload
    resp.headers = {}
    return resp


class TestListDestinations:
    def test_returns_uid_and_name(self):
        payload = {
            "destination": [
                {"uid": "https://myblog.micro.blog/", "name": "My Blog"},
                {"uid": "https://other.micro.blog/", "name": "Other Blog"},
            ]
        }
        with mock.patch.object(microblog.httpx, "Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            client.get.return_value = _config_response(payload)
            blogs = microblog.list_destinations("token-123")

        assert blogs == [
            {"uid": "https://myblog.micro.blog/", "name": "My Blog"},
            {"uid": "https://other.micro.blog/", "name": "Other Blog"},
        ]
        _, kwargs = client.get.call_args
        assert kwargs["params"] == {"q": "config"}
        assert kwargs["headers"]["Authorization"] == "Bearer token-123"

    def test_dedupes_and_skips_malformed_entries(self):
        payload = {
            "destination": [
                {"uid": "https://a.micro.blog/", "name": "A"},
                {"uid": "https://a.micro.blog/", "name": "A again"},
                {"name": "no uid"},
                "not-a-dict",
                {"uid": "   "},
                {"uid": "https://b.micro.blog/"},  # name falls back to uid
            ]
        }
        with mock.patch.object(microblog.httpx, "Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            client.get.return_value = _config_response(payload)
            blogs = microblog.list_destinations("t")

        assert blogs == [
            {"uid": "https://a.micro.blog/", "name": "A"},
            {"uid": "https://b.micro.blog/", "name": "https://b.micro.blog/"},
        ]

    def test_auth_error_on_401(self):
        with mock.patch.object(microblog.httpx, "Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            client.get.return_value = _config_response({"error": "forbidden"}, 403)
            with pytest.raises(microblog.MicroblogAuthError):
                microblog.list_destinations("bad-token")

    def test_generic_error_on_500(self):
        with mock.patch.object(microblog.httpx, "Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            client.get.return_value = _config_response({}, 500)
            with pytest.raises(microblog.MicroblogError) as exc:
                microblog.list_destinations("t")
        assert "500" in str(exc.value)

    def test_error_when_no_destination_key(self):
        with mock.patch.object(microblog.httpx, "Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            client.get.return_value = _config_response({"q": "config"})
            with pytest.raises(microblog.MicroblogError):
                microblog.list_destinations("t")

    def test_network_error_wrapped(self):
        import httpx as _httpx

        with mock.patch.object(microblog.httpx, "Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            client.get.side_effect = _httpx.ConnectError("boom")
            with pytest.raises(microblog.MicroblogError):
                microblog.list_destinations("t")

    def test_non_json_body_is_error(self):
        resp = mock.Mock()
        resp.status_code = 200
        resp.json.side_effect = ValueError("nope")
        with mock.patch.object(microblog.httpx, "Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            client.get.return_value = resp
            with pytest.raises(microblog.MicroblogError):
                microblog.list_destinations("t")


class TestCreatePost:
    def _post(self, **kwargs):
        kwargs.setdefault("token", "token-123")
        kwargs.setdefault("content", "Hello world")
        with mock.patch.object(microblog.httpx, "Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            client.post.return_value = _config_response(
                {}, 201
            )  # json() may be called; harmless
            client.post.return_value.headers = {"Location": "https://myblog.micro.blog/2026/08/hello.html"}
            result = microblog.create_post(**kwargs)
        return client.post.call_args, result

    def test_form_fields_include_h_entry_and_destination(self):
        (args, kwargs), result = self._post(
            destination="https://myblog.micro.blog/",
            photo_url="https://example.com/img.png",
            photo_alt="A photo",
        )
        data = kwargs["data"]
        assert data["h"] == "entry"
        assert data["content"] == "Hello world"
        assert data["mp-destination"] == "https://myblog.micro.blog/"
        assert data["photo"] == "https://example.com/img.png"
        assert data["mp-photo-alt"] == "A photo"
        assert result["location"] == "https://myblog.micro.blog/2026/08/hello.html"

    def test_omits_optional_fields_when_blank(self):
        (args, kwargs), _ = self._post()
        data = kwargs["data"]
        assert "mp-destination" not in data
        assert "photo" not in data
        assert "mp-photo-alt" not in data

    def test_202_accepted(self):
        with mock.patch.object(microblog.httpx, "Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            resp = _config_response({}, 202)
            resp.headers = {"Location": "https://x.micro.blog/1"}
            client.post.return_value = resp
            result = microblog.create_post(token="t", content="hi")
        assert result["location"] == "https://x.micro.blog/1"

    def test_401_raises_auth_error(self):
        with mock.patch.object(microblog.httpx, "Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            client.post.return_value = _config_response({"error": "unauthorized"}, 401)
            with pytest.raises(microblog.MicroblogAuthError):
                microblog.create_post(token="revoked", content="hi")

    def test_500_raises_generic_error(self):
        with mock.patch.object(microblog.httpx, "Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            client.post.return_value = _config_response({"error": "boom"}, 500)
            with pytest.raises(microblog.MicroblogError):
                microblog.create_post(token="t", content="hi")

    def test_500_with_unauthorized_body_is_not_auth_error(self):
        """A server error mentioning 'unauthorized' must stay retryable."""
        with mock.patch.object(microblog.httpx, "Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            client.post.return_value = _config_response(
                {"error": "unauthorized request id 123"}, 500
            )
            with pytest.raises(microblog.MicroblogError) as exc:
                microblog.create_post(token="t", content="hi")
        assert not isinstance(exc.value, microblog.MicroblogAuthError)

    def test_empty_content_rejected_locally(self):
        with pytest.raises(microblog.MicroblogError):
            microblog.create_post(token="t", content="   ")


def test_test_connection_reports_blog_names():
    with mock.patch.object(microblog.httpx, "Client") as client_cls:
        client = client_cls.return_value.__enter__.return_value
        client.get.return_value = _config_response(
            {"destination": [{"uid": "https://myblog.micro.blog/", "name": "My Blog"}]}
        )
        ok, message = microblog.test_connection("token-123")
    assert ok
    assert "My Blog" in message


# ── Scheduler dispatch ──────────────────────────────────────────────────────


class TestMicroblogDispatch:
    def test_happy_path_posts_and_records_success(self, db_tmp, monkeypatch):
        echo = _setup_microblog_echo(db_tmp)
        item = _item()

        captured = {}

        def fake_create_post(**kwargs):
            captured.update(kwargs)
            return {"location": "https://myblog.micro.blog/2026/08/x.html"}

        monkeypatch.setattr(scheduler, "microblog_create_post", fake_create_post)

        assert scheduler.process_echo(echo, item)

        assert captured["token"] == "token-123"
        assert captured["destination"] == "https://myblog.micro.blog/"
        assert "Test Post" in captured["content"]
        assert captured["photo_url"] == ""

        with db_tmp.get_db() as db:
            row = db.execute(
                "SELECT status, post_url FROM posted_items WHERE echo_id = 1"
            ).fetchone()
        assert row["status"] == "success"
        assert row["post_url"] == "https://myblog.micro.blog/2026/08/x.html"

    def test_photo_passed_when_attach_image(self, db_tmp, monkeypatch):
        echo = _setup_microblog_echo(db_tmp, {"attach_image": 1})
        item = _item(image_url="https://example.com/cat.jpg")

        captured = {}

        def fake_create_post(**kwargs):
            captured.update(kwargs)
            return {}

        monkeypatch.setattr(scheduler, "microblog_create_post", fake_create_post)
        monkeypatch.setattr(scheduler.alt_text, "is_enabled", lambda user_id: False)

        assert scheduler.process_echo(echo, item)

        assert captured["photo_url"] == "https://example.com/cat.jpg"
        assert captured["photo_alt"] == ""

    def test_alt_text_generated_when_enabled(self, db_tmp, monkeypatch):
        echo = _setup_microblog_echo(db_tmp, {"attach_image": 1})
        item = _item(image_url="https://example.com/cat.jpg")

        captured = {}

        def fake_create_post(**kwargs):
            captured.update(kwargs)
            return {}

        monkeypatch.setattr(scheduler, "microblog_create_post", fake_create_post)
        monkeypatch.setattr(scheduler.alt_text, "is_enabled", lambda user_id: True)
        monkeypatch.setattr(
            scheduler, "fetch_image", lambda url: (b"img", "image/jpeg")
        )
        monkeypatch.setattr(
            scheduler.alt_text,
            "generate_alt_text",
            lambda data, typ, user_id: "A cat",
        )

        assert scheduler.process_echo(echo, item)

        assert captured["photo_url"] == "https://example.com/cat.jpg"
        assert captured["photo_alt"] == "A cat"

    def test_missing_account_fails_permanently(self, db_tmp, monkeypatch):
        echo = _setup_microblog_echo(db_tmp)
        item = _item()
        with db_tmp.get_db() as db:
            db.execute("DELETE FROM microblog_accounts WHERE id = 1")

        gave_up = scheduler.process_echo(echo, item)

        assert gave_up
        with db_tmp.get_db() as db:
            status = db.execute(
                "SELECT status FROM posted_items WHERE echo_id = 1"
            ).fetchone()["status"]
        assert status == "gave_up"

    def test_auth_error_fails_permanently(self, db_tmp, monkeypatch):
        echo = _setup_microblog_echo(db_tmp)
        item = _item()

        def fake_create_post(**kwargs):
            raise microblog.MicroblogAuthError("rejected")

        monkeypatch.setattr(scheduler, "microblog_create_post", fake_create_post)

        gave_up = scheduler.process_echo(echo, item)

        assert gave_up
        with db_tmp.get_db() as db:
            row = db.execute(
                "SELECT status, error_message FROM posted_items WHERE echo_id = 1"
            ).fetchone()
        assert row["status"] == "gave_up"
        assert "token rejected" in (row["error_message"] or "").lower()

    def test_account_fetch_scoped_to_echo_owner(self, db_tmp, monkeypatch):
        """An echo must not dispatch through another user's micro.blog row."""
        echo = _setup_microblog_echo(db_tmp)
        item = _item()
        with db_tmp.get_db() as db:
            db.execute(
                """INSERT INTO microblog_accounts (id, name, uid, token, user_id)
                   VALUES (99, 'Other User', 'https://x.micro.blog/', 't2', 777)"""
            )
            db.execute(
                "UPDATE echoes SET destination_id = 99 WHERE id = 1"
            )

        # Re-read: real dispatch always works from a freshly loaded echo row.
        with db_tmp.get_db() as db:
            echo = db.execute("SELECT * FROM echoes WHERE id = 1").fetchone()

        sent = []
        monkeypatch.setattr(
            scheduler, "microblog_create_post", lambda **kw: sent.append(kw) or {}
        )

        # _fail_post returns True for "reached gave_up" (handled = cursor
        # unblocked); the assertion that matters is that nothing was sent.
        assert scheduler.process_echo(echo, item)
        assert sent == []
        with db_tmp.get_db() as db:
            status = db.execute(
                "SELECT status FROM posted_items WHERE echo_id = 1"
            ).fetchone()["status"]
        assert status == "gave_up"

    def test_generic_error_is_retryable(self, db_tmp, monkeypatch):
        echo = _setup_microblog_echo(db_tmp)
        item = _item()

        def fake_create_post(**kwargs):
            raise microblog.MicroblogError("HTTP 500")

        monkeypatch.setattr(scheduler, "microblog_create_post", fake_create_post)

        gave_up = scheduler.process_echo(echo, item)

        # Bounded retry: failed, not gave_up.
        assert not gave_up
        with db_tmp.get_db() as db:
            row = db.execute(
                "SELECT status, attempt_count FROM posted_items WHERE echo_id = 1"
            ).fetchone()
        assert row["status"] == "failed"
        assert row["attempt_count"] == 1


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


class TestMicroblogRoutes:
    def test_connect_creates_one_row_per_blog(self, multi_client):
        config = {
            "destination": [
                {"uid": "https://a.micro.blog/", "name": "A"},
                {"uid": "https://b.micro.blog/", "name": "B"},
            ]
        }
        with mock.patch("app.microblog_list_destinations", return_value=[
            {"uid": "https://a.micro.blog/", "name": "A"},
            {"uid": "https://b.micro.blog/", "name": "B"},
        ]):
            r = multi_client.post(
                "/api/microblog-accounts", data={"token": "token-abc"}, follow_redirects=False
            )
        assert r.status_code == 303
        assert "/accounts?status=microblog_connected" in r.headers["location"]
        with database.get_db() as db:
            rows = db.execute(
                "SELECT name, uid, token FROM microblog_accounts WHERE user_id = 5 ORDER BY name"
            ).fetchall()
        assert [row["uid"] for row in rows] == [
            "https://a.micro.blog/",
            "https://b.micro.blog/",
        ]
        assert all(row["token"] == "token-abc" for row in rows)

    def test_reconnect_refreshes_token_upsert(self, multi_client):
        with mock.patch(
            "app.microblog_list_destinations",
            return_value=[{"uid": "https://a.micro.blog/", "name": "A"}],
        ):
            multi_client.post("/api/microblog-accounts", data={"token": "old"}, follow_redirects=False)
            multi_client.post("/api/microblog-accounts", data={"token": "new"}, follow_redirects=False)
        with database.get_db() as db:
            rows = db.execute(
                "SELECT token FROM microblog_accounts WHERE user_id = 5"
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["token"] == "new"

    def test_connect_rejects_bad_token(self, multi_client):
        with mock.patch(
            "app.microblog_list_destinations",
            side_effect=microblog.MicroblogAuthError("rejected"),
        ):
            r = multi_client.post("/api/microblog-accounts", data={"token": "bad"})
        assert r.status_code == 200
        assert "rejected" in r.text
        with database.get_db() as db:
            count = db.execute(
                "SELECT COUNT(*) AS c FROM microblog_accounts"
            ).fetchone()["c"]
        assert count == 0

    def test_connect_blank_token(self, multi_client):
        r = multi_client.post("/api/microblog-accounts", data={"token": "   "})
        assert r.status_code == 200
        assert "Enter a Micro.blog app token" in r.text

    def test_test_endpoint(self, multi_client):
        with mock.patch(
            "app.microblog_list_destinations",
            return_value=[{"uid": "https://a.micro.blog/", "name": "A"}],
        ):
            multi_client.post("/api/microblog-accounts", data={"token": "t"}, follow_redirects=False)
        with mock.patch(
            "app.test_microblog_connection", return_value=(True, "Token OK")
        ):
            r = multi_client.post("/api/microblog-accounts/1/test")
        assert r.status_code == 200
        body = r.json()
        assert body == {"success": True, "message": "Token OK"}

    def test_delete_blocked_by_dependent_echo(self, multi_client):
        with mock.patch(
            "app.microblog_list_destinations",
            return_value=[{"uid": "https://a.micro.blog/", "name": "A"}],
        ):
            multi_client.post("/api/microblog-accounts", data={"token": "t"}, follow_redirects=False)
        with database.get_db() as db:
            db.execute(
                """INSERT INTO feeds (id, name, url, user_id)
                   VALUES (1, 'f', 'https://example.com/feed', 5)"""
            )
            db.execute(
                """INSERT INTO echoes (feed_id, destination_type, destination_id,
                                       template, user_id)
                   VALUES (1, 'microblog', 1, '{{ title }}', 5)"""
            )
        r = multi_client.post("/api/microblog-accounts/1/delete", follow_redirects=False)
        assert r.status_code == 200
        assert "used by echoes" in r.text
        with database.get_db() as db:
            count = db.execute(
                "SELECT COUNT(*) AS c FROM microblog_accounts"
            ).fetchone()["c"]
        assert count == 1

    def test_delete_succeeds_without_echoes(self, multi_client):
        with mock.patch(
            "app.microblog_list_destinations",
            return_value=[{"uid": "https://a.micro.blog/", "name": "A"}],
        ):
            multi_client.post("/api/microblog-accounts", data={"token": "t"}, follow_redirects=False)
        r = multi_client.post("/api/microblog-accounts/1/delete", follow_redirects=False)
        assert r.status_code == 303
        assert "microblog_deleted" in r.headers["location"]
        with database.get_db() as db:
            count = db.execute(
                "SELECT COUNT(*) AS c FROM microblog_accounts"
            ).fetchone()["c"]
        assert count == 0

    def test_accounts_page_lists_microblog_blogs(self, multi_client):
        with mock.patch(
            "app.microblog_list_destinations",
            return_value=[{"uid": "https://a.micro.blog/", "name": "A"}],
        ):
            multi_client.post("/api/microblog-accounts", data={"token": "t"}, follow_redirects=False)
        r = multi_client.get("/accounts")
        assert r.status_code == 200
        assert "Micro.blog Blogs" in r.text
        assert "https://a.micro.blog/" in r.text

    def test_echoes_page_offers_microblog_destination(self, multi_client):
        with mock.patch(
            "app.microblog_list_destinations",
            return_value=[{"uid": "https://a.micro.blog/", "name": "A"}],
        ):
            multi_client.post("/api/microblog-accounts", data={"token": "t"}, follow_redirects=False)
        with database.get_db() as db:
            db.execute(
                """INSERT INTO feeds (id, name, url, user_id)
                   VALUES (1, 'f', 'https://example.com/feed', 5)"""
            )
        r = multi_client.get("/echoes")
        assert r.status_code == 200
        assert 'value="microblog"' in r.text
        assert 'id="microblog-fields"' in r.text

    def test_add_echo_with_microblog_destination(self, multi_client):
        with mock.patch(
            "app.microblog_list_destinations",
            return_value=[{"uid": "https://a.micro.blog/", "name": "A"}],
        ):
            multi_client.post("/api/microblog-accounts", data={"token": "t"}, follow_redirects=False)
        with database.get_db() as db:
            db.execute(
                """INSERT INTO feeds (id, name, url, user_id)
                   VALUES (1, 'f', 'https://example.com/feed', 5)"""
            )
        r = multi_client.post(
            "/api/echoes",
            data={
                "feed_id": "1",
                "destination_type": "microblog",
                "microblog_account_id": "1",
                "template": "{{ title }} {{ link }}",
                "attach_image": "true",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        with database.get_db() as db:
            row = db.execute(
                "SELECT destination_type, destination_id, attach_image FROM echoes WHERE user_id = 5"
            ).fetchone()
        assert row["destination_type"] == "microblog"
        assert row["destination_id"] == 1
        assert row["attach_image"] == 1

    def test_add_echo_rejects_foreign_microblog_account(self, multi_client):
        with database.get_db() as db:
            db.execute(
                """INSERT INTO feeds (id, name, url, user_id)
                   VALUES (1, 'f', 'https://example.com/feed', 5)"""
            )
            db.execute(
                """INSERT INTO microblog_accounts (name, uid, token, user_id)
                   VALUES ('A', 'https://a.micro.blog/', 't', 999)"""
            )
        r = multi_client.post(
            "/api/echoes",
            data={
                "feed_id": "1",
                "destination_type": "microblog",
                "microblog_account_id": "1",
                "template": "{{ title }}",
            },
        )
        assert r.status_code == 404

    def test_dashboard_counts_microblog_accounts(self, multi_client):
        with mock.patch(
            "app.microblog_list_destinations",
            return_value=[
                {"uid": "https://a.micro.blog/", "name": "A"},
                {"uid": "https://b.micro.blog/", "name": "B"},
            ],
        ):
            multi_client.post("/api/microblog-accounts", data={"token": "t"}, follow_redirects=False)
        r = multi_client.get("/")
        assert r.status_code == 200
        assert "2" in r.text  # account count appears on the dashboard
