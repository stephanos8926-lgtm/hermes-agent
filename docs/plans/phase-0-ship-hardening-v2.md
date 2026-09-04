# Phase 0 -- Ship Hardening (v2 triage fixes for already-shipped hang-resilience)

Date: 2026-08-31
Spec: docs/specs/terminal-nonblocking-v2.md sec 6 synthesis
Depends: none (must be first)

## Goal

Make shipped Phases 1-3 merge-ready: resolve main divergence, apply v2 bug fixes, formalize tests/docs.

## File Manifest

| File | Action | Lines | Description |
|------|--------|-------|-------------|
| tools/process_registry.py | modify | ~15 | carry spill_path in adopt_foreground (B3), assert per-wait local event invariant comment |
| tools/environments/base.py | modify | ~5 | include full_output_path in promoted payload when spill overflowed (R3) |
| tools/terminal_tool.py | modify | ~5 | same spill_path propagation to model JSON |
| .gitignore | modify | +2 | add phase0-recon/*.log |
| tests/test_process_registry.py or tests/test_terminal_hang.py | create/modify | ~120 | formalize 3 edges: fast echo not promoted, ticking not promoted, silent sleep promotes + silent_for_seconds + kill |
| AGENTS.md / README | modify | ~40 | tuning guide: idle_promote_timeout_ms 60s -> 120s for npm install, silent_for_seconds semantics, broadcast_interrupt |
| agent/conversation_loop.py, agent/turn_context.py | resolve | conflict | merge main -> feature/rw-live, resolve conflicts (non-terminal) then merge feature/rw-live -> main |

## Tasks (order)

1. Git: resolve conversation_loop/turn_context conflicts on feature/rw-live (merge main in, fix, push).
2. Code: spill_path -- ProcessSession field `spill_path: str|None`, set in adopt_foreground from drained spill handle, emit in poll/promoted JSON when truncated.
3. Code: per-wait local event comment/assert -- _idle_last_output_at and _promotion_event are list/event per execute() invocation, add comment + debug assert not shared.
4. Code: promoted payload includes `full_output_path` when MAX_OUTPUT_CHARS spilled, plus truncated note verification.
5. Tests: formal pytest -- test_fast_not_promoted, test_ticking_not_promoted, test_silent_promotes, test_stop_broadcast_isolation, test_silent_threshold_zero_disables.
6. Docs: AGENTS.md tuning guide + README terminal section.
7. Lint: ruff/py_compile + targeted pytest -q.
8. Ship: merge feature/rw-live -> main (no-ff), push fork main, tag terminal-hang-resilience-v1.

## TDD Test Plan

- tests/test_terminal_hang.py::test_fast_not_promoted -- echo hello <200ms, no promoted key.
- tests/test_terminal_hang.py::test_ticking_not_promoted -- tick 6 * 0.5s not promoted.
- tests/test_terminal_hang.py::test_silent_promotes_and_pollable -- sleep 9999 with 2s override promotes, poll running, kill.
- tests/test_process_registry.py::test_silent_for_seconds -- spawn silent, poll after 2s shows >=2.
- tests/test_process_registry.py::test_broadcast_isolation -- task A killed, task B untouched.

## Rollback

Single commit revert. No flag change; all hardening behind existing 0-disable knobs.

## Acceptance

- [ ] pytest 5 new tests pass
- [ ] fork main == feature/rw-live tip, tag pushed
- [ ] `hermes config get terminal.idle_promote_timeout_ms` still 60000 on both branches
