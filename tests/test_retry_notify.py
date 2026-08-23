"""Tests for per-feed pause, bounded retries, and failure notifications."""

import os
import tempfile

import pytest


@pytest.fixture()
def env(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)

    import database

    monkeypatch.setattr(database, "DB_PATH", database.Path(path))
    database.init_db()

    import scheduler
    import notify

    monkeypatch.setattr(scheduler, "get_db", database.get_db)
    monkeypatch.setattr(notify, "get_db", database.get_db)

    yield database, scheduler, notify

    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except OSError:
            pass


def _seed(env, **echo_overrides):
    database, _, _ = env
    defaults = {
        "template": "{{ title }}",
        "visibility": "public",
        "filter_keywords": "",
        "filter_mode": "exclude",
        "enabled": 1,
    }
    defaults.update(echo_overrides)
    with database.get_db() as db:
        db.execute(
            "INSERT INTO accounts (name, username, instance, access_token) VALUES (?, ?, ?, ?)",
            ("main", "user", "https://mastodon.social", "tok"),
        )
        db.execute(
            "INSERT INTO feeds (name, url) VALUES (?, ?)",
            ("f", "https://example.com/feed"),
        )
        db.execute(
            """INSERT INTO echoes (feed_id, destination_type, destination_id, template,
                                   visibility, filter_keywords, filter_mode, enabled)
               VALUES (1, 'mastodon', 1, :template, :visibility,
                       :filter_keywords, :filter_mode, :enabled)""",
            defaults,
        )
        echo = db.execute("SELECT * FROM echoes WHERE id = 1").fetchone()
    return echo


def _item(id="item-1", title="Hello"):
    return {"id": id, "title": title, "link": f"https://example.com/{id}", "summary": ""}


class TestPausedFeed:
    def test_paused_feed_skips_fetch(self, env, monkeypatch):
        database, scheduler, _ = env
        _seed(env)

        fetch_calls = []
        monkeypatch.setattr(
            scheduler, "fetch_feed", lambda url: fetch_calls.append(url) or {"items": [_item()]}
        )

        with database.get_db() as db:
            db.execute("UPDATE feeds SET paused = 1, last_item_id = 'item-0' WHERE id = 1")

        scheduler.check_feed(1)
        assert fetch_calls == [], "paused feed must not be fetched"

    def test_unpaused_feed_fetches(self, env, monkeypatch):
        database, scheduler, _ = env
        _seed(env)

        monkeypatch.setattr(
            scheduler, "fetch_feed", lambda url: {"items": [_item()]}
        )
        monkeypatch.setattr(scheduler, "post_status", lambda **kw: {"id": "1"})

        with database.get_db() as db:
            db.execute("UPDATE feeds SET last_item_id = 'item-0' WHERE id = 1")

        scheduler.check_feed(1)
        with database.get_db() as db:
            row = db.execute("SELECT status FROM posted_items WHERE item_id = 'item-1'").fetchone()
        assert row["status"] == "success"


class TestRetryBackoff:
    def test_failure_sets_backoff(self, env, monkeypatch):
        database, scheduler, _ = env
        echo = _seed(env)
        monkeypatch.setattr(
            scheduler, "post_status", lambda **kw: (_ for _ in ()).throw(RuntimeError("down"))
        )

        assert scheduler.process_echo(echo, _item()) is False
        with database.get_db() as db:
            row = db.execute("SELECT * FROM posted_items WHERE item_id = 'item-1'").fetchone()
        assert row["status"] == "failed"
        assert row["next_retry_at"] is not None
        assert row["attempt_count"] == 1

    def test_backoff_blocks_immediate_retry(self, env, monkeypatch):
        database, scheduler, _ = env
        echo = _seed(env)
        calls = []

        def boom(**kw):
            calls.append(1)
            raise RuntimeError("down")

        monkeypatch.setattr(scheduler, "post_status", boom)

        scheduler.process_echo(echo, _item())
        # Second attempt right away: backoff not elapsed, claim refused
        assert scheduler.process_echo(echo, _item()) is False
        assert len(calls) == 1, "no delivery attempt during backoff"

    def test_max_attempts_gives_up(self, env, monkeypatch):
        database, scheduler, _ = env
        echo = _seed(env)
        monkeypatch.setattr(
            scheduler, "post_status", lambda **kw: (_ for _ in ()).throw(RuntimeError("down"))
        )

        with database.get_db() as db:
            db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('retry_max_attempts', '2')"
            )
            db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('retry_backoff_minutes', '1')"
            )

        assert scheduler.process_echo(echo, _item()) is False  # attempt 1 -> failed
        with database.get_db() as db:
            db.execute("UPDATE posted_items SET next_retry_at = NULL WHERE item_id = 'item-1'")
        # attempt 2 hits the cap -> gave_up, treated as handled so cursor advances
        assert scheduler.process_echo(echo, _item()) is True

        with database.get_db() as db:
            row = db.execute("SELECT * FROM posted_items WHERE item_id = 'item-1'").fetchone()
        assert row["status"] == "gave_up"
        assert "Gave up after 2 attempts" in row["error_message"]

    def test_zero_cap_retries_forever(self, env, monkeypatch):
        database, scheduler, _ = env
        echo = _seed(env)
        monkeypatch.setattr(
            scheduler, "post_status", lambda **kw: (_ for _ in ()).throw(RuntimeError("down"))
        )
        with database.get_db() as db:
            db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('retry_max_attempts', '0')"
            )
            # Simulate a row that has already failed 50 times
            db.execute(
                """INSERT INTO posted_items (echo_id, item_id, item_title, item_url, status,
                                             attempt_count, next_retry_at)
                   VALUES (1, 'item-1', 't', 'u', 'failed', 50, NULL)"""
            )

        assert scheduler.process_echo(echo, _item()) is False
        with database.get_db() as db:
            row = db.execute("SELECT * FROM posted_items WHERE item_id = 'item-1'").fetchone()
        assert row["status"] == "failed", "cap=0 must never give up"
        assert row["attempt_count"] == 51

    def test_gave_up_is_terminal(self, env, monkeypatch):
        database, scheduler, _ = env
        echo = _seed(env)
        monkeypatch.setattr(
            scheduler, "post_status", lambda **kw: pytest.fail("must not deliver gave_up row")
        )
        with database.get_db() as db:
            db.execute(
                """INSERT INTO posted_items (echo_id, item_id, item_title, item_url, status,
                                             attempt_count)
                   VALUES (1, 'item-1', 't', 'u', 'gave_up', 5)"""
            )
        assert scheduler.process_echo(echo, _item()) is True


class TestNotifications:
    def test_alert_at_threshold_and_recovery(self, env, monkeypatch):
        database, scheduler, notify = env
        echo = _seed(env)
        sent = []
        monkeypatch.setattr(notify, "send_email", lambda **kw: sent.append(kw))
        monkeypatch.setattr(scheduler, "send_email", lambda **kw: sent.append(kw))
        monkeypatch.setattr(
            notify, "get_smtp_settings", lambda user_id=1: {"host": "h", "port": 587}
        )
        with database.get_db() as db:
            db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('notify_failure_threshold', '2')"
            )
            db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('notify_email', 'me@example.com')"
            )

        monkeypatch.setattr(
            scheduler, "post_status", lambda **kw: (_ for _ in ()).throw(RuntimeError("down"))
        )

        scheduler.process_echo(echo, _item("i1"))
        assert sent == [], "below threshold: no alert"

        with database.get_db() as db:
            db.execute("UPDATE posted_items SET next_retry_at = NULL")
        scheduler.process_echo(echo, _item("i1"))
        assert len(sent) == 1, "at threshold: one alert"
        assert "failing" in sent[0]["subject"]
        assert sent[0]["to_email"] == "me@example.com"

        # Third failure: already alerted, no repeat
        with database.get_db() as db:
            db.execute("UPDATE posted_items SET next_retry_at = NULL")
        scheduler.process_echo(echo, _item("i1"))
        assert len(sent) == 1, "no repeat alert while in alerted state"

        # Recovery sends one all-clear
        monkeypatch.setattr(scheduler, "post_status", lambda **kw: {"id": "1"})
        with database.get_db() as db:
            db.execute("UPDATE posted_items SET next_retry_at = NULL")
        assert scheduler.process_echo(echo, _item("i1")) is True
        assert len(sent) == 2
        assert "recovered" in sent[1]["subject"]

    def test_threshold_zero_disables(self, env, monkeypatch):
        database, scheduler, notify = env
        echo = _seed(env)
        sent = []
        monkeypatch.setattr(notify, "send_email", lambda **kw: sent.append(kw))
        monkeypatch.setattr(notify, "get_smtp_settings", lambda user_id=1: {"host": "h", "port": 587})
        with database.get_db() as db:
            db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('notify_failure_threshold', '0')"
            )
        monkeypatch.setattr(
            scheduler, "post_status", lambda **kw: (_ for _ in ()).throw(RuntimeError("down"))
        )
        for i in range(5):
            with database.get_db() as db:
                db.execute("UPDATE posted_items SET next_retry_at = NULL")
            scheduler.process_echo(echo, _item())
        assert sent == []


class TestRetrySweep:
    def test_sweep_retries_due_rows(self, env, monkeypatch):
        """Failed rows behind the cursor get retried by the sweep."""
        database, scheduler, _ = env
        echo = _seed(env)

        # A failed row whose backoff has elapsed, item still in feed
        with database.get_db() as db:
            db.execute(
                """INSERT INTO posted_items (echo_id, item_id, item_title, item_url, status,
                                             attempt_count, next_retry_at)
                   VALUES (1, 'old-item', 't', 'u', 'failed', 1, '2000-01-01 00:00:00')"""
            )

        monkeypatch.setattr(
            scheduler, "fetch_feed", lambda url: {"items": [_item("old-item")]}
        )
        monkeypatch.setattr(scheduler, "post_status", lambda **kw: {"id": "1"})

        scheduler._retry_due_failures(1, [echo])
        with database.get_db() as db:
            row = db.execute(
                "SELECT status FROM posted_items WHERE item_id = 'old-item'"
            ).fetchone()
        assert row["status"] == "success"

    def test_sweep_gives_up_on_aged_out_items(self, env, monkeypatch):
        database, scheduler, _ = env
        echo = _seed(env)
        with database.get_db() as db:
            db.execute(
                """INSERT INTO posted_items (echo_id, item_id, item_title, item_url, status,
                                             attempt_count, next_retry_at)
                   VALUES (1, 'gone-item', 't', 'u', 'failed', 1, '2000-01-01 00:00:00')"""
            )
        monkeypatch.setattr(
            scheduler, "fetch_feed", lambda url: {"items": [_item("other")]}
        )
        scheduler._retry_due_failures(1, [echo])
        with database.get_db() as db:
            row = db.execute(
                "SELECT status FROM posted_items WHERE item_id = 'gone-item'"
            ).fetchone()
        assert row["status"] == "gave_up"

    def test_sweep_leaves_unelapsed_rows(self, env, monkeypatch):
        database, scheduler, _ = env
        echo = _seed(env)
        with database.get_db() as db:
            db.execute(
                """INSERT INTO posted_items (echo_id, item_id, item_title, item_url, status,
                                             attempt_count, next_retry_at)
                   VALUES (1, 'wait-item', 't', 'u', 'failed', 1, '2999-01-01 00:00:00')"""
            )
        called = []
        monkeypatch.setattr(
            scheduler, "fetch_feed", lambda url: called.append(1) or {"items": []}
        )
        scheduler._retry_due_failures(1, [echo])
        assert called == [], "no feed fetch when nothing is due"
