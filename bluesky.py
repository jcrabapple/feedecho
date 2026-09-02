"""Bluesky (AT Protocol) client — sessions, PDS resolution, posts, and images.

FeedEcho connects to Bluesky using app passwords (the standard method for
bots and automation). App passwords are created in the Bluesky app under
Settings > Privacy & Security > App Passwords. They are scoped to app-level
actions (posting among them) and cannot change account settings or be used
to log in to the app.
"""

from __future__ import annotations

import base64
import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx

from feed_parser import SSRFError, pinned_request, validate_outbound_url

PUBLIC_API = "https://public.api.bsky.app"
PLC_DIRECTORY = "https://plc.directory"
POST_COLLECTION = "app.bsky.feed.post"
POST_RECORD_TYPE = "app.bsky.feed.post"

MAX_POST_GRAPHEMES = 300
MAX_ALT_GRAPHEMES = 1000
MAX_BLOB_BYTES = 1_000_000  # bsky.social PDS limit for image blobs
BLUESKY_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}

# How long to trust a cached access JWT before refreshing (JWT exp takes
# precedence when decodable). Access tokens typically live ~2 hours.
DEFAULT_SESSION_TTL_SECONDS = 2 * 60 * 60


class BlueskyError(Exception):
    """Base error for Bluesky API interactions."""


class BlueskyAuthError(BlueskyError):
    """Credentials rejected, session expired, or app password revoked."""


def _error_detail(response) -> str:
    """Extract the PDS-provided error message from a JSON error body."""
    try:
        body = response.json()
    except ValueError:
        return ""
    if isinstance(body, dict):
        msg = body.get("message") or body.get("error")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()[:200]
    return ""


def _is_token_error(detail: str) -> bool:
    """Detect expired/revoked-token responses regardless of status code.

    bsky.social historically returns ExpiredToken as a 400; treat both
    explicit 401s and ExpiredToken/InvalidToken bodies as auth failures.
    """
    return "ExpiredToken" in detail or "InvalidToken" in detail


# ── Handle normalization ─────────────────────────────────────────────────────


_HANDLE_RE = re.compile(
    r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$"
)
_HANDLE_MAX_LEN = 253


def normalize_handle(raw: str) -> str:
    """Normalize user-entered handles to lowercase bare form.

    Accepts "Name", "@Name.Bsky.Social", and "https://bsky.app/profile/x"
    style inputs (the host part is taken from URLs). Enforces the AT Protocol
    handle grammar and length limit. Raises ValueError on invalid input.
    """
    value = (raw or "").strip().lower().removeprefix("@")
    value = re.sub(r"^https?://", "", value)
    # Take the last path segment if given a profile URL.
    value = value.split("/")[-1].strip()
    value = value.rstrip(".").strip()
    if not value or len(value) > _HANDLE_MAX_LEN or not _HANDLE_RE.match(value):
        raise ValueError("Enter a valid Bluesky handle, e.g. username.bsky.social")
    return value


# ── PDS discovery ────────────────────────────────────────────────────────────


def resolve_pds(handle: str) -> tuple[str, str]:
    """Resolve a handle to (did, pds_url) via the public identity service.

    Handles hosted on bsky.social and custom PDSes both work: the DID
    document's #atproto_pds service entry tells us where the account's data
    lives, so we never hardcode bsky.social.
    """
    handle = normalize_handle(handle)
    validate_outbound_url(PUBLIC_API)

    try:
        response = pinned_request(
            "GET",
            f"{PUBLIC_API}/xrpc/com.atproto.identity.resolveHandle",
            timeout=15.0,
            params={"handle": handle},
        )
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise BlueskyError(f"Could not resolve handle '{handle}'") from exc

    did = data.get("did")
    if not isinstance(did, str) or not did:
        raise BlueskyError(f"Handle '{handle}' did not resolve to a DID")

    if did.startswith("did:web:"):
        # did:web:example.com -> https://example.com/.well-known/did.json
        doc_url = f"https://{did.removeprefix('did:web:')}/.well-known/did.json"
        # The doc host is derived from remote identity data — SSRF-validate it
        # before fetching, like every other outbound hop.
        validate_outbound_url(doc_url)
        try:
            response = pinned_request("GET", doc_url, timeout=15.0)
            response.raise_for_status()
            doc = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BlueskyError(f"Could not fetch DID document for '{handle}'") from exc
    else:
        # did:plc:... -> query the PLC directory for the PDS service endpoint.
        try:
            response = pinned_request(
                "GET", f"{PLC_DIRECTORY}/{quote(did, safe='')}", timeout=15.0
            )
            response.raise_for_status()
            doc = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BlueskyError(f"Could not fetch DID document for '{handle}'") from exc

    # The DID document comes from a remote (for did:web, handle-controlled)
    # host, so nothing about its shape is guaranteed. Without these checks a
    # document that is a JSON array or has non-dict service entries raised
    # AttributeError, which none of the callers' except clauses catch.
    if not isinstance(doc, dict):
        raise BlueskyError(f"Invalid DID document for '{handle}'")
    pds = ""
    for service in doc.get("service") or []:
        if not isinstance(service, dict):
            continue
        endpoint = service.get("serviceEndpoint")
        if service.get("id") == "#atproto_pds" and isinstance(endpoint, str) and endpoint:
            pds = endpoint
            break
    if not pds:
        raise BlueskyError(f"No PDS endpoint found for '{handle}'")

    pds = pds.rstrip("/")
    # The PDS hostname came from a remote identity document — validate it
    # against SSRF/private-IP rules before making requests to it.
    validate_outbound_url(pds)
    return did, pds


# ── Sessions ─────────────────────────────────────────────────────────────────


def _decode_jwt_exp(jwt_str: str) -> datetime | None:
    """Return the exp claim as an aware UTC datetime, or None if undecodable."""
    try:
        payload = jwt_str.split(".")[1]
        # base64url decode with padding restored
        padded = payload + "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
        exp = claims.get("exp")
        if isinstance(exp, (int, float)):
            return datetime.fromtimestamp(exp, tz=timezone.utc)
    except (IndexError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return None


def create_session(pds: str, handle: str, app_password: str) -> dict:
    """Create a session with an app password. Returns did + JWTs."""
    pds = pds.rstrip("/")
    validate_outbound_url(pds)

    try:
        response = pinned_request(
            "POST",
            f"{pds}/xrpc/com.atproto.server.createSession",
            timeout=30.0,
            json={"identifier": handle, "password": app_password},
        )
    except (httpx.RequestError, SSRFError) as exc:
        raise BlueskyError(f"Could not reach PDS {pds}") from exc

    if response.status_code in (400, 401):
        detail = _error_detail(response)
        suffix = f" ({detail})" if detail else ""
        raise BlueskyAuthError(f"Invalid handle or app password{suffix}")
    if response.status_code >= 400:
        detail = _error_detail(response)
        suffix = f" ({detail})" if detail else ""
        raise BlueskyError(f"PDS returned HTTP {response.status_code}{suffix}")

    try:
        data = response.json()
    except ValueError as exc:
        raise BlueskyError("PDS returned an invalid session response") from exc

    did = data.get("did")
    access_jwt = data.get("accessJwt")
    refresh_jwt = data.get("refreshJwt")
    if not all(isinstance(v, str) and v for v in (did, access_jwt, refresh_jwt)):
        raise BlueskyError("PDS returned an incomplete session response")
    return {"did": did, "access_jwt": access_jwt, "refresh_jwt": refresh_jwt}


def refresh_session(pds: str, refresh_jwt: str) -> dict:
    """Refresh a session using its refresh JWT."""
    pds = pds.rstrip("/")
    validate_outbound_url(pds)

    try:
        response = pinned_request(
            "POST",
            f"{pds}/xrpc/com.atproto.server.refreshSession",
            timeout=30.0,
            headers={"Authorization": f"Bearer {refresh_jwt}"},
        )
    except (httpx.RequestError, SSRFError) as exc:
        raise BlueskyError(f"Could not reach PDS {pds}") from exc

    if response.status_code in (400, 401):
        detail = _error_detail(response)
        suffix = f" ({detail})" if detail else ""
        raise BlueskyAuthError(f"Session refresh rejected{suffix}")
    if response.status_code >= 400:
        detail = _error_detail(response)
        suffix = f" ({detail})" if detail else ""
        raise BlueskyError(f"PDS returned HTTP {response.status_code}{suffix}")

    try:
        data = response.json()
    except ValueError as exc:
        raise BlueskyError("PDS returned an invalid refresh response") from exc

    access_jwt = data.get("accessJwt")
    new_refresh = data.get("refreshJwt")
    did = data.get("did")
    if not isinstance(access_jwt, str) or not access_jwt or not isinstance(did, str):
        raise BlueskyError("PDS returned an incomplete refresh response")
    if not isinstance(new_refresh, str) or not new_refresh:
        # AT Protocol rotates refresh tokens on every refresh; a missing
        # rotated token means the old one is dead. Do not reuse it.
        raise BlueskyError("PDS refresh response omitted the rotated refresh token")
    return {"did": did, "access_jwt": access_jwt, "refresh_jwt": new_refresh}


def delete_session(pds: str, refresh_jwt: str) -> None:
    """Best-effort session cleanup (used by connection tests). Never raises."""
    pds = pds.rstrip("/")
    try:
        validate_outbound_url(pds)
        pinned_request(
            "POST",
            f"{pds}/xrpc/com.atproto.server.deleteSession",
            timeout=15.0,
            headers={"Authorization": f"Bearer {refresh_jwt}"},
        )
    except Exception:
        # SSRF validation failure or network error — cleanup is best-effort.
        pass


def session_expiry(access_jwt: str) -> str:
    """SQLite timestamp for when a cached access JWT should be refreshed."""
    exp = _decode_jwt_exp(access_jwt)
    if exp is None:
        exp = datetime.now(timezone.utc) + timedelta(seconds=DEFAULT_SESSION_TTL_SECONDS)
    else:
        # Refresh a minute early to avoid racing the actual expiry.
        exp = exp - timedelta(seconds=60)
    return exp.strftime("%Y-%m-%d %H:%M:%S")


# ── Rich text facets ─────────────────────────────────────────────────────────


_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_FACET_LINK_TYPE = "app.bsky.richtext.facet#link"


def build_facets(text: str) -> list[dict]:
    """Find URLs in post text and build link facets with UTF-8 byte offsets.

    Byte offsets are relative to the UTF-8 encoding of the text, as required
    by app.bsky.richtext.facet. Trailing punctuation is trimmed from each URL
    so it is not swallowed into the link.
    """
    encoded = text.encode("utf-8")
    facets: list[dict] = []

    for match in _URL_RE.finditer(text):
        uri = match.group(0)
        # If the truncation ellipsis is inside the match, the URL was cut off
        # mid-way — drop it instead of linkifying a broken/partial URL.
        if "…" in uri:
            continue
        # Strip trailing punctuation that is unlikely to be part of the URL.
        while uri and uri[-1] in ".,;:!?'\"()[]{}<>":
            uri = uri[:-1]
        if not uri:
            continue

        # Locate the trimmed URI's byte range inside the encoded text.
        char_start = match.start()
        byte_start = len(text[:char_start].encode("utf-8"))
        byte_end = byte_start + len(uri.encode("utf-8"))

        # If the text was truncated mid-URL, the byte range won't decode back
        # to the full URI — drop the facet instead of linkifying a broken URL.
        if byte_end > len(encoded) or encoded[byte_start:byte_end].decode("utf-8") != uri:
            continue

        facets.append(
            {
                "index": {"byteStart": byte_start, "byteEnd": byte_end},
                "features": [
                    {"$type": _FACET_LINK_TYPE, "uri": uri},
                ],
            }
        )

    return facets


# ── Grapheme-aware truncation ────────────────────────────────────────────────


_ZWJ = "\u200d"
_VARIATION_SELECTORS = {"\ufe0e", "\ufe0f"}
_SKIN_TONE_RANGE = range(0x1F3FB, 0x1F400)
_EMOJI_TAG_RANGE = range(0xE0020, 0xE0080)


def _is_regional_indicator(ch: str) -> bool:
    return 0x1F1E6 <= ord(ch) <= 0x1F1FF


def _trailing_ri_count(cluster: str) -> int:
    n = 0
    for ch in reversed(cluster):
        if _is_regional_indicator(ch):
            n += 1
        else:
            break
    return n


def _grapheme_clusters(text: str) -> list[str]:
    """Split text into grapheme clusters using a conservative UAX #29 subset.

    A new grapheme starts at any character that is not a combining mark,
    zero-width joiner, variation selector, skin-tone modifier, or emoji tag
    character. ZWJ glues in both directions (GB9 + a permissive GB11
    stand-in), so ZWJ emoji sequences like family emoji stay together.
    Regional-indicator pairs (flags) merge so a flag never splits. The
    remaining gaps over-merge, which can only under-count graphemes, so
    truncation never exceeds platform limits.
    """
    clusters: list[str] = []
    for ch in text:
        cat = unicodedata.category(ch)
        is_continuation = (
            cat in ("Mn", "Mc", "Me")
            or ch in _VARIATION_SELECTORS
            or ord(ch) in _SKIN_TONE_RANGE
            or ord(ch) in _EMOJI_TAG_RANGE
        )
        if clusters:
            is_continuation = (
                is_continuation
                or ch == _ZWJ
                or clusters[-1][-1] == _ZWJ
                or (
                    _is_regional_indicator(ch)
                    and _trailing_ri_count(clusters[-1]) == 1
                )
            )
        if clusters and is_continuation:
            clusters[-1] += ch
        else:
            clusters.append(ch)
    return clusters


def truncate_graphemes(text: str, max_graphemes: int = MAX_POST_GRAPHEMES) -> str:
    """Truncate to max_graphemes grapheme clusters, appending an ellipsis."""
    clusters = _grapheme_clusters(text)
    if len(clusters) <= max_graphemes:
        return text
    head = "".join(clusters[: max_graphemes - 1]).rstrip()
    # Don't leave a dangling ZWJ at the cut point — it renders as a broken
    # sequence in most clients.
    while head.endswith(_ZWJ):
        head = head[:-1]
    return head + "…"


# ── Posts and images ─────────────────────────────────────────────────────────


def upload_blob(
    pds: str, access_jwt: str, image_bytes: bytes, content_type: str
) -> dict | None:
    """Upload an image blob.

    Enforces Bluesky's image constraints (type allowlist, 1 MB cap). Returns
    the blob reference dict, or None on transport failure. Raises
    BlueskyAuthError on expired/revoked sessions and BlueskyError on other
    API rejections, so callers can distinguish "skip the image" from
    "refresh the session".
    """
    pds = pds.rstrip("/")
    validate_outbound_url(pds)

    if content_type not in BLUESKY_IMAGE_TYPES:
        raise BlueskyError(f"Unsupported image type for Bluesky: {content_type}")
    if len(image_bytes) > MAX_BLOB_BYTES:
        raise BlueskyError("Image exceeds the 1 MB Bluesky blob limit")

    try:
        response = pinned_request(
            "POST",
            f"{pds}/xrpc/com.atproto.repo.uploadBlob",
            timeout=60.0,
            headers={
                "Authorization": f"Bearer {access_jwt}",
                "Content-Type": content_type,
            },
            content=image_bytes,
        )
    except (httpx.RequestError, SSRFError):
        return None

    detail = _error_detail(response)
    if response.status_code == 401 or _is_token_error(detail):
        suffix = f" ({detail})" if detail else ""
        raise BlueskyAuthError(f"Blob upload rejected: session expired{suffix}")
    if response.status_code >= 400:
        suffix = f" ({detail})" if detail else ""
        raise BlueskyError(f"Blob upload failed (HTTP {response.status_code}){suffix}")

    try:
        blob = response.json().get("blob")
    except ValueError:
        return None
    if not isinstance(blob, dict) or "ref" not in blob:
        return None
    return blob


def create_post(
    pds: str,
    access_jwt: str,
    repo: str,
    text: str,
    facets: list[dict] | None = None,
    embed: dict | None = None,
) -> dict:
    """Create a post record. Returns {uri, cid}. Raises BlueskyAuthError on 401."""
    pds = pds.rstrip("/")
    validate_outbound_url(pds)

    record: dict = {
        "$type": POST_RECORD_TYPE,
        "text": text,
        "createdAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if facets:
        record["facets"] = facets
    if embed:
        record["embed"] = embed

    payload = {"repo": repo, "collection": POST_COLLECTION, "record": record}

    try:
        response = pinned_request(
            "POST",
            f"{pds}/xrpc/com.atproto.repo.createRecord",
            timeout=30.0,
            headers={"Authorization": f"Bearer {access_jwt}"},
            json=payload,
        )
    except (httpx.RequestError, SSRFError) as exc:
        raise BlueskyError(f"Could not reach PDS {pds}") from exc

    detail = _error_detail(response)
    if response.status_code == 401 or _is_token_error(detail):
        # 401 (or a 400 carrying ExpiredToken/InvalidToken) is a session
        # problem. A plain 400 is a record validation error, not auth.
        suffix = f" ({detail})" if detail else ""
        raise BlueskyAuthError(f"Post rejected (HTTP {response.status_code}){suffix}")
    if response.status_code >= 400:
        suffix = f" ({detail})" if detail else ""
        raise BlueskyError(f"Post rejected (HTTP {response.status_code}){suffix}")

    try:
        data = response.json()
    except ValueError as exc:
        raise BlueskyError("PDS returned an invalid post response") from exc
    if not isinstance(data.get("uri"), str) or not isinstance(data.get("cid"), str):
        raise BlueskyError("PDS returned an incomplete post response")
    return {"uri": data["uri"], "cid": data["cid"]}


def build_image_embed(blob: dict, alt_text: str) -> dict:
    """Wrap an uploaded blob in an app.bsky.embed.images embed."""
    alt = truncate_graphemes((alt_text or "").strip(), MAX_ALT_GRAPHEMES)
    return {
        "$type": "app.bsky.embed.images",
        "images": [{"alt": alt, "image": blob}],
    }


# ── Connection testing ───────────────────────────────────────────────────────


def test_connection(handle: str, app_password: str) -> tuple[bool, str]:
    """Resolve the handle and verify the app password works."""
    try:
        handle = normalize_handle(handle)
        did, pds = resolve_pds(handle)
        session = create_session(pds, handle, app_password)
        try:
            return True, f"Connected as @{handle} ({session['did'][:20]}…)"
        finally:
            # Don't accumulate server-side sessions from repeated tests —
            # bsky.social rate-limits createSession aggressively.
            delete_session(pds, session["refresh_jwt"])
    except ValueError as e:
        return False, str(e)
    except BlueskyAuthError:
        return False, "Invalid handle or app password"
    except BlueskyError as e:
        return False, str(e)
