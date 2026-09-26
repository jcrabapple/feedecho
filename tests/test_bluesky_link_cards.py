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
