"""Bluesky link cards (app.bsky.embed.external).

Bluesky renders a link card only when the posting client attaches an
external embed — the AppView does not generate cards for third-party
records. Text-only echoes with a link therefore fetch the page's Open
Graph metadata and attach a card (the Echofeed behavior members expect);
any failure degrades to the old cardless post.
"""

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


def _setup_bluesky_echo(db_tmp, echo_overrides=None):
    """Create a Bluesky account, feed, and echo. Returns the echo row."""
    import database

    echo_kwargs = {
        "destination_type": "bluesky",
        "destination_id": 1,
        "template": "{{ title }} {{ link }}",
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


def _stub_session(monkeypatch):
    """Stub Bluesky session functions so no network I/O happens in tests."""
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


OG_PAGE = """
<html><head>
<meta property="og:title" content="A Great &amp; Long Title">
<meta property="og:description" content="All about it.">
<meta property="og:image" content="https://example.com/cover.jpg">
<meta property="og:site_name" content="Example">
<title>Unused title tag</title>
</head><body>hello</body></html>
"""


# ── build_external_embed ─────────────────────────────────────────────────────


class TestBuildExternalEmbed:
    def test_shape_without_thumb(self):
        from bluesky import build_external_embed

        embed = build_external_embed(
            uri="https://example.com/a", title="T", description="D"
        )
        assert embed == {
            "$type": "app.bsky.embed.external",
            "external": {"uri": "https://example.com/a", "title": "T", "description": "D"},
        }

    def test_thumb_included_when_given(self):
        from bluesky import build_external_embed

        blob = {"$type": "blob", "ref": {"$link": "bafkrei"}}
        embed = build_external_embed(
            uri="https://example.com/a", title="T", thumb_blob=blob
        )
        assert embed["external"]["thumb"] is blob

    def test_no_thumb_key_when_none(self):
        from bluesky import build_external_embed

        embed = build_external_embed(uri="https://example.com/a", title="T")
        assert "thumb" not in embed["external"]

    def test_thumb_cap_is_the_external_lexicon_limit(self):
        """The external thumb kept its 1 MB lexicon maxSize when
        embed.images was raised to 2 MB — using MAX_BLOB_BYTES here would
        let a 1-2 MB blob through upload and get the whole record 400'd."""
        from bluesky import EXTERNAL_THUMB_MAX_BYTES, MAX_BLOB_BYTES

        assert EXTERNAL_THUMB_MAX_BYTES == 1_000_000
        assert EXTERNAL_THUMB_MAX_BYTES < MAX_BLOB_BYTES

    def test_long_title_and_description_truncated(self):
        from bluesky import (
            EXTERNAL_DESCRIPTION_MAX_CHARS,
            EXTERNAL_TITLE_MAX_CHARS,
            build_external_embed,
        )

        embed = build_external_embed(
            uri="https://example.com/a",
            title="x" * (EXTERNAL_TITLE_MAX_CHARS + 100),
            description="y" * (EXTERNAL_DESCRIPTION_MAX_CHARS + 100),
        )
        assert len(embed["external"]["title"]) == EXTERNAL_TITLE_MAX_CHARS
        assert (
            len(embed["external"]["description"]) == EXTERNAL_DESCRIPTION_MAX_CHARS
        )

    def test_whitespace_stripped_empty_ok(self):
        from bluesky import build_external_embed

        embed = build_external_embed(uri="https://example.com/a", title="  ", description="")
        assert embed["external"]["title"] == ""
        assert embed["external"]["description"] == ""


# ── first_link_uri ───────────────────────────────────────────────────────────


class TestFirstLinkUri:
    def test_none_when_no_facets(self):
        from bluesky import first_link_uri

        assert first_link_uri(None) is None
        assert first_link_uri([]) is None

    def test_returns_first_link_facet_uri(self):
        from bluesky import build_facets, first_link_uri

        facets = build_facets("a https://a.example.com b https://b.example.com")
        assert first_link_uri(facets) == "https://a.example.com"

    def test_rich_anchor_wins_over_bare_url(self):
        """build_facets seeds rich spans first, so first-wins keeps the
        anchor the reader actually saw."""
        from bluesky import build_facets, first_link_uri

        text = "Read this https://example.com/bare"
        facets = build_facets(text, extra_links=[(0, 9, "https://ex.com/anchor")])
        assert first_link_uri(facets) == "https://ex.com/anchor"

    def test_tags_only_returns_none(self):
        from bluesky import build_facets, first_link_uri

        assert first_link_uri(build_facets("nice #photo")) is None


# ── page metadata extraction ─────────────────────────────────────────────────


class TestExtractPageMetadata:
    def test_og_tags_parsed_and_unescaped(self):
        from feed_parser import _extract_page_metadata

        meta = _extract_page_metadata(OG_PAGE)
        assert meta["title"] == "A Great & Long Title"
        assert meta["description"] == "All about it."
        assert meta["image"] == "https://example.com/cover.jpg"

    def test_twitter_fallback_when_no_og(self):
        from feed_parser import _extract_page_metadata

        meta = _extract_page_metadata(
            '<meta name="twitter:title" content="T2">'
            '<meta name="twitter:image" content="https://example.com/t.jpg">'
        )
        assert meta["title"] == "T2"
        assert meta["image"] == "https://example.com/t.jpg"

    def test_og_wins_over_twitter(self):
        from feed_parser import _extract_page_metadata

        meta = _extract_page_metadata(
            '<meta name="twitter:title" content="T2">'
            '<meta property="og:title" content="OG wins">'
        )
        assert meta["title"] == "OG wins"

    def test_title_tag_and_meta_description_fallbacks(self):
        from feed_parser import _extract_page_metadata

        meta = _extract_page_metadata(
            "<html><head><title>Page &amp; Title</title>"
            '<meta name="description" content="plain desc">'
            "</head></html>"
        )
        assert meta["title"] == "Page & Title"
        assert meta["description"] == "plain desc"
        assert meta["image"] == ""

    def test_content_attribute_before_property(self):
        """Attribute order varies across sites; content-first tags parse."""
        from feed_parser import _extract_page_metadata

        meta = _extract_page_metadata(
            '<meta content="Reordered" property="og:title">'
        )
        assert meta["title"] == "Reordered"

    def test_nested_og_namespace_ignored(self):
        """article: / music: prefixed properties are not og: card fields."""
        from feed_parser import _extract_page_metadata

        meta = _extract_page_metadata(
            '<meta property="article:published_time" content="2026-01-01">'
        )
        assert meta["title"] == ""

    def test_apostrophe_in_double_quoted_value_not_truncated(self):
        """[^"']* would stop a double-quoted value at an apostrophe,
        corrupting every English title with a contraction."""
        from feed_parser import _extract_page_metadata

        meta = _extract_page_metadata(
            '<meta property="og:title" content="Today\'s Top Headline">'
            "<meta property=\"og:description\" content=\"It's a great day.\">"
        )
        assert meta["title"] == "Today's Top Headline"
        assert meta["description"] == "It's a great day."

    def test_single_quoted_value_with_double_quote_inside(self):
        from feed_parser import _extract_page_metadata

        meta = _extract_page_metadata(
            "<meta property='og:title' content='Say \"hi\" now'>"
        )
        assert meta["title"] == 'Say "hi" now'

    def test_twitter_image_src_normalized(self):
        """Legacy twitter:image:src carries the thumbnail; the extra colon
        must not discard it."""
        from feed_parser import _extract_page_metadata

        meta = _extract_page_metadata(
            '<meta name="twitter:image:src" content="https://example.com/legacy.jpg">'
        )
        assert meta["image"] == "https://example.com/legacy.jpg"

    def test_relative_image_resolved_against_base_url(self):
        from feed_parser import _extract_page_metadata

        meta = _extract_page_metadata(
            '<meta property="og:image" content="/img/cover.jpg">',
            base_url="https://example.com/posts/1",
        )
        assert meta["image"] == "https://example.com/img/cover.jpg"

    def test_title_tag_markup_stripped(self):
        from feed_parser import _extract_page_metadata

        meta = _extract_page_metadata(
            "<title>Breaking News &bull; <b>Live</b></title>"
        )
        assert meta["title"] == "Breaking News • Live"


# ── fetch_page_metadata (the network seam) ───────────────────────────────────


class TestFetchPageMetadata:
    OG = (
        "<html><head>"
        '<meta property="og:title" content="Net Title">'
        '<meta property="og:description" content="Net desc">'
        "</head></html>"
    ).encode()

    def _mock_fetch(self, monkeypatch, *, content=b"", raw_type="text/html", exc=None):
        import feed_parser

        closed = []
        monkeypatch.setattr(
            feed_parser,
            "ssrf_client",
            lambda urls: (type("C", (), {"close": staticmethod(lambda: closed.append(1))})(), object()),
        )

        def fake_fetch(client, url, headers, max_bytes, backend=None):
            if exc:
                raise exc
            return content, raw_type, {}

        monkeypatch.setattr(
            feed_parser, "_fetch_with_redirect_validation", fake_fetch
        )
        return closed

    def test_success_parses_metadata_and_closes_client(self, monkeypatch):
        import feed_parser

        closed = self._mock_fetch(monkeypatch, content=self.OG)
        meta = feed_parser.fetch_page_metadata("https://example.com/post")
        assert meta["title"] == "Net Title"
        assert meta["description"] == "Net desc"
        assert closed == [1]

    def test_non_html_content_type_returns_none(self, monkeypatch):
        import feed_parser

        self._mock_fetch(monkeypatch, content=b"%PDF-1.4", raw_type="application/pdf")
        assert feed_parser.fetch_page_metadata("https://example.com/doc.pdf") is None

    def test_unsafe_url_refused_without_fetch(self, monkeypatch):
        import feed_parser
        from feed_parser import SSRFError

        calls = []
        monkeypatch.setattr(
            feed_parser,
            "validate_outbound_url",
            lambda url: calls.append(url) or (_ for _ in ()).throw(SSRFError("private")),
        )
        assert feed_parser.fetch_page_metadata("http://127.0.0.1:8080/") is None
        assert calls == ["http://127.0.0.1:8080/"]

    def test_transport_error_returns_none(self, monkeypatch):
        import feed_parser

        self._mock_fetch(monkeypatch, exc=OSError("conn reset"))
        assert feed_parser.fetch_page_metadata("https://example.com/") is None

    def test_http_404_returns_none(self, monkeypatch):
        import httpx

        import feed_parser

        response = httpx.Response(404, request=httpx.Request("GET", "https://example.com/"))
        self._mock_fetch(monkeypatch, exc=httpx.HTTPStatusError("404", request=response.request, response=response))
        assert feed_parser.fetch_page_metadata("https://example.com/gone") is None

    def test_http_403_uses_fallback_proxy_when_configured(self, monkeypatch):
        import httpx

        import feed_parser

        response = httpx.Response(403, request=httpx.Request("GET", "https://example.com/"))
        self._mock_fetch(monkeypatch, exc=httpx.HTTPStatusError("403", request=response.request, response=response))
        monkeypatch.setattr("settings.FALLBACK_PROXY_URL", "https://proxy.example")
        proxy_calls = []
        monkeypatch.setattr(
            feed_parser,
            "_fetch_via_fallback_proxy",
            lambda url, headers, max_bytes: proxy_calls.append(url)
            or (self.OG, "text/html", {}),
        )
        meta = feed_parser.fetch_page_metadata("https://example.com/waf-post")
        assert proxy_calls == ["https://example.com/waf-post"]
        assert meta["title"] == "Net Title"

    def test_http_403_without_proxy_returns_none(self, monkeypatch):
        import httpx

        import feed_parser

        response = httpx.Response(403, request=httpx.Request("GET", "https://example.com/"))
        self._mock_fetch(monkeypatch, exc=httpx.HTTPStatusError("403", request=response.request, response=response))
        monkeypatch.setattr("settings.FALLBACK_PROXY_URL", "")
        assert feed_parser.fetch_page_metadata("https://example.com/waf-post") is None

    def test_relative_image_resolved_against_page_url(self, monkeypatch):
        import feed_parser

        page = (
            '<html><head><meta property="og:image" content="/img/c.jpg"></head></html>'
        ).encode()
        self._mock_fetch(monkeypatch, content=page)
        meta = feed_parser.fetch_page_metadata("https://example.com/posts/1")
        assert meta["image"] == "https://example.com/img/c.jpg"


# ── delivery wiring ──────────────────────────────────────────────────────────


class TestLinkCardDelivery:
    def test_card_attached_on_text_only_post_with_link(self, db_tmp, monkeypatch):
        import database
        import scheduler

        sent = []
        fetched = []
        monkeypatch.setattr(
            scheduler, "create_post", lambda **kw: sent.append(kw) or {"uri": "u", "cid": "c"}
        )
        _stub_session(monkeypatch)

        def fake_meta(url):
            fetched.append(url)
            return {"title": "OG Title", "description": "OG desc", "image": ""}

        monkeypatch.setattr(scheduler, "fetch_page_metadata", fake_meta)

        echo = _setup_bluesky_echo(db_tmp)
        ok = scheduler.process_echo(echo, _item())

        assert ok is True
        assert fetched == ["https://example.com/post/1"]
        embed = sent[0]["embed"]
        assert embed["$type"] == "app.bsky.embed.external"
        assert embed["external"]["uri"] == "https://example.com/post/1"
        assert embed["external"]["title"] == "OG Title"
        assert embed["external"]["description"] == "OG desc"
        assert "thumb" not in embed["external"]
        with database.get_db() as db:
            row = db.execute(
                "SELECT status FROM posted_items WHERE echo_id = 1"
            ).fetchone()
            assert row["status"] == "success"

    def test_thumb_attached_via_og_image(self, db_tmp, monkeypatch):
        import scheduler

        sent = []
        uploads = []
        monkeypatch.setattr(
            scheduler, "create_post", lambda **kw: sent.append(kw) or {"uri": "u", "cid": "c"}
        )
        _stub_session(monkeypatch)
        monkeypatch.setattr(
            scheduler,
            "fetch_page_metadata",
            lambda url: {
                "title": "T",
                "description": "",
                "image": "https://example.com/cover.jpg",
            },
        )
        thumb_fetches = []
        monkeypatch.setattr(
            scheduler,
            "fetch_image",
            lambda url: thumb_fetches.append(url)
            or (b"fake-image-bytes", "image/jpeg"),
        )
        monkeypatch.setattr(
            scheduler,
            "upload_blob",
            lambda **kw: uploads.append(kw) or {"$type": "blob", "ref": {"$link": "bafkrei"}},
        )

        echo = _setup_bluesky_echo(db_tmp)
        ok = scheduler.process_echo(echo, _item())

        assert ok is True
        assert thumb_fetches == ["https://example.com/cover.jpg"]
        embed = sent[0]["embed"]
        assert embed["external"]["thumb"]["ref"]["$link"] == "bafkrei"
        assert uploads[0]["image_bytes"] == b"fake-image-bytes"

    def test_og_image_fetch_failure_still_cards_without_thumb(self, db_tmp, monkeypatch):
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "create_post", lambda **kw: sent.append(kw) or {"uri": "u", "cid": "c"}
        )
        _stub_session(monkeypatch)
        monkeypatch.setattr(
            scheduler,
            "fetch_page_metadata",
            lambda url: {"title": "T", "description": "", "image": "https://example.com/dead.jpg"},
        )
        monkeypatch.setattr(scheduler, "fetch_image", lambda url: None)

        echo = _setup_bluesky_echo(db_tmp)
        ok = scheduler.process_echo(echo, _item())

        assert ok is True
        embed = sent[0]["embed"]
        assert embed["$type"] == "app.bsky.embed.external"
        assert "thumb" not in embed["external"]

    def test_metadata_failure_degrades_to_cardless(self, db_tmp, monkeypatch):
        """A dead page never blocks the echo: post ships without a card."""
        import database
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "create_post", lambda **kw: sent.append(kw) or {"uri": "u", "cid": "c"}
        )
        _stub_session(monkeypatch)
        monkeypatch.setattr(scheduler, "fetch_page_metadata", lambda url: None)

        echo = _setup_bluesky_echo(db_tmp)
        ok = scheduler.process_echo(echo, _item())

        assert ok is True
        assert sent[0]["embed"] is None
        with database.get_db() as db:
            row = db.execute(
                "SELECT status FROM posted_items WHERE echo_id = 1"
            ).fetchone()
            assert row["status"] == "success"

    def test_thumb_between_1mb_and_2mb_downscaled_not_uploaded_oversized(self, db_tmp, monkeypatch):
        """1-2 MB og:images used to ride MAX_BLOB_BYTES (2 MB) straight into
        the record, which the external thumb's 1 MB lexicon cap then 400'd,
        killing the post. They must downscale to fit EXTERNAL_THUMB_MAX_BYTES
        — or drop the thumb, never fail the post."""
        import scheduler

        sent = []
        uploads = []
        monkeypatch.setattr(
            scheduler, "create_post", lambda **kw: sent.append(kw) or {"uri": "u", "cid": "c"}
        )
        _stub_session(monkeypatch)
        monkeypatch.setattr(
            scheduler,
            "fetch_page_metadata",
            lambda url: {"title": "T", "description": "", "image": "https://example.com/big.jpg"},
        )
        monkeypatch.setattr(
            scheduler,
            "fetch_image",
            lambda url: (b"x" * 1_400_000, "image/jpeg"),
        )
        downscale_caps = []

        def fake_downscale(img_bytes, img_type, max_bytes):
            downscale_caps.append(max_bytes)
            if len(downscale_calls) == 0:
                downscale_calls.append(1)
                return (b"y" * 900_000, "image/jpeg")
            return None

        downscale_calls = []
        import images as images_mod

        monkeypatch.setattr(images_mod, "downscale_image", fake_downscale)
        monkeypatch.setattr(
            scheduler,
            "upload_blob",
            lambda **kw: uploads.append(kw) or {"$type": "blob", "ref": {"$link": "bafkreismall"}},
        )

        echo = _setup_bluesky_echo(db_tmp)
        ok = scheduler.process_echo(echo, _item())

        assert ok is True
        assert downscale_caps == [1_000_000]
        assert sent[0]["embed"]["external"]["thumb"]["ref"]["$link"] == "bafkreismall"
        assert uploads[0]["image_bytes"] == b"y" * 900_000

    def test_undownscalable_thumb_degrades_to_text_only_card(self, db_tmp, monkeypatch):
        import scheduler

        sent = []
        uploads = []
        monkeypatch.setattr(
            scheduler, "create_post", lambda **kw: sent.append(kw) or {"uri": "u", "cid": "c"}
        )
        _stub_session(monkeypatch)
        monkeypatch.setattr(
            scheduler,
            "fetch_page_metadata",
            lambda url: {"title": "T", "description": "", "image": "https://example.com/huge.jpg"},
        )
        monkeypatch.setattr(
            scheduler,
            "fetch_image",
            lambda url: (b"x" * 1_400_000, "image/jpeg"),
        )
        import images as images_mod

        monkeypatch.setattr(images_mod, "downscale_image", lambda *a: None)
        monkeypatch.setattr(
            scheduler,
            "upload_blob",
            lambda **kw: uploads.append(kw) or {"$type": "blob"},
        )

        echo = _setup_bluesky_echo(db_tmp)
        ok = scheduler.process_echo(echo, _item())

        assert ok is True
        assert uploads == []
        assert sent[0]["embed"]["$type"] == "app.bsky.embed.external"
        assert "thumb" not in sent[0]["embed"]["external"]

    def test_missing_title_falls_back_to_hostname(self, db_tmp, monkeypatch):
        """A title-less page must not ship an empty card container; the
        official app shows the host in the same spot."""
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "create_post", lambda **kw: sent.append(kw) or {"uri": "u", "cid": "c"}
        )
        _stub_session(monkeypatch)
        monkeypatch.setattr(
            scheduler,
            "fetch_page_metadata",
            lambda url: {"title": "", "description": "desc only", "image": ""},
        )

        echo = _setup_bluesky_echo(db_tmp)
        ok = scheduler.process_echo(echo, _item())

        assert ok is True
        embed = sent[0]["embed"]
        assert embed["external"]["title"] == "example.com"
        assert embed["external"]["description"] == "desc only"

    def test_metadata_fetched_once_across_reauth_retry(self, db_tmp, monkeypatch):
        """The page fetch is prefetched per dispatch; a session re-auth must
        rebuild the card from the cached metadata, not re-hit the origin."""
        import scheduler
        from bluesky import BlueskyAuthError

        sent = []
        meta_calls = []
        monkeypatch.setattr(
            scheduler, "create_post", lambda **kw: sent.append(kw) or {"uri": "u", "cid": "c"}
        )
        _stub_session(monkeypatch)

        def flaky_post(**kw):
            if len(sent) == 0:
                sent.append(kw)
                raise BlueskyAuthError("ExpiredToken")
            sent.append(kw)
            return {"uri": "u", "cid": "c"}

        monkeypatch.setattr(scheduler, "create_post", flaky_post)
        monkeypatch.setattr(
            scheduler,
            "fetch_page_metadata",
            lambda url: meta_calls.append(url)
            or {"title": "T", "description": "", "image": ""},
        )

        echo = _setup_bluesky_echo(db_tmp)
        ok = scheduler.process_echo(echo, _item())

        assert ok is True
        assert meta_calls == ["https://example.com/post/1"]
        assert sent[0]["embed"]["external"]["title"] == "T"

    def test_metadata_exception_degrades_to_cardless(self, db_tmp, monkeypatch):
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "create_post", lambda **kw: sent.append(kw) or {"uri": "u", "cid": "c"}
        )
        _stub_session(monkeypatch)

        def boom(url):
            raise RuntimeError("metadata bug")

        monkeypatch.setattr(scheduler, "fetch_page_metadata", boom)

        echo = _setup_bluesky_echo(db_tmp)
        ok = scheduler.process_echo(echo, _item())

        assert ok is True
        assert sent[0]["embed"] is None

    def test_image_embed_suppresses_link_card(self, db_tmp, monkeypatch):
        """When feed images attach, the post carries embed.images — Bluesky
        allows one embed, so the card must not even be attempted."""
        import scheduler

        sent = []
        meta_calls = []
        monkeypatch.setattr(
            scheduler, "create_post", lambda **kw: sent.append(kw) or {"uri": "u", "cid": "c"}
        )
        _stub_session(monkeypatch)
        monkeypatch.setattr(
            scheduler,
            "fetch_page_metadata",
            lambda url: meta_calls.append(url)
            or {"title": "T", "description": "", "image": ""},
        )
        monkeypatch.setattr(
            scheduler, "fetch_image", lambda url: (b"fake-image-bytes", "image/jpeg")
        )
        monkeypatch.setattr(
            scheduler,
            "upload_blob",
            lambda **kw: {"$type": "blob", "ref": {"$link": "bafkreifake"}},
        )
        import alt_text

        monkeypatch.setattr(alt_text, "is_enabled", lambda user_id=1: False)

        echo = _setup_bluesky_echo(db_tmp, {"attach_image": 1})
        item = _item(image_url="https://example.com/photo.jpg")
        ok = scheduler.process_echo(echo, item)

        assert ok is True
        assert sent[0]["embed"]["$type"] == "app.bsky.embed.images"
        assert meta_calls == []

    def test_card_uri_uses_rich_anchor_over_bare_link(self, db_tmp, monkeypatch):
        """{{ content_html }} renders rich spans; the card must point at the
        anchor the reader sees, not the trailing bare permalink."""
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "create_post", lambda **kw: sent.append(kw) or {"uri": "u", "cid": "c"}
        )
        _stub_session(monkeypatch)

        card_urls = []

        def fake_meta(url):
            card_urls.append(url)
            return {"title": "T", "description": "", "image": ""}

        monkeypatch.setattr(scheduler, "fetch_page_metadata", fake_meta)

        echo = _setup_bluesky_echo(
            db_tmp,
            {"template": "{{ content_html }} https://example.com/permalink"},
        )
        item = _item(
            link="https://example.com/permalink",
            content_html='<p>Read <a href="https://ex.com/anchor">this</a></p>',
        )
        ok = scheduler.process_echo(echo, item)

        assert ok is True
        assert card_urls == ["https://ex.com/anchor"]
        assert sent[0]["embed"]["external"]["uri"] == "https://ex.com/anchor"

    def test_no_link_means_no_metadata_fetch(self, db_tmp, monkeypatch):
        import scheduler

        sent = []
        meta_calls = []
        monkeypatch.setattr(
            scheduler, "create_post", lambda **kw: sent.append(kw) or {"uri": "u", "cid": "c"}
        )
        _stub_session(monkeypatch)
        monkeypatch.setattr(
            scheduler,
            "fetch_page_metadata",
            lambda url: meta_calls.append(url) or {"title": "T", "description": "", "image": ""},
        )

        echo = _setup_bluesky_echo(db_tmp)
        item = _item(title="Just words, no link", link="")
        ok = scheduler.process_echo(echo, item)

        assert ok is True
        assert sent[0]["embed"] is None
        assert meta_calls == []

    def test_card_built_with_the_session_that_posts(self, db_tmp, monkeypatch):
        """The thumb upload must use the post's session, so a re-auth retry
        rebuilds the card with the fresh token instead of shipping the old
        one's (possibly rejected) embed."""
        import database
        import scheduler
        from bluesky import BlueskyAuthError

        sent = []
        uploads = []
        logins = {"count": 0}
        monkeypatch.setattr(
            scheduler, "create_post", lambda **kw: sent.append(kw) or {"uri": "u", "cid": "c"}
        )
        _stub_session(monkeypatch)

        def counting_session(pds, handle, pw):
            logins["count"] += 1
            return {
                "did": "did:plc:test123",
                "access_jwt": f"aj-{logins['count']}",
                "refresh_jwt": f"rj-{logins['count']}",
            }

        monkeypatch.setattr(scheduler, "create_session", counting_session)
        monkeypatch.setattr(
            scheduler,
            "fetch_page_metadata",
            lambda url: {"title": "T", "description": "", "image": "https://example.com/c.jpg"},
        )
        monkeypatch.setattr(
            scheduler, "fetch_image", lambda url: (b"fake-image-bytes", "image/jpeg")
        )

        def flaky_upload(**kw):
            uploads.append(kw["access_jwt"])
            if logins["count"] == 1:
                raise BlueskyAuthError("Blob upload rejected: session expired")
            return {"$type": "blob", "ref": {"$link": "bafkreifresh"}}

        monkeypatch.setattr(scheduler, "upload_blob", flaky_upload)

        echo = _setup_bluesky_echo(db_tmp)
        ok = scheduler.process_echo(echo, _item())

        assert ok is True
        # The first attempt's dead session produced no durable post; the
        # retry (fresh session) shipped the card with the fresh thumb.
        assert len(sent) == 1
        assert sent[0]["embed"]["external"]["thumb"]["ref"]["$link"] == "bafkreifresh"
        assert sent[0]["access_jwt"] == "aj-2"
