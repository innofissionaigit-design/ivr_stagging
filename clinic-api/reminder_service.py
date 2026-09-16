"""Preparation reminders: planned per appointment type, sent the evening before.

Author: Chakravardhan
Story:  "As a patient with a fasting test tomorrow, I want a reminder tonight,
         so that my visit is not wasted."
Criteria: Reminders are sent on a schedule per appointment type, carrying the
          merged preparation instruction. The do-not-disturb registry and quiet
          hours are honoured, and opt-out is permanent and immediate.

HOW A REMINDER MOVES
--------------------
    appointment (+ tests)            plan()       one ScheduledReminder per rule
        │                                          of the appointment's TYPE,
        ▼                                          due_at + useful_until
    scheduled ──── send_due() at due_at ─────────► checks, in this order:
                                                     1. appointment still live and unchanged
                                                     2. opted out?          -> suppressed
                                                     3. DND registry fresh? -> wait / skipped
                                                     4. DND registered?     -> suppressed
                                                     5. still useful?       -> skipped (too_late)
                                                     6. quiet hours?        -> moved / skipped
                                                     7. render the merged preparation
                                                   -> a NotificationAttempt in the SMS ledger
                                                   -> notify_service.deliver_now()

Every outcome is a status with a reason on the row. Nothing is dropped silently
-- the rule notify_service.py already holds the confirmation SMS to.

SCHEDULE PER APPOINTMENT TYPE
-----------------------------
    consultation   (no tests attached)       18:00 the evening before
    test           (tests, no fasting)       19:00 the evening before
    fasting_test   (any test needs fasting)  19:00 the evening before, and
                                             never later than an hour before
                                             the fast has to begin

A reminder is only worth sending while the patient can still act on it:
`useful_until` is when the fast begins (fasting), or two hours before the
visit (everything else). Past it, the reminder is skipped, not sent late.

QUIET HOURS
-----------
No reminder is sent inside CLINIC_QUIET_HOURS (default 21:00-08:00, clinic
local time). A reminder that falls inside them is moved to just before they
start if that is still ahead, otherwise to when they end -- but only while it
is still useful. A fasting reminder that could only go out at 08:00 on the
morning of the test is skipped: at that point it would tell the patient to
fast after they have eaten.

DO-NOT-DISTURB
--------------
A number in the latest scrub of the national registry (DndRegistration) that
blocks health messages is not sent a reminder. A registry that has not been
scrubbed within CLINIC_DND_MAX_AGE_DAYS is not being honoured, so reminders
WAIT for a fresh scrub while they are still useful, and are skipped after.

OPT-OUT: PERMANENT AND IMMEDIATE
--------------------------------
opt_out() records the number and, in the same transaction, suppresses every
reminder still owed to it -- including one already handed to the SMS ledger
and not yet sent. Every reminder is checked against the list again at the
moment of sending. Nothing here removes a number from the list.

CLOCKS
------
Reminder times are CLINIC-LOCAL (CLINIC_UTC_OFFSET_MINUTES, default 330 --
India has no daylight saving), because appointment dates and times are stored
as local strings and "tonight" means tonight in Kolkata, whatever timezone the
server runs in. Ledger rows keep notify_service's own clock, which its
staleness rule is measured against.

RUNNING IT
----------
Off unless CLINIC_REMINDERS_ENABLED=1, in which case clinic-api's startup runs
tick() every CLINIC_REMINDER_TICK_S seconds (default 60) on a background
thread. reminders.py runs one tick by hand, and is where counter staff record
an opt-out, attach tests to an appointment, and load a DND scrub.

NO PHI IN LOGS: log lines carry the confirmation id and a reason, never a
phone number, a name or a message body.
"""

from __future__ import annotations

import dataclasses
import datetime
import logging
import os
import threading
from collections.abc import Callable, Iterable

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

import message_templates as mt
import notifications as notify
import notify_service
import preparation
from db import SessionLocal
from models import APPT_CANCELLED, Appointment, NotificationAttempt
from reminder_models import (
    AppointmentTest,
    DndRegistration,
    DndScrub,
    ReminderOptOut,
    ScheduledReminder,
)

log = logging.getLogger("clinic-api.reminders")

# ---------------------------------------------------------------------------
# Vocabulary. Persisted, so it is a stored data format.
# ---------------------------------------------------------------------------
TYPE_CONSULTATION = "consultation"
TYPE_TEST = "test"
TYPE_FASTING_TEST = "fasting_test"

R_SCHEDULED = "scheduled"  # owed, not yet due (or waiting on quiet hours / a DND scrub)
R_SENDING = "sending"  # claimed by one tick; nobody else may send it
R_HANDED_OFF = "handed_off"  # a NotificationAttempt exists; the ledger owns delivery now
R_SUPPRESSED = "suppressed"  # the patient must not be messaged: opt-out or DND
R_SKIPPED = "skipped"  # could not be sent while still useful
R_CANCELLED = "cancelled"  # the appointment was cancelled or changed

OPEN_STATUSES = (R_SCHEDULED, R_SENDING)

REASON_OPTED_OUT = "opted_out"
REASON_DND = "dnd_registered"
REASON_DND_STALE = "dnd_registry_stale"
REASON_QUIET_HOURS = "quiet_hours"
REASON_TOO_LATE = "too_late"
REASON_APPT_CANCELLED = "appointment_cancelled"
REASON_APPT_CHANGED = "appointment_changed"
REASON_TEMPLATE = "template_render"

# The NCPR preference category for health. "0" is a full block.
DND_FULL_BLOCK = "0"
DND_HEALTH_CATEGORY = "4"


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _parse_hhmm(value: str) -> datetime.time:
    hh, mm = value.strip().split(":")
    return datetime.time(int(hh), int(mm))


@dataclasses.dataclass(frozen=True)
class Settings:
    enabled: bool = False
    tick_s: int = 60
    quiet_start: datetime.time = datetime.time(21, 0)
    quiet_end: datetime.time = datetime.time(8, 0)
    utc_offset_minutes: int = 330
    dnd_max_age_days: int = 7
    dnd_require_fresh: bool = True

    @classmethod
    def from_env(cls) -> Settings:
        quiet_start, quiet_end = cls.quiet_start, cls.quiet_end
        raw = os.environ.get("CLINIC_QUIET_HOURS", "").strip()
        if raw:
            try:
                start, end = raw.split("-")
                quiet_start, quiet_end = _parse_hhmm(start), _parse_hhmm(end)
            except ValueError:
                log.error("CLINIC_QUIET_HOURS must look like 21:00-08:00 -- using the default")
        return cls(
            enabled=os.environ.get("CLINIC_REMINDERS_ENABLED", "0").strip() == "1",
            tick_s=max(5, _env_int("CLINIC_REMINDER_TICK_S", cls.tick_s)),
            quiet_start=quiet_start,
            quiet_end=quiet_end,
            utc_offset_minutes=_env_int("CLINIC_UTC_OFFSET_MINUTES", cls.utc_offset_minutes),
            dnd_max_age_days=_env_int("CLINIC_DND_MAX_AGE_DAYS", cls.dnd_max_age_days),
            dnd_require_fresh=os.environ.get("CLINIC_DND_REQUIRE_FRESH", "1").strip() != "0",
        )


def local_now(settings: Settings) -> datetime.datetime:
    """-> clinic-local wall-clock time, naive, like the appointment strings."""
    utc = datetime.datetime.now(datetime.timezone.utc)
    return (utc + datetime.timedelta(minutes=settings.utc_offset_minutes)).replace(tzinfo=None)


def normalize_phone(phone: str) -> str:
    """The last ten digits -- one number however it was written."""
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    return digits[-10:]


# ---------------------------------------------------------------------------
# The schedule per appointment type
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class Rule:
    name: str
    days_before: int
    at: datetime.time
    # Not useful after this many hours before the visit -- or, for a fasting
    # test, after the fast begins (when `until_fast` is set).
    hours_before: int = 2
    until_fast: bool = False
    # A fasting reminder goes out at least this long before the fast begins.
    lead_minutes: int = 60


SCHEDULES: dict[str, tuple[Rule, ...]] = {
    TYPE_CONSULTATION: (Rule("evening_before", days_before=1, at=datetime.time(18, 0)),),
    TYPE_TEST: (Rule("evening_before", days_before=1, at=datetime.time(19, 0)),),
    TYPE_FASTING_TEST: (Rule("night_before", days_before=1, at=datetime.time(19, 0), until_fast=True),),
}


@dataclasses.dataclass(frozen=True)
class Planned:
    appointment_type: str
    rule: str
    plan_key: str
    due_at: datetime.datetime
    useful_until: datetime.datetime


def appointment_at(appt: Appointment) -> datetime.datetime:
    return datetime.datetime.fromisoformat(f"{appt.date}T{appt.time_slot}")


def tests_for(db: Session, confirmation_id: str) -> list[str]:
    rows = db.query(AppointmentTest).filter_by(confirmation_id=confirmation_id).all()
    return sorted({r.test_name for r in rows})


def classify(test_names: list[str], merged: preparation.MergedPreparation) -> str:
    if not test_names:
        return TYPE_CONSULTATION
    return TYPE_FASTING_TEST if merged.needs_fasting else TYPE_TEST


def plan_key(appt: Appointment, test_names: list[str]) -> str:
    return f"{appt.date}T{appt.time_slot}|{','.join(test_names)}"


def plan_for(appt: Appointment, test_names: list[str]) -> list[Planned]:
    """The reminders one appointment is owed, from its type's schedule."""
    at = appointment_at(appt)
    merged = preparation.merge(test_names, at)
    kind = classify(test_names, merged)
    key = plan_key(appt, test_names)
    out = []
    for rule in SCHEDULES[kind]:
        day = at.date() - datetime.timedelta(days=rule.days_before)
        due = datetime.datetime.combine(day, rule.at)
        if rule.until_fast and merged.fast_from is not None:
            useful_until = merged.fast_from
            due = min(due, useful_until - datetime.timedelta(minutes=rule.lead_minutes))
        else:
            useful_until = at - datetime.timedelta(hours=rule.hours_before)
        out.append(Planned(kind, rule.name, key, due, useful_until))
    return out


# ---------------------------------------------------------------------------
# Quiet hours
# ---------------------------------------------------------------------------
def in_quiet_hours(moment: datetime.datetime, settings: Settings) -> bool:
    t, start, end = moment.time(), settings.quiet_start, settings.quiet_end
    if start == end:
        return False
    if start < end:
        return start <= t < end
    return t >= start or t < end  # the window crosses midnight


def outside_quiet_hours(
    due: datetime.datetime, useful_until: datetime.datetime, now: datetime.datetime, settings: Settings
) -> datetime.datetime | None:
    """-> when to send a reminder due at `due`, or None if quiet hours leave no useful moment.

    Earlier is preferred -- a reminder for tomorrow belongs to this evening --
    but never in the past; otherwise the end of the quiet window."""
    if not in_quiet_hours(due, settings):
        return due
    start, end = settings.quiet_start, settings.quiet_end
    if start < end or due.time() >= start:
        window_start = datetime.datetime.combine(due.date(), start)
    else:
        window_start = datetime.datetime.combine(due.date() - datetime.timedelta(days=1), start)
    window_end = datetime.datetime.combine(window_start.date(), end)
    if window_end <= window_start:
        window_end += datetime.timedelta(days=1)
    before = window_start - datetime.timedelta(minutes=1)
    if before >= now and before < useful_until:
        return before
    if window_end < useful_until:
        return max(window_end, now)
    return None


# ---------------------------------------------------------------------------
# Opt-out -- permanent and immediate
# ---------------------------------------------------------------------------
def is_opted_out(db: Session, phone: str) -> bool:
    number = normalize_phone(phone)
    return bool(number) and db.query(ReminderOptOut).filter_by(phone=number).first() is not None


def opt_out(db: Session, phone: str, source: str, now: datetime.datetime) -> ReminderOptOut:
    """Stop every reminder to `phone`, now and for good. Does NOT commit -- the
    caller commits, and the stop takes effect in that same commit.

    Idempotent: a second opt-out keeps the first one's time and source."""
    number = normalize_phone(phone)
    if len(number) != 10:
        raise ValueError("an opt-out needs a ten-digit phone number")
    row = db.query(ReminderOptOut).filter_by(phone=number).first()
    if row is None:
        row = ReminderOptOut(phone=number, source=(source or "unknown")[:40], opted_out_at=now)
        db.add(row)

    # Immediate: nothing still owed to this number goes out.
    owed = (
        db.query(ScheduledReminder)
        .filter(ScheduledReminder.phone == number, ScheduledReminder.status.in_(OPEN_STATUSES))
        .all()
    )
    for reminder in owed:
        _finish(reminder, R_SUPPRESSED, REASON_OPTED_OUT, now)

    # Including a reminder already handed to the SMS ledger but not yet sent:
    # notify_service.deliver_now() only ever sends a row that is still queued.
    handed = (
        db.query(ScheduledReminder)
        .filter(ScheduledReminder.phone == number, ScheduledReminder.status == R_HANDED_OFF)
        .all()
    )
    attempt_ids = [r.attempt_id for r in handed if r.attempt_id]
    if attempt_ids:
        (
            db.query(NotificationAttempt)
            .filter(
                NotificationAttempt.id.in_(attempt_ids),
                NotificationAttempt.status == notify.STATUS_QUEUED,
            )
            .update(
                {
                    NotificationAttempt.status: notify.STATUS_SKIPPED,
                    NotificationAttempt.error_code: REASON_OPTED_OUT,
                    NotificationAttempt.error_detail: "patient opted out of reminders before this was sent",
                    NotificationAttempt.updated_at: notify_service._now(),
                },
                synchronize_session=False,
            )
        )
    return row


# ---------------------------------------------------------------------------
# Do-not-disturb registry
# ---------------------------------------------------------------------------
def import_dnd(db: Session, rows: Iterable[tuple[str, str]], now: datetime.datetime) -> int:
    """Replace the registry with one scrub. -> how many numbers it holds.

    A scrub is a snapshot of the national registry, so the previous one is
    replaced rather than added to: a number that has left the registry must
    stop being treated as registered. Does NOT commit."""
    latest: dict[str, str] = {}
    for phone, preference in rows:
        number = normalize_phone(phone)
        if len(number) == 10:
            latest[number] = (preference or DND_FULL_BLOCK).strip() or DND_FULL_BLOCK
    # Flushed first: a bulk delete does not see rows still pending in this
    # session, so an earlier scrub in the same transaction would survive it.
    db.flush()
    db.query(DndRegistration).delete()
    for number, pref in latest.items():
        db.add(DndRegistration(phone=number, preference=pref, imported_at=now))
    db.add(DndScrub(imported_at=now, numbers=len(latest)))
    return len(latest)


def dnd_registry_fresh(db: Session, now: datetime.datetime, settings: Settings) -> bool:
    last = db.query(DndScrub).order_by(DndScrub.imported_at.desc()).first()
    return last is not None and now - last.imported_at <= datetime.timedelta(days=settings.dnd_max_age_days)


def dnd_blocks(db: Session, phone: str) -> bool:
    """Whether the registry forbids a health reminder to `phone`: a full block,
    or the health category among the categories the subscriber blocked."""
    row = db.query(DndRegistration).filter_by(phone=normalize_phone(phone)).first()
    if row is None:
        return False
    blocked = {p.strip().lower() for p in (row.preference or "").split(",") if p.strip()}
    return not blocked or bool(blocked & {DND_FULL_BLOCK, "all", DND_HEALTH_CATEGORY})


# ---------------------------------------------------------------------------
# Tests on an appointment
# ---------------------------------------------------------------------------
def order_tests(
    db: Session, confirmation_id: str, test_names: list[str], now: datetime.datetime
) -> list[str]:
    """Attach tests to an appointment. -> the names newly attached. Does NOT commit.

    The next tick re-plans the appointment: its type and preparation follow
    the tests, and a reminder planned without them is cancelled."""
    have = set(tests_for(db, confirmation_id))
    added = []
    for name in test_names:
        clean = (name or "").strip()
        if clean and clean not in have:
            db.add(AppointmentTest(confirmation_id=confirmation_id, test_name=clean, created_at=now))
            have.add(clean)
            added.append(clean)
    return added


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------
def _finish(reminder: ScheduledReminder, status: str, reason: str | None, now: datetime.datetime) -> None:
    reminder.status = status
    reminder.reason = reason
    reminder.updated_at = now


def plan(db: Session, now: datetime.datetime, settings: Settings | None = None) -> dict:
    """Create the reminders every upcoming appointment is owed, and cancel the
    ones that no longer fit it. Commits. -> counts.

    A due time inside quiet hours is moved HERE, while "earlier this evening"
    is still ahead -- by the time it is due, only "later" would be left."""
    settings = settings or Settings.from_env()
    counts = {"planned": 0, "cancelled": 0}
    today = now.date().isoformat()
    upcoming = (
        db.query(Appointment).filter(Appointment.status != APPT_CANCELLED, Appointment.date >= today).all()
    )
    live_ids = set()
    for appt in upcoming:
        confirmation_id = str(appt.confirmation_id)
        live_ids.add(confirmation_id)
        names = tests_for(db, confirmation_id)
        wanted = plan_for(appt, names)
        keys = {p.plan_key for p in wanted}
        existing = db.query(ScheduledReminder).filter_by(confirmation_id=confirmation_id).all()
        for reminder in existing:
            if reminder.status in OPEN_STATUSES and reminder.plan_key not in keys:
                _finish(reminder, R_CANCELLED, REASON_APPT_CHANGED, now)
                counts["cancelled"] += 1
        have = {(r.rule, r.plan_key) for r in existing}
        for p in wanted:
            if (p.rule, p.plan_key) in have:
                continue
            reminder = ScheduledReminder(
                confirmation_id=confirmation_id,
                phone=normalize_phone(str(appt.phone)),
                appointment_type=p.appointment_type,
                rule=p.rule,
                plan_key=p.plan_key,
                due_at=p.due_at,
                useful_until=p.useful_until,
                status=R_SCHEDULED,
                created_at=now,
                updated_at=now,
            )
            if now >= p.useful_until:
                reminder.status, reminder.reason = R_SKIPPED, REASON_TOO_LATE
            else:
                moved = outside_quiet_hours(max(p.due_at, now), p.useful_until, now, settings)
                if moved is None:
                    reminder.status, reminder.reason = R_SKIPPED, REASON_QUIET_HOURS
                else:
                    reminder.due_at = moved
            db.add(reminder)
            counts["planned"] += 1

    # A cancelled appointment owes nothing.
    for reminder in db.query(ScheduledReminder).filter(ScheduledReminder.status.in_(OPEN_STATUSES)).all():
        if reminder.confirmation_id in live_ids:
            continue
        owner = db.query(Appointment).filter_by(confirmation_id=reminder.confirmation_id).first()
        if owner is None or owner.status == APPT_CANCELLED:
            _finish(reminder, R_CANCELLED, REASON_APPT_CANCELLED, now)
            counts["cancelled"] += 1
    db.commit()
    return counts


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------
def _claim(db: Session, reminder_id: int, now: datetime.datetime) -> bool:
    """Take one due reminder for this tick. Only one tick can win it."""
    won = (
        db.query(ScheduledReminder)
        .filter(ScheduledReminder.id == reminder_id, ScheduledReminder.status == R_SCHEDULED)
        .update({ScheduledReminder.status: R_SENDING, ScheduledReminder.updated_at: now})
    )
    db.commit()
    return won == 1


def _render(appt: Appointment, test_names: list[str]) -> tuple[str, mt.MessageTemplate]:
    at = appointment_at(appt)
    merged = preparation.merge(test_names, at)
    base = notify_service._variables(appt, appt.doctor.name if appt.doctor else "")
    if not test_names:
        values = {**base, "preparation": mt.prep_phrase(mt.PREP_BRING_REPORTS)}
        return mt.render(mt.EVENT_REMINDER_VISIT, values)
    first, more = merged.test_variables()
    values = {**base, "preparation": first, "preparation_more": more}
    return mt.render(mt.EVENT_REMINDER_TEST, values)


def _hand_to_ledger(
    db: Session, appt: Appointment, text: str, tpl: mt.MessageTemplate
) -> NotificationAttempt:
    """The reminder becomes an ordinary ledger row -- the same preflight, the
    same statuses and the same staff queue as a confirmation SMS."""
    config = notify.load_config()
    ledger_now = notify_service._now()
    blocked = notify.preflight(config, tpl)
    status = notify.STATUS_QUEUED
    if blocked:
        status = notify.STATUS_SKIPPED if blocked == "gateway_not_configured" else notify.STATUS_FAILED
    attempt = NotificationAttempt(
        confirmation_id=str(appt.confirmation_id),
        event=tpl.event,
        channel=notify.CHANNEL_SMS,
        phone=notify.msisdn(str(appt.phone), config.country_code),
        template_id=tpl.template_id,
        body=text,
        status=status,
        attempts=0,
        error_code=blocked,
        error_detail=f"preflight refused to send: {blocked}" if blocked else None,
        created_at=ledger_now,
        updated_at=ledger_now,
    )
    db.add(attempt)
    return attempt


def send_due(
    db: Session,
    now: datetime.datetime,
    settings: Settings,
    deliver: Callable[[int], None] = notify_service.deliver_now,
) -> dict:
    """Send every reminder that is due and still allowed. Commits. -> counts."""
    counts = {R_HANDED_OFF: 0, R_SUPPRESSED: 0, R_SKIPPED: 0, R_CANCELLED: 0, "deferred": 0}
    due_ids = [
        r.id
        for r in db.query(ScheduledReminder)
        .filter(ScheduledReminder.status == R_SCHEDULED, ScheduledReminder.due_at <= now)
        .order_by(ScheduledReminder.due_at)
        .all()
    ]
    for reminder_id in due_ids:
        if not _claim(db, reminder_id, now):
            continue
        reminder = db.get(ScheduledReminder, reminder_id)
        if reminder is None:
            continue
        outcome, attempt_id = _decide(db, reminder, now, settings)
        counts[outcome] += 1
        db.commit()
        if attempt_id is not None:
            deliver(attempt_id)
    return counts


def _decide(
    db: Session, reminder: ScheduledReminder, now: datetime.datetime, settings: Settings
) -> tuple[str, int | None]:
    """Every check a claimed reminder passes through. -> (outcome, ledger row to deliver)."""
    appt = db.query(Appointment).filter_by(confirmation_id=reminder.confirmation_id).first()
    if appt is None or appt.status == APPT_CANCELLED:
        _finish(reminder, R_CANCELLED, REASON_APPT_CANCELLED, now)
        return R_CANCELLED, None
    phone = str(appt.phone)
    names = tests_for(db, str(appt.confirmation_id))
    if plan_key(appt, names) != reminder.plan_key:
        _finish(reminder, R_CANCELLED, REASON_APPT_CHANGED, now)
        return R_CANCELLED, None

    if is_opted_out(db, phone):
        _finish(reminder, R_SUPPRESSED, REASON_OPTED_OUT, now)
        return R_SUPPRESSED, None
    if settings.dnd_require_fresh and not dnd_registry_fresh(db, now, settings):
        return _wait_or_skip(
            reminder, now + datetime.timedelta(seconds=settings.tick_s), now, REASON_DND_STALE
        )
    if dnd_blocks(db, phone):
        _finish(reminder, R_SUPPRESSED, REASON_DND, now)
        return R_SUPPRESSED, None

    if now >= reminder.useful_until:
        _finish(reminder, R_SKIPPED, REASON_TOO_LATE, now)
        return R_SKIPPED, None
    if in_quiet_hours(now, settings):
        moved = outside_quiet_hours(now, reminder.useful_until, now, settings)
        if moved is None:
            _finish(reminder, R_SKIPPED, REASON_QUIET_HOURS, now)
            return R_SKIPPED, None
        return _wait_or_skip(reminder, moved, now, REASON_QUIET_HOURS)

    try:
        text, tpl = _render(appt, names)
    except mt.TemplateError as e:
        log.error("reminder for %s could not be rendered: %s", reminder.confirmation_id, e)
        _finish(reminder, R_SKIPPED, REASON_TEMPLATE, now)
        return R_SKIPPED, None
    attempt = _hand_to_ledger(db, appt, text, tpl)
    db.flush()
    reminder.attempt_id = int(attempt.id)
    _finish(reminder, R_HANDED_OFF, None, now)
    log.info("reminder for %s handed to the SMS ledger (%s)", reminder.confirmation_id, attempt.status)
    return R_HANDED_OFF, int(attempt.id) if attempt.status == notify.STATUS_QUEUED else None


def _wait_or_skip(
    reminder: ScheduledReminder, retry_at: datetime.datetime, now: datetime.datetime, reason: str
) -> tuple[str, int | None]:
    """Put a reminder back to wait until `retry_at` -- or skip it if that is too late."""
    if retry_at >= reminder.useful_until:
        _finish(reminder, R_SKIPPED, reason, now)
        return R_SKIPPED, None
    reminder.due_at = retry_at
    _finish(reminder, R_SCHEDULED, reason, now)
    return "deferred", None


# ---------------------------------------------------------------------------
# The tick, and the background loop that runs it
# ---------------------------------------------------------------------------
def tick(
    now: datetime.datetime | None = None,
    settings: Settings | None = None,
    deliver: Callable[[int], None] = notify_service.deliver_now,
) -> dict:
    """Plan, then send what is due. One pass, on its own session."""
    settings = settings or Settings.from_env()
    now = now or local_now(settings)
    db = SessionLocal()
    try:
        return {"planned": plan(db, now, settings), "sent": send_due(db, now, settings, deliver)}
    finally:
        db.close()


@dataclasses.dataclass
class _LoopState:
    thread: threading.Thread | None = None
    stop: threading.Event = dataclasses.field(default_factory=threading.Event)
    last_tick_at: str | None = None
    last_error: str | None = None


_LOOP = _LoopState()


def _run(settings: Settings) -> None:
    while not _LOOP.stop.is_set():
        try:
            tick(settings=settings)
            _LOOP.last_tick_at = local_now(settings).isoformat(timespec="seconds")
            _LOOP.last_error = None
        except (SQLAlchemyError, OSError, ValueError) as e:
            # Reported on /api/health; the next tick tries again.
            _LOOP.last_error = type(e).__name__
            log.error("reminder tick failed: %s", type(e).__name__)
        _LOOP.stop.wait(settings.tick_s)


def start_background(settings: Settings | None = None) -> bool:
    """Start the loop if CLINIC_REMINDERS_ENABLED=1. -> whether it is running."""
    settings = settings or Settings.from_env()
    if not settings.enabled:
        return False
    if _LOOP.thread is not None and _LOOP.thread.is_alive():
        return True
    _LOOP.stop.clear()
    _LOOP.thread = threading.Thread(target=_run, args=(settings,), name="clinic-reminders", daemon=True)
    _LOOP.thread.start()
    log.info("reminders running every %ss", settings.tick_s)
    return True


def stop_background() -> None:
    _LOOP.stop.set()
    if _LOOP.thread is not None:
        _LOOP.thread.join(timeout=5)
    _LOOP.thread = None


def summary(db: Session, settings: Settings | None = None) -> dict:
    """The reminders block of /api/health."""
    settings = settings or Settings.from_env()
    now = local_now(settings)
    counts = {
        status: db.query(ScheduledReminder).filter_by(status=status).count()
        for status in (R_SCHEDULED, R_HANDED_OFF, R_SUPPRESSED, R_SKIPPED, R_CANCELLED)
    }
    return {
        "enabled": settings.enabled,
        "running": _LOOP.thread is not None and _LOOP.thread.is_alive(),
        "last_tick_at": _LOOP.last_tick_at,
        "last_error": _LOOP.last_error,
        "quiet_hours": f"{settings.quiet_start:%H:%M}-{settings.quiet_end:%H:%M}",
        "opt_outs": db.query(ReminderOptOut).count(),
        "dnd_registry_fresh": dnd_registry_fresh(db, now, settings),
        "reminders": counts,
    }
