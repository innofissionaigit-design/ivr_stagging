"""Tests for "A single patient timeline the agent can read".

Author: Chakravardhan
Story:  "As a patient, I want the agent to already know what I have booked
         here, so that I am not made to recite my own history to the
         hospital that holds it."

WHAT THESE COVER
----------------
  * ONE RECORD -- clinic-api joins a patient's bookings and tests into one
    timeline, oldest first, and lists the bookings still ahead, soonest
    first. Cancelled and past bookings are history, never "upcoming".
  * THE WHOLE RECORD -- a booking made with the number written differently
    ("+91 ...", "91...") is still the patient's; another patient's booking
    never is.
  * NOT RECITED -- the agent reads the timeline once per verified call and
    answers "what have I booked" from it; a verified caller who books is not
    asked for the name and number the record already holds.
  * NOT LOOSENED -- the timeline opens with the same verification token and
    the same private-room check as the history; nothing is fetched before
    verification, nothing is spoken on a speakerphone, no confirmation number
    is read out, and the call audit keeps counts, never contents.

Everything that reaches clinic-api's database goes through the real
endpoints. The last test drives the agent's real ClinicToolsClient against
the real clinic-api in-process, so the two halves are checked together.

    python -m pytest tests/test_patient_timeline.py -v
"""

from __future__ import annotations

import asyncio
import datetime
import importlib
import importlib.util
import os
import sys
import tempfile
import types

import gate_support
import httpx
import pytest
from fastapi.testclient import TestClient

from agent import i18n, llm
from agent import language as lang_mod
from agent.echo_guard import PATH_SPEAKERPHONE
from agent.reply_templates import (
    BOOKINGS_SPOKEN_LIMIT,
    PURPOSE_BOOKINGS,
    bookings_reply,
    verification_prompt,
)
from agent.tools_client import ClinicToolsClient, _history_summary

_CLINIC = gate_support.ROOT / "clinic-api"

# clinic-api's db.py reads its path at import. If another test module has
# already chosen one, share it -- every fixture here clears the rows it uses.
os.environ.setdefault("CLINIC_DB_PATH", os.path.join(tempfile.mkdtemp(prefix="clinic-tl-"), "clinic.db"))
os.environ.setdefault("VOICE_AGENT_PROVIDER", "vast")

TODAY = datetime.date(2026, 9, 11)
PHONE = "9000000101"
OTHER_PHONE = "9000000102"
DOB = "1990-05-14"
CID_PREFIX = "KCD-TLN-"

_CLINIC_APP = None


def _clinic_module(name: str):
    if str(_CLINIC) not in sys.path:
        sys.path.insert(0, str(_CLINIC))
    if "db" not in sys.modules:
        os.environ.pop("DATABASE_URL", None)
    return importlib.import_module(name)


def _clinic_app():
    """clinic-api/main.py under its own module name -- there are two main.py
    files in this repo (see the same note in tests/test_history_verification.py)."""
    global _CLINIC_APP
    if _CLINIC_APP is None:
        _clinic_module("db")
        spec = importlib.util.spec_from_file_location("clinic_api_main_timeline", _CLINIC / "main.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules["clinic_api_main_timeline"] = module
        spec.loader.exec_module(module)
        _CLINIC_APP = module
    return _CLINIC_APP


# ===========================================================================
# clinic-api: the timeline itself, through the real endpoints
# ===========================================================================
@pytest.fixture()
def clinic(monkeypatch):
    """One verifiable patient with five bookings made from their number --
    past, cancelled, two upcoming written two different ways, one written
    plainly -- a test on record, and a second patient whose booking must
    never leak into the first one's timeline."""
    app_module = _clinic_app()
    db_mod = _clinic_module("db")
    models = _clinic_module("models")
    history_service = _clinic_module("history_service")
    monkeypatch.setattr(history_service, "_today", lambda: TODAY)

    with TestClient(app_module.app) as client:
        db = db_mod.SessionLocal()
        try:
            db.query(models.DisclosureAudit).delete()
            db.query(models.TestRecord).delete()
            db.query(models.Patient).delete()
            db.query(models.Appointment).filter(
                models.Appointment.confirmation_id.like(f"{CID_PREFIX}%")
            ).delete(synchronize_session=False)
            db.commit()

            now = datetime.datetime.now()
            patient = models.Patient(
                phone=PHONE, full_name="Iti Sen", date_of_birth=DOB, failed_attempts=0, created_at=now
            )
            db.add(patient)
            db.add(
                models.Patient(
                    phone=OTHER_PHONE,
                    full_name="Tapan Nandi",
                    date_of_birth=DOB,
                    failed_attempts=0,
                    created_at=now,
                )
            )
            db.commit()

            sen = db.query(models.Doctor).filter_by(name="Dr. A. Sen").one()
            ghosh = db.query(models.Doctor).filter_by(name="Dr. P. Ghosh").one()

            def book(cid, doctor, date, slot, phone, status=models.APPT_BOOKED):
                db.add(
                    models.Appointment(
                        confirmation_id=CID_PREFIX + cid,
                        doctor_id=doctor.id,
                        date=date,
                        time_slot=slot,
                        patient_name="Iti Sen",
                        phone=phone,
                        created_at=now,
                        status=status,
                        slot_lock=CID_PREFIX + cid
                        if status == models.APPT_CANCELLED
                        else models.SLOT_LOCK_ACTIVE,
                    )
                )

            book("PAST", sen, "2026-08-20", "07:05", PHONE)
            book("SOON", sen, "2026-09-14", "07:10", "91" + PHONE, status=models.APPT_RESCHEDULED)
            book("GONE", sen, "2026-09-15", "07:15", PHONE, status=models.APPT_CANCELLED)
            book("LATER", ghosh, "2026-09-20", "07:20", "+91 90000 00101")
            book("TODAY", ghosh, "2026-09-11", "07:25", PHONE)
            db.add(
                models.Appointment(
                    confirmation_id=CID_PREFIX + "OTHER",
                    doctor_id=sen.id,
                    date="2026-09-16",
                    time_slot="07:30",
                    patient_name="Tapan Nandi",
                    phone=OTHER_PHONE,
                    created_at=now,
                    status=models.APPT_BOOKED,
                    slot_lock=models.SLOT_LOCK_ACTIVE,
                )
            )
            db.add(
                models.TestRecord(
                    patient_id=patient.id,
                    test_name="CBC",
                    test_name_bn="সিবিসি",
                    taken_on="2026-08-01",
                    report_ready=True,
                    created_at=now,
                )
            )
            db.commit()
        finally:
            db.close()

        history_service._TOKENS.clear()
        yield types.SimpleNamespace(client=client, models=models, db=db_mod, history_service=history_service)


def _read_timeline(clinic) -> dict:
    verified = clinic.client.post(
        "/api/v1/history/verify", json={"phone": PHONE, "factor": "dob", "answer": DOB, "call_id": "tl-1"}
    ).json()
    assert verified["verified"] is True
    return clinic.client.post(
        "/api/v1/history/read", json={"token": verified["token"], "call_id": "tl-1"}
    ).json()


def test_the_timeline_opens_only_with_a_verification_token(clinic):
    """The timeline is part of the record, so it inherits the record's lock.
    No token, no timeline -- not a partial one, not a count."""
    r = clinic.client.post("/api/v1/history/read", json={"token": "", "call_id": "tl-1"}).json()
    assert r == {"found": False, "reason": "not_verified"}


def test_bookings_and_tests_are_one_timeline_oldest_first(clinic):
    r = _read_timeline(clinic)
    line = r["timeline"]
    assert [(e["kind"], e["date"]) for e in line] == [
        ("test", "2026-08-01"),
        ("appointment", "2026-08-20"),
        ("appointment", "2026-09-11"),
        ("appointment", "2026-09-14"),
        ("appointment", "2026-09-15"),
        ("appointment", "2026-09-20"),
    ]
    ghosh = [e for e in line if e.get("confirmation_id") == CID_PREFIX + "LATER"][0]
    assert ghosh["doctor_name"] == "Dr. P. Ghosh"
    assert ghosh["doctor_name_bn"] == "ঘোষ"


def test_upcoming_is_live_bookings_from_today_on_soonest_first(clinic):
    """A cancelled booking and a past one are history, not plans -- reading
    either out as "you have an appointment" would send a patient to a
    chamber that is not expecting them."""
    upcoming = _read_timeline(clinic)["upcoming_appointments"]
    assert [e["confirmation_id"] for e in upcoming] == [
        CID_PREFIX + "TODAY",
        CID_PREFIX + "SOON",
        CID_PREFIX + "LATER",
    ]
    assert all(e["status"] != "cancelled" for e in upcoming)


def test_a_booking_made_with_the_number_written_differently_is_still_found(clinic):
    """The clinic held both of these; an exact phone match told the patient
    they had neither."""
    r = _read_timeline(clinic)
    cids = {e["confirmation_id"] for e in r["timeline"] if e["kind"] == "appointment"}
    assert CID_PREFIX + "SOON" in cids  # stored as 91XXXXXXXXXX
    assert CID_PREFIX + "LATER" in cids  # stored as +91 XXXXX XXXXX
    assert len(r["appointments"]) == 5


def test_another_patients_booking_never_appears(clinic):
    r = _read_timeline(clinic)
    everything = r["timeline"] + r["upcoming_appointments"] + r["appointments"]
    assert CID_PREFIX + "OTHER" not in {e.get("confirmation_id") for e in everything}


def test_the_timeline_carries_no_clinical_result(clinic):
    """Minimum disclosure, as for the history: which test, when, and whether
    the report is ready -- never a value or a finding."""
    tests = [e for e in _read_timeline(clinic)["timeline"] if e["kind"] == "test"]
    assert set(tests[0]) == {
        "kind",
        "date",
        "time_slot",
        "upcoming",
        "test_name",
        "test_name_bn",
        "report_ready",
        "report_ready_on",
    }


def test_opening_the_timeline_is_audited_with_counts_not_contents(clinic):
    _read_timeline(clinic)
    db = clinic.db.SessionLocal()
    try:
        row = (
            db.query(clinic.models.DisclosureAudit)
            .filter_by(outcome="disclosed")
            .order_by(clinic.models.DisclosureAudit.id.desc())
            .first()
        )
    finally:
        db.close()
    assert row is not None and row.call_id == "tl-1"
    assert "3 upcoming" in row.detail
    assert CID_PREFIX not in row.detail and "Ghosh" not in row.detail


def test_build_timeline_treats_today_as_upcoming_and_a_cancellation_as_history():
    hs = _clinic_module("history_service")
    models = _clinic_module("models")
    today = models.Appointment(
        confirmation_id="A", doctor_id=1, date="2026-09-11", time_slot="18:00", status=models.APPT_BOOKED
    )
    cancelled = models.Appointment(
        confirmation_id="B", doctor_id=1, date="2026-09-12", time_slot="18:00", status=models.APPT_CANCELLED
    )
    out = hs.build_timeline([cancelled, today], [], {}, "2026-09-11")
    assert [e["confirmation_id"] for e in out["timeline"]] == ["A", "B"]
    assert [e["confirmation_id"] for e in out["upcoming_appointments"]] == ["A"]


# ===========================================================================
# What the caller hears
# ===========================================================================
@pytest.fixture
def tri(monkeypatch):
    gate_support.trilingual(monkeypatch)


def _upcoming(n: int) -> dict:
    return {
        "upcoming_appointments": [
            {
                "doctor_name": "Dr. A. Sen",
                "doctor_name_bn": "সেন",
                "date": f"2026-09-{14 + i}",
                "time_slot": "18:15",
                "confirmation_id": f"KCD-2026091{i}-ZZ{i}",
            }
            for i in range(n)
        ]
    }


def test_bookings_are_read_soonest_first_from_the_record(tri):
    reply = bookings_reply(_upcoming(2), "en")
    assert "Dr. A. Sen, 2026-09-14, at 18:15" in reply
    assert reply.index("2026-09-14") < reply.index("2026-09-15")


@pytest.mark.parametrize("code", lang_mod.ALL_LANGS)
def test_no_confirmation_number_is_ever_read_out(code, tri):
    """The point of the story is that the patient no longer needs one."""
    reply = bookings_reply(_upcoming(2), code)
    assert "KCD" not in reply and "ZZ" not in reply


def test_only_the_first_few_bookings_are_spoken(tri):
    reply = bookings_reply(_upcoming(BOOKINGS_SPOKEN_LIMIT + 2), "en")
    assert "2026-09-16" in reply and "2026-09-17" not in reply
    assert "2 more" in reply and gate_support.mentions_counter(reply)


@pytest.mark.parametrize("code", lang_mod.ALL_LANGS)
def test_no_bookings_is_stated_plainly(code, tri):
    assert bookings_reply({"upcoming_appointments": []}, code) == i18n.t(code, "timeline.no_bookings")


def test_the_challenge_names_what_it_unlocks_and_the_history_wording_is_unchanged(tri):
    assert verification_prompt("dob", "en") == i18n.t("en", "history.ask_dob")
    assert verification_prompt("pin", "en") == i18n.t("en", "history.ask_pin")
    assert "bookings" in verification_prompt("dob", "en", PURPOSE_BOOKINGS)
    assert "bookings" in verification_prompt("pin", "en", PURPOSE_BOOKINGS)


def test_every_timeline_sentence_exists_in_every_language():
    missing = [p for p in i18n.strict_check() if p.startswith("timeline.")]
    assert missing == []


def test_the_extractor_knows_the_new_intent():
    data = {"intent": "my_bookings", "slots": {k: None for k in _SLOT_KEYS}, "direct_reply_bn": None}
    ok, _errors = llm._validate(data)
    assert ok
    assert '"my_bookings"' in llm.SYSTEM_PROMPT_TEMPLATE


def test_the_call_audit_keeps_counts_of_the_timeline_never_its_contents():
    result = {
        "found": True,
        "tests": [{"test_name": "CBC"}],
        "appointments": [],
        "timeline": [{"doctor_name": "Dr. A. Sen"}, {"test_name": "CBC"}],
        "upcoming_appointments": [{"doctor_name": "Dr. A. Sen"}],
    }
    assert _history_summary(result) == {
        "found": True,
        "reason": None,
        "tests": 1,
        "appointments": 0,
        "timeline": 2,
        "upcoming_appointments": 1,
    }
    # An older response, with no timeline, summarises exactly as it did.
    assert set(_history_summary({"found": True, "tests": []})) == {"found", "reason", "tests", "appointments"}


# ===========================================================================
# The agent: read once, not recited
# ===========================================================================
_SLOT_KEYS = ("test_name", "doctor_name", "department", "date", "time_slot", "patient_name", "phone")

_TIMELINE_RESPONSE = {
    "found": True,
    "patient_name": "Iti Sen",
    "tests": [],
    "appointments": [],
    "timeline": [],
    "upcoming_appointments": [
        {"doctor_name": "Dr. P. Ghosh", "doctor_name_bn": "ঘোষ", "date": "2026-09-20", "time_slot": "18:15"}
    ],
}


class Speech:
    """Stands in for _speak and records the audit label as well as the words."""

    def __init__(self):
        self.lines: list[tuple[str, str | None]] = []

    async def __call__(self, session, text, fallback_reason=None, audit_redact=None):
        self.lines.append((text, audit_redact))

    @property
    def last(self) -> str:
        return self.lines[-1][0]


def _intent(name: str, **slots) -> dict:
    full = {k: None for k in _SLOT_KEYS}
    full.update(slots)
    return {"intent": name, "slots": full, "direct_reply_bn": None}


@pytest.fixture
def agent(monkeypatch):
    app = gate_support.load_main_pcm()
    monkeypatch.setenv("VOICE_AGENT_HISTORY_REQUIRE_PRIVATE_PATH", "0")
    monkeypatch.delenv("VOICE_AGENT_HISTORY_DISCLOSURE", raising=False)
    tools = gate_support.FakeTools(
        responses={
            "begin_verification": {"factor": "dob", "locked": False},
            "verify_caller": {"reply": "verified", "verified": True, "factor": "dob", "token": "tok-tl"},
            "read_history": _TIMELINE_RESPONSE,
            "book_appointment": {
                "success": True,
                "confirmation_id": "KCD-20260920-TL01",
                "doctor_name": "Dr. A. Sen",
                "doctor_name_bn": "সেন",
                "date": "2026-09-20",
                "time_slot": "18:15",
                "notification": {"status": "queued"},
            },
        }
    )
    speech = Speech()
    intents: dict[str, dict] = {}

    async def resolve(session, text):
        return intents[text]

    monkeypatch.setattr(app, "_tools", tools)
    monkeypatch.setattr(app, "_speak", speech)
    monkeypatch.setattr(app, "_resolve_intent", resolve)
    session = app.CallSession(gate_support.FakeWS())

    def say(text: str, intent: dict | None = None):
        if intent is not None:
            intents[text] = intent
        asyncio.run(app._run_turn(session, "", text_override=text))

    yield types.SimpleNamespace(app=app, tools=tools, speech=speech, session=session, say=say)
    session.cleanup()


def _verified(agent) -> None:
    s = agent.session
    s.history_token, s.history_phone = "tok-tl", PHONE
    s.timeline, s.timeline_stale = dict(_TIMELINE_RESPONSE), False


def test_what_have_i_booked_is_answered_from_the_record_after_verification(agent):
    agent.say("আমার কী বুকিং আছে", _intent("my_bookings"))
    # No number yet: the caller is asked which record, and nothing is fetched.
    assert agent.speech.last == i18n.t("bn", "timeline.ask_phone")
    assert agent.tools.called() == []

    agent.say(PHONE)
    assert agent.tools.called() == ["begin_verification"]
    assert agent.speech.last == verification_prompt("dob", "bn", PURPOSE_BOOKINGS)

    agent.say(DOB)
    assert agent.tools.called() == ["begin_verification", "verify_caller", "read_history"]
    text, redact = agent.speech.lines[-1]
    assert "ঘোষ" in text and "2026-09-20" in text
    assert redact == "patient_timeline"


def test_the_record_is_fetched_once_and_answers_the_rest_of_the_call(agent):
    agent.say("আমার কী বুকিং আছে", _intent("my_bookings", phone=PHONE))
    agent.say(DOB)
    agent.say("আমার আগের টেস্টগুলো", _intent("patient_history"))
    agent.say("আবার বুকিংগুলো বলুন", _intent("my_bookings"))
    assert agent.tools.called().count("read_history") == 1
    assert agent.tools.called().count("verify_caller") == 1


def test_nothing_is_fetched_before_verification(agent):
    agent.say("আমার কী বুকিং আছে", _intent("my_bookings", phone=PHONE))
    assert "read_history" not in agent.tools.called()
    assert agent.session.timeline is None


def test_nothing_is_read_out_on_a_speakerphone(agent, monkeypatch):
    monkeypatch.setenv("VOICE_AGENT_HISTORY_REQUIRE_PRIVATE_PATH", "1")
    _verified(agent)
    agent.session.timeline = None
    monkeypatch.setattr(agent.session.echo, "classify", lambda: PATH_SPEAKERPHONE)
    agent.say("আমার কী বুকিং আছে", _intent("my_bookings"))
    assert "read_history" not in agent.tools.called()
    assert "record_disclosure_refusal" in agent.tools.called()
    assert agent.speech.last == i18n.t("bn", "history.speakerphone")


def test_a_verified_caller_is_not_asked_for_their_name_and_number(agent):
    _verified(agent)
    agent.say(
        "সেনের কাছে ২০ তারিখ ১৮:১৫",
        _intent("book_appointment", doctor_name="সেন", date="2026-09-20", time_slot="18:15"),
    )
    name, args, _kw = agent.tools.calls[-1]
    assert name == "book_appointment"
    assert args == ("সেন", "2026-09-20", "18:15", "Iti Sen", PHONE)
    assert agent.speech.last.endswith(i18n.t("bn", "timeline.used_record"))
    assert "Iti Sen" not in agent.speech.last  # the name is used, never read back


def test_the_callers_own_words_win_over_the_record(agent):
    _verified(agent)
    agent.say(
        "মায়ের জন্য বুক করুন",
        _intent(
            "book_appointment",
            doctor_name="সেন",
            date="2026-09-20",
            time_slot="18:15",
            patient_name="Jaya Sen",
        ),
    )
    _name, args, _kw = agent.tools.calls[-1]
    assert args[3] == "Jaya Sen" and args[4] == PHONE


def test_the_record_also_fills_a_booking_built_up_over_several_turns(agent):
    _verified(agent)
    agent.session.pending = {
        "awaiting": "time_slot",
        "slots": {"doctor_name": "সেন", "date": "2026-09-20"},
        "candidates": None,
        "offered_date": None,
        "retries": 0,
    }
    agent.say("18:15")
    _name, args, _kw = agent.tools.calls[-1]
    assert args == ("সেন", "2026-09-20", "18:15", "Iti Sen", PHONE)


def test_an_unverified_caller_is_still_asked(agent):
    agent.say(
        "সেনের কাছে বুক করুন",
        _intent("book_appointment", doctor_name="সেন", date="2026-09-20", time_slot="18:15"),
    )
    assert "book_appointment" not in agent.tools.called()
    assert agent.session.pending["awaiting"] == "patient_name"


def test_a_booking_made_on_this_call_is_in_the_next_answer(agent):
    """The record held from earlier no longer lists everything; the next
    read fetches it again rather than reading out a stale copy."""
    _verified(agent)
    agent.say("বুক করুন", _intent("book_appointment", doctor_name="সেন", date="2026-09-20", time_slot="18:15"))
    assert agent.session.timeline_stale is True
    agent.say("আমার কী বুকিং আছে", _intent("my_bookings"))
    assert agent.tools.called().count("read_history") == 1


def test_the_record_dies_with_the_call(agent):
    _verified(agent)
    agent.session.cleanup()
    assert agent.session.timeline is None and agent.session.history_token is None


# ===========================================================================
# Both halves together: the agent's real client against the real clinic-api
# ===========================================================================
def test_the_agent_reads_the_real_timeline_end_to_end(clinic, agent, monkeypatch):
    tools = ClinicToolsClient("http://clinic.test")
    tools._client = httpx.AsyncClient(
        base_url="http://clinic.test", transport=httpx.ASGITransport(app=_clinic_app().app)
    )
    monkeypatch.setattr(agent.app, "_tools", tools)
    try:
        agent.say("আমার কী বুকিং আছে", _intent("my_bookings", phone=PHONE))
        agent.say(DOB)
    finally:
        asyncio.run(tools.aclose())

    spoken = agent.speech.last
    # Soonest first, from clinic-api's own rows: today with Dr. Ghosh, then
    # the rescheduled one with Dr. Sen, then the one written "+91 ...".
    assert spoken.index("2026-09-11") < spoken.index("2026-09-14") < spoken.index("2026-09-20")
    assert "2026-09-15" not in spoken  # cancelled
    assert "2026-08-20" not in spoken  # past
    assert CID_PREFIX not in spoken
    assert agent.session.timeline["patient_name"] == "Iti Sen"
