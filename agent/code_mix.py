"""A caller's mixed-language words, read the way the voice bot's own parts read them.

Author: Chakravardhan
Story:  "As a patient more comfortable speaking than typing, I want to send a
         voice note in whatever mixture I speak, so that literacy is not a
         barrier."

On the VOICE BOT (main.py / main_pcm.py -- the caller speaks, the bot
listens) this means: a caller who says one sentence in several
languages -- "CBC ka rate kitna hai", "ডাক্তার সেন kal baithenge?", "nine eight
three double zero ..." -- is understood as correctly as a caller who says it
all in Bengali. Nobody should have to pick one language, or type, to be
understood.

WHAT WAS WRONG
--------------
The local layers of the turn were written in Bengali:

  * agent/fast_path.py's cue words (রেট, কবে, বুক ...) and its GUARD words
    (আর, না, সব, weekday names) are Bengali-script only, and the catalogue's
    spoken forms are Bengali-script aliases (সিবিসি, ইএসআর ...).
  * agent/slot_parse.py reads dates, times, yes/no and phone numbers from
    Bengali, Devanagari and English words -- but not romanised Hindi
    ("kal", "saat baje"), not English spoken as number WORDS ("nine eight"),
    and not the Bengali-script spelling a Bengali checkpoint writes for an
    English or Hindi word ("টুমরো", "নেহি").

Measured on the seeded catalogue before this module:

    "CBC ka rate kitna hai"          -> not understood locally
    "ডাক্তার সেন monday বসবেন"         -> answered for TODAY, not Monday
    "সিবিসির দাম কত aur ESR"           -> quoted ONLY ESR -- half a two-test question
    "ডাক্তার সেন কাল বসবেন na?"        -> the negation was ignored

The last three are not "missed understanding". The guard words did not fire
because they were said in another language, so the fast path answered with
confidence something the caller did not ask. Understanding more of a mixture
is only safe if every language's GUARD words are understood as well as its
question words -- that is the rule this module is built on.

THREE VIEWS, NEVER A REWRITE OF THE RECORD
------------------------------------------
Nothing here changes what the caller said. The transcript that is audited,
cached and given to the model is the original. What changes is what each
LOCAL parser is shown:

  for_fast_path(text)  cue and guard words -> the Bengali the fast path reads;
                       clinical English -> the Bengali-script alias the
                       catalogue holds; acronyms spelled the way callers say
                       them (CBC -> সিবিসি); Devanagari written in Bengali
                       script; month / week / "next" -> a date marker, so a
                       date the fast path cannot resolve makes it abstain.
  for_slots(text)      dates, times, number words (incl. "double"/"triple"),
                       yes / no -> what slot_parse.py reads.
  for_matching(text)   Devanagari written in Bengali script, for matching a
                       spoken doctor name against a list.

THE RULES THAT KEEP IT SAFE
---------------------------
  1. NATIVE BENGALI IS NEVER REWRITTEN. Only words from another language (or
     another language's word in Bengali script) are in the tables, so an
     all-Bengali turn reaches every parser byte-for-byte as before.
     tests/test_code_mixed_speech.py checks that over the whole golden set.
  2. GUARDS TRAVEL WITH CUES. "and", "aur", "नहीं", "monday", "september" are
     mapped for the same reason "rate" is: to keep the fast path's abstention
     honest in every language.
  3. SLOTS ARE A SECOND CHANCE, NEVER A FIRST ONE. first_parse() asks each
     slot parser about the original words first; the mixed view is tried only
     when that found nothing. Whatever parsed yesterday parses identically.
  4. NAMES AND SECRETS ARE NEVER TOUCHED. A patient name, and the answer to a
     verification challenge, are not passed through here (see main.py).
  5. NO LOGGING. The words of a turn are PHI; this module records nothing.

VOICE_AGENT_CODE_MIX=0 turns all of it off.
"""

from __future__ import annotations

import dataclasses
import os
import re
import unicodedata
from collections.abc import Callable
from typing import TypeVar

from agent.bn_normalize import spell_out

T = TypeVar("T")

# ---------------------------------------------------------------------------
# What a mapped word is
# ---------------------------------------------------------------------------
CUE = "cue"  # question and guard words: every view
NUMBER = "number"  # a digit said as a word: every view
YES = "yes"
NO = "no"
FILLER = "filler"  # "hai", "ji", "please": ignored when deciding yes / no
ENTITY = "entity"  # clinical English -> catalogue alias: fast path only
DATE_MARK = "date_mark"  # a date the fast path cannot resolve: fast path only
REPEAT = "repeat"  # "double" / "triple" before a digit: slots only

# The fast path's own word for "this turn names a date": fast_path._resolve_date
# abstains on it. Used only in the fast-path view.
_DATE_MARKER = "তারিখ"
_YES_BN = "হ্যাঁ"
_NO_BN = "না"
_THANKS_BN = "ধন্যবাদ"
_OCLOCK_BN = "টায়"


def enabled() -> bool:
    return os.environ.get("VOICE_AGENT_CODE_MIX", "1").strip().lower() not in ("0", "false", "no", "off")


# ---------------------------------------------------------------------------
# THE TABLE -- canonical form <- the ways a caller (or a checkpoint) says it.
#
# Variants are romanised Hindi / Bengali, English, Devanagari, and the
# Bengali-script spelling of a NON-Bengali word. A native Bengali word is
# never a variant (rule 1). Multi-word variants are matched longest first.
# ---------------------------------------------------------------------------
_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    # -- price ---------------------------------------------------------------
    (CUE, "রেট", ("rate", "rates", "रेट")),
    (CUE, "দাম", ("price", "prices", "daam", "keemat", "kimat", "qimat", "कीमत", "दाम", "কীমত", "কিমত")),
    (CUE, "খরচ", ("cost", "costs", "kharcha", "kharch", "खर्चा", "खर्च", "কস্ট", "খর্চা")),
    (CUE, "চার্জ", ("charge", "charges", "fee", "fees", "चार्ज", "फीस", "ফিস")),
    (CUE, "টাকা", ("rupees", "rupee", "rs", "paisa", "paise", "rupaye", "taka", "रुपये", "रुपए", "पैसे", "पैसा")),
    (CUE, "কত", ("how much", "kitna", "kitne", "kitni", "koto", "कितना", "कितने", "कितनी", "কিতনা", "কিতনে")),
    # -- when does a doctor sit ----------------------------------------------
    (CUE, "কবে", ("when", "kab", "kobe", "कब", "কব")),
    (CUE, "কখন", ("what time", "kokhon", "किस समय")),
    (
        CUE,
        "বসবেন",
        (
            "sit",
            "sits",
            "sitting",
            "available",
            "availability",
            "baithenge",
            "baithte",
            "baithegi",
            "milenge",
            "बैठेंगे",
            "बैठते",
            "बैठेंगी",
            "बैठती",
            "मिलेंगे",
            "বৈঠেঙ্গে",
            "বৈঠতে",
            "মিলেঙ্গে",
            "অ্যাভেলেবল",
        ),
    ),
    (CUE, "চেম্বার", ("chamber", "चैंबर", "चेंबर")),
    (CUE, "শিডিউল", ("schedule", "timing", "timings", "शेड्यूल", "टाइमिंग")),
    (CUE, "ভিজিট", ("visit", "विजिट")),
    # -- booking: always handed to the model --------------------------------
    (CUE, "বুক", ("book", "booked", "booking", "बुक", "बुकिंग")),
    (CUE, "অ্যাপয়েন্টমেন্ট", ("appointment", "appointments", "अपॉइंटमेंट", "अपॉइन्टमेंट", "অপয়েন্টমেন্ট")),
    (CUE, "স্লট", ("slot", "slots", "स्लॉट")),
    (CUE, "সিরিয়াল", ("serial", "सीरियल")),
    # -- greeting and thanks -------------------------------------------------
    (CUE, "নমস্কার", ("namaste", "namaskar", "नमस्ते", "नमस्कार", "নমস্তে")),
    (CUE, "হ্যালো", ("hello", "hi", "हेलो", "हैलो")),
    (
        CUE,
        _THANKS_BN,
        (
            "thank you",
            "thanks",
            "dhanyavad",
            "shukriya",
            "धन्यवाद",
            "शुक्रिया",
            "শুক্রিয়া",
            "থ্যাংকস",
            "থ্যাঙ্কস",
            "থ্যাংক ইউ",
        ),
    ),
    # -- the nouns around a question ----------------------------------------
    (CUE, "ডাক্তার", ("doctor", "dr", "डॉक्टर", "डाक्टर")),
    (CUE, "টেস্ট", ("test", "tests", "jaanch", "janch", "टेस्ट", "जांच", "जाँच")),
    (CUE, "রিপোর্ট", ("report", "reports", "रिपोर्ट")),
    # -- GUARDS: a compound, negated or open question makes the fast path abstain
    (CUE, "আর", ("and", "also", "aur", "bhi", "और", "भी", "অউর", "ঔর")),
    (CUE, "নাকি", ("or", "ya", "या")),
    (CUE, "সব", ("all", "sab", "sabhi", "saare", "सब", "सभी", "सारे")),
    (CUE, "তালিকা", ("list", "सूची", "लिस्ट", "লিস্ট")),
    (CUE, "কোন", ("which", "kaun", "कौन", "কৌন")),
    (CUE, "কিন্তু", ("but", "lekin", "magar", "लेकिन", "मगर", "লেকিন")),
    (CUE, "অন্য", ("other", "another", "different", "dusra", "doosra", "दूसरा", "दूसरी", "দূসরা")),
    (CUE, "বদলে", ("instead", "badle", "बदले", "बजाय")),
    (CUE, "ছাড়া", ("except", "without", "chhodkar", "छोड़कर", "सिवाय", "बिना")),
    (CUE, "চেয়ে", ("than",)),
    (
        NO,
        _NO_BN,
        (
            "no",
            "not",
            "nope",
            "never",
            "don't",
            "dont",
            "nahi",
            "nahin",
            "nai",
            "na",
            "mat",
            "नहीं",
            "नही",
            "ना",
            "मत",
            "নেহি",
            "নহী",
            "নাহি",
            "নো",
        ),
    ),
    # -- days --------------------------------------------------------------
    (CUE, "আজ", ("today", "aaj", "aj", "आज", "টুডে")),
    (CUE, "কাল", ("tomorrow", "kal", "kaal", "कल", "টুমরো")),
    (CUE, "পরশু", ("day after tomorrow", "parso", "parson", "परसों", "परसो")),
    (CUE, "সোমবার", ("monday", "somvar", "somwar", "सोमवार", "মানডে")),
    (CUE, "মঙ্গলবার", ("tuesday", "mangalvar", "mangalwar", "मंगलवार", "টিউসডে")),
    (CUE, "বুধবার", ("wednesday", "budhvar", "budhwar", "बुधवार", "ওয়েডনেসডে")),
    (CUE, "বৃহস্পতিবার", ("thursday", "guruvar", "guruwar", "गुरुवार", "बृहस्पतिवार", "থার্সডে")),
    (CUE, "শুক্রবার", ("friday", "shukravar", "shukrawar", "शुक्रवार", "ফ্রাইডে")),
    (CUE, "শনিবার", ("saturday", "shanivar", "shaniwar", "शनिवार", "স্যাটারডে")),
    (CUE, "রবিবার", ("sunday", "ravivar", "raviwar", "itwar", "itvar", "रविवार", "इतवार", "সানডে")),
    # A date the fast path does not resolve itself -> it must abstain.
    (
        DATE_MARK,
        _DATE_MARKER,
        (
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
            "जनवरी",
            "फरवरी",
            "मार्च",
            "अप्रैल",
            "मई",
            "जून",
            "जुलाई",
            "अगस्त",
            "सितंबर",
            "अक्टूबर",
            "नवंबर",
            "दिसंबर",
            "date",
            "tarikh",
            "तारीख",
            "week",
            "hafte",
            "hafta",
            "हफ्ते",
            "हफ़्ते",
            "month",
            "mahina",
            "mahine",
            "महीने",
            "next",
            "agle",
            "अगले",
        ),
    ),
    # -- time of day ---------------------------------------------------------
    (CUE, _OCLOCK_BN, ("o'clock", "oclock", "o clock", "baje", "बजे", "বজে")),
    (CUE, "সাড়ে", ("half past", "saadhe", "sadhe", "साढ़े", "সাঢ়ে")),
    (CUE, "সোয়া", ("quarter past", "sava", "sawa", "सवा")),
    (CUE, "পৌনে", ("quarter to", "paune", "पौने")),
    (CUE, "সকাল", ("morning", "subah", "सुबह", "সুবহ", "মর্নিং")),
    (CUE, "দুপুর", ("afternoon", "noon", "dopahar", "दोपहर", "দোপহর")),
    (CUE, "সন্ধ্যা", ("evening", "shaam", "sham", "शाम", "শাম", "ইভনিং")),
    (CUE, "রাত", ("night", "raat", "रात", "নাইট")),
    (CUE, "pm", ("p.m.", "পিএম")),
    (CUE, "am", ("a.m.", "এএম")),
    # -- yes -----------------------------------------------------------------
    (
        YES,
        _YES_BN,
        (
            "yes",
            "yeah",
            "yep",
            "haan",
            "han",
            "haa",
            "ok",
            "okay",
            "sure",
            "alright",
            "right",
            "correct",
            "theek",
            "thik",
            "chalega",
            "हाँ",
            "हां",
            "ठीक",
            "चलेगा",
            "ইয়েস",
            "হাঁ",
        ),
    ),
    (FILLER, "", ("hai", "hain", "ji", "please", "plz", "sir", "madam", "है", "हैं", "जी", "হ্যায়", "প্লিজ")),
    (REPEAT, "2", ("double", "डबल", "ডাবল")),
    (REPEAT, "3", ("triple", "ट्रिपल", "ট্রিপল")),
    # -- clinical English -> the alias the catalogue holds --------------------
    (ENTITY, "সুগার", ("sugar",)),
    (ENTITY, "ফাস্টিং", ("fasting",)),
    (ENTITY, "ব্লাড", ("blood",)),
    (ENTITY, "লিপিড", ("lipid",)),
    (ENTITY, "প্রোফাইল", ("profile",)),
    (ENTITY, "কোলেস্টেরল", ("cholesterol",)),
    (ENTITY, "লিভার", ("liver",)),
    (ENTITY, "কিডনি", ("kidney",)),
    (ENTITY, "ফাংশন", ("function",)),
    (ENTITY, "থাইরয়েড", ("thyroid",)),
    (ENTITY, "ইউরিন", ("urine",)),
    (ENTITY, "রুটিন", ("routine",)),
    (ENTITY, "ডেঙ্গু", ("dengue",)),
    (ENTITY, "ম্যালেরিয়া", ("malaria",)),
    (ENTITY, "টাইফয়েড", ("typhoid",)),
    (ENTITY, "উইডাল", ("widal",)),
    (ENTITY, "হেপাটাইটিস", ("hepatitis",)),
    (ENTITY, "হিমোগ্লোবিন", ("hemoglobin", "haemoglobin")),
    (ENTITY, "এন্টিজেন", ("antigen",)),
)

# Digits said as words, in the languages that are NOT Bengali (rule 1: the
# Bengali number words are left exactly as they were).
_NUMBER_WORDS: dict[str, tuple[str, ...]] = {
    "0": ("zero", "shunya", "शून्य", "जीरो", "জিরো"),
    "1": ("one", "ek", "एक", "ওয়ান"),
    "2": ("two", "do", "दो", "টু", "দো"),
    "3": ("three", "teen", "तीन", "থ্রি", "তীন"),
    "4": ("four", "chaar", "char", "चार", "ফোর"),
    "5": ("five", "paanch", "panch", "पांच", "पाँच", "ফাইভ"),
    "6": ("six", "chhe", "chhah", "chah", "छह", "छः", "সিক্স"),
    "7": ("seven", "saat", "सात", "সেভেন"),
    "8": ("eight", "aath", "आठ", "এইট", "আঠ"),
    "9": ("nine", "nau", "नौ", "নাইন", "নৌ"),
    "10": ("ten", "das", "दस", "টেন", "দস"),
    "11": ("eleven", "gyarah", "ग्यारह", "ইলেভেন"),
    "12": ("twelve", "barah", "बारह", "টুয়েলভ"),
}

# Acronyms callers say letter by letter, which the catalogue holds spelled in
# Bengali ("সিবিসি"). A Latin token with no vowel, or with a digit, is always
# one; these are the ones that do contain a vowel.
_ACRONYMS_WITH_VOWELS = frozenset(
    {"esr", "ecg", "eeg", "ekg", "usg", "ige", "igg", "igm", "ana", "aso", "hiv", "hcv", "hbsag", "psa", "ra"}
)


def _key(word: str) -> str:
    return unicodedata.normalize("NFC", word).lower()


def _build() -> tuple[dict[str, tuple[str, str]], int]:
    table: dict[str, tuple[str, str]] = {}
    for kind, canonical, variants in _GROUPS:
        for variant in variants:
            table[_key(variant)] = (kind, canonical)
    for digit, variants in _NUMBER_WORDS.items():
        for variant in variants:
            table[_key(variant)] = (NUMBER, digit)
    longest = max(len(k.split()) for k in table)
    return table, longest


_TABLE, _LONGEST_PHRASE = _build()

_EDGE_PUNCT = "\"'()[]{}.,;:!?।॥-–—"
_LATIN_TOKEN = re.compile(r"^[a-z0-9]+$")
_TO_BN_DIGITS = str.maketrans("0123456789", "০১২৩৪৫৬৭৮৯")


# ---------------------------------------------------------------------------
# Devanagari -> Bengali script
# ---------------------------------------------------------------------------
def _devanagari_table() -> dict[int, str]:
    """The two blocks are laid out in parallel (Bengali = Devanagari + 0x80)
    wherever Bengali has the letter. The few Devanagari letters Bengali lacks
    are written as the Bengali letter a Kolkata listener hears for them."""
    table: dict[int, str] = {}
    for cp in range(0x0900, 0x0980):
        target = cp + 0x80
        if unicodedata.name(chr(cp), "") and unicodedata.name(chr(target), ""):
            table[cp] = chr(target)
    table.update(
        {
            0x0935: "ব",  # व
            0x0931: "র",  # ऱ
            0x0929: "ন",  # ऩ
            0x0933: "ল",  # ळ
            0x0934: "ল",  # ऴ
            0x0911: "অ",  # ऑ
            0x090D: "এ",  # ऍ
            0x090E: "এ",  # ऎ
            0x0912: "ও",  # ऒ
            0x0945: "ে",  # ॅ
            0x0946: "ে",  # ॆ
            0x0949: "ো",  # ॉ
            0x094A: "ো",  # ॊ
            0x0964: "।",
            0x0965: "।",
        }
    )
    return table


_DEVANAGARI_TO_BENGALI = _devanagari_table()
# Hindi spells borrowed English with long vowels ("सीबीसी") where Bengali
# spells the same word short ("সিবিসি"). Folded in transliterated words only.
_FOLD_LONG_VOWELS = str.maketrans({"ী": "ি", "ূ": "ু"})


def _has_devanagari(word: str) -> bool:
    return any(0x0900 <= ord(ch) <= 0x097F for ch in word)


def to_bengali_script(word: str) -> str:
    if not _has_devanagari(word):
        return word
    return word.translate(_DEVANAGARI_TO_BENGALI).translate(_FOLD_LONG_VOWELS)


def _spell_acronym(token: str) -> str | None:
    """ "cbc" -> "সিবিসি", "hba1c" -> "এইচবিএ১সি" -- the way the catalogue
    spells an acronym a caller reads out letter by letter."""
    if not _LATIN_TOKEN.match(token) or not 2 <= len(token) <= 6 or token.isdigit():
        return None
    has_digit = any(ch.isdigit() for ch in token)
    # "y" counts as a vowel here, so "my", "by", "why" are words, not letters.
    has_vowel = any(ch in "aeiouy" for ch in token)
    if not (has_digit or not has_vowel or token in _ACRONYMS_WITH_VOWELS):
        return None
    return "".join(spell_out(ch) if ch.isalpha() else ch.translate(_TO_BN_DIGITS) for ch in token)


# ---------------------------------------------------------------------------
# Reading the words
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class _Word:
    said: str  # as the transcript has it
    kind: str | None  # None: not in the table
    canonical: str


def _read(text: str) -> list[_Word]:
    """Tokens, with the longest table phrase at each position."""
    raw = (text or "").split()
    keys = [_key(w.strip(_EDGE_PUNCT)) for w in raw]
    keys = [k[:-2] if k.endswith("'s") else k for k in keys]
    out: list[_Word] = []
    i = 0
    while i < len(raw):
        for span in range(min(_LONGEST_PHRASE, len(raw) - i), 0, -1):
            phrase = " ".join(keys[i : i + span])
            hit = _TABLE.get(phrase)
            if hit is not None:
                out.append(_Word(" ".join(raw[i : i + span]), hit[0], hit[1]))
                i += span
                break
        else:
            out.append(_Word(raw[i], None, keys[i]))
            i += 1
    return out


@dataclasses.dataclass(frozen=True)
class Mixed:
    """One view of a turn: the words a parser is shown, and how many of them
    were read from another language."""

    text: str
    changed: int


def _unchanged(text: str) -> Mixed:
    return Mixed(text, 0)


def for_fast_path(text: str) -> Mixed:
    """The words agent/fast_path.py is shown. Changes nothing in a Bengali turn."""
    if not enabled():
        return _unchanged(text)
    words = _read(text)
    out: list[str] = []
    changed = 0
    for w in words:
        if w.kind in (CUE, NO, YES, ENTITY, DATE_MARK, NUMBER):
            out.append(w.canonical)
            changed += 1
            continue
        spelled = _spell_acronym(w.canonical) if w.kind is None else None
        if spelled:
            out.append(spelled)
            changed += 1
            continue
        written = to_bengali_script(w.said)
        changed += written != w.said
        out.append(written)
    return Mixed(" ".join(out), changed) if changed else _unchanged(text)


def for_slots(text: str) -> Mixed:
    """The words agent/slot_parse.py is shown: dates, times, numbers, yes / no."""
    if not enabled():
        return _unchanged(text)
    words = _read(text)
    meaningful = [w for w in words if w.kind != FILLER and not (w.kind == CUE and w.canonical == _THANKS_BN)]
    # A whole answer that is only yes (or only no), whatever language it was
    # said in, is the one word slot_parse's exact-match sets hold.
    if words and meaningful and len(words) != len([w for w in words if w.kind is None]):
        if all(w.kind == YES for w in meaningful):
            return Mixed(_YES_BN, len(words))
        if all(w.kind == NO for w in meaningful):
            return Mixed(_NO_BN, len(words))
    out: list[str] = []
    changed = 0
    repeat = 1
    for w in words:
        if w.kind == REPEAT:
            repeat = int(w.canonical)
            changed += 1
            continue
        if w.kind in (CUE, NO, YES, NUMBER):
            value = w.canonical
            changed += 1
        else:
            value = w.said
        # "double five" -> "5 5": said of the digit (or digit word) after it.
        if repeat > 1 and (w.kind == NUMBER or w.said.isdigit()):
            value = " ".join([value] * repeat)
        repeat = 1
        # "সাত বজে" -> "সাতটা": after a Bengali number WORD the o'clock suffix
        # is written joined, which is the only form slot_parse knows. After a
        # digit it stays apart ("7 টায়"), which slot_parse also reads.
        if w.kind == CUE and value == _OCLOCK_BN and out and not out[-1][-1:].isdigit():
            out[-1] += "টা"
            continue
        out.append(value)
    return Mixed(" ".join(out), changed) if changed else _unchanged(text)


def for_matching(text: str) -> Mixed:
    """A spoken name in Bengali script, for matching against a short list."""
    if not enabled():
        return _unchanged(text)
    written = " ".join(to_bengali_script(w) for w in (text or "").split())
    return Mixed(written, 1) if written != " ".join((text or "").split()) else _unchanged(text)


def first_parse(parse: Callable[..., T], text: str, *args: object, **kwargs: object) -> T:
    """Rule 3: `parse` on the caller's own words first; on the slot view of
    them only if that found nothing. Anything that parsed before this module
    existed parses identically."""
    value = parse(text, *args, **kwargs)
    if value:
        return value
    mixed = for_slots(text)
    if not mixed.changed:
        return value
    retried = parse(mixed.text, *args, **kwargs)
    return retried if retried else value


def first_match(match: Callable[..., T], text: str, *args: object) -> T:
    """first_parse for a spoken name: the second look is the Bengali-script view."""
    value = match(text, *args)
    if value:
        return value
    mixed = for_matching(text)
    if not mixed.changed:
        return value
    retried = match(mixed.text, *args)
    return retried if retried else value
