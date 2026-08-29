"""Regression coverage for the 2026-08-28 Kimi K3 full-repo review fixes.

Pins, one per finding: email-recipient validation, micro.blog reconnect at
cap, digest overflow held instead of truncated, digest send failures feeding
the notify counter, digest failures being invisible to the retry sweep, the
UTC clock invariant in verification.py, JSON-Feed detection under query
strings, the alt-text checkbox value, and OAuth state-secret precedence.
"""

import importlib
import os
import tempfile
from unittest import mock

import pytest

import database
import notify
import settings


@pytest.fixture()
def db_tmp(monkeypatch):
    """Point the DB layer at a fresh temp file per test (repo convention)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)

    import database
    import scheduler

    monkeypatch.setattr(database, "DB_PATH", database.Path(path))
    database.init_db()
    monkeypatch.setattr(scheduler, "get_db", database.get_db)
    yield database
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except OSError:
            pass


UID = 5


@pytest.fixture()
def multi_client(monkeypatch, db_tmp):
    """Signed-in multi-mode TestClient over the temp DB (repo convention)."""
    import auth
    import security
    from app import app
    from fastapi.testclient import TestClient

    monkeypatch.setattr(settings, "MULTI", True)
    monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
    auth._login_attempts.clear()
    auth._register_attempts.clear()
    with database.get_db() as db:
        db.execute(
            "INSERT INTO users (id, email, password_hash, email_verified)"
            " VALUES (?, 'u@example.com', '', 1)",
            (UID,),
        )
    client = TestClient(app)
    client.cookies.set("feedecho_session", security.sign_session(UID, "u@example.com"))
    return client


@pytest.fixture()
def restore_settings():
    """Re-read settings from the real env after tests that reload it."""
    yield
    importlib.reload(settings)


def _setup_email_echo(db, template="{{ title }} — {{ link }}"):
    """Create a test email account, feed, and digest echo. Returns the row."""
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO email_accounts (name, email) VALUES (?, ?)",
            ("Test User", "test@example.com"),
        )
        conn.execute(
            "INSERT INTO feeds (name, url) VALUES (?, ?)",
            ("f", "https://example.com/feed"),
        )
        conn.execute(
            """INSERT INTO echoes (feed_id, destination_type, destination_id,
                                   template, delivery_mode, enabled)
               VALUES (1, 'email', 1, ?, 'digest', 1)""",
            (template,),
        )
        return conn.execute("SELECT * FROM echoes WHERE id = 1").fetchone()


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


# ── HIGH 1: email recipient validation ──────────────────────────────────────


class TestEmailRecipientValidation:
    def test_rejects_crlf_recipient(self, multi_client):
        r = multi_client.post(
            "/api/email-accounts",
            data={"name": "Evil", "email": "a@b.com\r\nBcc: victim@example.com"},
        )
        assert r.status_code == 200
        assert "valid email" in r.text
        with database.get_db() as db:
            count = db.execute(
                "SELECT COUNT(*) AS c FROM email_accounts"
            ).fetchone()["c"]
        assert count == 0

    def test_rejects_whitespace_and_missing_at(self, multi_client):
        for bad in ("not-an-email", "a b@c.com", "@nowhere.com", "user@"):
            r = multi_client.post(
                "/api/email-accounts", data={"name": "x", "email": bad}
            )
            assert "valid email" in r.text, bad

    def test_accepts_normal_address(self, multi_client):
        r = multi_client.post(
            "/api/email-accounts",
            data={"name": "Ok", "email": "user@example.com"},
            follow_redirects=False,
        )
        assert r.status_code == 303


# ── MEDIUM 2: micro.blog cap counts only NEW rows ───────────────────────────


class TestMicroblogCapCountsNewOnly:
    def _connect(self, client, blogs, token="t"):
        with mock.patch(
            "app.microblog_list_destinations", return_value=blogs
        ):
            return client.post(
                "/api/microblog-accounts", data={"token": token},
                follow_redirects=False,
            )

    def test_reconnect_at_cap_succeeds(self, multi_client):
        blog = [{"uid": "https://a.micro.blog/", "name": "A"}]
        assert self._connect(multi_client, blog).status_code == 303
        # Fill the plan to the cap (5 destinations total).
        for i in range(4):
            multi_client.post(
                "/api/email-accounts",
                data={"name": f"a{i}", "email": f"user{i}@example.com"},
            )
        # Re-connecting the SAME blog (token rotation) must not be blocked.
        r = self._connect(multi_client, blog, token="rotated")
        assert r.status_code == 303

    def test_new_blog_at_cap_still_blocked(self, multi_client):
        blog = [{"uid": "https://a.micro.blog/", "name": "A"}]
        assert self._connect(multi_client, blog).status_code == 303
        for i in range(4):
            multi_client.post(
                "/api/email-accounts",
                data={"name": f"a{i}", "email": f"user{i}@example.com"},
            )
        r = self._connect(
            multi_client, [{"uid": "https://b.micro.blog/", "name": "B"}]
        )
        assert r.status_code == 200
        assert "would fit" in r.text

    def test_reconnect_over_cap_with_zero_new_blogs_succeeds(self, multi_client):
        """Over-cap legacy state (e.g. a plan downgrade) must not trap a
        token rotation: zero NEW rows means the cap check does not apply."""
        blog = [{"uid": "https://a.micro.blog/", "name": "A"}]
        assert self._connect(multi_client, blog).status_code == 303
        for i in range(4):
            multi_client.post(
                "/api/email-accounts",
                data={"name": f"a{i}", "email": f"user{i}@example.com"},
            )
        # Push the total over the cap outside the API (plan-downgrade shape).
        with database.get_db() as db:
            db.execute(
                "INSERT INTO email_accounts (name, email, user_id)"
                " VALUES ('extra', 'extra@example.com', ?)",
                (UID,),
            )
        r = self._connect(multi_client, blog, token="rotated")
        assert r.status_code == 303


# ── MEDIUM 3: digest overflow is held, not silently truncated ───────────────


class TestDigestOverflowHeld:
    def _queue(self, echo, n, content_len):
        import scheduler

        for i in range(n):
            item = _item(
                id=f"item-{i}", title=f"Post {i}",
                link=f"https://example.com/{i}", summary="x" * content_len,
            )
            scheduler.process_echo(echo, item)

    def test_overflow_items_stay_queued(self, db_tmp, monkeypatch):
        import scheduler

        monkeypatch.setattr(
            scheduler, "send_email", lambda **kw: {"success": True}
        )
        echo = _setup_email_echo(db_tmp, template="{{ title }}: {{ summary }}")
        self._queue(echo, 30, content_len=800)

        scheduler.flush_digests()

        with db_tmp.get_db() as db:
            remaining = db.execute(
                "SELECT COUNT(*) AS c FROM digest_items WHERE echo_id = ?",
                (echo["id"],),
            ).fetchone()["c"]
            held_rows = db.execute(
                """SELECT COUNT(*) AS c FROM posted_items
                    WHERE echo_id = ? AND status = 'queued'""",
                (echo["id"],),
            ).fetchall()
        assert remaining > 0, "overflow items must stay queued"
        assert held_rows[0]["c"] == remaining

    def test_sent_body_mentions_held_items(self, db_tmp, monkeypatch):
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "send_email", lambda **kw: sent.append(kw) or {"success": True}
        )
        echo = _setup_email_echo(db_tmp, template="{{ title }}: {{ summary }}")
        self._queue(echo, 30, content_len=800)
        scheduler.flush_digests()
        assert sent and "held for the next digest" in sent[0]["body"]
        assert len(sent[0]["body"]) <= scheduler.DIGEST_MAX_CHARS

    def test_repeated_flushes_drain_held_items(self, db_tmp, monkeypatch):
        import scheduler

        calls = []
        monkeypatch.setattr(
            scheduler, "send_email", lambda **kw: calls.append(kw) or {"success": True}
        )
        echo = _setup_email_echo(db_tmp, template="{{ title }}: {{ summary }}")
        self._queue(echo, 30, content_len=800)

        # ~12 items fit per 10K flush; keep flushing until the queue drains.
        remaining = None
        for _ in range(5):
            scheduler.flush_digests()
            with db_tmp.get_db() as db:
                remaining = db.execute(
                    "SELECT COUNT(*) AS c FROM digest_items WHERE echo_id = ?",
                    (echo["id"],),
                ).fetchone()["c"]
            if remaining == 0:
                break
        assert remaining == 0
        assert len(calls) >= 2, "held items went out on subsequent flushes"

    def test_single_oversized_item_still_sends_truncated(self, db_tmp, monkeypatch):
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "send_email", lambda **kw: sent.append(kw) or {"success": True}
        )
        echo = _setup_email_echo(db_tmp, template="{{ title }}: {{ summary }}")
        self._queue(echo, 1, content_len=scheduler.DIGEST_MAX_CHARS * 2)
        scheduler.flush_digests()
        assert sent, "one giant item must still go out (truncated)"
        assert "…" in sent[0]["body"]

    def test_degenerate_oversized_item_is_held_not_dropped(self, db_tmp, monkeypatch):
        """When even the title leaves no content budget, nothing is sent and
        the queue is untouched — a title-only send that deletes the item
        would silently drop its content."""
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "send_email", lambda **kw: sent.append(kw) or {"success": True}
        )
        echo = _setup_email_echo(db_tmp, template="{{ title }}: {{ summary }}")
        # Title alone exceeds the whole cap: no content budget can remain.
        huge_title = "T" * (scheduler.DIGEST_MAX_CHARS + 50)
        scheduler.process_echo(
            echo, _item(id="i1", title=huge_title, summary="body content here")
        )
        scheduler.flush_digests()
        assert sent == [], "content-less digest must not be sent"
        with db_tmp.get_db() as db:
            remaining = db.execute(
                "SELECT COUNT(*) AS c FROM digest_items WHERE echo_id = ?",
                (echo["id"],),
            ).fetchone()["c"]
            statuses = db.execute(
                "SELECT status FROM posted_items WHERE echo_id = ?",
                (echo["id"],),
            ).fetchall()
        assert remaining == 1
        assert all(r["status"] == "queued" for r in statuses)


# ── MEDIUM 4: digest send failures reach the notify counter ─────────────────


class TestDigestFailureNotifies:
    def test_failed_flush_marks_rows_failed_without_retry(self, db_tmp, monkeypatch):
        import scheduler

        def boom(**kw):
            raise RuntimeError("SMTP down")

        monkeypatch.setattr(scheduler, "send_email", boom)
        echo = _setup_email_echo(db_tmp)
        scheduler.process_echo(echo, _item(id="i1"))
        scheduler.flush_digests()

        with db_tmp.get_db() as db:
            rows = db.execute(
                "SELECT status, next_retry_at FROM posted_items WHERE echo_id = ?",
                (echo["id"],),
            ).fetchall()
        assert rows
        assert all(r["status"] == "failed" for r in rows)
        # next_retry_at must stay NULL: the retry sweep (which requires a
        # retry time) must never grab digest items — only flush_digests
        # owns them.
        assert all(r["next_retry_at"] is None for r in rows)
        with db_tmp.get_db() as db:
            queued = db.execute(
                "SELECT COUNT(*) AS c FROM digest_items WHERE echo_id = ?",
                (echo["id"],),
            ).fetchone()["c"]
        assert queued == 1, "digest_items survive for the next flush"

    def test_held_items_stay_queued_when_send_fails(self, db_tmp, monkeypatch):
        """A size-cap hold is not an attempt: when the send of a truncated
        body fails, only the attempted (sent-listed) items are marked
        'failed'; held items keep status 'queued' with no attempt inflation."""
        import scheduler

        def boom(**kw):
            raise RuntimeError("SMTP down")

        monkeypatch.setattr(scheduler, "send_email", boom)
        echo = _setup_email_echo(db_tmp, template="{{ title }}: {{ summary }}")
        # ~12 fit per flush; the rest are held at build time.
        for i in range(20):
            scheduler.process_echo(echo, _item(
                id=f"item-{i}", title=f"Post {i}",
                link=f"https://example.com/{i}", summary="x" * 800,
            ))

        scheduler.flush_digests()  # send fails

        with db_tmp.get_db() as db:
            rows = db.execute(
                "SELECT item_id, status FROM posted_items WHERE echo_id = ?",
                (echo["id"],),
            ).fetchall()
        statuses = {r["item_id"]: r["status"] for r in rows}
        failed = [k for k, v in statuses.items() if v == "failed"]
        queued = [k for k, v in statuses.items() if v == "queued"]
        assert failed, "attempted items must be marked failed"
        assert queued, "held items must stay queued (never attempted)"
        # The held set is exactly the tail that could not fit: every
        # attempted index precedes every held index.
        max_failed = max(int(k.split("-")[1]) for k in failed)
        min_queued = min(int(k.split("-")[1]) for k in queued)
        assert max_failed < min_queued

    def test_repeated_failures_trigger_alert(self, db_tmp, monkeypatch):
        import scheduler

        def boom(**kw):
            raise RuntimeError("SMTP down")

        monkeypatch.setattr(scheduler, "send_email", boom)
        alerts = []
        monkeypatch.setattr(
            notify, "send_email", lambda **kw: alerts.append(kw) or {"success": True}
        )
        # record_failure alerts only when SMTP is configured for the owner.
        monkeypatch.setattr(
            notify, "get_smtp_settings", lambda user_id=1: {"host": "h", "port": 587}
        )
        echo = _setup_email_echo(db_tmp)
        scheduler.process_echo(echo, _item(id="i1"))
        scheduler.flush_digests()
        assert alerts == [], "first failure is below the default threshold"
        scheduler.flush_digests()
        assert len(alerts) == 1
        assert "failing" in alerts[0]["subject"]

    def test_repeated_failures_use_default_threshold(self, db_tmp, monkeypatch):
        """With the default threshold of 3 and the queue-time attempt
        already counted, two failed flushes (attempt_count 2 then 3) must
        trip exactly one alert."""
        import scheduler

        def boom(**kw):
            raise RuntimeError("SMTP down")

        monkeypatch.setattr(scheduler, "send_email", boom)
        alerts = []
        monkeypatch.setattr(
            notify, "send_email", lambda **kw: alerts.append(kw) or {"success": True}
        )
        monkeypatch.setattr(
            notify, "get_smtp_settings", lambda user_id=1: {"host": "h", "port": 587}
        )
        echo = _setup_email_echo(db_tmp)
        scheduler.process_echo(echo, _item(id="i1"))
        scheduler.flush_digests()
        scheduler.flush_digests()
        assert len(alerts) == 1
        # One alert, not one per subsequent failure: notify state latches.
        scheduler.flush_digests()
        assert len(alerts) == 1

    def test_success_after_failure_finalizes_rows(self, db_tmp, monkeypatch):
        """A flush that succeeds after a prior failure must move the
        posted_items rows to 'success' — the queue delete below it must not
        strand a 'failed' row with no digest_items left (silent loss)."""
        import scheduler

        calls = []

        def flaky(**kw):
            calls.append(kw)
            if len(calls) == 1:
                raise RuntimeError("SMTP down")
            return {"success": True}

        monkeypatch.setattr(scheduler, "send_email", flaky)
        echo = _setup_email_echo(db_tmp)
        scheduler.process_echo(echo, _item(id="i1"))

        scheduler.flush_digests()  # fails
        scheduler.flush_digests()  # succeeds

        with db_tmp.get_db() as db:
            rows = db.execute(
                "SELECT status FROM posted_items WHERE echo_id = ?",
                (echo["id"],),
            ).fetchall()
        assert rows and all(r["status"] == "success" for r in rows), (
            "recovered digest items must be finalized as success, not left "
            "'failed' while their digest_items rows are deleted"
        )


# ── MEDIUM 6: verification.py uses bound UTC clocks ─────────────────────────


class TestVerificationUtcClocks:
    def test_no_current_timestamp_in_verification_sql(self):
        import inspect
        import verification

        src = inspect.getsource(verification)
        assert "CURRENT_TIMESTAMP" not in src, (
            "verification.py must bind UTC params; CURRENT_TIMESTAMP resolves "
            "in the PG session time zone"
        )

    def test_consume_rejects_expired_token(self, db_tmp):
        from datetime import datetime, timedelta, timezone

        from security import token_hash
        import verification

        past = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
            verification._TS
        )
        with db_tmp.get_db() as db:
            db.execute(
                "INSERT INTO email_tokens (user_id, token_hash, purpose, expires_at)"
                " VALUES (1, ?, 'verify', ?)",
                (token_hash("tok"), past),
            )
        assert verification.consume_token("tok", "verify") is None

    def test_peek_live_but_not_expired_token(self, db_tmp):
        from datetime import datetime, timedelta, timezone

        from security import token_hash
        import verification

        live = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime(
            verification._TS
        )
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
            verification._TS
        )
        with db_tmp.get_db() as db:
            db.execute(
                "INSERT INTO email_tokens (user_id, token_hash, purpose, expires_at)"
                " VALUES (1, ?, 'verify', ?)",
                (token_hash("live"), live),
            )
            db.execute(
                "INSERT INTO email_tokens (user_id, token_hash, purpose, expires_at)"
                " VALUES (1, ?, 'reset', ?)",
                (token_hash("dead"), past),
            )
        assert verification.peek_token("live", "verify") == 1
        assert verification.peek_token("dead", "reset") is None


# ── MEDIUM 7: JSON Feed detection via URL path ──────────────────────────────


class TestJsonFeedDetection:
    def test_query_string_json_url_uses_json_parser(self, monkeypatch):
        import feed_parser

        monkeypatch.setattr(
            feed_parser,
            "_fetch_with_redirect_validation",
            lambda client, u, headers, max_bytes, backend=None: (
                b'{"version": "https://jsonfeed.org/version/1"}', "text/plain"
            ),
        )
        # text/plain + .json-with-query path: only the path check can catch it.
        result = feed_parser.fetch_feed("https://example.com/feed.json?token=abc")
        assert result["items"] == []

    def test_xml_feed_still_uses_feedparser(self, monkeypatch):
        import feed_parser

        monkeypatch.setattr(
            feed_parser,
            "_fetch_with_redirect_validation",
            lambda client, u, headers, max_bytes, backend=None: (
                b"<rss></rss>", "text/xml"
            ),
        )
        result = feed_parser.fetch_feed("https://example.com/feed")
        assert result["items"] == []


# ── MEDIUM 8: alt-text checkbox value ───────────────────────────────────────


class TestAltTextCheckbox:
    def test_checkbox_submits_one(self):
        from pathlib import Path

        html = (
            Path(__file__).resolve().parent.parent
            / "templates" / "settings.html"
        ).read_text()
        assert 'name="alt_text_ai_enabled" value="1"' in html
        assert 'name="alt_text_ai_enabled" value="true"' not in html

    def test_save_then_render_roundtrip(self, multi_client):
        r = multi_client.post(
            "/api/settings/alt-text",
            data={
                "alt_text_ai_enabled": "1",
                "alt_text_ai_base_url": "http://localhost:8080",
                "alt_text_ai_model": "m",
                "alt_text_ai_api_key": "",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        page = multi_client.get("/settings")
        assert "checked" in page.text


# ── MEDIUM 9: OAuth state secret precedence ─────────────────────────────────


class TestOauthStateSecretPrecedence:
    @pytest.fixture()
    def restore_oauth(self, restore_settings):
        """Put the reloaded-by-this-test oauth module back after the env."""
        yield
        import oauth

        importlib.reload(oauth)

    def _reload(self, monkeypatch, env):
        import oauth

        for k, v in env.items():
            if v is None:
                monkeypatch.delenv(k, raising=False)
            else:
                monkeypatch.setenv(k, v)
        importlib.reload(settings)
        importlib.reload(oauth)

    def test_state_secret_beats_auth_token(self, monkeypatch, restore_oauth):
        self._reload(monkeypatch, {
            "FEEDECHO_MODE": "single",
            "FEEDECHO_AUTH_TOKEN": "auth-token-value",
            "FEEDECHO_STATE_SECRET": "state-secret-value",
        })
        import oauth

        assert oauth._STATE_SECRET == b"state-secret-value"

    def test_auth_token_still_fallback_without_state_secret(
        self, monkeypatch, restore_oauth,
    ):
        self._reload(monkeypatch, {
            "FEEDECHO_MODE": "single",
            "FEEDECHO_AUTH_TOKEN": "auth-token-value",
            "FEEDECHO_STATE_SECRET": None,
        })
        import oauth

        assert oauth._STATE_SECRET == b"auth-token-value"
