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
        for path in ("/about", "/privacy", "/terms"):
            page = multi_client.get(path).text
            assert "reply to any email" not in page

    def test_delete_feed_cannot_purge_another_tenants_items(self, multi_client):
        # IDOR regression: feed_items has no user_id column, so the purge
        # must be contingent on the scoped soft-delete actually matching.
        # User 2's feed must keep its items when user 1 (or a stranger)
        # targets its id.
        import security

        with get_db() as db:
            db.execute(
                "INSERT INTO users (id, email, password_hash, email_verified)"
                " VALUES (2, 'victim@example.com', '', 1)"
            )
            db.execute(
                "INSERT INTO feeds (id, name, url, user_id) VALUES (7, 'V', 'https://v.example.com/x', 2)"
            )
            db.execute(
                "INSERT INTO feed_items (feed_id, item_id, title)"
                " VALUES (7, 'i1', 'Victim Item')"
            )
        # No session at all -> must not reach the handler's DB writes.
        r = multi_client.post("/api/feeds/7/delete", follow_redirects=False)
        assert r.status_code in (302, 303, 401, 403)
        # Sign in as a DIFFERENT user (id 1, the default local user) and try.
        multi_client.cookies.set(
            "feedecho_session", security.sign_session(1, "local@example.com")
        )
        multi_client.post("/api/feeds/7/delete", follow_redirects=False)
        with get_db() as db:
            feed = db.execute("SELECT * FROM feeds WHERE id = 7").fetchone()
            assert feed["deleted_at"] is None, "another tenant's feed was deleted"
            assert feed["url"] == "https://v.example.com/x"
            c = db.execute(
                "SELECT COUNT(*) AS c FROM feed_items WHERE feed_id = 7"
            ).fetchone()
            assert c["c"] == 1, "another tenant's feed_items were purged"

    def test_delete_feed_finalizes_queued_posts(self, single_client):
        # A queued curation post for a feed must never send after the feed is
        # deleted (its stored item rows are gone, so the flush would dispatch
        # blank content).
        with get_db() as db:
            db.execute(
                "INSERT INTO feeds (name, url) VALUES ('Q', 'https://q.example.com/x')"
            )
            db.execute(
                "INSERT INTO queued_posts (feed_id, item_id, destination_type,"
                " destination_id, content, scheduled_at)"
                " VALUES (1, 'i1', 'mastodon', 1, 'body', '2020-01-01 00:00:00')"
            )
        single_client.post("/api/feeds/1/delete", follow_redirects=False)
        with get_db() as db:
            row = db.execute("SELECT * FROM queued_posts WHERE id = 1").fetchone()
            assert row["status"] == "failed"
            assert "Feed deleted" in row["error_message"]

    def test_flush_queue_finalizes_posts_for_deleted_feed(self, single_client):
        # Defense in depth: even a row that slips past delete_feed (deleted
        # before this shipped, or claimed in the race window) is finalized by
        # the flush, never dispatched.
        from scheduler import flush_queue

        with get_db() as db:
            db.execute(
                "INSERT INTO feeds (name, url, deleted_at)"
                " VALUES ('D', '', '2026-01-01 00:00:00')"
            )
            db.execute(
                "INSERT INTO queued_posts (feed_id, item_id, destination_type,"
                " destination_id, content, scheduled_at)"
                " VALUES (1, 'i1', 'mastodon', 1, 'body', '2020-01-01 00:00:00')"
            )
        flush_queue()
        with get_db() as db:
            row = db.execute("SELECT * FROM queued_posts WHERE id = 1").fetchone()
            assert row["status"] == "failed"
            assert "Feed deleted" in row["error_message"]

    def test_lifespan_resilences_uvicorn_access(self, monkeypatch, tmp_path):
        # `python app.py` / uvicorn.run() calls configure_logging() at server
        # start, AFTER import, resetting uvicorn.access to INFO. The lifespan
        # must re-silence it so the launch path cannot reopen the token leak.
        # The DB is isolated to tmp_path so the lifespan's init_db() never
        # touches real state.
        import logging

        monkeypatch.setattr(settings, "MULTI", False)
        monkeypatch.setattr(settings, "AUTH_TOKEN", "lifespan-test-token")
        monkeypatch.setattr(settings, "DATABASE_URL", "")
        monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
        monkeypatch.setattr(database, "DB_PATH", tmp_path / "lifespan.db")

        logging.getLogger("uvicorn.access").setLevel(logging.INFO)
        with TestClient(app):
            assert (
                logging.getLogger("uvicorn.access").getEffectiveLevel()
                >= logging.CRITICAL
            )

    def test_flush_queue_finalizes_row_with_purged_item_for_deleted_feed(
        self, single_client, monkeypatch
    ):
        # A queued row whose feed_items row is gone AND whose feed was
        # deleted must finalize, never dispatch. (A pruned feed_items row on
        # a LIVE feed still dispatches — the queue row carries its own
        # materialized content; test_queue.py pins that.)
        import scheduler

        calls = []

        def _fake_process_echo(*args, **kwargs):
            calls.append(args)
            return True

        monkeypatch.setattr(scheduler, "process_echo", _fake_process_echo)
        with get_db() as db:
            db.execute(
                "INSERT INTO feeds (name, url, deleted_at)"
                " VALUES ('D', '', '2026-01-01 00:00:00')"
            )
            db.execute(
                "INSERT INTO queued_posts (feed_id, feed_item_id, item_id,"
                " destination_type, destination_id, content, scheduled_at)"
                " VALUES (1, 424242, 'i1', 'mastodon', 1, 'body', '2020-01-01 00:00:00')"
            )

        scheduler.flush_queue()

        with get_db() as db:
            row = db.execute("SELECT * FROM queued_posts WHERE id = 1").fetchone()
            assert row["status"] == "failed"
            assert "Feed deleted" in row["error_message"]
        assert calls == []


class TestDisclosureBatch:
    """Second external-review batch (2026-09-23): disclosure gaps."""

    def test_privacy_names_transactional_email_provider(self, multi_client):
        page = multi_client.get("/privacy").text
        assert "Mailgun" in page

    def test_privacy_discloses_ai_alt_text_provider(self, multi_client):
        # Users can configure their own OpenAI-compatible vision API key;
        # attached images then go to that provider, and the key is stored
        # encrypted. Both the stored credential and the data flow must be
        # disclosed.
        page = multi_client.get("/privacy").text
        assert "alt text" in page
        assert "vision" in page.lower()
        assert "API key" in page

    def test_about_discloses_ai_alt_text_key(self, multi_client):
        page = multi_client.get("/about").text
        assert "vision" in page.lower()

    def test_privacy_connected_platforms_line_includes_telegram(self, multi_client):
        page = multi_client.get("/privacy").text
        assert (
            "Mastodon, Bluesky, micro.blog, your Matrix homeserver, Discord,"
            " Telegram, your email provider"
        ) in page

    def test_privacy_revoke_list_covers_all_platforms(self, multi_client):
        page = multi_client.get("/privacy").text
        assert "Discord, or Telegram settings" in page

    def test_privacy_sell_rent_wording_points_at_disclosures(self, multi_client):
        page = multi_client.get("/privacy").text
        assert "don't sell, rent, or share" not in page
        assert "disclosed only as described" in page

    def test_about_sell_rent_wording_points_at_disclosures(self, multi_client):
        page = multi_client.get("/about").text
        assert "disclosed only as described" in page

    def test_privacy_discloses_us_transfer_for_eea_uk(self, multi_client):
        page = multi_client.get("/privacy").text
        assert "transferred to and stored in the United States" in page
        # Bare acknowledgement is not a GDPR Chapter V hook; the policy must
        # name the contract-necessity derogation.
        assert "Art. 49(1)(b)" in page

    def test_privacy_discloses_backup_retention(self, multi_client):
        # Account deletion removes live data, but encrypted backups retain it
        # until they rotate out — the window must be stated.
        page = multi_client.get("/privacy").text
        assert "backup" in page.lower()
        assert "8 weeks" in page

    def test_about_discloses_backup_retention(self, multi_client):
        page = multi_client.get("/about").text
        assert "backup" in page.lower()

    def test_privacy_discloses_booster(self, multi_client):
        # BOOSTER_URL is set in production: boosted posts' public URLs go to
        # the FeedBooster service, so it must be disclosed.
        page = multi_client.get("/privacy").text
        assert "FeedBooster" in page

    def test_about_discloses_booster(self, multi_client):
        page = multi_client.get("/about").text
        assert "FeedBooster" in page

    def test_terms_states_auto_renewal_and_how_to_cancel(self, multi_client):
        page = multi_client.get("/terms").text
        assert "renews automatically" in page
        assert "Manage subscription" in page

    def test_pricing_states_auto_renewal(self, multi_client):
        page = multi_client.get("/pricing").text
        assert "renews automatically" in page

    def test_no_google_fonts_claim_remains(self):
        # The font has been self-hosted since v1.44.0; no page may claim
        # Google Fonts. (Second-batch reviewer had a stale snapshot, but pin
        # it so it can never come back.)
        for tpl in sorted((REPO_ROOT / "templates").glob("*.html")):
            assert "Google Fonts" not in tpl.read_text(), tpl.name
