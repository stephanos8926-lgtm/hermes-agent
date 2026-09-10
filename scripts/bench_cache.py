#!/usr/bin/env python3
"""Benchmark: tiered cache L1 performance (in-memory tiers).

Usage:
    python scripts/bench_cache.py [--runs 5] [--items 1000]

Measures per-tier throughput (get/set/miss) for in-memory caches.
Non-invasive — does not require Hermes to be running.
"""
from __future__ import annotations

import argparse
import statistics
import time
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent._cache import InProcessTinyLFUCache, InProcessLRUCache


def benchmark_get_set(
    cache,
    keys,
    *,
    reps: int = 3,
    value_size: int = 256,
) -> dict:
    """Run get/set through a cache and return timing stats."""
    values = {k: ("x" * value_size) for k in keys}
    # Warm: populate all
    for k, v in values.items():
        cache.put(k, v)

    results = {"gets_hit": [], "gets_miss": [], "sets": []}

    for _r in range(reps):
        # Hits
        t0 = time.perf_counter()
        for k in keys:
            cache.get(k)
        results["gets_hit"].append((time.perf_counter() - t0) * 1000)

        # Delete half for misses
        for k in list(keys)[: len(keys) // 2]:
            cache.invalidate(k)
        t0 = time.perf_counter()
        for k in keys:
            cache.get(k)
        results["gets_miss"].append((time.perf_counter() - t0) * 1000)

        # Sets
        t0 = time.perf_counter()
        for k, v in values.items():
            cache.put(k, v)
        results["sets"].append((time.perf_counter() - t0) * 1000)

    def summary(entries):
        if not entries:
            return {}
        avg = statistics.mean(entries)
        p50 = statistics.median(entries)
        p99_idx = min(int(len(entries) * 0.99), len(entries) - 1)
        p99 = sorted(entries)[p99_idx]
        return {"avg_ms": round(avg, 3), "p50_ms": round(p50, 3), "p99_ms": round(p99, 3)}

    return {
        "gets_hit": summary(results["gets_hit"]),
        "gets_miss": summary(results["gets_miss"]),
        "sets": summary(results["sets"]),
    }


def bench_tiny_lfu(n: int, reps: int = 3) -> dict:
    c = InProcessTinyLFUCache(max_entries=max(n * 2, 10_000))
    keys = [f"bench_key_{i}" for i in range(n)]
    return {"class": "InProcessTinyLFUCache", "stats": benchmark_get_set(c, keys, reps=reps)}


def bench_lru(n: int, reps: int = 3) -> dict:
    c = InProcessLRUCache(max_entries=max(n * 2, 10_000), max_bytes=64 * 1024 * 1024)
    keys = [f"bench_key_{i}" for i in range(n)]
    return {"class": "InProcessLRUCache", "stats": benchmark_get_set(c, keys, reps=reps)}


def main():
    parser = argparse.ArgumentParser(description="Tiered cache benchmark (L1 in-memory)")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--items", type=int, default=1000)
    parser.add_argument("--value-size", type=int, default=256)
    args = parser.parse_args()

    print(f"Items: {args.items}, Reps: {args.runs}, Value size: {args.value_size} bytes\n")
    print(f"{'Cache Tier':<30} {'Get Hit (ms)':<20} {'Get Miss (ms)':<20} {'Set (ms)':<20}")
    print("-" * 95)

    benches = [
        bench_tiny_lfu(args.items, reps=args.runs),
        bench_lru(args.items, reps=args.runs),
    ]

    for b in benches:
        s = b["stats"]
        gh = s.get("gets_hit", {})
        gm = s.get("gets_miss", {})
        se = s.get("sets", {})
        print(
            f"{b['class']:<30} "
            f"{gh.get('p50_ms', float('nan')):<20.3f} "
            f"{gm.get('p50_ms', float('nan')):<20.3f} "
            f"{se.get('p50_ms', float('nan')):<20.3f}"
        )

    print("\nDone.")


if __name__ == "__main__":
    main()
