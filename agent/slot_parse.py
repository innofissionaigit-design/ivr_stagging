"""Lightweight local parsers for slot values that fill an appointment
booking a field at a time, driven by main.py's CallSession.pending state
machine.

WHY LOCAL PARSING, NOT ANOTHER LLM CALL
----------------------------------------
Once a caller is inside a booking flow, main.py already knows exactly
which single field it just asked for. Re-running the LLM's full
intent+multi-slot extraction on a bare reply like "আজ" or "সাড়ে দশটা" is
both slower (a 7B-model round trip per field) and unreliable: llm.py's
SYSTEM_PROMPT_TEMPLATE classifies intent from cue words like "বুক" /
"অ্যাপয়েন্টমেন্ট", none of which appear in a bare "আজ" -- a caller who is
already three questions into booking is not going to repeat "আমি
অ্যাপয়েন্টমেন্ট করতে চাই" every turn just so the classifier has something
to key off. This is also the root cause of the original bug report: the
LLM sees each utterance in isolation with no memory of the conversation,
so a follow-up like "10 টায়" on its own has no doctor_name/date attached
to it and the LLM has nothing to extract them from.

So each of these functions answers one narrow question -- "does this
utterance look like a date/time/phone number, and if so, which one" --
using the same trust model as fast_path.py: return None whenever not
confident, and let main.py re-prompt (or give up and fall back to a
fresh LLM classification) rather than guess.
"""
from __future__ import annotations

import datetime
import re

from agent import language as _lang

_BN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")

# Every supported script's digits folded to ASCII in one table. Bengali
# numerals are a subset of it, so every Bengali code path below behaves
# exactly as it did; what is added is Devanagari (०१२३...), which a Hindi
# ASR checkpoint emits and which would otherwise fall straight through
# re.sub(r"\D") and leave a phone number one digit short. See
# agent/language.py's digit_translation().
_DIGITS = _lang.DIGITS_TO_ASCII

_WEEKDAYS_BN = {
    "সোমবার": 0, "সোম": 0,
    "মঙ্গলবার": 1, "মঙ্গল": 1,
    "বুধবার": 2, "বুধ": 2,
    "বৃহস্পতিবার": 3, "বৃহস্পতি": 3, "বিহস্পতি": 3,
    "শুক্রবার": 4, "শুক্র": 4,
    "শনিবার": 5, "শনি": 5,
    "রবিবার": 6, "রবি": 6,
    # Hindi
    "सोमवार": 0, "सोम": 0,
    "मंगलवार": 1, "मंगल": 1,
    "बुधवार": 2, "बुध": 2,
    "गुरुवार": 3, "बृहस्पतिवार": 3, "गुरु": 3,
    "शुक्रवार": 4, "शुक्र": 4,
    "शनिवार": 5, "शनि": 5,
    "रविवार": 6, "रवि": 6, "इतवार": 6,
    # English. Full names only -- the three-letter forms ("sat", "sun",
    # "mon") are substrings of ordinary words and this table is
    # substring-matched.
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

# Bengali, Hindi and English share this table. Substring-matched, so the
# entries must not be prefixes of unrelated words in ANY of the three --
# which is why English uses whole words only and why "kal"/"कल" (Hindi for
# both yesterday and tomorrow) is read as tomorrow: a caller booking an
# appointment cannot mean yesterday.
_RELATIVE_DAYS = {
    # Bengali
    "আজ": 0, "আজকে": 0, "আজকেই": 0,
    "কাল": 1, "আগামীকাল": 1, "কালকে": 1,
    "পরশু": 2, "পরশুদিন": 2,
    # Hindi
    "आज": 0, "आज ही": 0,
    "कल": 1, "आने वाले कल": 1,
    "परसों": 2, "परसो": 2,
    # English
    "today": 0, "tomorrow": 1, "day after tomorrow": 2,
}

# Both tables above are matched as SUBSTRINGS, so they must be walked
# longest-first or a short entry shadows a long one that contains it. Sorted
# once at import rather than on every turn.
_RELATIVE_DAYS_BY_LENGTH = tuple(sorted(_RELATIVE_DAYS.items(),
                                        key=lambda kv: -len(kv[0])))
_WEEKDAYS_BY_LENGTH = tuple(sorted(_WEEKDAYS_BN.items(),
                                   key=lambda kv: -len(kv[0])))

# Kept deliberately small and exact-match only (see is_affirmative /
# is_negative below) -- these gate whole-utterance decisions like "abandon
<<<<<<< HEAD
# the booking flow" and, since the booking-readback story, "confirm the
# write", so a false hit on a substring inside an unrelated reply (e.g. a
# patient name that happens to contain "না") would be a much worse failure
# than occasionally not recognising a yes/no.
#
# This system's own docstrings (reply_templates.py's LANGUAGE SUPPORT
# note) commit to three reply languages -- Bengali, English, Hinglish --
# and Kolkata callers code-switch Bengali with English just as often as
# Hindi with English ("Banglish"), so the affirmative/negative vocabulary
# a caller might actually SAY is not Bengali-only regardless of which
# language the AGENT chose to speak the prompt in. Previously this set
# only recognised Bengali words, so an English "yes"/"no" or a
# transliterated "haan"/"nahi" fell through to the unparseable-reply retry
# path instead of being understood immediately.
_AFFIRMATIVE = {
    # Bengali
    "হ্যাঁ", "হ্যা", "হুম", "হুঁ", "ঠিক", "ঠিক আছে", "ওই দিন", "ওইদিন",
    "সেদিন", "সেদিনই", "সেই দিন", "চলবে", "ওকে", "হবে",
    # English
    "yes", "yeah", "yep", "yup", "correct", "right", "that's right",
    "ok", "okay", "sure", "confirmed", "confirm",
    # Hinglish / Banglish (Latin-script transliteration -- shared
    # vocabulary between the two, since both are "regional language +
    # English" code-switches)
    "haan", "han", "haa", "thik ache", "thik achhe", "theek hai",
    "sahi hai", "sob thik ache", "sob thik",
}
_NEGATIVE = {
    # Bengali
    "না", "নাহ", "না না", "লাগবে না", "থাক", "দরকার নেই", "ইচ্ছা নেই",
    "না থাক", "লাগবে নাহ",
    # English
    "no", "nope", "not correct", "wrong", "incorrect", "not right",
    # Hinglish / Banglish
    "nahi", "nahin", "na", "galat", "thik na", "thik nei",
}
=======
# the booking flow", so a false hit on a substring inside an unrelated
# reply (e.g. a patient name that happens to contain "না") would be a much
# worse failure than occasionally not recognising a yes/no.
_AFFIRMATIVE = {"হ্যাঁ", "হ্যা", "হুম", "হুঁ", "ঠিক", "ঠিক আছে", "ওই দিন", "ওইদিন",
                "সেদিন", "সেদিনই", "সেই দিন", "চলবে", "ওকে", "হবে",
                # Hindi
                "हाँ", "हां", "जी", "जी हाँ", "ठीक", "ठीक है", "हूँ", "उसी दिन",
                "वही दिन", "चलेगा", "ओके",
                # English -- exact whole-utterance match, so short forms are
                # safe here in a way they would not be in a substring table.
                "yes", "yeah", "yep", "ok", "okay", "sure", "that day",
                "that works", "fine", "alright"}
_NEGATIVE = {"না", "নাহ", "না না", "লাগবে না", "থাক", "দরকার নেই", "ইচ্ছা নেই",
             "না থাক", "লাগবে নাহ",
             # Hindi
             "नहीं", "ना", "नही", "नहीं चाहिए", "रहने दीजिए", "रहने दो",
             "ज़रूरत नहीं", "जरूरत नहीं",
             # English
             "no", "nope", "not now", "no thanks", "no thank you",
             "leave it", "don't", "dont"}
>>>>>>> dev_chakravardhan


def _strip(text: str) -> str:
    # .lower() is a no-op on Bengali script (it has no case), so this is
    # safe for the existing Bengali entries and required for the English /
    # Hinglish / Banglish ones added above ("Yes", "YES" and "yes" must
    # all match).
    return text.strip().strip("।!?., ").lower()


def is_affirmative(text: str) -> bool:
    return _strip(text) in _AFFIRMATIVE


def is_negative(text: str) -> bool:
    return _strip(text) in _NEGATIVE


# story title: Every critical value is read back before it is used
# user story: As a patient giving a phone number, I want it read back, so that
#   a misheard digit does not send my report to a stranger.
# acceptance criteria: Phone numbers, dates, times and names are confirmed
#   aloud before any write, and a rejection opens a correction path rather than
#   repeating the prompt. Readback is mandatory regardless of confidence for
#   values that affect a write.
#
# Ported from dev_sourav. Used only after the pre-write readback is REJECTED:
# the caller is asked which single value is wrong instead of the five-field
# flow restarting, and this maps whatever they name back to one booking field.
#
# ORDER MATTERS, and it is not _BOOKING_FIELDS order or alphabetical. "নাম"
# (name) is a substring of how a caller says "the doctor's name"
# ("ডাক্তারের নাম") at least as often as they mean the patient's, so
# doctor_name and phone are matched FIRST on their own unambiguous words. A
# bare "নাম" then falls through to patient_name, which is the only reading
# left once the others are excluded.
#
# English/Hinglish variants are matched alongside the Bengali words (merged
# from dev_sourav) since callers code-switch mid-sentence.
_CORRECTION_FIELD_WORDS = {
    "doctor_name": ("ডাক্তার", "ডক্তার", "doctor"),
    "date": ("তারিখ", "দিন", "date"),
    "time_slot": ("সময়", "টাইম", "time"),
    "phone": ("ফোন", "নম্বর", "নাম্বার", "phone", "number"),
    "patient_name": ("নাম", "name"),
}
_CORRECTION_FIELD_ORDER = ("doctor_name", "date", "time_slot", "phone", "patient_name")


def parse_correction_field(text: str) -> str | None:
    """-> one booking field name, or None if the reply does not confidently
    name one.

    Same trust model as the rest of this module: return None rather than
    guess, and let main.py re-ask. Guessing here would silently re-collect the
    wrong field and then read the SAME wrong value back, which is worse than
    asking twice.
    """
    t = _strip(text).lower()
    if not t:
        return None
    for field in _CORRECTION_FIELD_ORDER:
        if any(word in t for word in _CORRECTION_FIELD_WORDS[field]):
            return field
    return None


# Any character in the Bengali Unicode block -- letters, vowel signs, the
# nukta, and the ০-৯ digits. Used as a word boundary that actually works for
# this script; see the comment inside parse_date().
_BN_CHAR = r"[ঀ-৿]"


def _bn_bounded(word: str, text: str) -> bool:
    """Is `word` present in `text` as a whole word, Bengali-aware?"""
    return re.search(rf"(?<!{_BN_CHAR}){re.escape(word)}(?!{_BN_CHAR})", text) is not None


def parse_date(text: str, today: datetime.date | None = None,
                offered_date: str | None = None) -> str | None:
    """-> ISO date string, or None if not confident.

    `offered_date` is the ISO date main.py already spoke out loud (e.g.
    doctor_availability_reply's "next available: 2026-09-08, do you want
    that day or another one?") -- a bare affirmative reply ("হ্যাঁ", "ওই
    দিন") confirms THAT date, not literally "today"."""
    today = today or datetime.date.today()
    t = text.translate(_DIGITS).strip()

    if offered_date and is_affirmative(t):
        return offered_date

<<<<<<< HEAD
    # story title: The model never originates a fact
    # user story: As a clinical lead, I want every price, date and identifier
    #   to come from a verified system response, so that a wrong answer is a
    #   data bug rather than a model bug.
    # acceptance criteria: Every factual sentence is a template substitution
    #   from a validated tool response and the model is never shown a figure
    #   it could restate. An automated assertion on every commit proves no
    #   model-composed span reaches synthesis on a factual intent.
    #
    # These two loops were `if word in t` -- a bare substring test, which is
    # a real, reproducible bug and not a theoretical one:
    #
    #     parse_date("সকাল দশটায়")   -> TOMORROW      ("সকাল" contains "কাল")
    #     parse_date("বিকাল পাঁচটায়") -> TOMORROW      ("বিকাল" contains "কাল")
    #
    # A caller answering "কোন দিন চান?" with "সকালে" was silently given
    # tomorrow's date. This function is the highest authority in
    # agent/date_calc.resolve()'s order of precedence -- it outranks the
    # model's own interpretation -- so a parser that invents a date is the
    # same defect as a model that invents one, only harder to notice.
    #
    # _bn_bounded() is the fix, and it is the same fix parse_time() already
    # documents for টা/টার/টায়: \b cannot be used here because Bengali vowel
    # signs and the nukta are combining marks that Python's \w does not count
    # as word characters, so \b matches in the middle of a word. Asserting
    # "no Bengali character adjacent" instead does what \b was meant to do.
    #
    # Longest key first so a shorter key can never consume part of a longer
    # one -- "কাল" must not fire inside "আগামীকাল".
    for word in sorted(_RELATIVE_DAYS, key=len, reverse=True):
        if _bn_bounded(word, t):
            return (today + datetime.timedelta(days=_RELATIVE_DAYS[word])).isoformat()

    for word in sorted(_WEEKDAYS_BN, key=len, reverse=True):
        if _bn_bounded(word, t):
            days_ahead = (_WEEKDAYS_BN[word] - today.weekday()) % 7
=======
    # LONGEST PHRASE FIRST. These are substring matches, and several
    # entries contain shorter ones: "day after tomorrow" contains
    # "tomorrow", "আগামীকাল" contains "কাল". Iterating in table order made
    # "day after tomorrow" resolve to tomorrow -- a caller booked a day
    # early, with nothing in the transcript to show why.
    for word, offset in _RELATIVE_DAYS_BY_LENGTH:
        if word in t:
            return (today + datetime.timedelta(days=offset)).isoformat()

    for word, weekday in _WEEKDAYS_BY_LENGTH:
        if word in t:
            days_ahead = (weekday - today.weekday()) % 7
>>>>>>> dev_chakravardhan
            days_ahead = days_ahead or 7  # naming today's weekday means NEXT week's
            return (today + datetime.timedelta(days=days_ahead)).isoformat()

    # Explicit ISO date (e.g. carried over from an earlier LLM extraction).
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", t)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return None

    # "১৫ তারিখ" / "15 তারিখে" -- day-of-month in the current month,
    # rolling into next month if that day has already passed.
    #
    # (?<!\w) / (?!\w) here, NOT \b: Bengali vowel signs and the nukta
    # (e.g. the ে in তারিখে) are Unicode combining marks, which Python's
    # \w does NOT count as word characters. That makes \b fail to match
    # at the boundary right after them -- so "তারিখে" sitting at the very
    # end of an utterance (the normal case) silently never matched. The
    # lookaround forms only check "not a word character adjacent", which
    # is true at end-of-string and before whitespace/punctuation either
    # way, so they don't have this blind spot. See parse_time() below for
    # the same fix applied to টা/টার/টায়, where it mattered even more.
    m = re.search(r"(?<!\w)(\d{1,2})\s*(?:তারিখ|তারিখে|ই)(?!\w)", t)
    if m:
        day = int(m.group(1))
        if 1 <= day <= 31:
            try:
                candidate = today.replace(day=day)
            except ValueError:
                return None  # e.g. "31 তারিখ" in a 30-day month -- ask again
            if candidate < today:
                next_month = today.month % 12 + 1
                next_year = today.year + (1 if today.month == 12 else 0)
                try:
                    candidate = candidate.replace(year=next_year, month=next_month)
                except ValueError:
                    return None
            return candidate.isoformat()

    return None


_HOUR_WORD_TO_NUM = {
    "একটা": 1, "দুটো": 2, "দুইটা": 2, "তিনটে": 3, "তিনটা": 3, "চারটে": 4, "চারটা": 4,
    "পাঁচটা": 5, "ছটা": 6, "ছয়টা": 6, "সাতটা": 7, "আটটা": 8, "নটা": 9, "নয়টা": 9,
    "দশটা": 10, "এগারোটা": 11, "বারোটা": 12,
}

# Bengali day-part words -> the 24h hours they cover, used only to decide
# whether a bare 1-12 number means AM or PM.
_DAYPART_WORDS = ("সকাল", "দুপুর", "বিকেল", "সন্ধ্যা", "রাত")


def _to_24h(hour12: int, daypart: str | None) -> int:
    hour12 = hour12 % 12
    if daypart in ("দুপুর", "বিকেল", "সন্ধ্যা", "রাত"):
        return hour12 + 12 if hour12 != 0 else 12
    if daypart == "সকাল":
        return hour12 if hour12 != 0 else 12
    # No day-part cue spoken: this clinic's chamber hours run evenings
    # (see clinic-api/seed.py's DoctorSchedule rows, typically 17:00-20:00),
    # so treat a bare 1-7 as PM and 8-12 as AM -- the common case for "the
    # doctor's hours are 6 to 8" said without "সন্ধ্যা" in front of it.
    if 1 <= hour12 <= 7:
        return hour12 + 12
    return hour12 if hour12 != 0 else 12


def _extract_hour12_after(t: str, prefix: str) -> int | None:
    """Look immediately after `prefix` (সাড়ে/সোয়া/পৌনে) for the hour it
    modifies, in EITHER digit form ("সাড়ে ৭টা") or word form ("সাড়ে
    সাতটা") -- real callers and ASR output mix both freely, and the
    original version here only ever checked the digit form, so "সাড়ে
    দশটা" (half past ten, said as a word) silently lost its "half past"
    and was parsed as a bare 10:00."""
    idx = t.find(prefix)
    if idx == -1:
        return None
    rest = t[idx + len(prefix):].lstrip()
    # The digit and টা/টার/টায় are written with NO space between them
    # ("সাড়ে ৭টা"), so a plain (?!\w) right after the digit would wrongly
    # reject it -- ট is a word character. Consume that suffix as PART of
    # the match instead of asserting against it.
    m = re.match(r"(\d{1,2})(?:টা|টার|টায়)?(?!\w)", rest)
    if m:
        return int(m.group(1))
    for word, hour12 in _HOUR_WORD_TO_NUM.items():
        if rest.startswith(word):
            return hour12
    return None


def parse_time(text: str) -> str | None:
    """-> "HH:MM" in 24h, or None if not confident."""
    t = text.translate(_DIGITS).strip()

    m = re.search(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b", t)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"

    daypart = next((w for w in _DAYPART_WORDS if w in t), None)

    hour12 = _extract_hour12_after(t, "সাড়ে")
    if hour12 is not None:
        return f"{_to_24h(hour12, daypart):02d}:30"
    hour12 = _extract_hour12_after(t, "সোয়া")
    if hour12 is not None:
        return f"{_to_24h(hour12, daypart):02d}:15"
    hour12 = _extract_hour12_after(t, "পৌনে")
    if hour12 is not None:
        h = _to_24h(hour12, daypart)
        return f"{(h - 1) % 24:02d}:45"

    # (?<!\w) / (?!\w), NOT \b -- see parse_date()'s তারিখ regex above for
    # why. This one matters even more: টা is the single most common way a
    # caller states an o'clock hour ("৭টা", "সন্ধ্যা ৭টা"), and it almost
    # always sits at the very end of the utterance -- exactly where \b
    # after a combining vowel sign (the া in টা) silently failed to match.
    # Confirmed by hand: "সন্ধ্যা ৭টা" and even a bare "৭টা" both returned
    # None under the old \b version.
    m = re.search(r"(?<!\w)(\d{1,2})\s*(?:টা|টার|টায়)(?!\w)", t)
    if m:
        return f"{_to_24h(int(m.group(1)), daypart):02d}:00"

    for word, hour12 in _HOUR_WORD_TO_NUM.items():
        if word in t:
            return f"{_to_24h(hour12, daypart):02d}:00"

    # Transliterated/English callers: "10 am", "10am", "10 pm".
    m = re.search(r"\b(\d{1,2})\s*([ap])\.?m\.?\b", t, re.IGNORECASE)
    if m:
        h = int(m.group(1)) % 12
        if m.group(2).lower() == "p":
            h += 12
        return f"{h:02d}:00"

    return None


# ADDED BY SOURAV -- "Lab Report Status & Secure Delivery" combined story.
# main_pcm.py's new "otp_code" pending state (see _continue_pending) uses
# this instead of running the caller's reply through agent/llm.py, for
# the SAME reason every other field in this file is parsed locally (see
# this module's docstring) -- PLUS a security reason unique to this one
# field: an OTP must never be sent to the LLM or the semantic cache at
# all. The LLM call is a real network hop to Ollama with its own request
# log, and agent/semantic_cache.py persists normalized utterance text as
# its cache key -- routing a 6-digit secret through either would be
# exactly the "secret-shaped literal in a log line" DoD gate 6 warns
# about, even though the OTP itself is a deliberately hardcoded prototype
# value (see clinic-api/seed.py and main.py's FRESH_OTP_CODE). Handling
# it here, alongside parse_phone, keeps it out of both.
#
# UPDATED BY SOURAV -- moved above parse_phone() (was originally defined
# only just above parse_otp(), further down this file) because parse_phone()
# now reuses this SAME word list for a real production bug fix -- see
# parse_phone()'s own docstring immediately below for the full writeup.
# Reused as-is, not duplicated or extended: still English digit words
# only (see parse_phone()'s docstring for why Bengali/Hindi spoken digit
# words are a separate, flagged, not-yet-closed gap, symmetric with the
# same pre-existing limitation this already had for OTP entry).
_DIGIT_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
}
_DIGIT_WORD_RE = re.compile(r"\b(" + "|".join(_DIGIT_WORDS) + r")\b", re.IGNORECASE)


def parse_phone(text: str) -> str | None:
    """-> a 10-digit phone number, or None if the utterance doesn't
<<<<<<< HEAD
    contain enough digits to be one.

    UPDATED BY SOURAV -- fixes a real production bug, reported directly
    from a live call transcript. A caller was asked "Is my report ready?"
    (report_status), the agent asked for their registered phone number
    (RULE 14/15 -- identity is resolved by phone, never by name), and the
    caller answered by SPEAKING THE DIGITS AS WORDS:

        "Yes, write nine zero zero zero zero zero zero zero zero one."

    This function used to only understand literal digit characters
    (Bengali numerals, via _BN_DIGITS, or plain ASCII digits) -- spoken
    English number words were never converted to digits at all, so
    stripping every non-digit character from "nine zero zero zero zero
    zero zero zero zero one" left an EMPTY string every single time,
    which is always < 10 digits, which always returned None. The caller
    could repeat themselves as many times and as clearly as they liked
    (confirmed in the transcript -- they tried three different phrasings)
    and would be stuck in an infinite "can you tell me your registered
    phone number?" loop forever, because nothing about repeating the same
    kind of answer could ever succeed.

    Fixed by resolving spoken English digit words into digits FIRST,
    using the exact same _DIGIT_WORDS/_DIGIT_WORD_RE word list parse_otp()
    below already uses for the identical reason -- reused, not duplicated.
    "nine zero zero zero zero zero zero zero zero one" now correctly
    resolves to 9000000001.

    Deliberately still returns None (fails closed) for a spoken REPETITION
    shorthand like "eight zeros" (meaning the digit 0 repeated eight
    times) -- seen in the same transcript ("Nine then eight zeros and
    one") as the caller's own retry after the first phrasing wasn't
    understood. This is NOT fixed here: telling "eight zeros" (0 repeated
    8 times) apart from the caller instead meaning the two separate
    digits "eight" then "zero" is genuinely ambiguous, and a phone number
    gates a real caller's private report data -- guessing wrong here would
    silently produce the WRONG number and risk exposing (or refusing)
    the wrong person's report, which is a worse failure than asking the
    caller to repeat themselves once more. Same fail-closed posture this
    whole module already commits to everywhere else (see the module's own
    docstring: "return None whenever not confident, and let main.py
    re-prompt... rather than guess"). Flagged in this story's test report,
    not silently absorbed.
    """
    translated = text.translate(_BN_DIGITS)
    words_resolved = _DIGIT_WORD_RE.sub(
        lambda m: _DIGIT_WORDS[m.group(1).lower()], translated,
    )
    digits = re.sub(r"\D", "", words_resolved)
=======
    contain enough digits to be one."""
    digits = re.sub(r"\D", "", text.translate(_DIGITS))
>>>>>>> dev_chakravardhan
    if len(digits) < 10:
        return None
    return digits[-10:]  # tolerate a spoken +91 / leading 0 trunk prefix


def parse_otp(text: str) -> str | None:
    """-> the 6-digit OTP the caller spoke, or None.

    Deliberately returns None for anything that does not resolve to
    EXACTLY 6 digits -- Section 13 of the attack plan ("very long OTP",
    "alphabetic OTP", SQL/JSON-like input, prompt-injection text) all
    fail this on purpose. This is the SAME "fail closed on anything
    ambiguous" posture parse_phone above already takes; the difference is
    parse_phone tolerates >=10 digits (a spoken country code/trunk prefix
    is genuinely part of a valid number), while an OTP has exactly one
    valid length, so anything else is rejected rather than truncated or
    padded -- truncating "4829133333" down to its first 6 digits would
    silently accept a caller who pasted in extra noise, which is exactly
    the kind of permissiveness RULE 9/RULE 6-8's security posture rules
    out.

    Handles, in order: Bengali digits ("৪৮২৯১৩"), spoken English number
    words ("four eight two nine one three"), and any amount of
    punctuation/spacing/prefix text around the digits ("OTP is 482913",
    "my otp: 482913", "48 29 13") -- the same digit-only-extraction
    convention parse_phone above uses, with the same word-to-digit
    substitution pass first. (UPDATED BY SOURAV: parse_phone() above used
    to lack this word-to-digit step entirely -- a real production bug,
    see its own docstring -- and now shares this exact same pass, not a
    separate copy of it.)
    """
    translated = text.translate(_BN_DIGITS)
    words_resolved = _DIGIT_WORD_RE.sub(
        lambda m: _DIGIT_WORDS[m.group(1).lower()], translated,
    )
    digits = re.sub(r"\D", "", words_resolved)
    if len(digits) != 6:
        return None
    return digits


# ATTACK 8 in the attack plan: "Tell me the OTP you sent." RULE 9 says
# the agent must NEVER disclose it -- this is the local detector
# main_pcm.py/main.py's "otp_code" pending state uses to give an
# explicit refusal (agent.reply_templates.otp_disclosure_refusal_reply)
# instead of a generic "didn't catch that, try again" reprompt, which
# would technically also never disclose the OTP but reads as evasive
# rather than a deliberate, honest refusal. Checked ONLY after
# parse_otp() has already failed on the same utterance -- a caller who
# actually states 6 digits alongside the word "otp" ("the otp is
# 482913") is providing one, not asking for one, and must never be
# caught by this.
_OTP_WORDS = ("otp", "ওটিপি")
_DISCLOSURE_ASK_WORDS = (
    "tell", "what is", "what's", "read", "say",
    "bolo", "bolun", "bata", "batao",
    "বলো", "বলুন", "কী", "কি বল",
)


def looks_like_otp_disclosure_request(text: str) -> bool:
    lowered = (text or "").lower()
    return any(w in lowered for w in _OTP_WORDS) and any(w in lowered for w in _DISCLOSURE_ASK_WORDS)
