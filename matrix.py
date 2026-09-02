"""Matrix client — posts feed items into Matrix rooms via the Client-Server API.

FeedEcho connects to Matrix with an **access token** (the standard method for
bots: log the bot user in once with any client, or use a registration/appservice
token, and paste the token here). A token can send messages as its user but
FeedEcho only ever calls the endpoints below.

Flow at connect time (``connect``):

1. ``discover_base_url`` follows ``/.well-known/matrix/client`` so a server
   name that delegates its client API (``example.com`` -> ``matrix.example.com``)
   resolves to the real API base. The resolved base is stored per account, so
   posting never repeats the discovery round trip.
2. ``whoami`` proves the token works and tells us which user we post as.
3. ``resolve_room`` turns a ``#alias:server`` into the canonical ``!id:server``
   (room IDs are what the send endpoint takes; aliases can be repointed).
4. ``joined_rooms`` confirms the bot is actually in the room. FeedEcho never
   auto-joins: joining is a visible action on the user's account, and a private
   room would reject it anyway. A clear "invite it first" error is more useful
   than a surprise membership.

Sending is ``PUT /rooms/{roomId}/send/m.room.message/{txnId}``. The transaction
ID is derived from the echo and feed item, not random, so a retry after a
timeout that the homeserver actually processed is de-duplicated server-side
instead of double-posting.

Images are uploaded to the homeserver's media repo and sent as a second
``m.image`` event (Matrix has no single event carrying text + image), with alt
text in ``body``. Any image failure degrades to text-only, matching the
Mastodon/Bluesky/micro.blog behaviour.
"""

from __future__ import annotations

import hashlib
import html
import logging
import re
from urllib.parse import quote

import httpx

from feed_parser import SSRFError, pinned_request, validate_outbound_url

logger = logging.getLogger(__name__)

CLIENT_API = "/_matrix/client/v3"
MEDIA_API = "/_matrix/media/v3"
WELL_KNOWN_PATH = "/.well-known/matrix/client"
MESSAGE_EVENT_TYPE = "m.room.message"
REQUEST_TIMEOUT = 30
WELL_KNOWN_TIMEOUT = 10

# Homeservers cap uploads (Synapse's default max_upload_size is 50M, but the
# feed images we forward are small). Keep our own ceiling so a huge image is
# rejected locally instead of after a slow upload.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MATRIX_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/avif"}

# Matrix has no hard body limit, but events over ~64 KiB are rejected by the
# spec's event size limit (which counts the whole PDU). Truncate well below it.
MAX_BODY_CHARS = 32_000

_ROOM_ID_RE = re.compile(r"^![^\s:]+:[^\s:/]+(:\d+)?$")
_ROOM_ALIAS_RE = re.compile(r"^#[^\s:]+:[^\s:/]+(:\d+)?$")
_URL_RE = re.compile(r"https?://[^\s<>\"]+")


class MatrixError(Exception):
    """Base error for Matrix API interactions."""


class MatrixAuthError(MatrixError):
    """Access token rejected, expired, or revoked (M_UNKNOWN_TOKEN)."""


class MatrixPermissionError(MatrixError):
    """Token is valid but cannot post here (not joined, no power level)."""


def _error_detail(response) -> str:
    """Extract the homeserver's error message from a Matrix error body.

    Matrix errors are ``{"errcode": "M_...", "error": "human text"}``.
    """
    try:
        body = response.json()
    except ValueError:
        return ""
    if isinstance(body, dict):
        msg = body.get("error") or body.get("errcode")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()[:200]
    return ""


def _errcode(response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    if isinstance(body, dict):
        code = body.get("errcode")
        if isinstance(code, str):
            return code
    return ""


def _raise_for_status(response, action: str) -> None:
    """Map a Matrix error response onto our exception hierarchy.

    Auth vs permission matters to the caller: a rejected token is permanent
    until the user reconnects, while a rate limit is worth retrying.
    """
    if response.status_code == 200:
        return
    errcode = _errcode(response)
    detail = _error_detail(response)
    if response.status_code == 401 or errcode in ("M_UNKNOWN_TOKEN", "M_MISSING_TOKEN"):
        raise MatrixAuthError(
            "Matrix rejected the access token. Reconnect the account with a fresh token."
        )
    if errcode == "M_FORBIDDEN":
        raise MatrixPermissionError(
            f"Matrix refused the {action}: {detail or 'forbidden'}"
        )
    raise MatrixError(
        f"Matrix {action} failed (HTTP {response.status_code})"
        + (f": {detail}" if detail else "")
    )


def _auth_headers(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token.strip()}"}


# ── Input normalization ─────────────────────────────────────────────────────


def normalize_homeserver(raw: str) -> str:
    """Normalize a user-entered homeserver to a bare ``https://host[:port]``.

    Accepts ``matrix.org``, ``https://matrix.org/``, and
    ``https://matrix.org/_matrix/client/v3`` (the API path is stripped — the
    stored value is a base URL that FeedEcho appends paths to). Raises
    ValueError on anything that is not an http(s) host.
    """
    value = (raw or "").strip()
    if not value:
        raise ValueError("Enter your Matrix homeserver, e.g. https://matrix.org")
    if "://" not in value:
        value = f"https://{value}"
    if not value.lower().startswith(("http://", "https://")):
        raise ValueError("Homeserver URL must start with http:// or https://")
    # Drop any path/query the user pasted: everything downstream builds
    # `base + /_matrix/...`, so a leftover path would produce a 404 that reads
    # like an auth problem.
    scheme, _, rest = value.partition("://")
    host = rest.split("/")[0].split("?")[0].strip()
    if not host:
        raise ValueError("Homeserver URL has no hostname")
    return f"{scheme.lower()}://{host}".rstrip("/")


def normalize_room(raw: str) -> str:
    """Validate a room ID (``!abc:server``) or alias (``#room:server``).

    Also accepts matrix.to links and ``matrix:`` URIs pasted from a client,
    since "copy room link" is the easiest way for a user to get this value.
    """
    value = (raw or "").strip()
    if not value:
        raise ValueError("Enter the Matrix room ID or alias, e.g. #feeds:example.org")
    # https://matrix.to/#/%23room%3Aexample.org  |  matrix:roomid/abc:example.org
    if value.lower().startswith(("https://matrix.to/", "http://matrix.to/")):
        from urllib.parse import unquote

        fragment = value.split("#/", 1)[-1]
        value = unquote(fragment.split("/")[0].split("?")[0]).strip()
    elif value.lower().startswith("matrix:"):
        from urllib.parse import unquote

        body = value[len("matrix:") :].split("?")[0]
        kind, _, ident = body.partition("/")
        ident = unquote(ident)
        if kind == "roomid" and ident:
            value = f"!{ident}"
        elif kind == "r" and ident:
            value = f"#{ident}"
    if _ROOM_ID_RE.match(value) or _ROOM_ALIAS_RE.match(value):
        return value
    raise ValueError(
        "Enter a room ID (!abcdef:example.org) or alias (#feeds:example.org)"
    )


def is_room_alias(room: str) -> bool:
    return room.startswith("#")


def permalink(room_id: str, event_id: str) -> str:
    """A matrix.to permalink for the history page's post link."""
    if not room_id or not event_id:
        return ""
    return f"https://matrix.to/#/{quote(room_id, safe='')}/{quote(event_id, safe='')}"


def html_body(text: str) -> str:
    """HTML for ``formatted_body``: escaped text with http(s) URLs linkified.

    Element and most clients linkify plain bodies already, but not all do, and
    a rendered template is usually "headline + link". Escaping happens FIRST,
    so the linkifier only ever sees inert text; a URL containing ``&`` becomes
    ``&amp;`` inside the href, which is the correct HTML spelling of it.
    """
    escaped = html.escape(text, quote=False)
    linked = _URL_RE.sub(
        lambda m: f'<a href="{m.group(0)}">{m.group(0)}</a>', escaped
    )
    return linked.replace("\n", "<br />")


def _truncate_body(text: str) -> str:
    if len(text) <= MAX_BODY_CHARS:
        return text
    return text[: MAX_BODY_CHARS - 1].rstrip() + "…"


def transaction_id(echo_id: int, item_id: str, suffix: str = "") -> str:
    """A deterministic transaction ID for one echo+item send.

    Matrix de-duplicates repeated PUTs with the same transaction ID, so a
    retry of a send that the homeserver actually processed (response lost in
    transit) returns the original event instead of posting twice. Derived
    from the item ID rather than random for exactly that reason.
    """
    digest = hashlib.sha256(str(item_id).encode("utf-8", "replace")).hexdigest()[:20]
    tail = f".{suffix}" if suffix else ""
    return f"feedecho.{echo_id}.{digest}{tail}"


# ── Discovery ───────────────────────────────────────────────────────────────


def discover_base_url(homeserver: str) -> str:
    """Resolve the client API base URL for a server name.

    ``/.well-known/matrix/client`` is how a domain delegates its client API to
    another host. Absent or unusable well-known data means the homeserver URL
    itself is the base (which is what every client does).
    """
    base = normalize_homeserver(homeserver)
    validate_outbound_url(base)
    url = f"{base}{WELL_KNOWN_PATH}"
    try:
        resp = pinned_request(
            "GET", url, timeout=WELL_KNOWN_TIMEOUT, follow_redirects=True
        )
    except (httpx.HTTPError, SSRFError):
        # Well-known is optional; a network failure here is not fatal because
        # the plain homeserver URL is the documented fallback. A redirect to
        # an internal address is refused (SSRFError) and falls back the same
        # way — well-known is served by the same domain being connected, but
        # it is still remote input pointing our requests somewhere.
        logger.debug("Matrix well-known lookup failed for %s", base, exc_info=True)
        return base
    if resp.status_code != 200:
        return base
    try:
        data = resp.json()
    except ValueError:
        return base
    if not isinstance(data, dict):
        return base
    entry = data.get("m.homeserver")
    if not isinstance(entry, dict):
        return base
    delegated = entry.get("base_url")
    if not isinstance(delegated, str) or not delegated.strip():
        return base
    try:
        resolved = normalize_homeserver(delegated)
        validate_outbound_url(resolved)
    except ValueError:
        # A malformed or internal-address delegation is ignored rather than
        # trusted: well-known is served by the same domain being connected,
        # but it is still remote input pointing our requests somewhere.
        logger.warning(
            "Ignoring unusable Matrix well-known delegation from %s: %.80r",
            base,
            delegated,
        )
        return base
    return resolved


def whoami(base_url: str, access_token: str) -> str:
    """Return the Matrix user ID the token belongs to."""
    validate_outbound_url(base_url)
    try:
        resp = pinned_request(
            "GET",
            f"{base_url}{CLIENT_API}/account/whoami",
            timeout=REQUEST_TIMEOUT,
            headers=_auth_headers(access_token),
            follow_redirects=True,
        )
    except (httpx.HTTPError, SSRFError) as e:
        raise MatrixError(f"Could not reach the Matrix homeserver: {e}") from e
    _raise_for_status(resp, "token check")
    try:
        data = resp.json()
    except ValueError as e:
        raise MatrixError("Matrix returned a non-JSON whoami response") from e
    user_id = data.get("user_id") if isinstance(data, dict) else None
    if not isinstance(user_id, str) or not user_id.strip():
        raise MatrixError("Matrix whoami response had no user_id")
    return user_id.strip()


def resolve_room(base_url: str, access_token: str, room: str) -> str:
    """Resolve a room alias to its room ID; pass room IDs through unchanged."""
    room = normalize_room(room)
    if not is_room_alias(room):
        return room
    validate_outbound_url(base_url)
    try:
        resp = pinned_request(
            "GET",
            f"{base_url}{CLIENT_API}/directory/room/{quote(room, safe='')}",
            timeout=REQUEST_TIMEOUT,
            headers=_auth_headers(access_token),
            follow_redirects=True,
        )
    except (httpx.HTTPError, SSRFError) as e:
        raise MatrixError(f"Could not reach the Matrix homeserver: {e}") from e
    if resp.status_code == 404:
        raise MatrixError(
            f"Matrix could not find the room {room}. Check the alias and try again."
        )
    _raise_for_status(resp, "room lookup")
    try:
        data = resp.json()
    except ValueError as e:
        raise MatrixError("Matrix returned a non-JSON room lookup response") from e
    room_id = data.get("room_id") if isinstance(data, dict) else None
    if not isinstance(room_id, str) or not room_id.strip():
        raise MatrixError(f"Matrix returned no room_id for {room}")
    return room_id.strip()


def joined_rooms(base_url: str, access_token: str) -> set[str]:
    """The room IDs this token's user has joined."""
    validate_outbound_url(base_url)
    try:
        resp = pinned_request(
            "GET",
            f"{base_url}{CLIENT_API}/joined_rooms",
            timeout=REQUEST_TIMEOUT,
            headers=_auth_headers(access_token),
            follow_redirects=True,
        )
    except (httpx.HTTPError, SSRFError) as e:
        raise MatrixError(f"Could not reach the Matrix homeserver: {e}") from e
    _raise_for_status(resp, "joined rooms lookup")
    try:
        data = resp.json()
    except ValueError as e:
        raise MatrixError("Matrix returned a non-JSON joined_rooms response") from e
    rooms = data.get("joined_rooms") if isinstance(data, dict) else None
    if not isinstance(rooms, list):
        return set()
    return {r.strip() for r in rooms if isinstance(r, str) and r.strip()}


def connect(homeserver: str, access_token: str, room: str) -> dict:
    """Verify a token + room and return everything needed to store the account.

    Returns ``{base_url, user_id, room_id, room_alias}``. ``room_alias`` is the
    alias the user typed when they gave one (kept for display), else "".
    Raises ValueError for malformed input, MatrixAuthError for a bad token,
    and MatrixError/MatrixPermissionError for everything else.
    """
    room = normalize_room(room)
    base_url = discover_base_url(homeserver)
    user_id = whoami(base_url, access_token)
    room_id = resolve_room(base_url, access_token, room)
    joined = joined_rooms(base_url, access_token)
    if room_id not in joined:
        raise MatrixPermissionError(
            f"{user_id} is not in {room}. Invite that user to the room and accept "
            "the invite, then connect again."
        )
    return {
        "base_url": base_url,
        "user_id": user_id,
        "room_id": room_id,
        "room_alias": room if is_room_alias(room) else "",
    }


# ── Sending ─────────────────────────────────────────────────────────────────


def send_event(
    base_url: str,
    access_token: str,
    room_id: str,
    content: dict,
    txn_id: str,
) -> str:
    """PUT one m.room.message event; returns its event ID."""
    validate_outbound_url(base_url)
    url = (
        f"{base_url}{CLIENT_API}/rooms/{quote(room_id, safe='')}"
        f"/send/{MESSAGE_EVENT_TYPE}/{quote(txn_id, safe='')}"
    )
    try:
        resp = pinned_request(
            "PUT",
            url,
            timeout=REQUEST_TIMEOUT,
            headers=_auth_headers(access_token),
            json=content,
            follow_redirects=True,
        )
    except (httpx.HTTPError, SSRFError) as e:
        raise MatrixError(f"Could not reach the Matrix homeserver: {e}") from e
    _raise_for_status(resp, "message send")
    try:
        data = resp.json()
    except ValueError as e:
        raise MatrixError("Matrix returned a non-JSON send response") from e
    event_id = data.get("event_id") if isinstance(data, dict) else None
    if not isinstance(event_id, str) or not event_id.strip():
        raise MatrixError("Matrix send response had no event_id")
    return event_id.strip()


def send_message(
    base_url: str,
    access_token: str,
    room_id: str,
    body: str,
    txn_id: str,
) -> str:
    """Send a text message, with an HTML body when the text contains links."""
    text = _truncate_body(body)
    if not text.strip():
        raise MatrixError("Cannot send an empty message to Matrix")
    content = {"msgtype": "m.text", "body": text}
    if _URL_RE.search(text):
        content["format"] = "org.matrix.custom.html"
        content["formatted_body"] = html_body(text)
    return send_event(base_url, access_token, room_id, content, txn_id)


def upload_media(
    base_url: str,
    access_token: str,
    data: bytes,
    content_type: str,
    filename: str = "image",
) -> str:
    """Upload bytes to the homeserver media repo; returns the mxc:// URI."""
    if not data:
        raise MatrixError("Cannot upload empty image data to Matrix")
    if len(data) > MAX_UPLOAD_BYTES:
        raise MatrixError(
            f"Image is {len(data)} bytes, over FeedEcho's {MAX_UPLOAD_BYTES}-byte "
            "Matrix upload limit"
        )
    validate_outbound_url(base_url)
    headers = _auth_headers(access_token)
    headers["Content-Type"] = content_type or "application/octet-stream"
    try:
        resp = pinned_request(
            "POST",
            f"{base_url}{MEDIA_API}/upload",
            timeout=REQUEST_TIMEOUT,
            headers=headers,
            params={"filename": filename},
            content=data,
            follow_redirects=True,
        )
    except (httpx.HTTPError, SSRFError) as e:
        raise MatrixError(f"Could not reach the Matrix homeserver: {e}") from e
    _raise_for_status(resp, "media upload")
    try:
        body = resp.json()
    except ValueError as e:
        raise MatrixError("Matrix returned a non-JSON upload response") from e
    uri = body.get("content_uri") if isinstance(body, dict) else None
    if not isinstance(uri, str) or not uri.startswith("mxc://"):
        raise MatrixError("Matrix upload response had no content_uri")
    return uri


def send_image(
    base_url: str,
    access_token: str,
    room_id: str,
    mxc_uri: str,
    body: str,
    content_type: str,
    size: int,
    txn_id: str,
) -> str:
    """Send an m.image event for an already-uploaded mxc:// URI.

    ``body`` carries the description (alt text when we have it, otherwise a
    filename) — that is the field Matrix clients read out for accessibility.
    """
    content = {
        "msgtype": "m.image",
        "body": _truncate_body(body or "image"),
        "url": mxc_uri,
        "info": {"mimetype": content_type or "application/octet-stream", "size": size},
    }
    return send_event(base_url, access_token, room_id, content, txn_id)


def test_connection(
    base_url: str, access_token: str, room_id: str = ""
) -> tuple[bool, str]:
    """Verify a stored account still works. Backs the accounts page Test button.

    Deliberately read-only: whoami plus a membership check, no test message
    into the user's room. ``base_url`` is the stored resolved base (from
    connect-time well-known discovery), so no discovery round trip is needed.
    """
    try:
        user_id = whoami(base_url, access_token)
        if room_id:
            joined = joined_rooms(base_url, access_token)
            if room_id not in joined:
                return False, f"{user_id} is no longer in {room_id}. Re-invite it."
    except MatrixAuthError as e:
        return False, str(e)
    except MatrixPermissionError as e:
        return False, str(e)
    except MatrixError as e:
        return False, str(e)
    except ValueError as e:  # normalize_homeserver / SSRF guard
        return False, str(e)
    where = f" — can post to {room_id}" if room_id else ""
    return True, f"Token OK for {user_id}{where}"
