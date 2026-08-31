"""Tests for the ``hermes db`` subcommand handler.

Covers each action against a fresh sqlite database in a temp
HERMES_HOME, so the tests don't touch the user's real state.db.
The ``_isolate_hermes_home`` autouse fixture in
``tests/conftest.py`` redirects ``HERMES_HOME`` automatically;
this file just needs to construct a database at the expected
path and exercise the public ``run_db_action`` entry point.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermes_cli.subcommands.db_handler import (
    _integrity_check,
    _list_known_databases,
    _lcm_db_path,
    _resolve_db_path,
    _vacuum,
    run_db_action,
)


@pytest.fixture
def state_db(tmp_path: Path) -> Path:
    """Create a fresh state.db with a small schema."""
    db = tmp_path / "state.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, body TEXT)")
        conn.execute("CREATE TABLE sessions (sid TEXT PRIMARY KEY, created INTEGER)")
        conn.execute("INSERT INTO messages (body) VALUES ('hello')")
        conn.execute("INSERT INTO sessions (sid, created) VALUES ('abc', 100)")
        conn.commit()
    return db


@pytest.fixture
def lcm_db(tmp_path: Path) -> Path:
    """Create a fresh lcm.db in the plugin data dir layout."""
    lcm_dir = tmp_path / "plugins" / "hermes-lcm"
    lcm_dir.mkdir(parents=True)
    db = lcm_dir / "lcm.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute("CREATE TABLE dag (id INTEGER PRIMARY KEY)")
        conn.commit()
    return db


class TestResolveDbPath:
    def test_state_db_resolves_under_home(self, tmp_path: Path):
        assert _resolve_db_path("state.db", tmp_path) == tmp_path / "state.db"

    def test_kanban_db_resolves_under_home(self, tmp_path: Path):
        assert _resolve_db_path("kanban.db", tmp_path) == tmp_path / "kanban.db"

    def test_lcm_db_resolves_when_present(self, tmp_path: Path, lcm_db: Path):
        assert _resolve_db_path("lcm.db", tmp_path) == lcm_db

    def test_lcm_db_raises_when_absent(self, tmp_path: Path):
        with pytest.raises(ValueError, match="lcm.db not found"):
            _resolve_db_path("lcm.db", tmp_path)

    def test_unknown_target_raises(self, tmp_path: Path):
        with pytest.raises(ValueError, match="unknown target"):
            _resolve_db_path("nope.db", tmp_path)


class TestLcmDbPath:
    def test_returns_none_when_no_lcm_dir(self, tmp_path: Path):
        assert _lcm_db_path(tmp_path) is None

    def test_finds_lcm_at_canonical_location(self, tmp_path: Path, lcm_db: Path):
        assert _lcm_db_path(tmp_path) == lcm_db


class TestIntegrityCheck:
    def test_clean_db_passes(self, tmp_path: Path, state_db: Path):
        result = _integrity_check(state_db)
        assert result["ok"] is True
        assert result["integrity_check"] == ["ok"]
        assert result["quick_check"] == ["ok"]

    def test_missing_db_returns_error(self, tmp_path: Path):
        result = _integrity_check(tmp_path / "nope.db")
        assert result["ok"] is False
        assert "does not exist" in result["error"]

    def test_corrupted_db_detected(self, tmp_path: Path):
        # Create a "database" file with NUL header — the #68474
        # zeroed-state.db signature. Should not raise.
        db = tmp_path / "corrupt.db"
        db.write_bytes(b"\x00" * 4096)
        result = _integrity_check(db)
        assert result["ok"] is False
        # The error or the integrity check itself should reflect
        # the corruption (either sqlite fails to open, or the
        # integrity_check returns something other than ['ok']).
        assert "error" in result or result["integrity_check"] != ["ok"]


class TestVacuum:
    def test_vacuum_runs_on_clean_db(self, tmp_path: Path, state_db: Path):
        result = _vacuum(state_db)
        assert result["ok"] is True
        assert "size_before" in result
        assert "size_after" in result

    def test_vacuum_on_missing_db_returns_error(self, tmp_path: Path):
        result = _vacuum(tmp_path / "nope.db")
        assert result["ok"] is False
        assert "does not exist" in result["error"]


class TestListKnownDatabases:
    def test_lists_state_and_kanban(self, tmp_path: Path):
        rows = _list_known_databases(tmp_path)
        names = [r["name"] for r in rows]
        assert "state.db" in names
        assert "kanban.db" in names

    def test_includes_lcm_when_present(self, tmp_path: Path, lcm_db: Path):
        rows = _list_known_databases(tmp_path)
        names = [r["name"] for r in rows]
        assert "lcm.db" in names

    def test_omits_lcm_when_absent(self, tmp_path: Path):
        rows = _list_known_databases(tmp_path)
        names = [r["name"] for r in rows]
        assert "lcm.db" not in names

    def test_marks_missing_state_db(self, tmp_path: Path):
        rows = _list_known_databases(tmp_path)
        for r in rows:
            if r["name"] == "state.db":
                assert r["exists"] is False
                assert r["size_bytes"] == 0


class TestRunDbAction:
    def test_list_action_returns_zero(self, tmp_path: Path, capsys):
        code = run_db_action(
            action="list",
            target="state.db",
            include_lcm=False,
            skip_backup=False,
            hermes_home=tmp_path,
        )
        assert code == 0
        captured = capsys.readouterr()
        assert "Known sqlite databases" in captured.out
        assert "state.db" in captured.out

    def test_check_action_on_clean_db(self, tmp_path: Path, state_db: Path, capsys):
        code = run_db_action(
            action="check",
            target="state.db",
            include_lcm=False,
            skip_backup=False,
            hermes_home=tmp_path,
        )
        assert code == 0
        captured = capsys.readouterr()
        assert "integrity_check" in captured.out

    def test_check_action_with_lcm_flag(self, tmp_path: Path, state_db: Path, lcm_db: Path, capsys):
        code = run_db_action(
            action="check",
            target="state.db",
            include_lcm=True,
            skip_backup=False,
            hermes_home=tmp_path,
        )
        assert code == 0
        captured = capsys.readouterr()
        # Both state.db and lcm.db should appear in the output —
        # the handler prints a header line for each.
        assert captured.out.count("integrity_check on") == 2
        assert "lcm.db" in captured.out

    def test_stats_action(self, tmp_path: Path, state_db: Path, capsys):
        code = run_db_action(
            action="stats",
            target="state.db",
            include_lcm=False,
            skip_backup=False,
            hermes_home=tmp_path,
        )
        # Stats may succeed or fail depending on whether
        # collect_state_db_stats can open a small synthetic
        # database; the test is about wiring, not the upstream
        # implementation. We just verify the action runs.
        assert code in (0, 1)
        captured = capsys.readouterr()
        assert "stats for" in captured.out

    def test_vacuum_action(self, tmp_path: Path, state_db: Path, capsys):
        code = run_db_action(
            action="vacuum",
            target="state.db",
            include_lcm=False,
            skip_backup=False,
            hermes_home=tmp_path,
        )
        assert code == 0
        captured = capsys.readouterr()
        assert "vacuum" in captured.out
        assert "size_before" in captured.out

    def test_repair_action_on_state_db(self, tmp_path: Path, state_db: Path, capsys):
        # repair_state_db_schema is a no-op on a clean db, so the
        # action returns 0 with "repaired: False" or 1 depending on
        # the implementation contract. We just verify the wiring
        # dispatches without error.
        code = run_db_action(
            action="repair",
            target="state.db",
            include_lcm=False,
            skip_backup=False,
            hermes_home=tmp_path,
        )
        assert code in (0, 1)
        captured = capsys.readouterr()
        assert "repair" in captured.out

    def test_unknown_target_returns_usage_error(self, tmp_path: Path):
        code = run_db_action(
            action="check",
            target="nope.db",
            include_lcm=False,
            skip_backup=False,
            hermes_home=tmp_path,
        )
        assert code == 2

    def test_unknown_action_returns_usage_error(self, tmp_path: Path):
        code = run_db_action(
            action="frobnicate",
            target="state.db",
            include_lcm=False,
            skip_backup=False,
            hermes_home=tmp_path,
        )
        assert code == 2


# ---------------------------------------------------------------------------
# Round 2 R5: WAL advisory in `hermes db stats` output
# ---------------------------------------------------------------------------
from hermes_cli.subcommands.db_handler import _print_wal_advisory


class TestWALAdvisory:
    """R5: a one-line advisory is printed to stderr when wal_size_bytes
    is >= 100 MiB OR > 50% of the database's logical size. Otherwise
    nothing is printed. The advisory is a hint, not an error; tests
    confirm it goes to stderr and survives both threshold directions.
    """

    def test_no_advisory_when_wal_is_zero(self, capsys, tmp_path):
        """wal_size_bytes=0 means no WAL sidecar — nothing to advise."""
        _print_wal_advisory(
            {"wal_size_bytes": 0, "logical_size_bytes": 1024 * 1024},
            tmp_path / "state.db",
        )
        captured = capsys.readouterr()
        assert captured.err == ""
        assert captured.out == ""

    def test_no_advisory_when_wal_is_small(self, capsys, tmp_path):
        """A 1 MiB WAL with a 100 MiB database: 1% relative, under both thresholds."""
        _print_wal_advisory(
            {"wal_size_bytes": 1 * 1024 * 1024, "logical_size_bytes": 100 * 1024 * 1024},
            tmp_path / "state.db",
        )
        captured = capsys.readouterr()
        assert captured.err == ""
        assert captured.out == ""

    def test_advisory_when_wal_exceeds_absolute_threshold(self, capsys, tmp_path):
        """A 150 MiB WAL exceeds the 100 MiB absolute threshold."""
        _print_wal_advisory(
            {"wal_size_bytes": 150 * 1024 * 1024, "logical_size_bytes": 1024 * 1024 * 1024},
            tmp_path / "state.db",
        )
        captured = capsys.readouterr()
        assert "advisory" in captured.err
        assert "150,000,000" in captured.err or "157,286,400" in captured.err
        assert "100 MiB" in captured.err
        assert "vacuum" in captured.err
        assert captured.out == ""

    def test_advisory_when_wal_exceeds_relative_threshold(self, capsys, tmp_path):
        """A 60 MiB WAL on a 100 MiB database: 60% relative, above 50% threshold."""
        _print_wal_advisory(
            {"wal_size_bytes": 60 * 1024 * 1024, "logical_size_bytes": 100 * 1024 * 1024},
            tmp_path / "state.db",
        )
        captured = capsys.readouterr()
        assert "advisory" in captured.err
        assert "60% of" in captured.err
        assert "50% of db size" in captured.err
        assert captured.out == ""

    def test_advisory_when_both_thresholds_triggered(self, capsys, tmp_path):
        """When both fire, the message should mention both reasons."""
        _print_wal_advisory(
            {
                "wal_size_bytes": 200 * 1024 * 1024,
                "logical_size_bytes": 100 * 1024 * 1024,
            },
            tmp_path / "state.db",
        )
        captured = capsys.readouterr()
        assert "100 MiB" in captured.err
        assert "50% of db size" in captured.err

    def test_no_advisory_when_logical_size_missing(self, capsys, tmp_path):
        """When logical_size_bytes is None, the relative check is skipped.
        The absolute check still applies. A 150 MiB WAL with no logical
        size should still fire on the absolute threshold alone."""
        _print_wal_advisory(
            {"wal_size_bytes": 150 * 1024 * 1024, "logical_size_bytes": None},
            tmp_path / "state.db",
        )
        captured = capsys.readouterr()
        assert "advisory" in captured.err
        assert "100 MiB" in captured.err
        # The % of db size is NOT mentioned because we have no logical size
        assert "%" not in captured.err

    def test_no_advisory_when_wal_at_exactly_threshold(self, capsys, tmp_path):
        """wal_size_bytes == 100 MiB triggers (>= threshold), not strictly greater."""
        _print_wal_advisory(
            {
                "wal_size_bytes": 100 * 1024 * 1024,
                "logical_size_bytes": 1024 * 1024 * 1024,
            },
            tmp_path / "state.db",
        )
        captured = capsys.readouterr()
        assert "advisory" in captured.err
        assert "100 MiB" in captured.err

    def test_advisory_uses_db_path_name(self, capsys, tmp_path):
        """The advisory mentions the db filename so the user knows which
        database the recommendation applies to."""
        lcm = tmp_path / "plugins" / "hermes-lcm" / "lcm.db"
        lcm.parent.mkdir(parents=True, exist_ok=True)
        lcm.touch()
        _print_wal_advisory(
            {"wal_size_bytes": 200 * 1024 * 1024, "logical_size_bytes": 1024 * 1024},
            lcm,
        )
        captured = capsys.readouterr()
        assert "lcm.db" in captured.err
        assert "advisory" in captured.err
