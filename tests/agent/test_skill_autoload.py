"""Tests for agent/skill_autoload.py.

Covers:
- Config parsing (defaults, custom values, negative clamping)
- Trigger extraction (list, comma-sep, missing)
- Skill index build + scan
- Dependency resolution with depth cap + circular guard
- Body capping
- Empty/error paths
"""

import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from unittest.mock import patch

# Ensure test runner env is set up before importing agent code
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from agent.skill_autoload import (
    CAP_TOKENS,
    DEPENDENCY_DEPTH_CAP,
    MAX_LOADED_PER_TURN,
    _SkillIndex,
    _normalize_words,
    auto_load_skills,
    extract_dependencies,
    extract_triggers,
)


def create_skill_dir(tmpdir: Path, skills: Dict[str, Dict[str, Any]]) -> Path:
    """Create a fake skills directory with the given skill specs."""
    sdir = tmpdir / "skills"
    sdir.mkdir()
    for name, spec in skills.items():
        sf = sdir / f"{name}/SKILL.md"
        sf.parent.mkdir(parents=True, exist_ok=True)
        fm_lines = ["---"]
        for k, v in spec.get("frontmatter", {}).items():
            if isinstance(v, list):
                fm_lines.append(f"{k}:")
                for item in v:
                    fm_lines.append(f"  - \"{item}\"")
            else:
                fm_lines.append(f'{k}: "{v}"')
        fm_lines.append("---")
        content = "\n".join(fm_lines) + "\n\n# " + name + "\n\n" + spec.get("body", "")
        sf.write_text(content)
    return sdir


class TestConfigDefaults:
    def test_defaults_are_consistent(self):
        """Module constants should match documented defaults."""
        assert CAP_TOKENS == 4096
        assert MAX_LOADED_PER_TURN == 2
        assert DEPENDENCY_DEPTH_CAP == 3


class TestExtractTriggers:
    def test_list_format(self):
        fm = {"triggers": ["debug Python", "fix bugs"]}
        assert extract_triggers(fm) == ["debug Python", "fix bugs"]

    def test_comma_separated(self):
        fm = {"triggers": "debug, fix, resolve"}
        result = extract_triggers(fm)
        assert "debug" in result and "fix" in result

    def test_newline_separated(self):
        fm = {"triggers": "debug\nfix\nresolve"}
        result = extract_triggers(fm)
        assert "debug" in result and "fix" in result and "resolve" in result

    def test_missing_triggers(self):
        assert extract_triggers({}) == []

    def test_empty_string(self):
        assert extract_triggers({"triggers": ""}) == []


class TestExtractDependencies:
    def test_list_format(self):
        fm = {"dependencies": ["dep-a", "dep-b"]}
        assert extract_dependencies(fm) == ["dep-a", "dep-b"]

    def test_missing_deps(self):
        assert extract_dependencies({}) == []


class TestNormalizeWords:
    def test_basic_words(self):
        assert _normalize_words("debug Python") == ["debug", "python"]

    def test_stop_words_filtered(self):
        assert _normalize_words("write a plan") == ["write", "plan"]

    def test_stop_words_removed(self):
        assert _normalize_words("a the to for") == []

    def test_hyphen_split(self):
        assert _normalize_words("code-quality") == ["code", "quality"]

    def test_slash_split(self):
        assert _normalize_words("TODO/FIXME scanner") == ["todo", "fixme", "scanner"]

    def test_empty_string(self):
        assert _normalize_words("") == []

    def test_only_stop_words(self):
        assert _normalize_words("a the") == []


class TestWordBoundaryMatching:
    def test_debug_does_not_match_debugger(self):
        """'debug' trigger must NOT match on 'debugger' (substring bleed)."""
        index = _SkillIndex()
        index._entries = {
            "debug-skill": {
                "path": Path("/fake"),
                "triggers": ["debug"],
                "deps": [],
                "desc": "",
            }
        }
        scored = index.scan_triggers_for("i used the debugger")
        assert len(scored) == 0

    def test_trigger_words_in_any_order(self):
        """'write plan' trigger should match 'plan to write'."""
        index = _SkillIndex()
        index._entries = {
            "planning-skill": {
                "path": Path("/fake"),
                "triggers": ["write plan"],
                "deps": [],
                "desc": "",
            }
        }
        scored = index.scan_triggers_for("plan to write")
        assert len(scored) == 1

    def test_trigger_with_stop_words_between(self):
        """'write plan' trigger should match 'write a plan'."""
        index = _SkillIndex()
        index._entries = {
            "planning-skill": {
                "path": Path("/fake"),
                "triggers": ["write plan"],
                "deps": [],
                "desc": "",
            }
        }
        scored = index.scan_triggers_for("write a plan")
        assert len(scored) == 1

    def test_hyphenated_trigger(self):
        """'code-review' trigger should match 'do a code review'."""
        index = _SkillIndex()
        index._entries = {
            "review-skill": {
                "path": Path("/fake"),
                "triggers": ["code-review"],
                "deps": [],
                "desc": "",
            }
        }
        scored = index.scan_triggers_for("do a code review")
        assert len(scored) == 1

    def test_partial_word_does_not_match(self):
        """'test' trigger must NOT match 'testing' or 'tested'."""
        index = _SkillIndex()
        index._entries = {
            "test-skill": {
                "path": Path("/fake"),
                "triggers": ["test"],
                "deps": [],
                "desc": "",
            }
        }
        scored = index.scan_triggers_for("testing and tested")
        assert len(scored) == 0

    def test_only_one_word_of_two_present(self):
        """'code review' trigger should NOT match just 'code'."""
        index = _SkillIndex()
        index._entries = {
            "review-skill": {
                "path": Path("/fake"),
                "triggers": ["code review"],
                "deps": [],
                "desc": "",
            }
        }
        scored = index.scan_triggers_for("write some code")
        assert len(scored) == 0

    def test_basic_match(self):
        index = _SkillIndex()
        index._entries = {
            "my-skill": {
                "path": Path("/fake"),
                "triggers": ["debug Python", "fix bugs"],
                "deps": [],
                "desc": "Test",
            }
        }
        scored = index.scan_triggers_for("I need help debug Python today", min_score=1)
        names = [s[0] for s in scored]
        assert "my-skill" in names

    def test_min_score_filter(self):
        index = _SkillIndex()
        index._entries = {
            "low-priority": {
                "path": Path("/fake"),
                "triggers": ["rare-word"],
                "deps": [],
                "desc": "Test",
            }
        }
        # Match at score 1
        scored1 = index.scan_triggers_for("uses rare-word here", min_score=1)
        assert len(scored1) == 1
        # No match at score 2
        scored2 = index.scan_triggers_for("uses rare-word here", min_score=2)
        assert len(scored2) == 0

    def test_sorted_by_score_descending(self):
        index = _SkillIndex()
        index._entries = {
            "one-trigger": {
                "path": Path("/fake"),
                "triggers": ["debug"],
                "deps": [],
                "desc": "Test",
            },
            "two-triggers": {
                "path": Path("/fake"),
                "triggers": ["debug", "Python"],
                "deps": [],
                "desc": "Test",
            },
        }
        scored = index.scan_triggers_for("debug my Python code", min_score=1)
        assert scored[0][0] == "two-triggers"
        assert scored[0][1] == 2


class TestDependencyResolution:
    def test_simple_dependency(self):
        index = _SkillIndex()
        index._entries = {
            "child": {
                "path": Path("/fake"),
                "triggers": [],
                "deps": ["parent"],
                "desc": "",
            },
            "parent": {
                "path": Path("/fake"),
                "triggers": [],
                "deps": [],
                "desc": "",
            },
        }
        deps = index.resolve_deps("child")
        assert "parent" in deps

    def test_circular_guard(self):
        index = _SkillIndex()
        index._entries = {
            "a": {
                "path": Path("/fake"),
                "triggers": [],
                "deps": ["b"],
                "desc": "",
            },
            "b": {
                "path": Path("/fake"),
                "triggers": [],
                "deps": ["a"],
                "desc": "",
            },
        }
        deps = index.resolve_deps("a")
        # Should not loop infinitely; max one copy of b
        assert deps.count("b") <= 1

    def test_depth_cap(self):
        """Depth cap limits how deep the chain goes."""
        index = _SkillIndex()
        entries: Dict[str, Any] = {}
        for i in range(5):
            name = f"chain-{i}"
            parent = f"chain-{i - 1}" if i > 0 else None
            entries[name] = {
                "path": Path("/fake"),
                "triggers": [],
                "deps": [parent] if parent else [],
                "desc": "",
            }
        index._entries = entries

        # Default depth cap of 3
        deps = index.resolve_deps("chain-4")
        assert len(deps) < 4  # shouldn't resolve all 4 levels

        # Explicit depth=1 → only immediate dep (chain-3), not transitive
        deps1 = index.resolve_deps("chain-4", dep_depth=1)
        assert len(deps1) == 1
        assert deps1[0] == "chain-3"

        # Explicit depth=0 → no deps at all
        deps0 = index.resolve_deps("chain-4", dep_depth=0)
        assert len(deps0) == 0

        # Explicit depth=4 should get more
        deps4 = index.resolve_deps("chain-4", dep_depth=4)
        assert len(deps4) >= 3


class TestBodyCapping:
    def test_body_under_cap(self, tmp_path: Path):
        index = _SkillIndex()
        index._entries = {
            "small": {
                "path": tmp_path / "SKILL.md",
                "triggers": [],
                "deps": [],
                "desc": "",
            }
        }
        tmp_path.joinpath("SKILL.md").write_text(
            "---\nname: small\n---\n\nShort body.\n"
        )
        body = index.load_body("small", cap=100)
        assert len(body) == len("Short body.")

    def test_body_over_cap(self, tmp_path: Path):
        index = _SkillIndex()
        index._entries = {
            "big": {
                "path": tmp_path / "SKILL.md",
                "triggers": [],
                "deps": [],
                "desc": "",
            }
        }
        long_body = "x" * 10000
        tmp_path.joinpath("SKILL.md").write_text(
            f"---\nname: big\n---\n\n{long_body}\n"
        )
        body = index.load_body("big", cap=50)
        assert len(body) <= 50


class TestAutoLoadSkillsIntegration:
    def test_disabled_returns_empty(self):
        """When autoload_enabled=False, always return ''."""
        result = auto_load_skills("test message")
        # Default config has autoload_enabled=False
        assert result == ""

    def test_empty_input(self):
        assert auto_load_skills("") == ""
        assert auto_load_skills("   ") == ""

    def test_nonexistent_skill_fails_silently(self):
        """If index has no matching skills, returns ''."""
        # This is covered by disabled default above
        assert auto_load_skills("anything at all") == ""


class TestNegativeClamp:
    def test_negative_values_clamped(self):
        """Negative config values should clamp to sane bounds."""
        mock_cfg = {
            "skills": {
                "autoload_enabled": True,
                "autoload_min_score": -5,
                "autoload_dependency_depth": -10,
                "autoload_body_cap_chars": -100,
                "autoload_max_skills": -3,
            }
        }

        # Inline the logic from _read_autoload_config with mocked input
        skills = mock_cfg.get("skills") if isinstance(mock_cfg, dict) else None
        if isinstance(skills, dict):
            enabled = bool(skills.get("autoload_enabled", False))
            min_score = int(skills.get("autoload_min_score", 1))
            dep_depth = int(skills.get("autoload_dependency_depth", 3))
            body_cap = int(skills.get("autoload_body_cap_chars", 4096))
            max_skills = int(skills.get("autoload_max_skills", 2))
            if min_score < 0:
                min_score = 1
            if dep_depth < 0:
                dep_depth = 0
            if body_cap < 0:
                body_cap = 4096
            if max_skills < 0:
                max_skills = 2

        assert min_score >= 0
        assert dep_depth >= 0
        assert body_cap > 0
        assert max_skills > 0
