"""Mastodon post URLs land in history (issue #9).

Bluesky and micro.blog have always stored ``post_url`` on success and the
history page already renders a "view post ↗" link when it is set — but the
Mastodon dispatch path threw the API's ``url`` field away, so Mastodon rows
showed no link. This module covers the persistence and the rendering.
"""

import re

import pytest
from fastapi.testclient import TestClient

import database
import settings
import scheduler
from database import get_db

from test_cw_and_images import _item, _setup_echo


@pytest.fixture()
def db_tmp(monkeypatch):
    """Point the DB layer at a fresh temp file per test."""
    fd, path = None, None
    import os
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)

    monkeypatch.setattr(database, "DB_PATH", database.Path(path))
    database.init_db()

    import scheduler as sched_mod

    monkeypatch.setattr(sched_mod, "get_db", database.get_db)

    yield database

    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except OSError:
            pass


def _post_url_for(echo_id=1):
    with get_db() as db:
        row = db.execute(
            "SELECT status, post_url FROM posted_items WHERE echo_id = ?", (echo_id,)
        ).fetchone()
    return row["status"], row["post_url"]


class TestMastodonPostUrlStored:
    def test_success_stores_the_api_url(self, db_tmp, monkeypatch):
        monkeypatch.setattr(
            scheduler,
            "post_status",
            lambda **kw: {"id": "110", "url": "https://mastodon.social/@user/110"},
        )
        echo = _setup_echo(db_tmp)
        assert scheduler.process_echo(echo, _item()) is True

        status, post_url = _post_url_for()
        assert status == "success"
        assert post_url == "https://mastodon.social/@user/110"

    def test_response_without_url_still_succeeds(self, db_tmp, monkeypatch):
        # An instance that omits `url` must not turn a delivered post into a
        # failure; the link is a bonus, not the delivery receipt.
        monkeypatch.setattr(scheduler, "post_status", lambda **kw: {"id": "111"})
        echo = _setup_echo(db_tmp)
        assert scheduler.process_echo(echo, _item()) is True

        status, post_url = _post_url_for()
        assert status == "success"
        assert post_url is None

    def test_non_string_url_is_ignored(self, db_tmp, monkeypatch):
        # post_url is a TEXT column; whatever an odd instance returns must be
        # dropped (or a clean empty), never stringified-and-stored: a stored
        # "12345" would render a broken link.
        monkeypatch.setattr(
            scheduler, "post_status", lambda **kw: {"id": "112", "url": 12345}
        )
        echo = _setup_echo(db_tmp)
        assert scheduler.process_echo(echo, _item()) is True

        status, post_url = _post_url_for()
        assert status == "success"
        assert post_url is None

    def test_item_link_dropped_for_a_javascript_url(self, multi_env, monkeypatch):
        # Same gate as the post link: the item link comes from the feed, and
        # a non-http scheme must not render a dead anchor back to /history.
        with get_db() as db:
            db.execute(
                "INSERT INTO posted_items (echo_id, item_id, item_title, item_url,"
                " status) VALUES (1, 'i5', 'Bad item', 'vbscript:x', 'success')"
            )
        page = _history_page(multi_env, monkeypatch)
        assert "vbscript:x" not in page
        m = re.search(r"<td data-label=\"Item\">.*?</td>", page, re.S)
        assert m and "<a " not in m.group(0), "no anchor should render"
        assert "Bad item" in m.group(0), "the title text must still show"

    def test_post_link_gate_survives_an_empty_post_url(self, multi_env, monkeypatch):
        # The link row renders inside a text cell that also carries the item
        # title; an empty-string post_url (micro.blog Location header missing)
        # must not produce a bare anchor.
        with get_db() as db:
            db.execute(
                "INSERT INTO posted_items (echo_id, item_id, item_title, status, post_url)"
                " VALUES (1, 'i6', 'No location', 'success', '')"
            )
        page = _history_page(multi_env, monkeypatch)
        assert ">view post" not in page

    def test_failed_delivery_stores_nothing(self, db_tmp, monkeypatch):
        def boom(**kw):
            raise RuntimeError("down")

        monkeypatch.setattr(scheduler, "post_status", boom)
        echo = _setup_echo(db_tmp)
        assert scheduler.process_echo(echo, _item()) is False

        status, post_url = _post_url_for()
        assert status == "failed"
        assert post_url is None


# ── rendering ────────────────────────────────────────────────────────────────

TENANT_ID = 21


@pytest.fixture
def multi_env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MULTI", True)
    monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "post-url.db")
    database.init_db()
    with database.get_db() as db:
        db.execute(
            "INSERT INTO users (id, email, password_hash, plan)"
            " VALUES (?, 'user@example.com', '', 'trial')",
            (TENANT_ID,),
        )
        db.execute(
            "INSERT INTO feeds (id, name, url, user_id)"
            " VALUES (1, 'Feed', 'https://example.com/feed.xml', ?)",
            (TENANT_ID,),
        )
        db.execute(
            "INSERT INTO accounts (id, name, username, instance, access_token, user_id)"
            " VALUES (1, 'Main', 'user', 'https://mastodon.social', 'tok', ?)",
            (TENANT_ID,),
        )
        db.execute(
            "INSERT INTO echoes (id, feed_id, destination_type, destination_id,"
            " template, user_id)"
            " VALUES (1, 1, 'mastodon', 1, '{{ title }}', ?)",
            (TENANT_ID,),
        )
    import security

    return security


def _history_page(multi_env, monkeypatch):
    from app import app

    monkeypatch.setattr("app.get_db", get_db)
    with TestClient(app) as c:
        c.cookies.set(
            "feedecho_session",
            multi_env.sign_session(TENANT_ID, "user@example.com"),
        )
        return c.get("/history").text


@pytest.mark.multi
class TestHistoryRendersPostLink:
    def test_mastodon_row_links_to_the_post(self, multi_env, monkeypatch):
        with get_db() as db:
            db.execute(
                "INSERT INTO posted_items (echo_id, item_id, item_title, item_url,"
                " status, post_url)"
                " VALUES (1, 'i1', 'An item', 'https://example.com/a', 'success',"
                " 'https://mastodon.social/@user/110')"
            )
        page = _history_page(multi_env, monkeypatch)
        m = re.search(r'href="([^"]+)"[^>]*>view post', page)
        assert m, "history should offer a post link"
        assert m.group(1) == "https://mastodon.social/@user/110"

    def test_link_dropped_for_a_javascript_url(self, multi_env, monkeypatch):
        # post_url originates from remote APIs; the safe_url filter must
        # still gate it in the history template.
        with get_db() as db:
            db.execute(
                "INSERT INTO posted_items (echo_id, item_id, item_title, status, post_url)"
                " VALUES (1, 'i2', 'X', 'success', 'javascript:alert(1)')"
            )
        page = _history_page(multi_env, monkeypatch)
        assert "javascript:alert(1)" not in page
        assert ">view post" not in page

    def test_no_link_without_a_post_url(self, multi_env, monkeypatch):
        with get_db() as db:
            db.execute(
                "INSERT INTO posted_items (echo_id, item_id, item_title, status)"
                " VALUES (1, 'i3', 'Email item', 'success')"
            )
        page = _history_page(multi_env, monkeypatch)
        assert ">view post" not in page
        assert "Email item" in page

    def test_dashboard_also_links_the_post(self, multi_env, monkeypatch):
        with get_db() as db:
            db.execute(
                "INSERT INTO posted_items (echo_id, item_id, item_title, status, post_url)"
                " VALUES (1, 'i4', 'Dash item', 'success',"
                " 'https://mastodon.social/@user/111')"
            )
        from app import app

        monkeypatch.setattr("app.get_db", get_db)
        with TestClient(app) as c:
            c.cookies.set(
                "feedecho_session",
                multi_env.sign_session(TENANT_ID, "user@example.com"),
            )
            page = c.get("/").text
        assert 'href="https://mastodon.social/@user/111"' in page
