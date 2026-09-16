"""Tests for "A reminder tonight for tomorrow's fasting test".

Author: Chakravardhan
Story:  "As a patient with a fasting test tomorrow, I want a reminder tonight,
         so that my visit is not wasted."
Criteria: Reminders are sent on a schedule per appointment type, carrying the
          merged preparation instruction. The do-not-disturb registry and quiet
          hours are honoured, and opt-out is permanent and immediate.

WHAT THESE COVER
----------------
  * THE STORY -- a fasting test tomorrow morning gets ONE reminder tonight,
    before the fast begins, with one merged instruction.
  * SCHEDULE PER TYPE -- consultation, test and fasting test each follow their
    own rule; a reminder is never sent after it stops being useful.
  * MERGED PREPARATION -- longest fast wins, instructions once each, never a
    truncated instruction, always within DLT limits and three SMS segments.
  * QUIET HOURS -- nothing sent inside them; moved earlier or later only while
    still useful; a fasting reminder is skipped rather than sent the morning of.
  * DND -- a registered number is not messaged; a stale registry is not honoured.
  * OPT-OUT -- immediate (even a reminder already in the SMS ledger), permanent
    (a new booking from the same number, however written, stays silent).
  * THE LEDGER -- a reminder becomes an ordinary NotificationAttempt; no double
    send; a changed or cancelled appointment cancels its reminder.

The gateway is faked at notifications.send(), the one function that touches
the network. Planning, sending, the ledger and delivery are the real code.

    python -m pytest tests/test_reminders.py -v
"""

from __future__ import annotations

import ast
import dataclasses
import datetime
import importlib
import os
import sys
import tempfile
import uuid

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CLINIC = os.path.join(_ROOT, "clinic-api")

# db.py reads CLINIC_DB_PATH at import. Another test module may already have
# imported it against its own scratch file; only point it somewhere new if not.
if "db" not in sys.modules:
    os.environ["CLINIC_DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="clinic-rem-"), "clinic.db")
    os.environ.pop("DATABASE_URL", None)
    os.environ["VOICE_AGENT_PROVIDER"] = "vast"
sys.path.insert(0, _CLINIC)

# Loaded after the scratch database path is set, which is why these are not
# plain import statements at the top of the file.
mt = importlib.import_module("message_templates")
notify = importlib.import_module("notifications")
notify_service = importlib.import_module("notify_service")
preparation = importlib.import_module("preparation")
rs = importlib.import_module("reminder_service")
_db = importlib.import_module("db")
_models = importlib.import_module("models")
_reminder_models = importlib.import_module("reminder_models")

SessionLocal, engine = _db.SessionLocal, _db.engine
APPT_BOOKED, APPT_CANCELLED, SLOT_LOCK_ACTIVE = (
    _models.APPT_BOOKED,
    _models.APPT_CANCELLED,
    _models.SLOT_LOCK_ACTIVE,
)
Appointment, Base, Doctor, LabTest, NotificationAttempt = (
    _models.Appointment,
    _models.Base,
    _models.Doctor,
    _models.LabTest,
    _models.NotificationAttempt,
)
AppointmentTest = _reminder_models.AppointmentTest
DndRegistration = _reminder_models.DndRegistration
DndScrub = _reminder_models.DndScrub
ReminderOptOut = _reminder_models.ReminderOptOut
ScheduledReminder = _reminder_models.ScheduledReminder

TODAY = datetime.date(2026, 9, 14)
TOMORROW = "2026-09-15"
PHONE = "9000000201"
OTHER_PHONE = "9000000202"
SETTINGS = rs.Settings(enabled=False, tick_s=60)


def at(hhmm: str, day: datetime.date = TODAY) -> datetime.datetime:
    return datetime.datetime.combine(day, datetime.time.fromisoformat(hhmm))


# ===========================================================================
# Fixtures
# ===========================================================================
@pytest.fixture()
def db(monkeypatch):
    """A clean reminder world on the shared scratch database, a fresh (empty)
    DND scrub, a configured gateway with registered reminder templates, and
    the gateway itself faked."""
    Base.metadata.create_all(engine)
    session = SessionLocal()
    if session.query(LabTest).count() == 0:
        from seed import seed

        session.close()
        seed()
        Base.metadata.create_all(engine)
        session = SessionLocal()
    for table in (ScheduledReminder, ReminderOptOut, DndRegistration, DndScrub, AppointmentTest):
        session.query(table).delete()
    session.query(NotificationAttempt).delete()
    session.query(Appointment).delete()
    session.commit()
    rs.import_dnd(session, [], at("09:00"))
    session.commit()

    for key, value in {
        "HOSPITAL_GATEWAY_URL": "https://gateway.example/send",
        "HOSPITAL_GATEWAY_SENDER_ID": "KCDIAG",
        "HOSPITAL_GATEWAY_ENTITY_ID": "1234567890",
    }.items():
        monkeypatch.setenv(key, value)
    registered = dict(mt.TEMPLATES)
    for n, event in enumerate(mt.REMINDER_EVENTS):
        registered[event] = dataclasses.replace(mt.TEMPLATES[event], template_id=f"170710000000000009{n}")
    monkeypatch.setattr(mt, "TEMPLATES", registered)

    sent: list[dict] = []

    def fake_send(config, *, to, text, tpl, client_ref):
        sent.append({"to": to, "text": text, "event": tpl.event, "template_id": tpl.template_id})
        return notify.GatewayResult(accepted=True, provider_message_id=f"pm-{len(sent)}")

    monkeypatch.setattr(notify, "send", fake_send)
    session.sent = sent
    yield session
    session.close()


def book(db, time_slot="08:00", date=TOMORROW, phone=PHONE, tests=(), name="Riya Das") -> Appointment:
    doctor = db.query(Doctor).first()
    appt = Appointment(
        confirmation_id=f"KCD-REM-{uuid.uuid4().hex[:6].upper()}",
        doctor_id=doctor.id,
        date=date,
        time_slot=time_slot,
        patient_name=name,
        phone=phone,
        created_at=at("10:00"),
        status=APPT_BOOKED,
        slot_lock=SLOT_LOCK_ACTIVE,
    )
    db.add(appt)
    db.commit()
    if tests:
        rs.order_tests(db, appt.confirmation_id, list(tests), at("10:00"))
        db.commit()
    return appt


def tick(db, now, settings=SETTINGS) -> dict:
    db.expire_all()
    result = {"planned": rs.plan(db, now, settings), "sent": rs.send_due(db, now, settings)}
    db.expire_all()
    return result


def reminders(db, appt) -> list[ScheduledReminder]:
    db.expire_all()
    return db.query(ScheduledReminder).filter_by(confirmation_id=appt.confirmation_id).all()


# ===========================================================================
# THE STORY
# ===========================================================================
def test_a_fasting_test_tomorrow_gets_one_reminder_tonight_before_the_fast(db):
    appt = book(db, "08:00", tests=("Blood Sugar Fasting", "Lipid Profile"))

    tick(db, at("11:00"))
    (reminder,) = reminders(db, appt)
    assert reminder.appointment_type == rs.TYPE_FASTING_TEST
    assert reminder.due_at == at("19:00")  # tonight
    assert reminder.useful_until == at("20:00")  # 12-hour fast before 08:00
    assert db.sent == []

    tick(db, at("18:59"))
    assert db.sent == []

    tick(db, at("19:00"))
    assert len(db.sent) == 1  # one message, not one per test
    text = db.sent[0]["text"]
    assert "20:00 থেকে শুধু জল খাবেন।" in text
    assert "আগের ২৪ ঘণ্টা মদ্যপান নয়।" in text
    assert appt.confirmation_id in text and db.sent[0]["event"] == mt.EVENT_REMINDER_TEST
    assert reminders(db, appt)[0].status == rs.R_HANDED_OFF


def test_the_reminder_is_an_ordinary_ledger_row(db):
    appt = book(db, "08:00", tests=("Blood Sugar Fasting",))
    tick(db, at("11:00"))
    tick(db, at("19:00"))
    reminder = reminders(db, appt)[0]
    attempt = db.get(NotificationAttempt, reminder.attempt_id)
    assert attempt.status == notify.STATUS_SENT
    assert attempt.event == mt.EVENT_REMINDER_TEST
    assert attempt.phone == "91" + PHONE
    assert attempt.body == db.sent[0]["text"]


def test_two_ticks_at_the_same_moment_send_once(db):
    book(db, "08:00", tests=("Blood Sugar Fasting",))
    tick(db, at("11:00"))
    tick(db, at("19:00"))
    tick(db, at("19:00"))
    assert len(db.sent) == 1
    assert db.query(NotificationAttempt).count() == 1


# ===========================================================================
# SCHEDULE PER APPOINTMENT TYPE
# ===========================================================================
def test_each_appointment_type_follows_its_own_schedule(db):
    visit = book(db, "11:00")
    test = book(db, "09:00", tests=("Urine Routine Examination",))
    fasting = book(db, "08:30", tests=("Blood Sugar Fasting",))
    tick(db, at("10:30"))

    (v,), (t,), (f,) = reminders(db, visit), reminders(db, test), reminders(db, fasting)
    tomorrow_9 = at("09:00", TODAY + datetime.timedelta(days=1))
    assert (v.appointment_type, v.due_at, v.useful_until) == (rs.TYPE_CONSULTATION, at("18:00"), tomorrow_9)
    assert (t.appointment_type, t.due_at) == (rs.TYPE_TEST, at("19:00"))
    assert (f.appointment_type, f.due_at, f.useful_until) == (rs.TYPE_FASTING_TEST, at("19:00"), at("22:30"))


def test_an_early_fast_moves_the_reminder_earlier(db):
    """A 12-hour fast before a 07:00 test starts at 19:00 -- the reminder goes an hour before."""
    appt = book(db, "07:00", tests=("Lipid Profile",))
    tick(db, at("10:00"))
    (reminder,) = reminders(db, appt)
    assert reminder.due_at == at("18:00") and reminder.useful_until == at("19:00")


def test_a_consultation_reminder_carries_the_visit_template(db):
    appt = book(db, "11:00")
    tick(db, at("10:00"))
    tick(db, at("18:00"))
    assert db.sent[0]["event"] == mt.EVENT_REMINDER_VISIT
    assert mt.prep_phrase(mt.PREP_BRING_REPORTS) in db.sent[0]["text"]
    assert appt.confirmation_id in db.sent[0]["text"]


def test_a_booking_made_after_the_fast_began_is_not_reminded(db):
    appt = book(db, "08:00", tests=("Lipid Profile",))
    tick(db, at("20:30"))
    (reminder,) = reminders(db, appt)
    assert (reminder.status, reminder.reason) == (rs.R_SKIPPED, rs.REASON_TOO_LATE)
    assert db.sent == []


def test_a_booking_made_after_the_usual_time_is_reminded_at_once_if_still_useful(db):
    appt = book(db, "08:00", tests=("Blood Sugar Fasting",))  # fast from 22:00
    tick(db, at("19:30"))
    assert len(db.sent) == 1
    assert reminders(db, appt)[0].status == rs.R_HANDED_OFF


# ===========================================================================
# MERGED PREPARATION
# ===========================================================================
def test_the_longest_fast_wins_and_instructions_appear_once():
    merged = preparation.merge(
        ["Blood Sugar Fasting", "Lipid Profile", "Liver Function Test (LFT)", "Lipid Profile"],
        at("08:00", TODAY + datetime.timedelta(days=1)),
    )
    assert merged.fasting_hours == 12
    assert merged.fast_from == at("20:00")
    assert merged.instructions == (mt.PREP_NO_ALCOHOL,)


def test_instructions_from_different_tests_are_merged_in_a_fixed_order():
    merged = preparation.merge(
        ["Urine Routine Examination", "USG Pregnancy Profile", "Lipid Profile"], at("09:00")
    )
    assert merged.instructions == (mt.PREP_NO_ALCOHOL, mt.PREP_FULL_BLADDER, mt.PREP_FIRST_URINE)
    first, more = merged.test_variables()
    assert first == mt.prep_phrase(mt.PREP_FAST_FROM, time="21:00")
    assert more == mt.prep_phrase(mt.PREP_MORE_AT_COUNTER)  # never a truncated instruction


def test_tests_that_need_nothing_say_so():
    merged = preparation.merge(["Complete Blood Count (CBC)"], at("09:00"))
    assert not merged.needs_fasting and merged.unknown_tests == ("Complete Blood Count (CBC)",)
    assert merged.test_variables() == (mt.prep_phrase(mt.PREP_NONE), mt.prep_phrase(mt.PREP_THANKS))


def test_every_preparation_phrase_fits_one_dlt_variable():
    for code, phrase in mt.PREP_PHRASES.items():
        filled = phrase.format(time="20:00") if "{time}" in phrase else phrase
        assert 0 < len(filled) <= mt.VAR_MAX_CHARS, code


@pytest.mark.parametrize("event", mt.REMINDER_EVENTS)
def test_a_worst_case_reminder_stays_within_three_segments(event):
    values = {
        "patient_name": "ক" * mt.VAR_MAX_CHARS,
        "date": TOMORROW,
        "time_slot": "08:00",
        "doctor_name": "ক" * mt.VAR_MAX_CHARS,
        "preparation": mt.prep_phrase(mt.PREP_MORE_AT_COUNTER),
        "preparation_more": mt.prep_phrase(mt.PREP_MORE_AT_COUNTER),
        "confirmation_id": "KCD-20260915-AB12",
    }
    text, tpl = mt.render(event, values)
    assert mt.estimated_segments(text) <= 3
    assert tpl.category == "transactional" and tpl.marker_count == len(tpl.variables)


def test_reminder_events_are_not_booking_events():
    assert not set(mt.REMINDER_EVENTS) & set(mt.ALL_EVENTS)


# ===========================================================================
# QUIET HOURS
# ===========================================================================
def test_quiet_hours_cross_midnight():
    assert rs.in_quiet_hours(at("21:00"), SETTINGS)
    assert rs.in_quiet_hours(at("02:00"), SETTINGS)
    assert rs.in_quiet_hours(at("07:59"), SETTINGS)
    assert not rs.in_quiet_hours(at("08:00"), SETTINGS)
    assert not rs.in_quiet_hours(at("20:59"), SETTINGS)


def test_nothing_is_sent_inside_quiet_hours_and_a_useful_reminder_waits_for_morning(db):
    appt = book(db, "11:00")  # consultation, useful until 09:00 tomorrow
    tick(db, at("21:30"))  # booked late at night
    (reminder,) = reminders(db, appt)
    assert reminder.due_at == at("08:00", TODAY + datetime.timedelta(days=1))
    tick(db, at("23:00"))
    tick(db, at("06:00", TODAY + datetime.timedelta(days=1)))
    assert db.sent == []
    tick(db, at("08:00", TODAY + datetime.timedelta(days=1)))
    assert len(db.sent) == 1


def test_a_fasting_reminder_is_skipped_rather_than_sent_the_morning_of(db):
    appt = book(db, "10:00", tests=("USG Whole Abdomen",))  # fast from 04:00
    tick(db, at("21:15"))
    (reminder,) = reminders(db, appt)
    assert (reminder.status, reminder.reason) == (rs.R_SKIPPED, rs.REASON_QUIET_HOURS)
    tick(db, at("08:00", TODAY + datetime.timedelta(days=1)))
    assert db.sent == []


def test_a_due_time_inside_quiet_hours_is_moved_earlier_the_same_evening(db):
    early_quiet = dataclasses.replace(SETTINGS, quiet_start=datetime.time(18, 30))
    appt = book(db, "08:00", tests=("Blood Sugar Fasting",))
    tick(db, at("11:00"), early_quiet)
    assert reminders(db, appt)[0].due_at == at("18:29")
    tick(db, at("18:29"), early_quiet)
    assert len(db.sent) == 1


def test_quiet_hours_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("CLINIC_QUIET_HOURS", "22:00-07:30")
    settings = rs.Settings.from_env()
    assert (settings.quiet_start, settings.quiet_end) == (datetime.time(22, 0), datetime.time(7, 30))
    monkeypatch.setenv("CLINIC_QUIET_HOURS", "nonsense")
    assert rs.Settings.from_env().quiet_start == datetime.time(21, 0)


# ===========================================================================
# DO-NOT-DISTURB
# ===========================================================================
@pytest.mark.parametrize(("preference", "blocked"), [("0", True), ("1,4", True), ("", True), ("1,2", False)])
def test_the_dnd_registry_is_honoured_for_health_messages(db, preference, blocked):
    rs.import_dnd(db, [("+91 " + PHONE, preference)], at("09:00"))
    db.commit()
    appt = book(db, "08:00", tests=("Blood Sugar Fasting",))
    tick(db, at("11:00"))
    tick(db, at("19:00"))
    reminder = reminders(db, appt)[0]
    if blocked:
        assert (reminder.status, reminder.reason) == (rs.R_SUPPRESSED, rs.REASON_DND)
        assert db.sent == []
    else:
        assert len(db.sent) == 1


def test_a_number_registered_after_planning_is_still_not_messaged(db):
    appt = book(db, "08:00", tests=("Blood Sugar Fasting",))
    tick(db, at("11:00"))
    rs.import_dnd(db, [(PHONE, "0")], at("15:00"))
    db.commit()
    tick(db, at("19:00"))
    assert reminders(db, appt)[0].status == rs.R_SUPPRESSED and db.sent == []


def test_a_stale_registry_is_not_honoured_so_reminders_wait_then_skip(db):
    db.query(DndScrub).update({DndScrub.imported_at: at("09:00", TODAY - datetime.timedelta(days=8))})
    db.commit()
    appt = book(db, "08:00", tests=("Blood Sugar Fasting",))  # useful until 22:00
    tick(db, at("11:00"))
    tick(db, at("19:00"))
    reminder = reminders(db, appt)[0]
    assert (reminder.status, reminder.reason) == (rs.R_SCHEDULED, rs.REASON_DND_STALE)
    assert db.sent == []

    rs.import_dnd(db, [], at("19:30"))  # the scrub arrives in time
    db.commit()
    tick(db, at("19:31"))
    assert len(db.sent) == 1


def test_a_registry_that_never_arrives_skips_the_reminder_at_its_deadline(db):
    db.query(DndScrub).delete()
    db.commit()
    appt = book(db, "08:00", tests=("Blood Sugar Fasting",))
    tick(db, at("11:00"))
    for hhmm in ("19:00", "20:00", "21:59"):
        tick(db, at(hhmm))
    reminder = reminders(db, appt)[0]
    assert (reminder.status, reminder.reason) == (rs.R_SKIPPED, rs.REASON_DND_STALE)
    assert db.sent == []


def test_a_scrub_replaces_the_previous_one(db):
    rs.import_dnd(db, [(PHONE, "0")], at("09:00"))
    rs.import_dnd(db, [(OTHER_PHONE, "0")], at("10:00"))
    db.commit()
    assert not rs.dnd_blocks(db, PHONE) and rs.dnd_blocks(db, OTHER_PHONE)


# ===========================================================================
# OPT-OUT -- permanent and immediate
# ===========================================================================
def test_opting_out_stops_a_planned_reminder_at_once(db):
    appt = book(db, "08:00", tests=("Blood Sugar Fasting",))
    tick(db, at("11:00"))
    rs.opt_out(db, PHONE, "counter", at("12:00"))
    db.commit()
    reminder = reminders(db, appt)[0]
    assert (reminder.status, reminder.reason) == (rs.R_SUPPRESSED, rs.REASON_OPTED_OUT)  # before any tick
    tick(db, at("19:00"))
    assert db.sent == []


def test_opting_out_stops_a_reminder_already_in_the_sms_ledger(db):
    appt = book(db, "08:00", tests=("Blood Sugar Fasting",))
    tick(db, at("11:00"))
    rs.send_due(db, at("19:00"), SETTINGS, deliver=lambda attempt_id: None)  # queued, not yet sent
    reminder = reminders(db, appt)[0]
    attempt_id = reminder.attempt_id
    assert db.get(NotificationAttempt, attempt_id).status == notify.STATUS_QUEUED

    rs.opt_out(db, PHONE, "phone_call", at("19:00"))
    db.commit()
    notify_service.deliver_now(attempt_id)
    db.expire_all()
    attempt = db.get(NotificationAttempt, attempt_id)
    assert (attempt.status, attempt.error_code) == (notify.STATUS_SKIPPED, rs.REASON_OPTED_OUT)
    assert db.sent == []


def test_an_opt_out_is_permanent_across_new_bookings_and_number_formats(db):
    rs.opt_out(db, "+91-" + PHONE, "counter", at("09:00"))
    db.commit()
    later = book(db, "08:00", date="2026-09-20", tests=("Blood Sugar Fasting",))
    tick(db, at("19:00", datetime.date(2026, 9, 19)))
    assert reminders(db, later)[0].reason == rs.REASON_OPTED_OUT
    assert db.sent == []


def test_opting_out_twice_keeps_the_first_record(db):
    rs.opt_out(db, PHONE, "counter", at("09:00"))
    db.commit()
    rs.opt_out(db, PHONE, "sms_reply", at("10:00"))
    db.commit()
    (row,) = db.query(ReminderOptOut).all()
    assert (row.source, row.opted_out_at) == ("counter", at("09:00"))


def test_an_opt_out_needs_a_real_number(db):
    with pytest.raises(ValueError):
        rs.opt_out(db, "12345", "counter", at("09:00"))


def test_nothing_in_clinic_api_can_undo_an_opt_out():
    """Structural: no code deletes or updates the opt-out table, and a catalogue
    reseed cannot drop it because seed.py never imports the reminder tables."""
    for name in os.listdir(_CLINIC):
        if name.endswith(".py"):
            source = open(os.path.join(_CLINIC, name), encoding="utf-8").read()
            assert "ReminderOptOut).delete" not in source, name
            assert "ReminderOptOut).update" not in source, name
    seed_tree = ast.parse(open(os.path.join(_CLINIC, "seed.py"), encoding="utf-8").read())
    imported = {n.module for n in ast.walk(seed_tree) if isinstance(n, ast.ImportFrom)}
    assert not imported & {"reminder_models", "reminder_service"}


def test_the_counter_command_records_an_opt_out_without_echoing_the_number(db, capsys):
    import reminders

    assert reminders.main(["opt-out", PHONE, "--source", "counter"]) == 0
    out = capsys.readouterr().out
    assert PHONE not in out and PHONE[-4:] in out
    db.expire_all()
    assert rs.is_opted_out(db, PHONE)


# ===========================================================================
# APPOINTMENT CHANGES
# ===========================================================================
def test_a_cancelled_appointment_cancels_its_reminder(db):
    appt = book(db, "08:00", tests=("Blood Sugar Fasting",))
    tick(db, at("11:00"))
    appt.status = APPT_CANCELLED
    appt.slot_lock = appt.confirmation_id
    db.commit()
    tick(db, at("19:00"))
    assert reminders(db, appt)[0].reason == rs.REASON_APPT_CANCELLED
    assert db.sent == []


def test_a_rescheduled_appointment_gets_a_new_reminder_and_the_old_one_is_cancelled(db):
    appt = book(db, "08:00", tests=("Blood Sugar Fasting",))
    tick(db, at("11:00"))
    appt.date = "2026-09-17"
    db.commit()
    tick(db, at("12:00"))
    rows = {r.status: r for r in reminders(db, appt)}
    assert rows[rs.R_CANCELLED].reason == rs.REASON_APPT_CHANGED
    assert rows[rs.R_SCHEDULED].due_at == at("19:00", datetime.date(2026, 9, 16))
    tick(db, at("19:00"))
    assert db.sent == []  # nothing for the old date


def test_tests_ordered_later_change_the_type_and_the_instruction(db):
    appt = book(db, "08:00")
    tick(db, at("10:00"))
    assert reminders(db, appt)[0].appointment_type == rs.TYPE_CONSULTATION
    rs.order_tests(db, appt.confirmation_id, ["Lipid Profile", "Lipid Profile"], at("12:00"))
    db.commit()
    tick(db, at("12:01"))
    types_ = {r.status: r.appointment_type for r in reminders(db, appt)}
    assert types_ == {rs.R_CANCELLED: rs.TYPE_CONSULTATION, rs.R_SCHEDULED: rs.TYPE_FASTING_TEST}


# ===========================================================================
# RUNNING IT
# ===========================================================================
def test_a_bench_pod_with_no_gateway_records_the_reminder_and_sends_nothing(db, monkeypatch):
    monkeypatch.delenv("HOSPITAL_GATEWAY_URL", raising=False)
    appt = book(db, "08:00", tests=("Blood Sugar Fasting",))
    tick(db, at("11:00"))
    tick(db, at("19:00"))
    reminder = reminders(db, appt)[0]
    assert reminder.status == rs.R_HANDED_OFF
    assert db.get(NotificationAttempt, reminder.attempt_id).status == notify.STATUS_SKIPPED
    assert db.sent == []


def test_the_loop_is_off_unless_switched_on(monkeypatch):
    monkeypatch.delenv("CLINIC_REMINDERS_ENABLED", raising=False)
    assert rs.start_background() is False


def test_health_reports_reminders(db):
    summary = rs.summary(db, SETTINGS)
    assert summary["enabled"] is False and summary["running"] is False
    assert summary["quiet_hours"] == "21:00-08:00"
    assert set(summary["reminders"]) == {
        rs.R_SCHEDULED,
        rs.R_HANDED_OFF,
        rs.R_SUPPRESSED,
        rs.R_SKIPPED,
        rs.R_CANCELLED,
    }
