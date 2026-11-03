"""GitHub destination — write each echo as a markdown file into a repository.

One connected account is one target repository (owner/repo + branch). Each
echo renders its template as the file body and writes it to a per-echo path
template, so a site, microblog, or link log lands in a repo as markdown.
Parity target is EchoFeed's GitHub service: token + repo + branch +
committer, with file path and commit message defined per echo.

Security:

- Fixed host ``https://api.github.com`` — no user-supplied base URL, so no
  new SSRF surface. All calls are sync ``httpx`` with
  ``DEFAULT_REQUEST_TIMEOUT``.
- The token is a credential: encrypted at rest by the caller
  (``security.encrypt_secret``), never rendered back in the UI, never
  interpolated into errors or logs. Error strings are static.
- Path traversal is rejected, not silently repaired: a path that reaches a
  ``..`` came from a bad template and must fail loudly.

Error mapping: 401, and 403 that is not a rate limit, are
``GitHubAuthError`` (permanent); 404 repo/branch is ``GitHubNotFoundError``
(permanent); 403/429 with ``x-ratelimit-remaining: 0`` or a Retry-After is
``GitHubRateLimitError`` (transient); 409/422 is ``GitHubRejectedError``
(permanent); every other 5xx/3xx is ``GitHubError`` (transient).
"""

from __future__ import annotations

import base64
import logging
import re
from typing import Any
from urllib.parse import quote

import httpx

import template_engine
from utils import (
    DEFAULT_REQUEST_TIMEOUT,
    DestinationAuthError,
    DestinationError,
    DestinationNotFoundError,
    DestinationRateLimitError,
    parse_retry_after,
)

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = DEFAULT_REQUEST_TIMEOUT

API_BASE = "https://api.github.com"

# Encoded-body cap: over the cap is a PERMANENT rejection — truncating a
# backup file silently corrupts it, so the item gives up instead.
MAX_FILE_BYTES = 1_000_000

# A path template renders to a repo-relative path: per-segment 255 chars,
# total 1024. Generous for real blogs, tight enough to stop abuse.
MAX_PATH_TOTAL = 1024
MAX_PATH_SEGMENT = 255

MAX_COMMIT_MESSAGE_CHARS = 200

DEFAULT_COMMIT_TEMPLATE = "Add {{ title }}"

# Sample item for connect-time echo validation: deliberately nasty (a title
# with a slash and a colon) so a naive `posts/{{ title }}.md` path fails AT
# SAVE TIME with a pointer to the `| slug` filter instead of permanently
# failing on the first real item.
SAMPLE_ITEM: dict = {
    "id": "sample-item",
    "title": "Feed Echo: test/demo post",
    "link": "https://example.com/posts/1",
    "summary": "Line one\nLine two",
    "content": "",
    "content_link": "",
    "author": "",
    "date": "2026-01-15T09:30:00",
    "tags": ["news"],
    "image_url": "",
    "image_alt": "",
}


class GitHubError(DestinationError):
    """Base error for GitHub delivery (transient unless a subclass says so)."""


class GitHubRateLimitError(GitHubError, DestinationRateLimitError):
    """403/429 rate limit. Transient; carries the server's suggested wait."""

    def __init__(self, retry_after: float | None = None):
        self.retry_after = retry_after
        msg = "GitHub rate limit hit"
        if retry_after is not None:
            msg += f" (retry after {retry_after:.0f}s)"
        super().__init__(msg)


class GitHubAuthError(GitHubError, DestinationAuthError):
    """401, or 403 that is not a rate limit — token rejected or lacks the
    Contents: Read and write permission."""


class GitHubNotFoundError(GitHubError, DestinationNotFoundError):
    """404 — the repository or branch no longer exists (or the token cannot
    see it)."""


class GitHubRejectedError(GitHubError):
    """409/422 — GitHub refused the write (protected branch, invalid payload).
    Permanent: retrying the same request cannot change the outcome."""


def normalize_token(raw: str) -> str:
    """Validate a GitHub token and return it stripped.

    Shape checks only (length, no whitespace) — the live ``connect()`` call
    is the real credential check. Raises ValueError with a user-facing
    message.
    """
    value = (raw or "").strip()
    if not value:
        raise ValueError("Enter a GitHub personal access token.")
    if len(value) < 20:
        raise ValueError("GitHub tokens are at least 20 characters.")
    if len(value) > 200:
        raise ValueError("GitHub tokens are shorter than 200 characters.")
    if any(ch.isspace() for ch in value):
        raise ValueError("GitHub tokens cannot contain whitespace.")
    return value


def parse_repo(raw: str) -> str:
    """Normalize a repository reference to lowercase ``owner/repo``.

    Accepts ``owner/repo``, ``https://github.com/owner/repo``, with or
    without a ``.git`` suffix or trailing slash. Rejects anything else with
    a user-facing ValueError.
    """
    value = (raw or "").strip()
    if not value:
        raise ValueError("Enter the repository as owner/repo.")
    lowered = value.lower()
    for prefix in (
        "https://github.com/", "http://github.com/", "github.com/",
    ):
        if lowered.startswith(prefix):
            value = value[len(prefix):]
            break
    else:
        if lowered.startswith("git@github.com:"):
            value = value[len("git@github.com:"):]
    value = value.strip("/")
    if value.lower().endswith(".git"):
        value = value[:-4].strip("/")
    parts = value.split("/")
    if (
        len(parts) != 2
        or not parts[0]
        or not parts[1]
        or any(any(c.isspace() for c in p) for p in parts)
        or any(any(c in p for c in "?#:@%") for p in parts)
    ):
        raise ValueError(
            "Repository must look like owner/repo"
            " (or the https://github.com/owner/repo URL)."
        )
    return "/".join(parts).lower()


def normalize_path(rendered: str) -> str:
    """Validate a rendered repo-relative path and return the stored form.

    Normalizes ``\\`` to ``/`` and collapses ``//``; rejects (rather than
    silently repairs) an empty result, an absolute path, ``.``/``..``
    segments, control characters, a segment over 255 chars, or a total over
    1024 chars. Appends ``.md`` when the last segment has no extension.
    """
    if not rendered or not rendered.strip():
        raise ValueError("GitHub file path is empty.")
    if any(ord(c) < 32 or ord(c) == 127 for c in rendered):
        raise ValueError("GitHub file path contains a control character.")
    path = rendered.replace("\\", "/")
    while "//" in path:
        path = path.replace("//", "/")
    if path.startswith("/"):
        raise ValueError("GitHub file path must be relative, not absolute.")
    segments = path.split("/")
    for seg in segments:
        if seg in (".", ".."):
            raise ValueError(
                "GitHub file path must not contain . or .. segments."
            )
        if len(seg) > MAX_PATH_SEGMENT:
            raise ValueError(
                f"GitHub file path segment is over {MAX_PATH_SEGMENT} characters."
            )
    if len(path) > MAX_PATH_TOTAL:
        raise ValueError(
            f"GitHub file path is over {MAX_PATH_TOTAL} characters."
        )
    last = segments[-1]
    if "." not in last:
        path += ".md"
    return path


def render_path(template: str, item: Any, feed_name: str = "") -> str:
    """Render a path template for one item and validate the result.

    ``item`` is the flat item dict the scheduler passes (a FeedItem maps
    fine). Raises template_engine exceptions on render failure and
    ValueError on path validation failure — both are permanent at dispatch.
    """
    rendered = template_engine.render_template(template, item, feed_name=feed_name)
    return normalize_path(rendered)


def build_commit_message(
    template: str, item: Any, feed_name: str = "", path: str = ""
) -> str:
    """Render the commit-message template, collapse whitespace, cap at 200.

    A blank stored template falls back to ``Add {{ title }}``; a render that
    comes out empty falls back to the path, so a commit always has a body.
    """
    tpl = (template or "").strip() or DEFAULT_COMMIT_TEMPLATE
    try:
        rendered = template_engine.render_template(tpl, item, feed_name=feed_name)
    except Exception:
        # A stored commit template can drift out of sync with the engine
        # (or the user hand-edited the DB). A commit message is decoration,
        # not content — fall back to the path rather than fail the delivery.
        rendered = ""
    rendered = re.sub(r"\s+", " ", rendered).strip()
    if not rendered:
        rendered = re.sub(r"\s+", " ", path).strip() or "Update file"
    return rendered[:MAX_COMMIT_MESSAGE_CHARS]


def _encode_content(content: str) -> str:
    """base64-encode the file body, enforcing the size cap BEFORE any network
    call. Over the cap is a permanent rejection."""
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    if len(encoded) > MAX_FILE_BYTES:
        raise GitHubRejectedError(
            f"File is over the {MAX_FILE_BYTES}-byte GitHub size cap"
            " and cannot be delivered without truncation."
        )
    return encoded


def _quote_path(path: str) -> str:
    """Percent-encode a repo-relative path per segment.

    ``quote(path, safe='/')`` is not enough for ``#``/``?`` — those would
    start a fragment/query inside the URL — so each segment is encoded with
    an empty safe set and the slashes are re-joined.
    """
    return "/".join(quote(seg, safe="") for seg in path.split("/"))


def _rate_limit_error(response) -> GitHubRateLimitError | None:
    """Map a 403/429 to a rate-limit error, or None when it is a real auth
    refusal (403 with budget remaining and no Retry-After)."""
    remaining = response.headers.get("x-ratelimit-remaining")
    if remaining is not None:
        try:
            if int(remaining) == 0:
                return GitHubRateLimitError(parse_retry_after(response))
        except ValueError:
            pass
    retry_after = parse_retry_after(response)
    if retry_after is not None:
        return GitHubRateLimitError(retry_after)
    if response.status_code == 429:
        return GitHubRateLimitError(None)
    return None


def _raise_api_error(response, *, not_found_is_create: bool = False) -> None:
    """Map a non-2xx Contents/Repo API response to the typed error ladder.

    Error strings are static: they never include the response body (which
    can echo the request) and never include the token.
    """
    status = response.status_code
    if status in (403, 429):
        rate_limited = _rate_limit_error(response)
        if rate_limited is not None:
            raise rate_limited
        raise GitHubAuthError(
            "GitHub rejected the token or it lacks the"
            " \"Contents: Read and write\" permission."
        )
    if status == 401:
        raise GitHubAuthError("GitHub rejected that token.")
    if status == 404:
        if not_found_is_create:
            return  # caller: no file yet -> create
        raise GitHubNotFoundError(
            "GitHub repository or branch not found (or the token cannot see it)."
        )
    if status in (409, 422):
        raise GitHubRejectedError(
            f"GitHub refused the write (HTTP {status}); the branch may be"
            " protected or the payload invalid."
        )
    if 300 <= status < 400:
        raise GitHubError(
            f"GitHub redirected the request (HTTP {status});"
            " redirects are not followed"
        )
    raise GitHubError(f"GitHub request failed (HTTP {status})")


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def connect(token: str, repo: str, branch: str) -> dict:
    """Validate token + repo + branch with live reads; return account info.

    ``GET /repos/{repo}`` proves the token and visibility (a ``push: false``
    permission is refused up front — connect-time validation is mandatory
    because a token that saves but cannot write fails at 6am inside the
    scheduler where nobody sees it), then ``GET /repos/{repo}/branches/
    {branch}`` proves the branch exists.

    Raises ValueError with a user-facing message on refusal, GitHubError on
    transient network/5xx failure.
    """
    token = normalize_token(token)
    repo = parse_repo(repo)
    branch = (branch or "").strip()
    if not branch:
        raise ValueError("Enter the branch name (for example main).")

    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        resp = client.get(f"{API_BASE}/repos/{repo}", headers=_headers(token))
        if resp.status_code == 401:
            raise ValueError("GitHub rejected that token.")
        if resp.status_code == 404:
            raise ValueError(
                "Repository not found, or this token cannot see it."
            )
        if resp.status_code != 200:
            _raise_api_error(resp)
        repo_info = resp.json()

        perms = repo_info.get("permissions") or {}
        if perms.get("push") is False:
            raise ValueError(
                "This token can read the repository but cannot push to it."
                ' Give it the "Contents: Read and write" permission.'
            )

        default_branch = repo_info.get("default_branch") or "main"
        full_name = repo_info.get("full_name") or repo

        resp = client.get(
            f"{API_BASE}/repos/{repo}/branches/{quote(branch, safe='')}",
            headers=_headers(token),
        )
        if resp.status_code == 404:
            raise ValueError(
                f"Branch '{branch}' was not found in {full_name}."
                f" The repository's default branch is '{default_branch}'."
            )
        if resp.status_code != 200:
            _raise_api_error(resp)

    return {
        "full_name": full_name,
        "private": bool(repo_info.get("private")),
        "default_branch": default_branch,
        "branch": branch,
        "permissions": perms,
    }


def read_branch_head(token: str, repo: str, branch: str) -> str:
    """Re-run the branch read; return the head commit sha. Backs the Test
    button and internal checks. Raises the typed error ladder."""
    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        resp = client.get(
            f"{API_BASE}/repos/{repo}/branches/{quote(branch, safe='')}",
            headers=_headers(token),
        )
    if resp.status_code != 200:
        _raise_api_error(resp)
    data = resp.json()
    commit = data.get("commit") or {}
    return commit.get("sha") or ""


def test_connection(token: str, repo: str, branch: str) -> tuple[bool, str]:
    """The accounts-page Test button: re-read the branch, report the head."""
    try:
        sha = read_branch_head(token, repo, branch)
    except GitHubRateLimitError as e:
        return False, str(e)
    except GitHubAuthError as e:
        return False, str(e)
    except GitHubNotFoundError as e:
        return False, str(e)
    except GitHubRejectedError as e:
        return False, str(e)
    except GitHubError as e:
        return False, str(e)
    return True, f"GitHub repository is reachable (branch head {sha[:7]})."


def get_file_sha(token: str, repo: str, branch: str, path: str) -> str | None:
    """GET the file's current blob sha, or None when it does not exist yet.

    The GET is idempotent: it may run before the dispatch claim guard.
    """
    quoted = _quote_path(path)
    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        resp = client.get(
            f"{API_BASE}/repos/{repo}/contents/{quoted}?ref={quote(branch, safe='')}",
            headers=_headers(token),
        )
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        _raise_api_error(resp)
    data = resp.json()
    if isinstance(data, dict):
        return data.get("sha") or None
    return None


def put_file(
    token: str,
    repo: str,
    branch: str,
    path: str,
    content: str,
    message: str,
    sha: str | None = None,
    committer: dict | None = None,
) -> dict:
    """PUT one file (create or update). Returns {sha, html_url, created}.

    The size cap is enforced BEFORE any network call: over the cap is a
    permanent rejection, and truncating a backup file silently corrupts it.
    """
    encoded = _encode_content(content)
    body: dict = {
        "message": message,
        "content": encoded,
        "branch": branch,
    }
    if sha:
        body["sha"] = sha
    if committer:
        body["committer"] = committer
    quoted = _quote_path(path)
    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        resp = client.put(
            f"{API_BASE}/repos/{repo}/contents/{quoted}",
            headers=_headers(token),
            json=body,
        )
    if resp.status_code not in (200, 201):
        _raise_api_error(resp)
    data = resp.json()
    commit = data.get("commit") or {}
    return {
        "sha": commit.get("sha") or data.get("sha") or "",
        "html_url": data.get("content", {}).get("html_url")
        or data.get("commit", {}).get("html_url")
        or "",
        "created": resp.status_code == 201,
    }


def build_committer(name: str, email: str) -> dict | None:
    """The committer block for the Contents API, or None to commit as the
    token's own user. Both fields are required by GitHub, so a half-filled
    pair falls back to the token identity rather than a guaranteed 422."""
    name = (name or "").strip()
    email = (email or "").strip()
    if name and email:
        return {"name": name, "email": email}
    return None


def send_file(
    token: str,
    repo: str,
    branch: str,
    path: str,
    content: str,
    message: str,
    committer: dict | None = None,
) -> dict:
    """The full two-step Contents API round trip: read the blob sha (create
    vs update), then PUT. Returns {sha, html_url, created}."""
    sha = get_file_sha(token, repo, branch, path)
    return put_file(
        token, repo, branch, path, content, message,
        sha=sha, committer=committer,
    )
