"""Tests for password hashing and signed session tokens (security module)."""

import pytest

import security
import settings


class TestPasswordHashing:
    def test_roundtrip(self):
        stored = security.hash_password("correct horse battery staple")
        assert stored.startswith("scrypt$")
        assert security.verify_password("correct horse battery staple", stored)
        assert not security.verify_password("wrong password", stored)

    def test_distinct_salts_produce_distinct_hashes(self):
        a = security.hash_password("same password")
        b = security.hash_password("same password")
        assert a != b
        assert security.verify_password("same password", a)
        assert security.verify_password("same password", b)

    def test_malformed_stored_hash_returns_false(self):
        assert not security.verify_password("pw", "not-a-scrypt-hash")
        assert not security.verify_password("pw", "")
        assert not security.verify_password("pw", "scrypt$bad")

    def test_unicode_password(self):
        stored = security.hash_password("pässwörd 🔒")
        assert security.verify_password("pässwörd 🔒", stored)


class TestSessionTokens:
    def _secret(self, monkeypatch, value="test-secret-key"):
        monkeypatch.setattr(settings, "SESSION_SECRET", value)
        monkeypatch.setattr(settings, "AUTH_TOKEN", None)

    def test_sign_and_read_roundtrip(self, monkeypatch):
        self._secret(monkeypatch)
        token = security.sign_session(42, "user@example.com")
        claims = security.read_session(token)
        assert claims == {"user_id": 42, "email": "user@example.com"}

    def test_tampered_payload_rejected(self, monkeypatch):
        self._secret(monkeypatch)
        token = security.sign_session(1, "a@example.com")
        payload, sig = token.rsplit(".", 1)
        tampered = payload[:-1] + ("A" if payload[-1] != "A" else "B")
        assert security.read_session(f"{tampered}.{sig}") is None

    def test_tampered_signature_rejected(self, monkeypatch):
        self._secret(monkeypatch)
        token = security.sign_session(1, "a@example.com")
        payload, sig = token.rsplit(".", 1)
        assert security.read_session(f"{payload}.{'0' * len(sig)}") is None

    def test_garbage_token_rejected(self, monkeypatch):
        self._secret(monkeypatch)
        assert security.read_session("") is None
        assert security.read_session("garbage") is None
        assert security.read_session("a.b.c") is None

    def test_expired_token_rejected(self, monkeypatch):
        self._secret(monkeypatch)
        monkeypatch.setattr(security, "SESSION_TTL_SECONDS", -1)
        token = security.sign_session(1, "a@example.com")
        assert security.read_session(token) is None

    def test_different_secrets_produce_invalid_tokens(self, monkeypatch):
        self._secret(monkeypatch, value="secret-one")
        token = security.sign_session(1, "a@example.com")
        monkeypatch.setattr(settings, "SESSION_SECRET", "secret-two")
        assert security.read_session(token) is None


class TestSessionSecret:
    def test_multi_mode_requires_session_secret(self, monkeypatch):
        monkeypatch.setattr(settings, "MULTI", True)
        monkeypatch.setattr(settings, "SESSION_SECRET", "")
        monkeypatch.setattr(settings, "AUTH_TOKEN", None)
        with pytest.raises(RuntimeError, match="FEEDCHO_SESSION_SECRET"):
            security.session_secret()

    def test_multi_mode_with_secret(self, monkeypatch):
        monkeypatch.setattr(settings, "MULTI", True)
        monkeypatch.setattr(settings, "SESSION_SECRET", "sekret")
        assert security.session_secret() == b"sekret"

    def test_single_mode_falls_back_to_auth_token(self, monkeypatch):
        monkeypatch.setattr(settings, "MULTI", False)
        monkeypatch.setattr(settings, "SESSION_SECRET", "")
        monkeypatch.setattr(settings, "AUTH_TOKEN", "tok")
        assert security.session_secret() == b"tok"
