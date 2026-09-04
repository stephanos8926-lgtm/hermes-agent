"""C4: aggregate byte-budget valve for the gateway agent cache.

Contract: when bounds.max_bytes is set, cached agents whose estimated
transcript weight pushes the total over budget are evicted LRU-oldest-
first, skipping agents mid-turn. When max_bytes is None (default), the
valve is fully disabled -- prior behavior byte-for-byte.
"""
import threading
from collections import OrderedDict
from unittest.mock import MagicMock

import pytest


class _FakeAgent:
    def __init__(self, n_msgs=0, msg_size=100):
        self._session_messages = [{"m": "x" * msg_size} for _ in range(n_msgs)]
        self._last_activity_ts = 0.0


def _make_runner(cache, max_bytes=None):
    from gateway.run import GatewayRunner
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._agent_cache = cache
    runner._agent_cache_lock = threading.Lock()
    runner._c4_test_bounds = _bounds(max_bytes)
    runner._agent_cache_bounds = lambda: runner._c4_test_bounds
    runner._running_agents = {}
    runner._AGENT_PENDING_SENTINEL = object()
    runner._running_agent_items = lambda: list(runner._running_agents.items())
    return runner


def _bounds(max_bytes=None):
    from gateway.agent_cache_pressure import AgentCacheBounds
    return AgentCacheBounds(
        max_size=None,
        idle_ttl_secs=None,
        max_bytes=max_bytes,
        memory_high_mb=None,
    )


def test_valve_disabled_when_max_bytes_none():
    cache = OrderedDict()
    cache["a"] = (_FakeAgent(n_msgs=10_000), "meta")
    r = _make_runner(cache, None)
    with r._agent_cache_lock:
        r._enforce_agent_cache_cap()
    assert len(cache) == 1


def test_over_budget_evicts_lru_first():
    small = _FakeAgent(n_msgs=1)
    huge = _FakeAgent(n_msgs=5_000)
    cache = OrderedDict()
    cache["a"] = (small, "m")   # oldest
    cache["b"] = (huge, "m")    # newest, byte hog
    r = _make_runner(cache, 2048)
    released = []
    r._release_evicted_agent_soft = lambda entry: released.append(entry)
    with r._agent_cache_lock:
        r._enforce_agent_cache_cap()
    assert "b" not in cache or len(cache) < 2
    assert len(released) >= 1


def test_running_agents_skipped():
    running = _FakeAgent(n_msgs=5_000)
    idle = _FakeAgent(n_msgs=1)
    cache = OrderedDict()
    cache["a"] = (idle, "m")
    cache["b"] = (running, "m")
    r = _make_runner(cache, 2048)
    # The valve consults running-agent ids via the runner's registry.
    r._running_agents["t1"] = running
    with r._agent_cache_lock:
        r._enforce_agent_cache_cap()
    # Running agent must survive even though it is the byte hog.
    assert "b" in cache


def test_estimator_monotonic_in_message_count():
    from gateway.run import _agent_cache_entry_byte_estimate as est
    small = est((_FakeAgent(n_msgs=10), "m"))
    big = est((_FakeAgent(n_msgs=1000), "m"))
    assert big > small > 0


def test_estimator_handles_garbage():
    from gateway.run import _agent_cache_entry_byte_estimate as est
    assert est(None) == 4096
    assert est((object(),)) == 4096
