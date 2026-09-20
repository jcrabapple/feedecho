"""Telegram Bot API client — posts feed items into Telegram chats and channels.

FeedEcho connects with a **bot token** (created via @BotFather) plus a **chat
ID** — a numeric id (negative for groups/channels) or a ``@publicname``. The
bot must be a member of the target chat before connecting; the connect-time
verification calls ``getMe`` (token) and ``getChat`` (chat + membership) so a
working pair is proven without posting anything.

Sending uses the Bot API's ``sendMessage`` / ``sendPhoto``:

- Plain templates (no ``{{ content_html }}``) send WITHOUT ``parse_mode``.
  Telegram auto-detects bare URLs and ``#hashtags`` in plain text, and raw
  prose (``<3``, ``a & b``) can never trip entity parsing.
- Templates embedding ``{{ content_html }}`` send WITH ``parse_mode=HTML``:
  the sanitized HTML is reduced to Telegram's supported subset (``a`` with
  href, ``b/strong``, ``i/em``, ``u``, ``s/del``, ``code``, ``pre``,
  ``blockquote``); everything else (``img``, headings, tables, lists) is
  dropped but its text survives, so Telegram's strict entity parser never
  sees an unsupported tag.

Image attachments (``attach_image`` echoes) send as ``sendPhoto`` with the
message text as the caption (Telegram's 1024-char caption cap); a failed
image fetch degrades to a text-only message, matching the other senders.

Rate limits: Telegram allows ~30 messages/second globally and 1 message/
second per chat (429 with ``retry_after`` beyond that). 429s are transient
and ride the scheduler's bounded retry pipeline. The bot token lives in the
URL path, so it is treated like the Discord webhook token: never rendered,
never interpolated into error messages.
"""

from __future__ import annotations

import logging
import re
from html.parser import HTMLParser

import httpx

from feed_parser import SSRFError, pinned_request
from utils import (
    DEFAULT_REQUEST_TIMEOUT,
    DestinationAuthError,
    DestinationError,
    DestinationNotFoundError,
    DestinationRateLimitError,
    json_error_detail,
    parse_retry_after,
    truncate_chars,
)

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = DEFAULT_REQUEST_TIMEOUT

API_BASE = "https://api.telegram.org"

# Telegram message text limit; captions (sendPhoto) are shorter.
MAX_MESSAGE_CHARS = 4096
MAX_CAPTION_CHARS = 1024

# Bot tokens are "<bot id>:<opaque secret>". Loose bounds on purpose: the
# getMe call below is the real check. Chat ids: numeric (negative for
# groups/channels/supergroups) or @publicname.
_TOKEN_RE = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{30,80}$")
_CHAT_ID_RE = re.compile(r"^(@[A-Za-z0-9_]{4,64}|-?[1-9]\d{0,14})$")

# Telegram's Bot API HTML subset. Anything outside it must be reduced to
# its text or sendMessage 400s ("unsupported start tag") and the post dies.
_HTML_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
              "a", "code", "pre", "blockquote"}


class TelegramError(DestinationError):
    """Base error for Telegram Bot API interactions."""


class TelegramRateLimitError(TelegramError, DestinationRateLimitError):
    """Telegram 429. Carries the suggested wait in seconds."""

    def __init__(self, retry_after: float | None = None):
        self.retry_after = retry_after
        msg = "Telegram rate limit hit"
        if retry_after is not None:
            msg += f" (retry after {retry_after:.0f}s)"
        super().__init__(msg)


class TelegramAuthError(TelegramError, DestinationAuthError):
    """Bot token rejected (401)."""


class TelegramNotFoundError(TelegramError, DestinationNotFoundError):
    """Chat not found (the bot is not a member, or the chat was deleted)."""


class TelegramBadRequestError(TelegramError):
    """Telegram refused the payload (400) — a formatting bug, not retryable."""


def _error_detail(response) -> str:
    # Telegram errors are {"ok": false, "description": "..."}.
    return json_error_detail(response, "description")


def _raise_for_status(response, action: str) -> None:
    if response.status_code == 200:
        return
    detail = _error_detail(response)
    if response.status_code == 429:
        raise TelegramRateLimitError(parse_retry_after(response))
    if response.status_code == 401:
        raise TelegramAuthError(
            "Telegram rejected this bot token (401)."
            " Check the token from @BotFather and connect again."
        )
    if response.status_code == 400 and "chat not found" in detail.lower():
        raise TelegramNotFoundError(
            "Telegram could not find that chat. Add the bot to the chat"
            " (and, for channels, make it an admin) before connecting."
        )
    if response.status_code == 404:
        raise TelegramNotFoundError("Telegram API endpoint not found.")
    if response.status_code == 400:
        raise TelegramBadRequestError(
            f"Telegram refused the {action} (HTTP 400)"
            + (f": {detail}" if detail else "")
        )
    raise TelegramError(
        f"Telegram {action} failed (HTTP {response.status_code})"
        + (f": {detail}" if detail else "")
    )


def _api_url(token: str, method: str) -> str:
    # Callers must never interpolate this into logs or user-facing errors —
    # the token lives in the path.
    return f"{API_BASE}/bot{token}/{method}"


def _post_json(token: str, method: str, payload: dict) -> dict:
    try:
        resp = pinned_request(
            "POST", _api_url(token, method), timeout=REQUEST_TIMEOUT,
            json=payload,
        )
    except (httpx.HTTPError, SSRFError) as e:
        # httpx exception text embeds the request URL, which carries the
        # bot token — never interpolate it into logs or user messages.
        raise TelegramError(f"Could not reach Telegram ({type(e).__name__})") from e
    _raise_for_status(resp, method)
    try:
        data = resp.json()
    except ValueError as e:
        raise TelegramError("Telegram returned a non-JSON response") from e
    if not isinstance(data, dict) or data.get("ok") is not True:
        raise TelegramError("Telegram returned an unexpected response")
    result = data.get("result")
    if not isinstance(result, dict):
        raise TelegramError("Telegram returned no result object")
    return result


def normalize_bot_token(raw: str) -> str:
    """Validate a pasted bot token. Raises ValueError with a user message."""
    value = (raw or "").strip()
    if not value:
        raise ValueError("Paste the bot token from @BotFather.")
    if not _TOKEN_RE.match(value):
        raise ValueError(
            "That does not look like a Telegram bot token (it should look"
            " like 123456789:AAE...). In Telegram, open @BotFather and use"
            " /mybots → your bot → API Token to see it."
        )
    return value


def normalize_chat_id(raw: str) -> str:
    """Validate a chat id: numeric (may be negative) or @publicname."""
    value = (raw or "").strip()
    if not value:
        raise ValueError("Paste the chat ID or @channel username.")
    if not _CHAT_ID_RE.match(value):
        raise ValueError(
            "That does not look like a Telegram chat ID. Use the numeric ID"
            " (negative for groups/channels) or the @publicname of the channel."
        )
    return value


def get_me(token: str) -> dict:
    """GET getMe — proves the token. Returns ``{name}`` (bot first name)."""
    result = _post_json(token, "getMe", {})
    name = result.get("first_name") or result.get("username") or ""
    return {"name": str(name)}


def get_chat(token: str, chat_id: str) -> dict:
    """GET getChat — proves the bot can see the target chat.

    Returns ``{chat_title, chat_username}`` (either may be empty for
    private chats).
    """
    result = _post_json(token, "getChat", {"chat_id": chat_id})
    title = result.get("title") or ""
    username = result.get("username") or ""
    return {"chat_title": str(title), "chat_username": str(username)}


def connect(raw_token: str, raw_chat_id: str) -> dict:
    """Verify a token/chat pair and return everything needed to store it.

    Raises ValueError for malformed input, TelegramAuthError for a bad
    token, TelegramNotFoundError for a chat the bot cannot see, and
    TelegramError for network/API failures.
    """
    token = normalize_bot_token(raw_token)
    chat_id = normalize_chat_id(raw_chat_id)
    me = get_me(token)
    chat = get_chat(token, chat_id)
    label = chat["chat_title"] or chat["chat_username"] or chat_id
    return {
        "bot_token": token,
        "chat_id": chat_id,
        "name": f"{me['name']} → {label}"[:200],
        **chat,
    }


class _TelegramHTMLReducer(HTMLParser):
    """Reduce sanitized HTML to Telegram's supported tag subset.

    Allowed tags pass through (``a`` keeps only its href); any other tag is
    dropped but its text survives, so ``img``, headings, tables, and lists
    from content_html degrade to readable text instead of a 400.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = (dict(attrs).get("href") or "")
            # Only http(s) hrefs; anything else degrades to plain text.
            if href.startswith(("http://", "https://")):
                self.out.append(f'<a href="{href}">')
            else:
                self.out.append("<a>")
        elif tag in ("b", "strong"):
            self.out.append("<b>")
        elif tag in ("i", "em"):
            self.out.append("<i>")
        elif tag in ("s", "strike", "del"):
            self.out.append("<s>")
        elif tag in ("u", "ins"):
            self.out.append("<u>")
        elif tag in _HTML_TAGS:
            self.out.append(f"<{tag}>")
        # Anything else (img, p, div, h1-6, table, ...) is dropped.

    def handle_endtag(self, tag):
        if tag == "a":
            self.out.append("</a>")
        elif tag in ("b", "strong"):
            self.out.append("</b>")
        elif tag in ("i", "em"):
            self.out.append("</i>")
        elif tag in ("s", "strike", "del"):
            self.out.append("</s>")
        elif tag in ("u", "ins"):
            self.out.append("</u>")
        elif tag in _HTML_TAGS:
            self.out.append(f"</{tag}>")

    def handle_data(self, data):
        self.out.append(data)


def _reduce_html(html_str: str) -> str:
    parser = _TelegramHTMLReducer()
    parser.feed(html_str)
    parser.close()
    return "".join(parser.out)


def prepare_text(rendered: str, rich: bool = False) -> str:
    """Prepare a rendered template for sendMessage.

    rich=True (template embeds content_html): reduce the sanitized HTML to
    Telegram's tag subset and send as parse_mode=HTML. rich=False: send the
    text with no parse_mode — Telegram auto-links URLs and hashtags and raw
    prose can never break entity parsing. Truncated to the message cap.
    """
    text = _reduce_html(rendered or "") if rich else (rendered or "")
    return truncate_chars(text, MAX_MESSAGE_CHARS)


def prepare_caption(rendered: str, rich: bool = False) -> str:
    """Same as prepare_text but under the sendPhoto caption cap."""
    text = _reduce_html(rendered or "") if rich else (rendered or "")
    return truncate_chars(text, MAX_CAPTION_CHARS)


def message_url(result: dict) -> str:
    """Public t.me link for posts into public channels, else ''."""
    chat = result.get("chat") or {}
    message_id = result.get("message_id")
    username = chat.get("username") if isinstance(chat, dict) else None
    if username and isinstance(message_id, int):
        return f"https://t.me/{username}/{message_id}"
    return ""


def send_message(token: str, chat_id: str, text: str, parse_mode: str | None) -> dict:
    """POST one message. Returns the message ``result`` object."""
    if not (text or "").strip():
        # Permanent: retries cannot turn an empty render into a message.
        raise TelegramBadRequestError("Cannot send an empty message to Telegram")
    payload: dict = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    return _post_json(token, "sendMessage", payload)


def send_photo(
    token: str, chat_id: str, photo_bytes: bytes, mime: str, caption: str,
    parse_mode: str | None,
) -> dict:
    """POST one photo with a caption via multipart upload."""
    if not photo_bytes:
        raise TelegramBadRequestError("Cannot send an empty photo to Telegram")
    data: dict = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption
    if parse_mode:
        data["parse_mode"] = parse_mode
    try:
        resp = pinned_request(
            "POST", _api_url(token, "sendPhoto"), timeout=REQUEST_TIMEOUT,
            data=data,
            files={"photo": ("image", photo_bytes, mime)},
        )
    except (httpx.HTTPError, SSRFError) as e:
        raise TelegramError(f"Could not reach Telegram ({type(e).__name__})") from e
    _raise_for_status(resp, "photo send")
    try:
        body = resp.json()
    except ValueError as e:
        raise TelegramError("Telegram returned a non-JSON response") from e
    if not isinstance(body, dict) or body.get("ok") is not True:
        raise TelegramError("Telegram returned an unexpected photo response")
    result = body.get("result")
    if not isinstance(result, dict):
        raise TelegramError("Telegram returned no photo result object")
    return result


def test_connection(token: str, chat_id: str) -> tuple[bool, str]:
    """Verify a stored account still works. Backs the accounts Test button.

    Deliberately read-only (getMe + getChat, no test message into the chat),
    matching the other destination test buttons.
    """
    try:
        me = get_me(token)
        chat = get_chat(token, chat_id)
    except TelegramAuthError as e:
        return False, str(e)
    except TelegramNotFoundError as e:
        return False, str(e)
    except TelegramError as e:
        return False, str(e)
    where = chat["chat_title"] or chat["chat_username"] or chat_id
    return True, f"Bot OK — posts as {me['name']} into {where}"
