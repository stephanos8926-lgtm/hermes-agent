"""Replay Economy — Request-level tool-result cache (D-1) + Wire-time compaction (D-2).

This module implements the replay-economy spec v2:
- D-1: In-process LRU cache for tool results, keyed by (tool_name, args).
  Reuses the existing InProcessLRUCache from agent/_cache.py.
- D-2: Wire-time compaction of oversized tool messages using LCM externalize.
  Reuses the existing LCM externalize primitives.
- M2 v1.1: Exposes counters for the gateway snapshot.

All configuration via HERMES_REPLAY_* environment variables (mirrored in
config.yaml under replay.* block). No provider-specific code.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration — env-var overrides (HERMES_REPLAY_*)
# ---------------------------------------------------------------------------

# D-1: Request cache
REPLAY_CACHE_L1_ENABLED = os.environ.get("HERMES_REPLAY_CACHE_L1_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
REPLAY_CACHE_MAX_BYTES = int(os.environ.get("HERMES_REPLAY_CACHE_MAX_BYTES", "52428800"))  # 50 MiB
REPLAY_CACHE_MAX_ENTRY_BYTES = int(os.environ.get("HERMES_REPLAY_CACHE_MAX_ENTRY_BYTES", "1048576"))  # 1 MiB

# D-2: Wire compaction
REPLAY_COMPACTION_ENABLED = os.environ.get("HERMES_REPLAY_COMPACTION_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
REPLAY_COMPACTION_THRESHOLD_CHARS = int(os.environ.get("HERMES_REPLAY_COMPACTION_THRESHOLD_CHARS", "100000"))
REPLAY_COMPACTION_HEAD_CHARS = int(os.environ.get("HERMES_REPLAY_COMPACTION_HEAD_CHARS", "2000"))
REPLAY_COMPACTION_TAIL_CHARS = int(os.environ.get("HERMES_REPLAY_COMPACTION_TAIL_CHARS", "2000"))

# LCM externalize availability (detected at startup)
_LCM_EXTERNALIZE_AVAILABLE: Optional[bool] = None
_LCM_EXTERNALIZE_FN = None
_LCM_CONFIG = None
_HERMES_HOME = None


# ---------------------------------------------------------------------------
# Default-non-cacheable tools (v1 conservative list)
# ---------------------------------------------------------------------------

DEFAULT_NON_CACHEABLE_TOOLS = frozenset({
    "bash",
    "shell",
    "terminal",
    "datetime",
    "time",
    "now",
    "random",
    "uuid",
    "web_search",
    "web_fetch",
})


# ---------------------------------------------------------------------------
# Counters for M2 v1.1 snapshot
# ---------------------------------------------------------------------------

class ReplayCounters:
    """Thread-safe counters for D-1 and D-2 telemetry."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # D-1 counters
        self.replay_l1_hits = 0
        self.replay_l1_misses = 0
        self.replay_l1_evictions = 0
        self.replay_l1_externalizations = 0
        self.replay_l1_non_cacheable_skips = 0
        # D-2 counters
        self.replay_wire_compactions = 0
        self.replay_wire_bytes_in = 0
        self.replay_wire_bytes_out = 0

    # D-1
    def inc_l1_hit(self) -> None:
        with self._lock:
            self.replay_l1_hits += 1
        _persist_counters()

    def inc_l1_miss(self) -> None:
        with self._lock:
            self.replay_l1_misses += 1
        _persist_counters()

    def inc_l1_eviction(self) -> None:
        with self._lock:
            self.replay_l1_evictions += 1
        _persist_counters()

    def inc_l1_externalization(self) -> None:
        with self._lock:
            self.replay_l1_externalizations += 1
        _persist_counters()

    def inc_l1_non_cacheable_skip(self) -> None:
        with self._lock:
            self.replay_l1_non_cacheable_skips += 1
        _persist_counters()

    # D-2
    def inc_wire_compaction(self, bytes_in: int, bytes_out: int) -> None:
        with self._lock:
            self.replay_wire_compactions += 1
            self.replay_wire_bytes_in += bytes_in
            self.replay_wire_bytes_out += bytes_out
        _persist_counters()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            total = self.replay_l1_hits + self.replay_l1_misses
            hit_rate = (self.replay_l1_hits / total * 100.0) if total else 0.0
            bytes_saved = self.replay_wire_bytes_in - self.replay_wire_bytes_out
            compression_ratio = (100.0 * bytes_saved / self.replay_wire_bytes_in) if self.replay_wire_bytes_in else 0.0
            return {
                "replay_l1": {
                    "hits": self.replay_l1_hits,
                    "misses": self.replay_l1_misses,
                    "hit_rate_pct": round(hit_rate, 2),
                    "evictions": self.replay_l1_evictions,
                    "externalizations": self.replay_l1_externalizations,
                    "non_cacheable_skips": self.replay_l1_non_cacheable_skips,
                },
                "replay_wire": {
                    "compactions": self.replay_wire_compactions,
                    "bytes_in": self.replay_wire_bytes_in,
                    "bytes_out": self.replay_wire_bytes_out,
                    "bytes_saved_total": bytes_saved,
                    "compression_ratio_pct": round(compression_ratio, 2),
                },
            }

    def reset(self) -> None:
        with self._lock:
            self.replay_l1_hits = 0
            self.replay_l1_misses = 0
            self.replay_l1_evictions = 0
            self.replay_l1_externalizations = 0
            self.replay_l1_non_cacheable_skips = 0
            self.replay_wire_compactions = 0
            self.replay_wire_bytes_in = 0
            self.replay_wire_bytes_out = 0


# Global counters instance
counters = ReplayCounters()


# ---------------------------------------------------------------------------
# LCM externalize detection + fallback
# ---------------------------------------------------------------------------

def _detect_lcm_externalize() -> tuple[bool, Optional[callable], Optional[Any], Optional[Path]]:
    """Detect if LCM externalize is callable from this process.

    Returns (available, function, config, hermes_home).
    """
    global _LCM_EXTERNALIZE_AVAILABLE, _LCM_EXTERNALIZE_FN, _LCM_CONFIG, _HERMES_HOME

    if _LCM_EXTERNALIZE_AVAILABLE is not None:
        return _LCM_EXTERNALIZE_AVAILABLE, _LCM_EXTERNALIZE_FN, _LCM_CONFIG, _HERMES_HOME

    try:
        from hermes_lcm.externalize import maybe_externalize_tool_output
        from hermes_lcm.config import LCMConfig
        from hermes_constants import get_hermes_home

        _LCM_EXTERNALIZE_FN = maybe_externalize_tool_output
        _LCM_CONFIG = LCMConfig()
        _HERMES_HOME = get_hermes_home()
        _LCM_EXTERNALIZE_AVAILABLE = True
        logger.info("Replay Economy: LCM externalize API available (maybe_externalize_tool_output)")
        return True, _LCM_EXTERNALIZE_FN, _LCM_CONFIG, _HERMES_HOME
    except Exception as exc:
        logger.warning("Replay Economy: LCM externalize not available (%s); using temp-file fallback", exc)
        _LCM_EXTERNALIZE_AVAILABLE = False
        _LCM_EXTERNALIZE_FN = None
        _LCM_CONFIG = None
        _HERMES_HOME = Path("~/.hermes").expanduser()
        return False, None, None, _HERMES_HOME


# ---------------------------------------------------------------------------
# Cache key generation
# ---------------------------------------------------------------------------

def _make_cache_key(tool_name: str, args: dict[str, Any]) -> str:
    """Generate a deterministic cache key for (tool_name, args).

    Uses sha256(tool_name + '\x00' + json.dumps(args, sort_keys=True, separators=(',', ':'), default=str))
    """
    # Canonical JSON serialization
    args_json = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    key_material = tool_name + "\x00" + args_json
    return hashlib.sha256(key_material.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# D-1: Request-level cache
# ---------------------------------------------------------------------------

# Lazy-initialized cache instance
_request_cache: Optional[Any] = None
_cache_lock = threading.Lock()


def _get_request_cache() -> Optional[Any]:
    """Get or create the InProcessLRUCache for D-1."""
    global _request_cache
    if not REPLAY_CACHE_L1_ENABLED:
        return None

    with _cache_lock:
        if _request_cache is not None:
            return _request_cache
        try:
            from agent._cache import InProcessLRUCache
            _request_cache = InProcessLRUCache(
                max_entries=10000,  # Large entry count; byte budget is the real limit
                max_bytes=REPLAY_CACHE_MAX_BYTES,
                value_max_bytes=REPLAY_CACHE_MAX_ENTRY_BYTES,
            )
            logger.info("Replay Economy: D-1 request cache initialized (max_bytes=%d, max_entry_bytes=%d)",
                        REPLAY_CACHE_MAX_BYTES, REPLAY_CACHE_MAX_ENTRY_BYTES)
            return _request_cache
        except Exception as exc:
            logger.warning("Replay Economy: Failed to initialize request cache: %s", exc)
            return None


def _is_cacheable_tool(tool_name: str) -> bool:
    """Check if a tool is cacheable (not on the default-non-cacheable list)."""
    return tool_name not in DEFAULT_NON_CACHEABLE_TOOLS


def _externalize_via_lcm(content: str, tool_call_id: str, session_id: str) -> Optional[str]:
    """Externalize content via LCM, return the lcm://replay/<hash> ref or None on failure."""
    available, fn, config, hermes_home = _detect_lcm_externalize()
    if not available or fn is None:
        return None
    try:
        # Use the tool-output wrapper which handles the lcm://replay/ namespace
        ref = fn(
            content=content,
            tool_call_id=tool_call_id,
            session_id=session_id,
            config=config,
            hermes_home=hermes_home,
            force=False,
        )
        if ref and ref.startswith("lcm://"):
            return ref
        return None
    except Exception as exc:
        logger.warning("Replay Economy: LCM externalize failed: %s", exc)
        return None


def _externalize_via_tempfile(content: str, tool_call_id: str) -> str:
    """Fallback: write content to a temp file and return the file path."""
    import tempfile
    tmp_dir = Path("/tmp/hermes-replay")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    # Use tool_call_id + hash for uniqueness
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    tmp_path = tmp_dir / f"{tool_call_id}_{content_hash}.txt"
    try:
        tmp_path.write_text(content, encoding="utf-8")
        return f"file://{tmp_path}"
    except Exception as exc:
        logger.warning("Replay Economy: Temp-file fallback failed: %s", exc)
        return ""


def _store_in_cache(cache, key: str, tool_result: dict[str, Any], session_id: str) -> None:
    """Store a tool result in the cache, externalizing if oversized."""
    content = tool_result.get("content", "")
    tool_call_id = tool_result.get("tool_call_id", "")

    # Check if content exceeds max_entry_bytes
    content_bytes = len(content.encode("utf-8"))
    if content_bytes > REPLAY_CACHE_MAX_ENTRY_BYTES:
        # Externalize
        lcm_ref = _externalize_via_lcm(content, tool_call_id, session_id)
        if lcm_ref:
            # Store compacted version with LCM ref
            head = content[:REPLAY_COMPACTION_HEAD_CHARS]
            tail = content[-REPLAY_COMPACTION_TAIL_CHARS:] if len(content) > REPLAY_COMPACTION_TAIL_CHARS else ""
            truncated_count = len(content) - len(head) - len(tail)
            compacted_content = (
                f"{head}\n\n"
                f"[... {truncated_count} chars truncated, recover via lcm_expand(ref={lcm_ref}) ...]\n\n"
                f"{tail}"
            )
            stored_result = {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": compacted_content,
            }
            cache.put(key, stored_result)
            counters.inc_l1_externalization()
            return
        else:
            # LCM failed, try temp-file fallback
            file_ref = _externalize_via_tempfile(content, tool_call_id)
            if file_ref:
                head = content[:REPLAY_COMPACTION_HEAD_CHARS]
                tail = content[-REPLAY_COMPACTION_TAIL_CHARS:] if len(content) > REPLAY_COMPACTION_TAIL_CHARS else ""
                truncated_count = len(content) - len(head) - len(tail)
                compacted_content = (
                    f"{head}\n\n"
                    f"[... {truncated_count} chars truncated, recover via file at {file_ref} ...]\n\n"
                    f"{tail}"
                )
                stored_result = {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": compacted_content,
                }
                cache.put(key, stored_result)
                counters.inc_l1_externalization()
                return
            # Both failed — skip caching if too large
            logger.warning("Replay Economy: Oversized result (%d bytes) and externalization failed; skipping cache", content_bytes)
            return

    # Normal case: store full content
    cache.put(key, tool_result)


def cache_check(tool_name: str, args: dict[str, Any], session_id: str) -> Optional[dict[str, Any]]:
    """D-1 cache check: return cached tool result if available, else None.

    Called BEFORE tool execution. Returns a tool result dict with role/tool_call_id/content
    if hit, None if miss or not cacheable.
    """
    if not REPLAY_CACHE_L1_ENABLED:
        return None

    if not _is_cacheable_tool(tool_name):
        counters.inc_l1_non_cacheable_skip()
        return None

    cache = _get_request_cache()
    if cache is None:
        return None

    key = _make_cache_key(tool_name, args)
    try:
        result = cache.get(key)
        if result is not None:
            # Cache hit — return a copy with a fresh tool_call_id (the downstream
            # code expects a new ID for each turn)
            hit_result = dict(result)
            # Note: we preserve the original tool_call_id for traceability,
            # but the caller may override it. The shape-equivalence contract
            # (T-D1-9) allows tool_call_id to differ.
            counters.inc_l1_hit()
            logger.debug("Replay Economy: D-1 cache HIT for %s (key=%s...)", tool_name, key[:16])
            return hit_result
        else:
            counters.inc_l1_miss()
            logger.debug("Replay Economy: D-1 cache MISS for %s (key=%s...)", tool_name, key[:16])
            return None
    except Exception as exc:
        logger.warning("Replay Economy: D-1 cache error (fail-open): %s", exc)
        return None


def cache_store(tool_name: str, args: dict[str, Any], tool_result: dict[str, Any], session_id: str) -> None:
    """D-1 cache store: store tool result after execution.

    Called AFTER tool execution (on cache miss path).
    """
    if not REPLAY_CACHE_L1_ENABLED:
        return

    if not _is_cacheable_tool(tool_name):
        return

    cache = _get_request_cache()
    if cache is None:
        return

    key = _make_cache_key(tool_name, args)
    try:
        _store_in_cache(cache, key, tool_result, session_id)
    except Exception as exc:
        logger.warning("Replay Economy: D-1 cache store error (fail-open): %s", exc)


# ---------------------------------------------------------------------------
# D-2: Wire-time compaction
# ---------------------------------------------------------------------------

def _compact_tool_content(content: str, tool_call_id: str, session_id: str) -> tuple[str, int, int]:
    """Compact a single tool message's content if over threshold.

    Returns (compacted_content, bytes_in, bytes_out).
    If not compacted, returns (original_content, 0, 0).
    """
    if not REPLAY_COMPACTION_ENABLED:
        return content, 0, 0

    if not isinstance(content, str):
        return content, 0, 0

    content_len = len(content)
    if content_len <= REPLAY_COMPACTION_THRESHOLD_CHARS:
        return content, 0, 0

    # Externalize the FULL original content (not the compacted version)
    lcm_ref = _externalize_via_lcm(content, tool_call_id, session_id)
    if lcm_ref is None:
        # Fallback to temp file
        file_ref = _externalize_via_tempfile(content, tool_call_id)
        if file_ref:
            lcm_ref = file_ref
        else:
            # Both failed — skip compaction
            logger.warning("Replay Economy: D-2 externalization failed for tool_call_id=%s; sending original", tool_call_id)
            return content, 0, 0

    head = content[:REPLAY_COMPACTION_HEAD_CHARS]
    tail = content[-REPLAY_COMPACTION_TAIL_CHARS:] if content_len > REPLAY_COMPACTION_TAIL_CHARS else ""
    truncated_count = content_len - len(head) - len(tail)

    compacted = (
        f"{head}\n\n"
        f"[... {truncated_count} chars truncated, recover via lcm_expand(ref={lcm_ref}) ...]\n\n"
        f"{tail}"
    )

    bytes_in = content_len
    bytes_out = len(compacted)
    return compacted, bytes_in, bytes_out


def compact_tool_messages(messages: list[dict[str, Any]], session_id: str) -> list[dict[str, Any]]:
    """D-2 wire compaction: process all tool messages in the message list.

    Called just before provider serialization. Returns a new list (immutable transform).
    """
    if not REPLAY_COMPACTION_ENABLED:
        return messages

    out = []
    total_bytes_in = 0
    total_bytes_out = 0

    for msg in messages:
        if not isinstance(msg, dict):
            out.append(msg)
            continue
        if msg.get("role") != "tool":
            out.append(msg)
            continue

        content = msg.get("content", "")
        tool_call_id = msg.get("tool_call_id", "")

        if not isinstance(content, str):
            # Non-string content (multimodal) — skip compaction
            out.append(msg)
            continue

        compacted, bytes_in, bytes_out = _compact_tool_content(content, tool_call_id, session_id)
        if bytes_in > 0:
            # Compaction happened
            new_msg = dict(msg)
            new_msg["content"] = compacted
            out.append(new_msg)
            total_bytes_in += bytes_in
            total_bytes_out += bytes_out
        else:
            out.append(msg)

    if total_bytes_in > 0:
        counters.inc_wire_compaction(total_bytes_in, total_bytes_out)

    return out


# ---------------------------------------------------------------------------
# Public API for gateway snapshot (M2 v1.1)
# ---------------------------------------------------------------------------

def _get_counters_file() -> Path:
    """Get the path to the replay economy counters file."""
    from hermes_constants import get_hermes_home
    data_dir = get_hermes_home() / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "replay_economy_counters.json"


def _persist_counters() -> None:
    """Persist counters to file for gateway snapshot to read."""
    try:
        data = counters.snapshot()
        data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _get_counters_file().write_text(json.dumps(data), encoding="utf-8")
    except Exception as exc:
        logger.debug("Replay Economy: Failed to persist counters: %s", exc)


def get_replay_counters() -> dict[str, Any]:
    """Return the current replay-economy counters for the gateway snapshot."""
    return counters.snapshot()


def reset_replay_counters() -> None:
    """Reset all replay-economy counters (used on gateway restart)."""
    counters.reset()
    _persist_counters()


# ---------------------------------------------------------------------------
# Module initialization (called on import)
# ---------------------------------------------------------------------------

# Detect LCM availability at import time
_detect_lcm_externalize()

# Export public API
__all__ = [
    "cache_check",
    "cache_store",
    "compact_tool_messages",
    "get_replay_counters",
    "reset_replay_counters",
    "REPLAY_CACHE_L1_ENABLED",
    "REPLAY_COMPACTION_ENABLED",
]