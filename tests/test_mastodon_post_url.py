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

from test_cw_and_images import _item

def _post_url_for(echo_id=1):
    with get_db() as db:
        row = db.execute(
            "SELECT status, post_url FROM posted_items WHERE echo_id = ?", (echo_id,)
        ).fetchone()
    return row["status"], row["post_url"]

class TestMastodonPostUrlStored:
    def test_success_stores_the_api_url(self, db_tmp, monkeypatch, setup_echo):
        monkeypatch.setattr(
            scheduler,
            "post_status",
            lambda **kw: {"id": "110", "url": "https://mastodon.social/@user/110"},
        )
        echo = setup_echo(attach_image=0)
        assert scheduler.process_echo(echo, _item()) is True

        status, post_url = _post_url_for()
        assert status == "success"
        assert post_url == "https://mastodon.social/@user/110"

    def test_response_without_url_still_succeeds(self, db_tmp, monkeypatch, setup_echo):
        # An instance that omits `url` must not turn a delivered post into a
        # failure; the link is a bonus, not the delivery receipt.
        monkeypatch.setattr(scheduler, "post_status", lambda **kw: {"id": "111"})
        echo = setup_echo(attach_image=0)
        assert scheduler.process_echo(echo, _item()) is True

        status, post_url = _post_url_for()
        assert status == "success"
        assert post_url is None

    def test_non_string_url_is_ignored(self, db_tmp, monkeypatch, setup_echo):
        # post_url is a TEXT column; whatever an odd instance returns must be
        # dropped (or a clean empty), never stringified-and-stored: a stored
        # "12345" would render a broken link.
        monkeypatch.setattr(
            scheduler, "post_status", lambda **kw: {"id": "112", "url": 12345}
        )
        echo = setup_echo(attach_image=0)
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

    def test_failed_delivery_stores_nothing(self, db_tmp, monkeypatch, setup_echo):
        def boom(**kw):
            raise RuntimeError("down")

        monkeypatch.setattr(scheduler, "post_status", boom)
        echo = setup_echo(attach_image=0)
        assert scheduler.process_echo(echo, _item()) is False

        status, post_url = _post_url_for()
        assert status == "failed"
        assert post_url is None


class TestMastodonAuthErrorIsPermanent:
    """Bug review finding #3: a 401/403 from Mastodon (revoked/expired
    token) must be classified permanent -- marked 'gave_up' immediately
    instead of scheduled for a retry that can never succeed, mirroring how
    Bluesky/Matrix/micro.blog already handle their auth errors.
    """

    def test_401_marks_the_post_permanently_failed(self, db_tmp, monkeypatch, setup_echo):
        from mastodon import MastodonAuthError

        def boom(**kw):
            raise MastodonAuthError("Mastodon rejected the access token (HTTP 401).")

        monkeypatch.setattr(scheduler, "post_status", boom)
        echo = setup_echo(attach_image=0)
        assert scheduler.process_echo(echo, _item()) is True  # gave_up counts as "handled"

        status, post_url = _post_url_for()
        assert status == "gave_up"
        assert post_url is None

    def test_403_marks_the_post_permanently_failed(self, db_tmp, monkeypatch, setup_echo):
        from mastodon import MastodonAuthError

        def boom(**kw):
            raise MastodonAuthError("Mastodon rejected the access token (HTTP 403).")

        monkeypatch.setattr(scheduler, "post_status", boom)
        echo = setup_echo(attach_image=0)
        assert scheduler.process_echo(echo, _item()) is True

        status, post_url = _post_url_for()
        assert status == "gave_up"
        assert post_url is None

    def test_generic_httpx_error_still_scheduled_for_retry(self, db_tmp, monkeypatch, setup_echo):
        # Contrast case: a non-auth HTTPStatusError (e.g. 500/429) must NOT
        # be treated as permanent -- it stays in the ordinary bounded-retry
        # path ('failed', not 'gave_up').
        import httpx

        def boom(**kw):
            request = httpx.Request("POST", "https://mastodon.example/api/v1/statuses")
            response = httpx.Response(503, request=request)
            raise httpx.HTTPStatusError("server error", request=request, response=response)

        monkeypatch.setattr(scheduler, "post_status", boom)
        echo = setup_echo(attach_image=0)
        assert scheduler.process_echo(echo, _item()) is False

        status, post_url = _post_url_for()
        assert status == "failed"
        assert post_url is None

        # The (MastodonError, httpx.HTTPStatusError) branch must record the
        # API's own failure text, not the flat generic string the old
        # `except Exception:` fallback produced.
        with get_db() as db:
            row = db.execute(
                "SELECT error_message FROM posted_items WHERE echo_id = 1"
            ).fetchone()
        assert row["error_message"] == "Mastodon delivery failed: server error"

    def test_upload_media_auth_error_fails_the_post_permanently(self, db_tmp, monkeypatch, setup_echo):
        """A token rejected at IMAGE UPLOAD time must finalize the post as
        permanently failed right there, not fall through to post_status.

        Falling through has two holes: a token that can post but lacks the
        media scope (403 on upload only) would silently publish text-only
        and mark the row successful forever, and a pure 401 followed by an
        unrelated transient post_status failure would enter the bounded
        retry path even though every retry is doomed.
        """
        from mastodon import MastodonAuthError

        post_calls = []
        monkeypatch.setattr(
            scheduler, "fetch_image", lambda url: (b"img-bytes", "image/jpeg")
        )

        def upload_boom(**kw):
            raise MastodonAuthError("Mastodon rejected the access token (HTTP 401).")

        monkeypatch.setattr(scheduler, "upload_media", upload_boom)
        monkeypatch.setattr(
            scheduler, "post_status", lambda **kw: post_calls.append(kw) or {"id": "1"}
        )
        echo = setup_echo(attach_image=1)
        assert (
            scheduler.process_echo(echo, _item(image_url="https://example.com/pic.jpg"))
            is True
        )

        # The post must never be attempted, text-only or otherwise.
        assert post_calls == []

        with get_db() as db:
            row = db.execute(
                "SELECT status, error_message FROM posted_items WHERE echo_id = 1"
            ).fetchone()
        assert row["status"] == "gave_up"
        assert row["error_message"].startswith("Mastodon token rejected:")

    def test_upload_media_auth_error_after_a_successful_image_sends_nothing(
        self, db_tmp, monkeypatch, setup_echo
    ):
        """Image 1 uploads, image 2 hits the rejected token: the post must
        not go out with partial media either."""
        from mastodon import MastodonAuthError

        post_calls = []
        upload_calls = []

        monkeypatch.setattr(
            scheduler, "fetch_image", lambda url: (b"img-bytes", "image/jpeg")
        )

        def upload(**kw):
            upload_calls.append(kw)
            if len(upload_calls) == 1:
                return {"id": "m1"}
            raise MastodonAuthError("Mastodon rejected the access token (HTTP 403).")

        monkeypatch.setattr(scheduler, "upload_media", upload)
        monkeypatch.setattr(
            scheduler, "post_status", lambda **kw: post_calls.append(kw) or {"id": "1"}
        )
        echo = setup_echo(attach_image=1)
        assert (
            scheduler.process_echo(
                echo,
                _item(
                    image_urls=[
                        "https://example.com/1.jpg",
                        "https://example.com/2.jpg",
                        "https://example.com/3.jpg",
                    ]
                ),
            )
            is True
        )

        # Two uploads attempted (the second raised); image 3 never tried.
        assert len(upload_calls) == 2
        assert post_calls == []  # never posted with partial media

        with get_db() as db:
            row = db.execute(
                "SELECT status FROM posted_items WHERE echo_id = 1"
            ).fetchone()
        assert row["status"] == "gave_up"

# ── rendering ────────────────────────────────────────────────────────────────

TENANT_ID = 21

@pytest.fixture
def multi_env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MULTI", True)
    monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
    monkeypatch.setattr(settings, "STATE_SECRET", "s" * 40)
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

# ── Idempotency key (crash-then-reclaim duplicate protection) ────────────────

class _FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {"id": "1", "url": "https://mastodon.social/@user/1"}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._body

class TestMastodonIdempotencyKey:
    """Matrix derives a deterministic transaction ID from (echo_id, item_id)
    so a homeserver-side retry after a crash-then-reclaim is deduplicated.
    Mastodon's POST /api/v1/statuses supports the same thing via a
    client-supplied Idempotency-Key header — these tests pin that a
    deterministic key is sent and threaded through from the scheduler.
    """

    def test_idempotency_key_is_deterministic_for_same_echo_and_item(self):
        import mastodon

        key1 = mastodon.idempotency_key(42, "item-1")
        key2 = mastodon.idempotency_key(42, "item-1")
        assert key1 == key2

    def test_idempotency_key_differs_across_items_and_echoes(self):
        import mastodon

        base = mastodon.idempotency_key(42, "item-1")
        assert mastodon.idempotency_key(42, "item-2") != base
        assert mastodon.idempotency_key(7, "item-1") != base

    def test_post_status_sends_idempotency_key_header(self, monkeypatch):
        import mastodon

        captured = {}

        def fake_pinned_request(method, url, **kw):
            captured.update(kw)
            return _FakeResponse()

        monkeypatch.setattr(mastodon, "pinned_request", fake_pinned_request)

        key = mastodon.idempotency_key(1, "item-1")
        mastodon.post_status(
            instance="https://mastodon.social",
            access_token="tok",
            content="hello",
            idempotency_key=key,
        )

        assert captured["headers"]["Idempotency-Key"] == key

    def test_post_status_omits_header_when_no_key_given(self, monkeypatch):
        import mastodon

        captured = {}

        def fake_pinned_request(method, url, **kw):
            captured.update(kw)
            return _FakeResponse()

        monkeypatch.setattr(mastodon, "pinned_request", fake_pinned_request)

        mastodon.post_status(
            instance="https://mastodon.social",
            access_token="tok",
            content="hello",
        )

        assert "Idempotency-Key" not in captured["headers"]

    def test_send_mastodon_passes_a_deterministic_key_across_calls(
        self, db_tmp, monkeypatch, setup_echo
    ):
        """Two dispatch attempts for the same echo+item (e.g. a crash-then-
        reclaim retry) must send the same Idempotency-Key both times, so
        Mastodon's server-side dedup can recognize the retry."""
        import scheduler

        keys_seen = []

        def fake_post_status(**kw):
            keys_seen.append(kw.get("idempotency_key"))
            return {"id": "1", "url": "https://mastodon.social/@user/1"}

        monkeypatch.setattr(scheduler, "post_status", fake_post_status)

        echo = setup_echo(attach_image=0)
        item = _item()
        assert scheduler.process_echo(echo, item) is True

        # Re-claim and dispatch the same item again (simulating a retry).
        with get_db() as db:
            db.execute("DELETE FROM posted_items WHERE echo_id = 1 AND item_id = ?", (item["id"],))
        assert scheduler.process_echo(echo, item) is True

        assert len(keys_seen) == 2
        assert keys_seen[0] == keys_seen[1]
        assert keys_seen[0] is not None
