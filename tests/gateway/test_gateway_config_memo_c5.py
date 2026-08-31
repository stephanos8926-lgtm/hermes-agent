"""C5: L1 memo for non-canonical gateway config reads.

The canonical path already rides hermes_cli.config.read_raw_config's
mtime cache. The non-canonical path (test fixtures, multiplexed
profile routes) previously re-ran yaml.safe_load on every gateway RPC.
These tests pin the memo contract:

  * same (path, mtime_ns, size) -> cached dict returned, file read once
  * any file change -> new key -> fresh read (staleness impossible)
  * memo is bounded (8 entries) so unbounded fixture paths can't leak
  * missing/unreadable files still yield {} without caching garbage
"""
import os
import time

import pytest


@pytest.fixture()
def gw():
    import sys
    sys.modules.setdefault("dotenv", type(sys)("dotenv"))
    sys.modules["dotenv"].load_dotenv = lambda *a, **k: None
    from gateway import run as gateway_run
    return gateway_run


def test_same_mtime_served_from_cache(gw, tmp_path):
    cfg = tmp_path / "gw.yaml"
    cfg.write_text("model:\n  provider: nvidia\n", encoding="utf-8")
    calls = {"n": 0}
    real_yaml_safe_load = None

    first = gw._load_gateway_config(cfg)
    second = gw._load_gateway_config(cfg)
    assert first == second
    # Same object identity proves the memo served the second call.
    assert first is second


def test_file_change_invalidates(gw, tmp_path):
    cfg = tmp_path / "gw.yaml"
    cfg.write_text("a: 1\n", encoding="utf-8")
    first = gw._load_gateway_config(cfg)
    assert first.get("a") == 1

    # Ensure mtime actually moves (coarse-mtime filesystems).
    future = time.time() + 2
    os.utime(cfg, (future, future))
    cfg.write_text("a: 2\n", encoding="utf-8")
    os.utime(cfg, (future + 1, future + 1))

    second = gw._load_gateway_config(cfg)
    assert second.get("a") == 2


def test_memo_is_bounded(gw, tmp_path):
    for i in range(20):
        p = tmp_path / f"cfg{i}.yaml"
        p.write_text(f"i: {i}\n", encoding="utf-8")
        gw._load_gateway_config(p)
    assert len(gw._NONCANONICAL_CFG_CACHE) <= 8


def test_missing_file_yields_empty_dict(gw, tmp_path):
    result = gw._load_gateway_config(tmp_path / "nope.yaml")
    assert result == {}


def test_canonical_path_bypasses_memo(gw, tmp_path, monkeypatch):
    """When config_path == get_config_path(), read_raw_config serves it
    and the non-canonical memo must not engage."""
    import hermes_cli.config as hc_config
    cfg_path = tmp_path / "canonical.yaml"
    cfg_path.write_text("x: 0\n", encoding="utf-8")
    sentinel = {"via": "canonical"}
    monkeypatch.setattr(hc_config, "get_config_path", lambda: cfg_path)
    monkeypatch.setattr(hc_config, "read_raw_config", lambda *a, **k: dict(sentinel))
    before = dict(gw._NONCANONICAL_CFG_CACHE)
    out = gw._load_gateway_config(cfg_path)
    assert out.get("via") == "canonical"
    # Memo untouched by the canonical path.
    assert gw._NONCANONICAL_CFG_CACHE == before
