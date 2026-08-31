"""Tests for agent/skill_auto_load — the RapidWebs skill auto-load feature.

Covers the A–E spec:
  A. Trigger scoring (word-boundary, min_confidence, negative_triggers)
  B. Reference auto-load (caps + truncation + pointer)
  C. Dedup window (per session / turns / minutes)
  D. Recursion guard over related_skills + max_reference_files
  E. Config (enabled toggle, defaults)
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

import agent.skill_auto_load as s

# ── A: trigger scoring ───────────────────────────────────────────────────────


def _cfg(**over):
    c = dict(s.DEFAULT_AUTO_LOAD_CONFIG)
    c.update(over)
    return c


def test_single_word_trigger_clears_default_threshold():
    skill = {"name": "systematic-debugging", "triggers": ["debug"]}
    cfg = _cfg(enabled=True)
    score = s.score_skill_for_message(skill, "help me debug this crash", cfg)
    assert score >= 0.6
    assert s.select_auto_load_skills([skill], "help me debug", cfg) == [skill]


def test_no_match_returns_zero():
    skill = {"name": "pytorch", "triggers": ["pytorch", "torch"]}
    assert s.score_skill_for_message(skill, "write a bash script", _cfg()) == 0.0


def test_negative_trigger_blocks():
    skill = {"name": "x", "triggers": ["frontend"], "negative_triggers": ["react"]}
    cfg = _cfg(enabled=True)
    # 'react' present → negative vetoes → 0 (no false positive)
    assert s.score_skill_for_message(skill, "build a react frontend", cfg) == 0.0
    # no negative → positive trigger fires
    assert s.score_skill_for_message(skill, "frontend issue here", cfg) > 0.6


def test_phrase_trigger_word_boundary():
    skill = {"name": "x", "triggers": ["claude code"]}
    cfg = _cfg(enabled=True)
    # phrase present (word boundary) → matches
    assert s.score_skill_for_message(skill, "using claude code for this", cfg) > 0.6
    # substring only (claudecode not a word) → no match via regex word boundary
    assert s.score_skill_for_message(skill, "hello claudecode world", cfg) == 0.0


def test_max_auto_load_caps_selection():
    cfg = _cfg(enabled=True, max_auto_load=1)
    skills = [
        {"name": "a", "triggers": ["alpha"]},
        {"name": "b", "triggers": ["alpha"]},
        {"name": "c", "triggers": ["alpha"]},
    ]
    sel = s.select_auto_load_skills(skills, "do alpha work", cfg)
    assert len(sel) == 1


# ── C: dedup ─────────────────────────────────────────────────────────────────


def test_dedup_once_per_session():
    cfg = _cfg(enabled=True, dedup_window="once_per_session")
    s.reset_session("sid")
    s.mark_loaded("skill-a", "sid")
    assert s.should_skip_dedup("skill-a", "sid", cfg) is True
    assert s.should_skip_dedup("skill-b", "sid", cfg) is False


def test_dedup_turns_window():
    cfg = _cfg(enabled=True, dedup_window="once_per_turns", dedup_turns=2)
    s.reset_session("t")
    s.bump_turn("t")
    s.mark_loaded("x", "t")
    # within window (0 turns since) → skip
    assert s.should_skip_dedup("x", "t", cfg) is True
    s.bump_turn("t")
    s.bump_turn("t")  # 2 turns elapsed
    assert s.should_skip_dedup("x", "t", cfg) is False


def test_reset_session_clears_state():
    cfg = _cfg(enabled=True)
    s.reset_session("r")
    s.mark_loaded("x", "r")
    s.reset_session("r")
    assert s.should_skip_dedup("x", "r", cfg) is False


# ── D: recursion guard ───────────────────────────────────────────────────────


def test_cyclic_related_skills_terminate():
    # A → B → A cycle must terminate.
    def resolver(name):
        return {"name": name, "related_skills": ["B"] if name == "A" else ["A"]}

    res = s.walk_related_skills({"name": "A"}, resolver, _cfg(max_depth=6, max_reference_files=8))
    # bounded; should not recurse indefinitely
    assert len(res) <= 8
    assert "A" not in [str(n).lower() for n in res]  # self excluded


def test_max_reference_files_honored():
    calls = []

    def resolver(name):
        calls.append(name)
        return {"name": name, "related_skills": [f"c{len(calls)}"]}

    res = s.walk_related_skills({"name": "root"}, resolver, _cfg(max_reference_files=3, max_depth=6))
    assert len(res) <= 3


# ── B: reference auto-load with caps ────────────────────────────────────────


def test_auto_load_references_disabled_returns_empty(tmp_path):
    skill_dir = tmp_path / "references"
    (skill_dir).mkdir(parents=True)
    (skill_dir / "a.md").write_text("hello")
    # disabled by default
    assert s.auto_load_references(tmp_path, _cfg(enabled=False)) == ""


def test_auto_load_references_truncates_and_points(tmp_path):
    cfg = _cfg(enabled=True, references_max_count=1, references_max_chars=20, truncation_pointer=True)
    ref_dir = tmp_path / "references"
    ref_dir.mkdir()
    (ref_dir / "big.md").write_text("x" * 100)
    block = s.auto_load_references(tmp_path, cfg)
    assert "references/big.md" in block
    assert "truncated" in block  # pointer line present
    assert len(block) < 200  # capped


def test_auto_load_references_count_cap(tmp_path):
    cfg = _cfg(enabled=True, references_max_count=1, references_max_chars=5000, truncation_pointer=False)
    ref_dir = tmp_path / "references"
    ref_dir.mkdir()
    (ref_dir / "a.md").write_text("a")
    (ref_dir / "b.md").write_text("b")
    block = s.auto_load_references(tmp_path, cfg)
    # only one file loaded (count cap)
    assert block.count("### references/") == 1


# ── E: config / enabled toggle ───────────────────────────────────────────────


def test_default_disabled():
    assert s.is_enabled() is False  # OFF by default out of the box


def test_enabled_toggle_via_config(monkeypatch):
    import agent.skill_preprocessing as pref

    monkeypatch.setattr(
        pref, "load_skills_config", lambda: {"auto_load": {"enabled": True}}
    )
    # reload module-level config caches if any (here load is dynamic)
    assert s.load_auto_load_config()["enabled"] is True


def test_build_auto_load_prompt_disabled_returns_empty():
    block, names = s.build_auto_load_prompt(
        "debug this", [{"name": "x", "triggers": ["debug"]}], session_id="z"
    )
    # disabled by default → empty
    assert block == "" and names == []


# ── New: candidate index + invoke-site wrapper ──────────────────────────────
# These use a synthetic HERMES_HOME skills tree (via monkeypatch of the
# candidate scan roots) so they don't depend on the live skills directory.


def _build_candidates_from_tree(root: Path, cfg) -> list:
    """Exercise build_candidates against a synthetic skills tree.

    Monkeypatch get_all_skills_dirs+iter_skill_index_files to point at
    ``root`` only, then clear the candidate cache so the scan runs fresh.
    """
    import agent.skill_utils as su

    def _fake_dirs():
        return [root]

    def _fake_iter(sk_dir, filename):
        if filename == "SKILL.md":
            for f in root.rglob("SKILL.md"):
                yield f

    monkey = pytest.MonkeyPatch()
    monkey.setattr(su, "get_all_skills_dirs", _fake_dirs)
    monkey.setattr(su, "iter_skill_index_files", _fake_iter)
    s._CANDIDATES_CACHE.clear()
    try:
        return s.build_candidates(cfg)
    finally:
        monkey.undo()


def test_build_candidates_parses_frontmatter_and_schema(tmp_path):
    # top-level triggers + newer metadata.hermes.tags both surface
    cat = tmp_path / "dev"
    cat.mkdir()
    (cat / "foo").mkdir()
    (cat / "foo" / "SKILL.md").write_text(
        "---\nname: foo\ndescription: a foo skill\ntriggers: ['foo', 'foo thing']\n"
        "related_skills: ['bar']\n---\n# Foo\n"
    )
    (cat / "bar").mkdir()
    (cat / "bar" / "SKILL.md").write_text(
        "---\nname: bar\ndescription: a bar skill\nmetadata:\n  hermes:\n    tags: ['barry', 'baz']\n    related_skills: ['foo']\n---\n# Bar\n"
    )
    cfg = _cfg(enabled=True)
    cands = _build_candidates_from_tree(tmp_path, cfg)
    by_name = {c["name"]: c for c in cands}
    assert "foo" in by_name
    assert "foo" in by_name["foo"]["triggers"]
    assert "foo thing" in by_name["foo"]["triggers"]
    assert "bar" in by_name["foo"]["related_skills"] or "bar" in by_name["foo"].get("related_skills", [])
    # metadata.hermes.tags surfaced as triggers for 'bar'
    assert "barry" in by_name["bar"]["triggers"]
    assert "baz" in by_name["bar"]["triggers"]
    # metadata.hermes.related_skills surfaced
    assert "foo" in by_name["bar"]["related_skills"]


def test_build_candidates_skips_disabled(tmp_path, monkeypatch):
    cat = tmp_path / "dev"
    cat.mkdir()
    (cat / "enabled-skill").mkdir()
    (cat / "enabled-skill" / "SKILL.md").write_text(
        "---\nname: enabled-skill\ndescription: e\ntriggers: ['go']\n---\n# e\n"
    )
    (cat / "disabled-skill").mkdir()
    (cat / "disabled-skill" / "SKILL.md").write_text(
        "---\nname: disabled-skill\ndescription: d\ntriggers: ['stop']\n---\n# d\n"
    )
    monkeypatch.setattr(
        "agent.skill_utils.get_disabled_skill_names", lambda: {"disabled-skill"}
    )
    cfg = _cfg(enabled=True)
    cands = _build_candidates_from_tree(tmp_path, cfg)
    names = {c["name"] for c in cands}
    assert "enabled-skill" in names
    assert "disabled-skill" not in names


def test_auto_load_for_message_fires_and_respects_budget(tmp_path):
    cat = tmp_path / "dev"
    cat.mkdir()
    (cat / "alpha").mkdir()
    (cat / "alpha" / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: an alpha skill\ntriggers: ['alpha']\n"
        "related_skills: ['beta']\n---\n# Alpha\n"
    )
    (cat / "beta").mkdir()
    (cat / "beta" / "SKILL.md").write_text(
        "---\nname: beta\ndescription: a beta skill\ntriggers: ['beta']\n"
        "related_skills: ['alpha']\n---\n# Beta\n"
    )
    cfg = _cfg(enabled=True, max_auto_load=2, min_confidence=0.5)
    s._CANDIDATES_CACHE.clear()

    import agent.skill_utils as su

    monkey = pytest.MonkeyPatch()
    monkey.setattr(su, "get_all_skills_dirs", lambda: [tmp_path])
    monkey.setattr(
        su,
        "iter_skill_index_files",
        lambda d, fn: (f for f in tmp_path.rglob("SKILL.md")) if fn == "SKILL.md" else iter(()),
    )
    # keep module-level auto-load disabled to isolate this invocation
    monkey.setattr(s, "load_auto_load_config", lambda: cfg)
    s.reset_session("auto-session")
    try:
        block, names = s.auto_load_for_message("do alpha for me", session_id="auto-session")
    finally:
        monkey.undo()
    assert "alpha" in names
    assert len(names) <= 2  # budget incl. related expansion
    assert "[Auto-loaded skill: alpha]" in block