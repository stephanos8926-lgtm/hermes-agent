# D2: `time.sleep(2)` → `asyncio.Event.wait(2)` in compression retry paths

## Status: DEFERRED (as of 2026-08-25)

## Background

The Muse planning agent identified three call sites in
`agent/conversation_loop.py` that block the entire turn thread for
two seconds each during a compression retry:

  * `agent/conversation_loop.py:5296`
  * `agent/conversation_loop.py:5599`
  * `agent/conversation_loop.py:5905`

The proposed fix was to replace each `time.sleep(2)` with
`asyncio.Event.wait(2)` so the loop remains cancellable. A 2-second
block is short but it accumulates — a retry storm can stack several
of these, and the gateway shows no progress during the wait.

## Why Deferred

The replacement is **not** a one-line swap. The three call sites
sit inside sync functions; `asyncio.Event` requires the calling
function to be `async`. Converting those functions to `async`
cascades into the entire compression-retry block, including:

  1. **Function signature change** — every caller of the surrounding
     function must already be async (it is, but the audit needs to
     cover the full call graph to be safe).
  2. **Await propagation** — the new async functions must `await`
     the new helpers, which means every site that calls them
     (potentially 4-5 levels deep) must be async too.
  3. **Test infrastructure** — the existing tests for the
     compression retry block are sync (`pytest-asyncio` not in use
     for these paths). Converting to async means either rewriting
     the tests as `@pytest.mark.asyncio` or using a sync wrapper.
  4. **Backwards compatibility** — the compression retry block is
     on the per-turn hot path. A refactor here is in the same
     blast-radius class as Phase 2's prompt_caching deepcopies —
     "must not change observable behavior under any input."

The Muse estimate of "1 hour" was off by 2-3x. Realistic cost:
**3-4 hours** of careful async conversion plus tests, with
non-trivial risk of breaking the compression-retry path on
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
