"""Mastodon API client — post statuses via the Mastodon REST API."""

import httpx
from typing import Optional
from feed_parser import pinned_request, validate_outbound_url


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
        response.raise_for_status()
        return response.json()
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

    Returns:
        Dict with response data including 'id' and 'url' on success.

    Raises:
        httpx.HTTPStatusError on API failure.
    """
    instance = instance.rstrip("/")
    validate_outbound_url(instance)
    url = f"{instance}/api/v1/statuses"
    headers = {"Authorization": f"Bearer {access_token}"}
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
    response.raise_for_status()
    return response.json()


def verify_credentials(instance: str, access_token: str) -> dict:
    """Verify credentials and return account info.

    Returns dict with 'username', 'display_name', 'url' on success.
    """
    instance = instance.rstrip("/")
    validate_outbound_url(instance)
    url = f"{instance}/api/v1/accounts/verify_credentials"
    headers = {"Authorization": f"Bearer {access_token}"}

    response = pinned_request("GET", url, timeout=30, headers=headers)
    response.raise_for_status()
    return response.json()


def test_connection(instance: str, access_token: str) -> tuple[bool, str]:
    """Test a Mastodon connection. Returns (success, message)."""
    try:
        result = verify_credentials(instance, access_token)
        return True, f"Connected as @{result.get('username', 'unknown')}"
    except httpx.HTTPStatusError as e:
        return False, f"HTTP {e.response.status_code}: {e.response.text[:200]}"
    except httpx.RequestError as e:
        return False, f"Network error: {e}"
