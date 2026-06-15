"""Tests for platforms.xiaohongshu.signer — pure-Python xhshow signing.

Spike result (2026-06-15, xhshow 0.2.0):
- sign_headers(method, uri, cookies, params|payload) works for both GET and POST
- Returns 7 headers: x-s, x-t, x-s-common, x-b3-traceid, x-mns, x-xray-traceid, xy-direction
- No a3_hash workaround needed (fixed upstream in 0.2.0)
"""

# pyright: basic
from __future__ import annotations

from unittest.mock import patch

from platforms.xiaohongshu.signer import (
    _xhs,
    get_xhs_sign,
    get_xhs_sign_full,
)

# ═══════════════════════════════════════════════════════════════
# xhshow availability
# ═══════════════════════════════════════════════════════════════


class TestXhshowAvailable:
    """The signer module loads xhshow at import time."""

    def test_xhs_instance_exists(self):
        """Module-level _xhs is a real Xhshow instance."""
        from xhshow import Xhshow

        assert isinstance(_xhs, Xhshow)


# ═══════════════════════════════════════════════════════════════
# get_xhs_sign() — short-form 3-key contract (backward compat)
# ═══════════════════════════════════════════════════════════════


class TestGetXhsSign:
    """get_xhs_sign returns {xs, xt, xs_common} short-form keys."""

    API = "/api/sns/web/v1/login/qrcode/create"
    SAMPLE_HEADERS = {
        "x-s": "XYS_abcdef123",
        "x-t": "1700000000000",
        "x-s-common": "CmnABC",
        "x-b3-traceid": "b3tid",
        "x-mns": "mns_val",
        "x-xray-traceid": "xray_tid",
        "xy-direction": "dy",
    }

    def test_returns_three_short_keys_post(self):
        """POST returns dict with exactly xs, xt, xs_common."""
        with patch.object(_xhs, "sign_headers", return_value=dict(self.SAMPLE_HEADERS)):
            result = get_xhs_sign(self.API, data={"qrcode_id": "abc"}, a1="a1_token", method="POST")
        assert set(result.keys()) == {"xs", "xt", "xs_common"}
        assert result["xs"] == "XYS_abcdef123"
        assert result["xt"] == "1700000000000"
        assert result["xs_common"] == "CmnABC"

    def test_returns_three_short_keys_get(self):
        """GET also returns the 3-key short form."""
        with patch.object(_xhs, "sign_headers", return_value=dict(self.SAMPLE_HEADERS)):
            result = get_xhs_sign("/api/sns/web/v1/user_posted", data="num=20", a1="a1_token", method="GET")
        assert set(result.keys()) == {"xs", "xt", "xs_common"}

    def test_default_method_is_post(self):
        """Default method is POST."""
        with patch.object(_xhs, "sign_headers", return_value=dict(self.SAMPLE_HEADERS)) as mock_sign:
            get_xhs_sign(self.API, data={"k": "v"}, a1="a1")
        # Inspect the method= passed to xhshow
        _, kwargs = mock_sign.call_args
        assert kwargs["method"] == "POST"

    def test_empty_data_and_a1(self):
        """Works with empty data and empty a1."""
        with patch.object(_xhs, "sign_headers", return_value=dict(self.SAMPLE_HEADERS)) as mock_sign:
            result = get_xhs_sign(self.API)
        assert result == {"xs": "XYS_abcdef123", "xt": "1700000000000", "xs_common": "CmnABC"}
        _, kwargs = mock_sign.call_args
        assert kwargs["cookies"] == ""

    def test_cookies_string_built_from_a1(self):
        """cookies argument to xhshow is 'a1=<value>' when a1 is provided."""
        with patch.object(_xhs, "sign_headers", return_value=dict(self.SAMPLE_HEADERS)) as mock_sign:
            get_xhs_sign(self.API, a1="my_a1_value")
        _, kwargs = mock_sign.call_args
        assert kwargs["cookies"] == "a1=my_a1_value"

    def test_post_payload_passed_through(self):
        """POST with dict data passes it as `payload` to xhshow."""
        with patch.object(_xhs, "sign_headers", return_value=dict(self.SAMPLE_HEADERS)) as mock_sign:
            get_xhs_sign(self.API, data={"k": "v"}, a1="a1", method="POST")
        _, kwargs = mock_sign.call_args
        assert kwargs["payload"] == {"k": "v"}
        assert kwargs["params"] is None

    def test_get_with_query_string_data(self):
        """GET with str data parses it into params dict for xhshow."""
        with patch.object(_xhs, "sign_headers", return_value=dict(self.SAMPLE_HEADERS)) as mock_sign:
            get_xhs_sign("/api/sns/web/v1/user_posted", data="num=20&cursor=abc", a1="a1", method="GET")
        _, kwargs = mock_sign.call_args
        assert kwargs["params"] == {"num": "20", "cursor": "abc"}
        assert kwargs["payload"] is None

    def test_get_with_dict_data(self):
        """GET with dict data passes it directly as params."""
        with patch.object(_xhs, "sign_headers", return_value=dict(self.SAMPLE_HEADERS)) as mock_sign:
            get_xhs_sign(
                "/api/sns/web/v1/user_posted",
                data={"num": "20", "user_id": "u1"},
                a1="a1",
                method="GET",
            )
        _, kwargs = mock_sign.call_args
        assert kwargs["params"] == {"num": "20", "user_id": "u1"}

    def test_uri_always_passed(self):
        """api arg is forwarded as uri to xhshow."""
        with patch.object(_xhs, "sign_headers", return_value=dict(self.SAMPLE_HEADERS)) as mock_sign:
            get_xhs_sign("/api/sns/web/v2/user/me", a1="a1", method="GET")
        _, kwargs = mock_sign.call_args
        assert kwargs["uri"] == "/api/sns/web/v2/user/me"


# ═══════════════════════════════════════════════════════════════
# get_xhs_sign_full() — full 7-key set for new code
# ═══════════════════════════════════════════════════════════════


class TestGetXhsSignFull:
    """get_xhs_sign_full returns the complete 7-key header set."""

    SAMPLE_HEADERS = {
        "x-s": "XYS_abcdef",
        "x-t": "1700000000000",
        "x-s-common": "CmnABC",
        "x-b3-traceid": "b3tid",
        "x-mns": "mns_val",
        "x-xray-traceid": "xray_tid",
        "xy-direction": "dy",
    }

    def test_returns_full_header_set(self):
        """Returns all 7 headers xhshow produces."""
        with patch.object(_xhs, "sign_headers", return_value=dict(self.SAMPLE_HEADERS)):
            result = get_xhs_sign_full("/api/sns/web/v2/user/me", a1="a1", method="GET")
        assert set(result.keys()) == {
            "x-s",
            "x-t",
            "x-s-common",
            "x-b3-traceid",
            "x-mns",
            "x-xray-traceid",
            "xy-direction",
        }

    def test_passthrough_no_key_remapping(self):
        """Full variant does NOT remap to short-form keys."""
        with patch.object(_xhs, "sign_headers", return_value=dict(self.SAMPLE_HEADERS)):
            result = get_xhs_sign_full("/api/x", a1="a1", method="POST")
        # hyphenated keys preserved verbatim
        assert result["x-s"] == "XYS_abcdef"
        assert result["x-s-common"] == "CmnABC"


# ═══════════════════════════════════════════════════════════════
# Real xhshow integration (no mocks) — verifies signing actually runs
# ═══════════════════════════════════════════════════════════════


class TestXhshowIntegration:
    """End-to-end signing via real xhshow library (no server contact)."""

    def test_real_post_signing_returns_nonempty_values(self):
        result = get_xhs_sign("/api/sns/web/v1/login/qrcode/create", data={"x": 1}, a1="1900000000000abc")
        assert result["xs"]
        assert result["xs"].startswith(("XYS_", "XYW_"))
        assert result["xt"]
        assert result["xs_common"]

    def test_real_get_signing_returns_nonempty_values(self):
        result = get_xhs_sign(
            "/api/sns/web/v1/user_posted",
            data="num=20&cursor=&user_id=abc",
            a1="1900000000000abc",
            method="GET",
        )
        assert result["xs"]
        assert result["xs"].startswith(("XYS_", "XYW_"))
        assert result["xt"]
        assert result["xs_common"]

    def test_real_full_signing_returns_seven_keys(self):
        result = get_xhs_sign_full("/api/sns/web/v2/user/me", a1="abc", method="GET")
        assert "x-b3-traceid" in result
        assert "x-xray-traceid" in result
        assert "x-mns" in result
        assert "xy-direction" in result

    def test_real_get_and_post_produce_different_xs(self):
        """GET and POST to the same URI must produce different x-s (different content strings)."""
        post = get_xhs_sign("/api/sns/web/v1/feed", data={"k": "v"}, a1="abc", method="POST")
        get = get_xhs_sign("/api/sns/web/v1/feed", data="k=v", a1="abc", method="GET")
        assert post["xs"] != get["xs"]
