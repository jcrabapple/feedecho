"""Keyword filters for echo delivery.

Filters are comma-separated substrings, matched case-insensitively against an
item's title and summary/description. Two modes:

- exclude (default): item is skipped when ANY keyword matches
- include: item is skipped UNLESS at least one keyword matches
  (an empty include filter matches everything)
"""

from __future__ import annotations


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


def match_reason(item: dict, keywords: str | None, mode: str | None) -> str | None:
    """Return a human-readable explanation if the item should be filtered, or None if kept.

    ``keywords`` is the raw comma-separated string stored on the echo.
    ``mode`` is 'exclude' or 'include' (anything else is treated as exclude).
    """
    parsed = parse_keywords(keywords)
    if not parsed:
        return None

    text = _item_text(item)
    matched_kws = [kw for kw in parsed if kw.casefold() in text]

    if mode == "include":
        if not matched_kws:
            return "no include keyword matched"
        return None

    if matched_kws:
        return f'matched "{matched_kws[0]}"'
    return None


def is_filtered(item: dict, keywords: str | None, mode: str | None) -> bool:
    """Return True when the item should NOT be delivered for this filter.

    ``keywords`` is the raw comma-separated string stored on the echo.
    ``mode`` is 'exclude' or 'include' (anything else is treated as exclude).
    """
    return match_reason(item, keywords, mode) is not None
