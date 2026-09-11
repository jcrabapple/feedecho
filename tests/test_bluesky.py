"""Tests for Bluesky integration: module helpers, dispatch, and API routes."""

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

# ── Handle normalization ─────────────────────────────────────────────────────

class TestNormalizeHandle:
    def test_lowercases_and_strips_at(self):
        from bluesky import normalize_handle

        assert normalize_handle("@User.bsky.Social") == "user.bsky.social"

    def test_strips_profile_url(self):
        from bluesky import normalize_handle

        assert normalize_handle("https://bsky.app/profile/name.bsky.social") == "name.bsky.social"

    def test_strips_whitespace(self):
        from bluesky import normalize_handle

        assert normalize_handle("  name.example.com  ") == "name.example.com"

    def test_rejects_garbage(self):
        from bluesky import normalize_handle

        with pytest.raises(ValueError):
            normalize_handle("")
        with pytest.raises(ValueError):
            normalize_handle("no-dot-here")
        with pytest.raises(ValueError):
            normalize_handle("has spaces.example.com")

    def test_rejects_invalid_charset(self):
        from bluesky import normalize_handle

        with pytest.raises(ValueError):
            normalize_handle("bad!chars.example.com")
        with pytest.raises(ValueError):
            normalize_handle("under_score.example.com")
        with pytest.raises(ValueError):
            normalize_handle("-leading.example.com")
        with pytest.raises(ValueError):
            normalize_handle("trailing-.example.com")

    def test_rejects_overlong_handle(self):
        from bluesky import normalize_handle

        with pytest.raises(ValueError):
            normalize_handle("a" * 60 + "." + "b" * 250)

# ── Grapheme-aware truncation ────────────────────────────────────────────────

class TestTruncateGraphemes:
    def test_short_text_unchanged(self):
        from bluesky import truncate_graphemes

        assert truncate_graphemes("short", 300) == "short"

    def test_long_ascii_truncated_with_ellipsis(self):
        from bluesky import truncate_graphemes

        result = truncate_graphemes("a" * 500, 300)
        assert len(result) == 300
        assert result == "a" * 299 + "…"

    def test_emoji_counted_as_single_grapheme(self):
        from bluesky import truncate_graphemes

        result = truncate_graphemes("👍" * 400, 300)
        assert len(result) == 300
        assert result == "👍" * 299 + "…"

    def test_combining_marks_stay_with_base(self):
        from bluesky import truncate_graphemes
        from bluesky import _grapheme_clusters

        # 'e' + combining acute accent is one grapheme
        assert _grapheme_clusters("e\u0301") == ["e\u0301"]
        # 2 graphemes fit in max_graphemes=2 without truncation
        assert truncate_graphemes("e\u0301x", 2) == "e\u0301x"

    def test_zwj_emoji_family_single_grapheme(self):
        from bluesky import _grapheme_clusters

        family = "\U0001F468\u200D\U0001F469\u200D\U0001F467"
        clusters = _grapheme_clusters(family + "!")
        assert clusters == [family, "!"]

    def test_flag_regional_indicators_merge(self):
        from bluesky import _grapheme_clusters

        flag = "\U0001F1E9\U0001F1EA"  # 🇩🇪
        clusters = _grapheme_clusters(flag + "x")
        assert clusters == [flag, "x"]

    def test_flag_not_split_at_truncation_boundary(self):
        from bluesky import truncate_graphemes

        # 301 flags would need 300 graphemes + ellipsis; the pair at the cut
        # must stay together, so the output never contains a lone RI.
        flag = "\U0001F1E9\U0001F1EA"
        result = truncate_graphemes(flag * 200, 10)
        # 9 graphemes + "…" = 10; every flag is a complete pair
        assert result.endswith("…")
        body = result[:-1]
        assert len(body) % 2 == 0
        assert all(
            c in "\U0001F1E9\U0001F1EA" for c in body
        )

    def test_truncate_zwj_emoji_sequence_never_split(self):
        from bluesky import truncate_graphemes

        # "woman technologist": WOMAN + ZWJ + LAPTOP is one atomic cluster
        # because LAPTOP is Extended_Pictographic (GB11 applies).
        # clusters: [a, b, <zwj-emoji>, e, f]
        emoji = "\U0001F469\u200D\U0001F4BB"
        text = "ab" + emoji + "ef"
        assert truncate_graphemes(text, 4) == "ab" + emoji + "…"
        # Cutting before the ZWJ cluster drops it whole, never splits it.
        assert truncate_graphemes(text, 3) == "ab…"

    def test_zwj_non_emoji_not_glued_forward(self):
        """Finding #10 (bug review): GB11 must not fire for a non-emoji
        character following a ZWJ. A ZWJ used outside an emoji sequence
        (e.g. some Indic-script ligatures) must not swallow whatever
        ordinary character follows it into one cluster -- doing so
        under-counts true grapheme clusters, which can let a post through
        the 300-grapheme cap that actually has more true clusters than
        that.
        """
        from bluesky import _grapheme_clusters, truncate_graphemes

        # "c" + ZWJ glues onto "c" (GB9 -- ZWJ always attaches backward),
        # but "d" is plain ASCII, not Extended_Pictographic, so it must
        # start a new cluster instead of being glued in by the old
        # unconditional GB11 stand-in.
        text = "c\u200Dd"
        clusters = _grapheme_clusters(text)
        assert len(clusters) == 2
        assert clusters == ["c\u200D", "d"]

        # With the old (buggy) unconditional glue this whole string counted
        # as ONE grapheme and truncate_graphemes(text, 1) would return it
        # unchanged; now it's two, so a max_graphemes=1 cap must truncate.
        assert len(truncate_graphemes(text, 1)) < len(text)

    def test_byte_cap_enforced_with_few_graphemes(self):
        """Finding #10 (bug review): app.bsky.feed.post's `text` caps at
        BOTH maxGraphemes: 300 AND maxLength: 3000 (UTF-8 bytes) -- two
        independent limits. A string can be well under the grapheme cap
        and still exceed the byte cap (e.g. many multi-codepoint ZWJ emoji
        sequences, each one grapheme cluster but tens of bytes).
        """
        from bluesky import MAX_POST_BYTES, truncate_graphemes

        # "woman technologist": WOMAN + ZWJ + LAPTOP -- 3 codepoints, ~14
        # bytes, one grapheme cluster.
        emoji = "\U0001F469‍\U0001F4BB"
        text = emoji * 290  # 290 grapheme clusters (< 300), well over 3000 bytes
        assert len(text.encode("utf-8")) > MAX_POST_BYTES

        result = truncate_graphemes(text, 300, max_bytes=MAX_POST_BYTES)
        assert len(result.encode("utf-8")) <= MAX_POST_BYTES
        # Byte-safe: the result must still be valid UTF-8 (no split
        # multi-byte sequence) and end on a whole grapheme cluster / the
        # ellipsis, never a torn emoji.
        assert result.encode("utf-8").decode("utf-8") == result
        assert result.endswith("…")

    def test_byte_cap_truncates_a_single_oversized_cluster(self):
        """A single grapheme cluster that alone exceeds the byte cap (e.g.
        a base character with an excessive combining-mark tail) must still
        be trimmed rather than shipped whole and over budget -- dropping
        the whole cluster and falling back to just the ellipsis is the
        safest byte-safe behavior available at cluster granularity.
        """
        from bluesky import MAX_POST_BYTES, truncate_graphemes

        text = "A" + "́" * 2000  # one grapheme cluster, ~4000 bytes
        result = truncate_graphemes(text, 300, max_bytes=MAX_POST_BYTES)
        assert len(result.encode("utf-8")) <= MAX_POST_BYTES

    def test_byte_cap_not_enforced_when_omitted(self):
        """max_bytes defaults to None (backward compatible) -- callers that
        don't pass it, like the alt-text truncation, are unaffected."""
        from bluesky import truncate_graphemes

        text = "A" + "́" * 2000
        result = truncate_graphemes(text, 300)
        assert result == text

# ── Facets ───────────────────────────────────────────────────────────────────

class TestBuildFacets:
    def test_no_urls_returns_empty(self):
        from bluesky import build_facets

        assert build_facets("plain text") == []

    def test_single_url_byte_offsets(self):
        from bluesky import build_facets

        text = "read https://example.com/a now"
        facets = build_facets(text)
        assert len(facets) == 1
        facet = facets[0]
        start, end = facet["index"]["byteStart"], facet["index"]["byteEnd"]
        assert text.encode("utf-8")[start:end].decode() == "https://example.com/a"
        assert facet["features"][0]["$type"] == "app.bsky.richtext.facet#link"
        assert facet["features"][0]["uri"] == "https://example.com/a"

    def test_multibyte_prefix_offsets(self):
        from bluesky import build_facets

        prefix = "héllo 👋 "
        uri = "https://example.com/1"
        facets = build_facets(prefix + uri)
        start, end = facets[0]["index"]["byteStart"], facets[0]["index"]["byteEnd"]
        assert start == len(prefix.encode("utf-8"))
        assert end == len((prefix + uri).encode("utf-8"))

    def test_trailing_punctuation_trimmed(self):
        from bluesky import build_facets

        facets = build_facets("see https://example.com/x.")
        assert facets[0]["features"][0]["uri"] == "https://example.com/x"

    def test_multiple_urls(self):
        from bluesky import build_facets

        facets = build_facets("a https://a.example.com b https://b.example.com")
        assert len(facets) == 2
        assert [f["features"][0]["uri"] for f in facets] == [
            "https://a.example.com",
            "https://b.example.com",
        ]

    def test_clipped_url_facet_dropped(self):
        """A URL sliced by truncation must not become a broken link facet."""
        from bluesky import build_facets

        text = "see https://example.com/some/very/long/path/that/gets/cut"
        clipped = text[:20] + "…"
        facets = build_facets(clipped)
        assert facets == []

    def test_untouched_url_kept_after_clip_of_later_url(self):
        from bluesky import build_facets

        text = "first https://a.example.com/x then https://b.example.com/long"
        clipped = text[:24] + "…"  # cuts through the first URL
        facets = build_facets(clipped)
        assert facets == []

    # ── hashtag facets ─────────────────────────────────────────────────────

    def test_tag_facet_basic(self):
        from bluesky import build_facets

        text = "Hello #world"
        facets = build_facets(text)
        assert len(facets) == 1
        assert facets[0]["features"][0] == {
            "$type": "app.bsky.richtext.facet#tag",
            "tag": "world",
        }
        start, end = facets[0]["index"]["byteStart"], facets[0]["index"]["byteEnd"]
        assert text.encode("utf-8")[start:end].decode() == "#world"

    def test_tag_real_post_shape(self):
        """The exact shape of the post that prompted the fix (2026-09-11):
        title, blank line, URL, blank line, hashtag."""
        from bluesky import build_facets

        text = (
            "Arriving in Kristiansand, Norway.\n\n"
            "https://glass.photo/digitalpardoe/36Khk4eOLYsafBXiOclVBC\n\n"
            "#Photography"
        )
        facets = build_facets(text)
        assert len(facets) == 2
        link, tag = facets
        assert link["features"][0]["$type"] == "app.bsky.richtext.facet#link"
        assert tag["features"][0] == {
            "$type": "app.bsky.richtext.facet#tag",
            "tag": "Photography",
        }
        start, end = tag["index"]["byteStart"], tag["index"]["byteEnd"]
        assert text.encode("utf-8")[start:end].decode() == "#Photography"

    def test_tag_multibyte_prefix_offsets(self):
        from bluesky import build_facets

        prefix = "café ☕ "
        text = prefix + "#photo"
        facets = build_facets(text)
        start, end = facets[0]["index"]["byteStart"], facets[0]["index"]["byteEnd"]
        assert start == len(prefix.encode("utf-8"))
        assert end == len(text.encode("utf-8"))

    def test_tag_trailing_punctuation_trimmed(self):
        from bluesky import build_facets

        text = "Nice shot #photo!"
        facets = build_facets(text)
        assert facets[0]["features"][0]["tag"] == "photo"
        start, end = facets[0]["index"]["byteStart"], facets[0]["index"]["byteEnd"]
        assert text.encode("utf-8")[start:end].decode() == "#photo"

    def test_tag_at_start_of_text(self):
        from bluesky import build_facets

        facets = build_facets("#first post")
        assert facets[0]["features"][0]["tag"] == "first"
        assert facets[0]["index"]["byteStart"] == 0
        assert facets[0]["index"]["byteEnd"] == len("#first".encode("utf-8"))

    def test_tag_digit_only_skipped(self):
        from bluesky import build_facets

        assert build_facets("count #123 things") == []

    def test_tag_all_punctuation_skipped(self):
        from bluesky import build_facets

        assert build_facets("wow #!!!") == []

    def test_tag_fullwidth_hash(self):
        from bluesky import build_facets

        text = "＃photo"
        facets = build_facets(text)
        assert facets[0]["features"][0]["tag"] == "photo"
        start, end = facets[0]["index"]["byteStart"], facets[0]["index"]["byteEnd"]
        assert start == 0
        assert text.encode("utf-8")[start:end].decode() == "＃photo"

    def test_multiple_tags_sorted(self):
        from bluesky import build_facets

        facets = build_facets("#one two #three")
        assert [f["features"][0]["tag"] for f in facets] == ["one", "three"]
        starts = [f["index"]["byteStart"] for f in facets]
        assert starts == sorted(starts)

    def test_tag_inside_url_not_duplicated(self):
        from bluesky import build_facets

        facets = build_facets("see https://example.com/page#anchor")
        assert len(facets) == 1
        assert facets[0]["features"][0]["$type"] == "app.bsky.richtext.facet#link"

    def test_url_and_tag_both_detected(self):
        from bluesky import build_facets

        facets = build_facets("read https://example.com/a then #tag")
        assert [f["features"][0]["$type"] for f in facets] == [
            "app.bsky.richtext.facet#link",
            "app.bsky.richtext.facet#tag",
        ]

    def test_clipped_tag_dropped(self):
        """A tag sliced by truncation must not become a broken tag facet."""
        from bluesky import build_facets

        assert build_facets("Ending #Photogra…") == []

    def test_tag_after_paren_not_matched(self):
        """Client parity: a tag must start the text or follow whitespace."""
        from bluesky import build_facets

        assert build_facets("(#paren)") == []

    def test_tag_64_char_cap(self):
        from bluesky import build_facets

        assert build_facets("#" + "a" * 65) == []
        tag = "a" * 64
        facets = build_facets("#" + tag)
        assert facets[0]["features"][0]["tag"] == tag

    def test_tag_case_preserved(self):
        from bluesky import build_facets

        facets = build_facets("#PhotoGraphy")
        assert facets[0]["features"][0]["tag"] == "PhotoGraphy"

    def test_tag_non_ascii_digits_are_tags(self):
        """JS \\d is ASCII-only, so the official client treats a tag of
        non-ASCII digits as a real tag (e.g. Arabic-Indic year tags)."""
        from bluesky import build_facets

        facets = build_facets("#٢٠٢٦")
        assert facets[0]["features"][0]["tag"] == "٢٠٢٦"

    def test_tag_combining_marks_use_grapheme_cap(self):
        """66 code points but 33 grapheme clusters: under the client cap
        (which needs BOTH counts over 64), so the tag survives."""
        from bluesky import build_facets

        tag_body = "a\u0301" * 33
        facets = build_facets("#" + tag_body)
        assert facets[0]["features"][0]["tag"] == tag_body

    def test_tag_over_640_bytes_dropped(self):
        """The tag lexicon caps the property at 640 UTF-8 bytes; an oversized
        tag facet would fail record validation and kill the whole post."""
        from bluesky import build_facets

        body = ("\U0001F600" + "\u0301" * 6) * 63  # 63 graphemes, 1008 bytes
        assert build_facets("#" + body) == []

class TestBuildImageEmbed:
    def test_wraps_multiple_entries_with_alts(self):
        from bluesky import build_image_embed

        blob = {"$type": "blob", "ref": {"$link": "bafy"}}
        embed = build_image_embed([
            {"blob": blob, "alt": " one "},
            {"blob": blob, "alt": "two"},
        ])
        assert embed["$type"] == "app.bsky.embed.images"
        assert [img["alt"] for img in embed["images"]] == ["one", "two"]
        assert all(img["image"] is blob for img in embed["images"])

    def test_caps_at_max_images(self):
        from bluesky import MAX_IMAGES, build_image_embed

        entries = [
            {"blob": {"$type": "blob"}, "alt": str(i)}
            for i in range(MAX_IMAGES + 2)
        ]
        embed = build_image_embed(entries)
        assert len(embed["images"]) == MAX_IMAGES

    def test_truncates_long_alt_text(self):
        from bluesky import MAX_ALT_GRAPHEMES, build_image_embed

        embed = build_image_embed([
            {"blob": {"$type": "blob"}, "alt": "x" * (MAX_ALT_GRAPHEMES + 50)},
        ])
        assert len(embed["images"][0]["alt"]) == MAX_ALT_GRAPHEMES

    def test_empty_entries_raise_value_error(self):
        from bluesky import build_image_embed

        with pytest.raises(ValueError):
            build_image_embed([])


# ── Session expiry ───────────────────────────────────────────────────────────

class TestSessionExpiry:
    def test_decodes_jwt_exp(self):
        import base64
        import json
        from datetime import datetime, timedelta, timezone

        from bluesky import session_expiry

        exp = datetime.now(timezone.utc) + timedelta(hours=1)
        payload = base64.urlsafe_b64encode(
            json.dumps({"exp": int(exp.timestamp())}).encode()
        ).rstrip(b"=")
        jwt = f"h.{payload.decode()}.s"
        parsed = datetime.strptime(
            session_expiry(jwt), "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=timezone.utc)
        delta = (exp - timedelta(seconds=60) - parsed).total_seconds()
        assert abs(delta) < 2

    def test_undecodable_jwt_defaults_to_two_hours(self):
        from datetime import datetime, timedelta, timezone

        from bluesky import session_expiry

        parsed = datetime.strptime(session_expiry("not-a-jwt"), "%Y-%m-%d %H:%M:%S")
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        assert timedelta(hours=1, minutes=50) < (parsed - now) < timedelta(hours=2)

# ── Scheduler dispatch ───────────────────────────────────────────────────────

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

class TestSendBluesky:
    def test_happy_path_posts_and_records_success(self, db_tmp, monkeypatch):
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "create_post", lambda **kw: sent.append(kw) or {"uri": "u", "cid": "c"}
        )
        _stub_session(monkeypatch)

        echo = _setup_bluesky_echo(db_tmp)
        ok = scheduler.process_echo(echo, _item())

        assert ok is True
        assert len(sent) == 1
        assert sent[0]["repo"] == "did:plc:test123"
        assert sent[0]["text"] == "Test Post https://example.com/post/1"
        assert sent[0]["facets"]
        assert sent[0]["embed"] is None

        import database

        with database.get_db() as db:
            row = db.execute(
                "SELECT status, post_url FROM posted_items WHERE echo_id = 1"
            ).fetchone()
            assert row["status"] == "success"
            assert row["post_url"] == (
                "https://bsky.app/profile/did:plc:test123/post/u"
            )

    def test_hashtag_facet_flows_to_record(self, db_tmp, monkeypatch):
        """A literal #hashtag in a member's template reaches the post record
        as a tag facet (the 2026-09-11 member report)."""
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "create_post", lambda **kw: sent.append(kw) or {"uri": "u", "cid": "c"}
        )
        _stub_session(monkeypatch)

        echo = _setup_bluesky_echo(
            db_tmp, {"template": "{{ title }} {{ link }} #Photography"}
        )
        ok = scheduler.process_echo(echo, _item())

        assert ok is True
        facets = sent[0]["facets"]
        assert [f["features"][0]["$type"] for f in facets] == [
            "app.bsky.richtext.facet#link",
            "app.bsky.richtext.facet#tag",
        ]
        tag_facet = facets[1]
        assert tag_facet["features"][0]["tag"] == "Photography"
        text = sent[0]["text"]
        start, end = tag_facet["index"]["byteStart"], tag_facet["index"]["byteEnd"]
        assert text.encode("utf-8")[start:end].decode() == "#Photography"

    def test_content_truncated_to_300_graphemes(self, db_tmp, monkeypatch):
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "create_post", lambda **kw: sent.append(kw) or {"uri": "u", "cid": "c"}
        )
        _stub_session(monkeypatch)

        echo = _setup_bluesky_echo(db_tmp)
        item = _item(title="x" * 500, link="")
        scheduler.process_echo(echo, item)

        assert len(sent[0]["text"]) == 300
        assert sent[0]["text"].endswith("…")

    def test_missing_account_fails_permanently(self, db_tmp, monkeypatch):
        import database
        import scheduler

        echo = _setup_bluesky_echo(db_tmp)
        with database.get_db() as db:
            db.execute("DELETE FROM bluesky_accounts")

        ok = scheduler.process_echo(echo, _item())

        assert ok is True  # gave_up unblocks the cursor
        with database.get_db() as db:
            row = db.execute(
                "SELECT status, error_message FROM posted_items WHERE echo_id = 1"
            ).fetchone()
            assert row["status"] == "gave_up"
            assert "not found" in row["error_message"]

    def test_image_attached_when_enabled(self, db_tmp, monkeypatch):
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "create_post", lambda **kw: sent.append(kw) or {"uri": "u", "cid": "c"}
        )
        _stub_session(monkeypatch)
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
        scheduler.process_echo(echo, item)

        assert sent[0]["embed"]["$type"] == "app.bsky.embed.images"
        assert sent[0]["embed"]["images"][0]["image"]["ref"]["$link"] == "bafkreifake"
        assert sent[0]["embed"]["images"][0]["alt"] == ""

    def test_unsupported_image_type_posts_text_only(self, db_tmp, monkeypatch):
        import scheduler

        sent = []
        upload_calls = []
        monkeypatch.setattr(
            scheduler, "create_post", lambda **kw: sent.append(kw) or {"uri": "u", "cid": "c"}
        )
        _stub_session(monkeypatch)
        monkeypatch.setattr(
            scheduler, "fetch_image", lambda url: (b"bytes", "image/avif")
        )
        monkeypatch.setattr(
            scheduler,
            "upload_blob",
            lambda **kw: upload_calls.append(kw) or {"$type": "blob"},
        )

        echo = _setup_bluesky_echo(db_tmp, {"attach_image": 1})
        item = _item(image_url="https://example.com/photo.avif")
        scheduler.process_echo(echo, item)

        assert len(upload_calls) == 0
        assert sent[0]["embed"] is None

    def test_attaches_up_to_four_images_from_image_urls(self, db_tmp, monkeypatch):
        """image_urls drives a multi-image embed (Bluesky cap 4), per-image alt."""
        import scheduler

        sent = []
        uploaded = []
        monkeypatch.setattr(
            scheduler, "create_post", lambda **kw: sent.append(kw) or {"uri": "u", "cid": "c"}
        )
        _stub_session(monkeypatch)
        monkeypatch.setattr(
            scheduler, "fetch_image", lambda url: (b"fake-image-bytes", "image/jpeg")
        )
        monkeypatch.setattr(
            scheduler,
            "upload_blob",
            lambda **kw: uploaded.append(kw)
            or {"$type": "blob", "ref": {"$link": "bafkreifake"}},
        )
        import alt_text

        monkeypatch.setattr(alt_text, "is_enabled", lambda user_id=1: False)

        echo = _setup_bluesky_echo(db_tmp, {"attach_image": 1})
        item = _item(image_urls=[
            {"url": "https://example.com/1.jpg", "alt": "one"},
            {"url": "https://example.com/2.jpg", "alt": "two"},
            {"url": "https://example.com/3.jpg", "alt": ""},
        ])
        scheduler.process_echo(echo, item)

        assert len(uploaded) == 3
        embed = sent[0]["embed"]
        assert embed["$type"] == "app.bsky.embed.images"
        assert [img["alt"] for img in embed["images"]] == ["one", "two", ""]
        assert all(
            img["image"]["ref"]["$link"] == "bafkreifake" for img in embed["images"]
        )

    def test_caps_at_four_images(self, db_tmp, monkeypatch):
        """More than 4 image_urls are truncated to Bluesky's cap of 4."""
        import scheduler

        sent = []
        upload_count = [0]
        monkeypatch.setattr(
            scheduler, "create_post", lambda **kw: sent.append(kw) or {"uri": "u", "cid": "c"}
        )
        _stub_session(monkeypatch)
        monkeypatch.setattr(
            scheduler, "fetch_image", lambda url: (b"fake-image-bytes", "image/jpeg")
        )

        def fake_upload(**kw):
            upload_count[0] += 1
            return {"$type": "blob", "ref": {"$link": f"bafkrei{upload_count[0]}"}}

        monkeypatch.setattr(scheduler, "upload_blob", fake_upload)
        import alt_text

        monkeypatch.setattr(alt_text, "is_enabled", lambda user_id=1: False)

        echo = _setup_bluesky_echo(db_tmp, {"attach_image": 1})
        item = _item(image_urls=[
            {"url": f"https://example.com/{i}.jpg", "alt": ""} for i in range(6)
        ])
        scheduler.process_echo(echo, item)

        assert upload_count[0] == 4
        assert len(sent[0]["embed"]["images"]) == 4

    def test_failed_image_fetch_skips_only_that_image(self, db_tmp, monkeypatch):
        """One dead image URL never drops the rest of the attachments."""
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "create_post", lambda **kw: sent.append(kw) or {"uri": "u", "cid": "c"}
        )
        _stub_session(monkeypatch)

        def flaky_fetch(url):
            if "broken" in url:
                return None
            return (b"fake-image-bytes", "image/jpeg")

        monkeypatch.setattr(scheduler, "fetch_image", flaky_fetch)
        monkeypatch.setattr(
            scheduler,
            "upload_blob",
            lambda **kw: {"$type": "blob", "ref": {"$link": "bafkreifake"}},
        )
        import alt_text

        monkeypatch.setattr(alt_text, "is_enabled", lambda user_id=1: False)

        echo = _setup_bluesky_echo(db_tmp, {"attach_image": 1})
        item = _item(image_urls=[
            {"url": "https://example.com/broken.jpg", "alt": ""},
            {"url": "https://example.com/good.jpg", "alt": ""},
        ])
        scheduler.process_echo(echo, item)

        assert len(sent[0]["embed"]["images"]) == 1

    def test_unsupported_type_skips_only_that_image(self, db_tmp, monkeypatch):
        """A non-image content type skips that image, keeps the supported one."""
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "create_post", lambda **kw: sent.append(kw) or {"uri": "u", "cid": "c"}
        )
        _stub_session(monkeypatch)

        def typed_fetch(url):
            if url.endswith(".avif"):
                return (b"bytes", "image/avif")
            return (b"fake-image-bytes", "image/jpeg")

        monkeypatch.setattr(scheduler, "fetch_image", typed_fetch)
        monkeypatch.setattr(
            scheduler,
            "upload_blob",
            lambda **kw: {"$type": "blob", "ref": {"$link": "bafkreifake"}},
        )
        import alt_text

        monkeypatch.setattr(alt_text, "is_enabled", lambda user_id=1: False)

        echo = _setup_bluesky_echo(db_tmp, {"attach_image": 1})
        item = _item(image_urls=[
            {"url": "https://example.com/photo.avif", "alt": ""},
            {"url": "https://example.com/photo.jpg", "alt": ""},
        ])
        scheduler.process_echo(echo, item)

        assert len(sent[0]["embed"]["images"]) == 1

    def test_upload_rejection_skips_only_that_image(self, db_tmp, monkeypatch):
        """An upload that comes back empty skips that image, keeps the rest."""
        import scheduler

        sent = []
        uploads = []
        monkeypatch.setattr(
            scheduler, "create_post", lambda **kw: sent.append(kw) or {"uri": "u", "cid": "c"}
        )
        _stub_session(monkeypatch)
        monkeypatch.setattr(
            scheduler, "fetch_image", lambda url: (b"fake-image-bytes", "image/jpeg")
        )

        def flaky_upload(**kw):
            uploads.append(kw)
            if len(uploads) == 1:
                return None
            return {"$type": "blob", "ref": {"$link": "bafkreifake"}}

        monkeypatch.setattr(scheduler, "upload_blob", flaky_upload)
        import alt_text

        monkeypatch.setattr(alt_text, "is_enabled", lambda user_id=1: False)

        echo = _setup_bluesky_echo(db_tmp, {"attach_image": 1})
        item = _item(image_urls=[
            {"url": "https://example.com/1.jpg", "alt": ""},
            {"url": "https://example.com/2.jpg", "alt": ""},
        ])
        scheduler.process_echo(echo, item)

        assert len(uploads) == 2
        assert len(sent[0]["embed"]["images"]) == 1

    def test_user_alt_override_on_primary_slot(self, db_tmp, monkeypatch):
        """item['image_alt'] (user-edited) wins for the first image; the
        secondary slot falls back to AI generation."""
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "create_post", lambda **kw: sent.append(kw) or {"uri": "u", "cid": "c"}
        )
        _stub_session(monkeypatch)
        monkeypatch.setattr(
            scheduler, "fetch_image", lambda url: (b"fake-image-bytes", "image/jpeg")
        )
        monkeypatch.setattr(
            scheduler,
            "upload_blob",
            lambda **kw: {"$type": "blob", "ref": {"$link": "bafkreifake"}},
        )
        import alt_text

        monkeypatch.setattr(alt_text, "is_enabled", lambda user_id=1: True)
        monkeypatch.setattr(
            alt_text, "generate_alt_text", lambda *a, **kw: "AI-GENERATED"
        )

        echo = _setup_bluesky_echo(db_tmp, {"attach_image": 1})
        item = _item(
            image_alt="USER EDITED ALT",
            image_urls=[
                {"url": "https://example.com/1.jpg", "alt": ""},
                {"url": "https://example.com/2.jpg", "alt": ""},
            ],
        )
        scheduler.process_echo(echo, item)

        assert [
            img["alt"] for img in sent[0]["embed"]["images"]
        ] == ["USER EDITED ALT", "AI-GENERATED"]

    def test_auth_error_during_upload_reauthenticates_and_retries(self, db_tmp, monkeypatch):
        """A session rejected mid-upload heals in the same dispatch: re-login
        with the app password, retry the remaining images, publish them all —
        never a text-only fallback, never a dead-token retry loop."""
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
            scheduler, "fetch_image", lambda url: (b"fake-image-bytes", "image/jpeg")
        )

        def flaky_upload(**kw):
            uploads.append(kw)
            if len(uploads) == 2:
                # Second image: rejected once, then accepted after re-login.
                raise BlueskyAuthError(
                    "Blob upload rejected: session expired (ExpiredToken)"
                )
            return {"$type": "blob", "ref": {"$link": "bafkreifake"}}

        monkeypatch.setattr(scheduler, "upload_blob", flaky_upload)
        import alt_text

        monkeypatch.setattr(alt_text, "is_enabled", lambda user_id=1: False)

        echo = _setup_bluesky_echo(db_tmp, {"attach_image": 1})
        item = _item(image_urls=[
            {"url": "https://example.com/1.jpg", "alt": ""},
            {"url": "https://example.com/2.jpg", "alt": ""},
        ])
        ok = scheduler.process_echo(echo, item)

        assert ok is True
        assert len(sent) == 1
        assert len(sent[0]["embed"]["images"]) == 2  # both images made it
        assert len(uploads) == 3  # the rejected upload was retried once
        assert uploads[0]["access_jwt"] == "aj-1"  # pre-re-auth upload
        assert uploads[2]["access_jwt"] == "aj-2"  # retried with fresh token
        assert sent[0]["access_jwt"] == "aj-2"  # post uses the rebound session
        assert logins["count"] == 2  # initial login + one re-auth
        with database.get_db() as db:
            account = db.execute(
                "SELECT access_jwt FROM bluesky_accounts WHERE id = 1"
            ).fetchone()
            assert account["access_jwt"] == "aj-2"  # fresh session persisted

    def test_skipped_image_before_auth_error_does_not_duplicate(
        self, db_tmp, monkeypatch
    ):
        """A skipped image before the rejected upload must not shift the retry
        window: already-uploaded images are never re-uploaded (the retry
        resumes at the input index, not the success count)."""
        import scheduler
        from bluesky import BlueskyAuthError

        sent = []
        fetched = []
        uploads = []
        monkeypatch.setattr(
            scheduler, "create_post", lambda **kw: sent.append(kw) or {"uri": "u", "cid": "c"}
        )
        _stub_session(monkeypatch)

        def flaky_fetch(url):
            fetched.append(url)
            if "dead" in url:
                return None  # skip: never uploaded
            return (b"fake-image-bytes", "image/jpeg")

        monkeypatch.setattr(scheduler, "fetch_image", flaky_fetch)

        def flaky_upload(**kw):
            uploads.append(kw)
            if len(uploads) == 2:
                raise BlueskyAuthError(
                    "Blob upload rejected: session expired (ExpiredToken)"
                )
            return {"$type": "blob", "ref": {"$link": "bafkreifake"}}

        monkeypatch.setattr(scheduler, "upload_blob", flaky_upload)
        import alt_text

        monkeypatch.setattr(alt_text, "is_enabled", lambda user_id=1: False)

        echo = _setup_bluesky_echo(db_tmp, {"attach_image": 1})
        item = _item(image_urls=[
            {"url": "https://example.com/dead.jpg", "alt": ""},  # skipped
            {"url": "https://example.com/1.jpg", "alt": ""},  # uploaded once
            {"url": "https://example.com/2.jpg", "alt": ""},  # rejected, retried
        ])
        ok = scheduler.process_echo(echo, item)

        assert ok is True
        embed_images = sent[0]["embed"]["images"]
        assert len(embed_images) == 2  # dead skipped; no duplicates
        assert embed_images[0]["image"]["ref"]["$link"] == "bafkreifake"
        assert len(uploads) == 3  # 1.jpg once, 2.jpg rejected + retried
        assert fetched.count("https://example.com/1.jpg") == 1

    def test_auth_error_during_upload_with_dead_credentials_gives_up(
        self, db_tmp, monkeypatch
    ):
        """When the re-login itself is rejected, no retry can help: fail
        permanently, publish nothing."""
        import database
        import scheduler
        from bluesky import BlueskyAuthError

        sent = []
        logins = {"count": 0}
        monkeypatch.setattr(
            scheduler, "create_post", lambda **kw: sent.append(kw) or {"uri": "u", "cid": "c"}
        )
        _stub_session(monkeypatch)

        def dead_session(pds, handle, pw):
            logins["count"] += 1
            if logins["count"] == 1:
                return {"did": "did:plc:test123", "access_jwt": "aj", "refresh_jwt": "rj"}
            raise BlueskyAuthError("Authentication Required")

        monkeypatch.setattr(scheduler, "create_session", dead_session)
        monkeypatch.setattr(
            scheduler, "fetch_image", lambda url: (b"fake-image-bytes", "image/jpeg")
        )

        def rejected_upload(**kw):
            raise BlueskyAuthError(
                "Blob upload rejected: session expired (ExpiredToken)"
            )

        monkeypatch.setattr(scheduler, "upload_blob", rejected_upload)
        import alt_text

        monkeypatch.setattr(alt_text, "is_enabled", lambda user_id=1: False)

        echo = _setup_bluesky_echo(db_tmp, {"attach_image": 1})
        item = _item(image_urls=[
            {"url": "https://example.com/1.jpg", "alt": ""},
        ])
        ok = scheduler.process_echo(echo, item)

        assert ok is True  # terminal failure unblocks the cursor
        assert len(sent) == 0
        with database.get_db() as db:
            row = db.execute(
                "SELECT status, error_message FROM posted_items WHERE echo_id = 1"
            ).fetchone()
            assert row["status"] == "gave_up"
            assert "credentials" in row["error_message"]

    def test_auth_error_during_upload_with_account_deleted_gives_up_permanently(
        self, db_tmp, monkeypatch
    ):
        """The account row disappears mid-dispatch (deleted while an image
        upload triggered a re-auth): give up permanently, not a transient
        retry that can never succeed against a gone account."""
        import database
        import scheduler
        from bluesky import BlueskyAuthError

        sent = []
        monkeypatch.setattr(
            scheduler, "create_post", lambda **kw: sent.append(kw) or {"uri": "u", "cid": "c"}
        )
        _stub_session(monkeypatch)
        monkeypatch.setattr(
            scheduler, "fetch_image", lambda url: (b"fake-image-bytes", "image/jpeg")
        )

        def rejected_upload(**kw):
            with database.get_db() as db:
                db.execute("DELETE FROM bluesky_accounts WHERE id = 1")
            raise BlueskyAuthError(
                "Blob upload rejected: session expired (ExpiredToken)"
            )

        monkeypatch.setattr(scheduler, "upload_blob", rejected_upload)
        import alt_text

        monkeypatch.setattr(alt_text, "is_enabled", lambda user_id=1: False)

        echo = _setup_bluesky_echo(db_tmp, {"attach_image": 1})
        item = _item(image_urls=[{"url": "https://example.com/1.jpg", "alt": ""}])
        ok = scheduler.process_echo(echo, item)

        assert ok is True  # terminal failure unblocks the cursor
        assert len(sent) == 0
        with database.get_db() as db:
            row = db.execute(
                "SELECT status, error_message FROM posted_items WHERE echo_id = 1"
            ).fetchone()
            assert row["status"] == "gave_up"
            assert "deleted" in row["error_message"].lower()

    def test_http_error_on_upload_skips_only_that_image(self, db_tmp, monkeypatch):
        """Non-auth PDS upload rejections stay image-level: skip that image,
        keep the rest of the post (same contract as Mastodon's upload_media)."""
        import scheduler
        from bluesky import BlueskyError

        sent = []
        uploads = []
        monkeypatch.setattr(
            scheduler, "create_post", lambda **kw: sent.append(kw) or {"uri": "u", "cid": "c"}
        )
        _stub_session(monkeypatch)
        monkeypatch.setattr(
            scheduler, "fetch_image", lambda url: (b"fake-image-bytes", "image/jpeg")
        )

        def failing_upload(**kw):
            uploads.append(kw)
            if len(uploads) == 1:
                raise BlueskyError("Blob upload failed (HTTP 500)")
            return {"$type": "blob", "ref": {"$link": "bafkreifake"}}

        monkeypatch.setattr(scheduler, "upload_blob", failing_upload)
        import alt_text

        monkeypatch.setattr(alt_text, "is_enabled", lambda user_id=1: False)

        echo = _setup_bluesky_echo(db_tmp, {"attach_image": 1})
        item = _item(image_urls=[
            {"url": "https://example.com/1.jpg", "alt": ""},
            {"url": "https://example.com/2.jpg", "alt": ""},
        ])
        ok = scheduler.process_echo(echo, item)

        assert ok is True
        assert len(sent[0]["embed"]["images"]) == 1

    def test_all_images_failed_warns_text_only(self, db_tmp, monkeypatch, caplog):
        """When every candidate image is skipped, the degrade to text-only is
        logged (the old code had a per-branch warning; the loop needs one)."""
        import logging
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "create_post", lambda **kw: sent.append(kw) or {"uri": "u", "cid": "c"}
        )
        _stub_session(monkeypatch)
        monkeypatch.setattr(scheduler, "fetch_image", lambda url: None)
        import alt_text

        monkeypatch.setattr(alt_text, "is_enabled", lambda user_id=1: False)

        echo = _setup_bluesky_echo(db_tmp, {"attach_image": 1})
        item = _item(image_urls=[
            {"url": "https://example.com/1.jpg", "alt": ""},
            {"url": "https://example.com/2.jpg", "alt": ""},
        ])
        with caplog.at_level(logging.WARNING):
            ok = scheduler.process_echo(echo, item)

        assert ok is True
        assert sent[0]["embed"] is None
        assert "no images uploaded" in caplog.text

    def test_auth_error_retries_with_fresh_session(self, db_tmp, monkeypatch):
        import database
        import scheduler

        calls = {"count": 0}
        from bluesky import BlueskyAuthError

        def flaky_post(**kw):
            calls["count"] += 1
            if calls["count"] == 1:
                raise BlueskyAuthError("ExpiredToken")
            return {"uri": "u", "cid": "c"}

        monkeypatch.setattr(scheduler, "create_post", flaky_post)
        _stub_session(monkeypatch)

        echo = _setup_bluesky_echo(db_tmp)
        ok = scheduler.process_echo(echo, _item())

        assert ok is True
        assert calls["count"] == 2
        with database.get_db() as db:
            row = db.execute(
                "SELECT access_jwt FROM bluesky_accounts WHERE id = 1"
            ).fetchone()
            assert row["access_jwt"] == "aj"  # refreshed via stub create_session

    def test_post_url_uses_refreshed_did_after_reauth(self, db_tmp, monkeypatch):
        """post_url must name the DID that actually published when a
        mid-post re-login resolved a different one."""
        import database
        import scheduler
        from bluesky import BlueskyAuthError

        calls = {"count": 0}
        logins = {"count": 0}

        def flaky_post(**kw):
            calls["count"] += 1
            if calls["count"] == 1:
                raise BlueskyAuthError("ExpiredToken")
            return {"uri": "u", "cid": "c"}

        monkeypatch.setattr(scheduler, "create_post", flaky_post)
        _stub_session(monkeypatch)

        def rotating_session(pds, handle, pw):
            logins["count"] += 1
            did = (
                "did:plc:test123"
                if logins["count"] == 1
                else "did:plc:refreshed"
            )
            return {
                "did": did,
                "access_jwt": f"aj{logins['count']}",
                "refresh_jwt": f"rj{logins['count']}",
            }

        monkeypatch.setattr(scheduler, "create_session", rotating_session)

        echo = _setup_bluesky_echo(db_tmp)
        ok = scheduler.process_echo(echo, _item())

        assert ok is True
        with database.get_db() as db:
            row = db.execute(
                "SELECT post_url FROM posted_items WHERE echo_id = 1"
            ).fetchone()
        assert (
            row["post_url"]
            == "https://bsky.app/profile/did:plc:refreshed/post/u"
        )

    def test_post_url_uses_refreshed_did_after_upload_reauth(
        self, db_tmp, monkeypatch
    ):
        """DID rotation triggered by a mid-upload rejection must also reach
        post_url (the upload-loop re-auth rebinds the session for create_post)."""
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

        def rotating_session(pds, handle, pw):
            logins["count"] += 1
            did = (
                "did:plc:test123"
                if logins["count"] == 1
                else "did:plc:refreshed"
            )
            return {
                "did": did,
                "access_jwt": f"aj{logins['count']}",
                "refresh_jwt": f"rj{logins['count']}",
            }

        monkeypatch.setattr(scheduler, "create_session", rotating_session)
        monkeypatch.setattr(
            scheduler, "fetch_image", lambda url: (b"fake-image-bytes", "image/jpeg")
        )

        def flaky_upload(**kw):
            uploads.append(kw)
            if len(uploads) == 1:
                raise BlueskyAuthError(
                    "Blob upload rejected: session expired (ExpiredToken)"
                )
            return {"$type": "blob", "ref": {"$link": "bafkreifake"}}

        monkeypatch.setattr(scheduler, "upload_blob", flaky_upload)
        import alt_text

        monkeypatch.setattr(alt_text, "is_enabled", lambda user_id=1: False)

        echo = _setup_bluesky_echo(db_tmp, {"attach_image": 1})
        item = _item(image_urls=[
            {"url": "https://example.com/1.jpg", "alt": ""},
        ])
        ok = scheduler.process_echo(echo, item)

        assert ok is True
        with database.get_db() as db:
            row = db.execute(
                "SELECT post_url FROM posted_items WHERE echo_id = 1"
            ).fetchone()
        assert (
            row["post_url"]
            == "https://bsky.app/profile/did:plc:refreshed/post/u"
        )

    def test_persistent_auth_failure_gives_up_permanently(self, db_tmp, monkeypatch):
        import database
        import scheduler

        from bluesky import BlueskyAuthError

        def always_fail(**kw):
            raise BlueskyAuthError("InvalidToken")

        monkeypatch.setattr(scheduler, "create_post", always_fail)
        _stub_session(monkeypatch)

        echo = _setup_bluesky_echo(db_tmp)
        ok = scheduler.process_echo(echo, _item())

        assert ok is True  # terminal failure unblocks the cursor
        with database.get_db() as db:
            row = db.execute(
                "SELECT status, error_message FROM posted_items WHERE echo_id = 1"
            ).fetchone()
            assert row["status"] == "gave_up"
            assert "credentials" in row["error_message"]

    def test_account_deleted_during_post_reauth_gives_up_permanently(
        self, db_tmp, monkeypatch
    ):
        """The account row disappears mid-dispatch (deleted while the
        create_post retry re-authenticated): give up permanently instead of
        scheduling a retry against an account that no longer exists."""
        import database
        import scheduler
        from bluesky import BlueskyAuthError

        def rejected_post(**kw):
            with database.get_db() as db:
                db.execute("DELETE FROM bluesky_accounts WHERE id = 1")
            raise BlueskyAuthError("InvalidToken")

        monkeypatch.setattr(scheduler, "create_post", rejected_post)
        _stub_session(monkeypatch)

        echo = _setup_bluesky_echo(db_tmp)
        ok = scheduler.process_echo(echo, _item())

        assert ok is True  # terminal failure unblocks the cursor
        with database.get_db() as db:
            row = db.execute(
                "SELECT status, error_message FROM posted_items WHERE echo_id = 1"
            ).fetchone()
            assert row["status"] == "gave_up"
            assert "deleted" in row["error_message"].lower()

    def test_claim_lost_before_dispatch_skips_post(self, db_tmp, monkeypatch):
        import database
        import scheduler

        posted = []
        monkeypatch.setattr(
            scheduler, "create_post", lambda **kw: posted.append(kw) or {"uri": "u", "cid": "c"}
        )
        _stub_session(monkeypatch)

        real_claim = scheduler._claim_post

        def steal_claim(echo_id, it):
            """Claim normally, then overwrite the token so ownership is lost."""
            result = real_claim(echo_id, it)
            if result:
                posted_id, _token = result
                with database.get_db() as db:
                    db.execute(
                        "UPDATE posted_items SET claim_token = 'stolen' WHERE id = ?",
                        (posted_id,),
                    )
            return result

        monkeypatch.setattr(scheduler, "_claim_post", steal_claim)

        echo = _setup_bluesky_echo(db_tmp)
        ok = scheduler.process_echo(echo, _item())

        # The pre-dispatch ownership check must abort before posting.
        assert len(posted) == 0
        assert ok is False
        with database.get_db() as db:
            row = db.execute(
                "SELECT status FROM posted_items WHERE echo_id = 1"
            ).fetchone()
            assert row["status"] == "pending"  # still owned by the thief

    def test_content_prep_failure_finalizes_row(self, db_tmp, monkeypatch):
        import database
        import scheduler

        def boom_facets(text):
            raise RuntimeError("facet bug")

        monkeypatch.setattr(scheduler, "build_facets", boom_facets)
        _stub_session(monkeypatch)

        echo = _setup_bluesky_echo(db_tmp)
        ok = scheduler.process_echo(echo, _item())

        assert ok is False
        with database.get_db() as db:
            row = db.execute(
                "SELECT status, error_message FROM posted_items WHERE echo_id = 1"
            ).fetchone()
            assert row["status"] == "failed"
            assert "preparation" in row["error_message"]

    def test_image_pipeline_exception_posts_text_only(self, db_tmp, monkeypatch):
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "create_post", lambda **kw: sent.append(kw) or {"uri": "u", "cid": "c"}
        )
        _stub_session(monkeypatch)

        def boom_fetch(url):
            raise RuntimeError("image bug")

        monkeypatch.setattr(scheduler, "fetch_image", boom_fetch)

        echo = _setup_bluesky_echo(db_tmp, {"attach_image": 1})
        item = _item(image_url="https://example.com/photo.jpg")
        ok = scheduler.process_echo(echo, item)

        assert ok is True
        assert sent[0]["embed"] is None

# ── Session caching ──────────────────────────────────────────────────────────

def _insert_bsky_account(db, **overrides):
    """Insert a Bluesky account row and return it."""
    values = {
        "name": "main",
        "handle": "user.bsky.social",
        "app_password": "abcd-efgh-ijkl-mnop",
        "did": "did:plc:test123",
        "pds": "https://bsky.social",
        "access_jwt": "",
        "refresh_jwt": "",
        "session_expires_at": None,
    }
    values.update(overrides)
    db.execute(
        """INSERT INTO bluesky_accounts
             (name, handle, app_password, did, pds, access_jwt, refresh_jwt, session_expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            values["name"],
            values["handle"],
            values["app_password"],
            values["did"],
            values["pds"],
            values["access_jwt"],
            values["refresh_jwt"],
            values["session_expires_at"],
        ),
    )
    return db.execute(
        "SELECT * FROM bluesky_accounts WHERE handle = ?", (values["handle"],)
    ).fetchone()

class TestBskySession:
    def test_reuses_cached_valid_session(self, db_tmp, monkeypatch):
        import database
        import scheduler

        monkeypatch.setattr(
            scheduler, "refresh_session", lambda *a, **kw: pytest.fail("should not refresh")
        )
        monkeypatch.setattr(
            scheduler, "create_session", lambda *a, **kw: pytest.fail("should not create")
        )

        with database.get_db() as db:
            account = _insert_bsky_account(
                db, access_jwt="cached-aj", session_expires_at="2099-01-01 00:00:00"
            )
            session = scheduler._bsky_session(account)

        assert session["access_jwt"] == "cached-aj"
        assert session["did"] == "did:plc:test123"

    def test_resolves_and_persists_pds_when_missing_with_cached_token(
        self, db_tmp, monkeypatch
    ):
        import database
        import scheduler

        monkeypatch.setattr(
            scheduler, "refresh_session", lambda *a, **kw: pytest.fail("should not refresh")
        )
        monkeypatch.setattr(
            scheduler, "create_session", lambda *a, **kw: pytest.fail("should not create")
        )
        monkeypatch.setattr(
            scheduler,
            "resolve_pds",
            lambda handle: ("did:plc:resolved", "https://resolved-pds.example"),
        )

        with database.get_db() as db:
            account = _insert_bsky_account(
                db, did="", pds="", access_jwt="cached-aj",
                session_expires_at="2099-01-01 00:00:00",
            )
        # The account-fetch transaction must be closed before _bsky_session
        # opens its own connection to persist the resolution.
        session = scheduler._bsky_session(account)

        assert session["pds"] == "https://resolved-pds.example"
        assert session["did"] == "did:plc:resolved"  # resolved DID, not stored one
        with database.get_db() as db:
            row = db.execute(
                "SELECT did, pds FROM bluesky_accounts WHERE id = 1"
            ).fetchone()
            assert row["did"] == "did:plc:resolved"
            assert row["pds"] == "https://resolved-pds.example"

    def test_refreshes_expired_session(self, db_tmp, monkeypatch):
        import database
        import scheduler

        monkeypatch.setattr(
            scheduler,
            "refresh_session",
            lambda pds, rj: {"did": "did:plc:test123", "access_jwt": "new-aj", "refresh_jwt": "new-rj"},
        )
        monkeypatch.setattr(
            scheduler, "create_session", lambda *a, **kw: pytest.fail("should not create")
        )

        with database.get_db() as db:
            account = _insert_bsky_account(
                db, refresh_jwt="old-rj", session_expires_at="2000-01-01 00:00:00"
            )
        session = scheduler._bsky_session(account)

        assert session["access_jwt"] == "new-aj"
        with database.get_db() as db:
            row = db.execute(
                "SELECT access_jwt, refresh_jwt FROM bluesky_accounts WHERE id = 1"
            ).fetchone()
            assert row["access_jwt"] == "new-aj"
            assert row["refresh_jwt"] == "new-rj"

    def test_falls_back_to_login_when_refresh_fails(self, db_tmp, monkeypatch):
        import database
        import scheduler

        from bluesky import BlueskyAuthError

        def fail_refresh(pds, rj):
            raise BlueskyAuthError("ExpiredToken")

        monkeypatch.setattr(scheduler, "refresh_session", fail_refresh)
        monkeypatch.setattr(
            scheduler,
            "create_session",
            lambda pds, handle, pw: {
                "did": "did:plc:test123",
                "access_jwt": "login-aj",
                "refresh_jwt": "login-rj",
            },
        )

        with database.get_db() as db:
            account = _insert_bsky_account(
                db, refresh_jwt="old-rj", session_expires_at="2000-01-01 00:00:00"
            )
        session = scheduler._bsky_session(account)

        assert session["access_jwt"] == "login-aj"

# ── API error classification ─────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}

    def json(self):
        return self._body

class _FakeClient:
    def __init__(self, response, **kw):
        self.response = response

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, *a, **kw):
        return self.response

    def get(self, *a, **kw):
        return self.response

class TestBlueskyApiErrors:
    def test_create_post_plain_400_is_not_auth_error(self, monkeypatch):
        import bluesky

        monkeypatch.setattr(bluesky, "pinned_request", lambda *a, **kw: _FakeResponse(400, {"message": "InvalidText"}))
        with pytest.raises(bluesky.BlueskyError) as excinfo:
            bluesky.create_post(
                "https://bsky.social", "tok", "did:plc:x", "text"
            )
        assert not isinstance(excinfo.value, bluesky.BlueskyAuthError)
        assert "InvalidText" in str(excinfo.value)

    def test_create_post_expired_token_400_is_auth_error(self, monkeypatch):
        import bluesky

        monkeypatch.setattr(bluesky, "pinned_request", lambda *a, **kw: _FakeResponse(400, {"message": "ExpiredToken"}))
        with pytest.raises(bluesky.BlueskyAuthError):
            bluesky.create_post(
                "https://bsky.social", "tok", "did:plc:x", "text"
            )

    def test_create_post_401_is_auth_error(self, monkeypatch):
        import bluesky

        monkeypatch.setattr(bluesky, "pinned_request", lambda *a, **kw: _FakeResponse(401, {"message": "nope"}))
        with pytest.raises(bluesky.BlueskyAuthError):
            bluesky.create_post(
                "https://bsky.social", "tok", "did:plc:x", "text"
            )

    def test_refresh_session_requires_rotated_token(self, monkeypatch):
        import bluesky

        monkeypatch.setattr(
            bluesky,
            "pinned_request",
            lambda *a, **kw: _FakeResponse(200, {"did": "did:plc:x", "accessJwt": "aj"}),
        )
        with pytest.raises(bluesky.BlueskyError) as excinfo:
            bluesky.refresh_session("https://bsky.social", "old-rj")
        assert "rotated" in str(excinfo.value)

    def test_upload_blob_rejects_unsupported_type(self):
        import bluesky

        with pytest.raises(bluesky.BlueskyError):
            bluesky.upload_blob(
                "https://bsky.social", "tok", b"x", "image/avif"
            )

    def test_upload_blob_rejects_oversize(self):
        import bluesky

        with pytest.raises(bluesky.BlueskyError):
            bluesky.upload_blob(
                "https://bsky.social", "tok", b"x" * (bluesky.MAX_BLOB_BYTES + 1), "image/jpeg"
            )

    def test_upload_blob_auth_failure_raises(self, monkeypatch):
        import bluesky

        monkeypatch.setattr(bluesky, "pinned_request", lambda *a, **kw: _FakeResponse(401, {"message": "ExpiredToken"}))
        with pytest.raises(bluesky.BlueskyAuthError):
            bluesky.upload_blob("https://bsky.social", "tok", b"x", "image/jpeg")

    def test_upload_blob_network_failure_returns_none(self, monkeypatch):
        import bluesky

        def _boom(*a, **kw):
            raise bluesky.httpx.RequestError("down")

        monkeypatch.setattr(bluesky, "pinned_request", _boom)
        assert bluesky.upload_blob("https://bsky.social", "tok", b"x", "image/jpeg") is None

    def test_test_connection_cleans_up_session(self, monkeypatch):
        import bluesky

        cleanup = []
        monkeypatch.setattr(
            bluesky, "resolve_pds", lambda handle: ("did:plc:x", "https://bsky.social")
        )
        monkeypatch.setattr(
            bluesky,
            "create_session",
            lambda pds, handle, pw: {
                "did": "did:plc:x",
                "access_jwt": "aj",
                "refresh_jwt": "rj",
            },
        )
        monkeypatch.setattr(
            bluesky,
            "delete_session",
            lambda pds, rj: cleanup.append((pds, rj)),
        )
        ok, msg = bluesky.test_connection("user.bsky.social", "pw")
        assert ok is True
        assert cleanup == [("https://bsky.social", "rj")]

# ── API routes ───────────────────────────────────────────────────────────────

class TestBlueskyAccountRoutes:
    @pytest.fixture()
    def client(self, db_tmp, monkeypatch):
        import app as app_module

        monkeypatch.setattr(app_module.settings, "AUTH_TOKEN", None)
        monkeypatch.setattr(app_module, "bluesky_session_expiry", lambda jwt: "2099-01-01 00:00:00")

        from fastapi.testclient import TestClient

        return TestClient(app_module.app)

    def test_add_account_verifies_and_stores(self, client, monkeypatch):
        import app as app_module
        import database

        monkeypatch.setattr(
            app_module, "bluesky_resolve_pds", lambda handle: ("did:plc:abc", "https://bsky.social")
        )
        monkeypatch.setattr(
            app_module,
            "bluesky_create_session",
            lambda pds, handle, pw: {
                "did": "did:plc:abc",
                "access_jwt": "aj",
                "refresh_jwt": "rj",
            },
        )

        resp = client.post(
            "/api/bluesky-accounts",
            data={"name": "My Bsky", "handle": "@User.Bsky.Social", "app_password": "abcd-efgh-ijkl-mnop"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "bluesky_connected" in resp.headers["location"]

        with database.get_db() as db:
            row = db.execute(
                "SELECT * FROM bluesky_accounts WHERE handle = 'user.bsky.social'"
            ).fetchone()
            assert row is not None
            assert row["name"] == "My Bsky"
            assert row["did"] == "did:plc:abc"
            assert row["pds"] == "https://bsky.social"
            assert row["access_jwt"] == "aj"

    def test_add_account_bad_password_shows_error(self, client, monkeypatch):
        import app as app_module
        import database

        from bluesky import BlueskyAuthError

        monkeypatch.setattr(
            app_module, "bluesky_resolve_pds", lambda handle: ("did:plc:abc", "https://bsky.social")
        )
        monkeypatch.setattr(
            app_module,
            "bluesky_create_session",
            lambda pds, handle, pw: (_ for _ in ()).throw(BlueskyAuthError("nope")),
        )

        resp = client.post(
            "/api/bluesky-accounts",
            data={"name": "Bad", "handle": "user.bsky.social", "app_password": "wrong"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert "app password" in resp.text

        with database.get_db() as db:
            count = db.execute("SELECT COUNT(*) as c FROM bluesky_accounts").fetchone()["c"]
            assert count == 0

    def test_add_account_invalid_handle_shows_error(self, client):
        resp = client.post(
            "/api/bluesky-accounts",
            data={"name": "Bad", "handle": "not a handle", "app_password": "x"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert "handle" in resp.text

    def test_test_endpoint_reports_success(self, client, monkeypatch):
        import app as app_module
        import database

        with database.get_db() as db:
            db.execute(
                """INSERT INTO bluesky_accounts (name, handle, app_password)
                   VALUES ('main', 'user.bsky.social', 'pw')"""
            )

        monkeypatch.setattr(
            app_module, "test_bluesky_connection", lambda h, p: (True, "Connected as @user.bsky.social")
        )
        resp = client.post("/api/bluesky-accounts/1/test")
        data = resp.json()
        assert data["success"] is True
        assert "user.bsky.social" in data["message"]

    def test_delete_endpoint_removes_account(self, client):
        import database

        with database.get_db() as db:
            db.execute(
                """INSERT INTO bluesky_accounts (name, handle, app_password)
                   VALUES ('main', 'user.bsky.social', 'pw')"""
            )

        resp = client.post("/api/bluesky-accounts/1/delete", follow_redirects=False)
        assert resp.status_code == 303
        assert "bluesky_deleted" in resp.headers["location"]

        with database.get_db() as db:
            count = db.execute("SELECT COUNT(*) as c FROM bluesky_accounts").fetchone()["c"]
            assert count == 0

    def test_delete_refused_when_echoes_reference_account(self, client):
        import database

        with database.get_db() as db:
            db.execute(
                """INSERT INTO bluesky_accounts (name, handle, app_password)
                   VALUES ('main', 'user.bsky.social', 'pw')"""
            )
            db.execute(
                "INSERT INTO feeds (name, url) VALUES ('f', 'https://example.com/feed')"
            )
            db.execute(
                """INSERT INTO echoes (feed_id, destination_type, destination_id, template)
                   VALUES (1, 'bluesky', 1, '{{ title }}')"""
            )

        resp = client.post("/api/bluesky-accounts/1/delete", follow_redirects=False)
        assert resp.status_code == 200
        assert "used by echoes" in resp.text

        with database.get_db() as db:
            count = db.execute("SELECT COUNT(*) as c FROM bluesky_accounts").fetchone()["c"]
            assert count == 1  # account survived

    def test_name_is_capped_at_100_chars(self, client, monkeypatch):
        import app as app_module
        import database

        monkeypatch.setattr(
            app_module, "bluesky_resolve_pds", lambda handle: ("did:plc:abc", "https://bsky.social")
        )
        monkeypatch.setattr(
            app_module,
            "bluesky_create_session",
            lambda pds, handle, pw: {
                "did": "did:plc:abc",
                "access_jwt": "aj",
                "refresh_jwt": "rj",
            },
        )

        resp = client.post(
            "/api/bluesky-accounts",
            data={"name": "N" * 500, "handle": "user.bsky.social", "app_password": "pw"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        with database.get_db() as db:
            row = db.execute(
                "SELECT name FROM bluesky_accounts WHERE handle = 'user.bsky.social'"
            ).fetchone()
            assert len(row["name"]) == 100

# ── Echo API validation ──────────────────────────────────────────────────────

class TestEchoDestinationValidation:
    @pytest.fixture()
    def client(self, db_tmp, monkeypatch):
        import app as app_module

        monkeypatch.setattr(app_module.settings, "AUTH_TOKEN", None)
        from fastapi.testclient import TestClient

        return TestClient(app_module.app)

    def _add_bluesky_account(self):
        import database

        with database.get_db() as db:
            db.execute(
                """INSERT INTO bluesky_accounts (name, handle, app_password)
                   VALUES ('main', 'user.bsky.social', 'pw')"""
            )
            db.execute(
                "INSERT INTO feeds (name, url) VALUES ('f', 'https://example.com/feed')"
            )

    def test_create_echo_for_bluesky(self, client):
        import database

        self._add_bluesky_account()
        resp = client.post(
            "/api/echoes",
            data={
                "feed_id": "1",
                "destination_type": "bluesky",
                "bluesky_account_id": "1",
                "template": "{{ title }} {{ link }}",
                "visibility": "public",
                "filter_mode": "exclude",
                "delivery_mode": "instant",
                "enabled": "true",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        with database.get_db() as db:
            row = db.execute("SELECT * FROM echoes WHERE id = 1").fetchone()
            assert row["destination_type"] == "bluesky"
            assert row["destination_id"] == 1

    def test_create_echo_bluesky_requires_account(self, client):
        self._add_bluesky_account()
        resp = client.post(
            "/api/echoes",
            data={
                "feed_id": "1",
                "destination_type": "bluesky",
                "template": "{{ title }}",
                "visibility": "public",
                "filter_mode": "exclude",
                "delivery_mode": "instant",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_invalid_destination_type_rejected(self, client):
        self._add_bluesky_account()
        resp = client.post(
            "/api/echoes",
            data={
                "feed_id": "1",
                "destination_type": "carrier-pigeon",
                "account_id": "1",
                "visibility": "public",
                "filter_mode": "exclude",
                "delivery_mode": "instant",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400
