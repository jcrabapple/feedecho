"""Scheduler config isolation: retry caps, notification addresses, and SMTP
settings must resolve per echo owner, never globally."""

import pytest

import database
import notify
import scheduler


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "sched.db")
    database.init_db()
    # Two users: 1 (singleton) and 2.
    with database.get_db() as db:
        db.execute(
            "INSERT INTO users (id, email, password_hash) VALUES (2, 'u2@example.com', '')"
        )
    return database, scheduler, notify


def _seed_echo(db, user_id: int, destination: str = "mastodon") -> int:
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO feeds (id, name, url, user_id) VALUES (?, 'F', 'https://example.com/f', ?)",
            (user_id, user_id),
        )
        if destination == "mastodon":
            conn.execute(
                "INSERT INTO accounts (id, name, instance, access_token, user_id)"
                " VALUES (?, 'A', 'https://mastodon.social', 'tok', ?)",
                (user_id, user_id),
            )
        else:
            conn.execute(
                "INSERT INTO email_accounts (id, name, email, user_id)"
                " VALUES (?, 'E', 'dest@example.com', ?)",
                (user_id, user_id),
            )
        conn.execute(
            "INSERT INTO echoes (id, feed_id, destination_type, destination_id, user_id)"
            " VALUES (?, ?, ?, ?, ?)",
            (user_id, user_id, destination, user_id, user_id),
        )
    return user_id


def _item(item_id="i1"):
    return {
        "id": item_id,
        "title": "Title",
        "link": "https://example.com/post",
        "summary": "",
        "published": "2026-01-01T00:00:00Z",
        "image_url": "",
    }


class TestRetryCapIsolation:
    def test_gave_up_uses_owners_cap(self, env, monkeypatch):
        database, scheduler, notify = env
        echo_id = _seed_echo(database, 2)
        with database.get_db() as db:
            db.execute(
                "INSERT INTO settings (user_id, key, value) VALUES (1, 'retry_max_attempts', '99')"
            )
            db.execute(
                "INSERT INTO settings (user_id, key, value) VALUES (2, 'retry_max_attempts', '1')"
            )
        monkeypatch.setattr(
            scheduler, "post_status", lambda **kw: (_ for _ in ()).throw(RuntimeError("down"))
        )

        echo = {"id": echo_id, "user_id": 2, "destination_type": "mastodon",
                "destination_id": 2, "template": "{{ title }}", "visibility": "public",
                "filter_keywords": "", "filter_mode": "exclude", "content_warning": "",
                "attach_image": 0, "delivery_mode": "instant", "drip_limit": 0}
        scheduler.process_echo(echo, _item())
        with database.get_db() as db:
            db.execute("UPDATE posted_items SET next_retry_at = NULL")
        scheduler.process_echo(echo, _item())
        with database.get_db() as db:
            row = db.execute("SELECT status, attempt_count FROM posted_items").fetchone()
        # Owner cap is 1 -> gave_up on the second attempt (first failure
        # records attempt_count 1, so the retry hits the cap)
        assert row["status"] == "gave_up"

    def test_user1_high_cap_unaffected_by_user2_setting(self, env, monkeypatch):
        database, scheduler, notify = env
        echo_id = _seed_echo(database, 2)
        # User 2 sets a huge cap; user 1 (default echo) keeps the default 5
        with database.get_db() as db:
            db.execute(
                "INSERT INTO settings (user_id, key, value) VALUES (2, 'retry_max_attempts', '99')"
            )
            db.execute(
                "INSERT INTO echoes (id, feed_id, destination_type, destination_id, user_id)"
                " VALUES (99, 2, 'mastodon', 2, 1)"
            )
        monkeypatch.setattr(
            scheduler, "post_status", lambda **kw: (_ for _ in ()).throw(RuntimeError("down"))
        )
        # The user-1 echo must NOT see user 2's 99 cap
        assert notify.max_attempts(echo_id=99) == 5
        assert notify.max_attempts(echo_id=echo_id) == 99


class TestNotificationIsolation:
    def test_alert_goes_to_owners_notify_email(self, env, monkeypatch):
        database, scheduler, notify = env
        echo_id = _seed_echo(database, 2)
        sent = []
        monkeypatch.setattr(notify, "send_email", lambda **kw: sent.append(kw))
        monkeypatch.setattr(
            notify, "get_smtp_settings", lambda user_id=1: {"host": "h", "port": 587}
        )
        monkeypatch.setattr(
            scheduler, "post_status", lambda **kw: (_ for _ in ()).throw(RuntimeError("down"))
        )
        with database.get_db() as db:
            db.execute(
                "INSERT INTO settings (user_id, key, value) VALUES (1, 'notify_email', 'owner1@example.com')"
            )
            db.execute(
                "INSERT INTO settings (user_id, key, value) VALUES (2, 'notify_email', 'owner2@example.com')"
            )
            db.execute(
                "INSERT INTO settings (user_id, key, value) VALUES (2, 'notify_failure_threshold', '1')"
            )
            db.execute(
                "INSERT INTO settings (user_id, key, value) VALUES (1, 'notify_failure_threshold', '0')"
            )

        scheduler.process_echo(
            {"id": echo_id, "user_id": 2, "destination_type": "mastodon",
             "destination_id": 2, "template": "{{ title }}", "visibility": "public",
             "filter_keywords": "", "filter_mode": "exclude", "content_warning": "",
             "attach_image": 0, "delivery_mode": "instant", "drip_limit": 0},
            _item(),
        )
        assert len(sent) == 1
        assert sent[0]["to_email"] == "owner2@example.com"


class TestSmtpIsolation:
    def test_email_echo_uses_owners_smtp(self, env, monkeypatch):
        database, scheduler, notify = env
        echo_id = _seed_echo(database, 2, destination="email")
        sent = []
        monkeypatch.setattr(scheduler, "send_email", lambda **kw: sent.append(kw))
        scheduler.process_echo(
            {"id": echo_id, "user_id": 2, "destination_type": "email",
             "destination_id": 2, "template": "{{ title }}", "visibility": "public",
             "filter_keywords": "", "filter_mode": "exclude", "content_warning": "",
             "attach_image": 0, "delivery_mode": "instant", "drip_limit": 0},
            _item(),
        )
        assert len(sent) == 1
        assert sent[0]["user_id"] == 2
        assert sent[0]["to_email"] == "dest@example.com"
