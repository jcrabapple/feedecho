"""S3 — third-party credentials encrypted at rest (multi mode only).

Fernet (cryptography) under FEEDECHO_CREDENTIAL_KEY encrypts the stored value
of every third-party credential (Mastodon/Bluesky/Matrix/micro.blog/Discord
tokens, SMTP passwords, vision API keys, OAuth client secrets). Single mode
stores plaintext (the operator owns the DB); multi mode with no key stores
plaintext (a startup warning fires). Legacy plaintext rows decrypt to
themselves, so a mixed DB keeps working.
"""

import pytest
from fastapi.testclient import TestClient
from cryptography.fernet import Fernet

import security
import settings as settings_mod
from database import get_db, init_db

TEST_KEY = Fernet.generate_key().decode()


@pytest.fixture
def _no_key(monkeypatch):
    monkeypatch.setattr(settings_mod, "CREDENTIAL_KEY", "")
    monkeypatch.setattr(settings_mod, "MULTI", True)
    security._credential_fernet = None
    security._credential_fernet_key = None
    yield


def _reset_fernet_cache(monkeypatch):
    security._credential_fernet = None
    security._credential_fernet_key = None


class TestEncryptDecrypt:
    def test_roundtrip_multi(self, monkeypatch):
        monkeypatch.setattr(settings_mod, "MULTI", True)
        monkeypatch.setattr(settings_mod, "CREDENTIAL_KEY", TEST_KEY)
        _reset_fernet_cache(monkeypatch)
        token = security.encrypt_secret("sk-abcdef-123")
        assert token != "sk-abcdef-123"
        assert token.startswith("gAAAA")
        assert security.decrypt_secret(token) == "sk-abcdef-123"

    def test_encrypt_is_nondeterministic(self, monkeypatch):
        monkeypatch.setattr(settings_mod, "MULTI", True)
        monkeypatch.setattr(settings_mod, "CREDENTIAL_KEY", TEST_KEY)
        _reset_fernet_cache(monkeypatch)
        assert security.encrypt_secret("same") != security.encrypt_secret("same")

    def test_single_mode_is_noop(self, monkeypatch):
        monkeypatch.setattr(settings_mod, "MULTI", False)
        monkeypatch.setattr(settings_mod, "CREDENTIAL_KEY", TEST_KEY)
        _reset_fernet_cache(monkeypatch)
        assert security.encrypt_secret("plaintext") == "plaintext"
        assert security.decrypt_secret("plaintext") == "plaintext"

    def test_multi_mode_no_key_is_noop(self, monkeypatch):
        monkeypatch.setattr(settings_mod, "MULTI", True)
        monkeypatch.setattr(settings_mod, "CREDENTIAL_KEY", "")
        _reset_fernet_cache(monkeypatch)
        assert security.encrypt_secret("plaintext") == "plaintext"

    def test_legacy_plaintext_falls_back(self, monkeypatch):
        monkeypatch.setattr(settings_mod, "MULTI", True)
        monkeypatch.setattr(settings_mod, "CREDENTIAL_KEY", TEST_KEY)
        _reset_fernet_cache(monkeypatch)
        # A row written before encryption shipped is plaintext; decrypt must
        # return it unchanged (not raise, not mangle).
        assert security.decrypt_secret("legacy-plaintext-token") == "legacy-plaintext-token"

    def test_empty_value_passes_through(self, monkeypatch):
        monkeypatch.setattr(settings_mod, "MULTI", True)
        monkeypatch.setattr(settings_mod, "CREDENTIAL_KEY", TEST_KEY)
        _reset_fernet_cache(monkeypatch)
        assert security.encrypt_secret("") == ""
        assert security.decrypt_secret("") == ""

    def test_hash_is_deterministic(self):
        url = "https://discord.com/api/webhooks/123/tok"
        assert security.hash_secret(url) == security.hash_secret(url)
        assert security.hash_secret(url) != security.hash_secret(url + "x")


@pytest.fixture
def multi_client(monkeypatch, tmp_path):
    """A signed-in multi-mode TestClient with a credential key set."""
    import app as app_module

    monkeypatch.setattr(app_module.settings, "MULTI", True)
    monkeypatch.setattr(app_module.settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(app_module.settings, "SESSION_SECRET", "s" * 40)
    monkeypatch.setattr(app_module.settings, "CREDENTIAL_KEY", TEST_KEY)
    monkeypatch.setattr("database.DB_PATH", str(tmp_path / "test.db"))
    _reset_fernet_cache(monkeypatch)
    init_db()
    with get_db() as db:
        db.execute(
            "INSERT INTO users (id, email, password_hash, email_verified)"
            " VALUES (42, 'u42@example.com', '', 1)"
        )
    c = TestClient(app_module.app)
    c.cookies.set("feedecho_session", security.sign_session(42, "u42@example.com"))
    return app_module, c


class TestCredentialEncryptedAtRest:
    def test_mastodon_access_token_encrypted(self, multi_client, monkeypatch):
        app_module, c = multi_client
        # Bypass the SSRF URL check so we don't do live DNS for a fake host.
        monkeypatch.setattr(app_module, "validate_url", lambda u: u)
        r = c.post(
            "/api/accounts",
            data={"name": "A", "username": "a", "instance": "https://m.example",
                  "access_token": "secret-mastodon-token"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        with get_db() as db:
            stored = db.execute(
                "SELECT access_token FROM accounts WHERE id = 1"
            ).fetchone()["access_token"]
        assert stored != "secret-mastodon-token"
        assert stored.startswith("gAAAA")
        assert security.decrypt_secret(stored) == "secret-mastodon-token"

    def test_smtp_password_encrypted(self, multi_client, monkeypatch):
        app_module, c = multi_client
        monkeypatch.setattr(app_module, "validate_outbound_url", lambda u: u)
        r = c.post(
            "/api/settings/smtp",
            data={"smtp_host": "smtp.gmail.com", "smtp_port": "587",
                  "smtp_password": "s3cret-pw", "smtp_from_email": "a@example.com"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        with get_db() as db:
            stored = db.execute(
                "SELECT value FROM settings WHERE key = 'smtp_password' AND user_id = 42"
            ).fetchone()["value"]
        assert stored.startswith("gAAAA")
        assert security.decrypt_secret(stored) == "s3cret-pw"

    def test_discord_webhook_dedup_and_encryption(self, multi_client, monkeypatch):
        app_module, c = multi_client
        monkeypatch.setattr(app_module, "discord_connect", lambda url: {
            "webhook_url": url, "name": "Bot", "channel_id": "111",
        })
        url = "https://discord.com/api/webhooks/1234567890123456789/token_abc"
        for _ in range(2):
            c.post("/api/discord-accounts", data={"webhook_url": url})
        with get_db() as db:
            rows = db.execute(
                "SELECT webhook_url FROM discord_accounts WHERE user_id = 42"
            ).fetchall()
        assert len(rows) == 1, "reconnecting the same webhook must not duplicate"
        assert rows[0]["webhook_url"].startswith("gAAAA")
        assert security.decrypt_secret(rows[0]["webhook_url"]) == url


class TestImportExportEncryption:
    def _setup(self, monkeypatch, tmp_path):
        import import_export

        monkeypatch.setattr(settings_mod, "MULTI", True)
        monkeypatch.setattr(settings_mod, "CREDENTIAL_KEY", TEST_KEY)
        monkeypatch.setattr(settings_mod, "PLAN_LIMITS", {
            "trial": {"max_feeds": 0, "max_destinations": 0,
                      "min_poll_interval": 15, "max_posts_per_hour": 60},
        })
        monkeypatch.setattr("database.DB_PATH", str(tmp_path / "multi.db"))
        _reset_fernet_cache(monkeypatch)
        init_db()
        with get_db() as db:
            db.execute(
                "INSERT INTO users (id, email, plan) VALUES (7, 't@example.com', 'trial')"
            )
        return import_export

    def test_export_decrypts_and_import_encrypts(self, monkeypatch, tmp_path):
        import_export = self._setup(monkeypatch, tmp_path)
        # Insert a Mastodon account with an encrypted token (as the app would).
        with get_db() as db:
            db.execute(
                "INSERT INTO accounts (name, username, instance, access_token, user_id)"
                " VALUES (?, ?, ?, ?, ?)",
                ("A", "a", "https://m.example",
                 security.encrypt_secret("mastodon-tok"), 7),
            )
        # Export: the document must carry the PLAINTEXT token.
        with get_db() as db:
            doc = import_export.build_export(db, 7)
        exported = doc["accounts"]["mastodon"][0]
        assert exported["access_token"] == "mastodon-tok"

        # Import into a fresh user: the stored value must be encrypted.
        with get_db() as db:
            db.execute("INSERT INTO users (id, email, plan) VALUES (8, 'u8@example.com', 'trial')")
        with get_db() as db:
            import_export.import_data(db, 8, doc)
        with get_db() as db:
            stored = db.execute(
                "SELECT access_token FROM accounts WHERE user_id = 8"
            ).fetchone()["access_token"]
        assert stored.startswith("gAAAA")
        assert security.decrypt_secret(stored) == "mastodon-tok"
