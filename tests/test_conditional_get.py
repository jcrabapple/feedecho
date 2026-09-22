"""Conditional GET for feed fetches (Tier 1-1).

Every poll of every feed used to download the full feed body even when nothing
changed. These tests pin the new ETag / If-Modified-Since behaviour: validators
stored on a feed row are sent as conditional headers, a 304 short-circuits the
parse-and-dispatch path, and fresh validators from a 200 are persisted.
"""

import pytest

import feed_parser

RSS = (
    b'<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>'
    b"<item><title>i1</title><guid>g1</guid><link>https://e/1</link></item>"
    b"</channel></rss>"
)


def _resp(body=RSS, status=200, etag=None, last_modified=None, ctype="application/rss+xml"):
    return body, ctype, {"status": status, "etag": etag, "last_modified": last_modified}


class TestConditionalFetch:
    def test_sends_conditional_headers_when_validators_present(self, monkeypatch):
        seen = {}

        def fake(client, url, headers, max_bytes=0, backend=None):
            seen["headers"] = headers
            return _resp()

        monkeypatch.setattr(feed_parser, "_fetch_with_redirect_validation", fake)

        feed_parser.fetch_feed(
            "https://example.com/feed",
            etag='"abc"',
            last_modified="Mon, 01 Jan 2024 00:00:00 GMT",
        )
        assert seen["headers"]["If-None-Match"] == '"abc"'
        assert seen["headers"]["If-Modified-Since"] == "Mon, 01 Jan 2024 00:00:00 GMT"

    def test_no_conditional_headers_without_validators(self, monkeypatch):
        seen = {}

        def fake(client, url, headers, max_bytes=0, backend=None):
            seen["headers"] = headers
            return _resp()

        monkeypatch.setattr(feed_parser, "_fetch_with_redirect_validation", fake)

        feed_parser.fetch_feed("https://example.com/feed")
        assert "If-None-Match" not in seen["headers"]
        assert "If-Modified-Since" not in seen["headers"]

    def test_304_returns_not_modified_without_parsing(self, monkeypatch):
        calls = []

        def fake(client, url, headers, max_bytes=0, backend=None):
            calls.append(url)
            # A 304 body is empty; fetching must NOT try to parse it.
            return _resp(body=b"", status=304)

        monkeypatch.setattr(feed_parser, "_fetch_with_redirect_validation", fake)

        result = feed_parser.fetch_feed("https://example.com/feed", etag='"abc"')
        assert result["not_modified"] is True
        assert result["items"] == []
        assert len(calls) == 1

    def test_304_echoes_sent_validators(self, monkeypatch):
        monkeypatch.setattr(
            feed_parser, "_fetch_with_redirect_validation",
            lambda *a, **k: _resp(body=b"", status=304),
        )
        result = feed_parser.fetch_feed(
            "https://example.com/feed",
            etag='"abc"',
            last_modified="Mon, 01 Jan 2024 00:00:00 GMT",
        )
        # A 304 confirms the validators we sent are still current; when the
        # response carries its own, those win.
        assert result["etag"] == '"abc"'
        assert result["last_modified"] == "Mon, 01 Jan 2024 00:00:00 GMT"

    def test_304_prefers_response_validators(self, monkeypatch):
        monkeypatch.setattr(
            feed_parser, "_fetch_with_redirect_validation",
            lambda *a, **k: _resp(body=b"", status=304, etag='"fresh"'),
        )
        result = feed_parser.fetch_feed("https://example.com/feed", etag='"old"')
        assert result["etag"] == '"fresh"'

    def test_200_captures_fresh_validators(self, monkeypatch):
        monkeypatch.setattr(
            feed_parser, "_fetch_with_redirect_validation",
            lambda *a, **k: _resp(etag='"new"', last_modified="Mon, 01 Jan 2024 00:00:01 GMT"),
        )
        result = feed_parser.fetch_feed("https://example.com/feed")
        assert result["etag"] == '"new"'
        assert result["last_modified"] == "Mon, 01 Jan 2024 00:00:01 GMT"
        assert result["items"][0]["title"] == "i1"


def _seed_feed(db, *, etag=None, last_modified=None):
    db.execute(
        "INSERT INTO accounts (name, username, instance, access_token) VALUES (?, ?, ?, ?)",
        ("main", "user", "https://mastodon.social", "tok"),
    )
    db.execute(
        "INSERT INTO feeds (name, url, etag, last_modified) VALUES (?, ?, ?, ?)",
        ("f", "https://example.com/feed", etag, last_modified),
    )
    db.execute(
        "INSERT INTO echoes (feed_id, destination_type, destination_id, template,"
        " visibility, filter_keywords, filter_mode, enabled)"
        " VALUES (1, 'mastodon', 1, '{{ title }}', 'public', '', 'exclude', 1)"
    )


class TestSchedulerConditional:
    def test_not_modified_skips_delivery_and_updates_last_fetched(self, db_tmp, monkeypatch):
        import scheduler

        with db_tmp.get_db() as db:
            _seed_feed(
                db,
                etag='"abc"',
                last_modified="Mon, 01 Jan 2024 00:00:00 GMT",
            )

        calls = {}

        def fake_feed(url, *args, **kwargs):
            calls["kwargs"] = kwargs
            return {"not_modified": True, "items": [], "etag": '"abc"'}

        monkeypatch.setattr(scheduler, "fetch_feed", fake_feed)
        monkeypatch.setattr(scheduler, "post_status", lambda **kw: {"id": "p1"})

        scheduler.check_feed(1)

        assert calls["kwargs"].get("etag") == '"abc"'
        assert calls["kwargs"].get("last_modified") == "Mon, 01 Jan 2024 00:00:00 GMT"

        with db_tmp.get_db() as db:
            row = db.execute(
                "SELECT last_fetched, last_item_id, etag FROM feeds WHERE id = 1"
            ).fetchone()
        assert row["last_fetched"] is not None
        assert row["last_item_id"] is None, "cursor must not advance on a 304"
        assert row["etag"] == '"abc"'

    def test_200_persists_new_validators(self, db_tmp, monkeypatch):
        import scheduler

        with db_tmp.get_db() as db:
            _seed_feed(db, etag='"old"', last_modified="Mon, 01 Jan 2024 00:00:00 GMT")

        item = {
            "id": "g1",
            "title": "t-g1",
            "link": "https://e/1",
            "summary": "",
            "date": "2026-01-01T00:00:00+00:00",
        }

        def fake_feed(url, *args, **kwargs):
            return {"items": [item], "etag": '"new"', "last_modified": None}

        monkeypatch.setattr(scheduler, "fetch_feed", fake_feed)
        monkeypatch.setattr(scheduler, "post_status", lambda **kw: {"id": "p1"})

        scheduler.check_feed(1)

        with db_tmp.get_db() as db:
            row = db.execute(
                "SELECT etag, last_modified FROM feeds WHERE id = 1"
            ).fetchone()
        assert row["etag"] == '"new"'
        assert row["last_modified"] is None, "a 200 without Last-Modified clears it"

    def test_retry_sweep_fetches_unconditionally(self, db_tmp, monkeypatch):
        import scheduler

        with db_tmp.get_db() as db:
            _seed_feed(db, etag='"old"')

        saw = {}

        def fake_feed(url, *args, **kwargs):
            saw.setdefault("kwargs", kwargs)
            return {"items": [], "etag": None, "last_modified": None}

        monkeypatch.setattr(scheduler, "fetch_feed", fake_feed)
        # A pending retry would normally pull the full feed; the point of this
        # test is that the retry path must NOT send conditional headers (a 304
        # with no body there would mark the pending item "gave_up").
        monkeypatch.setattr(scheduler, "process_echo", lambda *a, **k: True)

        scheduler._retry_due_failures(1, [], feed_name="f")

        assert saw.get("kwargs", {}) == {}, "retry sweep must fetch unconditionally"