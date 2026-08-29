"""disk_cleanup — ephemeral file cleanup for Hermes Agent.

Library module wrapping the deterministic cleanup rules written by
@LVT382009 in PR #12212, extended in Phase 1-5 of the disk-cleanup
extension project.

Rules:
  - test files    → quarantine immediately at task end (age >= 0)
  - temp files    → quarantine after 7 days
  - cron-output   → quarantine after 14 days
  - empty dirs    → remove only inside explicit sweep roots, age-gated (7d)
  - research      → keep 10 newest, prompt for older (deep only)
  - chrome-profile→ prompt after 14 days (deep only)
  - >500 MB files → prompt always (deep only)

Scope: strictly HERMES_HOME and /tmp/hermes-*
Never touches: ~/.hermes/logs/ or any system directory.

Phase 1 — Root-cause fix: replaced inverted allowlist sweep with explicit
SWEEP_ROOTS (inclusion-only) so only cache/, cron/output/, and
disk-cleanup/staging are ever descended into.

Phase 2 — Quarantine: all deletions move to $HERMES_HOME/disk-cleanup/.trash/
with a manifest. restore() and purge() subcommands enable recovery.

Phase 3 — Config-driven: optional disk_cleanup: block in config.yaml makes
sweep_roots, min_empty_dir_age_days, quarantine_retention_days, and
auto_cleanup_min_disk_pressure_pct tunable without code edits.

Phase 4 — Disk-pressure trigger: auto-cleanup only runs when HERMES_HOME
filesystem usage exceeds the configured threshold (default 85%).

Phase 5 — Matrix notification: one-line cleanup summary posted to the
Matrix home channel when configured (default: matrix).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from hermes_constants import get_hermes_home
except Exception:  # pragma: no cover — plugin may load before constants resolves
    import os

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
# Quarantine (trash) — reversible deletion
# ---------------------------------------------------------------------------

def get_trash_dir() -> Path:
    """Quarantine root: ``$HERMES_HOME/disk-cleanup/.trash/``."""
    return get_state_dir() / ".trash"


def _trash_manifest_path(trash_id: str) -> Path:
    return get_trash_dir() / trash_id / "manifest.json"


def _move_to_trash(path: Path, category: str, reason: str) -> Optional[str]:
    """Move *path* into quarantine.  Returns the trash-id on success, or None.

    The quarantine layout is::

        $HERMES_HOME/disk-cleanup/.trash/<uuid>/
            manifest.json   — original_path, category, timestamp, size, reason
            <original_name> — the file or directory itself
    """
    if not path.exists():
        return None

    trash_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + __import__("uuid").uuid4().hex[:8]
    trash_entry = get_trash_dir() / trash_id
    trash_entry.mkdir(parents=True, exist_ok=True)

    dest = trash_entry / path.name
    manifest = {
        "trash_id": trash_id,
        "original_path": str(path.resolve()),
        "category": category,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "size": path.stat().st_size if path.is_file() else 0,
        "reason": reason,
        "is_dir": path.is_dir(),
    }

    try:
        if path.is_dir():
            shutil.move(str(path), str(dest))
        else:
            shutil.move(str(path), str(dest))
        _trash_manifest_path(trash_id).write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
        _log(f"TRASHED: {path} ({category}) -> {trash_id}")
        return trash_id
    except OSError as e:
        _log(f"ERROR trashing {path}: {e}")
        # Attempt to clean up the empty trash entry.
        try:
            if dest.exists():
                if dest.is_dir():
                    shutil.rmtree(dest)
                else:
                    dest.unlink()
            trash_entry.rmdir()
        except OSError:
            pass
        return None


def restore(trash_id: str) -> bool:
    """Restore a quarantined item back to its original location.

    Returns True on success, False if the trash entry is missing or corrupt.
    """
    manifest_path = _trash_manifest_path(trash_id)
    if not manifest_path.exists():
        return False

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        return False

    original = Path(manifest["original_path"])
    trash_entry = get_trash_dir() / trash_id
    quarantined = trash_entry / original.name

    if not quarantined.exists():
        return False

    try:
        original.parent.mkdir(parents=True, exist_ok=True)
        if quarantined.is_dir():
            shutil.move(str(quarantined), str(original))
        else:
            shutil.move(str(quarantined), str(original))
        _log(f"RESTORED: {original} (from {trash_id})")
        # Clean up the now-empty trash entry.
        _purge_trash_entry(trash_entry)
        return True
    except OSError as e:
        _log(f"ERROR restoring {original}: {e}")
        return False


def purge(older_than_days: Optional[int] = None) -> int:
    """Permanently delete trash entries older than *older_than_days*.

    If *older_than_days* is None, uses the ``quarantine_retention_days``
    value from ``config.yaml`` (default 30).

    Returns the number of entries purged.
    """
    if older_than_days is None:
        older_than_days = get_quarantine_retention_days()
    trash_root = get_trash_dir()
    if not trash_root.is_dir():
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    purged = 0

    for entry in trash_root.iterdir():
        if not entry.is_dir():
            continue
        manifest_path = entry / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            ts = datetime.fromisoformat(manifest["timestamp"])
            if ts < cutoff:
                _purge_trash_entry(entry)
                purged += 1
        except (json.JSONDecodeError, ValueError, OSError):
            continue

    if purged:
        _log(f"PURGED: {purged} trash entries older than {older_than_days}d")
    return purged


def _purge_trash_entry(entry: Path) -> None:
    """Remove a trash entry and all its contents."""
    try:
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    except OSError:
        pass


def list_trash() -> List[Dict[str, Any]]:
    """Return a list of all trash entries with their manifests."""
    trash_root = get_trash_dir()
    if not trash_root.is_dir():
        return []

    entries = []
    for entry in trash_root.iterdir():
        if not entry.is_dir():
            continue
        manifest_path = entry / "manifest.json"
        if manifest_path.exists():
            try:
                entries.append(json.loads(manifest_path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, ValueError):
                continue
    entries.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
    return entries


# ---------------------------------------------------------------------------
# Config-driven settings (reads optional disk_cleanup: block from config.yaml)
# ---------------------------------------------------------------------------

_CONFIG_DEFAULTS: Dict[str, Any] = {
    "sweep_roots": frozenset({"cache", "cron/output", "disk-cleanup/staging"}),
    "min_empty_dir_age_days": 7,
    "quarantine_retention_days": 30,
    "auto_cleanup_min_disk_pressure_pct": 85,
    "notify_on_cleanup": "matrix",
}

_CONFIG_CACHE: Dict[str, Any] = {}


def _load_config() -> Dict[str, Any]:
    """Lazy-load the ``disk_cleanup:`` block from ``$HERMES_HOME/config.yaml``.

    Returns the merged config (user overrides on top of defaults).  The
    result is cached for the lifetime of the process so we don't re-parse
    YAML on every call.
    """
    if _CONFIG_CACHE:
        return _CONFIG_CACHE

    cfg = dict(_CONFIG_DEFAULTS)
    try:
        import yaml

        config_path = get_hermes_home() / "config.yaml"
        if config_path.is_file():
            with open(config_path, "r", encoding="utf-8") as fh:
                full = yaml.safe_load(fh) or {}
            block = full.get("disk_cleanup") or {}
            if isinstance(block, dict):
                if "sweep_roots" in block and isinstance(block["sweep_roots"], list):
                    cfg["sweep_roots"] = frozenset(block["sweep_roots"])
                if "min_empty_dir_age_days" in block:
                    cfg["min_empty_dir_age_days"] = int(block["min_empty_dir_age_days"])
                if "quarantine_retention_days" in block:
                    cfg["quarantine_retention_days"] = int(block["quarantine_retention_days"])
                if "auto_cleanup_min_disk_pressure_pct" in block:
                    cfg["auto_cleanup_min_disk_pressure_pct"] = int(block["auto_cleanup_min_disk_pressure_pct"])
                if "notify_on_cleanup" in block:
                    cfg["notify_on_cleanup"] = str(block["notify_on_cleanup"])
    except Exception:
        # Never let a config parse failure break cleanup.
        pass

    _CONFIG_CACHE.update(cfg)
    return _CONFIG_CACHE


def get_sweep_roots() -> frozenset:
    """Return the set of top-level dirs the empty-dir sweep may descend into."""
    return _load_config().get("sweep_roots", _CONFIG_DEFAULTS["sweep_roots"])


def get_min_empty_dir_age_days() -> int:
    """Return the minimum age (in days) for an empty dir to be removed."""
    return _load_config().get("min_empty_dir_age_days", _CONFIG_DEFAULTS["min_empty_dir_age_days"])


def get_quarantine_retention_days() -> int:
    """Return how long trash entries are kept before purge() removes them."""
    return _load_config().get("quarantine_retention_days", _CONFIG_DEFAULTS["quarantine_retention_days"])


def get_auto_cleanup_min_disk_pressure_pct() -> int:
    """Return the disk-usage threshold that triggers auto-cleanup."""
    return _load_config().get("auto_cleanup_min_disk_pressure_pct", _CONFIG_DEFAULTS["auto_cleanup_min_disk_pressure_pct"])


def get_notify_on_cleanup() -> str:
    """Return the notification channel for cleanup summaries."""
    return _load_config().get("notify_on_cleanup", _CONFIG_DEFAULTS["notify_on_cleanup"])


def get_disk_usage_pct(path: Optional[Path] = None) -> int:
    """Return the percentage of disk used by the filesystem containing *path*.

    If *path* is None, uses ``$HERMES_HOME``.  Returns 0 on error.
    """
    target = path or get_hermes_home()
    try:
        stat = os.statvfs(str(target))
        if stat.f_blocks == 0:
            return 0
        used = stat.f_blocks - stat.f_bfree
        return int((used / stat.f_blocks) * 100)
    except OSError:
        return 0


def should_auto_cleanup(path: Optional[Path] = None) -> bool:
    """Return True if disk usage exceeds the configured threshold."""
    return get_disk_usage_pct(path) >= get_auto_cleanup_min_disk_pressure_pct()


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

_SWEEP_ROOTS = frozenset({
    "cache",
    "cron/output",
    "disk-cleanup/staging",
})

# Empty-dir sweep: only descend into _SWEEP_ROOTS.  Every other
# top-level directory is protected by construction — no allowlist
# inversion bug possible.
_MIN_EMPTY_DIR_AGE_DAYS = 7

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

    Files are moved to quarantine (``$HERMES_HOME/disk-cleanup/.trash/``)
    instead of being hard-deleted, so they can be restored later.  Empty
    dirs inside sweep roots are still removed immediately (they contain
    nothing to recover).

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
            trash_id = _move_to_trash(p, cat, "quick-cleanup")
            if trash_id:
                freed += item["size"]
                deleted += 1
                _log(f"TRASHED: {p} ({cat}, {fmt_size(item['size'])}) -> {trash_id}")
            else:
                _log(f"ERROR trashing {p}: quarantine failed")
                errors.append(f"{p}: quarantine failed")
                new_tracked.append(item)
        else:
            new_tracked.append(item)

    # Remove empty dirs ONLY inside explicit sweep roots.  The old
    # implementation iterated every top-level dir except an allowlist,
    # which caused backup trees (backup-from-prod, state-snapshots, ...)
    # to be swept when omitted from the allowlist.  SWEEP_ROOTS is
    # inclusion-only: nothing outside these roots is ever touched.
    hermes_home = get_hermes_home()
    empty_removed = 0
    sweep_stack: List[Tuple[Path, bool]] = []
    now = datetime.now(timezone.utc)
    min_age = get_min_empty_dir_age_days()
    try:
        for root_name in get_sweep_roots():
            # Handle nested roots like "cron/output" by walking parents.
            parts = root_name.split("/")
            base = hermes_home
            for part in parts[:-1]:
                base = base / part
                if not base.is_dir():
                    break
            else:
                leaf = base / parts[-1]
                if leaf.is_dir() and not leaf.is_symlink():
                    sweep_stack.append((leaf, False))
        # Also sweep /tmp/hermes-* if present.
        tmp_root = Path("/tmp")
        if tmp_root.is_dir():
            for child in tmp_root.iterdir():
                if (
                    child.is_dir()
                    and not child.is_symlink()
                    and child.name.startswith("hermes-")
                ):
                    sweep_stack.append((child, False))
    except OSError:
        sweep_stack = []

    while sweep_stack:
        dirpath, visited = sweep_stack.pop()
        if visited:
            try:
                if not any(dirpath.iterdir()):
                    # Age-gate: only remove empty dirs older than
                    # min_age to avoid sweeping freshly created scaffolding.
                    mtime = dirpath.stat().st_mtime
                    age_days = (now - datetime.fromtimestamp(mtime, tz=timezone.utc)).days
                    if age_days < min_age:
                        continue
                    dirpath.rmdir()
                    empty_removed += 1
                    _log(f"DELETED: {dirpath} (empty dir, age={age_days}d)")
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

    Items confirmed for deletion are moved to quarantine rather than
    hard-deleted, so they can be restored later.

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
                p = Path(item["path"])
                trash_id = _move_to_trash(p, item["category"], "deep-cleanup")
                if trash_id:
                    to_remove.append(item)
                    freed += item["size"]
                    count += 1
                    _log(
                        f"TRASHED: {p} ({item['category']}, "
                        f"{fmt_size(item['size'])}) -> {trash_id}"
                    )
                else:
                    _log(f"ERROR trashing {p}: quarantine failed")

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
