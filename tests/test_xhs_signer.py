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
# get_xhs_sign() — full 7-key header set
# ═══════════════════════════════════════════════════════════════


class TestGetXhsSign:
    """get_xhs_sign returns the complete 7-key header set."""

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
            result = get_xhs_sign("/api/sns/web/v2/user/me", a1="a1", method="GET")
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
        """Keys are returned verbatim (hyphenated), not remapped."""
        with patch.object(_xhs, "sign_headers", return_value=dict(self.SAMPLE_HEADERS)):
            result = get_xhs_sign("/api/x", a1="a1", method="POST")
        # hyphenated keys preserved verbatim
        assert result["x-s"] == "XYS_abcdef"
        assert result["x-s-common"] == "CmnABC"

    def test_default_method_is_post(self):
        """Omitting method defaults to POST, payload path is taken."""
        with patch.object(_xhs, "sign_headers", return_value=dict(self.SAMPLE_HEADERS)) as m:
            get_xhs_sign("/api/x", data={"k": "v"}, a1="a1")
        assert m.call_args.kwargs["method"] == "POST"
        assert m.call_args.kwargs["payload"] == {"k": "v"}
        assert m.call_args.kwargs["params"] is None

    def test_empty_data_and_a1_pass_empty_cookies(self):
        """Empty data + empty a1 → empty cookies string, payload/params None."""
        with patch.object(_xhs, "sign_headers", return_value=dict(self.SAMPLE_HEADERS)) as m:
            get_xhs_sign("/api/x")
        assert m.call_args.kwargs["cookies"] == ""
        assert m.call_args.kwargs["payload"] is None
        assert m.call_args.kwargs["params"] is None

    def test_cookies_string_built_from_a1(self):
        """a1 cookie is composed into ``a1=<value>`` for xhshow."""
        with patch.object(_xhs, "sign_headers", return_value=dict(self.SAMPLE_HEADERS)) as m:
            get_xhs_sign("/api/x", a1="my_a1_value")
        assert m.call_args.kwargs["cookies"] == "a1=my_a1_value"

    def test_post_payload_passed_through(self):
        """POST with dict data → payload field of sign_headers."""
        with patch.object(_xhs, "sign_headers", return_value=dict(self.SAMPLE_HEADERS)) as m:
            get_xhs_sign("/api/x", data={"k": "v"}, a1="a1", method="POST")
        assert m.call_args.kwargs["payload"] == {"k": "v"}
        assert m.call_args.kwargs["params"] is None

    def test_get_with_query_string_data(self):
        """GET with string data → parsed via parse_qs into params dict,
        each value unwrapped from the single-element list to a scalar
        (signer._sign convention)."""
        with patch.object(_xhs, "sign_headers", return_value=dict(self.SAMPLE_HEADERS)) as m:
            get_xhs_sign("/api/x", data="k1=v1&k2=v2", a1="a1", method="GET")
        params = m.call_args.kwargs["params"]
        assert params["k1"] == "v1"
        assert params["k2"] == "v2"
        assert m.call_args.kwargs["payload"] is None

    def test_get_with_dict_data(self):
        """GET with dict data → params field, passed through as-is."""
        with patch.object(_xhs, "sign_headers", return_value=dict(self.SAMPLE_HEADERS)) as m:
            get_xhs_sign("/api/x", data={"k": "v"}, a1="a1", method="GET")
        assert m.call_args.kwargs["params"] == {"k": "v"}
        assert m.call_args.kwargs["payload"] is None

    def test_uri_always_passed(self):
        """api arg → uri field of sign_headers, verbatim."""
        with patch.object(_xhs, "sign_headers", return_value=dict(self.SAMPLE_HEADERS)) as m:
            get_xhs_sign("/api/sns/web/v1/feed", a1="a1")
        assert m.call_args.kwargs["uri"] == "/api/sns/web/v1/feed"


# ═══════════════════════════════════════════════════════════════
# Real xhshow integration (no mocks) — verifies signing actually runs
# ═══════════════════════════════════════════════════════════════


class TestXhshowIntegration:
    """End-to-end signing via real xhshow library (no server contact)."""

    def test_real_post_signing_returns_nonempty_values(self):
        result = get_xhs_sign("/api/sns/web/v1/login/qrcode/create", data={"x": 1}, a1="1900000000000abc")
        assert result["x-s"]
        assert result["x-s"].startswith(("XYS_", "XYW_"))
        assert result["x-t"]
        assert result["x-s-common"]

    def test_real_get_signing_returns_nonempty_values(self):
        result = get_xhs_sign(
            "/api/sns/web/v1/user_posted",
            data="num=20&cursor=&user_id=abc",
            a1="1900000000000abc",
            method="GET",
        )
        assert result["x-s"]
        assert result["x-s"].startswith(("XYS_", "XYW_"))
        assert result["x-t"]
        assert result["x-s-common"]

    def test_real_full_signing_returns_seven_keys(self):
        result = get_xhs_sign("/api/sns/web/v2/user/me", a1="abc", method="GET")
        assert "x-b3-traceid" in result
        assert "x-xray-traceid" in result
        assert "x-mns" in result
        assert "xy-direction" in result

    def test_real_get_and_post_produce_different_xs(self):
        """GET and POST to the same URI must produce different x-s (different content strings)."""
        post = get_xhs_sign("/api/sns/web/v1/feed", data={"k": "v"}, a1="abc", method="POST")
        get = get_xhs_sign("/api/sns/web/v1/feed", data="k=v", a1="abc", method="GET")
        assert post["x-s"] != get["x-s"]
