"""Tests for shared.cookie_utils — parse/build cookie functions."""

from __future__ import annotations

from shared.cookie_utils import (
    build_cookie_str,
    extract_cookie_value,
    parse_cookie_str,
    parse_set_cookie_headers,
)


class TestParseCookieStr:
    """parse_cookie_str: "k1=v1; k2=v2" → dict."""

    def test_empty_string(self):
        assert parse_cookie_str("") == {}

    def test_single_cookie(self):
        assert parse_cookie_str("a1=abc123") == {"a1": "abc123"}

    def test_multiple_cookies(self):
        assert parse_cookie_str("a1=abc123; webid=xyz789") == {"a1": "abc123", "webid": "xyz789"}

    def test_with_spaces(self):
        assert parse_cookie_str("  a1=abc  ;  b2=def  ") == {"a1": "abc", "b2": "def"}

    def test_malformed_pair_skipped(self):
        """Entries without '=' are silently dropped."""
        assert parse_cookie_str("a1=abc; justtext; b2=def") == {"a1": "abc", "b2": "def"}


class TestBuildCookieStr:
    """build_cookie_str: dict → "k1=v1; k2=v2"."""

    def test_empty_dict(self):
        assert build_cookie_str({}) == ""

    def test_single(self):
        assert build_cookie_str({"a1": "abc"}) == "a1=abc"

    def test_multiple(self):
        result = build_cookie_str({"a1": "abc", "webid": "xyz"})
        # Order is preserved in dict (Python 3.7+); split to avoid order dependency
        parts = result.split("; ")
        assert "a1=abc" in parts
        assert "webid=xyz" in parts

    def test_roundtrip(self):
        d = {"a1": "abc123", "b2": "def456"}
        assert parse_cookie_str(build_cookie_str(d)) == d


class TestParseSetCookieHeaders:
    """parse_set_cookie_headers: raw Set-Cookie values → flat dict."""

    def test_empty_list(self):
        assert parse_set_cookie_headers([]) == {}

    def test_single_header(self):
        headers = ["session_id=abc123; Path=/; HttpOnly"]
        assert parse_set_cookie_headers(headers) == {"session_id": "abc123"}

    def test_multiple_headers(self):
        headers = [
            "session_id=abc123; Path=/",
            "refresh_token=xyz789; Path=/; Max-Age=86400",
        ]
        result = parse_set_cookie_headers(headers)
        assert result["session_id"] == "abc123"
        assert result["refresh_token"] == "xyz789"

    def test_malformed_spaces_around_equals(self):
        """Fallback regex handles XHS-style malformed Set-Cookie."""
        headers = ["a1 = abc123 ; Path=/; Domain=.xiaohongshu.com"]
        result = parse_set_cookie_headers(headers)
        assert result["a1"] == "abc123"

    def test_stdlib_preferred(self):
        """SimpleCookie handles quoted values correctly."""
        headers = ['session="hello%20world"; Path=/']
        result = parse_set_cookie_headers(headers)
        # SimpleCookie strips quotes but does NOT URL-decode %20
        assert result["session"] == "hello%20world"


class TestExtractCookieValue:
    """extract_cookie_value: str|dict × name → value."""

    def test_from_dict_found(self):
        assert extract_cookie_value({"a1": "abc", "b2": "def"}, "a1") == "abc"

    def test_from_dict_missing(self):
        assert extract_cookie_value({"b2": "def"}, "a1") == ""

    def test_from_str_found(self):
        assert extract_cookie_value("a1=abc; b2=def", "a1") == "abc"

    def test_from_str_missing(self):
        assert extract_cookie_value("b2=def", "a1") == ""

    def test_from_empty_str(self):
        assert extract_cookie_value("", "a1") == ""
