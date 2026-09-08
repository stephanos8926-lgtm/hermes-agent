"""Tests for replay_economy re-wire through get_cache_router()."""
import os
import pytest
from unittest.mock import patch, MagicMock

from hermes_cli import replay_economy as re
from agent._cache import get_cache_router, TieredCacheRouter


class TestReplayEconomyRewire:
    """Tests for the re-wire of replay_economy through get_cache_router()."""

    def test_get_request_cache_uses_get_cache_router(self, monkeypatch):
        """_get_request_cache() should use get_cache_router() instead of InProcessLRUCache directly."""
        # Reset state
        re._request_cache = None
        
        # Mock get_cache_router to return a mock router with an L1 tier
        mock_router = MagicMock()
        mock_l1 = MagicMock()
        mock_router._tiers = [mock_l1]
        
        with patch('hermes_cli.replay_economy.get_cache_router', return_value=mock_router):
            cache = re._get_request_cache()
            # Should return the L1 tier from the router
            assert cache is mock_l1

    def test_get_request_cache_returns_none_when_disabled(self, monkeypatch):
        """When REPLAY_CACHE_L1_ENABLED=false, _get_request_cache() returns None."""
        re._request_cache = None
        
        with patch.dict(os.environ, {"HERMES_REPLAY_CACHE_L1_ENABLED": "false"}, clear=False):
            re._request_cache = None
            cache = re._get_request_cache()
            assert cache is None

    def test_get_request_cache_uses_l1_from_router(self):
        """The cache returned by _get_request_cache() should be the router's L1 tier."""
        re._request_cache = None
        import agent._cache as cache_mod
        cache_mod._router_singleton = None
        
        router = re.get_cache_router()
        cache = re._get_request_cache()
        
        # The cache should be the L1 tier from the router (first tier)
        assert cache is router._tiers[0]

    def test_cache_check_uses_router_cache(self, monkeypatch):
        """cache_check() should work through the router's L1 cache."""
        re._request_cache = None
        re.reset_replay_counters()
        
        # Use the real router
        import agent._cache as cache_mod
        cache_mod._router_singleton = None
        
        tool_result = {"role": "tool", "tool_call_id": "tc_1", "content": "hello"}
        re.cache_store("read_file", {"path": "/tmp/x"}, tool_result, "session_1")
        result = re.cache_check("read_file", {"path": "/tmp/x"}, "session_1")
        
        assert result is not None
        assert result["content"] == "hello"

    def test_cache_store_uses_router_cache(self):
        """cache_store() should work through the router's L1 cache."""
        import agent._cache as cache_mod
        cache_mod._router_singleton = None
        re._request_cache = None
        re.reset_replay_counters()
        
        tool_result = {"role": "tool", "tool_call_id": "tc_1", "content": "hello"}
        re.cache_store("read_file", {"path": "/tmp/x"}, tool_result, "session_1")
        
        # Verify it was stored by checking cache
        router = re.get_cache_router()
        l1 = router._tiers[0]
        key = re._make_cache_key("read_file", {"path": "/tmp/x"})
        cached = l1.get(key)
        assert cached is not None
        assert cached["content"] == "hello"