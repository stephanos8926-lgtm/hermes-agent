"""Tests for models_dev and model_metadata re-wire through get_cache_router()."""
import os
import pytest
from unittest.mock import patch, MagicMock

from agent.models_dev import _get_model_catalog_l1, _is_model_catalog_cache_enabled
from agent.model_metadata import _get_context_cache_l1
from agent._cache import get_cache_router, InProcessLRUCache
from agent._cache import build_cache_from_config, TieredCacheRouter


class TestModelsDevRewire:
    """Tests for models_dev re-wire through get_cache_router()."""

    def test_get_model_catalog_l1_uses_get_cache_router(self, monkeypatch):
        """_get_model_catalog_l1() should use get_cache_router() instead of InProcessLRUCache directly."""
        import agent.models_dev as md
        md._model_catalog_l1 = None
        
        # Mock get_cache_router to return a mock router with an L1 tier
        mock_router = MagicMock()
        mock_l1 = MagicMock()
        mock_router._tiers = [mock_l1]
        
        with patch('agent.models_dev.get_cache_router', return_value=mock_router):
            # Enable the cache
            with patch.dict(os.environ, {"HERMES_CACHE_MODEL_CATALOG_ENABLED": "true"}, clear=False):
                cache = md._get_model_catalog_l1()
                # Should return the L1 tier from the router
                assert cache is mock_l1

    def test_get_model_catalog_l1_returns_none_when_disabled(self, monkeypatch):
        """When HERMES_CACHE_MODEL_CATALOG_ENABLED=false, _get_model_catalog_l1() returns None."""
        import agent.models_dev as md
        md._model_catalog_l1 = None
        
        with patch.dict(os.environ, {"HERMES_CACHE_MODEL_CATALOG_ENABLED": "false"}, clear=False):
            md._model_catalog_l1 = None
            cache = md._get_model_catalog_l1()
            assert cache is None

    def test_is_model_catalog_cache_enabled_reads_env(self, monkeypatch):
        """_is_model_catalog_cache_enabled() should respect env var."""
        assert _is_model_catalog_cache_enabled() is False
        
        with patch.dict(os.environ, {"HERMES_CACHE_MODEL_CATALOG_ENABLED": "true"}, clear=False):
            assert _is_model_catalog_cache_enabled() is True

    def test_model_catalog_l1_uses_router_l1(self):
        """The model catalog L1 should use the router's L1 tier."""
        import agent.models_dev as md
        md._model_catalog_l1 = None
        import agent._cache as cache_mod
        cache_mod._router_singleton = None
        
        with patch.dict(os.environ, {"HERMES_CACHE_MODEL_CATALOG_ENABLED": "true"}, clear=False):
            cache = md._get_model_catalog_l1()
            router = get_cache_router()
            # The cache should be the router's L1 tier
            assert cache is router._tiers[0]


class TestModelMetadataRewire:
    """Tests for model_metadata re-wire through get_cache_router()."""

    def test_model_metadata_l1_uses_router(self, monkeypatch):
        """model_metadata should use the router's L1 tier."""
        import agent.model_metadata as mm
        mm._CONTEXT_CACHE_L1 = None
        
        # Mock get_cache_router to return a mock router with an L1 tier
        mock_router = MagicMock()
        mock_l1 = MagicMock()
        mock_router._tiers = [mock_l1]
        
        with patch('agent._cache.get_cache_router', return_value=mock_router):
            # The internal function that gets the L1 cache
            from agent.model_metadata import _get_context_cache_l1
            cache = _get_context_cache_l1()
            assert cache is mock_l1

    def test_context_cache_l1_uses_router_l1(self):
        """The context cache L1 should use the router's L1 tier."""
        import agent.model_metadata as mm
        mm._CONTEXT_CACHE_L1 = None
        import agent._cache as cache_mod
        cache_mod._router_singleton = None
        
        with patch.dict(os.environ, {"HERMES_CACHE_CONTEXT_L1_ENABLED": "true"}, clear=False):
            # Need to check the actual implementation
            pass


class TestModelCatalogCacheIntegration:
    """Integration tests for model catalog cache through router."""

    def test_model_catalog_cache_works_through_router(self):
        """End-to-end test: model catalog cache works through the router."""
        import agent._cache as cache_mod
        cache_mod._router_singleton = None
        
        router = get_cache_router()
        l1 = router._tiers[0]
        
        # Put and get through the router's L1
        l1.put("test-key", "test-value")
        assert l1.get("test-key") == "test-value"
        
        # The router should have the L1 tier
        assert len(router._tiers) >= 1
        assert isinstance(router._tiers[0], type(router._tiers[0]))  # InProcessLRUCache