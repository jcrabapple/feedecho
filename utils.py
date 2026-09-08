"""Shared stdlib-only utilities for FeedEcho.

Created by the 2026-09-08 code-duplication audit (Tier 1 adoption). House
rules:

- Standard library only. The destination modules (discord.py, matrix.py,
  webhook.py, ...) and app.py import this module; anything that touches
  database.py or settings.py here would create a cycle.
- ``response`` parameters are httpx.Response objects.
- Every function here replaces at least two formerly copy-pasted
  implementations. New shared code lands here only when it has two users.

NOT wired up yet (later audit tranches): the four email regex call sites
migrate to EMAIL_RE / is_valid_email() after the strict-vs-loose product
decision, and plans.py's allowance checks stay local until the Tier 3
refactor.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import TypedDict

# Default HTTP request timeout (seconds) shared by the destination
# modules. Replaces the four independent REQUEST_TIMEOUT = 30 constants.
DEFAULT_REQUEST_TIMEOUT = 30


def utc_now_str() -> str:
    """Explicit UTC timestamp string, safe to bind straight into SQL.

    Never CURRENT_TIMESTAMP on Postgres: it resolves in the session time
    zone while every reader assumes UTC, so modules stamp rows by binding
    this string as a parameter instead.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# Email shape checks. Both variants reject spaces/control characters (a
# requirement for addresses that can become message headers); they differ
# on whether the domain must contain a dot. The loose variant exists
# because internal addresses like feedecho@localhost are legitimate for
# SMTP relay config. Call sites choose; see is_valid_email.
EMAIL_RE = re.compile(r"^[^@\s\r\n]+@[^@\s\r\n]+\.[^@\s\r\n]+$")
EMAIL_RE_LOOSE = re.compile(r"^[^@\s\r\n]+@[^@\s\r\n]+$")


def is_valid_email(email: str, *, require_domain_dot: bool = True) -> bool:
    """Syntactic email validation.

    require_domain_dot=True (default) requires user@host.tld;
    internal/bare-host addresses pass only with False.
    """
    pattern = EMAIL_RE if require_domain_dot else EMAIL_RE_LOOSE
    return bool(pattern.match(email))


def truncate_chars(text: str, max_chars: int) -> str:
    """Truncate to max_chars without splitting trailing whitespace.

    Identical to the former discord.truncate_content and
    matrix._truncate_body bodies.
    """
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "\u2026"


def json_error_detail(response, *keys: str) -> str:
    """Extract a human-readable error message from a JSON error body.

    Keys are tried left to right with ``a or b`` fallthrough semantics
    (matching the former destination-module copies): the first truthy
    VALUE wins, and it must itself be a non-blank string; it is then
    stripped and capped at 200 characters. A truthy non-string value
    (dict, list, number) yields "" just like the originals did. Returns
    "" when the body is not JSON or not a dict.
    """
    try:
        body = response.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    msg = next((body.get(key) for key in keys if body.get(key)), None)
    if isinstance(msg, str) and msg.strip():
        return msg.strip()[:200]
    return ""


def parse_retry_after(response) -> float | None:
    """Seconds the remote asked the client to wait, or None.

    The Retry-After header wins when present: parsed as plain seconds or
    as an RFC 7231 HTTP-date (clamped at 0 for past dates). An absent or
    unparseable header falls back to retry_after/retryAfter in a JSON
    body. Body values are returned as-is, including 0 (some services
    answer rate-limit responses with retry_after: 0), matching the former
    discord.py behavior.
    """
    header = response.headers.get("Retry-After") if response.headers else None
    if header:
        try:
            return float(header)
        except ValueError:
            pass
        try:
            retry_dt = parsedate_to_datetime(header)
        except (TypeError, ValueError):
            retry_dt = None
        if retry_dt is not None:
            # RFC 7231 also permits the asctime() format, which carries no
            # timezone; treat it as UTC so subtracting the aware now()
            # below cannot raise TypeError.
            if retry_dt.tzinfo is None:
                retry_dt = retry_dt.replace(tzinfo=timezone.utc)
            wait = (retry_dt - datetime.now(timezone.utc)).total_seconds()
            return max(wait, 0.0)
    try:
        body = response.json()
        if isinstance(body, dict):
            value = body.get("retry_after")
            if value is None:
                value = body.get("retryAfter")
            if isinstance(value, (int, float)):
                return float(value)
    except ValueError:
        pass
    return None


def rows_to_dict(rows) -> dict:
    """Collapse a list of settings rows to {key: value} (audit finding 2.4).

    Replaces the inline dict comprehension in email_sender.get_smtp_settings,
    email_sender.get_system_smtp_settings, and alt_text._get_settings.
    """
    return {row["key"]: row["value"] for row in rows}


# Shared exception ancestors for the five destination adapters (bluesky,
# discord, matrix, microblog, webhook — mastodon has no custom exceptions
# today). Each module's own <Platform>Error keeps subclassing Exception
# via this shared base instead, and its Auth/NotFound/RateLimit subclasses
# additionally inherit the matching Destination*Error below via multiple
# inheritance — every existing `except BlueskyAuthError` (etc.) call site
# is unaffected, since the module-specific class still exists with the
# same name and MRO position. This only adds a shared ancestor so code
# that wants to (e.g. a future generic exception ladder in scheduler.py)
# can catch "any destination's auth failure" without importing all five
# modules' independent hierarchies. Design-patterns audit finding 2.3.
class DestinationError(Exception):
    """Base for every destination-adapter failure."""


class DestinationAuthError(DestinationError):
    """Rejected credentials or insufficient permission — permanent, not worth retrying."""


class DestinationNotFoundError(DestinationError):
    """The target (channel/room/blog/webhook/instance) no longer exists."""


class DestinationRateLimitError(DestinationError):
    """Caller should back off. Subclasses set retry_after (seconds) themselves."""

    retry_after: float | None = None


class FeedItem(TypedDict, total=False):
    """The template-facing shape of one feed item, as consumed by
    render_template() and every _send_X destination function in
    scheduler.py.

    Documentation, not an enforced contract: total=False because a
    backdated/drip-redelivered item reconstructed from the feed_items
    table may carry a subset of these keys, and because feed_parser.py's
    parse_rss_feed/parse_json_feed (the source of a freshly-fetched item)
    is the only place all of them are populated at once. Design-patterns
    audit finding 4.3 — added at the scheduler.py dispatch boundary as a
    zero-runtime-cost type hint; no call site's actual dict construction
    changes.
    """

    id: str
    title: str
    link: str
    summary: str
    content: str
    content_text: str
    content_link: str
    author: str
    date: str
    tags: list[str]
    image_url: str
    image_alt: str
    image_urls: list[dict]
    enclosure_url: str
    raw: dict
