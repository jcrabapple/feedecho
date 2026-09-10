"""Email sender — send feed items via SMTP.

Reads per-user SMTP config from the settings table for feed digests, and
deployment-wide SMTP config from system_settings for system mail (account
verification, password reset).
"""

import html
import logging
import re
import smtplib
import ssl
from email.mime.image import MIMEImage
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import settings
from database import get_db
from feed_parser import SSRFError, validate_outbound_url
from security import decrypt_secret
from utils import rows_to_dict

logger = logging.getLogger("feedecho.email_sender")


def get_smtp_settings(user_id: int = 1) -> dict | None:
    """Load per-user SMTP settings. Returns None if not configured."""
    with get_db() as db:
        rows = db.execute(
            "SELECT key, value FROM settings WHERE key LIKE 'smtp_%' AND user_id = ?",
            (user_id,),
        ).fetchall()

    if not rows:
        return None

    settings = rows_to_dict(rows)
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

    settings = rows_to_dict(rows)
    return _normalize(settings)


def _normalize(settings: dict) -> dict | None:
    if not settings.get("smtp_host") or not settings.get("smtp_port"):
        return None

    return {
        "host": settings.get("smtp_host", ""),
        "port": int(settings.get("smtp_port", 587)),
        "username": settings.get("smtp_username", ""),
        "password": decrypt_secret(settings.get("smtp_password", "")),
        "from_email": settings.get("smtp_from_email", ""),
        "from_name": settings.get("smtp_from_name", "FeedEcho"),
        "use_tls": settings.get("smtp_use_tls", "1") == "1",
    }


def _render_html_body(body: str, images: list[dict]) -> str:
    """HTML alternative with images embedded by Content-ID.

    The template output is plain text, so it is escaped verbatim before
    entering the HTML part; nothing else is trusted. Content-IDs use the
    image<index>@feedecho scheme written in _send_via.
    """
    parts = [f'<div style="white-space: pre-wrap;">{html.escape(body)}</div>']
    for i, image in enumerate(images):
        alt = html.escape((image.get("alt") or "").strip(), quote=True)
        parts.append(
            f'<img src="cid:image{i}@feedecho" alt="{alt}"'
            ' style="max-width: 100%; margin-top: 0.75em;" />'
        )
    return "".join(parts)


def _mime_image_part(cid: str, image: dict) -> MIMEImage:
    """One inline image part: base64-encoded, referenced by Content-ID."""
    content_type = (image.get("content_type") or "image/jpeg").split(";")[0].strip()
    subtype = content_type.split("/")[-1] if "/" in content_type else "jpeg"
    part = MIMEImage(image["data"], _subtype=subtype)
    part.add_header("Content-ID", f"<{cid}>")
    part.add_header("Content-Disposition", "inline")
    return part


def _send_via(cfg: dict, to_email: str, subject: str, body: str, images: list[dict] | None = None) -> None:
    """Send one email through the given SMTP config. Raises on failure.

    Without images the message is a plain multipart/alternative carrying the
    text body. With images it becomes multipart/related: the alternative
    gains an HTML part whose <img> tags reference inline image parts by
    Content-ID, so clients render the images without any remote requests.
    """
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

    images = images or []

    if images:
        root = MIMEMultipart("related")
        alternative = MIMEMultipart("alternative")
        root.attach(alternative)
    else:
        root = MIMEMultipart("alternative")
        alternative = root

    root["From"] = f"{from_name} <{from_email}>"
    root["To"] = to_email
    # Subject is built from untrusted feed content (an item title), unlike
    # the other header-bound fields above, which are validated against
    # embedded CR/LF at save time (see app.py's SMTP settings validation and
    # utils.EMAIL_RE). email.mime's compat32 policy raises HeaderParseError/
    # HeaderWriteError on serialization if a header value contains a raw
    # \r or \n, so an unsanitized title with an embedded newline (e.g. from
    # a CDATA title) would connect to and authenticate against the SMTP
    # server, then blow up on root.as_string() below — identically on every
    # retry, permanently breaking delivery for that item. Strip it here so
    # this is defended regardless of caller.
    root["Subject"] = re.sub(r"[\r\n]+", " ", subject)

    # Plain text version (template output is plain text)
    alternative.attach(MIMEText(body, "plain"))

    if images:
        alternative.attach(MIMEText(_render_html_body(body, images), "html"))
        for i, image in enumerate(images):
            root.attach(_mime_image_part(f"image{i}@feedecho", image))

    context = ssl.create_default_context()
    port = cfg["port"]

    if cfg["use_tls"] and port == 465:
        # Implicit TLS (port 465)
        with smtplib.SMTP_SSL(cfg["host"], port, context=context, timeout=30) as server:
            if cfg["username"]:
                server.login(cfg["username"], cfg["password"])
            server.sendmail(from_email, [to_email], root.as_string())
    else:
        # STARTTLS (port 587 or others)
        with smtplib.SMTP(cfg["host"], port, timeout=30) as server:
            if cfg["use_tls"]:
                server.starttls(context=context)
            if cfg["username"]:
                server.login(cfg["username"], cfg["password"])
            server.sendmail(from_email, [to_email], root.as_string())


def send_email(
    to_email: str, subject: str, body: str, user_id: int = 1, images: list[dict] | None = None
) -> dict:
    """Send a per-user email via the tenant's SMTP. Raises on failure.

    images is an optional list of {"data": bytes, "content_type": str,
    "alt": str} dicts embedded inline in the message.
    """
    settings = get_smtp_settings(user_id=user_id)
    if not settings:
        raise ValueError("SMTP not configured. Set SMTP settings first.")
    _send_via(settings, to_email, subject, body, images=images)
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
        # Catches misconfiguration (wrong host/password) and genuine bugs
        # alike with no server-side trace either way, previously — an admin
        # clicking "test" is low-volume enough that ERROR + full traceback
        # here won't spam logs, and it's the only place a real bug in this
        # path would ever surface. Error-handling audit finding 5.3b
        # (docs/reviews/2026-09-08-error-handling-audit.md).
        logger.error("SMTP test connection failed for user %s", user_id, exc_info=True)
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
        logger.error("System SMTP test connection failed", exc_info=True)
        return False, str(e)
