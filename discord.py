"""Discord webhook client — posts feed items into Discord channels via webhooks.

FeedEcho connects with a Discord **webhook URL** (Server Settings → Integrations
→ Webhooks → New Webhook → Copy Webhook URL). No bot account or OAuth: the URL
is the credential and the posting endpoint in one. Anyone with the URL can post
to that channel, so it is treated like a token — stored, never rendered back in
full.

Flow at connect time (``connect``):

1. ``normalize_webhook_url`` validates and canonicalizes the pasted URL: https,
   a Discord host (``discord.com`` or the legacy ``discordapp.com`` alias), and
   an ``/api/webhooks/<id>/<token>`` path. discordapp.com URLs are rewritten to
   discord.com so the same webhook never becomes two account rows.
2. ``inspect_webhook`` GETs the URL — Discord returns the webhook's metadata
   (name, channel id) without posting anything, proving the URL works and
   pre-filling a display name.

Sending is ``POST`` to the same URL with a JSON body: ``content`` (the rendered
template, truncated to Discord's 2000-character limit) plus one embed carrying
title/url/image when image attachments are on and the feed item has one.
Discord fetches embed images server-side, so FeedEcho never downloads or
uploads the image — a private or oversized image simply renders the post
without the embed picture, and the text delivery is unaffected.

Rate limits: Discord allows 30 webhook messages per minute per channel (429
with a Retry-After header beyond that). 429s are transient errors and ride the
scheduler's existing bounded retry pipeline; Discord's suggested wait is
carried on the exception and logged.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlsplit

import httpx

from feed_parser import SSRFError, pinned_request

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30

# Discord message `content` limit. Embeds add their own smaller caps
# (title 256); truncated locally rather than discovering the limit via 400s.
MAX_CONTENT_CHARS = 2000
MAX_EMBED_TITLE_CHARS = 256

# Webhook IDs are snowflakes (17-20 digits); tokens are a long opaque
# base64-ish string. Loose bounds on purpose: Discord has changed token
# lengths before, and the GET verification below is the real check.
_WEBHOOK_URL_RE = re.compile(
    r"^https://(?P<host>discordapp\.com|discord\.com)"
    r"/api/webhooks/(?P<webhook_id>\d{16,20})/(?P<token>[A-Za-z0-9_-]{20,120})$",
    re.IGNORECASE,
)


class DiscordError(Exception):
    """Base error for Discord webhook API interactions."""


class DiscordRateLimitError(DiscordError):
    """Discord 429 rate limit. Carries Discord's suggested wait in seconds.

    Still a transient error (the scheduler's bounded backoff retries it), but
    the retry_after value is logged so operators can see the throttle.
    """

    def __init__(self, retry_after: float | None = None):
        self.retry_after = retry_after
        msg = "Discord rate limit hit"
        if retry_after is not None:
            msg += f" (retry after {retry_after:.0f}s)"
        super().__init__(msg)


class DiscordAuthError(DiscordError):
    """Webhook URL rejected (invalid token)."""


class DiscordNotFoundError(DiscordError):
    """Webhook no longer exists (deleted from the server)."""


class DiscordBadRequestError(DiscordError):
    """Discord refused the payload (400) — a formatting bug, not retryable."""


def _error_detail(response) -> str:
    """Discord errors are ``{"message": "...", "code": ...}``."""
    try:
        body = response.json()
    except ValueError:
        return ""
    if isinstance(body, dict):
        msg = body.get("message")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()[:200]
    return ""


def _rate_limit_seconds(response) -> float | None:
    """Discord's suggested wait, from the body's retry_after or the header."""
    try:
        body = response.json()
        if isinstance(body, dict):
            value = body.get("retry_after")
            if isinstance(value, (int, float)):
                return float(value)
    except ValueError:
        pass
    header = response.headers.get("Retry-After") if response.headers else None
    if header:
        try:
            return float(header)
        except ValueError:
            return None
    return None


def _raise_for_status(response, action: str) -> None:
    if response.status_code in (200, 204):
        return
    detail = _error_detail(response)
    if response.status_code == 429:
        raise DiscordRateLimitError(_rate_limit_seconds(response))
    if response.status_code == 401:
        raise DiscordAuthError(
            "Discord rejected this webhook URL (invalid token)."
            " Recreate the webhook on the server and connect again."
        )
    if response.status_code == 404:
        raise DiscordNotFoundError(
            "This webhook no longer exists. It was deleted on the Discord"
            " server — create a new one and connect again."
        )
    if response.status_code == 400:
        raise DiscordBadRequestError(
            f"Discord refused the {action} (HTTP 400)"
            + (f": {detail}" if detail else "")
        )
    if 300 <= response.status_code < 400:
        # Webhook endpoints never legitimately redirect; following one could
        # replay the request (and the payload) at a different host.
        raise DiscordError(
            f"Discord {action} redirected (HTTP {response.status_code})"
        )
    raise DiscordError(
        f"Discord {action} failed (HTTP {response.status_code})"
        + (f": {detail}" if detail else "")
    )


def normalize_webhook_url(raw: str) -> str:
    """Validate a pasted webhook URL and return the canonical form.

    Accepts only https URLs on discord.com / discordapp.com with the
    ``/api/webhooks/<id>/<token>`` shape. The legacy discordapp.com host is
    rewritten to discord.com (official alias, same API) so one webhook can
    never be stored twice. Raises ValueError with a user-facing message.
    """
    value = (raw or "").strip()
    if not value:
        raise ValueError("Paste the Discord webhook URL.")
    match = _WEBHOOK_URL_RE.match(value)
    if not match:
        raise ValueError(
            "That does not look like a Discord webhook URL. In Discord, open"
            " the channel you want FeedEcho to post to, then Server Settings"
            " (or channel settings) → Integrations → Webhooks → New Webhook →"
            " Copy Webhook URL, and paste it here."
        )
    return (
        f"https://discord.com/api/webhooks/"
        f"{match.group('webhook_id')}/{match.group('token')}"
    )


def inspect_webhook(webhook_url: str) -> dict:
    """GET the webhook's metadata. Returns ``{name, channel_id}``.

    Discord lets anyone holding the URL GET its metadata without posting
    anything, which makes this the connect-time and test-time verification.
    The URL is re-validated here even though connect already normalized it,
    so a tampered stored row cannot point this request elsewhere.
    """
    try:
        webhook_url = normalize_webhook_url(webhook_url)
    except ValueError as e:
        # Stored rows are normalized at connect; this only triggers for a
        # tampered DB row. Converted so callers get a DiscordError, not a
        # bare ValueError from deep inside the pipeline.
        raise DiscordError(f"Stored webhook URL is malformed: {e}") from e
    try:
        resp = pinned_request("GET", webhook_url, timeout=REQUEST_TIMEOUT)
    except (httpx.HTTPError, SSRFError) as e:
        # httpx exception text embeds the request URL, which carries the
        # webhook token — never interpolate it into logs or user messages.
        raise DiscordError(f"Could not reach Discord ({type(e).__name__})") from e
    _raise_for_status(resp, "webhook check")
    try:
        data = resp.json()
    except ValueError as e:
        raise DiscordError("Discord returned a non-JSON webhook response") from e
    if not isinstance(data, dict):
        raise DiscordError("Discord returned an unexpected webhook response")
    name = data.get("name")
    channel_id = data.get("channel_id")
    if not isinstance(name, str) or not name.strip():
        name = ""
    if not isinstance(channel_id, str):
        channel_id = ""
    return {"name": name.strip(), "channel_id": channel_id.strip()}


def connect(raw_url: str) -> dict:
    """Verify a pasted webhook URL and return everything needed to store it.

    Returns ``{webhook_url, name, channel_id}``. Raises ValueError for a
    malformed URL, DiscordAuthError for a bad token, DiscordNotFoundError for
    a deleted webhook, and DiscordError for network/API failures.
    """
    webhook_url = normalize_webhook_url(raw_url)
    info = inspect_webhook(webhook_url)
    return {"webhook_url": webhook_url, **info}


def truncate_content(text: str) -> str:
    if len(text) <= MAX_CONTENT_CHARS:
        return text
    return text[: MAX_CONTENT_CHARS - 1].rstrip() + "…"


def build_embed(title: str, url: str, image_url: str) -> dict | None:
    """An embed carrying title + link + image, or None when nothing fits.

    Discord fetches embed image URLs server-side. ``image_url`` and ``url``
    must be http(s); anything else is dropped rather than shipped to the
    channel (and rather than failing the post).
    """
    fields = {}
    if title:
        fields["title"] = title[:MAX_EMBED_TITLE_CHARS]
    if url and urlsplit(url).scheme in ("http", "https"):
        fields["url"] = url
    if image_url and urlsplit(image_url).scheme in ("http", "https"):
        fields["image"] = {"url": image_url}
    if not fields:
        return None
    return fields


def send_webhook(
    webhook_url: str,
    content: str,
    embed: dict | None = None,
) -> None:
    """POST one message to the webhook. No return value on success.

    Discord replies 204 (no ``wait`` query param, so it does not echo the
    created message back). Content is truncated to Discord's limit here so a
    long template cannot produce a 400. The URL is re-validated even though
    connect normalized it, so a tampered stored row cannot redirect the POST.
    """
    try:
        webhook_url = normalize_webhook_url(webhook_url)
    except ValueError as e:
        # A tampered DB row; permanent, since retries cannot fix the URL.
        raise DiscordBadRequestError(f"Stored webhook URL is malformed: {e}") from e
    text = truncate_content(content)
    if not text.strip():
        # Permanent: retries cannot turn an empty render into a message.
        raise DiscordBadRequestError("Cannot send an empty message to Discord")
    payload: dict = {"content": text}
    if embed:
        payload["embeds"] = [embed]
    try:
        resp = pinned_request(
            "POST", webhook_url, timeout=REQUEST_TIMEOUT, json=payload
        )
    except (httpx.HTTPError, SSRFError) as e:
        # See inspect_webhook: httpx exception text embeds the webhook token.
        raise DiscordError(f"Could not reach Discord ({type(e).__name__})") from e
    _raise_for_status(resp, "message send")


def test_connection(webhook_url: str) -> tuple[bool, str]:
    """Verify a stored webhook still works. Backs the accounts page Test button.

    Deliberately read-only (a GET metadata check, no test message into the
    user's channel), matching the Matrix and Bluesky test buttons.
    """
    try:
        info = inspect_webhook(webhook_url)
    except DiscordAuthError as e:
        return False, str(e)
    except DiscordNotFoundError as e:
        return False, str(e)
    except DiscordError as e:
        return False, str(e)
    where = f" — posts as {info['name']}" if info["name"] else ""
    return True, f"Webhook OK{where}"
