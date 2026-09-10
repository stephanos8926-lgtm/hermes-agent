# Plan: Cache Infrastructure -- New Tiered Work (v2.0, Phased)

Date: 2026-09-06
Spec: docs/specs/cache-infrastructure-unified-v1.md
Synthesis: docs/plans/cache-synthesis-v2.md
Mode: MEDIUM
Branch: main (phased commits; merge after Plan #1 sign-off)

## Confidence Check

v1 audit findings G1, G2, G3, G7, G8 corrected this plan. The general tiered
router (`TieredCacheRouter`, `build_cache_from_config()`,
`is_l1/l2/l3_enabled()`, `ShardedFileCache`) is **dead code** -- verified in
both the working tree and the temp fork tip (`55152dd2a2`). So this plan
*activates and hardens* an existing skeleton, not invents one. Reference
projects (`diskcache`, `theine`, `cachka`) give battle-tested patterns to copy.

**Critical re-ordering (from G7):** the `cache:` config block does not yet
exist. Phase 0 now builds it **first**, because every consumer re-wire in
Plan #1 depends on `get_cache_router()` reading a real config block.

## Scope

- **G1 fix**: add `get_cache_router()` -- a lazy singleton wrapper around
  `build_cache_from_config()`. This is the single entry point every consumer
  calls. (v1 erroneously referenced a function that does not exist.)
- **L1 upgrade**: striped-LRU → Caffeine-style **W-TinyLFU** adaptive
  eviction with hierarchical timer-wheel TTL (`theine` port).
- **L2/L3 activation**: `FlatFileCache` (mmap ring + `flock`) and
  `ShardedFileCache` (2-level hash dir + mtime TTL) move from stub to real,
  with `diskcache`-style `FanoutCache` sharding, tag metadata, `vacuum`.
- **Router + side-cars**: `TieredCacheRouter` becomes the live singleton;
  circuit breaker + graceful degradation (L1-only fallback); single
  `CacheMetrics` surface latched by `memory_status.py`; eviction/pressure
  signal emitted to `agent_cache_pressure.py`.
- **Config surface (Phase 0, moved first)**: `cache:` block in
  `config_defaults.py` + `config.yaml`; `DEFAULT_L*` constants migrate into
  it; `HERMES_CACHE_*` env vars narrow to identity/secret operands.

## Phasing (5 phases, linear dependencies)

```
Phase 0 -- Config surface FIRST: cache: block, DEFAULT_L* migration, env-var
          narrowing, get_cache_router() singleton   <-- NEW ORDER (was Phase 4)
   |
   v
Phase 1 -- Skeleton hardening: formal pytest for tier classes, circuit-breaker
          stub, metrics scaffolding, tuning docs
   |
   v
Phase 2 -- L1 W-TinyLFU upgrade (theine port, timer-wheel TTL, admission filter)
   |
   v
Phase 3 -- L2/L3 activation (diskcache-style sharding, mmap ring, vacuum)
   |
   v
Phase 4 -- Router wiring: TieredCacheRouter live singleton, circuit breaker,
          graceful degradation, CacheMetrics surface, pressure signal,
          latch to memory_status.py
   |
   v
Phase 5 -- Full validation: config round-trip, targeted pytest, docs
```

| Phase | Scope | Files touched | Est. | Gate |
|---|---|---|---|---|
| 0 | **Config surface first**: `cache:` block in `config_defaults.py` + `config.yaml`, `DEFAULT_L*` migration, `HERMES_CACHE_*` narrowing, `get_cache_router()` singleton | `hermes_cli/config_defaults.py`, `~/.hermes/config.yaml`, `agent/_cache.py` | 0.5d | config round-trip + `get_cache_router()` returns a router |
| 1 | Skeleton hardening: formal pytest for tier classes, circuit-breaker stub, metrics scaffolding, tuning docs | `agent/_cache.py`, tests, docs | 0.5d | tier-class tests pass |
| 2 | L1 W-TinyLFU: `theine` port, hierarchical timer-wheel TTL, admission filter | `agent/_cache.py` | 1.0d | eviction-adoption tests pass |
| 3 | L2/L3: `FlatFileCache` mmap ring + `flock`, `ShardedFileCache` hash dir + TTL, `FanoutCache` sharding, tag metadata, `vacuum` | `agent/_cache.py` | 1.5d | L2/L3 read/write/evict tests pass |
| 4 | Router wiring: `TieredCacheRouter` live singleton, circuit breaker, graceful degradation, `CacheMetrics` surface, pressure signal to `agent_cache_pressure.py`, latch to `memory_status.py` | `agent/_cache.py`, `gateway/agent_cache_pressure.py`, `gateway/memory_status.py` | 1.0d | router cascade + degrade tests pass |
| 5 | Full validation: config round-trip, targeted pytest, docs | `hermes_cli/config_defaults.py`, `~/.hermes/config.yaml`, docs | 0.5d | config round-trip + full targeted pytest |

Total ~5.0d. Phases 1-4 cannot parallelize (they share `agent/_cache.py` and
the router contract). Phases are intentionally small for reviewability.

## Why Phase 0 is first (was Phase 4 in v1)

v1 put config surface last. That is backwards: Plan #1's consumer re-wires
call `get_cache_router()`, which reads the `cache:` block. Wiring to a
nonexistent block is how you get runtime `KeyError`s in production. Building
the block first makes every subsequent step testable against real config.

## Execution Rules

- **TDD per phase**: tests first, red-green-refactor. Each phase ends with
  `ruff check`, `py_compile`, and targeted pytest -- no full suite on the 4GB
  workstation host.
- **Reference-first implementation**: before writing each phase, re-read the
  matching mechanism in `~/.references/theine` (L1), `~/.references/diskcache`
  (L2/L3), and `~/.references/cachka` (router/circuit/observability). Copy
  the pattern, adapt to Hermes conventions, never translate line-by-line.
- Each phase is its own commit on `main`. All phases additive behind the
  `cache:` config block, so any single phase can be reverted independently.
- **Feature-flag discipline**: every new behavior defaults to *off* until
  Phase 0 lands the config surface. This keeps the running system stable
  between phases.

## Rollback

Revert the offending phase commit. Because everything is additive behind the
`cache:` block and the router singleton, reverting restores the pre-plan state
(except the hardening/tests from Phase 1, which are pure additions).

## Sign-off Required Before Code

User approval on this v2.0 plan before Phase 0.

---

## Implementation Status (Completed 2026-09-10)

All 5 phases implemented and committed on branch `feat/cache-phase1-hardening`:

| Phase | Commit | Notes |
|-------|--------|-------|
| 0 | `9d7c3bfc91` | `cache:` block in config + `get_cache_router()` singleton |
| 0.5 | `905b416de3` | 4 consumers wired through router |
| 1 | `1182b4378a` | CircuitBreaker, CacheMetrics, config schema |
| 2 | `b6a4e61d7d` | W-TinyLFU via theine, L1 default 256 entries |
| 3 | `8236988db6` | flock, vacuum, estimated_bytes, L2/L3 enabled by default |
| 4 | `a187d4fc0d` | `CacheStatusResponse`, `/api/status` endpoint |
| 5 | `d6b6ff3dfb` | 148 tests pass, config round-trip, e2e, circuit breaker |

**Benchmarks (scripts/bench_cache.py):**
- 1K entries × 256B: TinyLFU hit 2.4ms, miss 1.9ms, set 3.2ms; LRU hit 1.8ms, miss 1.7ms, set 5.1ms
- 10K entries: TinyLFU hit 26.9ms, miss 20.0ms; LRU hit 25.6ms, miss 17.9ms
- TinyLFU sets ~1.6× faster than LRU; get latency comparable

**Audit Candidates for Future Phases:**
1. `agent/moa_loop.py:_runtime_cache` — manual dict+lock+TTL, high call frequency
2. `agent/model_metadata.py:_CONTEXT_CACHE_L1` — partially wired but uses `router._tiers[0]` directly
3. `hermes_cli/models.py:_pricing_cache` — unbounded dict, no eviction
4. `agent/auxiliary_client.py:_client_cache` — HTTP client instances
5. `gateway/run.py:_agent_cache` — large session transcripts, complex eviction