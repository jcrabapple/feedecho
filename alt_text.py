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

from database import get_db

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


def generate_alt_text(image_bytes: bytes, content_type: str) -> str:
    """Generate alt text for an image via a vision API.

    Returns the description string, or "" if disabled, unconfigured,
    or the API call fails. Never raises — alt text is best-effort.
    """
    settings = _get_settings()
    if settings.get("alt_text_ai_enabled") != "1":
        return ""

    base_url = settings.get("alt_text_ai_base_url", "").rstrip("/")
    model = settings.get("alt_text_ai_model", "")
    api_key = settings.get("alt_text_ai_api_key", "")

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

                content = (
                    parsed.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content")
                )
                if not content:
                    # Some models return content in reasoning_content
                    content = (
                        parsed.get("choices", [{}])[0]
                        .get("message", {})
                        .get("reasoning_content")
                    )
                if content:
                    return content.strip()
                return ""
        except (httpx.HTTPStatusError, httpx.RequestError, KeyError, ValueError) as e:
            logger.warning(
                "Alt text API call failed (attempt %d/%d): %s",
                attempt,
                MAX_RETRIES,
                e,
            )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)

    return ""
