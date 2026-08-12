"""skill_auto_load — conservative, config-gated skill auto-loading.

RapidWebs fork feature. Ships INERT (off by default) and is enabled only via
``skills.auto_load.enabled`` in config.yaml (we enable it in our own config).

Implements the A–E spec (see docs/rw-skill-auto-load-DESIGN.md):
  A. Trigger-based scoring — word-boundary matches on skill ``triggers`` /
     name with a confidence threshold + negative_triggers, so we only
     auto-load genuinely relevant skills (avoids the false-positive trap
     that upstream deliberately avoided).
  B. Reference/template/script auto-load with per-category count + char caps,
     truncated with a pointer line to load the rest on demand.
  C. Dedup window — each skill + dependents load once per session / per N
     turns / per N minutes.
  D. Max reference count + recursion (cycle) guard over related_skills.
  E. Everything under ``skills.auto_load`` in config.yaml.

All public entry points are safe: read-only, time/char-bounded, exception-
tolerant — a failure must NEVER crash the prompt build.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────────────
# Config defaults (merged under config.yaml ``skills.auto_load``)
# ────────────────────────────────────────────────────────────────────────────
DEFAULT_AUTO_LOAD_CONFIG = {
    "enabled": False,          # master switch — OFF by default
    "mode": "hybrid",          # prompt | mechanical | hybrid
    "min_confidence": 0.6,     # score threshold for trigger-based (A) preload
    "max_auto_load": 3,        # max skills auto-loaded per turn (A)
    # B — per-category caps
    "references_max_count": 4,
    "references_max_chars": 2000,
    "templates_max_count": 4,
    "templates_max_chars": 1500,
    "scripts_max_count": 2,
    "scripts_max_chars": 1200,
    "assets_max_count": 2,
    "assets_max_chars": 1200,
    # C — dedup window
    "dedup_window": "once_per_session",  # or once_per_turns / once_per_minutes
    "dedup_turns": 10,
    "dedup_minutes": 5,
    # D — recursion guard + total cap
    "max_reference_files": 8,
    "max_depth": 6,
    # E — pointer line toggle
    "truncation_pointer": True,
}

# Session-scoped dedup store: {session_key: {skill_name: {"ts": float, "turns": int}}}
_dedup_store: Dict[str, Dict[str, Dict[str, Any]]] = {}
_dedup_lock = threading.Lock()

# Turn counter per session (bumped by the caller or internally).
_turn_counters: Dict[str, int] = {}


def load_auto_load_config() -> dict:
    """Load the ``skills.auto_load`` config, merged over hardcoded defaults.

    Never raises; returns at least the defaults dict (so callers can safely
    read any key).
    """
    merged = dict(DEFAULT_AUTO_LOAD_CONFIG)
    try:
        from agent.skill_preprocessing import load_skills_config

        skills_cfg = load_skills_config() or {}
        section = skills_cfg.get("auto_load")
        if isinstance(section, dict):
            merged.update(section)
    except Exception:
        logger.debug("Could not load skills.auto_load config", exc_info=True)
    return merged


def is_enabled() -> bool:
    """Master switch (code default OFF; our config turns it ON)."""
    return bool(load_auto_load_config().get("enabled", False))


# ────────────────────────────────────────────────────────────────────────────
# A — Trigger scoring
# ────────────────────────────────────────────────────────────────────────────
_WORD_RE = re.compile(r"\b([\w][\w\-']*)\b")


def _word_set(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(text or "")}


def score_skill_for_message(
    skill: dict,
    message: str,
    config: Optional[dict] = None,
) -> float:
    """Score a skill's relevance to an incoming message (0.0–1.0).

    Uses skill ``triggers`` (frontmatter) + the skill name. Multi-word phrases
    score higher per-hit than single words. ``negative_triggers`` subtract score.
    """
    cfg = config or load_auto_load_config()
    if not message:
        return 0.0

    msg_lower = message.lower()
    msg_words = _word_set(message)

    triggers = skill.get("triggers") or []
    if isinstance(triggers, str):
        triggers = [triggers]
    negative = skill.get("negative_triggers") or []
    if isinstance(negative, str):
        negative = [negative]

    score = 0.0
    triggers_matched = 0

    # Exact word-boundary match on each trigger (phrase or single word).
    for trig in triggers:
        t = str(trig).strip().lower()
        if not t:
            continue
        # Multi-word phrase → require the full phrase present (word-boundary).
        if " " in t:
            if re.search(rf"\b{re.escape(t)}\b", msg_lower):
                score += 0.8
                triggers_matched += 1
        else:
            if t in msg_words:
                score += 0.7
                triggers_matched += 1

    # Skill name as a soft trigger (word-boundary).
    skill_name = str(skill.get("name") or "").strip().lower()
    if skill_name and skill_name in msg_words:
        score += 0.3
        triggers_matched += 1

    # Negative triggers veto the skill entirely (prevents false auto-loads).
    # A single negative-trigger hit means "do not auto-load this skill here",
    # regardless of positive matches — stronger than a soft decrement, and it
    # directly addresses the false-positive concern that kept upstream from
    # shipping trigger-based auto-loading.
    for neg in negative:
        n = str(neg).strip().lower()
        if not n:
            continue
        if " " in n:
            if re.search(rf"\b{re.escape(n)}\b", msg_lower):
                return 0.0
        elif n in msg_words:
            return 0.0

    # Require at least one positive trigger hit to be considered at all.
    if triggers_matched == 0:
        return 0.0

    return max(0.0, min(1.0, score))


def select_auto_load_skills(
    candidates: List[dict],
    message: str,
    config: Optional[dict] = None,
) -> List[dict]:
    """Return the skills to auto-load for ``message``, capped by max_auto_load.

    Only skills whose score clears ``min_confidence`` are considered, sorted
    by score (highest first). Honors the A-side max per turn.
    """
    cfg = config or load_auto_load_config()
    if not cfg.get("enabled") or not message:
        return []
    threshold = float(cfg.get("min_confidence", 0.6))
    max_load = int(cfg.get("max_auto_load", 3))

    scored = []
    for skill in candidates:
        s = score_skill_for_message(skill, message, cfg)
        if s >= threshold:
            scored.append((s, skill))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [skill for _, skill in scored[:max_load]]


# ────────────────────────────────────────────────────────────────────────────
# C — Dedup window
# ────────────────────────────────────────────────────────────────────────────
def _session_key(session_id: Any) -> str:
    return str(session_id or "default")


def mark_loaded(skill_name: str, session_id: Any) -> None:
    """Record that ``skill_name`` was injected into ``session_id`` (now)."""
    key = _session_key(session_id)
    with _dedup_lock:
        _dedup_store.setdefault(key, {})[skill_name] = {
            "ts": time.time(),
            "turns": _turn_counters.get(key, 0),
        }


def bump_turn(session_id: Any) -> None:
    """Increment the per-session turn counter (call once per agent turn)."""
    key = _session_key(session_id)
    with _dedup_lock:
        _turn_counters[key] = _turn_counters.get(key, 0) + 1


def should_skip_dedup(skill_name: str, session_id: Any, config: Optional[dict] = None) -> bool:
    """Whether auto-loading ``skill_name`` in ``session_id`` is suppressed by the
    configured dedup window.

    Returns True (skip) when the skill was already loaded within the window.
    """
    cfg = config or load_auto_load_config()
    key = _session_key(session_id)
    with _dedup_lock:
        entry = _dedup_store.get(key, {}).get(skill_name)
        if not entry:
            return False
        window = cfg.get("dedup_window", "once_per_session")
        now = time.time()
        if window == "once_per_session":
            return True  # never re-inject within the session
        if window == "once_per_turns":
            turns_since = _turn_counters.get(key, 0) - entry["turns"]
            return turns_since < int(cfg.get("dedup_turns", 10))
        if window == "once_per_minutes":
            return (now - entry["ts"]) < float(cfg.get("dedup_minutes", 5)) * 60.0
        return True  # unknown window → safest to dedup


def reset_session(session_id: Any) -> None:
    """Clear dedup/turn state for a session (e.g. on /reset or session end)."""
    key = _session_key(session_id)
    with _dedup_lock:
        _dedup_store.pop(key, None)
        _turn_counters.pop(key, None)


# ────────────────────────────────────────────────────────────────────────────
# D — Recursion guard + total cap
# ────────────────────────────────────────────────────────────────────────────
def walk_related_skills(
    start_skill: dict,
    resolver: Any,
    config: Optional[dict] = None,
) -> List[str]:
    """Return the flat list of related skill names to auto-load (none heavy).

    BFS/DFS with a visited set + depth cap over ``related_skills`` so cyclic
    graphs (A → B → A) terminate. ``resolver`` must be callable:
    ``resolver(name) -> skill dict | None``.
    """
    cfg = config or load_auto_load_config()
    max_depth = int(cfg.get("max_depth", 6))
    max_total = int(cfg.get("max_reference_files", 8))

    visited: set[str] = set()
    result: List[str] = []
    stack: List[Tuple[str, int]] = [(
        str(start_skill.get("name") or ""),
        0,
    )]
    while stack and len(result) < max_total:
        name, depth = stack.pop()
        rel = str(name).strip().lower()
        if not rel or rel in visited or depth > max_depth:
            continue
        visited.add(rel)
        if rel != str(start_skill.get("name") or "").lower():
            result.append(name)
        if len(result) >= max_total:
            break
        sd = resolver(name) if resolver else None
        if not sd:
            continue
        related = sd.get("related_skills") or []
        if isinstance(related, str):
            related = [related]
        for child in reversed(list(related)):
            stack.append((str(child), depth + 1))
    return result


# ────────────────────────────────────────────────────────────────────────────
# B — Reference auto-load with caps + truncation + pointer
# ────────────────────────────────────────────────────────────────────────────
_CATEGORY_CAPS = {
    "references": ("references_max_count", "references_max_chars"),
    "templates": ("templates_max_count", "templates_max_chars"),
    "scripts": ("scripts_max_count", "scripts_max_chars"),
    "assets": ("assets_max_count", "assets_max_chars"),
}


def build_auto_load_prompt(
    message: str,
    candidates: List[dict],
    session_id: Any = None,
    resolver=None,
) -> Tuple[str, List[str]]:
    """Return (prompt_block, loaded_names) for skills auto-loaded by trigger.

    Combines A (trigger selection), C (dedup), and D (related-skill recursion).
    The returned block should be placed into the system prompt / message
    context when non-empty. Honors skills.auto_load.enabled (returns empty
    when disabled). Never raises.
    """
    cfg = load_auto_load_config()
    if not cfg.get("enabled") or not message:
        return "", []

    selected = select_auto_load_skills(candidates, message, cfg)
    if not selected:
        return "", []

    loaded_names: List[str] = []
    parts: List[str] = []

    # BFS/DES over related skills with dedup, so we load the skill + its
    # relevant dependents exactly once (C) with recursion termination (D).
    to_load = list(selected)
    seen: set[str] = set()
    while to_load:
        skill = to_load.pop(0)
        name = str(skill.get("name") or "").strip()
        if not name:
            continue
        lower = name.lower()
        if lower in seen:
            continue
        seen.add(lower)

        if session_id is not None and should_skip_dedup(name, session_id, cfg):
            continue

        parts.append(
            f"[Auto-loaded skill: {name}]\n"
            f"{skill.get('description') or ''}"
        )
        loaded_names.append(name)
        if session_id is not None:
            mark_loaded(name, session_id)

        # Related skills (D) — resolve and enqueue if we have a resolver.
        if resolver is not None:
            related = skill.get("related_skills") or []
            if isinstance(related, str):
                related = [related]
            for rel in related:
                rel = str(rel).strip()
                if not rel or rel.lower() in seen:
                    continue
                rd = resolver(rel) if callable(resolver) else None
                if rd is not None:
                    to_load.append(rd)

    if not parts:
        return "", []
    block = "\n\n".join(parts)
    return block, loaded_names


def auto_load_references(
    skill_dir,
    config: Optional[dict] = None,
) -> str:
    """Return auto-loaded content for a skill's supporting files.

    For each category (references/templates/scripts/assets), read up to
    ``<cat>_max_count`` files, each capped at ``<cat>_max_chars`` chars, and
    emit a truncated block with a pointer line. Returns "" when disabled or
    when there is nothing to load.
    """
    cfg = config or load_auto_load_config()
    if not cfg.get("enabled") or not skill_dir:
        return ""
    pointer_enabled = bool(cfg.get("truncation_pointer", True))

    from pathlib import Path

    root = Path(skill_dir)
    if not root.is_dir():
        return ""

    blocks: List[str] = []
    total_files = 0

    for category, (count_key, chars_key) in _CATEGORY_CAPS.items():
        cat_dir = root / category
        if not cat_dir.is_dir():
            continue
        max_count = int(cfg.get(count_key, 0))
        max_chars = int(cfg.get(chars_key, 0))
        if max_count <= 0 or max_chars <= 0:
            continue

        files = sorted(
            f for f in cat_dir.rglob("*") if f.is_file() and not f.is_symlink()
        )[:max_count]

        for f in files:
            if total_files >= int(cfg.get("max_reference_files", 8)):
                return "\n".join(blocks)
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            rel = str(f.relative_to(root))
            if len(text) > max_chars:
                chunk = text[:max_chars]
                pointer = (
                    f"\n… ({rel} truncated; load the rest with "
                    f'skill_view(file_path="{rel}"))'
                    if pointer_enabled
                    else ""
                )
                blocks.append(f"### {rel}\n{chunk}{pointer}")
            else:
                blocks.append(f"### {rel}\n{text}")
            total_files += 1

    return "\n\n".join(blocks)