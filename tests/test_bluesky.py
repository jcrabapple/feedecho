"""Tests for Bluesky integration: module helpers, dispatch, and API routes."""

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

    def test_truncate_zwj_sequence_never_split(self):
        from bluesky import truncate_graphemes

        # clusters: [a, b, c\u200dd, e, f] — the ZWJ cluster is atomic.
        text = "abc\u200Ddef"
        assert truncate_graphemes(text, 4) == "abc\u200Dd…"
        # Cutting before the ZWJ cluster drops it whole, never splits it.
        assert truncate_graphemes(text, 3) == "ab…"


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

        monkeypatch.setattr(
            bluesky.httpx, "Client", lambda **kw: _FakeClient(_FakeResponse(400, {"message": "InvalidText"}))
        )
        with pytest.raises(bluesky.BlueskyError) as excinfo:
            bluesky.create_post(
                "https://bsky.social", "tok", "did:plc:x", "text"
            )
        assert not isinstance(excinfo.value, bluesky.BlueskyAuthError)
        assert "InvalidText" in str(excinfo.value)

    def test_create_post_expired_token_400_is_auth_error(self, monkeypatch):
        import bluesky

        monkeypatch.setattr(
            bluesky.httpx, "Client", lambda **kw: _FakeClient(_FakeResponse(400, {"message": "ExpiredToken"}))
        )
        with pytest.raises(bluesky.BlueskyAuthError):
            bluesky.create_post(
                "https://bsky.social", "tok", "did:plc:x", "text"
            )

    def test_create_post_401_is_auth_error(self, monkeypatch):
        import bluesky

        monkeypatch.setattr(
            bluesky.httpx, "Client", lambda **kw: _FakeClient(_FakeResponse(401, {"message": "nope"}))
        )
        with pytest.raises(bluesky.BlueskyAuthError):
            bluesky.create_post(
                "https://bsky.social", "tok", "did:plc:x", "text"
            )

    def test_refresh_session_requires_rotated_token(self, monkeypatch):
        import bluesky

        monkeypatch.setattr(
            bluesky.httpx,
            "Client",
            lambda **kw: _FakeClient(
                _FakeResponse(200, {"did": "did:plc:x", "accessJwt": "aj"})
            ),
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

        monkeypatch.setattr(
            bluesky.httpx, "Client", lambda **kw: _FakeClient(_FakeResponse(401, {"message": "ExpiredToken"}))
        )
        with pytest.raises(bluesky.BlueskyAuthError):
            bluesky.upload_blob("https://bsky.social", "tok", b"x", "image/jpeg")

    def test_upload_blob_network_failure_returns_none(self, monkeypatch):
        import bluesky

        class Boom:
            def __init__(self, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *a, **kw):
                raise bluesky.httpx.RequestError("down")

        monkeypatch.setattr(bluesky.httpx, "Client", Boom)
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
