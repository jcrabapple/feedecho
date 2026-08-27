"""Invite-code registration gate (hosted beta).

Covers: code generation/uniqueness, the atomic consume (single conditional
UPDATE — rowcount decides), the register flow's gate (required vs optional,
invalid/used/revoked codes), the duplicate-email rollback NOT burning a
code, admin generate/revoke routes, and admin gating.
"""

import os
import tempfile
from unittest import mock

import pytest
from fastapi.testclient import TestClient

import auth
import database
import invites
import security
import settings
from app import app

UID = 5
ADMIN_ID = 9


@pytest.fixture()
def db_tmp(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    monkeypatch.setattr(database, "DB_PATH", database.Path(path))
    database.init_db()
    yield database
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except OSError:
            pass


@pytest.fixture()
def multi_env(monkeypatch, db_tmp):
    """Multi mode, invites REQUIRED, admin + regular user rows."""
    monkeypatch.setattr(settings, "MULTI", True)
    monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 40)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
    monkeypatch.setattr(settings, "INVITES_REQUIRED", True)
    auth._login_attempts.clear()
    auth._register_attempts.clear()
    with database.get_db() as db:
        db.execute(
            "INSERT INTO users (id, email, password_hash, email_verified, is_admin)"
            " VALUES (?, 'u@example.com', '', 1, 0)",
            (UID,),
        )
        db.execute(
            "INSERT INTO users (id, email, password_hash, email_verified, is_admin)"
            " VALUES (?, 'admin@example.com', '', 1, 1)",
            (ADMIN_ID,),
        )
    return db_tmp


@pytest.fixture()
def multi_env_open(monkeypatch, multi_env):
    """Multi mode with invites NOT required."""
    monkeypatch.setattr(settings, "INVITES_REQUIRED", False)
    return multi_env


def _client(user_id, email):
    c = TestClient(app)
    if user_id is not None:
        c.cookies.set("feedecho_session", security.sign_session(user_id, email))
    return c


@pytest.fixture()
def admin_client(multi_env):
    return _client(ADMIN_ID, "admin@example.com")


def _register(client, code=None, email="new@example.com", **overrides):
    data = {"email": email, "password": "longenough", "confirm": "longenough"}
    if code is not None:
        data["invite_code"] = code
    data.update(overrides)
    return client.post("/register", data=data, follow_redirects=False)


# ── Module: generation and consumption ──────────────────────────────────────


class TestGenerateAndConsume:
    def test_generated_codes_are_uppercase_unique_12chars(self, db_tmp):
        with db_tmp.get_db() as db:
            codes = invites.create_codes(db, 10, ADMIN_ID)
        assert len(codes) == 10
        assert len(set(codes)) == 10
        for c in codes:
            assert len(c) == 12
            assert c == c.upper()

    def test_consume_marks_used_with_user(self, db_tmp):
        with db_tmp.get_db() as db:
            (code,) = invites.create_codes(db, 1, ADMIN_ID)
            invites.validate_and_consume(db, code, UID)
            row = db.execute(
                "SELECT used_by, revoked FROM invite_codes WHERE code = ?", (code,)
            ).fetchone()
        assert row["used_by"] == UID
        assert not row["revoked"]

    def test_consume_twice_fails(self, db_tmp):
        with db_tmp.get_db() as db:
            (code,) = invites.create_codes(db, 1, ADMIN_ID)
            invites.validate_and_consume(db, code, UID)
            with pytest.raises(invites.InviteError, match="not valid"):
                invites.validate_and_consume(db, code, UID + 1)

    def test_unknown_code_fails(self, db_tmp):
        with db_tmp.get_db() as db:
            with pytest.raises(invites.InviteError, match="not valid"):
                invites.validate_and_consume(db, "NOPE12345678", UID)

    def test_revoked_code_fails(self, db_tmp):
        with db_tmp.get_db() as db:
            (code,) = invites.create_codes(db, 1, ADMIN_ID)
            assert invites.revoke(db, code, ADMIN_ID)
            # Unified message: no code-state enumeration for visitors
            with pytest.raises(invites.InviteError, match="not valid"):
                invites.validate_and_consume(db, code, UID)

    def test_consume_requires_user_id(self, db_tmp):
        """user_id is mandatory: the consume always knows its consumer."""
        with db_tmp.get_db() as db:
            (code,) = invites.create_codes(db, 1, ADMIN_ID)
            with pytest.raises(TypeError):
                invites.validate_and_consume(db, code)

    def test_revoke_used_code_returns_false(self, db_tmp):
        with db_tmp.get_db() as db:
            (code,) = invites.create_codes(db, 1, ADMIN_ID)
            invites.validate_and_consume(db, code, UID)
            assert invites.revoke(db, code, ADMIN_ID) is False

    def test_code_match_is_case_insensitive(self, db_tmp):
        with db_tmp.get_db() as db:
            (code,) = invites.create_codes(db, 1, ADMIN_ID)
            # consume with different case/whitespace must still work
            invites.validate_and_consume(db, f"  {code.lower()}  ", UID)
            row = db.execute(
                "SELECT used_by FROM invite_codes WHERE code = ?", (code,)
            ).fetchone()
        assert row["used_by"] == UID

    def test_blank_code_fails_with_required_message(self, db_tmp):
        with db_tmp.get_db() as db:
            with pytest.raises(invites.InviteError, match="required"):
                invites.validate_and_consume(db, "   ", UID)

    def test_failed_consume_leaves_code_unused(self, db_tmp):
        """A failed consume is a no-op: the code remains available."""
        with db_tmp.get_db() as db:
            (code,) = invites.create_codes(db, 1, ADMIN_ID)
            with pytest.raises(invites.InviteError):
                invites.validate_and_consume(db, code + "-typo", UID)
            row = db.execute(
                "SELECT used_by, used_at FROM invite_codes WHERE code = ?", (code,)
            ).fetchone()
        assert row["used_by"] is None and row["used_at"] is None


# ── Register gate ───────────────────────────────────────────────────────────


class TestRegisterGate:
    def test_register_page_shows_code_field_when_required(self, multi_env):
        c = _client(None, None)
        r = c.get("/register")
        assert r.status_code == 200
        assert 'name="invite_code"' in r.text

    def test_register_page_hides_field_when_open(self, multi_env_open):
        c = _client(None, None)
        r = c.get("/register")
        assert r.status_code == 200
        assert 'name="invite_code"' not in r.text

    def test_register_without_code_rejected(self, multi_env):
        c = _client(None, None)
        r = _register(c)
        assert r.status_code == 200
        assert "invite code is not valid" in r.text
        with database.get_db() as db:
            row = db.execute("SELECT id FROM users WHERE email = 'new@example.com'").fetchone()
        assert row is None

    def test_register_with_invalid_code_rejected(self, multi_env):
        c = _client(None, None)
        r = _register(c, code="WRONGCODE123")
        assert "not valid" in r.text
        with database.get_db() as db:
            row = db.execute("SELECT id FROM users WHERE email = 'new@example.com'").fetchone()
        assert row is None

    def test_used_code_gets_unified_error(self, multi_env):
        """Kimi F3: no code-state enumeration — used reads like invalid."""
        with database.get_db() as db:
            (code,) = invites.create_codes(db, 1, ADMIN_ID)
        c = _client(None, None)
        assert _register(c, code=code).status_code == 302
        r = _register(c, code=code, email="second@example.com")
        assert "not valid" in r.text
        assert "already been used" not in r.text

    def test_register_with_valid_code_creates_user_and_consumes(self, multi_env):
        with database.get_db() as db:
            (code,) = invites.create_codes(db, 1, ADMIN_ID)
        c = _client(None, None)
        r = _register(c, code=code)
        assert r.status_code == 302, r.text
        with database.get_db() as db:
            row = db.execute("SELECT id FROM users WHERE email = 'new@example.com'").fetchone()
            code_row = db.execute(
                "SELECT used_by FROM invite_codes WHERE code = ?", (code,)
            ).fetchone()
        assert row is not None
        assert code_row["used_by"] == row["id"]

    def test_same_code_cannot_register_twice(self, multi_env):
        with database.get_db() as db:
            (code,) = invites.create_codes(db, 1, ADMIN_ID)
        c = _client(None, None)
        assert _register(c, code=code).status_code == 302
        r = _register(c, code=code, email="second@example.com")
        assert "not valid" in r.text
        with database.get_db() as db:
            rows = db.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        assert rows == 4  # local, u@, admin@, first new@

    def test_open_registration_ignores_code_field(self, multi_env_open):
        c = _client(None, None)
        r = _register(c, code="GARBAGE")
        assert r.status_code == 302
        with database.get_db() as db:
            row = db.execute("SELECT id FROM users WHERE email = 'new@example.com'").fetchone()
        assert row is not None

    def test_failed_duplicate_signup_does_not_burn_code(self, multi_env):
        """The invite hold rolls back with the failed signup transaction."""
        with database.get_db() as db:
            (code,) = invites.create_codes(db, 1, ADMIN_ID)
        # 'u@example.com' already exists in the fixture
        c = _client(None, None)
        r = _register(c, code=code, email="u@example.com")
        assert "already exists" in r.text
        # Pre-check SELECT (non-consuming) fires before the INSERT, so the
        # duplicate banner comes without touching the code at all.
        with database.get_db() as db:
            row = db.execute(
                "SELECT used_by, used_at FROM invite_codes WHERE code = ?", (code,)
            ).fetchone()
        assert row["used_by"] is None and row["used_at"] is None
        # And the code still works for a fresh signup
        assert _register(c, code=code, email="fresh@example.com").status_code == 302

    def test_single_mode_has_no_invite_gate(self, monkeypatch, db_tmp):
        """Single mode: /register 404s (_require_multi) and invites never gate."""
        monkeypatch.setattr(settings, "MULTI", False)
        c = TestClient(app)
        c.headers["X-Auth-Token"] = "tok"
        monkeypatch.setattr(settings, "AUTH_TOKEN", "tok")
        r = c.get("/register")
        assert r.status_code == 404  # _require_multi, auth gate passed
        # And a single-mode-style signup flow never sees invite errors.
        assert invites.invites_required() is False


# ── Admin routes ────────────────────────────────────────────────────────────


class TestAdminInviteRoutes:
    def test_generate_creates_codes(self, admin_client):
        r = admin_client.post(
            "/admin/invites/generate", data={"count": "5"}, follow_redirects=False
        )
        assert r.status_code == 302
        assert "invites_created=5" in r.headers["location"]
        with database.get_db() as db:
            rows = invites.list_codes(db)
        assert len(rows) == 5
        assert all(row["created_by"] == ADMIN_ID for row in rows)
        assert all(not row["used_by"] and not row["revoked"] for row in rows)

    def test_generate_count_clamped(self, admin_client):
        admin_client.post("/admin/invites/generate", data={"count": "999"})
        with database.get_db() as db:
            rows = invites.list_codes(db)
        assert len(rows) == 50

    def test_revoke_unused_code(self, admin_client):
        with database.get_db() as db:
            (code,) = invites.create_codes(db, 1, ADMIN_ID)
        r = admin_client.post(
            "/admin/invites/revoke", data={"code": code}, follow_redirects=False
        )
        assert r.status_code == 302
        with database.get_db() as db:
            row = db.execute(
                "SELECT revoked FROM invite_codes WHERE code = ?", (code,)
            ).fetchone()
        assert row["revoked"] == 1

    def test_revoked_code_cannot_register(self, admin_client):
        with database.get_db() as db:
            (code,) = invites.create_codes(db, 1, ADMIN_ID)
        admin_client.post("/admin/invites/revoke", data={"code": code})
        c = _client(None, None)
        r = _register(c, code=code)
        assert "not valid" in r.text

    def test_admin_page_lists_codes(self, admin_client):
        with database.get_db() as db:
            (code,) = invites.create_codes(db, 1, ADMIN_ID)
        r = admin_client.get("/admin")
        assert r.status_code == 200
        assert code in r.text
        assert "Invite codes" in r.text

    def test_routes_require_admin(self, multi_env):
        c = _client(UID, "u@example.com")  # signed in, not admin
        r = c.post("/admin/invites/generate", data={"count": "1"}, follow_redirects=False)
        assert r.status_code == 403
        r = c.post("/admin/invites/revoke", data={"code": "x"}, follow_redirects=False)
        assert r.status_code == 403

    def test_anonymous_gets_401_on_admin_route(self, multi_env):
        c = _client(None, None)
        r = c.post("/admin/invites/generate", data={"count": "1"}, follow_redirects=False)
        # AuthMiddleware rejects anonymous API-style POSTs with 401 before
        # any handler code runs.
        assert r.status_code == 401
