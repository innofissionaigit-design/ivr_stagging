"""Caller verification: the rules that decide whether a history may be
spoken aloud.

Author: Chakravardhan
Story:  History disclosed only after verification
        "As a patient, I want my history spoken only to me, so that whoever
         else uses this handset cannot hear what tests I have had."

THE THREAT, PRECISELY
---------------------
The handset is SHARED. A family phone, a shop phone, a borrowed phone. The
attacker is not a hacker -- it is an ordinary person holding a phone that is
not theirs, who dials the clinic and is treated as its owner.

Everything below follows from that one sentence, and two consequences of it
are worth stating before the code, because both are counter-intuitive:

1. THE PHONE NUMBER IS NOT A FACTOR. It is how a caller is LOCATED. Treating
   "called from Meera's number" as "is Meera" is precisely the bug.

2. AN SMS OTP DOES NOT WORK HERE, and it is the first thing anyone will
   suggest. The code is delivered to the shared handset the caller is
   already holding. It proves possession of a phone that by the story's own
   premise proves nothing. Whoever borrowed it reads the code off the screen
   and is now "verified" -- with a stronger claim than before, because the
   system now believes it checked something.

So the factors here are KNOWLEDGE, and specifically knowledge that is not
sitting in that handset's message inbox.

WHAT IS DELIBERATELY NOT A FACTOR
---------------------------------
  * the confirmation_id -- SMSed to this handset, readable by whoever holds it
  * the patient's name -- a household member knows it, and it is spoken aloud
    during booking anyway
  * the appointment date -- same objection, and it is in the SMS

Accepting any of those would re-open the hole while looking like security.

THE SILENCE RULES
-----------------
Two behaviours here exist to avoid leaking through the FAILURE path, which
is the classic way a verification system betrays the thing it protects:

  * NO ENUMERATION. "No patient with that number" and "wrong PIN" return the
    SAME outcome to the caller. Otherwise the line becomes an oracle for
    "does this person use this clinic", which is itself medical information.
  * NO HINTS. The caller is never told which factor was expected, how many
    attempts remain, or what the right answer looked like.
"""
from __future__ import annotations

import dataclasses
import datetime
import hashlib
import hmac
import os
import secrets

# ---------------------------------------------------------------------------
# Factors
# ---------------------------------------------------------------------------
FACTOR_PIN = "pin"          # 4-digit, set at the counter, in person
FACTOR_DOB = "dob"          # date of birth
FACTOR_NONE = "none"        # nothing could be attempted

# Ordered strongest first. A patient with a PIN is challenged on the PIN; DOB
# is the fallback for patients who have never been to the counter to set one.
FACTOR_PREFERENCE = (FACTOR_PIN, FACTOR_DOB)

# ---------------------------------------------------------------------------
# Outcomes. Persisted to DisclosureAudit, so this is a stored data format.
# ---------------------------------------------------------------------------
OUTCOME_VERIFIED = "verified"
OUTCOME_WRONG = "wrong_factor"
OUTCOME_NO_PATIENT = "no_patient"
OUTCOME_LOCKED = "locked_out"
OUTCOME_NO_FACTOR = "no_factor_available"
OUTCOME_UNSAFE_PATH = "unsafe_audio_path"
OUTCOME_DISCLOSED = "disclosed"

# Everything the CALLER is allowed to distinguish between. Note how much
# smaller this is than the list above: the audit trail knows why, the caller
# is told only what they can act on.
REPLY_VERIFIED = "verified"
REPLY_FAILED = "failed"            # covers wrong, no-patient AND no-factor
REPLY_LOCKED = "locked"
REPLY_UNSAFE = "unsafe"


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


# Attempts before a patient is locked out. Three, not five: the honest
# caller mistypes once, maybe twice. Anything past that on a 4-digit PIN is
# somebody working through the space, and 10,000 combinations at three per
# lockout is a wall, not a speed bump.
MAX_ATTEMPTS = _int_env("VOICE_AGENT_VERIFY_MAX_ATTEMPTS", 3)

# Long enough that guessing is pointless, short enough that a genuine
# patient who fumbled can try again after tea rather than being sent to the
# counter for a mistake.
LOCKOUT_MINUTES = _int_env("VOICE_AGENT_VERIFY_LOCKOUT_MINUTES", 30)

# How long a verification survives WITHIN one call. Not across calls: a new
# call is a new person until proven otherwise, because the handset is shared
# and the last caller may have hung up in somebody else's hand.
SESSION_TTL_MINUTES = _int_env("VOICE_AGENT_VERIFY_TTL_MINUTES", 10)

# PBKDF2 iterations. Slow enough to matter if the database ever leaks, fast
# enough not to be felt inside a phone call.
_PBKDF2_ROUNDS = _int_env("VOICE_AGENT_VERIFY_PBKDF2_ROUNDS", 120_000)


# ---------------------------------------------------------------------------
# PIN storage
# ---------------------------------------------------------------------------
def new_salt() -> str:
    return secrets.token_hex(16)


def hash_pin(pin: str, salt: str) -> str:
    """PBKDF2-SHA256. The PIN itself is never stored, logged or returned.

    A 4-digit PIN has 10,000 possibilities, so hashing does not make it
    strong -- MAX_ATTEMPTS does. What hashing buys is that a database dump
    is not instantly a list of every patient's PIN, and that is worth the
    milliseconds on its own.
    """
    return hashlib.pbkdf2_hmac(
        "sha256", str(pin).encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ROUNDS,
    ).hex()


def pin_matches(pin: str, salt: str | None, expected_hash: str | None) -> bool:
    """Constant-time comparison.

    The timing channel is genuinely marginal over a phone line -- but this
    is one function call, and reasoning about whether an attacker can
    measure it is more expensive than simply not having the question.
    """
    if not salt or not expected_hash or pin is None:
        return False
    try:
        candidate = hash_pin(pin, salt)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, expected_hash)


def normalise_pin(spoken: str | None) -> str | None:
    """-> a 4-digit PIN pulled from what the caller said, or None.

    Callers say "one two three four", ASR writes digits, and Bengali or
    Devanagari numerals arrive from the corresponding checkpoints -- so the
    agent side folds those to ASCII before this is reached (see
    agent/language.py). Everything non-numeric is dropped: "my pin is 1234"
    and "1-2-3-4" are the same answer.
    """
    if spoken is None:
        return None
    digits = "".join(ch for ch in str(spoken) if ch.isdigit())
    return digits if len(digits) == 4 else None


def normalise_dob(spoken: str | None) -> str | None:
    """-> ISO yyyy-mm-dd, or None if it is not confidently a date.

    Accepts what slot_parse already produces plus the two orderings people
    actually say. Ambiguity returns None rather than a guess: a wrong guess
    here costs the caller one of three attempts for something they answered
    correctly.
    """
    if not spoken:
        return None
    text = str(spoken).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def dob_matches(offered: str | None, stored: str | None) -> bool:
    normalised = normalise_dob(offered)
    return bool(normalised and stored and hmac.compare_digest(normalised, stored))


# ---------------------------------------------------------------------------
# Lockout
# ---------------------------------------------------------------------------
def is_locked(locked_until: datetime.datetime | None,
              now: datetime.datetime | None = None) -> bool:
    if locked_until is None:
        return False
    return (now or datetime.datetime.now()) < locked_until


def lockout_until(now: datetime.datetime | None = None) -> datetime.datetime:
    return (now or datetime.datetime.now()) + datetime.timedelta(minutes=LOCKOUT_MINUTES)


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class Challenge:
    """What the caller is being asked for. Carries no secret -- only WHICH
    kind of proof is wanted, which is safe to say out loud."""
    factor: str
    patient_exists: bool     # NEVER sent to the caller; for the audit row only


@dataclasses.dataclass(frozen=True)
class Decision:
    """The result of one verification attempt.

    `reply` is what the caller may learn. `outcome` is what the audit row
    records. They are separate fields because they are deliberately
    different resolutions -- see the module docstring on enumeration.
    """
    reply: str
    outcome: str
    verified: bool
    factor: str
    lock_now: bool = False
    detail: str | None = None


def choose_factor(has_pin: bool, has_dob: bool) -> str:
    for factor in FACTOR_PREFERENCE:
        if factor == FACTOR_PIN and has_pin:
            return FACTOR_PIN
        if factor == FACTOR_DOB and has_dob:
            return FACTOR_DOB
    return FACTOR_NONE


def challenge_for(patient_exists: bool, has_pin: bool, has_dob: bool) -> Challenge:
    """What to ask, for a caller we have not yet verified.

    WHEN NO PATIENT MATCHES, THIS STILL RETURNS A CHALLENGE -- for date of
    birth, the factor a stranger is most likely to expect. That is
    deliberate. Answering "there is nobody here with that number" would turn
    the line into a lookup service for whether a given person is a patient
    of this clinic, which is itself medical information about them. So an
    unknown number is challenged exactly like a known one, and fails exactly
    like a wrong answer.
    """
    if not patient_exists:
        return Challenge(factor=FACTOR_DOB, patient_exists=False)
    factor = choose_factor(has_pin, has_dob)
    return Challenge(factor=factor, patient_exists=True)


def evaluate(*, patient_exists: bool, locked: bool, factor: str, matched: bool,
             attempts_before: int) -> Decision:
    """The single place that decides. Pure -- no database, no clock, no I/O.

    Ordering matters and is not arbitrary:

      1. LOCKED is checked first, so a locked patient's remaining attempts
         cannot be probed at all.
      2. NO PATIENT and WRONG ANSWER produce an identical `reply`, and differ
         only in the audit row.
      3. NO FACTOR AVAILABLE also produces that identical reply. A patient
         with neither PIN nor date of birth cannot be verified over the
         phone, and saying so out loud would confirm they exist.
    """
    if locked:
        return Decision(reply=REPLY_LOCKED, outcome=OUTCOME_LOCKED,
                        verified=False, factor=factor)

    if not patient_exists:
        return Decision(reply=REPLY_FAILED, outcome=OUTCOME_NO_PATIENT,
                        verified=False, factor=factor)

    if factor == FACTOR_NONE:
        return Decision(reply=REPLY_FAILED, outcome=OUTCOME_NO_FACTOR,
                        verified=False, factor=FACTOR_NONE,
                        detail="patient has neither pin nor date of birth")

    if matched:
        return Decision(reply=REPLY_VERIFIED, outcome=OUTCOME_VERIFIED,
                        verified=True, factor=factor)

    return Decision(reply=REPLY_FAILED, outcome=OUTCOME_WRONG, verified=False,
                    factor=factor, lock_now=(attempts_before + 1) >= MAX_ATTEMPTS)


# ---------------------------------------------------------------------------
# Session tokens
# ---------------------------------------------------------------------------
def new_token() -> str:
    """Opaque, unguessable, and meaningless on its own -- it indexes a
    server-side record rather than carrying any claim itself."""
    return secrets.token_urlsafe(24)


def token_expiry(now: datetime.datetime | None = None) -> datetime.datetime:
    return (now or datetime.datetime.now()) + datetime.timedelta(minutes=SESSION_TTL_MINUTES)


def token_valid(expires_at: datetime.datetime | None,
                now: datetime.datetime | None = None) -> bool:
    if expires_at is None:
        return False
    return (now or datetime.datetime.now()) < expires_at
