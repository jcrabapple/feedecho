"""Tests for AI alt text generation feature."""

import os
import tempfile
import base64

import pytest


@pytest.fixture()
def db_tmp(monkeypatch):
    """Point the DB layer at a fresh temp file per test."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)

    import database

    monkeypatch.setattr(database, "DB_PATH", database.Path(path))
    database.init_db()

    import scheduler

    monkeypatch.setattr(scheduler, "get_db", database.get_db)

    yield database

    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except OSError:
            pass


def _set_alt_text_settings(db_tmp, enabled=True, base_url="https://api.openai.com/v1",
                           model="gpt-4o-mini", api_key="sk-test-key"):
    """Write vision API settings to the database."""
    with db_tmp.get_db() as db:
        db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("alt_text_ai_enabled", "1" if enabled else "0"),
        )
        db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("alt_text_ai_base_url", base_url),
        )
        db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("alt_text_ai_model", model),
        )
        db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("alt_text_ai_api_key", api_key),
        )


# ── Alt Text Module Tests ────────────────────────────────────────────────────


class TestIsEnabled:
    def test_enabled_when_configured(self, db_tmp):
        _set_alt_text_settings(db_tmp)
        import alt_text
        assert alt_text.is_enabled() is True

    def test_disabled_when_not_enabled(self, db_tmp):
        _set_alt_text_settings(db_tmp, enabled=False)
        import alt_text
        assert alt_text.is_enabled() is False

    def test_disabled_when_missing_base_url(self, db_tmp):
        _set_alt_text_settings(db_tmp, base_url="")
        import alt_text
        assert alt_text.is_enabled() is False

    def test_disabled_when_missing_model(self, db_tmp):
        _set_alt_text_settings(db_tmp, model="")
        import alt_text
        assert alt_text.is_enabled() is False

    def test_disabled_when_missing_api_key(self, db_tmp):
        _set_alt_text_settings(db_tmp, api_key="")
        import alt_text
        assert alt_text.is_enabled() is False

    def test_disabled_when_nothing_configured(self, db_tmp):
        import alt_text
        assert alt_text.is_enabled() is False


class TestGenerateAltText:
    def test_returns_empty_when_disabled(self, db_tmp, monkeypatch):
        _set_alt_text_settings(db_tmp, enabled=False)
        import alt_text

        # Even with a working API, disabled means empty string
        result = alt_text.generate_alt_text(b"fake-image", "image/jpeg")
        assert result == ""

    def test_returns_empty_when_unconfigured(self, db_tmp, monkeypatch):
        import alt_text

        # No settings at all
        result = alt_text.generate_alt_text(b"fake-image", "image/jpeg")
        assert result == ""

    def test_returns_description_on_success(self, db_tmp, monkeypatch):
        _set_alt_text_settings(db_tmp)
        import alt_text

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "choices": [
                        {"message": {"content": "A red sports car on a mountain road."}}
                    ]
                }

        class FakeClient:
            def __init__(self, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, headers=None, json=None):
                return FakeResponse()

        monkeypatch.setattr(alt_text.httpx, "Client", FakeClient)
        result = alt_text.generate_alt_text(b"fake-image", "image/jpeg")
        assert result == "A red sports car on a mountain road."

    def test_strips_whitespace_from_response(self, db_tmp, monkeypatch):
        _set_alt_text_settings(db_tmp)
        import alt_text

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "choices": [
                        {"message": {"content": "  Padded description  "}}
                    ]
                }

        class FakeClient:
            def __init__(self, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, headers=None, json=None):
                return FakeResponse()

        monkeypatch.setattr(alt_text.httpx, "Client", FakeClient)
        result = alt_text.generate_alt_text(b"fake-image", "image/jpeg")
        assert result == "Padded description"

    def test_returns_empty_on_http_error(self, db_tmp, monkeypatch):
        _set_alt_text_settings(db_tmp)
        import alt_text

        class FakeClient:
            def __init__(self, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, headers=None, json=None):
                raise alt_text.httpx.HTTPStatusError(
                    "500 Server Error", request=None, response=None
                )

        monkeypatch.setattr(alt_text.httpx, "Client", FakeClient)
        # Should retry and ultimately return empty string, not raise
        result = alt_text.generate_alt_text(b"fake-image", "image/jpeg")
        assert result == ""

    def test_returns_empty_on_network_error(self, db_tmp, monkeypatch):
        _set_alt_text_settings(db_tmp)
        import alt_text

        class FakeClient:
            def __init__(self, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, headers=None, json=None):
                raise alt_text.httpx.RequestError("Network down")

        monkeypatch.setattr(alt_text.httpx, "Client", FakeClient)
        result = alt_text.generate_alt_text(b"fake-image", "image/jpeg")
        assert result == ""

    def test_returns_empty_on_missing_content(self, db_tmp, monkeypatch):
        _set_alt_text_settings(db_tmp)
        import alt_text

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {}}]}

        class FakeClient:
            def __init__(self, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, headers=None, json=None):
                return FakeResponse()

        monkeypatch.setattr(alt_text.httpx, "Client", FakeClient)
        result = alt_text.generate_alt_text(b"fake-image", "image/jpeg")
        assert result == ""

    def test_returns_empty_on_malformed_json(self, db_tmp, monkeypatch):
        _set_alt_text_settings(db_tmp)
        import alt_text

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                raise ValueError("Not JSON")

        class FakeClient:
            def __init__(self, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, headers=None, json=None):
                return FakeResponse()

        monkeypatch.setattr(alt_text.httpx, "Client", FakeClient)
        result = alt_text.generate_alt_text(b"fake-image", "image/jpeg")
        assert result == ""

    def test_sends_base64_image_in_request(self, db_tmp, monkeypatch):
        _set_alt_text_settings(db_tmp)
        import alt_text

        captured = {}

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": "desc"}}]}

        class FakeClient:
            def __init__(self, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, headers=None, json=None):
                captured["url"] = url
                captured["headers"] = headers
                captured["body"] = json
                return FakeResponse()

        monkeypatch.setattr(alt_text.httpx, "Client", FakeClient)
        alt_text.generate_alt_text(b"fake-image", "image/png")

        assert captured["url"] == "https://api.openai.com/v1/chat/completions"
        assert captured["headers"]["Authorization"] == "Bearer sk-test-key"
        assert captured["body"]["model"] == "gpt-4o-mini"
        # Verify image is sent as base64 data URL
        user_msg = captured["body"]["messages"][1]["content"]
        image_part = next(p for p in user_msg if p["type"] == "image_url")
        assert image_part["image_url"]["url"].startswith("data:image/png;base64,")
        # Verify the base64 decodes to our input bytes
        b64_data = image_part["image_url"]["url"].split(",", 1)[1]
        assert base64.b64decode(b64_data) == b"fake-image"

    def test_uses_reasoning_content_fallback(self, db_tmp, monkeypatch):
        """Some models return content in reasoning_content instead of content."""
        _set_alt_text_settings(db_tmp)
        import alt_text

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "choices": [
                        {"message": {"reasoning_content": "A blue sky over the ocean."}}
                    ]
                }

        class FakeClient:
            def __init__(self, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, headers=None, json=None):
                return FakeResponse()

        monkeypatch.setattr(alt_text.httpx, "Client", FakeClient)
        result = alt_text.generate_alt_text(b"fake-image", "image/jpeg")
        assert result == "A blue sky over the ocean."


# ── Scheduler Integration Tests ──────────────────────────────────────────────


def _item(**overrides):
    item = {
        "id": "item-1",
        "title": "Test Post",
        "link": "https://example.com/post/1",
        "summary": "A summary.",
        "image_url": "",
    }
    item.update(overrides)
    return item


def _setup_echo(db_tmp, echo_overrides=None):
    echo_kwargs = {
        "destination_type": "mastodon",
        "destination_id": 1,
        "template": "{{ title }}",
        "visibility": "public",
        "filter_keywords": "",
        "filter_mode": "exclude",
        "content_warning": "",
        "attach_image": 1,
        "enabled": 1,
    }
    if echo_overrides:
        echo_kwargs.update(echo_overrides)

    with db_tmp.get_db() as db:
        db.execute(
            "INSERT INTO accounts (name, username, instance, access_token) "
            "VALUES (?, ?, ?, ?)",
            ("main", "user", "https://mastodon.social", "tok"),
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
        return db.execute("SELECT * FROM echoes WHERE id = 1").fetchone()


class TestSchedulerAltTextIntegration:
    def test_alt_text_passed_to_upload_when_enabled(self, db_tmp, monkeypatch):
        _set_alt_text_settings(db_tmp)
        import scheduler
        import alt_text

        sent = []
        monkeypatch.setattr(
            scheduler, "post_status", lambda **kw: sent.append(kw) or {"id": "1"}
        )
        monkeypatch.setattr(
            scheduler, "fetch_image", lambda url: (b"img-bytes", "image/jpeg")
        )
        monkeypatch.setattr(alt_text, "generate_alt_text", lambda b, c, user_id=1: "A scenic mountain landscape.")
        upload_calls = []
        monkeypatch.setattr(
            scheduler, "upload_media", lambda **kw: upload_calls.append(kw) or {"id": "m1"}
        )

        echo = _setup_echo(db_tmp)
        scheduler.process_echo(echo, _item(image_url="https://example.com/pic.jpg"))

        assert len(upload_calls) == 1
        assert upload_calls[0]["description"] == "A scenic mountain landscape."
        assert sent[0]["media_ids"] == ["m1"]

    def test_no_alt_text_call_when_disabled(self, db_tmp, monkeypatch):
        """When AI alt text is not configured, generate_alt_text must not be called."""
        # Do NOT call _set_alt_text_settings — nothing configured
        import scheduler
        import alt_text

        sent = []
        monkeypatch.setattr(
            scheduler, "post_status", lambda **kw: sent.append(kw) or {"id": "1"}
        )
        monkeypatch.setattr(
            scheduler, "fetch_image", lambda url: (b"img-bytes", "image/jpeg")
        )
        alt_calls = []
        monkeypatch.setattr(
            alt_text, "generate_alt_text", lambda b, c, user_id=1: alt_calls.append((b, c)) or "should not be called"
        )
        monkeypatch.setattr(
            scheduler, "upload_media", lambda **kw: {"id": "m1"}
        )

        echo = _setup_echo(db_tmp)
        scheduler.process_echo(echo, _item(image_url="https://example.com/pic.jpg"))

        assert len(alt_calls) == 0, "alt text generation must not run when disabled"
        # Image should still be uploaded, just without description
        assert sent[0]["media_ids"] == ["m1"]

    def test_alt_text_failure_does_not_block_upload(self, db_tmp, monkeypatch):
        """If generate_alt_text raises, the image should still upload without description."""
        _set_alt_text_settings(db_tmp)
        import scheduler
        import alt_text

        sent = []
        monkeypatch.setattr(
            scheduler, "post_status", lambda **kw: sent.append(kw) or {"id": "1"}
        )
        monkeypatch.setattr(
            scheduler, "fetch_image", lambda url: (b"img-bytes", "image/jpeg")
        )
        monkeypatch.setattr(
            alt_text, "generate_alt_text", lambda b, c, user_id=1: (_ for _ in ()).throw(RuntimeError("API down"))
        )
        upload_calls = []
        monkeypatch.setattr(
            scheduler, "upload_media", lambda **kw: upload_calls.append(kw) or {"id": "m1"}
        )

        echo = _setup_echo(db_tmp)
        scheduler.process_echo(echo, _item(image_url="https://example.com/pic.jpg"))

        assert len(upload_calls) == 1
        assert upload_calls[0]["description"] == ""
        assert sent[0]["media_ids"] == ["m1"]

    def test_empty_alt_text_does_not_block_upload(self, db_tmp, monkeypatch):
        """If generate_alt_text returns empty string, upload proceeds without description."""
        _set_alt_text_settings(db_tmp)
        import scheduler
        import alt_text

        monkeypatch.setattr(
            scheduler, "post_status", lambda **kw: {"id": "1"}
        )
        monkeypatch.setattr(
            scheduler, "fetch_image", lambda url: (b"img-bytes", "image/jpeg")
        )
        monkeypatch.setattr(alt_text, "generate_alt_text", lambda b, c, user_id=1: "")
        upload_calls = []
        monkeypatch.setattr(
            scheduler, "upload_media", lambda **kw: upload_calls.append(kw) or {"id": "m1"}
        )

        echo = _setup_echo(db_tmp)
        scheduler.process_echo(echo, _item(image_url="https://example.com/pic.jpg"))

        assert len(upload_calls) == 1
        assert upload_calls[0]["description"] == ""
