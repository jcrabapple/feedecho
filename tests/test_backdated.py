"""Issue #13: optionally deliver backdated feed entries.

A backdated entry is one that appears positionally OLDER than the feed
cursor (so the position-based get_new_items scan skips it) but carries a
publish date inside the allowed window. Delivery is off by default; when
enabled it must never move the cursor backward, and already-posted items
must not be re-posted.
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from feed_parser import get_backdated_items, get_new_items

NOW = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)


def _item(id, days_ago=0.0):
    date = (NOW - timedelta(days=days_ago)).isoformat()
    return {"id": id, "title": f"t-{id}", "link": f"https://example.com/{id}", "date": date}


class TestGetBackdatedItems:
    def test_nothing_after_cursor_returns_empty(self):
        items = [_item("c"), _item("older", days_ago=99)]
        # cursor is the newest item; the older item is already-seen feed
        # content, but positionally after the cursor. It is outside the
        # window, so nothing is backdated.
        assert get_backdated_items(items, "c", max_days=3, now=NOW) == []

    def test_backdated_item_inside_window_found(self):
        items = [_item("newest"), _item("cursor"), _item("backdated", days_ago=1)]
        new = get_backdated_items(items, "cursor", max_days=3, now=NOW)
        assert [i["id"] for i in new] == ["backdated"]

    def test_outside_window_excluded(self):
        items = [_item("cursor"), _item("ancient", days_ago=10)]
        assert get_backdated_items(items, "cursor", max_days=3, now=NOW) == []

    def test_items_returned_oldest_first(self):
        items = [
            _item("cursor"),
            _item("b2", days_ago=1),
            _item("b1", days_ago=2),
        ]
        new = get_backdated_items(items, "cursor", max_days=3, now=NOW)
        assert [i["id"] for i in new] == ["b1", "b2"]

    def test_missing_or_unparseable_date_skipped(self):
        no_date = {"id": "x", "title": "x", "link": "l"}
        bad_date = {"id": "y", "title": "y", "link": "l", "date": "garbage-not-a-date"}
        items = [_item("cursor"), no_date, bad_date, _item("good", days_ago=1)]
        new = get_backdated_items(items, "cursor", max_days=3, now=NOW)
        assert [i["id"] for i in new] == ["good"]

    def test_non_string_date_skipped(self):
        """A non-string date (int, datetime, None) must not crash the parser."""
        items = [
            _item("cursor"),
            {"id": "int", "title": "t", "link": "l", "date": 1234567890},
            {"id": "dt", "title": "t", "link": "l", "date": datetime.now(timezone.utc)},
            {"id": "none", "title": "t", "link": "l", "date": None},
            _item("good", days_ago=0.5),
        ]
        new = get_backdated_items(items, "cursor", max_days=3, now=NOW)
        assert [i["id"] for i in new] == ["good"]

    def test_rfc822_date_parsed(self):
        item = {"id": "r", "title": "r", "link": "l",
                "date": "Fri, 28 Aug 2026 09:00:00 GMT"}
        items = [_item("cursor"), item]
        new = get_backdated_items(items, "cursor", max_days=3, now=NOW)
        assert [i["id"] for i in new] == ["r"]

    def test_naive_date_assumed_utc(self):
        item = {"id": "n", "title": "n", "link": "l",
                "date": "2026-08-28T12:00:00"}  # no tz suffix
        items = [_item("cursor"), item]
        new = get_backdated_items(items, "cursor", max_days=3, now=NOW)
        assert [i["id"] for i in new] == ["n"]

    def test_far_future_date_excluded(self):
        # More than a day ahead of now: a clock-skew-tolerant window
        # (up to 1 day future) must still reject clearly broken feeds.
        item = _item("fut", days_ago=-30)
        items = [_item("cursor"), item]
        assert get_backdated_items(items, "cursor", max_days=3, now=NOW) == []

    def test_slight_future_skew_allowed(self):
        item = _item("fut", days_ago=-0.5)  # 12h in the future
        items = [_item("cursor"), item]
        new = get_backdated_items(items, "cursor", max_days=3, now=NOW)
        assert [i["id"] for i in new] == ["fut"]

    def test_cursor_scrolled_off_skips_newest(self):
        # When the cursor is gone, get_new_items posts items[0]; the
        # backdated scan must not double-handle it.
        items = [_item("top", days_ago=1), _item("second", days_ago=2)]
        new = get_backdated_items(items, "missing-cursor", max_days=3, now=NOW)
        assert [i["id"] for i in new] == ["second"]

    def test_none_cursor_returns_empty(self):
        items = [_item("a"), _item("b")]
        assert get_backdated_items(items, None, max_days=3, now=NOW) == []

    def test_batch_cap_takes_newest_and_returns_oldest_first(self):
        # 15 items at 0.1, 0.2, ... 1.5 days ago — all within 3-day window.
        items = [_item("cursor")] + [
            _item(f"b{i}", days_ago=0.1 * (i + 1)) for i in range(15)
        ]
        new = get_backdated_items(items, "cursor", max_days=3, now=NOW, limit=10)
        assert len(new) == 10
        # The 10 newest in-window items (0.1..1.0 days), ordered oldest-first.
        assert [i["id"] for i in new] == [f"b{i}" for i in range(9, -1, -1)]

    def test_no_overlap_with_get_new_items(self):
        # Whatever get_new_items returns positionally must never be
        # returned again by the backdated scan.
        items = [_item("n1", days_ago=1), _item("cursor"), _item("bd", days_ago=2)]
        positional = get_new_items(items, "cursor")
        backdated = get_backdated_items(items, "cursor", max_days=3, now=NOW)
        ids = [i["id"] for i in positional] + [i["id"] for i in backdated]
        assert len(ids) == len(set(ids))


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


def _seed(env):
    database, _, _ = env
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
               VALUES (1, 'mastodon', 1, '{{ title }}', 'public', '', 'exclude', 1)"""
        )


def _mk_item(id, days_ago=0.0):
    date = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    return {"id": id, "title": f"t-{id}", "link": f"https://example.com/{id}",
            "summary": "", "date": date}


class TestBackdatedDelivery:
    def _run(self, env, monkeypatch, items, allow):
        import settings

        database, scheduler, _ = env
        _seed(env)
        monkeypatch.setattr(settings, "ALLOW_BACKDATED_ENTRIES", allow, raising=False)
        monkeypatch.setattr(settings, "MAX_BACKDATED_ENTRY_DAYS", 3, raising=False)
        posted = []
        monkeypatch.setattr(
            scheduler, "post_status", lambda **kw: posted.append(kw["content"]) or {"id": "p1"}
        )
        monkeypatch.setattr(scheduler, "fetch_feed", lambda url: {"items": items})
        with database.get_db() as db:
            db.execute("UPDATE feeds SET last_item_id = 'cursor' WHERE id = 1")
        scheduler.check_feed(1)
        with database.get_db() as db:
            cursor = db.execute("SELECT last_item_id FROM feeds WHERE id = 1").fetchone()[
                "last_item_id"
            ]
        return posted, cursor

    def test_flag_off_backdated_ignored(self, env, monkeypatch):
        items = [_mk_item("cursor"), _mk_item("backdated", days_ago=1)]
        posted, cursor = self._run(env, monkeypatch, items, allow=False)
        assert posted == []
        assert cursor == "cursor"

    def test_flag_on_posts_backdated_without_moving_cursor(self, env, monkeypatch):
        items = [_mk_item("cursor"), _mk_item("backdated", days_ago=1)]
        posted, cursor = self._run(env, monkeypatch, items, allow=True)
        assert any("t-backdated" in p for p in posted)
        assert cursor == "cursor", "backdated delivery must not move the cursor"

    def test_flag_on_no_new_items_last_fetched_still_updated(self, env, monkeypatch):
        database, scheduler, _ = env
        # Feed has only the cursor (no positional new items), but a backdated
        # item exists. last_fetched must still be updated.
        items = [_mk_item("cursor"), _mk_item("backdated", days_ago=1)]
        _seed(env)
        import settings

        monkeypatch.setattr(settings, "ALLOW_BACKDATED_ENTRIES", True, raising=False)
        monkeypatch.setattr(settings, "MAX_BACKDATED_ENTRY_DAYS", 3, raising=False)
        monkeypatch.setattr(
            scheduler, "post_status", lambda **kw: {"id": "p1"}
        )
        monkeypatch.setattr(scheduler, "fetch_feed", lambda url: {"items": items})

        with database.get_db() as db:
            db.execute("UPDATE feeds SET last_item_id = 'cursor' WHERE id = 1")
        scheduler.check_feed(1)
        with database.get_db() as db:
            row = db.execute("SELECT last_fetched FROM feeds WHERE id = 1").fetchone()
        assert row["last_fetched"], "last_fetched must be set even with only backdated items"
        # And the backdated item was delivered
        with database.get_db() as db:
            pi = db.execute(
                "SELECT status FROM posted_items WHERE item_id = 'backdated'"
            ).fetchone()
        assert pi and pi["status"] == "success"

    def test_already_posted_item_not_reposted(self, env, monkeypatch):
        database, scheduler, _ = env
        items = [_mk_item("cursor"), _mk_item("backdated", days_ago=1)]
        _seed(env)
        with database.get_db() as db:
            db.execute(
                "UPDATE feeds SET last_item_id = 'cursor' WHERE id = 1"
            )
            db.execute(
                """INSERT INTO posted_items (echo_id, item_id, item_title, item_url,
                                             status, posted_at)
                   VALUES (1, 'backdated', 't-backdated', 'https://example.com/backdated',
                           'success', '2026-08-29 00:00:00')"""
            )
        import settings

        monkeypatch.setattr(settings, "ALLOW_BACKDATED_ENTRIES", True, raising=False)
        monkeypatch.setattr(settings, "MAX_BACKDATED_ENTRY_DAYS", 3, raising=False)
        posted = []
        monkeypatch.setattr(
            scheduler, "post_status", lambda **kw: posted.append(kw["content"]) or {"id": "p1"}
        )
        monkeypatch.setattr(scheduler, "fetch_feed", lambda url: {"items": items})
        scheduler.check_feed(1)
        assert posted == []

    def test_positional_and_backdated_both_delivered(self, env, monkeypatch):
        items = [
            _mk_item("newest", days_ago=0.5),
            _mk_item("cursor"),
            _mk_item("backdated", days_ago=1),
        ]
        posted, cursor = self._run(env, monkeypatch, items, allow=True)
        assert any("t-newest" in p for p in posted)
        assert any("t-backdated" in p for p in posted)
        # Cursor advanced ONLY positionally to the newest item.
        assert cursor == "newest"

    def test_backdated_failure_does_not_block_later_items(self, env, monkeypatch):
        database, scheduler, _ = env
        items = [_mk_item("cursor"), _mk_item("bd-a", days_ago=1), _mk_item("bd-b", days_ago=1.5)]
        _seed(env)
        import settings

        monkeypatch.setattr(settings, "ALLOW_BACKDATED_ENTRIES", True, raising=False)
        monkeypatch.setattr(settings, "MAX_BACKDATED_ENTRY_DAYS", 3, raising=False)

        def flaky(**kw):
            if "t-bd-a" in kw["content"]:
                raise RuntimeError("boom")
            return {"id": "p1"}

        monkeypatch.setattr(scheduler, "post_status", flaky)
        monkeypatch.setattr(scheduler, "fetch_feed", lambda url: {"items": items})
        with database.get_db() as db:
            db.execute("UPDATE feeds SET last_item_id = 'cursor' WHERE id = 1")
        scheduler.check_feed(1)  # must not raise
        with database.get_db() as db:
            rows = db.execute(
                "SELECT item_id, status FROM posted_items ORDER BY item_id"
            ).fetchall()
        states = {r["item_id"]: r["status"] for r in rows}
        assert states.get("bd-a") == "failed", "failed backdated row is retryable"
        assert states.get("bd-b") == "success", "failure must not stop other backdated items"
        # cursor untouched
        with database.get_db() as db:
            cursor = db.execute("SELECT last_item_id FROM feeds WHERE id = 1").fetchone()[
                "last_item_id"
            ]
        assert cursor == "cursor"
