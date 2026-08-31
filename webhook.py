"""Generic webhook client — POSTs feed items as JSON to any HTTP endpoint.

FeedEcho connects a webhook by **URL plus optional custom headers** (for
``Authorization`` and friends). No account identity: anything that accepts a
JSON POST can receive echoes — Slack and Mattermost incoming webhooks, ntfy
and Gotify notifications, Zapier/n8n webhooks, or any homegrown receiver.

Payload: one flat JSON object per item.

.. code-block:: json

    {
      "text": "<the rendered echo template>",
      "id": "<feed item id>",
      "title": "...", "link": "...", "summary": "...", "content": "...",
      "content_link": "...", "author": "...", "published": "...", "tags": [...],
      "image_url": "...", "image_alt": "...", "feed_name": "..."
    }

``text`` is the rendered echo template (the same string the other
destinations post); every other field is the raw parsed item, so consumers
can map any shape they need. The consumer fetches ``image_url`` itself if it
wants the picture — FeedEcho never downloads anything for webhooks.

Security:

- Multi (hosted) mode: URLs are validated with the SSRF guard (no
  private/loopback addresses, no embedded credentials, http(s) only) and
  sends go through ``ssrf_client`` — the pinned-IP transport, so a DNS
  rebinding between validation and connect cannot redirect the POST.
- Single (self-hosted) mode: the operator owns both ends, so LAN and
  loopback targets are allowed (same call as the alt-text endpoint).
- Redirects are never followed: a 3xx is a failure. Header values are
  credentials — they are stored, never rendered back, and never interpolated
  into errors or logs.
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import urlsplit

import httpx

import settings
from feed_parser import SSRFError, ssrf_client, validate_outbound_url

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30

# curl-style "Name: Value" header lines. Names are validated against the HTTP
# token characters; values are free-form but may not contain control
# characters (a bare \r in a value would confuse every receiver).
HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
MAX_HEADER_COUNT = 20
MAX_HEADERS_TEXT = 4000


class WebhookError(Exception):
    """Base error for webhook delivery."""


class WebhookRateLimitError(WebhookError):
    """429 rate limit. Transient; carries the server's suggested wait."""

    def __init__(self, retry_after: float | None = None):
        self.retry_after = retry_after
        msg = "Webhook rate limit hit"
        if retry_after is not None:
            msg += f" (retry after {retry_after:.0f}s)"
        super().__init__(msg)


class WebhookAuthError(WebhookError):
    """401/403 — the endpoint rejected our credentials."""


class WebhookNotFoundError(WebhookError):
    """404/410 — the endpoint no longer exists."""


class WebhookRejectedError(WebhookError):
    """400/422 — the endpoint refused the payload shape."""


def parse_headers(text: str) -> dict[str, str]:
    """Parse curl-style ``Name: Value`` lines into a headers dict.

    Raises ValueError naming the offending line. Empty input yields {}.
    """
    headers: dict[str, str] = {}
    seen: set[str] = set()
    if not (text or "").strip():
        return headers
    if len(text) > MAX_HEADERS_TEXT:
        raise ValueError(f"Headers are too long (max {MAX_HEADERS_TEXT} characters)")
    for lineno, raw_line in enumerate(text.split("\n"), start=1):
        # Tolerate \r\n endings, but a bare \r anywhere else is a control
        # character: letting splitlines() treat it as a line break would
        # silently turn one pasted line into two headers.
        if "\r" in raw_line.rstrip("\r"):
            raise ValueError(f"Header line {lineno} has a control character")
        line = raw_line.strip()
        if not line:
            continue
        if ":" not in line:
            raise ValueError(f"Header line {lineno} is missing a colon")
        name, _, value = line.partition(":")
        name = name.strip()
        value = value.strip()
        if not name:
            raise ValueError(f"Header line {lineno} has an empty name")
        if not HEADER_NAME_RE.match(name):
            raise ValueError(f"Header line {lineno} has an invalid header name")
        if any(c in value for c in "\r\n\x00"):
            raise ValueError(f"Header line {lineno} has a control character in its value")
        if name.lower() in seen:
            raise ValueError(f"Header line {lineno} repeats {name}")
        seen.add(name.lower())
        headers[name] = value
    if len(headers) > MAX_HEADER_COUNT:
        raise ValueError(f"Too many headers (max {MAX_HEADER_COUNT})")
    return headers


def dump_headers(headers: dict[str, str]) -> str:
    """The stored JSON dict, decoded safely for internal use."""
    if not headers:
        return "{}"
    try:
        return json.dumps(headers)
    except (TypeError, ValueError):
        return "{}"


def load_headers(raw: str | None) -> dict[str, str]:
    """Decode a stored headers JSON string; never raises.

    Re-validates on the way out (the stored blob is trusted less than the
    connect-time parse): invalid names are dropped and values with control
    characters are discarded, so a tampered or legacy row cannot smuggle
    broken headers into an outbound request.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in parsed.items():
        if not isinstance(k, str) or not HEADER_NAME_RE.match(k):
            continue
        value = str(v) if isinstance(v, (str, int, float)) else ""
        if any(c in value for c in "\r\n\x00"):
            continue
        out[k] = value
    return out


def normalize_webhook_url(raw: str) -> str:
    """Validate a webhook URL and return it stripped of whitespace.

    Raises ValueError with a user-facing message. Always rejects non-http(s)
    schemes and URLs with embedded credentials. In multi (hosted) mode the
    URL must be https (custom headers are credentials — never send them in
    cleartext from our servers) and the SSRF guard additionally blocks
    private/loopback addresses; single mode allows http and LAN targets so
    self-hosters can point at receivers on their own network.
    """
    value = (raw or "").strip()
    if not value:
        raise ValueError("Enter the webhook URL.")
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Webhook URLs must start with http:// or https://")
    if parsed.username or parsed.password:
        raise ValueError(
            "Webhook URLs cannot contain embedded credentials."
            " Put authorization in the headers field instead."
        )
    if not parsed.hostname:
        raise ValueError("Webhook URL has no hostname")
    if settings.MULTI:
        if parsed.scheme != "https":
            raise ValueError(
                "Webhook URLs must use https in hosted mode — custom headers"
                " are credentials and will not be sent in cleartext."
            )
        try:
            validate_outbound_url(value)
        except SSRFError as e:
            raise ValueError(str(e)) from e
        except ValueError as e:
            raise ValueError(str(e)) from e
    return value


def build_payload(item: dict, text: str, feed_name: str = "") -> dict:
    """The flat JSON payload for one feed item."""
    tags = item.get("tags", [])
    if not isinstance(tags, list):
        tags = []
    return {
        "text": text,
        "id": item.get("id", "") or "",
        "title": item.get("title", "") or "",
        "link": item.get("link", "") or "",
        "summary": item.get("summary", "") or "",
        "content": item.get("content", "") or "",
        "content_link": item.get("content_link", "") or "",
        "author": item.get("author", "") or "",
        "published": item.get("date", "") or "",
        "tags": tags,
        "image_url": item.get("image_url", "") or "",
        "image_alt": item.get("image_alt", "") or "",
        "feed_name": feed_name or "",
    }


def _rate_limit_seconds(response) -> float | None:
    header = response.headers.get("Retry-After") if response.headers else None
    if header:
        try:
            return float(header)
        except ValueError:
            return None
    try:
        body = response.json()
        if isinstance(body, dict):
            value = body.get("retry_after") or body.get("retryAfter")
            if isinstance(value, (int, float)):
                return float(value)
    except ValueError:
        pass
    return None


def _raise_for_status(response) -> None:
    if 200 <= response.status_code < 300:
        return
    if response.status_code == 429:
        raise WebhookRateLimitError(_rate_limit_seconds(response))
    if response.status_code in (401, 403):
        raise WebhookAuthError(
            f"Webhook endpoint rejected the request (HTTP {response.status_code})."
            " Check the authorization headers."
        )
    if response.status_code in (404, 410):
        raise WebhookNotFoundError(
            f"Webhook endpoint is gone (HTTP {response.status_code})."
        )
    if response.status_code in (400, 422):
        raise WebhookRejectedError(
            f"Webhook endpoint rejected the payload (HTTP {response.status_code})."
            " The receiver expects a different body shape."
        )
    if 300 <= response.status_code < 400:
        raise WebhookError(
            f"Webhook endpoint redirected (HTTP {response.status_code});"
            " redirects are not followed"
        )
    if 400 <= response.status_code < 500:
        # Any other 4xx (405 method, 406, 415, ...) is a permanent refusal:
        # retrying the same request cannot change the outcome.
        raise WebhookRejectedError(
            f"Webhook endpoint refused the delivery (HTTP {response.status_code})."
        )
    raise WebhookError(f"Webhook delivery failed (HTTP {response.status_code})")


def send_webhook(url: str, headers: dict[str, str], payload: dict) -> None:
    """POST one JSON payload. Raises on any non-2xx; returns on success.

    Multi mode sends through the pinned-IP SSRF transport; single mode uses a
    plain client so LAN/loopback targets work. Redirects are never followed
    in either mode. Callers must treat WebhookAuthError / WebhookNotFoundError
    / WebhookRejectedError as permanent and everything else as transient.
    """
    try:
        url = normalize_webhook_url(url)
    except ValueError as e:
        raise WebhookRejectedError(f"Stored webhook URL is malformed: {e}") from e

    if settings.MULTI:
        client, _backend = ssrf_client([url], timeout=REQUEST_TIMEOUT)
        try:
            try:
                resp = client.post(
                    url,
                    json=payload,
                    headers=headers,
                    follow_redirects=False,
                )
            except httpx.HTTPError as e:
                raise WebhookError(
                    f"Could not reach the webhook endpoint ({type(e).__name__})"
                ) from e
        finally:
            client.close()
    else:
        try:
            with httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=False) as client:
                resp = client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as e:
            raise WebhookError(
                f"Could not reach the webhook endpoint ({type(e).__name__})"
            ) from e
    _raise_for_status(resp)


def test_connection(url: str, headers: dict[str, str]) -> tuple[bool, str]:
    """Send a real test payload. Backs the accounts page Test button.

    A generic webhook has no read-only check — the only way to prove the
    endpoint works is to deliver something, so this posts a minimal message
    with the account's own headers.
    """
    try:
        send_webhook(
            url,
            headers,
            {
                "text": "FeedEcho webhook test",
                "id": "",
                "title": "FeedEcho webhook test",
                "link": "",
                "summary": "",
                "content": "",
                "content_link": "",
                "author": "",
                "published": "",
                "tags": [],
                "image_url": "",
                "image_alt": "",
                "feed_name": "",
            },
        )
    except WebhookAuthError as e:
        return False, str(e)
    except WebhookNotFoundError as e:
        return False, str(e)
    except WebhookRejectedError as e:
        return False, str(e)
    except WebhookRateLimitError as e:
        return False, str(e)
    except WebhookError as e:
        return False, str(e)
    return True, "Webhook accepted the test delivery"
