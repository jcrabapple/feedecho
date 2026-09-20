"""Telegram destination: client, dispatch wiring, routes (#42)."""

import pytest


# ── telegram client ──────────────────────────────────────────────────────────

class TestNormalizeBotToken:
    def test_valid_token(self):
        from telegram import normalize_bot_token
        assert normalize_bot_token("  123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx  ") == \
            "123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

    def test_empty_raises(self):
        from telegram import normalize_bot_token
        with pytest.raises(ValueError):
            normalize_bot_token("")

    def test_garbage_rejected(self):
        from telegram import normalize_bot_token
        with pytest.raises(ValueError):
            normalize_bot_token("not a token")


class TestNormalizeChatId:
    def test_numeric(self):
        from telegram import normalize_chat_id
        assert normalize_chat_id(" -1001234567890 ") == "-1001234567890"

    def test_public_name(self):
        from telegram import normalize_chat_id
        assert normalize_chat_id("@mychannel") == "@mychannel"

    def test_garbage_rejected(self):
        from telegram import normalize_chat_id
        with pytest.raises(ValueError):
            normalize_chat_id("my channel!")

    def test_empty_raises(self):
        from telegram import normalize_chat_id
        with pytest.raises(ValueError):
            normalize_chat_id("")


class TestConnect:
    def test_returns_token_chat_and_label(self, monkeypatch):
        import telegram

        monkeypatch.setattr(telegram, "get_me", lambda t: {"name": "FeedEcho Bot"})
        monkeypatch.setattr(
            telegram, "get_chat",
            lambda t, c: {"chat_title": "My Channel", "chat_username": "mychannel"},
        )
        info = telegram.connect("123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "@mychannel")
        assert info["chat_id"] == "@mychannel"
        assert info["chat_title"] == "My Channel"
        assert "FeedEcho Bot" in info["name"]

    def test_bad_token_raises_value_error(self):
        import telegram

        with pytest.raises(ValueError):
            telegram.connect("garbage", "@mychannel")


class TestPrepareText:
    def test_plain_mode_keeps_raw_prose(self):
        from telegram import prepare_text
        text = prepare_text("Loved it <3 a & b https://ex.com/a")
        assert text == "Loved it <3 a & b https://ex.com/a"

    def test_rich_mode_reduces_to_supported_tags(self):
        from telegram import prepare_text
        html = ('<h1>Title</h1><p><a href="https://ex.com/a">link</a>'
                "<b>bold</b><img src=\"https://x/y.jpg\">tail</p>")
        text = prepare_text(html, rich=True)
        assert "Title" in text
        assert '<a href="https://ex.com/a">' in text
        assert "<b>" in text
        assert "<img" not in text and "<h1>" not in text
        assert "tail" in text

    def test_rich_mode_drops_non_http_href(self):
        from telegram import prepare_text
        text = prepare_text('<a href="javascript:alert(1)">x</a>', rich=True)
        assert "javascript" not in text and ">x<" in text

    def test_plain_truncated_to_4096(self):
        from telegram import prepare_text
        assert len(prepare_text("x" * 5000)) == 4096

    def test_caption_truncated_to_1024(self):
        from telegram import prepare_caption
        assert len(prepare_caption("x" * 5000)) == 1024


class TestSendMessage:
    def test_posts_without_parse_mode_by_default(self, monkeypatch):
        import telegram

        captured = {}
        monkeypatch.setattr(telegram, "_post_json", lambda t, m, p: captured.update(t=t, m=m, p=p) or {"message_id": 1, "chat": {}})
        telegram.send_message("tok", "@ch", "hello", parse_mode=None)
        assert captured["m"] == "sendMessage"
        assert captured["p"] == {"chat_id": "@ch", "text": "hello"}

    def test_empty_text_is_bad_request(self):
        import telegram

        with pytest.raises(telegram.TelegramBadRequestError):
            telegram.send_message("tok", "@ch", "  ", parse_mode=None)

    def test_401_is_auth_error(self, monkeypatch):
        import telegram

        class _Resp:
            status_code = 401
            text = ""
            def json(self):
                return {"ok": False, "description": "Unauthorized"}

        monkeypatch.setattr(telegram, "pinned_request", lambda *a, **k: _Resp())
        with pytest.raises(telegram.TelegramAuthError):
            telegram.send_message("tok", "@ch", "hello", parse_mode=None)

    def test_chat_not_found_is_not_found(self, monkeypatch):
        import telegram

        class _Resp:
            status_code = 400
            text = ""
            def json(self):
                return {"ok": False, "description": "Bad Request: chat not found"}

        monkeypatch.setattr(telegram, "pinned_request", lambda *a, **k: _Resp())
        with pytest.raises(telegram.TelegramNotFoundError):
            telegram.send_message("tok", "@ch", "hello", parse_mode=None)

    def test_429_carries_retry_after(self, monkeypatch):
        import telegram

        class _Resp:
            status_code = 429
            text = ""
            headers = {"Retry-After": "7"}
            def json(self):
                return {"ok": False, "description": "Too Many Requests"}

        monkeypatch.setattr(telegram, "pinned_request", lambda *a, **k: _Resp())
        with pytest.raises(telegram.TelegramRateLimitError) as exc:
            telegram.send_message("tok", "@ch", "hello", parse_mode=None)
        assert exc.value.retry_after == 7.0


class TestMessageUrl:
    def test_public_channel(self):
        from telegram import message_url
        url = message_url({"message_id": 42, "chat": {"username": "mychannel"}})
        assert url == "https://t.me/mychannel/42"

    def test_private_chat_has_no_url(self):
        from telegram import message_url
        assert message_url({"message_id": 42, "chat": {"id": -100123}}) == ""


# ── dispatch wiring ──────────────────────────────────────────────────────────

def _telegram_echo(db_tmp):
    import database

    with database.get_db() as db:
        db.execute(
            """INSERT INTO telegram_accounts (name, bot_token, chat_id, user_id)
               VALUES ('main', 'enc-token', '-1001234567890', 1)""",
        )
        db.execute(
            "INSERT INTO feeds (name, url) VALUES (?, ?)",
            ("f", "https://example.com/feed"),
        )
        db.execute(
            """INSERT INTO echoes (feed_id, destination_type, destination_id, template,
                                   visibility, filter_keywords, filter_mode,
                                   content_warning, attach_image, enabled)
               VALUES (1, 'telegram', 1, '{{ title }} {{ link }}',
                       'public', '', 'exclude', '', 0, 1)""",
        )
        return db.execute("SELECT * FROM echoes WHERE id = 1").fetchone()


def _stub_send(monkeypatch, telegram_module=None):
    import scheduler

    sent = []

    def _fake_send_message(token, chat_id, text, parse_mode):
        sent.append({"token": token, "chat_id": chat_id, "text": text,
                     "parse_mode": parse_mode, "photo": None})
        return {"message_id": 7, "chat": {"username": "mychannel"}}

    monkeypatch.setattr(scheduler, "telegram_send_message", _fake_send_message)
    monkeypatch.setattr(scheduler, "telegram_send_photo", lambda *a, **k: sent.append({"photo": a}) or {})
    monkeypatch.setattr(scheduler, "_still_owns_claim", lambda *a, **k: True)
    return sent


class TestTelegramDispatch:
    def test_plain_template_sends_without_parse_mode(self, db_tmp, monkeypatch):
        import scheduler

        echo = _telegram_echo(db_tmp)
        sent = _stub_send(monkeypatch)
        item = {"id": "i1", "title": "Hello", "link": "https://example.com/1"}
        ok = scheduler.process_echo(echo, item)
        assert ok is True
        assert sent[0]["parse_mode"] is None
        assert sent[0]["text"] == "Hello https://example.com/1"
        assert sent[0]["photo"] is None

    def test_content_html_template_sends_html_parse_mode(self, db_tmp, monkeypatch):
        import database
        import scheduler

        echo = _telegram_echo(db_tmp)
        with database.get_db() as db:
            db.execute("UPDATE echoes SET template = '{{ content_html }}' WHERE id = 1")
            echo = db.execute("SELECT * FROM echoes WHERE id = 1").fetchone()
        sent = _stub_send(monkeypatch)
        item = {
            "id": "i1",
            "title": "T",
            "link": "https://example.com/1",
            "content_html": '<p>Read <a href="https://ex.com/a">this</a>.</p>',
        }
        ok = scheduler.process_echo(echo, item)
        assert ok is True
        assert sent[0]["parse_mode"] == "HTML"
        assert "<a href=" in sent[0]["text"]

    def test_auth_error_is_permanent(self, db_tmp, monkeypatch):
        import telegram
        import scheduler

        echo = _telegram_echo(db_tmp)
        monkeypatch.setattr(
            scheduler, "telegram_send_message",
            lambda *a, **k: (_ for _ in ()).throw(telegram.TelegramAuthError("401")),
        )
        monkeypatch.setattr(scheduler, "_still_owns_claim", lambda *a, **k: True)
        item = {"id": "i1", "title": "Hello", "link": "https://example.com/1"}
        ok = scheduler.process_echo(echo, item)
        assert ok is True  # handled: finalized as failed (gave up)
        import database

        with database.get_db() as db:
            row = db.execute(
                "SELECT status, error_message FROM posted_items WHERE echo_id = 1"
            ).fetchone()
        assert row["status"] == "gave_up"
        assert "Telegram delivery refused" in row["error_message"]

    def test_rate_limit_is_retryable(self, db_tmp, monkeypatch):
        import telegram
        import scheduler

        echo = _telegram_echo(db_tmp)
        monkeypatch.setattr(
            scheduler, "telegram_send_message",
            lambda *a, **k: (_ for _ in ()).throw(telegram.TelegramRateLimitError(5.0)),
        )
        monkeypatch.setattr(scheduler, "_still_owns_claim", lambda *a, **k: True)
        item = {"id": "i1", "title": "Hello", "link": "https://example.com/1"}
        # Not gave_up: a transient failure keeps the bounded retry pipeline.
        assert scheduler.process_echo(echo, item) is False
        import database

        with database.get_db() as db:
            row = db.execute(
                "SELECT status, error_message, attempt_count FROM posted_items WHERE echo_id = 1"
            ).fetchone()
        assert row["status"] == "failed"
        assert row["attempt_count"] == 1
        assert "Telegram delivery failed" in row["error_message"]

    def test_missing_account_is_permanent(self, db_tmp, monkeypatch):
        import scheduler

        echo = _telegram_echo(db_tmp)
        sent = _stub_send(monkeypatch)
        item = {"id": "i1", "title": "Hello", "link": "https://example.com/1"}
        ok = scheduler._send_telegram(echo, item, "x", 999, 1, "tok")
        assert ok is True
        assert sent == []

    def test_error_text_never_contains_token(self, db_tmp, monkeypatch):
        import telegram
        import scheduler

        echo = _telegram_echo(db_tmp)
        monkeypatch.setattr(
            scheduler, "telegram_send_message",
            lambda *a, **k: (_ for _ in ()).throw(telegram.TelegramError("boom")),
        )
        monkeypatch.setattr(scheduler, "_still_owns_claim", lambda *a, **k: True)
        item = {"id": "i1", "title": "Hello", "link": "https://example.com/1"}
        scheduler.process_echo(echo, item)
        import database

        with database.get_db() as db:
            row = db.execute(
                "SELECT error_message FROM posted_items WHERE echo_id = 1"
            ).fetchone()
        assert "enc-token" not in row["error_message"]


# ── routes ───────────────────────────────────────────────────────────────────

class TestTelegramRoutes:
    def test_connect_stores_encrypted_token(self, db_tmp, monkeypatch):
        from fastapi.testclient import TestClient
        import app as app_module
        import database

        monkeypatch.setattr(app_module, "telegram_connect", lambda t, c: {
            "bot_token": t, "chat_id": c,
            "name": "FeedEcho Bot → My Channel",
            "chat_title": "My Channel", "chat_username": "mychannel",
        })
        client = TestClient(app_module.app, follow_redirects=False)
        resp = client.post("/api/telegram-accounts", data={
            "bot_token": "123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            "chat_id": "@mychannel",
        })
        assert resp.status_code == 303
        with database.get_db() as db:
            row = db.execute("SELECT * FROM telegram_accounts").fetchone()
        assert row["chat_id"] == "@mychannel"
        # Single mode stores the token plaintext (the operator owns the DB);
        # multi-mode encryption is pinned in test_credential_encryption.py.
        assert row["bot_token"] == "123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

    def test_reconnect_same_chat_updates_in_place(self, db_tmp, monkeypatch):
        from fastapi.testclient import TestClient
        import app as app_module
        import database

        monkeypatch.setattr(app_module, "telegram_connect", lambda t, c: {
            "bot_token": t, "chat_id": c, "name": "Bot → Chat",
            "chat_title": "Chat", "chat_username": "",
        })
        client = TestClient(app_module.app, follow_redirects=False)
        client.post("/api/telegram-accounts", data={
            "bot_token": "123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            "chat_id": "@mychannel",
        })
        client.post("/api/telegram-accounts", data={
            "bot_token": "987654321:AAEyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy",
            "chat_id": "@mychannel",
        })
        with database.get_db() as db:
            count = db.execute("SELECT COUNT(*) AS c FROM telegram_accounts").fetchone()["c"]
        assert count == 1

    def test_delete_blocked_while_echoes_reference_it(self, db_tmp, monkeypatch):
        from fastapi.testclient import TestClient
        import app as app_module
        import database

        echo = _telegram_echo(db_tmp)
        client = TestClient(app_module.app, follow_redirects=False)
        resp = client.post("/api/telegram-accounts/1/delete")
        # Re-renders the accounts page with an error instead of deleting.
        assert resp.status_code == 200
        with database.get_db() as db:
            count = db.execute("SELECT COUNT(*) AS c FROM telegram_accounts").fetchone()["c"]
        assert count == 1
