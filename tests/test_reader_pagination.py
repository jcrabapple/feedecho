"""Tests for reader keyset pagination (Phase 0.2)."""

import pytest
from fastapi.testclient import TestClient

import database
import settings
from app import app, _reader_keyset


@pytest.fixture
def pagination_env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MULTI", False)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "reader-pagination.db")
    database.init_db()
    import scheduler

    monkeypatch.setattr(scheduler, "fetch_feed", lambda url: {"items": []})
    with database.get_db() as db:
        db.execute(
            "INSERT INTO feeds (name, url, read_enabled) VALUES (?, ?, 1)",
            ("PageFeed", "https://example.com/pagefeed"),
        )
    return settings


def test_reader_keyset_helper():
    # Invalid / empty cursors
    assert _reader_keyset("") == ("", [])
    assert _reader_keyset(None) == ("", [])
    assert _reader_keyset("garbage") == ("", [])
    assert _reader_keyset("notadate|notanid") == ("", [])

    # Valid cursor with date
    sql, params = _reader_keyset("2026-01-01 12:00:00|42")
    assert "i.published_at IS NULL" in sql
    assert "i.published_at < ?" in sql
    assert params == ["2026-01-01 12:00:00", "2026-01-01 12:00:00", 42]

    # Valid cursor with empty date (NULL-dated block)
    sql_null, params_null = _reader_keyset("|42")
    assert "i.published_at IS NULL AND i.id < ?" in sql_null
    assert params_null == [42]


def test_reader_keyset_walk_120_items(pagination_env):
    """Seed 120 items: dated, duplicate timestamps, and NULL dates.

    Walk pages via after= cursor, asserting:
      (a) no duplicates across pages
      (b) no gaps
      (c) full set recovered
      (d) NULL-dated block lands last
      (e) garbage cursor falls back safely to page 1
    """
    total_items = 120
    # 100 dated items (some duplicate timestamps) + 20 NULL-dated items
    with database.get_db() as db:
        for i in range(100):
            # Duplicate some timestamps
            minute = i // 2
            pub = f"2026-01-01 00:{minute:02d}:00"
            db.execute(
                "INSERT INTO feed_items (feed_id, item_id, title, published_at, is_read)"
                " VALUES (1, ?, ?, ?, 0)",
                (f"item-{i:03d}", f"Dated Item {i}", pub),
            )
        for i in range(100, total_items):
            db.execute(
                "INSERT INTO feed_items (feed_id, item_id, title, published_at, is_read)"
                " VALUES (1, ?, ?, NULL, 0)",
                (f"item-{i:03d}", f"Null-date Item {i}"),
            )

    client = TestClient(app)

    # (e) Garbage cursor -> page 1 (does not 500, returns 50 items)
    r_bad = client.get("/reader?view=all&after=bad-cursor-value")
    assert r_bad.status_code == 200
    assert r_bad.text.count('class="reader-entry"') == 50

    # Walk pages
    collected_ids = []
    collected_item_ids = []
    cursor = ""
    page_count = 0

    while True:
        url = "/reader?view=all"
        if cursor:
            url += f"&after={cursor}"
        resp = client.get(url)
        assert resp.status_code == 200
        page_count += 1

        # Extract item ids and next cursor from response HTML
        import re

        entry_ids = [int(m) for m in re.findall(r'data-item-id="(\d+)"', resp.text)]
        # Filter only entry ids from the reader item list (avoiding any false matches)
        # Unique preserve order
        seen_in_page = []
        for eid in entry_ids:
            if eid not in seen_in_page:
                seen_in_page.append(eid)

        assert len(seen_in_page) <= 50
        collected_ids.extend(seen_in_page)

        # Check for next cursor
        match = re.search(r'data-next-cursor="([^"]+)"', resp.text)
        if match:
            cursor = match.group(1)
        else:
            break

    # (c) Full set recovered
    assert len(collected_ids) == total_items
    # (a) No duplicates across pages
    assert len(set(collected_ids)) == total_items

    # Check order: verify in DB that collected_ids matches the exact order of:
    # ORDER BY (published_at IS NULL), published_at DESC, id DESC
    with database.get_db() as db:
        expected_rows = db.execute(
            "SELECT id, published_at FROM feed_items WHERE feed_id = 1"
            " ORDER BY (published_at IS NULL), published_at DESC, id DESC"
        ).fetchall()
        expected_ids = [r["id"] for r in expected_rows]

    # (b) No gaps, exact order
    assert collected_ids == expected_ids

    # (d) NULL-dated block lands last
    null_start_index = next(
        idx for idx, eid in enumerate(collected_ids) if eid > 100
    )
    # The first 100 items must be dated, the last 20 NULL-dated
    assert null_start_index == 100


def test_reader_load_more_fetch_fragment(pagination_env):
    """X-Requested-With: fetch returns only reader_items.html fragment."""
    with database.get_db() as db:
        for i in range(10):
            db.execute(
                "INSERT INTO feed_items (feed_id, item_id, title, published_at, is_read)"
                " VALUES (1, ?, ?, '2026-01-01 00:00:00', 0)",
                (f"frag-{i}", f"Frag {i}"),
            )

    client = TestClient(app)
    resp = client.get("/reader?view=all", headers={"X-Requested-With": "fetch"})
    assert resp.status_code == 200
    # Must NOT contain base.html layout chrome
    assert "<!DOCTYPE html>" not in resp.text
    assert "<nav" not in resp.text
    assert "<footer" not in resp.text
    # Must contain reader entries
    assert 'class="reader-entry"' in resp.text
