# Plan: Cache Infrastructure -- Overhaul Existing Consumers (v2.0)

Date: 2026-09-06
Spec: docs/specs/cache-infrastructure-unified-v1.md
Synthesis: docs/plans/cache-synthesis-v2.md
Mode: MEDIUM
Branch: main (single commit, additive behind a feature flag)

## Confidence Check

v1 audit findings G1, G4, G5, G6 corrected this plan. The re-wire targets are
**lazy getters**, not constructors, and the entry point is
`get_cache_router()` (added in Plan #2 Phase 0), not `build_cache_from_config()`
(which has no production callers). The secrets bridge is consumed through
`agent/secret_sources/_cache.py` (`bridge_read`/`bridge_write`), so it is
re-routed, not replaced.

## Scope

Re-route the four existing cache consumers through the unified router. No new
tiers are built here -- that is Plan #2. This plan is purely **wiring**, and
every step depends on Plan #2 Phase 0 (router singleton + `cache:` config block)
landing first.

| # | Consumer | Current (verified) | Target |
|---|---|---|---|
| 1 | `hermes_cli/replay_economy.py` | lazy `_request_cache` → bare `InProcessLRUCache` (`:215-237`) | `get_cache_router("replay")` |
| 2 | `agent/models_dev.py` | lazy `_model_catalog_l1` → bare `InProcessLRUCache` (`:55,89,1592-1613`) | `get_cache_router("model_catalog")` |
| 3 | `agent/model_metadata.py` | lazy `_CONTEXT_CACHE_L1` → bare `InProcessLRUCache` (`:1516-1545`), **silent-failure risk** | `get_cache_router("model_metadata")` + H-1 guard |
| 4 | `agent/secret_sources/_cache.py` | `bridge_read`/`bridge_write` → own `TieredCacheRouter` (`:145,245`) | route through `get_cache_router("secrets")` |

## Dependency order

```
Plan #2 Phase 0 (router singleton + cache: block)  <-- MUST land first
   |
   v
Step 1 -- replay_economy re-wire (behavior-preserving, flag-guarded)
   |
   v
Step 2 -- models_dev + model_metadata re-wire
   |
   v
Step 3 -- secret_sources bridge re-route
   |
   v
Step 4 -- H-1 silent-failure guard applied to all 3 lazy getters
   |
   v
Step 5 -- Tests, lint, commit
```

| Step | Files touched | Est. | Gate |
|---|---|---|---|
| 1 | `hermes_cli/replay_economy.py`, tests | 0.5d | replay tests green, no behavior change |
| 2 | `agent/models_dev.py`, `agent/model_metadata.py`, tests | 0.5d | catalog + metadata tests green |
| 3 | `agent/secret_sources/_cache.py`, `agent/secret_sources/_cache_bridge.py`, tests | 0.5d | secret tests green, L2 path verified |
| 4 | `agent/model_metadata.py`, `agent/models_dev.py`, `hermes_cli/replay_economy.py` | 0.5d | construction errors surface, not swallow |
| 5 | `ruff`, `py_compile`, targeted pytest, commit | 0.5d | all gates pass |

Total ~2.5d. Steps 1-3 are independent of each other once Phase 0 lands, so
they can run in parallel. Step 4 depends on 1-3 being complete (it modifies
the same getters).

## Execution Rules

- **Behavior-preserving by default.** Every re-wire is behind a feature flag
  (`cache.enabled` / `cache.namespaces.<name>.enabled`) defaulting to the
  *current* behavior, so a bad re-wire is a one-line revert.
- **Re-wire the getter, not the constructor.** Each consumer has a lazy
  singleton getter (`_get_request_cache()`, `_get_model_catalog_l1()`,
  `_get_context_cache_l1()`). The re-wire changes what the getter *returns*,
  not how the cache is constructed.
- **TDD per step**: write the migration test first (assert old behavior still
  holds after re-wire), red-green-refactor.
- Each step ends with `ruff check`, `py_compile`, and the targeted pytest for
  that consumer. No full suite on the 4GB workstation host.
- One commit on `main` at the end. All steps additive behind flags, so any
  single step can be reverted independently.

## H-1: Silent-failure guard (Step 4)

All three lazy getters wrap construction in `try/except` and leave the cache
`None` forever on failure. This is a **silent-degradation bug**: the caller
gets `None` and behaves as "no cache," with no log, no alert, no retry.

Fix: introduce a module-level `_cache_init_error: Exception | None = None`.
On construction failure, set it and re-raise. Callers check it and either
re-raise or surface a warning. Never swallow.

## Rollback

Each step is its own commit. Revert the offending commit; the flag default
guarantees the previous behavior is restored.

## Sign-off Required Before Code

User approval on this v2.0 plan before Step 1 (which itself requires
Plan #2 Phase 0 to have landed).