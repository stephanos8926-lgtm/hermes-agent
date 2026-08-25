"""disk_cleanup — ephemeral file cleanup for Hermes Agent.

Library module wrapping the deterministic cleanup rules written by
@LVT382009 in PR #12212. The plugin ``__init__.py`` wires these
functions into ``post_tool_call`` and ``on_session_end`` hooks so
tracking and cleanup happen automatically — the agent never needs to
call a tool or remember a skill.

Rules:
  - test files    → delete immediately at task end (age >= 0)
  - temp files    → delete after 7 days
  - cron-output   → delete after 14 days
  - empty dirs    → always delete (under HERMES_HOME)
  - research      → keep 10 newest, prompt for older (deep only)
  - chrome-profile→ prompt after 14 days (deep only)
  - >500 MB files → prompt always (deep only)

Scope: strictly HERMES_HOME and /tmp/hermes-*
Never touches: ~/.hermes/logs/ or any system directory.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from hermes_constants import get_hermes_home
except Exception:  # pragma: no cover — plugin may load before constants resolves
    def get_hermes_home() -> Path:  # type: ignore[no-redef]
        val = (os.environ.get("HERMES_HOME") or "").strip()
        return Path(val).resolve() if val else (Path.home() / ".hermes").resolve()


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def get_state_dir() -> Path:
    """State dir — separate from ``$HERMES_HOME/logs/``."""
    return get_hermes_home() / "disk-cleanup"


def get_tracked_file() -> Path:
    return get_state_dir() / "tracked.json"


def get_log_file() -> Path:
    """Audit log — intentionally NOT under ``$HERMES_HOME/logs/``."""
    return get_state_dir() / "cleanup.log"


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------

def is_safe_path(path: Path) -> bool:
    """Accept only paths under HERMES_HOME or ``/tmp/hermes-*``.

    Rejects Windows mounts (``/mnt/c`` etc.) and any system directory.
    """
    hermes_home = get_hermes_home()
    try:
        path.resolve().relative_to(hermes_home)
        return True
    except (ValueError, OSError):
        pass
    # Allow /tmp/hermes-* explicitly
    parts = path.parts
    if len(parts) >= 3 and parts[1] == "tmp" and parts[2].startswith("hermes-"):
        return True
    return False


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def _log(message: str) -> None:
    try:
        log_file = get_log_file()
        log_file.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {message}\n")
    except OSError:
        # Never let the audit log break the agent loop.
        pass


# ---------------------------------------------------------------------------
# tracked.json — atomic read/write, backup scoped to tracked.json only
# ---------------------------------------------------------------------------

def load_tracked() -> List[Dict[str, Any]]:
    """Load tracked.json.  Restores from ``.bak`` on corruption."""
    tf = get_tracked_file()
    tf.parent.mkdir(parents=True, exist_ok=True)

    if not tf.exists():
        return []

    try:
        return json.loads(tf.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        bak = tf.with_suffix(".json.bak")
        if bak.exists():
            try:
                data = json.loads(bak.read_text(encoding="utf-8"))
                _log("WARN: tracked.json corrupted — restored from .bak")
                return data
            except Exception:
                pass
        _log("WARN: tracked.json corrupted, no backup — starting fresh")
        return []


def save_tracked(tracked: List[Dict[str, Any]]) -> None:
    """Atomic write: ``.tmp`` → backup old → rename."""
    tf = get_tracked_file()
    tf.parent.mkdir(parents=True, exist_ok=True)
    tmp = tf.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(tracked, indent=2), encoding="utf-8")
    if tf.exists():
        shutil.copy2(tf, tf.with_suffix(".json.bak"))
    tmp.replace(tf)


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------

ALLOWED_CATEGORIES = {
    "temp", "test", "research", "download",
    "chrome-profile", "cron-output", "other",
}

_EMPTY_DIR_PROTECTED_TOP_LEVEL = frozenset({
    "logs", "memories", "sessions", "cron", "cronjobs",
    "cache", "skills", "plugins", "disk-cleanup", "optional-skills",
    "hermes-agent", "backups", "profiles", ".worktrees",
    # User-authored project trees — never sweep empty directories
    # inside these (#75403).
    "patches", "projects", "skins", "themes", "contributors",
})

_EMPTY_DIR_SWEEP_PRUNE_DIRS = frozenset({
    ".git", "node_modules", "venv", ".venv",
    "site-packages", "__pycache__",
})


# Paths under $HERMES_HOME that must NEVER be deleted by quick(),
# regardless of what the stored category says.  This is a defense-in-depth
# guard against stale tracked.json entries from before #34840.
_PROTECTED_CRON_PATHS: set[str] = set()


def _is_protected_cron_path(p: Path) -> bool:
    """Return True if *p* is a cron control-plane file/directory that must
    never be deleted.

    This matches, by EXACT path only, the ``cron/`` directory itself, known
    control-plane files (``jobs.json``, ``.tick.lock``), and the ``output/``
    root directory. It does NOT (and must not be "simplified" to) blanket-match
    everything under ``cron/output/`` — those run artifacts are disposable and
    are cleaned by retention policy; only the ``output/`` root itself is
    protected, because deleting it wholesale erases every job's retained run
    history at once.
    """
    # Lazily build the set once per process so HERMES_HOME is resolved
    # exactly once.
    if not _PROTECTED_CRON_PATHS:
        hermes_home = get_hermes_home()
        for parent in ("cron", "cronjobs"):
            base = hermes_home / parent
            _PROTECTED_CRON_PATHS.add(str(base))
            _PROTECTED_CRON_PATHS.add(str(base / "output"))
            _PROTECTED_CRON_PATHS.add(str(base / "jobs.json"))
            _PROTECTED_CRON_PATHS.add(str(base / ".tick.lock"))
    resolved = str(p.resolve())
    return resolved in _PROTECTED_CRON_PATHS


def fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


# ---------------------------------------------------------------------------
# Track / forget
# ---------------------------------------------------------------------------

def track(path_str: str, category: str, silent: bool = False) -> bool:
    """Register a file for tracking. Returns True if newly tracked."""
    if category not in ALLOWED_CATEGORIES:
        _log(f"WARN: unknown category '{category}', using 'other'")
        category = "other"

    path = Path(path_str).resolve()

    if not path.exists():
        _log(f"SKIP: {path} (does not exist)")
        return False

    if not is_safe_path(path):
        _log(f"REJECT: {path} (outside HERMES_HOME)")
        return False

    size = path.stat().st_size if path.is_file() else 0
    tracked = load_tracked()

    # Deduplicate
    if any(item["path"] == str(path) for item in tracked):
        return False

    tracked.append({
        "path": str(path),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "category": category,
        "size": size,
    })
    save_tracked(tracked)
    _log(f"TRACKED: {path} ({category}, {fmt_size(size)})")
    if not silent:
        print(f"Tracked: {path} ({category}, {fmt_size(size)})")
    return True


def forget(path_str: str) -> int:
    """Remove a path from tracking without deleting the file."""
    p = Path(path_str).resolve()
    tracked = load_tracked()
    before = len(tracked)
    tracked = [i for i in tracked if Path(i["path"]).resolve() != p]
    removed = before - len(tracked)
    if removed:
        save_tracked(tracked)
        _log(f"FORGOT: {p} ({removed} entries)")
    return removed


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

def dry_run() -> Tuple[List[Dict], List[Dict]]:
    """Return (auto_delete_list, needs_prompt_list) without touching files."""
    tracked = load_tracked()
    now = datetime.now(timezone.utc)

    auto: List[Dict] = []
    prompt: List[Dict] = []

    for item in tracked:
        p = Path(item["path"])
        if not p.exists():
            continue
        age = (now - datetime.fromisoformat(item["timestamp"])).days
        cat = item["category"]
        size = item["size"]

        # Re-validate stale "cron-output" entries (fixes #37721).
        if cat == "cron-output":
            re_cat = guess_category(p)
            if re_cat != "cron-output":
                # Stale entry — would be skipped by quick(); omit from
                # dry-run output too.
                continue

        if cat == "test":
            auto.append(item)
        elif cat == "temp" and age > 7:
            auto.append(item)
        elif cat == "cron-output" and age > 14:
            auto.append(item)
        elif cat == "research" and age > 30:
            prompt.append(item)
        elif cat == "chrome-profile" and age > 14:
            prompt.append(item)
        elif size > 500 * 1024 * 1024:
            prompt.append(item)

    return auto, prompt


# ---------------------------------------------------------------------------
# Quick cleanup
# ---------------------------------------------------------------------------

def quick() -> Dict[str, Any]:
    """Safe deterministic cleanup — no prompts.

    Returns: ``{"deleted": N, "empty_dirs": N, "freed": bytes,
               "errors": [str, ...]}``.
    """
    tracked = load_tracked()
    now = datetime.now(timezone.utc)
    deleted = 0
    freed = 0
    new_tracked: List[Dict] = []
    errors: List[str] = []

    for item in tracked:
        p = Path(item["path"])
        cat = item["category"]

        if not p.exists():
            _log(f"STALE: {p} (removed from tracking)")
            continue

        age = (now - datetime.fromisoformat(item["timestamp"])).days

        # ---- stale-state migration (fixes #37721) ----
        # Old tracked.json entries may carry a "cron-output" category for
        # paths that are NOT under cron/output/ (e.g. cron/jobs.json).
        # guess_category() was fixed in #34840, but existing entries are
        # never re-validated.  Re-classify here so stale entries for cron
        # control-plane state are not deleted.
        if cat == "cron-output":
            re_cat = guess_category(p)
            if re_cat != "cron-output":
                _log(
                    f"SKIP stale cron-output entry: {p} "
                    f"(re-classified as {re_cat!r})"
                )
                # Drop the stale entry — it was misclassified.
                continue

        # ---- stale-state migration for 'test' category (fixes #75403) ----
        # Old tracked.json entries may carry a "test" category for paths
        # that are now under protected project directories (patches/,
        # projects/, etc.).  guess_category() was tightened in the fix for
        # #75403, but existing entries are never re-validated.  Re-classify
        # here so stale entries for protected paths are not deleted.
        if cat == "test":
            re_cat = guess_category(p)
            if re_cat != "test":
                _log(
                    f"SKIP stale test entry: {p} "
                    f"(re-classified as {re_cat!r} — under protected tree)"
                )
                continue

        # Hard safety net: never delete cron control-plane state even if
        # the category somehow slipped through re-validation above.
        if _is_protected_cron_path(p):
            _log(f"SKIP protected cron path: {p}")
            continue

        should_delete = (
            cat == "test"
            or (cat == "temp" and age > 7)
            or (cat == "cron-output" and age > 14)
        )

        if should_delete:
            try:
                if p.is_file():
                    p.unlink()
                elif p.is_dir():
                    shutil.rmtree(p)
                freed += item["size"]
                deleted += 1
                _log(f"DELETED: {p} ({cat}, {fmt_size(item['size'])})")
            except OSError as e:
                _log(f"ERROR deleting {p}: {e}")
                errors.append(f"{p}: {e}")
                new_tracked.append(item)
        else:
            new_tracked.append(item)

    # Remove empty dirs under HERMES_HOME, but never recurse into known
    # durable state trees.  Some installs place the Hermes checkout, venv,
    # and desktop build under HERMES_HOME; a full rglob over that tree can
    # stall the gateway event loop for minutes.
    hermes_home = get_hermes_home()
    empty_removed = 0
    sweep_stack: List[Tuple[Path, bool]] = []
    try:
        for top in hermes_home.iterdir():
            if (
                top.is_dir()
                and not top.is_symlink()
                and top.name not in _EMPTY_DIR_PROTECTED_TOP_LEVEL
                and top.name not in _EMPTY_DIR_SWEEP_PRUNE_DIRS
            ):
                sweep_stack.append((top, False))
    except OSError:
        sweep_stack = []

    while sweep_stack:
        dirpath, visited = sweep_stack.pop()
        if visited:
            try:
                if not any(dirpath.iterdir()):
                    dirpath.rmdir()
                    empty_removed += 1
                    _log(f"DELETED: {dirpath} (empty dir)")
            except OSError:
                pass
            continue

        sweep_stack.append((dirpath, True))
        try:
            for child in dirpath.iterdir():
                if (
                    child.is_dir()
                    and not child.is_symlink()
                    and child.name not in _EMPTY_DIR_SWEEP_PRUNE_DIRS
                ):
                    sweep_stack.append((child, False))
        except OSError:
            pass

    save_tracked(new_tracked)
    _log(
        f"QUICK_SUMMARY: {deleted} files, {empty_removed} dirs, "
        f"{fmt_size(freed)}"
    )
    return {
        "deleted": deleted,
        "empty_dirs": empty_removed,
        "freed": freed,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Deep cleanup (interactive — not called from plugin hooks)
# ---------------------------------------------------------------------------

def deep(
    confirm: Optional[callable] = None,
) -> Dict[str, Any]:
    """Deep cleanup.

    Runs :func:`quick` first, then asks the *confirm* callable for each
    risky item (research > 30d beyond 10 newest, chrome-profile > 14d,
    any file > 500 MB).  *confirm(item)* must return True to delete.

    Returns: ``{"quick": {...}, "deep_deleted": N, "deep_freed": bytes}``.
    """
    quick_result = quick()

    if confirm is None:
        # No interactive confirmer — deep stops after the quick pass.
        return {"quick": quick_result, "deep_deleted": 0, "deep_freed": 0}

    tracked = load_tracked()
    now = datetime.now(timezone.utc)
    research, chrome, large = [], [], []

    for item in tracked:
        p = Path(item["path"])
        if not p.exists():
            continue
        age = (now - datetime.fromisoformat(item["timestamp"])).days
        cat = item["category"]

        if cat == "research" and age > 30:
            research.append(item)
        elif cat == "chrome-profile" and age > 14:
            chrome.append(item)
        elif item["size"] > 500 * 1024 * 1024:
            large.append(item)

    research.sort(key=lambda x: x["timestamp"], reverse=True)
    old_research = research[10:]

    freed, count = 0, 0
    to_remove: List[Dict] = []

    for group in (old_research, chrome, large):
        for item in group:
            if confirm(item):
                try:
                    p = Path(item["path"])
                    if p.is_file():
                        p.unlink()
                    elif p.is_dir():
                        shutil.rmtree(p)
                    to_remove.append(item)
                    freed += item["size"]
                    count += 1
                    _log(
                        f"DELETED: {p} ({item['category']}, "
                        f"{fmt_size(item['size'])})"
                    )
                except OSError as e:
                    _log(f"ERROR deleting {item['path']}: {e}")

    if to_remove:
        remove_paths = {i["path"] for i in to_remove}
        save_tracked([i for i in tracked if i["path"] not in remove_paths])

    return {"quick": quick_result, "deep_deleted": count, "deep_freed": freed}


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def status() -> Dict[str, Any]:
    """Return per-category breakdown and top 10 largest tracked files."""
    tracked = load_tracked()
    cats: Dict[str, Dict] = {}
    for item in tracked:
        c = item["category"]
        cats.setdefault(c, {"count": 0, "size": 0})
        cats[c]["count"] += 1
        cats[c]["size"] += item["size"]

    existing = [
        (i["path"], i["size"], i["category"])
        for i in tracked if Path(i["path"]).exists()
    ]
    existing.sort(key=lambda x: x[1], reverse=True)

    return {
        "categories": cats,
        "top10": existing[:10],
        "total_tracked": len(tracked),
    }


def format_status(s: Dict[str, Any]) -> str:
    """Human-readable status string (for slash command output)."""
    lines = [f"{'Category':<20} {'Files':>6}  {'Size':>10}", "-" * 40]
    cats = s["categories"]
    for cat, d in sorted(cats.items(), key=lambda x: x[1]["size"], reverse=True):
        lines.append(f"{cat:<20} {d['count']:>6}  {fmt_size(d['size']):>10}")

    if not cats:
        lines.append("(nothing tracked yet)")

    lines.append("")
    lines.append("Top 10 largest tracked files:")
    if not s["top10"]:
        lines.append("  (none)")
    else:
        for rank, (path, size, cat) in enumerate(s["top10"], 1):
            lines.append(f"  {rank:>2}. {fmt_size(size):>8}  [{cat}]  {path}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Auto-categorisation from tool-call inspection
# ---------------------------------------------------------------------------

_TEST_PATTERNS = ("test_", "tmp_")
_TEST_SUFFIXES = (".test.py", ".test.js", ".test.ts", ".test.md")


def guess_category(path: Path) -> Optional[str]:
    """Return a category label for *path*, or None if we shouldn't track it.

    Used by the ``post_tool_call`` hook to auto-track ephemeral files.
    """
    if not is_safe_path(path):
        return None

    # Skip the state dir itself, logs, memory files, sessions, config.
    hermes_home = get_hermes_home()
    try:
        rel = path.resolve().relative_to(hermes_home)
        top = rel.parts[0] if rel.parts else ""
        if top in {
            "disk-cleanup", "logs", "memories", "sessions", "config.yaml",
            "skills", "plugins", ".env", "USER.md", "MEMORY.md", "SOUL.md",
            "auth.json", "hermes-agent",
            # User-authored and project trees — never auto-delete files
            # inside these just because they happen to be named test_* or
            # tmp_* (#75403, also #32164, #37721).
            "patches", "projects", "skins", "themes", "contributors",
            "profiles", "backups", "optional-skills",
        }:
            return None
        if top == "cron" or top == "cronjobs":
            # Only files under the disposable ``output/`` subtree are
            # cleanup candidates. Top-level cron control-plane state
            # (e.g. ``jobs.json``, ``.tick.lock``) must never be
            # auto-tracked — deleting it wipes the live scheduler
            # registry. See issue #32164.
            if len(rel.parts) >= 3 and rel.parts[1] == "output":
                return "cron-output"
            return None
        if top == "cache":
            return "temp"
    except ValueError:
        # Path isn't under HERMES_HOME (e.g. /tmp/hermes-*) — fall through.
        pass

    name = path.name
    if name.startswith(_TEST_PATTERNS):
        return "test"
    if any(name.endswith(sfx) for sfx in _TEST_SUFFIXES):
        return "test"
    return None


# ---------------------------------------------------------------------------
# v3.1.0 — Log retention, backup rotation, opt-in DB vacuum
#
# All three are feature-gated by the ``disk_cleanup:`` block in
# ``~/.hermes/config.yaml`` (with ``HERMES_DISK_CLEANUP_*`` env-var
# overrides). The functions below respect the gate: they read the
# config themselves, so callers can invoke them without checking
# the gate explicitly. Failures degrade silently — a broken
# log-prune must never block session end.
# ---------------------------------------------------------------------------


# Defaults — overridable via the disk_cleanup: config block.
DEFAULT_LOG_RETENTION_DAYS = 30
DEFAULT_BACKUP_RETENTION_DAYS = 60
DEFAULT_DB_VACUUM_ENABLED = False
DEFAULT_DB_VACUUM_MAX_AGE_HOURS = 168  # 7 days


def _read_disk_cleanup_config() -> Dict[str, Any]:
    """Read the ``disk_cleanup:`` config block, falling back to defaults.

    Mirrors the pattern in ``agent/_cache.py``: mtime-cached config
    read with graceful fallback to defaults on any failure. Every
    key has a default so downstream code can index without guards.
    """
    defaults: Dict[str, Any] = {
        "log_retention_days": DEFAULT_LOG_RETENTION_DAYS,
        "backup_retention_days": DEFAULT_BACKUP_RETENTION_DAYS,
        "db_vacuum_enabled": DEFAULT_DB_VACUUM_ENABLED,
        "db_vacuum_max_age_hours": DEFAULT_DB_VACUUM_MAX_AGE_HOURS,
    }
    try:
        from hermes_cli.config import read_raw_config_readonly
        raw = read_raw_config_readonly() or {}
    except Exception:
        return defaults
    cfg = raw.get("disk_cleanup", {}) if isinstance(raw, dict) else {}
    if not isinstance(cfg, dict):
        return defaults
    out = dict(defaults)
    for key in defaults:
        if key in cfg:
            out[key] = cfg[key]
    # Env-var overrides — follow the HERMES_DISK_CLEANUP_<KEY> pattern.
    for key in defaults:
        env_val = os.environ.get(f"HERMES_DISK_CLEANUP_{key.upper()}")
        if env_val is not None:
            try:
                out[key] = int(env_val) if isinstance(defaults[key], int) else env_val
            except ValueError:
                pass
    return out


def is_log_retention_enabled() -> bool:
    """Log retention is on by default; user can set days=0 to disable."""
    cfg = _read_disk_cleanup_config()
    return int(cfg.get("log_retention_days", DEFAULT_LOG_RETENTION_DAYS)) > 0


def is_backup_rotation_enabled() -> bool:
    """Backup rotation is on by default; user can set days=0 to disable."""
    cfg = _read_disk_cleanup_config()
    return int(cfg.get("backup_retention_days", DEFAULT_BACKUP_RETENTION_DAYS)) > 0


def is_db_vacuum_enabled() -> bool:
    """DB vacuum is OFF by default — explicit opt-in."""
    cfg = _read_disk_cleanup_config()
    return bool(cfg.get("db_vacuum_enabled", DEFAULT_DB_VACUUM_ENABLED))


def prune_old_logs(days: Optional[int] = None) -> Dict[str, Any]:
    """Prune rotated log files older than *days* (default from config).

    Thin wrapper over :func:`hermes_logging.prune_old_logs` that reads
    the retention days from the ``disk_cleanup.log_retention_days``
    config key when *days* is None. Returns the structured report.

    Never raises. Failures are logged to ``cleanup.log`` and reported
    in the return value, so the session-end hook can decide whether
    to mention the failure to the user.
    """
    cfg = _read_disk_cleanup_config()
    if days is None:
        days = int(cfg.get("log_retention_days", DEFAULT_LOG_RETENTION_DAYS))
    if days <= 0:
        return {"skipped": True, "reason": "log_retention_days=0"}

    logs_dir = get_hermes_home() / "logs"
    if not logs_dir.exists():
        return {"skipped": True, "reason": "logs dir not found", "scanned": 0}

    try:
        from hermes_logging import prune_old_logs as _prune
    except Exception as exc:
        return {"skipped": True, "reason": f"hermes_logging unavailable: {exc}"}

    try:
        report = _prune(logs_dir=logs_dir, retention_days=days)
    except Exception as exc:
        _log(f"LOG_RETENTION: failed: {exc}")
        return {"skipped": True, "reason": f"exception: {exc}"}

    deleted_count = len(report.get("deleted", []))
    if deleted_count > 0:
        _log(
            f"LOG_RETENTION: deleted={deleted_count} "
            f"kept={report.get('kept', 0)} retention_days={days}"
        )
    return report


def rotate_disk_cleanup_backups(days: Optional[int] = None) -> Dict[str, Any]:
    """Prune the ``tracked.json.bak`` files older than *days*.

    The ``tracked.json`` writer keeps one ``.bak`` as a corruption
    guard. Older ``.bak<N>`` (or other stragglers) accumulate if the
    file has been rotated externally. This function deletes any
    backup files in the disk-cleanup state dir whose mtime is older
    than the configured retention.

    Returns a structured report.
    """
    cfg = _read_disk_cleanup_config()
    if days is None:
        days = int(cfg.get("backup_retention_days", DEFAULT_BACKUP_RETENTION_DAYS))
    if days <= 0:
        return {"skipped": True, "reason": "backup_retention_days=0"}

    state_dir = get_state_dir()
    if not state_dir.exists():
        return {"skipped": True, "reason": "state dir not found", "scanned": 0}

    now = time.time()
    cutoff = now - (days * 86400)
    deleted: List[str] = []
    errors: List[str] = []
    scanned = 0

    for entry in state_dir.iterdir():
        # Match the .bak pattern but NOT tracked.json itself.
        if not entry.is_file():
            continue
        if entry.name == "tracked.json":
            continue
        if not (entry.suffix == ".bak" or ".bak" in entry.name):
            continue
        scanned += 1
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
                deleted.append(str(entry))
        except OSError as exc:
            errors.append(f"{entry}: {exc}")

    if deleted:
        _log(
            f"BACKUP_ROTATION: deleted={len(deleted)} retention_days={days}"
        )
    return {
        "scanned": scanned,
        "deleted_count": len(deleted),
        "deleted": deleted,
        "errors": errors,
        "retention_days": days,
    }


def auto_vacuum_dbs() -> Dict[str, Any]:
    """Opt-in: run ``hermes db vacuum`` against state.db and lcm.db.

    This is OFF by default. When on, the disk-cleanup plugin will,
    at session end, attempt to call the existing ``hermes db`` CLI
    subcommand to vacuum the live state database and the LCM store.
    Failures are reported but never raised.

    A max-age guard (``disk_cleanup.db_vacuum_max_age_hours``,
    default 168h = 7 days) prevents vacuuming on every session end —
    the sweep only runs if the last vacuum was longer ago than the
    threshold. A marker file at
    ``$HERMES_HOME/disk-cleanup/.last_vacuum`` tracks the last
    successful run.
    """
    cfg = _read_disk_cleanup_config()
    if not bool(cfg.get("db_vacuum_enabled", DEFAULT_DB_VACUUM_ENABLED)):
        return {"skipped": True, "reason": "db_vacuum_enabled=false"}

    max_age_hours = int(
        cfg.get("db_vacuum_max_age_hours", DEFAULT_DB_VACUUM_MAX_AGE_HOURS),
    )
    marker = get_state_dir() / ".last_vacuum"
    if marker.exists():
        try:
            age_hours = (time.time() - marker.stat().st_mtime) / 3600
            if age_hours < max_age_hours:
                return {
                    "skipped": True,
                    "reason": f"last vacuum {age_hours:.1f}h ago < {max_age_hours}h",
                }
        except OSError:
            pass

    # Use the same import the hermes CLI uses, but invoke the
    # internal action directly — no subprocess overhead.
    results: Dict[str, Any] = {}
    try:
        from hermes_cli.subcommands.db_handler import run_db_action
    except Exception as exc:
        return {"skipped": True, "reason": f"db_handler unavailable: {exc}"}

    for target in ("state.db", "lcm.db"):
        try:
            # Build a minimal argparse.Namespace to match what cmd_db
            # would pass. The handler accepts namespace objects.
            import argparse
            ns = argparse.Namespace(
                db_action="vacuum",
                target=target,
                lcm=(target == "lcm.db"),
                no_backup=False,
            )
            report = run_db_action(ns)
            results[target] = report
        except Exception as exc:
            results[target] = {"error": str(exc)}

    # Update the marker only on full success (no errors).
    if not any(isinstance(v, dict) and v.get("error") for v in results.values()):
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(
                datetime.now(timezone.utc).isoformat(),
                encoding="utf-8",
            )
        except OSError:
            pass
        _log("AUTO_VACUUM: state.db + lcm.db vacuumed")
    else:
        _log(f"AUTO_VACUUM: partial — {results}")

    return {"ok": True, "results": results}
