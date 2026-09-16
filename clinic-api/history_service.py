"""Patient history: verification, disclosure, and the audit trail behind
both.

Author: Chakravardhan
Story:  History disclosed only after verification

WHERE THIS SITS
---------------
verification.py is the POLICY and imports no ORM -- it can be reasoned
about and tested with no database at all. models.py is the schema. This
file is the only place the two meet, so clinic-api/main.py's endpoints stay
readable as endpoints.

THE ORDERING THAT MATTERS
-------------------------
Every path through this module writes an audit row, including -- especially
-- the ones that refuse. A verification system whose failures leave no
trace has no way to tell an honest patient fumbling a PIN from somebody
working through the keyspace, and those look identical from the outside.

TOKENS LIVE IN MEMORY, NOT IN THE DATABASE
------------------------------------------
Deliberate, and a trade worth stating. A restart drops every token and the
caller must verify again -- a small cost on a 10-minute TTL. What it buys
is that a database dump contains no live credentials at all: the tokens are
the one thing here that would grant access on their own, and they never
touch disk. clinic-api runs as a single uvicorn process (see
deploy/start_all.sh), so there is no second worker to share them with.
"""
from __future__ import annotations

import datetime
import logging
import threading

from sqlalchemy.orm import Session

import verification as v
from models import APPT_CANCELLED, Appointment, DisclosureAudit, Doctor, Patient, TestRecord

log = logging.getLogger("clinic-api.history")

# token -> (patient_id, expires_at). See the module docstring.
_TOKENS: dict[str, tuple[int, datetime.datetime]] = {}
_TOKEN_LOCK = threading.Lock()

# A throwaway salt+hash used ONLY to burn the same CPU on the no-patient
# path as on the real one. Without it, an unknown number returns visibly
# faster than a wrong PIN, and that timing difference is exactly the
# enumeration oracle verification.py's docstring refuses to provide.
_DUMMY_SALT = v.new_salt()
_DUMMY_HASH = v.hash_pin("0000", _DUMMY_SALT)


def _now() -> datetime.datetime:
    return datetime.datetime.now()


def _audit(db: Session, *, phone: str, patient_id: int | None, factor: str,
           outcome: str, call_id: str | None = None, detail: str | None = None) -> None:
    """One row per attempt. NEVER carries the offered secret -- see the
    DisclosureAudit docstring."""
    db.add(DisclosureAudit(
        phone=phone, patient_id=patient_id, factor=factor, outcome=outcome,
        detail=detail, call_id=call_id, created_at=_now(),
    ))
    if outcome in (v.OUTCOME_WRONG, v.OUTCOME_LOCKED, v.OUTCOME_NO_PATIENT):
        # WARNING, not INFO. One of these is somebody mistyping. A run of
        # them against one number is the thing this table exists to surface.
        log.warning("history attempt for %s: %s (factor=%s)", phone, outcome, factor)


def find_patient(db: Session, phone: str) -> Patient | None:
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())[-10:]
    if len(digits) < 10:
        return None
    return db.query(Patient).filter_by(phone=digits).first()


def begin(db: Session, phone: str, call_id: str | None = None) -> dict:
    """Start a verification. -> {"factor": ..., "locked": bool}

    RETURNS A CHALLENGE EVEN FOR A NUMBER WE HAVE NEVER SEEN. That is the
    anti-enumeration rule from verification.py made concrete: an unknown
    caller is asked for a date of birth exactly as a known one would be, and
    fails exactly as a wrong answer would. Saying "no patient here" would
    make this line a lookup for whether a given person attends this clinic,
    which is itself information about them.
    """
    patient = find_patient(db, phone)
    locked = bool(patient and v.is_locked(patient.locked_until))

    challenge = v.challenge_for(
        patient_exists=patient is not None,
        has_pin=bool(patient and patient.pin_hash),
        has_dob=bool(patient and patient.date_of_birth),
    )

    if locked:
        _audit(db, phone=phone, patient_id=patient.id if patient else None,
               factor=challenge.factor, outcome=v.OUTCOME_LOCKED, call_id=call_id)
        db.commit()
        return {"factor": challenge.factor, "locked": True}

    return {"factor": challenge.factor, "locked": False}


def attempt(db: Session, *, phone: str, factor: str, answer: str,
            call_id: str | None = None) -> tuple[v.Decision, str | None]:
    """One verification attempt. -> (decision, token or None).

    The token is returned ONLY on success and is the only thing that opens
    history(). Everything else -- including a correct answer from a locked
    patient -- returns None.
    """
    patient = find_patient(db, phone)
    now = _now()
    locked = bool(patient and v.is_locked(patient.locked_until, now))

    matched = False
    if patient is not None and not locked:
        if factor == v.FACTOR_PIN:
            pin = v.normalise_pin(answer)
            matched = bool(pin) and v.pin_matches(pin, patient.pin_salt, patient.pin_hash)
        elif factor == v.FACTOR_DOB:
            matched = v.dob_matches(answer, patient.date_of_birth)
    else:
        # Burn the same work on the unknown-number path. See _DUMMY_HASH.
        v.pin_matches(v.normalise_pin(answer) or "0000", _DUMMY_SALT, _DUMMY_HASH)

    decision = v.evaluate(
        patient_exists=patient is not None,
        locked=locked,
        factor=factor,
        matched=matched,
        attempts_before=patient.failed_attempts if patient else 0,
    )

    token = None
    if patient is not None:
        if decision.verified:
            patient.failed_attempts = 0
            patient.locked_until = None
            patient.last_verified_at = now
            token = v.new_token()
            with _TOKEN_LOCK:
                _TOKENS[token] = (patient.id, v.token_expiry(now))
        elif decision.outcome == v.OUTCOME_WRONG:
            patient.failed_attempts = (patient.failed_attempts or 0) + 1
            if decision.lock_now:
                # Counted PER PATIENT, so redialling does not reset the
                # budget -- the obvious way around a per-call counter.
                patient.locked_until = v.lockout_until(now)
                log.warning("patient %s locked out after %d failed attempts",
                            patient.id, patient.failed_attempts)

    _audit(db, phone=phone, patient_id=patient.id if patient else None,
           factor=decision.factor, outcome=decision.outcome, call_id=call_id,
           detail=decision.detail)
    db.commit()
    return decision, token


def resolve_token(token: str | None) -> int | None:
    """-> patient id, or None. Expired tokens are dropped as they are seen,
    which keeps the store from growing without a sweeper task."""
    if not token:
        return None
    with _TOKEN_LOCK:
        entry = _TOKENS.get(token)
        if entry is None:
            return None
        patient_id, expires_at = entry
        if not v.token_valid(expires_at):
            _TOKENS.pop(token, None)
            return None
        return patient_id


def revoke_token(token: str | None) -> None:
    """Called when a call ends. A shared handset means the next caller may
    be a different person, so a token must not outlive the conversation that
    earned it."""
    if token:
        with _TOKEN_LOCK:
            _TOKENS.pop(token, None)


# ===========================================================================
# A SINGLE PATIENT TIMELINE -- Author: Chakravardhan
# ---------------------------------------------------------------------------
# Story: "As a patient, I want the agent to already know what I have booked
#         here, so that I am not made to recite my own history to the
#         hospital that holds it."
#
# The clinic already held everything a patient might be asked to repeat --
# every booking, every test -- in two tables the voice line never joined.
# build_timeline() is that join: one list, oldest first, in which a booking
# and a test are entries of the same kind of thing, plus the live bookings
# still ahead, soonest first. It is returned only through history() below,
# so it opens with the same verification token and nothing else.
# ===========================================================================

# Timeline entry kinds. Returned over the API, so they are a data format.
KIND_APPOINTMENT = "appointment"
KIND_TEST = "test"


def _today() -> datetime.date:
    return datetime.date.today()


# Parameters below that take a column value are typed `object`: the models
# use untyped Column attributes, so a row's `phone` is not a `str` to mypy.
def _last10(phone: object) -> str:
    return "".join(ch for ch in str(phone or "") if ch.isdigit())[-10:]


def _spoken_alias(aliases_bn: object) -> str | None:
    """The first Bengali alias -- the name the caller will HEAR. The same
    rule as clinic-api/main.py's _first_alias_bn(), repeated here because
    main.py imports this module, not the other way round."""
    for alias in str(aliases_bn or "").split("|"):
        if alias.strip():
            return alias.strip()
    return None


def patient_appointments(db: Session, phone: object) -> list[Appointment]:
    """Every appointment booked under this patient's number, however that
    number was written when the booking was made.

    Patient.phone holds the last ten digits (see find_patient()), while
    Appointment.phone holds whatever the booking was given -- "9000000001",
    "+91 90000 00001", "919000000001". Matching them exactly, as history()
    used to, silently left the second and third forms out of the patient's
    record: the clinic held the booking and the line told the patient they
    had none. The LIKE narrows the scan; the digit comparison decides.
    """
    digits = _last10(phone)
    if len(digits) < 10:
        return []
    rows = db.query(Appointment).filter(Appointment.phone.like(f"%{digits[-4:]}%")).all()
    return [a for a in rows if _last10(a.phone) == digits]


def _doctors_for(db: Session, appointments: list[Appointment]) -> dict:
    """-> {doctor id: Doctor} for the doctors these appointments are with."""
    ids = {a.doctor_id for a in appointments}
    if not ids:
        return {}
    return {d.id: d for d in db.query(Doctor).filter(Doctor.id.in_(ids)).all()}


def build_timeline(appointments: list[Appointment], tests: list[TestRecord],
                   doctors: dict, today_iso: str) -> dict:
    """-> {"timeline": [...], "upcoming_appointments": [...]}.

    PURE: no database, no clock. The rows and today's date come in, so the
    ordering and the "upcoming" rule can be tested exactly.

    `timeline` holds every entry, oldest first -- cancelled bookings
    included, because a cancellation is part of a patient's history and
    "we have no record of that" is the worst answer a clinic can give.
    `upcoming_appointments` holds only the bookings still ahead of the
    patient: not cancelled, and dated today or later, soonest first. That
    list is what "what have I booked" is answered from.

    MINIMUM DISCLOSURE, as in history(): names, dates, times, status and
    whether a report is ready. No results, no values, no diagnoses.
    """
    entries: list[dict] = []
    for a in appointments:
        doctor = doctors.get(a.doctor_id)
        entries.append({
            "kind": KIND_APPOINTMENT,
            "date": a.date,
            "time_slot": a.time_slot,
            "status": a.status,
            "upcoming": a.status != APPT_CANCELLED and (a.date or "") >= today_iso,
            # Held so a later reschedule or cancellation by phone can act on
            # the booking without asking the patient to read it out.
            "confirmation_id": a.confirmation_id,
            "doctor_name": doctor.name if doctor else None,
            "doctor_name_bn": _spoken_alias(doctor.aliases_bn) if doctor else None,
        })
    for t in tests:
        entries.append({
            "kind": KIND_TEST,
            "date": t.taken_on,
            "time_slot": None,
            "upcoming": False,
            "test_name": t.test_name,
            "test_name_bn": t.test_name_bn,
            "report_ready": bool(t.report_ready),
            "report_ready_on": t.report_ready_on,
        })

    def _when(entry: dict) -> tuple[str, str]:
        return (entry["date"] or "", entry["time_slot"] or "")

    entries.sort(key=_when)
    return {
        "timeline": entries,
        "upcoming_appointments": [e for e in entries if e["upcoming"]],
    }


def history(db: Session, token: str, call_id: str | None = None) -> dict | None:
    """-> the patient's history, or None if the token does not open it.

    Carries the single timeline (build_timeline()) alongside the older
    `tests` / `appointments` lists, behind the same token and the same audit
    row -- the timeline is a better-joined view of the same record, not a
    wider one.

    MINIMUM DISCLOSURE. Test names, dates and whether a report is ready --
    not results, not values, not diagnoses. A voice line that reads clinical
    numbers aloud is a far larger disclosure surface than this story asks
    anyone to build, and the counter already exists as the path for detail
    (and is the non-smartphone path, which is not a coincidence).
    """
    patient_id = resolve_token(token)
    if patient_id is None:
        return None

    patient = db.get(Patient, patient_id)
    if patient is None:
        return None

    tests = (db.query(TestRecord).filter_by(patient_id=patient.id)
               .order_by(TestRecord.taken_on.desc()).all())
    appointments = sorted(patient_appointments(db, patient.phone),
                          key=lambda a: a.date, reverse=True)
    line = build_timeline(appointments, tests, _doctors_for(db, appointments),
                          _today().isoformat())

    _audit(db, phone=patient.phone, patient_id=patient.id, factor="token",
           outcome=v.OUTCOME_DISCLOSED, call_id=call_id,
           detail=(f"{len(tests)} tests, {len(appointments)} appointments, "
                   f"{len(line['upcoming_appointments'])} upcoming"))
    db.commit()

    return {
        # THE SINGLE TIMELINE. The agent reads this once per verified call
        # and answers "what have I booked" from it -- see build_timeline().
        # `tests` and `appointments` below are kept unchanged for the
        # callers that already read them.
        "timeline": line["timeline"],
        "upcoming_appointments": line["upcoming_appointments"],
        "patient_name": patient.full_name,
        "tests": [
            {"test_name": t.test_name, "test_name_bn": t.test_name_bn,
             "taken_on": t.taken_on, "report_ready": bool(t.report_ready),
             "report_ready_on": t.report_ready_on}
            for t in tests
        ],
        "appointments": [
            {"date": a.date, "time_slot": a.time_slot, "status": a.status,
             "confirmation_id": a.confirmation_id}
            for a in appointments
        ],
    }


def record_refusal(db: Session, *, phone: str, reason: str,
                   call_id: str | None = None) -> None:
    """A disclosure blocked by the audio path -- the caller was verified, or
    on their way to being, and the room was not private.

    Audited like any other refusal. Without this row, "the system would not
    tell me my history" has no explanation on the clinic's side, and the
    likeliest support answer would be to switch the check off.
    """
    patient = find_patient(db, phone)
    _audit(db, phone=phone, patient_id=patient.id if patient else None,
           factor=v.FACTOR_NONE, outcome=v.OUTCOME_UNSAFE_PATH,
           call_id=call_id, detail=reason)
    db.commit()


def set_pin(db: Session, *, phone: str, pin: str) -> bool:
    """Set a patient's PIN. FOR COUNTER STAFF, IN PERSON.

    Deliberately not reachable from the voice line. A PIN that can be set by
    whoever is holding the handset is not a second factor -- it is a button
    that says "make me verified", and it would undo the entire story.
    """
    patient = find_patient(db, phone)
    if patient is None:
        return False
    normalised = v.normalise_pin(pin)
    if not normalised:
        return False
    patient.pin_salt = v.new_salt()
    patient.pin_hash = v.hash_pin(normalised, patient.pin_salt)
    patient.pin_set_at = _now()
    patient.failed_attempts = 0
    patient.locked_until = None
    db.commit()
    log.info("PIN set at counter for patient %s", patient.id)
    return True


def summary(db: Session) -> dict:
    """Counts for /api/health, so a burst of failed verifications is visible
    to whatever already polls that endpoint."""
    out = {}
    for outcome in (v.OUTCOME_VERIFIED, v.OUTCOME_WRONG, v.OUTCOME_LOCKED,
                    v.OUTCOME_NO_PATIENT, v.OUTCOME_UNSAFE_PATH, v.OUTCOME_DISCLOSED):
        out[outcome] = db.query(DisclosureAudit).filter_by(outcome=outcome).count()
    with _TOKEN_LOCK:
        out["active_tokens"] = len(_TOKENS)
    return out
