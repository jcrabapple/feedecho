"""Admin per-user usage view: counts and delivery outcomes per tenant.

The support-triage view: when a beta user reports "nothing is posting" or
"it posted twice", the admin row must answer it at a glance — feed/echo/
destination counts, delivery outcomes over 24h/7d, queued backlog, and the
most recent error message. Pins:

- the aggregate is set-based (one query for all tenants) and dialect-safe
  (Python-computed UTC bounds, never datetime('now')/CURRENT_TIMESTAMP)
- usage rows only appear on the admin page (admin-gated render)
- counts are tenant-scoped: user A's posts never appear in user B's row
- content is never exposed — counts, statuses, and error strings only
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import auth
import database
import security
import settings
from app import app

ADMIN_ID = 9
A_ID = 5
B_ID = 6


@pytest.fixture()
def multi_env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MULTI", True)
    monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
    monkeypatch.setattr(database, "DB_PATH", database.Path(tmp_path / "usage.db"))
    database.init_db()
    auth._login_attempts.clear()
    auth._register_attempts.clear()
    with database.get_db() as db:
        db.execute(
            "INSERT INTO users (id, email, password_hash, email_verified, is_admin)"
            " VALUES (?, 'admin@example.com', '', 1, 1)",
            (ADMIN_ID,),
        )
        db.execute(
            "INSERT INTO users (id, email, password_hash, email_verified)"
            " VALUES (?, 'a@example.com', '', 1)",
            (A_ID,),
        )
        db.execute(
            "INSERT INTO users (id, email, password_hash, email_verified)"
            " VALUES (?, 'b@example.com', '', 1)",
            (B_ID,),
        )
    return database


def _admin_client(multi_env):
    c = TestClient(app)
    c.cookies.set("feedecho_session", security.sign_session(ADMIN_ID, "admin@example.com"))
    return c


def _now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _ago(days=0, hours=0):
    return (datetime.now(timezone.utc) - timedelta(days=days, hours=hours)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


class TestAdminUsageAggregate:
    def test_counts_are_per_tenant(self, multi_env):
        """User A's posts never appear in user B's usage row."""
        with database.get_db() as db:
            db.execute(
                "INSERT INTO feeds (id, name, url, user_id) VALUES (1, 'A', 'https://a.example/f', ?)",
                (A_ID,),
            )
            db.execute(
                """INSERT INTO echoes (id, feed_id, destination_type, destination_id,
                                       template, user_id, enabled)
                   VALUES (1, 1, 'email', 1, '{{ t }}', ?, 1)""",
                (A_ID,),
            )
            db.execute(
                """INSERT INTO email_accounts (id, name, email, user_id)
                   VALUES (1, 'a', 'a@dest.example', ?)""",
                (A_ID,),
            )
            db.execute(
                """INSERT INTO posted_items (echo_id, item_id, status, posted_at)
                   VALUES (1, 'item-1', 'success', ?)""",
                (_now_str(),),
            )

        with database.get_db() as db:
            rows = {r["user_id"]: r for r in __import__("app")._admin_usage(db)}

        assert rows[A_ID]["feeds"] == 1
        assert rows[A_ID]["echoes"] == 1
        assert rows[A_ID]["destinations"] == 1
        assert rows[A_ID]["posts_24h"] == 1
        # B has nothing; A's numbers must not leak into B's row.
        assert rows[B_ID]["feeds"] == 0
        assert rows[B_ID]["posts_24h"] == 0
        assert rows[B_ID]["destinations"] == 0

    def test_time_windows(self, multi_env):
        """24h/7d windows count only recent successes; old posts don't count."""
        with database.get_db() as db:
            db.execute(
                "INSERT INTO feeds (id, name, url, user_id) VALUES (1, 'A', 'https://a.example/f', ?)",
                (A_ID,),
            )
            db.execute(
                """INSERT INTO echoes (id, feed_id, destination_type, destination_id,
                                       template, user_id, enabled)
                   VALUES (1, 1, 'email', 1, '{{ t }}', ?, 1)""",
                (A_ID,),
            )
            db.execute(
                """INSERT INTO posted_items (echo_id, item_id, status, posted_at)
                   VALUES (1, 'recent', 'success', ?)""",
                (_ago(hours=2),),
            )
            db.execute(
                """INSERT INTO posted_items (echo_id, item_id, status, posted_at)
                   VALUES (1, 'old', 'success', ?)""",
                (_ago(days=10),),
            )
        with database.get_db() as db:
            rows = {r["user_id"]: r for r in __import__("app")._admin_usage(db)}
        assert rows[A_ID]["posts_24h"] == 1
        assert rows[A_ID]["posts_7d"] == 1

    def test_failures_and_last_error_surface(self, multi_env):
        with database.get_db() as db:
            db.execute(
                "INSERT INTO feeds (id, name, url, user_id) VALUES (1, 'A', 'https://a.example/f', ?)",
                (A_ID,),
            )
            db.execute(
                """INSERT INTO echoes (id, feed_id, destination_type, destination_id,
                                       template, user_id, enabled)
                   VALUES (1, 1, 'email', 1, '{{ t }}', ?, 1)""",
                (A_ID,),
            )
            db.execute(
                """INSERT INTO posted_items (echo_id, item_id, status, posted_at, error_message)
                   VALUES (1, 'bad', 'gave_up', ?, 'Micro.blog token rejected: nope')""",
                (_ago(hours=1),),
            )
        with database.get_db() as db:
            rows = {r["user_id"]: r for r in __import__("app")._admin_usage(db)}
        assert rows[A_ID]["failures_7d"] == 1
        assert "token rejected" in rows[A_ID]["last_error"]

    def test_paused_feeds_and_disabled_echoes_counted(self, multi_env):
        with database.get_db() as db:
            db.execute(
                """INSERT INTO feeds (id, name, url, user_id, paused)
                   VALUES (1, 'paused-feed', 'https://a.example/f', ?, 1)""",
                (A_ID,),
            )
            db.execute(
                """INSERT INTO echoes (id, feed_id, destination_type, destination_id,
                                       template, user_id, enabled)
                   VALUES (1, 1, 'email', 1, '{{ t }}', ?, 0)""",
                (A_ID,),
            )
        with database.get_db() as db:
            rows = {r["user_id"]: r for r in __import__("app")._admin_usage(db)}
        assert rows[A_ID]["feeds"] == 1
        assert rows[A_ID]["feeds_paused"] == 1
        assert rows[A_ID]["echoes"] == 1
        assert rows[A_ID]["echoes_enabled"] == 0

    def test_queued_backlog_visible(self, multi_env):
        with database.get_db() as db:
            db.execute(
                "INSERT INTO feeds (id, name, url, user_id) VALUES (1, 'A', 'https://a.example/f', ?)",
                (A_ID,),
            )
            db.execute(
                """INSERT INTO echoes (id, feed_id, destination_type, destination_id,
                                       template, user_id, enabled)
                   VALUES (1, 1, 'email', 1, '{{ t }}', ?, 1)""",
                (A_ID,),
            )
            db.execute(
                """INSERT INTO posted_items (echo_id, item_id, status)
                   VALUES (1, 'held', 'queued')"""
            )
        with database.get_db() as db:
            rows = {r["user_id"]: r for r in __import__("app")._admin_usage(db)}
        assert rows[A_ID]["queued_now"] == 1


class TestUsageXSSSafety:
    def test_hostile_error_string_is_escaped_on_admin_page(self, multi_env):
        """Kimi F1: error_message comes from destination API responses, so a
        crafted string must render escaped, never execute in the admin's
        browser. Jinja env has autoescape on (select_autoescape); this pins it."""
        with database.get_db() as db:
            db.execute(
                "INSERT INTO feeds (id, name, url, user_id) VALUES (1, 'A', 'https://a.example/f', ?)",
                (A_ID,),
            )
            db.execute(
                """INSERT INTO echoes (id, feed_id, destination_type, destination_id,
                                       template, user_id, enabled)
                   VALUES (1, 1, 'email', 1, '{{ t }}', ?, 1)""",
                (A_ID,),
            )
            db.execute(
                """INSERT INTO posted_items (echo_id, item_id, status, posted_at, error_message)
                   VALUES (1, 'xss', 'gave_up', ?,
                           '<script>alert(1)</script><b>bold</b>')""",
                (_ago(hours=1),),
            )
        c = TestClient(app)
        c.cookies.set("feedecho_session", security.sign_session(ADMIN_ID, "admin@example.com"))
        r = c.get("/admin")
        assert r.status_code == 200
        assert "<script>alert(1)</script>" not in r.text
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text


class TestAdminUsagePage:
    def test_usage_appears_on_admin_page(self, multi_env):
        with database.get_db() as db:
            db.execute(
                "INSERT INTO feeds (id, name, url, user_id) VALUES (1, 'A', 'https://a.example/f', ?)",
                (A_ID,),
            )
        c = TestClient(app)
        c.cookies.set("feedecho_session", security.sign_session(ADMIN_ID, "admin@example.com"))
        r = c.get("/admin")
        assert r.status_code == 200
        assert "usage-cell" in r.text  # the expandable detail row rendered

    def test_non_admin_has_no_admin_access(self, multi_env):
        c = TestClient(app)
        c.cookies.set("feedecho_session", security.sign_session(A_ID, "a@example.com"))
        r = c.get("/admin")
        assert r.status_code == 403

    def test_local_user_excluded_from_usage(self, multi_env):
        """The single-tenant 'local' placeholder never gets a usage row."""
        with database.get_db() as db:
            rows = {r["user_id"]: r for r in __import__("app")._admin_usage(db)}
        assert 1 not in rows
        assert set(rows) == {ADMIN_ID, A_ID, B_ID}