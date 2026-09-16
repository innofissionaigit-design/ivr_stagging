"""Tests for mixed-language speech on the voice bot.

Author: Chakravardhan
Story:  "As a patient more comfortable speaking than typing, I want to send a
         voice note in whatever mixture I speak, so that literacy is not a
         barrier."

On the voice bot: a caller who mixes Bengali, Hindi and English inside one
sentence is understood as correctly as one who speaks only Bengali.

WHAT THESE COVER
----------------
  * NOTHING BENGALI CHANGES -- every golden-set utterance, and every Bengali
    slot answer, reaches every parser exactly as before.
  * UNDERSTOOD NOW -- mixed questions the fast path could not read before
    ("CBC ka rate kitna hai", "डॉक्टर सेन कल बैठेंगे") are answered locally.
  * SAFER NOW -- guard words said in another language ("aur", "monday", "na")
    make the fast path abstain; before, it answered the wrong question.
  * STILL ABSTAINS -- booking, unknown doctors, explicit dates and negations
    go to the model whatever language they are said in.
  * SLOTS -- dates, times, phone numbers ("double zero"), yes / no, in any mix.
  * THE REAL TURN -- main._run_turn end to end: the fast path, the booking
    flow, doctor choice, the abandon escape hatch; the model always receives
    the caller's own words; a patient name and a verification answer are
    never rewritten; VOICE_AGENT_CODE_MIX=0 switches it off.

    python -m pytest tests/test_code_mixed_speech.py -v
"""

from __future__ import annotations

import ast
import asyncio
import datetime
import json
import types

import gate_support
import pytest

from agent import code_mix, slot_parse
from agent.fast_path import Catalogue, FastPath

golden = gate_support.load_script("gate_golden")
GOLDEN = json.loads(golden.GOLDEN_PATH.read_text(encoding="utf-8"))
TODAY = golden.TODAY
TOMORROW = (TODAY + datetime.timedelta(days=1)).isoformat()
app = gate_support.load_main_pcm()

RATE = {"found": True, "rate_inr": 250, "test_name": "CBC", "test_name_bn": "সিবিসি", "report_time_hours": 24}
BOOKED = {
    "success": True,
    "confirmation_id": "KCD-20260920-CM01",
    "doctor_name": "Dr. A. Sen",
    "date": "2026-09-20",
    "time_slot": "19:30",
}


@pytest.fixture(scope="module")
def fast():
    return FastPath(Catalogue(golden.seed_catalogue()), today=TODAY)


def _shape(hit):
    if hit is None:
        return None
    return hit.intent, {k: v for k, v in hit.slots.items() if v}


# ===========================================================================
# NOTHING BENGALI CHANGES
# ===========================================================================
BENGALI_SLOT_ANSWERS = (
    "আজ",
    "কাল",
    "পরশু",
    "সোমবার",
    "১৫ তারিখে",
    "সাড়ে দশটা",
    "সন্ধ্যা ৭টা",
    "সোয়া ছটায়",
    "হ্যাঁ",
    "না",
    "ঠিক আছে",
    "লাগবে না",
    "৯৮৩০০১২৩৪৫",
    "আমার নাম জয়া সেন",
)


@pytest.mark.parametrize("case_id", sorted(GOLDEN["fast_path"]))
def test_every_golden_utterance_reaches_every_parser_unchanged(case_id):
    utterance = GOLDEN["fast_path"][case_id]["utterance"]
    assert code_mix.for_fast_path(utterance) == code_mix.Mixed(utterance, 0)
    assert code_mix.for_slots(utterance) == code_mix.Mixed(utterance, 0)
    assert code_mix.for_matching(utterance) == code_mix.Mixed(utterance, 0)


@pytest.mark.parametrize("answer", BENGALI_SLOT_ANSWERS)
def test_a_bengali_slot_answer_is_shown_to_the_parsers_unchanged(answer):
    assert code_mix.for_slots(answer).changed == 0
    assert code_mix.for_fast_path(answer).changed == 0


def test_no_word_is_claimed_by_two_meanings():
    """A variant listed under two canonical forms would silently take whichever
    came last. Every spelling must mean one thing."""
    seen: dict[str, tuple[str, str]] = {}
    clashes = []
    groups = [(k, c, v) for k, c, v in code_mix._GROUPS] + [
        (code_mix.NUMBER, d, v) for d, v in code_mix._NUMBER_WORDS.items()
    ]
    for kind, canonical, variants in groups:
        for variant in variants:
            key = code_mix._key(variant)
            if key in seen and seen[key] != (kind, canonical):
                clashes.append((variant, seen[key], (kind, canonical)))
            seen[key] = (kind, canonical)
    assert clashes == []


def test_the_module_records_nothing():
    """The words of a turn are PHI. No logger, no print."""
    tree = ast.parse((gate_support.ROOT / "agent" / "code_mix.py").read_text(encoding="utf-8"))
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert "logging" not in imported
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id == "print"]


# ===========================================================================
# UNDERSTOOD NOW -- the fast path, before and after
# ===========================================================================
@pytest.mark.parametrize(
    ("said", "expected"),
    [
        ("CBC ka rate kitna hai", ("test_rate", {"test_name": "সিবিসি"})),
        ("सीबीसी टेस्ट का रेट क्या है", ("test_rate", {"test_name": "সিবিসি"})),
        ("ESR test ka price", ("test_rate", {"test_name": "ইএসআর"})),
        ("lipid profile ka rate kitna", ("test_rate", {"test_name": "লিপিড প্রোফাইল"})),
        ("HbA1c ka rate", ("test_rate", {"test_name": "এইচবিএ১সি"})),
        ("doctor Sen kab baithenge", ("doctor_availability", {"doctor_name": "sen"})),
        ("डॉक्टर सेन कल बैठेंगे", ("doctor_availability", {"doctor_name": "সেন", "date": TOMORROW})),
        (
            "Dr Ghosh tomorrow chamber e thakben?",
            ("doctor_availability", {"doctor_name": "ghosh", "date": TOMORROW}),
        ),
        ("डॉक्टर मुखर्जी कब बैठेंगे", ("doctor_availability", {"doctor_name": "মুখার্জী"})),
        ("hello", ("smalltalk", {})),
        ("thank you", ("smalltalk", {})),
    ],
)
def test_a_mixed_question_is_now_understood_locally(fast, said, expected):
    assert fast.resolve(said) is None  # before: not understood
    assert _shape(fast.resolve(code_mix.for_fast_path(said).text)) == expected


@pytest.mark.parametrize(
    "said",
    [
        "সিবিসির দাম কত aur ESR",  # two tests: was answered with ESR alone
        "ডাক্তার সেন monday বসবেন",  # Monday: was answered for today
        "ডাক্তার সেন কাল বসবেন na?",  # a negation: was ignored
    ],
)
def test_a_guard_word_in_another_language_now_makes_the_fast_path_abstain(fast, said):
    assert fast.resolve(said) is not None  # before: a confident, wrong answer
    assert fast.resolve(code_mix.for_fast_path(said).text) is None


@pytest.mark.parametrize(
    "said",
    [
        "CBC aur ESR ka rate",
        "Dr Sen monday ko baithenge",
        "Dr Sen 15 september ko baithenge",
        "Dr Sen next week kab baithenge",
        "CBC nahi dusra test ka rate",
        "Dr Sen ka appointment book karna hai",
        "doctor Thakur kab baithenge",
        "सब टेस्ट की लिस्ट",
    ],
)
def test_what_must_go_to_the_model_still_does_in_any_language(fast, said):
    assert fast.resolve(code_mix.for_fast_path(said).text) is None


@pytest.mark.parametrize(
    ("token", "spelled"),
    [("cbc", "সিবিসি"), ("esr", "ইএসআর"), ("tsh", "টিএসএইচ"), ("hba1c", "এইচবিএ১সি"), ("lft", "এলএফটি")],
)
def test_an_acronym_is_spelled_the_way_the_catalogue_holds_it(token, spelled):
    assert code_mix._spell_acronym(token) == spelled


@pytest.mark.parametrize("word", ["ka", "my", "by", "why", "sen", "hai", "2026"])
def test_an_ordinary_word_is_not_taken_for_an_acronym(word):
    assert code_mix._spell_acronym(word) is None


def test_devanagari_is_written_in_bengali_script():
    assert code_mix.to_bengali_script("सेन") == "সেন"
    assert code_mix.to_bengali_script("सीबीसी") == "সিবিসি"
    assert code_mix.to_bengali_script("সেন") == "সেন"  # already Bengali: untouched


# ===========================================================================
# SLOTS
# ===========================================================================
@pytest.mark.parametrize(
    ("said", "offset"),
    [("kal", 1), ("kal ko", 1), ("parso", 2), ("টুমরো", 1), ("aaj hi", 0), ("kal nahi parso", 2)],
)
def test_a_mixed_day_parses(said, offset):
    assert slot_parse.parse_date(said, TODAY) is None
    expected = (TODAY + datetime.timedelta(days=offset)).isoformat()
    assert code_mix.first_parse(slot_parse.parse_date, said, TODAY) == expected


def test_a_weekday_said_in_bengali_script_english_parses():
    value = code_mix.first_parse(slot_parse.parse_date, "সানডে", TODAY)
    assert datetime.date.fromisoformat(value).weekday() == 6


@pytest.mark.parametrize(
    ("said", "expected"),
    [
        ("saat baje", "19:00"),
        ("saadhe saat", "19:30"),
        ("subah das baje", "10:00"),
        ("evening 6 o'clock", "18:00"),
        ("সেভেন পিএম", "19:00"),
        ("শাম সাত বজে", "19:00"),
    ],
)
def test_a_mixed_time_parses(said, expected):
    assert slot_parse.parse_time(said) is None
    assert code_mix.first_parse(slot_parse.parse_time, said) == expected


@pytest.mark.parametrize(
    "said",
    [
        "nine eight three zero zero one two three four five",
        "nine eight three double zero one two three four five",
        "नौ आठ तीन शून्य शून्य एक दो तीन चार पांच",
        "৯৮৩০০ one two three four five",
        "নাইন এইট থ্রি ডাবল জিরো ওয়ান টু থ্রি ফোর ফাইভ",
    ],
)
def test_a_phone_number_said_in_words_parses(said):
    assert slot_parse.parse_phone(said) is None
    assert code_mix.first_parse(slot_parse.parse_phone, said) == "9830012345"


def test_triple_repeats_the_digit_after_it():
    assert code_mix.for_slots("triple zero").text == "0 0 0"
    assert code_mix.for_slots("double 5").text == "5 5"


@pytest.mark.parametrize("said", ["haan ji", "হাঁ", "ইয়েস", "theek hai", "ok ji", "yes please"])
def test_a_mixed_yes_is_a_yes(said):
    assert code_mix.first_parse(slot_parse.is_affirmative, said) is True


@pytest.mark.parametrize("said", ["nahi", "নেহি", "নো থ্যাংকস", "nahi ji", "no no"])
def test_a_mixed_no_is_a_no(said):
    assert code_mix.first_parse(slot_parse.is_negative, said) is True


@pytest.mark.parametrize("said", ["नहीं चाहिए", "रहने दो", "no thanks", "লাগবে না"])
def test_a_no_the_parser_already_knew_is_still_a_no(said):
    assert slot_parse.is_negative(said) is True
    assert code_mix.first_parse(slot_parse.is_negative, said) is True


def test_the_callers_own_words_are_always_asked_first():
    asked: list[str] = []

    def parser(text):
        asked.append(text)
        return "parsed" if text == "kal" else None

    assert code_mix.first_parse(parser, "kal") == "parsed"
    assert asked == ["kal"]  # the mixed view was never needed

    asked.clear()
    assert code_mix.first_parse(parser, "সোমবার") is None
    assert asked == ["সোমবার"]  # Bengali: no second look at all


def test_everything_is_off_when_switched_off(monkeypatch, fast):
    monkeypatch.setenv("VOICE_AGENT_CODE_MIX", "0")
    said = "CBC ka rate kitna hai"
    assert code_mix.for_fast_path(said) == code_mix.Mixed(said, 0)
    assert code_mix.first_parse(slot_parse.parse_date, "kal", TODAY) is None


# ===========================================================================
# THE REAL TURN -- main._run_turn
# ===========================================================================
class FakeCache:
    def get(self, text):
        return None, "miss"

    def put(self, text, data):
        pass


@pytest.fixture
def line(monkeypatch):
    speech = gate_support.SpeechLog()
    tools = gate_support.FakeTools(
        responses={
            "get_test_rate": RATE,
            "get_doctor_availability": {"found": True, "doctor_name": "Dr. A. Sen", "available": False},
            "book_appointment": BOOKED,
        }
    )
    model_heard: list[str] = []
    intents: list[tuple[str, dict]] = []

    def fake_extract(text):
        model_heard.append(text)
        slots = dict.fromkeys(("test_name", "doctor_name", "date", "time_slot", "patient_name", "phone"))
        return {"intent": "unclear", "slots": slots, "direct_reply_bn": None}, {
            "total_time_s": 0.0,
            "attempts": 1,
            "errors": [],
        }

    real_record = app._record_intent

    def record(session, data, source, **detail):
        intents.append((source, detail))
        real_record(session, data, source, **detail)

    monkeypatch.delenv("VOICE_AGENT_CODE_MIX", raising=False)
    monkeypatch.setattr(app, "_speak", speech)
    monkeypatch.setattr(app, "_tools", tools)
    monkeypatch.setattr(app, "_fast_path", FastPath(Catalogue(golden.seed_catalogue()), today=TODAY))
    monkeypatch.setattr(app, "_intent_cache", FakeCache())
    monkeypatch.setattr(app, "extract_intent", fake_extract)
    monkeypatch.setattr(app, "_record_intent", record)
    monkeypatch.setattr(app, "_audit_store", None)
    monkeypatch.setattr(app, "_conversations", None)
    return types.SimpleNamespace(speech=speech, tools=tools, model_heard=model_heard, intents=intents)


def _turn(text, pending=None):
    async def go():
        session = app.CallSession(gate_support.FakeWS())
        session.pending = pending
        try:
            await app._run_turn(session, "", text_override=text)
        finally:
            session.cleanup()
        return session

    return asyncio.run(go())


def _pending(awaiting, **slots):
    return {"awaiting": awaiting, "slots": slots, "candidates": None, "offered_date": None, "retries": 0}


def test_a_mixed_price_question_is_answered_without_the_model(line):
    _turn("CBC ka rate kitna hai")
    assert line.tools.calls == [("get_test_rate", ("সিবিসি",), {})]
    assert line.model_heard == []
    source, detail = line.intents[0]
    assert source == "fast_path" and detail["code_mix_words"] >= 3


def test_a_bengali_question_leaves_no_code_mix_mark(line):
    _turn("সিবিসি টেস্টের রেট কত")
    assert line.tools.calls == [("get_test_rate", ("সিবিসি",), {})]
    assert "code_mix_words" not in line.intents[0][1]


def test_the_model_always_hears_the_callers_own_words(line):
    said = "সিবিসির দাম কত aur ESR"
    _turn(said)
    assert line.tools.called() == []  # no half answer
    assert line.model_heard == [said]  # the original, not the Bengali view


def test_a_mixed_time_answer_fills_the_booking(line):
    session = _turn("saadhe saat baje", _pending("time_slot", doctor_name="Dr. A. Sen", date="2026-09-20"))
    assert session.pending["slots"]["time_slot"] == "19:30"
    assert session.pending["awaiting"] == "patient_name"


def test_a_phone_number_said_in_mixed_words_completes_the_booking(line):
    pending = _pending(
        "phone", doctor_name="Dr. A. Sen", date="2026-09-20", time_slot="19:30", patient_name="Jaya Sen"
    )
    _turn("nine eight three double zero one two three four five", pending)
    assert line.tools.calls == [
        ("book_appointment", ("Dr. A. Sen", "2026-09-20", "19:30", "Jaya Sen", "9830012345"), {})
    ]


def test_a_doctor_named_in_devanagari_is_picked_from_the_list(line):
    pending = _pending("doctor_choice")
    pending["candidates"] = [
        {"name": "Dr. A. Sen", "name_bn": "সেন"},
        {"name": "Dr. P. Ghosh", "name_bn": "ঘোষ"},
    ]
    pending["offered_date"] = "2026-09-20"
    _turn("डॉक्टर सेन", pending)
    assert line.tools.calls[0] == ("get_doctor_availability", ("Dr. A. Sen", "2026-09-20"), {})


def test_a_mixed_no_abandons_the_booking(line):
    session = _turn("nahi ji", _pending("time_slot", doctor_name="Dr. A. Sen", date="2026-09-20"))
    assert session.pending is None
    assert line.tools.called() == []


def test_a_patient_name_is_never_rewritten(line):
    """ "Das" is also Hindi for ten. A name must reach the booking as said."""
    session = _turn(
        "Riya Das", _pending("patient_name", doctor_name="Dr. A. Sen", date="2026-09-20", time_slot="19:30")
    )
    assert session.pending["slots"]["patient_name"] == "Riya Das"


def test_a_verification_answer_is_never_rewritten(line):
    said = "fourteen may nineteen ninety"
    pending = _pending("history_verify")
    pending.update(factor="dob", purpose="history")
    _turn(said, pending)
    name, args, _kw = line.tools.calls[0]
    assert name == "verify_caller" and args[2] == said


def test_switched_off_the_line_behaves_as_before(line, monkeypatch):
    monkeypatch.setenv("VOICE_AGENT_CODE_MIX", "0")
    said = "CBC ka rate kitna hai"
    _turn(said)
    assert line.tools.called() == []
    assert line.model_heard == [said]


# ===========================================================================
# HEARING ALL THREE LANGUAGES -- main._transcribe_in_caller_language
#
# A pod with Bengali, Hindi and English checkpoints and
# VOICE_AGENT_LANG_STRATEGY=parallel. Each fake checkpoint answers per clip,
# with the CTC/RNNT agreement a real one shows: high for its own language.
# ===========================================================================
class ClipASR:
    def __init__(self, heard):
        self.heard = heard
        self.calls: list[str] = []

    async def transcribe_utterance(self, wav_path):
        from agent.asr import ASRResult

        self.calls.append(wav_path)
        text, agreement = self.heard.get(wav_path, ("", 0.0))
        return ASRResult(text=text, decoder_used="rnnt", decoder_agreement=agreement)


class AuditLog:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def record(self, event_type, data=None, **_kw):
        self.events.append((event_type, dict(data or {})))


HINDI_QUESTION = "सीबीसी का रेट क्या है"


@pytest.fixture
def three_languages(monkeypatch):
    from agent import asr as asr_mod

    gate_support.trilingual(monkeypatch)
    monkeypatch.setenv("VOICE_AGENT_LANG_STRATEGY", "parallel")
    nodes = {
        "bn": ClipASR(
            {
                "greeting.wav": ("নমস্কার", 0.95),
                "hindi.wav": ("সিবি কা রেট", 0.20),
                "bengali.wav": ("সিবিসি টেস্টের রেট কত", 0.90),
                "unclear.wav": ("হুম", 0.30),
            }
        ),
        "hi": ClipASR(
            {
                "greeting.wav": ("नमस्कार", 0.40),
                "hindi.wav": (HINDI_QUESTION, 0.95),
                "bengali.wav": ("सिबिसि", 0.30),
                "unclear.wav": ("हम", 0.35),
            }
        ),
        "en": ClipASR(
            {
                "greeting.wav": ("no mo scar", 0.20),
                "hindi.wav": ("CB ka rate", 0.30),
                "bengali.wav": ("see bee", 0.20),
                "unclear.wav": ("hum", 0.32),
            }
        ),
    }
    monkeypatch.setattr(asr_mod, "_LANG_NODES", dict(nodes))
    session = types.SimpleNamespace(lang="bn", language_probe_done=False, call_id="mixed", audit=AuditLog())
    return nodes, session


def _hear(session, clip):
    return asyncio.run(app._transcribe_in_caller_language(session, clip))


def test_a_later_turn_in_another_language_is_heard_in_that_language(three_languages):
    nodes, session = three_languages
    assert _hear(session, "greeting.wav").text == "নমস্কার"  # turn one: probed, Bengali
    assert session.lang == "bn"

    result = _hear(session, "hindi.wav")  # turn two: the caller asks in Hindi

    assert result.text == HINDI_QUESTION
    assert session.lang == "hi"
    assert nodes["en"].calls == ["greeting.wav", "hindi.wav"]  # every checkpoint heard it
    assert ("LANGUAGE_DETECTED", {"language_from": "bn", "language_to": "hi", "source": "asr_reprobe"}) in (
        session.audit.events
    )


def test_a_turn_the_calls_language_heard_well_costs_no_extra_decode(three_languages):
    nodes, session = three_languages
    _hear(session, "greeting.wav")
    assert _hear(session, "bengali.wav").text == "সিবিসি টেস্টের রেট কত"
    assert "bengali.wav" not in nodes["hi"].calls + nodes["en"].calls


def test_a_near_tie_does_not_flip_the_calls_language(three_languages):
    nodes, session = three_languages
    _hear(session, "greeting.wav")
    result = _hear(session, "unclear.wav")  # everyone heard it badly
    assert "unclear.wav" in nodes["hi"].calls  # it was re-heard...
    assert session.lang == "bn" and result.text == "হুম"  # ...and nothing clearly better turned up


def test_the_fixed_strategy_never_reprobes(three_languages, monkeypatch):
    nodes, session = three_languages
    monkeypatch.setenv("VOICE_AGENT_LANG_STRATEGY", "fixed")
    assert _hear(session, "hindi.wav").text == "সিবি কা রেট"
    assert nodes["hi"].calls == [] and nodes["en"].calls == []


def test_a_pod_with_one_checkpoint_never_reprobes(three_languages, monkeypatch):
    nodes, session = three_languages
    for var in ("VOICE_AGENT_LANGUAGES", "VOICE_AGENT_NEMO_FILE_HI", "VOICE_AGENT_NEMO_FILE_EN"):
        monkeypatch.delenv(var, raising=False)
    _hear(session, "hindi.wav")
    assert nodes["hi"].calls == [] and nodes["en"].calls == []


def test_heard_in_hindi_then_understood_and_answered_in_hindi(three_languages, line):
    """Hearing and understanding together: the Hindi checkpoint's Devanagari
    transcript is read by the Bengali fast path through code_mix, and the
    answer is spoken in the language the caller used."""
    _nodes, heard_session = three_languages
    _hear(heard_session, "greeting.wav")
    transcript = _hear(heard_session, "hindi.wav").text

    async def go():
        session = app.CallSession(gate_support.FakeWS())
        session.lang = heard_session.lang
        try:
            await app._run_turn(session, "", text_override=transcript)
        finally:
            session.cleanup()

    asyncio.run(go())
    assert line.tools.calls == [("get_test_rate", ("সিবিসি",), {})]
    assert line.model_heard == []
    reply = line.speech.texts[-1]
    assert "250" in reply and any(0x0900 <= ord(ch) <= 0x097F for ch in reply)  # in Hindi
