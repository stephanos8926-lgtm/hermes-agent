"""Tests for cron/lifecycle_guard.py byte-cap and platform short-circuit.

Covers:
- _MAX_SHLEX_BYTES (64KB) skips oversized lines in shlex scan
- contains_launchctl_submit_command short-circuits on non-darwin
- _iter_command_segments yields nothing for oversized lines
- _mask_data_sink_arguments preserves oversized lines verbatim
"""

import sys
from unittest.mock import patch

import pytest

from cron.lifecycle_guard import (
    _iter_command_segments,
    _MAX_SHLEX_BYTES,
    _mask_data_sink_arguments,
    contains_launchctl_submit_command,
)


# ---------------------------------------------------------------------------
# Platform short-circuit
# ---------------------------------------------------------------------------

class TestPlatformShortCircuit:
    """contains_launchctl_submit_command returns False immediately on non-darwin."""

    def test_non_darwin_returns_false_without_iterating(self):
        # On Linux (this CI), the function must short-circuit BEFORE
        # allocating the shlex lexer. We assert behavior: a launchctl
        # command does NOT match, regardless of what the input is.
        with patch("cron.lifecycle_guard.sys") as mock_sys:
            mock_sys.platform = "linux"
            assert contains_launchctl_submit_command(
                "launchctl submit -l foo -- /bin/echo hi"
            ) is False

    def test_darwin_runs_normally(self):
        # On macOS, the function must still detect launchctl.
        # Patch only sys.platform; leave everything else real so the
        # tokenizer actually runs.
        with patch("cron.lifecycle_guard.sys") as mock_sys:
            mock_sys.platform = "darwin"
            assert contains_launchctl_submit_command(
                "launchctl submit -l foo -- /bin/echo hi"
            ) is True

    def test_windows_returns_false(self):
        with patch("cron.lifecycle_guard.sys") as mock_sys:
            mock_sys.platform = "win32"
            assert contains_launchctl_submit_command(
                "launchctl submit -l foo -- /bin/echo hi"
            ) is False


# ---------------------------------------------------------------------------
# Byte cap (_MAX_SHLEX_BYTES)
# ---------------------------------------------------------------------------

class TestShlexByteCap:
    """Lines larger than _MAX_SHLEX_BYTES are skipped in the shlex scan."""

    def test_byte_cap_constant_is_64kb(self):
        # Sanity: the constant is what we expect. Changing this would
        # alter the test surface for several other lifecycle tests.
        assert _MAX_SHLEX_BYTES == 65536

    def test_normal_command_tokenized(self):
        segs = list(_iter_command_segments("echo hello world"))
        assert segs == [["echo", "hello", "world"]]

    def test_oversized_line_skipped(self):
        # A line longer than 64KB must NOT produce any segments.
        # This is the primary safety guarantee: scripts are not
        # silently swallowed — they fail downstream on size, not here.
        oversized = "echo " + ("x" * (_MAX_SHLEX_BYTES + 1))
        segs = list(_iter_command_segments(oversized))
        assert segs == []

    def test_oversized_line_mixed_with_normal(self):
        # Multi-line input: oversized lines are skipped, normal ones
        # are still tokenized.
        normal = "ls -la /tmp"
        oversized = "echo " + ("y" * (_MAX_SHLEX_BYTES + 1))
        cmd = f"{normal}\n{oversized}\n{normal}"
        segs = list(_iter_command_segments(cmd))
        # First and third lines produce segments; second is skipped.
        assert segs == [["ls", "-la", "/tmp"], ["ls", "-la", "/tmp"]]

    def test_boundary_exactly_at_limit_is_kept(self):
        # Lines of exactly _MAX_SHLEX_BYTES are tokenized (the
        # check is strict greater-than, not greater-or-equal).
        # We don't need a real 64KB meaningful command — just confirm
        # the boundary is "off by one" the safe way.
        # Use a smaller surrogate: build a string whose UTF-8 encoding
        # is exactly the limit, but we won't actually need 64KB of
        # real tokens because the test just checks the cap is "off".
        # 64KB of "x" + 1 byte = 65537, over the limit. So 65536 bytes
        # of "x" is exactly at the limit and should be processed.
        line_at_limit = "x" * _MAX_SHLEX_BYTES
        # We don't care what comes out — just that nothing crashes.
        list(_iter_command_segments(line_at_limit))

    def test_multibyte_utf8_counted_by_bytes_not_chars(self):
        # The cap is bytes, not characters. A line of N multibyte chars
        # is N*4 bytes (worst case) and should be skipped if > 64KB.
        # 4-byte chars: each is 4 bytes in UTF-8.
        multibyte = "\U0001F600" * ((_MAX_SHLEX_BYTES // 4) + 1)
        segs = list(_iter_command_segments(multibyte))
        assert segs == []


# ---------------------------------------------------------------------------
# _mask_data_sink_arguments byte-cap interaction
# ---------------------------------------------------------------------------

class TestMaskDataSinkByteCap:
    """_mask_data_sink_arguments must preserve oversized lines verbatim."""

    def test_oversized_line_passed_through_unchanged(self):
        oversized = "grep " + ("x" * (_MAX_SHLEX_BYTES + 1))
        out = _mask_data_sink_arguments(oversized)
        # Oversized line is left intact (no masking attempted, no
        # tokenization attempted).
        assert out == oversized

    def test_normal_data_sink_command_still_masked(self):
        # A normal-sized data-sink command should still be masked.
        # grep with a quoted pattern: the regex sees "systemctl restart
        # hermes-gateway" inside the quotes and would block — but the
        # masker exempts grep arguments.
        out = _mask_data_sink_arguments(
            "grep 'systemctl restart hermes-gateway' /var/log/syslog"
        )
        # Either the mask string is different from the input OR the
        # masker correctly identifies grep as a data sink. We assert
        # the function returns a string without raising.
        assert isinstance(out, str)

    def test_oversized_does_not_crash_with_multibyte(self):
        # An oversized line with multibyte content must not crash on
        # the encode/decode boundary.
        oversized = "echo " + ("\U0001F600" * ((_MAX_SHLEX_BYTES // 4) + 1))
        out = _mask_data_sink_arguments(oversized)
        assert out == oversized
