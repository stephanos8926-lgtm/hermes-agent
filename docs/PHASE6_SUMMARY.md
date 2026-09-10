# Phase 6 Summary — Cache Migration Complete

## Date
2026-09-10

## What Was Done

### Phase 6a: model_metadata.py — _CONTEXT_CACHE_L1 → Unified Router
- Replaced bare `InProcessLRUCache` singleton (`_CONTEXT_CACHE_L1`) with `get_cache_router()` directly
- Removed `global _CONTEXT_CACHE_L1`, lazy-init try/except block, and `Optional["InProcessLRUCache"]` type annotation
- All `l1.get()/put()/invalidate()` calls now flow through `TieredCacheRouter`, gaining L2/L3 persistence and circuit breaker protection automatically
- Net reduction: -27 lines (10 added, 37 removed)

### Phase 6b: moa_loop.py — _runtime_cache
- **Decision: NOT migrated.** `_runtime_cache` uses per-key TTL (300s) for credential rotation semantics. The unified router's L1 has global TTL only — incompatible without architectural change. Left as-is; this is a deliberate scope boundary.

### Phase 6c: hermes_cli/models.py — _pricing_cache
- Added `_MAX_PRICING_CACHE_ENTRIES = 200` constant
- Added `_evict_pricing_cache_if_needed()` — evicts oldest entries (FIFO via dict insertion order) when over limit, also cleans up stale `_pricing_cache_retry_after` entries
- Called after every `_cache_catalog()` write
- Prevents unbounded memory growth in long-running processes (gateway, desktop backend run for weeks)

### Test Infrastructure
- Added `TieredCacheRouter.clear()` method to `_cache.py` for test isolation
- Added `_clear_cache_router` autouse fixture in `tests/conftest.py` to reset router singleton between tests
- This prevents cross-test contamination from the shared singleton

## Test Results
- `test_tiered_cache.py`: **148 passed** ✅
- `test_model_metadata.py`: 107 passed, 4 failed (all 4 are **pre-existing**, confirmed via stash comparison)
- `test_sale_pricing.py` + `test_api_key_providers.py`: 111 passed, 1 failed (pre-existing `DEEPINFRA_API_KEY` KeyError)

## Commits in This Session
1. `26bcd758` — feat(cache): migrate model_metadata context cache to unified router
2. `c8efb671` — feat(cache): add bounded eviction to _pricing_cache (200-entry cap)
3. `91053852` — test(cache): add L1 benchmark script — TinyLFU vs LRU throughput comparison
4. `f64b932e` — docs(cache): update SPEC and plan with implementation status — all 5 phases complete

## Benchmarks (scripts/bench_cache.py)
| Cache | Get Hit (ms) | Get Miss (ms) | Set (ms) |
|-------|-------------|---------------|----------|
| TinyLFU (1K entries) | 2.4 | 1.9 | 3.2 |
| LRU (1K entries) | 1.8 | 1.7 | 5.1 |
| TinyLFU (10K entries) | 26.9 | 20.0 | — |
| LRU (10K entries) | 25.6 | 17.9 | — |

TinyLFU sets ~1.6× faster than LRU; get latency comparable.

## Current State
- Branch: `feat/cache-phase1-hardening`
- All 5 phases of the cache infrastructure plan are complete
- Total commits since plan start: 9
- No new test failures introduced
