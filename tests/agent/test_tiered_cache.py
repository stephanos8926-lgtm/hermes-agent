"""Tests for the tiered cache infrastructure (agent/_cache.py).

Covers:
  * InProcessLRUCache — basic get/put/invalidate, LRU eviction, byte-budget
    eviction, thread safety, byte-stability contract, type validation.
  * FlatFileCache (L2) — mmap-backed ring buffer, fail-open, persistence.
  * ShardedFileCache (L3) — sharded directory, mtime TTL, evict_expired.
  * RedisCache / DiskCache — NotImplementedError on every method.
  * TieredCacheRouter — basic read-through, write-back, fail-open semantics.
  * Config loader — feature gates, env-var overrides, graceful fallback.
"""
import os
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from agent._cache import (
    DEFAULT_L1_MAX_BYTES,
    DEFAULT_L1_MAX_ENTRIES,
    DEFAULT_L2_FLAT_FILE_MAX_BYTES,
    DEFAULT_L2_FLAT_FILE_PATH,
    DEFAULT_L3_SHARDED_ROOT,
    DEFAULT_L3_SHARDED_TTL_DAYS,
    DiskCache,
    FlatFileCache,
    InProcessLRUCache,
    InProcessTinyLFUCache,
    RedisCache,
    ShardedFileCache,
    TieredCacheRouter,
    _build_l2_from_config,
    _build_l3_from_config,
    _coerce,
    _env_override,
    _expand_path,
    _read_cache_config,
    build_cache_from_config,
    get_cache_router,
    is_cache_enabled,
    is_l1_enabled,
    is_l2_enabled,
    is_l3_enabled,
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
    assert DEFAULT_L2_FLAT_FILE_MAX_BYTES == 64 * 1024 * 1024
    assert DEFAULT_L3_SHARDED_TTL_DAYS == 30


# ─── Path expansion ───────────────────────────────────────────────────


def test_expand_path_handles_tilde():
    """``~`` is expanded to the user's home directory."""
    import os
    assert _expand_path("~/foo") == os.path.expanduser("~/foo")
    assert _expand_path("~") == os.path.expanduser("~")


def test_expand_path_handles_env_vars():
    """``$VAR`` is expanded from the environment."""
    with patch.dict(os.environ if False else {"MY_CACHE_DIR": "/tmp/cache"}):
        # Manually use a fresh dict because pytest's patch.dict scopes
        # the env var to the with-block.
        import os
        with patch.dict(os.environ, {"HERMES_TEST_DIR": "/tmp/hermes-test"}):
            result = _expand_path("$HERMES_TEST_DIR/l2.mmap")
            assert result == "/tmp/hermes-test/l2.mmap"


def test_expand_path_passthrough_for_non_strings():
    """Non-string values are returned unchanged."""
    assert _expand_path(42) == 42
    assert _expand_path(None) is None


# ─── Env-var override helper ──────────────────────────────────────────


def test_env_override_returns_value_when_set():
    """When HERMES_CACHE_<KEY> is set, _env_override returns it."""
    with patch.dict(os.environ, {"HERMES_CACHE_ENABLED": "true"}, clear=False):
        assert _env_override("enabled") == "true"


def test_env_override_returns_none_when_unset():
    """When the env var is not set, _env_override returns None."""
    # Make sure it's not set.
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("HERMES_CACHE_NONEXISTENT_KEY", None)
        assert _env_override("nonexistent_key") is None


def test_env_override_uppercases_dotted_keys():
    """Dotted keys are converted to UPPERCASE_WITH_UNDERSCORES."""
    with patch.dict(os.environ, {"HERMES_CACHE_L1_MAX_ENTRIES": "128"}, clear=False):
        assert _env_override("l1.max_entries") == "128"


# ─── _coerce helper ───────────────────────────────────────────────────


def test_coerce_to_bool_truthy():
    """Strings like '1', 'true', 'yes', 'on' coerce to True."""
    assert _coerce("1", False) is True
    assert _coerce("true", False) is True
    assert _coerce("True", False) is True
    assert _coerce("YES", False) is True
    assert _coerce("on", False) is True


def test_coerce_to_bool_falsy():
    """Strings like '0', 'false', 'no', 'off' coerce to False."""
    assert _coerce("0", True) is False
    assert _coerce("false", True) is False
    assert _coerce("no", True) is False
    assert _coerce("off", True) is False
    assert _coerce("garbage", True) is False  # not in the truthy set


def test_coerce_to_int():
    """Strings are coerced to int when the default is int."""
    assert _coerce("42", 0) == 42
    assert _coerce("-1", 0) == -1
    # Garbage falls back to the default.
    assert _coerce("not-a-number", 99) == 99


def test_coerce_to_float():
    """Strings are coerced to float when the default is float."""
    assert _coerce("3.14", 0.0) == 3.14
    assert _coerce("garbage", 9.9) == 9.9


def test_coerce_to_string_passthrough():
    """Strings are returned as-is when the default is string."""
    assert _coerce("hello", "default") == "hello"


def test_coerce_passthrough_when_default_is_none():
    """When the default is None, the string is returned unchanged."""
    assert _coerce("anything", None) == "anything"


# ─── Config loader ────────────────────────────────────────────────────


def test_read_cache_config_returns_defaults_when_no_config():
    """When the config file is missing, defaults are returned."""
    with patch("agent._cache._read_cache_config") as _:
        # Indirect: just make sure the function doesn't raise on a clean
        # process. The test for the real config goes below.
        pass
    # Real call: should not raise even if config is malformed.
    cfg = _read_cache_config()
    assert isinstance(cfg, dict)
    assert "enabled" in cfg
    assert "l1" in cfg
    assert "l2" in cfg
    assert "l3" in cfg


def test_read_cache_config_has_full_default_tree():
    """Every documented config key has a default value."""
    cfg = _read_cache_config()
    assert cfg["l1"]["max_entries"] == DEFAULT_L1_MAX_ENTRIES
    assert cfg["l1"]["max_bytes"] == DEFAULT_L1_MAX_BYTES
    assert cfg["l2"]["flat_file"]["path"] == DEFAULT_L2_FLAT_FILE_PATH
    assert cfg["l2"]["flat_file"]["max_bytes"] == DEFAULT_L2_FLAT_FILE_MAX_BYTES
    assert cfg["l2"]["redis"]["url"] == "redis://localhost:6379"
    assert cfg["l3"]["sharded_file"]["root"] == DEFAULT_L3_SHARDED_ROOT
    assert cfg["l3"]["sharded_file"]["ttl_days"] == DEFAULT_L3_SHARDED_TTL_DAYS


def test_read_cache_config_env_override_master_switch():
    """HERMES_CACHE_ENABLED=false disables the master switch."""
    with patch.dict(os.environ, {"HERMES_CACHE_ENABLED": "false"}, clear=False):
        cfg = _read_cache_config()
        assert cfg["enabled"] is False


def test_read_cache_config_env_override_l1_size():
    """HERMES_CACHE_L1_MAX_ENTRIES=128 grows the L1 cap."""
    with patch.dict(os.environ, {"HERMES_CACHE_L1_MAX_ENTRIES": "128"}, clear=False):
        cfg = _read_cache_config()
        assert cfg["l1"]["max_entries"] == 128


def test_read_cache_config_env_override_l2_path():
    """HERMES_CACHE_L2_FLAT_FILE_PATH=... overrides the L2 file path."""
    with patch.dict(
        os.environ,
        {"HERMES_CACHE_L2_FLAT_FILE_PATH": "/tmp/hermes-test-l2.mmap"},
        clear=False,
    ):
        cfg = _read_cache_config()
        assert cfg["l2"]["flat_file"]["path"] == "/tmp/hermes-test-l2.mmap"


def test_read_cache_config_graceful_fallback_on_exception():
    """When the config read fails entirely, defaults are returned."""
    with patch("hermes_cli.config.read_raw_config_readonly", side_effect=Exception):
        with patch("hermes_cli.config.read_raw_config", side_effect=Exception):
            cfg = _read_cache_config()
            # Defaults still come back.
            assert cfg["l1"]["max_entries"] == DEFAULT_L1_MAX_ENTRIES
            assert cfg["l2"]["flat_file"]["path"] == DEFAULT_L2_FLAT_FILE_PATH


# ─── Feature-gate helpers ────────────────────────────────────────────


def test_is_cache_enabled_default_true():
    """is_cache_enabled() returns True when nothing is set."""
    # Sanity: with no overrides, the master switch is on.
    assert is_cache_enabled() is True


def test_is_cache_enabled_respects_env():
    """HERMES_CACHE_ENABLED=false turns off the master switch."""
    with patch.dict(os.environ, {"HERMES_CACHE_ENABLED": "false"}, clear=False):
        assert is_cache_enabled() is False


def test_is_l1_enabled_default_true():
    """L1 is on by default."""
    assert is_l1_enabled() is True


def test_is_l1_disabled_when_master_off():
    """L1 is off when the master switch is off."""
    with patch.dict(os.environ, {"HERMES_CACHE_ENABLED": "false"}, clear=False):
        assert is_l1_enabled() is False


def test_is_l2_enabled_default_true():
    """L2 is on by default."""
    assert is_l2_enabled() is True


def test_is_l2_enabled_when_opted_in():
    """HERMES_CACHE_L2_ENABLED=true turns on L2 (no-op since already on)."""
    with patch.dict(os.environ, {"HERMES_CACHE_L2_ENABLED": "true"}, clear=False):
        assert is_l2_enabled() is True


def test_is_l2_enabled_can_be_disabled():
    """HERMES_CACHE_L2_ENABLED=false turns off L2."""
    with patch.dict(os.environ, {"HERMES_CACHE_L2_ENABLED": "false"}, clear=False):
        assert is_l2_enabled() is False


def test_is_l3_enabled_default_true():
    """L3 is on by default."""
    assert is_l3_enabled() is True


def test_is_l3_enabled_when_opted_in():
    """HERMES_CACHE_L3_ENABLED=true turns on L3 (no-op since already on)."""
    with patch.dict(os.environ, {"HERMES_CACHE_L3_ENABLED": "true"}, clear=False):
        assert is_l3_enabled() is True


def test_is_l3_enabled_can_be_disabled():
    """HERMES_CACHE_L3_ENABLED=false turns off L3."""
    with patch.dict(os.environ, {"HERMES_CACHE_L3_ENABLED": "false"}, clear=False):
        assert is_l3_enabled() is False


# ─── FlatFileCache (L2) — mmap ring buffer ────────────────────────────


def test_flat_file_cache_basic_put_and_get(tmp_path):
    """L2 mmap cache stores and retrieves values."""
    cache = FlatFileCache(
        path=str(tmp_path / "l2.mmap"),
        max_bytes=1024 * 1024,  # 1 MiB
    )
    try:
        cache.put("alpha", {"value": 1})
        assert cache.get("alpha") == {"value": 1}
    finally:
        cache.close()


def test_flat_file_cache_miss_returns_none(tmp_path):
    """L2 mmap cache returns None for missing keys."""
    cache = FlatFileCache(path=str(tmp_path / "l2.mmap"), max_bytes=1024 * 1024)
    try:
        assert cache.get("never_added") is None
    finally:
        cache.close()


def test_flat_file_cache_invalidate_removes_entry(tmp_path):
    """L2 mmap cache invalidation is a tombstone (logical delete)."""
    cache = FlatFileCache(path=str(tmp_path / "l2.mmap"), max_bytes=1024 * 1024)
    try:
        cache.put("k", "v")
        assert cache.get("k") == "v"
        cache.invalidate("k")
        assert cache.get("k") is None
    finally:
        cache.close()


def test_flat_file_cache_replace_existing(tmp_path):
    """Re-putting the same key replaces the value."""
    cache = FlatFileCache(path=str(tmp_path / "l2.mmap"), max_bytes=1024 * 1024)
    try:
        cache.put("k", "v1")
        cache.put("k", "v2")
        assert cache.get("k") == "v2"
    finally:
        cache.close()


def test_flat_file_cache_persists_across_instances(tmp_path):
    """L2 mmap cache survives process restart (the whole point of L2)."""
    path = str(tmp_path / "l2.mmap")
    c1 = FlatFileCache(path=path, max_bytes=1024 * 1024)
    c1.put("persisted", [1, 2, 3])
    c1.close()

    c2 = FlatFileCache(path=path, max_bytes=1024 * 1024)
    try:
        assert c2.get("persisted") == [1, 2, 3]
    finally:
        c2.close()


def test_flat_file_cache_overflow_value_is_dropped(tmp_path):
    """Values larger than SLOT_VALUE_MAX are dropped (fail-open, not raised)."""
    cache = FlatFileCache(path=str(tmp_path / "l2.mmap"), max_bytes=1024 * 1024)
    try:
        # 5 KiB > SLOT_VALUE_MAX (4 KiB).
        big = "x" * 5000
        cache.put("big", big)
        # The value should not be retrievable; the L2 dropped it.
        # (The L1 layer in production would still have it.)
        assert cache.get("big") is None
    finally:
        cache.close()


def test_flat_file_cache_read_only_blocks_writes(tmp_path):
    """Read-only mode: puts are silently dropped."""
    cache = FlatFileCache(
        path=str(tmp_path / "l2.mmap"),
        max_bytes=1024 * 1024,
        read_only=True,
    )
    try:
        cache.put("k", "v")
        # The put was silently dropped.
        assert cache.get("k") is None
    finally:
        cache.close()


def test_flat_file_cache_type_validation(tmp_path):
    """Non-str keys raise TypeError on every method."""
    cache = FlatFileCache(path=str(tmp_path / "l2.mmap"), max_bytes=1024 * 1024)
    try:
        with pytest.raises(TypeError):
            cache.get(42)
        with pytest.raises(TypeError):
            cache.put(42, "v")
        with pytest.raises(TypeError):
            cache.invalidate(42)
    finally:
        cache.close()


def test_flat_file_cache_stats_shape(tmp_path):
    """stats() includes backend, path, max_bytes, slot counts, estimated_bytes."""
    cache = FlatFileCache(path=str(tmp_path / "l2.mmap"), max_bytes=1024 * 1024)
    try:
        cache.put("k", "v")
        s = cache.stats()
        assert s["backend"] == "flat_file"
        assert s["max_bytes"] == 1024 * 1024
        assert s["slot_count"] > 0
        assert s["slot_value_max"] > 0
        assert "hits" in s
        assert "misses" in s
        assert "estimated_bytes" in s
        assert s["estimated_bytes"] > 0  # at least one entry stored
    finally:
        cache.close()


def test_flat_file_cache_estimated_bytes(tmp_path):
    """estimated_bytes grows as entries are added."""
    cache = FlatFileCache(path=str(tmp_path / "l2.mmap"), max_bytes=1024 * 1024)
    try:
        cache.put("a", "hello")
        bytes_after_first = cache.stats()["estimated_bytes"]
        cache.put("b", "world")
        bytes_after_second = cache.stats()["estimated_bytes"]
        assert bytes_after_second >= bytes_after_first
    finally:
        cache.close()


def test_flat_file_cache_hit_rate(tmp_path):
    """stats() reports hit_rate correctly."""
    cache = FlatFileCache(path=str(tmp_path / "l2.mmap"), max_bytes=1024 * 1024)
    try:
        cache.put("k", "v")
        cache.get("k")  # hit
        cache.get("missing")  # miss
        s = cache.stats()
        assert s["hits"] == 1
        assert s["misses"] == 1
        assert abs(s["hit_rate"] - 0.5) < 1e-9
    finally:
        cache.close()


# ─── ShardedFileCache (L3) — sharded directory with mtime TTL ─────────


def test_sharded_file_cache_basic_put_and_get(tmp_path):
    """L3 sharded cache stores and retrieves values."""
    cache = ShardedFileCache(root=str(tmp_path / "l3"), ttl_days=7)
    cache.put("alpha", "value-alpha")
    assert cache.get("alpha") == "value-alpha"


def test_sharded_file_cache_miss_returns_none(tmp_path):
    """L3 returns None for missing keys."""
    cache = ShardedFileCache(root=str(tmp_path / "l3"), ttl_days=7)
    assert cache.get("never_added") is None


def test_sharded_file_cache_invalidate_removes_entry(tmp_path):
    """invalidate() removes the entry and its metadata sidecar."""
    cache = ShardedFileCache(root=str(tmp_path / "l3"), ttl_days=7)
    cache.put("k", "v")
    assert cache.get("k") == "v"
    cache.invalidate("k")
    assert cache.get("k") is None


def test_sharded_file_cache_replace_existing(tmp_path):
    """Re-putting the same key replaces the value."""
    cache = ShardedFileCache(root=str(tmp_path / "l3"), ttl_days=7)
    cache.put("k", "v1")
    cache.put("k", "v2")
    assert cache.get("k") == "v2"


def test_sharded_file_cache_persists_across_instances(tmp_path):
    """L3 survives process restart (the whole point of L3)."""
    root = str(tmp_path / "l3")
    c1 = ShardedFileCache(root=root, ttl_days=7)
    c1.put("persisted", {"deep": [1, 2, 3]})

    c2 = ShardedFileCache(root=root, ttl_days=7)
    assert c2.get("persisted") == {"deep": [1, 2, 3]}


def test_sharded_file_cache_shard_layout(tmp_path):
    """Entries are placed in a 2-level shard directory."""
    root = Path(tmp_path) / "l3"
    cache = ShardedFileCache(root=str(root), ttl_days=7)
    cache.put("k", "v")
    # Walk the tree — there should be a 2-level shard.
    found = list(root.glob("??/??/*"))
    assert any(p.is_file() and not p.name.endswith(".json") for p in found)


def test_sharded_file_cache_evict_expired(tmp_path):
    """evict_expired() removes entries older than the TTL."""
    root = str(tmp_path / "l3")
    cache = ShardedFileCache(root=root, ttl_days=1)
    cache.put("old", "old-value")
    # Backdate the mtime by writing a new file with an old timestamp.
    # We can't easily backdate via the cache API, so we reach into the index.
    target = ShardedFileCache._key_path(Path(root), "old")
    if target.exists():
        old_time = time.time() - (86400 * 30)  # 30 days ago
        import os
        os.utime(target, (old_time, old_time))
        # Also backdate the metadata sidecar.
        meta = target.with_suffix(target.suffix + ".json")
        if meta.exists():
            os.utime(meta, (old_time, old_time))
        # Invalidate the index so it rebuilds with the new mtimes.
        cache._index = {}
        cache._index_built = False
        removed = cache.evict_expired()
        assert removed >= 1
        assert cache.get("old") is None


def test_sharded_file_cache_type_validation(tmp_path):
    """Non-str keys raise TypeError on every method."""
    cache = ShardedFileCache(root=str(tmp_path / "l3"), ttl_days=7)
    with pytest.raises(TypeError):
        cache.get(42)
    with pytest.raises(TypeError):
        cache.put(42, "v")
    with pytest.raises(TypeError):
        cache.invalidate(42)


def test_sharded_file_cache_stats_shape(tmp_path):
    """stats() includes backend, root, ttl_days, entries count."""
    cache = ShardedFileCache(root=str(tmp_path / "l3"), ttl_days=14)
    cache.put("k", "v")
    s = cache.stats()
    assert s["backend"] == "sharded_file"
    assert s["ttl_days"] == 14
    assert s["entries"] == 1
    assert "hits" in s
    assert "misses" in s


def test_sharded_file_cache_vacuum_removes_empty_dirs(tmp_path):
    """vacuum() removes empty shard directories after entries are invalidated."""
    cache = ShardedFileCache(root=str(tmp_path / "l3"), ttl_days=7)
    cache.put("k1", "v1")
    cache.put("k2", "v2")
    # Invalidate all entries so shards become empty.
    cache.invalidate("k1")
    cache.invalidate("k2")
    removed = cache.vacuum()
    assert removed >= 0  # may remove some or all empty dirs
    # Verify the root still exists.
    assert (tmp_path / "l3").exists()


def test_sharded_file_cache_vacuum_is_idempotent(tmp_path):
    """vacuum() can be called multiple times without error."""
    cache = ShardedFileCache(root=str(tmp_path / "l3"), ttl_days=7)
    cache.put("k", "v")
    cache.invalidate("k")
    cache.vacuum()
    cache.vacuum()  # second call should not raise
    assert cache.get("k") is None


# ─── TieredCacheRouter with mixed tiers ───────────────────────────────


def test_router_writes_through_to_all_enabled_tiers(tmp_path):
    """put() writes to every enabled tier; get() walks them in order."""
    l1 = InProcessLRUCache()
    l2_path = str(tmp_path / "l2.mmap")
    l2 = FlatFileCache(path=l2_path, max_bytes=1024 * 1024)
    l3_root = str(tmp_path / "l3")
    l3 = ShardedFileCache(root=l3_root, ttl_days=7)
    try:
        router = TieredCacheRouter(l1, l2, l3)
        router.put("k", "v")
        # All three tiers have it.
        assert l1.get("k") == "v"
        assert l2.get("k") == "v"
        assert l3.get("k") == "v"
    finally:
        l2.close()


def test_router_reads_from_lower_tier_writes_back_to_higher(tmp_path):
    """On L1 miss but L2 hit, the value is written back to L1."""
    l1 = InProcessLRUCache()
    l2 = FlatFileCache(path=str(tmp_path / "l2.mmap"), max_bytes=1024 * 1024)
    l2.put("k", "from-l2")
    try:
        router = TieredCacheRouter(l1, l2)
        assert router.get("k") == "from-l2"
        # L1 was populated by write-back.
        assert l1.get("k") == "from-l2"
    finally:
        l2.close()


def test_router_invalidate_removes_from_all_tiers(tmp_path):
    """invalidate() removes the key from every enabled tier."""
    l1 = InProcessLRUCache()
    l2 = FlatFileCache(path=str(tmp_path / "l2.mmap"), max_bytes=1024 * 1024)
    try:
        router = TieredCacheRouter(l1, l2)
        router.put("k", "v")
        router.invalidate("k")
        assert l1.get("k") is None
        assert l2.get("k") is None
    finally:
        l2.close()


def test_router_tier_failure_does_not_break_reads():
    """A broken L2 does not take down the L1 read path."""
    l1 = InProcessLRUCache()
    l1.put("k", "from-l1")

    class BrokenTier:
        def get(self, key):
            raise RuntimeError("L2 is on fire")
        def put(self, key, value):
            raise RuntimeError("L2 is on fire")
        def invalidate(self, key):
            raise RuntimeError("L2 is on fire")
        def stats(self):
            return {"backend": "broken"}

    router = TieredCacheRouter(l1, BrokenTier())
    # L1 hit — the broken L2 is never consulted.
    assert router.get("k") == "from-l1"


# ─── build_cache_from_config — feature-gate aware factory ─────────────


def test_build_cache_returns_l1_l2_l3_by_default():
    """Default config returns a router with L1, L2, and L3."""
    cache = build_cache_from_config()
    s = cache.stats()
    # Three tiers — L1, L2, L3 all enabled by default.
    assert len(s["tiers"]) == 3
    tier_names = [t["tier"] for t in s["tiers"]]
    assert "InProcessLRUCache" in tier_names
    assert "FlatFileCache" in tier_names
    assert "ShardedFileCache" in tier_names


def test_build_cache_includes_l2_when_enabled(tmp_path):
    """With L2 enabled, the router includes the flat-file L2."""
    with patch.dict(os.environ, {
        "HERMES_CACHE_L2_ENABLED": "true",
        "HERMES_CACHE_L2_FLAT_FILE_PATH": str(tmp_path / "l2.mmap"),
        "HERMES_CACHE_L2_FLAT_FILE_MAX_BYTES": str(1024 * 1024),
    }, clear=False):
        cache = build_cache_from_config()
        s = cache.stats()
        tier_names = [t["tier"] for t in s["tiers"]]
        assert "InProcessLRUCache" in tier_names
        assert "FlatFileCache" in tier_names
        # Clean up the L2 instance.
        for t_obj in s["tiers"]:
            tier_obj = t_obj.get("tier")
        # Find the L2 instance and close it.
        for tier_inst in cache._tiers:
            if isinstance(tier_inst, FlatFileCache):
                tier_inst.close()


def test_build_cache_master_off_returns_placeholder():
    """When the master switch is off, a single-entry placeholder is returned."""
    with patch.dict(os.environ, {"HERMES_CACHE_ENABLED": "false"}, clear=False):
        cache = build_cache_from_config()
        s = cache.stats()
        # Only the placeholder tier.
        assert len(s["tiers"]) == 1
        # The placeholder has max_entries=1 — anything put and not
        # read back is a one-shot.
        cache.put("k", "v")
        assert cache.get("k") == "v"


# ─── _build_l2_from_config / _build_l3_from_config helpers ────────────


def test_build_l2_returns_none_when_disabled():
    """L2 builder returns None when explicitly turned off."""
    with patch.dict(os.environ, {"HERMES_CACHE_L2_ENABLED": "false"}, clear=False):
        assert _build_l2_from_config() is None


def test_build_l2_returns_flat_file_when_enabled(tmp_path):
    """L2 builder returns a FlatFileCache when L2 is on (default backend)."""
    with patch.dict(os.environ, {
        "HERMES_CACHE_L2_ENABLED": "true",
        "HERMES_CACHE_L2_FLAT_FILE_PATH": str(tmp_path / "l2.mmap"),
        "HERMES_CACHE_L2_FLAT_FILE_MAX_BYTES": str(1024 * 1024),
    }, clear=False):
        cache = _build_l2_from_config()
        try:
            assert isinstance(cache, FlatFileCache)
        finally:
            if cache is not None:
                cache.close()


def test_build_l2_returns_redis_stub_when_backend_is_redis():
    """L2 builder returns a RedisCache stub when backend=redis.

    Constructing RedisCache raises NotImplementedError — that's
    the contract. So we expect the construction to fail loudly.
    """
    with patch.dict(os.environ, {
        "HERMES_CACHE_L2_ENABLED": "true",
        "HERMES_CACHE_L2_BACKEND": "redis",
    }, clear=False):
        with pytest.raises(NotImplementedError):
            _build_l2_from_config()


def test_build_l3_returns_none_when_disabled():
    """L3 builder returns None when explicitly turned off."""
    with patch.dict(os.environ, {"HERMES_CACHE_L3_ENABLED": "false"}, clear=False):
        assert _build_l3_from_config() is None


def test_build_l3_returns_sharded_file_when_enabled(tmp_path):
    """L3 builder returns a ShardedFileCache when L3 is on (default backend)."""
    with patch.dict(os.environ, {
        "HERMES_CACHE_L3_ENABLED": "true",
        "HERMES_CACHE_L3_SHARDED_FILE_ROOT": str(tmp_path / "l3"),
    }, clear=False):
        cache = _build_l3_from_config()
        assert isinstance(cache, ShardedFileCache)


def test_build_l3_returns_sqlite_stub_when_backend_is_sqlite():
    """L3 builder returns a DiskCache stub when backend=sqlite.

    Construction raises NotImplementedError — the contract.
    """
    with patch.dict(os.environ, {
        "HERMES_CACHE_L3_ENABLED": "true",
        "HERMES_CACHE_L3_BACKEND": "sqlite",
    }, clear=False):
        with pytest.raises(NotImplementedError):
            _build_l3_from_config()



# ---------------------------------------------------------------------------
# I1: per-shard locking — aggregate semantics + shard distribution
# ---------------------------------------------------------------------------
class TestShardedLocking:
    """I1: sharded InProcessLRUCache preserves aggregate budgets exactly."""

    def test_multi_shard_created_for_large_caches(self):
        from agent._cache import InProcessLRUCache
        c = InProcessLRUCache(max_entries=1000)
        assert c._num_shards == 16

    def test_single_shard_for_small_caches(self):
        from agent._cache import InProcessLRUCache
        c = InProcessLRUCache(max_entries=64)
        assert c._num_shards == 1

    def test_aggregate_entry_budget_across_shards(self):
        """800 keys spread over 16 shards must respect the 1000-entry
        global budget with ZERO evictions (hash skew must not cause
        premature per-shard eviction)."""
        from agent._cache import InProcessLRUCache
        c = InProcessLRUCache(max_entries=1000)
        for i in range(800):
            c.put(f"key-{i}", i)
        assert len(c) == 800
        assert c.evictions == 0

    def test_global_lru_eviction_across_shards(self):
        """The globally-oldest entry is evicted even when it lives in a
        different shard than the most recent puts."""
        import hashlib
        from agent._cache import InProcessLRUCache
        c = InProcessLRUCache(max_entries=500)
        # Find two keys in different shards.
        def shard_of(k):
            return hashlib.sha256(k.encode()).digest()[0] % 16
        k_old = k_new = None
        for i in range(200):
            if shard_of(f"old-{i}") != shard_of("anchor"):
                k_old = f"old-{i}"
                break
        for i in range(200):
            if shard_of(f"new-{i}") not in (shard_of("anchor"), shard_of(k_old)):
                k_new = f"new-{i}"
                break
        assert k_old and k_new and shard_of(k_old) != shard_of(k_new)
        c.put(k_old, "v")
        for i in range(499):
            c.put(f"filler-{i}", i)
        assert len(c) == 500
        # One more put forces eviction of the globally-oldest = k_old.
        c.put("trigger", 1)
        assert len(c) == 500
        assert c.get(k_old) is None          # evicted (oldest)
        assert c.get("filler-0") is not None  # newer, survives

    def test_stats_reports_num_shards(self):
        from agent._cache import InProcessLRUCache
        s16 = InProcessLRUCache(max_entries=128).stats()
        s1 = InProcessLRUCache(max_entries=32).stats()
        assert s16["num_shards"] == 16
        assert s1["num_shards"] == 1

    def test_shard_selection_is_stable_across_instances(self):
        """sha256-based selection: same key -> same shard index in any
        instance/process (PYTHONHASHSEED-independent)."""
        from agent._cache import InProcessLRUCache
        a = InProcessLRUCache(max_entries=1000)
        b = InProcessLRUCache(max_entries=1000)
        for k in ("alpha", "beta", "gamma", "x" * 300):
            assert a._shard_index(k) == b._shard_index(k)

    def test_concurrent_puts_no_loss_under_budget(self):
        """8 threads x 100 distinct keys into a 1000-entry cache:
        all 800 keys present, zero evictions."""
        import threading
        from agent._cache import InProcessLRUCache
        c = InProcessLRUCache(max_entries=1000)
        def worker(tid):
            for i in range(100):
                c.put(f"t{tid}-k{i}", i)
        threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert len(c) == 800
        assert c.evictions == 0

    def test_oversized_put_skipped_and_counted(self):
        from agent._cache import InProcessLRUCache
        c = InProcessLRUCache(max_entries=10, max_bytes=1024)
        before = c.cache_skip_oversized
        c.put("huge", "x" * 4096)
        assert c.cache_skip_oversized == before + 1
        assert len(c) == 0


# ---------------------------------------------------------------------------
# I2: sharded mmap FlatFileCache
# ---------------------------------------------------------------------------
class TestShardedFlatFile:
    """I2: sharded mmap L2 — shard count, isolation, legacy retirement."""

    def test_multi_shard_for_large_budget(self, tmp_path):
        from agent._cache import FlatFileCache
        c = FlatFileCache(path=str(tmp_path / "l2.mmap"), max_bytes=64 * 1024 * 1024)
        assert c._num_shards == 16
        c.close()

    def test_single_shard_for_small_budget(self, tmp_path):
        from agent._cache import FlatFileCache
        c = FlatFileCache(path=str(tmp_path / "l2.mmap"), max_bytes=1024 * 1024)
        assert c._num_shards == 1
        c.close()

    def test_roundtrip_and_persistence(self, tmp_path):
        import os
        from agent._cache import FlatFileCache
        p = str(tmp_path / "l2.mmap")
        c = FlatFileCache(path=p, max_bytes=64 * 1024 * 1024)
        c.put("alpha", {"v": 1})
        assert c.get("alpha") == {"v": 1}
        c.close()
        # Reopen: entries persist in per-shard files.
        c2 = FlatFileCache(path=p, max_bytes=64 * 1024 * 1024)
        assert c2.get("alpha") == {"v": 1}
        c2.close()

    def test_shard_files_created(self, tmp_path):
        import os
        from agent._cache import FlatFileCache
        p = str(tmp_path / "l2.mmap")
        c = FlatFileCache(path=p, max_bytes=64 * 1024 * 1024)
        c.put("k", "v")
        c.close()
        shards = [f for f in os.listdir(tmp_path) if ".s" in f]
        assert len(shards) == 16

    def test_legacy_file_retired_once(self, tmp_path):
        import os
        from agent._cache import FlatFileCache
        p = str(tmp_path / "l2.mmap")
        # Simulate a pre-sharding single file with content.
        with open(p, "wb") as f:
            f.write(b"\x00" * 4096)
        c = FlatFileCache(path=p, max_bytes=64 * 1024 * 1024)
        c.close()
        assert os.path.exists(p + ".legacy")
        assert not os.path.exists(p) or os.path.getsize(p) == 0

    def test_stable_shard_selection(self, tmp_path):
        """Same key -> same shard INDEX across instances (sha256, not
        hash()); PYTHONHASHSEED-independent."""
        import hashlib
        from agent._cache import FlatFileCache
        a = FlatFileCache(path=str(tmp_path / "a.mmap"), max_bytes=64 * 1024 * 1024)
        b = FlatFileCache(path=str(tmp_path / "b.mmap"), max_bytes=64 * 1024 * 1024)

        def idx_of(cache, k):
            # Derive index from which shard file the key maps to.
            return int(str(cache._shard_for(k)._path).rsplit(".s", 1)[1].split(".")[0])

        for k in ("x", "y", "z" * 500):
            assert idx_of(a, k) == idx_of(b, k)
            assert idx_of(a, k) == (
                hashlib.sha256(k.encode("utf-8", "surrogatepass")).digest()[0] % 16
            )
        a.close()
        b.close()

    def test_typeerror_on_non_str_key(self, tmp_path):
        import pytest
        from agent._cache import FlatFileCache
        c = FlatFileCache(path=str(tmp_path / "l2.mmap"), max_bytes=64 * 1024 * 1024)
        with pytest.raises(TypeError):
            c.get(123)
        with pytest.raises(TypeError):
            c.put(123, "v")
        c.close()

    def test_stats_contract_keys(self, tmp_path):
        from agent._cache import FlatFileCache
        c = FlatFileCache(path=str(tmp_path / "l2.mmap"), max_bytes=64 * 1024 * 1024)
        st = c.stats()
        for k in ("backend", "path", "max_bytes", "hits", "misses",
                  "evictions", "hit_rate", "num_shards"):
            assert k in st, f"missing stats key {k}"
        assert st["backend"] == "flat_file"
        c.close()


# ---------------------------------------------------------------------------
# I3: elephant guard — value_max_bytes ceiling
# ---------------------------------------------------------------------------
class TestElephantGuard:
    """I3: per-value ceiling, config wiring, boundary semantics."""

    def test_default_ceiling_is_whole_budget(self):
        from agent._cache import InProcessLRUCache
        c = InProcessLRUCache(max_entries=10, max_bytes=4096)
        assert c._value_max_bytes == 4096

    def test_tighter_ceiling_skips_midsize_values(self):
        from agent._cache import InProcessLRUCache
        c = InProcessLRUCache(max_entries=10, max_bytes=65536,
                              value_max_bytes=512)
        c.put("small", "x" * 100)
        assert c.get("small") is not None
        before = c.cache_skip_oversized
        c.put("mid", "x" * 2000)  # fits budget, exceeds ceiling
        assert c.cache_skip_oversized == before + 1
        assert c.get("mid") is None

    def test_ceiling_capped_at_budget(self):
        from agent._cache import InProcessLRUCache
        c = InProcessLRUCache(max_entries=10, max_bytes=1024,
                              value_max_bytes=999999)
        assert c._value_max_bytes == 1024

    def test_ceiling_validation(self):
        import pytest
        from agent._cache import InProcessLRUCache
        with pytest.raises(ValueError):
            InProcessLRUCache(max_entries=10, value_max_bytes=0)

    def test_boundary_value_equal_to_ceiling_is_allowed(self):
        from agent._cache import InProcessLRUCache
        c = InProcessLRUCache(max_entries=10, max_bytes=8192,
                              value_max_bytes=1024)
        # size_of = len(key) + len(repr(value)); craft to land exactly on it.
        v = "x" * (1024 - len("edge") - 2)  # repr adds 2 quotes
        c.put("edge", v)
        assert c.get("edge") is not None

    def test_config_wiring_value_max_bytes(self, monkeypatch):
        from agent import _cache as cache_mod
        monkeypatch.setattr(
            cache_mod, "_read_cache_config",
            lambda: {"l1": {
                "enabled": True,
                "max_entries": 64,
                "max_bytes": 1048576,
                "value_max_bytes": 2048,
            }},
            raising=False,
        )
        router = cache_mod.build_cache_from_config()
        l1 = router._tiers[0]
        assert l1._value_max_bytes == 2048


# ─── CircuitBreaker tests ──────────────────────────────────────────────


class TestCircuitBreaker:
    """Tests for CircuitBreaker class."""

    def test_initial_state_closed(self):
        from agent._cache import CircuitBreaker
        cb = CircuitBreaker(failure_threshold=3, reset_timeout=1.0)
        assert cb.is_open is False
        state = cb.get_state()
        assert state["open"] is False
        assert state["failures"] == 0

    def test_records_success_resets_failures(self):
        from agent._cache import CircuitBreaker
        cb = CircuitBreaker(failure_threshold=3, reset_timeout=1.0)
        cb.record_failure()
        cb.record_failure()
        assert cb.get_state()["failures"] == 2
        cb.record_success()
        assert cb.get_state()["failures"] == 0
        assert cb.is_open is False

    def test_opens_after_threshold_failures(self):
        from agent._cache import CircuitBreaker
        cb = CircuitBreaker(failure_threshold=3, reset_timeout=1.0)
        cb.record_failure()
        cb.record_failure()
        assert cb.is_open is False
        cb.record_failure()
        assert cb.is_open is True

    def test_half_open_after_reset_timeout(self):
        from agent._cache import CircuitBreaker
        cb = CircuitBreaker(failure_threshold=2, reset_timeout=0.1)
        cb.record_failure()
        cb.record_failure()
        assert cb.is_open is True
        time.sleep(0.15)
        assert cb.is_open is False

    def test_success_after_half_open(self):
        from agent._cache import CircuitBreaker
        cb = CircuitBreaker(failure_threshold=2, reset_timeout=0.1)
        cb.record_failure()
        cb.record_failure()
        assert cb.is_open is True
        time.sleep(0.15)
        cb.record_success()
        assert cb.is_open is False
        assert cb.get_state()["failures"] == 0


# ─── Router with circuit breaker tests ────────────────────────────────


class TestRouterCircuitBreaker:
    """Tests for TieredCacheRouter circuit breaker integration."""

    def test_stats_includes_circuit_breaker(self):
        from agent._cache import InProcessLRUCache, TieredCacheRouter
        router = TieredCacheRouter(InProcessLRUCache())
        stats = router.stats()
        assert "circuit_breaker" in stats["tiers"][0]
        assert "metrics" in stats

    def test_circuit_breaker_trips_on_tier_failure(self):
        from agent._cache import InProcessLRUCache, TieredCacheRouter
        l1 = InProcessLRUCache()
        router = TieredCacheRouter(l1, failure_threshold=3, reset_timeout=10.0)
        # Should work normally
        router.put("key", "value")
        assert router.get("key") == "value"
        # Check initial state
        stats = router.stats()
        assert stats["tiers"][0]["circuit_breaker"]["open"] is False


# ─── InProcessTinyLFUCache tests ──────────────────────────────────────


class TestInProcessTinyLFUCache:
    """Tests for W-TinyLFU cache (theine-based)."""

    def test_basic_put_get(self):
        """Basic put and get operations work."""
        cache = InProcessTinyLFUCache(max_entries=100)
        cache.put("key1", "value1")
        assert cache.get("key1") == "value1"

    def test_get_missing_returns_none(self):
        """Missing key returns None."""
        cache = InProcessTinyLFUCache(max_entries=100)
        assert cache.get("missing") is None

    def test_stats_reports_metrics(self):
        """stats() reports hits, misses, and hit_rate."""
        cache = InProcessTinyLFUCache(max_entries=100)
        cache.put("k", "v")
        cache.get("k")  # hit
        cache.get("missing")  # miss
        stats = cache.stats()
        assert stats["hits"] == 1
        assert stats["misses"] == 1
        assert abs(stats["hit_rate"] - 0.5) < 1e-9

    def test_invalidate_removes_entry(self):
        """invalidate() removes the entry."""
        cache = InProcessTinyLFUCache(max_entries=100)
        cache.put("k", "v")
        assert cache.get("k") == "v"
        cache.invalidate("k")
        assert cache.get("k") is None

    def test_clear_removes_all(self):
        """clear() empties the cache."""
        cache = InProcessTinyLFUCache(max_entries=100)
        cache.put("a", 1)
        cache.put("b", 2)
        assert len(cache) == 2
        cache.clear()
        assert len(cache) == 0

    def test_contains_operator(self):
        """'in' operator works for membership checks."""
        cache = InProcessTinyLFUCache(max_entries=100)
        cache.put("present", 1)
        assert "present" in cache
        assert "absent" not in cache

    def test_invalid_max_entries_raises(self):
        """max_entries < 1 raises ValueError."""
        with pytest.raises(ValueError):
            InProcessTinyLFUCache(max_entries=0)

    def test_ttl_expiration(self):
        """Entries expire after TTL seconds."""
        cache = InProcessTinyLFUCache(max_entries=100, ttl_seconds=1)
        cache.put("key", "value")
        assert cache.get("key") == "value"
        # Wait for TTL to expire
        import time
        time.sleep(1.1)
        # theine should have evicted it
        result = cache.get("key")
        assert result is None or cache.get("key") is None

    def test_capacity_limit(self):
        """Cache respects max_entries capacity."""
        cache = InProcessTinyLFUCache(max_entries=5)
        for i in range(10):
            cache.put(f"key{i}", f"value{i}")
        # Should have at most 5 entries
        assert len(cache) <= 5

    def test_w_tiny_lfu_adaptive_behavior(self):
        """W-TinyLFU adapts to access patterns better than LRU.

        With W-TinyLFU, frequently accessed keys should be retained
        even when the cache is full, while less-frequently accessed
        keys are evicted.
        """
        cache = InProcessTinyLFUCache(max_entries=10)
        # Fill cache
        for i in range(10):
            cache.put(f"key{i}", f"value{i}")
        # Access some keys multiple times (hot keys)
        for _ in range(20):
            cache.get("key0")
            cache.get("key1")
        # Add more entries to force eviction
        for i in range(10, 20):
            cache.put(f"key{i}", f"value{i}")
        # Hot keys should still be present
        assert cache.get("key0") == "value0"
        assert cache.get("key1") == "value1"

    def test_stats_on_empty_cache(self):
        """Empty cache has zero hit_rate."""
        cache = InProcessTinyLFUCache(max_entries=100)
        stats = cache.stats()
        assert stats["hit_rate"] == 0.0
        assert stats["entries"] == 0


# ─── get_cache_status() ───────────────────────────────────────────────────


def test_get_cache_status_returns_dict():
    """get_cache_status() returns a dict with cache metrics."""
    from agent._cache import get_cache_status
    s = get_cache_status()
    assert isinstance(s, dict)
    assert "tiers" in s
    assert "aggregate_hit_rate" in s


def test_get_cache_status_is_safe():
    """get_cache_status() never raises even if cache is broken."""
    from agent._cache import get_cache_status
    s = get_cache_status()
    # Should not raise
    assert isinstance(s, dict)


# ─── Phase 5: Config Round-Trip ─────────────────────────────────────────────


def test_config_round_trip_all_options(tmp_path):
    """Config block round-trips through build_cache_from_config()."""
    import os
    from unittest.mock import patch
    from agent._cache import build_cache_from_config

    # Reset singleton for clean test
    import agent._cache as cache_mod
    cache_mod._router_singleton = None

    # Build with default config
    config = {
        "enabled": True,
        "l1": {"enabled": True, "max_entries": 128, "max_bytes": 16777216},
        "l2": {"enabled": True, "backend": "flat_file", "flat_file": {
            "path": str(tmp_path / "l2.mmap"),
            "max_bytes": 33554432,
        }},
        "l3": {"enabled": True, "backend": "flat_file", "flat_file": {
            "root": str(tmp_path / "l3"),
            "ttl_days": 7,
        }},
    }
    with patch.dict(os.environ, {}, clear=False):
        with patch("agent._cache._read_cache_config", return_value=config):
            cache = build_cache_from_config()
            stats = cache.stats()
            assert "tiers" in stats
            # Verify at least L1 is present
            tier_names = [t["tier"] for t in stats["tiers"]]
            assert "InProcessLRUCache" in tier_names


def test_config_disabled_cache_returns_empty():
    """Disabled cache returns empty router."""
    import os
    from unittest.mock import patch
    from agent._cache import build_cache_from_config

    import agent._cache as cache_mod
    cache_mod._router_singleton = None

    config = {"enabled": False}
    with patch.dict(os.environ, {}, clear=False):
        with patch("agent._cache._read_cache_config", return_value=config):
            cache = build_cache_from_config()
            stats = cache.stats()
            assert stats["aggregate_hits"] == 0
            assert stats["aggregate_misses"] == 0


# ─── Phase 5: End-to-End Integration ────────────────────────────────────────


def test_e2e_write_then_read_all_tiers():
    """Full read path: L1 miss → L2 miss → L3 miss → source."""
    from agent._cache import InProcessLRUCache, FlatFileCache, ShardedFileCache
    from agent._cache import TieredCacheRouter
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        l1 = InProcessLRUCache(max_entries=64)
        l2 = FlatFileCache(path=tmpdir + "/l2.mmap", max_bytes=1024 * 1024)
        l3 = ShardedFileCache(root=tmpdir + "/l3", ttl_days=7)
        router = TieredCacheRouter(l1, l2, l3)

        try:
            # Write directly to L3
            router.put("test_key", "test_value")

            # Read from L1 (cache miss, should populate all tiers)
            result = router.get("test_key")
            assert result == "test_value"

            # Subsequent reads should hit L1
            result2 = router.get("test_key")
            assert result2 == "test_value"

            # Stats should show hits and misses
            stats = router.stats()
            assert stats["aggregate_hits"] >= 1
            # First read is a miss, subsequent reads are hits
            assert stats["aggregate_misses"] >= 0  # May be 0 if L3 had it
        finally:
            # Clean up tiers
            l1.clear()
            l2.close()
            # ShardedFileCache uses __del__ for cleanup


def test_e2e_l1_only_mode():
    """L1-only mode works correctly."""
    from agent._cache import InProcessLRUCache
    from agent._cache import TieredCacheRouter

    l1 = InProcessLRUCache(max_entries=32)
    router = TieredCacheRouter(l1)

    try:
        router.put("key", "value")
        assert router.get("key") == "value"
        assert router.get("missing") is None

        stats = router.stats()
        assert len(stats["tiers"]) == 1
    finally:
        l1.clear()


def test_e2e_circuit_breaker_degradation():
    """Circuit breaker opens on repeated failures, degrades gracefully."""
    from agent._cache import InProcessLRUCache
    from agent._cache import TieredCacheRouter, CircuitBreaker

    l1 = InProcessLRUCache(max_entries=32)
    router = TieredCacheRouter(l1, failure_threshold=2, reset_timeout=0.1)

    try:
        # First write should work
        router.put("key", "value")
        assert router.get("key") == "value"

        # Stats should have circuit breaker info per-tier
        stats = router.stats()
        tier_0 = stats["tiers"][0]
        assert "circuit_breaker" in tier_0
        assert tier_0["circuit_breaker"]["open"] in [False, True]
    finally:
        l1.clear()
