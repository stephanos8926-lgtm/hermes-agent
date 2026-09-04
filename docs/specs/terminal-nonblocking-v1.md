# SPEC: Terminal Non-Blocking Hang-Resilience — v1.0

Status: Implemented & Shipped (feature/rw-live @ 758ff6de41) — spec is retrospective + contract for hardening
Date: 2026-08-31
Mode: Medium (per plan-and-audit)

## 1. Problem Statement

`terminal` foreground mode (`background=false`, default) blocks the agent turn until the command exits or hits `effective_timeout` (default 180s, clamped by `terminal.timeout`). Silent hangs — waiting for stdin (`read`, `python` REPL, `psql`), network I/O stall, `sleep 3600`, dead VPN — produce zero output for minutes. Symptoms:

- Agent frozen for full timeout with no diagnostic (`silent_for_seconds` not surfaced).
- `/stop` or `is_interrupted()` only kills the foreground wait; background `process_registry` sessions started via `background=true` were NOT killed — orphans.
- `background=true` is opt-in and pattern-gated (`watch_patterns`), so the common case (normal `terminal("sleep 10")`) has no visibility.
- Sandbox backends (Docker/Modal/Daytona) keep different lifecycle; local-only promotion must not regress them.

Industry expectation (CodeBolt, `agent-tui`/`agent-tty`, `Pair-Claude`, `amux`/`llmux`) is streaming/output-aware scheduling and detach/reattach. Hermes trailed here.

## 2. Goals

| ID | P | Description |
|---|---|---|
| G-1 | P0 | **Silence observability**: every running `terminal`/`process` surfaces `silent_for_seconds` when idle ≥ threshold, monotonic-safe, running-only. |
| G-2 | P0 | **Hang rescue**: silent foreground auto-promotes to background `process_registry` session without killing the process, returns deterministic JSON so agent can `poll`/`write`/`kill`. |
| G-3 | P0 | **Stop hygiene**: `interrupt` / `/stop` fans out to background sessions of the same `task_id` (config-gated, scope `task`\|`none`). |
| G-4 | P1 | Preserve existing contracts: fast commands (`echo`, `pwd`, `cat` small file) return ~5-10ms via adaptive poll, streaming output never falsely promotes, sandbox backends unchanged, bounded capture avoids gateway OOM (#64435). |
| G-5 | P1 | Operator tunability: all thresholds via `hermes_cli/config_defaults.py` + `hermes config get/set`, 0 disables each gate individually. |

Non-goals (this spec): tmux multiplex / shared PTY (next spec), `wait_for` regex primitive, asciicast recording — noted as vNext.

## 3. Compatibility & Behavior Rules

- **Monotonic only** for silence math: `spawn_monotonic` and `last_output_at` via `time.monotonic()`. Wall clock (`started_at = time.time()`) retained only for `uptime_seconds` display, never for idle math. Migration: old sessions missing monotonic fields fall back to `time.time() - started_at` (report, not promote).
- **Single stamp point**: `_append_chunk` helper called from 4 locations: `ProcessRegistry._reader_loop`, `_pty_reader_loop._append_text`, `_env_poller_loop` (delta branch), `BaseEnvironment._wait_for_process` drain thread (`_drain_iterable` + Windows + Unix select). Any new reader must call `_stamp_output`.
- **Running-only reporting**: `poll()` and `list_sessions()` only emit `silent_for_seconds` when `not exited` and `silent ≥ idle_silence_report_seconds`. Exited sessions never report.
- **Promotion preconditions (all must hold)**: `_promote_info is not None` (only foreground `terminal_tool` path supplies it) AND real pipe `proc.stdout.fileno()` exists (local backend) AND `proc.poll() is None` AND `idle_s >= timeout_ms/1000` AND `deadline - now > promote_margin_seconds` AND `timeout_ms > 0`.
- **Never kill on promote**: `adopt_foreground()` moves live `Popen` into registry via `ProcessSession(pid, host_start_time, output_buffer=partial, spawn_monotonic, last_output_at)`, starts `_reader_loop` on same fd, writes checkpoint, logs `Adopted foreground pid=`. Original fd/pipe ownership transferred atomically — single reader thread afterwards.
- **Drain cooperation**: foreground drain thread checks `_promotion_event.is_set()` at top of each loop iteration (all three drain paths). Poll loop sets event before `adopt_foreground`, joins drain with `timeout=2`.
- **Deterministic promotion payload** (both `env.execute()` internal and `terminal_tool` model-facing):
  ```json
  {"promoted": true, "session_id": "proc_xxx", "silent_for_seconds": 60, "promotion_reason": "idle_silence", "hint": "Command produced no output for 60s and was promoted to background (session_id=proc_xxx). It may be waiting for input or genuinely hung. Use process(action='poll') to check, process(action='write'/'submit') if it needs input, or process(action='kill') if stuck.", "output": "<ansi-stripped, redacted, truncated>", "exit_code": null}
  ```
  `output_preview` (poll) and `output` (promoted) always go through `strip_ansi` + `redact_terminal_output` + head/tail spill handling.
- **Task scoping**: `broadcast_interrupt(task_id)` only kills `_running` entries where `s.task_id == task_id and not s.exited`. Empty/None `task_id` → 0 killed. `interrupt_broadcast_scope=none` → fully disabled regardless of flag. `daemon_term_grace_seconds` honored for SIGKILL escalation.
- **Local-only promotion**: sandbox `proc` objects are `ProcessHandle` iterators, not live Popen — `_has_pipe` false, promotion skipped, existing `timeout`/`kill` behavior unchanged.
- **Bounded capture**: foreground model-facing path always uses `bounded_capture=True` (head/tail window + spill file) to avoid gateway OOM (#64435). Internal `env.execute()` for file ops stays `bounded_capture=False` (unbounded).

## 4. File Manifest

| File | Lines | Change |
|---|---|---|
| `hermes_cli/config_defaults.py:401-417` | +16 | 7 keys: `idle_silence_report_seconds=60`, `idle_promote_timeout_ms=60000`, `promote_margin_seconds=30`, `background_default_timeout_seconds=900`, `background_max_timeout_seconds=3600`, `interrupt_broadcast_to_background=True`, `interrupt_broadcast_scope="task"` |
| `tools/process_registry.py:367-410` | +80 | `ProcessSession.last_output_at`, `spawn_monotonic`, `_stamp_output()`, `_idle_*` helpers, `_compute_silent_fields()` |
| `tools/process_registry.py:1318,1450,1508` | +6 | Stamp calls in 3 reader loops |
| `tools/process_registry.py:1047,1277` | +2 | `spawn_local` / `spawn_via_env` seed `spawn_monotonic` |
| `tools/process_registry.py:2046,2504` | +6 | `poll()` / `list_sessions()` emit `silent_for_seconds` |
| `tools/process_registry.py:2675-2715` | +40 | `broadcast_interrupt(task_id)` |
| `tools/environments/base.py:1031,1453` | +8 | `execute(..., _promote_info)` plumbing |
| `tools/environments/base.py:1088-1280` | +120 | `_idle_last_output_at`, `_promotion_event`, drain loops cooperation, poll-loop promotion branch |
| `tools/terminal_tool.py:3080-3189` | +60 | Build `_promote_info`, early `promoted` return with redaction/truncation, `workdir` transient guard |
| `run_agent.py:3415` | +5 | `AIAgent.interrupt()` → `_pr.broadcast_interrupt(_task)` after worker-thread fan-out |

## 5. Acceptance Criteria

- [ ] `poll(proc_sleep10)["silent_for_seconds"] >= 60` after 65s of silence (monotonic), `list_sessions` same, exited session omits field, threshold 0 disables.
- [ ] `terminal("echo hello")` returns in <200ms, not promoted, exit_code 0.
- [ ] `terminal("for i in 0..5; do echo tick $i; sleep 0.5; done")` not promoted, 6 lines, exit 0.
- [ ] `terminal("python3 -c 'print(\"start\"); import sys,time; sys.stdout.flush(); time.sleep(9999)'")` promotes after ~60s (test override 2000ms), payload has `promoted:true`, `session_id`, `silent_for_seconds`, `hint`, `poll` shows running, `process(action='kill')` → exit -15.
- [ ] `/stop` while `sleep 20` background with `task=test-task-xyz` kills that session but leaves `other-task` `sleep 20` untouched.
- [ ] Sandbox handle (no fileno) never promotes, times out normally.
- [ ] `py_compile` clean on all 5 files, existing `tests/test_process_registry.py` / `tests/test_terminal.py` pass when available.
