"""Human handoff / fallback tests for the pre-human-review gate.

Author: Chakravardhan

This system has NO live transfer to a person -- that is recorded as a known
blocker in scripts/gate-config.json. What it does have is a handoff to the
clinic counter, spoken on every path where the agent cannot finish the job.
These tests pin that handoff, and pin that no caller-facing string promises a
transfer the system cannot perform.

    python -m pytest tests/test_gate_handoff_fallback.py -v
"""

from __future__ import annotations

import ast

import gate_support
import pytest

from agent import i18n, tts
from agent import language as lang_mod
from agent.reply_templates import verification_failed_reply

FAILURE_KEYS = [
    "generic.tool_failure",
    "generic.counter",
    "booking.failed",
    "reschedule.failed",
    "cancel.failed",
    "history.failed",
    "history.locked",
    "history.disclosure_off",
    "doctor.no_days",
]
TRANSFER_PROMISES = (
    "transfer",
    "connect you",
    "connecting you",
    "স্টাফের কাছে দিচ্ছি",
    "ট্রান্সফার",
    "कनेक्ट कर",
    "ट्रांसफर",
)


@pytest.fixture
def tri(monkeypatch):
    gate_support.trilingual(monkeypatch)


@pytest.mark.parametrize("key", FAILURE_KEYS)
def test_every_failure_path_hands_the_caller_to_the_counter(key, tri):
    for code in lang_mod.ALL_LANGS:
        text = i18n.t(code, key, name="X", date="2026-09-14", time="18:15")
        assert gate_support.mentions_counter(text), f"{key} [{code}]: {text}"


@pytest.mark.parametrize("code", lang_mod.ALL_LANGS)
def test_exhausted_verification_ends_at_the_counter(code, tri):
    assert gate_support.mentions_counter(verification_failed_reply(True, code))


def test_no_caller_facing_string_promises_a_transfer_that_does_not_exist():
    for key, code, text in i18n.all_strings():
        low = text.lower()
        assert not any(p in low for p in TRANSFER_PROMISES), f"{key} [{code}]: {text}"


def test_every_failure_class_has_a_prerecorded_clip():
    """If TTS is what failed, asking it to apologise is circular."""
    assert {"asr_empty", "llm_failure", "tool_failure", "tts_failure"} <= set(tts.FALLBACK_FILES)


def test_the_busy_line_is_prewarmed_so_it_never_waits_on_the_vocoder():
    assert tts.BUSY_LINE in tts.PREWARM_LINES


def test_every_inline_tool_failure_line_in_main_points_to_the_counter():
    """main.py speaks some tool-failure lines inline rather than through
    i18n. Each must still hand the caller somewhere."""
    tree = ast.parse((gate_support.ROOT / "main.py").read_text(encoding="utf-8"))
    checked = 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_speak"):
            continue
        reason = next(
            (
                k.value.value
                for k in node.keywords
                if k.arg == "fallback_reason" and isinstance(k.value, ast.Constant)
            ),
            None,
        )
        if reason != "tool_failure" or len(node.args) < 2:
            continue
        spoken = node.args[1]
        if isinstance(spoken, ast.Constant):
            assert gate_support.mentions_counter(spoken.value), f"line {node.lineno}: {spoken.value}"
        else:
            assert "generic.tool_failure" in ast.unparse(spoken), f"line {node.lineno}"
        checked += 1
    assert checked >= 3
