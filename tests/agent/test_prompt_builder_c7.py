"""C7: L1 memo for context-file loaders.

Contract: same (loader, cwd, context_length, stat-signature) -> cached
result returned without re-reading files. Any candidate file edit
(mtime_ns change), size change, addition, or removal produces a new
signature -> fresh read. Memo bounded at 32 entries.
"""
import os
import time

import pytest


@pytest.fixture()
def pb(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import agent.prompt_builder as m
    m._C7_CACHE.clear()
    return m


def test_hermes_md_cached(pb, tmp_path):
    f = tmp_path / ".hermes.md"
    f.write_text("hello", encoding="utf-8")
    a = pb._load_hermes_md_cached(tmp_path)
    b = pb._load_hermes_md_cached(tmp_path)
    assert "hello" in a and b == a


def test_edit_invalidates_via_mtime(pb, tmp_path):
    f = tmp_path / ".hermes.md"
    f.write_text("v1", encoding="utf-8")
    assert "v1" in pb._load_hermes_md_cached(tmp_path)
    future = time.time() + 2
    os.utime(f, (future, future))
    f.write_text("v2", encoding="utf-8")
    os.utime(f, (future + 1, future + 1))
    assert "v2" in pb._load_hermes_md_cached(tmp_path)


def test_removal_invalidates(pb, tmp_path):
    f = tmp_path / ".hermes.md"
    f.write_text("x", encoding="utf-8")
    assert "x" in pb._load_hermes_md_cached(tmp_path)
    f.unlink()
    assert pb._load_hermes_md_cached(tmp_path) == ""


def test_addition_invalidates(pb, tmp_path):
    assert pb._load_hermes_md_cached(tmp_path) == ""
    (tmp_path / ".hermes.md").write_text("new", encoding="utf-8")
    assert "new" in pb._load_hermes_md_cached(tmp_path)


def test_cursorrules_mdc_files_cached(pb, tmp_path):
    rules = tmp_path / ".cursor" / "rules"
    rules.mkdir(parents=True)
    (rules / "a.mdc").write_text("rule-a", encoding="utf-8")
    out = pb._load_cursorrules_cached(tmp_path)
    assert "rule-a" in out
    # Second call served from cache; content unchanged.
    assert pb._load_cursorrules_cached(tmp_path) == out


def test_agents_md_chain_change_invalidates(pb, tmp_path):
    (tmp_path / "AGENTS.md").write_text("root", encoding="utf-8")
    first = pb._load_agents_md_cached(tmp_path)
    assert "root" in first
    # Editing the file changes its stat signature -> fresh read.
    future = time.time() + 2
    os.utime(tmp_path / "AGENTS.md", (future, future))
    (tmp_path / "AGENTS.md").write_text("updated", encoding="utf-8")
    os.utime(tmp_path / "AGENTS.md", (future + 1, future + 1))
    second = pb._load_agents_md_cached(tmp_path)
    assert "updated" in second and second != first


def test_memo_bounded(pb):
    for i in range(40):
        d = pb.Path("/nonexistent-c7") / str(i)
        pb._c7_cached("probe", lambda c, cl: ("r", i), d, None, [d])
    assert len(pb._C7_CACHE) <= 32


def test_distinct_context_length_distinct_key(pb, tmp_path):
    (tmp_path / ".hermes.md").write_text("ctx", encoding="utf-8")
    a = pb._load_hermes_md_cached(tmp_path, 1000)
    b = pb._load_hermes_md_cached(tmp_path, 2000)
    assert "ctx" in a and "ctx" in b
    # Both keys cached independently.
    assert len(pb._C7_CACHE) >= 2
