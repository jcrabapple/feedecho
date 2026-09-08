"""Unit tests for utils.py (from the Tier-1 code-duplication audit)."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import utils


class FakeResponse:
    """Minimal httpx.Response stand-in for the utils helpers."""

    def __init__(self, body, status_code=200, headers=None):
        self._body = body
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self._body


class NotJsonResponse(FakeResponse):
    def json(self):
        raise ValueError("body is not JSON")


def test_utc_now_str_is_a_recent_utc_timestamp():
    ts = utils.utc_now_str()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", ts)
    stamp = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    assert abs((datetime.now(timezone.utc) - stamp).total_seconds()) < 5


def test_truncate_chars_short_text_untouched():
    assert utils.truncate_chars("hello", 10) == "hello"


def test_truncate_chars_exactly_at_limit_untouched():
    text = "a" * 10
    assert utils.truncate_chars(text, 10) is text


def test_truncate_chars_over_limit_strips_then_appends_ellipsis():
    assert utils.truncate_chars("a" * 100, 10) == "a" * 9 + "\u2026"
    # trailing whitespace inside the kept window is stripped first
    assert utils.truncate_chars("abcdef   tail", 10) == "abcdef\u2026"


def test_json_error_detail_first_key_wins():
    r = FakeResponse({"message": "boom"})
    assert utils.json_error_detail(r, "message", "error") == "boom"


def test_json_error_detail_falsy_first_key_falls_through():
    r = FakeResponse({"message": "", "error": "real"})
    assert utils.json_error_detail(r, "message", "error") == "real"


def test_json_error_detail_nonstring_truthy_value_blocks_later_keys():
    # faithful to the original `a or b` short-circuit: the first truthy
    # value is consumed even when it is not a string
    r = FakeResponse({"message": {"nested": 1}, "error": "nope"})
    assert utils.json_error_detail(r, "message", "error") == ""


def test_json_error_detail_non_dict_body_returns_empty():
    assert utils.json_error_detail(FakeResponse([1, 2, 3]), "message") == ""


def test_json_error_detail_unparseable_body_returns_empty():
    assert utils.json_error_detail(NotJsonResponse(None), "message") == ""


def test_json_error_detail_truncates_to_200_chars():
    r = FakeResponse({"message": "x" * 250})
    assert utils.json_error_detail(r, "message") == "x" * 200


def test_parse_retry_after_header_wins_over_body():
    r = FakeResponse({"retry_after": 5}, status_code=429, headers={"Retry-After": "17"})
    assert utils.parse_retry_after(r) == 17.0


def test_parse_retry_after_zero_in_body_is_preserved():
    # gate regression: the former discord.py copy returned 0.0 here, and
    # `or retryAfter` fallthrough would have dropped it to None
    r = FakeResponse({"retry_after": 0}, status_code=429)
    assert utils.parse_retry_after(r) == 0.0


def test_parse_retry_after_camelcase_key_fallback():
    r = FakeResponse({"retryAfter": 3.5}, status_code=429)
    assert utils.parse_retry_after(r) == 3.5


def test_parse_retry_after_nothing_present_returns_none():
    r = FakeResponse({"code": 1234}, status_code=429)
    assert utils.parse_retry_after(r) is None


def test_parse_retry_after_http_date_header():
    when = datetime.now(timezone.utc) + timedelta(seconds=120)
    r = FakeResponse(
        {"retry_after": 9},
        status_code=429,
        headers={"Retry-After": when.strftime("%a, %d %b %Y %H:%M:%S GMT")},
    )
    wait = utils.parse_retry_after(r)
    assert wait is not None and 110 <= wait <= 121


def test_parse_retry_after_unparseable_header_falls_through_to_body():
    r = FakeResponse(
        {"retry_after": 7}, status_code=429, headers={"Retry-After": "total-garbage"}
    )
    assert utils.parse_retry_after(r) == 7.0


def test_is_valid_email_strict_requires_dot_in_domain():
    assert utils.is_valid_email("user@example.com")
    assert not utils.is_valid_email("feedecho@localhost")
    assert not utils.is_valid_email("user@localhost")


def test_is_valid_email_loose_allows_bare_host():
    assert utils.is_valid_email("feedecho@localhost", require_domain_dot=False)


def test_is_valid_email_rejects_spaces_and_control_chars():
    assert not utils.is_valid_email("user name@example.com")
    assert not utils.is_valid_email("user@exam ple.com")
    assert not utils.is_valid_email("user@example.com\nX-Evil: 1")