"""Tests for the GitHub destination (issue #49).

Covers, per the plan doc (docs/plans/2026-11-02-github-destination.md):
- github.py: parse_repo, normalize_token, normalize_path, render_path,
  build_commit_message, the slug filter, connect(), get_file_sha, put_file,
  send_file — success paths and error mapping (401, 404, 409, 422, rate
  limits, 5xx), path encoding, and the file-size cap.
- scheduler dispatch: happy path, permanent-vs-transient failures, the
  GET-sha -> _guard_claim -> PUT ordering, and the size cap before any
  network call.
- app.py: account CRUD routes, connect/test/delete validation, echo-route
  clamps and the github_path leak rule, token hygiene (never rendered back).
- import/export: github echo without a path is rejected at import, quota
  counts, token normalization.
"""

import base64
import json
import logging

import httpx
import pytest

import auth
import database
import github
import import_export
import scheduler
import security
import settings
import template_engine
from app import app
from cryptography.fernet import Fernet
from database import get_db
from fastapi.testclient import TestClient


UID = 5
TEST_KEY = Fernet.generate_key().decode()


def _gh_account_id(*, name="blog", repo="octo/blog", branch="main",
                   token="ghp_" + "x" * 40, user_id=UID):
    with get_db() as db:
        db.execute(
            "INSERT INTO github_accounts (name, repo, branch, token, user_id)"
            " VALUES (?, ?, ?, ?, ?)",
            (name, repo, branch, token, user_id),
        )
        return db.execute("SELECT last_insert_rowid()").fetchone()[0]


def _github_echo_id(account_id, *, path="posts/{{ title | slug }}.md",
                    commit="", user_id=UID):
    with get_db() as db:
        db.execute(
            "INSERT INTO feeds (name, url, user_id) VALUES ('f',"
            " 'https://example.com/rss', ?)",
            (user_id,),
        )
        feed_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute(
            "INSERT INTO echoes (feed_id, destination_type, destination_id,"
            " template, filter_mode, delivery_mode, use_markdown,"
            " github_path, github_commit, user_id) VALUES (?, 'github', ?, ?,"
            " 'all', 'instant', 0, ?, ?, ?)",
            (feed_id, account_id, "{{ content }}\n{{ link }}", path, commit,
             user_id),
        )
        return db.execute("SELECT last_insert_rowid()").fetchone()[0]


def _mastodon_account_id():
    with get_db() as db:
        db.execute(
            "INSERT INTO accounts (name, username, instance, access_token,"
            " user_id) VALUES ('m', 'u', 'https://mastodon.social', 'tok', ?)",
            (UID,),
        )
        return db.execute("SELECT last_insert_rowid()").fetchone()[0]


ITEM = {
    "id": "1",
    "title": "Hello World",
    "link": "https://example.com/1",
    "summary": "Summary",
    "content": "Body text",
    "content_link": "",
    "author": "Tester",
    "date": "2026-01-15T09:30:00Z",
    "tags": ["news", "feed-echo"],
    "image_url": "",
    "image_alt": "",
}


@pytest.fixture()
def multi_client(monkeypatch, db_tmp):
    """Signed-in multi-mode TestClient over the temp DB (user 5)."""
    monkeypatch.setattr(settings, "MULTI", True)
    monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
    monkeypatch.setattr(settings, "STATE_SECRET", "s" * 40)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
    # Real credential encryption so "stored encrypted at rest" assertions
    # are meaningful (without a key encrypt_secret is a pass-through).
    monkeypatch.setattr(settings, "CREDENTIAL_KEY", TEST_KEY)
    security._credential_fernet = None
    security._credential_fernet_key = None
    auth._login_attempts.clear()
    auth._register_attempts.clear()

    with database.get_db() as db:
        db.execute(
            "INSERT INTO users (id, email, password_hash, email_verified)"
            " VALUES (?, 'u@example.com', '', 1)",
            (UID,),
        )
    client = TestClient(app)
    client.cookies.set(
        "feedecho_session", security.sign_session(UID, "u@example.com"))
    return client


class _FakeResponse:
    """httpx-like response with the bits github.py reads."""

    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self.headers = httpx.Headers(headers or {})
        self._payload = payload
        self.content = (json.dumps(payload).encode() if payload is not None
                        else b"")
        self.text = self.content.decode("utf-8", "replace")

    def json(self):
        return self._payload if self._payload is not None else {}


@pytest.fixture
def fake_client(monkeypatch):
    """Queue (status, payload, headers) tuples consumed in request order.

    Mocks the pinned_request seam github.py routes every call through —
    no real DNS or network.
    """
    state = {"responses": [], "calls": []}

    def _pinned(method, url, **kwargs):
        state["calls"].append((method, url, kwargs))
        assert state["responses"], f"no queued response for {method} {url}"
        status, payload, hdrs = state["responses"].pop(0)
        return _FakeResponse(status, payload, hdrs)

    monkeypatch.setattr(github, "pinned_request", _pinned)
    return state


def _last_put(state):
    return next(c for c in reversed(state["calls"]) if c[0] == "PUT")[2]["json"]


def _put_url(state):
    return next(c for c in reversed(state["calls"]) if c[0] == "PUT")[1]


class TestParseRepo:
    def test_bare(self):
        assert github.parse_repo("octo/blog") == "octo/blog"

    def test_url_forms(self):
        assert github.parse_repo("https://github.com/octo/blog") == "octo/blog"
        assert github.parse_repo("https://github.com/octo/blog.git") == "octo/blog"
        assert github.parse_repo("git@github.com:octo/blog.git") == "octo/blog"
        assert github.parse_repo("github.com/octo/blog/") == "octo/blog"

    def test_uppercase_normalized(self):
        assert github.parse_repo("Octo/Blog") == "octo/blog"

    def test_repo_with_dashes_underscores_digits(self):
        assert github.parse_repo("my-org/my_repo.v2") == "my-org/my_repo.v2"

    @pytest.mark.parametrize("bad", [
        "", " ", "octo", "/blog", "octo/", "octo/blog/extra",
        "octo blog", "octo/../evil", "octo/%2e%2e",
    ])
    def test_invalid(self, bad):
        with pytest.raises(ValueError):
            github.parse_repo(bad)


class TestNormalizeToken:
    def test_ok(self):
        assert github.normalize_token("ghp_" + "x" * 36) == "ghp_" + "x" * 36

    def test_whitespace_stripped(self):
        assert github.normalize_token("  ghp_" + "x" * 36 + "  ") == "ghp_" + "x" * 36

    @pytest.mark.parametrize("bad", ["", "short", "ghp_" + "x" * 200])
    def test_invalid(self, bad):
        with pytest.raises(ValueError):
            github.normalize_token(bad)


class TestNormalizePath:
    def test_clean(self):
        assert github.normalize_path("posts/hello.md") == "posts/hello.md"

    def test_dot_md_appended_when_no_extension(self):
        assert github.normalize_path("posts/hello") == "posts/hello.md"

    def test_existing_extension_kept(self):
        assert github.normalize_path("docs/readme.markdown") == "docs/readme.markdown"

    def test_backslash_normalized_to_forward_slash(self):
        # Windows-style separators normalize; the result stays repo-relative.
        assert github.normalize_path("C:\\temp\\x.md") == "C:/temp/x.md"

    @pytest.mark.parametrize("bad", [
        "/etc/passwd", "../evil.md", "a/../../evil.md",
        "bad\ttab.md", "bad\nnewline.md", "", "  ",
        "x" * 1025, "x" * 300 + "/ok.md",
    ])
    def test_invalid(self, bad):
        with pytest.raises(ValueError):
            github.normalize_path(bad)


class TestSlugFilter:
    @pytest.mark.parametrize("raw,expected", [
        ("Feed Echo: test/demo post", "feed-echo-testdemo-post"),
        ("What's New (v1.0)?", "whats-new-v10"),
        ("#Attualità", "attualità"),
        ("Multiple   spaces___and--dashes", "multiple-spaces-and-dashes"),
        ("", ""),
    ])
    def test_slug(self, raw, expected):
        item = {**ITEM, "title": raw}
        assert template_engine.render_template(
            "{{ title | slug }}", item) == expected

    def test_slug_in_path_member_shape(self):
        # Support ticket 13: the member's EchoFeed path template
        # `content/blog/{{ title:slug }}_{{ date:iso }}.md` must render.
        item = {**ITEM, "title": "My Post: Demo", "date": "2026-11-02"}
        out = template_engine.render_template(
            "content/blog/{{ title | slug }}_{{ date_iso }}.md", item)
        date_iso = template_engine.render_template("{{ date_iso }}", item)
        assert out == f"content/blog/my-post-demo_{date_iso}.md"
        assert " " not in out

    def test_available_variables_lists_slug(self):
        rows = template_engine.available_variables()
        assert any("slug" in row.get("var", "") for row in rows)


class TestRenderPath:
    def test_renders_and_normalizes(self):
        assert github.render_path("posts/{{ title | slug }}.md", ITEM) \
            == "posts/hello-world.md"

    def test_bad_template_raises(self):
        with pytest.raises(Exception):
            github.render_path("posts/{% if %}.md", ITEM)

    def test_absolute_after_render_rejected(self):
        with pytest.raises(ValueError):
            github.render_path("/{{ title }}.md", ITEM)


class TestBuildCommitMessage:
    def test_blank_template_defaults_to_title(self):
        msg = github.build_commit_message("", ITEM, path="posts/hello.md")
        assert msg == "Add Hello World"

    def test_custom_template(self):
        msg = github.build_commit_message("publish: {{ title }}", ITEM)
        assert msg == "publish: Hello World"

    def test_render_failure_falls_back(self):
        # A drifted/broken commit template falls back to the path, never
        # fails the delivery (per the plan doc).
        msg = github.build_commit_message("{% if %}", ITEM, path="posts/hello.md")
        assert msg == "posts/hello.md"

    def test_empty_render_falls_back(self):
        msg = github.build_commit_message("   ", ITEM, path="posts/hello.md")
        assert msg == "Add Hello World"

    def test_capped_at_200(self):
        msg = github.build_commit_message("x" * 300, ITEM)
        assert len(msg) == 200


class TestConnect:
    _OK_REPO = (200, {"permissions": {"push": True}, "default_branch": "main"}, {})

    def test_happy_path(self, fake_client):
        fake_client["responses"] = [
            self._OK_REPO, (200, {"name": "main"}, {}),
        ]
        info = github.connect("ghp_" + "x" * 40, "octo/blog", "main")
        assert info["full_name"] == "octo/blog"
        assert info["branch"] == "main"
        assert info["default_branch"] == "main"

    def test_rejects_invalid_token(self, fake_client):
        with pytest.raises(ValueError):
            github.connect("nope", "octo/blog", "main")

    def test_rejects_invalid_repo(self, fake_client):
        with pytest.raises(ValueError):
            github.connect("ghp_" + "x" * 40, "bad repo", "main")

    def test_auth_failure_named(self, fake_client):
        fake_client["responses"] = [(401, {"message": "Bad credentials"}, {})]
        with pytest.raises(ValueError, match="[Rr]ejected"):
            github.connect("ghp_" + "x" * 40, "octo/blog", "main")

    def test_repo_not_found_named(self, fake_client):
        fake_client["responses"] = [(404, {"message": "Not Found"}, {})]
        with pytest.raises(ValueError, match="[Nn]ot found"):
            github.connect("ghp_" + "x" * 40, "octo/blog", "main")

    def test_missing_push_permission_refused(self, fake_client):
        fake_client["responses"] = [
            (200, {"permissions": {"push": False}, "default_branch": "main"}, {}),
        ]
        with pytest.raises(ValueError, match="[Rr]ead and write"):
            github.connect("ghp_" + "x" * 40, "octo/blog", "main")

    def test_missing_branch_named_with_default(self, fake_client):
        fake_client["responses"] = [
            self._OK_REPO,
            (404, {"message": "Branch not found"}, {}),
        ]
        with pytest.raises(ValueError, match="not found"):
            github.connect("ghp_" + "x" * 40, "octo/blog", "nope")


class TestGetFileSha:
    def test_found(self, fake_client):
        fake_client["responses"] = [(200, {"sha": "abc123"}, {})]
        assert github.get_file_sha(
            "tok", "octo/blog", "main", "posts/a.md") == "abc123"

    def test_missing_returns_none(self, fake_client):
        fake_client["responses"] = [(404, {"message": "Not Found"}, {})]
        assert github.get_file_sha(
            "tok", "octo/blog", "main", "posts/a.md") is None

    def test_auth_error(self, fake_client):
        fake_client["responses"] = [(401, {}, {})]
        with pytest.raises(github.GitHubAuthError):
            github.get_file_sha("tok", "octo/blog", "main", "posts/a.md")


class TestPutFile:
    def test_create(self, fake_client):
        fake_client["responses"] = [
            (201, {"commit": {"sha": "c1"},
                   "content": {"html_url": "https://github.com/o/r/blob/main/posts/a.md"}}, {}),
        ]
        out = github.put_file(
            "tok", "octo/blog", "main", "posts/a.md", "hello", "msg",
            committer=github.build_committer("Bot", "b@e.com"))
        assert out["created"] is True
        body = _last_put(fake_client)
        assert body["message"] == "msg"
        assert base64.b64decode(body["content"]).decode() == "hello"
        assert "sha" not in body
        assert body["committer"] == {"name": "Bot", "email": "b@e.com"}

    def test_update_sends_sha(self, fake_client):
        fake_client["responses"] = [
            (200, {"commit": {"sha": "c2"},
                   "content": {"html_url": "https://github.com/o/r/blob/main/posts/a.md"}}, {}),
        ]
        out = github.put_file(
            "tok", "octo/blog", "main", "posts/a.md", "v2", "msg",
            sha="abc123")
        assert out["created"] is False
        assert _last_put(fake_client)["sha"] == "abc123"

    def test_empty_committer_commits_as_token_user(self, fake_client):
        # build_committer returns None for a half-filled pair; the Contents
        # API then commits as the token's own user.
        assert github.build_committer("", "") is None
        assert github.build_committer("Bot", "") is None
        fake_client["responses"] = [
            (201, {"commit": {"sha": "c1"},
                   "content": {"html_url": "https://github.com/o/r/blob/main/p.md"}}, {}),
        ]
        github.put_file("tok", "octo/blog", "main", "p.md", "x", "msg")
        assert "committer" not in _last_put(fake_client)

    def test_path_is_encoded(self, fake_client):
        fake_client["responses"] = [
            (201, {"commit": {"sha": "c1"},
                   "content": {"html_url": "https://github.com/o/r/blob/main/a.md"}}, {}),
        ]
        github.put_file("tok", "octo/blog", "main", "posts/a b#c?.md", "x", "msg")
        assert "posts/a%20b%23c%3F.md" in _put_url(fake_client)

    @pytest.mark.parametrize("status,exc", [
        (409, github.GitHubRejectedError),
        (422, github.GitHubRejectedError),
        (401, github.GitHubAuthError),
    ])
    def test_error_mapping(self, fake_client, status, exc):
        fake_client["responses"] = [(status, {"message": "x"}, {})]
        with pytest.raises(exc):
            github.put_file("tok", "octo/blog", "main", "p.md", "x", "m")

    def test_rate_limit_is_transient(self, fake_client):
        fake_client["responses"] = [
            (403, {"message": "API rate limit exceeded"},
             {"x-ratelimit-remaining": "0", "retry-after": "60"}),
        ]
        with pytest.raises(github.GitHubRateLimitError) as ei:
            github.put_file("tok", "octo/blog", "main", "p.md", "x", "m")
        assert ei.value.retry_after == 60

    def test_forbidden_without_rate_headers_is_auth(self, fake_client):
        fake_client["responses"] = [(403, {"message": "Forbidden"}, {})]
        with pytest.raises(github.GitHubAuthError):
            github.put_file("tok", "octo/blog", "main", "p.md", "x", "m")

    def test_5xx_is_transient(self, fake_client):
        fake_client["responses"] = [(502, {"message": "Bad Gateway"}, {})]
        with pytest.raises(github.GitHubError):
            github.put_file("tok", "octo/blog", "main", "p.md", "x", "m")

    def test_size_cap_rejected_before_network(self, fake_client):
        big = "x" * (github.MAX_FILE_BYTES + 1)
        with pytest.raises(github.GitHubRejectedError):
            github.put_file("tok", "octo/blog", "main", "p.md", big, "m")
        assert fake_client["calls"] == []

    def test_token_never_in_error_messages(self, fake_client, caplog):
        token = "ghp_secret_token_value"
        with caplog.at_level(logging.WARNING):
            fake_client["responses"] = [(401, {"message": "Bad credentials"}, {})]
            with pytest.raises(github.GitHubAuthError) as ei:
                github.put_file(token, "octo/blog", "main", "p.md", "x", "m")
        assert token not in str(ei.value)
        assert token not in caplog.text


class TestSendFile:
    def test_creates_new_file(self, fake_client):
        fake_client["responses"] = [
            (404, {"message": "Not Found"}, {}),
            (201, {"commit": {"sha": "c1"},
                   "content": {"html_url": "https://github.com/o/r/blob/main/posts/a.md"}}, {}),
        ]
        out = github.send_file(
            "tok", "octo/blog", "main", "posts/a.md", "hello", "msg",
            committer={"name": "B", "email": "b@e.com"})
        assert out["created"] is True
        assert out["html_url"] == "https://github.com/o/r/blob/main/posts/a.md"

    def test_updates_existing_file(self, fake_client):
        fake_client["responses"] = [
            (200, {"sha": "abc123"}, {}),
            (200, {"commit": {"sha": "c2"},
                   "content": {"html_url": "https://github.com/o/r/blob/main/posts/a.md"}}, {}),
        ]
        out = github.send_file(
            "tok", "octo/blog", "main", "posts/a.md", "v2", "msg")
        assert out["created"] is False
        assert _last_put(fake_client)["sha"] == "abc123"


def _echo_dict(echo_id):
    with get_db() as db:
        return dict(db.execute(
            "SELECT * FROM echoes WHERE id = ?", (echo_id,)).fetchone())


def _posted_row():
    with get_db() as db:
        return db.execute(
            "SELECT status, post_url, error_message FROM posted_items"
        ).fetchone()


class TestSchedulerDispatch:
    def test_happy_path(self, db_tmp, monkeypatch):
        account_id = _gh_account_id(user_id=1)
        echo_id = _github_echo_id(account_id, user_id=1)
        monkeypatch.setattr(scheduler, "github_get_file_sha",
                            lambda *a, **k: "abc123")
        monkeypatch.setattr(scheduler, "github_put_file", lambda *a, **k: {
            "sha": "c1",
            "html_url": "https://github.com/octo/blog/blob/main/posts/hello-world.md",
            "created": False,
        })
        ok = scheduler.process_echo(_echo_dict(echo_id), ITEM, feed_name="f")
        assert ok is True
        row = _posted_row()
        assert row["status"] == "success"
        assert "posts/hello-world.md" in row["post_url"]
        assert row["error_message"] is None

    def test_missing_account_gave_up(self, db_tmp, monkeypatch):
        echo_id = _github_echo_id(999, user_id=1)
        # Terminal failures count as handled, so process_echo returns True
        # even though the row finalized to gave_up.
        ok = scheduler.process_echo(_echo_dict(echo_id), ITEM, feed_name="f")
        assert ok is True
        row = _posted_row()
        assert row["status"] == "gave_up"
        assert "not found" in row["error_message"]

    def test_guard_runs_after_get_before_put(self, db_tmp, monkeypatch):
        account_id = _gh_account_id(user_id=1)
        echo_id = _github_echo_id(account_id, user_id=1)
        order = []
        monkeypatch.setattr(scheduler, "github_get_file_sha",
                            lambda *a, **k: order.append("get") or None)
        monkeypatch.setattr(scheduler, "github_put_file", lambda *a, **k: (
            order.append("put"),
            {"sha": "s", "html_url": "u", "created": True})[1])

        def guarded(*a, **k):
            order.append("guard")
            return False
        monkeypatch.setattr(scheduler, "_guard_claim", guarded)

        ok = scheduler.process_echo(_echo_dict(echo_id), ITEM, feed_name="f")
        assert ok is False
        assert order == ["get", "guard"], \
            f"PUT ran despite failed claim: {order}"

    def test_path_render_failure_is_permanent(self, db_tmp, monkeypatch):
        account_id = _gh_account_id(user_id=1)
        echo_id = _github_echo_id(account_id, path="../evil.md", user_id=1)
        called = []
        monkeypatch.setattr(scheduler, "github_get_file_sha",
                            lambda *a, **k: called.append(1))
        ok = scheduler.process_echo(_echo_dict(echo_id), ITEM, feed_name="f")
        assert ok is True  # gave_up is terminal → handled
        assert called == []  # no network on a path that cannot be fixed
        row = _posted_row()
        assert row["status"] == "gave_up"
        assert "path" in row["error_message"]

    def test_rate_limit_is_transient_failure(self, db_tmp, monkeypatch):
        account_id = _gh_account_id(user_id=1)
        echo_id = _github_echo_id(account_id, user_id=1)

        def boom(*a, **k):
            raise github.GitHubRateLimitError(60)
        monkeypatch.setattr(scheduler, "github_get_file_sha", boom)
        ok = scheduler.process_echo(_echo_dict(echo_id), ITEM, feed_name="f")
        assert ok is False
        assert _posted_row()["status"] == "failed"

    def test_5xx_is_transient_failure(self, db_tmp, monkeypatch):
        account_id = _gh_account_id(user_id=1)
        echo_id = _github_echo_id(account_id, user_id=1)

        def boom(*a, **k):
            raise github.GitHubError("bad gateway")
        monkeypatch.setattr(scheduler, "github_get_file_sha", boom)
        ok = scheduler.process_echo(_echo_dict(echo_id), ITEM, feed_name="f")
        assert ok is False
        assert _posted_row()["status"] == "failed"

    def test_size_cap_gave_up_before_network(self, db_tmp, monkeypatch):
        account_id = _gh_account_id(user_id=1)
        echo_id = _github_echo_id(account_id, user_id=1)
        called = []
        monkeypatch.setattr(scheduler, "github_get_file_sha",
                            lambda *a, **k: called.append(1))
        # override_content bypasses the echo template render; the size is
        # chosen so base64(content) exceeds MAX_FILE_BYTES while staying
        # under the engine's own 1M-char output backstop — the GitHub cap
        # is what fires.
        import base64 as _b64
        n = github.MAX_FILE_BYTES * 3 // 4 + 100
        assert len(_b64.b64encode(b"x" * n)) > github.MAX_FILE_BYTES
        big_content = "x" * n
        ok = scheduler.process_echo(
            _echo_dict(echo_id), ITEM, feed_name="f",
            override_content=big_content)
        assert ok is True  # gave_up is terminal → handled
        assert called == []
        assert _posted_row()["status"] == "gave_up"

    def test_token_never_in_error_message(self, db_tmp, monkeypatch):
        token = "ghp_secret_token_value"
        account_id = _gh_account_id(user_id=1, token=token)
        echo_id = _github_echo_id(account_id, user_id=1)

        def boom(*a, **k):
            raise github.GitHubAuthError("unauthorized")
        monkeypatch.setattr(scheduler, "github_get_file_sha", boom)
        scheduler.process_echo(_echo_dict(echo_id), ITEM, feed_name="f")
        row = _posted_row()
        assert token not in (row["error_message"] or "")


class TestAccountRoutes:
    def test_connect_requires_fields(self, multi_client):
        r = multi_client.post("/api/github-accounts", data={})
        assert r.status_code == 422

    def test_connect_shape_invalid_repo_rejected(self, multi_client):
        # The route pre-validates with parse_repo before any live call.
        r = multi_client.post("/api/github-accounts", data={
            "repo": "x", "branch": "main", "token": "ghp_" + "x" * 36})
        assert r.status_code == 200
        assert "Repository must look like" in r.text

    def test_connect_live_error_rendered(self, multi_client, monkeypatch):
        # Shape-valid inputs reach the live call; its ValueError renders.
        def boom(*a, **k):
            raise ValueError("GitHub rejected that token.")
        monkeypatch.setattr(github, "connect", boom)
        r = multi_client.post("/api/github-accounts", data={
            "repo": "octo/blog", "branch": "main", "token": "ghp_" + "x" * 36},
            follow_redirects=False)
        assert r.status_code == 200
        assert "GitHub rejected that token." in r.text

    def test_connect_invalid_token_rejected(self, multi_client, monkeypatch):
        def boom(*a, **k):
            raise ValueError("Invalid GitHub token.")
        monkeypatch.setattr(github, "connect", boom)
        r = multi_client.post("/api/github-accounts", data={
            "repo": "octo/blog", "branch": "main", "token": "bad"})
        assert r.status_code == 200
        assert "Invalid GitHub token" in r.text

    def test_connect_live_validation_failure_rejected(self, multi_client,
                                                      monkeypatch):
        def boom(*a, **k):
            raise ValueError("GitHub rejected the token (unauthorized).")
        monkeypatch.setattr(github, "connect", boom)
        r = multi_client.post("/api/github-accounts", data={
            "repo": "octo/blog", "branch": "main", "token": "ghp_" + "x" * 36},
            follow_redirects=False)
        assert r.status_code == 200
        assert "rejected" in r.text
        with get_db() as db:
            n = db.execute("SELECT COUNT(*) FROM github_accounts").fetchone()[0]
        assert n == 0

    def test_connect_happy_path(self, multi_client, monkeypatch):
        monkeypatch.setattr(github, "connect", lambda token, repo, branch: {
            "repo": repo, "branch": branch, "default_branch": "main"})
        token = "ghp_" + "x" * 36
        r = multi_client.post("/api/github-accounts", data={
            "repo": "octo/blog", "branch": "main", "token": token},
            follow_redirects=False)
        assert r.status_code == 303
        assert "github_connected" in r.headers["location"]
        with get_db() as db:
            row = db.execute("SELECT * FROM github_accounts").fetchone()
        assert row["repo"] == "octo/blog"
        # Token stored encrypted at rest, never as plaintext.
        assert row["token"] != token
        assert security.decrypt_secret(row["token"]) == token

    def test_connect_empty_token_on_first_connect_rejected(self, multi_client):
        r = multi_client.post("/api/github-accounts", data={
            "repo": "octo/blog", "branch": "main", "token": ""})
        assert r.status_code == 200
        assert "Enter a GitHub personal access token" in r.text

    def test_reconnect_blank_token_keeps_stored(self, multi_client, monkeypatch):
        connect_calls = []

        def fake_connect(token, repo, branch):
            connect_calls.append(token)
            return {"repo": repo, "branch": branch, "default_branch": "main"}
        monkeypatch.setattr(github, "connect", fake_connect)
        token = "ghp_" + "x" * 36
        multi_client.post("/api/github-accounts", data={
            "repo": "octo/blog", "branch": "main", "token": token})
        multi_client.post("/api/github-accounts", data={
            "repo": "octo/blog", "branch": "main", "token": "",
            "name": "renamed"})
        # Blank token: no live validation, stored token untouched.
        assert connect_calls == [token]
        with get_db() as db:
            row = db.execute(
                "SELECT name, token FROM github_accounts").fetchone()
        assert row["name"] == "renamed"
        assert security.decrypt_secret(row["token"]) == token

    def test_reconnect_clears_token_with_sentinel(self, multi_client,
                                                  monkeypatch):
        monkeypatch.setattr(github, "connect", lambda token, repo, branch: {
            "repo": repo, "branch": branch, "default_branch": "main"})
        token = "ghp_" + "x" * 36
        multi_client.post("/api/github-accounts", data={
            "repo": "octo/blog", "branch": "main", "token": token})
        r = multi_client.post("/api/github-accounts", data={
            "repo": "octo/blog", "branch": "main", "token": "{}"},
            follow_redirects=False)
        assert r.status_code == 303
        with get_db() as db:
            row = db.execute("SELECT token FROM github_accounts").fetchone()
        assert security.decrypt_secret(row["token"]) == ""

    def test_same_repo_branch_updates_in_place(self, multi_client, monkeypatch):
        monkeypatch.setattr(github, "connect", lambda token, repo, branch: {
            "repo": repo, "branch": branch, "default_branch": "main"})
        multi_client.post("/api/github-accounts", data={
            "repo": "octo/blog", "branch": "main", "token": "ghp_" + "x" * 36})
        multi_client.post("/api/github-accounts", data={
            "repo": "octo/blog", "branch": "main", "token": "ghp_" + "y" * 36,
            "name": "v2"})
        with get_db() as db:
            rows = db.execute("SELECT * FROM github_accounts").fetchall()
        assert len(rows) == 1
        assert security.decrypt_secret(rows[0]["token"]) == "ghp_" + "y" * 36

    def test_test_route_reports_failure(self, multi_client, monkeypatch):
        _gh_account_id()
        monkeypatch.setattr(github, "test_connection",
                            lambda *a, **k: (False, "unauthorized"))
        r = multi_client.post("/api/github-accounts/1/test")
        assert r.status_code == 200
        assert r.json() == {"success": False, "message": "unauthorized"}

    def test_test_route_reports_success(self, multi_client, monkeypatch):
        _gh_account_id()
        monkeypatch.setattr(github, "test_connection",
                            lambda *a, **k: (True, "reachable"))
        r = multi_client.post("/api/github-accounts/1/test")
        assert r.status_code == 200
        assert r.json()["success"] is True
        assert "reachable" in r.json()["message"]

    def test_delete_guarded_by_referencing_echo(self, multi_client):
        account_id = _gh_account_id()
        _github_echo_id(account_id)
        r = multi_client.post(f"/api/github-accounts/{account_id}/delete")
        assert r.status_code == 200
        assert "used by echoes" in r.text
        with get_db() as db:
            n = db.execute("SELECT COUNT(*) FROM github_accounts").fetchone()[0]
        assert n == 1

    def test_delete_without_references(self, multi_client):
        account_id = _gh_account_id()
        r = multi_client.post(f"/api/github-accounts/{account_id}/delete",
                              follow_redirects=False)
        assert r.status_code == 303
        with get_db() as db:
            n = db.execute("SELECT COUNT(*) FROM github_accounts").fetchone()[0]
        assert n == 0

    def test_accounts_page_lists_repo_never_token(self, multi_client,
                                                  monkeypatch):
        monkeypatch.setattr(github, "connect", lambda token, repo, branch: {
            "repo": repo, "branch": branch, "default_branch": "main"})
        token = "ghp_page_secret_" + "z" * 30
        multi_client.post("/api/github-accounts", data={
            "repo": "octo/blog", "branch": "main", "token": token})
        r = multi_client.get("/accounts")
        assert r.status_code == 200
        assert "octo/blog" in r.text
        assert "main" in r.text
        assert token not in r.text


class TestEchoRoutes:
    def _feed(self):
        with get_db() as db:
            db.execute(
                "INSERT INTO feeds (id, name, url, user_id)"
                " VALUES (1, 'f', 'https://example.com/feed', 5)"
            )

    def test_github_echo_requires_path(self, multi_client):
        self._feed()
        account_id = _gh_account_id()
        r = multi_client.post("/api/echoes", data={
            "feed_id": "1", "destination_type": "github",
            "github_account_id": str(account_id), "template": "x",
            "github_path": "",
        })
        assert r.status_code == 400
        assert "file path" in r.text

    def test_github_echo_bad_path_rejected(self, multi_client):
        self._feed()
        account_id = _gh_account_id()
        r = multi_client.post("/api/echoes", data={
            "feed_id": "1", "destination_type": "github",
            "github_account_id": str(account_id), "template": "x",
            "github_path": "/abs/evil.md",
        })
        assert r.status_code == 400

    def test_github_echo_happy_path(self, multi_client):
        self._feed()
        account_id = _gh_account_id()
        path = "content/blog/{{ title | slug }}_{{ date_iso }}.md"
        r = multi_client.post("/api/echoes", data={
            "feed_id": "1", "destination_type": "github",
            "github_account_id": str(account_id), "template": "{{ content }}",
            "github_path": path,
            "github_commit": "Add {{ title }}",
        }, follow_redirects=False)
        assert r.status_code == 303
        with get_db() as db:
            row = db.execute(
                "SELECT * FROM echoes WHERE destination_type = 'github'"
            ).fetchone()
        assert row["github_path"] == path
        assert row["github_commit"] == "Add {{ title }}"

    def test_non_github_echo_drops_stale_github_path(self, multi_client):
        self._feed()
        _mastodon_account_id()
        r = multi_client.post("/api/echoes", data={
            "feed_id": "1", "destination_type": "mastodon",
            "account_id": "1", "template": "{{ title }}",
            "github_path": "stale/evil.md",
        }, follow_redirects=False)
        assert r.status_code == 303
        with get_db() as db:
            row = db.execute("SELECT github_path FROM echoes").fetchone()
        assert row["github_path"] == ""

    def test_edit_to_other_destination_clears_path(self, multi_client):
        self._feed()
        account_id = _gh_account_id()
        _mastodon_account_id()
        multi_client.post("/api/echoes", data={
            "feed_id": "1", "destination_type": "github",
            "github_account_id": str(account_id), "template": "x",
            "github_path": "posts/a.md",
        }, follow_redirects=False)
        with get_db() as db:
            echo_id = db.execute("SELECT id FROM echoes").fetchone()[0]
        multi_client.post(f"/api/echoes/{echo_id}/edit", data={
            "feed_id": "1",
            "destination_type": "mastodon", "account_id": "1",
            "template": "x", "github_path": "stale/evil.md",
        }, follow_redirects=False)
        with get_db() as db:
            row = db.execute(
                "SELECT destination_type, github_path FROM echoes WHERE id = ?",
                (echo_id,)).fetchone()
        assert row["github_path"] == ""
        assert row["destination_type"] == "mastodon"

    def test_digest_clamps_to_instant_for_github(self, multi_client):
        self._feed()
        account_id = _gh_account_id()
        r = multi_client.post("/api/echoes", data={
            "feed_id": "1", "destination_type": "github",
            "github_account_id": str(account_id), "template": "x",
            "github_path": "posts/a.md", "delivery_mode": "digest",
        }, follow_redirects=False)
        assert r.status_code == 303
        with get_db() as db:
            row = db.execute("SELECT delivery_mode FROM echoes").fetchone()
        assert row["delivery_mode"] == "instant"

    def test_attach_image_clamped_off_for_github(self, multi_client):
        self._feed()
        account_id = _gh_account_id()
        multi_client.post("/api/echoes", data={
            "feed_id": "1", "destination_type": "github",
            "github_account_id": str(account_id), "template": "x",
            "github_path": "posts/a.md", "attach_image": "on",
        }, follow_redirects=False)
        with get_db() as db:
            row = db.execute("SELECT attach_image FROM echoes").fetchone()
        assert row["attach_image"] == 0


class TestImportExport:
    def _payload(self, accounts=None, echoes=None, feeds=None):
        return {
            "format": "feedecho-export",
            "version": 1,
            "settings": {"poll_interval": 900},
            "feeds": feeds or [{"id": 1, "name": "f",
                                "url": "https://a.example/rss"}],
            "accounts": accounts or {},
            "echoes": echoes or [],
        }

    def test_github_echo_without_path_rejected(self, monkeypatch, db_tmp):
        monkeypatch.setattr(database.settings, "MULTI", False)
        payload = self._payload(
            accounts={"github": [{"id": 1, "name": "b", "repo": "octo/blog",
                                  "branch": "main",
                                  "token": "ghp_" + "x" * 36}]},
            echoes=[{"id": 1, "feed_id": 1, "destination_type": "github",
                     "destination_id": 1, "template": "{{ content }}",
                     "filter_mode": "all", "delivery_mode": "instant",
                     "github_path": ""}],
        )
        with get_db() as db:
            result = import_export.import_data(db, 1, payload)
        # Rejected at import, not silently created broken: the echo is
        # skipped and counted so the UI summary surfaces it.
        assert result["added_echoes"] == 0
        assert result["skipped_echoes"] == 1

    def test_bad_repo_rejected(self, monkeypatch, db_tmp):
        monkeypatch.setattr(database.settings, "MULTI", False)
        payload = self._payload(
            accounts={"github": [{"id": 1, "name": "b",
                                  "repo": "not a repo", "branch": "main",
                                  "token": "ghp_" + "x" * 36}]},
        )
        with get_db() as db:
            with pytest.raises(import_export.ExportError):
                import_export.import_data(db, 1, payload)

    def test_valid_github_round_trip(self, monkeypatch, db_tmp):
        monkeypatch.setattr(database.settings, "MULTI", False)
        token = "ghp_" + "x" * 36
        payload = self._payload(
            accounts={"github": [{"id": 1, "name": "b", "repo": "octo/blog",
                                  "branch": "main", "token": token,
                                  "committer_name": "Bot",
                                  "committer_email": "b@e.com"}]},
            echoes=[{"id": 1, "feed_id": 1, "destination_type": "github",
                     "destination_id": 1, "template": "{{ content }}",
                     "filter_mode": "all", "delivery_mode": "instant",
                     "github_path": "posts/a.md",
                     "github_commit": "Add {{ title }}"}],
        )
        with get_db() as db:
            result = import_export.import_data(db, 1, payload)
        assert result["added_accounts"] == 1
        assert result["added_echoes"] == 1
        with get_db() as db:
            acc = db.execute("SELECT * FROM github_accounts").fetchone()
            echo = db.execute("SELECT * FROM echoes").fetchone()
        assert acc["repo"] == "octo/blog"
        assert acc["committer_name"] == "Bot"
        assert security.decrypt_secret(acc["token"]) == token
        assert echo["github_path"] == "posts/a.md"

    def test_export_includes_github_account_and_path(self, monkeypatch, db_tmp):
        monkeypatch.setattr(database.settings, "MULTI", False)
        account_id = _gh_account_id(user_id=1)
        _github_echo_id(account_id, user_id=1)
        with get_db() as db:
            payload = import_export.build_export(db, 1)
        assert "github" in payload["accounts"]
        entry = payload["accounts"]["github"][0]
        assert entry["repo"] == "octo/blog"
        assert entry["branch"] == "main"
        assert "token" in entry  # encrypted, present for the round trip
        echo = payload["echoes"][0]
        assert echo["github_path"] == "posts/{{ title | slug }}.md"
        assert echo["github_commit"] == ""

    def test_destination_quota_counts_github_accounts(self, monkeypatch,
                                                     tmp_path):
        db_path = tmp_path / "multi.db"
        monkeypatch.setattr("database.DB_PATH", db_path)
        monkeypatch.setattr(settings, "MULTI", True)
        monkeypatch.setattr(settings, "PLAN_LIMITS", {
            "trial": {"max_feeds": 5, "max_destinations": 1,
                      "min_poll_interval": 15, "max_posts_per_hour": 60},
        })
        database.init_db()
        with get_db() as db:
            db.execute("INSERT INTO users (id, email, plan) VALUES (?, ?, ?)",
                       (7, "t@example.com", "trial"))
        token = "ghp_" + "x" * 36
        payload = self._payload(
            accounts={"github": [
                {"id": 1, "name": "a", "repo": "o/a", "branch": "main",
                 "token": token},
                {"id": 2, "name": "b", "repo": "o/b", "branch": "main",
                 "token": token},
            ]},
        )
        with get_db() as db:
            with pytest.raises(import_export.ExportError):
                import_export.import_data(db, 7, payload)


class TestDashboardIntegration:
    def test_dashboard_counts_github_destinations(self, multi_client):
        _gh_account_id(name="ghdash42")
        with get_db() as db:
            db.execute(
                "INSERT INTO feeds (id, name, url, user_id)"
                " VALUES (1, 'f', 'https://example.com/feed', 5)"
            )
            db.execute(
                "INSERT INTO echoes (feed_id, destination_type,"
                " destination_id, template, filter_mode, delivery_mode,"
                " github_path, user_id)"
                " VALUES (1, 'github', 1, 'x', 'all', 'instant',"
                " 'posts/a.md', 5)"
            )
        r = multi_client.get("/")
        assert r.status_code == 200
        # The destinations stat line renders (count includes github_accounts
        # via the dashboard subquery).
        assert "connected account" in r.text
        assert "ghdash42" in r.text

    def test_history_join_renders_github_account(self, multi_client):
        account_id = _gh_account_id(name="myblog")
        echo_id = _github_echo_id(account_id)
        with get_db() as db:
            db.execute(
                "INSERT INTO posted_items (echo_id, item_id, item_title,"
                " item_url, status, post_url)"
                " VALUES (?, 'k1', 'Hello', 'https://example.com/1',"
                " 'success',"
                " 'https://github.com/octo/blog/blob/main/posts/a.md')",
                (echo_id,),
            )
        r = multi_client.get("/history")
        assert r.status_code == 200
        assert "myblog" in r.text
        assert "posts/a.md" in r.text
