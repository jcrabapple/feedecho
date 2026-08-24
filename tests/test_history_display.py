"""History page display fixes (issues #4 and #5).

#4: timestamps render as <time datetime="...Z"> with a UTC-labelled
fallback so the browser can convert to the viewer's timezone.
#5: drip-held ('queued') and in-flight ('pending') rows must not be
shown as failures.
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

    monkeypatch.setattr(app_module.settings, "AUTH_TOKEN", None)
    return TestClient(app_module.app)


def _seed():
    with get_db() as db:
        db.execute(
            "INSERT INTO feeds (name, url) VALUES (?, ?)",
            ("Test Feed", "https://example.com/feed.xml"),
        )
        db.execute(
            "INSERT INTO accounts (name, username, instance, access_token) "
            "VALUES (?, ?, ?, ?)",
            ("Test", "test", "https://example.com", "token"),
        )
        db.execute(
            "INSERT INTO echoes (feed_id, destination_type, destination_id, template) "
            "VALUES (?, ?, ?, ?)",
            (1, "mastodon", 1, "{{ title }}"),
        )


def _add_post(status, item_id, posted_at: str | None = "2026-08-24 06:46:00", **extra):
    with get_db() as db:
        db.execute(
            "INSERT INTO posted_items (echo_id, item_id, item_title, item_url,"
            " status, posted_at, attempt_count, next_retry_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (1, item_id, f"Item {item_id}", "https://example.com/item",
             status, posted_at, extra.get("attempt_count", 0),
             extra.get("next_retry_at")),
        )


def _history(client):
    return client.get("/history").text


class TestStatusRendering:
    def test_queued_shows_queued_not_failed(self, client, temp_db):
        _seed()
        _add_post("queued", "q-1")
        page = _history(client)
        assert "Queued" in page
        assert "held for drip rate limit" in page
        assert '<span class="badge badge-danger">Failed</span>' not in page

    def test_pending_shows_pending_not_failed(self, client, temp_db):
        _seed()
        _add_post("pending", "p-1")
        page = _history(client)
        assert "Pending" in page
        assert '<span class="badge badge-danger">Failed</span>' not in page

    def test_failed_still_renders_failed(self, client, temp_db):
        _seed()
        _add_post("failed", "f-1", attempt_count=2,
                  next_retry_at="2026-08-24 07:46:00")
        page = _history(client)
        assert '<span class="badge badge-danger">Failed</span>' in page
        assert "attempt 2" in page

    def test_terminal_statuses_unchanged(self, client, temp_db):
        _seed()
        _add_post("success", "s-1")
        _add_post("filtered", "fl-1")
        _add_post("gave_up", "g-1")
        page = _history(client)
        assert '<span class="badge badge-success">Success</span>' in page
        assert '<span class="badge badge-warning">Filtered</span>' in page
        assert '<span class="badge badge-danger">Gave up</span>' in page


class TestTimestampRendering:
    def test_posted_at_has_iso_datetime_and_utc_fallback(self, client, temp_db):
        _seed()
        _add_post("success", "s-1", posted_at="2026-08-24 06:46:00")
        page = _history(client)
        # Machine-readable ISO-8601 UTC attribute for browser-side conversion
        assert 'datetime="2026-08-24T06:46:00Z"' in page
        # Honest fallback text when JS is unavailable
        assert "2026-08-24 06:46:00 UTC" in page

    def test_retry_timestamp_has_iso_datetime(self, client, temp_db):
        _seed()
        _add_post("failed", "f-1", attempt_count=1,
                  next_retry_at="2026-08-24 07:46:00")
        page = _history(client)
        assert 'datetime="2026-08-24T07:46:00Z"' in page
        assert "retry" in page

    def test_time_element_uses_local_time_class(self, client, temp_db):
        _seed()
        _add_post("success", "s-1")
        page = _history(client)
        assert 'class="local-time"' in page

    def test_null_posted_at_does_not_500(self, client, temp_db):
        _seed()
        _add_post("queued", "q-1", posted_at=None)
        resp = client.get("/history")
        assert resp.status_code == 200
        assert "Queued" in resp.text


class TestDashboardStatusRendering:
    def test_queued_not_failed_on_dashboard(self, client, temp_db):
        _seed()
        _add_post("queued", "q-1")
        resp = client.get("/")
        assert resp.status_code == 200
        assert "Queued" in resp.text
        assert '<span class="badge badge-danger">Failed</span>' not in resp.text

    def test_dashboard_timestamp_has_iso_datetime(self, client, temp_db):
        _seed()
        _add_post("success", "s-1", posted_at="2026-08-24 06:46:00")
        page = client.get("/").text
        assert 'datetime="2026-08-24T06:46:00Z"' in page
