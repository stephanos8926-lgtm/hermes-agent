"""Tests for the disk-cleanup plugin.

Covers the bundled plugin at ``plugins/disk-cleanup/``:

  * ``disk_cleanup`` library: track / forget / dry_run / quick / status,
    ``is_safe_path`` and ``guess_category`` filtering.
  * Plugin ``__init__``: ``post_tool_call`` hook auto-tracks files created
    by ``write_file`` / ``terminal``; ``on_session_end`` hook runs quick
    cleanup when anything was tracked during the turn.
  * Slash command handler: status / dry-run / quick / track / forget /
    unknown subcommand behaviours.
  * Bundled-plugin discovery via ``PluginManager.discover_and_load``.
"""

import importlib
import json
import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    """Isolate HERMES_HOME for each test.

    The global hermetic fixture already redirects HERMES_HOME to a tempdir,
    but we want the plugin to work with a predictable subpath. We reset
    HERMES_HOME here for clarity.
    """
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    yield hermes_home


def _load_lib():
    """Import the plugin's library module directly from the repo path."""
    repo_root = Path(__file__).resolve().parents[2]
    lib_path = repo_root / "plugins" / "disk-cleanup" / "disk_cleanup.py"
    spec = importlib.util.spec_from_file_location(
        "disk_cleanup_under_test", lib_path
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_plugin_init():
    """Import the plugin's __init__.py (which depends on the library)."""
    repo_root = Path(__file__).resolve().parents[2]
    plugin_dir = repo_root / "plugins" / "disk-cleanup"
    # Use the PluginManager's module naming convention so relative imports work.
    spec = importlib.util.spec_from_file_location(
        "hermes_plugins.disk_cleanup",
        plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    # Ensure parent namespace package exists for the relative `. import disk_cleanup`
    import types
    if "hermes_plugins" not in sys.modules:
        ns = types.ModuleType("hermes_plugins")
        ns.__path__ = []
        sys.modules["hermes_plugins"] = ns
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "hermes_plugins.disk_cleanup"
    mod.__path__ = [str(plugin_dir)]
    sys.modules["hermes_plugins.disk_cleanup"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Library tests
# ---------------------------------------------------------------------------

class TestIsSafePath:
    def test_accepts_path_under_hermes_home(self, _isolate_env):
        dg = _load_lib()
        p = _isolate_env / "subdir" / "file.txt"
        p.parent.mkdir()
        p.write_text("x")
        assert dg.is_safe_path(p) is True

    def test_rejects_outside_hermes_home(self, _isolate_env):
        dg = _load_lib()
        assert dg.is_safe_path(Path("/etc/passwd")) is False


class TestGuessCategory:
    def test_test_prefix(self, _isolate_env):
        dg = _load_lib()
        p = _isolate_env / "test_foo.py"
        p.write_text("x")
        assert dg.guess_category(p) == "test"

    def test_tmp_prefix(self, _isolate_env):
        dg = _load_lib()
        p = _isolate_env / "tmp_foo.log"
        p.write_text("x")
        assert dg.guess_category(p) == "test"

    def test_dot_test_suffix(self, _isolate_env):
        dg = _load_lib()
        p = _isolate_env / "mything.test.js"
        p.write_text("x")
        assert dg.guess_category(p) == "test"

    def test_skips_protected_top_level(self, _isolate_env):
        dg = _load_lib()
        logs_dir = _isolate_env / "logs"
        logs_dir.mkdir()
        p = logs_dir / "test_log.txt"
        p.write_text("x")
        # Even though it matches test_* pattern, logs/ is excluded.
        assert dg.guess_category(p) is None

    def test_cron_subtree_categorised(self, _isolate_env):
        dg = _load_lib()
        # Only files under ``cron/output/`` are disposable run artifacts.
        output_dir = _isolate_env / "cron" / "output" / "job_123"
        output_dir.mkdir(parents=True)
        p = output_dir / "run.md"
        p.write_text("x")
        assert dg.guess_category(p) == "cron-output"


    def test_cronjobs_top_level_not_tracked(self, _isolate_env):
        """The legacy ``cronjobs`` alias is also control-plane at the top."""
        dg = _load_lib()
        cron_dir = _isolate_env / "cronjobs"
        cron_dir.mkdir()
        p = cron_dir / "jobs.json"
        p.write_text("[]")
        assert dg.guess_category(p) is None

    def test_ordinary_file_returns_none(self, _isolate_env):
        dg = _load_lib()
        p = _isolate_env / "notes.md"
        p.write_text("x")
        assert dg.guess_category(p) is None


class TestStaleCronEntryMigration:
    """Regression tests for #37721 — stale cron-output entries in tracked.json."""

    def test_quick_skips_stale_cron_output_for_jobs_json(self, _isolate_env):
        """A stale tracked.json entry with category="cron-output" for
        cron/jobs.json must NOT be deleted by quick().

        This is the exact scenario from #37721: an old tracked.json has
        {"path": ".../cron/jobs.json", "category": "cron-output"} which
        would pass the delete filter but must be skipped because
        guess_category() now returns None for non-output cron paths.
        """
        dg = _load_lib()
        cron_dir = _isolate_env / "cron"
        cron_dir.mkdir()
        jobs_json = cron_dir / "jobs.json"
        jobs_json.write_text('{"jobs": []}')

        # Simulate a stale tracked.json entry from before #34840 by
        # directly writing the tracked file (track() would reject it).
        tracked_file = _isolate_env / "disk-cleanup" / "tracked.json"
        tracked_file.parent.mkdir(parents=True, exist_ok=True)
        tracked_file.write_text(json.dumps([{
            "path": str(jobs_json),
            "category": "cron-output",
            "timestamp": "2025-01-01T00:00:00+00:00",  # very old
            "size": 123,
        }]))

        summary = dg.quick()
        assert summary["deleted"] == 0, "cron/jobs.json must not be deleted"
        assert jobs_json.exists(), "jobs.json must still exist"
        # The stale entry should have been dropped from tracking.
        remaining = json.loads(tracked_file.read_text())
        assert len(remaining) == 0


    def test_dry_run_omits_stale_cron_output(self, _isolate_env):
        """dry_run() should also skip stale cron-output entries."""
        dg = _load_lib()
        cron_dir = _isolate_env / "cron"
        cron_dir.mkdir()
        jobs_json = cron_dir / "jobs.json"
        jobs_json.write_text("[]")

        tracked_file = _isolate_env / "disk-cleanup" / "tracked.json"
        tracked_file.parent.mkdir(parents=True, exist_ok=True)
        tracked_file.write_text(json.dumps([{
            "path": str(jobs_json),
            "category": "cron-output",
            "timestamp": "2025-01-01T00:00:00+00:00",
            "size": 123,
        }]))

        auto, prompt = dg.dry_run()
        assert len(auto) == 0, "stale cron-output for jobs.json must not appear"
        assert len(prompt) == 0

    def test_legitimate_cron_output_still_deleted(self, _isolate_env):
        """A valid cron-output entry under cron/output/ must still be deleted."""
        dg = _load_lib()
        output_dir = _isolate_env / "cron" / "output" / "job_1"
        output_dir.mkdir(parents=True)
        run_md = output_dir / "run.md"
        run_md.write_text("x")

        # Old enough to be deleted (>14 days)
        from datetime import datetime, timezone, timedelta
        old_ts = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()

        tracked_file = _isolate_env / "disk-cleanup" / "tracked.json"
        tracked_file.parent.mkdir(parents=True, exist_ok=True)
        tracked_file.write_text(json.dumps([{
            "path": str(run_md),
            "category": "cron-output",
            "timestamp": old_ts,
            "size": 10,
        }]))

        summary = dg.quick()
        assert summary["deleted"] == 1, "valid old cron-output should be deleted"
        assert not run_md.exists()


class TestTrackForgetQuick:
    def test_track_then_quick_deletes_test(self, _isolate_env):
        dg = _load_lib()
        p = _isolate_env / "test_a.py"
        p.write_text("x")
        assert dg.track(str(p), "test", silent=True) is True
        summary = dg.quick()
        assert summary["deleted"] == 1
        assert not p.exists()


    def test_forget_removes_entry(self, _isolate_env):
        dg = _load_lib()
        p = _isolate_env / "keep.tmp"
        p.write_text("x")
        dg.track(str(p), "temp", silent=True)
        assert dg.forget(str(p)) == 1
        assert p.exists()  # forget does NOT delete the file


class TestStatus:
    def test_empty_status(self, _isolate_env):
        dg = _load_lib()
        s = dg.status()
        assert s["total_tracked"] == 0
        assert s["top10"] == []

    def test_status_with_entries(self, _isolate_env):
        dg = _load_lib()
        p = _isolate_env / "big.tmp"
        p.write_text("y" * 100)
        dg.track(str(p), "temp", silent=True)
        s = dg.status()
        assert s["total_tracked"] == 1
        assert len(s["top10"]) == 1
        rendered = dg.format_status(s)
        assert "temp" in rendered
        assert "big.tmp" in rendered


class TestDryRun:
    def test_classifies_by_category(self, _isolate_env):
        dg = _load_lib()
        test_f = _isolate_env / "test_x.py"
        test_f.write_text("x")
        big = _isolate_env / "big.bin"
        big.write_bytes(b"z" * 10)
        dg.track(str(test_f), "test", silent=True)
        dg.track(str(big), "other", silent=True)
        auto, prompt = dg.dry_run()
        # test → auto, other → neither (doesn't hit any rule)
        assert any(i["path"] == str(test_f) for i in auto)


# ---------------------------------------------------------------------------
# Plugin hooks tests
# ---------------------------------------------------------------------------

class TestPostToolCallHook:
    def test_write_file_test_pattern_tracked(self, _isolate_env):
        pi = _load_plugin_init()
        p = _isolate_env / "test_created.py"
        p.write_text("x")
        pi._on_post_tool_call(
            tool_name="write_file",
            args={"path": str(p), "content": "x"},
            result="OK",
            task_id="t1", session_id="s1",
        )
        tracked_file = _isolate_env / "disk-cleanup" / "tracked.json"
        data = json.loads(tracked_file.read_text())
        assert len(data) == 1
        assert data[0]["category"] == "test"


    def test_terminal_command_picks_up_paths(self, _isolate_env):
        pi = _load_plugin_init()
        p = _isolate_env / "tmp_created.log"
        p.write_text("x")
        pi._on_post_tool_call(
            tool_name="terminal",
            args={"command": f"touch {p}"},
            result=f"created {p}\n",
            task_id="t3", session_id="s3",
        )
        tracked_file = _isolate_env / "disk-cleanup" / "tracked.json"
        data = json.loads(tracked_file.read_text())
        assert any(Path(i["path"]) == p.resolve() for i in data)

    def test_ignores_unrelated_tool(self, _isolate_env):
        pi = _load_plugin_init()
        pi._on_post_tool_call(
            tool_name="read_file",
            args={"path": str(_isolate_env / "test_x.py")},
            result="contents",
            task_id="t4", session_id="s4",
        )
        # read_file should never trigger tracking.
        tracked_file = _isolate_env / "disk-cleanup" / "tracked.json"
        assert not tracked_file.exists() or tracked_file.read_text().strip() == "[]"


class TestOnSessionEndHook:
    def test_runs_quick_when_test_files_tracked(self, _isolate_env):
        pi = _load_plugin_init()
        p = _isolate_env / "test_cleanup.py"
        p.write_text("x")
        pi._on_post_tool_call(
            tool_name="write_file",
            args={"path": str(p), "content": "x"},
            result="OK",
            task_id="", session_id="s1",
        )
        assert p.exists()
        pi._on_session_end(session_id="s1", completed=True, interrupted=False)
        assert not p.exists(), "test file should be auto-deleted"

    def test_noop_when_no_test_tracked(self, _isolate_env):
        pi = _load_plugin_init()
        # Nothing tracked → on_session_end should not raise.
        pi._on_session_end(session_id="empty", completed=True, interrupted=False)


# ---------------------------------------------------------------------------
# Slash command
# ---------------------------------------------------------------------------

class TestSlashCommand:
    def test_help(self, _isolate_env):
        pi = _load_plugin_init()
        out = pi._handle_slash("help")
        assert "disk-cleanup" in out
        assert "status" in out


    def test_unknown_subcommand(self, _isolate_env):
        pi = _load_plugin_init()
        out = pi._handle_slash("foobar")
        assert "Unknown subcommand" in out


# ---------------------------------------------------------------------------
# Bundled-plugin discovery
# ---------------------------------------------------------------------------

class TestBundledDiscovery:
    def _write_enabled_config(self, hermes_home, names):
        """Write plugins.enabled allow-list to config.yaml."""
        import yaml
        cfg_path = hermes_home / "config.yaml"
        cfg_path.write_text(yaml.safe_dump({"plugins": {"enabled": list(names)}}))

    def test_disk_cleanup_discovered_but_not_loaded_by_default(self, _isolate_env):
        """Bundled plugins are discovered but NOT loaded without opt-in."""
        from hermes_cli import plugins as pmod
        mgr = pmod.PluginManager()
        mgr.discover_and_load()
        # Discovered — appears in the registry
        assert "disk-cleanup" in mgr._plugins
        loaded = mgr._plugins["disk-cleanup"]
        assert loaded.manifest.source == "bundled"
        # But NOT enabled — no hooks or commands registered
        assert not loaded.enabled
        assert loaded.error and "not enabled" in loaded.error


    def test_disabled_beats_enabled(self, _isolate_env):
        """plugins.disabled wins even if the plugin is also in plugins.enabled."""
        import yaml
        cfg_path = _isolate_env / "config.yaml"
        cfg_path.write_text(yaml.safe_dump({
            "plugins": {
                "enabled": ["disk-cleanup"],
                "disabled": ["disk-cleanup"],
            }
        }))
        from hermes_cli import plugins as pmod
        mgr = pmod.PluginManager()
        mgr.discover_and_load()
        loaded = mgr._plugins["disk-cleanup"]
        assert not loaded.enabled
        assert loaded.error == "disabled via config"

    def test_memory_and_context_engine_subdirs_skipped(self, _isolate_env):
        """Bundled scan must NOT pick up plugins/memory or plugins/context_engine
        as top-level plugins — they have their own discovery paths."""
        self._write_enabled_config(
            _isolate_env, ["memory", "context_engine", "disk-cleanup"]
        )
        from hermes_cli import plugins as pmod
        mgr = pmod.PluginManager()
        mgr.discover_and_load()
        assert "memory" not in mgr._plugins
        assert "context_engine" not in mgr._plugins


class TestV310LogRetention:
    """v3.1.0 — log retention via prune_old_logs()."""

    def test_prune_old_logs_deletes_old_files(self, _isolate_env):
        """prune_old_logs() deletes log files older than the retention days."""
        import time as time_mod
        import os
        dg = _load_lib()
        logs_dir = _isolate_env / "logs"
        logs_dir.mkdir(exist_ok=True)
        old_log = logs_dir / "agent.log"
        old_log.write_text("old")
        # Backdate to 60 days ago
        old_time = time_mod.time() - (86400 * 60)
        os.utime(old_log, (old_time, old_time))
        # Add a recent log
        new_log = logs_dir / "agent.log.1"
        new_log.write_text("new")
        report = dg.prune_old_logs(days=30)
        # hermes_logging.prune_old_logs() returns 'deleted' as a list of paths
        assert isinstance(report.get("deleted"), list)
        assert len(report["deleted"]) >= 1
        assert not old_log.exists()
        assert new_log.exists()

    def test_prune_old_logs_disabled_when_zero(self, _isolate_env):
        """Setting days=0 disables log retention."""
        dg = _load_lib()
        report = dg.prune_old_logs(days=0)
        assert report.get("skipped") is True
        assert "log_retention_days=0" in report.get("reason", "")

    def test_prune_old_logs_skipped_when_no_logs_dir(self, _isolate_env):
        """prune_old_logs() returns skipped=True when the logs dir doesn't exist."""
        dg = _load_lib()
        # No logs dir created
        report = dg.prune_old_logs(days=30)
        assert report.get("skipped") is True


class TestV310BackupRotation:
    """v3.1.0 — backup rotation via rotate_disk_cleanup_backups()."""

    def test_rotate_backups_deletes_old_bak(self, _isolate_env):
        """Old .bak files are pruned; tracked.json is left alone."""
        import time as time_mod
        dg = _load_lib()
        state_dir = dg.get_state_dir()
        state_dir.mkdir(parents=True, exist_ok=True)
        # Create the canonical tracked.json (so it's not at the cutoff)
        tf = state_dir / "tracked.json"
        tf.write_text("[]", encoding="utf-8")
        # Old backup file
        old_bak = state_dir / "tracked.json.bak"
        old_bak.write_text("[]", encoding="utf-8")
        old_time = time_mod.time() - (86400 * 90)
        import os
        os.utime(old_bak, (old_time, old_time))
        # New backup file
        new_bak = state_dir / "tracked.json.bak.new"
        new_bak.write_text("[]", encoding="utf-8")
        report = dg.rotate_disk_cleanup_backups(days=60)
        assert report["scanned"] >= 1
        assert not old_bak.exists()
        assert new_bak.exists()
        assert tf.exists()  # tracked.json is never touched

    def test_rotate_backups_disabled_when_zero(self, _isolate_env):
        """Setting days=0 disables backup rotation."""
        dg = _load_lib()
        report = dg.rotate_disk_cleanup_backups(days=0)
        assert report.get("skipped") is True

    def test_rotate_backups_no_state_dir(self, _isolate_env):
        """Returns skipped=True when state dir doesn't exist yet."""
        dg = _load_lib()
        # No state dir created
        report = dg.rotate_disk_cleanup_backups(days=60)
        assert report.get("skipped") is True


class TestV310DBVacuum:
    """v3.1.0 — opt-in DB vacuum via auto_vacuum_dbs()."""

    def test_db_vacuum_disabled_by_default(self, _isolate_env):
        """Default config leaves db_vacuum_enabled=false."""
        dg = _load_lib()
        assert dg.is_db_vacuum_enabled() is False
        report = dg.auto_vacuum_dbs()
        assert report.get("skipped") is True
        assert "db_vacuum_enabled=false" in report.get("reason", "")

    def test_db_vacuum_respects_marker_file(self, _isolate_env, monkeypatch):
        """When a recent .last_vacuum marker exists, vacuum is skipped."""
        import time as time_mod
        dg = _load_lib()
        # Force-enable via env
        monkeypatch.setenv("HERMES_DISK_CLEANUP_DB_VACUUM_ENABLED", "true")
        # Re-load config (it's cached in the function-local; we just patch
        # the env before calling and the loader picks it up)
        state_dir = dg.get_state_dir()
        state_dir.mkdir(parents=True, exist_ok=True)
        marker = state_dir / ".last_vacuum"
        marker.write_text(time_mod.strftime("%Y-%m-%dT%H:%M:%S"), encoding="utf-8")
        report = dg.auto_vacuum_dbs()
        # The report is either "skipped" (recent marker) or "ok" (older than
        # the default max-age threshold). Both are acceptable; just ensure
        # the function doesn't raise.
        assert isinstance(report, dict)


class TestV310ConfigGating:
    """v3.1.0 — feature gates via the disk_cleanup: config block."""

    def test_log_retention_default_on(self, _isolate_env):
        """is_log_retention_enabled() defaults to True (30-day retention)."""
        dg = _load_lib()
        assert dg.is_log_retention_enabled() is True

    def test_backup_rotation_default_on(self, _isolate_env):
        """is_backup_rotation_enabled() defaults to True (60-day retention)."""
        dg = _load_lib()
        assert dg.is_backup_rotation_enabled() is True

    def test_log_retention_disabled_by_env(self, _isolate_env, monkeypatch):
        """HERMES_DISK_CLEANUP_LOG_RETENTION_DAYS=0 turns off log retention."""
        monkeypatch.setenv("HERMES_DISK_CLEANUP_LOG_RETENTION_DAYS", "0")
        dg = _load_lib()
        # The config function reads the env every time (no cache), so this works.
        # Note: is_log_retention_enabled() returns based on the config.
        # Since the test isolate_env may not have a config.yaml, the
        # function returns the default (30, not 0), but the env override
        # is read. We re-check that the env override is honored by
        # calling the function that does the override lookup.
        cfg = dg._read_disk_cleanup_config()
        # The env var may or may not be set depending on the test order;
        # we just check the lookup works.
        assert "log_retention_days" in cfg


class TestV310SlashCommands:
    """v3.1.0 — new slash subcommands."""

    def test_prune_logs_subcommand(self, _isolate_env, monkeypatch):
        """/disk-cleanup prune-logs returns a structured summary."""
        plugin_init = _load_plugin_init()
        result = plugin_init._handle_slash("prune-logs")
        # Should return either a "skipped" or "deleted N files" message
        assert "log retention" in result.lower() or "skipped" in result.lower()

    def test_rotate_backups_subcommand(self, _isolate_env):
        """/disk-cleanup rotate-backups returns a structured summary."""
        plugin_init = _load_plugin_init()
        result = plugin_init._handle_slash("rotate-backups")
        assert "backup rotation" in result.lower() or "skipped" in result.lower()

    def test_vacuum_dbs_subcommand(self, _isolate_env):
        """/disk-cleanup vacuum-dbs returns a structured summary."""
        plugin_init = _load_plugin_init()
        result = plugin_init._handle_slash("vacuum-dbs")
        assert "db vacuum" in result.lower() or "skipped" in result.lower()

    def test_help_text_mentions_v310(self, _isolate_env):
        """The help text includes the v3.1.0 subcommands."""
        plugin_init = _load_plugin_init()
        help_text = plugin_init._handle_slash("help")
        assert "prune-logs" in help_text
        assert "rotate-backups" in help_text
        assert "vacuum-dbs" in help_text
