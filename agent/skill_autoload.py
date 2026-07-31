"""Auto-load skills based on trigger matching against user messages.

This module scans loaded skills' frontmatter for ``triggers`` and
``dependencies`` fields, matches them against incoming user input, and
injects the full skill body as pre-response context.

Design decisions:
- Trigger matching is word-boundary aware and unordered: a trigger matches
  when ALL of its non-stop words appear as whole words in the user message
  (in any order). This avoids substring bleed (``debug`` ≠ ``debugger``)
  and allows natural phrasing (trigger "write plan" matches "write a plan").
- Single-word triggers match the word at a word boundary only.
- Dependencies are resolved recursively with a depth cap of 3.
- Circular-reference detection via ``visited`` set prevents infinite loops.
- Each skill is size-capped at ``CAP_TOKENS`` characters (~4K chars ≈ 1K tokens).
- Maximum ``MAX_LOADED_PER_TURN`` auto-loaded skills to prevent bloat.
- Config read on every call (lightweight single-YAML parse); cached in-memory.
"""

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from hermes_constants import get_skills_dir
from agent.skill_utils import parse_frontmatter, iter_skill_index_files, get_all_skills_dirs

logger = logging.getLogger(__name__)

# ── Tuning constants ────────────────────────────────────────────────

CAP_TOKENS: int = 4096        # Characters per skill body (≈1K tokens)
MAX_LOADED_PER_TURN: int = 2  # Auto-loaded skills max per turn
DEPENDENCY_DEPTH_CAP: int = 3 # How deep dependency chain resolves

# Words too common to be meaningful for trigger matching.
_STOP_WORDS: frozenset[str] = frozenset({
    "a", "an", "the", "to", "for", "in", "of", "with", "on", "at", "by",
    "is", "it", "be", "do", "does", "did", "was", "were", "are", "am",
    "or", "and", "but", "not", "no", "yes", "so", "if", "then", "than",
    "i", "you", "we", "they", "he", "she", "it", "its", "me", "us", "them",
    "my", "your", "our", "their", "this", "that", "these", "those",
    "have", "has", "had", "will", "would", "can", "could", "should",
    "please", "help", "need", "want", "about", "into", "over", "under",
})

_WORD_RE = re.compile(r"[a-z][a-z0-9]*")


def _normalize_words(text: str) -> List[str]:
    """Split text into lowercase content words (stop words removed).

    Hyphenated triggers like "code-review" become ["code", "review"] so
    matching stays word-boundary based rather than character based.
    """
    words = _WORD_RE.findall(text.lower())
    return [w for w in words if w not in _STOP_WORDS and len(w) > 1]


def _read_autoload_config() -> Tuple[bool, int, int, int]:
    """Read all autoload settings from skills: section in config.yaml.

    Returns (enabled, min_score, dep_depth, body_cap_chars, max_skills).
    Defaults to (False, 1, 3, 4096, 2) when config is unreadable or keys
    are missing.
    """
    enabled = False
    min_score = 1
    dep_depth = 3
    body_cap = 4096
    max_skills = 2
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        skills = cfg.get("skills") if isinstance(cfg, dict) else None
        if isinstance(skills, dict):
            enabled = bool(skills.get("autoload_enabled", False))
            min_score = int(skills.get("autoload_min_score", 1))
            dep_depth = int(skills.get("autoload_dependency_depth", 3))
            body_cap = int(skills.get("autoload_body_cap_chars", 4096))
            max_skills = int(skills.get("autoload_max_skills", 2))
            # Clamp to sane bounds
            if min_score < 0: min_score = 1
            if dep_depth < 0: dep_depth = 0
            if body_cap < 0: body_cap = 4096
            if max_skills < 0: max_skills = 2
    except Exception:
        pass  # fail safe: keep defaults
    return enabled, min_score, dep_depth, body_cap, max_skills


def extract_triggers(frontmatter: Dict[str, Any]) -> List[str]:
    """Return list of trigger strings from a skill's frontmatter."""
    raw = frontmatter.get("triggers")
    if isinstance(raw, list):
        return [str(t) for t in raw]
    if isinstance(raw, str):
        # Comma-separated or newline-separated
        return [t.strip() for t in raw.replace("\n", ",").split(",") if t.strip()]
    return []


def extract_dependencies(frontmatter: Dict[str, Any]) -> List[str]:
    """Return list of dependency skill names from frontmatter."""
    raw = frontmatter.get("dependencies")
    if isinstance(raw, list):
        return [str(d) for d in raw]
    if isinstance(raw, str):
        return [d.strip() for d in raw.replace("\n", ",").split(",") if d.strip()]
    return []


class _SkillIndex:
    """Pre-scanned catalogue of skills → metadata for fast lookup."""

    def __init__(self) -> None:
        self._entries: Dict[str, Dict[str, Any]] = {}  # name → {path, triggers, deps, desc}

    def build(self, skills_dir: Path, *, external_dirs: Optional[List[Path]] = None) -> None:
        """Scan all SKILL.md files and cache their frontmatter."""
        all_dirs = [skills_dir] + (external_dirs or [])
        for sdir in all_dirs:
            if not sdir.exists():
                continue
            for sf in iter_skill_index_files(sdir, "SKILL.md"):
                try:
                    content = sf.read_text(encoding="utf-8")
                    fm, _body = parse_frontmatter(content)
                    name = fm.get("name") or sf.stem
                    if not name or name.startswith("."):
                        continue
                    self._entries[name] = {
                        "path": sf,
                        "triggers": extract_triggers(fm),
                        "deps": extract_dependencies(fm),
                        "desc": fm.get("description", ""),
                    }
                except Exception:
                    pass  # skip malformed files silently

    def scan_triggers_for(self, user_input: str, min_score: int = 1) -> List[Tuple[str, float]]:
        """Score skills by how many triggers match the user input.

        Uses word-boundary aware matching: a trigger matches when ALL of its
        non-stop words appear as whole words in the user message (any order).
        Hyphenated triggers are split at the hyphen so each word is checked
        independently.

        Returns sorted [(skill_name, score), ...] descending.
        Score = count of trigger matches found.
        Only returns skills with score >= min_score.
        """
        scored: List[Tuple[str, float]] = []
        input_words: set[str] = set(_normalize_words(user_input))
        if not input_words:
            return scored

        for name, info in self._entries.items():
            score = 0
            for trig in info["triggers"]:
                trig_words = _normalize_words(trig)
                if not trig_words:
                    continue
                # All trigger words must appear as whole words in input
                if all(tw in input_words for tw in trig_words):
                    score += 1
            if score >= min_score:
                scored.append((name, score))
        scored.sort(key=lambda x: -x[1])
        return scored

    def resolve_deps(
        self,
        skill_name: str,
        visited: Optional[Set[str]] = None,
        depth: int = 0,
        dep_depth: int = DEPENDENCY_DEPTH_CAP,
    ) -> List[str]:
        """Resolve dependency chain with circular-guard and depth cap.

        Returns deduplicated list of dependent skill names (depth-first order).
        """
        if visited is None:
            visited = set()
        if depth >= dep_depth or skill_name in visited:
            return []
        visited.add(skill_name)

        info = self._entries.get(skill_name)
        if not info:
            return []

        result: List[str] = []
        for dep in info["deps"]:
            if dep not in visited:
                result.append(dep)  # Include immediate dep, not just transitive
                sub = self.resolve_deps(dep, visited.copy(), depth + 1, dep_depth)
                for s in sub:
                    if s not in result:
                        result.append(s)
        return result

    def load_body(self, skill_name: str, cap: int = CAP_TOKENS) -> str:
        """Load and return skill body, capped at ``cap`` characters."""
        info = self._entries.get(skill_name)
        if not info:
            return ""
        try:
            path: Path = info["path"]
            content = path.read_text(encoding="utf-8")
            _, body = parse_frontmatter(content)
            if len(body) > cap:
                logger.debug("Capping skill '%s' body to %d chars", skill_name, cap)
                body = body[:cap]
            return body.strip()
        except Exception as e:
            logger.warning("Failed to load skill body '%s': %s", skill_name, e)
            return ""


def auto_load_skills(user_input: str) -> str:
    """Match triggers → resolve deps → inject bodies.

    Returns a block of text that can be prepended to the response context.
    Format:

        [[ AUTO-LOADED SKILL: <name> ]]
        <full body...>
        [[ END SKILL ]]

    Called once per turn when user sends a message.
    Fails gracefully — always returns "" on error.
    """
    try:
        if not user_input or not user_input.strip():
            return ""

        enabled, min_score, dep_depth, body_cap, max_skills = _read_autoload_config()
        if not enabled:
            return ""

        index = _SkillIndex()
        skills_dir = get_skills_dir()
        all_dirs_list = list(get_all_skills_dirs())
        index.build(skills_dir, external_dirs=all_dirs_list[1:])

        # 1. Score skills by trigger matches
        scored = index.scan_triggers_for(user_input, min_score)
        if not scored:
            return ""

        # 2. Take top-N matches (by score)
        picked_names: List[str] = []
        for name, score in scored[:max_skills]:
            picked_names.append(name)

        if not picked_names:
            return ""

        # 3. Inject bodies (with dependency ordering)
        blocks: List[str] = []
        for name in picked_names:
            # Resolve deps first (so prerequisite skills load before dependents)
            deps = index.resolve_deps(name, dep_depth=dep_depth)
            for dep_name in deps:
                body = index.load_body(dep_name, cap=body_cap)
                if body:
                    blocks.append(_format_block(dep_name, body))

            # Then the main skill itself
            body = index.load_body(name, cap=body_cap)
            if body:
                blocks.append(_format_block(name, body))

        return "\n\n".join(blocks)
    except Exception as e:
        logger.debug("auto_load_skills failed: %s", e)
        return ""


def _format_block(name: str, body: str) -> str:
    """Wrap a skill body in delimiters the model can recognise."""
    header = f"[[ AUTO-LOADED SKILL: {name} ]]"
    footer = "[[ END SKILL ]]"
    return f"{header}\n{body}\n{footer}"
