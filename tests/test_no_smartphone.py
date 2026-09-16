"""Tests for "Every flow completes without a smartphone".

WHAT THIS FILE IS FOR
---------------------
The story's acceptance criterion is a claim about EVERY flow, present and
future:

    Every flow has a non-link completion path including payment and report
    collection, even where that means attending the counter. No flow
    dead-ends on a smartphone requirement.

A criterion phrased over "every flow" cannot be met by checking the flows
that exist today -- it is broken by the next one somebody adds. So the
central test here does not check a list of flows. It walks EVERY
caller-facing string this system can emit, in every language, and asserts
that none of them requires a smartphone. A sentence added next month by
somebody who never read this file is caught by the same assertion.

The two directions it guards:

  * NEGATIVE -- no string may tell a caller to tap a link, scan a code,
    open an app, visit a site or check an email;
  * POSITIVE -- the two flows the criterion names by hand, payment and
    report collection, must always produce a usable completion path, and
    must keep producing one when clinic-api is unreachable.

WHAT IT CANNOT CHECK
--------------------
Whether the counter instructions are TRUE -- the opening hours, whether
card is really accepted, whether reception really will read a report over
the phone. Those are clinic facts and need a person from the clinic. The
test asserts the shape of the promise, not its accuracy.

    python -m pytest tests/test_no_smartphone.py -v
"""
from __future__ import annotations

import os
import re
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "clinic-api"))

from agent import i18n                                   # noqa: E402
from agent import language as lang_mod                    # noqa: E402
from agent.reply_templates import (                       # noqa: E402
    payment_reply, report_collection_reply, counter_fallback,
    booking_reply, doctor_availability_reply,
    doctors_by_department_reply, cancel_reply, reschedule_reply,
    missing_slot_prompt,
)
# Aliased deliberately: pytest collects any imported callable whose name
# starts with "test_", so importing test_rate_reply under its own name
# turns a reply builder into a broken test case demanding a `slots`
# fixture.
from agent.reply_templates import test_rate_reply as rate_reply   # noqa: E402


# ---------------------------------------------------------------------------
# What "requires a smartphone" looks like in text
# ---------------------------------------------------------------------------
# Each entry is (compiled pattern, what it would do to the caller). The
# reason strings are in the failure message on purpose: someone who trips
# this test six months from now needs to know WHY the sentence is banned,
# not just that a regex matched.
_BANNED = [
    (re.compile(r"https?://", re.I), "a URL the caller must open"),
    (re.compile(r"\bwww\.", re.I), "a website address"),
    (re.compile(r"\.(com|in|org|net)\b", re.I), "a domain name"),
    (re.compile(r"\blinks?\b", re.I), "an English 'link'"),
    (re.compile(r"লিঙ্ক|লিংক"), "a Bengali 'link'"),
    (re.compile(r"लिंक|लिन्क"), "a Hindi 'link'"),
    (re.compile(r"\bQR\b", re.I), "a QR code to scan"),
    (re.compile(r"\bscan\b", re.I), "something to scan"),
    (re.compile(r"স্ক্যান|स्कैन"), "something to scan"),
    # "app" only as a standalone word -- "appointment", "অ্যাপয়েন্টমেন্ট"
    # and "अपॉइंटमेंट" all legitimately start with those letters.
    (re.compile(r"\bapps?\b", re.I), "a mobile app"),
    (re.compile(r"অ্যাপ(?!য়েন্ট)"), "a mobile app"),
    (re.compile(r"ऐप|एप्प"), "a mobile app"),
    (re.compile(r"\bdownload\b", re.I), "something to download"),
    (re.compile(r"ডাউনলোড|डाउनलोड"), "something to download"),
    (re.compile(r"\b(portal|website)\b", re.I), "a site to visit"),
    (re.compile(r"পোর্টাল|पोर्टल"), "a portal to visit"),
    (re.compile(r"\bplay ?store\b|\bapp ?store\b", re.I), "an app store"),
    (re.compile(r"\bclick\b", re.I), "something to click"),
    (re.compile(r"ক্লিক|क्लिक"), "something to click"),
    # Online payment. Every method here needs a smartphone and data, so
    # naming one -- even as one option among several -- tells a caller on
    # a basic handset that the real route is one they cannot use. Payment
    # is at the counter, in cash or by card, and nowhere else.
    (re.compile(r"\bUPI\b", re.I), "an online payment (UPI)"),
    (re.compile(r"ইউপিআই|यूपीआई"), "an online payment (UPI)"),
    (re.compile(r"\bonline\b", re.I), "something to do online"),
    (re.compile(r"অনলাইন|ऑनलाइन"), "something to do online"),
    (re.compile(r"\b(g ?pay|google ?pay|phone ?pe|paytm)\b", re.I), "a payment app"),
    (re.compile(r"\bnet ?banking\b|\binternet banking\b", re.I), "online banking"),
    (re.compile(r"নেট ?ব্যাংকিং|नेट ?बैंकिंग"), "online banking"),
]


def _offences(text: str) -> list[str]:
    return [why for pattern, why in _BANNED if pattern.search(text or "")]


# ---------------------------------------------------------------------------
# THE CENTRAL TEST
# ---------------------------------------------------------------------------
def test_no_caller_facing_string_requires_a_smartphone():
    """Walks every sentence in every language.

    This is the assertion that makes the criterion hold for flows that do
    not exist yet: a new string added to agent/i18n.py is covered the
    moment it is added, without anyone remembering to extend this file.
    """
    bad = []
    for key, code, text in i18n.all_strings():
        for why in _offences(text):
            bad.append(f"{key} [{code}]: {why} -> {text!r}")
    assert not bad, (
        "caller-facing text requires a smartphone:\n  " + "\n  ".join(bad)
        + "\n\nEvery flow must be completable by a caller holding a feature "
          "phone. Offer the counter instead."
    )


def test_the_sms_templates_do_not_require_a_smartphone_either():
    """The written confirmation is read on the handset that received it.

    An SMS is fine -- a feature phone receives SMS. A LINK inside that SMS
    is not, and it is the most natural thing in the world for somebody to
    add later ("just add a link to the booking page"). Same guard,
    different file.
    """
    import message_templates as mt
    bad = []
    for tpl in mt.TEMPLATES.values():
        for why in _offences(tpl.body):
            bad.append(f"{tpl.event}: {why} -> {tpl.body!r}")
    assert not bad, "SMS template requires a smartphone:\n  " + "\n  ".join(bad)


@pytest.mark.parametrize("code", lang_mod.ALL_LANGS)
def test_every_reply_function_is_smartphone_free_in_every_language(code):
    """Belt and braces over i18n: exercises the FUNCTIONS, so a sentence
    assembled at runtime from several keys plus f-string glue is checked as
    the caller would actually hear it."""
    slots = {"test_name": "CBC", "doctor_name": "Sen", "department": "Cardiology"}
    ok = {"found": True, "success": True, "rate_inr": 650, "sample_type": "Blood",
          "report_time_hours": 24, "doctor_name": "Dr. A. Sen", "doctor_name_bn": "সেন",
          "date": "2026-09-14", "time_slot": "18:15", "available": True,
          "chamber_hours": "18:00-20:00", "confirmation_id": "KCD-20260914-0031",
          "department": "Cardiology", "doctors": [{"name": "Dr. A. Sen",
                                                   "doctor_name_bn": "সেন"}],
          "notification": {"status": "queued"}}
    fail = {"found": False, "success": False, "reason": "slot_taken",
            "alternative_slots": ["18:30"]}

    produced = []
    for result in (ok, fail, {}):
        produced += [
            payment_reply(slots, result, code),
            report_collection_reply(slots, result, code),
            rate_reply(slots, result, code),
            doctor_availability_reply(slots, result, code),
            doctors_by_department_reply(slots, result, code),
            booking_reply(slots, result, code),
            reschedule_reply(slots, result, code),
            cancel_reply(slots, result, code),
        ]
    produced.append(counter_fallback(code))
    produced.append(missing_slot_prompt("book_appointment", "phone", code))

    bad = [f"{why} -> {text!r}" for text in produced for why in _offences(text)]
    assert not bad, f"[{code}] " + "\n  ".join(bad)


# ---------------------------------------------------------------------------
# POSITIVE: the two flows the criterion names
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("code", lang_mod.ALL_LANGS)
def test_payment_always_offers_a_counter_path(code):
    """The criterion says payment must have a non-link completion path
    "even where that means attending the counter". This asserts the counter
    is actually named, not merely that no link was mentioned -- saying
    nothing about how to pay is its own dead end."""
    reply = payment_reply({}, {}, code)
    counter_words = ("কাউন্টার", "काउंटर", "counter")
    assert any(w in reply for w in counter_words), reply
    assert len(reply) > 40, "a payment answer this short cannot be actionable"


@pytest.mark.parametrize("code", lang_mod.ALL_LANGS)
def test_payment_is_cash_or_card_at_the_counter_only(code, monkeypatch):
    """A caller with no smartphone -- a basic handset, or no phone of their
    own -- must be told a way to pay they can actually use, and must not be
    offered one they cannot.

    Cash is the method every caller can use, so it must be named. And the
    reply must say plainly that nothing is paid over the phone, so a caller
    on a feature phone is not left wondering whether they are expected to
    do something with it."""
    # Enable every language, or resolve() quietly answers hi/en in Bengali
    # and this would test the Bengali sentence three times. Same fakes as
    # tests/test_multilingual.py's `trilingual` fixture.
    monkeypatch.setenv("VOICE_AGENT_LANGUAGES", "bn,hi,en")
    monkeypatch.setenv("VOICE_AGENT_NEMO_FILE_HI", "/fake/hi.nemo")
    monkeypatch.setenv("VOICE_AGENT_NEMO_FILE_EN", "/fake/en.nemo")
    reply = payment_reply({"test_name": "CBC"},
                          {"found": True, "rate_inr": 650}, code)
    cash_words = ("নগদ", "नकद", "cash")
    assert any(w in reply for w in cash_words), reply
    by_phone = {"bn": "ফোনে কোনো টাকা দিতে হবে না",
                "hi": "फ़ोन पर कोई भुगतान नहीं",
                "en": "do not need to pay anything by phone"}
    assert by_phone[code] in reply, reply
    assert not _offences(reply), _offences(reply)


@pytest.mark.parametrize("code", lang_mod.ALL_LANGS)
def test_report_collection_always_offers_a_counter_path(code):
    reply = report_collection_reply({}, {}, code)
    counter_words = ("কাউন্টার", "काउंटर", "counter")
    assert any(w in reply for w in counter_words), reply


def test_payment_answers_even_when_the_clinic_api_is_down():
    """A tool failure must not turn into a dead end.

    HOW you pay is clinic policy, not a database row. When clinic-api is
    unreachable the caller loses the amount and keeps the instructions,
    which is the difference between a degraded answer and no answer.
    """
    reply = payment_reply({"test_name": "CBC"}, {}, "bn")
    assert "কাউন্টার" in reply
    assert "650" not in reply          # no amount was available to quote


def test_report_collection_answers_without_a_named_test():
    """A caller who says only "রিপোর্ট কবে পাব?" named no test, so there
    are no hours to quote. They must still be told how to collect it."""
    reply = report_collection_reply({}, {}, "bn")
    assert "কাউন্টার" in reply
    assert "ঘণ্টা" not in reply        # no hours claimed that we do not have


def test_report_collection_does_not_demand_the_reference_number():
    """The counter path must work for a caller who lost the number.

    Requiring it would rebuild, at the counter, exactly the memory burden
    the written-confirmation story removed -- and would dead-end anyone
    whose SMS never arrived.
    """
    reply = report_collection_reply({}, {"report_time_hours": 24}, "bn")
    assert "নাম" in reply and "ফোন" in reply


def test_a_booking_with_no_message_still_tells_the_caller_how_to_be_found():
    """The no-smartphone rule reaches into the booking flow too.

    When no SMS is going (no gateway, or a failure), the caller used to be
    left holding only a 17-character reference read out once. That is a
    flow that completes only for somebody who can write it down. The
    fallback clause gives them a path that needs neither a phone nor a
    pen.
    """
    result = {"success": True, "date": "2026-09-14", "time_slot": "18:15",
              "confirmation_id": "KCD-20260914-0031", "doctor_name_bn": "সেন",
              "notification": {"status": "skipped"}}
    reply = booking_reply({}, result, "bn")
    assert "রিসেপশনে" in reply
    assert "নাম" in reply and "ফোন" in reply
    # ...and still no SMS is promised, which the earlier story established.
    assert "পাঠানো হচ্ছে" not in reply


def test_counter_fallback_is_usable_with_and_without_hours():
    for code in lang_mod.ALL_LANGS:
        assert counter_fallback(code)
        with_hours = counter_fallback(code, hours="9am-8pm")
        assert "9am-8pm" in with_hours
        assert len(with_hours) > len(counter_fallback(code))
