"""Safety-policy tests for the pre-human-review gate.

Author: Chakravardhan

Each test pins one rule this line must never break, whatever else changes:
the model never authors a clinical answer, a missing fact is never guessed,
booking (which carries PII) never takes the string-matching shortcut, and
private information is never spoken on an audio path not proven private.

    python -m pytest tests/test_gate_safety_policy.py -v
"""

from __future__ import annotations

import asyncio
import re
import types

import gate_support
import pytest

from agent import i18n, llm, privacy
from agent import language as lang_mod
from agent.echo_guard import PATH_HANDSET, PATH_SPEAKERPHONE, PATH_UNKNOWN
from agent.fast_path import Catalogue, FastPath
from agent.reply_templates import test_rate_reply as rate_reply

golden = gate_support.load_script("gate_golden")
SLOT_KEYS = ("test_name", "doctor_name", "department", "date", "time_slot", "patient_name", "phone")
DIGITS = re.compile(r"[0-9০-৯०-९]")


@pytest.fixture
def tri(monkeypatch):
    gate_support.trilingual(monkeypatch)


def _extraction(intent, reply=None):
    return {"intent": intent, "slots": {k: None for k in SLOT_KEYS}, "direct_reply_bn": reply}


def test_the_model_may_not_author_a_reply_outside_smalltalk():
    """A price the model made up is this system's worst failure. The schema
    guard strips any direct reply the model offers for a clinical intent."""
    data = _extraction("test_rate", "CBC costs 50 rupees")
    ok, _errors = llm._validate(data)
    assert ok
    assert data["direct_reply_bn"] is None


def test_a_smalltalk_reply_is_kept():
    data = _extraction("smalltalk", "নমস্কার!")
    ok, _errors = llm._validate(data)
    assert ok
    assert data["direct_reply_bn"] == "নমস্কার!"


def test_an_invented_intent_is_rejected():
    ok, errors = llm._validate(_extraction("prescribe_medicine"))
    assert not ok
    assert any("invalid intent" in e for e in errors)


def test_the_extractor_prompt_forbids_stating_clinic_facts():
    prompt = llm.SYSTEM_PROMPT_TEMPLATE
    assert "do not guess or state any of those" in prompt
    assert "Never invent a patient name, phone number, or date" in prompt


def test_booking_never_takes_the_string_matching_shortcut():
    """Booking carries a name and a phone number; it must always go through
    the full extraction path, never a fuzzy match."""
    fast = FastPath(Catalogue(golden.seed_catalogue()), today=golden.TODAY)
    assert fast.resolve("ডাক্তার সেনের অ্যাপয়েন্টমেন্ট বুক করতে চাই") is None
    assert fast.resolve("সিবিসি টেস্টের স্লট বুকিং করব") is None


@pytest.mark.parametrize("code", lang_mod.ALL_LANGS)
def test_a_price_that_was_not_found_is_never_quoted(code, tri):
    reply = rate_reply({"test_name": "অজানা"}, {"found": False, "query": "অজানা", "did_you_mean": []}, code)
    assert reply
    assert not DIGITS.search(reply), reply


@pytest.mark.parametrize("code", lang_mod.ALL_LANGS)
def test_a_backend_failure_is_never_worded_as_not_found(code, tri):
    failure = i18n.t(code, "generic.tool_failure")
    not_found = i18n.t(code, "test.not_found", name="X")
    assert failure != not_found
    assert gate_support.mentions_counter(failure), failure


def test_an_unclassified_audio_path_is_never_treated_as_private(monkeypatch):
    monkeypatch.delenv("VOICE_AGENT_HISTORY_REQUIRE_PRIVATE_PATH", raising=False)
    monkeypatch.delenv("VOICE_AGENT_HISTORY_DISCLOSURE", raising=False)
    unknown = types.SimpleNamespace(classify=lambda: PATH_UNKNOWN)
    assert privacy.audio_path_is_private(unknown) == (False, privacy.UNSAFE_UNKNOWN)


def test_a_speakerphone_blocks_disclosure(monkeypatch):
    monkeypatch.delenv("VOICE_AGENT_HISTORY_REQUIRE_PRIVATE_PATH", raising=False)
    speaker = types.SimpleNamespace(classify=lambda: PATH_SPEAKERPHONE)
    assert privacy.audio_path_is_private(speaker) == (False, privacy.UNSAFE_SPEAKERPHONE)
    handset = types.SimpleNamespace(classify=lambda: PATH_HANDSET)
    assert privacy.audio_path_is_private(handset) == (True, privacy.SAFE)


def test_history_is_never_fetched_before_verification(monkeypatch):
    """Asking for history starts a challenge. The history itself is not
    requested -- it is not fetched and then withheld, it is not fetched."""
    app = gate_support.load_main_pcm()
    monkeypatch.setenv("VOICE_AGENT_HISTORY_REQUIRE_PRIVATE_PATH", "0")
    tools = gate_support.FakeTools(responses={"begin_verification": {"factor": "pin", "locked": False}})
    speech = gate_support.SpeechLog()
    monkeypatch.setattr(app, "_tools", tools)
    monkeypatch.setattr(app, "_speak", speech)

    async def intent(session, text):
        return {"intent": "patient_history", "slots": {"phone": "9000000001"}, "direct_reply_bn": None}

    monkeypatch.setattr(app, "_resolve_intent", intent)
    session = app.CallSession(gate_support.FakeWS())
    try:
        asyncio.run(app._run_turn(session, "", text_override="আমার আগের টেস্টগুলো বলুন"))
    finally:
        session.cleanup()
    assert "begin_verification" in tools.called()
    assert "read_history" not in tools.called()
    assert session.history_token is None
