"""Email sender — send feed items via SMTP.

Reads per-user SMTP config from the settings table for feed digests, and
deployment-wide SMTP config from system_settings for system mail (account
verification, password reset).
"""

import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import settings
from database import get_db
from feed_parser import SSRFError, validate_outbound_url


def get_smtp_settings(user_id: int = 1) -> dict | None:
    """Load per-user SMTP settings. Returns None if not configured."""
    with get_db() as db:
        rows = db.execute(
            "SELECT key, value FROM settings WHERE key LIKE 'smtp_%' AND user_id = ?",
            (user_id,),
        ).fetchall()

    if not rows:
        return None

    settings = {row["key"]: row["value"] for row in rows}
    return _normalize(settings)


def get_system_smtp_settings() -> dict | None:
    """Load deployment-wide SMTP settings (system emails).

    Lives in system_settings, not the per-user settings table: verification
    and password-reset mail is sent by the deployment, not by tenants.
    """
    with get_db() as db:
        rows = db.execute(
            "SELECT key, value FROM system_settings WHERE key LIKE 'smtp_%'"
        ).fetchall()

    if not rows:
        return None

    settings = {row["key"]: row["value"] for row in rows}
    return _normalize(settings)


def _normalize(settings: dict) -> dict | None:
    if not settings.get("smtp_host") or not settings.get("smtp_port"):
        return None

    return {
        "host": settings.get("smtp_host", ""),
        "port": int(settings.get("smtp_port", 587)),
        "username": settings.get("smtp_username", ""),
        "password": settings.get("smtp_password", ""),
        "from_email": settings.get("smtp_from_email", ""),
        "from_name": settings.get("smtp_from_name", "FeedEcho"),
        "use_tls": settings.get("smtp_use_tls", "1") == "1",
    }


def _send_via(cfg: dict, to_email: str, subject: str, body: str) -> None:
    """Send one email through the given SMTP config. Raises on failure."""
    if settings.MULTI:
        # Re-validate the relay at dial time, not just save time. smtplib
        # re-resolves the hostname when it connects, so a save-time check
        # alone is TOCTOU-vulnerable to a low-TTL/attacker-controlled DNS
        # answer changing between save and send. This narrows the window and
        # catches a relay whose address has since become private. (Full IP
        # pinning for SMTP is deferred — no pinned smtplib transport exists;
        # the save-time check + port allowlist bound the residual.)
        try:
            validate_outbound_url(f"http://{cfg['host']}")
        except (SSRFError, ValueError) as exc:
            raise ValueError("SMTP host is not a public address") from exc
    from_email = cfg["from_email"] or cfg["username"]
    from_name = cfg["from_name"]

    msg = MIMEMultipart("alternative")
    msg["From"] = f"{from_name} <{from_email}>"
    msg["To"] = to_email
    msg["Subject"] = subject

    # Plain text version (template output is plain text)
    msg.attach(MIMEText(body, "plain"))

    context = ssl.create_default_context()
    port = cfg["port"]

    if cfg["use_tls"] and port == 465:
        # Implicit TLS (port 465)
        with smtplib.SMTP_SSL(cfg["host"], port, context=context, timeout=30) as server:
            if cfg["username"]:
                server.login(cfg["username"], cfg["password"])
            server.sendmail(from_email, [to_email], msg.as_string())
    else:
        # STARTTLS (port 587 or others)
        with smtplib.SMTP(cfg["host"], port, timeout=30) as server:
            if cfg["use_tls"]:
                server.starttls(context=context)
            if cfg["username"]:
                server.login(cfg["username"], cfg["password"])
            server.sendmail(from_email, [to_email], msg.as_string())


def send_email(to_email: str, subject: str, body: str, user_id: int = 1) -> dict:
    """Send a per-user email via the tenant's SMTP. Raises on failure."""
    settings = get_smtp_settings(user_id=user_id)
    if not settings:
        raise ValueError("SMTP not configured. Set SMTP settings first.")
    _send_via(settings, to_email, subject, body)
    return {"success": True}


def send_system_email(to_email: str, subject: str, body: str) -> dict:
    """Send a system email via the deployment SMTP. Raises on failure."""
    settings = get_system_smtp_settings()
    if not settings:
        raise ValueError("System SMTP not configured. Configure it in the admin dashboard.")
    _send_via(settings, to_email, subject, body)
    return {"success": True}


def test_smtp_connection(to_email: str = "", user_id: int = 1) -> tuple[bool, str]:
    """Test per-user SMTP settings by sending a test email."""
    try:
        smtp = get_smtp_settings(user_id=user_id)
        if not smtp:
            return False, "SMTP not configured. Set SMTP settings first."

        test_to = to_email or smtp["from_email"] or smtp["username"]
        if not test_to:
            return False, "No email address to send test to."

        _send_via(
            smtp,
            to_email=test_to,
            subject="FeedEcho Test Email",
            body="This is a test email from FeedEcho. If you received this, your SMTP settings are correct.",
        )
        return True, f"Test email sent to {test_to}"
    except Exception as e:
        return False, str(e)


def test_system_smtp_connection(to_email: str = "") -> tuple[bool, str]:
    """Test deployment SMTP settings by sending a test email."""
    try:
        smtp = get_system_smtp_settings()
        if not smtp:
            return False, "System SMTP not configured."

        test_to = to_email or smtp["from_email"] or smtp["username"]
        if not test_to:
            return False, "No email address to send test to."

        _send_via(
            smtp,
            to_email=test_to,
            subject="FeedEcho System Test Email",
            body="This is a test email from FeedEcho system mail. If you received this, the deployment SMTP settings are correct.",
        )
        return True, f"Test email sent to {test_to}"
    except Exception as e:
        return False, str(e)
