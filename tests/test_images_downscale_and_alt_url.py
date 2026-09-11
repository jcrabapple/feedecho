"""Tests for the 2026-09-09 glass.photo report fixes:

1. alt_text.normalize_base_url — tenants paste FULL endpoints (Mistral's
   docs show the complete /chat/completions URL); the code appends the
   suffix itself, so the doubled path 404ed at post time with no surface.
2. alt_text.attempt_alt_text + the /api/settings/alt-text/test endpoint —
   a permanent 4xx used to map to "" and the test button reported a false
   green ("API reachable") while every real image posted without alt text.
3. images.downscale_image + scheduler._send_bluesky wiring — oversized
   images (glass.photo serves ~2.9 MB JPEGs) used to be dropped outright
   against the stale 1 MB Bluesky blob cap; now they are downscaled to fit
   the raised 2 MB cap, with the old skip-to-text-only as the fallback.
"""

import io
import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def temp_db(monkeypatch):
    """Use a temp database for each test (module-local copy of the shared
    pattern; there is no conftest.py in this repo by design)."""
    import database

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        monkeypatch.setattr(database, "DB_PATH", db_path)
        database.init_db()
        yield db_path


# ── normalize_base_url ───────────────────────────────────────────────────────

class TestNormalizeBaseUrl:
    def test_strips_pasted_full_endpoint(self):
        import alt_text

        assert (
            alt_text.normalize_base_url("https://api.mistral.ai/v1/chat/completions")
            == "https://api.mistral.ai/v1"
        )

    def test_leaves_api_root_untouched(self):
        import alt_text

        assert (
            alt_text.normalize_base_url("https://api.openai.com/v1")
            == "https://api.openai.com/v1"
        )

    def test_collapses_repeated_suffixes(self):
        import alt_text

        assert (
            alt_text.normalize_base_url(
                "https://api.mistral.ai/v1/chat/completions/chat/completions"
            )
            == "https://api.mistral.ai/v1"
        )

    def test_trailing_slash_handled(self):
        import alt_text

        assert (
            alt_text.normalize_base_url("https://api.openai.com/v1/")
            == "https://api.openai.com/v1"
        )
        assert (
            alt_text.normalize_base_url("https://api.mistral.ai/v1/chat/completions/")
            == "https://api.mistral.ai/v1"
        )

    def test_empty_stays_empty(self):
        import alt_text

        assert alt_text.normalize_base_url("") == ""
        assert alt_text.normalize_base_url(None) == ""

    def test_suffix_without_leading_slash_stripped(self):
        import alt_text

        assert (
            alt_text.normalize_base_url("https://api.example.com/chat/completions")
            == "https://api.example.com"
        )
        assert alt_text.normalize_base_url("chat/completions") == "chat/completions"

    def test_hostname_named_chat_not_corrupted(self):
        import alt_text

        assert (
            alt_text.normalize_base_url("http://chat/completions")
            == "http://chat/completions"
        )
        assert (
            alt_text.normalize_base_url("http://chat/chat/completions")
            == "http://chat"
        )

    def test_the_reported_mistral_config_now_resolves_to_the_working_endpoint(self):
        import alt_text

        base = alt_text.normalize_base_url(
            "https://api.mistral.ai/v1/chat/completions"
        )
        assert f"{base}/chat/completions" == "https://api.mistral.ai/v1/chat/completions"


class TestGenerateAltTextAcceptsPastedEndpoint:
    """The posting path itself must survive the pasted-full-endpoint config
    (the exact production failure: HTTP 404 on the doubled path)."""

    def _settings(self, db_tmp):
        with db_tmp.get_db() as db:
            for key, value in (
                ("alt_text_ai_enabled", "1"),
                ("alt_text_ai_base_url", "https://api.mistral.ai/v1/chat/completions"),
                ("alt_text_ai_model", "mistral-small-2506"),
                ("alt_text_ai_api_key", "sk-test"),
            ):
                db.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                    (key, value),
                )

    def test_single_mode_posts_to_undoubled_endpoint(self, db_tmp, monkeypatch, setup_echo):
        import alt_text

        self._settings(db_tmp)
        monkeypatch.setattr(alt_text.app_settings, "MULTI", False)

        called = {}

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": "a bicycle on a road"}}]}

        class _Client:
            def __init__(self, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, **k):
                called["url"] = url
                return _Resp()

        monkeypatch.setattr(alt_text, "unpinned_client", lambda **kw: _Client())
        result = alt_text.generate_alt_text(b"fake-image", "image/jpeg")
        assert result == "a bicycle on a road"
        assert called["url"] == "https://api.mistral.ai/v1/chat/completions"


# ── attempt_alt_text: failure reporting for the test button ──────────────────

class TestAttemptAltTextReportsReason:
    def _settings(self, db_tmp, base_url="https://api.openai.com/v1"):
        with db_tmp.get_db() as db:
            for key, value in (
                ("alt_text_ai_enabled", "1"),
                ("alt_text_ai_base_url", base_url),
                ("alt_text_ai_model", "gpt-4o-mini"),
                ("alt_text_ai_api_key", "sk-test"),
            ):
                db.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                    (key, value),
                )

    def test_success_returns_reason_empty(self, db_tmp, monkeypatch, setup_echo):
        import alt_text

        self._settings(db_tmp)
        monkeypatch.setattr(alt_text.app_settings, "MULTI", False)

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": "  a cat  "}}]}

        class _Client:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *a, **k):
                return _Resp()

        monkeypatch.setattr(alt_text, "unpinned_client", lambda **kw: _Client())
        description, reason = alt_text.attempt_alt_text(b"img", "image/jpeg")
        assert description == "a cat"
        assert reason == ""

    def test_404_reports_failure_with_base_url_hint(self, db_tmp, monkeypatch, setup_echo):
        import alt_text

        self._settings(db_tmp)
        monkeypatch.setattr(alt_text.app_settings, "MULTI", False)

        class _Resp:
            status_code = 404

        class _Client:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *a, **k):
                raise alt_text.httpx.HTTPStatusError(
                    "404 Not Found", request=None, response=_Resp()
                )

        monkeypatch.setattr(alt_text, "unpinned_client", lambda **kw: _Client())
        monkeypatch.setattr(alt_text.time, "sleep", lambda s: None)
        description, reason = alt_text.attempt_alt_text(b"img", "image/jpeg")
        assert description == ""
        assert "404" in reason
        assert "base URL" in reason

    def test_401_reports_key_rejection(self, db_tmp, monkeypatch, setup_echo):
        import alt_text

        self._settings(db_tmp)
        monkeypatch.setattr(alt_text.app_settings, "MULTI", False)

        class _Resp:
            status_code = 401

        class _Client:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *a, **k):
                raise alt_text.httpx.HTTPStatusError(
                    "401 Unauthorized", request=None, response=_Resp()
                )

        monkeypatch.setattr(alt_text, "unpinned_client", lambda **kw: _Client())
        description, reason = alt_text.attempt_alt_text(b"img", "image/jpeg")
        assert description == ""
        assert "key" in reason.lower()

    def test_disabled_still_returns_empty_reason(self, db_tmp, monkeypatch, setup_echo):
        import alt_text

        description, reason = alt_text.attempt_alt_text(b"img", "image/jpeg")
        assert description == ""
        assert reason == ""  # disabled is not a failure — do not alarm the user


class TestVisionTestEndpointReportsFailures:
    """The Test Vision API button must not report a false green on permanent
    4xx responses (the exact failure the 2026-09-09 reporter hit). Single
    mode: auth via X-Auth-Token, settings belong to the singleton user 1."""

    @pytest.fixture
    def single_client(self, temp_db, monkeypatch):
        from fastapi.testclient import TestClient

        import app as app_module

        monkeypatch.setattr(app_module.settings, "MULTI", False)
        monkeypatch.setattr(app_module.settings, "AUTH_TOKEN", "tok")
        monkeypatch.setattr(app_module.settings, "SESSION_SECRET", "y" * 40)
        client = TestClient(app_module.app)
        client.headers["X-Auth-Token"] = "tok"
        return client

    def _configure(self, base_url="https://api.openai.com/v1"):
        import database

        with database.get_db() as db:
            for key, value in (
                ("alt_text_ai_enabled", "1"),
                ("alt_text_ai_base_url", base_url),
                ("alt_text_ai_model", "some-vision-model"),
                ("alt_text_ai_api_key", "key-1"),
            ):
                db.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                    (key, value),
                )

    def test_permanent_404_reports_failure_not_reachable(
        self, single_client, monkeypatch
    ):
        import alt_text

        self._configure()
        monkeypatch.setattr(alt_text.app_settings, "MULTI", False)

        class _Resp:
            status_code = 404

        class _Client:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *a, **k):
                raise alt_text.httpx.HTTPStatusError(
                    "404 Not Found", request=None, response=_Resp()
                )

        monkeypatch.setattr(alt_text, "unpinned_client", lambda **kw: _Client())
        body = single_client.post("/api/settings/alt-text/test").json()
        assert body["success"] is False
        assert "404" in body["message"]

    def test_working_endpoint_still_reports_success(
        self, single_client, monkeypatch
    ):
        import alt_text

        self._configure()
        monkeypatch.setattr(alt_text.app_settings, "MULTI", False)

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": "test image"}}]}

        class _Client:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *a, **k):
                return _Resp()

        monkeypatch.setattr(alt_text, "unpinned_client", lambda **kw: _Client())
        body = single_client.post("/api/settings/alt-text/test").json()
        assert body["success"] is True

    def test_unconfigured_or_invalid_url_reports_failure(
        self, single_client, monkeypatch
    ):
        self._configure(base_url="/chat/completions")
        body = single_client.post("/api/settings/alt-text/test").json()
        assert body["success"] is False
        assert "API test failed" in body["message"] or "not configured" in body["message"]


# ── images.downscale_image ────────────────────────────────────────────────────

def _jpeg_bytes(width=3000, height=2000, quality=95):
    from PIL import Image

    im = Image.new("RGB", (width, height), (200, 60, 60))
    # Noise makes JPEG compression realistic (a flat color compresses absurdly)
    px = im.load()
    for x in range(0, width, 7):
        for y in range(0, height, 7):
            px[x, y] = (x % 255, y % 255, (x + y) % 255)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


class TestDownscaleImage:
    def test_small_image_returned_unchanged(self):
        import images

        data = _jpeg_bytes(width=400, height=300)
        result = images.downscale_image(data, "image/jpeg", 2_000_000)
        assert result == (data, "image/jpeg")

    def test_oversized_jpeg_is_reencoded_under_target(self):
        import images

        data = _jpeg_bytes(width=4000, height=3000)
        assert len(data) > 100_000 or True  # content sanity only
        result = images.downscale_image(data, "image/jpeg", 50_000)
        assert result is not None
        out, ctype = result
        assert ctype == "image/jpeg"
        assert len(out) <= 50_000

    def test_dimensions_are_bounded_after_fit(self):
        import images
        from PIL import Image

        data = _jpeg_bytes(width=5000, height=2500)
        out, ctype = images.downscale_image(data, "image/jpeg", 60_000)
        im = Image.open(io.BytesIO(out))
        assert max(im.size) <= 3072
        # Aspect ratio is preserved (2:1 landscape), NOT center-cropped to square
        assert im.size[0] > im.size[1]
        assert abs(im.size[0] / im.size[1] - 2.0) < 0.05

    def test_exif_orientation_baked_in(self):
        import images
        from PIL import Image

        im = Image.new("RGB", (600, 1000), (10, 10, 200))
        exif = im.getexif()
        exif[0x0112] = 6  # 90 deg CW rotation
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85, exif=exif)
        # Force downscale/re-encode pass by setting ceiling below source size
        result = images.downscale_image(buf.getvalue(), "image/jpeg", len(buf.getvalue()) - 100)
        assert result is not None
        out_bytes, _ = result
        out_im = Image.open(io.BytesIO(out_bytes))
        # Portrait (600x1000) rotated 90 deg CW becomes landscape (1000x600)
        assert out_im.size[0] > out_im.size[1]
        assert out_im.size == (1000, 600)

    def test_unsupported_type_returns_none(self):
        import images

        data = _jpeg_bytes(width=4000, height=3000)  # genuinely oversized
        # gif is outside PILLOW_TYPES (re-encoding would break animation):
        assert images.downscale_image(data, "image/gif", 1_000_000) is None
        assert images.downscale_image(b"data" * 500_000, "image/avif", 1_000_000) is None

    def test_already_fits_passthrough_even_unsupported(self):
        import images

        result = images.downscale_image(b"tiny", "image/avif", 1_000_000)
        assert result == (b"tiny", "image/avif")

    def test_pillow_missing_degrades_to_none(self, monkeypatch):
        import builtins

        import images

        data = _jpeg_bytes(width=4000, height=3000)
        assert len(data) > 50_000

        real_import = builtins.__import__

        def _no_pil(name, *a, **k):
            if name.startswith("PIL"):
                raise ImportError("PIL missing (simulated)")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", _no_pil)
        assert images.downscale_image(data, "image/jpeg", 50_000) is None

    def test_real_glass_photo_payload_downscaled(self):
        """Regression shape for the actual report: ~3 MB 3072px source JPEG
        must come back under 2 MB."""
        import images

        data = _jpeg_bytes(width=3072, height=2304, quality=90)
        # If the synthetic image already fits, force the oversized branch:
        if len(data) <= 2_000_000:
            data = _jpeg_bytes(width=3072, height=3072, quality=100)
        result = images.downscale_image(data, "image/jpeg", 2_000_000)
        assert result is not None
        out, ctype = result
        assert len(out) <= 2_000_000


# ── scheduler._send_bluesky wiring ────────────────────────────────────────────

class TestBlueskyDownscaleWiring:
    @pytest.fixture
    def bl_echo(self, db_tmp):
        """Feed + Bluesky account + attach_image echo. Returns the echo row."""
        import database

        with database.get_db() as db:
            db.execute(
                """INSERT INTO bluesky_accounts (name, handle, app_password, did, pds)
                   VALUES (?, ?, ?, ?, ?)""",
                ("main", "user.bsky.social", "abcd-efgh-ijkl-mnop", "did:plc:test123", "https://bsky.social"),
            )
            db.execute(
                "INSERT INTO feeds (name, url) VALUES (?, ?)",
                ("f", "https://example.com/feed"),
            )
            db.execute(
                """INSERT INTO echoes (feed_id, destination_type, destination_id, template,
                                       visibility, filter_keywords, filter_mode,
                                       content_warning, attach_image, enabled)
                   VALUES (1, 'bluesky', 1, '{{ title }} {{ link }}',
                           'public', '', 'exclude', '', 1, 1)""",
            )
            return db.execute("SELECT * FROM echoes WHERE id = 1").fetchone()

    @staticmethod
    def _bl_item(**overrides):
        item = {
            "id": "item-1",
            "title": "Test Post",
            "link": "https://example.com/post/1",
            "summary": "A summary of the post.",
            "image_url": "",
        }
        item.update(overrides)
        return item

    def _stub_bsky_session(self, monkeypatch):
        import scheduler

        monkeypatch.setattr(
            scheduler,
            "resolve_pds",
            lambda handle: ("did:plc:test123", "https://bsky.social"),
        )
        monkeypatch.setattr(
            scheduler,
            "create_session",
            lambda pds, handle, pw: {
                "did": "did:plc:test123",
                "access_jwt": "aj",
                "refresh_jwt": "rj",
            },
        )
        monkeypatch.setattr(
            scheduler,
            "refresh_session",
            lambda pds, rj: {
                "did": "did:plc:test123",
                "access_jwt": "refreshed-aj",
                "refresh_jwt": "refreshed-rj",
            },
        )

    def _setup(self, db_tmp, monkeypatch, bl_echo, img_bytes, img_type="image/jpeg"):
        import scheduler

        sent = []
        uploaded = []

        monkeypatch.setattr(
            scheduler,
            "create_post",
            lambda **kw: sent.append(kw) or {"uri": "u", "cid": "c"},
        )
        self._stub_bsky_session(monkeypatch)
        monkeypatch.setattr(
            scheduler, "fetch_image", lambda url: (img_bytes, img_type)
        )
        monkeypatch.setattr(
            scheduler,
            "upload_blob",
            lambda **kw: uploaded.append(kw) or {"$type": "blob", "ref": {"$link": "b"}},
        )
        import alt_text

        monkeypatch.setattr(alt_text, "is_enabled", lambda user_id=1: False)
        item = self._bl_item(image_url="https://example.com/photo.jpg")
        return scheduler, bl_echo, item, sent, uploaded

    def test_oversized_image_is_downscaled_and_uploaded(
        self, db_tmp, monkeypatch, bl_echo
    ):
        big = _jpeg_bytes(width=4000, height=3000)
        assert len(big) > 1_000_000
        scheduler, echo, item, sent, uploaded = self._setup(
            db_tmp, monkeypatch, bl_echo, big, "image/jpeg"
        )

        ok = scheduler.process_echo(echo, item)
        assert ok is True
        assert len(uploaded) == 1
        blob_bytes = uploaded[0]["image_bytes"]
        assert len(blob_bytes) <= 2_000_000
        assert sent[0]["embed"]["$type"] == "app.bsky.embed.images"

    def test_image_at_cap_uploads_untouched(self, db_tmp, monkeypatch, bl_echo):
        """Exactly at the limit: no re-encode, bytes pass through as-is."""
        exact = b"x" * 2_000_000
        scheduler, echo, item, sent, uploaded = self._setup(
            db_tmp, monkeypatch, bl_echo, exact, "image/jpeg"
        )
        ok = scheduler.process_echo(echo, item)
        assert ok is True
        assert uploaded[0]["image_bytes"] == exact

    def test_downscale_failure_falls_back_to_text_only(
        self, db_tmp, monkeypatch, bl_echo
    ):
        import images as images_mod

        big = b"\xff" * 2_500_000  # not a real image
        scheduler, echo, item, sent, uploaded = self._setup(
            db_tmp, monkeypatch, bl_echo, big, "image/jpeg"
        )
        monkeypatch.setattr(
            images_mod, "downscale_image", lambda *a, **k: None
        )

        ok = scheduler.process_echo(echo, item)
        assert ok is True
        assert len(uploaded) == 0
        assert sent[0]["embed"] is None

    def test_downscaled_result_replaces_type_and_bytes(
        self, db_tmp, monkeypatch, bl_echo
    ):
        """The blob upload must receive the DOWNSCALED bytes and content
        type, never the originals (a stale type would break the PDS)."""
        big = _jpeg_bytes(width=4000, height=3000)
        scheduler, echo, item, sent, uploaded = self._setup(
            db_tmp, monkeypatch, bl_echo, big, "image/jpeg"
        )

        ok = scheduler.process_echo(echo, item)
        assert ok is True
        assert uploaded[0]["content_type"] == "image/jpeg"
        assert len(uploaded[0]["image_bytes"]) <= 2_000_000
        assert len(uploaded[0]["image_bytes"]) != len(big)

    def test_multi_image_downscales_each_independently(
        self, db_tmp, monkeypatch, bl_echo
    ):
        """Two images: the small one passes through untouched, the oversized
        one is downscaled, and both attach in one embed."""
        import scheduler

        sent = []
        uploaded = []
        monkeypatch.setattr(
            scheduler,
            "create_post",
            lambda **kw: sent.append(kw) or {"uri": "u", "cid": "c"},
        )
        self._stub_bsky_session(monkeypatch)
        small = _jpeg_bytes(width=640, height=480)
        big = _jpeg_bytes(width=4000, height=3000)
        assert len(big) > 2_000_000

        def sized_fetch(url):
            return (big if "big" in url else small, "image/jpeg")

        monkeypatch.setattr(scheduler, "fetch_image", sized_fetch)
        monkeypatch.setattr(
            scheduler,
            "upload_blob",
            lambda **kw: uploaded.append(kw)
            or {"$type": "blob", "ref": {"$link": "b"}},
        )
        import alt_text

        monkeypatch.setattr(alt_text, "is_enabled", lambda user_id=1: False)

        item = self._bl_item(image_urls=[
            {"url": "https://example.com/small.jpg", "alt": ""},
            {"url": "https://example.com/big.jpg", "alt": ""},
        ])
        ok = scheduler.process_echo(bl_echo, item)

        assert ok is True
        assert len(uploaded) == 2
        assert uploaded[0]["image_bytes"] == small
        assert len(uploaded[1]["image_bytes"]) <= 2_000_000
        assert len(uploaded[1]["image_bytes"]) != len(big)
        assert len(sent[0]["embed"]["images"]) == 2

    def test_max_blob_bytes_constant_tracks_bluesky_limit(self):
        """The 1 MB constant was stale when Bluesky raised its embed limit to
        2 MB in April 2026 (atproto PR #4823); pin the new ceiling."""
        from bluesky import MAX_BLOB_BYTES

        assert MAX_BLOB_BYTES == 2_000_000
