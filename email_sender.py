"""Email sender — send feed items via SMTP.

Reads SMTP config from the settings table and sends rendered template
content as a plain-text email to the configured address.
"""

import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from database import get_db


def get_smtp_settings(user_id: int = 1) -> dict | None:
    """Load SMTP settings from the settings table. Returns None if not configured."""
    with get_db() as db:
        rows = db.execute(
            "SELECT key, value FROM settings WHERE key LIKE 'smtp_%' AND user_id = ?",
            (user_id,),
        ).fetchall()

    if not rows:
        return None

    settings = {row["key"]: row["value"] for row in rows}
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


def send_email(to_email: str, subject: str, body: str, user_id: int = 1) -> dict:
    """Send an email via SMTP. Returns dict with success status.

    Raises Exception on failure.
    """
    settings = get_smtp_settings(user_id=user_id)
    if not settings:
        raise ValueError("SMTP not configured. Set SMTP settings first.")

    from_email = settings["from_email"] or settings["username"]
    from_name = settings["from_name"]

    msg = MIMEMultipart("alternative")
    msg["From"] = f"{from_name} <{from_email}>"
    msg["To"] = to_email
    msg["Subject"] = subject

    # Plain text version (template output is plain text)
    msg.attach(MIMEText(body, "plain"))

    # Connect and send
    context = ssl.create_default_context()
    port = settings["port"]

    if settings["use_tls"] and port == 465:
        # Implicit TLS (port 465)
        with smtplib.SMTP_SSL(settings["host"], port, context=context, timeout=30) as server:
            if settings["username"]:
                server.login(settings["username"], settings["password"])
            server.sendmail(from_email, [to_email], msg.as_string())
    else:
        # STARTTLS (port 587 or others)
        with smtplib.SMTP(settings["host"], port, timeout=30) as server:
            if settings["use_tls"]:
                server.starttls(context=context)
            if settings["username"]:
                server.login(settings["username"], settings["password"])
            server.sendmail(from_email, [to_email], msg.as_string())

    return {"success": True}


def test_smtp_connection(to_email: str = "", user_id: int = 1) -> tuple[bool, str]:
    """Test SMTP settings by sending a test email. Returns (success, message)."""
    try:
        smtp = get_smtp_settings(user_id=user_id)
        if not smtp:
            return False, "SMTP not configured. Set SMTP settings first."

        test_to = to_email or smtp["from_email"] or smtp["username"]
        if not test_to:
            return False, "No email address to send test to."

        send_email(
            to_email=test_to,
            subject="FeedEcho Test Email",
            body="This is a test email from FeedEcho. If you received this, your SMTP settings are correct.",
        )
        return True, f"Test email sent to {test_to}"
    except Exception as e:
        return False, str(e)
