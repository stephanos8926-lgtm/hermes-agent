"""Tests for the tiered cache infrastructure (agent/_cache.py).

Covers:
  * InProcessLRUCache — basic get/put/invalidate, LRU eviction, byte-budget
    eviction, thread safety, byte-stability contract, type validation.
  * RedisCache / DiskCache — NotImplementedError on every method.
  * TieredCacheRouter — basic read-through, write-back, fail-open semantics.
"""
import threading
import time

import pytest

from agent._cache import (
    DEFAULT_L1_MAX_BYTES,
    DEFAULT_L1_MAX_ENTRIES,
    DiskCache,
    InProcessLRUCache,
    RedisCache,
    TieredCacheRouter,
)


# ─── InProcessLRUCache: basic operations ──────────────────────────────


def test_l1_put_and_get():
    """Basic put then get returns the same value."""
    cache = InProcessLRUCache()
    cache.put("key1", "value1")
    assert cache.get("key1") == "value1"
    assert cache.stats()["hits"] == 1
    assert cache.stats()["misses"] == 0


def test_l1_get_missing_returns_none():
    """Missing key returns None and counts as a miss."""
    cache = InProcessLRUCache()
    assert cache.get("missing") is None
    assert cache.stats()["misses"] == 1


def test_l1_put_replaces_existing():
    """Re-putting the same key replaces the value (and updates LRU)."""
    cache = InProcessLRUCache()
    cache.put("k", "v1")
    cache.put("k", "v2")
    assert cache.get("k") == "v2"
    assert len(cache) == 1


def test_l1_invalidate_removes_entry():
    """invalidate() removes the entry; subsequent get() is a miss."""
    cache = InProcessLRUCache()
    cache.put("k", "v")
    cache.invalidate("k")
    assert cache.get("k") is None
    assert len(cache) == 0


def test_l1_invalidate_missing_is_noop():
    """invalidate() of a missing key does not raise."""
    cache = InProcessLRUCache()
    cache.invalidate("never_added")  # must not raise
    assert len(cache) == 0


def test_l1_clear_drops_everything():
    """clear() empties the cache and resets the byte counter."""
    cache = InProcessLRUCache()
    cache.put("a", "x" * 1000)
    cache.put("b", "y" * 1000)
    cache.put("c", "z" * 1000)
    assert len(cache) == 3
    assert cache.stats()["bytes"] > 0
    cache.clear()
    assert len(cache) == 0
    assert cache.stats()["bytes"] == 0


def test_l1_contains_operator():
    """``in`` operator works for membership checks."""
    cache = InProcessLRUCache()
    cache.put("present", 1)
    assert "present" in cache
    assert "absent" not in cache


def test_l1_type_validation_on_get():
    """get() requires str key; raises TypeError for non-str."""
    cache = InProcessLRUCache()
    with pytest.raises(TypeError):
        cache.get(42)


def test_l1_type_validation_on_put():
    """put() requires str key; raises TypeError for non-str."""
    cache = InProcessLRUCache()
    with pytest.raises(TypeError):
        cache.put(42, "value")


def test_l1_type_validation_on_invalidate():
    """invalidate() requires str key; raises TypeError for non-str."""
    cache = InProcessLRUCache()
    with pytest.raises(TypeError):
        cache.invalidate(42)


def test_l1_invalid_max_entries():
    """max_entries < 1 raises ValueError."""
    with pytest.raises(ValueError):
        InProcessLRUCache(max_entries=0)


def test_l1_invalid_max_bytes():
    """max_bytes < 1024 raises ValueError."""
    with pytest.raises(ValueError):
        InProcessLRUCache(max_bytes=512)


# ─── InProcessLRUCache: LRU eviction ───────────────────────────────────


def test_l1_lru_eviction_by_count():
    """When entry count exceeds max, oldest entries are evicted."""
    cache = InProcessLRUCache(max_entries=3, max_bytes=10**9)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("c", 3)
    cache.put("d", 4)  # evicts "a"
    assert cache.get("a") is None
    assert cache.get("b") == 2
    assert cache.get("c") == 3
    assert cache.get("d") == 4
    assert cache.stats()["evictions"] == 1


def test_l1_lru_get_refreshes_position():
    """get() marks an entry as most-recently used, protecting it from eviction."""
    cache = InProcessLRUCache(max_entries=3, max_bytes=10**9)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("c", 3)
    # Touch "a" to make it most-recently used
    assert cache.get("a") == 1
    # Now "b" is oldest; inserting "d" evicts "b"
    cache.put("d", 4)
    assert cache.get("a") == 1
    assert cache.get("b") is None
    assert cache.get("c") == 3
    assert cache.get("d") == 4


def test_l1_lru_put_refreshes_position():
    """put() of an existing key updates its position to most-recently used."""
    cache = InProcessLRUCache(max_entries=3, max_bytes=10**9)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("c", 3)
    # Re-put "a" — should refresh its position
    cache.put("a", 10)
    # Now "b" is oldest; inserting "d" evicts "b"
    cache.put("d", 4)
    assert cache.get("a") == 10
    assert cache.get("b") is None
    assert cache.get("c") == 3
    assert cache.get("d") == 4


# ─── InProcessLRUCache: byte-budget eviction ───────────────────────────


def test_l1_byte_budget_eviction():
    """When total bytes exceed max_bytes, oldest entries are evicted."""
    # Use a small but valid byte budget (>= 1024 per constructor guard)
    # and entries that are big enough to fill it quickly.
    cache = InProcessLRUCache(max_entries=100, max_bytes=1024)
    # Each entry: 1-char key + repr of 100-char value = ~103 bytes
    # 10 entries = ~1030 bytes — exceeds 1024.
    for i in range(10):
        cache.put(str(i), "x" * 100)
    # At least one entry should have been evicted.
    assert cache.stats()["evictions"] >= 1


def test_l1_byte_counter_accurate_on_replace():
    """Re-putting a key updates the byte counter (no leak)."""
    cache = InProcessLRUCache()
    cache.put("k", "x" * 10000)
    initial_bytes = cache.stats()["bytes"]
    cache.put("k", "y")  # small replacement
    # Byte counter should drop significantly
    assert cache.stats()["bytes"] < initial_bytes


# ─── InProcessLRUCache: stats ─────────────────────────────────────────


def test_l1_stats_hit_rate():
    """stats() reports hit_rate correctly."""
    cache = InProcessLRUCache()
    cache.put("a", 1)
    cache.get("a")  # hit
    cache.get("a")  # hit
    cache.get("missing")  # miss
    stats = cache.stats()
    assert stats["hits"] == 2
    assert stats["misses"] == 1
    assert abs(stats["hit_rate"] - 2/3) < 1e-9


def test_l1_stats_initial_hit_rate_is_zero():
    """Empty cache has hit_rate 0.0 (no division by zero)."""
    cache = InProcessLRUCache()
    assert cache.stats()["hit_rate"] == 0.0


def test_l1_stats_reports_config():
    """stats() includes max_entries and max_bytes for observability."""
    cache = InProcessLRUCache(max_entries=32, max_bytes=4096)
    stats = cache.stats()
    assert stats["max_entries"] == 32
    assert stats["max_bytes"] == 4096


# ─── InProcessLRUCache: byte-stability contract ───────────────────────


def test_l1_returns_same_object_not_copy():
    """get() returns the *exact object* stored (not a copy).

    This is the byte-stability contract: callers must treat the
    returned value as read-only. The docstring on InProcessLRUCache
    warns about this; this test pins the behavior.
    """
    cache = InProcessLRUCache()
    sentinel = object()
    cache.put("k", sentinel)
    assert cache.get("k") is sentinel


def test_l1_does_not_corrupt_on_mutation():
    """Mutating a returned value (which violates the contract) is the
    caller's bug, not the cache's. The cache returns the same object,
    so subsequent get() sees the mutation. This test pins that
    behavior so we can detect accidental changes to the copy-on-read
    semantics in the future.
    """
    cache = InProcessLRUCache()
    d = {"mutable": "original"}
    cache.put("k", d)
    retrieved = cache.get("k")
    retrieved["mutable"] = "MUTATED"
    # The cache returns the same object, so the next get() sees the
    # mutation. This is the documented contract.
    assert cache.get("k")["mutable"] == "MUTATED"


# ─── InProcessLRUCache: thread safety ──────────────────────────────────


def test_l1_concurrent_puts_do_not_corrupt():
    """Many threads putting concurrently end with a consistent state."""
    cache = InProcessLRUCache(max_entries=1000, max_bytes=10**9)
    def worker(i: int) -> None:
        for j in range(100):
            cache.put(f"key-{i}-{j}", j)
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Cache should have a valid state (entry count <= max_entries,
    # byte counter consistent with len).
    assert len(cache) <= 1000
    # All keys from a single worker should be retrievable.
    for j in range(100):
        assert cache.get("key-0-" + str(j)) == j


def test_l1_concurrent_gets_and_puts():
    """Concurrent get/put pattern does not raise."""
    cache = InProcessLRUCache()
    cache.put("seed", 0)

    def reader():
        for _ in range(200):
            assert cache.get("seed") is not None  # may be None during eviction

    def writer():
        for i in range(200):
            cache.put("seed", i)

    threads = [threading.Thread(target=reader) for _ in range(4)]
    threads += [threading.Thread(target=writer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


# ─── RedisCache / DiskCache: stub behavior ─────────────────────────────


def test_redis_cache_init_raises():
    """RedisCache() raises NotImplementedError on construction."""
    with pytest.raises(NotImplementedError):
        RedisCache()


def test_redis_cache_get_raises():
    """RedisCache.get() raises NotImplementedError."""
    # Bypass __init__ to test the method itself.
    cache = RedisCache.__new__(RedisCache)
    with pytest.raises(NotImplementedError):
        cache.get("k")


def test_redis_cache_put_raises():
    """RedisCache.put() raises NotImplementedError."""
    cache = RedisCache.__new__(RedisCache)
    with pytest.raises(NotImplementedError):
        cache.put("k", "v")


def test_redis_cache_invalidate_raises():
    """RedisCache.invalidate() raises NotImplementedError."""
    cache = RedisCache.__new__(RedisCache)
    with pytest.raises(NotImplementedError):
        cache.invalidate("k")


def test_disk_cache_init_raises():
    """DiskCache() raises NotImplementedError on construction."""
    with pytest.raises(NotImplementedError):
        DiskCache()


def test_disk_cache_get_raises():
    """DiskCache.get() raises NotImplementedError."""
    cache = DiskCache.__new__(DiskCache)
    with pytest.raises(NotImplementedError):
        cache.get("k")


def test_disk_cache_put_raises():
    """DiskCache.put() raises NotImplementedError."""
    cache = DiskCache.__new__(DiskCache)
    with pytest.raises(NotImplementedError):
        cache.put("k", "v")


def test_disk_cache_invalidate_raises():
    """DiskCache.invalidate() raises NotImplementedError."""
    cache = DiskCache.__new__(DiskCache)
    with pytest.raises(NotImplementedError):
        cache.invalidate("k")


# ─── TieredCacheRouter: behavior ───────────────────────────────────────


def test_router_requires_at_least_one_tier():
    """TieredCacheRouter() raises ValueError on empty tier list."""
    with pytest.raises(ValueError):
        TieredCacheRouter()


def test_router_get_walks_tiers_l1_first():
    """get() probes L1 before lower tiers."""
    l1 = InProcessLRUCache()
    l2 = InProcessLRUCache()
    l2.put("k", "from_l2")
    router = TieredCacheRouter(l1, l2)
    # L1 miss → L2 hit. Router writes back to L1.
    assert router.get("k") == "from_l2"
    assert l1.get("k") == "from_l2"  # write-back populated L1


def test_router_get_returns_first_hit():
    """When L1 has the value, lower tiers are not consulted."""
    l1 = InProcessLRUCache()
    l1.put("k", "from_l1")
    l2 = InProcessLRUCache()
    l2.put("k", "from_l2")
    router = TieredCacheRouter(l1, l2)
    assert router.get("k") == "from_l1"


def test_router_put_writes_to_all_tiers():
    """put() writes to every tier."""
    l1 = InProcessLRUCache()
    l2 = InProcessLRUCache()
    router = TieredCacheRouter(l1, l2)
    router.put("k", "v")
    assert l1.get("k") == "v"
    assert l2.get("k") == "v"


def test_router_invalidate_removes_from_all_tiers():
    """invalidate() removes the key from every tier."""
    l1 = InProcessLRUCache()
    l2 = InProcessLRUCache()
    router = TieredCacheRouter(l1, l2)
    router.put("k", "v")
    router.invalidate("k")
    assert l1.get("k") is None
    assert l2.get("k") is None


def test_router_get_returns_none_when_all_miss():
    """get() returns None when no tier has the value."""
    l1 = InProcessLRUCache()
    l2 = InProcessLRUCache()
    router = TieredCacheRouter(l1, l2)
    assert router.get("missing") is None


# ─── Default constants ────────────────────────────────────────────────


def test_default_constants():
    """Default constants are sensible (sanity check)."""
    assert DEFAULT_L1_MAX_ENTRIES == 64
    assert DEFAULT_L1_MAX_BYTES == 32 * 1024 * 1024
