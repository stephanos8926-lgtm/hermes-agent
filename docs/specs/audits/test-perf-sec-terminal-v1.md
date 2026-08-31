# Test / Perf / Sec Docs

## Tests
- Manual edge cases passed: fast echo 0.14s not promoted, ticking not promoted, silent sleep promoted 2.4s (task-scoped), kill -15, broadcast isolation.
- Missing: pytest formalization for 3 edges + silent_for_seconds in tests/test_process_registry.py -- add in polish before merge.
- Multiplex tests: to add in tests/test_multiplex.py (availability, name, perms, snapshot, wait, kill, fallback).

## Perf
- Adaptive poll 5ms -> fast commands 6ms vs 200ms baseline -- intact.
- Drain select 0.1s + idle_after_exit 3 cycles 300ms -- minor overhead, dominated by 60s promote threshold.
- Tmux future: pipe-pane + capture-pane is O(capture_lines), keep capture -S -1000 to bound.

## Sec
- WAL SQLite warning unrelated.
- No new secrets in diff; tmux socket perms 0700 enforced in spec v2.
