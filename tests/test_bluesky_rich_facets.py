"""Bluesky rich rendering: content_html anchors become link facets (#41)."""

import pytest


# ── template_engine.render_template_rich ─────────────────────────────────────

class TestRenderTemplateRich:
    def test_anchor_becomes_text_plus_link_span(self):
        from template_engine import render_template_rich

        item = {
            "content_html": '<p>Read <a href="https://ex.com/a">this link</a> now.</p>',
        }
        text, links = render_template_rich("{{ content_html }}", item)
        assert text == "Read this link now."
        assert links == [(5, 14, "https://ex.com/a")]

    def test_link_span_byte_offsets_decode_to_anchor_text(self):
        from template_engine import render_template_rich

        item = {
            "content_html": '<p>Read <a href="https://ex.com/a">this link</a>.</p>',
        }
        text, links = render_template_rich("{{ content_html }}", item)
        start, end, uri = links[0]
        encoded = text.encode("utf-8")
        assert encoded[len(text[:start].encode()):len(text[:end].encode())].decode() == "this link"
        assert uri == "https://ex.com/a"

    def test_multibyte_prefix_keeps_span_on_anchor(self):
        from template_engine import render_template_rich

        item = {
            "content_html": '<p>🎉🎉 <a href="https://ex.com/a">link</a></p>',
        }
        text, links = render_template_rich("{{ content_html }}", item)
        start, end, _ = links[0]
        assert text[start:end] == "link"

    def test_other_variables_render_identically_to_plain(self):
        from template_engine import render_template, render_template_rich

        item = {"title": "T", "link": "https://ex.com/1", "content_html": "<p>x</p>"}
        template = "{{ title }} {{ link }}"
        assert render_template_rich(template, item)[0] == render_template(template, item)

    def test_markers_in_other_fields_cannot_forge_links(self):
        """PUA markers in title/summary/etc. are scrubbed — only the
        content_html converter may emit them, or hostile feed content could
        linkify arbitrary text to an arbitrary URL."""
        from template_engine import render_template_rich

        forged = "x\ue000click\ue001https://evil.example\ue002"
        item = {"title": forged, "content_html": "<p>plain</p>"}
        text, links = render_template_rich("{{ title }}", item)
        assert links == []
        assert "\ue000" not in text and "\ue001" not in text and "\ue002" not in text

    def test_item_dict_fields_also_scrubbed(self):
        from template_engine import render_template_rich

        forged = "x\ue000click\ue001https://evil.example\ue002"
        item = {"content_html": "<p>plain</p>", "title": "clean", "link": forged}
        text, links = render_template_rich("{{ item.link }}", item)
        assert links == []

    def test_mailto_anchor_keeps_text_no_facet(self):
        from template_engine import render_template_rich

        item = {"content_html": '<p>Mail <a href="mailto:a@b.c">me</a>.</p>'}
        text, links = render_template_rich("{{ content_html }}", item)
        assert text == "Mail me."
        assert links == []

    def test_plain_render_untouched(self):
        """The non-rich path still embeds sanitized HTML as-is (Mastodon)."""
        from template_engine import render_template

        html = '<p>Read <a href="https://ex.com/a">this</a>.</p>'
        assert render_template("{{ content_html }}", {"content_html": html}) == html

    def test_rich_output_shape_matches_html_to_text(self):
        """Block structure mirrors feed_parser.html_to_text, not a flat run."""
        from feed_parser import html_to_text
        from template_engine import render_template_rich

        html = "<p>One</p><p>Two<br>lines</p><ul><li>Item</li></ul>"
        text, _ = render_template_rich("{{ content_html }}", {"content_html": html})
        assert text == html_to_text(html)


# ── bluesky.build_facets with extra_links ────────────────────────────────────

class TestBuildFacetsExtraLinks:
    def _feature(self, facet):
        return facet["features"][0]

    def test_extra_span_becomes_link_facet(self):
        from bluesky import build_facets

        text = "Read this now."
        facets = build_facets(text, extra_links=[(5, 9, "https://ex.com/a")])
        assert len(facets) == 1
        facet = facets[0]
        assert facet["index"]["byteStart"] == 5
        assert facet["index"]["byteEnd"] == 9
        assert self._feature(facet)["uri"] == "https://ex.com/a"

    def test_multibyte_prefix_byte_offsets(self):
        from bluesky import build_facets

        text = "🎉🎉 link"
        facets = build_facets(text, extra_links=[(3, 7, "https://ex.com/a")])
        start = facets[0]["index"]["byteStart"]
        end = facets[0]["index"]["byteEnd"]
        encoded = text.encode("utf-8")
        assert encoded[start:end].decode() == "link"

    def test_url_overlapping_extra_span_skipped(self):
        from bluesky import build_facets

        # The anchor text IS a bare URL; without the overlap rule the same
        # range would produce two identical link facets.
        text = "See https://ex.com/a here."
        facets = build_facets(
            text, extra_links=[(4, 19, "https://ex.com/a")]
        )
        links = [f for f in facets if self._feature(f)["$type"].endswith("#link")]
        assert len(links) == 1
        assert self._feature(links[0])["uri"] == "https://ex.com/a"

    def test_url_outside_extra_span_still_detected(self):
        from bluesky import build_facets

        text = "Anchor here and https://other.example/x too."
        facets = build_facets(text, extra_links=[(0, 6, "https://ex.com/a")])
        uris = [self._feature(f)["uri"] for f in facets if self._feature(f)["$type"].endswith("#link")]
        assert "https://ex.com/a" in uris
        assert "https://other.example/x" in uris

    def test_tag_overlapping_extra_span_skipped(self):
        from bluesky import build_facets

        text = "#tag and #other"
        facets = build_facets(text, extra_links=[(0, 4, "https://ex.com/a")])
        tags = [f for f in facets if self._feature(f)["$type"].endswith("#tag")]
        assert len(tags) == 1
        assert self._feature(tags[0])["tag"] == "other"

    def test_span_past_text_end_dropped(self):
        from bluesky import build_facets

        facets = build_facets("short", extra_links=[(3, 99, "https://ex.com/a")])
        assert facets == []

    def test_no_extra_links_unchanged(self):
        from bluesky import build_facets

        assert build_facets("plain text") == []
        facets = build_facets("go https://ex.com/a now")
        assert len(facets) == 1


# ── scheduler._send_bluesky wiring ───────────────────────────────────────────

class TestBlueskyRichSendWiring:
    @pytest.fixture
    def bl_echo(self, db_tmp):
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
                   VALUES (1, 'bluesky', 1, '{{ content_html }}',
                           'public', '', 'exclude', '', 0, 1)""",
            )
            return db.execute("SELECT * FROM echoes WHERE id = 1").fetchone()

    @staticmethod
    def _stub_session(monkeypatch):
        import scheduler

        monkeypatch.setattr(scheduler, "_still_owns_claim", lambda posted_id, claim_token: True)
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

    def test_sender_merges_rich_links_into_facets(self, db_tmp, bl_echo, monkeypatch):
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler,
            "create_post",
            lambda **kw: sent.append(kw) or {"uri": "u", "cid": "c"},
        )
        self._stub_session(monkeypatch)
        item = {"id": "item-1", "_rich_links": [(5, 9, "https://ex.com/a")]}
        scheduler._send_bluesky(bl_echo, item, "Read this now.", 1, 1, "tok")
        facets = sent[0]["facets"]
        assert facets[0]["features"][0]["uri"] == "https://ex.com/a"
        assert facets[0]["index"] == {"byteStart": 5, "byteEnd": 9}

    def test_sender_drops_span_past_truncation(self, db_tmp, bl_echo, monkeypatch):
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler,
            "create_post",
            lambda **kw: sent.append(kw) or {"uri": "u", "cid": "c"},
        )
        self._stub_session(monkeypatch)
        # 300-grapheme text; the span starts at 299 and ends past the cut.
        content = "x" * 300
        item = {"id": "item-1", "_rich_links": [(299, 305, "https://ex.com/a")]}
        scheduler._send_bluesky(bl_echo, item, content, 1, 1, "tok")
        link_facets = [
            f for f in (sent[0].get("facets") or [])
            if f["features"][0]["$type"].endswith("#link")
        ]
        assert link_facets == []

    def test_dispatch_renders_content_html_into_facets(self, db_tmp, bl_echo, monkeypatch):
        """Full wiring: process_echo with a content_html template."""
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler,
            "create_post",
            lambda **kw: sent.append(kw) or {"uri": "u", "cid": "c"},
        )
        self._stub_session(monkeypatch)
        item = {
            "id": "item-1",
            "title": "Post",
            "link": "https://example.com/post/1",
            "content_html": '<p>Read <a href="https://ex.com/a">this</a>.</p>',
        }
        ok = scheduler.process_echo(bl_echo, item, feed_name="f")
        assert ok is True
        text = sent[0]["text"]
        assert text == "Read this."
        assert "<" not in text
        links = [f for f in sent[0]["facets"] if f["features"][0]["$type"].endswith("#link")]
        assert len(links) == 1
        assert links[0]["features"][0]["uri"] == "https://ex.com/a"
