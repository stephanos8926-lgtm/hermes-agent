"""Tests for get_cache_router() singleton."""
import os
import pytest
from unittest.mock import patch

from agent._cache import (
    build_cache_from_config,
    get_cache_router,
    is_cache_enabled,
    is_l1_enabled,
    is_l2_enabled,
    is_l3_enabled,
    _read_cache_config,
)


def test_get_cache_router_returns_singleton():
    """get_cache_router() returns the same instance on repeated calls."""
    # Reset any cached router first
    import agent._cache as cache_mod
    cache_mod._router_singleton = None
    
    router1 = get_cache_router()
    router2 = get_cache_router()
    assert router1 is router2


def test_get_cache_router_returns_router_instance():
    """get_cache_router() returns a TieredCacheRouter instance."""
    import agent._cache as cache_mod
    cache_mod._router_singleton = None
    
    from agent._cache import TieredCacheRouter
    router = get_cache_router()
    assert isinstance(router, TieredCacheRouter)


def test_get_cache_router_respects_cache_disabled():
    """When cache.enabled=false, router wraps a placeholder."""
    import agent._cache as cache_mod
    cache_mod._router_singleton = None
    
    with patch.dict(os.environ, {"HERMES_CACHE_ENABLED": "false"}, clear=False):
        router = get_cache_router()
        from agent._cache import TieredCacheRouter
        assert isinstance(router, TieredCacheRouter)
        # Should have a single placeholder tier
        stats = router.stats()
        assert len(stats["tiers"]) == 1
        assert stats["tiers"][0]["tier"] == "InProcessLRUCache"
        # The placeholder has max_entries=1 (nested in tier.stats)
        assert stats["tiers"][0]["stats"]["max_entries"] == 1


def test_get_cache_router_respects_l2_enabled():
    """When L2 is enabled, router includes FlatFileCache tier."""
    import agent._cache as cache_mod
    cache_mod._router_singleton = None
    
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        with patch.dict(os.environ, {
            "HERMES_CACHE_L2_ENABLED": "true",
            "HERMES_CACHE_L2_FLAT_FILE_PATH": os.path.join(tmpdir, "l2.mmap"),
            "HERMES_CACHE_L2_FLAT_FILE_MAX_BYTES": str(1024 * 1024),
        }, clear=False):
            cache_mod._router_singleton = None
            router = get_cache_router()
            stats = router.stats()
            tier_names = [t["tier"] for t in stats["tiers"]]
            assert "InProcessLRUCache" in tier_names
            assert "FlatFileCache" in tier_names


def test_get_cache_router_uses_build_cache_from_config():
    """get_cache_router() delegates to build_cache_from_config()."""
    import agent._cache as cache_mod
    cache_mod._router_singleton = None
    
    # The router should be built by build_cache_from_config()
    router = get_cache_router()
    assert router is not None
    # Should be a TieredCacheRouter
    from agent._cache import TieredCacheRouter
    assert isinstance(router, TieredCacheRouter)


def test_get_cache_router_resets_on_config_change():
    """If config changes, caller must reset the singleton."""
    import agent._cache as cache_mod
    cache_mod._router_singleton = None
    
    router1 = get_cache_router()
    # Simulate config change by resetting the singleton
    cache_mod._router_singleton = None
    router2 = get_cache_router()
    
    # Different instances after reset
    assert router1 is not router2


def test_get_cache_router_is_thread_safe():
    """Multiple threads calling get_cache_router() get the same instance."""
    import threading
    import agent._cache as cache_mod
    cache_mod._router_singleton = None
    
    results = []
    def worker():
        router = get_cache_router()
        results.append(id(router))
    
    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    
    # All threads should get the same instance
    assert len(set(results)) == 1