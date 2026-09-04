"""Tests for agent/git_truth.py — the post-compaction git-truth snapshot.

Verifies the read-only, time-boxed, failure-safe contract:
  - in a git repo it emits branch + recent commits + working-tree status
  - outside a repo it emits nothing
  - inject=False emits nothing
  - failure never raises
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from agent.git_truth import build_git_truth_block


@pytest.fixture(scope="module")
def git_repo(tmp_path_factory):
    """Create a tiny throwaway git repo in a temp dir."""
    root = tmp_path_factory.mktemp("reptruth")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "a.py"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "feat: initial"], cwd=root, check=True)
    return root


def test_emits_branch_and_commit(git_repo):
    block = build_git_truth_block(cwd=str(git_repo), time_budget_ms=2000)
    assert block
    assert "<git_truth>" in block
    assert "Branch:" in block
    assert "feat: initial" in block  # our commit subject


def test_emits_working_tree_changes(git_repo):
    (git_repo / "a.py").write_text("x = 2\n")
    block = build_git_truth_block(cwd=str(git_repo), time_budget_ms=2000)
    assert "Working tree" in block
    assert "a.py" in block


def test_non_repo_returns_empty(tmp_path):
    empty = tmp_path / "not_a_repo"
    empty.mkdir()
    block = build_git_truth_block(cwd=str(empty), time_budget_ms=2000)
    assert block == ""


def test_inject_false_returns_empty(git_repo):
    assert build_git_truth_block(cwd=str(git_repo), inject=False) == ""


def test_never_raises_on_bad_path():
    block = build_git_truth_block(cwd="/definitely/not/a/path", time_budget_ms=500)
    assert block == ""


def test_git_absent_returns_empty(monkeypatch, git_repo):
    # Simulate git not found so subprocess raises FileNotFoundError.
    def _boom(*a, **k):
        raise FileNotFoundError("git not found")

    # Path the module actually uses is subprocess.run; patch it.
    import agent.git_truth as gt

    orig = subprocess.run
    try:
        monkeypatch.setattr(subprocess, "run", _boom)
        block = gt.build_git_truth_block(cwd=str(git_repo), time_budget_ms=500)
        assert block == ""
    finally:
        subprocess.run = orig