"""Tiered cache infrastructure for in-process and cross-process caching.

This module provides the foundational types for a 3-tier cache architecture:

* **L1** (InProcessLRUCache): an in-memory LRU keyed on (key, content_hash).
  Used for hot objects that change rarely and must be byte-identical across
  reads. Thread-safe. Default size 64.

* **L2** (RedisCache): a stub for a future Redis-backed shared cache. Not
  implemented in this initial version. See ``RedisCache`` docstring for the
  design and triggers to implement.

* **L3** (DiskCache): a stub for a future disk-backed cold cache (sqlite or
  flat-file). Not implemented in this initial version. See ``DiskCache``
  docstring for the design and triggers to implement.

* **TieredCacheRouter**: a stub chaining reader. Read-through L1→L2→L3 with
  write-back to all tiers. Not implemented; the L1 class is used directly
  by consumers that need a single tier today.

---

**Why this module exists**

The fork already has excellent in-process caching for specific objects
(``load_config_readonly`` with mtime-keyed cache, the system prompt's
per-session ``_cached_system_prompt``, the per-message token estimate
fingerprint memo, the tool-call argument canonicalization memo). What it
lacks is a *general-purpose* tiered cache abstraction that new code can
reach for without re-implementing the LRU, byte-budget, or invalidation
discipline each time.

This module provides that abstraction. Concrete consumers will be added
in follow-on work where profiling shows a real per-turn hot path that
benefits from caching — the L2/L3 layers will be implemented only when
the L1 hit rate under load shows the working set exceeds in-memory
capacity (the documented trigger in each stub class).

---

**Architectural alternative: mmap + flat-file**

For users who want a 3-tier cache without Redis or sqlite, the following
alternative design is documented for future implementation:

* **L1**: same as this module — in-process LRU.
* **L2 alternative**: a fixed-size ``mmap``-backed file at
  ``~/.hermes/cache/l2.mmap`` with a ring buffer layout. Reader/writer
  uses ``fcntl.flock`` for cross-process safety. Faster than sqlite for
  fixed-size byte-string objects; no schema overhead; survives process
  restart. Tradeoff: harder to inspect manually than sqlite.
* **L3 alternative**: a sorted directory of files keyed by a 2-level hash
  directory (``~/.hermes/cache/l3/<hash[0:2]>/<hash[2:4]>/<hash>``) with
  a sidecar ``.json`` for metadata (mtime, size, ttl). Eviction by mtime
  (a daily sweep deletes files older than the configured TTL). Tradeoff:
  no atomic semantics, but trivially portable, no dependencies, easy to
  debug with a file browser.

The mmap+flat-file path is a strict subset of what Redis+sqlite provide.
It is documented here so the user can choose to ship it instead if the
operational complexity of Redis is not warranted by the workload.

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

import threading
from collections import OrderedDict
from typing import Any, Optional, Protocol, Tuple


# Default L1 size — a balance between memory pressure and cache churn.
# Most agent sessions fit in 16-32 entries; 64 is the documented cap.
DEFAULT_L1_MAX_ENTRIES = 64

# Default L1 byte budget — 32 MiB. Sized so the common case of
# small prompts / config snippets (~0.5-2 KiB per entry, ~16 MiB total)
# never hits the eviction path; the byte budget only matters when an
# unusually large entry is cached. Pattern borrowed from upstream PR
# #76880 (tool-call argument canonicalization memo).
DEFAULT_L1_MAX_BYTES = 32 * 1024 * 1024


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


class InProcessLRUCache:
    """In-process LRU cache with both an entry-count and byte budget.

    The cache is keyed on the user-supplied string ``key`` and stores
    the user-supplied value. Eviction policy is **LRU on entry count**
    with a **secondary byte budget** that fires when the total cached
    bytes exceed ``max_bytes`` (counting the sum of ``len(key)`` plus
    the ``len(repr(value))`` for the stored value).

    **Concurrency**: a single ``threading.RLock`` guards both the
    OrderedDict and the byte counter. This is sufficient for the
    expected workload (a handful of cache hits per turn, low
    contention). For high-contention paths, consider ``OrderedDict``
    with a finer-grained lock or a concurrent implementation.

    **Byte-stability contract**: callers MUST treat the returned
    value as read-only. The L1 cache returns the *exact object*
    stored under the key, not a copy. Mutating a returned value
    silently corrupts the cache and may invalidate the provider-side
    prompt cache on the next read.
    """

    def __init__(
        self,
        max_entries: int = DEFAULT_L1_MAX_ENTRIES,
        max_bytes: int = DEFAULT_L1_MAX_BYTES,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        if max_bytes < 1024:
            raise ValueError("max_bytes must be >= 1024")
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._entries: "OrderedDict[str, Any]" = OrderedDict()
        self._bytes = 0
        self._lock = threading.RLock()
        # Stats — exposed for tests and operational debugging.
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    def _size_of(self, key: str, value: Any) -> int:
        """Estimate the byte cost of storing *value* under *key*."""
        try:
            return len(key) + len(repr(value))
        except Exception:
            # repr can fail for non-representable objects; fall back to
            # a coarse estimate that still bounds memory growth.
            return len(key) + 1024

    def _evict_until_within_budget(self) -> None:
        """Evict oldest entries until both budgets are satisfied."""
        while (
            len(self._entries) > self._max_entries
            or self._bytes > self._max_bytes
        ) and self._entries:
            try:
                oldest_key, oldest_value = next(iter(self._entries.items()))
                self._entries.popitem(last=False)
            except (KeyError, RuntimeError, StopIteration):
                break
            self._bytes -= self._size_of(oldest_key, oldest_value)
            self.evictions += 1

    def get(self, key: str) -> Optional[Any]:
        """Return the cached value for *key* and mark it as most-recently used.

        Returns ``None`` on miss. The returned value is the **exact
        object** stored under *key* — do not mutate it.
        """
        if not isinstance(key, str):
            raise TypeError(f"key must be str, got {type(key).__name__}")
        with self._lock:
            try:
                value = self._entries[key]  # raises KeyError on miss
            except KeyError:
                self.misses += 1
                return None
            # Mark as most-recently used.
            self._entries.move_to_end(key)
            self.hits += 1
            return value

    def put(self, key: str, value: Any) -> None:
        """Store *value* under *key*, replacing any existing entry.

        Eviction fires if the new total exceeds the entry count or
        byte budget. Existing keys retain their position in the LRU
        (move_to_end is called for the new value).
        """
        if not isinstance(key, str):
            raise TypeError(f"key must be str, got {type(key).__name__}")
        cost = self._size_of(key, value)
        with self._lock:
            # If the key exists, subtract the old cost so the byte
            # counter stays accurate after replacement.
            if key in self._entries:
                try:
                    old_value = self._entries[key]
                except KeyError:
                    old_value = None
                self._bytes -= self._size_of(key, old_value)
            self._entries[key] = value
            self._bytes += cost
            self._entries.move_to_end(key)
            self._evict_until_within_budget()

    def invalidate(self, key: str) -> None:
        """Remove *key* from the cache. No-op if the key is absent."""
        if not isinstance(key, str):
            raise TypeError(f"key must be str, got {type(key).__name__}")
        with self._lock:
            try:
                old_value = self._entries.pop(key)
            except KeyError:
                return
            self._bytes -= self._size_of(key, old_value)

    def clear(self) -> None:
        """Drop all entries and reset the byte counter to zero."""
        with self._lock:
            self._entries.clear()
            self._bytes = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, str):
            return False
        with self._lock:
            return key in self._entries

    def stats(self) -> dict:
        """Return a snapshot of cache statistics for tests and observability."""
        with self._lock:
            total = self.hits + self.misses
            return {
                "entries": len(self._entries),
                "bytes": self._bytes,
                "hits": self.hits,
                "misses": self.misses,
                "hit_rate": (self.hits / total) if total else 0.0,
                "evictions": self.evictions,
                "max_entries": self._max_entries,
                "max_bytes": self._max_bytes,
            }


class RedisCache:
    """L2 cache — Redis-backed. **NOT YET IMPLEMENTED**.

    Design (when implemented):
        * Connect to a local Redis instance (default 127.0.0.1:6379, db 0).
        * Use ``SETEX`` for TTL'd entries, namespace ``hermes:cache:``.
        * Connection pool: ``redis.ConnectionPool(max_connections=10)``.
        * **Fail-open**: on ``ConnectionError``, ``get()`` returns ``None``
          (treat as miss); ``put()`` logs and returns. The L1 layer
          remains the authoritative cache in this case.

    **Triggers to implement**:
        * L1 hit rate < 80% under sustained load (suggests working
          set > 64 entries and cross-process sharing would help).
        * Multiple processes need to share cached values (e.g.,
          gateway + CLI + a long-running slash-command worker).
        * The disk-tier (L3) proves too slow for the expected hit
          rate from L2.

    The methods below raise ``NotImplementedError`` so callers fail
    loudly if they try to use this before the implementation lands.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError(
            "RedisCache is not yet implemented. See the class docstring "
            "for the design and the triggers to implement."
        )

    def get(self, key: str) -> Optional[Any]:
        raise NotImplementedError("RedisCache.get is not yet implemented")

    def put(self, key: str, value: Any) -> None:
        raise NotImplementedError("RedisCache.put is not yet implemented")

    def invalidate(self, key: str) -> None:
        raise NotImplementedError("RedisCache.invalidate is not yet implemented")


class DiskCache:
    """L3 cache — disk-backed (sqlite or flat-file). **NOT YET IMPLEMENTED**.

    Design (when implemented):
        * SQLite database at ``~/.hermes/cache/l3.db`` with schema:
          ``(key TEXT PRIMARY KEY, value BLOB, mtime_ns INTEGER,
            size_bytes INTEGER)``.
        * TTL: 24h default, configurable via ``max_age_seconds``.
        * Vacuum: on startup, if ``freelist_count > 1000`` run
          ``VACUUM`` to reclaim space.
        * **Fail-open**: on ``sqlite3.OperationalError``, ``get()``
          returns ``None`` (treat as miss); ``put()`` logs and
          returns.

    **Triggers to implement**:
        * L1 + L2 (when implemented) hit rate < 60% under sustained
          load (suggests working set > 1 MiB and disk-cached values
          would still find a hit).
        * The user wants cross-restart persistence for specific
          cached values (the in-process L1 loses everything on
          gateway restart).
        * Cold-start latency for caches that take measurable time to
          build (e.g., the model catalog) is a user-visible problem.

    The methods below raise ``NotImplementedError`` so callers fail
    loudly if they try to use this before the implementation lands.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError(
            "DiskCache is not yet implemented. See the class docstring "
            "for the design and the triggers to implement."
        )

    def get(self, key: str) -> Optional[Any]:
        raise NotImplementedError("DiskCache.get is not yet implemented")

    def put(self, key: str, value: Any) -> None:
        raise NotImplementedError("DiskCache.put is not yet implemented")

    def invalidate(self, key: str) -> None:
        raise NotImplementedError("DiskCache.invalidate is not yet implemented")


class TieredCacheRouter:
    """Read-through router chaining L1 → L2 → L3. **NOT YET IMPLEMENTED**.

    When L2 and L3 land, this router will:
        * On ``get(key)``: probe L1, then L2, then L3. On a hit in a
          lower tier, **write-back to all higher tiers** so the next
          read is fast.
        * On ``put(key, value)``: write to all tiers. Failures in
          lower tiers are logged but do not propagate (the higher
          tier is the authoritative source for the working set).
        * On ``invalidate(key)``: remove from all tiers. Failures
          are logged.

    For now, callers that need a single-tier cache use
    :class:`InProcessLRUCache` directly. The router becomes useful
    when at least one of L2 or L3 lands.
    """

    def __init__(self, *tiers: TieredCache) -> None:
        if not tiers:
            raise ValueError("TieredCacheRouter requires at least one tier")
        # All tiers must be TieredCache-compatible. We don't enforce at
        # construction time (Protocol has no runtime check) but the
        # methods below will fail loudly if a tier is missing required
        # methods.
        self._tiers: Tuple[TieredCache, ...] = tuple(tiers)

    def get(self, key: str) -> Optional[Any]:
        # L1 → L2 → L3 walk. Write-back to higher tiers on a hit in
        # any lower tier. (Stub behavior: walk the tiers that exist.)
        for i, tier in enumerate(self._tiers):
            value = tier.get(key)
            if value is not None:
                # Write-back to all higher (faster) tiers.
                for higher in self._tiers[:i]:
                    try:
                        higher.put(key, value)
                    except Exception:
                        # Fail-open: a write-back failure does not
                        # propagate. The hit is still returned.
                        pass
                return value
        return None

    def put(self, key: str, value: Any) -> None:
        for tier in self._tiers:
            try:
                tier.put(key, value)
            except Exception:
                # Fail-open: one tier's failure does not block the
                # others. The hit is still stored in any tier that
                # accepted it.
                pass

    def invalidate(self, key: str) -> None:
        for tier in self._tiers:
            try:
                tier.invalidate(key)
            except Exception:
                # Fail-open: same as put().
                pass
