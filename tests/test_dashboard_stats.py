"""Dashboard "Failed" stat (GET /) must count both 'failed' and 'gave_up'
posted_items, matching the admin usage view's 7-day failure metric
(_admin_usage). Bug review 2026-09-09 finding #12.
"""

import re
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

    monkeypatch.setattr(app_module.settings, "AUTH_TOKEN", None)
    return TestClient(app_module.app)


def _seed_feed_and_echo():
    with get_db() as db:
        db.execute(
            "INSERT INTO feeds (id, name, url, user_id) VALUES (1, 'F', 'https://example.com/f', 1)"
        )
        db.execute(
            """INSERT INTO echoes (id, feed_id, destination_type, destination_id,
                                   template, user_id, enabled)
               VALUES (1, 1, 'email', 1, '{{ t }}', 1, 1)"""
        )


def _failed_stat_from_page(html: str) -> int:
    m = re.search(
        r'stat-value">(\d+)</span>\s*<span class="stat-label">Failed</span>', html
    )
    assert m, "Failed stat card not found in dashboard HTML"
    return int(m.group(1))


class TestDashboardFailedPostsStat:
    def test_gave_up_posts_are_counted_as_failed(self, client, temp_db):
        _seed_feed_and_echo()
        with get_db() as db:
            db.execute(
                "INSERT INTO posted_items (echo_id, item_id, item_title, status) VALUES (1, 'a', 'A', 'failed')"
            )
            db.execute(
                "INSERT INTO posted_items (echo_id, item_id, item_title, status) VALUES (1, 'b', 'B', 'gave_up')"
            )
            db.execute(
                "INSERT INTO posted_items (echo_id, item_id, item_title, status) VALUES (1, 'c', 'C', 'success')"
            )
            db.execute(
                "INSERT INTO posted_items (echo_id, item_id, item_title, status) VALUES (1, 'd', 'D', 'queued')"
            )

        resp = client.get("/")
        assert resp.status_code == 200
        # 'failed' + 'gave_up' == 2; 'success' and 'queued' must not count.
        assert _failed_stat_from_page(resp.text) == 2

    def test_only_gave_up_posts_still_counted(self, client, temp_db):
        """A feed whose only failures exhausted retries (no lingering
        'failed' rows) must still surface a nonzero Failed stat."""
        _seed_feed_and_echo()
        with get_db() as db:
            db.execute(
                "INSERT INTO posted_items (echo_id, item_id, item_title, status) VALUES (1, 'a', 'A', 'gave_up')"
            )
            db.execute(
                "INSERT INTO posted_items (echo_id, item_id, item_title, status) VALUES (1, 'b', 'B', 'success')"
            )

        resp = client.get("/")
        assert resp.status_code == 200
        assert _failed_stat_from_page(resp.text) == 1
