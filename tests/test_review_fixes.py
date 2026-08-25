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

        c.cookies.set("feedecho_session", security.sign_session(42, "u42@example.com"))
        resp = c.post("/api/settings/alt-text/test")
        assert resp.status_code == 200, resp.text[:300]
        assert resp.json()["success"] is True
        assert seen["user_id"] == 42, "the test hit another tenant's vision config"

    def test_private_base_url_is_refused_before_any_request(self, monkeypatch, temp_db):
        import alt_text

        self._configure_alt_text(1, "http://169.254.169.254/latest/meta-data")

        def _no_network(*args, **kwargs):
            raise AssertionError("outbound request made to a blocked address")

        monkeypatch.setattr(alt_text.httpx, "Client", _no_network)
        assert alt_text.generate_alt_text(b"\x89PNG", "image/png", user_id=1) == ""

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
