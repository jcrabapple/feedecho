"""Template engine — render feed item data into post content with Jinja2.

Post templates are real, sandboxed Jinja2 templates. Everything the
original regex engine supported keeps working unchanged, plus:

  - Conditionals: {% if summary %}...{% else %}...{% endif %}
  - Filters: {{ title | truncate(120) }}, {{ author | default('Unknown') }}
  - Full item access: {{ item.title }}, {{ item['link'] }}
  - feed_name for the owning feed

Supported flat variables: {{ title }}, {{ link }}, {{ content_link }},
{{ summary }}, {{ content }}, {{ author }}, {{ date }}, {{ date_iso }},
{{ date_short }}, {{ tags }}, {{ hashtags }}, {{ image_url }}, {{ feed_name }}.

Templates are sandboxed: attribute access on unsafe objects and method
calls are blocked (use filters instead of methods), and templates cannot
reach the filesystem, imports, or Python builtins.
"""

import re
from datetime import datetime

from jinja2 import TemplateSyntaxError
from jinja2.exceptions import SecurityError
from jinja2.sandbox import SandboxedEnvironment

# A post is at most a few KB (Mastodon 500, Bluesky 300, Matrix 32K, email a
# few KB). These caps are deliberately generous for real templates while
# stopping the exponential-blowup DoS: `{{ 'A' * 50000000 }}` renders a 50 MB
# string in ~0.2s, and `{% set a = 'A' * 100000 %}{{ a * 100000 }}` requests
# 10 GB. The multiplication is evaluated before any output cap could catch it,
# so it must be bounded at the operator.
_MAX_REPEAT = 100_000    # max chars/items a single `*` may produce
_MAX_OUTPUT = 1_000_000  # max rendered output chars (backstop)


class CappedSandbox(SandboxedEnvironment):
    """SandboxedEnvironment that also bounds sequence repetition (`*`)."""

    intercepted_binops = frozenset(["*"])

    def call_binop(self, context, operator, left, right):
        if operator == "*":
            self._guard_repeat(left, right)
        return super().call_binop(context, operator, left, right)

    def _guard_repeat(self, left, right) -> None:
        """Reject `seq * n` that would allocate more than _MAX_REPEAT items.

        Checked before the operator runs, so the oversized result is never
        allocated. ``n < 0`` and ``n == 0`` produce an empty sequence and are
        safe.
        """
        for seq, n in ((left, right), (right, left)):
            if isinstance(seq, (str, bytes, list, tuple)) and isinstance(n, int):
                if n < 0:
                    return
                if n and len(seq) * n > _MAX_REPEAT:
                    raise SecurityError(
                        "Template uses `*` to build an oversized value "
                        f"({len(seq)} items repeated {n} times)"
                    )
                return


# Post content is plain text (Mastodon/Bluesky statuses, email bodies),
# never HTML — no autoescaping.
env = CappedSandbox(autoescape=False)

# Jinja2 identifiers cannot contain colons, but the original engine exposed
# {{ date:iso }} and {{ date:short }}. Normalize those two tokens before
# compilation so every existing stored template keeps rendering.
_LEGACY_DATE_TOKENS = {
    "date:iso": "date_iso",
    "date:short": "date_short",
}

# Only rewrite inside {{ ... }} expressions. A blanket str.replace would
# also mangle string literals and plain template prose that mentions the
# token text (e.g. "format: date:iso").
_EXPRESSION_RE = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)


def _format_date(date_str: str | None, fmt: str) -> str:
    """Format a date string using the given format string."""
    if not date_str:
        return ""
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.strftime(fmt)
    except (ValueError, TypeError):
        return date_str


def _format_hashtags(tags) -> str:
    """Format a list of tags as hashtag string."""
    if not tags:
        return ""
    hashtags = []
    for tag in tags:
        clean = re.sub(r"[^a-zA-Z0-9]", "", str(tag))
        if clean:
            hashtags.append(f"#{clean}")
    return " ".join(hashtags)


def _normalize(template: str) -> str:
    """Rewrite legacy colon date tokens into Jinja2-safe identifiers.

    Only inside ``{{ ... }}`` expressions, leaving string literals and
    plain template text untouched.
    """

    def _fix_expression(match: re.Match) -> str:
        inner = match.group(1)
        # Quoted literals are left alone: rewriting inside them changed
        # {{ item["date:short"] }} into a lookup of a different key, and
        # {{ "date:iso" }} into a variable reference.
        if '"' in inner or "'" in inner:
            return match.group(0)
        for old, new in _LEGACY_DATE_TOKENS.items():
            inner = inner.replace(old, new)
        return "{{" + inner + "}}"

    return _EXPRESSION_RE.sub(_fix_expression, template)


def _build_context(item: dict, feed_name: str = "") -> dict:
    """Build the Jinja2 context from a feed item dict."""
    date_str = item.get("date", "")
    return {
        "title": item.get("title", ""),
        "link": item.get("link", ""),
        "summary": item.get("summary", ""),
        "content": item.get("content", ""),
        "content_link": item.get("content_link", ""),
        "author": item.get("author", ""),
        "date": date_str,
        "date_iso": _format_date(date_str, "%Y-%m-%dT%H:%M:%S"),
        "date_short": _format_date(date_str, "%Y-%m-%d"),
        "tags": item.get("tags", []) or [],
        "hashtags": _format_hashtags(item.get("tags", [])),
        "image_url": item.get("image_url", ""),
        "feed_name": feed_name or "",
        # Full item dict for power users: {{ item.title }}, {{ item['link'] }}
        "item": item,
    }


def render_template(template: str, item: dict, feed_name: str = "") -> str:
    """Render a template string with feed item data.

    Args:
        template: Jinja2 template string with {{ variable }} placeholders
        item: Feed item dict from feed_parser
        feed_name: Optional name of the owning feed ({{ feed_name }})

    Returns:
        Rendered string ready for posting.

    Raises:
        jinja2.TemplateSyntaxError on malformed templates.
        jinja2.exceptions.SecurityError on sandbox violations or oversized output.
    """
    context = _build_context(item, feed_name)
    result = env.from_string(_normalize(template or "")).render(**context)
    if len(result) > _MAX_OUTPUT:
        raise SecurityError(
            f"Template output is {len(result)} chars, over the "
            f"{_MAX_OUTPUT}-char cap"
        )
    return result


def validate_template(template: str) -> None:
    """Raise TemplateSyntaxError if the template cannot be parsed.

    Used by the echo create/edit handlers to reject bad templates at
    save time instead of marking posts gave_up later.
    """
    env.parse(_normalize(template or ""))


def available_variables() -> list[dict]:
    """Return description of available template variables for UI display."""
    return [
        {"var": "{{ title }}", "desc": "Post title"},
        {"var": "{{ link }}", "desc": "Post URL"},
        {"var": "{{ content_link }}", "desc": "First link inside the post content (link-blogs)"},
        {"var": "{{ summary }}", "desc": "Post summary/excerpt"},
        {"var": "{{ content }}", "desc": "Full post content (HTML cleaned)"},
        {"var": "{{ author }}", "desc": "Author name"},
        {"var": "{{ date }}", "desc": "Publication date (raw)"},
        {"var": "{{ date_iso }}", "desc": "ISO 8601 date (2024-01-15T09:30:00)"},
        {"var": "{{ date_short }}", "desc": "Short date (2024-01-15)"},
        {"var": "{{ tags }}", "desc": "Raw tag list"},
        {"var": "{{ hashtags }}", "desc": "Feed tags as #hashtags"},
        {"var": "{{ image_url }}", "desc": "First image URL from the item"},
        {"var": "{{ image_alt }}", "desc": "The image's alt text from the feed (may be empty)"},
        {"var": "{{ feed_name }}", "desc": "Name of the source feed"},
        {"var": "{{ item.title }}", "desc": "Any item field via the item dict"},
    ]
