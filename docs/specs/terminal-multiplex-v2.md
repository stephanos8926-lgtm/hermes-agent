# SPEC: Terminal Multiplex (tmux) + vNext Enhancements -- v2.0

Status: Proposed -- v2 incorporates adversarial + reverse fixes (0700 GC, injection hardening, caps, mutex)
Date: 2026-08-31
Mode: Medium

## 1. Problem Statement

`terminal` and `process_registry` today give two modes: foreground (blocking, auto-promotes after 60s silence) and background `proc_*` (detached, pollable). Both are agent-private: the human cannot see the live PTY, cannot attach, cannot share. Shortcomings vs. industry (`amux`, `rmux`, `llmux`, `pTTY`, `Pair-Claude`):

- No persistent named shell (`resume=true` / directory-keyed session).
- No human-in-the-loop handoff (agent starts `npx tsc --watch`, human takes over).
- No joint control (pair-programming share).
- No deterministic `wait_for` -- agent must sleep+poll loops.
- No audit trail beyond rolling `output_buffer` (200KB truncated).

## 2. Goals

| ID | P | Description |
|---|---|---|
| G-M1 | P0 | `multiplex=true` flag on `terminal` -- open PTY inside a tmux server, return `tmux_target` + `attach_cmd`, keep until `kill`. Default false, additive. |
| G-M2 | P0 | Modes: `multiplex_mode` = `agent` (agent owns), `handoff` (agent detaches, human attaches), `share` (both can type, FIFO). |
| G-M3 | P1 | Persistent named multiplex: `multiplex_name` stable; re-invoking with same name reattaches vs duplicates. |
| G-M4 | P1 | Deterministic `wait_for`: `wait_for_regex`, `wait_for_idle_ms`, `wait_for_screen_stable_ms` as terminal args, powered by tmux polling or registry reader. |
| G-M5 | P1 | Recording: every multiplex session auto `pipe-pane` to asciicast-v3 under `~/.hermes/multiplex/<id>.cast`, plus `snapshot`/`screenshot` helper. |
| G-M6 | P1 | Safety: per-task socket isolation, 0700, local-only, graceful fallback to Popen when tmux missing. |

Non-goals: cloud replication across hosts, multi-host attach, Windows ConPTY parity in v1.

## 3. Compatibility & Behavior Rules

- **Default false, additive**: `multiplex=false` preserves all v1 paths (Popen foreground, background proc_*, idle promotion, bounded capture). No breaking change.
- **Mutex with background**: `multiplex=true` + `background=true` is error (400) with hint "multiplex sessions are already persistent; use poll/kill/snapshot instead".
- **Socket isolation**: `tmux -S $XDG_RUNTIME_DIR/hermes-tmux-<task_id>.sock` or `~/.hermes/tmux/<task>.sock` (create dir 0700). Never default `/tmp/tmux-1000`. One socket per `task_id` (gateway session key) to avoid cross-task leaks.
- **Target naming**: `hermes-<multiplex_name or 8-hex>` window inside socket. `multiplex_name` sanitized `[^a-zA-Z0-9_-]` -> `_`, length 64.
- **Lifecycle**: `tmux new-session -d -s <name> -c <cwd> "<command> ; echo __HERMES_EXIT:$?__"` or `pipe-pane`-logged shell. `kill` -> `tmux kill-session -t <target>` then reap `pipe-pane`. Server lingers via `loginctl enable-linger` already enabled; doc notes manual `tmux kill-server`.
- **Output capture dual path**: `pipe-pane -o "cat >> <cast>"` for recording + `capture-pane -p -S -<N>` for `poll(output_preview)` and `snapshot`. Drain thread not needed; tmux owns PTY.
- **Wait primitives**: poll `capture-pane` tail every 200ms until `regex` matches or `idle_ms` since last change or `screen_stable_ms` hash stable. Timeout -> `promotion`-like JSON with `wait_timeout:true`. Reuses `idle_promote_timeout_ms` margin logic.
- **Local-only**: if `terminal.backend != local` or `which tmux` missing, return `{multiplex_unsupported:true, fallback:"popen", hint:"tmux not available..."}` and run as normal Popen (no multiplex).
- **Security**: socket 0700, target name not guessable (random suffix if no name), `broadcast_interrupt(task_id)` kills matching multiplex sessions too (same task filter). `interrupt_broadcast_scope=none` disables.
- **Resize**: `multiplex_resize(cols, rows)` via `tmux resize-window -t <target> -x <cols> -y <rows>`.
- **Snapshot API**: `process(action='snapshot', session_id=..., mode='text|outline|cells')` -> text from capture-pane; `screenshot` returns U+FFFD-safe text (PNG later via ghostty-web optional).
- **GC**: on gateway restart, reap tmux sessions with no matching task heartbeat >24h (opt-in).

## 4. File Manifest

| File | Change |
|---|---|
| `hermes_cli/config_defaults.py` | +6 keys: `terminal.multiplex_default=false`, `multiplex_socket_dir`, `multiplex_scope="task"`, `multiplex_wait_poll_ms=200`, `multiplex_record=true`, `multiplex_max_sessions_per_task=16` |
| `tools/process_registry.py` | Extend `ProcessSession` with `tmux_socket`, `tmux_target`, `cast_path`, `is_multiplex bool`; add `_tmux_available()`, `spawn_via_tmux()`, `kill_tmux()`, `snapshot()`, list/poll tmux branch |
| `tools/environments/base.py` | New `_wait_for_tmux()` or multiplex branch in `execute()` (skip promotion poll, use tmux capture) |
| `tools/terminal_tool.py` | Parse `multiplex`, `multiplex_name`, `multiplex_mode`, `wait_for_*`; route to tmux vs Popen; validate mutex; format multiplex return JSON (attach_cmd, tmux_target, cast_path, wait_result) |
| `run_agent.py` | Extend `broadcast_interrupt` to include multiplex sessions (same filter) |
| `docs/specs/terminal-multiplex-v1.md` | This spec |
| `tests/test_multiplex.py` | New: tmux availability, name sanitization, socket perms, snapshot, wait, kill |

## 5. Acceptance Criteria

- [ ] `terminal("echo hi", multiplex=false)` unchanged vs v1.
- [ ] `terminal("sleep 9999", multiplex=true, multiplex_name="demo")` returns `tmux_target`, `attach_cmd`, `cast_path`, poll shows running, `process(action='snapshot')` returns pane text, `kill` removes session.
- [ ] Re-invoking same `multiplex_name` reattaches (second call returns same target, not new session) unless previous killed.
- [ ] `multiplex=true` + task A does not appear in task B `list_sessions`.
- [ ] `wait_for_regex="READY"` with server that prints READY after 2s returns within 2-3s, not timeout.
- [ ] No tmux binary -> graceful fallback JSON, no crash.
- [ ] Socket file mode 0700, path under task-scoped dir.


## 6. Synthesis (v2 triage incorporation)

- R1/R2: Socket GC note expanded: XDG_RUNTIME_DIR fallback to ~/.hermes/tmux/<task>.sock when runtime dir absent (reboot). Name collision: if sanitized names collide, error with hint to use unique names (do not auto-suffix silently).
- R7: Mutex: wait_for_* and idle promotion share no fd in multiplex path; promotion disabled when tmux owns PTY.
- R8: Max 16 enforced with 429 JSON {error:"too_many_multiplex_sessions", limit:16, hint:"kill one first"}.
- A2/A3: Trust boundary documented: gateway injects task_id; tmux commands use shlex.quote, never raw interpolate `command`.
- A4/A5: Cast raw but viewer strips ANSI; cast capped 10MB with rotation.
- B3 spill_path: Multiplex pipe-pane path distinct from foreground spill; snapshot never reads cast directly (uses capture-pane).
- AC expanded: attach_cmd examples for `tmux -S <sock> attach -t <target>` and SSH variant.
