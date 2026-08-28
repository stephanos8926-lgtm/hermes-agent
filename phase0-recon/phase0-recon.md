# Phase 0 — Reconnaissance Report

**Date:** 2026-08-25
**Fork:** `/home/sysop/Workspaces/hermes-agent-fork/`
**Status:** Reconnaissance complete. The fork is **at or near upstream v2026.8.3** (v0.20.0).

## Executive Summary

The fork is essentially current with upstream. All "cherry-pick" candidates from the Phase 1 plan are **already in the fork**. The major optimization work that remains is:

1. **Phase 2 (D1 — token estimate memoization)** — `estimate_messages_tokens_rough` at `agent/model_metadata.py:3444` is **not** memoized. Real win.
2. **Phase 3 (system-prompt L1 cache)** — not implemented. Real win.
3. **Phase 4 (lifecycle_guard shlex)** — not addressed. Small but real win.
4. **Phase 5 (tui_gateway deepcopy)** — not addressed. The TUI still has its own `_cfg_cache` and deepcopy.
5. **Phase 6 (db maintenance)** — `hermes sessions optimize` exists. Missing: LCM DB support, formal `hermes db` subcommand, integrity-check wrapper.
6. **Phase 7 (log retention)** — `RotatingFileHandler` is in place. Missing: age-based log pruning, journalctl policy, backup rotation in `disk-cleanup`.

## PR Audit Table (verified)

| PR | Title | Status | Evidence |
|---|---|---|---|
| #17041 | mtime-cache `load_config()` | **IN FORK** | `_LOAD_CONFIG_CACHE` at `hermes_cli/config.py:250-254` |
| #28866 | `load_config_readonly()` + 47% fn-call cut | **IN FORK** | `load_config_readonly` at `hermes_cli/config.py:3440`; 50+ call sites; `_needs_thinking_reasoning_pad` at `run_agent.py:7819` |
| #73359 | Background token accounting queue | **IN FORK** | `queue_token_counts` at `hermes_state.py:7769`; `flush_token_counts` at `hermes_state.py:7822`; `_stop_token_writer` at `hermes_state.py:4941` |
| #74211 | `read_raw_config_readonly()` (54x telemetry) | **IN FORK** | `read_raw_config_readonly` at `hermes_cli/config.py:3328`; used in `relay_shared_metrics.py:1072` and `tools/tool_backend_helpers.py:337` |
| #74228 | One config.yaml parse per process | **IN FORK** | `env_loader.py:13` uses `fast_safe_load` from `utils`; centralized reader |
| #74322 | `load_config_readonly` at 29 call sites (28x cheaper) | **IN FORK** | 50+ call sites in `agent/`, `hermes_state.py`, `tui_gateway/`, `tools/`, `plugins/` |
| #76880 | Tool-call arg canonicalization memo | **IN FORK** | `_canonicalize_tool_call_arguments` at `agent/conversation_loop.py:1232`; 32MB byte budget; full test suite at `tests/agent/test_canon_args_memo_parity.py` |
| #20424 | wire `should_compress_preflight` | **IN FORK** | `agent/turn_context.py:1115-1146` (comment cites #20316) |
| #41974 | `_release_evicted_agent_soft` | **IN FORK** | `gateway/run.py:27220, 27211`; documented in `docs/session-lifecycle.md:563, 602` |
| #57229 | AIAgent hot-path salvage (prompt-cache copy, etc.) | **IN FORK** | `_clone_message_for_send` at `agent/conversation_loop.py:1260` |
| #77651 / #71835 / #75218 | Desktop performance (2nd 60fps wave) | **DEFERRED** | Not applicable (no desktop) |

**No cherry-pick work is needed for Phase 1. The fork is current.**

## Hot-Path Verification (Phase 2-5 prerequisites)

| Symbol | Location | Status |
|---|---|---|
| `estimate_messages_tokens_rough` | `agent/model_metadata.py:3444` | **No memoization** — D1 still applies |
| `copy.deepcopy(tools)` in `prompt_caching.py:307` | `agent/prompt_caching.py:307` | **Required** — `strip_anthropic_tool_cache_control` mutates |
| `copy.deepcopy(api_messages)` in `prompt_caching.py:395` | `agent/prompt_caching.py:395` | **Required** — `strip_anthropic_cache_control` mutates in place |
| `copy.deepcopy(messages[0])` in `prompt_caching.py:482` | `agent/prompt_caching.py:482` | **Could be shallow** — comment at 473-477 says "Shallow top-level copy is enough" but uses deepcopy |
| `copy.deepcopy(messages[idx])` in `prompt_caching.py:498` | `agent/prompt_caching.py:498` | **Could be shallow** — same as 482 |
| `isinstance(msg, dict)` in `turn_context.py` | `agent/turn_context.py:213, 282, 641, 654, 792, 1196, 1276, 1344, 1490` | All 9 sites confirmed |
| `time.sleep(2)` in `conversation_loop.py` | `agent/conversation_loop.py:5296, 5599, 5905` | Still present — D2 deferral justified |
| `_cfg_cache` in `tui_gateway/server.py` | `tui_gateway/server.py:163, 3328, 3339, 3340, 3348, 3351, 3402, 3417` | Duplicate logic + deepcopy at 3340/3351 — Phase 5 still applies |
| `shlex.shlex(...)` in `lifecycle_guard.py` | `cron/lifecycle_guard.py:185, 271` | Still present — Phase 4 still applies |

**Notable finding:** Lines 473-477 of `prompt_caching.py` show that someone has **already** identified that shallow copy is enough — they have a comment "Shallow top-level copy is enough" — but the actual code at 482 and 498 still uses `copy.deepcopy`. **This is a low-risk optimization opportunity** — replace those two deepcopies with shallow `dict(msg)` and the cache-control logic. Documented in `_clone_message_for_send` at `conversation_loop.py:1282-1283` — "history messages are JSON-shaped and acyclic (depth < 10 in practice)" — which means deep copies are unnecessarily defensive.

## LCM Env Vars Status

The LCM env vars from earlier session (e.g., `LCM_DEFERRED_MAINTENANCE_ENABLED`) are at the user runtime level, not the fork. **Out of scope for fork-side optimization** — the LCM env vars are runtime configuration, not source code changes.

## What Phase 0 Reveals About the Plan Set

**The original Phase 1 (cherry-pick upstream PRs) is entirely obsolete.** The fork is current.

**Phase 2 needs significant revision:**
- The `prompt_caching.py` deepcopies (307, 395) are required — cannot be removed
- Only the (482, 498) sites have a shallow-copy option per the existing comment
- D1 (`estimate_messages_tokens_rough` memoization) remains the headline win
- The `_clone_message_for_send` function is the pattern to mirror

**Phase 3-7 remain valid** as designed.

**Phase 8 (D2 deferral) is correct** as designed.

## Reconnaissance Complete

This file is the input to all subsequent phases. Implementation can begin.
