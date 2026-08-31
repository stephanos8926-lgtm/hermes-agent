# Phase 2 -- Wait + Snapshot + Recording

Date: 2026-08-31
Spec: docs/specs/terminal-multiplex-v2.md G-M4, G-M5 sec 3 sec 6
Depends: Phase 1

## Goal

Deterministic wait primitives over tmux capture plus audit recording with cap.

## File Manifest

| File | Action | Lines | Description |
|------|--------|-------|-------------|
| tools/process_registry.py | modify | ~120 | wait helpers: _wait_for_tmux(target, regex, idle_ms, screen_stable_ms, timeout) polling capture-pane hash; recording: pipe-pane "cat >> <cast>" on spawn, rotation at 10MB (cap + rename .1) |
| tools/terminal_tool.py | modify | ~60 | Parse wait_for_regex/wait_for_idle_ms/wait_for_screen_stable_ms, mutex with promotion (disabled when multiplex tmux owns PTY), return wait_result in JSON |
| tools/environments/base.py | modify | ~30 | _wait_for_tmux path (no promotion branch) for multiplex, pass wait params |
| tests/test_multiplex.py | modify | +80 | wait regex/idle/stable tests, cast cap, snapshot text mode |

## Tasks

1. capture poll loop -- sha256 of capture-pane -p, idle_ms since last change, screen_stable_ms hash stable window, regex match on tail joined lines, 200ms poll, timeout -> wait_timeout JSON.
2. Mutex -- when tmux target present, promotion branch skipped (no fd).
3. Recording -- pipe-pane open on spawn to ~/.hermes/multiplex/<target>.cast (asciicast-v3 header), close on kill, cap 10MB rotate, viewer strips ANSI.
4. Terminal args -- schema + validation (regex compile check, ms >0).
5. Tests: wait regex hits in 2s, wait idle fires after silence, cast capped, snapshot returns text.
6. Lint + pytest.

## TDD

- test_wait_regex_hits -- server prints READY after 1s, wait_for_regex=READY returns <3s
- test_wait_idle_fires
- test_wait_timeout_returns_wait_timeout_key
- test_cast_cap_10MB_rotate
- test_snapshot_text_no_ansi

## Rollback

Single revert; waits are opt-in so existing multiplex still works.

## Acceptance

- [ ] wait_for_regex works deterministically
- [ ] cast never exceeds ~10MB
- [ ] snapshot text mode ANSI-stripped
