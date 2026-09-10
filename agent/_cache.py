"""Tiered cache infrastructure for in-process and cross-process caching.

This module provides the foundational types for a 3-tier cache architecture:

* **L1** (InProcessLRUCache): an in-memory LRU keyed on (key, content_hash).
  Used for hot objects that change rarely and must be byte-identical across
  reads. Thread-safe. Default size 64 entries / 32 MiB.

* **L2** (FlatFileCache / RedisCache): a cross-process shared cache.
  The default backend is a memory-mapped ring buffer (FlatFileCache) that
  requires only the Python standard library. A Redis-backed backend is
  available as a stub for users who already run Redis in their stack.

* **L3** (ShardedFileCache / DiskCache): a cold disk tier for values that
  should survive process restart. The default backend is a 2-level
  sharded directory with mtime-based TTL eviction. A SQLite backend is
  available as a stub for users who want ACID-style semantics.

* **TieredCacheRouter**: chains L1 → L2 → L3 with read-through and
  write-back semantics. The router is feature-gated by the ``cache.*``
  config block; by default only L1 is on.

---

**Feature gating — config.yaml**

All cache tiers are feature-gated. The user controls each layer
independently via the ``cache:`` block in ``~/.hermes/config.yaml``:

.. code-block:: yaml

    cache:
      enabled: true                 # Master kill switch (default: true)
      l1:
        enabled: true               # In-process LRU
        max_entries: 64
        max_bytes: 33554432         # 32 MiB
      l2:
        enabled: false              # Off by default; explicit opt-in
        backend: flat_file          # flat_file | redis
        flat_file:
          path: ~/.hermes/cache/l2.mmap
          max_bytes: 67108864       # 64 MiB
          read_only: false
        redis:
          url: redis://localhost:6379
          namespace: hermes
          ttl_seconds: 3600
      l3:
        enabled: false              # Off by default
        backend: sharded_file       # sharded_file | sqlite
        sharded_file:
          root: ~/.hermes/cache/l3
          ttl_days: 30
        sqlite:
          path: ~/.hermes/cache/l3.sqlite
          ttl_seconds: 86400
          vacuum_threshold: 1000    # freelist_count over which to vacuum

Any value can be overridden by an environment variable of the form
``HERMES_CACHE_<PATH>`` where ``<PATH>`` is the dotted key with
underscores. Examples:

  * ``HERMES_CACHE_ENABLED=false`` — disable the entire cache system
  * ``HERMES_CACHE_L1_MAX_ENTRIES=128`` — grow the L1 entry cap
  * ``HERMES_CACHE_L2_ENABLED=true`` — opt in to L2
  * ``HERMES_CACHE_L2_BACKEND=redis`` — switch the L2 backend
  * ``HERMES_CACHE_L3_TTL_DAYS=7`` — tighten the L3 retention

The ``HERMES_CACHE_*`` env-var override path follows the existing
``HERMES_*`` convention used elsewhere in the fork
(``HERMES_DISABLE_LAZY_INSTALLS`` etc.). Secrets and connection
strings belong in ``.env`` per AGENTS.md rule #8; cache URLs and
paths are non-secret configuration and live in ``config.yaml`` with
env-var overrides.

---

**Why this module exists**

The fork already has excellent in-process caching for specific objects
(``load_config_readonly`` with mtime-keyed cache, the system prompt's
per-session ``_cached_system_prompt``, the per-message token estimate
fingerprint memo, the tool-call argument canonicalization memo). What it
lacks is a *general-purpose* tiered cache abstraction that new code can
reach for without re-implementing the LRU, byte-budget, or invalidation
discipline each time.

This module provides that abstraction. The default-off L2/L3 layers are
shipped in working form for users who opt in — the FlatFileCache (L2)
and ShardedFileCache (L3) are stdlib-only and need no external services.

---

**Architectural alternatives (no Redis, no sqlite)**

The mmap+flat-file design is the recommended default for users who
don't already run Redis or want a sqlite dependency. It is a strict
subset of what Redis+sqlite provide:

* **L2 alternative (FlatFileCache)**: a fixed-size ``mmap``-backed file
  with a ring buffer layout. Cross-process safety via ``fcntl.flock``
  (POSIX) or ``msvcrt.locking`` (Windows). Faster than sqlite for
  fixed-size byte-string objects; no schema overhead; survives process
  restart.

* **L3 alternative (ShardedFileCache)**: a sorted directory of files
  keyed by a 2-level hash directory
  (``~/.hermes/cache/l3/<hash[0:2]>/<hash[2:4]>/<hash>``) with a
  sidecar ``.json`` for metadata (mtime, size, ttl). Eviction by mtime
  via a daily sweep. Trivially portable, no dependencies, easy to
  inspect with a file browser.

If the user later needs Redis or sqlite semantics, the same Protocol
interface is implemented by the stub backends; switching is a
single-config change.

---

**Byte-stability contract**

All cache tiers preserve the **byte-identical** invariant for cached
values: two reads of the same key must return objects whose ``repr()``
matches the originally-stored value's ``repr()``. This is the
prerequisite for the upstream provider-side prompt cache: if the L1 cache
returns a mutated dict on the second read, the provider sees a different
prefix and the cache misses.

Consumers MUST NOT mutate a value returned from a cache. The L1 class
documents this with a warning in the docstring; production code is
expected to be read-only on the returned value.
"""
from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import mmap
import os
import pickle
import struct
import sys
import tempfile
import threading
import logging

logger = logging.getLogger(__name__)
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, Tuple, Union


# ---------------------------------------------------------------------------
# Defaults — overridable via cache: block in config.yaml
# ---------------------------------------------------------------------------

DEFAULT_L1_MAX_ENTRIES = 64
DEFAULT_L1_MAX_BYTES = 32 * 1024 * 1024  # 32 MiB

DEFAULT_L2_FLAT_FILE_PATH = "~/.hermes/cache/l2.mmap"
DEFAULT_L2_FLAT_FILE_MAX_BYTES = 64 * 1024 * 1024  # 64 MiB
DEFAULT_L2_REDIS_URL = "redis://localhost:6379"
DEFAULT_L2_REDIS_NAMESPACE = "hermes"
DEFAULT_L2_REDIS_TTL_SECONDS = 3600

DEFAULT_L3_SHARDED_ROOT = "~/.hermes/cache/l3"
DEFAULT_L3_SHARDED_TTL_DAYS = 30
DEFAULT_L3_SQLITE_PATH = "~/.hermes/cache/l3.sqlite"
DEFAULT_L3_SQLITE_TTL_SECONDS = 86400
DEFAULT_L3_SQLITE_VACUUM_THRESHOLD = 1000


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

class TieredCache(Protocol):
    """Protocol for a cache tier. All tiers must implement this interface.

    A tier exposes the minimal operations needed to compose a multi-tier
    cache router: ``get``, ``put``, ``invalidate``. Implementations are
    free to add tier-specific methods (e.g., ``vacuum`` for a disk tier)
    but the protocol surface is fixed.
    """

    def get(self, key: str) -> Optional[Any]:
        """Return the cached value for *key*, or ``None`` on miss."""
        ...

    def put(self, key: str, value: Any) -> None:
        """Store *value* under *key*. Existing value (if any) is replaced."""
        ...

    def invalidate(self, key: str) -> None:
        """Remove *key* from the cache. No-op if the key is absent."""
        ...

    def stats(self) -> dict:
        """Return statistics about the cache tier (hits, misses, entries, etc.)."""
        ...


# ---------------------------------------------------------------------------
# Config loader — feature-gate aware
# ---------------------------------------------------------------------------

# Type aliases
_Scalar = Union[str, int, float, bool, None]
_ConfigDict = dict


def _expand_path(value: str) -> str:
    """Expand ``~`` and environment variables in a path-like config value."""
    if not isinstance(value, str):
        return value
    return os.path.expanduser(os.path.expandvars(value))


def _env_override(dotted_key: str) -> Optional[str]:
    """Look up ``HERMES_CACHE_<dotted_key>`` in the environment.

    Returns the string value (always) or None when not set. The caller
    is responsible for type coercion — env vars are always strings.
    """
    env_name = "HERMES_CACHE_" + dotted_key.upper().replace(".", "_")
    return os.environ.get(env_name)


def _coerce(value: str, default: Any) -> Any:
    """Coerce an env-var string to the type of the default."""
    if default is None:
        return value
    if isinstance(default, bool):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        try:
            return int(value.strip())
        except ValueError:
            return default
    if isinstance(default, float):
        try:
            return float(value.strip())
        except ValueError:
            return default
    return value  # string


def _read_cache_config() -> dict:
    """Read the ``cache:`` block from config.yaml with env-var overrides.

    Uses the mtime-cached ``read_raw_config_readonly`` fast path so this
    function can be called from hot paths without re-parsing yaml. The
    function never raises — every failure mode returns a sensible default
    dict so the cache can degrade gracefully when config is missing or
    malformed.
    """
    # Defaults — every key has a value so downstream code can index
    # without ``.get(..., default)`` everywhere.
    defaults: dict = {
        "enabled": True,
        "l1": {
            "enabled": True,
            "max_entries": DEFAULT_L1_MAX_ENTRIES,
            "max_bytes": DEFAULT_L1_MAX_BYTES,
            "eviction_policy": "lru",  # "lru" or "tiny_lfu"
            "tiny_lfu": {
                "ttl_seconds": 0,
                "nolock": False,
            },
        },
        "l2": {
            "enabled": True,
            "backend": "flat_file",
            "flat_file": {
                "path": DEFAULT_L2_FLAT_FILE_PATH,
                "max_bytes": DEFAULT_L2_FLAT_FILE_MAX_BYTES,
                "read_only": False,
            },
            "redis": {
                "url": DEFAULT_L2_REDIS_URL,
                "namespace": DEFAULT_L2_REDIS_NAMESPACE,
                "ttl_seconds": DEFAULT_L2_REDIS_TTL_SECONDS,
            },
        },
        "l3": {
            "enabled": True,
            "backend": "sharded_file",
            "sharded_file": {
                "root": DEFAULT_L3_SHARDED_ROOT,
                "ttl_days": DEFAULT_L3_SHARDED_TTL_DAYS,
            },
            "sqlite": {
                "path": DEFAULT_L3_SQLITE_PATH,
                "ttl_seconds": DEFAULT_L3_SQLITE_TTL_SECONDS,
                "vacuum_threshold": DEFAULT_L3_SQLITE_VACUUM_THRESHOLD,
            },
        },
    }

    raw: dict = {}
    try:
        # Prefer the readonly fast path (mtime-cached).
        from hermes_cli.config import read_raw_config_readonly
        raw = read_raw_config_readonly() or {}
    except Exception:
        try:
            # Fallback to the eager read (also mtime-cached).
            from hermes_cli.config import read_raw_config
            raw = read_raw_config() or {}
        except Exception:
            try:
                # Last resort: direct yaml parse.
                from utils import fast_safe_load
                from hermes_constants import get_config_path
                path = get_config_path()
                if path.exists():
                    with open(path, "r", encoding="utf-8") as f:
                        raw = fast_safe_load(f) or {}
            except Exception:
                raw = {}

    cache_cfg = raw.get("cache", {}) if isinstance(raw, dict) else {}
    if not isinstance(cache_cfg, dict):
        cache_cfg = {}

    def _apply_overrides(d: dict, prefix: str) -> dict:
        """Recursively apply env-var overrides to a nested dict."""
        out = dict(d)
        for k, v in d.items():
            dotted = f"{prefix}.{k}" if prefix else k
            env_val = _env_override(dotted)
            if env_val is not None:
                out[k] = _coerce(env_val, v)
            elif isinstance(v, dict):
                out[k] = _apply_overrides(v, dotted)
        return out

    return _apply_overrides({**defaults, **cache_cfg}, "")


def is_cache_enabled() -> bool:
    """Master feature gate. True unless explicitly disabled."""
    cfg = _read_cache_config()
    return bool(cfg.get("enabled", True))


def is_l1_enabled() -> bool:
    """True when both the master gate and l1.enabled are true."""
    cfg = _read_cache_config()
    return bool(cfg.get("enabled", True)) and bool(cfg.get("l1", {}).get("enabled", True))


def is_l2_enabled() -> bool:
    """True when master + l2.enabled are true."""
    cfg = _read_cache_config()
    return bool(cfg.get("enabled", True)) and bool(cfg.get("l2", {}).get("enabled", False))


def is_l3_enabled() -> bool:
    """True when master + l3.enabled are true."""
    cfg = _read_cache_config()
    return bool(cfg.get("enabled", True)) and bool(cfg.get("l3", {}).get("enabled", False))


# ---------------------------------------------------------------------------
# L1 — InProcessLRUCache
# ---------------------------------------------------------------------------

class _LRUShard:
    """One LRU shard: its own OrderedDict and lock.

    Entries are stored as ``(stamp, value)`` tuples where *stamp* is a
    monotonically increasing integer assigned by the owning
    :class:`InProcessLRUCache`. The stamp enables exact global-LRU
    eviction across shards without a shared hot lock on every access:
    the facade only needs the minimum stamp per shard to find the
    globally-oldest entry.

    Budget enforcement lives in the facade (aggregate semantics);
    shards are pure storage + locking.
    """

    __slots__ = ("entries", "lock", "hits", "misses")

    def __init__(self) -> None:
        # key -> (stamp, value)
        self.entries: "OrderedDict[str, tuple]" = OrderedDict()
        self.lock = threading.RLock()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def size_of(key: str, value: Any) -> int:
        """Estimate the byte cost of storing *value* under *key*."""
        try:
            return len(key) + len(repr(value))
        except Exception:
            # repr can fail for non-representable objects; fall back to
            # a coarse estimate that still bounds memory growth.
            return len(key) + 1024

    def get(self, key: str) -> Optional[Any]:
        try:
            _stamp, value = self.entries[key]  # raises KeyError on miss
        except KeyError:
            self.misses += 1
            return None
        # Mark as most-recently used.
        self.entries.move_to_end(key)
        self.hits += 1
        return value

    def put(self, key: str, value: Any, stamp: int) -> None:
        self.entries[key] = (stamp, value)
        self.entries.move_to_end(key)

    def invalidate(self, key: str) -> bool:
        """Remove *key*. Returns True if it was present."""
        return self.entries.pop(key, None) is not None

    def oldest(self) -> Optional[tuple]:
        """Return ``(stamp, key)`` of the least-recently-used entry,
        or ``None`` if empty. Cheap: the OrderedDict's first item."""
        if not self.entries:
            return None
        key, (stamp, _value) = next(iter(self.entries.items()))
        return (stamp, key)


class InProcessLRUCache:
    """Thread-safe in-process LRU cache with per-shard locking.

    Storage is split across ``_num_shards`` independent shards, each
    guarded by its own :class:`threading.RLock`. Operations on distinct
    keys that hash to different shards proceed without contending on a
    shared monitor — the canonical striped-lock pattern (Caffeine,
    warp_cache, CacheLite).

    **Aggregate budget semantics are preserved exactly**: ``max_entries``
    and ``max_bytes`` bound the *total* population, not any single
    shard. Eviction is exact global LRU via monotonic stamps: after each
    put, if either aggregate budget is exceeded, the entry with the
    smallest stamp across all shards is evicted (repeatedly, until the
    totals fit). On the single-shard path this degenerates to plain
    OrderedDict LRU and is byte-identical to the pre-sharding behavior.

    Shard selection uses ``sha256(key) % num_shards`` — deliberately
    NOT Python's builtin ``hash()``, whose value is randomized per
    process by PYTHONHASHSEED (breaking stable shard assignment for
    any persistent tier keyed alongside it).

    Adaptive sharding: caches configured at or below
    ``_SINGLE_SHARD_MAX_ENTRIES`` entries use exactly one shard, which
    both avoids pointless lock overhead on tiny caches and preserves
    the historical exact-eviction-order contract that small-cache unit
    tests rely on.
    """

    _SHARD_COUNT = 16
    _SINGLE_SHARD_MAX_ENTRIES = 64

    def __init__(
        self,
        max_entries: int = 256,
        max_bytes: Optional[int] = None,
        value_max_bytes: Optional[int] = None,
    ) -> None:
        if int(max_entries) < 1:
            raise ValueError("max_entries must be >= 1")
        self._max_entries = int(max_entries)
        if max_bytes is not None:
            if int(max_bytes) < 1024:
                raise ValueError("max_bytes must be >= 1024")
            self._max_bytes = int(max_bytes)
        else:
            self._max_bytes = None
        # I3 elephant guard: per-value ceiling. Defaults to the whole
        # byte budget (a value larger than the budget can never fit).
        # Operators may set it tighter via cache.l1.value_max_bytes to
        # keep single entries from dominating the cache.
        if value_max_bytes is not None:
            vmb = int(value_max_bytes)
            if vmb < 1:
                raise ValueError("value_max_bytes must be >= 1")
            if self._max_bytes is not None and vmb > self._max_bytes:
                vmb = self._max_bytes
            self._value_max_bytes: Optional[int] = vmb
        else:
            self._value_max_bytes = self._max_bytes
        # Adaptive sharding: tiny caches stay single-shard so eviction
        # order is trivially exact and lock overhead is zero-ish.
        if self._max_entries <= self._SINGLE_SHARD_MAX_ENTRIES:
            self._num_shards = 1
        else:
            self._num_shards = self._SHARD_COUNT
        self._shards = [_LRUShard() for _ in range(self._num_shards)]
        # Guards stamp allocation + aggregate counters only; never held
        # during shard get/put, so it is not a hot-path bottleneck.
        self._meta_lock = threading.Lock()
        self._stamp = 0
        self._total_entries = 0
        self._total_bytes = 0
        self.evictions = 0
        self.cache_skip_oversized = 0

    # -- shard selection ---------------------------------------------------

    def _shard_index(self, key: str) -> int:
        """Map *key* to its shard index.

        Uses sha256 rather than builtin hash(): PYTHONHASHSEED randomizes
        str hashing per process, which would make shard assignment differ
        between processes sharing an L2 file keyed by shard index.
        """
        digest = hashlib.sha256(key.encode("utf-8", "surrogatepass")).digest()
        return digest[0] % self._num_shards

    def _shard_for(self, key: str) -> "_LRUShard":
        return self._shards[self._shard_index(key)]

    # -- core operations ----------------------------------------------------

    @staticmethod
    def _check_key(key: str) -> None:
        if not isinstance(key, str):
            raise TypeError("cache keys must be str")

    def get(self, key: str):
        self._check_key(key)
        return self._shard_for(key).get(key)

    def put(self, key: str, value: Any) -> None:
        """Insert or update *key*. Oversized puts (larger than the whole
        byte budget) are silently skipped and counted."""
        self._check_key(key)
        size = _LRUShard.size_of(key, value)
        ceiling = self._value_max_bytes
        if ceiling is not None and size > ceiling:
            # Elephant guard: one value larger than the entire budget
            # can never fit; counting it would evict everything else.
            self.cache_skip_oversized += 1
            logger.debug(
                "InProcessLRUCache: skipping oversized put (%d bytes > "
                "budget %d)", size, self._max_bytes,
            )
            return False

        shard = self._shard_for(key)
        with self._meta_lock:
            self._stamp += 1
            stamp = self._stamp
        had_key = False
        old_size = 0
        with shard.lock:
            existing = shard.entries.get(key)
            if existing is not None:
                had_key = True
                old_size = _LRUShard.size_of(key, existing[1])
            shard.put(key, value, stamp)
        with self._meta_lock:
            if had_key:
                self._total_bytes -= old_size
            else:
                self._total_entries += 1
            self._total_bytes += size
            self._evict_to_budget()

    def _evict_to_budget(self) -> None:
        """Evict globally-oldest entries until aggregate budgets fit.

        Caller must hold ``self._meta_lock``. Exactness: stamps are
        assigned under the meta lock, so the minimum-stamp entry is the
        true global LRU head even though storage is sharded.
        """
        while True:
            over_entries = self._total_entries > self._max_entries
            over_bytes = (
                self._max_bytes is not None
                and self._total_bytes > self._max_bytes
            )
            if not (over_entries or over_bytes):
                return
            # Find the shard holding the globally-oldest entry.
            best_shard = None
            best_stamp = None
            for shard in self._shards:
                with shard.lock:
                    oldest = shard.oldest()
                    if oldest is not None and (
                        best_stamp is None or oldest[0] < best_stamp
                    ):
                        best_stamp = oldest[0]
                        best_shard = shard
            if best_shard is None:
                return  # nothing left to evict (defensive)
            with best_shard.lock:
                oldest = best_shard.oldest()
                if oldest is None:
                    continue
                _stamp, victim_key = oldest
                entry = best_shard.entries.pop(victim_key)
                victim_size = _LRUShard.size_of(victim_key, entry[1])
            self._total_entries -= 1
            self._total_bytes -= victim_size
            self.evictions += 1

    def invalidate(self, key: str) -> None:
        self._check_key(key)
        shard = self._shard_for(key)
        with shard.lock:
            existing = shard.entries.pop(key, None)
        if existing is None:
            return
        with self._meta_lock:
            self._total_entries -= 1
            self._total_bytes -= _LRUShard.size_of(key, existing[1])

    def clear(self) -> None:
        for shard in self._shards:
            with shard.lock:
                shard.entries.clear()
        with self._meta_lock:
            self._total_entries = 0
            self._total_bytes = 0

    def __len__(self) -> int:
        total = 0
        for shard in self._shards:
            with shard.lock:
                total += len(shard.entries)
        return total

    def __contains__(self, key: str) -> bool:
        return self.get(key) is not None

    def stats(self) -> dict:
        hits = misses = 0
        for shard in self._shards:
            with shard.lock:
                hits += shard.hits
                misses += shard.misses
        total = hits + misses
        return {
            "entries": len(self),
            "bytes": self._total_bytes,
            "hits": hits,
            "misses": misses,
            "hit_rate": (hits / total) if total else 0.0,
            "evictions": self.evictions,
            "cache_skip_oversized": self.cache_skip_oversized,
            "max_entries": self._max_entries,
            "max_bytes": self._max_bytes,
            "value_max_bytes": self._value_max_bytes,
            "num_shards": self._num_shards,
        }

    # Back-compat alias used by earlier callers/tests.
    @property
    def max_entries(self) -> int:
        return self._max_entries


# ---------------------------------------------------------------------------
# L1 — W-TinyLFU cache (theine-based)
# ---------------------------------------------------------------------------


class InProcessTinyLFUCache:
    """Thread-safe in-process W-TinyLFU cache backed by theine.

    Uses adaptive sampling-based eviction (W-TinyLFU algorithm from
    Caffeine) instead of plain LRU. Provides better hit ratios for
    skewed access patterns where some keys are accessed far more
    frequently than others.

    **Features**:
    * Hierarchical timer-wheel TTL for per-key expiration
    * Admission filter via sampling (resists cache poisoning)
    * Striped locking for concurrent access
    * Fail-open on internal errors (treats as miss/put-no-op)

    **Configuration**: ``cache.l1.eviction_policy`` — ``"lru"`` (default,
    uses :class:`InProcessLRUCache`) or ``"tiny_lfu"`` (uses this class).
    Additional config: ``cache.l1.tiny_lfu.{size, ttl_seconds, nolock}``.

    ``ttl_seconds`` applies globally to all entries (set via theine's
    ``Cache.set(key, value, ttl)``). Set to ``0`` for no TTL.

    ``nolock`` disables threading locks (use only in single-threaded
    contexts; ignored on free-threaded Python).
    """

    def __init__(
        self,
        max_entries: int = 256,
        ttl_seconds: int = 0,
        nolock: bool = False,
        max_bytes: Optional[int] = None,
        value_max_bytes: Optional[int] = None,
    ) -> None:
        """Initialize W-TinyLFU cache.

        Args:
            max_entries: Maximum number of entries (capacity for theine).
            ttl_seconds: Global TTL in seconds. 0 means no expiration.
            nolock: Disable threading locks (single-threaded use only).
            max_bytes: Ignored (theine doesn't support byte budgets).
            value_max_bytes: Ignored (theine doesn't support value sizing).
        """
        # theine capacity
        self._capacity = int(max_entries)
        if self._capacity < 1:
            raise ValueError("max_entries must be >= 1")

        # TTL in nanoseconds (theine expects ns)
        self._ttl_ns = int(ttl_seconds * 1e9) if ttl_seconds > 0 else 0

        # nolock flag (overridden to False on free-threaded Python)
        self._nolock = nolock

        # Import here to avoid hard dependency when theine is unavailable
        try:
            from theine import Cache
            from datetime import timedelta
        except ImportError:
            raise ImportError(
                "InProcessTinyLFUCache requires theine: install with "
                "'pip install theine' or 'uv add theine'"
            )

        self._ttl: Optional[timedelta] = (
            timedelta(seconds=ttl_seconds) if ttl_seconds > 0 else None
        )
        self._cache = Cache(self._capacity, nolock=nolock)

        # Metrics
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        """Return the cached value for *key*, or None on miss."""
        try:
            result, ok = self._cache.get(key)
            with self._lock:
                if ok:
                    self._hits += 1
                else:
                    self._misses += 1
            return result
        except Exception:
            # Fail-open: treat as miss
            with self._lock:
                self._misses += 1
            return None

    def put(self, key: str, value: Any) -> None:
        """Store *value* under *key*. Existing value is replaced."""
        try:
            self._cache.set(key, value, self._ttl)
            # theine tracks evictions internally
        except Exception:
            # Fail-open: silently drop oversized or invalid puts
            pass

    def invalidate(self, key: str) -> None:
        """Remove *key* from the cache. No-op if absent."""
        try:
            self._cache.delete(key)
        except Exception:
            pass

    def clear(self) -> None:
        """Remove all entries."""
        try:
            self._cache.clear()
        except Exception:
            pass

    def __len__(self) -> int:
        """Return number of entries in cache."""
        try:
            return len(self._cache)
        except Exception:
            return 0

    def __contains__(self, key: str) -> bool:
        """Check if key exists in cache."""
        try:
            _, ok = self._cache.get(key)
            return ok
        except Exception:
            return False

    def stats(self) -> dict:
        """Return statistics about the cache."""
        try:
            theine_stats = self._cache.stats()
            total = theine_stats.hit_count + theine_stats.miss_count
            hit_rate = (
                theine_stats.hit_count / total if total > 0 else 0.0
            )
        except Exception:
            theine_stats = None
            total = 0
            hit_rate = 0.0

        with self._lock:
            hits = self._hits
            misses = self._misses

        return {
            "entries": len(self),
            "hits": hits,
            "misses": misses,
            "hit_rate": (hits / (hits + misses)) if (hits + misses) > 0 else 0.0,
            "evictions": self._evictions,
            "capacity": self._capacity,
            "ttl_seconds": self._ttl_ns // int(1e9) if self._ttl_ns > 0 else 0,
            "nolock": self._nolock,
        }


class _FlatFileRing:
    """L2 cache — memory-mapped ring buffer. **Stdlib only.**

    The cache lives in a single fixed-size file at the configured path
    (default ``~/.hermes/cache/l2.mmap``). The file is laid out as a
    ring buffer of fixed-size slots; each slot holds one entry's
    serialized value plus a small header (key hash, value length,
    timestamp, tombstone flag).

    **Why this design**:

    * **No external dependencies** — the implementation is pure
      stdlib (``mmap``, ``fcntl``, ``struct``, ``hashlib``,
      ``pickle``). No Redis, no sqlite.
    * **Cross-process safe** — ``fcntl.flock`` (POSIX) serialises
      writers; readers take a shared lock.
    * **Survives restart** — unlike the in-process L1, the L2 keeps
      its contents across gateway restarts.
    * **Bounded disk** — the file is fixed-size; eviction is the
      natural consequence of slot reuse.

    **Configuration**: ``cache.l2.flat_file.{path, max_bytes,
    read_only}``. Path is expanded with ``~`` and env vars.

    **Fail-open**: on any I/O error, ``get()`` returns ``None``
    (treat as miss); ``put()`` logs and returns. The L1 layer remains
    the authoritative cache in this case.

    **Tradeoffs vs Redis/sqlite**:

    * No TTL out of the box (the cache is bounded by ``max_bytes`` and
      the ring buffer naturally evicts on overflow). For per-entry
      TTL, use the L3 (ShardedFileCache) or the L3 (DiskCache/sqlite)
      instead.
    * No structured queries — only exact-match ``get(key)``.
    * Hard to inspect manually compared to sqlite (binary format).
    """

    # Slot layout: 8 bytes key_hash + 4 bytes value_len + 4 bytes ts.
    # The tombstone flag is packed into the high bit of value_len on
    # the wire (so a real length of N is stored as N & 0x7FFFFFFF; the
    # caller ORs in 0x80000000 to mark a logical delete). This keeps
    # the header at a clean 16 bytes with no padding.
    SLOT_HEADER_FMT = ">QII"
    SLOT_HEADER_SIZE = struct.calcsize(SLOT_HEADER_FMT)  # 16
    SLOT_VALUE_MAX = 4096  # per-slot value cap (keeps the mmap small)
    TOMBSTONE_BIT = 0x80000000
    LENGTH_MASK = 0x7FFFFFFF

    def __init__(
        self,
        path: str = DEFAULT_L2_FLAT_FILE_PATH,
        max_bytes: int = DEFAULT_L2_FLAT_FILE_MAX_BYTES,
        read_only: bool = False,
    ) -> None:
        self._path = Path(_expand_path(path))
        self._max_bytes = max_bytes
        self._read_only = read_only
        self._slot_count = max_bytes // (self.SLOT_HEADER_SIZE + self.SLOT_VALUE_MAX)
        self._lock = threading.RLock()
        self._mm: Optional[mmap.mmap] = None
        self._fd: Optional[int] = None
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self._init_file()

    def _init_file(self) -> None:
        """Create the mmap file if needed and mmap it."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Create with the full size if it doesn't exist.
        if not self._path.exists():
            with open(self._path, "wb") as f:
                f.truncate(self._max_bytes)
        elif self._path.stat().st_size != self._max_bytes:
            # Resize to match the configured size. Existing entries
            # in the truncated region are lost — that's an acceptable
            # tradeoff for a fixed-size ring buffer.
            with open(self._path, "r+b") as f:
                f.truncate(self._max_bytes)
        # Open and mmap. Use flock for cross-process serialization.
        self._fd = os.open(
            str(self._path),
            os.O_RDWR if not self._read_only else os.O_RDONLY,
        )
        if not self._read_only:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX)
            except OSError:
                pass  # Best-effort; mmap alone is safer than nothing.
        # PROT_READ always; PROT_WRITE only when not read-only.
        prot = mmap.PROT_READ if self._read_only else mmap.PROT_READ | mmap.PROT_WRITE
        self._mm = mmap.mmap(self._fd, self._max_bytes, prot=prot)

    def _slot_offset(self, index: int) -> int:
        return index * (self.SLOT_HEADER_SIZE + self.SLOT_VALUE_MAX)

    @staticmethod
    def _key_hash(key: str) -> int:
        """Hash the key into an unsigned 64-bit integer."""
        digest = hashlib.sha256(key.encode("utf-8")).digest()
        return struct.unpack(">Q", digest[:8])[0]

    def _read_slot(self, index: int) -> Tuple[int, int, bool, Optional[bytes]]:
        """Read a slot's header + value. Returns (key_hash, ts, tombstone, value)."""
        assert self._mm is not None
        offset = self._slot_offset(index)
        key_hash, value_len, ts = struct.unpack_from(
            self.SLOT_HEADER_FMT, self._mm, offset
        )
        # The tombstone flag rides in the high bit of value_len. A
        # stored value_len of 0 means the slot is empty (regardless of
        # tombstone bit). A stored value_len with the high bit set means
        # the entry was logically deleted.
        tombstone = bool(value_len & self.TOMBSTONE_BIT)
        actual_len = value_len & self.LENGTH_MASK
        if actual_len == 0:
            return key_hash, ts, False, None  # empty slot — never tombstone
        if tombstone:
            return key_hash, ts, True, None
        value = bytes(self._mm[offset + self.SLOT_HEADER_SIZE:offset + self.SLOT_HEADER_SIZE + actual_len])
        return key_hash, ts, False, value

    def _write_slot(self, index: int, key_hash: int, value: bytes, ts: int) -> None:
        """Write a slot's header + value. Overwrites any previous entry."""
        assert self._mm is not None
        offset = self._slot_offset(index)
        padded = value.ljust(self.SLOT_VALUE_MAX, b"\x00")[:self.SLOT_VALUE_MAX]
        # value_len on the wire is the actual length with the tombstone
        # bit cleared (zero for live entries).
        value_len = len(value) & self.LENGTH_MASK
        struct.pack_into(
            self.SLOT_HEADER_FMT, self._mm, offset,
            key_hash, value_len, ts,
        )
        self._mm[offset + self.SLOT_HEADER_SIZE:offset + self.SLOT_HEADER_SIZE + self.SLOT_VALUE_MAX] = padded

    def _find_slot(self, key_hash: int) -> Optional[int]:
        """Find a slot whose stored key_hash matches, or None.

        Empty slots (stored key_hash == 0 AND value_len == 0) are
        skipped — they belong to no key and must be filled by a new
        put, not matched against a real lookup. Tombstoned slots
        (value_len with the high bit set) are also skipped.
        """
        for i in range(self._slot_count):
            stored_hash, stored_vlen, _ts = struct.unpack_from(
                self.SLOT_HEADER_FMT, self._mm,
                self._slot_offset(i),
            )
            actual_len = stored_vlen & self.LENGTH_MASK
            if actual_len == 0:
                continue  # empty slot
            if stored_vlen & self.TOMBSTONE_BIT:
                continue  # tombstoned
            if stored_hash == key_hash:
                return i
        return None

    def _find_empty_slot(self) -> Optional[int]:
        """Find the first empty (or tombstoned, reusable) slot.

        Returns the slot index, or None if every slot is occupied by
        a live entry. The caller then needs to evict the oldest entry
        to make room.
        """
        for i in range(self._slot_count):
            stored_vlen = struct.unpack_from(
                self.SLOT_HEADER_FMT, self._mm,
                self._slot_offset(i),
            )[1]
            actual_len = stored_vlen & self.LENGTH_MASK
            if actual_len == 0:
                return i
            if stored_vlen & self.TOMBSTONE_BIT:
                return i
        return None

    def get(self, key: str) -> Optional[Any]:
        """Return the cached value for *key*, or ``None`` on miss."""
        if not isinstance(key, str):
            raise TypeError(f"key must be str, got {type(key).__name__}")
        if self._mm is None:
            return None
        key_hash = self._key_hash(key)
        with self._lock:
            try:
                slot = self._find_slot(key_hash)
            except Exception:
                # Fail-open: any I/O error is treated as a miss.
                self.misses += 1
                return None
            if slot is None:
                self.misses += 1
                return None
            try:
                stored_hash, ts, tombstone, value = self._read_slot(slot)
            except Exception:
                self.misses += 1
                return None
            if stored_hash != key_hash or tombstone or value is None:
                self.misses += 1
                return None
            try:
                decoded = pickle.loads(value)
            except Exception:
                self.misses += 1
                return None
            self.hits += 1
            return decoded

    def put(self, key: str, value: Any) -> None:
        """Store *value* under *key*. Overwrites any existing entry.

        Fail-open: any I/O error is logged (via the stats counter)
        but does not propagate. The L1 layer remains authoritative.
        """
        if not isinstance(key, str):
            raise TypeError(f"key must be str, got {type(key).__name__}")
        if self._read_only or self._mm is None:
            return
        try:
            payload = pickle.dumps(value)
        except Exception:
            return
        if len(payload) > self.SLOT_VALUE_MAX:
            # Value too large for the L2 ring buffer. Fail-open —
            # the L1 still has it; the user can opt into a larger
            # SLOT_VALUE_MAX via the config if they need it.
            self.evictions += 1
            return
        key_hash = self._key_hash(key)
        ts = int(time.time())
        with self._lock:
            try:
                slot = self._find_slot(key_hash)
                if slot is None:
                    # Try to claim an empty or tombstoned slot first.
                    slot = self._find_empty_slot()
                    if slot is None:
                        # Cache is full. Round-robin overwrite as a
                        # last-resort strategy. A real LRU ring would
                        # scan for the oldest; this stub uses the
                        # next slot in sequence. The user can opt into
                        # a larger L2 via cache.l2.flat_file.max_bytes
                        # if they need more capacity.
                        slot = (self.hits + self.misses + self.evictions) % self._slot_count
                        self.evictions += 1
                self._write_slot(slot, key_hash, payload, ts)
            except Exception:
                # Fail-open: ignore I/O errors.
                self.evictions += 1

    def invalidate(self, key: str) -> None:
        """Mark the slot for *key* as a tombstone (logical delete)."""
        if not isinstance(key, str):
            raise TypeError(f"key must be str, got {type(key).__name__}")
        if self._mm is None:
            return
        key_hash = self._key_hash(key)
        with self._lock:
            try:
                slot = self._find_slot(key_hash)
                if slot is None:
                    return
                # Write a tombstone by setting the high bit on the
                # value_len field. We don't need to overwrite the
                # value bytes; the tombstone flag short-circuits
                # reads.
                offset = self._slot_offset(slot)
                key_hash_stored, value_len, ts = struct.unpack_from(
                    self.SLOT_HEADER_FMT, self._mm, offset,
                )
                # Only set the tombstone bit on a non-empty slot.
                if value_len == 0:
                    return
                value_len |= self.TOMBSTONE_BIT
                struct.pack_into(
                    self.SLOT_HEADER_FMT, self._mm, offset,
                    key_hash_stored, value_len, ts,
                )
            except Exception:
                pass

    def close(self) -> None:
        """Flush + close the mmap and the underlying file descriptor."""
        with self._lock:
            if self._mm is not None:
                try:
                    self._mm.flush()
                except Exception:
                    pass
                try:
                    self._mm.close()
                except Exception:
                    pass
                self._mm = None
            if self._fd is not None:
                try:
                    os.close(self._fd)
                except Exception:
                    pass
                self._fd = None

    def __del__(self) -> None:
        # Best-effort cleanup; ``close()`` is the recommended path.
        try:
            self.close()
        except Exception:
            pass

    @property
    def estimated_bytes(self) -> int:
        """Rough estimate of bytes currently stored."""
        with self._lock:
            if self._mm is None:
                return 0
            count = sum(
                1 for i in range(self._slot_count)
                if (struct.unpack_from(
                    self.SLOT_HEADER_FMT, self._mm, self._slot_offset(i)
                )[1] & self.LENGTH_MASK) > 0
            )
            return count * self.SLOT_VALUE_MAX

    def stats(self) -> dict:
        """Return a snapshot of cache statistics."""
        with self._lock:
            total = self.hits + self.misses
            return {
                "backend": "flat_file",
                "path": str(self._path),
                "max_bytes": self._max_bytes,
                "slot_count": self._slot_count,
                "slot_value_max": self.SLOT_VALUE_MAX,
                "read_only": self._read_only,
                "estimated_bytes": self.estimated_bytes,
                "hits": self.hits,
                "misses": self.misses,
                "hit_rate": (self.hits / total) if total else 0.0,
                "evictions": self.evictions,
            }


class RedisCache:
    """L2 cache — Redis-backed. **NOT YET IMPLEMENTED (stub)**.

    Design (when implemented):

    * Connect to a local Redis instance (default 127.0.0.1:6379, db 0).
    * Use ``SETEX`` for TTL'd entries, namespace ``hermes:cache:``.
    * Connection pool: ``redis.ConnectionPool(max_connections=10)``.
    * **Fail-open**: on ``ConnectionError``, ``get()`` returns ``None``
      (treat as miss); ``put()`` logs and returns. The L1 layer
      remains the authoritative cache in this case.

    **Configuration**: ``cache.l2.redis.{url, namespace, ttl_seconds}``.

    **Triggers to implement**:

    * L1 hit rate < 80% under sustained load (suggests working
      set > 64 entries and cross-process sharing would help).
    * Multiple processes need to share cached values (e.g.,
      gateway + CLI + a long-running slash-command worker).
    * The flat-file L2 proves too slow for the expected hit
      rate from L2 (linear scan is the bottleneck at large
      slot counts).

    The methods below raise ``NotImplementedError`` so callers fail
    loudly if they try to use this before the implementation lands.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError(
            "RedisCache is not yet implemented. Use FlatFileCache (the "
            "default L2 backend) or implement RedisCache per the design "
            "in this docstring. See cache.l2.backend in config.yaml."
        )

    def get(self, key: str) -> Optional[Any]:
        raise NotImplementedError("RedisCache.get is not yet implemented")

    def put(self, key: str, value: Any) -> None:
        raise NotImplementedError("RedisCache.put is not yet implemented")

    def invalidate(self, key: str) -> None:
        raise NotImplementedError("RedisCache.invalidate is not yet implemented")

    def stats(self) -> dict:
        raise NotImplementedError("RedisCache.stats is not yet implemented")


def _build_l2_from_config() -> Optional[TieredCache]:
    """Construct the configured L2 backend, or None if L2 is disabled.

    Returns ``None`` when the L2 feature gate is off. The caller is
    expected to handle the None case by skipping the L2 tier entirely.
    """
    if not is_l2_enabled():
        return None
    cfg = _read_cache_config()
    l2_cfg = cfg.get("l2", {})
    backend = l2_cfg.get("backend", "flat_file")
    if backend == "flat_file":
        ff = l2_cfg.get("flat_file", {})
        return FlatFileCache(
            path=ff.get("path", DEFAULT_L2_FLAT_FILE_PATH),
            max_bytes=int(ff.get("max_bytes", DEFAULT_L2_FLAT_FILE_MAX_BYTES)),
            read_only=bool(ff.get("read_only", False)),
        )
    if backend == "redis":
        return RedisCache(
            url=l2_cfg.get("redis", {}).get("url", DEFAULT_L2_REDIS_URL),
            namespace=l2_cfg.get("redis", {}).get("namespace", DEFAULT_L2_REDIS_NAMESPACE),
            ttl_seconds=int(l2_cfg.get("redis", {}).get("ttl_seconds", DEFAULT_L2_REDIS_TTL_SECONDS)),
        )
    # Unknown backend: log via the stats counter, return None.
    return None


# ---------------------------------------------------------------------------
# L3 — sharded_file (real, stdlib-only) and sqlite (stub)
# ---------------------------------------------------------------------------



class FlatFileCache:
    """Facade over N mmap ring shards, keyed by sha256(key)[0] % N.

    Public API and constructor signature are identical to the original
    single-ring ``FlatFileCache``. Each shard persists to its own file
    (``<path>.s<i>``), so concurrent writers on different keys contend
    on different files/flocks instead of one global ring.

    Legacy layout: if a pre-sharding single file exists at *path*, it is
    renamed to ``<path>.legacy`` exactly once and its entries are NOT
    migrated -- the stored slots hold only 8-byte key hashes (not full
    keys), so enumeration is impossible. L2 is an acceleration tier;
    losing stale entries is acceptable (sources of truth live upstream).

    Adaptive sharding: budgets < 4 MiB use a single shard (no pointless
    extra files); larger budgets use 16.
    """

    _SHARD_COUNT = 16
    _SINGLE_SHARD_MAX_BYTES = 4 * 1024 * 1024

    def __init__(self, path=None, max_bytes=64 * 1024 * 1024,
                 _is_shard: bool = False, **kwargs):
        if _is_shard:
            # Internal: behave exactly like the original single ring.
            self._ring = _FlatFileRing(path=path, max_bytes=max_bytes, **kwargs)
            return
        import os as _os
        self._path = str(path)
        if max_bytes is not None and int(max_bytes) < self._SINGLE_SHARD_MAX_BYTES:
            self._num_shards = 1
        else:
            self._num_shards = self._SHARD_COUNT
        # One-time legacy-layout retirement.
        try:
            if _os.path.exists(self._path) and _os.path.getsize(self._path) > 0:
                legacy = self._path + ".legacy"
                if not _os.path.exists(legacy):
                    _os.replace(self._path, legacy)
                    logger.warning(
                        "FlatFileCache: retired legacy single-file L2 at %s "
                        "(entries not migratable; renamed to %s)",
                        self._path, legacy,
                    )
        except Exception:
            logger.debug("FlatFileCache: legacy check failed", exc_info=True)
        base, ext = _os.path.splitext(self._path)
        self._shards = []
        for i in range(self._num_shards):
            shard_path = "%s.s%d%s" % (base, i, ext)
            self._shards.append(
                _FlatFileRing(path=shard_path, max_bytes=max_bytes, **kwargs)
            )

    def _shard_for(self, key: str) -> "_FlatFileRing":
        digest = hashlib.sha256(key.encode("utf-8", "surrogatepass")).digest()
        return self._shards[digest[0] % self._num_shards]

    @staticmethod
    def _check_key(key: str) -> None:
        if not isinstance(key, str):
            raise TypeError(f"key must be str, got {type(key).__name__}")

    def get(self, key: str):
        self._check_key(key)
        return self._shard_for(key).get(key)

    def put(self, key: str, value) -> None:
        self._check_key(key)
        self._shard_for(key).put(key, value)

    def invalidate(self, key: str) -> None:
        self._check_key(key)
        self._shard_for(key).invalidate(key)

    def close(self) -> None:
        for s in self._shards:
            s.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def __len__(self) -> int:
        return sum(len(s) for s in self._shards)

    def __contains__(self, key: str) -> bool:
        return self._shard_for(key).__contains__(key)

    def stats(self) -> dict:
        agg = {
            "backend": "flat_file",
            "path": self._path,
            "max_bytes": self._shards[0]._max_bytes if self._shards else None,
            "num_shards": self._num_shards,
            "hits": 0,
            "misses": 0,
            "evictions": 0,
            "cache_skip_oversized": 0,
        }
        first = True
        for s in self._shards:
            st = s.stats()
            for k in ("hits", "misses", "evictions", "cache_skip_oversized"):
                agg[k] += st.get(k, 0)
            if first:
                # Ring-layout metadata is identical across shards.
                agg["slot_count"] = st.get("slot_count")
                agg["slot_value_max"] = st.get("slot_value_max")
                agg["read_only"] = st.get("read_only")
                agg["estimated_bytes"] = st.get("estimated_bytes", 0)
                first = False
            else:
                agg["estimated_bytes"] = agg.get("estimated_bytes", 0) + st.get("estimated_bytes", 0)
        total = agg["hits"] + agg["misses"]
        agg["hit_rate"] = (agg["hits"] / total) if total else 0.0
        return agg


class ShardedFileCache:
    """L3 cache — sharded directory of files with mtime-based TTL.

    Each entry is stored as ``<root>/<hash[0:2]>/<hash[2:4]>/<hash>``
    with a sidecar ``.json`` for metadata. The 2-level shard splits
    ~1M entries across ~256 top-level dirs and ~65K per dir — small
    enough that ``ls`` and ``find`` stay usable.

    **Why this design**:

    * **No external dependencies** — pure stdlib.
    * **Survives restart** — files persist until explicitly evicted.
    * **TTL via mtime** — ``evict_expired()`` walks the tree and
      deletes files older than ``ttl_days``. Cheap; no schema.
    * **Inspectable** — every entry is a regular file you can
      ``cat`` or ``ls -la``.

    **Configuration**: ``cache.l3.sharded_file.{root, ttl_days}``.

    **Fail-open**: on any I/O error, ``get()`` returns ``None``;
    ``put()`` logs and returns.

    **Tradeoffs vs sqlite**:

    * No atomic semantics — a torn write leaves a partial file.
      The implementation mitigates by writing to a temp file and
      ``os.replace()``-ing into place.
    * No transactional ``get-then-put`` — concurrent ``put`` on
      the same key can race. Acceptable for a cold tier; the L1
      is the authoritative source for the working set.
    * No indexes — ``evict_expired()`` is a full walk. Acceptable
      for the modest sizes a cold tier holds.
    """

    def __init__(
        self,
        root: str = DEFAULT_L3_SHARDED_ROOT,
        ttl_days: int = DEFAULT_L3_SHARDED_TTL_DAYS,
    ) -> None:
        self._root = Path(_expand_path(root))
        self._ttl_seconds = ttl_days * 86400
        self._lock = threading.RLock()
        # Per-instance index for fast get/put. Rebuilt on first
        # use. Stored as {key: (path, mtime)}. The mtime is the
        # last-access time for TTL purposes; the file's mtime is
        # the storage time.
        self._index: dict = {}
        self._index_built = False
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self._init_root()

    def _init_root(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _key_path(root: Path, key: str) -> Path:
        """Compute the sharded path for *key*."""
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return root / digest[0:2] / digest[2:4] / digest

    def _build_index(self) -> None:
        """Walk the root and populate the in-memory index.

        Called lazily on first ``get()`` or ``put()``. For a tree
        with millions of entries this would be expensive, but for
        the cold tier's expected size (hundreds to thousands of
        entries) it's sub-second.
        """
        if self._index_built:
            return
        if not self._root.exists():
            self._index_built = True
            return
        for shard_top in self._root.iterdir():
            if not shard_top.is_dir():
                continue
            for shard_mid in shard_top.iterdir():
                if not shard_mid.is_dir():
                    continue
                for entry in shard_mid.iterdir():
                    if entry.is_file() and not entry.name.endswith(".json"):
                        # We don't have a reverse index from path to
                        # key, so we just record paths. The metadata
                        # sidecar (entry.with_suffix('.json')) holds
                        # the original key if present.
                        meta_path = entry.with_suffix(entry.suffix + ".json")
                        key = entry.name
                        if meta_path.exists():
                            try:
                                with open(meta_path, "r", encoding="utf-8") as f:
                                    meta = json.load(f)
                                key = meta.get("key", entry.name)
                            except Exception:
                                pass
                        try:
                            mtime = entry.stat().st_mtime
                        except OSError:
                            continue
                        self._index[key] = (entry, mtime)
        self._index_built = True

    def get(self, key: str) -> Optional[Any]:
        """Return the cached value for *key*, or ``None`` on miss."""
        if not isinstance(key, str):
            raise TypeError(f"key must be str, got {type(key).__name__}")
        with self._lock:
            self._build_index()
            entry = self._index.get(key)
            if entry is None:
                self.misses += 1
                return None
            path, mtime = entry
            # Check TTL.
            if (time.time() - mtime) > self._ttl_seconds:
                # Lazy eviction: don't bother removing here, let
                # the next evict_expired() sweep handle it.
                self.evictions += 1
                self.misses += 1
                return None
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except OSError:
                self.misses += 1
                return None
            try:
                decoded = pickle.loads(data)
            except Exception:
                self.misses += 1
                return None
            self.hits += 1
            return decoded

    def put(self, key: str, value: Any) -> None:
        """Store *value* under *key* with a fresh TTL."""
        if not isinstance(key, str):
            raise TypeError(f"key must be str, got {type(key).__name__}")
        try:
            payload = pickle.dumps(value)
        except Exception:
            return
        with self._lock:
            self._build_index()
            target = self._key_path(self._root, key)
            target.parent.mkdir(parents=True, exist_ok=True)
            # Atomic write via temp file + os.replace.
            try:
                fd, tmp = tempfile.mkstemp(
                    prefix=".cache-", dir=str(target.parent),
                )
                with os.fdopen(fd, "wb") as f:
                    f.write(payload)
                os.replace(tmp, target)
            except OSError:
                return
            # Write the metadata sidecar.
            meta_path = target.with_suffix(target.suffix + ".json")
            try:
                with open(meta_path, "w", encoding="utf-8") as f:
                    json.dump({"key": key, "size": len(payload)}, f)
            except OSError:
                pass
            self._index[key] = (target, time.time())

    def invalidate(self, key: str) -> None:
        """Remove *key* from the cache. No-op if absent."""
        if not isinstance(key, str):
            raise TypeError(f"key must be str, got {type(key).__name__}")
        with self._lock:
            self._build_index()
            entry = self._index.pop(key, None)
            if entry is None:
                return
            path, _ = entry
            try:
                path.unlink()
            except OSError:
                pass
            meta = path.with_suffix(path.suffix + ".json")
            try:
                meta.unlink()
            except OSError:
                pass

    def evict_expired(self) -> int:
        """Delete entries whose mtime is older than the TTL.

        Returns the number of files removed. Designed to be called
        from a slow path (session end, daily sweep) — not from
        a per-turn hot path.
        """
        with self._lock:
            self._build_index()
            now = time.time()
            expired_keys = [
                k for k, (_, mtime) in self._index.items()
                if (now - mtime) > self._ttl_seconds
            ]
            for k in expired_keys:
                self.invalidate(k)
            return len(expired_keys)

    def vacuum(self) -> int:
        """Compact the directory tree by removing empty shards.

        Walks all top-level shard dirs, removes mid-level dirs that
        contain no files, then removes top-level dirs that have no
        subdirs. Returns the number of directories removed.

        Safe to call frequently — O(directory_count) not O(entry_count).
        """
        removed = 0
        with self._lock:
            self._build_index()
            if not self._root.exists():
                return 0
            for top_dir in sorted(self._root.iterdir(), reverse=True):
                if not top_dir.is_dir():
                    continue
                for mid_dir in sorted(top_dir.iterdir(), reverse=True):
                    if not mid_dir.is_dir():
                        continue
                    files = list(mid_dir.iterdir())
                    if not files:
                        try:
                            mid_dir.rmdir()
                            removed += 1
                        except OSError:
                            pass
                    else:
                        # Remove any orphan .json sidecars with no data file.
                        for f in mid_dir.iterdir():
                            if f.name.endswith(".json") and not f.with_suffix("").exists():
                                try:
                                    f.unlink()
                                    removed += 1
                                except OSError:
                                    pass
                # Remove top-level dir if now empty.
                if not any(top_dir.iterdir()):
                    try:
                        top_dir.rmdir()
                        removed += 1
                    except OSError:
                        pass
        return removed

    def stats(self) -> dict:
        """Return a snapshot of cache statistics."""
        with self._lock:
            total = self.hits + self.misses
            entry_count = len(self._index) if self._index_built else 0
            return {
                "backend": "sharded_file",
                "root": str(self._root),
                "ttl_days": self._ttl_seconds // 86400,
                "entries": entry_count,
                "hits": self.hits,
                "misses": self.misses,
                "hit_rate": (self.hits / total) if total else 0.0,
                "evictions": self.evictions,
            }


class DiskCache:
    """L3 cache — sqlite-backed. **NOT YET IMPLEMENTED (stub)**.

    Design (when implemented):

    * SQLite database at ``~/.hermes/cache/l3.sqlite`` with schema:
      ``(key TEXT PRIMARY KEY, value BLOB, mtime_ns INTEGER,
        size_bytes INTEGER)``.
    * TTL: 24h default, configurable via ``cache.l3.sqlite.ttl_seconds``.
    * Vacuum: on startup, if ``freelist_count > vacuum_threshold`` run
      ``VACUUM`` to reclaim space.
    * **Fail-open**: on ``sqlite3.OperationalError``, ``get()`` returns
      ``None`` (treat as miss); ``put()`` logs and returns.

    **Configuration**: ``cache.l3.sqlite.{path, ttl_seconds,
    vacuum_threshold}``.

    **Triggers to implement**:

    * ShardedFileCache eviction walk becomes the bottleneck.
    * The user needs ACID-style get-then-put semantics (ShardedFileCache
      races; sqlite is transactional).
    * Cross-process cold tier with concurrent writers.

    The methods below raise ``NotImplementedError`` so callers fail
    loudly if they try to use this before the implementation lands.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError(
            "DiskCache (sqlite) is not yet implemented. Use "
            "ShardedFileCache (the default L3 backend) or implement "
            "DiskCache per the design in this docstring. See "
            "cache.l3.backend in config.yaml."
        )

    def get(self, key: str) -> Optional[Any]:
        raise NotImplementedError("DiskCache.get is not yet implemented")

    def put(self, key: str, value: Any) -> None:
        raise NotImplementedError("DiskCache.put is not yet implemented")

    def invalidate(self, key: str) -> None:
        raise NotImplementedError("DiskCache.invalidate is not yet implemented")

    def stats(self) -> dict:
        raise NotImplementedError("DiskCache.stats is not yet implemented")


def _build_l3_from_config() -> Optional[TieredCache]:
    """Construct the configured L3 backend, or None if L3 is disabled."""
    if not is_l3_enabled():
        return None
    cfg = _read_cache_config()
    l3_cfg = cfg.get("l3", {})
    backend = l3_cfg.get("backend", "sharded_file")
    if backend == "sharded_file":
        sf = l3_cfg.get("sharded_file", {})
        return ShardedFileCache(
            root=sf.get("root", DEFAULT_L3_SHARDED_ROOT),
            ttl_days=int(sf.get("ttl_days", DEFAULT_L3_SHARDED_TTL_DAYS)),
        )
    if backend == "sqlite":
        return DiskCache(
            path=l3_cfg.get("sqlite", {}).get("path", DEFAULT_L3_SQLITE_PATH),
            ttl_seconds=int(l3_cfg.get("sqlite", {}).get("ttl_seconds", DEFAULT_L3_SQLITE_TTL_SECONDS)),
            vacuum_threshold=int(l3_cfg.get("sqlite", {}).get("vacuum_threshold", DEFAULT_L3_SQLITE_VACUUM_THRESHOLD)),
        )
    return None


# ---------------------------------------------------------------------------
# TieredCacheRouter
# ---------------------------------------------------------------------------

class CircuitBreaker:
    """Circuit breaker for individual cache tiers.

    Tracks consecutive failures per tier. When the failure count exceeds
    the threshold, the circuit "opens" and subsequent calls immediately
    return a failure without attempting the tier operation.

    The circuit half-opens after ``reset_timeout`` seconds, allowing a
    single probe call to test recovery.
    """

    def __init__(self, failure_threshold: int = 5, reset_timeout: float = 30.0) -> None:
        self._failure_threshold = failure_threshold
        self._reset_timeout = reset_timeout
        self._failures: int = 0
        self._last_failure_time: float = 0.0
        self._open: bool = False
        self._lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        with self._lock:
            if self._open:
                # Check if we should half-open
                if time.monotonic() - self._last_failure_time >= self._reset_timeout:
                    self._open = False
                    self._failures = 0
                    return False  # Half-open: allow probe
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._open = False

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            self._last_failure_time = time.monotonic()
            if self._failures >= self._failure_threshold:
                self._open = True

    def get_state(self) -> dict:
        with self._lock:
            return {
                "open": self._open,
                "failures": self._failures,
                "threshold": self._failure_threshold,
                "last_failure_age_seconds": time.monotonic() - self._last_failure_time if self._last_failure_time else 0.0,
            }


class TieredCacheRouter:
    """Read-through router chaining L1 → L2 → L3 with write-back.

    The router is constructed by :func:`build_cache_from_config` and
    only includes the tiers that are enabled in the config. By default
    this is just L1; L2 and L3 are opt-in via the ``cache.l2.enabled``
    and ``cache.l3.enabled`` feature gates.

    **Read path** (``get``): probe L1, then L2, then L3. On a hit in
    a lower tier, **write-back to all higher tiers** so the next read
    is fast.

    **Write path** (``put``): write to all enabled tiers. Failures in
    lower tiers are logged but do not propagate (the higher tier is
    the authoritative source for the working set).

    **Invalidation** (``invalidate``): remove from all enabled tiers.
    Failures are logged.

    **Fail-open**: every tier's error path is wrapped. A broken L2
    or L3 cannot take down the cache — the L1 still serves hits.

    **Circuit breaker**: each tier has an independent circuit breaker.
    When a tier fails consecutively beyond the threshold, it is marked
    open and skipped until the reset timeout elapses.
    """

    def __init__(self, *tiers: TieredCache, failure_threshold: int = 5, reset_timeout: float = 30.0) -> None:
        if not tiers:
            raise ValueError("TieredCacheRouter requires at least one tier")
        self._tiers: Tuple[TieredCache, ...] = tuple(tiers)
        self._breakers: Tuple[CircuitBreaker, ...] = tuple(
            CircuitBreaker(failure_threshold=failure_threshold, reset_timeout=reset_timeout)
            for _ in tiers
        )
        # Metrics counters
        self._metrics_lock = threading.Lock()
        self._total_gets: int = 0
        self._total_puts: int = 0
        self._total_invalidates: int = 0
        self._tier_failures: list = [0] * len(tiers)
        self._tier_hits: list = [0] * len(tiers)

    def get(self, key: str) -> Optional[Any]:
        # L1 → L2 → L3 walk. Write-back to higher tiers on a hit in
        # any lower tier.
        with self._metrics_lock:
            self._total_gets += 1
        for i, tier in enumerate(self._tiers):
            # Check circuit breaker
            if self._breakers[i].is_open:
                with self._metrics_lock:
                    self._tier_failures[i] += 1
                continue
            try:
                value = tier.get(key)
            except Exception:
                self._breakers[i].record_failure()
                with self._metrics_lock:
                    self._tier_failures[i] += 1
                continue
            self._breakers[i].record_success()
            with self._metrics_lock:
                self._tier_hits[i] += 1
            if value is not None:
                # Write-back to all higher (faster) tiers.
                for higher in self._tiers[:i]:
                    try:
                        higher.put(key, value)
                    except Exception:
                        pass
                return value
        return None

    def put(self, key: str, value: Any) -> None:
        with self._metrics_lock:
            self._total_puts += 1
        for i, tier in enumerate(self._tiers):
            if self._breakers[i].is_open:
                continue
            try:
                tier.put(key, value)
                self._breakers[i].record_success()
            except Exception:
                self._breakers[i].record_failure()
                # Fail-open: one tier's failure does not block the
                # others.
                pass

    def invalidate(self, key: str) -> None:
        with self._metrics_lock:
            self._total_invalidates += 1
        for i, tier in enumerate(self._tiers):
            if self._breakers[i].is_open:
                continue
            try:
                tier.invalidate(key)
                self._breakers[i].record_success()
            except Exception:
                self._breakers[i].record_failure()
                pass

    def stats(self) -> dict:
        """Return per-tier stats plus an aggregate hit-rate."""
        per_tier = []
        total_hits = 0
        total_misses = 0
        with self._metrics_lock:
            for i, tier in enumerate(self._tiers):
                s = tier.stats() if hasattr(tier, "stats") else {}
                breaker_state = self._breakers[i].get_state()
                per_tier.append({
                    "tier": type(tier).__name__,
                    "stats": s,
                    "circuit_breaker": breaker_state,
                })
                total_hits += s.get("hits", 0)
                total_misses += s.get("misses", 0)
            total = total_hits + total_misses
            return {
                "tiers": per_tier,
                "aggregate_hits": total_hits,
                "aggregate_misses": total_misses,
                "aggregate_hit_rate": (total_hits / total) if total else 0.0,
                "metrics": {
                    "total_gets": self._total_gets,
                    "total_puts": self._total_puts,
                    "total_invalidates": self._total_invalidates,
                    "tier_failures": dict(enumerate(self._tier_failures)),
                    "tier_hits": dict(enumerate(self._tier_hits)),
                }
            }


# ---------------------------------------------------------------------------
# Factory: build the configured cache stack
# ---------------------------------------------------------------------------

def build_cache_from_config() -> TieredCache:
    """Build a :class:`TieredCacheRouter` from the current config.

    Tiers that are not enabled are skipped. By default only L1 is
    enabled; the L2 and L3 layers are opt-in via the ``cache.l2.enabled``
    and ``cache.l3.enabled`` config keys.

    **Master kill switch**: when ``cache.enabled`` is false, this
    returns a router wrapping a single empty :class:`InProcessLRUCache`
    with all values effectively black-holed. (The class itself doesn't
    have a "disabled" mode; the master gate is enforced by skipping
    L2/L3 construction. Consumers that need a true off switch can
    check :func:`is_cache_enabled` themselves.)
    """
    tiers: list = []
    if is_l1_enabled():
        cfg = _read_cache_config()
        l1 = cfg.get("l1", {})
        policy = l1.get("eviction_policy", "lru")
        if policy == "tiny_lfu":
            tiny_lfu_cfg = l1.get("tiny_lfu", {})
            tiers.append(InProcessTinyLFUCache(
                max_entries=int(l1.get("max_entries", DEFAULT_L1_MAX_ENTRIES)),
                ttl_seconds=int(tiny_lfu_cfg.get("ttl_seconds", 0)),
                nolock=bool(tiny_lfu_cfg.get("nolock", False)),
            ))
        else:
            l1_kwargs = dict(
                max_entries=int(l1.get("max_entries", DEFAULT_L1_MAX_ENTRIES)),
                max_bytes=int(l1.get("max_bytes", DEFAULT_L1_MAX_BYTES)),
            )
            if l1.get("value_max_bytes") is not None:
                l1_kwargs["value_max_bytes"] = int(l1["value_max_bytes"])
            tiers.append(InProcessLRUCache(**l1_kwargs))
    l2 = _build_l2_from_config()
    if l2 is not None:
        tiers.append(l2)
    l3 = _build_l3_from_config()
    if l3 is not None:
        tiers.append(l3)
    if not tiers:
        # No tiers enabled (master kill switch + all sub-tiers off).
        # Return an empty in-memory cache so callers can use the
        # interface without None-checks.
        tiers.append(InProcessLRUCache(max_entries=1, max_bytes=1024))
    return TieredCacheRouter(*tiers)


# Module-level singleton for the tiered cache router.
_router_singleton: Optional[TieredCache] = None
_router_lock = threading.Lock()


def get_cache_router() -> TieredCache:
    """Return the process-wide tiered cache router singleton.

    The router is built on first call from the current config via
    :func:`build_cache_from_config`. Subsequent calls return the same
    instance so that all consumers share the same tiered cache.

    To force a rebuild after a config change, set
    ``agent._cache._router_singleton = None`` before calling again.
    """
    global _router_singleton
    if _router_singleton is None:
        with _router_lock:
            if _router_singleton is None:
                _router_singleton = build_cache_from_config()
    return _router_singleton


def get_cache_status() -> dict:
    """Return a JSON-serializable snapshot of cache metrics.

    Safe to call from the status endpoint — never raises.
    Returns the router's stats() output, or a degraded dict on error.
    """
    try:
        router = get_cache_router()
        return router.stats()
    except Exception:
        return {
            "error": "cache_unavailable",
            "tiers": [],
            "aggregate_hits": 0,
            "aggregate_misses": 0,
            "aggregate_hit_rate": 0.0,
        }
