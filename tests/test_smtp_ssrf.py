"""S4 — SMTP relay SSRF guard + error-oracle scrub (hosted mode).

The per-tenant SMTP relay is dialed with raw smtplib (not the pinned httpx
transport), so none of the SSRF machinery applies. In multi mode the relay
address must be a public host on a standard SMTP port, and the test route must
not leak the connection's error detail (a port/banner oracle). Single mode is
unchanged: localhost/LAN relays and arbitrary ports stay legal, and the
operator keeps the raw error text.
"""

import pytest
from fastapi.testclient import TestClient

import security
from database import get_db, init_db
from feed_parser import SSRFError


@pytest.fixture
def multi_client(monkeypatch, tmp_path):
    """A signed-in multi-mode TestClient over a fresh temp DB."""
    import app as app_module

    monkeypatch.setattr(app_module.settings, "MULTI", True)
    monkeypatch.setattr(app_module.settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(app_module.settings, "SESSION_SECRET", "s" * 40)
    monkeypatch.setattr("database.DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(app_module, "_smtp_test_attempts", {})
    init_db()

    with get_db() as db:
        db.execute(
            "INSERT INTO users (id, email, password_hash, email_verified)"
            " VALUES (42, 'u42@example.com', '', 1)"
        )
    c = TestClient(app_module.app)
    c.cookies.set("feedecho_session", security.sign_session(42, "u42@example.com"))
    return app_module, c


def _save(c, host="smtp.gmail.com", port="587"):
    return c.post(
        "/api/settings/smtp",
        data={
            "smtp_host": host,
            "smtp_port": port,
            "smtp_from_email": "a@example.com",
            "smtp_from_name": "FeedEcho",
        },
        follow_redirects=False,
    )


class TestSmtpSaveSsrFGuard:
    def test_private_ip_host_rejected(self, multi_client):
        _, c = multi_client
        r = _save(c, host="10.0.0.5")
        assert r.status_code == 400
        assert "public hostname" in r.text

    def test_loopback_host_rejected(self, multi_client):
        _, c = multi_client
        r = _save(c, host="127.0.0.1")
        assert r.status_code == 400
        assert "public hostname" in r.text

    def test_hostname_resolving_to_private_rejected(self, multi_client, monkeypatch):
        app_module, c = multi_client

        def _block(url):
            raise SSRFError("Blocked: private/reserved")

        monkeypatch.setattr(app_module, "validate_outbound_url", _block)
        r = _save(c, host="internal.example.com")
        assert r.status_code == 400
        assert "public hostname" in r.text

    def test_non_smtp_port_rejected(self, multi_client, monkeypatch):
        app_module, c = multi_client
        monkeypatch.setattr(app_module, "validate_outbound_url", lambda u: u)
        r = _save(c, port="22")
        assert r.status_code == 400
        assert "25, 465, 587, or 2525" in r.text

    def test_public_host_on_smtp_port_accepted(self, multi_client, monkeypatch):
        app_module, c = multi_client
        monkeypatch.setattr(app_module, "validate_outbound_url", lambda u: u)
        r = _save(c, host="smtp.gmail.com", port="587")
        assert r.status_code == 303

    def test_malformed_host_returns_400_not_500(self, multi_client):
        _, c = multi_client
        # Unbracketed IPv6 makes urlparse raise ValueError on .hostname —
        # the guard must catch it and return 400, not a 500.
        r = _save(c, host="::1")
        assert r.status_code == 400
        assert "public hostname" in r.text

    def test_private_ip_not_reached_by_the_guard_passthrough(self, multi_client, monkeypatch):
        """The SSRF rejection happens before any DB write; a rejected save
        must not persist a partial row."""
        app_module, c = multi_client
        r = _save(c, host="169.254.169.254")
        assert r.status_code == 400
        with get_db() as db:
            row = db.execute(
                "SELECT value FROM settings WHERE key = 'smtp_host' AND user_id = 42"
            ).fetchone()
        assert row is None, "rejected save must not persist smtp_host"


class TestSmtpTestRoute:
    def test_error_detail_scrubbed(self, multi_client, monkeypatch):
        app_module, c = multi_client
        monkeypatch.setattr(app_module, "validate_outbound_url", lambda u: u)
        _save(c, host="smtp.gmail.com", port="587")

        def _revealing(to_email, user_id=1):
            return False, "Connection refused to 10.0.0.5:22 (got a non-SMTP banner)"

        monkeypatch.setattr(app_module, "test_smtp_connection", _revealing)
        r = c.post("/api/settings/smtp/test", data={"test_email": "x@example.com"})
        body = r.json()
        assert body["success"] is False
        assert "Connection refused" not in body["message"]
        assert body["message"] == "SMTP connection failed. Check the server settings and try again."

    def test_throttled_after_limit(self, multi_client, monkeypatch):
        app_module, c = multi_client
        monkeypatch.setattr(
            app_module, "test_smtp_connection", lambda to, user_id=1: (True, "sent")
        )
        for _ in range(app_module._SMTP_TEST_LIMIT):
            c.post("/api/settings/smtp/test", data={"test_email": "x@example.com"})
        r = c.post("/api/settings/smtp/test", data={"test_email": "x@example.com"})
        body = r.json()
        assert body["success"] is False
        assert "Too many test attempts" in body["message"]

    def test_success_passes_through_unscrubbed(self, multi_client, monkeypatch):
        app_module, c = multi_client
        monkeypatch.setattr(
            app_module, "test_smtp_connection", lambda to, user_id=1: (True, "Test email sent to x@example.com")
        )
        r = c.post("/api/settings/smtp/test", data={"test_email": "x@example.com"})
        body = r.json()
        assert body["success"] is True
        assert "x@example.com" in body["message"]

    def test_dial_time_revalidation_rejects_now_private_host(self, multi_client, monkeypatch):
        """Save-time validation passes (public), but at dial time the host
        resolves private — _send_via must refuse rather than dial it."""
        import email_sender

        app_module, c = multi_client
        monkeypatch.setattr(app_module, "validate_outbound_url", lambda u: u)
        _save(c, host="smtp.gmail.com", port="587")

        def _now_private(url):
            raise SSRFError("Blocked: private/reserved")

        monkeypatch.setattr(email_sender, "validate_outbound_url", _now_private)
        r = c.post("/api/settings/smtp/test", data={"test_email": "x@example.com"})
        body = r.json()
        assert body["success"] is False
        assert "SMTP connection failed" in body["message"]

    def test_bucket_cap_refuses_new_ips_under_spray(self, multi_client, monkeypatch):
        """At the tracked-IP ceiling a brand-new source is refused (fail-closed),
        so a source-IP spray cannot grow the bucket map unboundedly."""
        app_module, _ = multi_client
        monkeypatch.setattr(app_module, "_SMTP_TEST_MAX_IPS", 2)
        monkeypatch.setattr(app_module, "_smtp_last_sweep", 0.0)
        monkeypatch.setattr(app_module, "_smtp_test_attempts", {})

        assert app_module._check_and_record_smtp_test("1.1.1.1") is False
        assert app_module._check_and_record_smtp_test("2.2.2.2") is False
        # Third distinct IP hits the cap → refused.
        assert app_module._check_and_record_smtp_test("3.3.3.3") is True
        # An already-tracked IP still proceeds (under its own per-IP limit).
        assert app_module._check_and_record_smtp_test("1.1.1.1") is False


class TestSingleModeUnchanged:
    @pytest.fixture
    def single_client(self, monkeypatch, tmp_path):
        import app as app_module

        monkeypatch.setattr(app_module.settings, "MULTI", False)
        monkeypatch.setattr(app_module.settings, "AUTH_TOKEN", None)
        monkeypatch.setattr("database.DB_PATH", str(tmp_path / "test.db"))
        init_db()
        return app_module, TestClient(app_module.app)

    def test_lan_host_and_arbitrary_port_accepted(self, single_client):
        _, c = single_client
        r = _save(c, host="localhost", port="1025")
        assert r.status_code == 303

    def test_error_detail_not_scrubbed(self, single_client, monkeypatch):
        app_module, c = single_client
        _save(c, host="localhost", port="1025")

        def _revealing(to_email, user_id=1):
            return False, "Connection refused to localhost:1025"

        monkeypatch.setattr(app_module, "test_smtp_connection", _revealing)
        r = c.post("/api/settings/smtp/test", data={"test_email": "x@example.com"})
        body = r.json()
        assert body["success"] is False
        assert "Connection refused" in body["message"]
