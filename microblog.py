"""Micro.blog client — Micropub posting for configured blogs.

Micro.blog implements the Micropub standard for posting. FeedEcho connects
with an app token (created at https://micro.blog/account/apps — tokens can
post on the user's behalf but nothing else). One token may be able to post
to several microblogs; GET /micropub?q=config lists them as "destination"
entries whose "uid" is the blog URL. FeedEcho stores one account row per
blog and targets it on each post with the mp-destination parameter.

Posting is form-encoded (not JSON) because Micro.blog's photo handling
follows the classic form flow: pass the feed item's image URL directly as
photo=, with mp-photo-alt supplying alt text. No blob upload is needed on
our side.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

MICROPUB_ENDPOINT = "https://micro.blog/micropub"
REQUEST_TIMEOUT = 30
TOKEN_DISPLAY_MAX = 8


class MicroblogError(Exception):
    """Base error for Micro.blog API interactions."""


class MicroblogAuthError(MicroblogError):
    """Token rejected, expired, or revoked."""


def _error_detail(response) -> str:
    """Extract a Micropub error message from a JSON error body."""
    try:
        body = response.json()
    except ValueError:
        return ""
    if isinstance(body, dict):
        msg = body.get("error_description") or body.get("error")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()[:200]
    return ""


def _bearer(token: str) -> str:
    return f"Bearer {token.strip()}"


def _is_auth_rejection(status_code: int) -> bool:
    """Whether the HTTP status means the token was rejected.

    Deliberately status-code-only: string-matching error bodies for
    "unauthorized"/"forbidden" on other statuses would misclassify
    transient server errors as permanent auth failures.
    """
    return status_code in (401, 403)


def fetch_config(token: str) -> dict:
    """Query GET /micropub?q=config with the given app token.

    Returns the parsed JSON config. Raises MicroblogAuthError when the
    token is rejected and MicroblogError for anything else (network,
    non-JSON body, unexpected status).
    """
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            resp = client.get(
                MICROPUB_ENDPOINT,
                params={"q": "config"},
                headers={"Authorization": _bearer(token)},
                follow_redirects=True,
            )
    except httpx.HTTPError as e:
        raise MicroblogError(f"Could not reach micro.blog: {e}") from e

    detail = _error_detail(resp)
    if _is_auth_rejection(resp.status_code):
        raise MicroblogAuthError(
            "Micro.blog rejected the app token. Check the token and try again."
        )
    if resp.status_code != 200:
        raise MicroblogError(
            f"Micro.blog config query failed (HTTP {resp.status_code})"
            + (f": {detail}" if detail else "")
        )
    try:
        config = resp.json()
    except ValueError as e:
        raise MicroblogError("Micro.blog returned a non-JSON config response") from e
    if not isinstance(config, dict):
        raise MicroblogError("Micro.blog config response was not an object")
    return config


def list_destinations(token: str) -> list[dict]:
    """Return the blogs a token can post to, as [{uid, name}].

    The q=config "destination" key holds a list of blog objects; "uid" is
    the canonical blog URL used as mp-destination when posting, "name" the
    display name. Malformed entries are skipped rather than failing the
    whole connect.
    """
    config = fetch_config(token)
    destinations = config.get("destination")
    if not isinstance(destinations, list):
        raise MicroblogError(
            "Micro.blog config listed no blogs for this token."
        )
    blogs: list[dict] = []
    seen: set[str] = set()
    for entry in destinations:
        if not isinstance(entry, dict):
            continue
        uid = entry.get("uid")
        if not isinstance(uid, str) or not uid.strip():
            continue
        uid = uid.strip()
        if uid in seen:
            continue
        seen.add(uid)
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            name = uid
        blogs.append({"uid": uid, "name": name.strip()[:200]})
    if not blogs:
        raise MicroblogError(
            "Micro.blog config listed no usable blogs for this token."
        )
    return blogs


def create_post(
    token: str,
    content: str,
    destination: str = "",
    photo_url: str = "",
    photo_alt: str = "",
    name: str = "",
) -> dict:
    """Create an h=entry post via form-encoded Micropub.

    destination is the blog uid from list_destinations (mp-destination);
    when empty, Micro.blog posts to the account's default blog.
    photo_url is passed through as photo= so Micro.blog fetches and hosts
    the image itself; photo_alt becomes mp-photo-alt. name gives the post
    a title (blank for a normal microblog entry).

    Returns the parsed Location/JSON response. Micro.blog answers 201 or
    202 with a Location header on success; both are accepted.
    """
    if not content.strip():
        raise MicroblogError("Cannot post empty content to Micro.blog")

    data: dict[str, str] = {"h": "entry", "content": content}
    if destination:
        data["mp-destination"] = destination
    if photo_url:
        data["photo"] = photo_url
        if photo_alt:
            data["mp-photo-alt"] = photo_alt
    if name:
        data["name"] = name

    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            resp = client.post(
                MICROPUB_ENDPOINT,
                data=data,
                headers={"Authorization": _bearer(token)},
                follow_redirects=True,
            )
    except httpx.HTTPError as e:
        raise MicroblogError(f"Could not reach micro.blog: {e}") from e

    detail = _error_detail(resp)
    if _is_auth_rejection(resp.status_code):
        raise MicroblogAuthError(
            "Micro.blog rejected the app token. Reconnect the account."
        )
    if resp.status_code not in (200, 201, 202):
        raise MicroblogError(
            f"Micro.blog post failed (HTTP {resp.status_code})"
            + (f": {detail}" if detail else "")
        )

    result: dict = {}
    location = resp.headers.get("Location", "")
    if location:
        result["location"] = location
    try:
        body = resp.json()
        if isinstance(body, dict):
            result.update(body)
    except ValueError:
        pass
    return result


def test_connection(token: str) -> tuple[bool, str]:
    """Verify a token still works by querying config. For the Test button."""
    try:
        blogs = list_destinations(token)
    except MicroblogAuthError as e:
        return False, str(e)
    except MicroblogError as e:
        return False, str(e)
    names = ", ".join(b["name"] for b in blogs)
    return True, f"Token OK — can post to: {names}"
