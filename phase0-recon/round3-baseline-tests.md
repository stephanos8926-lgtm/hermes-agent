# Round 3 Baseline — Test Failure Set (Phase 0b)

**Captured:** 2026-08-25, post venv repair (requests, python-dotenv, ruamel.yaml installed)
**Branch:** feature/rw-repatch @ e2ffafc455 (C3 tip)
**Command:** pytest over the 16-file round-3 verification set, `--tb=no -q`

## Result

```
1 failed, 727 passed in 18.31s
```

## The Canonical Pre-Existing Failure Set

| Test | Error | Verified pre-existing via |
|---|---|---|
| `tests/test_hermes_state.py::TestFTS5Search::test_search_projection_skips_context_enrichment_queries` | `assert 0 == 1` (context-enrichment query count) | `git stash` + retest on clean main, multiple sessions |

## Regression Rule for Phases I1–I3, C5, C7, C4

> "No regressions" = the failure set after each phase is **exactly this set**.
> Any NEW failure not in this table is a regression caused by that phase's changes
> and must be fixed or the phase rolled back before proceeding.

## Notes

- Venv repair eliminated all prior `ModuleNotFoundError: requests` /
  `dotenv` / `ruamel.yaml` cascading failures. Those tests now pass.
- Do NOT "fix" the FTS5 failure during Round 3 — it is out of scope and
  tracked separately.
