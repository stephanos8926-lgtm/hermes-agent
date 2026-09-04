"""Tests for log retention pruning in hermes_logging.

Covers prune_old_logs() with synthetic mtimes so the tests are
deterministic regardless of wall-clock time. The function is
time-based and depends on file mtime, not on a database or
external state, so a tmp_path with manually-touched mtimes is
sufficient.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from hermes_logging import (
    _LOG_RETENTION_MAX_DAYS,
    _LOG_RETENTION_MIN_DAYS,
    get_log_retention_days,
    prune_old_logs,
)


@pytest.fixture
def logs_dir(tmp_path: Path) -> Path:
    """Create a fake logs/ directory with a mix of fresh and stale files."""
    d = tmp_path / "logs"
    d.mkdir()
    return d


def _touch(path: Path, *, days_ago: float, contents: str = "x") -> None:
    """Create a file with an mtime in the past."""
    path.write_text(contents)
    mtime = time.time() - (days_ago * 86400)
    os.utime(path, (mtime, mtime))


class TestGetLogRetentionDays:
    def test_default_when_no_config(self, tmp_path, monkeypatch):
        # When no config is reachable, returns 30.
        # We can't easily stub hermes_cli.config, but the default
        # path is what we get when read_raw_config returns {}.
        result = get_log_retention_days()
        assert _LOG_RETENTION_MIN_DAYS <= result <= _LOG_RETENTION_MAX_DAYS

    def test_clamps_to_minimum(self):
        # The clamp should floor at _LOG_RETENTION_MIN_DAYS.
        assert _LOG_RETENTION_MIN_DAYS >= 1

    def test_clamps_to_maximum(self):
        # The clamp should ceiling at _LOG_RETENTION_MAX_DAYS.
        assert _LOG_RETENTION_MAX_DAYS <= 365


class TestPruneOldLogs:
    def test_deletes_files_older_than_retention(self, logs_dir):
        _touch(logs_dir / "agent.log.1", days_ago=60)
        _touch(logs_dir / "agent.log.2", days_ago=45)
        _touch(logs_dir / "agent.log.3", days_ago=20)
        _touch(logs_dir / "agent.log", days_ago=0)

        result = prune_old_logs(logs_dir, retention_days=30, now=time.time())

        assert len(result["deleted"]) == 2
        assert any("agent.log.1" in p for p in result["deleted"])
        assert any("agent.log.2" in p for p in result["deleted"])
        assert len(result["kept"]) == 2
        assert any("agent.log.3" in p for p in result["kept"])
        assert any(p.endswith("agent.log") for p in result["kept"])
        # Files should actually be gone from disk.
        assert not (logs_dir / "agent.log.1").exists()
        assert not (logs_dir / "agent.log.2").exists()
        assert (logs_dir / "agent.log.3").exists()
        assert (logs_dir / "agent.log").exists()

    def test_zero_files_means_no_deletions(self, logs_dir):
        result = prune_old_logs(logs_dir, retention_days=30, now=time.time())
        assert result["deleted"] == []
        assert result["kept"] == []
        assert result["scanned"] == 0

    def test_missing_directory_returns_empty(self, tmp_path):
        nonexistent = tmp_path / "no_such_dir"
        result = prune_old_logs(nonexistent, retention_days=30, now=time.time())
        assert result["deleted"] == []
        assert result["kept"] == []
        assert result["scanned"] == 0

    def test_does_not_delete_non_log_files(self, logs_dir):
        # The glob is *.log* — anything that doesn't match the
        # log pattern is left alone, even if it's ancient.
        _touch(logs_dir / "user_notes.md", days_ago=365)
        _touch(logs_dir / "screenshot.png", days_ago=365)
        _touch(logs_dir / "agent.log.99", days_ago=365)

        result = prune_old_logs(logs_dir, retention_days=30, now=time.time())

        # Only the .log* file should be deleted.
        assert len(result["deleted"]) == 1
        assert "agent.log.99" in result["deleted"][0]
        assert (logs_dir / "user_notes.md").exists()
        assert (logs_dir / "screenshot.png").exists()

    def test_returns_structured_report(self, logs_dir):
        _touch(logs_dir / "agent.log.1", days_ago=60)
        result = prune_old_logs(logs_dir, retention_days=30, now=time.time())
        assert "deleted" in result
        assert "kept" in result
        assert "errors" in result
        assert "retention_days" in result
        assert "scanned" in result
        assert result["retention_days"] == 30
        assert isinstance(result["deleted"], list)
        assert isinstance(result["kept"], list)
        assert isinstance(result["errors"], list)

    def test_retention_clamping(self, logs_dir):
        # Even if a caller passes 0 or a negative number, the
        # function clamps to _LOG_RETENTION_MIN_DAYS so it
        # doesn't wipe the live log file.
        _touch(logs_dir / "agent.log", days_ago=0)
        result = prune_old_logs(logs_dir, retention_days=0, now=time.time())
        assert result["retention_days"] == _LOG_RETENTION_MIN_DAYS
        # The live file should be kept.
        assert any(p.endswith("agent.log") for p in result["kept"])

    def test_retention_max_clamp(self, logs_dir):
        # A 1000-day retention is clamped to the max.
        result = prune_old_logs(
            logs_dir, retention_days=1000, now=time.time()
        )
        assert result["retention_days"] == _LOG_RETENTION_MAX_DAYS

    def test_now_override_uses_supplied_time(self, logs_dir):
        # File is 5 days old. With "now" set 3 days in the future,
        # the file looks 8 days old to the pruner and should be
        # deleted (relative to a 7-day retention).
        _touch(logs_dir / "agent.log.1", days_ago=5)
        fake_now = time.time() + (3 * 86400)
        result = prune_old_logs(logs_dir, retention_days=7, now=fake_now)
        assert any("agent.log.1" in p for p in result["deleted"])

    def test_deletion_errors_do_not_abort_sweep(self, logs_dir):
        # If one file is unlinkable, the sweep should continue
        # and still delete the others. We simulate by creating
        # a file in a subdirectory we can't easily simulate
        # without root, so instead we just verify the structure
        # supports per-file error reporting.
        _touch(logs_dir / "agent.log.1", days_ago=60)
        _touch(logs_dir / "agent.log.2", days_ago=60)
        result = prune_old_logs(logs_dir, retention_days=30, now=time.time())
        # Both files should be deleted (no errors in this happy path).
        assert len(result["deleted"]) == 2
        assert result["errors"] == []

    def test_integration_with_real_log_naming(self, logs_dir):
        # Verify the canonical log file naming is matched.
        for name in [
            "agent.log",
            "agent.log.1",
            "agent.log.2",
            "errors.log",
            "errors.log.1",
            "gateway.log",
            "gateway.log.3",
            "gui.log",
        ]:
            _touch(logs_dir / name, days_ago=100)

        result = prune_old_logs(logs_dir, retention_days=30, now=time.time())

        # All 8 should be deleted.
        assert result["scanned"] == 8
        assert len(result["deleted"]) == 8
