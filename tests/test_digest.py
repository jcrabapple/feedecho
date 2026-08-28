"""Tests for digest/batch email delivery mode."""

import os
import tempfile

import pytest


@pytest.fixture()
def db_tmp(monkeypatch):
    """Point the DB layer at a fresh temp file per test."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)

    import database

    monkeypatch.setattr(database, "DB_PATH", database.Path(path))
    database.init_db()

    import scheduler

    monkeypatch.setattr(scheduler, "get_db", database.get_db)

    yield database

    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except OSError:
            pass


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


def _setup_email_echo(db_tmp, echo_overrides=None):
    """Create a test email account, feed, and echo. Returns the echo row."""
    echo_kwargs = {
        "destination_type": "email",
        "destination_id": 1,
        "template": "{{ title }} — {{ link }}",
        "visibility": "public",
        "filter_keywords": "",
        "filter_mode": "exclude",
        "content_warning": "",
        "attach_image": 0,
        "delivery_mode": "digest",
        "enabled": 1,
    }
    if echo_overrides:
        echo_kwargs.update(echo_overrides)

    with db_tmp.get_db() as db:
        db.execute(
            "INSERT INTO email_accounts (name, email) VALUES (?, ?)",
            ("Test User", "test@example.com"),
        )
        db.execute(
            "INSERT INTO feeds (name, url) VALUES (?, ?)",
            ("f", "https://example.com/feed"),
        )
        db.execute(
            """INSERT INTO echoes (feed_id, destination_type, destination_id, template,
                                   visibility, filter_keywords, filter_mode,
                                   content_warning, attach_image, delivery_mode, enabled)
               VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                echo_kwargs["destination_type"],
                echo_kwargs["destination_id"],
                echo_kwargs["template"],
                echo_kwargs["visibility"],
                echo_kwargs["filter_keywords"],
                echo_kwargs["filter_mode"],
                echo_kwargs["content_warning"],
                echo_kwargs["attach_image"],
                echo_kwargs["delivery_mode"],
                echo_kwargs["enabled"],
            ),
        )
        return db.execute("SELECT * FROM echoes WHERE id = 1").fetchone()


# ── Queue for Digest Tests ───────────────────────────────────────────────────


class TestDigestQueueing:
    def test_digest_mode_queues_item(self, db_tmp, monkeypatch):
        """In digest mode, items are stored in digest_items, not sent immediately."""
        import scheduler

        sent_emails = []
        monkeypatch.setattr(
            scheduler, "send_email", lambda **kw: sent_emails.append(kw) or {"success": True}
        )

        echo = _setup_email_echo(db_tmp)
        scheduler.process_echo(echo, _item())

        # No email should be sent immediately
        assert len(sent_emails) == 0

        # Item should be in digest_items
        with db_tmp.get_db() as db:
            rows = db.execute("SELECT * FROM digest_items WHERE echo_id = 1").fetchall()
        assert len(rows) == 1
        assert rows[0]["item_id"] == "item-1"
        assert rows[0]["item_title"] == "Test Post"
        assert "Test Post" in rows[0]["rendered_content"]

    def test_digest_mode_posts_status_queued(self, db_tmp, monkeypatch):
        """In digest mode, posted_items should have status 'queued'."""
        import scheduler

        monkeypatch.setattr(scheduler, "send_email", lambda **kw: {"success": True})

        echo = _setup_email_echo(db_tmp)
        scheduler.process_echo(echo, _item())

        with db_tmp.get_db() as db:
            row = db.execute(
                "SELECT status FROM posted_items WHERE echo_id = 1 AND item_id = 'item-1'"
            ).fetchone()
        assert row is not None
        assert row["status"] == "queued"

    def test_instant_mode_sends_immediately(self, db_tmp, monkeypatch):
        """In instant mode, items are sent immediately, not queued."""
        import scheduler

        sent_emails = []
        monkeypatch.setattr(
            scheduler, "send_email", lambda **kw: sent_emails.append(kw) or {"success": True}
        )

        echo = _setup_email_echo(db_tmp, {"delivery_mode": "instant"})
        scheduler.process_echo(echo, _item())

        assert len(sent_emails) == 1
        with db_tmp.get_db() as db:
            rows = db.execute("SELECT * FROM digest_items").fetchall()
        assert len(rows) == 0

    def test_queued_status_is_terminal(self, db_tmp, monkeypatch):
        """'queued' status must be terminal so the cursor advances."""
        import scheduler

        monkeypatch.setattr(scheduler, "send_email", lambda **kw: {"success": True})

        echo = _setup_email_echo(db_tmp)
        assert scheduler.process_echo(echo, _item()) is True

        # Second pass should not re-queue
        assert scheduler.process_echo(echo, _item()) is True

        with db_tmp.get_db() as db:
            rows = db.execute(
                "SELECT status FROM posted_items WHERE echo_id = 1 AND item_id = 'item-1'"
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["status"] == "queued"

    def test_multiple_items_queue_separately(self, db_tmp, monkeypatch):
        """Multiple items in digest mode each get their own digest_items row."""
        import scheduler

        monkeypatch.setattr(scheduler, "send_email", lambda **kw: {"success": True})

        echo = _setup_email_echo(db_tmp)
        scheduler.process_echo(echo, _item(id="item-1", title="First Post"))
        scheduler.process_echo(echo, _item(id="item-2", title="Second Post"))
        scheduler.process_echo(echo, _item(id="item-3", title="Third Post"))

        with db_tmp.get_db() as db:
            rows = db.execute(
                "SELECT * FROM digest_items WHERE echo_id = 1 ORDER BY created_at"
            ).fetchall()
        assert len(rows) == 3
        assert rows[0]["item_title"] == "First Post"
        assert rows[1]["item_title"] == "Second Post"
        assert rows[2]["item_title"] == "Third Post"

    def test_duplicate_item_not_requeued(self, db_tmp, monkeypatch):
        """Re-processing the same item should not create a duplicate digest_items row."""
        import scheduler

        monkeypatch.setattr(scheduler, "send_email", lambda **kw: {"success": True})

        echo = _setup_email_echo(db_tmp)
        scheduler.process_echo(echo, _item())
        # Re-process (e.g. cursor replay)
        scheduler.process_echo(echo, _item())

        with db_tmp.get_db() as db:
            rows = db.execute("SELECT * FROM digest_items WHERE echo_id = 1").fetchall()
        assert len(rows) == 1


# ── Digest Flush Tests ───────────────────────────────────────────────────────


class TestDigestFlush:
    def test_flush_sends_one_email_per_echo(self, db_tmp, monkeypatch):
        """flush_digests should send one email containing all queued items."""
        import scheduler

        sent_emails = []
        monkeypatch.setattr(
            scheduler, "send_email", lambda **kw: sent_emails.append(kw) or {"success": True}
        )

        echo = _setup_email_echo(db_tmp)
        scheduler.process_echo(echo, _item(id="item-1", title="First Post"))
        scheduler.process_echo(echo, _item(id="item-2", title="Second Post"))
        scheduler.process_echo(echo, _item(id="item-3", title="Third Post"))

        scheduler.flush_digests()

        assert len(sent_emails) == 1
        body = sent_emails[0]["body"]
        assert "First Post" in body
        assert "Second Post" in body
        assert "Third Post" in body
        assert "Digest" in sent_emails[0]["subject"]

    def test_flush_clears_digest_items(self, db_tmp, monkeypatch):
        """After flush, digest_items should be empty for that echo."""
        import scheduler

        monkeypatch.setattr(scheduler, "send_email", lambda **kw: {"success": True})

        echo = _setup_email_echo(db_tmp)
        scheduler.process_echo(echo, _item(id="item-1", title="First"))
        scheduler.process_echo(echo, _item(id="item-2", title="Second"))

        scheduler.flush_digests()

        with db_tmp.get_db() as db:
            rows = db.execute("SELECT * FROM digest_items WHERE echo_id = 1").fetchall()
        assert len(rows) == 0

    def test_flush_updates_posted_items_to_success(self, db_tmp, monkeypatch):
        """After flush, posted_items should transition from 'queued' to 'success'."""
        import scheduler

        monkeypatch.setattr(scheduler, "send_email", lambda **kw: {"success": True})

        echo = _setup_email_echo(db_tmp)
        scheduler.process_echo(echo, _item(id="item-1", title="First"))
        scheduler.process_echo(echo, _item(id="item-2", title="Second"))

        scheduler.flush_digests()

        with db_tmp.get_db() as db:
            rows = db.execute(
                "SELECT status FROM posted_items WHERE echo_id = 1 ORDER BY item_id"
            ).fetchall()
        assert len(rows) == 2
        assert all(r["status"] == "success" for r in rows)

    def test_flush_no_pending_items_is_noop(self, db_tmp, monkeypatch):
        """flush_digests with no pending items should not send any emails."""
        import scheduler

        sent_emails = []
        monkeypatch.setattr(
            scheduler, "send_email", lambda **kw: sent_emails.append(kw) or {"success": True}
        )

        # No items queued
        scheduler.flush_digests()
        assert len(sent_emails) == 0

    def test_flush_email_failure_preserves_items(self, db_tmp, monkeypatch):
        """If email send fails, digest_items should be preserved for next flush."""
        import scheduler

        send_calls = []
        monkeypatch.setattr(
            scheduler,
            "send_email",
            lambda **kw: send_calls.append(kw) or (_ for _ in ()).throw(RuntimeError("SMTP down")),
        )

        echo = _setup_email_echo(db_tmp)
        scheduler.process_echo(echo, _item(id="item-1", title="First"))
        scheduler.process_echo(echo, _item(id="item-2", title="Second"))

        scheduler.flush_digests()

        # Items should still be in digest_items (not deleted on failure)
        with db_tmp.get_db() as db:
            rows = db.execute("SELECT * FROM digest_items WHERE echo_id = 1").fetchall()
        assert len(rows) == 2

        # posted_items should be 'failed' with no retry time: the notify
        # counter must see digest send errors, but the retry sweep must
        # not claim them — only flush_digests owns digest delivery.
        with db_tmp.get_db() as db:
            rows = db.execute(
                "SELECT status, next_retry_at FROM posted_items WHERE echo_id = 1"
            ).fetchall()
        assert all(
            r["status"] == "failed" and r["next_retry_at"] is None for r in rows
        )

    def test_flush_multiple_echoes_separately(self, db_tmp, monkeypatch):
        """Each echo with digest mode should get its own email."""
        import scheduler

        sent_emails = []
        monkeypatch.setattr(
            scheduler, "send_email", lambda **kw: sent_emails.append(kw) or {"success": True}
        )

        # Set up first email echo (creates email_account 1, feed 1, echo 1)
        echo1 = _setup_email_echo(db_tmp)

        # Set up second feed + email account + echo
        with db_tmp.get_db() as db:
            db.execute(
                "INSERT INTO email_accounts (name, email) VALUES (?, ?)",
                ("User 2", "user2@example.com"),
            )
            db.execute(
                "INSERT INTO feeds (name, url) VALUES (?, ?)",
                ("f2", "https://example.com/feed2"),
            )
            db.execute(
                """INSERT INTO echoes (feed_id, destination_type, destination_id, template,
                                       visibility, filter_keywords, filter_mode,
                                       content_warning, attach_image, delivery_mode, enabled)
                   VALUES (2, 'email', 2, '{{ title }}', 'public', '', 'exclude', '', 0, 'digest', 1)""",
            )
            echo2 = db.execute("SELECT * FROM echoes WHERE id = 2").fetchone()

        scheduler.process_echo(echo1, _item(id="a1", title="From Feed 1"))
        scheduler.process_echo(echo2, _item(id="b1", title="From Feed 2"))

        scheduler.flush_digests()

        assert len(sent_emails) == 2
        bodies = [e["body"] for e in sent_emails]
        assert any("From Feed 1" in b for b in bodies)
        assert any("From Feed 2" in b for b in bodies)

    def test_flush_skips_disabled_echoes(self, db_tmp, monkeypatch):
        """Disabled echoes should not be flushed."""
        import scheduler

        sent_emails = []
        monkeypatch.setattr(
            scheduler, "send_email", lambda **kw: sent_emails.append(kw) or {"success": True}
        )

        echo = _setup_email_echo(db_tmp, {"enabled": 0})
        scheduler.process_echo(echo, _item(id="item-1", title="Test"))

        # _setup_email_echo with enabled=0 means process_echo won't be called
        # by the scheduler, but if called directly, the item gets queued.
        # flush_digests should skip disabled echoes.
        scheduler.flush_digests()
        assert len(sent_emails) == 0

    def test_flush_skips_instant_mode_echoes(self, db_tmp, monkeypatch):
        """Echoes in instant mode should not appear in digest flushes."""
        import scheduler

        sent_emails = []
        monkeypatch.setattr(
            scheduler, "send_email", lambda **kw: sent_emails.append(kw) or {"success": True}
        )

        # Even if somehow an instant-mode echo has digest_items, flush should skip it
        echo = _setup_email_echo(db_tmp, {"delivery_mode": "instant"})
        # Manually insert a digest_item for this instant echo
        with db_tmp.get_db() as db:
            db.execute(
                "INSERT INTO digest_items (echo_id, item_id, item_title, item_url, rendered_content) "
                "VALUES (1, 'x', 'test', 'url', 'content')"
            )

        scheduler.flush_digests()
        assert len(sent_emails) == 0

    def test_digest_subject_includes_feed_name(self, db_tmp, monkeypatch):
        """The digest email subject should include the feed name."""
        import scheduler

        sent_emails = []
        monkeypatch.setattr(
            scheduler, "send_email", lambda **kw: sent_emails.append(kw) or {"success": True}
        )

        echo = _setup_email_echo(db_tmp)
        scheduler.process_echo(echo, _item(id="item-1", title="Test"))

        scheduler.flush_digests()

        assert len(sent_emails) == 1
        assert "f" in sent_emails[0]["subject"]  # feed name is "f"
        assert "Digest" in sent_emails[0]["subject"]

    def test_digest_body_includes_numbered_items(self, db_tmp, monkeypatch):
        """The digest body should number items sequentially."""
        import scheduler

        sent_emails = []
        monkeypatch.setattr(
            scheduler, "send_email", lambda **kw: sent_emails.append(kw) or {"success": True}
        )

        echo = _setup_email_echo(db_tmp)
        scheduler.process_echo(echo, _item(id="item-1", title="First"))
        scheduler.process_echo(echo, _item(id="item-2", title="Second"))

        scheduler.flush_digests()

        body = sent_emails[0]["body"]
        assert "1. First" in body
        assert "2. Second" in body

    def test_flush_after_failed_then_succeeded(self, db_tmp, monkeypatch):
        """If first flush fails, items should remain and succeed on second flush."""
        import scheduler

        call_count = [0]
        sent_emails = []

        def mock_send(**kw):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("First attempt fails")
            sent_emails.append(kw)
            return {"success": True}

        monkeypatch.setattr(scheduler, "send_email", mock_send)

        echo = _setup_email_echo(db_tmp)
        scheduler.process_echo(echo, _item(id="item-1", title="Test"))

        # First flush fails
        scheduler.flush_digests()
        assert len(sent_emails) == 0

        # Second flush succeeds
        scheduler.flush_digests()
        assert len(sent_emails) == 1

        # Items should now be cleared
        with db_tmp.get_db() as db:
            rows = db.execute("SELECT * FROM digest_items WHERE echo_id = 1").fetchall()
        assert len(rows) == 0
