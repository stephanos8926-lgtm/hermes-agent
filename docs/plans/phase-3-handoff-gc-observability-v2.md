# Phase 3 -- Handoff / Share + GC + Observability

Date: 2026-08-31
Spec: docs/specs/terminal-multiplex-v2.md G-M2, G-M3 sec 3 + ADR 4/5/6 addenda
Depends: Phase 2

## Goal

Human handoff, joint share, GC, and operator visibility.

## File Manifest

| File | Action | Lines | Description |
|------|--------|-------|-------------|
| tools/process_registry.py | modify | ~60 | multiplex_mode handling (agent/handoff/share): handoff just leaves session alive after agent detaches (no autokill), share notes FIFO queue placeholder (v1 readwrite all); GC helper _reap_stale_multiplex() for >24h sockets |
| tools/terminal_tool.py | modify | ~20 | multiplex_mode arg validate, attach_cmd variant for SSH (`ssh <host> tmux -S <sock> attach`), multiplex_resize helper |
| AGENTS.md / README | modify | ~30 | Handoff/share usage, socket GC, hermes doctor warning when multiplex requested but tmux missing |
| hermes_cli/doctor.py (or tools) | modify | ~15 | doctor check: which tmux, socket dir writable, dangling sessions warning |

## Tasks

1. multiplex_mode -- agent (default, agent keeps), handoff (agent detaches, human attaches), share (both, FIFO queue documented as readwrite v1).
2. GC -- on gateway restart, reap tmux sessions with no heartbeat >24h (opt-in helper, not auto on every call).
3. Doctor -- warn if multiplex requested but tmux missing or socket dir not 0700.
4. Docs -- handoff/share examples + GC note.
5. Tests: mode validation, GC no-op on fresh (<24h), doctor warning.
6. Lint + pytest.

## TDD

- test_multiplex_mode_invalid_rejected
- test_handoff_leaves_session_alive
- test_gc_does_not_reap_fresh
- test_doctor_warns_no_tmux

## Rollback

Revert; modes are additive.

## Acceptance

- [ ] handoff session survives after terminal returns
- [ ] socket GC not aggressive on fresh sessions
- [ ] hermes doctor reports missing tmux when multiplex used
