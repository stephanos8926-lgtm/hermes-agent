# Cache Infrastructure — Unified Layered Cache (SPEC v1)

Status: Proposed
Author: Lucien (RapidWebs)
Date: 2026-09-06

## 1. Purpose

Replace the current fragmented, partially-dead caching landscape with a
single, cohesive, spec'd, configurable, and operational multi-tier cache
layer that:

1. serves as the **core runtime cache** of the Hermes Agent project (not a
   bolt-on),
2. exposes its full configuration surface through `config.yaml` (with only
   truly-secret/identity items surfaced as environment variables),
3. is designed from the ground up against well-known, production-grade
   caching systems, and
4. slots cleanly into the most appropriate Hermes subsystem for each tier.

## 2. Current-state audit (verified, not assumed)

### 2.1 The tier engine — `agent/_cache.py` (1,594 lines)

Defines the correct *abstractions* but is **not wired into production**.

| Symbol | Line | Purpose | Prod-caller? |
|---|---|---|---|
| `TieredCache` (Protocol) | `:179` | `get`/`put`/`invalidate` contract | — |
| `InProcessLRUCache` | `:423` | **L1** striped in-memory LRU (16 shards) | ✅ 3 ad-hoc uses |
| `_LRUShard` | `:362` | shard internals | internal |
| `FlatFileCache` | `:1059` | **L2** mmap ring + `flock` cross-process | ❌ none |
| `ShardedFileCache` | `:1177` | **L3** 2-level hash dir + mtime TTL | ❌ none |
| `RedisCache` | `:980` | **L2 redis** (stub) | ❌ none |
| `DiskCache` | `:1397` | **L3 sqlite** (stub) | ❌ none |
| `TieredCacheRouter` | `:1469` | L1→L2→L3 cascade | ❌ none |
| `_flat_file_ring` / `_sharded` internals | `:656`–`:1176` | implementation | internal |
| `build_cache_from_config()` | `:1558` | build tiers from config | ❌ **dead** |
| `is_cache_enabled` / `is_l1/l2/l3_enabled` | `:334`–`:352` | gate helpers | ❌ **dead** |
| `_read_cache_config()` | `:246` | read config.yaml + env | ❌ **dead** |
| `_env_override()` | `:217` | `HERMES_CACHE_<PATH>` lookups | ❌ **dead** |

Defaults (`:159`–`:172`): L1 = 64 entries / 32 MiB; L2 = 64 MiB flat mmap at
`~/.hermes/cache/l2.mmap`; L3 = 30-day TTL sharded dir at `~/.hermes/cache/l3`.

### 2.2 Live consumers (disjoint, ad-hoc)

| Consumer | Tier used | Data cached | Wiring |
|---|---|---|---|
| `hermes_cli/replay_economy.py` | bare L1 | tool results keyed `sha256(tool+args)`, denylist-skipped | instantiates `InProcessLRUCache` directly |
| `agent/models_dev.py` | bare L1 | model catalog | imports `InProcessLRUCache` directly |
| `agent/model_metadata.py` | ad-hoc dict + disk JSON | model metadata | **not** tiered at all |
| `agent/secret_sources/_cache_bridge.py` | isolated L1+L2 | secret-source lookups | separate, self-contained router |

### 2.3 Adjacent caching/memory subsystems (out of scope for *this* tier, but
must not be duplicated by it)

- `agent/prompt_caching.py` + `prompt_cache_scope.py` + `prompt_cache_boundary.py`
  — **provider-side** Anthropic prompt caching (different concern: alters the
  upstream API payload, not local data).
- `agent/context_compressor.py` — context compression (different: LLM call).
- `agent/system_prompt.py` — tiered system-prompt block assembly.
- `gateway/memory_monitor.py`, `memory_status.py`, `agent_cache_pressure.py` —
  **side-car pressure monitors** (these are the natural runtime owners of the
  eviction/tuning signal this layer should expose).
- `tools/memory_tool.py`, `tools/registry.py` — their *own* small caches
  (tool-discovery verdict, prefix invariant); candidate *consumers* of the
  unified tier, not part of the engine.

## 3. Target architecture

### 3.1 One cohesive unit

`agent/_cache.py` becomes the **single source of truth** for all local data
caching. Every current consumer (`replay_economy`, `models_dev`,
`model_metadata`, `secret_sources`) is re-wired to obtain its tier through
`get_cache_router()` / `build_cache_from_config()` instead of instantiating a
bare `InProcessLRUCache` or rolling ad-hoc dicts. The `secret_sources` bridge
is collapsed *into* the router as a **namespace-scoped tier** (secrets get
their own L2/L3 partition, not their own engine).

New caches that arrive later are registered through the same router and
automatically inherit tiering, eviction, TTL, and observability.

### 3.2 Tier responsibilities (unchanged semantics, hardened impl)

| Tier | Backend | Purpose | Eviction | TTL |
|---|---|---|---|---|
| L1 | in-process memory | hot, per-process | W-TinyLFU (upgrade from striped-LRU) | optional, per-key |
| L2 | disk (mmap flat-file) default; Redis optional | cross-process/cross-restart, small | ring/segment LRU | seconds–hours |
| L3 | disk (sharded dir) default; SQLite optional | cold, large | sharded-capacity | days |

### 3.3 Side-cars (operational, not data-path)

- **Pressure/eviction signal**: the router emits hit/miss/size/eviction
  counters consumed by `agent_cache_pressure.py`; pressure thresholds tune L1
  growth at runtime (memory-constrained workstation vs server).
- **Observability**: a single `CacheMetrics` (hits, misses, bytes in-flight,
  per-tier latency, evictions) surface, latched by `memory_status.py`.
- **Graceful degradation**: if L2/L3 backend is unreachable, the router
  degrades to L1 and records a circuit state (borrowed from `cachka`).

## 4. Configuration surface (mandate)

- **`config.yaml` is the only place** feature gates, sizing, TTLs, backends,
  magic numbers, and booleans live. A `cache:` block (`cache.l1`, `cache.l2`,
  `cache.l3`, `cache.namespaces`, `cache.pressure`, `cache.observe`) fully
  drives `build_cache_from_config()`.
- **Environment variables** remain only for: `HERMES_HOME`-adjacent identity,
  and *with extreme restraint* a handful of secret-like operands (e.g. a Redis
  URL containing credentials). The current `HERMES_CACHE_*` override path is
  *narrowed* to that identity/secret subset rather than being the primary
  config channel.
- All current `DEFAULT_L*` constants move into `config.yaml` defaults
  (single source), with `config_defaults.py` retaining them only as schema
  seed values.

## 5. Reference projects (cloned to `~/.references`)

| Project | License | What we borrow |
|---|---|---|
| `diskcache` | Apache-2.0 | SQLite+mmap persistence, `FanoutCache` sharding, LRU/LFU policies, tag metadata, `vacuum`, multiprocess-safe file locking → **L2/L3 blueprint** |
| `theine` | BSD-3 | Caffeine-style **W-TinyLFU** adaptive eviction, hierarchical timer-wheel TTL → **L1 upgrade** |
| `cachka` | MIT | explicit L1/L2/L3 composition, **circuit breaker + graceful degradation + observability + encryption**, namespace isolation → **router + side-car blueprint** |

## 6. Non-goals (this SPEC)

- Provider-side prompt caching (separate concern; audited, not merged here).
- Context compression and LM call scheduling.
- Distributed/replicated caching (server cluster); single-node-first, with
  Redis only as an optional L2 backend.

## 7. Out of scope for the tier but audited (future SPECs)

- Performance/memory/stability optimization of Hermes beyond the cache layer
  (indexed separately; see `docs/specs/` follow-ups).