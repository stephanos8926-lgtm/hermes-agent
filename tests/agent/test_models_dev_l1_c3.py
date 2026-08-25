"""Round 2 / Phase C3: parsed models_dev catalog -> L1 in-process LRU.

The hot path get_model_info() in agent/models_dev.py does a per-call
override lookup + merge + parse to produce a ModelInfo. The data
dict is already cached in memory; the expensive part is the
per-call work to produce a typed ModelInfo.

C3 adds an L1 in-process LRU that mirrors the parsed ModelInfo
objects, keyed on (mdev_id, model_id, override_hash, config_version).
Override changes correctly change the key (no stale L1 hits).
config_version is bumped on every config save (the existing
invalidation model).
"""

import importlib
import sys
import types

import pytest


# ---------------------------------------------------------------------------
# Module loading: agent.models_dev imports `requests` at module top. The
# venv may or may not have it installed. We stub it before import so
# tests can run in either environment.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module", autouse=True)
def _stub_requests():
    if "requests" not in sys.modules:
        stub = types.ModuleType("requests")
        stub.get = lambda *a, **kw: None
        stub_exc = types.ModuleType("requests.exceptions")
        stub_exc.RequestException = Exception
        stub.exceptions = stub_exc
        sys.modules["requests"] = stub
        sys.modules["requests.exceptions"] = stub_exc
    yield


@pytest.fixture
def models_dev():
    """Fresh import of agent.models_dev for each test so module-level
    state (_model_catalog_l1, etc.) is reset."""
    mod = importlib.import_module("agent.models_dev")
    importlib.reload(mod)
    return mod


@pytest.fixture
def reset_l1(monkeypatch, models_dev):
    """Clear the L1 between tests. The internal _model_catalog_l1
    global is reset on every fixture setup."""
    monkeypatch.setattr(models_dev, "_model_catalog_l1", None)
    yield


# ---------------------------------------------------------------------------
# Master switch
# ---------------------------------------------------------------------------
class TestC3MasterSwitch:
    """_is_model_catalog_cache_enabled() is the master kill switch.
    Default is False (off by default) so existing behavior is preserved.
    When False, get_model_info() must NOT consult or populate the L1.
    """

    def test_default_disabled(self, models_dev, reset_l1):
        """No config + no env var => disabled."""
        assert models_dev._is_model_catalog_cache_enabled() is False

    def test_explicit_env_enables(self, models_dev, reset_l1, monkeypatch):
        """HERMES_CACHE_MODEL_CATALOG_ENABLED=true enables the cache."""
        monkeypatch.setenv("HERMES_CACHE_MODEL_CATALOG_ENABLED", "true")
        assert models_dev._is_model_catalog_cache_enabled() is True

    def test_explicit_env_one_enables(self, models_dev, reset_l1, monkeypatch):
        """HERMES_CACHE_MODEL_CATALOG_ENABLED=1 also enables (per the
        ('1','true','yes','on') truthy set in the implementation)."""
        monkeypatch.setenv("HERMES_CACHE_MODEL_CATALOG_ENABLED", "1")
        assert models_dev._is_model_catalog_cache_enabled() is True

    def test_explicit_env_disables(self, models_dev, reset_l1, monkeypatch):
        """HERMES_CACHE_MODEL_CATALOG_ENABLED=false disables."""
        monkeypatch.setenv("HERMES_CACHE_MODEL_CATALOG_ENABLED", "false")
        assert models_dev._is_model_catalog_cache_enabled() is False

    def test_garbage_value_falls_back_to_config(
        self, models_dev, reset_l1, monkeypatch
    ):
        """Unparseable env values fall through to config lookup. With
        no config either, the default (off) is returned."""
        monkeypatch.setenv("HERMES_CACHE_MODEL_CATALOG_ENABLED", "maybe")
        assert models_dev._is_model_catalog_cache_enabled() is False

    def test_disabled_l1_returns_none(
        self, models_dev, reset_l1, monkeypatch
    ):
        """When disabled, _get_model_catalog_l1() returns None so every
        call site is a no-op (the L1 is never created)."""
        assert models_dev._is_model_catalog_cache_enabled() is False
        assert models_dev._get_model_catalog_l1() is None
        # The global should still be None - no L1 was instantiated.
        assert models_dev._model_catalog_l1 is None


# ---------------------------------------------------------------------------
# L1 key + override hash
# ---------------------------------------------------------------------------
class TestC3KeyAndHash:
    """The L1 key + override hash combine to ensure correctness:
    same input -> same key (cacheable), different override -> different key.
    """

    def test_l1_key_includes_all_components(self, models_dev, reset_l1):
        """The L1 key must include mdev_id, model_id, override_hash,
        and config_version so any change in any one produces a different
        key (no stale hits). The format is 'mc:{mdev_id}:{model_id}:
        {override_hash}:{cv}' with the 'mc:' namespace prefix."""
        k1 = models_dev._l1_key("nvidia", "nemotron", "ohash_a", 7)
        assert k1 == "mc:nvidia:nemotron:ohash_a:7"
        assert isinstance(k1, str)

    def test_l1_key_distinguishes_config_versions(self, models_dev, reset_l1):
        """Different config_version -> different key."""
        a = models_dev._l1_key("nvidia", "nemotron", "x", 1)
        b = models_dev._l1_key("nvidia", "nemotron", "x", 2)
        assert a != b

    def test_l1_key_distinguishes_overrides(self, models_dev, reset_l1):
        """Different override_hash -> different key (override changes
        correctly invalidate the cache)."""
        a = models_dev._l1_key("nvidia", "nemotron", "x", 1)
        b = models_dev._l1_key("nvidia", "nemotron", "y", 1)
        assert a != b

    def test_l1_key_namespace_prefix(self, models_dev, reset_l1):
        """The 'mc:' prefix namespaces the L1 key so it never collides
        with other L1 users (e.g. C2's context-length cache) in the
        same process. This is the safety net against L1 cross-talk."""
        k = models_dev._l1_key("a", "b", "c", 1)
        assert k.startswith("mc:")

    def test_override_hash_stable(self, models_dev, reset_l1):
        """The same override dict produces the same hash every time."""
        ov = {"context_length": 128000, "pricing": {"input": 1.0}}
        h1 = models_dev._override_hash(ov)
        h2 = models_dev._override_hash(ov)
        assert h1 == h2
        assert isinstance(h1, str)

    def test_override_hash_none_is_sentinel(self, models_dev, reset_l1):
        """None override -> 'none' sentinel. The sentinel is a string
        (not the empty string) so the key is never ambiguous."""
        assert models_dev._override_hash(None) == "none"

    def test_override_hash_distinguishes_dicts(self, models_dev, reset_l1):
        """Different override dicts produce different hashes."""
        a = models_dev._override_hash({"context_length": 1000})
        b = models_dev._override_hash({"context_length": 2000})
        assert a != b

    def test_override_hash_collision_resistant(self, models_dev, reset_l1):
        """Order of keys does not affect hash (dict is normalized via
        sort_keys=True in json.dumps)."""
        a = models_dev._override_hash({"a": 1, "b": 2})
        b = models_dev._override_hash({"b": 2, "a": 1})
        assert a == b


# ---------------------------------------------------------------------------
# L1 get/put/invalidate behavior
# ---------------------------------------------------------------------------
class TestC3LRUBehavior:
    """The L1 cache itself (InProcessLRUCache) plus the get/put wrappers."""

    def test_l1_created_when_enabled(self, models_dev, reset_l1, monkeypatch):
        """When enabled, _get_model_catalog_l1 returns a real cache."""
        monkeypatch.setenv("HERMES_CACHE_MODEL_CATALOG_ENABLED", "true")
        l1 = models_dev._get_model_catalog_l1()
        assert l1 is not None

    def test_l1_is_singleton(self, models_dev, reset_l1, monkeypatch):
        """A second call returns the same L1 instance (lazy singleton)."""
        monkeypatch.setenv("HERMES_CACHE_MODEL_CATALOG_ENABLED", "true")
        a = models_dev._get_model_catalog_l1()
        b = models_dev._get_model_catalog_l1()
        assert a is b

    def test_l1_put_and_get_round_trip(self, models_dev, reset_l1, monkeypatch):
        """A put + get returns the same object."""
        monkeypatch.setenv("HERMES_CACHE_MODEL_CATALOG_ENABLED", "true")
        l1 = models_dev._get_model_catalog_l1()
        sentinel = object()
        l1.put("mc:nvidia:nemotron:none:1", sentinel)
        assert l1.get("mc:nvidia:nemotron:none:1") is sentinel

    def test_l1_invalidate(self, models_dev, reset_l1, monkeypatch):
        """After invalidate, the key is gone."""
        monkeypatch.setenv("HERMES_CACHE_MODEL_CATALOG_ENABLED", "true")
        l1 = models_dev._get_model_catalog_l1()
        l1.put("mc:nvidia:nemotron:none:1", "v")
        l1.invalidate("mc:nvidia:nemotron:none:1")
        assert l1.get("mc:nvidia:nemotron:none:1") is None

    def test_l1_size_floors(self, models_dev, reset_l1, monkeypatch):
        """l1_max_entries<16 and l1_max_bytes<1MiB are clamped to floors.
        The L1 must never be initialized with zero capacity (that would
        be a foot-gun: a 0-cap LRU silently drops everything)."""
        monkeypatch.setenv("HERMES_CACHE_MODEL_CATALOG_ENABLED", "true")
        monkeypatch.setenv("HERMES_CACHE_MODEL_CATALOG_L1_MAX_ENTRIES", "0")
        monkeypatch.setenv("HERMES_CACHE_MODEL_CATALOG_L1_MAX_BYTES", "100")
        l1 = models_dev._get_model_catalog_l1()
        # Floors: entries >= 16, bytes >= 1 MiB
        assert l1._max_entries >= 16
        assert l1._max_bytes >= 1024 * 1024

    def test_l1_init_failure_returns_none(
        self, models_dev, reset_l1, monkeypatch
    ):
        """If InProcessLRUCache itself raises during init (e.g. invalid
        args), the function returns None and the L1 is marked None.
        The hot path must never crash on L1 init failure."""
        monkeypatch.setenv("HERMES_CACHE_MODEL_CATALOG_ENABLED", "true")

        def boom(*a, **kw):
            raise RuntimeError("L1 init failed")

        monkeypatch.setattr(models_dev, "InProcessLRUCache", boom)
        # Reset the global so init runs again
        models_dev._model_catalog_l1 = None
        result = models_dev._get_model_catalog_l1()
        assert result is None
        # The global is reset to None so a future call could try again
        assert models_dev._model_catalog_l1 is None


# ---------------------------------------------------------------------------
# End-to-end: get_model_info consults L1 when enabled
# ---------------------------------------------------------------------------
class TestC3GetModelInfoIntegration:
    """The end-to-end story: with the cache enabled, a second call
    for the same model returns the cached ModelInfo without re-parsing.
    Disabled cache falls through to the existing path every time.
    """

    def test_enabled_returns_cached_model_info(
        self, models_dev, reset_l1, monkeypatch
    ):
        """A second call for the same (provider, model) returns the
        same object from the L1 (no re-parse)."""
        monkeypatch.setenv("HERMES_CACHE_MODEL_CATALOG_ENABLED", "true")
        l1 = models_dev._get_model_catalog_l1()
        sentinel = object()
        k = models_dev._l1_key(
            "nvidia",
            "nemotron-3-ultra-550b-a55b",
            "none",
            models_dev._config_version(),
        )
        l1.put(k, sentinel)
        result = models_dev.get_model_info(
            "nvidia", "nemotron-3-ultra-550b-a55b", allow_network=False
        )
        assert result is sentinel  # L1 hit, no re-parse

    def test_l1_hit_does_not_invoke_parse(
        self, models_dev, reset_l1, monkeypatch
    ):
        """Verify the L1 hit short-circuits the expensive merge + parse
        path. _override_for is allowed to be called (the cache key
        needs the override hash), but _parse_model_info and
        _merge_catalog_entry_with_override must not be — those are
        the operations the L1 is there to skip."""
        monkeypatch.setenv("HERMES_CACHE_MODEL_CATALOG_ENABLED", "true")
        l1 = models_dev._get_model_catalog_l1()
        sentinel = object()
        k = models_dev._l1_key("nvidia", "nemotron", "none", models_dev._config_version())
        l1.put(k, sentinel)

        def boom_parse(*a, **kw):
            raise AssertionError("L1 hit should not call _parse_model_info")
        def boom_merge(*a, **kw):
            raise AssertionError("L1 hit should not call _merge_catalog_entry_with_override")

        monkeypatch.setattr(models_dev, "_parse_model_info", boom_parse)
        monkeypatch.setattr(models_dev, "_merge_catalog_entry_with_override", boom_merge)

        result = models_dev.get_model_info("nvidia", "nemotron", allow_network=False)
        assert result is sentinel

    def test_disabled_l1_never_populated(
        self, models_dev, reset_l1, monkeypatch
    ):
        """When the L1 is disabled, get_model_info must NOT attempt to
        populate it. We verify this by confirming _get_model_catalog_l1
        returns None (so the L1 put path is never reached)."""
        # Cache is disabled (no env var)
        assert models_dev._is_model_catalog_cache_enabled() is False
        assert models_dev._get_model_catalog_l1() is None


# ---------------------------------------------------------------------------
# config_version integration
# ---------------------------------------------------------------------------
class TestC3ConfigVersion:
    """The config_version is part of the L1 key. Any change to it
    must produce a different key (no stale L1 hits across config saves).
    """

    def test_config_version_returns_int(self, models_dev, reset_l1):
        """_config_version() returns an int. It might be 0 in the
        test environment (no HERMES_HOME), but it must be an int
        and must not raise."""
        v = models_dev._config_version()
        assert isinstance(v, int)
        assert v >= 0

    def test_config_version_changes_produce_different_keys(
        self, models_dev, reset_l1, monkeypatch
    ):
        """When the operator saves config.yaml, _config_version is
        bumped. _l1_key must reflect that change so a save never
        serves a stale L1 entry."""
        v1 = models_dev._config_version()
        new_version = v1 + 1
        monkeypatch.setattr(models_dev, "_config_version", lambda: new_version)
        k1 = models_dev._l1_key("nvidia", "x", "y", v1)
        k2 = models_dev._l1_key("nvidia", "x", "y", new_version)
        assert k1 != k2

    def test_config_version_loader_handles_import_error(
        self, models_dev, reset_l1, monkeypatch
    ):
        """When the _config_version reader raises (env not initialized,
        hermes_cli.config not importable, etc.), _config_version returns
        a stable fallback (0) rather than raising. The L1 must never
        break the hot path.

        We simulate the failure by patching the symbol that
        _config_version() looks up in hermes_cli.config — not by
        patching _config_version itself, which would make the test
        tautological.
        """
        import hermes_cli.config as hcc
        monkeypatch.setattr(hcc, "_config_version", None, raising=False)
        # The above deletes the symbol; _config_version() should
        # then catch the ImportError-style exception and return 0.
        v = models_dev._config_version()
        assert isinstance(v, int)
        assert v >= 0
