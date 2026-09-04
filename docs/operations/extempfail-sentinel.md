# `EX_TEMPFAIL` (75) — The Gateway Restart Sentinel

> **If you see `exit-code=75` in your gateway logs, that is correct behavior, not a bug.**

## What it is

`EX_TEMPFAIL` is exit code **75**, defined in BSD's [`sysexits.h`](https://man.openbsd.org/sysexits.h). The canonical comment from the header is:

> "Failure of a (normally) temporary nature. The user is encouraged to try again. This is the canonical "try again" exit code."

Hermes uses this exit code **intentionally** as a sentinel to ask the system service manager (systemd on Linux, launchd on macOS) to **cleanly restart the gateway process** without raising an error.

## Where it is used

| File | Line | What happens |
|------|------|--------------|
| `gateway/restart.py` | 8-10 | Defines `GATEWAY_SERVICE_RESTART_EXIT_CODE = 75` with a comment "used to ask the service manager to restart" |
| `gateway/restart.py` | 78 | Drain-then-restart helper exit path |
| `gateway/run.py` | 15293-15299 | Drain finished -> return TEMPFAIL/75 sentinel for systemd revival |
| `gateway/run.py` | 31176-31181 | Alternate drain path with the same exit semantics |
| `hermes_cli/kanban_db.py` | 422-430 | Kanban rate-limit retry path also uses 75 so the kanban process can re-poll without raising an error in the journal |

## How systemd is configured to honor it

The unit file at `scripts/hermes-gateway` (line 92) declares:

```
RestartForceExitStatus=75
```

This tells systemd: "when the gateway process exits with code 75, treat it as a successful restart request — relaunch the service normally and do not log an error."

The result is a **healthy restart loop** in the user's logs:

```
hermes-gateway.service: Scheduled restart job, restart counter is at N.
Started hermes-gateway.service.
hermes-gateway.service: Main process exited, code=exited, status=75/n/a
```

The "Main process exited, code=exited, status=75/n/a" line is the systemd journal acknowledging the restart request. There is **no error** and **no failure** in the journalctl output.

## Why not just `Restart=always`?

systemd's default `Restart=on-failure` triggers a restart on abnormal exits (crashes, non-zero codes that aren't explicitly handled). `RestartForceExitStatus=75` is **more precise**: it forces a restart *only* on this specific exit code, which means an actual crash (segfault, OOM kill, Python exception) will still surface as a real error in the journal.

The combination is:

  * `Restart=on-failure` — restart on any non-zero exit (handles crashes)
  * `RestartForceExitStatus=75` — explicitly include 75 in the "expected restart" set
  * 75 = the canonical "I'm done, please restart me" signal

## How to read the logs

The `restart_loop.json` file at `~/.hermes/gateway/restart_loop.json` records the recent restart history. The diagnostic log at `~/.hermes/logs/gateway-exit-diag.log` records the most recent exit reason. **These are intentional artifacts of the design, not bugs.**

If you see:

  * `TEMPFAIL` in the gateway stdout/stderr — that is the gateway announcing its own intent to exit
  * `code=exited, status=75/n/a` in journalctl — that is systemd acknowledging the restart
  * `Scheduled restart job` followed by `Started hermes-gateway.service` — that is the system working as designed

…all three are the **healthy** path.

## When to worry

You should investigate if you see:

  * Exit codes OTHER than 75 in `restart_loop.json` (especially 1, 134, 137, 139 — segfault, OOM kill, abort)
  * `journalctl` showing "Main process exited, code=killed" or "code=dumped" instead of "code=exited, status=75"
  * `Scheduled restart job` without a subsequent `Started hermes-gateway.service` (the service is failing to start at all)
  * Restart counter incrementing faster than once per minute (suggests a crash loop, not a drain)

For a healthy drain-then-restart cycle, the expected sequence is:

  1. Gateway receives SIGTERM (or operator runs `hermes gateway restart`)
  2. Gateway drains in-flight turns (may take seconds to minutes depending on workload)
  3. Gateway logs "Drain finished. Returning TEMPFAIL/75 sentinel for systemd revival"
  4. Gateway exits with code 75
  5. systemd sees 75, sees `RestartForceExitStatus=75`, restarts the service
  6. New gateway process boots, opens its DB, takes over

## Do NOT "fix" this

A common refactor instinct is to remove the 75 exit code in favor of `Restart=always` or to log the exit as an error. **Do not do this.** The 75 sentinel is the design's way of distinguishing:

  * **Intentional drain-and-restart** (code 75 — no error)
  * **Crashes and unexpected exits** (any other code — error, surface in journal)

Removing the distinction would mask real crashes behind routine restarts and make the user's log noise floor much higher.

## Related

  * `gateway/restart.py:8-10` — definition of the sentinel constant
  * `hermes_cli/kanban_db.py:422-430` — same pattern in the kanban subsystem
  * AGENTS.md rule #6 — "Don't wire in dead code without E2E validation" (the 75 path is the "live" version of this concern)
