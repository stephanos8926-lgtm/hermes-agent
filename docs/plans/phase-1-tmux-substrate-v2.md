# Phase 1 -- tmux Substrate (multiplex scaffold)

Date: 2026-08-31
Spec: docs/specs/terminal-multiplex-v2.md G-M1, G-M6 sec 3
Depends: Phase 0 (hardening)

## Goal

multiplex=true creates a real tmux session, isolated per task, with graceful Popen fallback.

## File Manifest

| File | Action | Lines | Description |
|------|--------|-------|-------------|
| hermes_cli/config_defaults.py | modify | ~10 | 6 keys: multiplex_default=false, multiplex_socket_dir, multiplex_scope="task", multiplex_wait_poll_ms=200, multiplex_record=true, multiplex_max_sessions_per_task=16 |
| tools/process_registry.py | modify | ~180 | New fields tmux_socket/target/cast_path/is_multiplex, helpers _tmux_available() (which + version), spawn_via_tmux(), kill_tmux(), snapshot()/poll tmux branch, list_sessions mux, max-session 429, sanitizer multiplex_name (strip, 64 cap, collision error) |
| tools/terminal_tool.py | modify | ~80 | Parse multiplex/multiplex_name/multiplex_mode, mutex check background+multiplex=400, route to spawn_via_tmux vs spawn_local, format multiplex JSON (tmux_target, attach_cmd, cast_path, multiplex_unsupported fallback) |
| run_agent.py | modify | ~8 | broadcast_interrupt also kills multiplex sessions (same task filter) |
| tests/test_multiplex.py | create | ~140 | availability, sanitization, perms 0700, max 429, fallback, kill |

## Tasks

1. Config defaults -- add 6 keys, docs in comments.
2. Registry helpers -- _tmux_available caches which tmux + version --prefix check, sanitizer _sanitize_mux_name.
3. spawn_via_tmux -- per-task socket XDG_RUNTIME_DIR/hermes-tmux-<task>.sock else ~/.hermes/tmux/<task>.sock (0700), session hermes-<name>-<hex>, error on name collision (do not suffix), return ProcessSession with is_multiplex.
4. kill_tmux / poll tmux -- `tmux -S <sock> kill-session -t <target>`, `capture-pane -p -S -1000` for poll preview, still reports silent_for_seconds.
5. Terminal tool -- multiplex arg parsing, mutex, fallback JSON when tmux missing / sandbox (multiplex_unsupported:true).
6. Broadcast extension -- run_agent wiring covers tmux sessions.
7. Tests + manual smoke: multiplex echo, sleep 9999 poll/snapshot/kill, fallback.
8. Lint + pytest targeted.

## TDD Test Plan

- test_tmux_available_false_when_missing (mock which)
- test_sanitize_rejects_traversal (../../etc -> 400)
- test_collision_same_name_errors
- test_fallback_when_no_tmux_returns_popen
- test_socket_perms_0700 (stat)
- test_max_sessions_429

## Rollback

Revert commit; multiplex_default false so existing installs unaffected even before revert.

## Acceptance

- [ ] terminal("echo hi", multiplex=true) returns tmux_target + attach_cmd
- [ ] No tmux binary -> {multiplex_unsupported,hint} and Popen fallback
- [ ] Socket 0700 under task dir, two names with same sanitize collide => error
