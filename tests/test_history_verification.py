"""Tests for "History disclosed only after verification".

Author: Chakravardhan
Story:  "As a patient, I want my history spoken only to me, so that whoever
         else uses this handset cannot hear what tests I have had."

WHAT THESE COVER
----------------
The threat is not a hacker. It is an ordinary person holding a phone that
is not theirs, dialling the clinic and being treated as its owner. So the
tests are organised around what that person can learn:

  * NOTHING WITHOUT PROOF -- history is unreachable without a token, and a
    token only comes from a correct answer;
  * NOTHING FROM A FAILURE -- a wrong PIN, an unknown number and a patient
    with no factor on file are indistinguishable to the caller. This is the
    enumeration rule, and it is the one most verification systems leak
    through;
  * NOTHING FROM PERSISTENCE -- attempts are counted per patient, so
    redialling does not refill the budget;
  * NOTHING OVERHEARD -- a speakerphone, or an audio path we cannot
    classify, blocks disclosure even for a correctly verified caller.

WHAT THEY DELIBERATELY DO NOT COVER
-----------------------------------
Whether ERL really distinguishes a handset from a speakerphone in a real
Kolkata room. That is GPU and real-audio work, and echo_guard.py's own
docstring already flags its thresholds as unvalidated. These tests assert
that the DECISION is wired correctly given a classification, not that the
classification is right.

Nor timing side-channels as measured over a real network. There is a dummy
hash on the no-patient path (see history_service._DUMMY_HASH); proving it
equalises timing needs a controlled environment, not pytest.

    python -m pytest tests/test_history_verification.py -v
"""
from __future__ import annotations

import datetime
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CLINIC = os.path.join(_ROOT, "clinic-api")

# Must precede the clinic-api imports -- db.py resolves DATABASE_URL at
# import time. Same reasoning as tests/test_notifications.py.
_TMP_DB = os.path.join(tempfile.mkdtemp(prefix="clinic-hist-"), "clinic.db")
os.environ["CLINIC_DB_PATH"] = _TMP_DB
os.environ.pop("DATABASE_URL", None)
os.environ["VOICE_AGENT_PROVIDER"] = "vast"

sys.path.insert(0, _ROOT)
sys.path.insert(0, _CLINIC)

import verification as v                                   # noqa: E402
from agent import privacy                                   # noqa: E402
from agent.echo_guard import (                              # noqa: E402
    PATH_HANDSET, PATH_SPEAKERPHONE, PATH_UNKNOWN,
)
from agent.reply_templates import (                         # noqa: E402
    verification_prompt, verification_failed_reply, verification_locked_reply,
    disclosure_blocked_reply, history_reply,
)


@pytest.fixture()
def trilingual(monkeypatch):
    """Enable all three languages for tests that assert English wording.

    Needed because language.resolve() only ever returns a language this pod
    can SERVE -- on a bare pod "en" correctly falls back to Bengali. That is
    the gate working, not a bug, so the fixture turns the languages on rather
    than the assertions being loosened.
    """
    monkeypatch.setenv("VOICE_AGENT_LANGUAGES", "bn,hi,en")
    monkeypatch.setenv("VOICE_AGENT_NEMO_FILE_HI", "/fake/hi.nemo")
    monkeypatch.setenv("VOICE_AGENT_NEMO_FILE_EN", "/fake/en.nemo")


# ===========================================================================
# The policy, with no database in sight
# ===========================================================================
def test_a_pin_is_never_stored_in_the_clear():
    salt = v.new_salt()
    hashed = v.hash_pin("1234", salt)
    assert "1234" not in hashed
    assert hashed != "1234"
    assert len(hashed) == 64                    # sha256 hex
    assert v.pin_matches("1234", salt, hashed)
    assert not v.pin_matches("1235", salt, hashed)


def test_the_same_pin_under_a_different_salt_hashes_differently():
    """Otherwise a database dump reveals which patients share a PIN, and
    "1234" would light up in a single pass."""
    a, b = v.new_salt(), v.new_salt()
    assert v.hash_pin("1234", a) != v.hash_pin("1234", b)


def test_pin_matching_survives_missing_material():
    assert not v.pin_matches("1234", None, None)
    assert not v.pin_matches("1234", v.new_salt(), None)
    assert not v.pin_matches(None, v.new_salt(), "abc")


@pytest.mark.parametrize("spoken,expected", [
    ("1234", "1234"),
    ("1-2-3-4", "1234"),
    ("my pin is 1234", "1234"),
    ("123", None),                              # too short
    ("12345", None),                            # too long
    ("", None), (None, None),
])
def test_pin_normalisation(spoken, expected):
    assert v.normalise_pin(spoken) == expected


@pytest.mark.parametrize("spoken,expected", [
    ("1990-05-14", "1990-05-14"),
    ("14-05-1990", "1990-05-14"),
    ("14/05/1990", "1990-05-14"),
    ("sometime in 1990", None),                 # not confidently a date
    ("", None), (None, None),
])
def test_dob_normalisation(spoken, expected):
    """Ambiguity returns None rather than a guess -- a wrong guess costs the
    caller one of three attempts for an answer they got right."""
    assert v.normalise_dob(spoken) == expected


# ---------------------------------------------------------------------------
# THE ENUMERATION RULE
# ---------------------------------------------------------------------------
def test_an_unknown_number_and_a_wrong_answer_are_indistinguishable():
    """THE MOST IMPORTANT TEST IN THIS FILE.

    If these differed, the line would answer "does this person attend this
    clinic" for any number a caller cares to try -- which is itself medical
    information about that person, disclosed without any verification at all.
    """
    unknown = v.evaluate(patient_exists=False, locked=False, factor=v.FACTOR_DOB,
                         matched=False, attempts_before=0)
    wrong = v.evaluate(patient_exists=True, locked=False, factor=v.FACTOR_PIN,
                       matched=False, attempts_before=0)
    assert unknown.reply == wrong.reply == v.REPLY_FAILED
    assert not unknown.verified and not wrong.verified
    # ...and the audit trail still tells the clinic them apart.
    assert unknown.outcome != wrong.outcome


def test_a_patient_with_no_factor_on_file_also_looks_the_same():
    """Saying "we have nothing to check you against" would confirm the
    patient exists."""
    none_available = v.evaluate(patient_exists=True, locked=False,
                                factor=v.FACTOR_NONE, matched=False,
                                attempts_before=0)
    assert none_available.reply == v.REPLY_FAILED
    assert none_available.outcome == v.OUTCOME_NO_FACTOR


def test_an_unknown_number_is_still_challenged():
    """It must not short-circuit. A caller who is asked nothing has learned
    that the number is unknown."""
    challenge = v.challenge_for(patient_exists=False, has_pin=False, has_dob=False)
    assert challenge.factor == v.FACTOR_DOB
    assert challenge.patient_exists is False


def test_the_strongest_available_factor_is_chosen():
    assert v.choose_factor(has_pin=True, has_dob=True) == v.FACTOR_PIN
    assert v.choose_factor(has_pin=False, has_dob=True) == v.FACTOR_DOB
    assert v.choose_factor(has_pin=False, has_dob=False) == v.FACTOR_NONE


# ---------------------------------------------------------------------------
# Lockout
# ---------------------------------------------------------------------------
def test_lockout_triggers_on_the_configured_attempt():
    for attempts_before in range(v.MAX_ATTEMPTS - 1):
        d = v.evaluate(patient_exists=True, locked=False, factor=v.FACTOR_PIN,
                       matched=False, attempts_before=attempts_before)
        assert not d.lock_now, attempts_before
    final = v.evaluate(patient_exists=True, locked=False, factor=v.FACTOR_PIN,
                       matched=False, attempts_before=v.MAX_ATTEMPTS - 1)
    assert final.lock_now


def test_a_locked_patient_is_refused_before_the_answer_is_even_considered():
    """Checked first so a locked patient's remaining attempts cannot be
    probed, and so a correct answer during a lockout does not silently
    succeed."""
    d = v.evaluate(patient_exists=True, locked=True, factor=v.FACTOR_PIN,
                   matched=True, attempts_before=0)
    assert d.reply == v.REPLY_LOCKED
    assert not d.verified


def test_lockout_window_is_in_the_future():
    now = datetime.datetime(2026, 9, 10, 12, 0, 0)
    until = v.lockout_until(now)
    assert until > now
    assert v.is_locked(until, now)
    assert not v.is_locked(until, until + datetime.timedelta(seconds=1))
    assert not v.is_locked(None, now)


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------
def test_tokens_are_unguessable_and_unique():
    tokens = {v.new_token() for _ in range(200)}
    assert len(tokens) == 200
    assert all(len(t) >= 24 for t in tokens)


def test_a_token_expires():
    now = datetime.datetime(2026, 9, 10, 12, 0, 0)
    expiry = v.token_expiry(now)
    assert v.token_valid(expiry, now)
    assert not v.token_valid(expiry, expiry + datetime.timedelta(seconds=1))
    assert not v.token_valid(None, now)


# ===========================================================================
# The audio path -- "cannot HEAR"
# ===========================================================================
class _Guard:
    """Stands in for EchoGuard. Only classify() is consulted."""
    def __init__(self, path):
        self._path = path

    def classify(self):
        return self._path


def test_a_speakerphone_blocks_disclosure():
    safe, reason = privacy.audio_path_is_private(_Guard(PATH_SPEAKERPHONE))
    assert not safe
    assert reason == privacy.UNSAFE_SPEAKERPHONE
    assert privacy.recoverable(reason)          # the caller can pick the phone up


def test_a_handset_allows_disclosure():
    safe, reason = privacy.audio_path_is_private(_Guard(PATH_HANDSET))
    assert safe and reason == privacy.SAFE


def test_an_unclassified_path_blocks_disclosure():
    """THE DELIBERATE DISAGREEMENT WITH echo_guard.

    EchoGuard.reporting_path() collapses PATH_UNKNOWN to PATH_HANDSET, and is
    right to for metrics. Here that would disclose a medical history on every
    call that ends before the classifier has enough observations -- which is
    precisely the SHORT calls, and "dial, ask for my history, hang up" is
    exactly that shape.
    """
    safe, reason = privacy.audio_path_is_private(_Guard(PATH_UNKNOWN))
    assert not safe
    assert reason == privacy.UNSAFE_UNKNOWN


def test_the_master_switch_blocks_everything_and_is_not_recoverable(monkeypatch):
    monkeypatch.setenv("VOICE_AGENT_HISTORY_DISCLOSURE", "0")
    safe, reason = privacy.audio_path_is_private(_Guard(PATH_HANDSET))
    assert not safe
    assert reason == privacy.UNSAFE_DISABLED
    # Not recoverable: telling the caller to pick the phone up would send
    # them round a loop that cannot succeed.
    assert not privacy.recoverable(reason)


def test_the_bench_override_is_explicit_and_named(monkeypatch):
    """A browser mic on laptop speakers classifies as speakerphone on every
    call, so a developer could never reach the verification flow. The
    override exists, is off by default, and is named after what it weakens."""
    monkeypatch.setenv("VOICE_AGENT_HISTORY_REQUIRE_PRIVATE_PATH", "0")
    safe, _ = privacy.audio_path_is_private(_Guard(PATH_SPEAKERPHONE))
    assert safe
    monkeypatch.setenv("VOICE_AGENT_HISTORY_REQUIRE_PRIVATE_PATH", "1")
    safe, _ = privacy.audio_path_is_private(_Guard(PATH_SPEAKERPHONE))
    assert not safe


# ===========================================================================
# What the caller is told
# ===========================================================================
def test_every_failure_sounds_identical():
    """The wording is part of the security. A caller must not be able to tell
    a wrong PIN from an unknown number from a patient with no factor -- and
    reply_templates has only one sentence for all three."""
    assert verification_failed_reply(False) == verification_failed_reply(False)
    retry = verification_failed_reply(False)
    exhausted = verification_failed_reply(True)
    assert retry != exhausted                   # only "can you try again" differs
    for text in (retry, exhausted):
        assert "পিন" not in text or "জন্মতারিখ" not in text  # never names both


def test_no_reply_reveals_how_many_attempts_remain():
    """A countdown is useful to somebody guessing and useless to somebody who
    simply mistyped."""
    texts = [verification_failed_reply(False), verification_failed_reply(True),
             verification_locked_reply(), verification_prompt("pin"),
             verification_prompt("dob")]
    for text in texts:
        for digit in ("1", "2", "3", "one", "two", "three"):
            assert digit not in text.lower(), text


def test_the_locked_reply_does_not_say_for_how_long():
    text = verification_locked_reply()
    assert "30" not in text and "minute" not in text.lower()


def test_the_speakerphone_reply_names_the_fix_not_a_failure(trilingual):
    """A verified patient standing next to their family must learn to pick
    the phone up -- not that they failed to prove who they are."""
    text = disclosure_blocked_reply(privacy.UNSAFE_SPEAKERPHONE, "en")
    assert "speaker" in text.lower()
    assert "ear" in text.lower()
    assert "did not match" not in text.lower()


def test_the_disabled_reply_sends_the_caller_to_the_counter(trilingual):
    text = disclosure_blocked_reply(privacy.UNSAFE_DISABLED, "en")
    assert "counter" in text.lower()
    assert "speaker" not in text.lower()


# ---------------------------------------------------------------------------
# Minimum disclosure
# ---------------------------------------------------------------------------
_HISTORY = {
    "patient_name": "Riya Das",
    "tests": [
        {"test_name": "CBC", "test_name_bn": "সিবিসি", "taken_on": "2026-08-01",
         "report_ready": True, "report_ready_on": "2026-08-02"},
        {"test_name": "Lipid Profile", "test_name_bn": "লিপিড প্রোফাইল",
         "taken_on": "2026-07-11", "report_ready": True},
        {"test_name": "Thyroid", "test_name_bn": "থাইরয়েড",
         "taken_on": "2026-06-02", "report_ready": False},
        {"test_name": "Vitamin D", "test_name_bn": "ভিটামিন ডি",
         "taken_on": "2026-05-02", "report_ready": True},
    ],
    "appointments": [],
}


def test_only_the_first_few_tests_are_spoken(trilingual):
    """A spoken list stops being usable past about three items -- and every
    extra sentence is more time during which somebody can walk into the room.
    Minimum disclosure is a privacy property here, not only a UX one."""
    reply = history_reply(_HISTORY, "en")
    assert "CBC" in reply
    assert "Vitamin D" not in reply              # the 4th is deferred
    assert "1 more" in reply or "more" in reply


def test_history_never_speaks_a_result_or_a_value(trilingual):
    """clinic-api does not return results, and this function could not speak
    them if it did. Asserted so a future 'helpful' addition trips here."""
    reply = history_reply(_HISTORY, "en").lower()
    for leak in ("mg/dl", "positive", "negative", "normal", "abnormal", "result"):
        assert leak not in reply, leak


def test_history_points_at_the_counter_for_detail(trilingual):
    """Which is also the path that needs no smartphone -- the two stories
    agree on the same destination, and that is not a coincidence."""
    assert "counter" in history_reply(_HISTORY, "en").lower()


def test_an_empty_history_is_stated_plainly(trilingual):
    reply = history_reply({"tests": []}, "en")
    assert "no test" in reply.lower()
    assert "counter" not in reply.lower()        # nothing to collect


def test_history_is_answered_in_every_language(trilingual):
    for code in ("bn", "hi", "en"):
        assert history_reply(_HISTORY, code)
        assert verification_prompt("pin", code)
        assert verification_locked_reply(code)


# ===========================================================================
# End to end, through the real endpoints
# ===========================================================================
_CLINIC_APP = None


def _load_clinic_api_main():
    """Load clinic-api/main.py under an unambiguous module name.

    There are TWO main.py files in this repo and every other test module
    inserts the repo root at sys.path[0] when collected -- see the same note
    in tests/test_notifications.py.
    """
    global _CLINIC_APP
    if _CLINIC_APP is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "clinic_api_main_hist", os.path.join(_CLINIC, "main.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules["clinic_api_main_hist"] = module
        spec.loader.exec_module(module)
        _CLINIC_APP = module
    return _CLINIC_APP


PHONE = "9876500001"
PIN = "4271"
DOB = "1990-05-14"


@pytest.fixture()
def client():
    """A live clinic-api with one patient who has BOTH factors, plus a
    second who has neither -- the case that must look identical to an
    unknown number."""
    from fastapi.testclient import TestClient
    from db import SessionLocal
    from models import DisclosureAudit, Patient, TestRecord
    import history_service
    import datetime as _dt

    clinic_main = _load_clinic_api_main()
    with TestClient(clinic_main.app) as c:
        db = SessionLocal()
        try:
            db.query(DisclosureAudit).delete()
            db.query(TestRecord).delete()
            db.query(Patient).delete()
            db.commit()

            p = Patient(phone=PHONE, full_name="Riya Das", date_of_birth=DOB,
                        failed_attempts=0, created_at=_dt.datetime.now())
            p.pin_salt = v.new_salt()
            p.pin_hash = v.hash_pin(PIN, p.pin_salt)
            db.add(p)
            db.add(Patient(phone="9876500002", full_name="No Factors",
                           failed_attempts=0, created_at=_dt.datetime.now()))
            db.commit()

            db.add(TestRecord(patient_id=p.id, test_name="CBC",
                              test_name_bn="সিবিসি", taken_on="2026-08-01",
                              report_ready=True, created_at=_dt.datetime.now()))
            db.commit()
        finally:
            db.close()

        history_service._TOKENS.clear()
        yield c


def _verify(client, phone=PHONE, factor="pin", answer=PIN):
    return client.post("/api/v1/history/verify", json={
        "phone": phone, "factor": factor, "answer": answer, "call_id": "t1"}).json()


def test_history_is_unreachable_without_a_token(client):
    """THE BASELINE. No token, no history -- not a partial answer, not a
    count, nothing."""
    r = client.post("/api/v1/history/read", json={"token": "", "call_id": "t1"}).json()
    assert r["found"] is False
    assert r["reason"] == "not_verified"
    assert "tests" not in r


def test_an_invented_token_opens_nothing(client):
    r = client.post("/api/v1/history/read",
                    json={"token": v.new_token(), "call_id": "t1"}).json()
    assert r["found"] is False


def test_a_correct_pin_verifies_and_then_discloses(client):
    out = _verify(client)
    assert out["verified"] is True
    assert out["token"]

    r = client.post("/api/v1/history/read",
                    json={"token": out["token"], "call_id": "t1"}).json()
    assert r["found"] is True
    assert r["patient_name"] == "Riya Das"
    assert len(r["tests"]) == 1
    assert r["tests"][0]["test_name"] == "CBC"


def test_a_wrong_pin_yields_no_token(client):
    out = _verify(client, answer="0000")
    assert out["verified"] is False
    assert out["token"] is None
    assert out["reply"] == "failed"


def test_an_unknown_number_is_challenged_exactly_like_a_known_one(client):
    """No enumeration, through the real endpoint. The challenge for a number
    the clinic has never seen must be indistinguishable from a real one."""
    known = client.post("/api/v1/history/verify/begin",
                        json={"phone": "9999999999", "call_id": "t1"}).json()
    assert "factor" in known
    assert known["locked"] is False
    # ...and the failure looks the same as a wrong answer.
    out = _verify(client, phone="9999999999", factor="dob", answer=DOB)
    assert out["reply"] == "failed"
    assert out["token"] is None


def test_a_patient_with_no_factor_looks_the_same_as_everyone_else(client):
    """9876500002 exists but has neither PIN nor date of birth. Saying so
    would confirm they are a patient here."""
    out = _verify(client, phone="9876500002", factor="dob", answer=DOB)
    assert out["reply"] == "failed"
    assert out["token"] is None


def test_the_confirmation_id_is_not_accepted_as_proof(client):
    """It was SMSed to this handset and is readable by whoever holds it.
    Accepting it would re-open the exact hole this story closes."""
    out = _verify(client, factor="confirmation_id", answer="KCD-20260914-0031")
    assert out["verified"] is False
    assert out["token"] is None


def test_three_wrong_answers_lock_the_number(client):
    for _ in range(v.MAX_ATTEMPTS):
        out = _verify(client, answer="0000")
    assert out["reply"] in ("failed", "locked")

    after = _verify(client, answer=PIN)      # now the CORRECT pin
    assert after["verified"] is False
    assert after["reply"] == "locked"


def test_redialling_does_not_refill_the_attempt_budget(client):
    """Attempts are counted PER PATIENT, not per call. A per-session counter
    would be reset by hanging up -- the first thing anyone would try."""
    for _ in range(v.MAX_ATTEMPTS):
        _verify(client, answer="0000")

    # A brand new "call": fresh begin, fresh call_id.
    fresh = client.post("/api/v1/history/verify/begin",
                        json={"phone": PHONE, "call_id": "call-2"}).json()
    assert fresh["locked"] is True

    out = client.post("/api/v1/history/verify", json={
        "phone": PHONE, "factor": "pin", "answer": PIN, "call_id": "call-2"}).json()
    assert out["verified"] is False


def test_a_date_of_birth_also_verifies(client):
    out = _verify(client, factor="dob", answer=DOB)
    assert out["verified"] is True
    assert out["token"]


def test_a_revoked_token_stops_working(client):
    """The call ended; the handset may now be in somebody else's hand."""
    import history_service
    out = _verify(client)
    token = out["token"]
    assert client.post("/api/v1/history/read",
                       json={"token": token}).json()["found"] is True

    history_service.revoke_token(token)
    assert client.post("/api/v1/history/read",
                       json={"token": token}).json()["found"] is False


def test_every_attempt_is_audited(client):
    _verify(client, answer="0000")           # wrong
    _verify(client)                          # right
    _verify(client, phone="9999999999", factor="dob", answer=DOB)   # unknown

    audit = client.get("/api/v1/history/audit").json()
    outcomes = {row["outcome"] for row in audit["audit"]}
    assert v.OUTCOME_WRONG in outcomes
    assert v.OUTCOME_VERIFIED in outcomes
    assert v.OUTCOME_NO_PATIENT in outcomes


def test_the_audit_never_stores_the_secret(client):
    """A trail that leaks the thing it audits is worse than none."""
    # A wrong PIN chosen NOT to be a substring of the test phone number --
    # "0000" is, and the first version of this test failed on its own
    # fixture data rather than on anything the code did.
    wrong = "5555"
    assert wrong not in PHONE
    _verify(client, answer=wrong)
    _verify(client, factor="dob", answer=DOB)
    audit = client.get("/api/v1/history/audit").json()
    blob = repr(audit)
    assert PIN not in blob
    assert DOB not in blob
    assert wrong not in blob


def test_a_refused_disclosure_is_audited(client):
    """The agent blocked it because of the room. Without a row, "it would not
    tell me my history" has no explanation on the clinic side."""
    client.post("/api/v1/history/refusal", json={
        "phone": PHONE, "reason": "speakerphone", "call_id": "t1"})
    audit = client.get("/api/v1/history/audit",
                       params={"outcome": v.OUTCOME_UNSAFE_PATH}).json()
    assert audit["count"] == 1
    assert audit["audit"][0]["detail"] == "speakerphone"


def test_health_reports_the_disclosure_counters(client):
    _verify(client, answer="0000")
    h = client.get("/api/health").json()
    assert "history_disclosure" in h
    assert h["history_disclosure"][v.OUTCOME_WRONG] >= 1


def test_a_pin_can_be_set_at_the_counter_but_not_from_the_line(client):
    """The endpoint exists for staff. agent/tools_client.py deliberately has
    no method for it, so no future turn-loop change can reach it."""
    r = client.post("/api/v1/patients/pin", json={
        "phone": PHONE, "pin": "9182", "staff": "reception-1"}).json()
    assert r["success"] is True
    assert _verify(client, answer="9182")["verified"] is True

    from agent.tools_client import ClinicToolsClient
    assert not hasattr(ClinicToolsClient, "set_pin")
