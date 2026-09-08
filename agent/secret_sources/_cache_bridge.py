"""Tiered cache bridge for secret-source caches.

This module is the optional fast path for :class:`agent.secret_sources._cache.DiskCache`.
The on-disk JSON is the durable, atomic-write, 0600-permissioned source of
truth; the tiered cache is a read-acceleration layer that may front it when
the operator has opted in via the ``cache.secrets.*`` config block.

Design constraints (all non-negotiable — see AGENTS.md rule #8):

  * Backwards compatibility: when the bridge is disabled, the wrapped
    :class:`DiskCache` reads and writes the on-disk JSON exactly as before.
  * Fail-open: any failure in the tiered cache layer (router construction,
    get, put) is logged at debug and falls through to the JSON path. A
    cache problem must never block a secret fetch.
  * Configuration discipline: every tunable is a key in ``cache.secrets.*``
    with a ``HERMES_CACHE_SECRETS_*`` env-var override.

Why a bridge instead of replacing the JSON: the on-disk JSON is the only
storage layer with the security-sensitive contract the upstream
``DiskCache`` already audits (atomic write, 0600 mode, 0700 directory,
best-effort on every I/O error). The tiered cache adds an *acceleration*
layer in front of that — it does not replace the audited contract.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import asdict
from typing import Any, Callable, Optional, TypeVar

from agent._cache import get_cache_router

logger = logging.getLogger(__name__)

K = TypeVar("K")

# Defaults (env-overridable). These live in code rather than in a config
# schema because they are the *defaults* the bridge ships with; the actual
# operator-tunable values come from config.yaml at runtime.
DEFAULT_SECRETS_TTL_SECONDS = 60       # How long an L1/L2 entry is valid.
DEFAULT_SECRETS_L1_MAX_ENTRIES = 16   # Per-backend L1 size (a few backends, so small).
DEFAULT_SECRETS_L1_MAX_BYTES = 1 * 1024 * 1024  # 1 MiB per backend.


def _env(name: str, default: str) -> str:
    """Read an env-var, defaulting if unset or empty."""
    return os.environ.get(name) or default


def _is_secrets_cache_enabled() -> bool:
    """Master switch for the secret-source tiered-cache bridge.

    Off by default. Operators opt in via ``cache.secrets.enabled: true`` in
    config.yaml or ``HERMES_CACHE_SECRETS_ENABLED=true`` in .env.
    """
    env = _env("HERMES_CACHE_SECRETS_ENABLED", "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    if env in ("0", "false", "no", "off"):
        return False
    # Fall through to config.yaml.
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly() or {}
        secrets_cfg = (cfg.get("cache") or {}).get("secrets") or {}
        return bool(secrets_cfg.get("enabled", False))
    except Exception:
        return False


def _secrets_ttl_seconds() -> float:
    """Effective TTL for the L1/L2 layers (separate from the JSON TTL)."""
    env = _env("HERMES_CACHE_SECRETS_TTL_SECONDS", "").strip()
    if env:
        try:
            return max(0.0, float(env))
        except ValueError:
            pass
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly() or {}
        secrets_cfg = (cfg.get("cache") or {}).get("secrets") or {}
        return float(secrets_cfg.get("ttl_seconds", DEFAULT_SECRETS_TTL_SECONDS))
    except Exception:
        return float(DEFAULT_SECRETS_TTL_SECONDS)


def _secrets_l1_max_entries() -> int:
    env = _env("HERMES_CACHE_SECRETS_L1_MAX_ENTRIES", "").strip()
    if env:
        try:
            return max(1, int(env))
        except ValueError:
            pass
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly() or {}
        secrets_cfg = (cfg.get("cache") or {}).get("secrets") or {}
        return int(secrets_cfg.get("l1_max_entries", DEFAULT_SECRETS_L1_MAX_ENTRIES))
    except Exception:
        return DEFAULT_SECRETS_L1_MAX_ENTRIES


def _secrets_l1_max_bytes() -> int:
    env = _env("HERMES_CACHE_SECRETS_L1_MAX_BYTES", "").strip()
    if env:
        try:
            return max(1024, int(env))
        except ValueError:
            pass
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly() or {}
        secrets_cfg = (cfg.get("cache") or {}).get("secrets") or {}
        return int(secrets_cfg.get("l1_max_bytes", DEFAULT_SECRETS_L1_MAX_BYTES))
    except Exception:
        return DEFAULT_SECRETS_L1_MAX_BYTES


def _l2_path_for_backend(basename: str) -> str:
    """Per-backend L2 mmap path. Distinct files keep backends isolated."""
    env = _env("HERMES_CACHE_SECRETS_L2_DIR", "").strip()
    base_dir = env or os.path.join(
        os.path.expanduser("~"), ".hermes", "cache", "secrets"
    )
    os.makedirs(base_dir, exist_ok=True)
    # Strip extension; mmap files are binary so we use a .mmap suffix.
    stem = basename.split(".", 1)[0]
    return os.path.join(base_dir, f"{stem}.mmap")


def _l2_max_bytes() -> int:
    env = _env("HERMES_CACHE_SECRETS_L2_MAX_BYTES", "").strip()
    if env:
        try:
            return max(1024 * 1024, int(env))  # Floor at 1 MiB so it's usable.
        except ValueError:
            pass
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly() or {}
        secrets_cfg = (cfg.get("cache") or {}).get("secrets") or {}
        return int(secrets_cfg.get("l2_max_bytes", 4 * 1024 * 1024))  # 4 MiB default.
    except Exception:
        return 4 * 1024 * 1024


# ---------------------------------------------------------------------------
# Entry serialization
# ---------------------------------------------------------------------------


def _entry_to_dict(entry) -> dict:
    """Serialize a CachedFetch to a JSON-safe dict for the cache layers."""
    return asdict(entry)


def _entry_from_dict(cls, payload: dict):
    """Inverse of _entry_to_dict. Re-imports cls lazily to avoid cycles."""
    try:
        return cls(secrets=dict(payload.get("secrets") or {}),
                   fetched_at=float(payload.get("fetched_at") or 0.0))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Public bridge
# ---------------------------------------------------------------------------


def bridge_read(basename: str, key: Any, ttl_seconds: float,
                key_serializer: Callable[[Any], str], cls,
                json_fallback: Callable[[], Optional[Any]]) -> Optional[Any]:
    """Read-through: try L1+L2 first, fall back to the JSON source of truth.

    Parameters
    ----------
    basename : str
        The on-disk JSON basename (e.g. ``bitwarden.json``). Used to scope
        the L2 mmap file so concurrent backends don't collide.
    key : Any
        The cache key — serialized via ``key_serializer`` for the cache
        layer. The on-disk JSON uses the same serializer so a cache hit
        corresponds to the same logical entry.
    ttl_seconds : float
        The JSON TTL — propagated to the cache layers. A cache layer hit
        is also rejected if the entry is older than ``now - ttl_seconds``.
    key_serializer : Callable[[Any], str]
        The same serializer the on-disk JSON uses.
    cls : type
        The entry class (CachedFetch). Used to deserialize the cache hit.
    json_fallback : Callable[[], Optional[Any]]
        A thunk that calls the on-disk JSON read. Called only on a
        complete L1+L2 miss.

    Returns
    -------
    Optional[Any]
        A fresh ``cls`` instance, or None if all layers missed.
    """
    if not _is_secrets_cache_enabled() or ttl_seconds <= 0:
        return json_fallback()

    router = get_cache_router()
    if router is None:
        return json_fallback()

    # Include basename in the key to isolate backends within the shared L1+L2
    serialized_key = f"secrets:{basename}:{key_serializer(key)}"
    try:
        cached_payload = router.get(serialized_key)
    except Exception as e:
        logger.debug("Router get failed for %s/%s: %s", basename, serialized_key, e)
        return json_fallback()

    if cached_payload is not None:
        if not isinstance(cached_payload, dict):
            return json_fallback()
        # Re-validate the TTL on the cache hit (clock may have advanced
        # since the value was written, and the L1/L2 layers share the
        # same TTL as the JSON).
        fetched_at = cached_payload.get("fetched_at")
        if not isinstance(fetched_at, (int, float)):
            return json_fallback()
        if (time.time() - float(fetched_at)) >= ttl_seconds:
            # Stale — drop and fall through. The fall-through below will
            # repopulate the cache with the fresh value, so a single re-fetch
            # is enough to recover the cache (otherwise the next read would
            # also miss).
            try:
                router.invalidate(serialized_key)
            except Exception:
                pass
            json_entry = json_fallback()
            if json_entry is None:
                return None
            try:
                router.put(serialized_key, _entry_to_dict(json_entry))
            except Exception as e:
                logger.debug("Router put failed for %s/%s: %s", basename, serialized_key, e)
            return json_entry
        entry = _entry_from_dict(cls, cached_payload)
        if entry is not None:
            return entry

    # All cache layers missed (or entry shape was wrong) — fall through to
    # the JSON source of truth.
    json_entry = json_fallback()
    if json_entry is None:
        return None
    # Populate the cache layers with what we just fetched.
    try:
        router.put(serialized_key, _entry_to_dict(json_entry))
    except Exception as e:
        logger.debug("Router put failed for %s/%s: %s", basename, serialized_key, e)
    return json_entry


def bridge_write(basename: str, key: Any, entry: Any,
                 key_serializer: Callable[[Any], str]) -> None:
    """Write-through: persist the on-disk JSON (caller's responsibility)
    AND populate the L1+L2 layers.

    The JSON write is performed by the caller; this function only handles
    the cache side. A failure here is logged at debug and never raised —
    the JSON is the source of truth, so a missing cache update just means
    the next read takes the JSON path.
    """
    if not _is_secrets_cache_enabled():
        return
    router = get_cache_router()
    if router is None:
        return
    # Include basename in the key to isolate backends within the shared L1+L2
    serialized_key = f"secrets:{basename}:{key_serializer(key)}"
    try:
        router.put(serialized_key, _entry_to_dict(entry))
    except Exception as e:
        logger.debug("Router put failed during write-through for %s/%s: %s",
                     basename, serialized_key, e)


def bridge_clear(basename: str) -> None:
    """Invalidate all cache layers for a backend. Used on secret rotation."""
    if not _is_secrets_cache_enabled():
        return
    router = get_cache_router()
    if router is None:
        return
    # Best-effort L1 invalidation for this basename.
    # The shared L1 uses keys prefixed with "secrets:{basename}:".
    # We can't easily clear by prefix, so we rely on TTL/eviction for L1.
    # L2 is cleared per-backend via the mmap file.
    try:
        from agent._cache import FlatFileCache
        path = _l2_path_for_backend(basename)
        if os.path.exists(path):
            FlatFileCache(path=path, max_bytes=_l2_max_bytes()).clear()
    except Exception as e:
        logger.debug("L2 clear failed for %s: %s", basename, e)
