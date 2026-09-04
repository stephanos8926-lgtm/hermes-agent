"""Shared substrate for external secret-source backends.

Every backend (Bitwarden, 1Password, …) needs the same handful of
security-sensitive primitives:

  * a uniform result object (:class:`FetchResult`),
  * environment-variable name validation (:func:`is_valid_env_name`),
  * a two-layer fetch cache whose disk half writes atomically with ``0600``
    permissions and honours a TTL (:class:`DiskCache`, :class:`CachedFetch`).

These used to live inline inside ``bitwarden.py``.  Pulling them here means
the atomic-write / ``0600`` / TTL logic is audited and fixed in exactly one
place instead of drifting across copy-pasted per-backend modules — each
backend supplies only its own cache-key shape and a serializer for it.

Nothing in this module ever raises out to the caller's hot path: the disk
layer is strictly best-effort (a miss just triggers a refetch), because a
cache problem must never block Hermes startup.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Generic, Optional, TypeVar

__all__ = [
    "FetchResult",
    "CachedFetch",
    "DiskCache",
    "is_valid_env_name",
    "resolve_cache_home",
]


# ---------------------------------------------------------------------------
# Result object + env-name validation — canonical definitions live in
# ``agent.secret_sources.base`` (the SecretSource contract module); re-exported
# here so backends that import from ``_cache`` keep working.
# ---------------------------------------------------------------------------

from agent.secret_sources.base import (  # noqa: E402
    FetchResult,
    is_valid_env_name,
)


# ---------------------------------------------------------------------------
# Cache entry
# ---------------------------------------------------------------------------


@dataclass
class CachedFetch:
    """A set of fetched secret values plus when they were fetched."""

    secrets: Dict[str, str]
    fetched_at: float

    def is_fresh(self, ttl_seconds: float) -> bool:
        if ttl_seconds <= 0:
            return False
        return (time.time() - self.fetched_at) < ttl_seconds




# ---------------------------------------------------------------------------
# Disk cache
# ---------------------------------------------------------------------------


def resolve_cache_home(home_path: Optional[Path] = None) -> Path:
    """Resolve the Hermes home used for cache paths.

    ``home_path`` is whatever ``load_hermes_dotenv()`` already resolved;
    falling back to ``$HERMES_HOME`` / ``~/.hermes`` keeps direct callers
    (and tests that don't thread a home through) working.
    """
    if home_path is None:
        from hermes_constants import get_hermes_home

        home_path = get_hermes_home()
    return home_path


K = TypeVar("K")


class DiskCache(Generic[K]):
    """Best-effort, profile-aware on-disk cache for fetched secret values.

    One JSON object per backend lives at ``<hermes_home>/cache/<basename>``::

        {"key": "<serialized cache key>", "secrets": {...}, "fetched_at": 1.0}

    The file holds only secret *values* keyed by the serialized cache key —
    never raw auth material.  Backends are responsible for fingerprinting
    tokens/sessions *before* they reach ``key_serializer`` so the token can't
    land in the key.

    Writes are atomic (``mkstemp`` → ``chmod 0600`` → ``os.replace``) and the
    containing ``cache/`` directory is forced to ``0700`` — ``mkdir``'s mode is
    umask-subject, so the chmod is the reliable form.  Both ``read`` and
    ``write`` short-circuit when ``ttl_seconds <= 0``, so setting the TTL to
    zero disables *both* cache layers symmetrically: a user opting out never
    gets secret values written to disk at all.
    """

    def __init__(self, basename: str, *, key_serializer: Callable[[K], str]) -> None:
        self._basename = basename
        self._key_serializer = key_serializer
        # Temp-file prefix derived from the basename so concurrent writers for
        # different backends in the same dir don't collide on the staging name.
        stem = basename.split(".", 1)[0]
        self._tmp_prefix = f".{stem}_"

    def path(self, home_path: Optional[Path] = None) -> Path:
        return resolve_cache_home(home_path) / "cache" / self._basename

    def read(
        self,
        key: K,
        ttl_seconds: float,
        home_path: Optional[Path] = None,
    ) -> Optional[CachedFetch]:
        """Return a fresh cached entry for ``key``, or None.

        Best-effort: any I/O or parse error, a key mismatch, or a stale entry
        all return None so the caller re-fetches.

        When ``cache.secrets.enabled`` is true in config.yaml, an L1+L2
        tiered cache (from :mod:`agent._cache`) fronts the on-disk JSON
        for sub-millisecond reads in the steady state. The JSON is still
        authoritative; a cache-layer failure or miss falls through to the
        on-disk read and the result is written back to the cache layers.
        """
        if ttl_seconds <= 0:
            return None

        from agent.secret_sources._cache_bridge import bridge_read

        return bridge_read(
            basename=self._basename,
            key=key,
            ttl_seconds=ttl_seconds,
            key_serializer=self._key_serializer,
            cls=CachedFetch,
            json_fallback=lambda: self._read_json(key, ttl_seconds, home_path),
        )

    def _read_json(
        self,
        key: K,
        ttl_seconds: float,
        home_path: Optional[Path] = None,
    ) -> Optional[CachedFetch]:
        """The original on-disk JSON read path, now an internal helper.

        This is the fallback when the tiered cache misses or is disabled.
        Atomic-write / 0600 / TTL discipline lives here.
        """
        path = self.path(home_path)
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("key") != self._key_serializer(key):
            return None
        secrets = payload.get("secrets")
        fetched_at = payload.get("fetched_at")
        if not isinstance(secrets, dict) or not isinstance(fetched_at, (int, float)):
            return None
        # JSON permits non-string values; env vars need strings, so coerce by
        # dropping anything that isn't a str→str pair.
        typed: Dict[str, str] = {
            k: v for k, v in secrets.items() if isinstance(k, str) and isinstance(v, str)
        }
        entry = CachedFetch(secrets=typed, fetched_at=float(fetched_at))
        if not entry.is_fresh(ttl_seconds):
            return None
        return entry

    def write(
        self,
        key: K,
        entry: CachedFetch,
        ttl_seconds: float,  # accepted for backward-compat with callers; not
                             # needed here because ``entry.fetched_at`` is
                             # already stamped and the bridge compares against
                             # that.
        home_path: Optional[Path] = None,
    ) -> None:
        """Persist a CachedFetch under ``key``. Best-effort: failures are silent.

        Writes to the on-disk JSON (0600, atomic-rename). When the tiered
        cache is enabled, also writes to L1+L2 so subsequent reads return
        from memory. The on-disk JSON remains the cross-process / crash
        recovery source of truth.
        """
        path = self.path(home_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # mkdir's mode is umask-subject; chmod the dir to 0700 so cache
            # metadata isn't exposed if HERMES_HOME is ever made traversable.
            try:
                os.chmod(path.parent, 0o700)
            except OSError:
                pass
            payload = {
                "key": self._key_serializer(key),
                "secrets": dict(entry.secrets),
                "fetched_at": float(entry.fetched_at),
            }
            fd, tmp = tempfile.mkstemp(
                prefix=self._tmp_prefix, suffix=".tmp", dir=str(path.parent)
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f)
                os.chmod(tmp, 0o600)
                os.replace(tmp, path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError:
            return  # best-effort — a disk-cache miss next invocation is fine

        # Caching is off (caller passed ttl_seconds <= 0). Skip the L1/L2
        # bridge too — the contract is "no caching anywhere", honoring the
        # caller's intent to bypass every layer.
        if ttl_seconds <= 0:
            return

        from agent.secret_sources._cache_bridge import bridge_write

        bridge_write(
            basename=self._basename,
            key=key,
            key_serializer=self._key_serializer,
            entry=entry,
        )

    def clear(self, home_path: Optional[Path] = None) -> None:
        """Delete the on-disk cache file if present (idempotent)."""
        try:
            self.path(home_path).unlink()
        except (FileNotFoundError, OSError):
            pass
