"""Tests for the Bengali / Hindi / English voice path.

WHAT THESE COVER
----------------
Everything about serving three languages that can be settled without a
GPU: the language registry and its refusal to promise what the models
cannot do, script detection, explicit switching, trilingual slot parsing,
and that every reply builder answers in the language it was asked for.

THE MOST IMPORTANT TEST IN THIS FILE is the last group: that the BENGALI
output is byte-identical to what the system produced before any of this
existed. Multilingual support that quietly reworded the Bengali line would
be a regression for every caller the system currently has, and no amount
of new Hindi would pay for it.

WHAT THEY DELIBERATELY DO NOT COVER
-----------------------------------
Whether Hindi or English speech is actually TRANSCRIBED. The pod has one
Bengali-only IndicConformer checkpoint; Hindi and English ASR need
checkpoints that are not there. Nothing on a laptop can establish that,
and a test that faked it would be asserting an assumption.

Nor whether the Hindi and English wording is good. It has not been
reviewed by a speaker of either -- flagged in the implementation notes.

    python -m pytest tests/test_multilingual.py -v
"""
from __future__ import annotations

import asyncio
import datetime
import os
import sys
import types

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from agent import i18n                                    # noqa: E402
from agent import language as lang_mod                     # noqa: E402
from agent.slot_parse import (                             # noqa: E402
    parse_date, parse_phone, is_affirmative, is_negative,
)
from agent.reply_templates import (                        # noqa: E402
    booking_reply, payment_reply, report_collection_reply,
    missing_slot_prompt, language_switch_reply, language_unavailable_reply,
)


@pytest.fixture()
def trilingual(monkeypatch):
    """A pod configured with all three languages.

    Both Hindi and English need a checkpoint variable set, because
    language.enabled() refuses to advertise a language whose ASR is not
    configured -- which is the behaviour the next test asserts.
    """
    monkeypatch.setenv("VOICE_AGENT_LANGUAGES", "bn,hi,en")
    monkeypatch.setenv("VOICE_AGENT_NEMO_FILE_HI", "/fake/hi.nemo")
    monkeypatch.setenv("VOICE_AGENT_NEMO_FILE_EN", "/fake/en.nemo")
    return lang_mod.enabled()


# ===========================================================================
# The registry, and its refusal to over-promise
# ===========================================================================
def test_a_bare_pod_serves_bengali_only():
    """The default must be exactly what the system did before this work.

    A pod that has not been told about other languages, and has no
    checkpoints for them, serves Bengali and says so.
    """
    assert lang_mod.default_lang() == lang_mod.BN
    assert lang_mod.enabled() == (lang_mod.BN,)
    assert lang_mod.strategy() == lang_mod.STRATEGY_FIXED


def test_a_language_without_an_asr_checkpoint_is_not_enabled(monkeypatch):
    """THE GUARD THAT MATTERS. Configuration must not be able to claim a
    language the models cannot hear.

    A caller answered in Hindi by a Bengali-only ASR is worse off than one
    answered in Bengali, because the system sounds like it understood them.
    """
    monkeypatch.setenv("VOICE_AGENT_LANGUAGES", "bn,hi,en")
    monkeypatch.delenv("VOICE_AGENT_NEMO_FILE_HI", raising=False)
    monkeypatch.delenv("VOICE_AGENT_NEMO_FILE_EN", raising=False)
    assert lang_mod.enabled() == (lang_mod.BN,)
    assert not lang_mod.is_enabled(lang_mod.HI)


def test_configuring_the_checkpoints_enables_the_languages(trilingual):
    assert set(trilingual) == {lang_mod.BN, lang_mod.HI, lang_mod.EN}


def test_the_default_language_is_always_enabled(monkeypatch):
    """Even a configuration that forgets to list it. There is no valid
    state in which this line can serve nobody."""
    monkeypatch.setenv("VOICE_AGENT_LANGUAGES", "hi")
    assert lang_mod.default_lang() in lang_mod.enabled()


def test_resolve_never_returns_something_unserveable():
    for value in ("hi", "en", "fr", "", None, "BN", "nonsense"):
        assert lang_mod.resolve(value) in lang_mod.enabled()


# ===========================================================================
# Script detection
# ===========================================================================
@pytest.mark.parametrize("text,expected", [
    ("আমি অ্যাপয়েন্টমেন্ট চাই", lang_mod.BN),
    ("मुझे अपॉइंटमेंट चाहिए", lang_mod.HI),
    ("I need an appointment", lang_mod.EN),
])
def test_script_detection_reads_the_dominant_script(text, expected, trilingual):
    assert lang_mod.detect_from_text(text) == expected


def test_one_borrowed_english_word_does_not_flip_the_call(trilingual):
    """A caller saying "রিপোর্ট ready তো?" is speaking Bengali. Code-switching
    is normal on this line; a majority test survives it, an any-match test
    would answer them in English."""
    assert lang_mod.detect_from_text("রিপোর্ট ready তো?") == lang_mod.BN


def test_digits_and_punctuation_alone_decide_nothing(trilingual):
    assert lang_mod.detect_from_text("9876543210", fallback="hi") == lang_mod.HI
    assert lang_mod.detect_from_text("", fallback="en") == lang_mod.EN


# ===========================================================================
# Explicit switching
# ===========================================================================
@pytest.mark.parametrize("text,expected", [
    ("can you speak hindi", lang_mod.HI),
    ("বাংলায় বলুন", lang_mod.BN),
    ("अंग्रेज़ी में बताइए", lang_mod.EN),
    ("English please", lang_mod.EN),
])
def test_a_caller_can_ask_for_a_language_by_name(text, expected, trilingual):
    assert lang_mod.requested_switch(text) == expected


def test_asking_for_an_unavailable_language_is_reported_not_ignored(monkeypatch):
    """Silently ignoring the request reads as not having heard them, and
    they ask again -- burning turns on a line that can never say yes."""
    monkeypatch.setenv("VOICE_AGENT_LANGUAGES", "bn")
    monkeypatch.delenv("VOICE_AGENT_NEMO_FILE_HI", raising=False)
    assert lang_mod.requested_switch("hindi please") is None
    assert lang_mod.requested_switch_unavailable("hindi please") == lang_mod.HI

    reply = language_unavailable_reply("bn")
    assert "বাংলা" in reply                 # names what IS available


def test_an_ordinary_sentence_is_not_a_switch_request(trilingual):
    for text in ("আমার রিপোর্ট কবে পাব", "मुझे रिपोर्ट चाहिए", "what is the CBC rate"):
        assert lang_mod.requested_switch(text) is None


def test_the_switch_confirmation_is_spoken_in_the_new_language(trilingual):
    assert "বাংলা" in language_switch_reply("bn")
    assert "हिंदी" in language_switch_reply("hi")
    assert "English" in language_switch_reply("en")


# ===========================================================================
# Slot parsing across three languages
# ===========================================================================
_THURSDAY = datetime.date(2026, 9, 10)


@pytest.mark.parametrize("text,offset", [
    ("আজ", 0), ("কাল", 1), ("পরশু", 2),
    ("आज", 0), ("कल", 1), ("परसों", 2),
    ("today", 0), ("tomorrow", 1), ("day after tomorrow", 2),
])
def test_relative_days_parse_in_every_language(text, offset):
    assert parse_date(text, today=_THURSDAY) == (
        _THURSDAY + datetime.timedelta(days=offset)).isoformat()


@pytest.mark.parametrize("text", ["শুক্রবার", "शुक्रवार", "friday"])
def test_weekday_names_parse_in_every_language(text):
    assert parse_date(text, today=_THURSDAY) == "2026-09-11"


@pytest.mark.parametrize("digits", ["9876543210", "৯৮৭৬৫৪৩২১০", "९८७६५४३२१०"])
def test_phone_numbers_parse_in_every_numeral_script(digits):
    """Devanagari numerals were the gap. Before the shared digit table they
    fell straight through re.sub(r"\\D") and left the number one digit
    short -- a booking that silently could not be messaged."""
    assert parse_phone(digits) == "9876543210"


@pytest.mark.parametrize("text", ["হ্যাঁ", "हाँ", "yes", "ok", "जी हाँ"])
def test_affirmatives_in_every_language(text):
    assert is_affirmative(text)


@pytest.mark.parametrize("text", ["না", "नहीं", "no", "no thanks"])
def test_negatives_in_every_language(text):
    assert is_negative(text)


def test_yes_no_matching_stays_whole_utterance_only():
    """These gate whole-turn decisions like abandoning a booking. A patient
    name containing "না" must not read as a refusal -- which is why the sets
    are exact-match and adding English short forms to them is safe."""
    assert not is_negative("নাসরিন")
    assert not is_affirmative("yesterday")


# ===========================================================================
# The string table
# ===========================================================================
def test_every_string_exists_in_all_three_languages():
    problems = i18n.strict_check()
    assert not problems, "untranslated strings:\n  " + "\n  ".join(problems)


def test_an_unknown_key_degrades_instead_of_raising():
    """A caller is on a live line. A KeyError here would drop the turn."""
    assert i18n.t("bn", "no.such.key") == "no.such.key"


def test_a_missing_placeholder_returns_a_sentence_not_a_crash():
    text = i18n.t("bn", "test.rate", name="CBC")     # `rate` omitted
    assert "{rate}" in text and "CBC" not in text     # unformatted, but intact


def test_an_unserveable_language_falls_back_to_the_default():
    assert i18n.t("fr", "fallback.greeting") == i18n.t("bn", "fallback.greeting")


# ===========================================================================
# Replies come back in the language asked for
# ===========================================================================
_RESULT = {"success": True, "confirmation_id": "KCD-20260914-0031",
           "doctor_name_bn": "সেন", "doctor_name": "Dr. A. Sen",
           "date": "2026-09-14", "time_slot": "18:15",
           "notification": {"status": "queued"}}


def test_booking_confirmation_answers_in_the_requested_language(trilingual):
    assert "কনফার্মেশন নম্বর" in booking_reply({}, _RESULT, "bn")
    assert "कन्फर्मेशन नंबर" in booking_reply({}, _RESULT, "hi")
    assert "Confirmation number" in booking_reply({}, _RESULT, "en")


def test_the_confirmation_id_is_identical_in_every_language(trilingual):
    """The reference number is a fact from clinic-api. Translation may
    change the sentence around it and must never change it."""
    for code in lang_mod.ALL_LANGS:
        assert "KCD-20260914-0031" in booking_reply({}, _RESULT, code)


def test_payment_and_report_answer_in_the_requested_language(trilingual):
    assert "কাউন্টার" in payment_reply({}, {}, "bn")
    assert "काउंटर" in payment_reply({}, {}, "hi")
    assert "counter" in payment_reply({}, {}, "en")
    assert "কাউন্টার" in report_collection_reply({}, {}, "bn")
    assert "counter" in report_collection_reply({}, {}, "en")


def test_an_unserveable_language_is_answered_in_the_default_one():
    """Not silence, and not a crash. French is not on offer; Bengali is."""
    assert booking_reply({}, _RESULT, "fr") == booking_reply({}, _RESULT, "bn")


# ===========================================================================
# THE REGRESSION GUARD -- Bengali must not have moved
# ===========================================================================
def test_bengali_booking_confirmation_is_unchanged():
    """Byte-for-byte against what the system said before any of this.

    The strings moved from reply_templates.py into i18n.py. That was meant
    to be a pure relocation, and this is the assertion that says so.
    """
    expected = ("আপনার অ্যাপয়েন্টমেন্ট কনফার্ম হয়েছে। ডাঃ সেন, 2026-09-14, সময় 18:15। "
                "কনফার্মেশন নম্বর: KCD-20260914-0031।"
                " কনফার্মেশনের একটা মেসেজ আপনার ফোনে পাঠানো হচ্ছে, রিসেপশনে ওটা দেখালেই হবে।")
    assert booking_reply({}, _RESULT) == expected


def test_bengali_missing_slot_prompts_are_unchanged():
    assert missing_slot_prompt("book_appointment", "phone") == (
        "একটা ফোন নম্বর দেবেন, যাতে কনফার্মেশন পাঠাতে পারি?")
    assert missing_slot_prompt("test_rate", "test_name") == (
        "কোন টেস্টের রেট জানতে চান, একটু বলবেন?")
    assert missing_slot_prompt("book_appointment", "patient_name") == "রোগীর নামটা বলবেন?"


def test_an_unknown_slot_prompt_still_falls_back_to_the_generic_one():
    assert missing_slot_prompt("test_rate", "nonexistent") == "দুঃখিত, একটু স্পষ্ট করে বলবেন?"


def test_calling_a_reply_with_no_lang_argument_still_returns_bengali():
    """Every existing call site in main.py and main_pcm.py passed two
    arguments. They must keep working, and keep getting Bengali."""
    assert "কাউন্টার" in payment_reply({}, {})
    assert "কাউন্টার" in report_collection_reply({}, {})


# ===========================================================================
# Identifying the language the caller is SPEAKING
# ===========================================================================
# A written message announces its language in its own script; speech does
# not. These cover the probe that lets a pod with more than one checkpoint
# work it out from the audio -- and, just as important, that a pod with one
# checkpoint behaves exactly as it always has.
class _FakeASR:
    """Stands in for one language's checkpoint. `agreement` is what the CTC
    and RNNT decoders agreed on -- high for the language the model knows."""

    def __init__(self, text: str, agreement: float):
        self.text, self.agreement, self.calls = text, agreement, 0

    async def transcribe_utterance(self, wav_path):
        from agent.asr import ASRResult
        self.calls += 1
        return ASRResult(text=self.text, decoder_used="rnnt", decoder_agreement=self.agreement)


def _probe_pod(monkeypatch, *, strategy="parallel"):
    """All three checkpoints present and faked, a caller speaking Hindi."""
    import gate_support

    app = gate_support.load_main_pcm()       # installs the NeMo stand-ins FIRST
    from agent import asr as asr_mod
    monkeypatch.setenv("VOICE_AGENT_LANG_STRATEGY", strategy)
    nodes = {
        "bn": _FakeASR("গর গর", 0.20),                      # Bengali model hearing Hindi
        "hi": _FakeASR("सीबीसी की कीमत क्या है", 0.95),      # the one that actually heard it
        "en": _FakeASR("see bee see", 0.40),
    }
    monkeypatch.setattr(asr_mod, "_LANG_NODES", dict(nodes))
    session = types.SimpleNamespace(lang="bn", language_probe_done=False, call_id="probe")
    return app, nodes, session


def test_the_spoken_language_is_identified_from_the_audio(trilingual, monkeypatch):
    app, nodes, session = _probe_pod(monkeypatch)

    result = asyncio.run(app._transcribe_in_caller_language(session, "utt.wav"))

    assert session.lang == "hi"                     # the call switches to Hindi
    assert result.text == nodes["hi"].text          # and keeps THAT transcript
    assert [n.calls for n in nodes.values()] == [1, 1, 1]   # each heard it once


def test_the_language_is_probed_once_per_call_not_once_per_turn(trilingual, monkeypatch):
    app, nodes, session = _probe_pod(monkeypatch)
    asyncio.run(app._transcribe_in_caller_language(session, "utt1.wav"))
    asyncio.run(app._transcribe_in_caller_language(session, "utt2.wav"))

    assert session.language_probe_done is True
    assert nodes["hi"].calls == 2                   # the chosen language, both turns
    assert nodes["bn"].calls == 1 and nodes["en"].calls == 1   # probed only once


def test_by_default_nothing_is_probed(trilingual, monkeypatch):
    """STRATEGY_FIXED is the default and must stay the shipped behaviour."""
    app, nodes, session = _probe_pod(monkeypatch, strategy="fixed")
    result = asyncio.run(app._transcribe_in_caller_language(session, "utt.wav"))

    assert session.lang == "bn"
    assert result.text == nodes["bn"].text
    assert nodes["hi"].calls == 0 and nodes["en"].calls == 0


def test_a_pod_with_one_checkpoint_never_probes(monkeypatch):
    """The Bengali-only pod this line actually runs on: there is nothing to
    compare against, so the probe must not cost a second decode."""
    for var in ("VOICE_AGENT_LANGUAGES", "VOICE_AGENT_NEMO_FILE_HI", "VOICE_AGENT_NEMO_FILE_EN"):
        monkeypatch.delenv(var, raising=False)
    app, nodes, session = _probe_pod(monkeypatch)

    asyncio.run(app._transcribe_in_caller_language(session, "utt.wav"))

    assert session.lang == "bn"
    assert nodes["bn"].calls == 1
    assert nodes["hi"].calls == 0 and nodes["en"].calls == 0


def test_the_script_strategy_believes_the_transcript(trilingual, monkeypatch):
    """Only honest with a checkpoint that can EMIT more than one script --
    which is why it is not the default."""
    app, nodes, session = _probe_pod(monkeypatch, strategy="script")
    nodes["bn"].text = "सीबीसी की कीमत क्या है"       # a multilingual checkpoint's output

    asyncio.run(app._transcribe_in_caller_language(session, "utt.wav"))

    assert session.lang == "hi"
    assert nodes["hi"].calls == 0                    # no second decode, just the script


def test_a_wrong_language_decode_scores_below_a_right_one(monkeypatch):
    import gate_support
    app = gate_support.load_main_pcm()
    from agent.asr import ASRResult

    right = ASRResult(text="सीबीसी की कीमत क्या है", decoder_used="rnnt", decoder_agreement=0.95)
    wrong = ASRResult(text="গর গর", decoder_used="rnnt", decoder_agreement=0.20)
    empty = ASRResult(text="", decoder_used="none", decoder_agreement=1.0)

    assert app._transcript_score(right) > app._transcript_score(wrong)
    assert app._transcript_score(empty) == 0.0       # heard nothing, however "agreed"
