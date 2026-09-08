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
    string wins, stripped and capped at 200 characters. Returns "" when
    the body is not JSON, not a dict, or carries none of the keys.
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

    The Retry-After header wins when present, falling back to
    retry_after/retryAfter in a JSON body. The former discord.py copy
    checked the body first, but Discord sends matching values in header
    and body on 429, so the unified order changes no real response.
    """
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