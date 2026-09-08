"""Tests for keyword filters and filtered-item delivery behavior."""

import os
import tempfile

import pytest

def _item(**overrides):
    item = {
        "id": "item-1",
        "title": "Weekly giveaway roundup",
        "link": "https://example.com/post/1",
        "summary": "Our weekly roundup of giveaway deals.",
    }
    item.update(overrides)
    return item

class TestParseKeywords:
    def test_basic(self):
        from filters import parse_keywords

        assert parse_keywords("spoiler, giveaway ,nsfw") == ["spoiler", "giveaway", "nsfw"]

    def test_empty_and_none(self):
        from filters import parse_keywords

        assert parse_keywords("") == []
        assert parse_keywords(None) == []
        assert parse_keywords(" , , ") == []

class TestIsFiltered:
    def test_exclude_match_in_title(self):
        from filters import is_filtered

        assert is_filtered(_item(), "giveaway", "exclude") is True

    def test_exclude_match_in_summary(self):
        from filters import is_filtered

        assert is_filtered(_item(title="Deals post"), "roundup", "exclude") is True

    def test_exclude_case_insensitive(self):
        from filters import is_filtered

        assert is_filtered(_item(), "GIVEAWAY", "exclude") is True
        assert is_filtered(_item(title="SPOILER free"), "spoiler", "exclude") is True

    def test_exclude_no_match(self):
        from filters import is_filtered

        assert is_filtered(_item(), "nsfw, crypto", "exclude") is False

    def test_empty_filter_never_filters(self):
        from filters import is_filtered

        assert is_filtered(_item(), "", "exclude") is False
        assert is_filtered(_item(), None, None) is False
        assert is_filtered(_item(), "", "include") is False

    def test_include_mode_keeps_matches(self):
        from filters import is_filtered

        assert is_filtered(_item(), "giveaway", "include") is False

    def test_include_mode_drops_non_matches(self):
        from filters import is_filtered

        assert is_filtered(_item(), "crypto, nft", "include") is True

    def test_substring_not_word_match(self):
        from filters import is_filtered

        # 'give' is a substring of 'giveaway' — substring matching is intended
        assert is_filtered(_item(), "give", "exclude") is True

class TestSchedulerFiltering:
    def test_filtered_item_recorded_and_skips_delivery(self, db_tmp, monkeypatch):
        import database
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "post_status", lambda **kw: sent.append(kw) or {"id": "1"}
        )

        with database.get_db() as db:
            db.execute(
                "INSERT INTO accounts (name, username, instance, access_token) VALUES (?, ?, ?, ?)",
                ("main", "user", "https://mastodon.social", "tok"),
            )
            db.execute(
                "INSERT INTO feeds (name, url) VALUES (?, ?)", ("f", "https://example.com/feed")
            )
            db.execute(
                """INSERT INTO echoes (feed_id, destination_type, destination_id, template,
                                       visibility, filter_keywords, filter_mode, enabled)
                   VALUES (1, 'mastodon', 1, '{{ title }}', 'public', 'giveaway', 'exclude', 1)"""
            )
            echo = db.execute("SELECT * FROM echoes WHERE id = 1").fetchone()

        ok = scheduler.process_echo(echo, _item())
        assert ok is True
        assert sent == [], "filtered item must not be delivered"

        with database.get_db() as db:
            row = db.execute(
                "SELECT status FROM posted_items WHERE echo_id = 1 AND item_id = 'item-1'"
            ).fetchone()
        assert row is not None
        assert row["status"] == "filtered"

    def test_unfiltered_item_delivers_normally(self, db_tmp, monkeypatch):
        import database
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "post_status", lambda **kw: sent.append(kw) or {"id": "1"}
        )

        with database.get_db() as db:
            db.execute(
                "INSERT INTO accounts (name, username, instance, access_token) VALUES (?, ?, ?, ?)",
                ("main", "user", "https://mastodon.social", "tok"),
            )
            db.execute(
                "INSERT INTO feeds (name, url) VALUES (?, ?)", ("f", "https://example.com/feed")
            )
            db.execute(
                """INSERT INTO echoes (feed_id, destination_type, destination_id, template,
                                       visibility, filter_keywords, filter_mode, enabled)
                   VALUES (1, 'mastodon', 1, '{{ title }}', 'public', 'crypto', 'exclude', 1)"""
            )
            echo = db.execute("SELECT * FROM echoes WHERE id = 1").fetchone()

        ok = scheduler.process_echo(echo, _item())
        assert ok is True
        assert len(sent) == 1

        with database.get_db() as db:
            row = db.execute(
                "SELECT status FROM posted_items WHERE echo_id = 1 AND item_id = 'item-1'"
            ).fetchone()
        assert row["status"] == "success"

    def test_filtered_row_not_retried(self, db_tmp, monkeypatch):
        """A 'filtered' row must not be reclaimed as a fresh pending attempt."""
        import database
        import scheduler

        monkeypatch.setattr(
            scheduler, "post_status", lambda **kw: pytest.fail("must not deliver")
        )

        with database.get_db() as db:
            db.execute(
                "INSERT INTO accounts (name, username, instance, access_token) VALUES (?, ?, ?, ?)",
                ("main", "user", "https://mastodon.social", "tok"),
            )
            db.execute(
                "INSERT INTO feeds (name, url) VALUES (?, ?)", ("f", "https://example.com/feed")
            )
            db.execute(
                """INSERT INTO echoes (feed_id, destination_type, destination_id, template,
                                       visibility, filter_keywords, filter_mode, enabled)
                   VALUES (1, 'mastodon', 1, '{{ title }}', 'public', 'giveaway', 'exclude', 1)"""
            )
            echo = db.execute("SELECT * FROM echoes WHERE id = 1").fetchone()

        assert scheduler.process_echo(echo, _item()) is True
        # Second pass over the same item (e.g. cursor replay) stays filtered, no dup row
        assert scheduler.process_echo(echo, _item()) is True

        with database.get_db() as db:
            rows = db.execute(
                "SELECT status FROM posted_items WHERE echo_id = 1 AND item_id = 'item-1'"
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["status"] == "filtered"

    def test_filter_removed_does_not_replay(self, db_tmp, monkeypatch):
        """'filtered' rows are terminal: clearing the filter must NOT cause a
        backlog replay (that would spam destinations with old, already-seen
        items). New items deliver; old filtered rows stay filtered."""
        import database
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "post_status", lambda **kw: sent.append(kw) or {"id": "1"}
        )

        with database.get_db() as db:
            db.execute(
                "INSERT INTO accounts (name, username, instance, access_token) VALUES (?, ?, ?, ?)",
                ("main", "user", "https://mastodon.social", "tok"),
            )
            db.execute(
                "INSERT INTO feeds (name, url) VALUES (?, ?)", ("f", "https://example.com/feed")
            )
            db.execute(
                """INSERT INTO echoes (feed_id, destination_type, destination_id, template,
                                       visibility, filter_keywords, filter_mode, enabled)
                   VALUES (1, 'mastodon', 1, '{{ title }}', 'public', 'giveaway', 'exclude', 1)"""
            )

        with database.get_db() as db:
            echo = db.execute("SELECT * FROM echoes WHERE id = 1").fetchone()
        assert scheduler.process_echo(echo, _item()) is True
        assert sent == []

        # User clears the filter. The old filtered item stays suppressed even
        # if the cursor somehow replays it (no backlog dump)...
        with database.get_db() as db:
            db.execute("UPDATE echoes SET filter_keywords = '' WHERE id = 1")
            echo = db.execute("SELECT * FROM echoes WHERE id = 1").fetchone()

        result = scheduler.process_echo(echo, _item())
        assert sent == [], "previously filtered items must not replay"
        assert result is True  # treated as already handled, not a failure

        # ...while genuinely new items deliver normally.
        assert scheduler.process_echo(echo, _item(id="item-2", title="Fresh post")) is True
        assert len(sent) == 1
