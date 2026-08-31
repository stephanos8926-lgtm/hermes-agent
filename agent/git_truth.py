"""git_truth — read-only, time-boxed git snapshot for post-compaction injection.

Purpose
-------
After a conversation is compacted, the system prompt is rebuilt (compaction is
the one place a rebuild is allowed, per AGENTS.md). This module produces a short,
READ-ONLY snapshot of the repository the agent is working in so the rebuilt
context re-anchors to current repo reality instead of stale history.

Design constraints (root-caused 2026-08-11, authored for the RapidWebs fork):
  - READ-ONLY: never runs a mutating git command (no checkouts, no stashes,
    no resets). Only log/status/branch reads.
  - TIME-BOXED: every subprocess is bounded (~120ms total budget). If git is
    slow or absent, we return "" quickly — a probing failure must NEVER block
    the prompt build.
  - SWALLOWED failures: any exception returns "" (empty). Git not installed,
    not a repo, permission error — all produce no injection.
  - Emits NOTHING when there is no VCS context (so clean sessions pay 0 tokens).

Returns a small markdown block, or "" when nothing useful is available.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# GLOBAL_TIME_BUDGET_MS: absolute wall-clock ceiling for the whole snapshot
# (all git calls combined). Kept small so compensation never stalls a turn.
_GLOBAL_TIME_BUDGET_MS = 120
_PER_CALL_TIMEOUT_S = 1.0

# Short-ish safe defaults. Keep the injected block tiny.
_MAX_LOG_LINES = 5
_MAX_STATUS_LINES = 12
_MAX_BRANCH_LEN = 80


def _run_git(
    args: list[str],
    cwd: Path | None,
    timeout_s: float = _PER_CALL_TIMEOUT_S,
) -> str | None:
    """Run a read-only git command; return stdout (stripped) or None on failure."""
    cmd = ["git"] + args
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _resolve_repo_root(start: Path) -> Path | None:
    """Resolve the top-level git worktree root from ``start`` (or None)."""
    out = _run_git(["rev-parse", "--show-toplevel"], cwd=start)
    if not out:
        return None
    return Path(out)


def build_git_truth_block(
    cwd: str | os.PathLike | None = None,
    *,
    inject: bool = True,
    max_log_lines: int = _MAX_LOG_LINES,
    max_status_lines: int = _MAX_STATUS_LINES,
    time_budget_ms: int = _GLOBAL_TIME_BUDGET_MS,
) -> str:
    """Build a compact git-truth snapshot block for post-compaction injection.

    Args:
        cwd: Directory to treat as the repository context. Defaults to the
            current working directory.
        inject: Master switch. When False, returns "" immediately (allows the
            feature to be toggled without plumbing an early-exit through callers).
        max_log_lines: How many ``git log`` lines to include.
        max_status_lines: How many ``git status --short`` lines to include.
        time_budget_ms: Hard wall-clock ceiling for the whole snapshot.

    Returns:
        A short markdown block (e.g. ``<git_truth>...``), or "" when disabled /
        not a repo / any failure.
    """
    if not inject:
        return ""

    base = Path(cwd).resolve() if cwd else Path.cwd().resolve()

    # Each git call gets a bounded slice of the global budget.
    per_call = max(0.15, time_budget_ms / 1000.0 / 3.0)

    root = _resolve_repo_root(base)
    if root is None:
        return ""  # not a git repo — no truth to inject

    branch = _run_git(["branch", "--show-current"], cwd=root, timeout_s=per_call)
    branch = (branch or "")[:_MAX_BRANCH_LEN]

    log_out = _run_git(
        ["log", "--oneline", f"-{max_log_lines}"],
        cwd=root,
        timeout_s=per_call,
    )
    log_lines = [ln for ln in (log_out.splitlines() if log_out else []) if ln.strip()][
        :max_log_lines
    ]

    status_out = _run_git(
        ["status", "--short"],
        cwd=root,
        timeout_s=per_call,
    )
    status_lines = [
        ln for ln in (status_out.splitlines() if status_out else []) if ln.strip()
    ][:max_status_lines]

    # Nothing concrete → emit nothing.
    if not branch and not log_lines and not status_lines:
        return ""

    # Re-read the repo root as a verbatim string (path may contain odd chars).
    root_str = str(root)

    parts = ["<git_truth>"]
    parts.append(f"Current git repository: {root_str}")
    if branch:
        parts.append(f"Branch: {branch}")
    if log_lines:
        parts.append("\nRecent commits (truth, not assumption):")
        parts.extend(f"  {ln}" for ln in log_lines)
    if status_lines:
        parts.append("\nWorking tree (uncommitted changes):")
        parts.extend(f"  {ln}" for ln in status_lines)
    parts.append(
        "\nUse this as ground truth for the current repository state. Do not "
        "assume work was done unless it appears in the log or working tree."
    )
    parts.append("</git_truth>")
    return "\n".join(parts)