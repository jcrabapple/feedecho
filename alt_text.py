"""AI alt text generation — optional vision API integration.

Calls an OpenAI-compatible /chat/completions endpoint with a base64-encoded
image and returns a concise description. Used to auto-generate alt text for
image attachments before uploading to Mastodon.

Requires vision API settings to be configured in the Settings page:
  - alt_text_ai_enabled: "1" or "0"
  - alt_text_ai_base_url: e.g. "https://api.openai.com/v1"
  - alt_text_ai_model: e.g. "gpt-4o-mini"
  - alt_text_ai_api_key: API key

If not configured or the API call fails, returns an empty string — the image
is uploaded without alt text, which Mastodon accepts.
"""

import base64
import logging
import time

import httpx

import settings as app_settings
from database import get_db
from feed_parser import SSRFError, pinned_request, unpinned_client, validate_outbound_url
from security import decrypt_secret
from utils import rows_to_dict

logger = logging.getLogger("feedecho.alt_text")

TIMEOUT_SECONDS = 30
MAX_RETRIES = 2
RETRY_DELAY = 2
MAX_TOKENS = 300
DESCRIPTION_WORD_LIMIT = 50

SYSTEM_PROMPT = (
    "You are an alt text generator for a social media platform. "
    "Output ONLY the image description, no reasoning, no numbering, no preamble. "
    "Be concise — one or two sentences maximum."
)

# Suffix the code appends to the configured base URL. Tenants routinely paste
# a FULL endpoint (vendor docs show the complete URL — Mistral's do, and the
# 2026-09-09 glass.photo report came from exactly that), which used to produce
# a doubled path -> HTTP 404 -> silent no-alt.
_COMPLETIONS_SUFFIX = "/chat/completions"


def normalize_base_url(base_url: str) -> str:
    """Strip a pasted full endpoint down to the API root.

    ``https://api.mistral.ai/v1/chat/completions`` -> ``https://api.mistral.ai/v1``.
    Repeated suffixes collapse too. Empty input returns empty.
    """
    url = (base_url or "").strip().rstrip("/")
    while url.endswith(_COMPLETIONS_SUFFIX):
        url = url[: -len(_COMPLETIONS_SUFFIX)].rstrip("/")
    return url


USER_PROMPT = (
    f"Describe this image for alt text in one or two sentences. "
    f"Focus on the main subject and its most obvious visual features. "
    f"Keep it under {DESCRIPTION_WORD_LIMIT} words. "
    "Do NOT infer the occasion, event, or purpose. "
    "Do NOT guess relationships between people. "
    "Do NOT speculate about unseen context. "
    "Ignore partial, cropped, or illegible text. "
    "Do not interpret the meaning of any visible text."
)


def _get_settings(user_id: int = 1) -> dict[str, str]:
    """Load vision API settings from the database."""
    with get_db() as db:
        rows = db.execute(
            """SELECT key, value FROM settings
               WHERE key IN ('alt_text_ai_enabled', 'alt_text_ai_base_url',
                             'alt_text_ai_model', 'alt_text_ai_api_key')
                 AND user_id = ?""",
            (user_id,),
        ).fetchall()
    return rows_to_dict(rows)


def is_enabled(user_id: int = 1) -> bool:
    """Check if AI alt text generation is configured and enabled."""
    s = _get_settings(user_id=user_id)
    return (
        s.get("alt_text_ai_enabled") == "1"
        and bool(s.get("alt_text_ai_base_url"))
        and bool(s.get("alt_text_ai_model"))
        and bool(s.get("alt_text_ai_api_key"))
    )


def endpoint_rejection_reason(user_id: int = 1) -> str:
    """Why the configured vision endpoint would be refused, or "" if allowed.

    Only hosted (multi) mode restricts the address — see the note in
    :func:`generate_alt_text`. Lets the settings-page test button report a
    blocked address instead of the silent skip the posting path performs.
    """
    if not app_settings.MULTI:
        return ""
    cfg = _get_settings(user_id=user_id)
    base_url = normalize_base_url(cfg.get("alt_text_ai_base_url", ""))
    if not base_url:
        return ""
    try:
        validate_outbound_url(f"{base_url}/chat/completions")
    except SSRFError as e:
        return str(e)
    except ValueError as e:  # malformed URL (validate_outbound_url's own guard)
        return str(e)
    return ""


def attempt_alt_text(image_bytes: bytes, content_type: str, user_id: int = 1) -> tuple[str, str]:
    """Generate alt text, reporting WHY it failed.

    Returns ``(description, reason)``. ``description`` is "" unless a
    description was produced; ``reason`` is "" on success or when the
    feature is disabled/unconfigured, and a short human-readable string on
    any failure. Never raises.

    The posting paths use :func:`generate_alt_text` and only care about the
    description; the settings-page Test button surfaces the reason so a
    misconfigured endpoint reports failure instead of a false green.
    """
    # Named cfg, not settings: the module-level `settings` module is needed
    # below, and shadowing it here would turn app_settings.MULTI into a dict
    # lookup.
    cfg = _get_settings(user_id=user_id)
    if cfg.get("alt_text_ai_enabled") != "1":
        return "", ""

    base_url = normalize_base_url(cfg.get("alt_text_ai_base_url", ""))
    model = cfg.get("alt_text_ai_model", "")
    api_key = decrypt_secret(cfg.get("alt_text_ai_api_key", ""))

    if not (base_url and model and api_key):
        return "", ""

    b64 = base64.b64encode(image_bytes).decode("ascii")
    data_url = f"data:{content_type};base64,{b64}"

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": USER_PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ],
        "max_tokens": MAX_TOKENS,
    }

    endpoint = f"{base_url}/chat/completions"
    # Hosted mode only: the base URL is tenant-supplied there, so without the
    # guard a tenant can aim the vision call at cloud metadata or an internal
    # service and have the server issue the request for them.
    #
    # Deliberately NOT applied in single mode. Self-hosters legitimately point
    # this at a LAN or loopback vision server (Ollama, llama.cpp, LocalAI),
    # which validate_outbound_url blocks by design, and single-mode behaviour
    # must stay unchanged. In single mode the operator owns both the app and
    # the address, so there is no privilege boundary to cross.
    if app_settings.MULTI:
        try:
            validate_outbound_url(endpoint)
        except SSRFError as e:
            logger.warning("Alt text base URL rejected: %s", e)
            return "", str(e)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if app_settings.MULTI:
                # Hosted: the endpoint is tenant-supplied. validate_outbound_url
                # already refused internal addresses above; pinned_request dials
                # the exact IP it approved (no DNS re-resolution window).
                response = pinned_request(
                    "POST",
                    endpoint,
                    timeout=TIMEOUT_SECONDS,
                    headers=headers,
                    json=body,
                )
            else:
                # Single mode: the operator owns both the app and the endpoint
                # address (often a LAN vision server), so there is no privilege
                # boundary to cross — keep an unpinned client, matching the
                # MULTI-gated validate_outbound_url check above.
                with unpinned_client(timeout=TIMEOUT_SECONDS) as client:
                    response = client.post(endpoint, headers=headers, json=body)
            response.raise_for_status()
            parsed = response.json()

            # Defensive unpacking: "never raises" is part of this
            # function's contract, and OpenAI-compatible endpoints do
            # return empty choices lists and null messages (content
            # filters, proxies). Indexing [{}] only helps when the key
            # is absent, not when it is an empty list.
            choices = parsed.get("choices") if isinstance(parsed, dict) else None
            if not isinstance(choices, list) or not choices:
                return "", "API returned no choices (content filter or incompatible endpoint)"
            message = choices[0].get("message") if isinstance(choices[0], dict) else None
            if not isinstance(message, dict):
                return "", "API response had no message object"
            content = message.get("content") or message.get("reasoning_content")
            if not isinstance(content, str) or not content.strip():
                return "", "API returned an empty description"
            return content.strip(), ""
        except (
            httpx.HTTPStatusError,
            httpx.RequestError,
            KeyError,
            ValueError,
            IndexError,
            AttributeError,
        ) as e:
            # A permanent client error (bad API key, malformed request, wrong
            # endpoint path) fails identically on every retry — burning the
            # retry budget on it just delays the empty-string fallback for no
            # benefit. e.response can be None here (some callers construct
            # HTTPStatusError without one), so only special-case when a real
            # status code is available; anything else falls through to the
            # normal retry path unchanged. Error-handling audit finding 4.3
            # (docs/reviews/2026-09-08-error-handling-audit.md).
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (400, 401, 403, 404):
                logger.warning(
                    "Alt text API call failed permanently (HTTP %s), not retrying: %s",
                    status, e,
                )
                hint = {
                    401: "API key rejected",
                    403: "API key lacks access to this model",
                    404: (
                        "endpoint not found — the base URL is likely wrong "
                        "(use the API root, e.g. https://api.mistral.ai/v1)"
                    ),
                }.get(status, "request rejected")
                return "", f"HTTP {status}: {hint}"
            logger.warning(
                "Alt text API call failed (attempt %d/%d): %s",
                attempt,
                MAX_RETRIES,
                e,
            )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * attempt)

    return "", "API unreachable after retries"


def generate_alt_text(image_bytes: bytes, content_type: str, user_id: int = 1) -> str:
    """Generate alt text for an image via a vision API.

    Returns the description string, or "" if disabled, unconfigured,
    or the API call fails. Never raises — alt text is best-effort.
    """
    description, _reason = attempt_alt_text(image_bytes, content_type, user_id=user_id)
    return description
