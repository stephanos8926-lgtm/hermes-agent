# Synthesis: Cache Infrastructure Unification — Audit Findings → Plan v2.0

Date: 2026-09-06
Mode: MEDIUM (plan-and-audit skill, phases 3-5 complete)
Source artifacts: `docs/plans/cache-overhaul-existing-v1.md`, `docs/plans/cache-new-tiered-work-v1.md`
Reference projects: `~/.references/diskcache`, `~/.references/theine`, `~/.references/cachka`

## Audit verdict

Both v1 plans contained **API references that do not exist** and **topology
assumptions that are wrong**. The good news: the underlying architecture is
sound and the dead-code findings are all *wiring* problems, not design flaws.
v2.0 corrects every finding and re-orders the work so the config surface is
built **first**, because nothing can be wired to a config block that doesn't
exist.

## Findings folded into v2.0

| ID | Finding | Severity | v2.0 response |
|----|---------|----------|---------------|
| G1 | Plans call `get_cache_router()` — **function does not exist**. Only `build_cache_from_config()` exists at `agent/_cache.py:1558`. | 🔴 High | **Add `get_cache_router()` as a lazy singleton wrapper** around `build_cache_from_config()`. It is the single entry point every consumer calls. |
| G2 | `build_cache_from_config()` has **zero production callers** (only a docstring mention at `:1472`). | 🔴 Critical | Plan #2 Phase 3 now **builds and wires the singleton**; this is the phase that converts dead code to live code. |
| G3 | `is_cache_enabled()` / `is_l1/l2/l3_enabled()` have **zero production callers**. | 🔴 Critical | These become the **gating functions inside `get_cache_router()`** — they get callers by construction once the router is wired. |
| G4 | Secrets bridge is wired through `agent/secret_sources/_cache.py` (`bridge_read`/`bridge_write` at `:145,245`), **not** through a router singleton. | 🟠 High | Plan #1 Step 4 re-targeted: collapse the bridge by routing `bridge_read`/`bridge_write` through the unified `get_cache_router()` namespace, **not** by replacing the bridge's own `TieredCacheRouter`. |
| G5 | All 3 L1 consumers use **lazy singletons** (`_CONTEXT_CACHE_L1`, `_model_catalog_l1`, `_request_cache`), not direct constructors. | 🟡 Medium | Plan #1 re-wire targets the **lazy getters**, not the constructors. Each getter is re-routed to `get_cache_router("namespace")`. |
| G6 | `model_metadata` L1: `try/except` around construction leaves `_CONTEXT_CACHE_L1 = None` **forever** on failure (silent failure). | 🟠 High | New hardening task: lazy getters must **fail loud** on construction error (log + re-raise), never silently swallow. |
| G7 | **No `cache:` block exists** in `config.yaml` or `config_defaults.py`. | 🔴 Critical | **Re-order**: Plan #2 Phase 4 (config surface) now runs **before** any wiring. You cannot wire consumers to a config block that does not exist. |
| G8 | `ShardedFileCache` (L3) has **zero production callers**. | 🔴 Critical | Plan #2 Phase 2 activates it; Phase 3 wires it through the router so it gains callers. |

## Re-ordering decision

v1 had config surface last (Phase 4). v2.0 moves it to **Phase 0** of the
new-work plan, before any wiring. Rationale: every consumer re-wire in Plan #1
depends on `get_cache_router()` reading a `cache:` block. Building the block
first makes every subsequent step testable against a real config.

## New hardening task (from G6)

**H-1: Lazy-cache silent-failure guard.** All three L1 lazy getters
(`_CONTEXT_CACHE_L1`, `_model_catalog_l1`, `_request_cache`) swallow
construction exceptions and leave the cache `None` permanently. Add a
`_cache_init_error` sentinel + explicit re-raise, so a backend failure is
visible instead of silently degrading to "no cache."

## Deliverables produced by this synthesis

- `docs/plans/cache-overhaul-existing-v1.md` → **rewritten as v2.0**
- `docs/plans/cache-new-tiered-work-v1.md` → **rewritten as v2.0**
- `docs/specs/cache-infrastructure-unified-v1.md` → unchanged (still valid; G1-G8 are plan-level, not spec-level)
- `docs/ADR.md` → unchanged (already written)

## Sign-off gate

Both v2.0 plans are ready for review. Nothing in `agent/` has been modified.
User sign-off on v2.0 → implementation begins.