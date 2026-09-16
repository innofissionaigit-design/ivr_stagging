"""Tables for preparation reminders -- what a patient is told the night before.

Author: Chakravardhan
Story:  "As a patient with a fasting test tomorrow, I want a reminder tonight,
         so that my visit is not wasted."
Criteria: Reminders are sent on a schedule per appointment type, carrying the
          merged preparation instruction. The do-not-disturb registry and
          quiet hours are honoured, and opt-out is permanent and immediate.

WHY A SEPARATE MODULE FROM models.py
------------------------------------
Two reasons, and the second is the one that matters:

  * nothing in models.py changes, so no existing table, query or migration is
    touched by this story;
  * seed.py rebuilds the catalogue with models.Base.metadata.drop_all(). These
    tables share that Base (so clinic-api's startup create_all() makes them),
    but a plain `python seed.py` imports models.py only -- so it never sees,
    and never drops, the opt-out list. A patient's "stop" is not something a
    catalogue reseed may quietly undo.

Every table here is NEW, so create_all() makes it on an existing database
with no migration.

PII
---
`phone` appears in three tables. It is stored as the last ten digits -- the
same form agent/slot_parse.parse_phone() produces -- so a number typed with or
without +91 is one number, which is what makes an opt-out actually stick.
"""

from __future__ import annotations

import datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from models import Base


class AppointmentTest(Base):
    """A test the patient is coming in for, attached to an appointment.

    The booking contract (POST /api/v1/appointments) carries no tests, and
    changing it is an API-contract change for code-owner review. So tests are
    attached here, by the counter or the lab system (reminders.py
    order-tests), keyed by confirmation_id like the notification ledger. An
    appointment with no rows here is a doctor consultation.

    The test name is denormalised for the same reason TestRecord's is: seed.py
    can rebuild lab_tests, and an attached test must not dangle when it does.
    """

    __tablename__ = "appointment_tests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    confirmation_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    test_name: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (UniqueConstraint("confirmation_id", "test_name", name="uq_appointment_test"),)


class ReminderOptOut(Base):
    """A number that must never be sent a reminder again.

    PERMANENT: nothing in this codebase deletes or updates a row here. There
    is no "opt back in" function, a new booking does not clear it, and seed.py
    cannot drop it (see the module docstring). Reversing an opt-out is a
    deliberate act outside this service, not a side effect of anything in it.

    IMMEDIATE: reminder_service.opt_out() writes this row and, in the same
    transaction, suppresses every reminder still owed to the number -- and
    every reminder is re-checked against this table at the moment of sending.
    """

    __tablename__ = "reminder_opt_outs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    phone: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    source: Mapped[str] = mapped_column(
        String, nullable=False
    )  # "counter" | "phone_call" | "sms_reply" | ...
    opted_out_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False)


class DndRegistration(Base):
    """One number from a scrub of the national do-not-disturb registry (NCPR).

    `preference` is what the subscriber blocked: "0" means fully blocked;
    otherwise the comma-joined preference categories they chose to block.
    reminder_service.dnd_blocks() decides what that means for a health
    reminder. `imported_at` is the scrub the row came from -- a registry that
    has not been scrubbed recently is not being honoured, and the service
    treats it that way.
    """

    __tablename__ = "dnd_registrations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    phone: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    preference: Mapped[str] = mapped_column(String, nullable=False, default="0")
    imported_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False)


class DndScrub(Base):
    """When the registry was last loaded, and how many numbers it held.

    Kept as its own row rather than read off DndRegistration.imported_at, so a
    scrub that found NO registered numbers still proves the registry was
    checked."""

    __tablename__ = "dnd_scrubs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    imported_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False, index=True)
    numbers: Mapped[int] = mapped_column(Integer, nullable=False)


class ScheduledReminder(Base):
    """One reminder a patient is owed, from the moment it is planned.

    Written when the appointment is planned, long before it is due, and never
    deleted -- every outcome is a status with a reason, the same rule the
    notification ledger follows. "The reminder silently did not go" is not a
    state this table can hold.

    When a reminder is actually sent, the message itself becomes an ordinary
    NotificationAttempt (attempt_id), so delivery, receipts, retries and the
    staff failure queue are the confirmation SMS's own, not a second copy.

    `plan_key` is the appointment's date and time and the tests it carried
    when this reminder was planned. A rescheduled appointment, or one whose
    tests changed, gets a new key; reminders under the old key are cancelled
    rather than sent with stale times or stale instructions.
    """

    __tablename__ = "scheduled_reminders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    confirmation_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    phone: Mapped[str] = mapped_column(String, nullable=False, index=True)

    appointment_type: Mapped[str] = mapped_column(String, nullable=False)  # reminder_service.TYPE_*
    rule: Mapped[str] = mapped_column(String, nullable=False)  # which scheduled reminder of that type
    plan_key: Mapped[str] = mapped_column(String, nullable=False)

    due_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, index=True
    )  # clinic-local time
    # The last moment this reminder is still worth sending -- for a fasting
    # test, when the fast has to begin. Past it, the reminder is skipped.
    useful_until: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False)

    status: Mapped[str] = mapped_column(String, nullable=False, index=True)  # reminder_service.R_*
    reason: Mapped[str | None] = mapped_column(String, nullable=True)  # why suppressed / skipped / cancelled
    attempt_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )  # the NotificationAttempt, once sent

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (UniqueConstraint("confirmation_id", "rule", "plan_key", name="uq_reminder_plan"),)
