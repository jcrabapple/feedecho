"""Tests for content warnings and image attachment features."""

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
        "summary": "A summary of the post.",
        "image_url": "",
    }
    item.update(overrides)
    return item


def _setup_echo(db_tmp, echo_overrides=None):
    """Create a test account, feed, and echo. Returns the echo row."""
    import database

    echo_kwargs = {
        "destination_type": "mastodon",
        "destination_id": 1,
        "template": "{{ title }}",
        "visibility": "public",
        "filter_keywords": "",
        "filter_mode": "exclude",
        "content_warning": "",
        "attach_image": 0,
        "enabled": 1,
    }
    if echo_overrides:
        echo_kwargs.update(echo_overrides)

    with database.get_db() as db:
        db.execute(
            "INSERT INTO accounts (name, username, instance, access_token) "
            "VALUES (?, ?, ?, ?)",
            ("main", "user", "https://mastodon.social", "tok"),
        )
        db.execute(
            "INSERT INTO feeds (name, url) VALUES (?, ?)",
            ("f", "https://example.com/feed"),
        )
        db.execute(
            """INSERT INTO echoes (feed_id, destination_type, destination_id, template,
                                   visibility, filter_keywords, filter_mode,
                                   content_warning, attach_image, enabled)
               VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                echo_kwargs["destination_type"],
                echo_kwargs["destination_id"],
                echo_kwargs["template"],
                echo_kwargs["visibility"],
                echo_kwargs["filter_keywords"],
                echo_kwargs["filter_mode"],
                echo_kwargs["content_warning"],
                echo_kwargs["attach_image"],
                echo_kwargs["enabled"],
            ),
        )
        return db.execute("SELECT * FROM echoes WHERE id = 1").fetchone()


# ── Content Warning Tests ────────────────────────────────────────────────────


class TestContentWarning:
    def test_cw_sent_as_spoiler_text(self, db_tmp, monkeypatch):
        """CW text must be passed as spoiler_text and sensitive=True."""
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "post_status", lambda **kw: sent.append(kw) or {"id": "1"}
        )

        echo = _setup_echo(db_tmp, {"content_warning": "Spoilers"})
        scheduler.process_echo(echo, _item())

        assert len(sent) == 1
        assert sent[0]["spoiler_text"] == "Spoilers"
        assert sent[0]["sensitive"] is True

    def test_no_cw_means_no_spoiler_text(self, db_tmp, monkeypatch):
        """Without a CW, spoiler_text must be absent and sensitive=False."""
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "post_status", lambda **kw: sent.append(kw) or {"id": "1"}
        )

        echo = _setup_echo(db_tmp, {"content_warning": ""})
        scheduler.process_echo(echo, _item())

        assert len(sent) == 1
        assert sent[0]["sensitive"] is False
        # spoiler_text key should not be in the kwargs, or should be empty
        assert not sent[0].get("spoiler_text")

    def test_cw_empty_string_treated_as_no_cw(self, db_tmp, monkeypatch):
        """An empty CW string should not trigger sensitive=True."""
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "post_status", lambda **kw: sent.append(kw) or {"id": "1"}
        )

        echo = _setup_echo(db_tmp, {"content_warning": ""})
        scheduler.process_echo(echo, _item())

        assert sent[0]["sensitive"] is False

    def test_cw_strips_whitespace(self, db_tmp, monkeypatch):
        """CW text should be stripped before sending."""
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "post_status", lambda **kw: sent.append(kw) or {"id": "1"}
        )

        echo = _setup_echo(db_tmp, {"content_warning": "  Spacers  "})
        scheduler.process_echo(echo, _item())

        assert sent[0]["spoiler_text"] == "Spacers"


# ── Image Attachment Tests ───────────────────────────────────────────────────


class TestImageAttachment:
    def test_attach_image_disabled_no_upload(self, db_tmp, monkeypatch):
        """When attach_image=0, no image fetch or upload should occur."""
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "post_status", lambda **kw: sent.append(kw) or {"id": "1"}
        )
        upload_calls = []
        monkeypatch.setattr(
            scheduler, "upload_media", lambda **kw: upload_calls.append(kw) or {"id": "m1"}
        )

        echo = _setup_echo(db_tmp, {"attach_image": 0})
        item = _item(image_url="https://example.com/image.jpg")
        scheduler.process_echo(echo, item)

        assert len(sent) == 1
        assert len(upload_calls) == 0
        assert sent[0].get("media_ids") is None

    def test_attach_image_no_image_url_posts_text_only(self, db_tmp, monkeypatch):
        """When attach_image=1 but item has no image_url, post text-only."""
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "post_status", lambda **kw: sent.append(kw) or {"id": "1"}
        )
        upload_calls = []
        monkeypatch.setattr(
            scheduler, "upload_media", lambda **kw: upload_calls.append(kw) or {"id": "m1"}
        )

        echo = _setup_echo(db_tmp, {"attach_image": 1})
        item = _item(image_url="")
        scheduler.process_echo(echo, item)

        assert len(sent) == 1
        assert len(upload_calls) == 0
        assert sent[0].get("media_ids") is None

    def test_attach_image_success(self, db_tmp, monkeypatch):
        """When attach_image=1 and item has image_url, upload and attach."""
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "post_status", lambda **kw: sent.append(kw) or {"id": "1"}
        )
        monkeypatch.setattr(
            scheduler, "fetch_image", lambda url: (b"fake-image-bytes", "image/jpeg")
        )
        monkeypatch.setattr(
            scheduler, "upload_media", lambda **kw: {"id": "media-123"}
        )

        echo = _setup_echo(db_tmp, {"attach_image": 1})
        item = _item(image_url="https://example.com/photo.jpg")
        scheduler.process_echo(echo, item)

        assert len(sent) == 1
        assert sent[0]["media_ids"] == ["media-123"]

    def test_image_fetch_failure_posts_text_only(self, db_tmp, monkeypatch):
        """If fetch_image returns None (network/SSRF/size), post text-only."""
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "post_status", lambda **kw: sent.append(kw) or {"id": "1"}
        )
        monkeypatch.setattr(scheduler, "fetch_image", lambda url: None)
        upload_calls = []
        monkeypatch.setattr(
            scheduler, "upload_media", lambda **kw: upload_calls.append(kw)
        )

        echo = _setup_echo(db_tmp, {"attach_image": 1})
        item = _item(image_url="https://example.com/broken.jpg")
        scheduler.process_echo(echo, item)

        assert len(sent) == 1
        assert len(upload_calls) == 0
        assert sent[0].get("media_ids") is None

    def test_image_upload_failure_posts_text_only(self, db_tmp, monkeypatch):
        """If upload_media returns None (API failure), post text-only."""
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "post_status", lambda **kw: sent.append(kw) or {"id": "1"}
        )
        monkeypatch.setattr(
            scheduler, "fetch_image", lambda url: (b"fake-bytes", "image/png")
        )
        monkeypatch.setattr(scheduler, "upload_media", lambda **kw: None)

        echo = _setup_echo(db_tmp, {"attach_image": 1})
        item = _item(image_url="https://example.com/photo.png")
        scheduler.process_echo(echo, item)

        assert len(sent) == 1
        assert sent[0].get("media_ids") is None

    def test_cw_and_image_combined(self, db_tmp, monkeypatch):
        """CW and image attachment should work together."""
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "post_status", lambda **kw: sent.append(kw) or {"id": "1"}
        )
        monkeypatch.setattr(
            scheduler, "fetch_image", lambda url: (b"bytes", "image/jpeg")
        )
        monkeypatch.setattr(
            scheduler, "upload_media", lambda **kw: {"id": "m-1"}
        )

        echo = _setup_echo(
            db_tmp, {"content_warning": "Spoilers", "attach_image": 1}
        )
        item = _item(image_url="https://example.com/cover.jpg")
        scheduler.process_echo(echo, item)

        assert len(sent) == 1
        assert sent[0]["spoiler_text"] == "Spoilers"
        assert sent[0]["sensitive"] is True
        assert sent[0]["media_ids"] == ["m-1"]


# ── Feed Parser Image Extraction Tests ───────────────────────────────────────


class TestImageExtraction:
    def test_rss_media_content(self):
        from feed_parser import _extract_rss_image

        entry = {"media_content": [{"url": "https://example.com/media.jpg"}]}
        assert _extract_rss_image(entry) == "https://example.com/media.jpg"

    def test_rss_media_thumbnail(self):
        from feed_parser import _extract_rss_image

        entry = {"media_thumbnail": [{"url": "https://example.com/thumb.jpg"}]}
        assert _extract_rss_image(entry) == "https://example.com/thumb.jpg"

    def test_rss_enclosure(self):
        from feed_parser import _extract_rss_image

        entry = {
            "enclosures": [
                {"type": "image/jpeg", "href": "https://example.com/enc.jpg"}
            ]
        }
        assert _extract_rss_image(entry) == "https://example.com/enc.jpg"

    def test_rss_enclosure_skips_non_image(self):
        from feed_parser import _extract_rss_image

        entry = {
            "enclosures": [
                {"type": "audio/mpeg", "href": "https://example.com/pod.mp3"}
            ]
        }
        assert _extract_rss_image(entry) == ""

    def test_rss_img_in_content(self):
        from feed_parser import _extract_rss_image

        entry = {
            "content": [{"value": '<p>Some text <img src="https://example.com/img.png"> more</p>'}]
        }
        assert _extract_rss_image(entry) == "https://example.com/img.png"

    def test_rss_img_in_summary(self):
        from feed_parser import _extract_rss_image

        entry = {"summary": '<img src="https://example.com/sum.jpg" alt="pic">'}
        assert _extract_rss_image(entry) == "https://example.com/sum.jpg"

    def test_rss_no_image(self):
        from feed_parser import _extract_rss_image

        entry = {"title": "No image here", "summary": "Just text"}
        assert _extract_rss_image(entry) == ""

    def test_rss_media_content_priority_over_enclosure(self):
        from feed_parser import _extract_rss_image

        entry = {
            "media_content": [{"url": "https://example.com/media.jpg"}],
            "enclosures": [{"type": "image/jpeg", "href": "https://example.com/enc.jpg"}],
        }
        assert _extract_rss_image(entry) == "https://example.com/media.jpg"

    def test_json_feed_image(self):
        from feed_parser import _extract_json_feed_image

        entry = {"image": "https://example.com/jf-image.jpg"}
        assert _extract_json_feed_image(entry) == "https://example.com/jf-image.jpg"

    def test_json_feed_banner(self):
        from feed_parser import _extract_json_feed_image

        entry = {"banner_image": "https://example.com/banner.jpg"}
        assert _extract_json_feed_image(entry) == "https://example.com/banner.jpg"

    def test_json_feed_img_in_content(self):
        from feed_parser import _extract_json_feed_image

        entry = {"content_html": '<p><img src="https://example.com/jf-content.png"></p>'}
        assert _extract_json_feed_image(entry) == "https://example.com/jf-content.png"

    def test_json_feed_no_image(self):
        from feed_parser import _extract_json_feed_image

        entry = {"title": "No image", "content_text": "Just text"}
        assert _extract_json_feed_image(entry) == ""


# ── Mastodon API Parameter Tests ──────────────────────────────────────────────


class TestMastodonPostStatusParams:
    def test_spoiler_text_included_when_provided(self, monkeypatch):
        import mastodon

        captured = {}

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"id": "1"}

        def _fake_post(method, url, **kw):
            data = kw.get("data")
            captured["data"] = data
            return FakeResponse()
        monkeypatch.setattr(mastodon, "pinned_request", _fake_post)
        mastodon.post_status(
            instance="https://example.com",
            access_token="tok",
            content="hello",
            spoiler_text="CW text",
        )
        assert captured["data"]["spoiler_text"] == "CW text"
        assert captured["data"]["sensitive"] is True

    def test_spoiler_text_omitted_when_empty(self, monkeypatch):
        import mastodon

        captured = {}

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"id": "1"}

        def _fake_post(method, url, **kw):
            data = kw.get("data")
            captured["data"] = data
            return FakeResponse()
        monkeypatch.setattr(mastodon, "pinned_request", _fake_post)
        mastodon.post_status(
            instance="https://example.com",
            access_token="tok",
            content="hello",
        )
        assert "spoiler_text" not in captured["data"]

    def test_media_ids_included_when_provided(self, monkeypatch):
        import mastodon

        captured = {}

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"id": "1"}

        def _fake_post(method, url, **kw):
            data = kw.get("data")
            captured["data"] = data
            return FakeResponse()
        monkeypatch.setattr(mastodon, "pinned_request", _fake_post)
        mastodon.post_status(
            instance="https://example.com",
            access_token="tok",
            content="hello",
            media_ids=["123", "456"],
        )
        assert captured["data"]["media_ids[]"] == ["123", "456"]

    def test_media_ids_omitted_when_none(self, monkeypatch):
        import mastodon

        captured = {}

        class FakeResponse:
            fetch_status = None

            def raise_for_status(self):
                pass

            def json(self):
                return {"id": "1"}

        def _fake_post(method, url, **kw):
            data = kw.get("data")
            captured["data"] = data
            return FakeResponse()
        monkeypatch.setattr(mastodon, "pinned_request", _fake_post)
        mastodon.post_status(
            instance="https://example.com",
            access_token="pres", content="hello",
        )
        assert "media_ids[]" not in captured["data"]


class TestMastodonUploadMedia:
    def test_upload_returns_dict_on_success(self, monkeypatch):
        import mastodon

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"id": "media-42"}

        def _fake_post(method, url, **kw):
            data = kw.get("data")
            return FakeResponse()
        monkeypatch.setattr(mastodon, "pinned_request", _fake_post)
        result = mastodon.upload_media(
            instance="https://example.com",
            access_token="tok",
            image_bytes=b"fake-image",
            content_type="image/jpeg",
            description="A test image",
        )
        assert result == {"id": "media-42"}

    def test_upload_returns_none_on_http_error(self, monkeypatch):
        import mastodon

        def _fake_post(method, url, **kw):
            data = kw.get("data")
            raise mastodon.httpx.HTTPStatusError(
                "500", request=None, response=None
            )
        monkeypatch.setattr(mastodon, "pinned_request", _fake_post)
        result = mastodon.upload_media(
            instance="https://example.com",
            access_token="tok",
            image_bytes=b"fake",
            content_type="image/jpeg",
        )
        assert result is None

    def test_upload_returns_none_on_network_error(self, monkeypatch):
        import mastodon

        def _fake_post(method, url, **kw):
            data = kw.get("data")
            raise mastodon.httpx.RequestError("network down")
        monkeypatch.setattr(mastodon, "pinned_request", _fake_post)
        result = mastodon.upload_media(
            instance="https://example.com",
            access_token="tok",
            image_bytes=b"fake",
            content_type="image/jpeg",
        )
        assert result is None
