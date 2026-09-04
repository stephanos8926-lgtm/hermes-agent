# Skill Auto-Loading — RapidWebs Fork Design

Author: Lucien (RapidWebs) / Steven. Date: 2026-08-11.
Status: Implementation reference.

## Why (upstream context)

Stock Hermes has **no mechanical skill auto-load**. Skills are loaded only when
the model chooses to call `skill_view`, or preloaded at session start via
`--skills` / `HERMES_TUI_SKILLS`. Upstream deliberately kept this prompt-driven
(issue #4589, 337 upvotes) over worries about trigger false-positives, and kept
skill names visible (rejecting lazy-loading because models rediscover poorly).
This fork adds a conservative, config-gated auto-load on top of that design.

## Defaults

- **Code default: OFF.** All of this ships inert unless `skills.auto_load`
  is enabled in config.yaml.
- **Our machine: ON.** We set `skills.auto_load.enabled: true` in our config.

## The spec (A–E)

### A. Trigger-based auto-load (skill content into context)
When an incoming message matches a skill's triggers/description, preload that
skill's full content (SKILL.md) into context — no LLM opt-in needed.

- **Accuracy**: naive substring matching causes false positives (upstream's
  worry). We use a **conservative scorer**:
  - Word-boundary match against skill `triggers` (frontmatter `triggers:` list)
    and/or the skill name.
  - A skill only auto-loads when a trigger is matched AND the score clears a
    `min_confidence` threshold (default 0.6). `phrases` (multi-word) score
    higher than single words.
  - `negative_triggers` subtract score to avoid forcing in the wrong context.
- Config shape:
  ```yaml
  skills:
    auto_load:
      enabled: true          # master switch
      mode: hybrid            # prompt | mechanical | hybrid
      min_confidence: 0.6
      max_auto_load: 3        # max skills auto-loaded per turn
  ```

### B. Auto-load references/templates with caps + truncation + pointers
When a skill IS loaded (via `skill_view`, `--skills`, or auto-load), include a
first chunk of each supporting file, capped per category, with a pointer to load
the rest on demand.

- Per-category count + char caps (config):
  ```yaml
  skills:
    auto_load:
      references_max_count: 4
      references_max_chars: 2000
      templates_max_count: 4
      templates_max_chars: 1500
      scripts_max_count: 2
      scripts_max_chars: 1200
  ```
- Each truncated file gets a pointer line:
  `… (file truncated; load the rest with skill_view(file_path="<path>"))`
- Implementation hook: `_build_skill_message` in `agent/skill_commands.py`
  already lists supporting files. Extend it to *append content* (truncated)
  when auto-load is enabled.

### C. Dedup window (load each skill + dependents once)
Guard against re-injecting the same skill (and its dependents) across a long
session, which inflates context.

- Track loaded skill ids in a session-scoped store.
- Window modes (config `dedup_window`):
  - `once_per_session` (default): never re-inject.
  - `once_per_turns: N`: re-inject after N turns.
  - `once_per_minutes: N`: re-inject after N minutes.
- Implemented as an in-memory dict keyed by session_id → {skill: last_injected},
  checked/populated at every auto-load/preload site.

### D. Max reference count + recursion guard
- Hard cap on the total number of files auto-loaded from any single skill graph
  (`max_reference_files`, default 8).
- **Cycle guard**: when a skill references another skill (via `related_skills`),
  track a visited set; never revisit a skill already loaded on the current
  branch. Depth cap (`max_depth`, default 6) as a second backstop. This
  guarantees termination even for cyclic skill graphs (A → B → A).

### E. Everything configurable in config.yaml
All knobs live under `skills.auto_load` (see shapes above). No new env vars
(AGENTS.md: config.yaml is for settings, .env is for secrets). The code reads a
merged config dictionary; a single `load_auto_load_config()` helper centralizes
defaults so the rest of the code can't drift.

## Files touched

| File | Change |
|------|--------|
| `hermes_cli/config.py` | Add `skills.auto_load` defaults to DEFAULT_CONFIG / validation |
| `agent/skill_utils.py` | `parse_frontmatter` surface; scoring helpers for (A); config loader (`load_auto_load_config`) |
| `agent/skill_commands.py` | `_build_skill_message` gains reference-content auto-load (B); `build_preloaded_skills_prompt` gains dedup (C) + recursion guard (D) |
| `agent/skill_auto_load.py` (NEW) | Central engine: trigger scorer (A), cap/truncation (B), dedup store (C), recursion guard (D) |
| `tools/skills_tool.py` | `skill_view` calls the auto-load engine for reference expansion (B) |
| `gateway/run.py` | (optional) call trigger auto-load on incoming message |

## Testing

- `tests/agent/test_skill_auto_load.py`:
  - A: trigger scoring (word-boundary, min_confidence, negative_triggers)
  - B: char/count caps + pointer line appended; no content when disabled
  - C: dedup — second load skipped within window; re-loads after turns/minutes
  - D: cyclic skill graph terminates; max_reference_files honored
  - E: defaults loaded from config; enable/disable toggle works
- Run: `env HERMES_HOME=$(mktemp -d) .venv/bin/python -m pytest tests/agent/test_skill_auto_load.py`

## Safety

- All read-only; never mutates skills or repo.
- Off by default; enabled only in our config.
- Time/char-bounded so auto-load can never starve context.
- `max_auto_load` + dedup + recursion guard prevent infinite/unbounded growth.