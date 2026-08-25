"""Regressions for the 2026-08-25 GLM 5.3 review fixes (sqlite paths).

One module per review batch keeps the mapping from finding to test obvious;
the Postgres-only dialect regressions live in tests/test_pg_dialect.py.
"""

import sqlite3
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from database import get_db, init_db


@pytest.fixture
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        monkeypatch.setattr("database.DB_PATH", db_path)
        init_db()
        yield db_path


@pytest.fixture
def client(temp_db, monkeypatch):
    import app as app_module

    monkeypatch.setattr(app_module.settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(app_module.settings, "MULTI", False)
    return TestClient(app_module.app)


def _seed_posted_item(status: str, attempt_count: int = 5) -> int:
    with get_db() as db:
        db.execute(
            "INSERT INTO feeds (id, name, url) VALUES (1, 'F', 'https://e.example.com/f')"
        )
        db.execute(
            "INSERT INTO accounts (id, name, username, instance, access_token)"
            " VALUES (1, 'A', 'a', 'https://m.example.com', 'tok')"
        )
        db.execute(
            "INSERT INTO echoes (id, feed_id, destination_type, destination_id, template)"
            " VALUES (1, 1, 'mastodon', 1, '{{ title }}')"
        )
        db.execute(
            "INSERT INTO posted_items (id, echo_id, item_id, item_title, status,"
            " attempt_count, error_message, next_retry_at)"
            " VALUES (1, 1, 'i-1', 'Item', ?, ?, 'boom', '2026-01-01 00:00:00')",
            (status, attempt_count),
        )
    return 1


class TestRetryResetsStatus:
    """A4: retry_post cleared the backoff but left status = 'gave_up'.

    The scheduler only reconsiders rows with status = 'failed', so the
    endpoint reported success while nothing would ever reprocess the row.
    """

    def test_retry_on_gave_up_row_returns_it_to_failed(self, client, temp_db):
        _seed_posted_item("gave_up")
        resp = client.post("/api/history/1/retry")
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        with get_db() as db:
            row = db.execute(
                "SELECT status, attempt_count, next_retry_at, error_message"
                " FROM posted_items WHERE id = 1"
            ).fetchone()
        assert row["status"] == "failed"
        assert row["attempt_count"] == 0
        assert row["next_retry_at"] is None
        assert row["error_message"] is None

    def test_retried_row_is_visible_to_the_scheduler_sweep(self, client, temp_db):
        """The predicate the scheduler actually uses must now match the row."""
        _seed_posted_item("gave_up")
        client.post("/api/history/1/retry")
        with get_db() as db:
            found = db.execute(
                """
                SELECT id FROM posted_items
                 WHERE status = 'failed'
                   AND (next_retry_at IS NULL OR next_retry_at <= ?)
                """,
                ("2026-12-31 00:00:00",),
            ).fetchall()
        assert [r["id"] for r in found] == [1]

    def test_retry_on_failed_row_still_works(self, client, temp_db):
        _seed_posted_item("failed")
        assert client.post("/api/history/1/retry").json()["success"] is True
        with get_db() as db:
            assert (
                db.execute(
                    "SELECT status FROM posted_items WHERE id = 1"
                ).fetchone()["status"]
                == "failed"
            )

    def test_retry_on_success_row_is_rejected(self, client, temp_db):
        _seed_posted_item("success")
        assert client.post("/api/history/1/retry").status_code == 404


class TestLogoutClearsBothCookies:
    """A5: logout deleted only feedecho_session, so single mode never logged out."""

    def test_logout_deletes_session_and_shared_secret_cookies(self, client, temp_db):
        resp = client.post("/logout", follow_redirects=False)
        assert resp.status_code == 302
        cleared = "; ".join(resp.headers.get_list("set-cookie"))
        assert "feedecho_session=" in cleared
        assert "feedecho_auth=" in cleared

    def test_single_mode_logout_expires_the_shared_secret_cookie(
        self, temp_db, monkeypatch
    ):
        """The shared-secret cookie is what grants access in single mode, so
        logout must expire it (httpx's jar keeps manually-set cookies, so the
        assertion is on the emitted Set-Cookie headers)."""
        import app as app_module

        monkeypatch.setattr(app_module.settings, "MULTI", False)
        monkeypatch.setattr(app_module.settings, "AUTH_TOKEN", "s3cret-token")
        c = TestClient(app_module.app)

        # Without the cookie the instance is closed, so the cookie is the
        # credential that logout has to revoke.
        assert c.get("/", follow_redirects=False).status_code in (302, 401)
        c.cookies.set("feedecho_auth", "s3cret-token")
        assert c.get("/").status_code == 200

        resp = c.post("/logout", follow_redirects=False)
        expiring = [
            h for h in resp.headers.get_list("set-cookie") if "feedecho_auth=" in h
        ]
        assert expiring, "logout did not touch feedecho_auth"
        header = expiring[0].lower()
        assert 'feedecho_auth=""' in header or "feedecho_auth=;" in header
        assert "max-age=0" in header or "expires=thu, 01 jan 1970" in header


class TestOAuthCallbackErrorPages:
    """A3: the callback is auth-exempt, so its error paths 401'd in multi mode."""

    @pytest.fixture
    def multi_client(self, temp_db, monkeypatch):
        import app as app_module

        monkeypatch.setattr(app_module.settings, "MULTI", True)
        monkeypatch.setattr(app_module.settings, "AUTH_TOKEN", None)
        monkeypatch.setattr(
            app_module.settings, "SESSION_SECRET", "x" * 40
        )
        return TestClient(app_module.app)

    def test_denied_authorization_renders_an_error_page(self, multi_client):
        resp = multi_client.get("/oauth/callback?error=access_denied")
        assert resp.status_code == 400, resp.text[:200]
        assert "Authentication required" not in resp.text
        assert "denied" in resp.text.lower()

    def test_denied_authorization_in_single_mode_still_renders(
        self, temp_db, monkeypatch
    ):
        import app as app_module

        monkeypatch.setattr(app_module.settings, "MULTI", False)
        monkeypatch.setattr(app_module.settings, "AUTH_TOKEN", None)
        resp = TestClient(app_module.app).get("/oauth/callback?error=access_denied")
        assert resp.status_code == 400
        assert "denied" in resp.text.lower()


class TestSqliteSessionEpochBackfill:
    """B9: only the Postgres migration path backfilled users.session_epoch.

    A sqlite database created before the column existed kept running without
    it, and multi mode SELECTs it on every authenticated request.
    """

    def test_init_db_adds_session_epoch_to_a_legacy_users_table(self, monkeypatch):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "legacy.db"
            # A pre-session_epoch users table, as shipped before the column.
            with sqlite3.connect(db_path) as raw:
                raw.execute(
                    """
                    CREATE TABLE users (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        email TEXT NOT NULL UNIQUE,
                        password_hash TEXT NOT NULL DEFAULT '',
                        plan TEXT NOT NULL DEFAULT 'trial',
                        trial_ends_at TIMESTAMP,
                        email_verified INTEGER NOT NULL DEFAULT 0,
                        suspended INTEGER NOT NULL DEFAULT 0,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                raw.execute(
                    "INSERT INTO users (id, email) VALUES (1, 'legacy@example.com')"
                )
            cols = {
                r[1]
                for r in sqlite3.connect(db_path).execute("PRAGMA table_info(users)")
            }
            assert "session_epoch" not in cols, "fixture must start without the column"

            monkeypatch.setattr("database.DB_PATH", db_path)
            init_db()

            with get_db() as db:
                row = db.execute(
                    "SELECT session_epoch FROM users WHERE id = 1"
                ).fetchone()
            assert row["session_epoch"] == 0


class TestNoBlockingCallsOnAsyncHandlers:
    """A1: nine async handlers called sync httpx/SMTP directly.

    An async handler runs on the single event loop, so one 30-second feed
    fetch or SMTP connect stalls every request in the process. Handlers that
    do blocking I/O must be plain `def` (FastAPI threadpool-offloads them).
    """

    BLOCKING = {
        "test_connection",
        "test_bluesky_connection",
        "test_smtp_connection",
        "test_system_smtp_connection",
        "fetch_feed",
        "check_feed",
        "fetch_image",
        "get_authorize_url",
        "exchange_code",
        "verify_credentials",
        "send_system_email",
        "send_email",
        "generate_alt_text",
        "resolve_pds",
        "create_session",
        "post_status",
        "upload_media",
    }

    def test_no_async_route_performs_blocking_io(self):
        import ast
        from pathlib import Path

        src = Path(__file__).resolve().parent.parent / "app.py"
        tree = ast.parse(src.read_text())
        offenders = []
        for node in tree.body:
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            called = set()
            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    name = getattr(child.func, "id", None) or getattr(
                        child.func, "attr", None
                    )
                    if name in self.BLOCKING:
                        called.add(name)
            if called:
                offenders.append(f"{node.name} (line {node.lineno}): {sorted(called)}")
        assert not offenders, (
            "async handlers doing blocking I/O — declare them `def` so FastAPI "
            "offloads them to the threadpool:\n  " + "\n  ".join(offenders)
        )


class TestAltTextTenantScopingAndSsrf:
    """C5 and C1: the vision-API test endpoint and outbound URL validation."""

    @pytest.fixture
    def multi_client(self, temp_db, monkeypatch):
        import app as app_module

        monkeypatch.setattr(app_module.settings, "MULTI", True)
        monkeypatch.setattr(app_module.settings, "AUTH_TOKEN", None)
        monkeypatch.setattr(app_module.settings, "SESSION_SECRET", "y" * 40)
        return app_module, TestClient(app_module.app)

    def _configure_alt_text(self, user_id: int, base_url: str) -> None:
        with get_db() as db:
            db.execute(
                "INSERT INTO users (id, email, password_hash, email_verified)"
                " VALUES (?, ?, '', 1)"
                " ON CONFLICT(id) DO UPDATE SET email_verified = 1",
                (user_id, f"u{user_id}@example.com"),
            )
            for key, value in (
                ("alt_text_ai_enabled", "1"),
                ("alt_text_ai_base_url", base_url),
                ("alt_text_ai_model", "some-vision-model"),
                ("alt_text_ai_api_key", f"key-for-user-{user_id}"),
            ):
                db.execute(
                    "INSERT INTO settings (user_id, key, value) VALUES (?, ?, ?)"
                    " ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
                    (user_id, key, value),
                )

    def test_test_endpoint_uses_the_callers_settings_not_tenant_1(
        self, multi_client, monkeypatch
    ):
        import security
        import alt_text

        app_module, c = multi_client
        self._configure_alt_text(1, "https://tenant-one.example.com/v1")
        self._configure_alt_text(42, "https://tenant-42.example.com/v1")

        seen = {}

        def _fake(image_bytes, content_type, user_id=1):
            seen["user_id"] = user_id
            return "a description"

        monkeypatch.setattr(alt_text, "generate_alt_text", _fake)
        # This test is about which tenant's settings are used, not about
        # address validation; example.com subdomains do not resolve.
        monkeypatch.setattr(alt_text, "endpoint_rejection_reason", lambda user_id=1: "")

        c.cookies.set("feedecho_session", security.sign_session(42, "u42@example.com"))
        resp = c.post("/api/settings/alt-text/test")
        assert resp.status_code == 200, resp.text[:300]
        assert resp.json()["success"] is True
        assert seen["user_id"] == 42, "the test hit another tenant's vision config"

    def test_private_base_url_is_refused_in_hosted_mode(self, monkeypatch, temp_db):
        import alt_text

        monkeypatch.setattr(alt_text.app_settings, "MULTI", True)
        self._configure_alt_text(1, "http://169.254.169.254/latest/meta-data")

        def _no_network(*args, **kwargs):
            raise AssertionError("outbound request made to a blocked address")

        # Replace the module reference on alt_text rather than mutating the
        # real httpx module's attributes.
        monkeypatch.setattr(alt_text, "httpx", type("m", (), {"Client": _no_network}))
        assert alt_text.generate_alt_text(b"\x89PNG", "image/png", user_id=1) == ""
        assert "private" in alt_text.endpoint_rejection_reason(user_id=1).lower()

    def test_lan_base_url_is_allowed_in_single_mode(self, monkeypatch, temp_db):
        """Self-hosters point this at Ollama/llama.cpp on localhost or a LAN IP.

        The SSRF guard blocks those addresses by design, so applying it in
        single mode would silently disable alt text for exactly the people the
        feature was built for.
        """
        import alt_text

        monkeypatch.setattr(alt_text.app_settings, "MULTI", False)
        self._configure_alt_text(1, "http://192.168.1.50:11434/v1")

        called = {}

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": "a cat"}}]}

        class _Client:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, **k):
                called["url"] = url
                return _Resp()

        monkeypatch.setattr(alt_text, "httpx", type("m", (), {"Client": _Client}))
        result = alt_text.generate_alt_text(b"\x89PNG", "image/png", user_id=1)
        assert result == "a cat"
        assert called["url"] == "http://192.168.1.50:11434/v1/chat/completions"
        assert alt_text.endpoint_rejection_reason(user_id=1) == ""

    def test_test_endpoint_reports_a_blocked_address_as_failure(
        self, multi_client, monkeypatch
    ):
        """A refused address must not report 'API reachable'."""
        import security
        import alt_text

        app_module, c = multi_client
        monkeypatch.setattr(alt_text.app_settings, "MULTI", True)
        self._configure_alt_text(42, "http://10.0.0.5:8080/v1")

        def _boom(*a, **k):
            raise AssertionError("request attempted against a blocked address")

        monkeypatch.setattr(alt_text, "httpx", type("m", (), {"Client": _boom}))
        c.cookies.set("feedecho_session", security.sign_session(42, "u42@example.com"))
        body = c.post("/api/settings/alt-text/test").json()
        assert body["success"] is False
        assert "refused" in body["message"].lower()

    @pytest.mark.parametrize(
        "payload",
        [
            {"choices": []},
            {"choices": [{"message": None}]},
            {"choices": [{}]},
            {"choices": "nope"},
            [],
            {"choices": [{"message": {"content": None}}]},
        ],
    )
    def test_malformed_api_responses_return_empty_not_raise(
        self, payload, monkeypatch, temp_db
    ):
        """C2: generate_alt_text documents 'never raises'."""
        import alt_text

        self._configure_alt_text(1, "https://vision.example.com/v1")

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return payload

        class _Client:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *a, **k):
                return _Resp()

        # Narrow stand-ins: if these were Exception, the function's own
        # except tuple would catch everything and the test could not tell
        # "returned empty" from "raised and was swallowed by the retry loop".
        class _FakeHttpError(Exception):
            pass

        monkeypatch.setattr(
            alt_text,
            "httpx",
            type(
                "m",
                (),
                {
                    "Client": _Client,
                    "HTTPStatusError": _FakeHttpError,
                    "RequestError": _FakeHttpError,
                },
            ),
        )
        monkeypatch.setattr(alt_text, "RETRY_DELAY", 0)
        monkeypatch.setattr(alt_text, "validate_outbound_url", lambda url: url)
        assert alt_text.generate_alt_text(b"\x89PNG", "image/png", user_id=1) == ""


class TestSecondBatchReviewFixes:
    """A2, A6, A7, A10, B8, B10, D3 from the same review."""

    def test_smtp_settings_reject_header_injection(self, client, temp_db):
        """A2: these values become mail headers, so CRLF must fail at save time."""
        resp = client.post(
            "/api/settings/smtp",
            data={
                "smtp_host": "smtp.example.com",
                "smtp_port": "587",
                "smtp_username": "user@example.com",
                "smtp_from_name": "FeedEcho\r\nBcc: victim@example.com",
                "smtp_from_email": "feedecho@example.com",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert "smtp_from_name" in resp.text
        with get_db() as db:
            stored = db.execute(
                "SELECT COUNT(*) AS c FROM settings WHERE key = 'smtp_from_name'"
            ).fetchone()["c"]
        assert stored == 0, "nothing may be persisted when validation fails"

    def test_smtp_settings_reject_out_of_range_port(self, client, temp_db):
        resp = client.post(
            "/api/settings/smtp",
            data={"smtp_host": "smtp.example.com", "smtp_port": "70000"},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert "1 and 65535" in resp.text

    def test_smtp_settings_reject_malformed_from_address(self, client, temp_db):
        resp = client.post(
            "/api/settings/smtp",
            data={
                "smtp_host": "smtp.example.com",
                "smtp_port": "587",
                "smtp_from_email": "not-an-address",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert "valid email" in resp.text

    def test_valid_smtp_settings_still_save(self, client, temp_db):
        resp = client.post(
            "/api/settings/smtp",
            data={
                "smtp_host": " smtp.example.com ",
                "smtp_port": "587",
                "smtp_username": "user@example.com",
                "smtp_from_name": "FeedEcho",
                "smtp_from_email": "feedecho@example.com",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        with get_db() as db:
            host = db.execute(
                "SELECT value FROM settings WHERE key = 'smtp_host'"
            ).fetchone()["value"]
        assert host == "smtp.example.com", "values are stripped before storage"

    def _seed_destination_and_echo(self, kind: str) -> None:
        with get_db() as db:
            db.execute(
                "INSERT INTO feeds (id, name, url) VALUES (1, 'F', 'https://e.example.com/f')"
            )
            if kind == "mastodon":
                db.execute(
                    "INSERT INTO accounts (id, name, username, instance, access_token)"
                    " VALUES (1, 'A', 'a', 'https://m.example.com', 'tok')"
                )
            else:
                db.execute(
                    "INSERT INTO email_accounts (id, name, email)"
                    " VALUES (1, 'E', 'to@example.com')"
                )
            db.execute(
                "INSERT INTO echoes (id, feed_id, destination_type, destination_id, template)"
                f" VALUES (1, 1, '{kind}', 1, '{{{{ title }}}}')"
            )

    @pytest.mark.parametrize(
        "kind,endpoint,table",
        [
            ("mastodon", "/api/accounts/1/delete", "accounts"),
            ("email", "/api/email-accounts/1/delete", "email_accounts"),
        ],
    )
    def test_destination_in_use_cannot_be_deleted(
        self, client, temp_db, kind, endpoint, table
    ):
        """A6: the Bluesky delete always guarded this; the other two did not."""
        self._seed_destination_and_echo(kind)
        resp = client.post(endpoint, follow_redirects=False)
        assert resp.status_code == 200, "expected the accounts page with an error"
        assert "used by echoes" in resp.text
        with get_db() as db:
            remaining = db.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
        assert remaining == 1, "the destination must survive"

    @pytest.mark.parametrize(
        "kind,endpoint,table",
        [
            ("mastodon", "/api/accounts/1/delete", "accounts"),
            ("email", "/api/email-accounts/1/delete", "email_accounts"),
        ],
    )
    def test_unused_destination_still_deletes(
        self, client, temp_db, kind, endpoint, table
    ):
        self._seed_destination_and_echo(kind)
        with get_db() as db:
            db.execute("UPDATE echoes SET deleted_at = '2026-01-01 00:00:00' WHERE id = 1")
        assert client.post(endpoint, follow_redirects=False).status_code == 303
        with get_db() as db:
            assert db.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"] == 0

    def test_single_mode_token_login_is_throttled(self, temp_db, monkeypatch):
        """A7: the shared token was brute-forceable with no lockout."""
        import app as app_module
        import auth

        monkeypatch.setattr(app_module.settings, "MULTI", False)
        monkeypatch.setattr(app_module.settings, "AUTH_TOKEN", "the-real-token")
        monkeypatch.setattr(auth, "_login_attempts", {})
        c = TestClient(app_module.app)

        import auth as auth_module

        limit = auth_module._MAX_LOGIN_ATTEMPTS
        for _ in range(limit - 1):
            resp = c.post("/login", data={"token": "wrong"}, follow_redirects=False)
            assert "Invalid token" in resp.text
        # The failure that reaches the limit reports the lockout instead.
        blocked = c.post("/login", data={"token": "wrong"}, follow_redirects=False)
        assert "Too many failed attempts" in blocked.text

    def test_throttle_cannot_lock_the_operator_out_of_their_own_instance(
        self, temp_db, monkeypatch
    ):
        """The correct token must work even while the bucket is full.

        Single mode has one credential and no account recovery, so gating the
        comparison on the throttle would let anyone who can reach /login keep
        the operator out indefinitely by posting wrong tokens.
        """
        import app as app_module
        import auth

        monkeypatch.setattr(app_module.settings, "MULTI", False)
        monkeypatch.setattr(app_module.settings, "AUTH_TOKEN", "the-real-token")
        monkeypatch.setattr(auth, "_login_attempts", {})
        c = TestClient(app_module.app)

        # An attacker fills the bucket from the same address.
        for _ in range(10):
            c.post("/login", data={"token": "wrong"}, follow_redirects=False)
        assert auth._throttled("testclient") is True

        resp = c.post(
            "/login", data={"token": "the-real-token"}, follow_redirects=False
        )
        assert resp.status_code == 302 and resp.headers["location"] == "/"
        assert "feedecho_auth" in resp.headers.get("set-cookie", "")
        # A successful login clears the bucket.
        assert auth._throttled("testclient") is False

    def test_non_ascii_token_is_rejected_not_a_500(self, temp_db, monkeypatch):
        """A10: compare_digest raises TypeError on non-ASCII str arguments.

        Coverage note: httpx refuses to transmit a non-ASCII cookie or header
        value at all, so the login form is the only reachable path from a test
        client. The middleware performs the identical byte-encoded comparison
        (asserted directly below) for the cookie a real browser would send.
        """
        import app as app_module
        import auth
        import secrets as _secrets

        monkeypatch.setattr(app_module.settings, "MULTI", False)
        monkeypatch.setattr(app_module.settings, "AUTH_TOKEN", "the-real-token")
        monkeypatch.setattr(auth, "_login_attempts", {})
        c = TestClient(app_module.app)

        resp = c.post("/login", data={"token": "tökén"}, follow_redirects=False)
        assert resp.status_code == 200 and "Invalid token" in resp.text

        # The comparison the middleware runs: bytes, so no TypeError.
        assert (
            _secrets.compare_digest(
                "tökén".encode("utf-8", "surrogatepass"),
                "the-real-token".encode("utf-8", "surrogatepass"),
            )
            is False
        )
        with pytest.raises(TypeError):
            # The pre-fix expression, for contrast.
            _secrets.compare_digest("tökén", "the-real-token")

    def test_claim_recheck_blocks_a_lost_mastodon_claim(self, temp_db, monkeypatch):
        """B8: only the Bluesky path re-checked the claim before dispatch."""
        import scheduler

        with get_db() as db:
            db.execute(
                "INSERT INTO feeds (id, name, url) VALUES (1, 'F', 'https://e.example.com/f')"
            )
            db.execute(
                "INSERT INTO accounts (id, name, username, instance, access_token)"
                " VALUES (1, 'A', 'a', 'https://m.example.com', 'tok')"
            )
            db.execute(
                "INSERT INTO echoes (id, feed_id, destination_type, destination_id, template)"
                " VALUES (1, 1, 'mastodon', 1, '{{ title }}')"
            )
            # Claimed by a DIFFERENT worker: this row is no longer ours.
            db.execute(
                "INSERT INTO posted_items (id, echo_id, item_id, status, claim_token)"
                " VALUES (1, 1, 'i-1', 'pending', 'someone-elses-token')"
            )
            echo = db.execute("SELECT * FROM echoes WHERE id = 1").fetchone()

        calls = []
        monkeypatch.setattr(scheduler, "post_status", lambda **k: calls.append(k))
        assert scheduler._still_owns_claim(1, "our-token") is False
        result = scheduler._send_mastodon(
            echo, {"id": "i-1", "title": "T"}, "T", 1, 1, "our-token"
        )
        assert result is False
        # The point of the guard: no post is attempted at all. (Without it the
        # send happens and only the follow-up _update_post no-ops, which is a
        # duplicate public post.)
        assert calls == [], "posted despite having lost the claim"

    def test_item_dates_are_not_shifted_by_the_host_timezone(self):
        """B10: mktime read feedparser's UTC struct_time as local wall time."""
        from feed_parser import _parse_date_struct

        # 2026-08-25 12:00:00 UTC as feedparser hands it over.
        struct = (2026, 8, 25, 12, 0, 0, 1, 237, 0)
        assert _parse_date_struct({"published_parsed": struct}) == (
            "2026-08-25T12:00:00+00:00"
        )

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://example.com/a", "https://example.com/a"),
            ("http://example.com/a", "http://example.com/a"),
            ("javascript:alert(document.cookie)", ""),
            ("JavaScript:alert(1)", ""),
            ("data:text/html;base64,PHNjcmlwdD4=", ""),
            ("  https://example.com/b  ", "https://example.com/b"),
            (None, ""),
            ("", ""),
        ],
    )
    def test_safe_url_filter_drops_non_http_schemes(self, url, expected):
        """D3: autoescaping does nothing about the scheme in an href."""
        import app as app_module

        assert app_module._safe_url(url) == expected

    def test_history_page_drops_a_javascript_item_url(self, client, temp_db):
        with get_db() as db:
            db.execute(
                "INSERT INTO feeds (id, name, url) VALUES (1, 'F', 'https://e.example.com/f')"
            )
            db.execute(
                "INSERT INTO accounts (id, name, username, instance, access_token)"
                " VALUES (1, 'A', 'a', 'https://m.example.com', 'tok')"
            )
            db.execute(
                "INSERT INTO echoes (id, feed_id, destination_type, destination_id, template)"
                " VALUES (1, 1, 'mastodon', 1, '{{ title }}')"
            )
            db.execute(
                "INSERT INTO posted_items (echo_id, item_id, item_title, item_url, status)"
                " VALUES (1, 'i-1', 'Evil', 'javascript:alert(1)', 'success')"
            )
        page = client.get("/history")
        assert page.status_code == 200
        assert "javascript:alert" not in page.text


class TestContainerRunsUnprivileged:
    """E4: the app process must not be root, and an upgrade must not brick.

    Container behaviour itself is verified by building and running the image
    (done manually against a legacy root-owned volume, a fresh volume, and an
    explicit --user). These assertions guard the contract so a later edit
    cannot quietly drop either half of it.
    """

    @staticmethod
    def _repo_file(name: str) -> str:
        from pathlib import Path

        return (Path(__file__).resolve().parent.parent / name).read_text()

    def test_entrypoint_drops_privileges_and_repairs_ownership(self):
        script = self._repo_file("docker-entrypoint.sh")
        # Drops to the app uid rather than running the server as root.
        assert "setpriv" in script and "--reuid=\"$APP_UID\"" in script
        # Repairs an inherited root-owned data dir, but only when needed, so a
        # correct bind mount is not rewritten on the host.
        assert "test -w /app/data" in script
        assert "chown -R" in script
        # An explicit --user must pass straight through.
        assert 'exec "$@"' in script

    def test_dockerfile_wires_the_entrypoint_and_creates_the_app_user(self):
        dockerfile = self._repo_file("Dockerfile")
        assert 'ENTRYPOINT ["/app/docker-entrypoint.sh"]' in dockerfile
        assert "--uid 10001" in dockerfile
        assert "chmod +x /app/docker-entrypoint.sh" in dockerfile
        # A bare USER directive would skip the ownership repair and brick every
        # deployment created before v1.13.6.
        assert "\nUSER " not in dockerfile
