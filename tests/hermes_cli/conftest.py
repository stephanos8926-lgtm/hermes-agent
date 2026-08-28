"""Fixtures shared across hermes_cli kanban tests."""

from __future__ import annotations

import pytest


@pytest.fixture
def all_assignees_spawnable(monkeypatch):
    """Pretend every assignee maps to a real Hermes profile.

    Most dispatcher tests use synthetic assignees ("alice", "bob") that
    don't correspond to actual profile directories on disk. Without this
    patch, the dispatcher's profile-exists guard (PR #20105) routes
    those tasks into ``skipped_nonspawnable`` instead of spawning, which
    would break tests that assert spawn behavior.
    """
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)


@pytest.fixture(autouse=True)
def _suppress_concurrent_hermes_gate(request, monkeypatch):
    """Default ``_detect_concurrent_hermes_instances`` to ``[]`` for every test.

    The Windows update path now refuses to proceed when another
    ``hermes.exe`` is detected (issue #26670). On a developer's Windows
    machine running the test suite via ``hermes`` itself, this would
    flag the running agent as a concurrent instance and abort every
    ``cmd_update`` test. Tests that want to exercise the gate explicitly
    re-patch ``_detect_concurrent_hermes_instances`` with their own
    return value — autouse here gives a clean default without touching
    the rest of the suite.

    Tests that need to call the REAL function (e.g. unit tests for the
    helper itself) opt out with ``@pytest.mark.real_concurrent_gate``.
    """
    if request.node.get_closest_marker("real_concurrent_gate"):
        return
    try:
        from hermes_cli import main as _cli_main
    except Exception:
        return
    # raising=False: under pytest's per-test spawn isolation, a concurrent
    # xdist worker importing a module that transitively touches hermes_cli.main
    # can briefly expose a partially-initialized module object here — one where
    # _detect_concurrent_hermes_instances isn't defined yet. A bare setattr
    # would raise AttributeError and error the (unrelated) test. The attribute
    # always exists once main.py finishes importing, so a no-op when it's
    # transiently absent is the correct, race-free default.
    monkeypatch.setattr(
        _cli_main,
        "_detect_concurrent_hermes_instances",
        lambda *_a, **_k: [],
        raising=False,
    )


# ---------------------------------------------------------------------------
# Replay-economy fixtures (added per v2 spec §4.3, 2026-08-28)
#
# These fixtures provide hermetic isolation for tests of
# `hermes_cli.replay_economy` — both the example tests in
# `tests/hermes_cli/test_replay_economy.py` and the property tests in
# `tests/hermes_cli/test_replay_economy_properties.py`.
#
# Design notes (audit findings addressed):
#   - F-LOW-1: `_reset_replay_state` is autouse=True so every test in
#     tests/hermes_cli/ gets hermetic state (cache, counters, HERMES_HOME).
#   - F-LOW-2: `fake_lcm_externalize` stubs BOTH `_LCM_EXTERNALIZE_FN` AND
#     the cached `_LCM_EXTERNALIZE_AVAILABLE` flag — the latter is what
#     `_detect_lcm_externalize()` caches, so without setting both the
#     fixture is silently ignored.
#   - F-LOW-3: `install_test_cache` is a FACTORY (returns a function) so
#     each test can install a cache with custom params without committing
#     to a single shared cache instance.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_replay_state(tmp_path, monkeypatch):
    """Reset cache, counters, and redirect HERMES_HOME to a tempdir per test.

    Autouse=True ensures EVERY test in tests/hermes_cli/ starts with a
    clean replay-economy state. Without this, property tests with
    Hypothesis would share state across the run and fail spuriously.
    """
    from hermes_cli import replay_economy as _re

    monkeypatch.setattr(_re, "_HERMES_HOME", tmp_path)
    _re.reset_replay_counters()
    monkeypatch.setattr(_re, "_request_cache", None)
    yield
    _re.reset_replay_counters()
    _re._request_cache = None


@pytest.fixture
def fake_lcm_externalize(monkeypatch):
    """Stub out the LCM externalize function so tests don't need the plugin.

    Returns a list that records every call the stub receives, so tests
    can assert on the call count and arguments.

    IMPORTANT (per F-LOW-2): sets both `_LCM_EXTERNALIZE_FN` and the
    cached `_LCM_EXTERNALIZE_AVAILABLE` flag. The detection helper
    `_detect_lcm_externalize()` caches its result in the latter; if
    only the function is patched, the cache says "no LCM" and the patch
    is silently bypassed.
    """
    from hermes_cli import replay_economy as _re

    calls: list[dict] = []

    def _fake_externalize(content, tool_call_id, session_id, config, hermes_home, force=False):
        calls.append({
            "content_len": len(content),
            "tool_call_id": tool_call_id,
            "session_id": session_id,
        })
        return f"lcm://replay/test-{tool_call_id}"

    monkeypatch.setattr(_re, "_LCM_EXTERNALIZE_FN", _fake_externalize)
    monkeypatch.setattr(_re, "_LCM_EXTERNALIZE_AVAILABLE", True)
    monkeypatch.setattr(_re, "_LCM_CONFIG", None)
    return calls


@pytest.fixture
def install_test_cache(monkeypatch):
    """Factory: install a fresh InProcessLRUCache with custom params.

    Returns a callable that installs a cache with the given kwargs and
    returns the cache instance. Each test can therefore tune the cache
    to its needs (tiny for LRU eviction tests, big for size-boundary
    tests) without sharing state with other tests.
    """
    from agent._cache import InProcessLRUCache
    from hermes_cli import replay_economy as _re

    def _install(**kwargs):
        params = {
            "max_entries": 100,
            "max_bytes": 10 * 1024 * 1024,
            "value_max_bytes": 1024 * 1024,
        }
        params.update(kwargs)
        cache = InProcessLRUCache(**params)
        monkeypatch.setattr(_re, "_request_cache", cache)
        return cache

    return _install
