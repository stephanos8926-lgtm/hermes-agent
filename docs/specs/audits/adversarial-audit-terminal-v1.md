# Adversarial Audit -- security + edge cases

Date: 2026-08-31

## Threats

| ID | Threat | Mitigation | Status |
|----|--------|------------|--------|
| A1 | Socket traversal via multiplex_name "../../etc" | Sanitization + length cap + join under task dir only | SPEC has but needs test |
| A2 | Kill cross-task via spoofed task_id | broadcast_interrupt only kills if caller task_id == session task_id; agent cannot spoof other task (gateway injects task_id). Document trust boundary. | OK, add note |
| A3 | Tmux command injection via command string passed to shell -c | Use tmux send-keys with shlex, never string-interpolate `command` into server args without quoting. | SPEC missing -- add to file manifest |
| A4 | Log injection (cast file) -- ANSI/OSC sequences replayed -> XSS in dashboard | Snapshot must strip ANSI before rendering; cast is raw but viewer must sanitize. | Add security note |
| A5 | DoS: 200KB rolling buffer per session * 16 multiplex = 3.2MB + cast unbounded | Cap cast at 10MB + truncate, per-task max + 429. | To add in v2 |
| A6 | Race: kill during adopt -- adopt_foreground vs concurrent kill_process | Lock _running insertion under registry lock; kill checks exited before SIGTERM | Code does -- spec should assert lock |

## Edge cases
- E1: Silent streaming (progress bar without \n) -- reader counts bytes not lines, still stamps. OK.
- E2: Exit exactly at promote deadline -- promotion checks deadline-now > margin, so near-deadline exits win. Correct per spec.
- E3: pty reader loop for multiplex is tmux-owned; no local reader -- no promotion. Correct.
- E4: which tmux not in PATH on minimal container -- fallback JSON must be stable.
