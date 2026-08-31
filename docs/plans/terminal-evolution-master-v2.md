# Plan: Terminal Evolution -- Master Phasing (v2.0 Spec)

Date: 2026-08-31
Specs: docs/specs/terminal-nonblocking-v2.md (shipped+hardening), docs/specs/terminal-multiplex-v2.md (new), docs/specs/terminal-adrs-v2.md
Mode: Medium
Branch: feature/rw-live (current) -> merge to main after Phase 0

## Confidence Check

SPEC + ADR v2 are all-inclusive for this round: forward audit 17/18 pass, 8 reverse gaps and adversarial findings folded into v2 (spill_path carry, per-wait local event, 0700 GC, injection hardening, 10MB cast cap, 429 mutex). No open requirement remains unaddressed. Good to proceed.

## Phasing (4 phases, dependencies linear)

```
Phase 0 -- Ship Hardening (must be first: fixes cover shipped code)
   |
   v
Phase 1 -- tmux Substrate (socket, spawn/kill/list, fallback)
   |
   v
Phase 2 -- Wait + Snapshot + Recording (wait_for_*, capture-pane, cast)
   |
   v
Phase 3 -- Handoff / Share + GC + Observability (multiplex_mode, reap, docs)
```

| Phase | Scope | Files touched | Est. | Gate |
|-------|-------|---------------|------|------|
| 0 | Ship hardening: merge conflicts, spill_path, concurrent isolation, formal pytest, tuning docs, tag | 6 files | 0.5d | pytest + merge to main |
| 1 | tmux substrate | 4 files | 0.5d | multiplex smoke + fallback |
| 2 | wait + recording | 4 files | 0.5d | wait determinism + 10MB cap |
| 3 | handoff/share + GC + observability | 3 files | 0.5d | attach + perms 0700 + doctor |

Total ~2d. Phases 1 and 2 cannot parallelize (share process_registry). Phases are intentionally small for reviewability.

## Execution Rules (per plan-and-audit Medium)

- TDD per phase: tests first, red-green-refactor.
- Each phase ends with ruff/py_compile + targeted pytest, no full suite on 4GB host.
- Each phase is its own commit on feature/rw-live, then squash-no-ff merge to main only after Phase 0.
- Rollback: revert single phase commit (all phases additive behind flags, so revert is safe).

## Sign-off Required Before Code

User approval on master + phase plans before implementation starts.
