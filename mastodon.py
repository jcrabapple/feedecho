"""Mastodon API client — post statuses via the Mastodon REST API."""

import hashlib
import httpx
from typing import Optional
from feed_parser import pinned_request, validate_outbound_url
from utils import DestinationAuthError, DestinationError


class MastodonError(DestinationError):
    """Base error for Mastodon API interactions."""


class MastodonAuthError(MastodonError, DestinationAuthError):
    """Access token rejected or insufficient permission (HTTP 401/403).

    Permanent: retries cannot help until the user reconnects the account.
    """


def _raise_for_status(response) -> None:
    """Raise MastodonAuthError on 401/403, otherwise the plain httpx error.

    401/403 mean the stored token was revoked/expired or lacks the required
    scope — permanent, not worth retrying. Every other 4xx/5xx (rate limits,
    server errors, etc.) falls through to httpx's generic HTTPStatusError,
    which callers already treat as transient/retryable.
    """
    if response.status_code in (401, 403):
        raise MastodonAuthError(
            f"Mastodon rejected the access token (HTTP {response.status_code})."
        )
    response.raise_for_status()


def idempotency_key(echo_id: int, item_id: str) -> str:
    """A deterministic Idempotency-Key for one echo+item post.

    Mastodon's POST /api/v1/statuses honors a client-supplied
    Idempotency-Key header: submitting the same key twice within its
    retention window returns the original status instead of creating a
    duplicate. Deriving the key from the item ID (not random) means a
    crash-then-reclaim retry of the same logical post reuses the same key
    and is deduplicated server-side, mirroring Matrix's transaction_id.
    """
    digest = hashlib.sha256(str(item_id).encode("utf-8", "replace")).hexdigest()[:32]
    return f"feedecho-{echo_id}-{digest}"


def upload_media(
    instance: str,
    access_token: str,
    image_bytes: bytes,
    content_type: str,
    description: str = "",
) -> dict | None:
    """Upload an image to a Mastodon instance for attachment to a status.

    Args:
        instance: Base URL of the instance (e.g. "https://dmv.community")
        access_token: OAuth access token
        image_bytes: Raw image bytes
        content_type: MIME type (e.g. "image/jpeg")
        description: Alt text for the image (optional)

    Returns:
        Dict with 'id' (media attachment ID) on success, None on failure.
    """
    instance = instance.rstrip("/")
    validate_outbound_url(instance)
    url = f"{instance}/api/v2/media"
    headers = {"Authorization": f"Bearer {access_token}"}

    # Map MIME type to filename extension for the upload
    ext_map = {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/gif": "gif",
        "image/webp": "webp",
        "image/avif": "avif",
    }
    ext = ext_map.get(content_type, "jpg")
    files = {"file": (f"image.{ext}", image_bytes, content_type)}
    data = {}
    if description:
        data["description"] = description[:1500]  # Mastodon caps alt text at 1500 chars

    try:
        response = pinned_request(
            "POST", url, timeout=60, headers=headers, files=files, data=data
        )
        _raise_for_status(response)
        return response.json()
    except MastodonAuthError:
        # Permanent: the caller (scheduler._send_mastodon) needs to
        # distinguish this from the transient failures below, so let it
        # propagate rather than folding it into the "None on failure"
        # contract used for everything else here.
        raise
    # ValueError covers a 200 with a non-JSON body (an instance behind an
    # HTML-returning proxy), which otherwise escaped the documented
    # "None on failure" contract and crashed the caller. SSRFError is a
    # ValueError subclass, so a refused address returns None the same way.
    except (httpx.HTTPStatusError, httpx.RequestError, ValueError):
        return None


def post_status(
    instance: str,
    access_token: str,
    content: str,
    visibility: str = "public",
    sensitive: bool = False,
    spoiler_text: str = "",
    media_ids: list[str] | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Post a status to a Mastodon instance.

    Args:
        instance: Base URL of the instance (e.g. "https://dmv.community")
        access_token: OAuth access token
        content: The status text
        visibility: public, unlisted, private, or direct
        sensitive: Mark as sensitive content
        spoiler_text: Content warning text (shown above the post body)
        media_ids: List of media attachment IDs to attach
        idempotency_key: Optional client-supplied key sent as the
            Idempotency-Key header. Mastodon deduplicates a repeated POST
            with the same key (within its retention window), returning the
            original status instead of creating a second one — protects
            against a crash-then-reclaim retry double-posting.

    Returns:
        Dict with response data including 'id' and 'url' on success.

    Raises:
        MastodonAuthError on a rejected/expired token (HTTP 401/403) —
        permanent, not worth retrying. httpx.HTTPStatusError on any other
        API failure (rate limits, server errors, etc.) — transient.
    """
    instance = instance.rstrip("/")
    validate_outbound_url(instance)
    url = f"{instance}/api/v1/statuses"
    headers = {"Authorization": f"Bearer {access_token}"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    data = {
        "status": content,
        "visibility": visibility,
        "sensitive": sensitive,
    }
    if spoiler_text:
        data["spoiler_text"] = spoiler_text
        data["sensitive"] = True
    if media_ids:
        data["media_ids[]"] = media_ids

    response = pinned_request(
        "POST", url, timeout=30, headers=headers, data=data
    )
    _raise_for_status(response)
    return response.json()


def verify_credentials(instance: str, access_token: str) -> dict:
    """Verify credentials and return account info.

    Returns dict with 'username', 'display_name', 'url' on success.

    Raises:
        MastodonAuthError on a rejected/expired token (HTTP 401/403).
        httpx.HTTPStatusError on any other API failure.
    """
    instance = instance.rstrip("/")
    validate_outbound_url(instance)
    url = f"{instance}/api/v1/accounts/verify_credentials"
    headers = {"Authorization": f"Bearer {access_token}"}

    response = pinned_request("GET", url, timeout=30, headers=headers)
    _raise_for_status(response)
    return response.json()


def test_connection(instance: str, access_token: str) -> tuple[bool, str]:
    """Test a Mastodon connection. Returns (success, message)."""
    try:
        result = verify_credentials(instance, access_token)
        return True, f"Connected as @{result.get('username', 'unknown')}"
    except MastodonAuthError as e:
        return False, str(e)
    except httpx.HTTPStatusError as e:
        return False, f"HTTP {e.response.status_code}: {e.response.text[:200]}"
    except httpx.RequestError as e:
        return False, f"Network error: {e}"
