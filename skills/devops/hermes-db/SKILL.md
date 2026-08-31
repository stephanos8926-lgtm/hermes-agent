---
name: hermes-db
category: devops
description: SQLite maintenance CLI for the Hermes agent runtime. Use when running integrity checks, VACUUM, or schema repair on state.db / lcm.db.
keywords: [sqlite, integrity_check, vacuum, state.db, lcm.db, fts, wal, hermes db, repair, schema, migration]
triggers:
  - "hermes db"
  - "hermes db check"
  - "hermes db vacuum"
  - "hermes db repair"
  - "hermes db stats"
  - "hermes db list"
version: 1.0.0
---

# hermes db — SQLite Maintenance CLI

Surfaced from existing `hermes_state.py` primitives (`repair_state_db_schema`,
`quarantine_zeroed_state_db`, `collect_state_db_stats`) plus a local LCM-DB
integrity check. Read-only by default; explicit opt-in for write actions.

## See Also

- `hermes-cache` — the in-process LRU + mmap tiered cache infrastructure
- `disk-cleanup` v3.1.0+ — auto-prune logs and rotate `.bak` files on session end
- **docs/operations/extempfail-sentinel.md** — why gateway exit code 75 is correct, not a bug

## When to Use This Skill

- state.db has grown past 1 GB and needs VACUUM
- lcm.db needs an integrity check after a crash
- A migration failed and you need to repair the schema
- You want a stats snapshot before/after a cleanup
- You want to enumerate all known sqlite databases

## Quick Reference

```bash
hermes db check                 # integrity_check + quick_check (read-only)
hermes db check --lcm           # also probe the LCM context store
hermes db stats                 # page count, WAL size, FTS presence, etc.
hermes db vacuum                # wal_checkpoint(TRUNCATE) + VACUUM
hermes db repair                # full schema repair (writes)
hermes db list                  # enumerate known databases
```

## Decision Tree

| Symptom | Action |
|---------|--------|
| state.db > 1 GB | `hermes db vacuum` |
| Crashed during write | `hermes db check state.db` → `hermes db repair` if not OK |
| Migration failed | `hermes db repair state.db` (with --no-backup for dev) |
| LCM store corrupted | `hermes db check --lcm` |
| Just want a snapshot | `hermes db stats` |
| Don't know what's there | `hermes db list` |

## Action Details

### `check`

Read-only. Runs `PRAGMA integrity_check` (the gold-standard verifier) plus
`PRAGMA quick_check` (faster heuristic). Safe against a live database held
by the gateway. Use this first; only escalate to `repair` if `check` reports
an issue.

### `stats`

Calls `collect_state_db_stats()` for `state.db` or the local equivalent
for `lcm.db`. Returns page count, WAL size, row counts, FTS presence,
and journal mode. Use before/after a `vacuum` to verify the gain.

### `vacuum`

The expensive action. Runs `PRAGMA wal_checkpoint(TRUNCATE)` first to
collapse the WAL, then `VACUUM` to rebuild the database. May take
minutes on multi-GB databases. Only call manually or from a scheduled job.

### `repair`

Writes. For `state.db`, uses `repair_state_db_schema` (full schema repair
with a `repair_state_db_schema_locked` cross-process lock to prevent
concurrent gateway writes). For other targets, runs `check` first and
only proceeds to a `vacuum` if needed. With `--no-backup`, skips the
pre-repair backup (use only in dev).

### `list`

Enumerates all known sqlite databases: `state.db`, `kanban.db`,
`plugins/hermes-lcm/lcm.db`. Returns path, size, and last-modified time.

## What `hermes db` Does NOT Do

- **Migrate schemas.** Schema migration is a separate concern, handled by
  the migration runner in `hermes_state.py`. Use the migration runner for
  schema version bumps.
- **Repair *the gateway's in-memory state*.** If the gateway has a
  corrupted in-memory session, restart it. `hermes db` operates on
  persistent storage only.
- **Tune sqlite parameters.** This CLI uses safe defaults. For
  performance tuning (e.g. `cache_size`, `mmap_size`), edit
  `hermes_state.py` directly with a maintainer's review.

## See Also

- `skills/devops/hermes-cache/SKILL.md` — companion skill for the
  tiered cache subsystem, uses the same config-driven feature-gate
  pattern.
- `plugins/disk-cleanup/` — handles file-system hygiene around the
  sqlite files; v3.1.0+ has a `vacuum-dbs` slash subcommand that wires
  this CLI into the on_session_end hook.
