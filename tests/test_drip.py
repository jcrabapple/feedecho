"""Tests for drip mode: per-echo hourly rate limiting with a release queue."""

import json
import os
import tempfile
import time

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
        "delivery_mode": "instant",
        "drip_limit": 0,
        "content_warning": "",
        "attach_image": 0,
    }
    defaults.update(echo_overrides)
    with database.get_db() as db:
        db.execute(
            "INSERT INTO accounts (name, username, instance, access_token)"
            " VALUES (?, ?, ?, ?)",
            ("main", "user", "https://mastodon.social", "tok"),
        )
        db.execute(
            "INSERT INTO feeds (name, url) VALUES (?, ?)",
            ("f", "https://example.com/feed"),
        )
        db.execute(
            """INSERT INTO echoes (feed_id, destination_type, destination_id, template,
                                   visibility, filter_keywords, filter_mode, enabled,
                                   delivery_mode, drip_limit, content_warning, attach_image)
               VALUES (1, 'mastodon', 1, :template, :visibility, :filter_keywords,
                       :filter_mode, :enabled, :delivery_mode, :drip_limit,
                       :content_warning, :attach_image)""",
            defaults,
        )
        echo = db.execute("SELECT * FROM echoes WHERE id = 1").fetchone()
    return echo


def _item(id="item-1", title="Hello"):
    return {
        "id": id,
        "title": title,
        "link": f"https://example.com/{id}",
        "summary": "",
        "tags": [],
    }


def _record_success(database, echo_id=1, item_id="item-0", age_minutes=None):
    with database.get_db() as db:
        if age_minutes is None:
            db.execute(
                """INSERT INTO posted_items (echo_id, item_id, item_title, item_url, status)
                   VALUES (?, ?, 't', 'https://example.com', 'success')""",
                (echo_id, item_id),
            )
        else:
            db.execute(
                """INSERT INTO posted_items (echo_id, item_id, item_title, item_url,
                                             status, posted_at)
                   VALUES (?, ?, 't', 'https://example.com', 'success', datetime('now', ?))""",
                (echo_id, item_id, f"-{age_minutes} minutes"),
            )


def _queue_item(database, echo_id, item_id, item):
    with database.get_db() as db:
        db.execute(
            """INSERT INTO posted_items (echo_id, item_id, item_title, item_url, status)
               VALUES (?, ?, 't', 'https://example.com', 'queued')""",
            (echo_id, item_id),
        )
        db.execute(
            "INSERT INTO drip_items (echo_id, item_id, item_json) VALUES (?, ?, ?)",
            (echo_id, item_id, json.dumps(item)),
        )


def _state(database, echo_id, item_id):
    with database.get_db() as db:
        row = db.execute(
            "SELECT status FROM posted_items WHERE echo_id = ? AND item_id = ?",
            (echo_id, item_id),
        ).fetchone()
    return row["status"] if row else None


def _drip_row_count(database, echo_id):
    with database.get_db() as db:
        return db.execute(
            "SELECT COUNT(*) AS n FROM drip_items WHERE echo_id = ?", (echo_id,)
        ).fetchone()["n"]


class TestDripQueueing:
    def test_rate_limited_item_queued(self, env, monkeypatch):
        database, scheduler, _ = env
        echo = _seed(env, drip_limit=1)
        _record_success(database)

        posted = []
        monkeypatch.setattr(scheduler, "post_status", lambda **kw: posted.append(kw) or {"id": "x"})

        result = scheduler.process_echo(echo, _item())
        assert result is True
        assert posted == []
        assert _state(database, 1, "item-1") == "queued"
        assert _drip_row_count(database, 1) == 1

    def test_room_available_dispatches_immediately(self, env, monkeypatch):
        database, scheduler, _ = env
        echo = _seed(env, drip_limit=1)

        posted = []
        monkeypatch.setattr(scheduler, "post_status", lambda **kw: posted.append(kw) or {"id": "x"})

        assert scheduler.process_echo(echo, _item()) is True
        assert len(posted) == 1
        assert _state(database, 1, "item-1") == "success"
        assert _drip_row_count(database, 1) == 0

    def test_old_success_outside_window_does_not_count(self, env, monkeypatch):
        database, scheduler, _ = env
        echo = _seed(env, drip_limit=1)
        _record_success(database, item_id="item-0", age_minutes=90)

        posted = []
        monkeypatch.setattr(scheduler, "post_status", lambda **kw: posted.append(kw) or {"id": "x"})

        assert scheduler.process_echo(echo, _item()) is True
        assert len(posted) == 1

    def test_drip_off_never_queues(self, env, monkeypatch):
        database, scheduler, _ = env
        echo = _seed(env, drip_limit=0)
        _record_success(database)

        posted = []
        monkeypatch.setattr(scheduler, "post_status", lambda **kw: posted.append(kw) or {"id": "x"})

        assert scheduler.process_echo(echo, _item()) is True
        assert len(posted) == 1
        assert _drip_row_count(database, 1) == 0

    def test_digest_echo_ignores_drip(self, env, monkeypatch):
        database, scheduler, _ = env
        echo = _seed(env, drip_limit=1, delivery_mode="digest")
        _record_success(database)

        posted = []
        monkeypatch.setattr(scheduler, "post_status", lambda **kw: posted.append(kw) or {"id": "x"})

        assert scheduler.process_echo(echo, _item()) is True
        assert len(posted) == 1

    def test_queue_full_drops_item_as_gave_up(self, env, monkeypatch):
        database, scheduler, _ = env
        echo = _seed(env, drip_limit=1)
        _record_success(database)

        # Fill the queue to capacity with distinct items
        for i in range(scheduler.DRIP_QUEUE_CAP):
            _queue_item(database, 1, f"fill-{i}", _item(id=f"fill-{i}"))

        posted = []
        monkeypatch.setattr(scheduler, "post_status", lambda **kw: posted.append(kw) or {"id": "x"})

        assert scheduler.process_echo(echo, _item()) is True
        assert posted == []
        assert _state(database, 1, "item-1") == "gave_up"
        assert _drip_row_count(database, 1) == scheduler.DRIP_QUEUE_CAP

    def test_item_payload_excludes_raw_and_serializes(self, env, monkeypatch):
        database, scheduler, _ = env
        echo = _seed(env, drip_limit=1)
        _record_success(database)

        item = _item()
        item["raw"] = {"published_parsed": time.struct_time((2024, 1, 15, 9, 30, 0, 0, 15, 0))}

        monkeypatch.setattr(scheduler, "post_status", lambda **kw: {"id": "x"})
        assert scheduler.process_echo(echo, item) is True

        with database.get_db() as db:
            row = db.execute(
                "SELECT item_json FROM drip_items WHERE echo_id = 1 AND item_id = 'item-1'"
            ).fetchone()
        stored = json.loads(row["item_json"])
        assert stored["title"] == "Hello"
        assert "raw" not in stored


class TestFlushDrips:
    def test_flush_releases_when_room(self, env, monkeypatch):
        database, scheduler, _ = env
        echo = _seed(env, drip_limit=2)
        _queue_item(database, 1, "item-1", _item())

        posted = []
        monkeypatch.setattr(scheduler, "post_status", lambda **kw: posted.append(kw) or {"id": "x"})

        scheduler.flush_drips()

        assert len(posted) == 1
        assert _state(database, 1, "item-1") == "success"
        assert _drip_row_count(database, 1) == 0

    def test_flush_respects_full_window(self, env, monkeypatch):
        database, scheduler, _ = env
        _seed(env, drip_limit=1)
        _record_success(database, item_id="item-0")
        _queue_item(database, 1, "item-1", _item())

        posted = []
        monkeypatch.setattr(scheduler, "post_status", lambda **kw: posted.append(kw) or {"id": "x"})

        scheduler.flush_drips()

        assert posted == []
        assert _state(database, 1, "item-1") == "queued"
        assert _drip_row_count(database, 1) == 1

    def test_flush_releases_only_up_to_room(self, env, monkeypatch):
        database, scheduler, _ = env
        _seed(env, drip_limit=2)
        _queue_item(database, 1, "item-1", _item(id="item-1"))
        _queue_item(database, 1, "item-2", _item(id="item-2"))
        _queue_item(database, 1, "item-3", _item(id="item-3"))

        posted = []
        monkeypatch.setattr(scheduler, "post_status", lambda **kw: posted.append(kw) or {"id": "x"})

        scheduler.flush_drips()

        assert len(posted) == 2
        assert _state(database, 1, "item-1") == "success"
        assert _state(database, 1, "item-2") == "success"
        assert _state(database, 1, "item-3") == "queued"
        assert _drip_row_count(database, 1) == 1

    def test_flush_downgrade_releases_all(self, env, monkeypatch):
        database, scheduler, _ = env
        _seed(env, drip_limit=0)
        _queue_item(database, 1, "item-1", _item(id="item-1"))
        _queue_item(database, 1, "item-2", _item(id="item-2"))

        posted = []
        monkeypatch.setattr(scheduler, "post_status", lambda **kw: posted.append(kw) or {"id": "x"})

        scheduler.flush_drips()

        assert len(posted) == 2
        assert _drip_row_count(database, 1) == 0

    def test_flush_downgrade_drains_in_bounded_batches(self, env, monkeypatch):
        database, scheduler, _ = env
        _seed(env, drip_limit=0)
        for i in range(12):
            _queue_item(database, 1, f"item-{i}", _item(id=f"item-{i}"))

        posted = []
        monkeypatch.setattr(scheduler, "post_status", lambda **kw: posted.append(kw) or {"id": "x"})

        scheduler.flush_drips()

        # Removing the limit must not dump the whole stale backlog at once.
        assert len(posted) == scheduler.DRIP_DOWNGRADE_BATCH
        assert _drip_row_count(database, 1) == 2

    def test_flush_skips_echo_with_no_room_and_keeps_payload(self, env, monkeypatch):
        database, scheduler, _ = env
        _seed(env, drip_limit=1)
        _record_success(database, item_id="item-0")
        _queue_item(database, 1, "item-1", _item())

        posted = []
        monkeypatch.setattr(scheduler, "post_status", lambda **kw: posted.append(kw) or {"id": "x"})

        scheduler.flush_drips()

        assert posted == []
        assert _state(database, 1, "item-1") == "queued"
        assert _drip_row_count(database, 1) == 1

    def test_disabled_echo_backlog_discarded(self, env, monkeypatch):
        database, scheduler, _ = env
        _seed(env, drip_limit=2, enabled=0)
        _queue_item(database, 1, "item-1", _item())

        posted = []
        monkeypatch.setattr(scheduler, "post_status", lambda **kw: posted.append(kw) or {"id": "x"})

        scheduler.flush_drips()

        # No stale burst on re-enable: the backlog is finalized as
        # gave_up and recorded in history.
        assert posted == []
        assert _state(database, 1, "item-1") == "gave_up"
        assert _drip_row_count(database, 1) == 0

    def test_failed_release_returns_item_to_queue(self, env, monkeypatch):
        database, scheduler, _ = env
        _seed(env, drip_limit=2)
        _queue_item(database, 1, "item-1", _item())

        def boom(**kw):
            raise RuntimeError("network down")

        monkeypatch.setattr(scheduler, "post_status", boom)
        scheduler.flush_drips()

        # Payload preserved: the item returns to the queue instead of the
        # feed-dependent retry path, so it survives feed rotation.
        assert _state(database, 1, "item-1") == "queued"
        assert _drip_row_count(database, 1) == 1
        with database.get_db() as db:
            attempts = db.execute(
                "SELECT attempts FROM drip_items WHERE echo_id = 1 AND item_id = 'item-1'"
            ).fetchone()["attempts"]
        assert attempts == 1

        # A later flush with a working destination delivers from the
        # stored payload, no feed fetch involved.
        posted = []
        monkeypatch.setattr(scheduler, "post_status", lambda **kw: posted.append(kw) or {"id": "x"})
        scheduler.flush_drips()
        assert len(posted) == 1
        assert _state(database, 1, "item-1") == "success"
        assert _drip_row_count(database, 1) == 0

    def test_release_gives_up_after_attempt_cap(self, env, monkeypatch):
        database, scheduler, _ = env
        _seed(env, drip_limit=5)
        _queue_item(database, 1, "item-1", _item())

        def boom(**kw):
            raise RuntimeError("network down")

        monkeypatch.setattr(scheduler, "post_status", boom)
        for _ in range(scheduler.max_attempts()):
            scheduler.flush_drips()

        assert _state(database, 1, "item-1") == "gave_up"
        assert _drip_row_count(database, 1) == 0


class TestDripAPI:
    @pytest.fixture()
    def client(self, env, monkeypatch):
        import app as app_module

        monkeypatch.setattr(app_module, "_AUTH_TOKEN", None)
        from fastapi.testclient import TestClient

        return TestClient(app_module.app)

    def _mastodon_form(self, drip_limit):
        return {
            "feed_id": "1",
            "destination_type": "mastodon",
            "account_id": "1",
            "template": "{{ title }} {{ link }}",
            "drip_limit": str(drip_limit),
        }

    def test_add_echo_persists_drip_limit(self, client, env):
        database, _, _ = env
        _seed(env)
        resp = client.post("/api/echoes", data=self._mastodon_form(5), follow_redirects=False)
        assert resp.status_code == 303
        with database.get_db() as db:
            row = db.execute(
                "SELECT drip_limit FROM echoes ORDER BY id DESC LIMIT 1"
            ).fetchone()
        assert row["drip_limit"] == 5

    def test_add_echo_rejects_negative_limit(self, client, env):
        _seed(env)
        resp = client.post("/api/echoes", data=self._mastodon_form(-1), follow_redirects=False)
        assert resp.status_code == 400

    def test_edit_echo_persists_drip_limit(self, client, env):
        database, _, _ = env
        _seed(env, drip_limit=3)
        resp = client.post("/api/echoes/1/edit", data=self._mastodon_form(7), follow_redirects=False)
        assert resp.status_code == 303
        with database.get_db() as db:
            row = db.execute("SELECT drip_limit FROM echoes WHERE id = 1").fetchone()
        assert row["drip_limit"] == 7
