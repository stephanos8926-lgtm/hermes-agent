"""Tests for the custom compaction prompt feature (compression.prompt).

Verifies that ContextCompressor honours a ``compaction_prompt_override`` by
replacing the default summarizer preamble in both the batch-and the
micro-summary compaction prompts, while always preserving the [REDACTED]
credential rule and the stable output structure.
"""

from __future__ import annotations

import pytest

from agent.context_compressor import ContextCompressor

CUSTOM = "You are an operations-focused summarizer. Prioritise infra facts."


def _make(override=None):
    """Minimal __new__-based double (mirrors the project's test pattern) but with
    a real __init__ so compaction_prompt is set per the override."""
    if override is None:
        c = ContextCompressor.__new__(ContextCompressor)
        # compact_prompt intentionally unset -> exercise the getattr fallback
        c.protect_first_n = 2
        c.protect_last_n = 5
        c.tail_token_budget = 20000
        c.context_length = 200000
        c.threshold_percent = 0.80
        c.threshold_tokens = 160000
        c.summary_target_ratio = 0.20
        c.max_summary_tokens = 10000
        c.quiet_mode = True
        c.compression_count = 0
        c.last_prompt_tokens = 0
        c._previous_summary = None
        c._ineffective_compression_count = 0
        c._verify_compaction_cleared_threshold = False
        c._summary_failure_cooldown_until = 0.0
        c.summary_model = None
        c.model = "test-model"
        c.provider = "test"
        c.base_url = "http://localhost"
        c.api_key = "test-key"
        c.api_mode = "chat_completions"
        return c
    return ContextCompressor(
        model="test-model",
        provider="test",
        base_url="http://localhost",
        api_key="test-key",
        compaction_prompt_override=override,
    )


def test_default_preamble_used_when_no_override():
    """Without an override the default summarizer preamble is present."""
    c = _make(None)
    prompt = c._build_micro_summary_prompt(
        existing_summary="", exchange_text="user: hello"
    )
    assert "summarization agent creating a compact record" in prompt[1]["content"]


def test_custom_prompt_replaces_default_in_micro_prompt():
    c = _make(CUSTOM)
    prompt = c._build_micro_summary_prompt(
        existing_summary="", exchange_text="user: hello"
    )
    content = prompt[1]["content"]
    assert CUSTOM in content
    assert "summarization agent creating a compact record" not in content


def test_redaction_rule_preserved_with_custom_prompt():
    """The [REDACTED] credential rule must survive a custom prompt."""
    c = _make(CUSTOM)
    prompt = c._build_micro_summary_prompt(
        existing_summary="", exchange_text="user: hello"
    )
    assert "[REDACTED]" in prompt[1]["content"]


def test_bare_double_without_attribute_does_not_crash():
    """A bare __new__ double (no compaction_prompt attr) must not raise."""
    c = _make(None)
    prompt = c._build_micro_summary_prompt(
        existing_summary="", exchange_text="x"
    )
    assert isinstance(prompt, list)


def test_empty_override_treated_as_no_override():
    """Whitespace/empty override is normalized to None (default prompt used)."""
    c = ContextCompressor(
        model="test-model",
        provider="test",
        base_url="http://localhost",
        api_key="test-key",
        compaction_prompt_override="   \n  ",
    )
    assert c.compaction_prompt is None


def test_override_is_stripped():
    c = ContextCompressor(
        model="test-model",
        provider="test",
        base_url="http://localhost",
        api_key="test-key",
        compaction_prompt_override="  custom\n  ",
    )
    assert c.compaction_prompt == "custom"