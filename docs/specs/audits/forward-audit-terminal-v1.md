# Forward Audit -- terminal-nonblocking + multiplex v1

Date: 2026-08-31
Scope: Validate spec claims against code on feature/rw-live @ 758ff6de41

## Method
Grep + read of hermes_cli/config_defaults.py, tools/process_registry.py, tools/environments/base.py, tools/terminal_tool.py, run_agent.py

## Findings -- 18 claims, 17 pass, 1 partial

| # | Spec claim | File:line | Verdict |
|---|------------|-----------|---------|
| F1 | 7 config keys present | config_defaults.py:401-417 | PASS -- all 7 found |
| F2 | ProcessSession.last_output_at + spawn_monotonic monotonic | process_registry.py:424 | PASS |
| F3 | _stamp_output + _compute_silent_fields | process_registry.py:546-573 | PASS |
| F4 | 4 stamp points (reader, pty, env_poller, drain) | process_registry.py:1318,1450,1508 + base.py:1139 | PASS |
| F5 | spawn_local/via_env seed spawn_monotonic | process_registry.py:1168,1399 | PASS |
| F6 | poll/list_sessions emit silent_for_seconds running-only | process_registry.py:2181,2504 | PASS |
| F7 | broadcast_interrupt(task_id) exists | process_registry.py:2675 | PASS |
| F8 | base.py execute _promote_info plumbing | base.py:1033,1324 | PASS |
| F9 | _idle_last_output_at + _promotion_event drain cooperation | base.py:1092,1334 | PASS |
| F10 | terminal_tool _promote_info build + promoted JSON | terminal_tool.py:3090,3167 | PASS |
| F11 | run_agent.py wire broadcast_interrupt | run_agent.py:3415 | PASS (grep 1 hit) |
| F12 | local-only promotion (fileno check) | base.py:1326 | PASS |
| F13 | bounded_capture=True foreground | terminal_tool.py:3080 | PASS |
| F14 | task-scoped kill, empty task_id no-op | process_registry.py:2685-2710 | PASS |
| F15 | 0 disables each gate | config + _idle_* helpers default 0 check | PASS |
| F16 | deterministic promoted payload shape | terminal_tool.py:3160 + base.py:1371 | PASS |
| F17 | monotonic vs wall clock split | process_registry.py:553-559 | PASS |
| F18 | multiplex spec claim -- spawn_via_tmux exists | N/A (not yet implemented) | PARTIAL -- spec is forward-looking, code intentionally absent. Gate with _tmux_available() check and fallback JSON required before merge. |

## Gaps escalated to reverse audit
- Spill file path included in promoted payload? Only terminal_tool mentions it, base.py truncates via render(suffix=) -- need explicit contract.
- drain join timeout=2 not in spec -- add.
