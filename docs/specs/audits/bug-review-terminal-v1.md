# Bug Review -- logic / quality

Date: 2026-08-31

## High
- B1: _compute_silent_fields int() truncates sub-second: silent 59.9 -> 59, poll shows 59 while promotion at 60000ms uses float idle_s*1000 >= ms, so 59s shown then promote 100ms later. Minor UX, not bug; document.

## Medium
- B2: Drain tail flush uses errors="replace" -- U+FFFD emitted for incomplete UTF-8 split across read boundary. Acceptable, note in spec.
- B3: Adopted session inherits output_buffer partial head/tail but not spill spill_path handle -- poll full tail may be truncated after adopt. Need to carry spill_path into ProcessSession.
- B4: Phase0-recon logs untracked (phase0-recon/*.log) -- should be gitignored.

## Low
- B5: launch_site.py uses bufsize=line buffering typo? Not in scope.
- B6: WATCH_STRIKE_LIMIT=3 independent of promotion -- promotion should not reset strike count.
