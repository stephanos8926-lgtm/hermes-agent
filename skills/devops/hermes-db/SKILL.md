---
name: hermes-db
description: Inspect, validate, and maintain Hermes sqlite databases (state.db, lcm.db, kanban).
version: 1.0.0
author: RapidWebs + Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [sqlite, maintenance, vacuum, integrity, hermes-cli]
    category: devops
    requires_toolsets: [cli]
    commands: [hermes db]
environments:
  - cli
---

# Hermes DB Skill

Surface-level operational guide for the `hermes db` CLI subcommand group. The skill teaches the agent when to use each action and how to interpret the output. The actual implementation lives in `hermes_cli/subcommands/db.py` and `hermes_cli/subcommands/db_handler.py` — this skill is documentation and decision support, not a separate runtime.

## When to Use

Use this skill when **any** of the following are true:

- The user asks about state.db corruption, WAL growth, or "why is my sessions.db huge"
- The user wants to know how to vacuum / reindex / optimize the LCM store
- The agent itself is exhibiting symptoms of a corrupt state.db (lost sessions, weird errors, repeated restarts)
- A scheduled maintenance job is being set up and needs a `hermes db` invocation
- The user asks "what databases does hermes use"

Do **not** use this skill for general sqlite questions unrelated to Hermes databases. The skill is scoped to the Hermes-specific databases and their schema quirks.

## Commands

| Action | Read-only? | Purpose |
|--------|-----------|---------|
| `hermes db list` | yes | Enumerate every known Hermes sqlite database with size and mtime |
| `hermes db check [target]` | yes | Run `PRAGMA integrity_check` + `quick_check` against the target DB |
| `hermes db stats [target]` | yes | Page count, WAL size, row counts, FTS presence, journal mode |
| `hermes db vacuum [target]` | **mutating** | `PRAGMA wal_checkpoint(TRUNCATE)` + `VACUUM` (reclaims space) |
| `hermes db repair [target]` | **mutating** | For state.db, calls the existing `repair_state_db_schema` zeroed-DB recovery path |

The optional `[target]` argument is one of: `state.db` (default), `lcm.db`, `kanban`. The `--lcm` flag extends `check` / `stats` to also probe the LCM context store (~/.hermes/plugins/hermes-lcm/lcm.db). The `--no-backup` flag on `repair` skips the safety backup that the action takes by default.

## Workflow

When asked to investigate a database issue:

1. **Read-only first.** Start with `hermes db list` to see what's there, then `hermes db check state.db` to verify integrity. Never jump to a mutating action without first understanding the state.
2. **WAL bloat.** A multi-gigabyte WAL is the most common "the db is huge" complaint. Run `hermes db stats state.db` to see the journal mode and WAL size. If WAL is large, run `hermes db vacuum state.db` to checkpoint + truncate.
3. **Corruption symptoms.** If the user reports sessions disappearing, crashes mid-write, or "sqlite3.DatabaseError: database disk image is malformed", the path is `check` → `repair`. The `repair` action is safe-by-default: it makes a backup before any write.
4. **LCM store.** The LCM context store is separate from state.db and has its own schema. Use the `--lcm` flag to extend `check` / `stats` to include it. A standalone LCM `repair` is **not** implemented — fall through to the integrity check + manual `vacuum` for now.
5. **Scheduled jobs.** For cron-style invocation, the read-only actions are safe to run at any time. `vacuum` and `repair` should be run during low-traffic windows (e.g., `on_session_end` from a `disk-cleanup` plugin, or a `cron` schedule outside the user's working hours).

## Output interpretation

- `hermes db check` returns one of `ok`, `ok` (with warnings), or a multi-line report of the integrity issues found. A clean `ok` is the expected result.
- `hermes db stats` returns a JSON-shaped block with `pages`, `page_size`, `wal_size_bytes`, `journal_mode`, and per-table row counts. The two numbers to watch are `wal_size_bytes` (if this is much larger than `pages × page_size`, vacuum will reclaim a lot) and any zero-row tables that you expect to have data in.
- `hermes db list` returns one line per database: path, size in bytes, mtime. Use this to spot databases that have grown unexpectedly or that haven't been touched in weeks (the latter may be a sign of a broken plugin).

## Non-goals

This skill is intentionally narrow:

- It does **not** teach the agent how to interpret Hermes state.db schema details — that's a separate skill (`hermes-state-inspector` if/when it exists).
- It does **not** include the disk-cleanup plugin's vacuum integration — that lives in the plugin's own `on_session_end` hook and is documented there.
- It does **not** cover the LCM `quarantine` or `deferral` features — those are LCM-plugin-specific and out of scope.

## Feature gating

All cache subcommand behavior is feature-gated by the `cache:` block in `~/.hermes/config.yaml` (with `HERMES_CACHE_*` env-var overrides) — see `agent/_cache.py`. The `hermes db` subcommand itself is **not** feature-gated: it is a maintenance tool, and the user invoking it is the feature gate.
