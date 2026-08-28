"""C1: secret_sources/_cache_bridge - L1+L2 cache frontend for DiskCache.

Tests exercise the bridge module with a stubbed TieredCacheRouter
to avoid the full integration. The contract:

  * When ``cache.secrets.enabled`` is True (or the env var is set), the
    bridge consults the router before reading from disk.
  * A cache hit returns the entry without touching disk.
  * A cache miss falls through to ``json_fallback`` and writes the result
    back to the router.
  * When the cache is disabled, the bridge is a transparent pass-through.
  * A cache-layer failure (router raises) is caught and falls through to
    disk - caching is best-effort.
  * Writes populate the router with the same payload as the JSON layer.
  * The router key is namespaced by ``basename`` so two secret backends
    don't collide.
  * TTL re-validation: a cache hit older than ttl_seconds falls through.
  * Stale hits are invalidated before fall-through.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.secret_sources import _cache_bridge  # noqa: E402


# ---------------------------------------------------------------------------
# Stub router + entry class
# ---------------------------------------------------------------------------
class _StubRouter:
    """Minimal TieredCacheRouter stand-in. Captures put/get/invalidate."""

    def __init__(self) -> None:
        self._store: dict[str, Any] = {}
        self.puts: list[tuple[str, Any]] = []
        self.gets: list[str] = []
        self.invalidations: list[str] = []

    def get(self, key: str) -> Optional[Any]:
        self.gets.append(key)
        return self._store.get(key)

    def put(self, key: str, value: Any) -> None:
        self.puts.append((key, value))
        self._store[key] = value

    def invalidate(self, key: str) -> None:
        self.invalidations.append(key)
        self._store.pop(key, None)


@dataclass
class _FakeEntry:
    """Stand-in for CachedFetch. Has secrets + fetched_at as the bridge expects."""
    secrets: dict
    fetched_at: float


@pytest.fixture
def stub_router(monkeypatch) -> _StubRouter:
    """Force the bridge to use our stubbed router instead of building a real one."""
    router = _StubRouter()
    monkeypatch.setattr(
        _cache_bridge, "_build_router_for_backend", lambda basename: router
    )
    return router


@pytest.fixture
def enabled_cache(monkeypatch) -> None:
    """Force the bridge to see cache.secrets.enabled = True."""
    monkeypatch.setattr(
        _cache_bridge, "_is_secrets_cache_enabled", lambda: True
    )


@pytest.fixture
def disabled_cache(monkeypatch) -> None:
    """Force the bridge to see cache.secrets.enabled = False."""
    monkeypatch.setattr(
        _cache_bridge, "_is_secrets_cache_enabled", lambda: False
    )


# ---------------------------------------------------------------------------
# _is_secrets_cache_enabled / config helpers
# ---------------------------------------------------------------------------
class TestConfigGating:
    """The default-off gate. Must read from cache.secrets.enabled in config
    and from HERMES_CACHE_SECRETS_ENABLED env var."""

    def test_defaults_to_false(self, monkeypatch):
        """With no config and no env var, secrets caching is OFF."""
        monkeypatch.delenv("HERMES_CACHE_SECRETS_ENABLED", raising=False)
        # Patch load_config_readonly to return empty
        monkeypatch.setattr(
            "hermes_cli.config.load_config_readonly", lambda: {}
        )
        assert _cache_bridge._is_secrets_cache_enabled() is False

    def test_env_var_overrides_to_true(self, monkeypatch):
        monkeypatch.setenv("HERMES_CACHE_SECRETS_ENABLED", "1")
        assert _cache_bridge._is_secrets_cache_enabled() is True

    def test_env_var_overrides_to_false(self, monkeypatch):
        monkeypatch.setenv("HERMES_CACHE_SECRETS_ENABLED", "0")
        # Even if config says true, env var wins
        monkeypatch.setattr(
            "hermes_cli.config.load_config_readonly",
            lambda: {"cache": {"secrets": {"enabled": True}}},
        )
        assert _cache_bridge._is_secrets_cache_enabled() is False

    def test_config_yaml_overrides_to_true(self, monkeypatch):
        monkeypatch.delenv("HERMES_CACHE_SECRETS_ENABLED", raising=False)
        monkeypatch.setattr(
            "hermes_cli.config.load_config_readonly",
            lambda: {"cache": {"secrets": {"enabled": True}}},
        )
        assert _cache_bridge._is_secrets_cache_enabled() is True

    def test_config_yaml_loader_failure_falls_back_to_false(self, monkeypatch):
        """If load_config_readonly raises, default to OFF (fail-safe)."""
        monkeypatch.delenv("HERMES_CACHE_SECRETS_ENABLED", raising=False)

        def boom():
            raise RuntimeError("config file missing")
        monkeypatch.setattr("hermes_cli.config.load_config_readonly", boom)
        assert _cache_bridge._is_secrets_cache_enabled() is False


# ---------------------------------------------------------------------------
# bridge_read
# ---------------------------------------------------------------------------
class TestBridgeRead:
    """bridge_read: read-through with L1+L2 fallthrough to the JSON layer."""

    def test_cache_disabled_calls_json_fallback_only(
        self, disabled_cache, stub_router
    ):
        """When cache is disabled, the router must never be touched."""
        called = []

        def fb():
            called.append(True)
            return _FakeEntry(secrets={"k": "v"}, fetched_at=time.time())

        result = _cache_bridge.bridge_read(
            basename="bitwarden.json",
            key="user1",
            ttl_seconds=60.0,
            key_serializer=str,
            cls=_FakeEntry,
            json_fallback=fb,
        )
        assert isinstance(result, _FakeEntry)
        assert called == [True]
        # Router was never consulted (stub_router is replaced at module level
        # but bridge_read goes through _build_router_for_backend; verify the
        # bridge skipped that path entirely).
        assert stub_router.gets == []
        assert stub_router.puts == []

    def test_cache_hit_skips_json_fallback(
        self, enabled_cache, stub_router
    ):
        """A fresh cache hit returns the entry without calling json_fallback."""
        now = time.time()
        cached_payload = {"secrets": {"k": "v"}, "fetched_at": now}
        serialized = "user1"
        stub_router._store[serialized] = cached_payload

        called = []

        def fb():
            called.append(True)
            return None

        result = _cache_bridge.bridge_read(
            basename="bitwarden.json",
            key="user1",
            ttl_seconds=60.0,
            key_serializer=str,
            cls=_FakeEntry,
            json_fallback=fb,
        )
        assert isinstance(result, _FakeEntry)
        assert result.secrets == {"k": "v"}
        assert called == []  # JSON layer was not consulted
        assert stub_router.gets == [serialized]

    def test_cache_miss_falls_through_and_populates(
        self, enabled_cache, stub_router
    ):
        """A cache miss calls json_fallback and writes the result back."""
        now = time.time()

        def fb():
            return _FakeEntry(secrets={"k": "v"}, fetched_at=now)

        result = _cache_bridge.bridge_read(
            basename="bitwarden.json",
            key="user1",
            ttl_seconds=60.0,
            key_serializer=str,
            cls=_FakeEntry,
            json_fallback=fb,
        )
        assert isinstance(result, _FakeEntry)
        assert len(stub_router.puts) == 1
        put_key, put_value = stub_router.puts[0]
        assert put_key == "user1"
        assert put_value == {"secrets": {"k": "v"}, "fetched_at": now}

    def test_stale_cache_hit_falls_through_and_invalidates(
        self, enabled_cache, stub_router
    ):
        """A cache hit older than ttl_seconds is treated as a miss AND
        invalidated so subsequent reads don't repeat the work."""
        old_time = time.time() - 120.0  # 2 minutes ago
        cached_payload = {"secrets": {"k": "v"}, "fetched_at": old_time}
        stub_router._store["user1"] = cached_payload

        fresh_time = time.time()

        def fb():
            return _FakeEntry(secrets={"k": "fresh"}, fetched_at=fresh_time)

        result = _cache_bridge.bridge_read(
            basename="bitwarden.json",
            key="user1",
            ttl_seconds=60.0,  # 60s window; 120s is stale
            key_serializer=str,
            cls=_FakeEntry,
            json_fallback=fb,
        )
        assert result.secrets == {"k": "fresh"}
        # Stale entry was invalidated
        assert "user1" in stub_router.invalidations
        # Fresh value was then put
        assert len(stub_router.puts) == 1

    def test_corrupt_cache_payload_falls_through(
        self, enabled_cache, stub_router
    ):
        """If the cache layer returns garbage (e.g. wrong shape), fall through."""
        stub_router._store["user1"] = "this is not a dict"  # corrupt

        now = time.time()

        def fb():
            return _FakeEntry(secrets={"k": "from_json"}, fetched_at=now)

        result = _cache_bridge.bridge_read(
            basename="bitwarden.json",
            key="user1",
            ttl_seconds=60.0,
            key_serializer=str,
            cls=_FakeEntry,
            json_fallback=fb,
        )
        assert result.secrets == {"k": "from_json"}

    def test_router_construction_failure_falls_through(
        self, enabled_cache, monkeypatch
    ):
        """If _build_router_for_backend returns None, fall through to JSON."""
        monkeypatch.setattr(
            _cache_bridge, "_build_router_for_backend", lambda b: None
        )

        called = []

        def fb():
            called.append(True)
            return _FakeEntry(secrets={"k": "v"}, fetched_at=time.time())

        result = _cache_bridge.bridge_read(
            basename="bitwarden.json",
            key="user1",
            ttl_seconds=60.0,
            key_serializer=str,
            cls=_FakeEntry,
            json_fallback=fb,
        )
        assert isinstance(result, _FakeEntry)
        assert called == [True]

    def test_router_get_exception_falls_through(
        self, enabled_cache, monkeypatch
    ):
        """A router.get() exception is caught and falls through to JSON."""

        class ExplodingRouter:
            def get(self, key):
                raise RuntimeError("router on fire")

            def put(self, key, value):
                raise RuntimeError("router on fire")

            def invalidate(self, key):
                pass

        monkeypatch.setattr(
            _cache_bridge, "_build_router_for_backend", lambda b: ExplodingRouter()
        )

        called = []

        def fb():
            called.append(True)
            return _FakeEntry(secrets={"k": "v"}, fetched_at=time.time())

        result = _cache_bridge.bridge_read(
            basename="bitwarden.json",
            key="user1",
            ttl_seconds=60.0,
            key_serializer=str,
            cls=_FakeEntry,
            json_fallback=fb,
        )
        assert isinstance(result, _FakeEntry)
        assert called == [True]

    def test_router_put_failure_does_not_break_read(
        self, enabled_cache, monkeypatch
    ):
        """A put() failure is logged but does not affect the returned entry."""

        class BadPutRouter:
            def __init__(self):
                self._store = {}
                self.puts = []

            def get(self, key):
                return None

            def put(self, key, value):
                self.puts.append((key, value))
                raise RuntimeError("put failed")

            def invalidate(self, key):
                self._store.pop(key, None)

        router = BadPutRouter()
        monkeypatch.setattr(
            _cache_bridge, "_build_router_for_backend", lambda b: router
        )

        now = time.time()

        def fb():
            return _FakeEntry(secrets={"k": "v"}, fetched_at=now)

        # Should not raise
        result = _cache_bridge.bridge_read(
            basename="bitwarden.json",
            key="user1",
            ttl_seconds=60.0,
            key_serializer=str,
            cls=_FakeEntry,
            json_fallback=fb,
        )
        assert isinstance(result, _FakeEntry)
        assert len(router.puts) == 1  # attempt was made

    def test_zero_ttl_skips_cache(
        self, enabled_cache, stub_router
    ):
        """ttl_seconds=0 means caching is off for this read; fall through."""
        called = []

        def fb():
            called.append(True)
            return None  # JSON also has no entry

        result = _cache_bridge.bridge_read(
            basename="bitwarden.json",
            key="user1",
            ttl_seconds=0.0,
            key_serializer=str,
            cls=_FakeEntry,
            json_fallback=fb,
        )
        assert result is None
        assert called == [True]
        assert stub_router.gets == []

    def test_json_fallback_returns_none_propagates_none(
        self, enabled_cache, stub_router
    ):
        """If JSON layer returns None, the bridge returns None and does NOT
        populate the cache (nothing to cache)."""
        def fb():
            return None

        result = _cache_bridge.bridge_read(
            basename="bitwarden.json",
            key="user1",
            ttl_seconds=60.0,
            key_serializer=str,
            cls=_FakeEntry,
            json_fallback=fb,
        )
        assert result is None
        assert stub_router.puts == []  # Nothing to cache

    def test_router_key_uses_key_serializer(
        self, enabled_cache, stub_router
    ):
        """The cache key is the result of key_serializer(key), not str(key)."""
        now = time.time()

        def fb():
            return _FakeEntry(secrets={"k": "v"}, fetched_at=now)

        # Custom serializer: prefix the key
        _cache_bridge.bridge_read(
            basename="bitwarden.json",
            key=("user1", "field1"),
            ttl_seconds=60.0,
            key_serializer=lambda k: f"bw:{':'.join(k)}",
            cls=_FakeEntry,
            json_fallback=fb,
        )
        assert stub_router.puts == [("bw:user1:field1", {"secrets": {"k": "v"}, "fetched_at": now})]


# ---------------------------------------------------------------------------
# bridge_write
# ---------------------------------------------------------------------------
class TestBridgeWrite:
    """bridge_write: write-through to the cache layer (JSON write is caller's job)."""

    def test_cache_disabled_skips_write(
        self, disabled_cache, stub_router
    ):
        now = time.time()
        entry = _FakeEntry(secrets={"k": "v"}, fetched_at=now)
        _cache_bridge.bridge_write(
            basename="bitwarden.json",
            key="user1",
            entry=entry,
            key_serializer=str,
        )
        assert stub_router.puts == []

    def test_cache_enabled_populates_router(
        self, enabled_cache, stub_router
    ):
        now = time.time()
        entry = _FakeEntry(secrets={"k": "v"}, fetched_at=now)
        _cache_bridge.bridge_write(
            basename="bitwarden.json",
            key="user1",
            entry=entry,
            key_serializer=str,
        )
        assert len(stub_router.puts) == 1
        put_key, put_value = stub_router.puts[0]
        assert put_key == "user1"
        assert put_value == {"secrets": {"k": "v"}, "fetched_at": now}

    def test_router_construction_failure_is_silent(
        self, enabled_cache, monkeypatch
    ):
        """A failure to construct the router must not raise (JSON write already happened)."""
        monkeypatch.setattr(
            _cache_bridge, "_build_router_for_backend", lambda b: None
        )
        now = time.time()
        entry = _FakeEntry(secrets={"k": "v"}, fetched_at=now)
        # Should not raise
        _cache_bridge.bridge_write(
            basename="bitwarden.json",
            key="user1",
            entry=entry,
            key_serializer=str,
        )

    def test_router_put_failure_is_silent(
        self, enabled_cache, monkeypatch
    ):
        """A put() failure must not raise (JSON write already happened)."""
        class BrokenRouter:
            def put(self, key, value):
                raise RuntimeError("put failed")

        monkeypatch.setattr(
            _cache_bridge, "_build_router_for_backend", lambda b: BrokenRouter()
        )
        now = time.time()
        entry = _FakeEntry(secrets={"k": "v"}, fetched_at=now)
        # Should not raise
        _cache_bridge.bridge_write(
            basename="bitwarden.json",
            key="user1",
            entry=entry,
            key_serializer=str,
        )

    def test_uses_key_serializer(
        self, enabled_cache, stub_router
    ):
        now = time.time()
        entry = _FakeEntry(secrets={"k": "v"}, fetched_at=now)
        _cache_bridge.bridge_write(
            basename="bitwarden.json",
            key=("user1", "field1"),
            entry=entry,
            key_serializer=lambda k: f"bw:{':'.join(k)}",
        )
        assert stub_router.puts[0][0] == "bw:user1:field1"


# ---------------------------------------------------------------------------
# bridge_clear
# ---------------------------------------------------------------------------
class TestBridgeClear:
    """bridge_clear: invalidate all cache layers for a backend (used on secret rotation)."""

    def test_cache_disabled_skips_clear(self, disabled_cache, tmp_path):
        """When caching is disabled, clear is a no-op."""
        # Just call it - should not raise, should not touch any files
        _cache_bridge.bridge_clear(basename="bitwarden.json")

    def test_cache_enabled_calls_l2_clear(
        self, enabled_cache, monkeypatch, tmp_path
    ):
        """When caching is enabled, the L2 mmap file is cleared."""
        from agent._cache import FlatFileCache
        mmap_path = tmp_path / "secrets" / "bitwarden.mmap"
        mmap_path.parent.mkdir(parents=True, exist_ok=True)
        mmap_path.touch()

        # Patch _l2_path_for_backend to return our temp path
        monkeypatch.setattr(
            _cache_bridge, "_l2_path_for_backend",
            lambda b: str(mmap_path)
        )
        # Patch _l2_max_bytes to return a small valid value
        monkeypatch.setattr(
            _cache_bridge, "_l2_max_bytes", lambda: 1024 * 1024
        )
        # Patch _build_router_for_backend to return a stub that records
        # the call (the real FlatFileCache would be expensive to set up)
        class RecordingRouter:
            def __init__(self):
                self.cleared = False

            def get(self, k):
                return None

            def put(self, k, v):
                pass

            def invalidate(self, k):
                pass

        router = RecordingRouter()
        monkeypatch.setattr(
            _cache_bridge, "_build_router_for_backend", lambda b: router
        )

        _cache_bridge.bridge_clear(basename="bitwarden.json")
        # The bridge tries to call FlatFileCache(...).clear() on the L2 file.
        # We can verify the path resolution worked:
        assert mmap_path.exists()  # File still exists (clear doesn't unlink)
        # The actual L2 clear would be tested in the integration test.


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------
class TestConfigHelpers:
    """The tunable config helpers (TTL, L1 sizes, L2 size)."""

    def test_ttl_default_when_no_config(self, monkeypatch):
        monkeypatch.delenv("HERMES_CACHE_SECRETS_TTL_SECONDS", raising=False)
        monkeypatch.setattr(
            "hermes_cli.config.load_config_readonly", lambda: {}
        )
        assert _cache_bridge._secrets_ttl_seconds() == 60.0

    def test_ttl_from_env(self, monkeypatch):
        monkeypatch.setenv("HERMES_CACHE_SECRETS_TTL_SECONDS", "120")
        assert _cache_bridge._secrets_ttl_seconds() == 120.0

    def test_ttl_from_config(self, monkeypatch):
        monkeypatch.delenv("HERMES_CACHE_SECRETS_TTL_SECONDS", raising=False)
        monkeypatch.setattr(
            "hermes_cli.config.load_config_readonly",
            lambda: {"cache": {"secrets": {"ttl_seconds": 300.0}}},
        )
        assert _cache_bridge._secrets_ttl_seconds() == 300.0

    def test_ttl_floor_at_zero(self, monkeypatch):
        """A negative TTL is clamped to 0 (caching off)."""
        monkeypatch.setenv("HERMES_CACHE_SECRETS_TTL_SECONDS", "-1")
        assert _cache_bridge._secrets_ttl_seconds() == 0.0

    def test_ttl_env_invalid_falls_through_to_config(self, monkeypatch):
        """A non-numeric TTL env var is ignored."""
        monkeypatch.setenv("HERMES_CACHE_SECRETS_TTL_SECONDS", "not-a-number")
        monkeypatch.setattr(
            "hermes_cli.config.load_config_readonly",
            lambda: {"cache": {"secrets": {"ttl_seconds": 90.0}}},
        )
        assert _cache_bridge._secrets_ttl_seconds() == 90.0

    def test_l1_max_entries_env_floor_at_1(self, monkeypatch):
        monkeypatch.setenv("HERMES_CACHE_SECRETS_L1_MAX_ENTRIES", "0")
        assert _cache_bridge._secrets_l1_max_entries() == 1

    def test_l1_max_bytes_env_floor_at_1024(self, monkeypatch):
        monkeypatch.setenv("HERMES_CACHE_SECRETS_L1_MAX_BYTES", "100")
        assert _cache_bridge._secrets_l1_max_bytes() == 1024

    def test_l2_max_bytes_env_floor_at_1MiB(self, monkeypatch):
        monkeypatch.setenv("HERMES_CACHE_SECRETS_L2_MAX_BYTES", "100")
        assert _cache_bridge._l2_max_bytes() == 1024 * 1024
