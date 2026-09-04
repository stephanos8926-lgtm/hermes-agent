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
import os
import sys
from datetime import datetime, timedelta, timezone
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


# ---------------------------------------------------------------------------
# Phase 1 regression: SWEEP_ROOLS (inclusion-only) + age-gate
# ---------------------------------------------------------------------------

class TestSweepRoots:
    """The old implementation used an inverted allowlist for empty-dir sweeping,
    which caused backup trees (backup-from-prod, state-snapshots, ...) to be
    touched when omitted from the allowlist.  Phase 1 replaces that with an
    explicit inclusion set (_SWEEP_ROOTS) and an age-gate so only known
    ephemeral roots are ever descended into.
    """

    def test_sweep_roots_constant(self, _isolate_env):
        """_SWEEP_ROOTS must contain the three intended ephemeral roots."""
        dg = _load_lib()
        expected = {"cache", "cron/output", "disk-cleanup/staging"}
        assert dg._SWEEP_ROOTS == expected

    def test_sweep_only_touches_sweep_roots(self, _isolate_env):
        """Empty dirs outside _SWEEP_ROOTS must never be swept."""
        dg = _load_lib()
        hermes_home = _isolate_env

        inside = hermes_home / "cache" / "empty_sub"
        inside.mkdir(parents=True)
        outside = hermes_home / "backup-from-prod" / "empty_sub"
        outside.mkdir(parents=True)

        old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).timestamp()
        os.utime(inside, (old_ts, old_ts))
        os.utime(outside, (old_ts, old_ts))

        dg.quick()

        assert not inside.exists(), "inside sweep root should be removed"
        assert outside.exists(), "outside sweep root must NOT be removed"

    def test_sweep_skips_fresh_empty_dirs(self, _isolate_env):
        """Empty dirs younger than _MIN_EMPTY_DIR_AGE_DAYS must be kept."""
        dg = _load_lib()
        hermes_home = _isolate_env

        fresh = hermes_home / "cache" / "fresh_empty"
        fresh.mkdir(parents=True)

        dg.quick()

        assert fresh.exists(), "fresh empty dir must be kept"

    def test_sweep_removes_old_empty_dirs(self, _isolate_env):
        """Empty dirs older than _MIN_EMPTY_DIR_AGE_DAYS must be removed."""
        dg = _load_lib()
        hermes_home = _isolate_env

        old = hermes_home / "cache" / "old_empty"
        old.mkdir(parents=True)

        old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).timestamp()
        os.utime(old, (old_ts, old_ts))

        dg.quick()

        assert not old.exists(), "old empty dir should be removed"

    def test_sweep_never_touches_backup_trees(self, _isolate_env):
        """All known backup/snapshot/app trees must be protected."""
        dg = _load_lib()
        hermes_home = _isolate_env

        protected_trees = [
            "backup-from-prod",
            "state-snapshots",
            "bin",
            "image_cache",
            "desktop-plugins",
        ]

        old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).timestamp()

        for tree in protected_trees:
            d = hermes_home / tree / "empty_sub"
            d.mkdir(parents=True)
            os.utime(d, (old_ts, old_ts))

        dg.quick()

        for tree in protected_trees:
            d = hermes_home / tree / "empty_sub"
            assert d.exists(), f"{tree} must not be swept"

    def test_sweep_tmp_hermes_dirs(self, _isolate_env):
        """Empty dirs under /tmp/hermes-* must be swept."""
        dg = _load_lib()
        tmp_dir = Path("/tmp") / f"hermes-test-sweep-{os.getpid()}"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        empty = tmp_dir / "empty_sub"
        empty.mkdir()

        old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).timestamp()
        os.utime(empty, (old_ts, old_ts))

        try:
            dg.quick()
            assert not empty.exists(), "/tmp/hermes-* empty dir should be swept"
        finally:
            if tmp_dir.exists():
                import shutil
                shutil.rmtree(tmp_dir)


# ---------------------------------------------------------------------------
# Phase 2 regression: quarantine (trash) — reversible deletion
# ---------------------------------------------------------------------------

class TestQuarantine:
    """Files deleted by quick()/deep() must land in quarantine, not be
    hard-deleted.  restore() and purge() must round-trip correctly.
    """

    def test_move_file_to_trash(self, _isolate_env):
        dg = _load_lib()
        p = _isolate_env / "test_remove.py"
        p.write_text("x")

        trash_id = dg._move_to_trash(p, "test", "quick-cleanup")
        assert trash_id is not None
        assert not p.exists()

        trash_dir = dg.get_trash_dir() / trash_id
        assert trash_dir.is_dir()
        assert (trash_dir / "test_remove.py").exists()
        manifest = json.loads((trash_dir / "manifest.json").read_text())
        assert manifest["original_path"] == str(p.resolve())
        assert manifest["category"] == "test"
        assert manifest["reason"] == "quick-cleanup"
        assert manifest["is_dir"] is False

    def test_move_dir_to_trash(self, _isolate_env):
        dg = _load_lib()
        d = _isolate_env / "tmp_bundle"
        d.mkdir()
        (d / "file.txt").write_text("x")

        trash_id = dg._move_to_trash(d, "temp", "quick-cleanup")
        assert trash_id is not None
        assert not d.exists()

        trash_dir = dg.get_trash_dir() / trash_id
        assert (trash_dir / "tmp_bundle").is_dir()
        manifest = json.loads((trash_dir / "manifest.json").read_text())
        assert manifest["is_dir"] is True

    def test_move_to_trash_nonexistent_path(self, _isolate_env):
        dg = _load_lib()
        missing = _isolate_env / "does_not_exist.txt"
        assert dg._move_to_trash(missing, "test", "quick") is None

    def test_restore_file(self, _isolate_env):
        dg = _load_lib()
        p = _isolate_env / "restore_me.py"
        p.write_text("x")
        trash_id = dg._move_to_trash(p, "test", "quick-cleanup")
        assert trash_id is not None

        assert dg.restore(trash_id) is True
        assert p.exists()
        assert not (dg.get_trash_dir() / trash_id).exists()

    def test_restore_missing_trash_id(self, _isolate_env):
        dg = _load_lib()
        assert dg.restore("nonexistent-id") is False

    def test_restore_corrupt_manifest(self, _isolate_env):
        dg = _load_lib()
        trash_dir = dg.get_trash_dir() / "corrupt-id"
        trash_dir.mkdir(parents=True)
        (trash_dir / "manifest.json").write_text("not json")
        assert dg.restore("corrupt-id") is False

    def test_purge_removes_old_entries(self, _isolate_env):
        dg = _load_lib()
        p = _isolate_env / "old_file.txt"
        p.write_text("x")
        trash_id = dg._move_to_trash(p, "test", "quick-cleanup")
        assert trash_id is not None

        # Back-date the manifest timestamp by 40 days.
        manifest_path = dg.get_trash_dir() / trash_id / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        old_ts = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
        manifest["timestamp"] = old_ts
        manifest_path.write_text(json.dumps(manifest))

        purged = dg.purge(older_than_days=30)
        assert purged == 1
        assert not (dg.get_trash_dir() / trash_id).exists()

    def test_purge_keeps_recent_entries(self, _isolate_env):
        dg = _load_lib()
        p = _isolate_env / "recent_file.txt"
        p.write_text("x")
        trash_id = dg._move_to_trash(p, "test", "quick-cleanup")
        assert trash_id is not None

        purged = dg.purge(older_than_days=30)
        assert purged == 0
        assert (dg.get_trash_dir() / trash_id).exists()

    def test_list_trash_sorted_desc(self, _isolate_env):
        dg = _load_lib()
        ids = []
        for name in ("a.txt", "b.txt"):
            p = _isolate_env / name
            p.write_text("x")
            tid = dg._move_to_trash(p, "test", "quick-cleanup")
            ids.append(tid)

        entries = dg.list_trash()
        assert len(entries) == 2
        # Most recent first.
        assert entries[0]["trash_id"] == ids[1]
        assert entries[1]["trash_id"] == ids[0]

    def test_quick_uses_quarantine(self, _isolate_env):
        """quick() must move deletable files to trash, not hard-delete."""
        dg = _load_lib()
        p = _isolate_env / "test_quarantine.py"
        p.write_text("x")
        dg.track(str(p), "test", silent=True)

        summary = dg.quick()
        assert summary["deleted"] == 1
        assert not p.exists()
        # File must be in trash.
        trash_entries = dg.list_trash()
        assert any(e["original_path"] == str(p.resolve()) for e in trash_entries)

    def test_deep_uses_quarantine(self, _isolate_env):
        """deep() must move confirmed items to trash, not hard-delete."""
        dg = _load_lib()
        p = _isolate_env / "chrome_old.txt"
        p.write_text("x")
        # Track as chrome-profile with an old timestamp so deep() picks it up.
        # chrome-profile has no 10-newest retention filter (unlike research).
        from datetime import datetime, timezone, timedelta
        old_ts = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
        dg.track(str(p), "chrome-profile", silent=True)
        tracked = dg.load_tracked()
        for item in tracked:
            if item["path"] == str(p.resolve()):
                item["timestamp"] = old_ts
        dg.save_tracked(tracked)

        def confirm(item):
            return True

        result = dg.deep(confirm=confirm)
        assert result["deep_deleted"] == 1
        assert not p.exists()
        trash_entries = dg.list_trash()
        assert any(e["original_path"] == str(p.resolve()) for e in trash_entries)


# ---------------------------------------------------------------------------
# Phase 4 regression: disk-pressure trigger
# ---------------------------------------------------------------------------

class TestDiskPressure:
    """Auto-cleanup at session end should only run when disk pressure is high
    (or when test files were tracked).
    """

    def test_get_disk_usage_pct_returns_0_on_error(self, _isolate_env, monkeypatch):
        dg = _load_lib()
        monkeypatch.setattr(os, "statvfs", lambda *a, **kw: (_ for _ in ()).throw(OSError()))
        assert dg.get_disk_usage_pct(_isolate_env) == 0

    def test_get_disk_usage_pct_returns_int(self, _isolate_env, monkeypatch):
        dg = _load_lib()

        class FakeStat:
            f_blocks = 1000
            f_bfree = 200

        monkeypatch.setattr(os, "statvfs", lambda *a, **kw: FakeStat())
        pct = dg.get_disk_usage_pct(_isolate_env)
        assert isinstance(pct, int)
        assert pct == 80  # (1000-200)/1000 * 100

    def test_should_auto_cleanup_true_when_above_threshold(self, _isolate_env, monkeypatch):
        dg = _load_lib()

        class FakeStat:
            f_blocks = 1000
            f_bfree = 100  # 90% used

        monkeypatch.setattr(os, "statvfs", lambda *a, **kw: FakeStat())
        # Default threshold is 85%.
        assert dg.should_auto_cleanup(_isolate_env) is True

    def test_should_auto_cleanup_false_when_below_threshold(self, _isolate_env, monkeypatch):
        dg = _load_lib()

        class FakeStat:
            f_blocks = 1000
            f_bfree = 300  # 70% used

        monkeypatch.setattr(os, "statvfs", lambda *a, **kw: FakeStat())
        assert dg.should_auto_cleanup(_isolate_env) is False

    def test_on_session_end_skips_when_no_tests_and_low_disk(self, _isolate_env, monkeypatch):
        pi = _load_plugin_init()
        # Mock should_auto_cleanup on the module that __init__.py actually uses.
        actual_dg = sys.modules["hermes_plugins.disk_cleanup.disk_cleanup"]
        monkeypatch.setattr(actual_dg, "should_auto_cleanup", lambda *a, **kw: False)

        # Nothing tracked, low disk → on_session_end should not call quick().
        pi._on_session_end(session_id="s1", completed=True, interrupted=False)
        # No tracked.json should have been created (quick() would have created it).
        tracked_file = _isolate_env / "disk-cleanup" / "tracked.json"
        assert not tracked_file.exists()

    def test_on_session_end_runs_when_no_tests_but_high_disk(self, _isolate_env, monkeypatch):
        pi = _load_plugin_init()
        # Mock should_auto_cleanup on the module that __init__.py actually uses.
        actual_dg = sys.modules["hermes_plugins.disk_cleanup.disk_cleanup"]
        monkeypatch.setattr(actual_dg, "should_auto_cleanup", lambda *a, **kw: True)

        # Nothing tracked, but high disk → on_session_end should call quick().
        pi._on_session_end(session_id="s1", completed=True, interrupted=False)
        # tracked.json is created by quick() even when empty.
        tracked_file = _isolate_env / "disk-cleanup" / "tracked.json"
        assert tracked_file.exists()


# ---------------------------------------------------------------------------
# Phase 5 regression: Matrix notification
# ---------------------------------------------------------------------------

class TestMatrixNotification:
    """Cleanup summaries should be posted to Matrix when configured."""

    def test_notify_skips_when_channel_not_matrix(self, _isolate_env, monkeypatch, caplog):
        pi = _load_plugin_init()
        actual_dg = sys.modules["hermes_plugins.disk_cleanup.disk_cleanup"]
        monkeypatch.setattr(actual_dg, "get_notify_on_cleanup", lambda: "none")
        monkeypatch.setattr(actual_dg, "quick", lambda: {
            "deleted": 1, "empty_dirs": 0, "freed": 100, "errors": []
        })

        with caplog.at_level("INFO"):
            pi._on_session_end(session_id="s1", completed=True, interrupted=False)
        assert "disk-cleanup notification" not in caplog.text

    def test_notify_logs_when_no_gateway(self, _isolate_env, monkeypatch, caplog):
        pi = _load_plugin_init()
        actual_dg = sys.modules["hermes_plugins.disk_cleanup.disk_cleanup"]
        monkeypatch.setattr(actual_dg, "get_notify_on_cleanup", lambda: "matrix")
        monkeypatch.setattr(actual_dg, "should_auto_cleanup", lambda *a, **kw: True)
        monkeypatch.setattr(actual_dg, "quick", lambda: {
            "deleted": 1, "empty_dirs": 0, "freed": 100, "errors": []
        })

        with caplog.at_level("INFO"):
            pi._on_session_end(session_id="s1", completed=True, interrupted=False)
        assert "disk-cleanup notification (matrix)" in caplog.text
        assert "Cleaned 1 files" in caplog.text
