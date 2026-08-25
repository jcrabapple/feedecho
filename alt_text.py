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
from feed_parser import SSRFError, validate_outbound_url

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
    return {row["key"]: row["value"] for row in rows}


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
    base_url = cfg.get("alt_text_ai_base_url", "").rstrip("/")
    if not base_url:
        return ""
    try:
        validate_outbound_url(f"{base_url}/chat/completions")
    except SSRFError as e:
        return str(e)
    except ValueError as e:  # malformed URL (validate_outbound_url's own guard)
        return str(e)
    return ""


def generate_alt_text(image_bytes: bytes, content_type: str, user_id: int = 1) -> str:
    """Generate alt text for an image via a vision API.

    Returns the description string, or "" if disabled, unconfigured,
    or the API call fails. Never raises — alt text is best-effort.
    """
    # Named cfg, not settings: the module-level `settings` module is needed
    # below, and shadowing it here would turn app_settings.MULTI into a dict
    # lookup.
    cfg = _get_settings(user_id=user_id)
    if cfg.get("alt_text_ai_enabled") != "1":
        return ""

    base_url = cfg.get("alt_text_ai_base_url", "").rstrip("/")
    model = cfg.get("alt_text_ai_model", "")
    api_key = cfg.get("alt_text_ai_api_key", "")

    if not (base_url and model and api_key):
        return ""

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
            return ""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
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
                    return ""
                message = choices[0].get("message") if isinstance(choices[0], dict) else None
                if not isinstance(message, dict):
                    return ""
                content = message.get("content") or message.get("reasoning_content")
                return content.strip() if isinstance(content, str) else ""
        except (
            httpx.HTTPStatusError,
            httpx.RequestError,
            KeyError,
            ValueError,
            IndexError,
            AttributeError,
        ) as e:
            logger.warning(
                "Alt text API call failed (attempt %d/%d): %s",
                attempt,
                MAX_RETRIES,
                e,
            )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)

    return ""
