"""Gateway snapshot — versioned, agent-readable status.

The snapshot is a single JSON object suitable for both human display
(structured keys) and machine consumption (deterministic ordering,
no locale-dependent formatting, schema_version field for evolution).

Surface (v1):
    gateway:    pid, uptime, RSS, threads, status, code_sha, code_version
    runtime:    gateway_state, active_agents, platforms (per-platform)
                session_store status
    cache:      placeholder block — gateway doesn't expose counters
                yet; filled with `null` fields and a note. v2 will
                wire real hit/miss/latency counters once the gateway
                instruments them.
    disk:       hermes_home free / total / used_pct
    errors:     list of {field, reason} — non-fatal collection errors
                (psutil missing, gateway_state.json stale, etc.)

Failure modes (all non-fatal — snapshot is best-effort):
    * No gateway running        → "gateway.pid": null, "gateway.status": "stopped"
    * gateway_state.json missing → runtime fields are null + an error entry
    * psutil missing             → rss_bytes, threads = null + an error entry
    * disk_usage fails           → disk fields = null + an error entry

A consumer can rely on: keys are always present, values are typed
(str | int | float | bool | null | list | dict). New fields may be
added in future minor versions; existing fields will not change type
without a major version bump.

Versioning
----------
SNAPSHOT_SCHEMA_VERSION follows semver. Bump the MAJOR component
when an existing field changes type or semantics. Bump MINOR when
fields are added. The version is also stamped in every snapshot
output, so consumers can guard against drift.

Why a separate module
---------------------
Keeps the snapshot collection testable in isolation (no argparse, no
print, no service-manager side effects) and lets the renderer evolve
independently. The gateway status path imports this module lazily
only when --json is requested, so the default human status path
pays zero overhead.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

#: Schema version. Bump MAJOR on breaking field changes, MINOR on additions.
SNAPSHOT_SCHEMA_VERSION = "1.1"

#: Project root is consulted only for the disk check; default is the
#: current working directory if the import that sets it is unavailable.
_DEFAULT_DISK_TARGETS: tuple[str, ...] = ("~/.hermes",)


def _human_bytes(n: int | float | None) -> str | None:
    """Format bytes as a human-readable string. None-safe."""
    if n is None:
        return None
    try:
        size = float(n)
    except (TypeError, ValueError):
        return None
    if size < 1024:
        return f"{int(size)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        size /= 1024.0
        if size < 1024:
            return f"{size:.1f} {unit}"
    return f"{size / 1024.0:.1f} TB"


def _read_gateway_state_json(path: Path) -> tuple[dict | None, str | None]:
    """Read gateway_state.json; return (state, error). Both can be None.

    The file is rewritten by the gateway frequently (heartbeat), so a
    parse failure here is rare; the most common cause is the gateway
    not running (no file) or the file being briefly mid-rewrite.
    """
    if not path.exists():
        return None, f"file not found: {path}"
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"could not read {path}: {exc}"
    if not isinstance(data, dict):
        return None, f"unexpected non-dict content in {path}"
    return data, None


def _psutil_process_info(pid: int) -> tuple[int | None, int | None, int | None, str | None]:
    """Return (rss_bytes, threads, create_time_unix, error).

    ``create_time`` is psutil's Unix-epoch seconds for the process
    start; used to derive a real uptime. The gateway_state.json's
    ``start_time`` is *clock ticks since boot* on Linux (used for
    PID-reuse guarding, not uptime), so we deliberately do NOT
    reuse that value here. None for any field that fails — the
    snapshot stays well-typed and consumers can rely on that.
    """
    try:
        import psutil  # type: ignore
    except Exception as exc:
        return None, None, None, f"psutil unavailable: {exc}"
    try:
        proc = psutil.Process(pid)
        rss = int(proc.memory_info().rss)
        threads = int(proc.num_threads())
        # create_time is float epoch seconds; round to int for clean output
        create_time = int(proc.create_time())
        return rss, threads, create_time, None
    except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
        return None, None, None, f"psutil: {exc}"
    except Exception as exc:  # catch-all: snapshot is best-effort
        return None, None, None, f"psutil: {exc}"


def _disk_snapshot(target: Path) -> tuple[int | None, int | None, float | None, str | None]:
    """Return (free_bytes, total_bytes, used_pct, error).

    used_pct is rounded to one decimal. None for any field that fails.
    """
    try:
        usage = shutil.disk_usage(target)
    except OSError as exc:
        return None, None, None, f"disk_usage({target}) failed: {exc}"
    free = int(usage.free)
    total = int(usage.total)
    used_pct = round((1.0 - (free / total)) * 100.0, 1) if total else None
    return free, total, used_pct, None


def _uptime_seconds(start_time: float | None) -> int | None:
    """Return seconds since start_time. None-safe."""
    if start_time is None:
        return None
    try:
        return max(0, int(time.time() - float(start_time)))
    except (TypeError, ValueError):
        return None


def _read_replay_economy_counters(hermes_home: Path) -> dict[str, Any] | None:
    """Read replay economy counters from the persisted file.
    
    Returns None if file doesn't exist or is stale (> 5 min old).
    """
    counters_path = hermes_home / "data" / "replay_economy_counters.json"
    if not counters_path.exists():
        return None
    try:
        data = json.loads(counters_path.read_text(encoding="utf-8"))
        # Check staleness
        updated_at = data.get("updated_at")
        if updated_at:
            from datetime import datetime, timezone
            try:
                updated_dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - updated_dt).total_seconds()
                if age > 300:  # 5 minutes
                    return None
            except Exception:
                pass
        return data
    except Exception:
        return None


def collect_snapshot(
    *,
    gateway_pids: tuple[int, ...] = (),
    gateway_state_path: Path | None = None,
    hermes_home: Path | None = None,
) -> dict[str, Any]:
    """Build a versioned snapshot of the gateway's current state.

    Parameters
    ----------
    gateway_pids
        PIDs returned by :func:`hermes_cli.gateway.find_gateway_pids`.
        Empty when no gateway is running.
    gateway_state_path
        Path to ``gateway_state.json``. Defaults to
        ``~/.hermes/gateway_state.json`` when None.
    hermes_home
        Path to ``~/.hermes``. Defaults to the same env var resolution
        used elsewhere (``~/.hermes`` or the active profile's home).
    """
    errors: list[dict[str, str]] = []

    if gateway_state_path is None:
        gateway_state_path = Path("~/.hermes/gateway_state.json").expanduser()
    if hermes_home is None:
        hermes_home = Path("~/.hermes").expanduser()

    # ── gateway block ────────────────────────────────────────────────────
    pid = gateway_pids[0] if gateway_pids else None
    rss_bytes: int | None = None
    threads: int | None = None
    create_time: int | None = None
    if pid is not None:
        rss_bytes, threads, create_time, err = _psutil_process_info(pid)
        if err:
            errors.append({"field": "gateway.rss_bytes", "reason": err})
            errors.append({"field": "gateway.threads", "reason": err})

    # ── runtime block (from gateway_state.json) ──────────────────────────
    state, state_err = _read_gateway_state_json(gateway_state_path)
    if state_err:
        errors.append({"field": "runtime", "reason": state_err})

    runtime: dict[str, Any] = {
        "gateway_state": (state or {}).get("gateway_state"),
        "exit_reason": (state or {}).get("exit_reason"),
        "active_agents": (state or {}).get("active_agents"),
        "session_store_status": ((state or {}).get("session_store") or {}).get("status"),
        "platforms": (state or {}).get("platforms") or {},
        "updated_at": (state or {}).get("updated_at"),
        "code_sha": (state or {}).get("code_sha"),
        "code_version": (state or {}).get("code_version"),
    }
    # Uptime preference: psutil create_time (real Unix epoch) > gateway_state.json
    # start_time (clock ticks since boot on Linux — a fingerprint, not a date).
    # _uptime_seconds only succeeds on Unix-epoch inputs, so it self-disables
    # when the JSON value is the ticks fingerprint.
    if create_time is not None:
        runtime["uptime_seconds"] = _uptime_seconds(create_time)
    else:
        _state_uptime = _uptime_seconds((state or {}).get("start_time"))
        if _state_uptime is not None:
            runtime["uptime_seconds"] = _state_uptime

    # ── cache block (v1.1: includes replay economy counters) ────────────────
    # v1: shape the consumer will read, values null. v1.1 adds replay
    # economy fields (request_cache, wire_compaction) that are populated
    # when the replay economy module is active. Fields remain null when
    # the feature is disabled or not yet initialized.
    replay_counters = _read_replay_economy_counters(hermes_home)
    
    if replay_counters:
        # Replay economy is active — populate from persisted counters
        l1 = replay_counters.get("replay_l1", {})
        wire = replay_counters.get("replay_wire", {})
        cache: dict[str, Any] = {
            "l1": {"hits": None, "misses": None, "hit_rate_pct": None},
            "l2": {"hits": None, "misses": None, "hit_rate_pct": None},
            "agent_cache": {
                "enabled": None,
                "max_bytes": None,
                "current_bytes": None,
            },
            "request_cache": {
                "hits": l1.get("hits"),
                "misses": l1.get("misses"),
                "hit_rate_pct": l1.get("hit_rate_pct"),
                "entries": None,  # Not tracked in current implementation
                "max_entries": None,
            },
            "wire_compaction": {
                "externalized_count": wire.get("compactions"),
                "bytes_saved": wire.get("bytes_saved_total"),
                "avg_compression_ratio": wire.get("compression_ratio_pct"),
            },
            "_note": (
                "Cache counters are not yet exposed by the gateway. "
                "Fields are null in v1; will be wired in a v2 schema bump. "
                "v1.1 adds replay economy fields (request_cache, wire_compaction)."
            ),
        }
    else:
        # Replay economy not active — v1 shape with nulls
        cache: dict[str, Any] = {
            "l1": {"hits": None, "misses": None, "hit_rate_pct": None},
            "l2": {"hits": None, "misses": None, "hit_rate_pct": None},
            "agent_cache": {
                "enabled": None,
                "max_bytes": None,
                "current_bytes": None,
            },
            "request_cache": {
                "hits": None,
                "misses": None,
                "hit_rate_pct": None,
                "entries": None,
                "max_entries": None,
            },
            "wire_compaction": {
                "externalized_count": None,
                "bytes_saved": None,
                "avg_compression_ratio": None,
            },
            "_note": (
                "Cache counters are not yet exposed by the gateway. "
                "Fields are null in v1; will be wired in a v2 schema bump. "
                "v1.1 adds replay economy fields (request_cache, wire_compaction)."
            ),
        }

    # ── disk block ───────────────────────────────────────────────────────
    free, total, used_pct, disk_err = _disk_snapshot(hermes_home)
    if disk_err:
        errors.append({"field": "disk", "reason": disk_err})
    disk: dict[str, Any] = {
        "hermes_home_free_bytes": free,
        "hermes_home_free_human": _human_bytes(free),
        "hermes_home_total_bytes": total,
        "hermes_home_total_human": _human_bytes(total),
        "hermes_home_used_pct": used_pct,
    }

    # ── assemble + stamp ─────────────────────────────────────────────────
    snapshot: dict[str, Any] = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "gateway": {
            "pid": pid,
            "status": "running" if pid is not None else "stopped",
            "rss_bytes": rss_bytes,
            "rss_human": _human_bytes(rss_bytes),
            "threads": threads,
            # uptime_seconds is authoritative in the runtime block
            # (derived from psutil create_time when available). Mirror
            # it here for consumers that only read the gateway block.
            "uptime_seconds": runtime.get("uptime_seconds"),
        },
        "runtime": runtime,
        "cache": cache,
        "disk": disk,
        "errors": errors,
    }
    return snapshot


def render_json(snapshot: dict[str, Any]) -> str:
    """Render the snapshot as a deterministic JSON string.

    sort_keys=True keeps the output stable across runs (handy for
    diffing). The two-space indent matches what most CLI users expect
    when piping to ``less`` or ``jq``.
    """
    return json.dumps(snapshot, indent=2, sort_keys=True, default=str)
