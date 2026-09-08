# Architecture Decision Records

## 2026-07-13: Scope plugin manager state by Hermes home/profile (keyed cache)

Status: Accepted

Context:
Hermes supports multiple profiles via different Hermes home directories.
Homes are switched two ways in a running process: the `HERMES_HOME`
environment variable (single-profile CLI/gateway processes), and the
context-local `set_hermes_home_override()` (`hermes_constants.py`), which
the multiplexed gateway worker (`gateway/run.py`'s `_profile_scope`) and
subagent/embedded callers use to serve several profiles from one
long-lived process. The override is a `ContextVar` and deliberately does
**not** mutate `os.environ`, since that would leak one profile's home
into every other concurrent task in the same process.

The plugin manager was a process-global single-slot singleton
(`_plugin_manager`). User-installed plugins are discovered from
`get_hermes_home() / "plugins"`, and context-engine plugins (e.g.
`hermes-lcm`) capture profile-scoped state — such as the LCM database
path — at registration time. A single-slot cache meant:

1. Switching homes via `set_hermes_home_override()` was invisible to a
   naive "did `HERMES_HOME` change" check, so the singleton silently kept
   serving the first profile's manager to every other profile in the
   process.
2. Even when a fresh `PluginManager` *was* created for a new home, plugin
   modules are imported into `sys.modules` as `hermes_plugins.<slug>` by
   `_load_directory_module`, and only that top-level module was ever
   replaced. A same-slug plugin's *relative* imports
   (`from . import state`) are cached separately under
   `hermes_plugins.<slug>.<submodule>`, and Python's import machinery
   resolves those from `sys.modules` first — so a profile switch could
   silently keep serving a previous profile's already-imported submodule
   code/state instead of re-executing the new profile's plugin.

Decision:
- Replace the single-slot singleton with a cache keyed on the *resolved*
  Hermes home path (`_plugin_managers_by_home: Dict[Path, PluginManager]`).
  `get_plugin_manager()` resolves the current home via `get_hermes_home()`
  (which itself already consults `get_hermes_home_override()` before
  `os.environ`), so both the env-var and context-local override paths are
  covered uniformly.
- `_plugin_manager` (the old single-slot name) is kept as a thin "last
  manager returned" pointer purely for backward compatibility with
  existing test code that does
  `monkeypatch.setattr(plugins_mod, "_plugin_manager", some_manager)`.
  When that name is monkeypatched to a manager the keyed cache doesn't
  know about, `get_plugin_manager()` treats it as an explicit injection
  and adopts it into the cache under the *current* resolved home, rather
  than discarding it.
- Both `PluginManager._load_directory_module` (initial/`force=True`
  reload within the same home) and the shared `_clear_plugin_submodules`
  helper (profile switch / test teardown) evict `sys.modules[module_name]`
  **and every name prefixed with `module_name + "."`** before a plugin
  slug is (re-)imported, so relative-import submodules can never survive
  a reload or a home switch.
- Test isolation (`tests/conftest.py`'s `_hermetic_environment` fixture)
  calls a new `_reset_plugin_managers_for_tests()` helper that drops the
  entire keyed cache and purges every plugin submodule from `sys.modules`
  between tests, instead of only resetting the single-slot pointer.

Consequences:
- Per-profile LCM instances (and any other context-engine plugin) use
  their own `{home}/lcm.db` regardless of whether the profile switch went
  through `HERMES_HOME` or `set_hermes_home_override()`.
- Plugin discovery remains cached within a profile for normal
  performance, and re-entering a previously-seen profile reuses its
  cached manager instead of rebuilding from scratch.
- Sequential *and* interleaved profile switching — in tests, the gateway
  multiplexer worker, or embedded callers using the context-local
  override — no longer leaks context-engine state, plugin module state,
  or stale relative-import submodules across profiles.
- Regression coverage exercises the real production path
  (`set_hermes_home_override()`) rather than only the env-var path, and
  includes a dedicated relative-import leak test.

## 2026-09-06: Unify the Hermes cache layer into one tiered runtime

Status: Proposed (SPEC v1 written; awaiting sign-off)

Context:
The Hermes project has 13 distinct cache/memory subsystems, but the local
data-caching tier was fragmented and partially dead:

- `agent/_cache.py` (1,594 lines) already defines the right abstractions — a
  `TieredCache` Protocol, `InProcessLRUCache` (L1), `FlatFileCache` (L2),
  `ShardedFileCache` (L3), `RedisCache`/`DiskCache` stubs, a `TieredCacheRouter`,
  `build_cache_from_config()`, `is_l1/l2/l3_enabled()`, and a config reader —
  but **none of the general tiered router has a production caller**. It is
  dead code.
- Live consumers were ad-hoc and disjoint: `replay_economy.py` and
  `models_dev.py` each instantiate a bare `InProcessLRUCache`; `model_metadata.py`
  rolls its own dict + disk JSON; `secret_sources/_cache_bridge.py` runs an
  isolated L1+L2 router of its own.
- Adjacent subsystems (Anthropic prompt caching, context compression, tiered
  system-prompt blocks, `memory_monitor`/`memory_status`/`agent_cache_pressure`
  sidecars) are separate concerns and must not be duplicated by this tier.

Decision:
- Make `agent/_cache.py` the **single source of truth** for all local data
  caching. Every current consumer is re-wired to obtain its tier through
  `get_cache_router()` / `build_cache_from_config()` instead of instantiating a
  bare `InProcessLRUCache` or rolling ad-hoc dicts.
- Collapse the `secret_sources` bridge **into** the router as a
  **namespace-scoped tier** (secrets get their own L2/L3 partition, not their
  own engine).
- **`config.yaml` is the sole configuration surface.** Feature gates, sizing,
  TTLs, backends, magic numbers, and booleans live in a `cache:` block
  (`cache.l1`, `cache.l2`, `cache.l3`, `cache.namespaces`, `cache.pressure`,
  `cache.observe`). All current `DEFAULT_L*` constants move into that block as
  defaults; `config_defaults.py` retains them only as schema seed values.
- **Environment variables are narrowed** to identity/secret operands only
  (e.g. a Redis URL carrying credentials). The existing `HERMES_CACHE_*`
  override path is repurposed for that subset rather than acting as the
  primary config channel.
- Side-cars stay operational, not data-path: the router emits
  hit/miss/size/eviction counters consumed by `agent_cache_pressure.py`; a
  single `CacheMetrics` surface is latched by `memory_status.py`; and the
  router degrades gracefully (L1 only) with a circuit state when an L2/L3
  backend is unreachable (borrowed from `cachka`).

Consequences:
- One router, one config surface, one observability surface. New caches that
  arrive later register through the same router and automatically inherit
  tiering, eviction, TTL, and metrics.
- L1 upgrades from the current striped-LRU to a Caffeine-style **W-TinyLFU**
  adaptive eviction (borrowed from `theine`); L2/L3 follow the
  `diskcache` blueprint (mmap ring + `FanoutCache` sharding, tag metadata,
  vacuum, multiprocess-safe locking).
- The secrets cache keeps its isolation semantics but gains the unified
  eviction/TTL/observability contract.
- This is a **full rebuild of the wiring**, not a flag flip — the tiered router
  currently has zero callers, so enabling it is a code change, not a config
  edit.

References consulted (cloned to `~/.references`):
- `diskcache` (Apache-2.0) — L2/L3 blueprint.
- `theine` (BSD-3) — L1 W-TinyLFU upgrade.
- `cachka` (MIT) — router + circuit breaker + observability blueprint.
