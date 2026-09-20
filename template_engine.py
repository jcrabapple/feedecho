"""Template engine — render feed item data into post content with Jinja2.

Post templates are real, sandboxed Jinja2 templates. Everything the
original regex engine supported keeps working unchanged, plus:

  - Conditionals: {% if summary %}...{% else %}...{% endif %}
  - Filters: {{ title | truncate(120) }}, {{ author | default('Unknown') }}
  - Full item access: {{ item.title }}, {{ item['link'] }}
  - feed_name for the owning feed

Supported flat variables: {{ title }}, {{ link }}, {{ content_link }},
{{ summary }}, {{ content }}, {{ content_html }} (sanitized article HTML —
renders in webhook bodies and other markup-aware destinations; on Bluesky
it converts to clean text whose article links become clickable facets), {{ author }},
{{ date }}, {{ date_iso }}, {{ date_short }}, {{ tags }}, {{ hashtags }},
{{ image_url }}, {{ feed_name }}.

Templates are sandboxed: attribute access on unsafe objects and method
calls are blocked (use filters instead of methods), and templates cannot
reach the filesystem, imports, or Python builtins.
"""

import re
from datetime import datetime
from html.parser import HTMLParser

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


def _build_context(item: dict, feed_name: str = "", rich: bool = False) -> dict:
    """Build the Jinja2 context from a feed item dict.

    With ``rich=True`` (facet-aware destinations), ``content_html`` is
    converted to plain text where each http(s) anchor becomes its visible
    text wrapped in private-use marker pairs, and every other string field —
    including the embedded ``item`` dict — is scrubbed of that marker range,
    so only the converter can emit markers and feed content cannot forge a
    link facet.
    """
    date_str = item.get("date", "")
    context = {
        "title": item.get("title", ""),
        "link": item.get("link", ""),
        "summary": item.get("summary", ""),
        "content": item.get("content", ""),
        "content_link": item.get("content_link", ""),
        "content_html": item.get("content_html") or "",
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
    if rich:
        converted = _html_to_rich(item.get("content_html") or "")
        context["content_html"] = converted
        context["item"] = {
            key: _scrub_pua(value) for key, value in item.items()
        }
        context["item"]["content_html"] = converted
        for key, value in context.items():
            if key in ("content_html", "item"):
                continue
            context[key] = _scrub_pua(value)
    return context


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


# ── Rich rendering (facet-aware plain-text destinations) ─────────────────────
#
# Private-use-area markers wrap anchors recovered from content_html during
# rich rendering. Only _html_to_rich may emit them: render_template_rich
# scrubs the whole marker range from every other context field, so hostile
# feed content cannot forge a marker pair (which would linkify arbitrary
# text to an arbitrary URL — a phishing upgrade over the bare-URL facets
# build_facets already creates).
_RICH_START = "\ue000"
_RICH_SEP = "\ue001"
_RICH_END = "\ue002"
_PUA_RE = re.compile("[\ue000-\ue00f]")
_RICH_LINK_RE = re.compile(
    re.escape(_RICH_START) + r"(.*?)" + re.escape(_RICH_SEP)
    + r"(.*?)" + re.escape(_RICH_END),
    re.DOTALL,
)


def _scrub_pua(value):
    """Recursively strip the marker range from strings in nested structures.

    Lists (item["tags"], item["image_urls"]), dicts, and nested combinations
    all get scrubbed, so a hostile feed cannot smuggle marker codepoints into
    the rendered output through {{ tags | join(' ') }}, {{ item.tags[0] }}, etc.
    """
    if isinstance(value, str):
        return _PUA_RE.sub("", value)
    if isinstance(value, list):
        return [_scrub_pua(entry) for entry in value]
    if isinstance(value, dict):
        return {key: _scrub_pua(entry) for key, entry in value.items()}
    return value


class _RichHTMLParser(HTMLParser):
    """Convert sanitized HTML to text, wrapping http(s) anchors in markers.

    Mirrors feed_parser.html_to_text's output shape (block closes and <br>
    become newlines, <li> a bullet, whitespace normalized) so a rich render
    reads like the plain-text conversion users already expect. Anchors whose
    href is http(s) render as ``<MARK>text<SEP>url<END>``; other anchors
    (mailto, relative — the sanitizer already strips foreign schemes) keep
    their text only.
    """

    _BLOCK = {
        "p", "div", "section", "article", "blockquote", "pre",
        "h1", "h2", "h3", "h4", "h5", "h6", "li", "ul", "ol",
        "table", "tr", "figure", "figcaption",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._href: str | None = None
        self._anchor: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("br", "hr"):
            # Inside an anchor the newline belongs to the anchor text, not
            # the surrounding document — otherwise the marker pair would
            # only wrap the text after the break.
            if self._href is not None:
                self._anchor.append("\n")
            else:
                self.out.append("\n")
        elif tag == "li":
            if self._href is not None:
                self._anchor.append("\n• ")
            else:
                self.out.append("\n• ")
        elif tag == "a":
            self._href = dict(attrs).get("href")
            self._anchor = []

    def handle_endtag(self, tag):
        if tag == "a":
            text = re.sub(r"\s+", " ", "".join(self._anchor)).strip()
            href = (self._href or "").strip()
            if text and href and href.startswith(("http://", "https://")):
                text = _PUA_RE.sub("", text)
                href = _PUA_RE.sub("", href)
                if text and href:
                    self.out.append(_RICH_START + text + _RICH_SEP + href + _RICH_END)
            elif text:
                self.out.append(_PUA_RE.sub("", text))
            self._href = None
            self._anchor = []
        elif tag in self._BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if self._href is not None:
            self._anchor.append(data)
        else:
            self.out.append(_PUA_RE.sub("", data))

    def close(self):
        super().close()
        # An anchor left open at EOF (defensive: nh3 balances tags at ingest)
        # still flushes its collected text instead of dropping it.
        if self._href is not None or self._anchor:
            text = re.sub(r"\s+", " ", "".join(self._anchor)).strip()
            if text:
                self.out.append(_PUA_RE.sub("", text))
            self._href = None
            self._anchor = []


def _html_to_rich(html_str: str) -> str:
    """Sanitized article HTML -> plain text with marker-wrapped anchors."""
    if not isinstance(html_str, str) or not html_str:
        return ""
    parser = _RichHTMLParser()
    parser.feed(html_str)
    parser.close()
    text = "".join(parser.out)
    lines = [re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in text.split("\n")]
    out: list[str] = []
    blank = True
    for ln in lines:
        if not ln:
            if blank:
                continue
            out.append("")
            blank = True
            continue
        out.append(ln)
        blank = False
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def _extract_rich_links(rendered: str) -> tuple[str, list[tuple[int, int, str]]]:
    """Strip marker pairs from a rendered string, recovering link spans.

    Returns (text, links) where links are (char_start, char_end, uri)
    pointing at the anchor text's position in the returned text.
    """
    parts: list[str] = []
    links: list[tuple[int, int, str]] = []
    length = 0
    last = 0
    for match in _RICH_LINK_RE.finditer(rendered):
        chunk = rendered[last:match.start()]
        parts.append(chunk)
        length += len(chunk)
        anchor_text, uri = match.group(1), match.group(2)
        links.append((length, length + len(anchor_text), uri))
        parts.append(anchor_text)
        length += len(anchor_text)
        last = match.end()
    parts.append(rendered[last:])
    text = "".join(parts)
    if _PUA_RE.search(text):
        # A template filter sliced through a marker pair (e.g.
        # {{ content_html | truncate(50) }}), leaving dangling markers. The
        # surviving spans' positions are no longer trustworthy and the
        # markers must never leak into visible post text: scrub everything
        # and fall back to bare-URL facet detection only.
        return _PUA_RE.sub("", text), []
    return text, links


def render_template_rich(
    template: str, item: dict, feed_name: str = ""
) -> tuple[str, list[tuple[int, int, str]]]:
    """Render for a facet-aware plain-text destination (Bluesky).

    Like render_template, but ``content_html`` converts to plain text where
    each http(s) anchor becomes its visible text, recovered as a link span
    into the returned text. Other variables render identically to
    render_template. Returns (text, links) with links as
    (char_start, char_end, uri).

    Raises the same exceptions as render_template.
    """
    context = _build_context(item, feed_name, rich=True)
    result = env.from_string(_normalize(template or "")).render(**context)
    if len(result) > _MAX_OUTPUT:
        raise SecurityError(
            f"Template output is {len(result)} chars, over the "
            f"{_MAX_OUTPUT}-char cap"
        )
    return _extract_rich_links(result)


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
        {"var": "{{ content_html }}", "desc": "Full content as sanitized HTML (links/formatting kept; renders in webhook bodies; on Bluesky becomes clean text with clickable article links)"},
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
