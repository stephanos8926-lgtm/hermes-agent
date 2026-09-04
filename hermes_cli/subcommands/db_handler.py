"""Handler logic for the ``hermes db`` subcommand.

Thin wrapper over:

  * :func:`hermes_state.repair_state_db_schema` (state.db FTS rebuild,
    sqlite_master de-duplication, FTS schema drop+VACUUM escalation).
  * :func:`hermes_state.quarantine_zeroed_state_db` (the #68474
    "state.db file size > 0 but the first page is NULs" recovery).
  * :func:`hermes_state.collect_state_db_stats` (read-only stats
    snapshot — page count, WAL size, row counts, FTS presence).

Plus a local LCM-store integrity check (the LCM database is a separate
sqlite file with its own schema; it's not managed by ``hermes_state.py``).

Why this lives in its own module instead of inlining in main.py:

* ``hermes_cli/main.py`` is the project's known god-file
  (see AGENTS.md rule #9 — "Refactor god-files into clean modules").
* The handler is non-trivial (each action has its own preflight,
  progress reporting, and exit-code mapping) and benefits from
  isolated unit tests.
* Future work (auto-vacuum on disk pressure, scheduled integrity
  sweeps) hooks naturally into ``run_db_action`` without growing
  main.py further.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


# Recognized target names -> relative paths under HERMES_HOME.
_KNOWN_TARGETS = {
    "state.db": "state.db",
    "kanban.db": "kanban.db",
    # The LCM context store lives in the bundled plugin's data dir.
    # Path is resolved at runtime (see _lcm_db_path) because the
    # plugin's data dir can move between installs.
}


def _lcm_db_path(hermes_home: Path) -> Optional[Path]:
    """Best-effort location of the LCM context-engine sqlite store.

    The LCM plugin's data dir is created on first ingest. We probe the
    common locations and return the first one that exists. Returns
    None when the LCM store is not on disk yet.
    """
    candidates = [
        hermes_home / "plugins" / "hermes-lcm" / "lcm.db",
        hermes_home / "plugins" / "hermes-lcm" / "data" / "lcm.db",
        hermes_home / "lcm.db",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def _resolve_db_path(target: str, hermes_home: Path) -> Path:
    """Resolve a CLI target name to an absolute path under HERMES_HOME.

    Raises ValueError for unknown targets; main.py maps that to
    exit code 2 (usage error).
    """
    if target == "lcm.db":
        resolved = _lcm_db_path(hermes_home)
        if resolved is None:
            raise ValueError(
                "lcm.db not found — has the LCM plugin ingested any "
                "context yet? (no data dir under ~/.hermes/plugins/hermes-lcm/)"
            )
        return resolved
    if target in _KNOWN_TARGETS:
        return hermes_home / _KNOWN_TARGETS[target]
    raise ValueError(
        f"unknown target {target!r}; valid: {sorted(_KNOWN_TARGETS)} or 'lcm.db'"
    )


def _list_known_databases(hermes_home: Path) -> List[Dict[str, Any]]:
    """Enumerate all known sqlite databases under HERMES_HOME.

    Returns a list of {name, path, size_bytes, exists} dicts. Used by
    the 'list' action.
    """
    out: List[Dict[str, Any]] = []
    for name, rel in _KNOWN_TARGETS.items():
        p = hermes_home / rel
        out.append({
            "name": name,
            "path": str(p),
            "size_bytes": p.stat().st_size if p.exists() else 0,
            "exists": p.exists(),
        })
    lcm = _lcm_db_path(hermes_home)
    if lcm is not None:
        out.append({
            "name": "lcm.db",
            "path": str(lcm),
            "size_bytes": lcm.stat().st_size,
            "exists": True,
        })
    return out


def _integrity_check(db_path: Path) -> Dict[str, Any]:
    """Read-only sqlite integrity check on a database.

    Returns a dict with the result, the raw sqlite output, and the
    database size. Never raises — a corrupted database that can't
    be opened returns a structured error dict instead.
    """
    if not db_path.exists():
        return {"ok": False, "error": f"{db_path} does not exist", "size_bytes": 0}
    try:
        # URI mode=ro so we never acquire a write lock against a live
        # gateway; short timeout to fail fast on a busy database.
        uri = f"file:{db_path}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=2) as conn:
            cur = conn.execute("PRAGMA integrity_check;")
            rows = [r[0] for r in cur.fetchall()]
            cur = conn.execute("PRAGMA quick_check;")
            quick = [r[0] for r in cur.fetchall()]
        return {
            "ok": rows == ["ok"] and quick == ["ok"],
            "integrity_check": rows,
            "quick_check": quick,
            "size_bytes": db_path.stat().st_size,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "size_bytes": db_path.stat().st_size,
        }


def _vacuum(db_path: Path) -> Dict[str, Any]:
    """Run VACUUM + WAL checkpoint on a database.

    Opens the database read-write, runs PRAGMA wal_checkpoint(TRUNCATE)
    to drop the WAL file, then VACUUM to rebuild the b-tree from
    scratch. This is the most expensive maintenance action — only
    call it manually or from a scheduled job.

    Returns a report dict with the size before/after and a status.
    """
    if not db_path.exists():
        return {"ok": False, "error": f"{db_path} does not exist"}
    size_before = db_path.stat().st_size
    try:
        with sqlite3.connect(str(db_path), timeout=30) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            conn.execute("VACUUM;")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        size_after = db_path.stat().st_size
        return {
            "ok": True,
            "size_before": size_before,
            "size_after": size_after,
            "freed_bytes": max(0, size_before - size_after),
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "size_before": size_before,
        }


def _check_lcm(hermes_home: Path) -> Dict[str, Any]:
    """Run the LCM-store integrity check independently of state.db.

    The LCM store has its own schema (lifecycle_state, dag, assertion,
    rollup, trajectory, vector_store tables) and lives under the plugin
    data dir, not under HERMES_HOME/state.db. A separate probe is the
    only way to surface its health via this CLI.
    """
    lcm = _lcm_db_path(hermes_home)
    if lcm is None:
        return {
            "ok": True,
            "skipped": True,
            "reason": "lcm.db not on disk yet (LCM plugin has not ingested context)",
        }
    return _integrity_check(lcm)


def _stats_lcm(hermes_home: Path) -> Dict[str, Any]:
    """Stats snapshot for the LCM store, mirroring the state.db shape."""
    lcm = _lcm_db_path(hermes_home)
    if lcm is None:
        return {"exists": False, "path": None}
    try:
        from hermes_state import collect_state_db_stats
        return collect_state_db_stats(lcm)
    except Exception as exc:
        return {"exists": True, "error": f"{type(exc).__name__}: {exc}", "path": str(lcm)}


def _print_wal_advisory(stats: Dict[str, Any], db_path: Path) -> None:
    """Print a one-line advisory when the WAL is suspiciously large.

    The advisory fires under two independent conditions:

      1. Absolute: wal_size_bytes >= 100 MiB (regardless of db size)
      2. Relative: wal_size_bytes > 50% of the database logical size
         (the WAL has outgrown the database it shadows)

    Both conditions suggest the WAL is not being checkpointed often
    enough, which can mean:
      * A long-running write transaction is preventing checkpoint
      * The auto-checkpoint threshold (database.wal_autocheckpoint)
        is set too high
      * The gateway has been writing aggressively without a passive
        checkpoint opportunity

    The recommended remedy is hermes db vacuum which runs
    PRAGMA wal_checkpoint(TRUNCATE) + VACUUM. The advisory is a hint,
    not an error: the user can ignore it if they know the workload
    is bursty and the WAL will shrink naturally.
    """
    wal = stats.get("wal_size_bytes")
    if wal is None or wal == 0:
        return  # No WAL sidecar or stat() failed - nothing to advise
    # Absolute threshold: 100 MiB
    WAL_ABSOLUTE_THRESHOLD = 100 * 1024 * 1024
    # Relative threshold: WAL > 50% of logical db size
    WAL_RELATIVE_RATIO = 0.5

    absolute = wal >= WAL_ABSOLUTE_THRESHOLD
    logical = stats.get("logical_size_bytes")
    relative = (
        logical is not None
        and logical > 0
        and wal > logical * WAL_RELATIVE_RATIO
    )
    if not (absolute or relative):
        return

    rel_str = ""
    if relative and logical:
        pct = wal * 100 / logical
        rel_str = f" ({pct:.0f}% of {logical:,d} byte db)"
    abs_str = f"{wal:,d} bytes"
    reasons = []
    if absolute:
        reasons.append(">= 100 MiB")
    if relative:
        reasons.append("> 50% of db size")
    reason_str = ", ".join(reasons)
    target_name = db_path.name if db_path else "state.db"
    print(
        f"  advisory: wal_size_bytes is {abs_str}{rel_str}; "
        f"recommend hermes db vacuum {target_name} ({reason_str})",
        file=sys.stderr,
    )


def run_db_action(
    *,
    action: str,
    target: str,
    include_lcm: bool,
    skip_backup: bool,
    hermes_home: Path,
) -> int:
    """Dispatch a ``hermes db <action>`` invocation.

    Returns a process exit code (0 = success, 1 = error, 2 = usage error).
    All results are printed to stdout as JSON-ish plain text so the
    output is easy to grep / pipe.
    """
    if action == "list":
        rows = _list_known_databases(hermes_home)
        print("Known sqlite databases:")
        for r in rows:
            marker = "" if r["exists"] else " (missing)"
            size = f"{r['size_bytes']:>12,d} bytes" if r["exists"] else "             —"
            print(f"  {r['name']:<12} {size}  {r['path']}{marker}")
        return 0

    try:
        db_path = _resolve_db_path(target, hermes_home)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if action == "check":
        result = _integrity_check(db_path)
        print(f"integrity_check on {db_path}:")
        for k, v in result.items():
            print(f"  {k}: {v}")
        if include_lcm:
            print()
            lcm_result = _check_lcm(hermes_home)
            print(f"integrity_check on lcm.db:")
            for k, v in lcm_result.items():
                print(f"  {k}: {v}")
            # Combined verdict
            overall = result.get("ok", False) and lcm_result.get("ok", False)
        else:
            overall = result.get("ok", False)
        return 0 if overall else 1

    if action == "stats":
        try:
            from hermes_state import collect_state_db_stats
            stats = collect_state_db_stats(db_path)
        except Exception as exc:
            print(f"error: failed to collect stats: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        print(f"stats for {db_path}:")
        for k, v in stats.items():
            print(f"  {k}: {v}")
        _print_wal_advisory(stats, db_path)
        if include_lcm:
            print()
            lcm_stats = _stats_lcm(hermes_home)
            print(f"stats for lcm.db:")
            for k, v in lcm_stats.items():
                print(f"  {k}: {v}")
            _print_wal_advisory(lcm_stats, hermes_home / "plugins" / "hermes-lcm" / "lcm.db")
        return 0

    if action == "vacuum":
        if target == "lcm.db":
            print(
                "warning: vacuuming lcm.db directly bypasses the LCM "
                "plugin's own compaction. Prefer 'lcm compact' or the "
                "plugin's auto-maintenance hooks. Proceeding anyway.",
                file=sys.stderr,
            )
        result = _vacuum(db_path)
        print(f"vacuum {db_path}: {result}")
        return 0 if result.get("ok") else 1

    if action == "repair":
        # state.db has a dedicated repair primitive; for other DBs
        # we fall through to integrity_check + vacuum which is the
        # closest non-destructive action available.
        if target == "state.db":
            try:
                from hermes_state import repair_state_db_schema
                report = repair_state_db_schema(db_path, backup=not skip_backup)
            except Exception as exc:
                print(f"error: repair failed: {type(exc).__name__}: {exc}", file=sys.stderr)
                return 1
            print(f"repair {db_path}:")
            for k, v in report.items():
                print(f"  {k}: {v}")
            return 0 if report.get("repaired") else 1
        else:
            # For non-state.db targets, "repair" is integrity + vacuum
            # — the same escalation the LCM store would benefit from.
            check = _integrity_check(db_path)
            if not check.get("ok"):
                print(
                    f"error: {db_path} failed integrity check: {check.get('error', check)}",
                    file=sys.stderr,
                )
                return 1
            result = _vacuum(db_path)
            print(f"repair (vacuum) {db_path}: {result}")
            return 0 if result.get("ok") else 1

    print(f"error: unknown action {action!r}", file=sys.stderr)
    return 2
