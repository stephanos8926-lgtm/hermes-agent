"""Round 2 R3 — matrix plugin's apply_yaml_config_fn hook.

The matrix plugin reads its connection params (homeserver, user_id,
password, device_id, encryption flags) from config.extra AND env vars
(MATRIX_HOMESERVER, MATRIX_USER_ID, MATRIX_PASSWORD, MATRIX_DEVICE_ID,
MATRIX_E2EE_MODE). When the user configures the matrix: block in
config.yaml, those fields are bridged into env vars so the existing
env-var-driven path in gateway/config.py::_apply_env_overrides picks
them up. Without this bridge, a user who configures matrix purely via
config.yaml sees 'homeserver URL not configured' because the env-var
path requires MATRIX_HOMESERVER to be set in the env.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path


def _load_adapter_module():
    """Load the matrix adapter module directly (it isn't a proper package).

    The matrix adapter is a single-file plugin; it must be registered in
    sys.modules so that @dataclass(frozen=True) can resolve its type
    annotations (dataclasses inspect sys.modules[cls.__module__]).
    """
    repo = Path(__file__).resolve().parents[4]  # tests/... -> repo root
    path = repo / "plugins" / "platforms" / "matrix" / "adapter.py"
    spec = importlib.util.spec_from_file_location("matrix_adapter", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["matrix_adapter"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_homeserver_is_bridged_to_env(monkeypatch):
    """homeserver from matrix: block reaches MATRIX_HOMESERVER env var."""
    monkeypatch.delenv("MATRIX_HOMESERVER", raising=False)
    adapter = _load_adapter_module()
    adapter._apply_yaml_config({}, {"homeserver": "https://matrix.example.org"})
    assert os.environ["MATRIX_HOMESERVER"] == "https://matrix.example.org"


def test_homeserver_strips_trailing_slash(monkeypatch):
    """The adapter code at line 1199 does .rstrip('/'); the bridge mirrors it."""
    monkeypatch.delenv("MATRIX_HOMESERVER", raising=False)
    adapter = _load_adapter_module()
    adapter._apply_yaml_config({}, {"homeserver": "https://matrix.example.org/"})
    assert os.environ["MATRIX_HOMESERVER"] == "https://matrix.example.org"


def test_homeserver_env_var_takes_precedence(monkeypatch):
    """A pre-set MATRIX_HOMESERVER is NOT overwritten by config.yaml."""
    monkeypatch.setenv("MATRIX_HOMESERVER", "https://env.example.org")
    adapter = _load_adapter_module()
    adapter._apply_yaml_config({}, {"homeserver": "https://yaml.example.org"})
    assert os.environ["MATRIX_HOMESERVER"] == "https://env.example.org"


def test_homeserver_omitted_does_not_clear_env(monkeypatch):
    """If config.yaml has no homeserver key, the env var is untouched."""
    monkeypatch.setenv("MATRIX_HOMESERVER", "https://env.example.org")
    adapter = _load_adapter_module()
    adapter._apply_yaml_config({}, {})
    assert os.environ["MATRIX_HOMESERVER"] == "https://env.example.org"


def test_user_id_is_bridged_to_env(monkeypatch):
    """user_id from matrix: block reaches MATRIX_USER_ID env var."""
    monkeypatch.delenv("MATRIX_USER_ID", raising=False)
    adapter = _load_adapter_module()
    adapter._apply_yaml_config({}, {"user_id": "@bot:matrix.example.org"})
    assert os.environ["MATRIX_USER_ID"] == "@bot:matrix.example.org"


def test_user_id_env_var_takes_precedence(monkeypatch):
    """A pre-set MATRIX_USER_ID is NOT overwritten by config.yaml."""
    monkeypatch.setenv("MATRIX_USER_ID", "@env:matrix.example.org")
    adapter = _load_adapter_module()
    adapter._apply_yaml_config({}, {"user_id": "@yaml:matrix.example.org"})
    assert os.environ["MATRIX_USER_ID"] == "@env:matrix.example.org"


def test_password_is_bridged_to_env(monkeypatch):
    """password from matrix: block reaches MATRIX_PASSWORD env var."""
    monkeypatch.delenv("MATRIX_PASSWORD", raising=False)
    adapter = _load_adapter_module()
    adapter._apply_yaml_config({}, {"password": "supersecret"})
    assert os.environ["MATRIX_PASSWORD"] == "supersecret"


def test_device_id_is_bridged_to_env(monkeypatch):
    """device_id from matrix: block reaches MATRIX_DEVICE_ID env var."""
    monkeypatch.delenv("MATRIX_DEVICE_ID", raising=False)
    adapter = _load_adapter_module()
    adapter._apply_yaml_config({}, {"device_id": "DEVICE123"})
    assert os.environ["MATRIX_DEVICE_ID"] == "DEVICE123"


def test_e2ee_mode_is_bridged_to_env_lowercased(monkeypatch):
    """e2ee_mode is lowercased to match the env-var convention."""
    monkeypatch.delenv("MATRIX_E2EE_MODE", raising=False)
    adapter = _load_adapter_module()
    adapter._apply_yaml_config({}, {"e2ee_mode": "REQUIRED"})
    assert os.environ["MATRIX_E2EE_MODE"] == "required"


def test_e2ee_mode_env_var_takes_precedence(monkeypatch):
    """A pre-set MATRIX_E2EE_MODE is NOT overwritten by config.yaml."""
    monkeypatch.setenv("MATRIX_E2EE_MODE", "optional")
    adapter = _load_adapter_module()
    adapter._apply_yaml_config({}, {"e2ee_mode": "required"})
    assert os.environ["MATRIX_E2EE_MODE"] == "optional"


def test_all_fields_bridged_together(monkeypatch):
    """A realistic matrix: block with all connection fields bridges correctly."""
    monkeypatch.delenv("MATRIX_HOMESERVER", raising=False)
    monkeypatch.delenv("MATRIX_USER_ID", raising=False)
    monkeypatch.delenv("MATRIX_PASSWORD", raising=False)
    monkeypatch.delenv("MATRIX_DEVICE_ID", raising=False)
    monkeypatch.delenv("MATRIX_E2EE_MODE", raising=False)
    adapter = _load_adapter_module()
    adapter._apply_yaml_config(
        {},
        {
            "homeserver": "https://matrix.example.org",
            "user_id": "@bot:matrix.example.org",
            "password": "secret",
            "device_id": "ABC123",
            "e2ee_mode": "preferred",
        },
    )
    assert os.environ["MATRIX_HOMESERVER"] == "https://matrix.example.org"
    assert os.environ["MATRIX_USER_ID"] == "@bot:matrix.example.org"
    assert os.environ["MATRIX_PASSWORD"] == "secret"
    assert os.environ["MATRIX_DEVICE_ID"] == "ABC123"
    assert os.environ["MATRIX_E2EE_MODE"] == "preferred"


def test_existing_fields_still_bridged(monkeypatch):
    """Regression: the original gating fields (require_mention, allowed_users, ...)
    are still bridged by the same hook."""
    monkeypatch.delenv("MATRIX_REQUIRE_MENTION", raising=False)
    monkeypatch.delenv("MATRIX_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("MATRIX_FREE_RESPONSE_ROOMS", raising=False)
    adapter = _load_adapter_module()
    adapter._apply_yaml_config(
        {},
        {
            "require_mention": True,
            "allowed_users": ["@alice:example.org", "@bob:example.org"],
            "free_response_rooms": ["!room1:example.org", "!room2:example.org"],
        },
    )
    assert os.environ["MATRIX_REQUIRE_MENTION"] == "true"
    assert os.environ["MATRIX_ALLOWED_USERS"] == "@alice:example.org,@bob:example.org"
    assert (
        os.environ["MATRIX_FREE_RESPONSE_ROOMS"]
        == "!room1:example.org,!room2:example.org"
    )


def test_returns_none_for_contract_compatibility(monkeypatch):
    """The hook contract returns None — env vars are the side effect."""
    monkeypatch.delenv("MATRIX_HOMESERVER", raising=False)
    adapter = _load_adapter_module()
    result = adapter._apply_yaml_config({}, {"homeserver": "https://x.org"})
    assert result is None
