"""Escalation and abstention tests for the pre-human-review gate.

Author: Chakravardhan

When the agent is not sure, it must say so and step down -- ask again, offer
the keypad, point to the counter -- and it must never act on a guess. These
tests drive main_pcm.py's real turn function with the model and the clinic
API replaced, and assert both what the caller hears and that no backend
action was taken on a turn the agent did not understand.

    python -m pytest tests/test_gate_escalation.py -v
"""

from __future__ import annotations

import asyncio

import gate_support
import pytest

from agent.i18n import t
from agent.llm import ExtractionError
from agent.reply_templates import missing_slot_prompt
from agent.tools_client import ToolCallError

app = gate_support.load_main_pcm()


@pytest.fixture
def wired(monkeypatch):
    tools = gate_support.FakeTools()
    speech = gate_support.SpeechLog()
    monkeypatch.setattr(app, "_tools", tools)
    monkeypatch.setattr(app, "_speak", speech)
    session = app.CallSession(gate_support.FakeWS())
    yield tools, speech, session
    session.cleanup()


def _intent(monkeypatch, result=None, raises=None):
    async def resolve(session, text):
        if raises is not None:
            raise raises
        return result

    monkeypatch.setattr(app, "_resolve_intent", resolve)


def _turn(session, text):
    asyncio.run(app._run_turn(session, "", text_override=text))


def test_a_model_failure_apologises_and_touches_nothing(monkeypatch, wired):
    tools, speech, session = wired
    _intent(monkeypatch, raises=ExtractionError("budget exhausted"))
    _turn(session, "সিবিসির দাম")
    assert speech.lines == [(t(session.lang, "fallback.llm_failure"), "llm_failure")]
    assert tools.calls == []


def test_an_unclear_turn_asks_again_and_touches_nothing(monkeypatch, wired):
    tools, speech, session = wired
    _intent(monkeypatch, {"intent": "unclear", "slots": {}, "direct_reply_bn": None})
    _turn(session, "হুম")
    assert speech.texts == [t(session.lang, "fallback.unclear")]
    assert tools.calls == []


def test_a_missing_slot_is_asked_for_never_guessed(monkeypatch, wired):
    tools, speech, session = wired
    _intent(monkeypatch, {"intent": "test_rate", "slots": {"test_name": None}, "direct_reply_bn": None})
    _turn(session, "একটা টেস্টের দাম কত")
    assert speech.texts == [missing_slot_prompt("test_rate", "test_name", session.lang)]
    assert tools.calls == []


def test_a_booking_is_never_placed_with_fields_missing(monkeypatch, wired):
    tools, speech, session = wired
    _intent(
        monkeypatch, {"intent": "book_appointment", "slots": {"doctor_name": "সেন"}, "direct_reply_bn": None}
    )
    _turn(session, "ডাক্তার সেনের কাছে বুক করুন")
    assert "book_appointment" not in tools.called()
    assert session.pending and session.pending["awaiting"] == "date"
    assert speech.texts == [missing_slot_prompt("book_appointment", "date")]


def test_a_backend_failure_escalates_the_caller_to_the_counter(monkeypatch, wired):
    tools, speech, session = wired
    tools.raises["get_test_rate"] = ToolCallError("connection refused")
    _intent(monkeypatch, {"intent": "test_rate", "slots": {"test_name": "সিবিসি"}, "direct_reply_bn": None})
    _turn(session, "সিবিসির দাম")
    ((text, reason),) = speech.lines
    assert reason == "tool_failure"
    assert gate_support.mentions_counter(text)


def test_repeated_failures_climb_the_ladder_to_the_keypad(wired):
    _tools, speech, session = wired
    asyncio.run(app._clarify_or_offer_keypad(session, reason="asr_empty"))
    assert speech.lines[-1] == (app.CLARIFY_PROMPT_BN, "asr_empty")
    asyncio.run(app._clarify_or_offer_keypad(session, reason="asr_empty"))
    assert speech.lines[-1] == (app.KEYPAD_PROMPT_BN, "keypad_offer")
    assert any('"_keypad"' in frame for frame in session.ws.frames)
