"""Tests for content warnings and image attachment features."""

import os
import tempfile

import pytest

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

class TestContentWarning:
    def test_cw_sent_as_spoiler_text(self, db_tmp, monkeypatch, setup_echo):
        """CW text must be passed as spoiler_text and sensitive=True."""
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "post_status", lambda **kw: sent.append(kw) or {"id": "1"}
        )

        echo = setup_echo({"content_warning": "Spoilers"})
        scheduler.process_echo(echo, _item())

        assert len(sent) == 1
        assert sent[0]["spoiler_text"] == "Spoilers"
        assert sent[0]["sensitive"] is True

    def test_no_cw_means_no_spoiler_text(self, db_tmp, monkeypatch, setup_echo):
        """Without a CW, spoiler_text must be absent and sensitive=False."""
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "post_status", lambda **kw: sent.append(kw) or {"id": "1"}
        )

        echo = setup_echo({"content_warning": ""})
        scheduler.process_echo(echo, _item())

        assert len(sent) == 1
        assert sent[0]["sensitive"] is False
        # spoiler_text key should not be in the kwargs, or should be empty
        assert not sent[0].get("spoiler_text")

    def test_cw_empty_string_treated_as_no_cw(self, db_tmp, monkeypatch, setup_echo):
        """An empty CW string should not trigger sensitive=True."""
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "post_status", lambda **kw: sent.append(kw) or {"id": "1"}
        )

        echo = setup_echo({"content_warning": ""})
        scheduler.process_echo(echo, _item())

        assert sent[0]["sensitive"] is False

    def test_cw_strips_whitespace(self, db_tmp, monkeypatch, setup_echo):
        """CW text should be stripped before sending."""
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "post_status", lambda **kw: sent.append(kw) or {"id": "1"}
        )

        echo = setup_echo({"content_warning": "  Spacers  "})
        scheduler.process_echo(echo, _item())

        assert sent[0]["spoiler_text"] == "Spacers"

# ── Image Attachment Tests ───────────────────────────────────────────────────

class TestImageAttachment:
    def test_attach_image_disabled_no_upload(self, db_tmp, monkeypatch, setup_echo):
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

        echo = setup_echo({"attach_image": 0})
        item = _item(image_url="https://example.com/image.jpg")
        scheduler.process_echo(echo, item)

        assert len(sent) == 1
        assert len(upload_calls) == 0
        assert sent[0].get("media_ids") is None

    def test_attach_image_no_image_url_posts_text_only(self, db_tmp, monkeypatch, setup_echo):
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

        echo = setup_echo({"attach_image": 1})
        item = _item(image_url="")
        scheduler.process_echo(echo, item)

        assert len(sent) == 1
        assert len(upload_calls) == 0
        assert sent[0].get("media_ids") is None

    def test_attach_image_success(self, db_tmp, monkeypatch, setup_echo):
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

        echo = setup_echo({"attach_image": 1})
        item = _item(image_url="https://example.com/photo.jpg")
        scheduler.process_echo(echo, item)

        assert len(sent) == 1
        assert sent[0]["media_ids"] == ["media-123"]

    def test_attach_up_to_four_images_from_image_urls(self, db_tmp, monkeypatch, setup_echo):
        """When item carries image_urls, upload and attach up to 4 images."""
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "post_status", lambda **kw: sent.append(kw) or {"id": "1"}
        )
        uploaded_urls = []
        def fake_fetch(url):
            return (b"fake-bytes", "image/jpeg")
        def fake_upload(**kw):
            uploaded_urls.append(kw.get("image_bytes"))
            return {"id": f"media-{len(uploaded_urls)}"}
        monkeypatch.setattr(scheduler, "fetch_image", fake_fetch)
        monkeypatch.setattr(scheduler, "upload_media", fake_upload)

        echo = setup_echo({"attach_image": 1})
        item = _item(image_urls=[
            {"url": "https://example.com/1.jpg", "alt": "one"},
            {"url": "https://example.com/2.jpg", "alt": "two"},
            {"url": "https://example.com/3.jpg", "alt": ""},
            {"url": "https://example.com/4.jpg", "alt": "four"},
        ])
        scheduler.process_echo(echo, item)

        assert len(sent) == 1
        assert sent[0]["media_ids"] == ["media-1", "media-2", "media-3", "media-4"]

    def test_caps_at_four_images(self, db_tmp, monkeypatch, setup_echo):
        """More than 4 image_urls are truncated to 4."""
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "post_status", lambda **kw: sent.append(kw) or {"id": "1"}
        )
        counter = [0]
        def fake_fetch(url):
            return (b"fake-bytes", "image/jpeg")
        def fake_upload(**kw):
            counter[0] += 1
            return {"id": f"media-{counter[0]}"}
        monkeypatch.setattr(scheduler, "fetch_image", fake_fetch)
        monkeypatch.setattr(scheduler, "upload_media", fake_upload)

        echo = setup_echo({"attach_image": 1})
        item = _item(image_urls=[
            {"url": f"https://example.com/{i}.jpg", "alt": ""} for i in range(6)
        ])
        scheduler.process_echo(echo, item)

        assert len(sent) == 1
        assert len(sent[0]["media_ids"]) == 4
        assert counter[0] == 4

    def test_image_urls_json_string_supported(self, db_tmp, monkeypatch, setup_echo):
        """image_urls may arrive as a JSON string (from the feed_items column)."""
        import json as _json
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "post_status", lambda **kw: sent.append(kw) or {"id": "1"}
        )
        counter = [0]
        monkeypatch.setattr(
            scheduler, "fetch_image", lambda url: (b"fake-bytes", "image/jpeg")
        )
        monkeypatch.setattr(
            scheduler, "upload_media", lambda **kw: counter.__setitem__(0, counter[0] + 1) or {"id": f"media-{counter[0]}"}
        )

        echo = setup_echo({"attach_image": 1})
        item = _item(image_urls=_json.dumps([
            {"url": "https://example.com/a.jpg", "alt": "a"},
            {"url": "https://example.com/b.jpg", "alt": "b"},
        ]))
        scheduler.process_echo(echo, item)

        assert len(sent) == 1
        assert sent[0]["media_ids"] == ["media-1", "media-2"]

    def test_falls_back_to_single_image_url(self, db_tmp, monkeypatch, setup_echo):
        """Legacy rows without image_urls still attach the single image_url."""
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "post_status", lambda **kw: sent.append(kw) or {"id": "1"}
        )
        monkeypatch.setattr(
            scheduler, "fetch_image", lambda url: (b"fake-bytes", "image/jpeg")
        )
        monkeypatch.setattr(
            scheduler, "upload_media", lambda **kw: {"id": "media-legacy"}
        )

        echo = setup_echo({"attach_image": 1})
        item = _item(image_url="https://example.com/single.jpg", image_alt="legacy alt")
        scheduler.process_echo(echo, item)

        assert len(sent) == 1
        assert sent[0]["media_ids"] == ["media-legacy"]

    def test_skips_failed_fetch_and_continues(self, db_tmp, monkeypatch, setup_echo):
        """A failing image fetch skips that image but attaches the rest."""
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "post_status", lambda **kw: sent.append(kw) or {"id": "1"}
        )
        def flaky_fetch(url):
            if "broken" in url:
                return None
            return (b"fake-bytes", "image/jpeg")
        counter = [0]
        def fake_upload(**kw):
            counter[0] += 1
            return {"id": f"media-{counter[0]}"}
        monkeypatch.setattr(scheduler, "fetch_image", flaky_fetch)
        monkeypatch.setattr(scheduler, "upload_media", fake_upload)

        echo = setup_echo({"attach_image": 1})
        item = _item(image_urls=[
            {"url": "https://example.com/broken.jpg", "alt": ""},
            {"url": "https://example.com/good.jpg", "alt": ""},
        ])
        scheduler.process_echo(echo, item)

        assert len(sent) == 1
        assert sent[0]["media_ids"] == ["media-1"]

    def test_caller_image_alt_overrides_feed_alt_on_primary_slot(self, db_tmp, monkeypatch, setup_echo):
        """item['image_alt'] (user-edited) wins over feed alt for the first image."""
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "post_status", lambda **kw: sent.append(kw) or {"id": "1"}
        )
        descriptions = []
        monkeypatch.setattr(
            scheduler, "fetch_image", lambda url: (b"fake-bytes", "image/jpeg")
        )
        def fake_upload(**kw):
            descriptions.append(kw.get("description"))
            return {"id": f"media-{len(descriptions)}"}
        monkeypatch.setattr(scheduler, "upload_media", fake_upload)
        monkeypatch.setattr(scheduler.alt_text, "is_enabled", lambda user_id: True)
        monkeypatch.setattr(
            scheduler.alt_text, "generate_alt_text",
            lambda *a, **kw: "AI-GENERATED",
        )

        echo = setup_echo({"attach_image": 1})
        item = _item(
            image_alt="USER EDITED ALT",
            image_urls=[
                {"url": "https://example.com/1.jpg", "alt": ""},
                {"url": "https://example.com/2.jpg", "alt": ""},
            ],
        )
        scheduler.process_echo(echo, item)

        # Primary slot uses the user's alt; secondary slot falls back to AI.
        assert descriptions[0] == "USER EDITED ALT"
        assert descriptions[1] == "AI-GENERATED"

    def test_image_fetch_failure_posts_text_only(self, db_tmp, monkeypatch, setup_echo):
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

        echo = setup_echo({"attach_image": 1})
        item = _item(image_url="https://example.com/broken.jpg")
        scheduler.process_echo(echo, item)

        assert len(sent) == 1
        assert len(upload_calls) == 0
        assert sent[0].get("media_ids") is None

    def test_image_upload_failure_posts_text_only(self, db_tmp, monkeypatch, setup_echo):
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

        echo = setup_echo({"attach_image": 1})
        item = _item(image_url="https://example.com/photo.png")
        scheduler.process_echo(echo, item)

        assert len(sent) == 1
        assert sent[0].get("media_ids") is None

    def test_cw_and_image_combined(self, db_tmp, monkeypatch, setup_echo):
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

        echo = setup_echo(
            {"content_warning": "Spoilers", "attach_image": 1}
        )
        item = _item(image_url="https://example.com/cover.jpg")
        scheduler.process_echo(echo, item)

        assert len(sent) == 1
        assert sent[0]["spoiler_text"] == "Spoilers"
        assert sent[0]["sensitive"] is True
        assert sent[0]["media_ids"] == ["m-1"]

# ── Feed Parser Image Extraction Tests ───────────────────────────────────────

class TestImageExtraction:
    def test_extract_rss_images_multiple_img_tags(self):
        from feed_parser import _extract_rss_images

        entry = {
            "content": [{"value": (
                '<img src="https://example.com/1.jpg" alt="first">'
                '<img src="https://example.com/2.jpg" alt="second">'
                '<img src="https://example.com/3.jpg">'
            )}]
        }
        images = _extract_rss_images(entry)
        assert [img["url"] for img in images] == [
            "https://example.com/1.jpg",
            "https://example.com/2.jpg",
            "https://example.com/3.jpg",
        ]
        assert images[0]["alt"] == "first"
        assert images[1]["alt"] == "second"
        assert images[2]["alt"] == ""

    def test_extract_rss_images_media_and_enclosures(self):
        from feed_parser import _extract_rss_images

        entry = {
            "media_content": [
                {"url": "https://example.com/m1.jpg", "media_text": [{"text": "one"}]},
                {"url": "https://example.com/m2.jpg"},
            ],
            "enclosures": [
                {"type": "image/png", "href": "https://example.com/enc.png", "alt": "enclosure alt"},
                {"type": "audio/mpeg", "href": "https://example.com/pod.mp3"},
            ],
        }
        images = _extract_rss_images(entry)
        assert [img["url"] for img in images] == [
            "https://example.com/m1.jpg",
            "https://example.com/m2.jpg",
            "https://example.com/enc.png",
        ]
        assert images[0]["alt"] == "one"
        assert images[2]["alt"] == "enclosure alt"

    def test_extract_rss_images_caps_at_four(self):
        from feed_parser import _extract_rss_images

        entry = {
            "content": [{"value": "".join(
                f'<img src="https://example.com/{i}.jpg">' for i in range(7)
            )}]
        }
        images = _extract_rss_images(entry)
        assert len(images) == 4
        assert images[0]["url"] == "https://example.com/0.jpg"
        assert images[3]["url"] == "https://example.com/3.jpg"

    def test_extract_rss_images_dedupes_urls(self):
        from feed_parser import _extract_rss_images

        entry = {
            "media_content": [{"url": "https://example.com/dup.jpg"}],
            "content": [{"value": '<img src="https://example.com/dup.jpg" alt="in content">'}],
        }
        images = _extract_rss_images(entry)
        assert len(images) == 1
        assert images[0]["url"] == "https://example.com/dup.jpg"
        # media_content slot wins (its alt is empty); content img is deduped away
        assert images[0]["alt"] == ""

    def test_extract_rss_images_thumbnail_is_fallback_only(self):
        from feed_parser import _extract_rss_images

        entry = {
            "media_content": [{"url": "https://example.com/full.jpg"}],
            "media_thumbnail": [{"url": "https://example.com/thumb.jpg"}],
        }
        images = _extract_rss_images(entry)
        # Full image wins; its thumbnail must NOT occupy a second slot
        assert [img["url"] for img in images] == ["https://example.com/full.jpg"]

    def test_extract_rss_images_skips_video_media_content(self):
        from feed_parser import _extract_rss_images

        entry = {
            "media_content": [
                {"url": "https://example.com/clip.mp4", "medium": "video", "type": "video/mp4"},
                {"url": "https://example.com/photo.jpg", "medium": "image", "type": "image/jpeg"},
            ],
        }
        images = _extract_rss_images(entry)
        assert [img["url"] for img in images] == ["https://example.com/photo.jpg"]

    def test_extract_rss_images_skips_data_uris(self):
        from feed_parser import _extract_rss_images

        entry = {
            "content": [{"value": (
                '<img src="data:image/svg+xml;base64,AAAA">'
                '<img src="https://example.com/real.jpg">'
            )}]
        }
        images = _extract_rss_images(entry)
        assert [img["url"] for img in images] == ["https://example.com/real.jpg"]

    def test_extract_rss_images_unescapes_entities(self):
        from feed_parser import _extract_rss_images

        entry = {
            "content": [{"value": (
                '<img src="https://example.com/i.jpg?w=800&amp;q=80" alt="a &amp; b">'
            )}]
        }
        images = _extract_rss_images(entry)
        assert images[0]["url"] == "https://example.com/i.jpg?w=800&q=80"
        assert images[0]["alt"] == "a & b"

    def test_extract_json_feed_images_priority(self):
        from feed_parser import _extract_json_feed_images

        entry = {
            "image": {"url": "https://example.com/primary.jpg", "caption": "primary"},
            "attachments": [
                {"url": "https://example.com/supp.jpg", "mime_type": "image/jpeg"},
            ],
            "banner_image": {"url": "https://example.com/banner.jpg"},
        }
        images = _extract_json_feed_images(entry)
        # image first, then attachments; banner must NOT post alongside them
        assert [img["url"] for img in images] == [
            "https://example.com/primary.jpg",
            "https://example.com/supp.jpg",
        ]

    def test_extract_json_feed_images_banner_only_when_no_image(self):
        from feed_parser import _extract_json_feed_images

        entry = {"banner_image": {"url": "https://example.com/banner.jpg"}}
        images = _extract_json_feed_images(entry)
        assert [img["url"] for img in images] == ["https://example.com/banner.jpg"]

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
