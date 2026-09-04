# Reverse Audit -- gaps the specs missed

Date: 2026-08-31

## Critical gaps (would block merge if multiplex shipped without fix)

| ID | Gap | Risk |
|----|-----|------|
| R1 | Multiplex socket TOCTOU: spec says 0700 but not who chowns GC on reboot vs linger. If socket dir is XDG_RUNTIME_DIR it vanishes on reboot -- need fallback and doc. | stale attach_cmd after reboot |
| R2 | multiplex_name sanitization: spec says [^a-zA-Z0-9_-]->"_" but not length 64 vs 8-hex collision when two names sanitize to same string. Need suffix or error. | session hijack |
| R3 | Spill file lifecycle for promoted foreground: spec mentions bounded_capture but not that promoted payload must include full_output_path when truncated, else agent cannot retrieve tail. | lost output OOM tail |
| R4 | Concurrent hangs: two foreground terminals hanging simultaneously -- promotion must not interleave _promotion_event (shared list vs per-proc). Current code uses per-wait list -- spec should assert isolation. | cross-promotion |
| R5 | Checkpoint recovery: adopted sessions written via _write_checkpoint -- spec lacks acceptance for gateway restart re-attach. Should add poll still succeeds after restart. | orphan after restart |
| R6 | Sandbox promo error shape: spec says fallback but not exact JSON keys (multiplex_unsupported, fallback). Need canonical error code for agent to branch. | brittle client |
| R7 | wait_for polling interval for tmux: spec says 200ms but not max wait vs promote_margin interaction -- wait must not also trigger promotion on same fd. Mutex needed. | double trigger |
| R8 | Max sessions per task: spec says 16 but no enforcement described, no LRU or 429 shape. | FD/bus DoS |

## Medium gaps
- R9: ADR-4 does not discuss screen/zellij license/CI weight; add.
- R10: ADR-5 asciicast-v3 growth -- needs size cap/rotation (10MB per cast).
- R11: Spec lacks explicit deny for multiplex+background=true mutex message.
- R12: No mention of pty resize propagation or cols/rows defaults.
