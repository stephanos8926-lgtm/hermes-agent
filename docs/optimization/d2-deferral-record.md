# D2: `time.sleep(2)` → `asyncio.Event.wait(2)` in compression retry paths

## Status: DEFERRED (as of 2026-08-25, scope verified by Forge agent)

## Background

The Muse planning agent identified three call sites in
`agent/conversation_loop.py` that block the entire turn thread for
two seconds each during a compression retry:

  * `agent/conversation_loop.py:5298`
  * `agent/conversation_loop.py:5602`
  * `agent/conversation_loop.py:5909`

The proposed fix was to replace each `time.sleep(2)` with
`asyncio.Event.wait(2)` so the loop remains cancellable. A 2-second
block is short but it accumulates — a retry storm can stack several
of these, and the gateway shows no progress during the wait.

## Why Deferred

The replacement is **not** a one-line swap. The three call sites
all sit inside a single function, `run_conversation()`
(`agent/conversation_loop.py:1766` signature). The Forge agent
verified the call graph on 2026-08-25:

  1. **`run_conversation` is a synchronous function** — its
     signature is `def run_conversation(agent, user_message, ...)`
     (line 1766). It is not `async def`. `asyncio.Event.wait()`
     and `asyncio.sleep()` both require an async context.

  2. **`run_conversation` is 1,500+ lines long** (line 1766 through
     the end of the file around line 7000+). Converting it to
     `async` cascades through its entire body, including the
     compression-retry block, the tool-call dispatch, the
     response streaming, the message persistence, and the
     compression-leasing primitives.

  3. **Call-graph blast radius** — every caller of
     `run_conversation` must also be `async`. The current callers
     include the TUI gateway methods, the CLI turn entry point,
     the subagent dispatch worker, the cron execution harness,
     and the slash-command invocation path. Converting each is
     a separate refactor.

  4. **Test infrastructure** — the existing tests for the
     compression retry block are sync (`pytest-asyncio` not in
     use for these paths). Converting to async means either
     rewriting the tests as `@pytest.mark.asyncio` or using a
     sync wrapper. The `tests/agent/test_compression_attempt_telemetry.py`
     and `tests/agent/test_idle_compaction_lock_and_guards.py`
     files both exercise the retry path under sync test
     semantics.

  5. **Backwards compatibility** — the compression retry block is
     on the per-turn hot path. A refactor here is in the same
     blast-radius class as Phase 2's `prompt_caching` deepcopies:
     "must not change observable behavior under any input."

The Muse estimate of "1 hour" was off by 3-4x. The Forge agent
updated the realistic cost estimate to **8-12 hours** of careful
async conversion plus the cascading caller conversion plus tests,
with non-trivial risk of breaking the compression-retry path on
inputs that exercise the dead-retry branches.

## Implementation Sketch (for future work)

If/when this work is picked up:

```python
# 1. Make the helper function async.
async def _wait_for_compression_retry_signal(self, timeout: float = 2.0):
    """Block until a retry is requested or the timeout expires.

    Replace the sync time.sleep(2) at lines 5296, 5599, 5905 with
    an async wait so the turn thread stays cancellable.

    A shared asyncio.Event per AIAgent instance (set by a future
    "force retry" tool) is one option. A simpler first pass is
    `await asyncio.sleep(2)` — same wall-clock cost as time.sleep(2)
    but cancellable via Task.cancel().
    """
    await asyncio.sleep(timeout)

# 2. Update the call sites to await the helper.
await _wait_for_compression_retry_signal()
```

A even simpler first step: just replace `time.sleep(2)` with
`asyncio.sleep(2)` and add `await` at the call site. The function
must be `async` for this to work. If the surrounding function
is sync, this becomes a multi-hour refactor.

## Cheaper Alternatives Considered (and Rejected)

The Forge agent considered three cheaper alternatives that avoid
the full async conversion:

  1. **`threading.Event` instead of `time.sleep()`** — sync-friendly
     replacement that would still block the thread but could be
     signalled externally. **Rejected** because the per-turn thread
     is still blocked, which is the actual user-visible problem
     (gateway shows no progress during the wait). Threading.Event
     doesn't help cancellability from the caller's perspective.

  2. **Busy-poll with a cancellation flag** — replace
     `time.sleep(2)` with `time.sleep(0.05); if cancellation_flag: break`
     in a 40-iteration loop. **Rejected** because it's worse: it
     wastes CPU cycles during the wait and provides no real
     improvement over the current `time.sleep(2)`.

  3. **Reduce the sleep duration to 0.5s and add retries** — the
     underlying rate-limit window is typically 60s, not 2s, so the
     2s sleep is arbitrary. **Rejected** because it would change
     observable behavior on the retry path and would need its own
     validation pass.

The only path that actually fixes the problem is the full async
conversion. Given the 8-12 hour cost, the deferral remains the
right call.

## Triggers to Revisit

Pick this up if any of the following land:

  * **User-visible "gateway stuck" reports** — multiple reports
    correlating with compression retries. The current 2-second
    pauses would show up in such traces.
  * **Compression retry storms under heavy load** — observed via
    the `compression_logging_session_context` telemetry. If
    retry counts are climbing, the per-retry 2s penalty is
    amplified.
  * **Gateway cancellation work** — if a separate initiative
    adds cancellable turns (e.g. for the `/stop` slash command
    to be immediate), the sync `time.sleep(2)` becomes the
    bottleneck and a hard refactor blocker.
  * **Async-migration of conversation_loop.py** — if/when the
    whole loop goes async for other reasons, the three sites
    become trivial to fix in the same pass.

## Risk If Reactivated

  * **Medium** — touches the per-turn hot path. Compression
    retry behavior must remain identical under all inputs
    including the dead-retry and the partial-retry branches.
  * **Test surface** — `tests/agent/test_compression_attempt_telemetry.py`
    and `tests/agent/test_idle_compaction_lock_and_guards.py`
    both exercise the retry path. New async variants need to
    preserve the same observable behavior or update the tests
    deliberately (not by accident).
  * **AGENTS.md rule #5** — "Behavior contracts over snapshots"
    applies. The fix must be an invariant preservation, not a
    snapshot match.

## See Also

  * Phase 0 recon — `phase0-recon/phase0-recon.md` (compression
    retry site notes)
  * The plan files: `2026-08-25-hermes-opt-phase-8-d2-future-v1-v1-v1.1.md`
  * The conversation_loop module's existing test files
