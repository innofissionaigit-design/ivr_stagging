"""Tests for "the phone and the message channel agree".

Author: Chakravardhan
Story:  "As a patient, I want the phone and the message channel to agree, so
         that I know which one to believe."
Criteria: a shared answer service backs every channel, and a regression suite
          asks the same question set on each, asserting equivalent answers. A
          divergence fails the build.

HOW THE SUITE WORKS
-------------------
THE SAME QUESTION SET (`QUESTIONS` below) is put to both channels in the
same process, against the same faked clinic responses and the same faked
model output. The only thing that differs between the two runs is the
channel. The reply must then be the SAME WORDS -- not merely the same facts,
because "650 টাকা" on the phone and "Rs 650" by message would leave a patient
wondering which to believe.

A DIVERGENCE FAILS THE BUILD. These tests live in tests/, so they are part
of the `unit-tests` suite the gate runs; nothing in the gate configuration
had to change. Two guards do the work:

  * the answers themselves are compared, question by question;
  * main.py is counted: every place that asks which channel it is must be
    one of the divergences declared in agent/answer_contract.py. A new
    `if this is a message` fails here until somebody declares it and says why.

WHAT IS NOT ASSERTED
--------------------
That the two channels ask for a booking in the same number of turns. They do
not, and that is declared: a patient writing from WhatsApp is not asked for
the number they are writing from. The booking that results is identical, and
that IS asserted.

    python -m pytest tests/test_channel_parity.py -v
"""

from __future__ import annotations

import asyncio
import json
import time
import types
import uuid

import gate_support
import httpx
import pytest

from agent import answer_contract, i18n, message_service, privacy, whatsapp
from agent.reply_templates import (
    doctor_availability_reply,
    doctors_by_department_reply,
    missing_slot_prompt,
    payment_reply,
    report_collection_reply,
)
from agent.reply_templates import test_rate_reply as rate_reply

PHONE = "9000000101"
SENDER = "91" + PHONE

_SLOT_KEYS = ("test_name", "doctor_name", "department", "date", "time_slot", "patient_name", "phone")

_RATE = {
    "found": True,
    "rate_inr": 650,
    "test_name": "Lipid Profile",
    "test_name_bn": "লিপিড প্রোফাইল",
    "sample_type": "Blood",
    "report_time_hours": 24,
}
_AVAILABILITY = {
    "found": True,
    "available": True,
    "doctor_name": "Dr. A. Sen",
    "doctor_name_bn": "সেন",
    "date": "2026-09-20",
    "chamber_hours": "18:00-20:00",
}
_DEPARTMENT = {
    "found": True,
    "department": "Cardiology",
    "doctors": [{"name": "Dr. A. Sen", "doctor_name_bn": "সেন"}],
}
_BOOKED = {
    "success": True,
    "confirmation_id": "KCD-20260920-PAR1",
    "doctor_name": "Dr. A. Sen",
    "doctor_name_bn": "সেন",
    "date": "2026-09-20",
    "time_slot": "18:15",
    "notification": {"status": "queued"},
}


def _intent(name: str, **slots) -> dict:
    full = {k: None for k in _SLOT_KEYS}
    full.update(slots)
    return {"intent": name, "slots": full, "direct_reply_bn": None}


# ===========================================================================
# THE QUESTION SET -- put to both channels, word for word
# ===========================================================================
QUESTIONS: tuple[tuple[str, str, dict, str], ...] = (
    (
        "test_rate",
        "লিপিড প্রোফাইলের রেট কত",
        _intent("test_rate", test_name="লিপিড প্রোফাইল"),
        "a price",
    ),
    (
        "test_rate_missing_slot",
        "রেট কত",
        _intent("test_rate"),
        "a price question with no test named",
    ),
    (
        "doctor_availability",
        "ডাক্তার সেন কি আছেন",
        _intent("doctor_availability", doctor_name="সেন", date="2026-09-20"),
        "a doctor's hours",
    ),
    (
        "doctors_by_department",
        "কার্ডিওলজিতে কারা আছেন",
        _intent("doctors_by_department", department="Cardiology", date="2026-09-20"),
        "who sits in a department",
    ),
    (
        "payment",
        "কীভাবে টাকা দেব",
        _intent("payment", test_name="লিপিড প্রোফাইল"),
        "how to pay",
    ),
    (
        "report_collection",
        "রিপোর্ট কবে পাব",
        _intent("report_collection", test_name="লিপিড প্রোফাইল"),
        "when a report is ready",
    ),
    ("smalltalk", "নমস্কার", _intent("smalltalk"), "a greeting"),
    ("unclear", "…", _intent("unclear"), "something we did not understand"),
)


# ===========================================================================
# One agent, two channels, identical inputs
# ===========================================================================
@pytest.fixture
def both(monkeypatch, tmp_path):
    """The voice agent with the clinic and the model faked, plus a message
    channel wired to the same module. Whatever differs between the two runs
    is the channel and nothing else."""
    app = gate_support.load_main_pcm()
    monkeypatch.delenv("VOICE_AGENT_HISTORY_DISCLOSURE", raising=False)
    monkeypatch.setenv("VOICE_AGENT_HISTORY_REQUIRE_PRIVATE_PATH", "0")

    tools = gate_support.FakeTools(
        responses={
            "get_test_rate": _RATE,
            "get_doctor_availability": _AVAILABILITY,
            "get_doctors_by_department": _DEPARTMENT,
            "book_appointment": _BOOKED,
            "begin_verification": {"factor": "dob", "locked": False},
        }
    )
    intents: dict[str, dict] = {}
    spoken: list[str] = []
    real_speak = app._speak

    async def resolve(session, text):
        data = intents[text]
        app._record_intent(session, data, "parity")
        return data

    async def speak(session, text, fallback_reason=None, audit_redact=None):
        if getattr(session, "channel", privacy.CHANNEL_VOICE) != privacy.CHANNEL_VOICE:
            await real_speak(session, text, fallback_reason, audit_redact)  # the real written path
        else:
            spoken.append(text)

    monkeypatch.setattr(app, "_tools", tools)
    monkeypatch.setattr(app, "_resolve_intent", resolve)
    monkeypatch.setattr(app, "_speak", speak)
    monkeypatch.setattr(app, "_conversations", None)
    monkeypatch.setattr(app, "_audit_store", None)

    from agent import conversation_store

    store = conversation_store.ConversationStore(path=str(tmp_path / "parity.db"), key=b"parity")
    config = whatsapp.Config(
        access_token="bench",
        phone_number_id="1234",
        app_secret="parity-secret",
        verify_token="parity",
        api_base="https://graph.test",
        graph_version="v21.0",
        country_code="91",
        timeout_s=2.0,
        reply_expired_template="",
    )
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": f"wamid.{len(sent)}"}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    channel = message_service.MessageChannel(app, store, config, client)

    def by_phone(text: str, intent: dict | None = None) -> list[str]:
        """Ask on the PHONE. -> what the caller hears."""
        if intent is not None:
            intents[text] = intent
        spoken.clear()
        asyncio.run(app.answer_turn(_call_session(app), text, source="parity"))
        return list(spoken)

    fresh = [0]

    def by_message(text: str, intent: dict | None = None, *, sender: str | None = None) -> list[str]:
        """Ask by MESSAGE. -> what the patient reads.

        A FRESH sender each time unless one is named, so that the comparison
        is like for like: on the phone every question here is a new call, and
        a message thread that carried state from the previous question would
        be answered as its continuation, not as a new question."""
        if intent is not None:
            intents[text] = intent
        if sender is None:
            fresh[0] += 1
            sender = f"91900000{fresh[0]:04d}"  # inside the approved fictional range
        msg = whatsapp.Inbound(
            message_id=f"wamid.{uuid.uuid4().hex}",
            sender=sender,
            kind="text",
            text=text,
            sent_at=time.time(),
        )
        return list(asyncio.run(channel.answer(msg)).replies)

    sessions: list = []

    def _call_session(module):
        session = module.CallSession(gate_support.FakeWS())
        sessions.append(session)
        return session

    yield types.SimpleNamespace(
        app=app,
        tools=tools,
        by_phone=by_phone,
        by_message=by_message,
        store=store,
        sent=sent,
        sessions=sessions,
    )
    for session in sessions:
        session.cleanup()
    store.close()


# ===========================================================================
# THE SAME QUESTION SET ON EACH CHANNEL
# ===========================================================================
@pytest.mark.parametrize(("qid", "text", "intent", "about"), QUESTIONS, ids=[q[0] for q in QUESTIONS])
def test_both_channels_give_the_same_answer(both, qid, text, intent, about):
    """The patient must never have to decide which channel to believe."""
    heard = both.by_phone(text, intent)
    read = both.by_message(text, intent)
    assert heard == read, f"{about} differs between the phone line and a message"


def test_the_question_set_covers_every_answering_intent(both):
    """A question set that quietly stopped covering an intent would let that
    intent drift between the channels unnoticed."""
    from agent import llm

    covered = {intent["intent"] for _qid, _text, intent, _about in QUESTIONS}
    # Everything the agent answers, minus the two it may not answer by
    # message at all (declared: private_information).
    answerable = set(llm.VALID_INTENTS) - {"patient_history", "my_bookings", "book_appointment"}
    assert answerable <= covered


def test_a_price_is_quoted_identically_to_the_digit(both):
    """The sharpest form of the story: the number itself."""
    text, intent = QUESTIONS[0][1], QUESTIONS[0][2]
    expected = rate_reply(intent["slots"], _RATE, "bn")
    assert both.by_phone(text, intent) == [expected]
    assert both.by_message(text, intent) == [expected]
    assert "650" in expected


def test_every_channel_reads_its_words_from_the_same_table(both):
    """Each answer equals what reply_templates produces -- neither channel
    composes a sentence of its own."""
    cases = [
        (QUESTIONS[2], doctor_availability_reply(QUESTIONS[2][2]["slots"], _AVAILABILITY, "bn")),
        (QUESTIONS[3], doctors_by_department_reply(QUESTIONS[3][2]["slots"], _DEPARTMENT, "bn")),
        (QUESTIONS[4], payment_reply(QUESTIONS[4][2]["slots"], _RATE, "bn")),
        (QUESTIONS[5], report_collection_reply(QUESTIONS[5][2]["slots"], _RATE, "bn")),
    ]
    for (_qid, text, intent, about), expected in cases:
        assert both.by_phone(text, intent) == [expected], about
        assert both.by_message(text, intent) == [expected], about


def test_a_tool_failure_says_the_same_thing_on_both(both):
    """The failure path is where wording drifts most easily, because nobody
    reads it until something is wrong."""
    from agent.tools_client import ToolCallError

    both.tools.raises["get_test_rate"] = ToolCallError("clinic-api down")
    text, intent = QUESTIONS[0][1], QUESTIONS[0][2]
    heard, read = both.by_phone(text, intent), both.by_message(text, intent)
    assert heard == read == [i18n.t("bn", "generic.tool_failure")]


# ===========================================================================
# THE DECLARED DIVERGENCES -- allowed, and checked to behave as described
# ===========================================================================
def test_private_information_is_the_declared_difference(both):
    """Declared: a message may not carry history or bookings."""
    text = "আমার কী বুকিং আছে"
    intent = _intent("my_bookings", phone=PHONE)
    heard = both.by_phone(text, intent)
    read = both.by_message(text, intent)

    assert heard != read  # they differ...
    assert read == [i18n.t("bn", "channel.private_by_message")]  # ...exactly here
    assert gate_support.mentions_counter(read[0])
    assert "private_information" in answer_contract.DIVERGENCE_IDS


def test_the_number_already_known_is_the_declared_difference(both):
    """Declared: a patient writing from their number is not asked for it.
    One question fewer -- and the SAME booking at the end of it."""
    text = "সেনের কাছে ২০ তারিখ ১৮:১৫ বুক করুন"
    intent = _intent("book_appointment", doctor_name="সেন", date="2026-09-20", time_slot="18:15")

    assert both.by_phone(text, intent) == [missing_slot_prompt("book_appointment", "patient_name")]
    assert both.by_message(text, intent, sender=SENDER) == [
        missing_slot_prompt("book_appointment", "patient_name")
    ]

    # The phone line then asks for the number; the message channel does not.
    assert both.by_message("ইতি সেন", sender=SENDER) == [
        __import__("agent.reply_templates", fromlist=["booking_reply"]).booking_reply(
            {"doctor_name": "সেন"}, _BOOKED, "bn"
        )
    ]
    booked = [c for c in both.tools.calls if c[0] == "book_appointment"]
    assert booked and booked[-1][1] == ("সেন", "2026-09-20", "18:15", "ইতি সেন", PHONE)
    assert "number_already_known" in answer_contract.DIVERGENCE_IDS


# ===========================================================================
# A DIVERGENCE THAT IS NOT DECLARED FAILS THE BUILD
# ===========================================================================
def _main_sources() -> dict[str, str]:
    return {
        name: (gate_support.ROOT / name).read_text(encoding="utf-8") for name in ("main.py", "main_pcm.py")
    }


def test_nothing_branches_on_the_channel_without_being_declared():
    """THE GUARD THIS STORY EXISTS FOR. Every place that asks which channel
    this is must be one of the declared divergences -- so a new one fails
    here until somebody writes down what it is and why."""
    for name, src in _main_sources().items():
        tests = src.count('getattr(session, "channel"')
        assert tests == answer_contract.CHANNEL_TESTS_IN_MAIN, (
            f"{name} asks which channel it is {tests} times, but "
            f"{answer_contract.CHANNEL_TESTS_IN_MAIN} divergences are declared in "
            f"agent/answer_contract.py. Declare it (with the reason) or remove it.\n\n"
            + answer_contract.describe()
        )


def test_every_declared_divergence_is_actually_in_the_code():
    """The other direction: a declaration left behind after the code went is
    a lie in the contract."""
    for name, src in _main_sources().items():
        for divergence in answer_contract.DIVERGENCES:
            assert divergence.marker in src, f"{divergence.id} is declared but not in {name}"


def test_both_channels_enter_through_the_one_answer_service():
    for name, src in _main_sources().items():
        assert f"async def {answer_contract.SHARED_ANSWER_ENTRY}(" in src, name
        assert 'await answer_turn(session, text, source="keypad")' in src, name
        assert 'await answer_turn(session, text, source="message")' in src, name


def test_only_the_declared_sentences_belong_to_a_channel():
    """Every other sentence must read the same whichever channel asks."""
    keys = {key for key, _code, _text in i18n.all_strings() if key.startswith("channel.")}
    assert keys == answer_contract.CHANNEL_SENTENCE_KEYS


def test_the_contract_reads_as_a_list_a_reviewer_can_check():
    described = answer_contract.describe()
    for divergence in answer_contract.DIVERGENCES:
        assert divergence.id in described and divergence.why[:30] in described
