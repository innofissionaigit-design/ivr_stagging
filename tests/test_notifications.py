"""Tests for the written patient confirmation -- templates, the delivery
ledger, and the three appointment events that trigger a message.

WHAT THESE COVER
----------------
Everything the story's acceptance criterion can be settled on a laptop:

  * that a message is queued on booking, reschedule AND cancellation;
  * that its text comes from a registered template and nowhere else, and
    that the DLT rules which get a message rejected (variable count,
    variable length, unregistered template id) are enforced HERE rather
    than discovered at the operator;
  * that delivery receipts are recorded, including the out-of-order and
    duplicate ones operators actually send;
  * that failures reach the staff queue -- including the silent kind,
    where the gateway accepted the message and no receipt ever came back.

WHAT THEY DELIBERATELY DO NOT COVER
-----------------------------------
Whether the real hospital gateway speaks the JSON contract in
notifications.send()'s docstring. Nothing on a laptop can establish that;
the gateway is a third party and its adapter is one function, changed once
its real contract is known. Every test here fakes that boundary and
asserts what THIS codebase does on either side of it.

Nor do they cover the Bengali wording being good Bengali. That needs a
speaker, and asserting it here would be asserting an assumption.

    python -m pytest tests/test_notifications.py -v     (from the repo root)

The clinic-api modules are imported as top-level names (`import
notifications`), the same way clinic-api/main.py imports them, so this file
puts clinic-api/ on sys.path. CLINIC_DB_PATH is pointed at a scratch file
BEFORE that import, because db.py reads it at import time.
"""
from __future__ import annotations

import datetime
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CLINIC = os.path.join(_ROOT, "clinic-api")

# Must precede the clinic-api imports: db.py resolves DATABASE_URL at
# import time, so a path set afterwards would be ignored and the test would
# quietly run against the real /workspace/clinic.db.
_TMP_DB = os.path.join(tempfile.mkdtemp(prefix="clinic-test-"), "clinic.db")
os.environ["CLINIC_DB_PATH"] = _TMP_DB
os.environ.pop("DATABASE_URL", None)
os.environ["VOICE_AGENT_PROVIDER"] = "vast"          # WAL; see db.py

sys.path.insert(0, _ROOT)
sys.path.insert(0, _CLINIC)

import message_templates as mt                        # noqa: E402
import notifications as notify                        # noqa: E402
import notify_service                                 # noqa: E402
from agent.reply_templates import (                    # noqa: E402
    booking_reply, cancel_reply, reschedule_reply,
)


# ===========================================================================
# message_templates -- the DLT rules, enforced before the operator sees them
# ===========================================================================
_BOOKED_VALUES = {
    "patient_name": "Riya Das",
    "doctor_name": "Sen",
    "date": "2026-09-14",
    "time_slot": "18:15",
    "confirmation_id": "KCD-20260914-0031",
}


@pytest.mark.parametrize("event", mt.ALL_EVENTS)
def test_every_event_has_a_template_whose_markers_match_its_variables(event):
    """The check that runs at import time, asserted per event so a failure
    names the template that broke."""
    tpl = mt.TEMPLATES[event]
    assert tpl.marker_count == len(tpl.variables)
    assert tpl.category == "transactional"


@pytest.mark.parametrize("event", mt.ALL_EVENTS)
def test_rendered_message_carries_the_reference_and_no_leftover_markers(event):
    text, _ = mt.render(event, _BOOKED_VALUES)
    assert "{#var#}" not in text
    assert _BOOKED_VALUES["confirmation_id"] in text


def test_booked_and_rescheduled_tell_the_patient_to_show_the_message():
    """The story's actual requirement. A confirmation that does not say it
    IS the thing to show at reception leaves the patient still believing
    the reference number is their responsibility."""
    for event in (mt.EVENT_BOOKED, mt.EVENT_RESCHEDULED):
        text, _ = mt.render(event, _BOOKED_VALUES)
        assert "রিসেপশনে" in text


def test_a_variable_over_the_dlt_limit_is_refused_here_not_by_the_operator():
    values = dict(_BOOKED_VALUES, patient_name="র" * (mt.VAR_MAX_CHARS + 1))
    with pytest.raises(mt.TemplateError):
        mt.render(mt.EVENT_BOOKED, values)


def test_a_missing_or_empty_variable_is_refused():
    for bad in ({k: v for k, v in _BOOKED_VALUES.items() if k != "date"},
                dict(_BOOKED_VALUES, date="   ")):
        with pytest.raises(mt.TemplateError):
            mt.render(mt.EVENT_BOOKED, bad)


def test_unknown_event_is_refused():
    with pytest.raises(mt.TemplateError):
        mt.render("reminder", _BOOKED_VALUES)


def test_a_marker_inside_a_variable_cannot_shift_the_remaining_positions():
    """A patient name arrives through ASR from whatever a caller said. If
    substitution were repeated str.replace, a name containing the literal
    marker would consume the NEXT variable's slot and silently shift the
    date into the time position."""
    values = dict(_BOOKED_VALUES, patient_name="A {#var#} B")
    text, _ = mt.render(mt.EVENT_BOOKED, values)
    assert text.count("{#var#}") == 1               # the one inside the name
    assert _BOOKED_VALUES["date"] in text
    assert _BOOKED_VALUES["time_slot"] in text


def test_templates_stay_within_their_costed_segment_count():
    """Bengali is UCS-2: 70 characters per segment. A template that grows
    past three segments multiplies the hospital's per-message cost, and an
    invoice is a slow way to find out."""
    for event in mt.ALL_EVENTS:
        text, _ = mt.render(event, _BOOKED_VALUES)
        assert mt.estimated_segments(text) <= 3, f"{event} is {len(text)} chars"


# ===========================================================================
# notifications -- gateway vocabulary, with no database in sight
# ===========================================================================
def test_msisdn_adds_the_country_code_to_a_ten_digit_number_and_leaves_longer_alone():
    assert notify.msisdn("9876543210", "91") == "919876543210"
    assert notify.msisdn("919876543210", "91") == "919876543210"
    # parse_phone() strips punctuation, but a staff-entered value may not have
    assert notify.msisdn("+91 98765-43210", "91") == "919876543210"


def _config(**over) -> notify.GatewayConfig:
    base = dict(url="https://gw.example/send", api_key="k", auth_header="Authorization",
                sender_id="KCDIAG", entity_id="1234567890", country_code="91",
                timeout_s=8.0, dlr_token="t")
    base.update(over)
    return notify.GatewayConfig(**base)


def _registered(event=mt.EVENT_BOOKED, template_id="1707100000000000001"):
    tpl = mt.TEMPLATES[event]
    return mt.MessageTemplate(event=tpl.event, template_id=template_id,
                              category=tpl.category, body=tpl.body,
                              variables=tpl.variables, lang=tpl.lang)


def test_preflight_blocks_every_condition_the_operator_would_reject_us_for():
    assert notify.preflight(_config(), _registered()) is None
    assert notify.preflight(_config(url=""), _registered()) == "gateway_not_configured"
    assert notify.preflight(_config(), _registered(template_id="")) == "template_not_registered"
    assert notify.preflight(_config(sender_id=""), _registered()) == "sender_id_not_configured"
    assert notify.preflight(_config(entity_id=""), _registered()) == "entity_id_not_configured"


@pytest.mark.parametrize("raw,expected", [
    ("DELIVERED", notify.STATUS_DELIVERED),
    ("delivrd", notify.STATUS_DELIVERED),
    ("success", notify.STATUS_DELIVERED),
    ("FAILED", notify.STATUS_FAILED),
    ("expired", notify.STATUS_FAILED),
    ("DND", notify.STATUS_FAILED),
    ("en-route", notify.STATUS_SENT),
    ("queued", notify.STATUS_SENT),
])
def test_receipt_statuses_map_to_ledger_statuses(raw, expected):
    assert notify.classify_receipt(raw) == expected


def test_an_unrecognised_receipt_status_is_a_failure_not_a_success():
    """The default direction matters. Reading an unmapped vendor code as
    'delivered' would hide exactly the messages nobody has seen before."""
    assert notify.classify_receipt("SOME_NEW_VENDOR_CODE") == notify.STATUS_FAILED
    assert notify.classify_receipt("") == notify.STATUS_FAILED


# ===========================================================================
# End to end, through the real endpoints
# ===========================================================================
_CLINIC_APP = None


def _load_clinic_api_main():
    """Load clinic-api/main.py under an unambiguous module name.

    A plain `import main` is a trap in this repository: there are TWO
    main.py files -- the voice agent's at the repo root and clinic-api's --
    and every other test module inserts the repo root at sys.path[0] when
    it is collected. Running this file alone resolved to the right one;
    running the whole suite resolved to the voice agent's, which imports
    agent/asr.py and fails on a gated model checkpoint that has nothing to
    do with notifications. Loading it by path removes the ambiguity
    entirely instead of depending on sys.path ordering between modules.
    """
    global _CLINIC_APP
    if _CLINIC_APP is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "clinic_api_main", os.path.join(_CLINIC, "main.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules["clinic_api_main"] = module
        spec.loader.exec_module(module)
        _CLINIC_APP = module
    return _CLINIC_APP


@pytest.fixture()
def client(monkeypatch):
    """A live clinic-api against a scratch SQLite file, with the gateway
    faked at notifications.send() -- the one function that touches the
    network. Everything on either side of it is the real code path,
    including the BackgroundTask, which starlette's TestClient runs
    synchronously before returning the response.
    """
    from fastapi.testclient import TestClient
    clinic_main = _load_clinic_api_main()

    sent: list[dict] = []

    def fake_send(config, *, to, text, tpl, client_ref):
        sent.append({"to": to, "text": text, "template_id": tpl.template_id,
                     "client_ref": client_ref})
        return notify.GatewayResult(accepted=True,
                                    provider_message_id=f"pm-{len(sent)}")

    monkeypatch.setattr(notify, "send", fake_send)
    monkeypatch.setattr(notify_service.notify, "send", fake_send)

    # A configured gateway with registered templates -- otherwise every row
    # is `skipped` and the interesting paths never run.
    for key, value in {
        "HOSPITAL_GATEWAY_URL": "https://gw.example/send",
        "HOSPITAL_GATEWAY_API_KEY": "test-key",
        "HOSPITAL_GATEWAY_SENDER_ID": "KCDIAG",
        "HOSPITAL_GATEWAY_ENTITY_ID": "1234567890",
        "HOSPITAL_GATEWAY_DLR_TOKEN": "test-token",
        "HOSPITAL_GATEWAY_TEMPLATE_BOOKED": "1707100000000000001",
        "HOSPITAL_GATEWAY_TEMPLATE_RESCHEDULED": "1707100000000000002",
        "HOSPITAL_GATEWAY_TEMPLATE_CANCELLED": "1707100000000000003",
    }.items():
        monkeypatch.setenv(key, value)

    # Template ids are read at module import, so the registry has to be
    # rebuilt now that the environment carries them.
    monkeypatch.setattr(mt, "TEMPLATES", {
        event: _registered(event, os.environ[f"HOSPITAL_GATEWAY_TEMPLATE_{event.upper()}"])
        for event in mt.ALL_EVENTS
    })

    with TestClient(clinic_main.app) as c:
        # The scratch database is shared by every test in this file, and
        # startup only seeds an EMPTY one. Clearing the two tables this
        # suite writes -- and only those -- gives each test the same
        # starting point without re-seeding 32 doctors each time, and keeps
        # "the first free slot" a meaningful phrase in _book().
        from db import SessionLocal
        from models import Appointment, NotificationAttempt
        db = SessionLocal()
        try:
            db.query(NotificationAttempt).delete()
            db.query(Appointment).delete()
            db.commit()
        finally:
            db.close()

        c.sent = sent                                   # type: ignore[attr-defined]
        yield c


def _a_bookable_slot(client) -> tuple[str, str, str]:
    """-> (doctor_name, iso_date, time_slot) for a doctor who actually sits
    that day, found through the same availability endpoint the voice agent
    uses rather than by reaching into the seed data."""
    for offset in range(0, 14):
        date = (datetime.date.today() + datetime.timedelta(days=offset)).isoformat()
        r = client.get("/api/v1/doctors/by-department",
                       params={"department": "Cardiology", "date": date}).json()
        if r.get("found") and r["doctors"]:
            doctor = r["doctors"][0]
            start = doctor["chamber_hours"].split("-")[0]
            return doctor["name"], date, start
    pytest.skip("no seeded doctor sits in the next fortnight")


def _book(client, patient="Riya Das", phone="9876543210"):
    name, date, slot = _a_bookable_slot(client)
    r = client.post("/api/v1/appointments", json={
        "doctor_name": name, "date": date, "time_slot": slot,
        "patient_name": patient, "phone": phone,
    })
    return r.json(), (name, date, slot)


def test_booking_queues_a_message_and_the_background_task_sends_it(client):
    body, _ = _book(client)
    assert body["success"] is True

    # The response reports what is OWED, not what was delivered.
    assert body["notification"]["event"] == mt.EVENT_BOOKED
    assert body["notification"]["status"] == notify.STATUS_QUEUED

    # TestClient runs the BackgroundTask before returning, so by now the
    # gateway has been called and the row has moved on.
    assert len(client.sent) == 1
    assert client.sent[0]["to"] == "919876543210"
    assert body["confirmation_id"] in client.sent[0]["text"]
    assert client.sent[0]["template_id"] == "1707100000000000001"

    ledger = client.get(
        f"/api/v1/appointments/{body['confirmation_id']}/notifications").json()
    assert ledger["count"] == 1
    assert ledger["notifications"][0]["status"] == notify.STATUS_SENT
    assert ledger["notifications"][0]["provider_message_id"] == "pm-1"


def test_the_stored_body_is_the_exact_text_submitted(client):
    """Reception's question is "what were they sent?", and the answer has to
    be the message itself, not one re-rendered through today's template."""
    body, _ = _book(client)
    ledger = client.get(
        f"/api/v1/appointments/{body['confirmation_id']}/notifications").json()
    assert ledger["notifications"][0]["body"] == client.sent[0]["text"]


def test_a_delivery_receipt_marks_the_row_delivered(client):
    body, _ = _book(client)
    r = client.post("/api/v1/notifications/receipt",
                    headers={"x-gateway-token": "test-token"},
                    json={"status": "DELIVERED", "provider_message_id": "pm-1"})
    assert r.json()["accepted"] is True
    assert r.json()["status"] == notify.STATUS_DELIVERED

    ledger = client.get(
        f"/api/v1/appointments/{body['confirmation_id']}/notifications").json()
    assert ledger["notifications"][0]["delivered_at"] is not None


def test_a_receipt_can_be_matched_by_client_ref_when_the_provider_id_was_lost(client):
    """If the gateway's HTTP response never reached us we accepted the send
    without learning its id. client_ref -- our own ledger row id, echoed
    back -- is then the only way its receipt finds the row."""
    body, _ = _book(client)
    attempt_id = body["notification"]["id"]
    r = client.post("/api/v1/notifications/receipt",
                    headers={"x-gateway-token": "test-token"},
                    json={"status": "delivered", "client_ref": str(attempt_id)})
    assert r.json() == {"accepted": True, "id": attempt_id,
                        "status": notify.STATUS_DELIVERED}


def test_a_late_duplicate_receipt_cannot_reopen_a_delivered_row(client):
    """Operators re-send receipts, out of order. A stale 'enroute' arriving
    after 'delivered' must not push a delivered message back into the staff
    queue."""
    _book(client)
    hdr = {"x-gateway-token": "test-token"}
    client.post("/api/v1/notifications/receipt", headers=hdr,
                json={"status": "DELIVERED", "provider_message_id": "pm-1"})
    client.post("/api/v1/notifications/receipt", headers=hdr,
                json={"status": "enroute", "provider_message_id": "pm-1"})
    failures = client.get("/api/v1/notifications/failures",
                          params={"stale_minutes": 1}).json()
    assert failures["count"] == 0


def test_a_receipt_with_the_wrong_token_is_rejected(client):
    _book(client)
    r = client.post("/api/v1/notifications/receipt",
                    headers={"x-gateway-token": "not-the-token"},
                    json={"status": "DELIVERED", "provider_message_id": "pm-1"})
    assert r.json() == {"accepted": False, "reason": "unauthorized"}


def test_an_unmatched_receipt_answers_200_rather_than_looping_the_gateway(client):
    r = client.post("/api/v1/notifications/receipt",
                    headers={"x-gateway-token": "test-token"},
                    json={"status": "DELIVERED", "provider_message_id": "nope"})
    assert r.status_code == 200
    assert r.json()["accepted"] is False


def test_a_rejected_message_reaches_the_staff_queue(client, monkeypatch):
    def refuse(config, *, to, text, tpl, client_ref):
        return notify.GatewayResult(accepted=False, error_code="dnd_blocked",
                                    error_detail="number on DND")
    monkeypatch.setattr(notify_service.notify, "send", refuse)

    body, _ = _book(client, patient="Ashok Roy", phone="9000000001")
    failures = client.get("/api/v1/notifications/failures").json()
    assert failures["count"] == 1
    row = failures["failures"][0]
    assert row["confirmation_id"] == body["confirmation_id"]
    assert row["error_code"] == "dnd_blocked"
    # The body is in the queue so staff can read the patient what they
    # should have received.
    assert body["confirmation_id"] in row["body"]


def test_an_accepted_message_with_no_receipt_becomes_a_failure_once_stale(client):
    """THE SILENT CASE, and the reason this endpoint exists. Nothing
    errored, nothing was logged, and without the staleness rule the patient
    simply arrives at reception with nothing."""
    body, _ = _book(client)
    assert client.get("/api/v1/notifications/failures").json()["count"] == 0

    from models import NotificationAttempt
    from db import SessionLocal
    db = SessionLocal()
    try:
        row = db.get(NotificationAttempt, body["notification"]["id"])
        assert row.status == notify.STATUS_SENT
        row.updated_at = datetime.datetime.now() - datetime.timedelta(hours=2)
        db.commit()
    finally:
        db.close()

    failures = client.get("/api/v1/notifications/failures").json()
    assert failures["count"] == 1
    assert failures["failures"][0]["status"] == notify.STATUS_SENT


def test_acknowledging_a_failure_clears_the_queue_without_rewriting_the_status(client,
                                                                              monkeypatch):
    monkeypatch.setattr(notify_service.notify, "send",
                        lambda *a, **k: notify.GatewayResult(accepted=False,
                                                             error_code="transport"))
    body, _ = _book(client, patient="Mita Kar", phone="9000000002")
    attempt_id = body["notification"]["id"]

    r = client.post(f"/api/v1/notifications/{attempt_id}/acknowledge",
                    json={"staff": "reception-2"}).json()
    assert r["success"] is True
    assert r["notification"]["acknowledged_by"] == "reception-2"
    # The failure is still a failure in the record.
    assert r["notification"]["status"] == notify.STATUS_FAILED
    assert client.get("/api/v1/notifications/failures").json()["count"] == 0


def test_retry_resends_the_stored_body(client, monkeypatch):
    monkeypatch.setattr(notify_service.notify, "send",
                        lambda *a, **k: notify.GatewayResult(accepted=False,
                                                             error_code="transport"))
    body, _ = _book(client, patient="Sujata Bose", phone="9000000003")
    attempt_id = body["notification"]["id"]

    resent: list[str] = []

    def accept(config, *, to, text, tpl, client_ref):
        resent.append(text)
        return notify.GatewayResult(accepted=True, provider_message_id="pm-retry")
    monkeypatch.setattr(notify_service.notify, "send", accept)

    r = client.post(f"/api/v1/notifications/{attempt_id}/retry").json()
    assert r["success"] is True
    assert len(resent) == 1
    assert body["confirmation_id"] in resent[0]
    assert client.get("/api/v1/notifications/failures").json()["count"] == 0


def test_rescheduling_keeps_the_reference_and_sends_a_second_message(client):
    body, (doctor, date, slot) = _book(client, patient="Nabin Pal", phone="9000000004")
    cid = body["confirmation_id"]

    availability = client.get("/api/v1/doctors/availability",
                              params={"name": doctor, "date": date}).json()
    start, end = availability["chamber_hours"].split("-")
    later = (datetime.datetime.strptime(start, "%H:%M")
             + datetime.timedelta(minutes=15)).strftime("%H:%M")
    assert later < end

    r = client.post(f"/api/v1/appointments/{cid}/reschedule",
                    json={"date": date, "time_slot": later}).json()
    assert r["success"] is True
    # The story's point: the patient's reference does not change under them.
    assert r["confirmation_id"] == cid
    assert r["previous_time_slot"] == slot
    assert r["notification"]["event"] == mt.EVENT_RESCHEDULED

    assert len(client.sent) == 2
    assert cid in client.sent[1]["text"]

    # And the slot it vacated is bookable again.
    again = client.post("/api/v1/appointments", json={
        "doctor_name": doctor, "date": date, "time_slot": slot,
        "patient_name": "Someone Else", "phone": "9000000005",
    }).json()
    assert again["success"] is True


def test_cancelling_frees_the_slot_for_somebody_else(client):
    """The slot_lock rule, end to end. Before it, a cancelled row went on
    occupying its slot under the unique constraint and nobody could ever
    book the slot it had released."""
    body, (doctor, date, slot) = _book(client, patient="Tapan Nandi", phone="9000000006")
    cid = body["confirmation_id"]

    r = client.post(f"/api/v1/appointments/{cid}/cancel",
                    json={"reason": "patient unwell"}).json()
    assert r["success"] is True
    assert r["already_cancelled"] is False
    assert r["notification"]["event"] == mt.EVENT_CANCELLED

    again = client.post("/api/v1/appointments", json={
        "doctor_name": doctor, "date": date, "time_slot": slot,
        "patient_name": "Bela Mitra", "phone": "9000000007",
    }).json()
    assert again["success"] is True
    assert again["confirmation_id"] != cid


def test_cancelling_twice_does_not_message_the_patient_again(client):
    body, _ = _book(client, patient="Arun Ghosh", phone="9000000008")
    cid = body["confirmation_id"]
    client.post(f"/api/v1/appointments/{cid}/cancel", json={})
    before = len(client.sent)

    second = client.post(f"/api/v1/appointments/{cid}/cancel", json={}).json()
    assert second["success"] is True
    assert second["already_cancelled"] is True
    assert len(client.sent) == before


def test_a_cancelled_appointment_cannot_be_rescheduled(client):
    body, (doctor, date, slot) = _book(client, patient="Iti Sen", phone="9000000009")
    cid = body["confirmation_id"]
    client.post(f"/api/v1/appointments/{cid}/cancel", json={})
    r = client.post(f"/api/v1/appointments/{cid}/reschedule",
                    json={"date": date, "time_slot": slot}).json()
    assert r["success"] is False
    assert r["reason"] == "appointment_cancelled"


def test_health_reports_the_ledger_and_a_clean_schema(client):
    _book(client)
    h = client.get("/api/health").json()
    assert h["gateway_configured"] is True
    assert h["schema_warning"] is None
    assert h["notifications"]["sent"] >= 1
    assert "open_failures" in h["notifications"]


def test_an_unknown_confirmation_id_is_reported_not_crashed(client):
    for path, payload in ((f"/api/v1/appointments/KCD-NOPE/cancel", {}),
                          (f"/api/v1/appointments/KCD-NOPE/reschedule",
                           {"date": "2026-09-14", "time_slot": "18:00"})):
        r = client.post(path, json=payload).json()
        assert r["success"] is False
        assert r["reason"] == "appointment_not_found"


# ---------------------------------------------------------------------------
# Regression tests for three defects found auditing this work against the
# acceptance criteria. Each one failed before the fix that follows it.
# ---------------------------------------------------------------------------
def test_a_long_patient_name_still_gets_a_message(client):
    """A perfectly ordinary Bengali name can exceed DLT's 30-character
    variable limit -- main.py's _clean_patient_name() hands over whatever
    the caller said. Before the fix, render() raised, the row was recorded
    as `failed`, and the patient got nothing because their name was long.
    """
    long_name = "রিয়া দাস চ্যাটার্জী মুখোপাধ্যায়"
    assert len(long_name) > mt.VAR_MAX_CHARS

    body, _ = _book(client, patient=long_name, phone="9000003333")
    assert body["success"] is True
    assert body["notification"]["status"] == notify.STATUS_QUEUED
    assert body["notification"]["error_code"] is None

    # The message went, with the name trimmed to fit rather than dropped.
    assert len(client.sent) == 1
    assert body["confirmation_id"] in client.sent[0]["text"]
    assert long_name[:mt.VAR_MAX_CHARS].strip() in client.sent[0]["text"]


def test_a_retry_submits_the_template_id_the_message_was_composed_under(client,
                                                                        monkeypatch):
    """`body` is stored and re-sent verbatim, so the template id submitted
    with it must be the one recorded at compose time. Before the fix,
    deliver_now() read the id from the LIVE registry: a retry after the
    templates were re-registered paired an old body with a new id, which is
    exactly the mismatch the operator rejects -- silently.
    """
    body, _ = _book(client, patient="Dipa Roy", phone="9000004444")
    composed_id = client.sent[0]["template_id"]
    assert composed_id == "1707100000000000001"

    # The hospital re-registers its templates; the registry now holds new ids.
    monkeypatch.setattr(mt, "TEMPLATES", {
        event: _registered(event, "9999999999999999999") for event in mt.ALL_EVENTS
    })

    r = client.post(f"/api/v1/notifications/{body['notification']['id']}/retry").json()
    assert r["success"] is True
    assert len(client.sent) == 2
    assert client.sent[1]["template_id"] == composed_id


def test_a_failing_second_commit_cannot_lose_the_ledger_row(client, monkeypatch):
    """THE "SILENTLY DROPPED" CASE, reproduced.

    The ledger row is staged in the SAME transaction as the appointment.
    When it was instead committed separately, AFTER the booking, a failure
    of that second commit -- `database is locked` is entirely reachable
    here, db.py sets busy_timeout to 3s precisely because contention is
    expected -- left a committed booking with NO ledger row at all. No
    staff queue entry, nothing owed on record, patient told nothing.

    Measured both ways with the commit made to fail on its second call:

        separate commits : booking success=True, ledger rows = 0   <- dropped
        one transaction  : booking success=True, ledger rows = 1

    The second commit that fails here is the background sender's, which is
    the point -- the ledger row is already durable by then.
    """
    import sqlalchemy.exc
    import sqlalchemy.orm

    real_commit = sqlalchemy.orm.Session.commit
    calls = {"n": 0}

    def flaky(self):
        calls["n"] += 1
        if calls["n"] == 2:
            raise sqlalchemy.exc.OperationalError("database is locked", None, None)
        return real_commit(self)

    monkeypatch.setattr(sqlalchemy.orm.Session, "commit", flaky)
    body, _ = _book(client, patient="Race Victim", phone="9000009999")
    monkeypatch.undo()

    assert body["success"] is True
    assert calls["n"] >= 2, "the second commit never happened -- test proves nothing"

    from models import NotificationAttempt
    from db import SessionLocal
    db = SessionLocal()
    try:
        rows = db.query(NotificationAttempt).filter_by(
            confirmation_id=body["confirmation_id"]).all()
    finally:
        db.close()
    assert len(rows) == 1, "a committed booking was left with nothing owed on record"


def test_a_booking_that_loses_the_slot_race_leaves_no_orphan_ledger_row(client,
                                                                        monkeypatch):
    """The other half of sharing a transaction: a rollback discards both.

    An orphan row would promise a message for an appointment that was never
    made, and would sit in the staff queue forever because nothing can ever
    deliver it. NOTE this holds under the old ordering too -- the early
    return on IntegrityError got there first -- so this is an invariant
    worth pinning, not a reproduction of a past defect. The reproduction is
    the test above.
    """
    body, (doctor, date, slot) = _book(client, patient="Jaya Sen", phone="9000005555")
    assert body["success"] is True

    from models import NotificationAttempt
    from db import SessionLocal

    def count_rows() -> int:
        db = SessionLocal()
        try:
            return db.query(NotificationAttempt).count()
        finally:
            db.close()

    assert count_rows() == 1

    # Force the pre-check to pass so the insert itself collides -- the same
    # interleaving two simultaneous callers produce.
    clinic_main = _load_clinic_api_main()
    monkeypatch.setattr(clinic_main, "_live_slots", lambda *a, **k: set())

    second = client.post("/api/v1/appointments", json={
        "doctor_name": doctor, "date": date, "time_slot": slot,
        "patient_name": "Second Caller", "phone": "9000005556",
    }).json()
    assert second["success"] is False
    assert count_rows() == 1, "a rolled-back booking left a ledger row behind"


@pytest.fixture()
def bench_client(monkeypatch):
    """A pod with NO gateway configured -- the default state of every
    deployment in this repository today, and the one the story is being
    developed against. It must behave correctly and message nobody."""
    from fastapi.testclient import TestClient
    clinic_main = _load_clinic_api_main()

    for key in ("HOSPITAL_GATEWAY_URL", "HOSPITAL_GATEWAY_API_KEY",
                "HOSPITAL_GATEWAY_SENDER_ID", "HOSPITAL_GATEWAY_ENTITY_ID",
                "HOSPITAL_GATEWAY_DLR_TOKEN"):
        monkeypatch.delenv(key, raising=False)

    def explode(*a, **k):                     # pragma: no cover -- must not run
        raise AssertionError("the gateway was called with no gateway configured")
    monkeypatch.setattr(notify_service.notify, "send", explode)

    with TestClient(clinic_main.app) as c:
        from db import SessionLocal
        from models import Appointment, NotificationAttempt
        db = SessionLocal()
        try:
            db.query(NotificationAttempt).delete()
            db.query(Appointment).delete()
            db.commit()
        finally:
            db.close()
        yield c


def test_with_no_gateway_the_booking_still_succeeds_and_the_row_says_why(bench_client):
    body, _ = _book(bench_client, patient="Bench Patient", phone="9000001111")
    assert body["success"] is True
    assert body["notification"]["status"] == notify.STATUS_SKIPPED
    assert body["notification"]["error_code"] == "gateway_not_configured"

    # Recorded, not discarded -- "we chose not to send" is written down.
    ledger = bench_client.get(
        f"/api/v1/appointments/{body['confirmation_id']}/notifications").json()
    assert ledger["count"] == 1
    assert ledger["notifications"][0]["status"] == notify.STATUS_SKIPPED
    assert ledger["notifications"][0]["body"]        # the text it WOULD have sent

    # ...and it does not flood the staff queue on a bench pod.
    assert bench_client.get("/api/v1/notifications/failures").json()["count"] == 0
    assert bench_client.get(
        "/api/v1/notifications/failures",
        params={"include_skipped": True}).json()["count"] == 1


def test_retrying_a_skipped_row_on_an_unconfigured_pod_still_sends_nothing(bench_client):
    """requeue() puts ANY settled row back to `queued`, including a
    `skipped` one. Without a preflight re-check inside deliver_now(), a
    staff retry on a pod with no gateway would hand an unregistered
    template straight to send() -- the bench fixture asserts if that
    happens.
    """
    body, _ = _book(bench_client, patient="Bench Three", phone="9000003333")
    attempt_id = body["notification"]["id"]

    r = bench_client.post(f"/api/v1/notifications/{attempt_id}/retry").json()
    assert r["success"] is True

    ledger = bench_client.get(
        f"/api/v1/appointments/{body['confirmation_id']}/notifications").json()
    assert ledger["notifications"][0]["status"] == notify.STATUS_SKIPPED
    assert ledger["notifications"][0]["error_code"] == "gateway_not_configured"


def test_with_no_gateway_the_caller_is_not_promised_a_message(bench_client):
    """End to end for the honesty rule: the response says `skipped`, so
    booking_reply() must not tell the caller to expect an SMS."""
    body, _ = _book(bench_client, patient="Bench Two", phone="9000002222")
    reply = booking_reply({"doctor_name": "সেন"}, body)
    assert "মেসেজ" not in reply
    assert body["confirmation_id"] in reply


# ===========================================================================
# The spoken reply -- what the caller is told about the written one
# ===========================================================================
_SPOKEN = {"doctor_name": "সেন"}
_RESULT = {"success": True, "confirmation_id": "KCD-20260914-0031",
           "doctor_name_bn": "সেন", "date": "2026-09-14", "time_slot": "18:15"}


def test_the_caller_is_promised_a_message_only_when_one_is_queued():
    promised = booking_reply(_SPOKEN, dict(_RESULT, notification={"status": "queued"}))
    assert "মেসেজ" in promised
    assert "রিসেপশনে" in promised


@pytest.mark.parametrize("notification", [
    {"status": "skipped", "error_code": "gateway_not_configured"},
    {"status": "failed", "error_code": "template_not_registered"},
    {"status": "not_recorded"},
    None,
])
def test_no_message_is_promised_when_none_is_coming(notification):
    """Promising an SMS that will not arrive is worse than saying nothing:
    the caller stops noting the number down and finds out at the counter."""
    reply = booking_reply(_SPOKEN, dict(_RESULT, notification=notification))
    assert "মেসেজ" not in reply
    # ...and the spoken number is still there, which is where they were
    # before this feature existed.
    assert _RESULT["confirmation_id"] in reply


def test_the_spoken_number_survives_even_when_a_message_is_promised():
    reply = booking_reply(_SPOKEN, dict(_RESULT, notification={"status": "queued"}))
    assert _RESULT["confirmation_id"] in reply


def test_reschedule_reply_says_the_reference_has_not_changed():
    reply = reschedule_reply(_SPOKEN, dict(_RESULT, notification={"status": "queued"}))
    assert "একই" in reply
    assert _RESULT["confirmation_id"] in reply


def test_cancel_reply_reports_an_already_cancelled_appointment_as_a_plain_fact():
    reply = cancel_reply(_SPOKEN, {"success": True, "already_cancelled": True,
                                   "date": "2026-09-14",
                                   "notification": {"status": "not_resent"}})
    assert "বাতিল" in reply
    assert "দুঃখিত" not in reply
    assert "মেসেজ" not in reply
