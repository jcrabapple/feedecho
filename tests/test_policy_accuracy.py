"""Legal/marketing pages must match what the code actually does.

Origin: external review 2026-09-22 — the About and Privacy pages made claims
the code contradicts:

- "Feed content is not archived" — false: feed_items stores title, summary,
  content, sanitized HTML, author, image URLs and read/starred state (up to
  READER_MAX_ITEMS_PER_FEED unstarred per feed; starred items are kept).
- "Query strings are never logged" — false in practice: the app's own access
  logger is path-only, but uvicorn's built-in access log records the full
  request line (reset/verify tokens to stdout) unless --no-access-log is set.
- The access log also records client IP and account id — personal data the
  policy did not disclose.
- Privacy said deleting a feed "removes access to its history" while About
  said it "keeps its history"; the code keeps post history.

These tests pin the corrected behavior and text so the pages cannot drift
from the code again. /about, /terms and /privacy are hosted-only pages, so
the page-content tests run in multi mode; the deletion behavior test runs in
single mode (feed deletion works there too).
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import database
import settings
from app import app
from database import get_db

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def multi_client(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MULTI", True)
    monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
    monkeypatch.setattr(settings, "STATE_SECRET", "s" * 40)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "policy.db")
    database.init_db()
    return TestClient(app)


@pytest.fixture()
def single_client(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MULTI", False)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "policy-single.db")
    database.init_db()
    return TestClient(app)


class TestAccessLogging:
    def test_uvicorn_builtin_access_log_disabled(self):
        # uvicorn's access log records the full request line (query string
        # included), which leaks password-reset and email-verification tokens
        # to stdout. The app's own middleware access log is path-only, so the
        # built-in one must be off in the image.
        dockerfile = (REPO_ROOT / "Dockerfile").read_text()
        assert "--no-access-log" in dockerfile

    def test_uvicorn_access_logger_silenced_for_source_installs(self):
        # Source installs (systemd) don't pass --no-access-log, so the app
        # silences uvicorn's query-string-leaking access logger itself at
        # import time (logging_setup runs after uvicorn's own config).
        import logging

        import app as _app  # noqa: F401 — logging_setup runs at import

        assert (
            logging.getLogger("uvicorn.access").getEffectiveLevel()
            >= logging.CRITICAL
        )

    def test_about_discloses_ip_logging(self, multi_client):
        page = multi_client.get("/about")
        assert page.status_code == 200
        assert "never query strings" in page.text
        # IPs are personal data under GDPR; the page must disclose them.
        assert "IP address" in page.text

    def test_privacy_discloses_ip_logging(self, multi_client):
        page = multi_client.get("/privacy")
        assert page.status_code == 200
        assert "IP address" in page.text


class TestFeedContentStorageClaims:
    def test_about_does_not_claim_content_discarded(self, multi_client):
        page = multi_client.get("/about").text
        assert "not archived" not in page
        assert "discarded" not in page

    def test_privacy_does_not_claim_content_discarded(self, multi_client):
        page = multi_client.get("/privacy").text
        assert "not archived" not in page
        assert "discarded" not in page

    def test_privacy_collect_table_covers_reader_data(self, multi_client):
        page = multi_client.get("/privacy").text
        assert "starred" in page
        assert "saved searches" in page
        assert "folders" in page.lower()


class TestDeletionSemantics:
    def test_pages_do_not_claim_delete_removes_history_access(self, multi_client):
        privacy = multi_client.get("/privacy").text
        assert "removes access to its history" not in privacy

    def test_delete_feed_blanks_url_and_drops_items(self, single_client):
        # Feed URLs often embed private tokens; a soft-deleted row keeping the
        # URL (and 200 stored items) forever is a leak. Deletion must blank
        # the URL and drop the feed's stored items while leaving the
        # posted_items audit trail alone.
        with get_db() as db:
            db.execute(
                "INSERT INTO feeds (name, url, poll_interval) VALUES (?, ?, ?)",
                ("Tok", "https://example.com/feed.xml?token=SECRET", 15),
            )
            db.execute(
                "INSERT INTO feed_items (feed_id, item_id, title)"
                " VALUES (1, 'i1', 'Item One')"
            )
        resp = single_client.post("/api/feeds/1/delete", follow_redirects=False)
        assert resp.status_code in (302, 303)
        with get_db() as db:
            feed = db.execute("SELECT * FROM feeds WHERE id = 1").fetchone()
            assert feed["deleted_at"] is not None
            assert feed["url"] == ""
            c = db.execute(
                "SELECT COUNT(*) AS c FROM feed_items WHERE feed_id = 1"
            ).fetchone()
            assert c["c"] == 0

    def test_no_template_invites_email_replies(self, multi_client):
        # System mail comes from a no-reply address; replies are unroutable,
        # so no page may tell users to reply to service email.
        for path in ("/about", "/privacy"):
            page = multi_client.get(path).text
            assert "reply to any email" not in page
