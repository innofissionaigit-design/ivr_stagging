"""Tests for "Who have I reached, and is it automated?".

Author: Chakravardhan
Story:  "As a caller, I want to know immediately who I have reached and that
         this is automated, so that I can decide how to use it."
Criteria: The greeting names the hospital and discloses the automated system
          in the caller language, and a closing restates what was done and
          what happens next. Both are pre-warmed so neither costs synthesis
          latency. The wording is reviewed by the clinical and legal leads.

WHAT THESE COVER
----------------
  * THE GREETING -- names the hospital and says it is automated, in bn/hi/en,
    in the call's language; it is what a live call actually speaks first.
  * THE CLOSING -- restates what the clinic API confirmed (never more) and
    what happens next; an SMS is promised only when one is really queued.
  * PRE-WARMED -- every greeting and closing sentence, in every language the
    pod serves, is synthesized at startup, so no line a call can speak misses
    the cache; a failing TTS does not stop startup.
  * REVIEWED -- the approval is for exact words: unsigned or changed wording
    is reported as not approved.
  * THE CALL -- the closing on end_call and on the idle timeout, spoken once.

    python -m pytest tests/test_call_script.py -v
"""

from __future__ import annotations

import asyncio
import dataclasses
import itertools
import json
import types

import gate_support
import httpx
import pytest
from test_no_smartphone import _offences

from agent import call_script as cs
from agent import language as lang_mod

app = gate_support.load_main_pcm()
LANGS = ("bn", "hi", "en")

DISCLOSURE = {
    "bn": ("ভয়েস এআই বট", "মানুষ নই"),
    "hi": ("वॉइस एआई बॉट", "इंसान नहीं"),
    "en": ("voice AI bot", "not a person"),
}


# ===========================================================================
# THE GREETING
# ===========================================================================
@pytest.mark.parametrize("code", LANGS)
def test_the_greeting_names_the_hospital_and_discloses_automation(code):
    text = cs.GREETING[code]
    assert cs.HOSPITAL_NAME[code] in text
    for words in DISCLOSURE[code]:
        assert words in text


@pytest.mark.parametrize("code", LANGS)
def test_the_greeting_is_spoken_in_the_call_language(code, monkeypatch):
    gate_support.trilingual(monkeypatch)
    assert cs.greeting(code) == cs.GREETING[code]


def test_a_language_the_pod_cannot_serve_falls_back_to_the_default():
    assert cs.greeting("fr") == cs.GREETING[lang_mod.default_lang()]


def test_the_old_greeting_did_not_disclose_automation():
    old = "নমস্কার, কলকাতা কেয়ার ডায়াগনস্টিকসে স্বাগতম। কীভাবে সাহায্য করতে পারি?"
    assert not any(word in old for word in DISCLOSURE["bn"])


def test_a_live_call_opens_with_the_disclosed_greeting(monkeypatch):
    spoken = gate_support.SpeechLog()
    monkeypatch.setattr(app, "_speak", spoken)
    monkeypatch.setattr(app, "_audit_store", None)
    monkeypatch.setattr(app, "_conversations", None)

    class HangsUp(gate_support.FakeWS):
        async def accept(self):
            pass

        async def receive(self):
            return {"type": "websocket.disconnect"}

    asyncio.run(asyncio.wait_for(app.ws_audio(HangsUp()), 10))
    assert spoken.texts[0] == cs.greeting(lang_mod.default_lang())


# ===========================================================================
# THE CLOSING
# ===========================================================================
def keys(**outcomes):
    pending = outcomes.pop("pending", None)
    return cs.closing_keys(cs.CallOutcomes(**outcomes), pending)


def test_a_call_that_did_nothing_says_so_and_what_to_do_next():
    assert keys() == [cs.DONE_NOTHING, cs.NEXT_CALL_AGAIN, cs.GOODBYE]


def test_a_call_that_only_answered_questions_says_nothing_was_booked():
    assert keys(answered=True) == [cs.DONE_ANSWERED, cs.NEXT_CALL_AGAIN, cs.GOODBYE]
    assert "কোনো বুকিং করা হয়নি" in cs.CLOSING[cs.DONE_ANSWERED]["bn"]


def test_a_booking_with_a_queued_message_promises_the_message():
    assert keys(booked=True, answered=True, message_on_its_way=True) == [
        cs.DONE_BOOKED,
        cs.NEXT_SMS,
        cs.GOODBYE,
    ]


def test_a_booking_with_no_message_on_its_way_never_promises_one():
    assert keys(booked=True) == [cs.DONE_BOOKED, cs.NEXT_COUNTER, cs.GOODBYE]


def test_an_unfinished_booking_is_restated_as_not_made():
    assert keys(answered=True, pending={"awaiting": "time_slot"}) == [
        cs.DONE_BOOKING_UNFINISHED,
        cs.NEXT_CALL_AGAIN,
        cs.GOODBYE,
    ]


def test_several_things_done_are_each_restated():
    assert keys(booked=True, cancelled=True, records_shared=True)[:3] == [
        cs.DONE_BOOKED,
        cs.DONE_CANCELLED,
        cs.DONE_RECORDS,
    ]


def test_a_verification_in_progress_is_not_called_a_booking():
    assert keys(pending={"awaiting": "history_verify"})[0] == cs.DONE_NOTHING


def test_the_idle_path_closing_has_no_second_goodbye():
    lines = cs.closing(cs.CallOutcomes(), None, "bn", include_goodbye=False)
    assert cs.CLOSING[cs.GOODBYE]["bn"] not in lines and len(lines) == 2


@pytest.mark.parametrize(
    ("action", "result", "field"),
    [
        ("book_appointment", {"success": True, "notification": {"status": "queued"}}, "booked"),
        ("reschedule_appointment", {"success": True}, "rescheduled"),
        ("cancel_appointment", {"success": True}, "cancelled"),
        ("read_history", {"found": True}, "records_shared"),
        ("get_test_rate", {"found": False}, "answered"),  # "not found" is still an answer
        ("get_doctor_availability", {"found": True}, "answered"),
    ],
)
def test_outcomes_come_from_the_clinic_apis_own_answers(action, result, field):
    outcomes = cs.CallOutcomes()
    outcomes.note(action, result)
    assert getattr(outcomes, field) is True


def test_a_failed_booking_is_not_restated_as_made():
    outcomes = cs.CallOutcomes()
    outcomes.note("book_appointment", {"success": False, "reason": "slot_taken"})
    outcomes.note("book_appointment", "not a dict")
    assert outcomes == cs.CallOutcomes()


def test_only_a_queued_message_earns_the_promise():
    outcomes = cs.CallOutcomes()
    outcomes.note("book_appointment", {"success": True, "notification": {"status": "skipped"}})
    assert outcomes.booked and not outcomes.message_on_its_way


def test_the_calls_audit_remembers_what_the_api_answered_and_still_records_it():
    audit = cs.ObservedCallAudit(None, "call-script-test")

    async def book():
        return {"success": True, "notification": {"status": "queued"}}

    async def broken():
        raise httpx.ConnectError("clinic-api down")

    result = asyncio.run(audit.api_call("book_appointment", {"phone": "9000000301"}, book))
    assert result["success"] and audit.outcomes.booked and audit.outcomes.message_on_its_way
    assert audit.api_calls == 1

    with pytest.raises(httpx.ConnectError):
        asyncio.run(audit.api_call("cancel_appointment", {}, broken))
    assert not audit.outcomes.cancelled and audit.api_failures == 1


# ===========================================================================
# EVERY LINE, EVERY LANGUAGE, NO SMARTPHONE
# ===========================================================================
def test_every_closing_sentence_exists_in_every_language():
    for key, entry in cs.CLOSING.items():
        assert set(entry) == set(LANGS), key
        assert all(text.strip() for text in entry.values()), key


@pytest.mark.parametrize("code", LANGS)
def test_nothing_the_script_says_needs_a_smartphone(code):
    for line in cs.lines_for(code):
        assert not _offences(line), line


def test_the_goodbye_names_the_hospital_in_every_language():
    for code in LANGS:
        assert cs.HOSPITAL_NAME[code] in cs.CLOSING[cs.GOODBYE][code]


# ===========================================================================
# PRE-WARMED
# ===========================================================================
class RecordingTTS:
    def __init__(self, fail_on: str | None = None):
        self.cached: set[tuple[str, str]] = set()
        self.fail_on = fail_on

    async def synthesize(self, text, lang=None):
        if self.fail_on and lang == self.fail_on:
            raise httpx.ConnectError("tts down")
        self.cached.add((lang, text))
        return b"RIFF"


def _every_possible_closing(code):
    flags = ("booked", "rescheduled", "cancelled", "records_shared", "answered", "message_on_its_way")
    for values in itertools.product((False, True), repeat=len(flags)):
        for pending in (None, {"awaiting": "date"}):
            outcomes = cs.CallOutcomes(**dict(zip(flags, values, strict=True)))
            yield from cs.closing(outcomes, pending, code)
            yield from cs.closing(outcomes, pending, code, include_goodbye=False)


def test_every_line_a_call_can_speak_is_prewarmed_in_every_language(monkeypatch):
    gate_support.trilingual(monkeypatch)
    tts = RecordingTTS()
    warm = asyncio.run(cs.prewarm(tts))
    assert warm == {"bn": True, "hi": True, "en": True}
    for code in LANGS:
        assert (code, cs.greeting(code)) in tts.cached
        for line in _every_possible_closing(code):
            assert (code, line) in tts.cached, line  # no closing can miss the cache


def test_a_bengali_only_pod_prewarms_bengali(monkeypatch):
    for var in ("VOICE_AGENT_LANGUAGES", "VOICE_AGENT_NEMO_FILE_HI", "VOICE_AGENT_NEMO_FILE_EN"):
        monkeypatch.delenv(var, raising=False)
    tts = RecordingTTS()
    assert asyncio.run(cs.prewarm(tts)) == {"bn": True}
    assert {lang for lang, _ in tts.cached} == {"bn"}


def test_a_failing_tts_does_not_stop_startup_and_is_reported(monkeypatch):
    gate_support.trilingual(monkeypatch)
    warm = asyncio.run(cs.prewarm(RecordingTTS(fail_on="hi")))
    assert warm == {"bn": True, "hi": False, "en": True}
    assert cs.health()["prewarmed"]["hi"] is False


def test_startup_prewarms_the_script():
    source = (gate_support.ROOT / "main.py").read_text(encoding="utf-8")
    assert "await call_script.prewarm(_tts)" in source


# ===========================================================================
# REVIEWED BY THE CLINICAL AND LEGAL LEADS
# ===========================================================================
def test_unsigned_wording_is_not_approved():
    status = cs.review_status()
    assert status["approved"] is False
    assert cs.health()["review"]["approved"] is False


def test_wording_signed_by_both_leads_is_approved():
    signed = dataclasses.replace(
        cs.REVIEW,
        wording_sha256=cs.wording_fingerprint(),
        clinical_lead="Clinical Lead",
        legal_lead="Legal Lead",
        approved_on="2026-09-20",
    )
    assert cs.review_status(signed)["approved"] is True
    assert cs.review_status(dataclasses.replace(signed, legal_lead=None))["approved"] is False


def test_changing_one_word_voids_the_approval(monkeypatch):
    signed = dataclasses.replace(
        cs.REVIEW,
        wording_sha256=cs.wording_fingerprint(),
        clinical_lead="Clinical Lead",
        legal_lead="Legal Lead",
        approved_on="2026-09-20",
    )
    changed = json.loads(json.dumps(cs.GREETING))
    changed["en"] = changed["en"].replace("not a person", "a helper")
    monkeypatch.setattr(cs, "GREETING", changed)
    status = cs.review_status(signed)
    assert status["approved"] is False and status["wording_matches_approval"] is False


# ===========================================================================
# THE CALL -- end_call and the idle timeout
# ===========================================================================
@pytest.fixture
def call(monkeypatch):
    spoken = gate_support.SpeechLog()
    monkeypatch.setattr(app, "_speak", spoken)
    monkeypatch.setattr(app, "_audit_store", None)
    session = app.CallSession(gate_support.FakeWS())
    yield types.SimpleNamespace(session=session, spoken=spoken)
    session.cleanup()


def test_the_closing_restates_a_booking_from_the_api_and_is_spoken_once(call):
    call.session.audit.outcomes.note(
        "book_appointment", {"success": True, "notification": {"status": "queued"}}
    )
    asyncio.run(app._speak_closing(call.session))
    asyncio.run(app._speak_closing(call.session))
    code = call.session.lang
    assert call.spoken.texts == [
        cs.CLOSING[cs.DONE_BOOKED][code],
        cs.CLOSING[cs.NEXT_SMS][code],
        cs.CLOSING[cs.GOODBYE][code],
    ]


def test_the_closing_follows_the_callers_language(call, monkeypatch):
    gate_support.trilingual(monkeypatch)
    call.session.lang = "hi"
    asyncio.run(app._speak_closing(call.session))
    assert call.spoken.texts[-1] == cs.CLOSING[cs.GOODBYE]["hi"]


def test_end_call_speaks_the_closing_then_closes_the_call(call):
    closed = []

    async def close():
        closed.append(True)

    call.session.ws.close = close
    asyncio.run(app._end_call_with_closing(call.session))
    assert call.spoken.texts[-1] == cs.CLOSING[cs.GOODBYE][call.session.lang]
    assert call.session.end_reason == cs.END_CALLER_ENDED
    assert closed == [True]


def test_the_end_call_control_message_is_understood(call, monkeypatch):
    started = []

    async def fake_end(session):
        started.append(session)

    monkeypatch.setattr(app, "_end_call_with_closing", fake_end)

    async def go():
        await app._handle_control(call.session, json.dumps({"type": "end_call"}))
        await asyncio.sleep(0)

    asyncio.run(go())
    assert started == [call.session]


def test_waiting_for_playback_never_outlasts_its_cap(call):
    call.session.hold_gate_for(600.0)  # a client that never reports playback done
    asyncio.run(asyncio.wait_for(app._wait_for_playback(call.session, cap_s=0.2), 5))
