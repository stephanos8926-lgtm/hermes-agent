# Round 3 — RSS Baseline Snapshot

**Captured:** 2026-08-25 ~22:20 EDT, post apt-upgrade (kernel 6.12.105-1), gateway uptime ~2h since Phase-5 restart.

## Gateway Process (PID 90453)

| Metric | Value | Notes |
|---|---|---|
| `VmRSS` | **178,056 kB (~174 MiB)** | Current resident set |
| `VmHWM` | 178,816 kB (~175 MiB) | Peak RSS this process lifetime |
| `VmPeak` | 4,479,028 kB (~4.3 GiB) | Virtual — not meaningful for pressure; ignore |
| Command | `python -m hermes_cli.main gateway run` | Runtime install at `~/.hermes/hermes-agent` |

## Companion Processes

| PID | Cmd | RSS |
|---|---|---|
| 90473 | `mcp_stdio_watchdog` (rw-ast-tools socat bridge) | 18,744 kB (~18 MiB) |

## Interpretation

- Current steady-state RSS is **~174 MiB**, well below the 250–440 MiB range seen in the
  `gateway-exit-diag.log` heartbeats from the prior day. Two explanations:
  1. This process is only ~2h old (restarted during Round 2 verification) — the climb
     to 250–440 MiB likely takes longer than 2h of session churn.
  2. The heartbeat samples were taken at exit time after heavy multi-session days.
- **This confirms C4's motivation but tempers expectations**: the growth curve, not the
  starting point, is what matters. The soak criteria (S1/S2 in plan §10) use slope and
  regression-vs-baseline, both of which are now measurable against this snapshot.

## Soak Comparison Anchor

For the C4 24h soak gate:
- Baseline RSS at T0: **~174 MiB**
- S1 pass requires: soak-period RSS ≤ baseline + 10% at equivalent workload
- S2 pass requires: growth slope ≤ 5 MiB/hour sustained

## Method

```
grep -E "VmRSS|VmPeak|VmHWM" /proc/$(pgrep -f "hermes_cli.main gateway run")/status
```
