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
import re
import time
from urllib.parse import urlsplit

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
# Reasoning models (OpenAI's gpt-5.x / gpt-6 family, and anything else that
# spends output budget on hidden reasoning) can burn the whole cap before
# writing a word of description, so the retry that switches to
# `max_completion_tokens` needs a budget with room for the reasoning.
REASONING_MAX_TOKENS = 1200
# How much of a provider's own error message to quote back to the tenant.
MAX_REASON_DETAIL = 300

DESCRIPTION_WORD_LIMIT = 50

# Probe image for the settings-page Test button: 96x96, white canvas with a
# solid red circle. It replaced a 1x1 PNG because current vision models refuse
# to acknowledge a 1x1 ("No image is attached", OpenAI gpt-6-astra on
# 2026-11-06), which made the button green while proving nothing. 330 bytes,
# generated with stdlib zlib so no asset file has to ship or be kept in sync.
TEST_IMAGE_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAGAAAABgCAIAAABt+uBvAAABEUlEQVR42u3cuxUDIQwEQFXi2P1X"
    "4a5w6ugSfpI8+64AmAzQXgx5TCAABAgQIECAAAESQIAAAQIECBAgyQT0eb0fvj8FekZJhRX5Xe5K"
    "RS2a80xRkeYkU5TWOWAUpWkOMEUPnX1G0UZnk1F00tlhFM10lhtFP521RoCOAGXTWWgUXXVWGQHa"
    "DJRZZ4kRoJ1A+XXmjQABugVURWfSCBAgQBmBaunMGAECBAgQIECAAAEC5KgBCBCgRkCuXAEB8nDo"
    "ZTU9kOEFQAaojOABqgA0jAFXMVq7I1WE40BDmSWn0aaNKNRdBRoqmReZzqxcLTwT0KTUlaX6NUV6"
    "oOQBBAgQIECAAAECJIAAAQIECBAgQPKTL6GYPNfMN32dAAAAAElFTkSuQmCC"
)

# Phrases a vision model uses when it did not receive the image. Matched
# case-insensitively against short answers only (see _claims_no_image).
_NO_IMAGE_PATTERNS = (
    re.compile(r"\bno image (?:is|was) (?:attached|visible|provided|included|received)\b"),
    re.compile(r"\bno image attached\b"),
    re.compile(r"\bthere is no image\b"),
    re.compile(r"\b(?:i )?(?:can(?:no|')t|could not|couldn't|do not|don't) see (?:an|any) image\b"),
    re.compile(r"\bplease (?:upload|attach|provide) (?:the|an) image\b"),
)

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
    raw = (base_url or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
        if parts.scheme and parts.netloc:
            path = parts.path.rstrip("/")
            while path.endswith(_COMPLETIONS_SUFFIX):
                path = path[: -len(_COMPLETIONS_SUFFIX)].rstrip("/")
            return parts._replace(path=path).geturl().rstrip("/")
    except Exception:
        pass
    url = raw.rstrip("/")
    while url.endswith(_COMPLETIONS_SUFFIX):
        url = url[: -len(_COMPLETIONS_SUFFIX)].rstrip("/")
    return url


def _upstream_error_message(exc: Exception) -> str:
    """The provider's own error sentence from a failed response, or "".

    The settings-page Test button used to report a blanket "request rejected"
    for every 4xx, which hid the one sentence that explains the failure
    (OpenAI's "Unsupported parameter: 'max_tokens' …" is the case that made a
    paid tenant's correct-looking config look like a network problem).

    Not every provider speaks OpenAI's envelope: FastAPI-based gateways
    (vLLM, LocalAI wrappers) return ``{"detail": ...}`` and REST gateways
    often return a top-level ``{"message": ...}``, so those shapes count too.
    An HTML error page from a proxy is never a useful detail: returning ""
    lets the caller fall back to its tailored hint instead of printing
    truncated markup at the tenant.
    """
    response = getattr(exc, "response", None)
    if response is None:
        return ""
    try:
        payload = response.json()
    except Exception:
        try:
            text = (response.text or "").strip()
        except Exception:
            return ""
        if text.startswith("<"):
            return ""
        return text[:MAX_REASON_DETAIL]
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str):
            return message.strip()[:MAX_REASON_DETAIL]
    elif isinstance(error, str):
        return error.strip()[:MAX_REASON_DETAIL]
    detail = payload.get("detail")
    if isinstance(detail, str):
        return detail.strip()[:MAX_REASON_DETAIL]
    if isinstance(detail, list):
        # FastAPI validation errors: [{"loc": [...], "msg": "..."}, ...]
        parts = [
            item["msg"].strip()
            for item in detail
            if isinstance(item, dict) and isinstance(item.get("msg"), str)
        ]
        if parts:
            return "; ".join(parts)[:MAX_REASON_DETAIL]
    message = payload.get("message")
    if isinstance(message, str):
        return message.strip()[:MAX_REASON_DETAIL]
    return ""



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


def _claims_no_image(text: str) -> bool:
    """True when the model answered that it received no image.

    A vision endpoint can return HTTP 200 and a fluent sentence that means
    "I could not see anything" (OpenAI gpt-6-astra on a 1x1 probe:
    "No image is attached. Please upload the image..."). That is a failed
    vision call, not alt text: posting it would put a false statement under
    someone's photo. Kept short-text only so a real description that happens
    to discuss images is not caught.
    """
    if len(text) > 200:
        return False
    lowered = text.lower()
    return any(pattern.search(lowered) for pattern in _NO_IMAGE_PATTERNS)


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
        and bool(normalize_base_url(s.get("alt_text_ai_base_url", "")))
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

    # The output cap goes out under the legacy `max_tokens` name. OpenAI's
    # current reasoning models (gpt-5.x / gpt-6 family) reject that name
    # outright — HTTP 400 `unsupported_parameter`, "Use
    # 'max_completion_tokens' instead" — while every OpenAI-compatible server
    # in the wild (Ollama, llama.cpp, LocalAI, Mistral) still speaks it. So
    # send the legacy name and switch ONCE if the endpoint's own 400 names the
    # parameter: a model-name prefix list would be wrong the day the next
    # family ships, and this call runs for every image on every echo.
    token_param = "max_tokens"

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
        token_param: MAX_TOKENS,
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
        except (SSRFError, ValueError) as e:
            logger.warning("Alt text base URL rejected: %s", e)
            return "", str(e)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # attempts counts real transport attempts only. The parameter switch below
    # is protocol negotiation, not a failed attempt, so it hands its attempt
    # back (a switch that consumed the last retry would exit the loop having
    # never sent the corrected request).
    attempts = 0
    token_switched = False
    while attempts < MAX_RETRIES:
        attempts += 1
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
            raw_content = message.get("content")
            finish_reason = (
                choices[0].get("finish_reason") if isinstance(choices[0], dict) else None
            )
            if (
                (not isinstance(raw_content, str) or not raw_content.strip())
                and finish_reason == "length"
            ):
                # A reasoning model whose hidden tokens ate the whole output
                # cap returns empty content with finish_reason=length; saying
                # "empty description" sends the tenant hunting for a bug in
                # their config instead of the token budget. Checked BEFORE the
                # reasoning_content fallback: models that expose their scratch
                # reasoning (DeepSeek-R1 and friends) would otherwise post a
                # truncated chain of thought as alt text.
                return "", (
                    "API ran out of output tokens before writing a description "
                    "(reasoning models spend the cap on reasoning first)"
                )
            content = raw_content or message.get("reasoning_content")
            if not isinstance(content, str) or not content.strip():
                return "", "API returned an empty description"
            description = content.strip()
            if _claims_no_image(description):
                # HTTP 200 with "No image is attached" is a failed vision call.
                # Returning it as alt text would caption a real photo with a
                # claim that there is no image.
                return "", "API said it received no image (the image did not reach the model)"
            return description, ""
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
            detail = _upstream_error_message(e)
            # Current OpenAI reasoning models reject the legacy `max_tokens`
            # parameter name. FeedEcho chose the parameter, not the tenant, so
            # switch it once and try again instead of failing a config that is
            # otherwise correct. Guarded by the endpoint's own message: an
            # endpoint that does not name the parameter keeps its normal retry
            # path.
            if (
                status == 400
                and not token_switched
                and "max_tokens" in detail
                and "max_completion_tokens" in detail
            ):
                logger.info(
                    "Alt text endpoint rejected max_tokens; retrying with "
                    "max_completion_tokens: %s",
                    detail,
                )
                token_switched = True
                body.pop(token_param, None)
                token_param = "max_completion_tokens"
                body[token_param] = REASONING_MAX_TOKENS
                attempts -= 1  # negotiation, not a transport attempt
                continue
            # Every other client error is permanent: a 422 (bad image), a 413
            # (too large) or a 415 (unsupported media type) fails identically
            # on retry, and retrying it replaces the provider's explanation
            # with "API unreachable". 408 and 429 are the client-side 4xx that
            # DO clear on their own.
            if status is not None and 400 <= status < 500 and status not in (408, 429):
                logger.warning(
                    "Alt text API call failed permanently (HTTP %s), not retrying: %s%s",
                    status,
                    e,
                    f" ({detail})" if detail else "",
                )
                hint = {
                    401: "API key rejected",
                    403: "API key lacks access to this model",
                    404: (
                        "endpoint not found — the base URL is likely wrong "
                        "(use the API root, e.g. https://api.mistral.ai/v1)"
                    ),
                }.get(status, "request rejected")
                return "", f"HTTP {status}: {detail or hint}"
            logger.warning(
                "Alt text API call failed (attempt %d/%d): %s",
                attempts,
                MAX_RETRIES,
                e,
            )
            if attempts < MAX_RETRIES:
                time.sleep(RETRY_DELAY * attempts)

    return "", "API unreachable after retries"


def generate_alt_text(image_bytes: bytes, content_type: str, user_id: int = 1) -> str:
    """Generate alt text for an image via a vision API.

    Returns the description string, or "" if disabled, unconfigured,
    or the API call fails. Never raises — alt text is best-effort.
    """
    description, _reason = attempt_alt_text(image_bytes, content_type, user_id=user_id)
    return description
