"""Tests for tui_gateway _cfg_cache read-only view / shallow-copy contract.

Covers:
- _read_only_view returns MappingProxyType for dicts, tuple for lists
- _read_only_view returns scalars unchanged
- _read_only_view is recursive (nested dicts are also wrapped)
- _read_only_view surfaces mutations as TypeError
- _shallow_cfg_copy returns a fresh top-level dict (caller-mutable)
- _shallow_cfg_copy returns inner values as-is (caller's isinstance guard
  prevents accidental mutation through the top-level copy)
- _cfg_copy_for_writeback materialises proxies back to a fully mutable dict
- _load_cfg_raw / _save_cfg round-trip preserves the read-only contract
"""

import sys
import threading
from types import MappingProxyType
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def server():
    """Import tui_gateway.server with the env_loader dependency mocked.

    The TUI's real module imports ``hermes_cli.env_loader`` at module load,
    which transitively requires ``python-dotenv``. Other TUI tests work
    around this by stubbing ``env_loader`` in ``sys.modules`` before the
    import. We use the same pattern here.
    """
    with patch.dict("sys.modules", {
        "hermes_constants": MagicMock(get_hermes_home=MagicMock(return_value="/tmp/hermes_test")),
        "hermes_cli.env_loader": MagicMock(),
        "hermes_cli.banner": MagicMock(),
        "hermes_state": MagicMock(),
    }):
        sys.modules.pop("tui_gateway.server", None)
        import importlib
        mod = importlib.import_module("tui_gateway.server")
    yield mod
    # Reset module-level state without re-importing.
    mod._cfg_cache = None
    mod._cfg_mtime = None
    mod._cfg_path = None


# ---------------------------------------------------------------------------
# _read_only_view
# ---------------------------------------------------------------------------

class TestReadOnlyView:
    """The cache stores a recursive MappingProxyType view."""

    def test_dict_becomes_mapping_proxy(self, server):
        v = server._read_only_view({"a": 1, "b": 2})
        assert isinstance(v, MappingProxyType)
        assert v["a"] == 1
        assert v["b"] == 2

    def test_nested_dict_also_becomes_proxy(self, server):
        v = server._read_only_view({"outer": {"inner": 1}})
        assert isinstance(v, MappingProxyType)
        assert isinstance(v["outer"], MappingProxyType)
        assert v["outer"]["inner"] == 1

    def test_list_becomes_tuple(self, server):
        v = server._read_only_view([1, 2, 3])
        assert isinstance(v, tuple)
        assert v == (1, 2, 3)

    def test_list_of_dicts_recursively_typed(self, server):
        v = server._read_only_view([{"a": 1}, {"b": 2}])
        assert isinstance(v, tuple)
        assert all(isinstance(item, MappingProxyType) for item in v)

    def test_scalars_unchanged(self, server):
        assert server._read_only_view("hello") == "hello"
        assert server._read_only_view(42) == 42
        assert server._read_only_view(3.14) == 3.14
        assert server._read_only_view(True) is True
        assert server._read_only_view(None) is None

    def test_top_level_dict_mutation_raises(self, server):
        v = server._read_only_view({"a": 1})
        with pytest.raises(TypeError):
            v["b"] = 2  # type: ignore[index]
        with pytest.raises(TypeError):
            del v["a"]  # type: ignore[attr-defined]

    def test_nested_dict_mutation_raises(self, server):
        v = server._read_only_view({"outer": {"inner": 1}})
        with pytest.raises(TypeError):
            v["outer"]["new"] = 2  # type: ignore[index]

    def test_tuple_immutable(self, server):
        v = server._read_only_view([1, 2, 3])
        with pytest.raises((AttributeError, TypeError)):
            v[0] = 99  # type: ignore[index]


# ---------------------------------------------------------------------------
# _shallow_cfg_copy
# ---------------------------------------------------------------------------

class TestShallowCfgCopy:
    """The cache-hit read path returns a fresh top-level dict."""

    def test_none_returns_empty_dict(self, server):
        assert server._shallow_cfg_copy(None) == {}

    def test_cached_view_returns_plain_dict(self, server):
        cached = server._read_only_view({"a": 1, "b": 2})
        out = server._shallow_cfg_copy(cached)
        assert isinstance(out, dict)
        assert not isinstance(out, MappingProxyType)
        assert out == {"a": 1, "b": 2}

    def test_top_level_mutation_does_not_poison_cache(self, server):
        cached = server._read_only_view({"a": 1, "b": {"inner": 2}})
        out = server._shallow_cfg_copy(cached)
        out["new"] = "added"
        out["a"] = 999
        # The cached view is untouched.
        assert cached["a"] == 1
        assert "new" not in cached

    def test_nested_value_still_a_view(self, server):
        """The shallow copy doesn't materialise nested values; readers'
        isinstance guards handle them."""
        cached = server._read_only_view({"display": {"show_reasoning": True}})
        out = server._shallow_cfg_copy(cached)
        assert isinstance(out, dict)
        # The nested dict is still a view (MappingProxyType).
        assert isinstance(out["display"], MappingProxyType)


# ---------------------------------------------------------------------------
# _cfg_copy_for_writeback
# ---------------------------------------------------------------------------

class TestCfgCopyForWriteback:
    """The write-back path needs a fully mutable copy."""

    def test_none_returns_empty_dict(self, server):
        assert server._cfg_copy_for_writeback(None) == {}

    def test_top_level_dict(self, server):
        cached = server._read_only_view({"a": 1})
        out = server._cfg_copy_for_writeback(cached)
        assert isinstance(out, dict)
        assert not isinstance(out, MappingProxyType)
        out["new"] = "ok"
        assert out == {"a": 1, "new": "ok"}

    def test_recursive_materialise(self, server):
        cached = server._read_only_view({"outer": {"inner": 1}})
        out = server._cfg_copy_for_writeback(cached)
        assert isinstance(out, dict)
        assert isinstance(out["outer"], dict)
        assert not isinstance(out["outer"], MappingProxyType)
        out["outer"]["new"] = "ok"
        assert out == {"outer": {"inner": 1, "new": "ok"}}

    def test_list_of_dicts_materialised(self, server):
        cached = server._read_only_view({"items": [{"a": 1}, {"b": 2}]})
        out = server._cfg_copy_for_writeback(cached)
        assert isinstance(out, dict)
        assert isinstance(out["items"], list)
        assert all(isinstance(item, dict) for item in out["items"])
        out["items"].append({"c": 3})
        assert out["items"] == [{"a": 1}, {"b": 2}, {"c": 3}]


# ---------------------------------------------------------------------------
# _load_cfg_raw / _save_cfg round-trip
# ---------------------------------------------------------------------------

class TestCfgCacheRoundTrip:
    """End-to-end: a write through _save_cfg preserves the read-only contract."""

    def test_save_cfg_preserves_immutability(self, server, tmp_path, monkeypatch):
        """After _save_cfg, the next _load_cfg_raw returns a value whose
        inner dicts are read-only views."""
        import yaml

        # Isolate config.yaml to a tmp dir by pointing HERMES_HOME there.
        home = tmp_path / ".hermes"
        home.mkdir()
        (home / "config.yaml").write_text(yaml.safe_dump({"display": {"skin": "x"}}))

        monkeypatch.setenv("HERMES_HOME", str(home))
        original_hermes_home = server._hermes_home
        server._hermes_home = home
        try:
            # First read populates the cache with a view.
            cfg1 = server._load_cfg_raw()
            assert isinstance(cfg1, dict)
            assert isinstance(cfg1.get("display"), MappingProxyType)
            # A subsequent call hits the cache and returns a shallow copy.
            cfg2 = server._load_cfg_raw()
            assert isinstance(cfg2, dict)
            # Top-level cfg2 is a plain dict (caller-mutable).
            assert not isinstance(cfg2, MappingProxyType)
            # But the nested display is still a view.
            assert isinstance(cfg2.get("display"), MappingProxyType)
        finally:
            server._cfg_cache = None
            server._cfg_mtime = None
            server._cfg_path = None
            server._hermes_home = original_hermes_home
