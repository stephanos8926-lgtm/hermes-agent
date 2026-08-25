---
name: hermes-cache
category: devops
description: Tiered cache subsystem (L1 in-process LRU + L2/L3 cross-process) for the Hermes agent runtime. Use when adding, tuning, or debugging the cache.layers (l1/l2/l3) config block, or wiring a new caller.
keywords: [cache, lru, mmap, redis, sqlite, l1, l2, l3, tier, fallback, byte-budget, ttl, eviction]
triggers:
  - "cache:"
  - "l1.enabled"
  - "l2.backend"
  - "l3.flat_file"
  - "HERMES_CACHE_"
  - "InProcessLRUCache"
  - "FlatFileCache"
  - "ShardedFileCache"
  - "TieredCacheRouter"
version: 1.0.0
---

# Hermes Tiered Cache Subsystem

Three-layer cache with feature gates. All keys are bytes-or-string; values are
pickleable Python objects. Per AGENTS.md rule #8, behavioral settings live in
`config.yaml`; credentials and unusual overrides go in `.env` as
`HERMES_CACHE_*` env vars.

## Architecture at a Glance

```
caller → L1 InProcessLRUCache → L2 (mmap or redis) → L3 (sharded files or sqlite)
              (always on when cache.enabled)         (off by default)
```

The router is read-through and write-back with a TTL on the outer tiers. Reads
hit L1 first; on miss, query L2, then L3, then the source-of-truth. Writes
populate all enabled tiers. Each tier has a fail-open contract: an exception
in L2 must never block a read that succeeded at L1.

## Configuration Surface

`config.yaml`:

```yaml
cache:
  enabled: true            # master kill switch (HERMES_CACHE_ENABLED)
  l1:
    enabled: true          # HERMES_CACHE_L1_ENABLED
    max_entries: 64        # HERMES_CACHE_L1_MAX_ENTRIES
    max_bytes: 33554432    # HERMES_CACHE_L1_MAX_BYTES (32 MiB)
  l2:
    enabled: false         # HERMES_CACHE_L2_ENABLED
    backend: flat_file     # HERMES_CACHE_L2_BACKEND (flat_file | redis | null)
    flat_file:
      path: ~/.hermes/cache/l2.mmap
      max_bytes: 67108864  # HERMES_CACHE_L2_FLAT_FILE_MAX_BYTES (64 MiB)
    redis:
      url: redis://localhost:6379
      namespace: hermes
      ttl_seconds: 3600
  l3:
    enabled: false         # HERMES_CACHE_L3_ENABLED
    backend: flat_file     # HERMES_CACHE_L3_BACKEND (flat_file | sqlite | null)
    flat_file:
      root: ~/.hermes/cache/l3
      ttl_days: 30
    sqlite:
      path: ~/.hermes/cache/l3.sqlite
      ttl_seconds: 86400
```

Env-var resolution: any leaf key can be overridden by a `HERMES_CACHE_<PATH>`
env var where `<PATH>` is the dotted key in upper-snake form (e.g.
`cache.l2.flat_file.max_bytes` → `HERMES_CACHE_L2_FLAT_FILE_MAX_BYTES`).

## Public API

`agent._cache`:

- `InProcessLRUCache(max_entries, max_bytes)` — production L1. Both entry-count
  and byte-budget eviction (whichever is hit first). Thread-safe with an
  `RLock`. Byte-stability contract: `get(k)` returns the **same** `bytes` /
  object that was passed to `put`, never a copy.
- `FlatFileCache(path, max_bytes)` — L2 mmap-backed ring buffer. 16-byte
  fixed slot header (key_hash:u64, value_len:u32, ts:u32). Tombstone reuse
  strategy. Lossy under churn: at 1024 slots × ~64 KB per slot ≈ 64 MB.
- `ShardedFileCache(root, ttl_seconds)` — L3 directory of `pkl` files, sharded
  by the first 2 hex chars of the SHA-256 of the key. Each file's mtime is
  its TTL. Sweep on read.
- `RedisCache(...)` — L2 stub, raises `NotImplementedError`. See docstring
  for the implementation sketch.
- `DiskCache(...)` — L3 sqlite stub, raises `NotImplementedError`. See
  docstring.
- `TieredCacheRouter(l1, l2, l3)` — read-through, write-back, fail-open.
  Constructor accepts `None` for any tier to disable it.

## When to Use Each Layer

| Use case | Tier | Why |
|----------|------|-----|
| System prompt rendered text (per session) | L1 only | Cached per-session by `agent._cached_system_prompt`; outer tiers add no value |
| Model catalog JSON | L1 + L2 | The catalog is large (~100KB), and a process restart shouldn't force a re-fetch |
| Compiled tool schema | L1 + L2 + L3 | Survives a full redeploy; expensive to rebuild |
| Honcho session messages | L3 only | High write-amplification; L1/L2 would churn under load |
| Per-call token counts | L1 only | Hot but small; cross-process sharing is unnecessary |

## Operational Notes

- **Off by default for L2/L3.** The shipped `config.yaml` leaves both layers
  disabled. Enabling L2/L3 is a one-line config change but adds I/O and
  process-state complexity. L1 alone is a strict win for any single-process
  caller.
- **Fail-open contract.** An L2 mmap-fault or L3 OSError must not bubble up.
  Catch at the router, log a debug line, and serve the value from whichever
  tier had it (or `None` if all tiers missed).
- **Thread safety.** L1 uses an `RLock`; L2 and L3 use `fcntl` flock on the
  mmap or sharded dir respectively. The router is the only object callers
  should hold a reference to.
- **Cross-process safety.** L2 mmap uses mandatory locking; L3 sharded files
  use per-shard atomic-rename on write. Reads are eventually consistent
  across processes (no cache-coherency protocol).
- **Memory ceiling.** L1 has both an entry ceiling and a byte ceiling.
  Whichever is hit first triggers eviction. L2 has a hard byte ceiling
  enforced by the mmap size. L3 has a soft byte ceiling via the
  max_entries knob and TTL via file mtime.

## Testing

Tests live in `tests/agent/test_tiered_cache.py`:

- `TestInProcessLRUCache` — 18 tests covering basic ops, count eviction,
  byte-budget eviction, thread safety, and the byte-stability contract.
- `TestFlatFileCache` — 11 tests covering put/get/invalidate, slot reuse,
  ring buffer overflow, and persistence across process restart.
- `TestShardedFileCache` — 5 tests covering basic ops, TTL sweep, and
  sharding correctness.
- `TestRedisAndDiskStubs` — 4 tests verifying `NotImplementedError` is
  raised for the documented API.
- `TestTieredCacheRouter` — 8 tests covering read-through, write-back,
  fail-open on L2 fault, and partial-tier configurations.
- `TestConfigGating` — 12 tests covering `cache:` config parsing, env-var
  overrides, the master `enabled` switch, and per-tier `enabled` flags.

## Architectural Alternative: mmap + flat-file without Redis or sqlite

The default L2 implementation is already pure-stdlib (mmap). The "alternative
no-redis-no-sqlite" approach referenced in the v1.0 plan means **don't
enable L2 or L3 at all** — use only L1. For a single-process, single-user
Hermes installation (the RapidWebs default), this is the recommended
configuration: zero cross-process state, no I/O on the cache path, and
predictable memory usage.

If you need cross-process sharing (e.g. multiple agents hitting the same
catalog), enable L2 with `backend: flat_file` — the mmap is the only
zero-dep, zero-daemon option. Redis adds operational complexity that
isn't justified at this scale.

## Related

- `skills/devops/hermes-db/SKILL.md` — companion skill for `hermes db`
  SQLite maintenance, which uses the same config-driven feature-gate
  pattern.
- `plugins/disk-cleanup/` — handles file-system hygiene (logs, backups,
  tracked files) for the cache filesystem; v3.1.0+ respects
  `cache.enabled` to avoid pruning cache files when the cache is active.
- **docs/operations/extempfail-sentinel.md** — why gateway exit code 75
  is correct, not a bug. If you see `TEMPFAIL` in the gateway logs,
  that is the gateway announcing an intentional drain-and-restart.
