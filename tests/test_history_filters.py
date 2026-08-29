"""Filtering post history by feed and destination (issue #16).

The page shows the newest 100 rows across everything, so "which items went to
this one feed / this one account" was unanswerable once a busy instance filled
that window. Filtering therefore happens in SQL, before the LIMIT.
"""

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from database import get_db, init_db


@pytest.fixture
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        monkeypatch.setattr("database.DB_PATH", db_path)
        init_db()
        yield db_path


@pytest.fixture
def client(temp_db, monkeypatch):
    import app as app_module

    # A FEEDECHO_AUTH_TOKEN in the ambient shell would 401 every request here.
    monkeypatch.setattr(app_module.settings, "AUTH_TOKEN", None)
    return TestClient(app_module.app)


def _seed(user_id: int = 1):
    """Two feeds crossed with two destinations of different types.

    feed 1 -> mastodon 1  ("Alpha to Mastodon")
    feed 2 -> mastodon 1  ("Beta to Mastodon")
    feed 1 -> bluesky 1   ("Alpha to Bluesky")

    Plus a feed with no history at all, which must not appear in the dropdown.
    """
    with get_db() as db:
        db.execute(
            "INSERT INTO feeds (name, url, user_id) VALUES (?, ?, ?)",
            ("Alpha Feed", "https://example.com/alpha.xml", user_id),
        )
        db.execute(
            "INSERT INTO feeds (name, url, user_id) VALUES (?, ?, ?)",
            ("Beta Feed", "https://example.com/beta.xml", user_id),
        )
        db.execute(
            "INSERT INTO feeds (name, url, user_id) VALUES (?, ?, ?)",
            ("Silent Feed", "https://example.com/silent.xml", user_id),
        )
        db.execute(
            "INSERT INTO accounts (name, username, instance, access_token, user_id) "
            "VALUES (?, ?, ?, ?, ?)",
            ("Masto", "alice", "https://mastodon.example", "token", user_id),
        )
        db.execute(
            "INSERT INTO bluesky_accounts (name, handle, app_password, pds, user_id) "
            "VALUES (?, ?, ?, ?, ?)",
            ("Bsky", "alice.bsky.social", "pw", "https://bsky.social", user_id),
        )
        for feed_id, dest_type, dest_id in (
            (1, "mastodon", 1),
            (2, "mastodon", 1),
            (1, "bluesky", 1),
        ):
            db.execute(
                "INSERT INTO echoes (feed_id, destination_type, destination_id, "
                "template, user_id) VALUES (?, ?, ?, ?, ?)",
                (feed_id, dest_type, dest_id, "{{ title }}", user_id),
            )
        for echo_id, title in (
            (1, "Alpha to Mastodon"),
            (2, "Beta to Mastodon"),
            (3, "Alpha to Bluesky"),
        ):
            db.execute(
                "INSERT INTO posted_items (echo_id, item_id, item_title, item_url, "
                "status) VALUES (?, ?, ?, ?, ?)",
                (echo_id, f"item-{echo_id}", title, f"https://example.com/{echo_id}", "success"),
            )


class TestUnfiltered:
    def test_all_rows_and_filter_bar(self, client, temp_db):
        _seed()
        body = client.get("/history").text
        assert "Alpha to Mastodon" in body
        assert "Beta to Mastodon" in body
        assert "Alpha to Bluesky" in body
        # The bar itself, with both dropdowns and their "all" defaults.
        assert 'id="history-feed"' in body
        assert 'id="history-account"' in body
        assert "All feeds" in body
        assert "All destinations" in body
        # Nothing to clear when nothing is filtered.
        assert 'class="filter-clear"' not in body

    def test_options_come_from_history_only(self, client, temp_db):
        _seed()
        body = client.get("/history").text
        assert 'value="1"' in body and "Alpha Feed" in body
        assert "Beta Feed" in body
        # A feed that never posted has nothing to filter and stays out.
        assert "Silent Feed" not in body

    def test_no_history_shows_plain_empty_state(self, client, temp_db):
        body = client.get("/history").text
        assert "No posts yet." in body
        assert 'id="history-feed"' not in body


class TestFeedFilter:
    def test_filters_by_feed(self, client, temp_db):
        _seed()
        body = client.get("/history?feed=1").text
        assert "Alpha to Mastodon" in body
        assert "Alpha to Bluesky" in body
        assert "Beta to Mastodon" not in body

    def test_selection_is_reflected(self, client, temp_db):
        _seed()
        body = client.get("/history?feed=2").text
        assert '<option value="2" selected>Beta Feed</option>' in body
        assert 'class="filter-clear"' in body


class TestDestinationFilter:
    def test_filters_by_destination(self, client, temp_db):
        _seed()
        body = client.get("/history?account=mastodon:1").text
        assert "Alpha to Mastodon" in body
        assert "Beta to Mastodon" in body
        assert "Alpha to Bluesky" not in body

    def test_destination_type_is_part_of_the_key(self, client, temp_db):
        """mastodon:1 and bluesky:1 are different destinations, same id."""
        _seed()
        body = client.get("/history?account=bluesky:1").text
        assert "Alpha to Bluesky" in body
        assert "Alpha to Mastodon" not in body

    def test_both_filters_combine(self, client, temp_db):
        _seed()
        body = client.get("/history?feed=1&account=mastodon:1").text
        assert "Alpha to Mastodon" in body
        assert "Alpha to Bluesky" not in body
        assert "Beta to Mastodon" not in body

    def test_disconnected_destination_still_listed(self, client, temp_db):
        """History outlives the account it was sent to (soft-delete rules).

        Its label has nothing left to join to, so it falls back to type + id
        rather than rendering a blank option nobody can pick.
        """
        _seed()
        with get_db() as db:
            db.execute("DELETE FROM bluesky_accounts WHERE id = 1")
        body = client.get("/history").text
        assert "bluesky #1 (removed)" in body
        assert "Alpha to Bluesky" in body
        filtered = client.get("/history?account=bluesky:1").text
        assert "Alpha to Bluesky" in filtered
        assert "Alpha to Mastodon" not in filtered


class TestFilterEdgeCases:
    @pytest.mark.parametrize(
        "query",
        [
            "feed=abc",
            "feed=",
            "feed=1;DROP TABLE posted_items",
            "feed=1 OR 1=1",
            # str.isdigit() is True for these but int() rejects them — the
            # first cut of this filter 500'd on both (caught in review).
            "feed=%C2%B2",
            "feed=%E2%91%A0",
            "account=mastodon:%C2%B2",
            "account=bogus:1",
            "account=mastodon:abc",
            "account=mastodon",
            "account=",
            "account=:",
        ],
    )
    def test_malformed_filters_are_ignored_not_errors(self, client, temp_db, query):
        _seed()
        resp = client.get(f"/history?{query}")
        assert resp.status_code == 200
        # Unfiltered view: every row still there, nothing to clear.
        assert "Alpha to Mastodon" in resp.text
        assert "Beta to Mastodon" in resp.text
        assert "Alpha to Bluesky" in resp.text
        assert 'class="filter-clear"' not in resp.text
        with get_db() as db:
            rows = db.execute("SELECT COUNT(*) as c FROM posted_items").fetchone()["c"]
        assert rows == 3

    def test_filter_with_no_matches(self, client, temp_db):
        _seed()
        body = client.get("/history?feed=1&account=bluesky:99").text
        assert "No posts match this filter." in body
        # The bar stays up so the filter can be changed or cleared.
        assert 'id="history-feed"' in body

    def test_other_users_history_is_not_reachable(self, client, temp_db):
        """Single mode is user 1; another tenant's rows must not leak.

        Both the rows and the dropdown options are scoped by echoes.user_id,
        so a guessed feed id cannot pull someone else's history into view.
        """
        _seed(user_id=1)
        with get_db() as db:
            db.execute(
                "INSERT INTO feeds (name, url, user_id) VALUES (?, ?, ?)",
                ("Other Tenant Feed", "https://example.com/other.xml", 2),
            )
            db.execute(
                "INSERT INTO accounts (name, username, instance, access_token, user_id) "
                "VALUES (?, ?, ?, ?, ?)",
                ("Other", "bob", "https://other.example", "token", 2),
            )
            db.execute(
                "INSERT INTO echoes (feed_id, destination_type, destination_id, "
                "template, user_id) VALUES (?, ?, ?, ?, ?)",
                (4, "mastodon", 2, "{{ title }}", 2),
            )
            db.execute(
                "INSERT INTO posted_items (echo_id, item_id, item_title, item_url, "
                "status) VALUES (?, ?, ?, ?, ?)",
                (4, "item-other", "Other Tenant Post", "https://example.com/x", "success"),
            )
        unfiltered = client.get("/history").text
        assert "Other Tenant Post" not in unfiltered
        assert "Other Tenant Feed" not in unfiltered
        targeted = client.get("/history?feed=4&account=mastodon:2").text
        assert "Other Tenant Post" not in targeted
