"""Tests for platforms.xiaohongshu.signer — Node.js subprocess signing."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from platforms.xiaohongshu.signer import _SIGN_WRAPPER, _VENDOR_DIR, _check_node, get_xhs_sign

# ═══════════════════════════════════════════════════════════════
# _check_node()
# ═══════════════════════════════════════════════════════════════


class TestCheckNode:
    """Tests for _check_node()."""

    def test_node_exists(self):
        """Returns True when Node.js is on PATH."""
        with patch("shutil.which", return_value="/usr/bin/node"):
            assert _check_node() is True

    def test_node_missing(self):
        """Returns False when Node.js is not on PATH."""
        with patch("shutil.which", return_value=None):
            assert _check_node() is False


# ═══════════════════════════════════════════════════════════════
# get_xhs_sign()
# ═══════════════════════════════════════════════════════════════


class TestGetXhsSign:
    """Tests for get_xhs_sign()."""

    API = "/api/sns/web/v1/login/qrcode/create"
    VALID_RESULT = {"xs": "xs_value", "xt": 12345, "xs_common": "common_value"}

    def test_success(self):
        """Returns correct dict with xs, xt, xs_common on success."""
        with (
            patch("shutil.which", return_value="/usr/bin/node"),
            patch("subprocess.run") as mock_run,
            patch.object(Path, "exists", return_value=True),
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(self.VALID_RESULT),
                stderr="",
            )

            result = get_xhs_sign(self.API, data={"qrcode_id": "abc"}, a1="a1_token")

            assert result == {
                "xs": "xs_value",
                "xt": "12345",  # xt is always converted to str
                "xs_common": "common_value",
            }

    def test_success_with_empty_data_and_a1(self):
        """Works with empty data and empty a1."""
        with (
            patch("shutil.which", return_value="/usr/bin/node"),
            patch("subprocess.run") as mock_run,
            patch.object(Path, "exists", return_value=True),
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(self.VALID_RESULT),
                stderr="",
            )

            result = get_xhs_sign(self.API)

            assert result == {
                "xs": "xs_value",
                "xt": "12345",
                "xs_common": "common_value",
            }

    def test_method_get(self):
        """Passes 'GET' as method argument to subprocess."""
        with (
            patch("shutil.which", return_value="/usr/bin/node"),
            patch("subprocess.run") as mock_run,
            patch.object(Path, "exists", return_value=True),
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(self.VALID_RESULT),
                stderr="",
            )

            get_xhs_sign(self.API, data={"a": "1"}, method="GET")

            args = mock_run.call_args[0][0]
            assert args[0] == "node"
            assert str(_SIGN_WRAPPER) in str(args[1])
            assert args[2] == self.API
            assert args[3] == "GET"

    def test_subprocess_input_json(self):
        """stdin payload is correct JSON with a1 and data."""
        with (
            patch("shutil.which", return_value="/usr/bin/node"),
            patch("subprocess.run") as mock_run,
            patch.object(Path, "exists", return_value=True),
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(self.VALID_RESULT),
                stderr="",
            )

            get_xhs_sign(self.API, data={"k": "v"}, a1="my_a1")

            stdin_str = mock_run.call_args[1]["input"]
            stdin_data = json.loads(stdin_str)
            assert stdin_data == {"a1": "my_a1", "data": {"k": "v"}}

    def test_subprocess_cwd_set_to_vendor_dir(self):
        """cwd is set to _VENDOR_DIR."""
        with (
            patch("shutil.which", return_value="/usr/bin/node"),
            patch("subprocess.run") as mock_run,
            patch.object(Path, "exists", return_value=True),
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(self.VALID_RESULT),
                stderr="",
            )

            get_xhs_sign(self.API)

            assert mock_run.call_args[1]["cwd"] == str(_VENDOR_DIR)

    def test_missing_node_raises(self):
        """Raises RuntimeError when Node.js is not installed."""
        with patch("shutil.which", return_value=None):
            with pytest.raises(RuntimeError, match="Node.js is required"):
                get_xhs_sign(self.API)

    def test_missing_wrapper_raises(self):
        """Raises RuntimeError when sign_wrapper.js is missing."""
        with patch("shutil.which", return_value="/usr/bin/node"), patch.object(Path, "exists", return_value=False):
            with pytest.raises(RuntimeError, match="Sign wrapper not found"):
                get_xhs_sign(self.API)

    def test_nonzero_return_code_with_json_stderr(self):
        """Raises RuntimeError with error message from JSON stderr."""
        with (
            patch("shutil.which", return_value="/usr/bin/node"),
            patch("subprocess.run") as mock_run,
            patch.object(Path, "exists", return_value=True),
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout="",
                stderr='{"error": "invalid a1 token"}',
            )

            with pytest.raises(RuntimeError, match="invalid a1 token"):
                get_xhs_sign(self.API)

    def test_nonzero_return_code_with_plain_text_stderr(self):
        """Raises RuntimeError with raw stderr when it's not valid JSON."""
        with (
            patch("shutil.which", return_value="/usr/bin/node"),
            patch("subprocess.run") as mock_run,
            patch.object(Path, "exists", return_value=True),
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout="",
                stderr="Node.js runtime error\n",
            )

            with pytest.raises(RuntimeError, match="Node.js runtime error"):
                get_xhs_sign(self.API)

    def test_nonzero_return_code_with_empty_stderr(self):
        """Raises RuntimeError with exit code message when stderr is empty/blank."""
        with (
            patch("shutil.which", return_value="/usr/bin/node"),
            patch("subprocess.run") as mock_run,
            patch.object(Path, "exists", return_value=True),
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=2,
                stdout="",
                stderr="",
            )

            with pytest.raises(RuntimeError, match="exit code 2"):
                get_xhs_sign(self.API)

    def test_invalid_json_stdout_raises(self):
        """Raises RuntimeError when stdout is not valid JSON."""
        with (
            patch("shutil.which", return_value="/usr/bin/node"),
            patch("subprocess.run") as mock_run,
            patch.object(Path, "exists", return_value=True),
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="not json here",
                stderr="",
            )

            with pytest.raises(RuntimeError, match="Invalid sign output"):
                get_xhs_sign(self.API)

    def test_result_missing_keys_default_to_empty(self):
        """Missing keys in output default to empty string or '0'."""
        with (
            patch("shutil.which", return_value="/usr/bin/node"),
            patch("subprocess.run") as mock_run,
            patch.object(Path, "exists", return_value=True),
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps({"xs": "only_xs"}),
                stderr="",
            )

            result = get_xhs_sign(self.API)

            assert result == {"xs": "only_xs", "xt": "", "xs_common": ""}
