"""The readback, and the correction path a rejection opens.

story title: Every critical value is read back before it is used
user story: As a patient giving a phone number, I want it read back, so that a
    misheard digit does not send my report to a stranger.
acceptance criteria: Phone numbers, dates, times and names are confirmed aloud
    before any write, and a rejection opens a correction path rather than
    repeating the prompt. Readback is mandatory regardless of confidence for
    values that affect a write.

Ported from dev_sourav's test_booking_readback.py, Bengali only, and narrowed
to what this branch did not already have. The readback itself arrived with the
confidence-gate story and is already pinned by tests/test_fact_provenance.py
(all five values present, exactly one write site, and it passes confirmed=True).

What was MISSING here, and what most of this file is about, is the second
clause. A rejection used to re-ask "just say yes or no" twice and then abandon
the booking -- which is the literal behaviour the criterion names as wrong, and
a poor exchange besides: the caller reports an error and the agent responds by
repeating itself, then hanging up on the booking.

---

Tests for story "Every critical value is read back before it is used"
(Answer Quality and Grounding, owner: Saurav).

Acceptance criteria under test (verbatim from the sprint sheet):
  1. Phone numbers, dates, times and names are confirmed aloud before any
     write.
  2. A rejection opens a correction path rather than repeating the prompt.
  3. Readback is mandatory regardless of confidence for values that
     affect a write.

Layout:
  - TestBookingConfirmationPromptFidelity / TestBookingCorrectionPrompt:
    pure template tests (agent/reply_templates.py), same style as the
    existing test_reply_templates_fidelity.py.
  - TestParseCorrectionField: pure parser tests (agent/slot_parse.py).
  - TestConfirmBookingState / TestConfirmCorrectionState /
    TestNoWriteBeforeConfirmation / TestSingleShotBookingAlsoConfirms:
    state-machine regression tests against main_pcm._continue_pending and
    main_pcm._dispatch_turn, with the tool client and _speak stubbed --
    deterministic, no network, no GPU (see tests/conftest.py for why
    main_pcm.py itself imports cleanly without the real ASR/VAD/LLM
    stack).

No pytest-asyncio dependency: the project's requirements.txt pins only
`pytest`, so async entry points are driven with plain asyncio.run() inside
ordinary sync test functions rather than adding a new test-only package.
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import types

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import main  # noqa: E402
import main_pcm  # noqa: E402
from agent.reply_templates import (  # noqa: E402
    booking_confirm_prompt, booking_confirmation_prompt, booking_correction_prompt,
    missing_slot_prompt, _spoken_doctor_name,
)
from agent.slot_parse import parse_correction_field, is_affirmative, is_negative  # noqa: E402

FULL = {"doctor_name": "Dr. A Sen", "doctor_name_bn": "সেন",
        "date": "2026-09-14", "time_slot": "18:30",
        "patient_name": "রিয়া দাস", "phone": "9876543210"}


class _Session:
    call_id = "rb01"
    utt_seq = 1
    history_token = None
    timeline = None

    def __init__(self, awaiting="confirm_booking", slots=None):
        self.call_state = main.call_state_mod.build()
        self.said: list[str] = []
        self.pending = {"awaiting": awaiting, "slots": dict(slots or FULL),
                        "candidates": None, "offered_date": None, "retries": 0}


async def _say(session, text, fallback_reason=None):
    session.said.append(text)


@pytest.fixture
def wired(monkeypatch):
    """No TTS, no clinic API. _finish_booking is replaced by a spy so a write
    is observable without one ever happening."""
    written: list[dict] = []

    async def _finish(session, slots, *, confirmed=False, language=None):
        written.append({"slots": dict(slots), "confirmed": confirmed})
        session.pending = None

    monkeypatch.setattr(main, "_speak", _say)
    monkeypatch.setattr(main, "_finish_booking", _finish)
    return written


def _turn(session, text):
    return asyncio.run(main._continue_pending(session, text))


# ------------------------------------------------------- the readback

def test_all_five_values_are_read_back_before_any_write():
    """Phone, date, time and name, aloud, in one sentence. The doctor too."""
    prompt = booking_confirm_prompt(FULL)
    for value in ("রিয়া দাস", "সেন", "2026-09-14", "18:30", "9876543210"):
        assert value in prompt, f"{value!r} is not read back"


def test_an_affirmative_is_what_writes(wired):
    session = _Session()
    assert _turn(session, "হ্যাঁ")
    assert len(wired) == 1 and wired[0]["confirmed"] is True
    assert wired[0]["slots"]["phone"] == "9876543210"


@pytest.mark.parametrize("reply", ["", "ইয়ে", "একটু দাঁড়ান", "ডাক্তার সেন", "1815"])
def test_an_ambiguous_reply_is_not_consent(wired, reply):
    """Silence, a restatement, a half-heard grunt. None of these are a yes, and
    treating one as a yes is the guess-becomes-a-booking failure the whole
    state exists to prevent."""
    session = _Session()
    _turn(session, reply)
    assert wired == [], f"{reply!r} was treated as consent"


# ------------------------------------------------- the correction path

def test_a_rejection_opens_a_correction_path_instead_of_repeating(wired):
    """The clause this branch failed. "না" must ask WHICH value is wrong --
    a different question -- not re-ask the same yes/no."""
    session = _Session()
    assert _turn(session, "না")

    assert wired == [], "a rejection must never write"
    assert session.said == [booking_correction_prompt()]
    assert session.said[0] != booking_confirm_prompt(FULL), "the prompt was repeated"
    assert session.pending["awaiting"] == "confirm_correction"


def test_a_rejection_does_not_discard_the_four_correct_values(wired):
    """A correction is not a restart. The caller said one value was wrong, so
    four are right and re-collecting them would be its own small insult."""
    session = _Session()
    _turn(session, "না")
    assert session.pending["slots"] == FULL


@pytest.mark.parametrize("said,field", [
    ("ডাক্তারের নাম ভুল", "doctor_name"),
    ("তারিখটা ভুল", "date"),
    ("সময় ঠিক নেই", "time_slot"),
    ("ফোন নম্বরটা", "phone"),
    ("রোগীর নাম", "patient_name"),
])
def test_the_named_field_is_the_one_re_collected(wired, said, field):
    session = _Session(awaiting="confirm_correction")
    assert _turn(session, said)
    assert session.pending["awaiting"] == field
    assert session.said == [missing_slot_prompt("book_appointment", field)]
    assert wired == []


def test_a_bare_name_means_the_patient_not_the_doctor():
    """Order inside parse_correction_field, pinned. "নাম" is a substring of
    how a caller says "the doctor's name", so doctor_name is matched first on
    its own word and a bare "নাম" can only fall through to patient_name."""
    assert parse_correction_field("নাম") == "patient_name"
    assert parse_correction_field("ডাক্তারের নাম") == "doctor_name"


def test_an_unnameable_field_re_asks_rather_than_guessing(wired):
    """Guessing here would re-collect the wrong value and then read the SAME
    wrong one back -- worse than asking twice."""
    session = _Session(awaiting="confirm_correction")
    _turn(session, "জানি না")
    assert session.pending["awaiting"] == "confirm_correction"
    assert session.said == [booking_correction_prompt()]


def test_the_correction_loop_is_capped(wired):
    session = _Session(awaiting="confirm_correction")
    for _ in range(4):
        _turn(session, "জানি না")
    assert session.pending is None, "the correction loop never ended"
    assert session.said[-1] == main.BOOKING_NOT_CONFIRMED_BN
    assert wired == [], "nothing may be written after giving up"


def test_saying_no_to_the_correction_question_abandons(wired):
    """The considered divergence from dev_sourav, which excludes this state
    from the abandon hatch too. Once the agent has listed the five options, a
    caller saying "না" is not naming a field -- they have given up."""
    session = _Session(awaiting="confirm_correction")
    _turn(session, "না")
    assert session.pending is None
    assert wired == []


# ------------------------------------------------ the round trip

def test_a_corrected_booking_is_read_back_in_full_again(wired):
    """A correction must never shorten the path to the write. After the one
    field is re-collected, all five are read back and an affirmative is still
    required."""
    session = _Session()

    _turn(session, "না")                       # readback rejected
    _turn(session, "ফোন নম্বরটা ভুল")           # names the field
    _turn(session, "৯১২৩৪৫৬৭৮৯")               # gives the new value

    assert wired == [], "nothing written yet"
    assert session.pending["awaiting"] == "confirm_booking"
    assert session.pending["slots"]["phone"] == "9123456789", "the new value"
    assert session.pending["slots"]["date"] == FULL["date"], "the others are intact"
    assert session.said[-1] == booking_confirm_prompt(session.pending["slots"])

    _turn(session, "হ্যাঁ")
    assert len(wired) == 1 and wired[0]["slots"]["phone"] == "9123456789"


def test_the_correction_prompt_is_sayable():
    """It is spoken on a phone line, so it has to survive the tokenizer."""
    from agent import speakability
    assert speakability.check(booking_correction_prompt()).state == speakability.SPEAKABLE




def run(coro):
    """Drive a single coroutine to completion without pytest-asyncio."""
    return asyncio.run(coro)


# --------------------------------------------------------------------- #
# Pure template tests
# --------------------------------------------------------------------- #

SLOTS = {
    # "Dr. A. Sen" -- matches clinic-api/seed.py's actual convention
    # (every seeded doctor's canonical name already carries "Dr."), not a
    # simplified test fixture. Chosen deliberately after a real bug was
    # caught here: an earlier version of _spoken_doctor_name() prepended
    # a SECOND "Dr." for the english/hinglish/banglish branches, producing
    # "Dr. Dr. A. Sen" -- a plain "Sen" fixture would never have caught
    # that.
    "doctor_name": "Dr. A. Sen",
    "date": "2026-09-15",
    "time_slot": "18:30",
    "patient_name": "Rina Das",
    "phone": "9831234567",
}

ALL_LANGUAGES = ["bengali", "english", "hinglish", "banglish"]


class TestBookingConfirmationPromptFidelity:
    """Mirrors test_reply_templates_fidelity.py's discipline: every value
    must appear byte-identical in the prompt, in every supported
    language -- no rounding, truncation or reformatting of the digits."""

    @pytest.mark.parametrize("language", ALL_LANGUAGES)
    def test_contains_every_critical_value_verbatim(self, language):
        prompt = booking_confirmation_prompt(SLOTS, language=language)
        for field, value in SLOTS.items():
            if field == "doctor_name":
                continue  # covered separately: honorific may legitimately change
            assert value in prompt, f"{value!r} missing from {language} readback: {prompt!r}"

    @pytest.mark.parametrize("language", ALL_LANGUAGES)
    def test_doctor_name_never_doubles_its_honorific(self, language):
        # Regression test for the exact bug caught during review: seed
        # data doctor names already start with "Dr." (clinic-api/seed.py),
        # so _spoken_doctor_name() must never prepend a second one.
        prompt = booking_confirmation_prompt(SLOTS, language=language)
        assert "Dr. Dr." not in prompt
        assert "ডাঃ Dr." not in prompt

    def test_phone_digits_are_not_reformatted(self):
        # No dashes, spaces or grouping inserted -- verbalize() downstream
        # (agent/bn_normalize.py, called from agent/tts.py) is what turns
        # this into digit-by-digit speech; the template must hand it the
        # raw string unchanged.
        prompt = booking_confirmation_prompt(SLOTS)
        assert "9831234567" in prompt
        assert "983-123-4567" not in prompt
        assert "98312 34567" not in prompt

    def test_date_stays_iso_for_downstream_verbalization(self):
        # bn_normalize.verbalize()'s _RE_DATE only matches YYYY-MM-DD --
        # pre-formatting the date here would silently break that regex
        # and the date would come out as three unrelated numbers instead
        # of a spoken date.
        prompt = booking_confirmation_prompt(SLOTS)
        assert "2026-09-15" in prompt

    def test_missing_fields_do_not_crash_and_render_empty(self):
        # Defensive: a caller of this function with a partially-empty
        # dict (should never happen once _next_missing() has returned
        # None, but the function must not raise if it ever does).
        prompt = booking_confirmation_prompt({})
        assert isinstance(prompt, str)

    def test_asks_a_yes_no_question(self):
        # Loose sanity check that this actually reads as a confirmation
        # ask, not a statement -- every language branch ends on a question.
        for language in ALL_LANGUAGES:
            prompt = booking_confirmation_prompt(SLOTS, language=language)
            assert "?" in prompt

    def test_banglish_is_not_hinglish_text(self):
        # The two must not collapse into the same wording -- "hinglish"
        # here is Hindi vocabulary ("hai", "karne", "kya"); "banglish" is
        # transliterated Bengali ("kore nin", "thik ache"). A caller who
        # code-switches Bengali+English should not be handed a Hindi
        # sentence just because both are "regional language + English".
        hinglish = booking_confirmation_prompt(SLOTS, language="hinglish")
        banglish = booking_confirmation_prompt(SLOTS, language="banglish")
        assert hinglish != banglish
        # Loose vocabulary check in each direction.
        assert "hai" not in banglish.lower()
        assert "ache" not in hinglish.lower()


class TestSpokenDoctorNameLanguageAwareness:
    """_spoken_doctor_name() directly, across all four languages -- this
    is the helper booking_confirmation_prompt() (and the pre-existing
    booking_reply()/doctor_availability_reply()) all rely on to get the
    doctor's name right."""

    def test_bengali_prefers_alias_with_honorific(self):
        assert _spoken_doctor_name(
            {"doctor_name": "Dr. A. Sen"}, {"doctor_name_bn": "সেন"}, language="bengali"
        ) == "ডাঃ সেন"

    def test_bengali_without_alias_falls_back_to_raw_name_unchanged(self):
        # This is the exact, byte-for-byte behaviour the ORIGINAL
        # function had for its only real-world call pattern (Bengali,
        # no language argument) -- must not regress.
        assert _spoken_doctor_name({"doctor_name": "Dr. A. Sen"}, {}, language="bengali") \
            == "Dr. A. Sen"

    @pytest.mark.parametrize("language", ["english", "hinglish", "banglish"])
    def test_non_bengali_uses_dr_honorific_not_the_bengali_prefix(self, language):
        name = _spoken_doctor_name({"doctor_name": "Amit Sen"}, {}, language=language)
        assert name == "Dr. Amit Sen"
        assert "ডাঃ" not in name

    @pytest.mark.parametrize("language", ["english", "hinglish", "banglish"])
    def test_non_bengali_does_not_double_an_existing_dr(self, language):
        for name in ("Dr. A. Sen", "dr. a. sen", "DR. A. SEN"):
            result = _spoken_doctor_name({"doctor_name": name}, {}, language=language)
            assert result.lower().count("dr.") == 1, f"{name!r} -> {result!r}"

    @pytest.mark.parametrize("language,expected", [
        ("bengali", "ডাক্তার"), ("english", "the doctor"),
        ("hinglish", "doctor"), ("banglish", "doctor"),
    ])
    def test_fallback_when_no_name_at_all(self, language, expected):
        assert _spoken_doctor_name({}, {}, language=language) == expected

    def test_non_bengali_ignores_bengali_alias_even_if_present(self):
        # A pure-Bengali-script alias ("সেন") would be exactly as
        # unreadable in an English sentence as "ডাঃ" is -- the English
        # branch must use the Latin name, not the alias, even when both
        # are available.
        name = _spoken_doctor_name(
            {"doctor_name": "Dr. A. Sen"}, {"doctor_name_bn": "সেন"}, language="english"
        )
        assert name == "Dr. A. Sen"
        assert "সেন" not in name


class TestAffirmativeNegativeAcrossLanguages:
    """is_affirmative()/is_negative() gate the entire confirm_booking
    state (see main_pcm.py) -- if these only understood Bengali, an
    English/Hinglish/Banglish caller's "yes" or "no" would silently fall
    through to the unparseable-reply retry path instead of being acted on
    immediately, regardless of which language the AGENT happened to speak
    the prompt in."""

    @pytest.mark.parametrize("text", [
        "হ্যাঁ", "ঠিক আছে",  # bengali (must still work -- no regression)
        "yes", "Yes", "YES", "yeah", "correct", "ok", "sure",  # english, case-insensitive
        "haan", "Haan", "thik ache", "sahi hai",  # hinglish/banglish
    ])
    def test_recognized_as_affirmative(self, text):
        assert is_affirmative(text) is True

    @pytest.mark.parametrize("text", [
        "না", "থাক",  # bengali (must still work -- no regression)
        "no", "No", "NO", "nope", "wrong", "incorrect",  # english, case-insensitive
        "nahi", "Nahi", "na", "galat",  # hinglish/banglish
    ])
    def test_recognized_as_negative(self, text):
        assert is_negative(text) is True

    @pytest.mark.parametrize("text", ["ফোন নম্বর", "Rina Das", "18:30", "", "maybe", "hmm"])
    def test_unrelated_text_is_neither(self, text):
        assert is_affirmative(text) is False
        assert is_negative(text) is False


class TestBookingCorrectionPrompt:
    def test_is_not_the_same_text_as_the_confirmation_prompt(self):
        # AC: "opens a correction path rather than repeating the prompt."
        # Directly encodes that these must never be textually identical.
        confirmation = booking_confirmation_prompt(SLOTS)
        correction = booking_correction_prompt()
        assert confirmation != correction

    @pytest.mark.parametrize("language", ALL_LANGUAGES)
    def test_offers_all_five_fields_as_options(self, language):
        prompt = booking_correction_prompt(language=language).lower()
        # Loose per-language keyword check -- the point is the caller is
        # given a specific menu, not a vague "please repeat".
        expected = {
            "bengali": ["ডাক্তার", "তারিখ", "সময়", "নাম", "ফোন"],
            "english": ["doctor", "date", "time", "name", "phone"],
            "hinglish": ["doctor", "date", "time", "naam", "phone"],
            "banglish": ["doctor", "date", "time", "naam", "phone"],
        }[language]
        for word in expected:
            assert word.lower() in prompt


# --------------------------------------------------------------------- #
# Pure parser tests
# --------------------------------------------------------------------- #

class TestParseCorrectionField:
    @pytest.mark.parametrize("text,expected", [
        ("ডাক্তার", "doctor_name"),
        ("ডাক্তারটা ভুল বলেছি", "doctor_name"),
        ("তারিখ", "date"),
        ("দিনটা ভুল", "date"),
        ("সময়টা ঠিক না", "time_slot"),
        ("ফোন নম্বর", "phone"),
        ("নম্বরটা ভুল", "phone"),
        ("নাম", "patient_name"),
        ("রোগীর নাম ভুল", "patient_name"),
        ("doctor", "doctor_name"),
        ("phone number", "phone"),
        ("name", "patient_name"),
    ])
    def test_recognized_fields(self, text, expected):
        assert parse_correction_field(text) == expected

    def test_doctor_name_wins_over_bare_naam_substring(self):
        # "ডাক্তারের নাম" (the doctor's name) contains "নাম" but must
        # resolve to doctor_name, not patient_name -- this is exactly
        # why doctor_name is checked before patient_name.
        assert parse_correction_field("ডাক্তারের নাম ভুল বলেছিলাম") == "doctor_name"

    @pytest.mark.parametrize("text", ["", "হ্যাঁ", "আচ্ছা ঠিক আছে", "xyz"])
    def test_unrecognized_returns_none(self, text):
        assert parse_correction_field(text) is None


class TestCleanPatientNameDoesNotEatTheFirstName:
    """main_pcm._clean_patient_name() -- previously untested. Found and
    fixed during a separate review of a real production bug report
    ("agent captures only the surname, not the first name"): the
    original version re-checked every entry in _NAME_PREFIXES in a plain
    `for` loop with no `break`, testing each prefix against the ALREADY-
    stripped text from the previous iteration. Real disfluent speech (or
    ASR output) that happens to start with more than one filler phrase in
    a row -- e.g. "নাম আমি সেন" ("name -- I'm Sen") -- walked through BOTH
    matching prefixes one after another and silently dropped the first
    name along with the filler words, leaving only the surname. Fixed by
    stopping after the first prefix match (at most one filler phrase is
    ever meant to be stripped)."""

    def test_double_filler_prefix_no_longer_eats_the_name(self):
        # Direct reproduction of the bug report: this used to return
        # "সেন" (surname only) before the fix below.
        assert main_pcm._clean_patient_name("নাম আমি সেন") == "আমি সেন"

    def test_single_filler_prefix_still_stripped(self):
        assert main_pcm._clean_patient_name("আমার নাম রাহুল সেন") == "রাহুল সেন"
        assert main_pcm._clean_patient_name("আমি রাহুল সেন") == "রাহুল সেন"
        assert main_pcm._clean_patient_name("নাম রাহুল সেন") == "রাহুল সেন"

    def test_full_name_with_no_filler_words_preserved_whole(self):
        # The common case: caller just says the name. First name AND
        # surname must both survive -- this is the exact failure the bug
        # report described.
        assert main_pcm._clean_patient_name("রাহুল সেন") == "রাহুল সেন"
        assert main_pcm._clean_patient_name("অলোক মুখার্জী") == "অলোক মুখার্জী"


# --------------------------------------------------------------------- #
# State-machine regression tests (main_pcm._continue_pending / _dispatch_turn)
# --------------------------------------------------------------------- #

class FakeToolsClient:
    """Stand-in for agent.tools_client.ClinicToolsClient. Records every
    call so tests can assert book_appointment is (or is not) invoked --
    the whole point of this story is that it must never fire before an
    explicit affirmative."""

    def __init__(self):
        self.book_appointment_calls = []

    async def book_appointment(self, doctor_name, date, time_slot, patient_name, phone):
        self.book_appointment_calls.append(
            {"doctor_name": doctor_name, "date": date, "time_slot": time_slot,
             "patient_name": patient_name, "phone": phone}
        )
        return {"success": True, "confirmation_id": "KCD-20260915-0099",
                "doctor_name": doctor_name, "date": date, "time_slot": time_slot}


class _AsyncNoOp:
    async def __call__(self, *args, **kwargs):
        return None


def make_session(pending=None):
    return types.SimpleNamespace(
        call_id="test-call-1",
        pending=pending,
        dispatch_lock=asyncio.Lock(),
        send_json=_AsyncNoOp(),
    )


def full_pending(awaiting="confirm_booking", **slot_overrides):
    slots = dict(SLOTS)
    slots.update(slot_overrides)
    return {"awaiting": awaiting, "slots": slots, "candidates": None,
            "offered_date": None, "retries": 0}


@pytest.fixture(autouse=True)
def stub_speak_and_tools(monkeypatch):
    """Deterministic stand-ins for the two things _continue_pending talks
    to besides the parsers it exercises directly: the TTS-facing _speak()
    call and the tool client's write call."""
    spoken = []

    async def fake_speak(session, text, fallback_reason=None):
        spoken.append(text)

    monkeypatch.setattr(main_pcm, "_speak", fake_speak)
    fake_tools = FakeToolsClient()
    monkeypatch.setattr(main_pcm, "_tools", fake_tools)
    return types.SimpleNamespace(spoken=spoken, tools=fake_tools)


class TestConfirmBookingState:
    """awaiting == "confirm_booking": the state entered once all 5 fields
    are known, before book_appointment() is ever called."""

    def test_affirmative_finishes_the_booking(self, stub_speak_and_tools):
        session = make_session(pending=full_pending())
        handled = run(main_pcm._continue_pending(session, "হ্যাঁ"))
        assert handled is True
        assert len(stub_speak_and_tools.tools.book_appointment_calls) == 1
        call = stub_speak_and_tools.tools.book_appointment_calls[0]
        assert call == {
            "doctor_name": SLOTS["doctor_name"], "date": SLOTS["date"],
            "time_slot": SLOTS["time_slot"], "patient_name": SLOTS["patient_name"],
            "phone": SLOTS["phone"],
        }
        assert session.pending is None  # cleared by _finish_booking

    @pytest.mark.parametrize("reply", ["yes", "Yes", "haan", "thik ache", "sure", "confirmed"])
    def test_affirmative_in_english_hinglish_or_banglish_also_books(
        self, stub_speak_and_tools, reply
    ):
        # The prompt itself may have been spoken in Bengali (main_pcm.py
        # never threads a detected language through today -- see the
        # session summary), but the CALLER's reply doesn't have to be:
        # a bilingual Kolkata caller answering a Bengali question with an
        # English "yes" is routine, not an edge case.
        session = make_session(pending=full_pending())
        handled = run(main_pcm._continue_pending(session, reply))
        assert handled is True
        assert len(stub_speak_and_tools.tools.book_appointment_calls) == 1

    @pytest.mark.parametrize("reply", ["no", "No", "nahi", "wrong", "incorrect"])
    def test_negative_in_english_or_hinglish_also_opens_correction(
        self, stub_speak_and_tools, reply
    ):
        session = make_session(pending=full_pending())
        handled = run(main_pcm._continue_pending(session, reply))
        assert handled is True
        assert stub_speak_and_tools.tools.book_appointment_calls == []
        assert session.pending["awaiting"] == "confirm_correction"

    def test_high_confidence_reply_still_does_not_skip_confirmation(
        self, stub_speak_and_tools
    ):
        # AC: "Readback is mandatory regardless of confidence." Simulated
        # here by an unambiguous-looking but non-affirmative reply -- it
        # must NOT be treated as an implicit yes.
        session = make_session(pending=full_pending())
        run(main_pcm._continue_pending(session, "দশটা"))  # sounds like a time, not yes/no
        assert stub_speak_and_tools.tools.book_appointment_calls == []

    def test_rejection_does_not_book_and_opens_correction_path(
        self, stub_speak_and_tools
    ):
        session = make_session(pending=full_pending())
        handled = run(main_pcm._continue_pending(session, "না"))
        assert handled is True
        assert stub_speak_and_tools.tools.book_appointment_calls == []
        assert session.pending["awaiting"] == "confirm_correction"
        # The spoken text must be the correction prompt, NOT a repeat of
        # the confirmation prompt -- this is the AC's literal wording.
        assert stub_speak_and_tools.spoken[-1] != booking_confirmation_prompt(SLOTS)

    def test_unparseable_reply_retries_then_gives_up(self, stub_speak_and_tools):
        session = make_session(pending=full_pending())
        for _ in range(2):
            handled = run(main_pcm._continue_pending(session, "কি বললেন?"))
            assert handled is True
            assert session.pending is not None
        # Third unparseable reply exceeds the retry budget (matches the
        # >2 cap every other awaiting-state in this module uses).
        handled = run(main_pcm._continue_pending(session, "কি বললেন?"))
        assert handled is False
        assert session.pending is None
        assert stub_speak_and_tools.tools.book_appointment_calls == []


class TestConfirmCorrectionState:
    """awaiting == "confirm_correction": which field is the caller
    fixing, entered only after a rejection above."""

    def test_naming_phone_reenters_phone_collection_with_others_kept(
        self, stub_speak_and_tools
    ):
        session = make_session(pending=full_pending(awaiting="confirm_correction"))
        handled = run(main_pcm._continue_pending(session, "ফোন নম্বরটা ভুল"))
        assert handled is True
        assert session.pending["awaiting"] == "phone"
        # The other 4 already-confirmed values are NOT thrown away --
        # only the disputed field is cleared.
        assert "phone" not in session.pending["slots"]
        for field in ("doctor_name", "date", "time_slot", "patient_name"):
            assert session.pending["slots"][field] == SLOTS[field]
        assert stub_speak_and_tools.tools.book_appointment_calls == []

    def test_full_correction_round_trip_reaches_confirm_booking_again(
        self, stub_speak_and_tools
    ):
        # Regression test for the whole loop: reject -> name a field ->
        # supply a new value -> back to confirm_booking -> affirm ->
        # exactly one booking, with the corrected value.
        session = make_session(pending=full_pending())
        run(main_pcm._continue_pending(session, "না"))  # reject
        assert session.pending["awaiting"] == "confirm_correction"

        run(main_pcm._continue_pending(session, "ফোন নম্বর"))  # name the field
        assert session.pending["awaiting"] == "phone"

        run(main_pcm._continue_pending(session, "৯৮৭৬৫৪৩২১০"))  # corrected phone (Bengali digits)
        assert session.pending["awaiting"] == "confirm_booking"
        assert session.pending["slots"]["phone"] == "9876543210"
        assert stub_speak_and_tools.tools.book_appointment_calls == []  # still not booked

        run(main_pcm._continue_pending(session, "হ্যাঁ"))  # affirm
        assert len(stub_speak_and_tools.tools.book_appointment_calls) == 1
        assert stub_speak_and_tools.tools.book_appointment_calls[0]["phone"] == "9876543210"

    def test_unrecognized_field_name_reprompts_without_losing_state(
        self, stub_speak_and_tools
    ):
        session = make_session(pending=full_pending(awaiting="confirm_correction"))
        handled = run(main_pcm._continue_pending(session, "xyz"))
        assert handled is True
        assert session.pending["awaiting"] == "confirm_correction"
        assert session.pending["slots"] == SLOTS


class TestNoWriteBeforeConfirmation:
    """Sanity check spanning the whole module: book_appointment must be
    reachable from exactly one place."""

    def test_finish_booking_is_the_only_caller_of_book_appointment(self):
        import ast
        import inspect

        source = inspect.getsource(main_pcm)
        tree = ast.parse(source)
        callers = []
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                for inner in ast.walk(node):
                    if (isinstance(inner, ast.Attribute) and inner.attr == "book_appointment"):
                        callers.append(node.name)
        assert callers == ["_finish_booking"], (
            f"book_appointment() must only ever be called from _finish_booking, "
            f"found it referenced inside: {callers}"
        )


class TestSingleShotBookingAlsoConfirms:
    """The other place all 5 fields can become known at once: a caller
    who gives everything in a single utterance, handled inline in
    _dispatch_turn's book_appointment branch rather than via
    _continue_pending. Exercised through _dispatch_turn directly with ASR
    and intent extraction stubbed -- still no network, no GPU, no model
    load (see tests/conftest.py)."""

    def test_all_fields_in_one_utterance_still_waits_for_confirmation(
        self, monkeypatch, stub_speak_and_tools, tmp_path
    ):
        class FakeASRResult:
            text = "ignored -- _resolve_intent is stubbed directly below"

        class FakeASR:
            async def transcribe_utterance(self, wav_path):
                return FakeASRResult()

        async def fake_resolve_intent(session, text):
            return {"intent": "book_appointment", "slots": dict(SLOTS)}

        monkeypatch.setattr(main_pcm, "_asr", FakeASR())
        monkeypatch.setattr(main_pcm, "_resolve_intent", fake_resolve_intent)

        session = make_session(pending=None)
        wav_path = tmp_path / "utt.wav"
        wav_path.write_bytes(b"")  # _dispatch_turn just os.remove()s this

        run(main_pcm._dispatch_turn(session, str(wav_path)))

        assert stub_speak_and_tools.tools.book_appointment_calls == []
        assert session.pending is not None
        assert session.pending["awaiting"] == "confirm_booking"
        assert session.pending["slots"] == SLOTS
