"""``hermes db`` subcommand parser.

Thin CLI surface over the existing state.db maintenance primitives in
``hermes_state.py`` (repair_state_db_schema, quarantine_zeroed_state_db,
collect_state_db_stats) plus an LCM-DB integrity check.

Why a new subcommand instead of expanding ``hermes sessions``:

* ``hermes sessions`` is per-session focused (list/show/optimize). DB
  maintenance is a cross-cutting concern (schema repair, integrity
  check, VACUUM) that doesn't fit the per-session model.
* The LCM store is a separate sqlite database with its own schema;
  bundling it under ``sessions`` would conflate the two.

Per the project's footprint ladder (AGENTS.md), a new CLI command +
``hermes db`` skill is the right surface for this — no core-tool
registration needed.
"""
from __future__ import annotations

import argparse
from typing import Callable


def build_db_parser(subparsers, *, cmd_db: Callable) -> None:
    """Attach the ``db`` subcommand to ``subparsers``."""
    db_parser = subparsers.add_parser(
        "db",
        help="Inspect and maintain Hermes sqlite databases",
        description=(
            "Check integrity, gather stats, vacuum, and repair the "
            "primary state.db plus the LCM context-engine store."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
    hermes db check                Integrity-check state.db (read-only)
    hermes db check --lcm          Also integrity-check lcm.db
    hermes db stats                Show row counts, page counts, WAL size
    hermes db vacuum state.db      Run VACUUM + WAL checkpoint
    hermes db vacuum lcm.db        Vacuum the LCM context store
    hermes db repair               Attempt automatic schema repair
    hermes db list                 List all known sqlite databases
""",
    )
    db_parser.add_argument(
        "action",
        choices=["check", "stats", "vacuum", "repair", "list"],
        help="Which maintenance action to perform",
    )
    db_parser.add_argument(
        "target",
        nargs="?",
        default="state.db",
        help=(
            "Which database to act on (default: state.db). "
            "For 'list' this is ignored."
        ),
    )
    db_parser.add_argument(
        "--lcm",
        action="store_true",
        help=(
            "For 'check' and 'stats': also include the LCM context "
            "store alongside state.db."
        ),
    )
    db_parser.add_argument(
        "--no-backup",
        action="store_true",
        help=(
            "For 'repair': skip the pre-repair backup. The repair "
            "code takes a backup by default; this flag suppresses it "
            "for fully automated runs where a snapshot already exists."
        ),
    )
    db_parser.set_defaults(func=cmd_db)
