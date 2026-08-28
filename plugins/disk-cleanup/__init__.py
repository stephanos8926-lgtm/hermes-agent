"""disk-cleanup plugin — auto-cleanup of ephemeral Hermes session files.

Wires three behaviours:

1. ``post_tool_call`` hook — inspects ``write_file`` and ``terminal``
   tool results for newly-created paths matching test/temp patterns
   under ``HERMES_HOME`` and tracks them silently.  Zero agent
   compliance required.

2. ``on_session_end`` hook — when any test files were auto-tracked
   during the just-finished turn, runs :func:`disk_cleanup.quick` and
   logs a single line to ``$HERMES_HOME/disk-cleanup/cleanup.log``.
   Also triggers cleanup when disk pressure exceeds the configured
   threshold, even without tracked test files.

3. ``/disk-cleanup`` slash command — manual ``status``, ``dry-run``,
   ``quick``, ``deep``, ``track``, ``forget``, ``restore``, ``purge``,
   ``list-trash``, ``prune-logs``, ``rotate-backups``, ``vacuum-dbs``.

Phase 2 extension: deleted files move to quarantine
(``$HERMES_HOME/disk-cleanup/.trash/``) with a manifest, enabling
``restore`` and ``purge`` operations.

Phase 5 extension: optional Matrix notification posts a one-line cleanup
summary to the Matrix home channel after each session-end cleanup.

Phase 3.1.0 extension: log retention, backup rotation, and opt-in DB vacuum
at session end when their respective feature gates are enabled.

Replaces PR #12212's skill-plus-script design: the agent no longer
needs to remember to run commands.
"""

from __future__ import annotations

import logging
import re
import shlex
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Set

from . import disk_cleanup as dg

logger = logging.getLogger(__name__)


# Per-task set of "test files newly tracked this turn".  Keyed by task_id
# (or session_id as fallback) so on_session_end can decide whether to run
# cleanup.  Guarded by a lock — post_tool_call can fire concurrently on
# parallel tool calls.
_recent_test_tracks: Dict[str, Set[str]] = {}
_lock = threading.Lock()


# Tool-call result shapes we can parse
_WRITE_FILE_PATH_KEY = "path"
_TERMINAL_PATH_REGEX = re.compile(r"(?:^|\s)(/[^\s'\"`]+|~/[^\s'\"`]+)")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tracker_key(task_id: str, session_id: str) -> str:
    return task_id or session_id or "default"


def _record_track(task_id: str, session_id: str, path: Path, category: str) -> None:
    """Record that we tracked *path* as *category* during this turn."""
    if category != "test":
        return
    key = _tracker_key(task_id, session_id)
    with _lock:
        _recent_test_tracks.setdefault(key, set()).add(str(path))


def _drain(task_id: str, session_id: str) -> Set[str]:
    """Pop the set of test paths tracked during this turn."""
    key = _tracker_key(task_id, session_id)
    with _lock:
        return _recent_test_tracks.pop(key, set())


def _attempt_track(path_str: str, task_id: str, session_id: str) -> None:
    """Best-effort auto-track. Never raises."""
    try:
        p = Path(path_str).expanduser()
    except Exception:
        return
    if not p.exists():
        return
    category = dg.guess_category(p)
    if category is None:
        return
    newly = dg.track(str(p), category, silent=True)
    if newly:
        _record_track(task_id, session_id, p, category)


def _extract_paths_from_write_file(args: Dict[str, Any]) -> Set[str]:
    path = args.get(_WRITE_FILE_PATH_KEY)
    return {path} if isinstance(path, str) and path else set()


def _extract_paths_from_patch(args: Dict[str, Any]) -> Set[str]:
    # The patch tool creates new files via the `mode="patch"` path too, but
    # most of its use is editing existing files — we only care about new
    # ephemeral creations, so treat patch conservatively and only pick up
    # the single-file `path` arg.  Track-then-cleanup is idempotent, so
    # re-tracking an already-tracked file is a no-op (dedup in track()).
    path = args.get("path")
    return {path} if isinstance(path, str) and path else set()


def _extract_paths_from_terminal(args: Dict[str, Any], result: str) -> Set[str]:
    """Best-effort: pull candidate filesystem paths from a terminal command
    and its output, then let ``guess_category`` / ``is_safe_path`` filter.
    """
    paths: Set[str] = set()
    cmd = args.get("command") or ""
    if isinstance(cmd, str) and cmd:
        # Tokenise the command — catches `touch /tmp/hermes-x/test_foo.py`
        try:
            for tok in shlex.split(cmd, posix=True):
                if tok.startswith(("/", "~")):
                    paths.add(tok)
        except ValueError:
            pass
    # Only scan the result text if it's a reasonable size (avoid 50KB dumps).
    if isinstance(result, str) and len(result) < 4096:
        for match in _TERMINAL_PATH_REGEX.findall(result):
            paths.add(match)
    return paths


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------

def _on_post_tool_call(
    tool_name: str = "",
    args: Optional[Dict[str, Any]] = None,
    result: Any = None,
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    **_: Any,
) -> None:
    """Auto-track ephemeral files created by recent tool calls."""
    if not isinstance(args, dict):
        return

    candidates: Set[str] = set()
    if tool_name == "write_file":
        candidates = _extract_paths_from_write_file(args)
    elif tool_name == "patch":
        candidates = _extract_paths_from_patch(args)
    elif tool_name == "terminal":
        candidates = _extract_paths_from_terminal(args, result if isinstance(result, str) else "")
    else:
        return

    for path_str in candidates:
        _attempt_track(path_str, task_id, session_id)


def _on_session_end(
    session_id: str = "",
    completed: bool = True,
    interrupted: bool = False,
    **_: Any,
) -> None:
    """Run quick cleanup if any test files were tracked during this turn.

    Also runs the v3.1.0 housekeeping jobs at session end:
    log retention, backup rotation, and (opt-in) DB vacuum. Each is
    feature-gated and never raises.
    """
    # Drain both task-level and session-level buckets.  In practice only one
    # is populated per turn; the other is empty.
    drained_session = _drain("", session_id)
    # Also drain any task-scoped buckets that happen to exist.  This is a
    # cheap sweep: if an agent spawned subagents (each with their own
    # task_id) they'll have recorded into separate buckets; we want to
    # cleanup them all at session end.
    with _lock:
        task_buckets = list(_recent_test_tracks.keys())
    for key in task_buckets:
        if key and key != session_id:
            _recent_test_tracks.pop(key, None)

    # ---- Core ephemeral-file cleanup (gated on activity) ----
    ran_quick = False
    summary = None
    if drained_session or task_buckets:
        try:
            summary = dg.quick()
        except Exception as exc:
            logger.debug("disk-cleanup quick cleanup failed: %s", exc)
            summary = None
        if summary and (summary["deleted"] or summary["empty_dirs"]):
            dg._log(
                f"AUTO_QUICK (session_end): deleted={summary['deleted']} "
                f"dirs={summary['empty_dirs']} freed={dg.fmt_size(summary['freed'])}"
            )
            ran_quick = True
    elif not dg.should_auto_cleanup():
        # No test files tracked this turn — only run cleanup if disk
        # pressure exceeds the configured threshold.
        return

    if summary is None:
        try:
            summary = dg.quick()
        except Exception as exc:
            logger.debug("disk-cleanup quick cleanup failed: %s", exc)
            summary = None

    if summary and (summary["deleted"] or summary["empty_dirs"]):
        ran_quick = True
        dg._log(
            f"AUTO_QUICK (session_end): deleted={summary['deleted']} "
            f"dirs={summary['empty_dirs']} freed={dg.fmt_size(summary['freed'])}"
        )

    # ---- v3.1.0: log retention ----
    try:
        log_report = dg.prune_old_logs()
        if len(log_report.get("deleted", [])) > 0:
            dg._log(
                f"AUTO_LOG_RETENTION (session_end): "
                f"deleted={len(log_report['deleted'])} retention_days={log_report.get('retention_days')}"
            )
    except Exception as exc:
        logger.debug("disk-cleanup log retention failed: %s", exc)

    # ---- v3.1.0: backup rotation ----
    try:
        backup_report = dg.rotate_disk_cleanup_backups()
        if backup_report.get("deleted_count", 0) > 0:
            dg._log(
                f"AUTO_BACKUP_ROTATION (session_end): "
                f"deleted={backup_report['deleted_count']}"
            )
    except Exception as exc:
        logger.debug("disk-cleanup backup rotation failed: %s", exc)

    # ---- v3.1.0: opt-in DB vacuum (gated by disk_cleanup.db_vacuum_enabled) ----
    try:
        vacuum_report = dg.auto_vacuum_dbs()
        if vacuum_report.get("ok"):
            dg._log(
                f"AUTO_VACUUM (session_end): state.db + lcm.db vacuumed"
            )
    except Exception as exc:
        logger.debug("disk-cleanup auto vacuum failed: %s", exc)

    if ran_quick and summary is not None:
        _notify_cleanup(summary)


def _notify_cleanup(summary: Dict[str, Any]) -> None:
    """Best-effort notification of cleanup results.

    Posts a one-line summary to the channel configured in
    ``disk_cleanup.notify_on_cleanup`` (default: ``matrix``).  If the
    gateway's Matrix adapter is available the message is sent there;
    otherwise it is logged so the summary is never silently lost.
    """
    channel = dg.get_notify_on_cleanup()
    if channel != "matrix":
        return

    text = (
        f"[disk-cleanup] Cleaned {summary['deleted']} files + "
        f"{summary['empty_dirs']} empty dirs, freed {dg.fmt_size(summary['freed'])}."
    )
    if summary.get("errors"):
        text += f" {len(summary['errors'])} error(s); see cleanup.log."

    # Try the live gateway Matrix adapter first (non-blocking).
    sent = False
    try:
        from gateway.run import _gateway_runner_ref
        runner = _gateway_runner_ref()
    except Exception:
        runner = None

    if runner is not None:
        try:
            from gateway.config import Platform, load_gateway_config
            config = load_gateway_config()
            platform = Platform.MATRIX
            pconfig = config.platforms.get(platform)
            if pconfig and pconfig.enabled:
                home = config.get_home_channel(platform)
                if home:
                    adapter = runner.adapters.get(platform)
                    if adapter is not None:
                        import asyncio
                        coro = adapter.send(
                            chat_id=home.chat_id,
                            content=text,
                            metadata=None,
                        )
                        try:
                            loop = asyncio.get_running_loop()
                            loop.create_task(coro)
                        except RuntimeError:
                            asyncio.run(coro)
                        sent = True
        except Exception:
            sent = False

    if not sent:
        logger.info("disk-cleanup notification (matrix): %s", text)


# ---------------------------------------------------------------------------
# Slash command
# ---------------------------------------------------------------------------

_HELP_TEXT = """\
/disk-cleanup — ephemeral-file cleanup

Subcommands:
  status                     Per-category breakdown + top-10 largest
  dry-run                    Preview what quick/deep would delete
  quick                      Run safe cleanup now (no prompts)
  deep                       Run quick, then list items that need prompts
  track <path> <category>    Manually add a path to tracking
  forget <path>              Stop tracking a path (does not delete)
  restore <trash_id>         Restore a quarantined item to its original path
  purge [days]               Permanently delete trash older than N days (default 30)
  list-trash                 Show all items currently in quarantine
  prune-logs [days]          Prune rotated log files older than N days
                             (default from disk_cleanup.log_retention_days)
  rotate-backups [days]      Prune tracked.json.bak files older than N days
                             (default from disk_cleanup.backup_retention_days)
  vacuum-dbs                 Opt-in: run hermes db vacuum on state.db + lcm.db
                             (gated by disk_cleanup.db_vacuum_enabled)

Categories: temp | test | research | download | chrome-profile | cron-output | other

All operations are scoped to HERMES_HOME and /tmp/hermes-*.
Test files are auto-tracked on write_file / terminal and auto-cleaned at session end.
Log retention, backup rotation, and DB vacuum also run automatically at session end
when their respective feature gates are enabled.
"""


def _fmt_summary(summary: Dict[str, Any]) -> str:
    base = (
        f"[disk-cleanup] Cleaned {summary['deleted']} files + "
        f"{summary['empty_dirs']} empty dirs, freed {dg.fmt_size(summary['freed'])}."
    )
    if summary.get("errors"):
        base += f"\n  {len(summary['errors'])} error(s); see cleanup.log."
    return base


def _handle_slash(raw_args: str) -> Optional[str]:
    argv = raw_args.strip().split()
    if not argv or argv[0] in {"help", "-h", "--help"}:
        return _HELP_TEXT

    sub = argv[0]

    if sub == "status":
        return dg.format_status(dg.status())

    if sub == "dry-run":
        auto, prompt = dg.dry_run()
        auto_size = sum(i["size"] for i in auto)
        prompt_size = sum(i["size"] for i in prompt)
        lines = [
            "Dry-run preview (nothing deleted):",
            f"  Auto-delete : {len(auto)} files ({dg.fmt_size(auto_size)})",
        ]
        for item in auto:
            lines.append(f"    [{item['category']}] {item['path']}")
        lines.append(
            f"  Needs prompt: {len(prompt)} files ({dg.fmt_size(prompt_size)})"
        )
        for item in prompt:
            lines.append(f"    [{item['category']}] {item['path']}")
        lines.append(
            f"\n  Total potential: {dg.fmt_size(auto_size + prompt_size)}"
        )
        return "\n".join(lines)

    if sub == "quick":
        return _fmt_summary(dg.quick())

    if sub == "deep":
        # In-session deep can't prompt the user interactively — show what
        # quick cleaned plus the items that WOULD need confirmation.
        quick_summary = dg.quick()
        _auto, prompt_items = dg.dry_run()
        lines = [_fmt_summary(quick_summary)]
        if prompt_items:
            size = sum(i["size"] for i in prompt_items)
            lines.append(
                f"\n{len(prompt_items)} item(s) need confirmation "
                f"({dg.fmt_size(size)}):"
            )
            for item in prompt_items:
                lines.append(f"  [{item['category']}] {item['path']}")
            lines.append(
                "\nRun `/disk-cleanup forget <path>` to skip, or delete "
                "manually via terminal."
            )
        return "\n".join(lines)

    if sub == "track":
        if len(argv) < 3:
            return "Usage: /disk-cleanup track <path> <category>"
        path_arg = argv[1]
        category = argv[2]
        if category not in dg.ALLOWED_CATEGORIES:
            return (
                f"Unknown category '{category}'. "
                f"Allowed: {sorted(dg.ALLOWED_CATEGORIES)}"
            )
        if dg.track(path_arg, category, silent=True):
            return f"Tracked {path_arg} as '{category}'."
        return (
            f"Not tracked (already present, missing, or outside HERMES_HOME): "
            f"{path_arg}"
        )

    if sub == "forget":
        if len(argv) < 2:
            return "Usage: /disk-cleanup forget <path>"
        n = dg.forget(argv[1])
        return (
            f"Removed {n} tracking entr{'y' if n == 1 else 'ies'} for {argv[1]}."
            if n else f"Not found in tracking: {argv[1]}"
        )

    if sub == "restore":
        if len(argv) < 2:
            return "Usage: /disk-cleanup restore <trash_id>"
        if dg.restore(argv[1]):
            return f"Restored {argv[1]} to its original location."
        return f"Trash entry not found or corrupt: {argv[1]}"

    if sub == "purge":
        days = int(argv[1]) if len(argv) > 1 else 30
        n = dg.purge(older_than_days=days)
        return f"Purged {n} trash entry{'ies' if n != 1 else ''} older than {days}d."

    if sub == "list-trash":
        entries = dg.list_trash()
        if not entries:
            return "Quarantine is empty."
        lines = [f"Quarantine ({len(entries)} entries):"]
        for e in entries:
            lines.append(
                f"  [{e['trash_id']}] {e['original_path']} "
                f"({e['category']}, {e['reason']}, {e['timestamp']})"
            )
        return "\n".join(lines)

    if sub == "prune-logs":
        days = int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else None
        report = dg.prune_old_logs(days=days)
        if report.get("skipped"):
            return f"[disk-cleanup] log retention: skipped ({report.get('reason')})"
        deleted_count = len(report.get("deleted", []))
        return (
            f"[disk-cleanup] log retention: deleted {deleted_count} files "
            f"(kept {report.get('kept', 0)}, retention_days={report.get('retention_days')})"
        )

    if sub == "rotate-backups":
        days = int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else None
        report = dg.rotate_disk_cleanup_backups(days=days)
        if report.get("skipped"):
            return f"[disk-cleanup] backup rotation: skipped ({report.get('reason')})"
        return (
            f"[disk-cleanup] backup rotation: scanned {report['scanned']}, "
            f"deleted {report['deleted_count']} files (retention_days={report['retention_days']})"
        )

    if sub == "vacuum-dbs":
        report = dg.auto_vacuum_dbs()
        if report.get("skipped"):
            return f"[disk-cleanup] db vacuum: skipped ({report.get('reason')})"
        if report.get("ok"):
            targets = ", ".join(report.get("results", {}).keys())
            return f"[disk-cleanup] db vacuum: {targets} vacuumed"
        return f"[disk-cleanup] db vacuum: {report}"

    return f"Unknown subcommand: {sub}\n\n{_HELP_TEXT}"


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    ctx.add_hook("post_tool_call", _on_post_tool_call)
    ctx.add_hook("on_session_end", _on_session_end)
    ctx.add_slash_command("disk-cleanup", _handle_slash)