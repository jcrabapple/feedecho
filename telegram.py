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

import html as _html
import logging
import re
from html.parser import HTMLParser

import httpx

from feed_parser import SSRFError, pinned_request, html_to_text
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
    if response.status_code == 404:
        # Telegram 404s on /bot<token>/... when the token is not recognized
        # (our method names are fixed, so the URL can't otherwise 404).
        raise TelegramAuthError(
            "Telegram does not recognize this bot token (404)."
            " Check the token from @BotFather and connect again."
        )
    if response.status_code == 400 and "chat not found" in detail.lower():
        raise TelegramNotFoundError(
            "Telegram could not find that chat. Add the bot to the chat"
            " (and, for channels, make it an admin) before connecting."
        )
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
        # bot token — raise with the cause detached (`from None`) so no
        # traceback formatter can ever surface the request URL.
        raise TelegramError(
            f"Could not reach Telegram ({type(e).__name__})"
        ) from None
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

    Returns ``{chat_id, chat_title, chat_username}`` where ``chat_id`` is
    Telegram's canonical numeric id (as a string). Storing the canonical id
    keeps one row per chat even when the user connects via ``@publicname``
    and the username later changes.
    """
    result = _post_json(token, "getChat", {"chat_id": chat_id})
    title = result.get("title") or ""
    username = result.get("username") or ""
    return {
        "chat_id": str(result.get("id") or chat_id),
        "chat_title": str(title),
        "chat_username": str(username),
    }


def connect(raw_token: str, raw_chat_id: str) -> dict:
    """Verify a token/chat pair and return everything needed to store it.

    The stored ``chat_id`` is Telegram's canonical numeric id, so the same
    chat connected via ``@publicname`` and via its number is one row, and a
    later channel rename cannot break delivery.

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
        "chat_id": chat["chat_id"],
        "name": f"{me['name']} → {label}"[:200],
        **chat,
    }


class _TelegramHTMLReducer(HTMLParser):
    """Reduce sanitized HTML to Telegram's supported tag subset.

    ``convert_charrefs`` stays ON (entities decode once here) and every text
    node is re-escaped with ``html.escape``: Telegram's parser requires raw
    ``&``, ``<``, ``>`` OUTSIDE supported tags to be escaped, and feed text
    like "Rock & Roll" or "Q&A" is everywhere. Without the re-escape, any
    ampersand in an article would 400 the whole send as a permanent failure.

    Allowed tags pass through (``a`` keeps only its href, re-escaped); any
    other tag is dropped but its text survives, so ``img``, headings,
    tables, and lists from content_html degrade to readable text instead of
    a 400. Block boundaries emit newlines so paragraphs don't mash together.
    An ``<a>`` whose href is not http(s) emits NO tag at all — Telegram
    400s on ``<a>`` without an href — the text just degrades to plain.
    """

    _BLOCK = {
        "p", "div", "section", "article", "h1", "h2", "h3", "h4", "h5",
        "h6", "li", "ul", "ol", "table", "tr", "figure", "figcaption",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        # Whether an <a> open tag was actually emitted (href http(s)) — its
        # matching </a> may only be emitted when the opening tag was.
        self._a_open = False

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = (dict(attrs).get("href") or "").strip()
            # Only http(s) hrefs; anything else degrades to plain text.
            if href.startswith(("http://", "https://")):
                self.out.append(f'<a href="{_html.escape(href, quote=True)}">')
                self._a_open = True
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
        elif tag == "br":
            self.out.append("\n")
        elif tag in self._BLOCK:
            # Unsupported block container: keep the text, add the break so
            # paragraphs/lists don't mash into one word-salad line.
            self.out.append("\n")
        # Anything else (img, ...) is dropped entirely.

    def handle_endtag(self, tag):
        if tag == "a":
            if self._a_open:
                self.out.append("</a>")
                self._a_open = False
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
        elif tag in self._BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        self.out.append(_html.escape(data, quote=False))


def _reduce_html(html_str: str) -> str:
    parser = _TelegramHTMLReducer()
    parser.feed(html_str)
    parser.close()
    return "".join(parser.out)


def build_message(rendered: str, rich: bool, cap: int) -> tuple[str, str | None]:
    """Prepare a rendered template for one Telegram send.

    Returns ``(text, parse_mode)``.

    rich=True (template embeds content_html): reduce the sanitized HTML to
    Telegram's tag subset and send as parse_mode=HTML — but only if the
    reduced text fits the cap. If it doesn't, fall back to plain text
    (``html_to_text``, then truncate): naive slicing of HTML can sever a
    tag mid-flight, and Telegram 400s permanently on unclosed tags. Plain
    text needs no parse mode and Telegram auto-links URLs and hashtags.

    rich=False: send the raw rendered text with no parse_mode — raw prose
    ("Rock & Roll", "<3") can never break entity parsing.
    """
    if rich:
        html_text = _reduce_html(rendered or "")
        if len(html_text) <= cap:
            return html_text, "HTML"
        return truncate_chars(html_to_text(rendered or ""), cap), None
    return truncate_chars(rendered or "", cap), None


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
        # parse_mode only matters when there IS a caption to parse.
        if parse_mode:
            data["parse_mode"] = parse_mode
    try:
        resp = pinned_request(
            "POST", _api_url(token, "sendPhoto"), timeout=REQUEST_TIMEOUT,
            data=data,
            files={"photo": ("image", photo_bytes, mime)},
        )
    except (httpx.HTTPError, SSRFError) as e:
        # See _post_json: the request URL carries the bot token.
        raise TelegramError(
            f"Could not reach Telegram ({type(e).__name__})"
        ) from None
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
