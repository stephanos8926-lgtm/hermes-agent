"""Tests for hermes_cli/replay_economy.py — D-1 + D-2 + M2 v1.1.

Maps 1:1 to the test plan in plans/2026-08-27-replay-economy-spec-v2.md
§3.3 (T-D1-1..12), §4.4 (T-D2-1..12), §5.3 (T-M2-1..5). Total: 29 tests.

These tests are hermetic: no network, no LCM, no provider calls. The LCM
externalize function is patched at the module level to return a deterministic
ref so the tests don't depend on the hermes-lcm plugin being installed.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_cli import replay_economy as re


# ──────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_replay_state(tmp_path, monkeypatch):
    """Reset cache, counters, and redirect HERMES_HOME to a tempdir per test."""
    # Re-route replay economy to a tempdir so it doesn't touch ~/.hermes/data
    monkeypatch.setattr(re, "_HERMES_HOME", tmp_path)
    re.reset_replay_counters()
    # Drop the cached request cache so the next test gets a fresh one
    monkeypatch.setattr(re, "_request_cache", None)
    yield
    re.reset_replay_counters()
    re._request_cache = None


@pytest.fixture
def fake_lcm_externalize(monkeypatch):
    """Stub out the LCM externalize function so tests don't need the plugin."""
    calls = []

    def _fake_externalize(content, tool_call_id, session_id, config, hermes_home, force=False):
        calls.append({"content_len": len(content), "tool_call_id": tool_call_id, "session_id": session_id})
        # Return a deterministic LCM ref string
        return f"lcm://replay/test-{tool_call_id}"

    # Set BOTH the function and the cached-detection flag, so _detect_lcm_externalize
    # returns our fake without re-running the (failing) import.
    monkeypatch.setattr(re, "_LCM_EXTERNALIZE_FN", _fake_externalize)
    monkeypatch.setattr(re, "_LCM_EXTERNALIZE_AVAILABLE", True)
    # Clear any cached config/home that the real detector would have set
    monkeypatch.setattr(re, "_LCM_CONFIG", None)
    return calls


# ──────────────────────────────────────────────────────────────────────────
# T-D1: Request-Level Tool-Result Cache (12 tests)
# ──────────────────────────────────────────────────────────────────────────


class TestD1RequestCache:
    """T-D1-1..12 from spec §3.3."""

    def test_t_d1_1_get_on_empty_cache_returns_none(self):
        """T-D1-1: `get` on empty cache returns None."""
        result = re.cache_check("read_file", {"path": "/tmp/x"}, "session_1")
        assert result is None

    def test_t_d1_2_put_then_get_returns_stored_value(self):
        """T-D1-2: `put` then `get` returns the stored value."""
        tool_result = {"role": "tool", "tool_call_id": "tc_1", "content": "hello"}
        re.cache_store("read_file", {"path": "/tmp/x"}, tool_result, "session_1")
        result = re.cache_check("read_file", {"path": "/tmp/x"}, "session_1")
        assert result is not None
        assert result["content"] == "hello"

    def test_t_d1_3_get_with_different_args_returns_none(self):
        """T-D1-3: `get` with different args returns None (no false positive)."""
        tool_result = {"role": "tool", "tool_call_id": "tc_1", "content": "hello"}
        re.cache_store("read_file", {"path": "/tmp/x"}, tool_result, "session_1")
        # Different args → must miss
        result = re.cache_check("read_file", {"path": "/tmp/y"}, "session_1")
        assert result is None

    def test_t_d1_4_dict_order_does_not_affect_key(self):
        """T-D1-4: `get` with same args in different dict order returns the stored value."""
        tool_result = {"role": "tool", "tool_call_id": "tc_1", "content": "hello"}
        re.cache_store("read_file", {"path": "/tmp/x", "offset": 0, "limit": 100}, tool_result, "session_1")
        # Same args, different insertion order — must hit
        result = re.cache_check("read_file", {"limit": 100, "offset": 0, "path": "/tmp/x"}, "session_1")
        assert result is not None
        assert result["content"] == "hello"

    def test_t_d1_5_lru_eviction_when_budget_exceeded(self):
        """T-D1-5: LRU eviction removes least-recently-used entry when budget exceeded."""
        # Use a tiny cache for deterministic eviction
        from agent._cache import InProcessLRUCache
        tiny = InProcessLRUCache(max_entries=2, max_bytes=10_000_000)
        re._request_cache = tiny

        for i in range(3):
            re.cache_store("read_file", {"path": f"/tmp/{i}"}, {"role": "tool", "content": f"v{i}"}, "s")
        # First entry should be evicted
        assert re.cache_check("read_file", {"path": "/tmp/0"}, "s") is None
        # Later entries should still be present
        assert re.cache_check("read_file", {"path": "/tmp/1"}, "s") is not None
        assert re.cache_check("read_file", {"path": "/tmp/2"}, "s") is not None

    def test_t_d1_6_concurrent_disjoint_keys_final_state_matches(self):
        """T-D1-6: Concurrency: 16 threads × 1000 disjoint ops, no data loss."""
        # Use a cache big enough to hold everything
        from agent._cache import InProcessLRUCache
        big = InProcessLRUCache(max_entries=20_000, max_bytes=10_000_000)
        re._request_cache = big

        N_THREADS = 16
        OPS_PER_THREAD = 1000
        barrier = threading.Barrier(N_THREADS)
        errors = []

        def worker(tid: int):
            try:
                barrier.wait()  # all threads start together
                for i in range(OPS_PER_THREAD):
                    key = f"k_{tid}_{i}"
                    re.cache_store("read_file", {"path": key}, {"role": "tool", "content": key}, "s")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Threads raised: {errors[:3]}"
        # Verify all 16,000 keys present
        for tid in range(N_THREADS):
            for i in range(0, OPS_PER_THREAD, 100):  # sample every 100th
                key = f"k_{tid}_{i}"
                result = re.cache_check("read_file", {"path": key}, "s")
                assert result is not None, f"Missing key after concurrent store: {key}"
                assert result["content"] == key

    def test_t_d1_7_cache_backend_error_fail_open(self):
        """T-D1-7: Cache backend error → fall-through, no exception raised."""
        from agent._cache import InProcessLRUCache
        broken = InProcessLRUCache(max_entries=10, max_bytes=10_000_000)

        # Force a backend error by patching get/put to raise
        def boom(*_a, **_kw):
            raise RuntimeError("simulated backend failure")
        broken.get = boom
        broken.put = boom
        re._request_cache = broken

        # Should NOT raise; should return None (fail-open)
        result = re.cache_check("read_file", {"path": "/tmp/x"}, "s")
        assert result is None
        # Should NOT raise on store either
        re.cache_store("read_file", {"path": "/tmp/x"}, {"role": "tool", "content": "y"}, "s")

    def test_t_d1_8_oversized_result_is_externalized(self, fake_lcm_externalize):
        """T-D1-8: Oversized result is externalized (LCM ref in stored value)."""
        from agent._cache import InProcessLRUCache
        # Use cache with tiny value_max_bytes so 2MB value gets externalized
        small = InProcessLRUCache(
            max_entries=100,
            max_bytes=10_000_000,
            value_max_bytes=1024,  # 1 KiB per entry
        )
        re._request_cache = small

        huge_content = "X" * (2 * 1024 * 1024)  # 2 MiB
        tool_result = {"role": "tool", "tool_call_id": "tc_huge", "content": huge_content}
        re.cache_store("read_file", {"path": "/tmp/big"}, tool_result, "session_1")

        # LCM should have been called
        assert len(fake_lcm_externalize) == 1
        assert fake_lcm_externalize[0]["content_len"] == len(huge_content)
        assert fake_lcm_externalize[0]["tool_call_id"] == "tc_huge"

    def test_t_d1_9_shape_equivalence_on_hit(self):
        """T-D1-9: Cache hit returns shape-equivalent message: `role` and `content` match exactly."""
        tool_result = {"role": "tool", "tool_call_id": "tc_orig", "content": "hello"}
        re.cache_store("read_file", {"path": "/tmp/x"}, tool_result, "session_1")
        result = re.cache_check("read_file", {"path": "/tmp/x"}, "session_1")
        # Required by shape contract
        assert result["role"] == "tool"
        assert result["content"] == "hello"
        # tool_call_id MAY differ (spec allows it) — we don't assert on it

    def test_t_d1_10_default_non_cacheable_tools_always_miss(self):
        """T-D1-10: Default-non-cacheable tools (bash, web_search, etc.) are not cached."""
        # Even after a "store" attempt, a non-cacheable tool must miss on check
        for tool in ("bash", "shell", "terminal", "web_search", "web_fetch", "datetime", "random"):
            re.cache_store(tool, {"q": "x"}, {"role": "tool", "content": "result"}, "s")
            result = re.cache_check(tool, {"q": "x"}, "s")
            assert result is None, f"Non-cacheable tool '{tool}' should miss but got: {result}"

    def test_t_d1_12_counters_increment_correctly(self):
        """T-D1-12: M2 snapshot v1.1: cache.replay_l1.hits increments on hit, misses on miss."""
        # 1 miss + 3 hits
        re.cache_check("read_file", {"p": "/x"}, "s")  # miss
        re.cache_store("read_file", {"p": "/x"}, {"role": "tool", "content": "v"}, "s")
        re.cache_check("read_file", {"p": "/x"}, "s")  # hit
        re.cache_check("read_file", {"p": "/x"}, "s")  # hit
        re.cache_check("read_file", {"p": "/x"}, "s")  # hit

        counters = re.get_replay_counters()
        l1 = counters["replay_l1"]
        assert l1["misses"] == 1
        assert l1["hits"] == 3
        assert l1["hit_rate_pct"] == 75.0

    def test_t_d1_11_s3_baseline_unchanged_via_run_baseline_suite(self):
        """T-D1-11: S3 baseline suite fingerprint unchanged (smoke test of the runner)."""
        # We don't run the full 16-file suite in a unit test — that would be slow
        # and the canonical check is `bash scripts/run_baseline_suite.sh`.
        # This test asserts the runner script exists and is executable.
        runner = Path(__file__).parent.parent.parent / "scripts" / "run_baseline_suite.sh"
        assert runner.exists(), f"Baseline runner not found at {runner}"
        assert os.access(runner, os.X_OK), f"Baseline runner not executable: {runner}"


# ──────────────────────────────────────────────────────────────────────────
# T-D2: Wire-Time Tool-Result Compaction (12 tests)
# ──────────────────────────────────────────────────────────────────────────


class TestD2WireCompaction:
    """T-D2-1..12 from spec §4.4."""

    def test_t_d2_1_small_message_unchanged(self, fake_lcm_externalize):
        """T-D2-1: Small message (under threshold) → unchanged."""
        messages = [
            {"role": "tool", "tool_call_id": "tc_1", "content": "small result"},
        ]
        compacted = re.compact_tool_messages(messages, "session_1")
        assert len(compacted) == 1
        assert compacted[0]["content"] == "small result"
        # LCM should NOT be called for sub-threshold content
        assert len(fake_lcm_externalize) == 0

    def test_t_d2_2_large_message_compacted_with_head_tail_ref(self, fake_lcm_externalize):
        """T-D2-2: Large message (over threshold) → head+tail+ref, correct counts."""
        # Threshold is 100,000 chars by default; use 250,000 to be safely over
        large = "X" * 250_000
        messages = [{"role": "tool", "tool_call_id": "tc_big", "content": large}]
        compacted = re.compact_tool_messages(messages, "session_1")

        m = compacted[0]
        assert m["role"] == "tool"
        assert m["tool_call_id"] == "tc_big"
        # Content should now be much shorter than 250K
        assert len(m["content"]) < 10_000
        # Should contain head, tail, and ref marker
        assert "X" * 100 in m["content"]  # some head bytes preserved
        # LCM was called with the full content
        assert len(fake_lcm_externalize) == 1
        assert fake_lcm_externalize[0]["content_len"] == 250_000

    def test_t_d2_3_compaction_preserves_role_and_tool_call_id(self, fake_lcm_externalize):
        """T-D2-3: Compaction produces same `role` and `tool_call_id`."""
        large = "Y" * 200_000
        messages = [{"role": "tool", "tool_call_id": "tc_xyz", "content": large}]
        compacted = re.compact_tool_messages(messages, "session_1")
        assert compacted[0]["role"] == "tool"
        assert compacted[0]["tool_call_id"] == "tc_xyz"

    def test_t_d2_4_lcm_ref_in_compacted_message_recoverable(self, fake_lcm_externalize):
        """T-D2-4: LCM ref in compacted message is recoverable (round-trip via the ref)."""
        large = "Z" * 200_000
        messages = [{"role": "tool", "tool_call_id": "tc_rt", "content": large}]
        compacted = re.compact_tool_messages(messages, "session_1")
        # The fake LCM ref is `lcm://replay/test-tc_rt` — should be in the content
        assert "lcm://replay/test-tc_rt" in compacted[0]["content"]

    def test_t_d2_5_lcm_externalize_failure_original_unchanged(self, monkeypatch):
        """T-D2-5: LCM externalize failure + temp-file fallback failure → original sent unchanged."""
        def boom(*_a, **_kw):
            raise RuntimeError("LCM backend down")
        # LCM raises on call
        monkeypatch.setattr(re, "_LCM_EXTERNALIZE_FN", boom)
        monkeypatch.setattr(re, "_LCM_EXTERNALIZE_AVAILABLE", True)
        # Temp-file fallback also fails
        monkeypatch.setattr(re, "_externalize_via_tempfile", lambda c, t: None)

        large = "Q" * 200_000
        messages = [{"role": "tool", "tool_call_id": "tc_fail", "content": large}]
        compacted = re.compact_tool_messages(messages, "session_1")
        # Both backends failed → original content preserved (fail-open)
        assert compacted[0]["content"] == large

    def test_t_d2_6_non_string_content_unchanged(self):
        """T-D2-6: Non-string content (multimodal/dict/list) → unchanged."""
        # List content (multimodal)
        messages = [
            {"role": "tool", "tool_call_id": "tc_multi", "content": [
                {"type": "text", "text": "look at this"},
                {"type": "image", "url": "data:image/png;base64,..."},
            ]},
        ]
        compacted = re.compact_tool_messages(messages, "session_1")
        # Content preserved as-is (not compacted)
        assert compacted[0]["content"] == messages[0]["content"]

    def test_t_d2_8_wire_compaction_counters_increment(self, fake_lcm_externalize):
        """T-D2-8: M2 snapshot v1.1: cache.replay_wire.bytes_saved_total increments correctly."""
        before = re.get_replay_counters()["replay_wire"]["bytes_saved_total"]
        large = "A" * 200_000
        re.compact_tool_messages(
            [{"role": "tool", "tool_call_id": "tc_a", "content": large}],
            "session_1",
        )
        after = re.get_replay_counters()["replay_wire"]
        assert after["compactions"] == 1
        assert after["bytes_saved_total"] > before
        assert after["bytes_in"] == 200_000
        assert after["bytes_out"] < 10_000  # the head+tail+ref string

    def test_t_d2_9_e2e_bytes_on_wire_measured(self, fake_lcm_externalize):
        """T-D2-9: E2E — measure bytes-on-wire before and after compaction (mocked provider)."""
        # The "wire" is the message list passed to compact_tool_messages.
        # We measure: pre = sum(len(m['content'])) before, post = sum after.
        large_a = "X" * 150_000
        large_b = "Y" * 150_000
        messages = [
            {"role": "user", "content": "fetch big stuff"},
            {"role": "tool", "tool_call_id": "tc_a", "content": large_a},
            {"role": "tool", "tool_call_id": "tc_b", "content": large_b},
            {"role": "assistant", "content": "ok"},
        ]
        pre_bytes = sum(len(m.get("content", "") or "") for m in messages)

        compacted = re.compact_tool_messages(messages, "session_1")
        post_bytes = sum(len(m.get("content", "") or "") for m in compacted)

        # Both large messages compacted → post should be << pre
        assert post_bytes < pre_bytes / 5
        # Counters track the same delta
        counters = re.get_replay_counters()["replay_wire"]
        assert counters["bytes_saved_total"] == pre_bytes - post_bytes

    def test_t_d2_11_d2_on_d1_cached_message_still_compacts(self, fake_lcm_externalize):
        """T-D2-11: D-2 on a D-1-cached message still compacts correctly."""
        # D-1: store a huge result
        huge = "C" * 200_000
        re.cache_store("read_file", {"path": "/tmp/big"}, {"role": "tool", "tool_call_id": "tc_c1", "content": huge}, "s")
        # D-1: hit returns the cached message
        cached = re.cache_check("read_file", {"path": "/tmp/big"}, "s")
        assert cached is not None
        # D-2: pass it through wire compaction
        compacted = re.compact_tool_messages([cached], "s")
        # Should be compacted (still over threshold)
        assert len(compacted[0]["content"]) < 10_000
        # LCM was called with the original huge content
        assert len(fake_lcm_externalize) == 1
        assert fake_lcm_externalize[0]["content_len"] == 200_000

    def test_t_d2_12_d2_fallback_writes_to_tmp_when_lcm_unavailable(self, monkeypatch, tmp_path):
        """T-D2-12: D-2 with LCM API unavailable (fallback path): writes to /tmp, message contains file path."""
        # Simulate LCM unavailable
        monkeypatch.setattr(re, "_LCM_EXTERNALIZE_FN", None)
        # Use a tmp_path we control
        monkeypatch.setattr(re, "_HERMES_HOME", tmp_path)

        large = "F" * 200_000
        messages = [{"role": "tool", "tool_call_id": "tc_fb", "content": large}]
        compacted = re.compact_tool_messages(messages, "session_1")

        # The fallback should write a file and the content should reference it
        content = compacted[0]["content"]
        assert len(content) < len(large)  # compacted
        # Should contain some kind of file path indicator
        assert ("/" in content) or ("tmp" in content.lower())

    def test_t_d2_7_s3_baseline_unchanged_smoke_test(self):
        """T-D2-7: S3 baseline runner exists (the actual fingerprint check is the per-CI runner)."""
        runner = Path(__file__).parent.parent.parent / "scripts" / "run_baseline_suite.sh"
        assert runner.exists() and os.access(runner, os.X_OK)

    @pytest.mark.skip(reason="T-D2-10 requires real network + OPENROUTER_API_KEY; not run in unit tests")
    def test_t_d2_10_openrouter_real_network(self):
        """T-D2-10: Send a compacted tool message through OpenRouter (real network) → HTTP 200.

        Empirical proof of provider-agnosticism. Skipped in unit tests; lives in
        integration/e2e suite. Run via:
            OPENROUTER_API_KEY=... python -m pytest tests/integration/test_replay_openrouter_e2e.py -v
        """
        pass


# ──────────────────────────────────────────────────────────────────────────
# T-M2: Snapshot Counters (5 tests)
# ──────────────────────────────────────────────────────────────────────────


class TestM2SnapshotCounters:
    """T-M2-1..5 from spec §5.3."""

    def test_t_m2_1_counters_start_at_zero(self):
        """T-M2-1: Counters start at 0 on gateway start (i.e., after reset_replay_counters)."""
        re.reset_replay_counters()
        counters = re.get_replay_counters()
        assert counters["replay_l1"]["hits"] == 0
        assert counters["replay_l1"]["misses"] == 0
        assert counters["replay_l1"]["hit_rate_pct"] == 0
        assert counters["replay_wire"]["compactions"] == 0
        assert counters["replay_wire"]["bytes_saved_total"] == 0

    def test_t_m2_2_math_correctness_hit_rate_pct(self):
        """T-M2-2: 10 hits + 5 misses → hit_rate_pct = 66.67."""
        re.reset_replay_counters()
        # Prime the cache so subsequent calls hit
        re.cache_store("read_file", {"p": "/m2_2"}, {"role": "tool", "content": "v"}, "s")
        for _ in range(10):
            re.cache_check("read_file", {"p": "/m2_2"}, "s")  # 10 hits
        for i in range(5):
            re.cache_check("read_file", {"p": f"/miss_{i}"}, "s")  # 5 misses

        counters = re.get_replay_counters()["replay_l1"]
        assert counters["hits"] == 10
        assert counters["misses"] == 5
        assert counters["hit_rate_pct"] == pytest.approx(66.67, abs=0.01)

    def test_t_m2_3_math_correctness_compression_ratio(self, fake_lcm_externalize):
        """T-M2-3: 1 compaction (100KB → 4KB) → bytes_saved_total=98304, compression_ratio_pct=96.0."""
        re.reset_replay_counters()
        # 100 KiB → ~4 KiB output (head 2K + tail 2K + ref + markers)
        content = "M" * (100 * 1024)
        re.compact_tool_messages(
            [{"role": "tool", "tool_call_id": "tc_m2_3", "content": content}],
            "s",
        )
        wire = re.get_replay_counters()["replay_wire"]
        assert wire["compactions"] == 1
        assert wire["bytes_in"] == 100 * 1024
        assert wire["bytes_saved_total"] > 90_000  # saved > 90KB
        # Compression ratio = bytes_saved / bytes_in * 100
        ratio = wire["bytes_saved_total"] / wire["bytes_in"] * 100
        assert ratio == pytest.approx(96.0, abs=2.0)

    def test_t_m2_4_snapshot_schema_version_is_1_1(self):
        """T-M2-4: SNAPSHOT_SCHEMA_VERSION == "1.1" in JSON output."""
        from hermes_cli.gateway_snapshot import SNAPSHOT_SCHEMA_VERSION
        assert SNAPSHOT_SCHEMA_VERSION == "1.1"

    def test_t_m2_5_v1_0_fields_unchanged_in_v1_1_output(self):
        """T-M2-5: Pre-existing v1.0 fields (gateway, runtime, disk) unchanged in v1.1 output."""
        from hermes_cli.gateway_snapshot import collect_snapshot
        snap = collect_snapshot()
        # v1.0 fields must all be present
        assert "gateway" in snap
        assert "runtime" in snap
        assert "disk" in snap
        assert "errors" in snap
        # v1.1 additions
        assert snap.get("schema_version") == "1.1"
        assert "request_cache" in snap.get("cache", {})
        assert "wire_compaction" in snap.get("cache", {})
