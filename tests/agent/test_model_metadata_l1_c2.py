"""Tests for the L1 cache bridge in agent.model_metadata (cache C2).

The L1 is an in-process LRU mirror of the on-disk
``context_length_cache.yaml``. These tests exercise:

  * The L1 is initialized lazily, never raises on init failure.
  * get_cached_context_length consults L1 before YAML.
  * get_cached_context_length repopulates L1 on a YAML fall-through.
  * save_context_length writes through to both L1 and YAML.
  * save_context_length with the no-op (already-stored) path still
    populates L1 so the next read is hot.
  * _invalidate_cached_context_length drops the L1 entry BEFORE
    dropping the YAML entry.
  * Legacy (slashed) keys repopulate the canonical key in both layers.
  * L1 init is floored (max_entries>=1, max_bytes>=1024).
  * L1 get/put/invalidate exceptions fall through to the YAML path
    without propagating.
  * A model save with a non-positive length is still rejected (the
    pre-C2 contract) and does not touch L1.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def _fresh_hermes_home(tmp_path, monkeypatch):
    """Each test gets a clean HERMES_HOME and a clean L1 module-level cache.

    The L1 is a module-level singleton in agent.model_metadata. We must
    reset it between tests, otherwise a populated L1 leaks across tests
    and the YAML-backed contract under test is no longer the system
    under test.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from agent import model_metadata

    original_l1 = model_metadata._CONTEXT_CACHE_L1
    original_tombstones = set(model_metadata._CONTEXT_L1_TOMBSTONES)
    model_metadata._CONTEXT_CACHE_L1 = None
    model_metadata._CONTEXT_L1_TOMBSTONES = set()
    try:
        yield tmp_path, model_metadata
    finally:
        model_metadata._CONTEXT_CACHE_L1 = original_l1
        model_metadata._CONTEXT_L1_TOMBSTONES = original_tombstones


# ---------------------------------------------------------------------------
# L1 lazy init
# ---------------------------------------------------------------------------
def test_l1_init_returns_lru_cache(_fresh_hermes_home):
    """First access to L1 creates a real InProcessLRUCache."""
    _tmp, mm = _fresh_hermes_home
    l1 = mm._get_context_cache_l1()
    assert l1 is not None
    # Round-trip sanity
    l1.put("k", 42)
    assert l1.get("k") == 42


def test_l1_init_uses_default_when_no_config_section(_fresh_hermes_home):
    """When cache.model_metadata is absent, L1 falls back to 256 / 8 MiB."""
    _tmp, mm = _fresh_hermes_home
    l1 = mm._get_context_cache_l1()
    assert l1 is not None
    for i in range(300):
        l1.put(f"k{i}", i)
    # The first 44 should be evicted (300-256=44).
    assert l1.get("k0") is None
    assert l1.get("k299") == 299


def test_l1_init_honors_config_max_entries(_fresh_hermes_home):
    """cache.model_metadata.l1_max_entries is honored."""
    _tmp, mm = _fresh_hermes_home
    cfg_path = _tmp / "config.yaml"
    cfg_path.write_text(
        "cache:\n  model_metadata:\n    l1_max_entries: 4\n    l1_max_bytes: 65536\n"
    )
    l1 = mm._get_context_cache_l1()
    assert l1 is not None
    for i in range(10):
        l1.put(f"k{i}", i)
    # With max_entries=4, the oldest 6 must be evicted.
    assert l1.get("k0") is None
    assert l1.get("k3") is None
    assert l1.get("k9") == 9


def test_l1_init_floors_max_entries_and_bytes(_fresh_hermes_home):
    """A misconfigured max_entries<1 and max_bytes<1024 are floored.

    Defense in depth — the loader never accepts degenerate bounds.
    """
    _tmp, mm = _fresh_hermes_home
    cfg_path = _tmp / "config.yaml"
    cfg_path.write_text(
        "cache:\n  model_metadata:\n    l1_max_entries: 0\n    l1_max_bytes: 0\n"
    )
    l1 = mm._get_context_cache_l1()
    # Should not raise; should accept at least one entry.
    l1.put("k", 1)
    assert l1.get("k") == 1


def test_l1_init_does_not_raise_when_config_loader_throws(_fresh_hermes_home):
    """An exception in the config loader is logged and the L1 still works."""
    _tmp, mm = _fresh_hermes_home

    def _broken():
        raise RuntimeError("synthetic")

    with patch("hermes_cli.config.load_config_readonly", side_effect=_broken):
        # Reset so we re-init
        mm._CONTEXT_CACHE_L1 = None
        l1 = mm._get_context_cache_l1()
    assert l1 is not None
    l1.put("k", 1)
    assert l1.get("k") == 1


# ---------------------------------------------------------------------------
# get_cached_context_length uses L1
# ---------------------------------------------------------------------------
def test_get_hits_l1_first_and_skips_yaml(_fresh_hermes_home):
    """When L1 has the value, YAML is never read.

    We assert this by making the YAML file unreadable and confirming
    the L1-served value is still returned.
    """
    _tmp, mm = _fresh_hermes_home
    l1 = mm._get_context_cache_l1()
    l1.put("gpt-4@https://api.openai.com/v1", 8192)
    bad_path = _tmp / "context_length_cache.yaml"
    bad_path.parent.mkdir(parents=True, exist_ok=True)
    bad_path.write_text("not a valid yaml: : :")
    val = mm.get_cached_context_length("gpt-4", "https://api.openai.com/v1")
    assert val == 8192


def test_get_falls_through_to_yaml_and_repopulates_l1(_fresh_hermes_home):
    """A YAML hit repopulates L1 so the next call is hot."""
    _tmp, mm = _fresh_hermes_home
    yaml_path = _tmp / "context_length_cache.yaml"
    yaml_path.write_text(
        "context_lengths:\n  gpt-4@https://api.openai.com/v1: 4096\n"
    )
    assert mm._get_context_cache_l1() is not None
    assert mm._get_context_cache_l1().get("gpt-4@https://api.openai.com/v1") is None
    val = mm.get_cached_context_length("gpt-4", "https://api.openai.com/v1")
    assert val == 4096
    assert mm._get_context_cache_l1().get("gpt-4@https://api.openai.com/v1") == 4096


def test_get_returns_none_when_neither_layer_has_value(_fresh_hermes_home):
    """Cold cache, no YAML entry: returns None."""
    _tmp, mm = _fresh_hermes_home
    val = mm.get_cached_context_length("unknown", "https://api.example.com/v1")
    assert val is None


# ---------------------------------------------------------------------------
# save_context_length writes through
# ---------------------------------------------------------------------------
def test_save_writes_through_to_l1_and_yaml(_fresh_hermes_home):
    """save_context_length populates both L1 and YAML."""
    _tmp, mm = _fresh_hermes_home
    mm.save_context_length("claude-3", "https://api.anthropic.com/v1", 200000)
    assert mm._get_context_cache_l1().get("claude-3@https://api.anthropic.com/v1") == 200000
    yaml_path = _tmp / "context_length_cache.yaml"
    assert yaml_path.exists()
    content = yaml_path.read_text()
    assert "claude-3@https://api.anthropic.com/v1" in content
    assert "200000" in content


def test_save_noop_path_still_populates_l1(_fresh_hermes_home):
    """When the value is already at the same length in YAML, the no-op
    early-return still mirrors the value into L1."""
    _tmp, mm = _fresh_hermes_home
    yaml_path = _tmp / "context_length_cache.yaml"
    yaml_path.write_text(
        "context_lengths:\n  claude-3@https://api.anthropic.com/v1: 200000\n"
    )
    mm._CONTEXT_CACHE_L1 = None
    mm.save_context_length("claude-3", "https://api.anthropic.com/v1", 200000)
    assert mm._get_context_cache_l1().get("claude-3@https://api.anthropic.com/v1") == 200000


def test_save_rejects_non_positive_length(_fresh_hermes_home):
    """save_context_length(..., 0) is a no-op (pre-C2 contract) and does
    not touch L1 or YAML."""
    _tmp, mm = _fresh_hermes_home
    mm.save_context_length("bad-model", "https://api.example.com/v1", 0)
    assert mm._get_context_cache_l1().get("bad-model@https://api.example.com/v1") is None
    yaml_path = _tmp / "context_length_cache.yaml"
    assert not yaml_path.exists()


# ---------------------------------------------------------------------------
# _invalidate_cached_context_length
# ---------------------------------------------------------------------------
def test_invalidate_drops_l1_then_yaml(_fresh_hermes_home):
    """Invalidation removes the entry from both L1 and YAML."""
    _tmp, mm = _fresh_hermes_home
    mm.save_context_length("to-invalidate", "https://api.example.com/v1", 1234)
    assert mm._get_context_cache_l1().get("to-invalidate@https://api.example.com/v1") == 1234
    mm._invalidate_cached_context_length("to-invalidate", "https://api.example.com/v1")
    assert mm._get_context_cache_l1().get("to-invalidate@https://api.example.com/v1") is None
    val = mm.get_cached_context_length("to-invalidate", "https://api.example.com/v1")
    assert val is None


# ---------------------------------------------------------------------------
# Legacy (slashed) keys
# ---------------------------------------------------------------------------
def test_legacy_slashed_key_repopulates_canonical_in_both_layers(_fresh_hermes_home):
    """A legacy ``model@http://host/v1/`` row is honored and promotes
    the canonical ``model@http://host/v1`` key in BOTH L1 and YAML so
    the next read uses the normalized form."""
    _tmp, mm = _fresh_hermes_home
    yaml_path = _tmp / "context_length_cache.yaml"
    yaml_path.write_text(
        "context_lengths:\n  old-model@https://api.example.com/v1/: 5000\n"
    )
    mm._CONTEXT_CACHE_L1 = None
    val = mm.get_cached_context_length("old-model", "https://api.example.com/v1")
    assert val == 5000
    assert mm._get_context_cache_l1().get("old-model@https://api.example.com/v1") == 5000
    yaml_content = yaml_path.read_text()
    assert "old-model@https://api.example.com/v1:" in yaml_content
    assert "old-model@https://api.example.com/v1/:" in yaml_content


# ---------------------------------------------------------------------------
# L1 exception handling
# ---------------------------------------------------------------------------
def test_l1_get_exception_falls_through_to_yaml(_fresh_hermes_home):
    """An L1 .get() exception does not propagate; YAML is consulted."""
    _tmp, mm = _fresh_hermes_home
    yaml_path = _tmp / "context_length_cache.yaml"
    yaml_path.write_text(
        "context_lengths:\n  model@https://api.example.com/v1: 9999\n"
    )
    l1 = mm._get_context_cache_l1()

    def _boom(_):
        raise RuntimeError("synthetic L1 failure")

    with patch.object(l1, "get", side_effect=_boom):
        val = mm.get_cached_context_length("model", "https://api.example.com/v1")
    assert val == 9999


def test_l1_put_exception_does_not_break_save(_fresh_hermes_home):
    """An L1 .put() exception does not prevent the YAML write."""
    _tmp, mm = _fresh_hermes_home
    l1 = mm._get_context_cache_l1()

    def _boom(*_args, **_kwargs):
        raise RuntimeError("synthetic L1 failure")

    with patch.object(l1, "put", side_effect=_boom):
        mm.save_context_length("m", "https://api.example.com/v1", 1234)
    yaml_path = _tmp / "context_length_cache.yaml"
    assert yaml_path.exists()
    assert "m@https://api.example.com/v1" in yaml_path.read_text()


def test_l1_invalidate_exception_does_not_break_yaml_invalidation(_fresh_hermes_home):
    """An L1 .invalidate() exception does not prevent the YAML write.

    Also verifies the tombstone mechanism: after a failed invalidate,
    the next get MUST skip L1 (because L1 still holds the value) and
    consult YAML (which now lacks the value), returning None.
    """
    _tmp, mm = _fresh_hermes_home
    mm.save_context_length("m", "https://api.example.com/v1", 1234)
    l1 = mm._get_context_cache_l1()

    def _boom(_):
        raise RuntimeError("synthetic L1 failure")

    with patch.object(l1, "invalidate", side_effect=_boom):
        mm._invalidate_cached_context_length("m", "https://api.example.com/v1")
    # The tombstone forces the next read to consult YAML.
    assert "m@https://api.example.com/v1" in mm._CONTEXT_L1_TOMBSTONES
    val = mm.get_cached_context_length("m", "https://api.example.com/v1")
    assert val is None
    # After the fall-through, the tombstone is cleared.
    assert "m@https://api.example.com/v1" not in mm._CONTEXT_L1_TOMBSTONES


def test_tombstone_cleared_after_yaml_fallthrough(_fresh_hermes_home):
    """Tombstone is removed the moment the get falls through to YAML.

    A persistent tombstone would silently degrade L1 performance forever
    after a single failed invalidate. The contract is: tombstone blocks
    exactly one read.
    """
    _tmp, mm = _fresh_hermes_home
    mm.save_context_length("model", "https://api.example.com/v1", 9999)
    # Force a tombstone by simulating a failed L1.invalidate.
    mm._CONTEXT_L1_TOMBSTONES.add("model@https://api.example.com/v1")
    # First read falls through (YAML has the value), repopulates L1, clears tombstone.
    val = mm.get_cached_context_length("model", "https://api.example.com/v1")
    assert val == 9999
    assert "model@https://api.example.com/v1" not in mm._CONTEXT_L1_TOMBSTONES
    # Second read is hot in L1 (no tombstone, no YAML consultation).
    val2 = mm.get_cached_context_length("model", "https://api.example.com/v1")
    assert val2 == 9999
