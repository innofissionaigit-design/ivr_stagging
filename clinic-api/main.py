"""Clinic data service -- implements the exact 3-endpoint contract
agent/tools_client.py in the voice agent already expects. Backed by
PostgreSQL, seeded with dummy departments/doctors/schedules/tests via
seed.py.

Entity matching lives in match_band.py, which scores every spelling the
catalogue holds and returns one of three verdicts -- commit, ambiguous,
none. It is still simple (difflib plus a containment tier) rather than the
phonetic-fold gazetteer that production callers slurring "লিপিড প্রোফাইল"
through a phone mic deserve. What changed is that it no longer resolves an
ambiguity by picking a row: several plausible rows come back as several, and
the agent asks. The single-test lookups below (search, preparation, sample,
duration, walk-in eligibility, prescription requirements) share a simpler
exact/Bengali-alias-then-fuzzy ladder (_find_lab_test() /
_lab_test_fuzzy_suggestions()) -- see search_test()'s own docstring for why
that endpoint alone goes through match_band instead.

======================================================================
UPDATED BY SOURAV -- "Lab Report Status & Secure Delivery" combined
story (previously two separate stories: "is my report ready" and "send
my report"), per the VOICE CARE AGENT FINAL TESTING / STORY / RULES /
EDGE-CASE ATTACK PLAN doc.

Added below (search "SOURAV" for every changed/new block):
  - GET  /api/v1/reports/status           (Rule 1-3, 13, 14; Section 4)
  - POST /api/v1/reports/delivery/request (Rule 3, 4, 15, 16)
  - POST /api/v1/reports/otp/verify       (Rule 4-9, 17; the whole OTP
                                            attack surface in Section 6)
  - GET  /api/v1/reports/link/{token}     (Rule 11, 12; Section 8 link
                                            attacks)

FAIL SAFE, NOT FAIL OPEN (plan Section 23) is the guiding principle for
every one of these: every endpoint below returns a NAMED reason instead
of a generic error/boolean whenever it refuses to act, precisely so the
voice agent (main.py / main_pcm.py) never has to guess why something
didn't happen, and never defaults to acting just because a check was
inconclusive.
======================================================================
"""

from __future__ import annotations

import logging

import datetime
import difflib
import secrets
import uuid

from fastapi import BackgroundTasks, FastAPI, Depends, Header, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import match_band

# ADDED BY CHAKRAVARDHAN -- "History disclosed only after verification",
# "Preparation reminders" and related stories, below.
import history_service
import message_templates as mt
import notifications as notify
import notify_service

# PREPARATION REMINDERS -- Author: Chakravardhan. Imported before startup so
# its tables are registered on models.Base when create_all() runs.
import reminder_service
import verification as verify
from db import get_db, SessionLocal
from models import (
    APPT_BOOKED,
    APPT_CANCELLED,
    APPT_RESCHEDULED,
    SLOT_LOCK_ACTIVE,
    Department,
    Doctor,
    DoctorSchedule,
    LabTest,
    Appointment,
    NotificationAttempt,
    # SOURAV: needed for the report-status/delivery/OTP endpoints below.
    Patient,
    LabReport,
    ReportOTP,
    ReportDelivery,
    # ADDED BY SOURAV -- "Caller asks about a health package" and "Caller
    # asks opening hours, address or directions" stories, below.
    ClinicInfo,
    HealthPackage,
    HealthPackageTest,
    # ADDED BY SOURAV -- "otp will not be hardcoded" -- the single shared
    # random-OTP generator every ReportOTP row now goes through (see
    # models.py's own comment on generate_otp_code() for why this lives
    # there and not duplicated here and in seed.py).
    generate_otp_code,
    # ADDED BY SOURAV -- Phase 1: Database Schema & Policy Tables. Walk-in
    # Eligibility / Prescription Requirements / Insurance Coverage Policy /
    # Outstanding Balance stories, below.
    InsuranceProvider,
    InsurancePolicy,
    PatientBilling,
    # ADDED BY SOURAV -- "Caller asks to be called back" story, below.
    CallbackRequest,
    # ADDED BY CHAKRAVARDHAN -- patient identity/verification and history
    # disclosure audit trail, below.
    TestRecord,
    DisclosureAudit,
)

# ADDED BY SOURAV -- "otp will not be hardcoded": how the freshly
# generated code actually reaches the patient is a separate, pluggable
# concern -- see this module's own docstring on the file below.
from otp_messaging_config import send_otp_via_provider

app = FastAPI(title="Kolkata Care Diagnostics -- Clinic Data API (dummy)")

SLOT_STEP_MIN = 15


@app.on_event("startup")
def _ensure_seeded():
    """Create the schema, and seed it only if it is EMPTY.

    The catalogue is derived data -- 8 departments, 32 doctors, 34 tests,
    all defined in seed.py -- so regenerating it costs nothing and removes
    the manual reseed step that every pod restart used to require.

    Guarded on emptiness because seed() itself is destructive (drop_all
    then create_all). Running it unconditionally at startup would wipe
    every appointment booked since the last boot, turning a convenience
    into data loss.
    """
    from db import engine
    from models import Base, LabTest

    Base.metadata.create_all(engine)
    _check_appointment_schema()
    db = SessionLocal()
    try:
        if db.query(LabTest).count() == 0:
            logging.getLogger("clinic-api").info("empty database -- seeding catalogue")
            from seed import seed

            seed()
        else:
            logging.getLogger("clinic-api").info("catalogue already present, not reseeding")
    finally:
        db.close()


# PREPARATION REMINDERS -- Author: Chakravardhan.
# Registered after _ensure_seeded, so the reminder tables exist before the
# first tick. A no-op unless CLINIC_REMINDERS_ENABLED=1 -- see reminder_service.
@app.on_event("startup")
def _start_reminders():
    reminder_service.start_background()


@app.on_event("shutdown")
def _stop_reminders():
    reminder_service.stop_background()


# Set by _check_appointment_schema() and reported by /api/health. Not a
# bool: the health endpoint says WHICH columns are missing, because the
# person reading it is usually the person about to run the migration.
_SCHEMA_WARNING: str | None = None


def _check_appointment_schema() -> None:
    """Detect an `appointments` table predating the notification work.

    create_all() creates tables that do not exist; it does NOT alter ones
    that do. A database that already held appointments before this change
    therefore comes up with the OLD three-column unique constraint and
    without `status`/`slot_lock`/`updated_at` -- and SQLite cannot add or
    drop a table-level UNIQUE constraint with ALTER at all, so this is a
    table rebuild, not a column addition.

    That rebuild is NOT run automatically. This service's own startup
    docstring already explains why the seed is guarded on emptiness: an
    unconditional destructive step at boot "would wipe every appointment
    booked since the last boot, turning a convenience into data loss". A
    silent table rebuild is that same hazard with a wider blast radius. So
    the mismatch is detected, logged loudly, and surfaced on /api/health,
    and `python migrate_notifications.py` performs the rebuild when someone
    decides to run it.
    """
    global _SCHEMA_WARNING
    from sqlalchemy import inspect
    from db import engine

    try:
        insp = inspect(engine)
        if "appointments" not in insp.get_table_names():
            _SCHEMA_WARNING = None
            return
        columns = {c["name"] for c in insp.get_columns("appointments")}
    except Exception as e:  # noqa: BLE001
        logging.getLogger("clinic-api").warning("could not inspect schema: %s", e)
        _SCHEMA_WARNING = None
        return

    missing = sorted({"status", "slot_lock", "updated_at"} - columns)
    if not missing:
        _SCHEMA_WARNING = None
        return

    _SCHEMA_WARNING = (
        f"appointments table is missing {', '.join(missing)} -- run "
        f"`python migrate_notifications.py` in clinic-api/. Until then, "
        f"bookings will fail and cancelled slots cannot be rebooked."
    )
    logging.getLogger("clinic-api").error(_SCHEMA_WARNING)


@app.get("/api/health")
def health(db: Session = Depends(get_db)):
    """Now also reports the delivery ledger.

    deploy/status.sh and anything else that polls this endpoint gets the
    notification backlog for free, which is the difference between a
    failing gateway being noticed in minutes and being noticed by a patient
    at a counter. `notifications.open_failures` is the number staff are
    expected to act on -- see notify_service.open_failures().
    """
    config = notify.load_config()
    return {
        "status": "ok",
        "departments": db.query(Department).count(),
        "doctors": db.query(Doctor).count(),
        "lab_tests": db.query(LabTest).count(),
        "gateway_configured": config.configured,
        "notifications": notify_service.summary(db),
        # A burst of failed verifications is somebody guessing. Surfaced on
        # the endpoint that is already polled rather than on one nobody
        # remembers to look at -- see history_service.summary().
        "history_disclosure": history_service.summary(db),
        # None on a healthy schema. Non-null means the appointments table
        # predates this change and migrate_notifications.py has not been
        # run -- see _check_appointment_schema().
        "schema_warning": _SCHEMA_WARNING,
        # Preparation reminders -- Author: Chakravardhan. Whether the loop is
        # running, when it last ticked, opt-outs, DND scrub freshness, and
        # reminders by status.
        "reminders": reminder_service.summary(db),
    }


# =============================================================================
# Tool 1: GET /api/v1/tests/search?name=...
# =============================================================================
def _first_alias_bn(aliases_bn: str) -> str | None:
    """The spoken form. The reply the caller HEARS is synthesized by a
    Bengali-only tokenizer that silently drops Latin script, so returning
    only `t.name` ("Uric Acid") means the caller is read a price with the
    test name missing from the sentence. Every row is seeded with at least
    one Bengali alias for exactly this reason -- see seed.py."""
    for alias in (aliases_bn or "").split("|"):
        if alias.strip():
            return alias.strip()
    return None


def _test_reply_dict(t: LabTest) -> dict:
    return {
        "found": True,
        "test_name": t.name,
        "test_name_bn": _first_alias_bn(t.aliases_bn),
        "rate_inr": t.rate_inr,
        "sample_type": t.sample_type,
        "report_time_hours": t.report_time_hours,
    }


# story title: Near matches are offered rather than guessed or refused
# user story: As a caller naming something loosely, I want the close matches
#   offered, so that I am not told my test does not exist when it does.
# acceptance criteria: When several catalogue rows fall within the match band
#   the agent offers up to three by name and asks which. Candidates are
#   generated across every supported language and romanised spelling. The
#   did-you-mean path covers the ambiguous case and not only total failure.
#
# EVERY FORM THE CATALOGUE HOLDS, in every script, scored through one
# function. The English canonical name and the Bengali aliases go into the
# same pool because a caller may say either and the ASR may land on either --
# the same argument that put aliases_bn into the old fuzzy fallback, applied
# now to the whole lookup rather than only to its last resort.
def _forms(row, *, extra=()) -> list[str]:
    aliases = [a.strip() for a in (row.aliases_bn or "").split("|") if a.strip()]
    return [row.name, *extra, *aliases]


def _candidate_dicts(rows) -> list[dict]:
    """-> [{"name", "name_bn"}, ...], or [] if ANY row cannot be said aloud.

    All or nothing, deliberately. Dropping the one candidate that has no
    Bengali alias would turn "which of these two did you mean" back into
    "did you mean this one" -- a guess wearing a question mark, which is the
    exact failure this story exists to remove. An empty list tells the agent
    to ask the caller to name it again instead, which is honest.

    Every seeded row has an alias today (seed.py guarantees it), so this is a
    trap being closed rather than a bug being fixed.
    """
    out = []
    for row in rows:
        spoken = _first_alias_bn(row.aliases_bn)
        if not spoken:
            return []
        out.append({"name": row.name, "name_bn": spoken})
    return out


def _ambiguous_reply(query: str, rows) -> dict:
    """The third response shape, alongside found and not-found.

    `ambiguous` is its own flag rather than an overloaded found=false,
    because agent/tool_outcome.py reads a falsy `found` as NOT_FOUND and an
    ambiguity counted as a thing-not-existing would poison not_found_rate --
    the one metric the preceding story built to tell an empty catalogue from
    a working one.
    """
    return {"found": False, "ambiguous": True, "query": query, "candidates": _candidate_dicts(rows)}


@app.get("/api/v1/catalogue")
def catalogue(db: Session = Depends(get_db)):
    """Every test and doctor with their Bengali aliases, in one call.

    Exists for the voice agent's deterministic fast path: matching a
    caller's words against a 74-row catalogue is a local string operation,
    but only if the caller HAS the catalogue. Fetching it once at startup
    turns "which test did they say" from a 7B-model inference into a
    microsecond comparison -- see agent/fast_path.py.
    """
    return {
        "tests": [
            {"name": t.name, "aliases_bn": [a for a in (t.aliases_bn or "").split("|") if a]}
            for t in db.query(LabTest).all()
        ],
        "doctors": [
            {
                "name": d.name,
                "surname": d.name.split()[-1],
                "aliases_bn": [a for a in (d.aliases_bn or "").split("|") if a],
            }
            for d in db.query(Doctor).all()
        ],
    }


def _find_lab_test(db: Session, name: str) -> LabTest | None:
    """Factored out so /api/v1/tests/preparation and the other single-test
    lookups below (sample, duration, walk-in eligibility, prescription
    requirements, ...) can reuse the exact same exact/Bengali-alias matching
    ladder rather than duplicating it. Returns None when neither stage finds
    anything; each caller decides its own not-found shape.

    NOTE: search_test() below deliberately does NOT call this -- see its own
    docstring. It goes through the match_band ranker instead so a near-miss is
    OFFERED rather than silently resolved to the first substring hit, which
    is exactly the bug this function's simpler ladder would reintroduce for
    the one endpoint whose whole story is about not doing that. Every other
    lookup here still wants the plain exact-then-fuzzy shape, so both ladders
    coexist on purpose."""
    # English substring match -- covers callers who say the test name in
    # English/transliterated form.
    exact = db.query(LabTest).filter(func.lower(LabTest.name).contains(name.lower())).first()
    if exact:
        return exact

    # Bengali-script match -- covers the actual common case. A caller
    # saying "ইউরিক এসিড" was matched against nothing before this existed:
    # the DB only stored the English name "Uric Acid", and Bengali script
    # shares zero characters with Latin script, so substring AND fuzzy
    # matching against the English column alone can NEVER succeed on
    # Bengali input, regardless of how close the pronunciation is.
    for t in db.query(LabTest).all():
        aliases = [a for a in t.aliases_bn.split("|") if a]
        if any(name in alias or alias in name for alias in aliases):
            return t

    return None


def _lab_test_fuzzy_suggestions(db: Session, name: str) -> list[str]:
    """Fuzzy-fallback logic paired with _find_lab_test() above, shared by
    /api/v1/tests/preparation and the other single-test lookups."""
    all_tests = db.query(LabTest).all()
    candidates = []
    for t in all_tests:
        candidates.append(t.name)
        candidates.extend(a for a in t.aliases_bn.split("|") if a)
    suggestions = difflib.get_close_matches(name, candidates, n=3, cutoff=0.5)
    # Map suggested aliases back to their canonical English name for display.
    alias_to_name = {a: t.name for t in all_tests for a in t.aliases_bn.split("|") if a}
    return list(dict.fromkeys(alias_to_name.get(s, s) for s in suggestions))


@app.get("/api/v1/tests/search")
def search_test(name: str = Query(...), db: Session = Depends(get_db)):
    # story title: Near matches are offered rather than guessed or refused
    # user story: As a caller naming something loosely, I want the close matches
    #   offered, so that I am not told my test does not exist when it does.
    # acceptance criteria: When several catalogue rows fall within the match band
    #   the agent offers up to three by name and asks which. Candidates are
    #   generated across every supported language and romanised spelling. The
    #   did-you-mean path covers the ambiguous case and not only total failure.
    #
    # WHAT THIS REPLACED, because the shape of the bug is not the shape people
    # expect. There were three passes: an English substring query ending in
    # .first(), a Bengali alias loop ending in `return` on its first hit, and
    # only then a fuzzy fallback. The first two did no scoring AT ALL -- so
    # "Blood Sugar", which matches both "Blood Sugar Fasting" and "Blood Sugar
    # PP", was resolved by row order, silently, and so were "সুগার",
    # "ভিটামিন" and "Vitamin". A runner-up margin alone would not have touched
    # any of them; there was no runner-up to compare against, only a list and
    # an index.
    #
    # Now every form of every row is scored once, and the same question --
    # is second place close? -- is asked on every path. A substring hit is a
    # high score rather than an early return.
    all_tests = db.query(LabTest).all()
    verdict, candidates = match_band.decide(match_band.rank(name, [(t, _forms(t)) for t in all_tests]))

    if verdict == match_band.COMMIT:
        return _test_reply_dict(candidates[0].key)

    if verdict == match_band.AMBIGUOUS:
        return _ambiguous_reply(name, [c.key for c in candidates])

    # Nothing cleared match_band.BAND_FLOOR, which is the same 0.50 the old
    # difflib.get_close_matches(cutoff=0.5) used -- so this is exactly the
    # case that used to produce an EMPTY suggestion list, and the keys are
    # still emitted, still empty, so a client reading them sees no change.
    #
    # The non-empty case they used to carry is now the ambiguous branch
    # above. That is the story's third clause: did-you-mean stops being a
    # total-failure consolation and becomes the same mechanism that handles
    # two rows tying at 0.90.
    return {"found": False, "query": name, "did_you_mean": [], "did_you_mean_bn": []}


# =============================================================================
# Tool 12: GET /api/v1/tests/preparation?name=...
#
# ADDED BY SOURAV -- "Caller asks how to prepare for a test" story.
# Reuses /api/v1/tests/search's own exact/Bengali-alias/fuzzy matching
# ladder unchanged (see _find_lab_test()/_lab_test_fuzzy_suggestions()
# above) so a test that can already be priced or looked up can also be
# asked about by name here -- this endpoint only differs in what it
# returns once the row is found.
# =============================================================================


def _test_preparation_reply_dict(t: LabTest) -> dict:
    """`advisory_available=False` is a real, honest, DIFFERENT outcome
    from `found=False` (test_preparation()'s own not-found branch below):
    the test itself exists and can be looked up fine, but nobody has
    ever supplied real preparation content for it (see models.py's own
    comment on why LabTest's advisory columns are nullable with no
    default). The voice agent must tell these two apart -- suggesting
    "did you mean" alternatives for a test that WAS found correctly
    would be nonsensical, and guessing "no special preparation" for one
    with no advisory row would be fabricating a medical instruction."""
    if t.fasting_required is None:
        return {
            "found": True,
            "test_name": t.name,
            "test_name_bn": _first_alias_bn(t.aliases_bn),
            "advisory_available": False,
        }
    return {
        "found": True,
        "test_name": t.name,
        "test_name_bn": _first_alias_bn(t.aliases_bn),
        "advisory_available": True,
        "fasting_required": t.fasting_required,
        "fasting_hours": t.fasting_hours,
        "water_allowance": t.water_allowance,
        "medication_hold": t.medication_hold,
        "timing_rule": t.timing_rule,
        "advisory_script_en": t.advisory_script_en,
        "advisory_script_hinglish": t.advisory_script_hinglish,
        "advisory_script_banglish": t.advisory_script_banglish,
        "advisory_script_bn": t.advisory_script_bn,
    }


@app.get("/api/v1/tests/preparation")
def test_preparation(name: str = Query(...), db: Session = Depends(get_db)):
    t = _find_lab_test(db, name)
    if t:
        return _test_preparation_reply_dict(t)
    suggestions = _lab_test_fuzzy_suggestions(db, name)
    return {"found": False, "query": name, "did_you_mean": suggestions}


# =============================================================================
# Tool 13: GET /api/v1/tests/walkin-policy?name=...
#
# ADDED BY SOURAV -- Phase 1: Database Schema & Policy Tables. Walk-in
# Eligibility story. Reuses /api/v1/tests/search's own exact/Bengali-alias/
# fuzzy matching ladder unchanged (see _find_lab_test()/
# _lab_test_fuzzy_suggestions() above), same as /api/v1/tests/preparation
# just above -- only what gets returned once the row is found differs.
# =============================================================================


def _walkin_policy_reply_dict(t: LabTest) -> dict:
    """`policy_available=False` is a real, honest, DIFFERENT outcome from
    `found=False` below -- same "found vs. has-real-content" split as
    test_preparation's own advisory_available (see that function's own
    docstring for the full reasoning): the test exists and can be looked
    up fine, but nobody has confirmed its walk-in policy yet. Never
    guess "walk-ins welcome" for a test with no reviewed answer."""
    if t.walkin_eligible is None:
        return {
            "found": True,
            "test_name": t.name,
            "test_name_bn": _first_alias_bn(t.aliases_bn),
            "policy_available": False,
        }
    return {
        "found": True,
        "test_name": t.name,
        "test_name_bn": _first_alias_bn(t.aliases_bn),
        "policy_available": True,
        "walkin_eligible": t.walkin_eligible,
        "walkin_hours": t.walkin_hours,
    }


@app.get("/api/v1/tests/walkin-policy")
def test_walkin_policy(name: str = Query(...), db: Session = Depends(get_db)):
    t = _find_lab_test(db, name)
    if t:
        return _walkin_policy_reply_dict(t)
    suggestions = _lab_test_fuzzy_suggestions(db, name)
    return {"found": False, "query": name, "did_you_mean": suggestions}


# =============================================================================
# Tool 14: GET /api/v1/tests/prescription-policy?name=...
#
# ADDED BY SOURAV -- Phase 1: Database Schema & Policy Tables. Prescription
# Requirements story. Same matching ladder and found/policy_available
# split as walkin-policy just above.
# =============================================================================


def _prescription_policy_reply_dict(t: LabTest) -> dict:
    if t.prescription_required is None:
        return {
            "found": True,
            "test_name": t.name,
            "test_name_bn": _first_alias_bn(t.aliases_bn),
            "policy_available": False,
        }
    # prescription_channels is stored "|"-joined (same convention as
    # aliases_bn) -- split it into a real list for the caller-facing
    # layer here, same as aliases_bn is split wherever it's read.
    channels = [c for c in (t.prescription_channels or "").split("|") if c]
    return {
        "found": True,
        "test_name": t.name,
        "test_name_bn": _first_alias_bn(t.aliases_bn),
        "policy_available": True,
        "prescription_required": t.prescription_required,
        "prescription_channels": channels,
    }


@app.get("/api/v1/tests/prescription-policy")
def test_prescription_policy(name: str = Query(...), db: Session = Depends(get_db)):
    t = _find_lab_test(db, name)
    if t:
        return _prescription_policy_reply_dict(t)
    suggestions = _lab_test_fuzzy_suggestions(db, name)
    return {"found": False, "query": name, "did_you_mean": suggestions}


# =============================================================================
# Tool 15: GET /api/v1/insurance/coverage?test_name=...&provider_name=...
#
# ADDED BY SOURAV -- Phase 1: Database Schema & Policy Tables. Insurance
# Coverage Policy story. Two names to resolve, not one: the test (same
# ladder as every other LabTest lookup above) AND the insurer (its own
# exact/alias ladder below, same shape as _find_lab_test's, over
# InsuranceProvider.aliases instead of LabTest.aliases_bn).
# =============================================================================


def _find_insurance_provider(db: Session, name: str) -> InsuranceProvider | None:
    """Same two-stage ladder as _find_lab_test() above: substring match on
    the canonical name first, then a "|"-joined alias match -- covers a
    caller naming their insurer in English, Hinglish/Banglish, or Bengali
    script, the same way LabTest.aliases_bn covers a test name."""
    exact = (
        db.query(InsuranceProvider).filter(func.lower(InsuranceProvider.name).contains(name.lower())).first()
    )
    if exact:
        return exact
    for p in db.query(InsuranceProvider).all():
        aliases = [a for a in p.aliases.split("|") if a]
        if any(name in alias or alias in name for alias in aliases):
            return p
    return None


@app.get("/api/v1/insurance/coverage")
def insurance_coverage(
    test_name: str = Query(...), provider_name: str = Query(...), db: Session = Depends(get_db)
):
    t = _find_lab_test(db, test_name)
    if not t:
        suggestions = _lab_test_fuzzy_suggestions(db, test_name)
        return {"test_found": False, "query": test_name, "did_you_mean": suggestions}

    provider = _find_insurance_provider(db, provider_name)
    if not provider:
        # Honest "we don't recognise that insurer", never silently
        # matched to the wrong one and never assumed "not covered".
        return {
            "test_found": True,
            "test_name": t.name,
            "provider_found": False,
            "query_provider": provider_name,
        }

    policy = db.query(InsurancePolicy).filter_by(test_id=t.id, provider_id=provider.id).first()
    if policy is None or policy.coverage_status is None:
        # No reviewed (test, provider) row -- honest "we don't know yet",
        # never a guessed COVERED/NOT_COVERED (see models.py's own
        # comment on InsurancePolicy.coverage_status).
        return {
            "test_found": True,
            "test_name": t.name,
            "provider_found": True,
            "provider_name": provider.name,
            "policy_available": False,
        }
    return {
        "test_found": True,
        "test_name": t.name,
        "provider_found": True,
        "provider_name": provider.name,
        "policy_available": True,
        "coverage_status": policy.coverage_status,
        "pre_auth_required": policy.pre_auth_required,
    }


# =============================================================================
# Tool 2: GET /api/v1/doctors/availability?name=...&date=YYYY-MM-DD (optional)
# Tool 2b: GET /api/v1/doctors/by-department?department=...
# =============================================================================
# FUZZY_SURNAME_FLOOR -- LOW confidence, reasoned not measured (no real call
# audio to calibrate against yet, unlike voicerx/gate.py's SIMILARITY_FLOOR).
#
# This exists because of a bug caught in local testing: matching the raw
# query against the full formatted name ("Dr. A. Sen") let a query for
# "Doctor Nobody" fuzzy-match "Dr. N. Roy" at ratio 0.522 -- HIGHER than the
# ratio for a real garbled name against its own doctor ("sen" vs "Dr. A. Sen"
# scores only 0.462, because SequenceMatcher penalizes the length mismatch
# against the "Dr. X." prefix on both sides, so short queries and wrong
# queries land in the same range). That is this system's own small version
# of the "Naloxone" bug: confidently answering with the wrong doctor's real
# schedule instead of saying "not found".
#
# Fix: match against the SURNAME only, which cleanly separates the two
# cases in testing -- genuine garbles (e.g. "mukharji" vs "Mukherjee")
# scored 0.70-0.80; unrelated queries (e.g. "doctor nobody" vs "Roy")
# scored <=0.44. 0.60 sits in the gap. Recalibrate once real call audio
# exists, the same way gate.py's floors were tightened from real samples.
FUZZY_SURNAME_FLOOR = 0.60


# story title: Near matches are offered rather than guessed or refused
# user story: As a caller naming something loosely, I want the close matches
#   offered, so that I am not told my test does not exist when it does.
# acceptance criteria: When several catalogue rows fall within the match band
#   the agent offers up to three by name and asks which. Candidates are
#   generated across every supported language and romanised spelling. The
#   did-you-mean path covers the ambiguous case and not only total failure.
#
# The surname is passed as an extra form because it is what callers actually
# say -- "Sen", not "Dr. A. Sen" -- and because it is the form that makes the
# tiering matter: "সেন" EQUALS Dr Sen's alias (1.0) and is CONTAINED IN Dr
# Sengupta's (0.90), so it clears the margin and commits, where a flat
# containment score for both would have asked the caller to choose between a
# doctor they named exactly and one they did not.
#
# FUZZY_SURNAME_FLOOR's 0.60 is no longer a commit threshold -- match_band
# commits at 0.72 and OFFERS between 0.50 and 0.72. That band used to be a
# silent commit. The Doctor Nobody incident that set 0.60 in the first place
# (a wrong doctor matched at 0.522) lands in it: the caller is now asked
# "did you mean Dr Roy?" and can say no, instead of being answered about a
# doctor they never named.
def _resolve_doctor(db: Session, name: str) -> tuple[str, Doctor | None, list[Doctor]]:
    """-> (verdict, the one doctor if committing, the rows to offer)."""
    all_doctors = db.query(Doctor).all()
    rows = [(d, _forms(d, extra=[d.name.split()[-1]])) for d in all_doctors]
    verdict, candidates = match_band.decide(match_band.rank(name, rows))
    if verdict == match_band.COMMIT:
        return verdict, candidates[0].key, []
    return verdict, None, [c.key for c in candidates]


# story title: Near matches are offered rather than guessed or refused
# user story: As a caller naming something loosely, I want the close matches
#   offered, so that I am not told my test does not exist when it does.
# acceptance criteria: When several catalogue rows fall within the match band
#   the agent offers up to three by name and asks which. Candidates are
#   generated across every supported language and romanised spelling. The
#   did-you-mean path covers the ambiguous case and not only total failure.
#
# Departments are the one entity type whose aliases ALREADY span scripts --
# "কার্ডিওলজি", "হার্ট", "heart", "cardio". They go into the pool exactly as
# they are, which is why the criterion's romanised-spelling clause is met here
# and not for tests or doctors: it is a data question, and the seed rows for
# those two carry Bengali script plus the English name only. Noted as the
# known gap rather than closed, because seed() is destructive and reseeding is
# an operational decision, not a deploy.
def _resolve_department(db: Session, department_name: str) -> tuple[str, Department | None, list[Department]]:
    """-> (verdict, the one department if committing, the rows to offer)."""
    all_departments = db.query(Department).all()
    verdict, candidates = match_band.decide(
        match_band.rank(department_name, [(d, _forms(d)) for d in all_departments])
    )
    if verdict == match_band.COMMIT:
        return verdict, candidates[0].key, []
    return verdict, None, [c.key for c in candidates]


def _schedule_for_weekday(db: Session, doctor_id: int, weekday: int) -> DoctorSchedule | None:
    return db.query(DoctorSchedule).filter_by(doctor_id=doctor_id, weekday=weekday).first()


def _next_available_date(
    db: Session, doctor_id: int, from_date: datetime.date, horizon_days: int = 14
) -> str | None:
    for offset in range(horizon_days):
        d = from_date + datetime.timedelta(days=offset)
        if _schedule_for_weekday(db, doctor_id, d.weekday()):
            return d.isoformat()
    return None


@app.get("/api/v1/doctors/availability")
def doctor_availability(
    name: str = Query(...), date: str | None = Query(None), db: Session = Depends(get_db)
):
    verdict, doctor, offered = _resolve_doctor(db, name)
    if verdict == match_band.AMBIGUOUS:
        return _ambiguous_reply(name, offered)
    if not doctor:
        return {"found": False, "query": name}

    today = datetime.date.today()

    if date:
        try:
            target = datetime.date.fromisoformat(date)
        except ValueError:
            return {"found": False, "query": name}
        sched = _schedule_for_weekday(db, doctor.id, target.weekday())
        if sched:
            return {
                "found": True,
                "doctor_name": doctor.name,
                "doctor_name_bn": _first_alias_bn(doctor.aliases_bn),
                "date": target.isoformat(),
                "available": True,
                "chamber_hours": f"{sched.start_time}-{sched.end_time}",
                "next_available_date": None,
            }
        next_date = _next_available_date(db, doctor.id, target + datetime.timedelta(days=1))
        return {
            "found": True,
            "doctor_name": doctor.name,
            "doctor_name_bn": _first_alias_bn(doctor.aliases_bn),
            "date": target.isoformat(),
            "available": False,
            "chamber_hours": None,
            "next_available_date": next_date,
        }

    # No date given -> "when is this doctor next available"
    next_date = _next_available_date(db, doctor.id, today)
    if not next_date:
        return {
            "found": True,
            "doctor_name": doctor.name,
            "doctor_name_bn": _first_alias_bn(doctor.aliases_bn),
            "date": None,
            "available": False,
            "chamber_hours": None,
            "next_available_date": None,
        }
    sched = _schedule_for_weekday(db, doctor.id, datetime.date.fromisoformat(next_date).weekday())
    return {
        "found": True,
        "doctor_name": doctor.name,
        "doctor_name_bn": _first_alias_bn(doctor.aliases_bn),
        "date": next_date,
        "available": True,
        "chamber_hours": f"{sched.start_time}-{sched.end_time}",
        "next_available_date": None,
    }


@app.get("/api/v1/doctors/schedule")
def doctor_schedule(name: str = Query(...), db: Session = Depends(get_db)):
    """ADDED BY SOURAV -- "Caller asks when a doctor sits" story.

    Deliberately DATE-FREE, unlike doctor_availability() just above. That
    endpoint always resolves to one particular day (today, an explicit
    date, or the computed "next available" day) because it answers "is
    the doctor in THEN". This endpoint answers a different, more general
    question a caller actually asks in practice -- "which days does Dr X
    usually sit?" -- with no date involved at all: it returns the
    doctor's FULL recurring weekly schedule, every DoctorSchedule row
    they have, in weekday order (0=Monday .. 6=Sunday, matching
    DoctorSchedule's own docstring). The caller-facing wording is built
    from this list in agent/reply_templates.py::doctor_schedule_reply().

    Response shapes:
      found=false: {"found": false, "query": "..."}
      found=true, doctor has 1+ scheduled weekdays:
        {"found": true, "doctor_name": "...", "doctor_name_bn": "...",
         "schedule": [{"weekday": 0, "start_time": "10:00", "end_time": "12:00"}, ...]}
      found=true, doctor exists but has ZERO DoctorSchedule rows (a real,
      honest edge case -- e.g. a doctor on indefinite leave with no
      chamber days configured at all): "schedule": [] -- the caller-facing
      reply function must say so plainly rather than fabricating a day.
    """
    # FIXED -- this called _find_doctor(db, name), which is never defined
    # anywhere in this module and would raise NameError on any real call.
    # See book_appointment()'s comment further down, which already
    # documented this as a latent bug inherited from dev_chakravardhan's
    # branch. Switched to _resolve_doctor(), the same ambiguity-aware
    # lookup doctor_availability() and book_appointment() already use, so
    # a name matching more than one doctor is offered as candidates
    # rather than silently resolving to whichever query happened to
    # return first.
    verdict, doctor, offered = _resolve_doctor(db, name)
    if verdict == match_band.AMBIGUOUS:
        return _ambiguous_reply(name, offered)
    if not doctor:
        return {"found": False, "query": name}

    rows = (
        db.query(DoctorSchedule).filter_by(doctor_id=doctor.id).order_by(DoctorSchedule.weekday.asc()).all()
    )
    return {
        "found": True,
        "doctor_name": doctor.name,
        "doctor_name_bn": _first_alias_bn(doctor.aliases_bn),
        "schedule": [
            {"weekday": r.weekday, "start_time": r.start_time, "end_time": r.end_time} for r in rows
        ],
    }


@app.get("/api/v1/doctors/by-department")
def doctors_by_department(
    department: str = Query(...), date: str | None = Query(None), db: Session = Depends(get_db)
):
    """Get all doctors in a department, supports short forms like 'ortho' for
    Orthopaedics.

    `date` is OPTIONAL, same convention as /doctors/availability above.
    Omit it for a plain "who's in this department" listing (every doctor,
    unfiltered -- unchanged from before this parameter existed). Pass it
    for "which ortho doctor is available TODAY/that day": the list is
    filtered down to doctors who actually have a DoctorSchedule row for
    that date's weekday, and each gets its chamber_hours attached, mirroring
    what doctor_availability() already reports for a single named doctor.
    """
    verdict, dept, offered = _resolve_department(db, department)
    if verdict == match_band.AMBIGUOUS:
        return _ambiguous_reply(department, offered)
    if not dept:
        return {"found": False, "query": department}

    doctors = db.query(Doctor).filter_by(department_id=dept.id).all()

    target = None
    if date:
        try:
            target = datetime.date.fromisoformat(date)
        except ValueError:
            target = None  # malformed date -- fall back to the unfiltered listing

    out = []
    for d in doctors:
        entry = {
            "name": d.name,
            "doctor_name_bn": _first_alias_bn(d.aliases_bn),
            "qualifications": d.qualifications,
        }
        if target is not None:
            sched = _schedule_for_weekday(db, d.id, target.weekday())
            if not sched:
                continue  # doesn't sit that day -- excluded, not just flagged
            entry["chamber_hours"] = f"{sched.start_time}-{sched.end_time}"
        out.append(entry)

    return {
        "found": True,
        "department": dept.name,
        # STORY [Answer Quality and Grounding]
        # As a patient, I want to hear the whole sentence, so that I am
        # not left guessing what the agent tried to say.
        # The SPOKEN department name. Without it the agent's listing reply
        # reads "<English> বিভাগে ... আছেন" and the Bengali tokenizer drops the
        # Latin word, so the caller loses the SUBJECT of the sentence -- on
        # every one of the eight seeded departments, not an edge case. Same
        # helper, same reason, as test_name_bn and doctor_name_bn above; every
        # department is seeded with a Bengali alias first (see seed.py's
        # DEPARTMENT_ALIASES), so this needs no new data.
        "department_bn": _first_alias_bn(dept.aliases_bn),
        "date": target.isoformat() if target else None,
        "doctors": out,
    }


# =============================================================================
# Tool 3: POST /api/v1/appointments
# =============================================================================
class BookingRequest(BaseModel):
    doctor_name: str
    date: str
    time_slot: str
    patient_name: str
    phone: str


def _generate_slots(start: str, end: str, step_min: int = SLOT_STEP_MIN) -> list[str]:
    t = datetime.datetime.strptime(start, "%H:%M")
    end_t = datetime.datetime.strptime(end, "%H:%M")
    slots = []
    while t < end_t:
        slots.append(t.strftime("%H:%M"))
        t += datetime.timedelta(minutes=step_min)
    return slots


def _live_slots(db: Session, doctor_id: int, date: str) -> set[str]:
    """Which of a doctor's slots are actually held on a date.

    `slot_lock == SLOT_LOCK_ACTIVE` is the filter that makes cancellation
    mean something: a cancelled row is kept for the audit trail and for the
    notification ledger that points at it, but it must stop occupying the
    slot it released. See the Appointment docstring in models.py.

    Every "is this taken" question in this file goes through here rather
    than repeating the filter, because forgetting it in one place is a bug
    that presents as "the slot I cancelled can never be rebooked" -- and
    only for the callers unlucky enough to want that slot.
    """
    return {
        a.time_slot
        for a in db.query(Appointment)
        .filter_by(
            doctor_id=doctor_id,
            date=date,
            slot_lock=SLOT_LOCK_ACTIVE,
        )
        .all()
    }


def _prepare_notification(
    db: Session, appt: Appointment, event: str, doctor_name: str
) -> NotificationAttempt | None:
    """Stage the patient's written confirmation IN THE CALLER'S TRANSACTION.

    MUST be called BEFORE the commit that persists the appointment change,
    and never after it. That ordering is the whole reason "silently
    dropped" is not a state this system can reach:

      * one commit persists the appointment change and the ledger row
        together, so there is no window in which a booking exists and no
        record says a message was due;
      * if the commit fails or the booking loses a slot race, the rollback
        discards BOTH -- no orphan ledger row promising a message for an
        appointment that was never made.

    This works because `confirmation_id` is generated in Python before the
    insert, not by the database, so the message can be composed against an
    appointment row that has not been written yet.

    Returns None if the row could not even be staged. Nothing in here may
    raise into the caller: a booking that succeeded must never be reported
    as failed because an SMS could not be composed.
    """
    try:
        return notify_service.queue_message(db, appt, event, doctor_name)
    except Exception:  # noqa: BLE001
        logging.getLogger("clinic-api").exception(
            "could not stage a %s notification for %s (patient %s on %s) -- the appointment itself stands",
            event,
            appt.confirmation_id,
            appt.patient_name,
            appt.phone,
        )
        return None


def _schedule_delivery(background: BackgroundTasks, attempt: NotificationAttempt | None, event: str) -> dict:
    """Hand a committed ledger row to the background sender, and describe it
    for the response.

    Called AFTER the commit, so the row the background task will re-read
    definitely exists -- a task scheduled against an uncommitted row would
    find nothing when it opened its own session.
    """
    if attempt is None:
        return {"status": "not_recorded", "event": event}

    if attempt.status == notify.STATUS_QUEUED:
        background.add_task(notify_service.deliver_now, attempt.id)

    return {
        "id": attempt.id,
        "event": event,
        "status": attempt.status,
        "error_code": attempt.error_code,
    }


@app.post("/api/v1/appointments")
def book_appointment(req: BookingRequest, background: BackgroundTasks, db: Session = Depends(get_db)):
    # story title: Near matches are offered rather than guessed or refused
    # user story: As a caller naming something loosely, I want the close matches
    #   offered, so that I am not told my test does not exist when it does.
    # acceptance criteria: When several catalogue rows fall within the match band
    #   the agent offers up to three by name and asks which. Candidates are
    #   generated across every supported language and romanised spelling. The
    #   did-you-mean path covers the ambiguous case and not only total failure.
    #
    # A WRITE NEVER PROCEEDS UNDER AMBIGUITY. Booking the higher-scoring of
    # two plausible doctors is the worst version of this bug: the caller
    # leaves believing they have an appointment, and they do -- with someone
    # else. The refusal carries the candidates so the agent can ask, rather
    # than reporting "no such doctor" for a doctor who exists twice over.
    #
    # ADDED BY CHAKRAVARDHAN -- `background: BackgroundTasks` (needed below,
    # unconflicted, by _schedule_delivery()) merged onto Sourav's doctor
    # ambiguity resolution rather than dev_chakravardhan's simpler
    # _find_doctor(), which was never defined anywhere in this module (it
    # would raise NameError). doctor_schedule() a few functions above had
    # the same latent call and has since been fixed to use _resolve_doctor()
    # too -- see the FIXED comment on its own body.
    verdict, doctor, offered = _resolve_doctor(db, req.doctor_name)
    if verdict == match_band.AMBIGUOUS:
        return {"success": False, "reason": "doctor_ambiguous", "candidates": _candidate_dicts(offered)}
    if not doctor:
        return {"success": False, "reason": "doctor_not_found"}

    try:
        target = datetime.date.fromisoformat(req.date)
    except ValueError:
        return {"success": False, "reason": "missing_field"}

    sched = _schedule_for_weekday(db, doctor.id, target.weekday())
    if not sched:
        # Doctor doesn't sit that day at all -- not in the caller-facing
        # reason enum reply_templates.booking_reply() specifically handles,
        # so it falls to that function's generic "couldn't book" message,
        # which remains true and safe rather than a false "slot taken".
        return {"success": False, "reason": "doctor_not_available_that_day"}

    valid_slots = _generate_slots(sched.start_time, sched.end_time)
    if req.time_slot not in valid_slots:
        return {"success": False, "reason": "slot_taken", "alternative_slots": valid_slots[:3]}

    def _free_slots() -> list[str]:
        taken = _live_slots(db, doctor.id, req.date)
        return [s for s in valid_slots if s not in taken][:3]

    if req.time_slot in _live_slots(db, doctor.id, req.date):
        return {"success": False, "reason": "slot_taken", "alternative_slots": _free_slots()}

    confirmation_id = f"KCD-{req.date.replace('-', '')}-{uuid.uuid4().hex[:4].upper()}"
    appt = Appointment(
        confirmation_id=confirmation_id,
        doctor_id=doctor.id,
        date=req.date,
        time_slot=req.time_slot,
        patient_name=req.patient_name,
        phone=req.phone,
        created_at=datetime.datetime.now(),
        status=APPT_BOOKED,
        slot_lock=SLOT_LOCK_ACTIVE,
    )

    # Staged BEFORE the commit below, deliberately, so one transaction
    # carries the booking and the record that a message is owed for it. If
    # the commit loses the slot race, the rollback discards both.
    attempt = _prepare_notification(db, appt, mt.EVENT_BOOKED, doctor.name)

    # THE CHECK ABOVE IS NOT ATOMIC WITH THIS INSERT.
    #
    # Two callers being told the same slot is free, then both booking it, is
    # a genuine interleaving -- and it is MOST likely at peak, when the same
    # popular slots are being offered to several people at once.
    #
    # models.py's UniqueConstraint("doctor_id", "date", "time_slot") already
    # makes that safe for the DATA: the second insert cannot succeed. What it
    # did not do was make it safe for the CALLER. The IntegrityError was
    # uncaught, so it surfaced as a 500, which tools_client.py turns into a
    # ToolCallError, which main.py speaks as "এই মুহূর্তে দেখতে পারছি না" --
    # "I can't check right now". That is misleading: the system checked
    # perfectly well and knows exactly what happened.
    #
    # Catching it here turns the race into the honest answer the caller
    # deserves -- "that slot just went, here are three others" -- reusing the
    # same slot_taken shape reply_templates.booking_reply() already handles.
    try:
        db.add(appt)
        db.commit()
    except IntegrityError:
        db.rollback()
        if req.time_slot in _live_slots(db, doctor.id, req.date):
            logging.getLogger("clinic-api").info(
                "slot %s on %s for doctor %s lost a booking race -- offering alternatives",
                req.time_slot,
                req.date,
                doctor.name,
            )
            return {"success": False, "reason": "slot_taken", "alternative_slots": _free_slots()}

        # The slot is still free, so the collision was on the only other
        # unique column, confirmation_id -- a 16^4 UUID-suffix clash. Do not
        # dress this up as slot_taken; it is not, and telling the caller to
        # pick another time would be a lie. booking_reply() renders an
        # unrecognised reason as its generic "couldn't book" message.
        logging.getLogger("clinic-api").warning(
            "unexpected IntegrityError booking %s on %s (slot still free) -- "
            "likely confirmation_id collision",
            req.time_slot,
            req.date,
        )
        return {"success": False, "reason": "booking_failed"}

    # The booking AND its ledger row are now committed together.
    notification = _schedule_delivery(background, attempt, mt.EVENT_BOOKED)

    return {
        "success": True,
        "confirmation_id": confirmation_id,
        "doctor_name": doctor.name,
        "doctor_name_bn": _first_alias_bn(doctor.aliases_bn),
        "date": req.date,
        "time_slot": req.time_slot,
        # What the patient is owed in writing, so reply_templates.py can
        # decide whether to promise them a message. `queued` at this point
        # is the normal case -- the send has not been attempted yet.
        "notification": notification,
    }


# =============================================================================
# NOTE (merge dev_chakravardhan -> staging_merged): both branches
# independently added a section labelled "Tool 5" at this same point in the
# file -- Sourav's report-status/delivery/OTP story below, and
# Chakravardhan's reschedule/cancel/notifications/history-verification
# stories further down (search "=== merged: dev_chakravardhan's additions
# below ===" for the boundary). They touch entirely disjoint routes,
# classes and helper names (confirmed by diffing every `def`/`class`/`@app.`
# line on each side), so both are kept in full rather than one replacing
# the other.
# =============================================================================
# Tool 5: GET /api/v1/reports/status?phone=...&test_name=... (optional)
# Tool 6: POST /api/v1/reports/delivery/request
# Tool 7: POST /api/v1/reports/otp/verify
# Tool 8: GET /api/v1/reports/link/{token}
#
# ADDED BY SOURAV -- "Lab Report Status & Secure Delivery" combined story.
# See this file's module docstring for the rule references. Every helper
# and endpoint below is new; nothing above this line was touched except
# the import block.
# =============================================================================

OTP_VALIDITY_MINUTES = 10
SIGNED_LINK_VALIDITY_MINUTES = 15
# UPDATED BY SOURAV -- "otp will not be hardcoded" (the user's own
# explicit instruction, superseding the earlier prototype decision this
# comment used to document: "the otp [is] hardcoded, for now user will
# tell the otp and the matching will be done"). A report with no
# pre-seeded ReportOTP row (or whose only rows are used/expired/maxed)
# now gets a genuinely random code from models.generate_otp_code() every
# time delivery is requested live, exactly like every other ReportOTP
# row (seeded or live) already does -- see that function's own docstring.
# Seeded patients that already carry their own ReportOTP row (Arjun/
# Sohini/Amit -- see seed.py SECTION 9) still take priority via the
# `reusable` check just below; this path only fires for reports with no
# usable row yet (e.g. Mita's and the two Rahul Das reports).
#
# Whoever actually needs the code now (a real caller, or a tester with no
# provider connected) gets it via send_otp_via_provider() -- best-effort,
# see clinic-api/otp_messaging_config.py -- or, with no provider
# connected, by reading the freshly-created ReportOTP row directly from
# the database, the same way this file's own test suite already does.


def _mask_phone_last4(phone: str) -> str:
    """Rule 10: never say the full registered number back to the caller.
    Used only in this file's own response payloads (masked_phone) --
    reply_templates.py has its own copy for anything it composes
    directly from a phone slot the caller spoke, since that module must
    not import from clinic-api (they are separate deployables)."""
    digits = "".join(c for c in phone if c.isdigit())
    return digits[-4:] if len(digits) >= 4 else digits


def _find_patient_by_phone(db: Session, phone: str) -> Patient | None:
    return db.query(Patient).filter_by(phone=phone).first()


def _report_summary(report: LabReport, test_name: str) -> dict:
    return {
        "report_number": report.report_number,
        "test_name": test_name,
        "status": report.status,
        "delivery_enabled": report.delivery_enabled,
        "expected_ready_at": report.expected_ready_at.isoformat() if report.expected_ready_at else None,
        "ready_at": report.ready_at.isoformat() if report.ready_at else None,
    }


@app.get("/api/v1/reports/status")
def report_status(
    phone: str = Query(...), test_name: str | None = Query(None), db: Session = Depends(get_db)
):
    """RULE 1 (never invent a report), RULE 13 (multiple reports require
    clarification), RULE 14 (same-name patients must not be merged).

    Identity is resolved by PHONE, never by name -- this is what makes
    RULE 14 hold structurally rather than by convention: Patient.phone is
    a unique column (see models.py), so two "Rahul Das" rows can never
    collide here regardless of how the caller pronounces the name. A
    caller who states only a name and no phone is a slot the voice agent
    must ask for BEFORE calling this endpoint at all (see main_pcm.py's
    new "phone" pending state) -- this endpoint has no name-based lookup
    path to accidentally fall back to.
    """
    patient = _find_patient_by_phone(db, phone)
    if not patient:
        return {"patient_found": False}

    reports = db.query(LabReport).filter_by(patient_id=patient.id).all()
    if not reports:
        # RULE 1: an honest NOT_FOUND, never a fabricated report.
        return {"patient_found": True, "found": False, "reason": "NOT_FOUND"}

    lab_test_ids = {r.lab_test_id for r in reports}
    tests_by_id = {t.id: t for t in db.query(LabTest).filter(LabTest.id.in_(lab_test_ids)).all()}

    if test_name:
        matches = [r for r in reports if test_name.lower() in tests_by_id[r.lab_test_id].name.lower()]
    else:
        matches = reports

    if not matches:
        # A test_name was given but nothing this patient has matches it --
        # honest NOT_FOUND rather than silently falling back to "all
        # reports" (which would let a misheard test name return an
        # unrelated report).
        return {"patient_found": True, "found": False, "reason": "NOT_FOUND"}

    if len(matches) > 1:
        # RULE 13: multiple candidates -> ask, never guess.
        return {
            "patient_found": True,
            "found": False,
            "reason": "AMBIGUOUS",
            "candidates": [_report_summary(r, tests_by_id[r.lab_test_id].name) for r in matches],
        }

    report = matches[0]
    return {
        "patient_found": True,
        "found": True,
        **_report_summary(report, tests_by_id[report.lab_test_id].name),
    }


def _blocking_reason_for(report: LabReport) -> str | None:
    """RULE 3 / RULE 16: only a READY, delivery-enabled report may enter
    the delivery flow. Returns the specific reason delivery is blocked,
    or None if it is allowed to proceed -- used identically by both the
    delivery-request and otp-verify endpoints below, so a report that
    changes state between those two calls (e.g. cancelled in between) is
    re-checked, not trusted from the first call."""
    if report.status != "READY":
        return report.status  # NOT_READY / PROCESSING / CANCELLED
    if not report.delivery_enabled:
        return "DELIVERY_DISABLED"
    return None


def _resolve_patient_and_report(db: Session, phone: str, report_number: str):
    """Shared identity+report resolution for delivery/request and
    otp/verify. Returns (patient, report, error_reason). error_reason is
    None on success.

    RULE 15: the report must belong to the PHONE-resolved patient, not
    merely exist. A real report_number for a DIFFERENT patient (ATTACK
    15) and a nonexistent one both return the same "NOT_FOUND" -- never a
    distinct "wrong patient" reason, which would let an attacker probe
    which report numbers are real by watching the error change.
    """
    patient = _find_patient_by_phone(db, phone)
    if not patient:
        return None, None, "PATIENT_NOT_FOUND"

    report = db.query(LabReport).filter_by(report_number=report_number).first()
    if not report or report.patient_id != patient.id:
        return patient, None, "NOT_FOUND"

    return patient, report, None


class DeliveryRequest(BaseModel):
    phone: str
    report_number: str


@app.post("/api/v1/reports/delivery/request")
def request_report_delivery(req: DeliveryRequest, db: Session = Depends(get_db)):
    """RULE 4 (OTP required before delivery), RULE 6/7/8 (an exhausted,
    expired or already-used OTP is never silently reused -- a fresh
    request always gets a fresh, USABLE OTP row instead)."""
    patient, report, error = _resolve_patient_and_report(db, req.phone, req.report_number)
    if error:
        return {"success": False, "reason": error}

    blocked = _blocking_reason_for(report)
    if blocked:
        return {"success": False, "reason": blocked}

    now = datetime.datetime.now()

    # Reuse the most recent OTP row for this (report, patient) ONLY if it
    # is still usable. RULE 8's "require a new verification flow" is what
    # this is: a maxed-out, expired or already-used row is treated as
    # dead, and a brand-new row is minted instead of ever handing the
    # same exhausted OTP back out.
    active = (
        db.query(ReportOTP)
        .filter_by(report_id=report.id, patient_id=patient.id)
        # UPDATED BY SOURAV -- tie-break on id, not just created_at.
        # Two OTP rows for the same (report, patient) can share an
        # identical created_at timestamp (Python's datetime.now() and
        # SQLite's storage resolution both make this a real possibility,
        # not just a seed-data artifact -- clinic-api/seed.py's own
        # Patient E rows originally did exactly this before being fixed
        # to use distinct timestamps). Without a secondary sort key,
        # "most recent" was undefined on a tie -- caught by
        # tests/test_clinic_api_reports.py::TestOtpVerify::
        # test_max_attempts_already_reached returning OTP_EXPIRED instead
        # of OTP_MAX_ATTEMPTS. `id` is autoincrement, so it is a reliable
        # insertion-order tie-breaker regardless of what the clock reads.
        .order_by(ReportOTP.created_at.desc(), ReportOTP.id.desc())
        .first()
    )
    reusable = (
        active is not None
        and not active.used
        and active.expires_at > now
        and active.attempt_count < active.max_attempts
    )

    if not reusable:
        active = ReportOTP(
            report_id=report.id,
            patient_id=patient.id,
            phone=patient.phone,
            otp_code=generate_otp_code(),
            created_at=now,
            expires_at=now + datetime.timedelta(minutes=OTP_VALIDITY_MINUTES),
            used=False,
            attempt_count=0,
        )
        db.add(active)
        db.commit()

    # ADDED BY SOURAV -- "otp will not be hardcoded". Every delivery
    # request -- whether it just minted a fresh row above or is reusing
    # an existing valid one -- attempts to actually push the current
    # active code out through whatever provider a deploying company has
    # configured (see otp_messaging_config.py, the one file they need to
    # touch). Best-effort and never blocking: `active`'s row is already
    # committed to the database by this point regardless of whether this
    # send succeeds, matching this whole file's "fail safe, not fail
    # open" posture (module docstring) -- a provider hiccup must never be
    # the reason a patient can't proceed. send_otp_via_provider() already
    # promises never to raise on its own, but this endpoint's own success
    # response is still wrapped in a try/except here too -- defense in
    # depth, the same posture every other tie-break/edge case in this
    # file already takes, rather than resting entirely on another file's
    # contract holding forever.
    try:
        send_otp_via_provider(patient.phone, active.otp_code)
    except Exception as e:
        logging.getLogger("clinic-api").error(
            "send_otp_via_provider() raised unexpectedly for report %s: %s",
            report.report_number,
            e,
        )

    return {
        "success": True,
        "reason": "OTP_REQUIRED",
        "masked_phone": _mask_phone_last4(patient.phone),
    }


class OtpVerifyRequest(BaseModel):
    phone: str
    report_number: str
    otp_code: str
    # SOURAV: test-only hook for CASE 2 ("Delivery failure tests" /
    # provider failure) in the attack plan's Section 9. The voice agent
    # itself never sets this -- there is no real SMS/e-mail provider in
    # this prototype to fail on its own, so this is how the automated
    # test suite deterministically exercises "OTP was right, but the
    # provider failed" (RULE 17) without needing real delivery infra.
    simulate_delivery_failure: bool = False


@app.post("/api/v1/reports/otp/verify")
def verify_report_otp(req: OtpVerifyRequest, db: Session = Depends(get_db)):
    """The entire OTP attack surface (plan Section 6) lives in this one
    function, in a fixed order, on purpose -- RULE 9 says never reveal
    the correct OTP, so the code is compared LAST, after every other
    reason to refuse has already been ruled out; an attacker never
    learns anything about the correct value from which check fired."""
    patient, report, error = _resolve_patient_and_report(db, req.phone, req.report_number)
    if error:
        return {"success": False, "reason": error}

    blocked = _blocking_reason_for(report)
    if blocked:
        return {"success": False, "reason": blocked}

    now = datetime.datetime.now()

    active = (
        db.query(ReportOTP)
        .filter_by(report_id=report.id, patient_id=patient.id)
        # UPDATED BY SOURAV -- tie-break on id, not just created_at.
        # Two OTP rows for the same (report, patient) can share an
        # identical created_at timestamp (Python's datetime.now() and
        # SQLite's storage resolution both make this a real possibility,
        # not just a seed-data artifact -- clinic-api/seed.py's own
        # Patient E rows originally did exactly this before being fixed
        # to use distinct timestamps). Without a secondary sort key,
        # "most recent" was undefined on a tie -- caught by
        # tests/test_clinic_api_reports.py::TestOtpVerify::
        # test_max_attempts_already_reached returning OTP_EXPIRED instead
        # of OTP_MAX_ATTEMPTS. `id` is autoincrement, so it is a reliable
        # insertion-order tie-breaker regardless of what the clock reads.
        .order_by(ReportOTP.created_at.desc(), ReportOTP.id.desc())
        .first()
    )
    if active is None:
        return {"success": False, "reason": "OTP_NOT_REQUESTED"}

    if active.used:
        # RULE 7: an OTP is single-use, full stop -- even if the caller
        # types the exact right value again.
        return {"success": False, "reason": "OTP_ALREADY_USED"}

    if active.expires_at <= now:
        # RULE 6.
        return {"success": False, "reason": "OTP_EXPIRED"}

    if active.attempt_count >= active.max_attempts:
        # RULE 8 -- checked BEFORE the code comparison, so a caller who
        # is already locked out never gets another "wrong"/"right"
        # signal about a code that no longer matters.
        return {"success": False, "reason": "OTP_MAX_ATTEMPTS"}

    if active.otp_code != req.otp_code:
        # RULE 9: the response says only that it was wrong, never what
        # the right one is.
        active.attempt_count += 1
        db.commit()
        if active.attempt_count >= active.max_attempts:
            return {"success": False, "reason": "OTP_MAX_ATTEMPTS"}
        return {"success": False, "reason": "OTP_INVALID"}

    # Correct code, and every gate above passed -- RULE 5 held throughout
    # because `active` was scoped to (report.id, patient.id) from the
    # very first query above: an OTP that is valid for a DIFFERENT report
    # or patient is simply never the row being compared against here, so
    # ATTACK 5/6 (cross-report / cross-patient OTP reuse) fail not
    # because of a special case, but because the right row was never
    # found in the first place.
    active.used = True
    active.verified_at = now
    db.commit()

    if req.simulate_delivery_failure:
        # RULE 17: OTP success must NOT be conflated with delivery
        # success -- the two are recorded and reported separately.
        delivery = ReportDelivery(
            report_id=report.id,
            patient_id=patient.id,
            recipient=f"registered contact for {patient.phone}",
            delivery_channel="EMAIL",
            verification_status="VERIFIED",
            delivery_status="FAILED",
            created_at=now,
            verified_at=now,
            failed_at=now,
            failure_reason="DELIVERY_PROVIDER_FAILURE",
            audit_note="OTP verified successfully; delivery provider failed on send.",
        )
        db.add(delivery)
        db.commit()
        return {"success": False, "reason": "DELIVERY_FAILED"}

    token = secrets.token_urlsafe(16)
    expires_at = now + datetime.timedelta(minutes=SIGNED_LINK_VALIDITY_MINUTES)
    delivery = ReportDelivery(
        report_id=report.id,
        patient_id=patient.id,
        recipient=f"registered contact for {patient.phone}",
        delivery_channel="EMAIL",
        verification_status="VERIFIED",
        delivery_status="SENT",
        signed_link_token=token,
        signed_link_expires_at=expires_at,
        created_at=now,
        verified_at=now,
        sent_at=now,
        audit_note="OTP verified; report delivered successfully.",
    )
    db.add(delivery)
    db.commit()

    return {
        "success": True,
        "reason": "DELIVERY_SENT",
        "masked_phone": _mask_phone_last4(patient.phone),
        "signed_link_expires_minutes": SIGNED_LINK_VALIDITY_MINUTES,
    }


@app.get("/api/v1/reports/link/{token}")
def validate_report_link(token: str, db: Session = Depends(get_db)):
    """RULE 11 (links must expire), RULE 12 (no direct access without
    authorization). This is an HTTP-level endpoint, not something the
    voice agent itself calls -- the caller never speaks a token back
    over the phone. It exists so the plan's Section 8 link/token attacks
    (23-28: expired, reused, modified, empty, random, cross-report) have
    something real to attack, matching a real "click the link we sent
    you" step in the actual delivery channel."""
    if not token:
        return {"valid": False, "reason": "LINK_INVALID"}

    delivery = db.query(ReportDelivery).filter_by(signed_link_token=token).first()
    if not delivery:
        # Covers a genuinely random token, an empty one, AND a modified
        # one (flipping one character of a real token overwhelmingly
        # lands on a value nothing in signed_link_token equals) --
        # ATTACK 25/26/27 all resolve to this same branch, which is the
        # point: a corrupted token carries no partial credit.
        return {"valid": False, "reason": "LINK_INVALID"}

    if not delivery.signed_link_expires_at or delivery.signed_link_expires_at <= datetime.datetime.now():
        return {"valid": False, "reason": "LINK_EXPIRED"}

    linked_report = db.query(LabReport).filter_by(id=delivery.report_id).first()
    return {
        "valid": True,
        "report_number": linked_report.report_number if linked_report else None,
    }


# =============================================================================
# Tool 16: GET /api/v1/patient/billing?phone=...
#
# ADDED BY SOURAV -- Phase 1: Database Schema & Policy Tables. Outstanding
# Balance / Billing story. Identity resolved by PHONE (RULE 14/15), same
# as /api/v1/reports/status above, reusing the same _find_patient_by_phone
# helper -- deliberately NOT gated behind OTP verification the way report
# delivery is (see models.py's PatientBilling docstring for that scoping
# decision).
# =============================================================================


@app.get("/api/v1/patient/billing")
def patient_billing(phone: str = Query(...), db: Session = Depends(get_db)):
    patient = _find_patient_by_phone(db, phone)
    if not patient:
        return {"patient_found": False}

    billing = db.query(PatientBilling).filter_by(patient_id=patient.id).first()
    if billing is None or billing.outstanding_amount is None:
        # No billing record for this patient at all -- honest NOT_FOUND,
        # never a guessed/defaulted zero balance (see models.py's
        # PatientBilling docstring: no row and a real 0.0 are different,
        # both real, outcomes).
        return {"patient_found": True, "found": False, "reason": "NOT_FOUND"}

    return {
        "patient_found": True,
        "found": True,
        "outstanding_amount": billing.outstanding_amount,
        "due_date": billing.due_date.isoformat() if billing.due_date else None,
    }


# =============================================================================
# Tool 9: GET /api/v1/clinic/info
#
# ADDED BY SOURAV -- "Caller asks opening hours, address or directions"
# story. models.py's own ClinicInfo docstring lists the exact caller
# phrasings this backs ("When do you open?" / "Clinic kab khulta hai?" /
# "ঠিকানাটা কী?") -- the DB already stored everything needed (see that
# model's own comment: "The actual voice response language should ideally
# be handled by the agent/template layer... The DB stores the factual
# information"); this endpoint is simply the missing HTTP surface over it.
# A singleton table (seed.py inserts exactly one row) -- no query params.
# =============================================================================

# Ordered Monday-first to match DoctorSchedule's own weekday convention
# (0=Monday .. 6=Sunday) used throughout this file, so any future caller
# of this endpoint can zip the two together without a re-mapping step.
_CLINIC_WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


@app.get("/api/v1/clinic/info")
def clinic_info(db: Session = Depends(get_db)):
    """found=false is a real, honest edge case (an unseeded/emptied
    ClinicInfo table), not something the voice agent should ever silently
    paper over with a guessed address -- same fail-safe posture as every
    other endpoint in this file. Normally there is exactly one row."""
    info = db.query(ClinicInfo).first()
    if not info:
        return {"found": False}

    hours = {}
    for day in _CLINIC_WEEKDAYS:
        closed = bool(getattr(info, f"{day}_closed", False))
        hours[day] = {
            "closed": closed,
            "open": None if closed else getattr(info, f"{day}_open"),
            "close": None if closed else getattr(info, f"{day}_close"),
        }

    return {
        "found": True,
        "clinic_name": info.clinic_name,
        "phone": info.phone,
        "address": info.address,
        "directions": info.directions,
        "hours": hours,
    }


# =============================================================================
# Tool 10: GET /api/v1/health-packages
# Tool 11: GET /api/v1/health-packages/search?name=...
#
# ADDED BY SOURAV -- "Caller asks about a health package" story. Mirrors
# /api/v1/tests/search's own English -> Bengali-alias -> fuzzy matching
# ladder exactly (see that endpoint's comments for why each stage exists),
# now that seed.py actually seeds HealthPackage.aliases -- see this file's
# own "ADDED BY SOURAV" comment in seed.py's HEALTH_PACKAGES list for that
# half of this story.
# =============================================================================


def _package_tests(db: Session, pkg: HealthPackage) -> list[LabTest]:
    return (
        db.query(LabTest)
        .join(HealthPackageTest, HealthPackageTest.lab_test_id == LabTest.id)
        .filter(HealthPackageTest.package_id == pkg.id)
        .all()
    )


def _package_reply_dict(db: Session, pkg: HealthPackage) -> dict:
    tests = _package_tests(db, pkg)
    return {
        "found": True,
        "package_name": pkg.name,
        # _first_alias_bn() is named for its original (LabTest/Doctor)
        # callers, but its behaviour -- "first non-empty '|'-separated
        # entry" -- has nothing Bengali-specific about it; HealthPackage's
        # own alias list (seed.py) deliberately mixes English/Hinglish/
        # Banglish/Bengali-script entries the same way, so it is reused
        # here as-is rather than duplicated under a new name.
        "package_name_bn": _first_alias_bn(pkg.aliases),
        "description": pkg.description,
        "price_inr": pkg.price_inr,
        "tests": [t.name for t in tests],
        "tests_bn": [_first_alias_bn(t.aliases_bn) for t in tests],
    }


def _find_health_package(db: Session, name: str) -> HealthPackage | None:
    active = db.query(HealthPackage).filter_by(active=True)

    # English substring match.
    exact = active.filter(func.lower(HealthPackage.name).contains(name.lower())).first()
    if exact:
        return exact

    all_packages = active.all()

    # Alias match (English/Hinglish/Banglish/Bengali-script -- see
    # seed.py's HEALTH_PACKAGES aliases for exactly what this catches).
    for p in all_packages:
        aliases = [a for a in (p.aliases or "").split("|") if a]
        if any(name.lower() in alias.lower() or alias.lower() in name.lower() for alias in aliases):
            return p

    # Fuzzy fallback -- same shape as _find_department()'s own fallback.
    best_pkg, best_ratio = None, 0.0
    for p in all_packages:
        candidates = [p.name.lower()] + [a.lower() for a in (p.aliases or "").split("|") if a]
        for c in candidates:
            ratio = difflib.SequenceMatcher(None, name.lower(), c).ratio()
            if ratio > best_ratio:
                best_pkg, best_ratio = p, ratio

    return best_pkg if best_ratio >= 0.6 else None


@app.get("/api/v1/health-packages")
def list_health_packages(db: Session = Depends(get_db)):
    """A plain "what packages do you have?" listing -- every ACTIVE
    package, unfiltered. Deliberately excludes inactive packages (same
    posture as active-only lookups elsewhere): a caller should never be
    offered, or able to ask follow-up questions about, a package the
    clinic has withdrawn."""
    packages = db.query(HealthPackage).filter_by(active=True).all()
    return {
        "packages": [_package_reply_dict(db, p) for p in packages],
    }


@app.get("/api/v1/health-packages/search")
def search_health_package(name: str = Query(...), db: Session = Depends(get_db)):
    pkg = _find_health_package(db, name)
    if pkg:
        return _package_reply_dict(db, pkg)

    # Fuzzy "did you mean" suggestions -- same pattern as search_test()'s
    # own fallback: try both the English name and every alias, then map
    # suggested aliases back to their canonical English name for display.
    active_packages = db.query(HealthPackage).filter_by(active=True).all()
    candidates = []
    for p in active_packages:
        candidates.append(p.name)
        candidates.extend(a for a in (p.aliases or "").split("|") if a)
    suggestions = difflib.get_close_matches(name, candidates, n=3, cutoff=0.5)
    alias_to_name = {a: p.name for p in active_packages for a in (p.aliases or "").split("|") if a}
    suggestions = list(dict.fromkeys(alias_to_name.get(s, s) for s in suggestions))
    return {"found": False, "query": name, "did_you_mean": suggestions}


# =============================================================================
# Tool 12: POST /api/v1/callbacks
#
# ADDED BY SOURAV -- "Caller asks to be called back" story. See
# models.py's own CallbackRequest docstring for why this is its own table,
# and agent/callback_flow.py's module docstring for why "operating hours"
# is checked entirely on the VOICE AGENT side (main.py), before this
# endpoint is ever called -- this endpoint's only job is to persist a
# request that has already been decided to be acceptable; it does not
# re-check availability itself, the same division of labour
# book_appointment() above already has with the agent's own slot
# collection (the agent decides WHEN to call this; this endpoint decides
# only HOW to store what it's given).
# =============================================================================
class CallbackRequestIn(BaseModel):
    phone: str
    time_window: str
    reason: str | None = None


@app.post("/api/v1/callbacks")
def request_callback(req: CallbackRequestIn, db: Session = Depends(get_db)):
    """Always succeeds for a syntactically valid body (FastAPI/Pydantic
    already reject a request missing `phone`/`time_window` with a 422
    before this function body ever runs) -- unlike book_appointment()
    above, there is no slot-taken/doctor-not-found domain check to fail:
    a callback request has no capacity limit and needs nothing else to
    already exist in the database. `success` is still returned, matching
    every other write in this file's response shape, so main.py's existing
    "check success, then check the fields that must be non-empty before
    speaking them" pattern (see agent/outcomes.py's missing_booking_write_
    fields(), mirrored for this endpoint as missing_callback_write_fields())
    needs no special-casing for this endpoint."""
    callback_id = f"CB-{datetime.date.today().strftime('%Y%m%d')}-{uuid.uuid4().hex[:4].upper()}"
    row = CallbackRequest(
        callback_id=callback_id,
        phone=req.phone,
        time_window=req.time_window,
        reason=req.reason,
        status="pending",
        created_at=datetime.datetime.now(),
    )
    db.add(row)
    db.commit()

    return {
        "success": True,
        "callback_id": callback_id,
        "phone": req.phone,
        "time_window": req.time_window,
        "reason": req.reason,
        "status": "pending",
    }


# === merged: dev_chakravardhan's additions below ===
# Tool 5: POST /api/v1/appointments/{confirmation_id}/reschedule
# =============================================================================
# The story's acceptance criterion covers booking, reschedule AND
# cancellation, and only the first of the three existed. These two
# endpoints are what make the other two events real events rather than
# hypothetical ones -- there was previously no way for an appointment to
# move or end at all, so there was nothing to message about.
class RescheduleRequest(BaseModel):
    date: str
    time_slot: str


class CancelRequest(BaseModel):
    # Optional, purely for the audit log. Nothing branches on it, and it is
    # deliberately NOT put into the patient's message: the cancellation
    # template is registered with four variables and adding a free-text
    # fifth is exactly the kind of change an operator rejects.
    reason: str | None = None


def _find_live_appointment(db: Session, confirmation_id: str) -> Appointment | None:
    return db.query(Appointment).filter_by(confirmation_id=confirmation_id.strip().upper()).first()


@app.post("/api/v1/appointments/{confirmation_id}/reschedule")
def reschedule_appointment(
    confirmation_id: str, req: RescheduleRequest, background: BackgroundTasks, db: Session = Depends(get_db)
):
    """Move an existing appointment, KEEPING its confirmation_id.

    The id is deliberately not reissued. The patient's whole complaint in
    the story is having to track a reference number; handing them a second
    one every time a clinic moves their slot would make that worse, and the
    reschedule template says "রেফারেন্স {#var#} একই থাকছে" -- the reference
    stays the same -- precisely so the earlier message they may still be
    holding does not become misleading.
    """
    appt = _find_live_appointment(db, confirmation_id)
    if appt is None:
        return {"success": False, "reason": "appointment_not_found"}
    if appt.status == APPT_CANCELLED:
        return {"success": False, "reason": "appointment_cancelled"}

    try:
        target = datetime.date.fromisoformat(req.date)
    except ValueError:
        return {"success": False, "reason": "missing_field"}

    doctor = db.get(Doctor, appt.doctor_id)
    if doctor is None:
        return {"success": False, "reason": "doctor_not_found"}

    sched = _schedule_for_weekday(db, doctor.id, target.weekday())
    if not sched:
        return {"success": False, "reason": "doctor_not_available_that_day"}

    valid_slots = _generate_slots(sched.start_time, sched.end_time)
    taken = _live_slots(db, doctor.id, req.date) - {
        # The appointment's own current slot is not an obstacle to itself.
        # Without this, "move it fifteen minutes later, same day" works but
        # "confirm the slot it already has" reports slot_taken, which reads
        # to a caller as the system having lost their booking.
        appt.time_slot if appt.date == req.date else ""
    }
    if req.time_slot not in valid_slots:
        free = [s for s in valid_slots if s not in taken][:3]
        return {"success": False, "reason": "slot_taken", "alternative_slots": free}
    if req.time_slot in taken:
        free = [s for s in valid_slots if s not in taken][:3]
        return {"success": False, "reason": "slot_taken", "alternative_slots": free}

    previous = {"date": appt.date, "time_slot": appt.time_slot}
    appt.date = req.date
    appt.time_slot = req.time_slot
    appt.status = APPT_RESCHEDULED
    appt.updated_at = datetime.datetime.now()

    # Staged after the new date/slot are set, so the message carries the
    # values the patient is being moved to -- and before the commit, so
    # losing the race below discards the message with the move.
    attempt = _prepare_notification(db, appt, mt.EVENT_RESCHEDULED, doctor.name)

    try:
        db.commit()
    except IntegrityError:
        # Same race as book_appointment()'s, same honest answer: somebody
        # took the target slot between the check above and this commit.
        db.rollback()
        logging.getLogger("clinic-api").info(
            "reschedule of %s to %s %s lost a race",
            confirmation_id,
            req.date,
            req.time_slot,
        )
        return {
            "success": False,
            "reason": "slot_taken",
            "alternative_slots": [s for s in valid_slots if s not in _live_slots(db, doctor.id, req.date)][
                :3
            ],
        }

    notification = _schedule_delivery(background, attempt, mt.EVENT_RESCHEDULED)

    return {
        "success": True,
        "confirmation_id": appt.confirmation_id,
        "doctor_name": doctor.name,
        "doctor_name_bn": _first_alias_bn(doctor.aliases_bn),
        "date": appt.date,
        "time_slot": appt.time_slot,
        "previous_date": previous["date"],
        "previous_time_slot": previous["time_slot"],
        "notification": notification,
    }


@app.post("/api/v1/appointments/{confirmation_id}/cancel")
def cancel_appointment(
    confirmation_id: str, req: CancelRequest, background: BackgroundTasks, db: Session = Depends(get_db)
):
    """Cancel an appointment, release its slot, and tell the patient.

    The row is kept, not deleted -- see the Appointment docstring for why,
    and for what slot_lock does to keep the unique constraint honest once a
    cancelled row stops owning its slot.

    Cancelling an already-cancelled appointment is reported as success with
    `already_cancelled` set, and sends NO second message. A gateway retry,
    a double-tap in a staff UI and a patient calling twice all reach this
    endpoint, and none of them is a reason to message somebody about a
    cancellation they were already told about.
    """
    appt = _find_live_appointment(db, confirmation_id)
    if appt is None:
        return {"success": False, "reason": "appointment_not_found"}

    doctor = db.get(Doctor, appt.doctor_id)
    doctor_name = doctor.name if doctor else ""

    if appt.status == APPT_CANCELLED:
        return {
            "success": True,
            "already_cancelled": True,
            "confirmation_id": appt.confirmation_id,
            "date": appt.date,
            "time_slot": appt.time_slot,
            "notification": {"status": "not_resent", "event": mt.EVENT_CANCELLED},
        }

    appt.status = APPT_CANCELLED
    # Releases the slot. The confirmation_id is unique, so this row can
    # never collide with the live booking that replaces it, nor with any
    # other cancelled row on the same slot.
    appt.slot_lock = appt.confirmation_id
    appt.updated_at = datetime.datetime.now()

    # Staged before the commit, same as the other two events: the
    # cancellation and the record that the patient must be told about it
    # land together or not at all.
    attempt = _prepare_notification(db, appt, mt.EVENT_CANCELLED, doctor_name)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        logging.getLogger("clinic-api").exception("cancelling %s hit an integrity error", confirmation_id)
        return {"success": False, "reason": "cancel_failed"}

    if req.reason:
        logging.getLogger("clinic-api").info(
            "appointment %s cancelled, reason given: %s", confirmation_id, req.reason[:200]
        )

    notification = _schedule_delivery(background, attempt, mt.EVENT_CANCELLED)

    return {
        "success": True,
        "already_cancelled": False,
        "confirmation_id": appt.confirmation_id,
        "doctor_name": doctor_name,
        "doctor_name_bn": _first_alias_bn(doctor.aliases_bn) if doctor else None,
        "date": appt.date,
        "time_slot": appt.time_slot,
        "notification": notification,
    }


# =============================================================================
# Delivery receipts and the staff failure queue
# =============================================================================
# "Delivery receipts are recorded and failures surfaced to staff rather
# than silently dropped" is half of the story's acceptance criterion, and
# it is the half that needs endpoints rather than just a send call.
class DeliveryReceipt(BaseModel):
    """What the gateway POSTs back once it knows what happened to a message.

    Field names follow the same tolerance as notifications._provider_id():
    gateways disagree, and either identifier is enough to find the row.
    """

    status: str
    provider_message_id: str | None = None
    message_id: str | None = None
    client_ref: str | None = None
    error_code: str | None = None
    error_detail: str | None = None


@app.post("/api/v1/notifications/receipt")
def delivery_receipt(
    receipt: DeliveryReceipt,
    db: Session = Depends(get_db),
    x_gateway_token: str | None = Header(default=None),
):
    """The gateway's delivery-receipt callback.

    AUTHENTICATION IS CONDITIONAL, and the condition is deliberate: a
    deployment with a configured gateway MUST set HOSPITAL_GATEWAY_DLR_TOKEN
    and matching receipts are rejected without it, because anything on the
    network could otherwise mark a patient's message delivered. A bench pod
    with no gateway at all has no token to check and accepts receipts so
    the path can be tested.

    ALWAYS ANSWERS 200 for an unmatched receipt rather than an error.
    Gateways retry a receipt that gets a non-2xx, often indefinitely; a
    receipt we cannot match is our problem to see in the log, not a reason
    to make theirs a loop.
    """
    config = notify.load_config()
    if config.configured:
        if not config.dlr_token:
            logging.getLogger("clinic-api").error(
                "receipt rejected: gateway is configured but HOSPITAL_GATEWAY_DLR_TOKEN is not"
            )
            return {"accepted": False, "reason": "receipt_auth_not_configured"}
        if x_gateway_token != config.dlr_token:
            logging.getLogger("clinic-api").warning("receipt rejected: bad gateway token")
            return {"accepted": False, "reason": "unauthorized"}

    attempt = notify_service.apply_receipt(
        db,
        raw_status=receipt.status,
        provider_message_id=receipt.provider_message_id or receipt.message_id,
        client_ref=receipt.client_ref,
        error_code=receipt.error_code,
        error_detail=receipt.error_detail,
    )
    if attempt is None:
        logging.getLogger("clinic-api").warning(
            "delivery receipt matched no ledger row (provider id %r, client_ref %r)",
            receipt.provider_message_id or receipt.message_id,
            receipt.client_ref,
        )
        return {"accepted": False, "reason": "unknown_message"}

    db.commit()
    return {"accepted": True, "id": attempt.id, "status": attempt.status}


@app.get("/api/v1/notifications/failures")
def notification_failures(
    stale_minutes: int = Query(notify.DEFAULT_STALE_MINUTES, ge=1),
    include_acknowledged: bool = Query(False),
    include_skipped: bool = Query(False),
    db: Session = Depends(get_db),
):
    """The staff queue: every patient still owed a written confirmation.

    This is what "surfaced to staff rather than silently dropped" means in
    practice -- a list somebody at the clinic can work through, including
    the messages that failed in the quietest way possible, by being
    accepted and then never delivered. See notify_service.open_failures()
    for the three conditions that put a row here.
    """
    rows = notify_service.open_failures(
        db,
        stale_minutes=stale_minutes,
        include_acknowledged=include_acknowledged,
        include_skipped=include_skipped,
    )
    return {
        "count": len(rows),
        "stale_minutes": stale_minutes,
        "failures": [notify_service.as_dict(r) for r in rows],
    }


@app.get("/api/v1/appointments/{confirmation_id}/notifications")
def appointment_notifications(confirmation_id: str, db: Session = Depends(get_db)):
    """Every message ever owed for one appointment, newest first.

    The reception-desk question: a patient is standing there saying they
    were told to expect a message. This answers whether one was sent, what
    it said, and whether the operator ever confirmed delivery.
    """
    rows = (
        db.query(NotificationAttempt)
        .filter_by(confirmation_id=confirmation_id.strip().upper())
        .order_by(NotificationAttempt.created_at.desc())
        .all()
    )
    return {
        "confirmation_id": confirmation_id.strip().upper(),
        "count": len(rows),
        "notifications": [notify_service.as_dict(r) for r in rows],
    }


class AcknowledgeRequest(BaseModel):
    staff: str


@app.post("/api/v1/notifications/{attempt_id}/acknowledge")
def acknowledge_failure(attempt_id: int, req: AcknowledgeRequest, db: Session = Depends(get_db)):
    """A member of staff takes a failure on -- they have phoned the patient,
    or handed them a printed slip. The row leaves the queue; its status
    stays whatever it actually was."""
    attempt = notify_service.acknowledge(db, attempt_id, req.staff)
    if attempt is None:
        return {"success": False, "reason": "not_found"}
    db.commit()
    return {"success": True, "notification": notify_service.as_dict(attempt)}


@app.post("/api/v1/notifications/{attempt_id}/retry")
def retry_notification(attempt_id: int, background: BackgroundTasks, db: Session = Depends(get_db)):
    """Send a failed message again, from the stored body.

    Re-sends what was composed at the time, NOT a freshly rendered message
    -- see the NotificationAttempt docstring. If the template has changed
    since, a re-render would put text in front of the patient that nobody
    ever approved for this appointment.
    """
    attempt = notify_service.requeue(db, attempt_id)
    if attempt is None:
        return {"success": False, "reason": "not_found_or_already_queued"}
    db.commit()
    background.add_task(notify_service.deliver_now, attempt.id)
    return {"success": True, "notification": notify_service.as_dict(attempt)}


# =============================================================================
# PATIENT HISTORY -- disclosed only after verification
# =============================================================================
# Author: Chakravardhan
#
# Story: "As a patient, I want my history spoken only to me, so that whoever
# else uses this handset cannot hear what tests I have had."
#
# THE PHONE NUMBER IS NOT A CREDENTIAL. It says which record to LOOK AT, and
# nothing at all about who is holding the phone. Every endpoint below is
# built on that distinction; see clinic-api/verification.py for why an SMS
# OTP is not the answer here.
#
# EVERY ONE OF THESE IS A POST, INCLUDING THE READ. A token in a query string
# lands in the access log, the proxy log and anything scraping either -- and
# a token is the one value in this system that grants access on its own. Bodies
# are not logged; URLs are.
class VerifyBeginRequest(BaseModel):
    phone: str
    call_id: str | None = None


class VerifyAnswerRequest(BaseModel):
    phone: str
    factor: str
    answer: str
    call_id: str | None = None


class HistoryRequest(BaseModel):
    token: str
    call_id: str | None = None


class RefusalRequest(BaseModel):
    phone: str
    reason: str
    call_id: str | None = None


@app.post("/api/v1/history/verify/begin")
def history_verify_begin(req: VerifyBeginRequest, db: Session = Depends(get_db)):
    """Which proof to ask this caller for.

    ANSWERS IDENTICALLY FOR A NUMBER WE HAVE NEVER SEEN. An unknown caller is
    asked for a date of birth exactly as a known one is. Replying "no patient
    with that number" would turn this line into a lookup for whether a named
    person attends this clinic -- which is itself information about them, and
    is the enumeration hole that most verification systems leak through.
    """
    return history_service.begin(db, req.phone, call_id=req.call_id)


@app.post("/api/v1/history/verify")
def history_verify(req: VerifyAnswerRequest, db: Session = Depends(get_db)):
    """One attempt. -> {"reply": ..., "token": ... | null}

    `reply` is the ONLY thing the caller may learn, and it is deliberately
    coarser than what the audit row records: a wrong PIN, an unknown number
    and a patient with no factor at all all come back as "failed". The
    reasons differ; what the caller can distinguish must not.
    """
    decision, token = history_service.attempt(
        db,
        phone=req.phone,
        factor=req.factor,
        answer=req.answer,
        call_id=req.call_id,
    )
    return {
        "reply": decision.reply,
        "verified": decision.verified,
        # Returned so the agent can ask the same question again. Never a hint
        # about the ANSWER -- only about which kind of proof is wanted.
        "factor": decision.factor,
        "token": token,
    }


@app.post("/api/v1/history/read")
def history_read(req: HistoryRequest, db: Session = Depends(get_db)):
    """The history itself. Opens only with a token from a verified attempt.

    A missing or expired token is answered as `not_verified` rather than as
    an error -- from the caller's side it is the same situation as never
    having verified, and the agent handles both by asking again.

    The response also carries the patient's single timeline (`timeline`,
    `upcoming_appointments`) -- see history_service.build_timeline(). It is
    returned here, behind the same token, rather than from a new endpoint:
    one door into the record, one audit row per opening.
    """
    data = history_service.history(db, req.token, call_id=req.call_id)
    if data is None:
        return {"found": False, "reason": "not_verified"}
    return {"found": True, **data}


@app.post("/api/v1/history/refusal")
def history_refusal(req: RefusalRequest, db: Session = Depends(get_db)):
    """Record a disclosure the AGENT refused -- a speakerphone, an
    unclassified audio path.

    Audited here rather than only in the agent's log because without a row,
    "the system would not tell me my history" has no explanation on the
    clinic's side, and the likeliest support response would be to switch the
    check off.
    """
    history_service.record_refusal(db, phone=req.phone, reason=req.reason, call_id=req.call_id)
    return {"recorded": True}


class SetPinRequest(BaseModel):
    phone: str
    pin: str
    staff: str


@app.post("/api/v1/patients/pin")
def set_patient_pin(req: SetPinRequest, db: Session = Depends(get_db)):
    """FOR COUNTER STAFF, IN PERSON. Never reachable from the voice line.

    A PIN that can be set by whoever is holding the handset is not a second
    factor -- it is a button labelled "make me verified", and it would undo
    the whole story. The voice agent has no client method for this endpoint,
    deliberately (see agent/tools_client.py).
    """
    ok = history_service.set_pin(db, phone=req.phone, pin=req.pin)
    if ok:
        logging.getLogger("clinic-api").info("PIN set for %s by staff %s", req.phone, req.staff[:60])
    return {"success": ok}


@app.get("/api/v1/history/audit")
def history_audit(
    limit: int = Query(50, ge=1, le=500), outcome: str | None = Query(None), db: Session = Depends(get_db)
):
    """The audit trail, for staff.

    The failures are the interesting part: a run of them against one number
    is somebody guessing, and that is invisible without this. No secret is
    stored in these rows -- only which KIND of proof was attempted.
    """
    q = db.query(DisclosureAudit)
    if outcome:
        q = q.filter_by(outcome=outcome)
    rows = q.order_by(DisclosureAudit.created_at.desc()).limit(limit).all()
    return {
        "count": len(rows),
        "audit": [
            {
                "id": r.id,
                "phone": r.phone,
                "patient_id": r.patient_id,
                "factor": r.factor,
                "outcome": r.outcome,
                "detail": r.detail,
                "call_id": r.call_id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }
