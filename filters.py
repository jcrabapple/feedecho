"""Keyword filters for echo delivery.

Filters are comma-separated substrings, matched case-insensitively against an
item's title and summary/description. Two modes:

- exclude (default): item is skipped when ANY keyword matches
- include: item is skipped UNLESS at least one keyword matches
  (an empty include filter matches everything)
"""

from __future__ import annotations

import re

# Field-scoped clause: "tag:posse", "author:jane", "url:/notes/", "link:...",
# "content:...". Unknown prefixes (e.g. a legacy keyword like "release:2")
# stay bare keywords; a recognized prefix with an empty value ("tag:") also
# falls back to a bare keyword (legacy safety).
_FIELD_CLAUSE_RE = re.compile(r"^(tag|author|url|link|content):(.+)$")


def split_clause(part: str) -> tuple[str | None, str]:
    """Split a parsed keyword into a (field, value) clause or a bare keyword.

    Returns ``(field, value)`` for a recognized field-scoped clause (with a
    non-empty value), or ``(None, part)`` for a bare keyword.
    """
    m = _FIELD_CLAUSE_RE.match(part)
    if m and m.group(2).strip():
        return m.group(1), m.group(2).strip()
    return None, part


def parse_keywords(raw: str | None) -> list[str]:
    """Split a comma-separated filter string into trimmed, non-empty keywords."""
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _item_text(item: dict) -> str:
    parts = [
        item.get("title") or "",
        item.get("summary") or "",
        item.get("description") or "",
    ]
    return " ".join(parts).casefold()


def _field_text(item: dict, field: str) -> str:
    """Casefolded haystack for a field-scoped clause.

    NOTE: ``content:`` searches ``content`` and ``content_text`` only;
    ``content_html`` is deliberately NOT searched — its sanitized markup
    (tag names, attribute soup) can false-positive on ordinary keywords.
    """
    if field == "tag":
        tags_raw = item.get("tags") or []
        if isinstance(tags_raw, str):
            # Defensive: a malformed feed can deliver tags as a bare string;
            # treat it as one tag rather than iterating its characters.
            tags_raw = [tags_raw]
        tags = [str(t) for t in tags_raw if t]
        categories = item.get("categories")
        if isinstance(categories, dict):
            tags.extend(str(v) for v in categories.values() if v)
        return " ".join(tags).casefold()
    if field == "author":
        return (item.get("author") or "").casefold()
    if field in ("url", "link"):
        return (item.get("link") or "").casefold()
    # content
    parts = [item.get("content") or "", item.get("content_text") or ""]
    return " ".join(parts).casefold()


def _clause_matched(item: dict, field: str | None, value: str, bare_text: str) -> bool:
    if field is None:
        return value.casefold() in bare_text
    return value.casefold() in _field_text(item, field)


def match_reason(item: dict, keywords: str | None, mode: str | None) -> str | None:
    """Return a human-readable explanation if the item should be filtered, or None if kept.

    ``keywords`` is the raw comma-separated string stored on the echo.
    ``mode`` is 'exclude' or 'include' (anything else is treated as exclude).

    Keywords may be field-scoped (``tag:``, ``author:``, ``url:``/``link:``,
    ``content:``); anything else matches the item's title/summary/description.
    """
    parsed = parse_keywords(keywords)
    if not parsed:
        return None

    text = _item_text(item)
    clauses = [split_clause(kw) for kw in parsed]
    matched = [clause for clause in clauses if _clause_matched(item, clause[0], clause[1], text)]

    if mode == "include":
        if not matched:
            return "no include keyword matched"
        return None

    if matched:
        field, value = matched[0]
        if field:
            return f'matched {field} "{value}"'
        return f'matched "{value}"'
    return None


def is_filtered(item: dict, keywords: str | None, mode: str | None) -> bool:
    """Return True when the item should NOT be delivered for this filter.

    ``keywords`` is the raw comma-separated string stored on the echo.
    ``mode`` is 'exclude' or 'include' (anything else is treated as exclude).
    """
    return match_reason(item, keywords, mode) is not None
