# ADR: Terminal Evolution -- v2.0

Date: 2026-08-31
Deciders: sysop, lucien

## ADR-1: Monotonic clock for silence math

Context: `ProcessSession.started_at` was `time.time()` (wall clock). NTP steps or DST skew would false-trigger or miss silence detection. Need stable idle measurement across drain threads and poll loop.

Decision: Introduce `spawn_monotonic` + `last_output_at` using `time.monotonic()`. All `silent_for_seconds` and promotion math uses monotonic. Wall clock kept only for `uptime_seconds` display. Pre-migration sessions fall back to wall clock for reporting only (never promote).

Consequences: + skew-free. - Two clocks to reason about; new reader loops must call `_stamp_output`.

## ADR-2: Adopt-foreground promotion (reparent) over detached-return

Context: Two ways to rescue hung foreground: (A) return early with `session_id` but keep Popen in `base.py` wait loop (detached-return), (B) reparent live Popen into `ProcessRegistry` with handoff of fd/reader (adopt).

Decision: Adopt (B). `adopt_foreground(Popen)` creates `ProcessSession(pid, host_start_time, output_buffer=partial, spawn_monotonic, last_output_at)`, registers in `_running`, starts `_reader_loop` on same pipe, writes checkpoint, signals `_promotion_event`. Process never killed on promote.

Consequences: + `poll`/`write`/`kill` work uniformly; checkpoint survives. - Must atomically transfer fd ownership and stamp point; mismatch risks double-read/lost tail (mitigated by promotion_event + drain join). Sandbox backends have no live handle so must fallback to timeout.

## ADR-3: Task-scoped interrupt broadcast

Context: `/stop` and `AIAgent.interrupt()` already fan out to `_tool_worker_threads` via `set_interrupt`. Background `process_registry` sessions hung on I/O never see `is_interrupted()` -> orphans.

Decision: `ProcessRegistry.broadcast_interrupt(task_id)` kills only `_running` entries where `task_id == task_id` and not exited, config-gated by `interrupt_broadcast_to_background` and `interrupt_broadcast_scope` (`task`|`none`). Wired in `run_agent.py:AIAgent.interrupt()` after worker-thread loop.

Consequences: + stops orphans without touching other tasks (dev server safe). - Killing is best-effort; `kill_process` may fail if PID already reaped (treated as already_exited).

## ADR-4: tmux as multiplex substrate (vs screen / zellij / rmux)

Context: Need detach/reattach, handoff, share. Options: `tmux` (ubiquitous, pane capture, pipe-pane, server mode, per-project sockets, ecosystem amux/llmux/pTTY), `screen` (older, fewer hooks), `zellij`/`rmux` (nicer UX but not ubiquitous, requires install), custom PTY proxy (high risk).

Decision: `tmux` when `multiplex=true`, local backend only, per-task socket `$XDG_RUNTIME_DIR/hermes-tmux-<task>.sock` (0700), session `hermes-<name>-<hex>`. Fallback to Popen when tmux missing or sandbox. Record via `pipe-pane`, observe via `capture-pane -p`. Add `wait_for_*` polling on capture tail.

Consequences: + leverages battle-tested tmux, `tmux attach` works via SSH, no new daemon, recording free. - Requires tmux installed; socket GC needed; Windows ConPTY not supported in v1.

## ADR-5: Recording to asciicast-v3 via pipe-pane

Context: Audit trail for agent actions; `agent-tty`/`agent-tui` patterns prove value of replay.

Decision: Every multiplex session `pipe-pane -o "cat >> <cast>"` to `~/.hermes/multiplex/<target>.cast` (asciicast-v3). `snapshot` is capture-pane text, not renderer-dependent PNG in v1 (PNG/wasm ghostty-web deferred).

Consequences: + deterministic replay, no LLM summarization drift. - Disk growth; bounded by per-task max sessions and 24h reap.

## ADR-6: Config-gated, additive rollout

Context: Feature must not surprise existing operators or break prompt-cache-sensitive flows.

Decision: All gates default sane but disable-able with 0/false: `idle_silence_report_seconds=60`, `idle_promote_timeout_ms=60000`, `promote_margin_seconds=30`, `interrupt_broadcast_to_background=true`. Multiplex defaults false. All new behavior is additive.

Consequences: + safe rollout, tunable per env. - More knobs to document/test.


## Changes in v2

- ADR-4 addendum: screen/zellij rejected also on license/CI weight (zellij 0.40 binaries not in Debian stable, rmux Go binary extra build step). Documented.
- ADR-5 addendum: asciicast cap 10MB + rotation; viewer sanitization added.
- ADR-6 addendum: explicit mutex rule multiplex+background=true => 400.
- New ADR-7: Per-wait local _promotion_event (vs global) to support concurrent hangs -- decided local to avoid cross-promotion.
- New ADR-8: Spill_path carried across adopt -- preserves bounded_capture tail after reparent.
