"""Which language a caller is speaking, and what that implies for every
component downstream.

WHY THIS IS ONE MODULE AND NOT A FLAG THREADED THROUGH EVERYTHING
-----------------------------------------------------------------
A language choice is not one setting. It selects an ASR checkpoint, a TTS
checkpoint and speaker, a digit map, a set of date and yes/no words, and a
whole table of spoken replies. Scattering that across six files means the
seventh place that needs it gets forgotten, and the failure mode is a
caller being answered in a language they did not speak.

So every component asks this module, and this module is the only thing
that knows what "hi" means in practice.

WHAT IS AND IS NOT TRUE TODAY
-----------------------------
The plumbing here is complete and tested. The MODELS are not present:

  * ASR is `ai4bharat/indicconformer_stt_bn_hybrid_ctc_rnnt_large` -- a
    BENGALI-ONLY checkpoint. It cannot transcribe Hindi or English, and no
    amount of code here changes that. Hindi and English become real when a
    checkpoint for each is on the pod and named in the environment.
  * TTS is a Bengali FastPitch checkpoint at /workspace/tts_checkpoints/bn.
    tts_server.py already accepts a `lang` field and currently ignores it.

Because of that, the DEFAULT STRATEGY IS "fixed" AND THE DEFAULT LANGUAGE
IS BENGALI -- which is exactly what the system did before this module
existed. A pod with no extra checkpoints behaves identically to how it
behaved yesterday. Nothing here degrades the Bengali path in order to
promise multilingual support that the models cannot deliver.

Set VOICE_AGENT_LANGUAGES to turn the others on once their checkpoints
exist. enabled() refuses to enable a language whose ASR checkpoint is not
configured, so the promise and the capability cannot drift apart.
"""
from __future__ import annotations

import contextvars
import dataclasses
import os

BN = "bn"
HI = "hi"
EN = "en"

ALL_LANGS = (BN, HI, EN)

# Unicode blocks. Script is the cheapest signal available for text that is
# already transcribed, and it separates these three cleanly: Bengali and
# Devanagari share no code points, and neither shares any with Latin.
_BENGALI_RANGE = (0x0980, 0x09FF)
_DEVANAGARI_RANGE = (0x0900, 0x097F)


@dataclasses.dataclass(frozen=True)
class LanguageSpec:
    """Everything downstream needs to know about one language.

    `asr_checkpoint_env` is a variable NAME, not a value. The checkpoint
    path differs per pod and is nobody's business but the deployment's;
    what this file owns is which variable to look in.
    """
    code: str
    english_name: str
    native_name: str
    asr_checkpoint_env: str
    asr_language_id: str          # what NeMo's transcribe(language_id=...) wants
    tts_lang: str                 # what tts_server.py's SynthesizeRequest wants
    tts_speaker_env: str
    digits: str                   # native digit glyphs, "" when ASCII


SPECS: dict[str, LanguageSpec] = {
    BN: LanguageSpec(
        code=BN, english_name="Bengali", native_name="বাংলা",
        asr_checkpoint_env="VOICE_AGENT_NEMO_FILE",      # the existing variable
        asr_language_id="bn",
        tts_lang="bn", tts_speaker_env="TTS_SPEAKER",
        digits="০১২৩৪৫৬৭৮৯",
    ),
    HI: LanguageSpec(
        code=HI, english_name="Hindi", native_name="हिंदी",
        asr_checkpoint_env="VOICE_AGENT_NEMO_FILE_HI",
        asr_language_id="hi",
        tts_lang="hi", tts_speaker_env="TTS_SPEAKER_HI",
        digits="०१२३४५६७८९",
    ),
    EN: LanguageSpec(
        code=EN, english_name="English", native_name="English",
        asr_checkpoint_env="VOICE_AGENT_NEMO_FILE_EN",
        asr_language_id="en",
        tts_lang="en", tts_speaker_env="TTS_SPEAKER_EN",
        digits="",                                        # already ASCII
    ),
}

DEFAULT_LANG = BN

# How the language of a turn is decided.
#
#   "fixed"    -- every caller gets DEFAULT_LANG. What the system did before
#                 this module existed, and the only honest default while ASR
#                 is a single Bengali checkpoint.
#   "script"   -- read the script of the transcript. Correct and free, but
#                 only meaningful once ASR can EMIT more than one script;
#                 a Bengali-only model returns Bengali glyphs for Hindi
#                 speech, so this would report "bn" for every caller.
#   "parallel" -- decode the first turn with every enabled checkpoint and
#                 keep the best. The only strategy that genuinely detects a
#                 language from audio, and the most expensive: N decodes on
#                 turn one, on a GPU already shared with TTS.
STRATEGY_FIXED = "fixed"
STRATEGY_SCRIPT = "script"
STRATEGY_PARALLEL = "parallel"


def strategy() -> str:
    value = os.environ.get("VOICE_AGENT_LANG_STRATEGY", STRATEGY_FIXED).strip().lower()
    return value if value in (STRATEGY_FIXED, STRATEGY_SCRIPT, STRATEGY_PARALLEL) else STRATEGY_FIXED


def default_lang() -> str:
    value = os.environ.get("VOICE_AGENT_DEFAULT_LANG", DEFAULT_LANG).strip().lower()
    return value if value in SPECS else DEFAULT_LANG


# A WRITTEN CHANNEL HEARS NOTHING -- Author: Chakravardhan
#
# enabled() gates Hindi and English on an ASR checkpoint, which is right for
# speech and wrong for text: a typed message needs no speech model, and
# agent/i18n.py holds every sentence in all three languages. Without this, a
# Hindi WhatsApp message on a Bengali-only pod is detected as Hindi and then
# answered in Bengali, because resolve() would fold it straight back.
#
# A ContextVar rather than a flag: one message service answers many patients
# at once, each in its own task, and asyncio gives every task its own copy.
_TEXT_CHANNEL: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "language_text_channel", default=False)


def use_text_channel(flag: bool = True) -> contextvars.Token:
    """Mark this turn as WRITTEN. Reset with the token it returns."""
    return _TEXT_CHANNEL.set(flag)


def reset_text_channel(token: contextvars.Token) -> None:
    _TEXT_CHANNEL.reset(token)


def text_languages() -> tuple[str, ...]:
    """Every language a WRITTEN turn may be answered in: what
    VOICE_AGENT_LANGUAGES lists, or all three when it lists nothing. No ASR
    gate -- there is no audio to transcribe."""
    raw = os.environ.get("VOICE_AGENT_LANGUAGES", "").strip()
    wanted = [c.strip().lower() for c in raw.split(",") if c.strip()] if raw else list(ALL_LANGS)
    out = [c for c in wanted if c in SPECS]
    if default_lang() not in out:
        out.insert(0, default_lang())
    return tuple(dict.fromkeys(out))


def enabled() -> tuple[str, ...]:
    """The languages this pod can ACTUALLY serve, in preference order.

    Two gates, and the second is the important one:

      1. the language is listed in VOICE_AGENT_LANGUAGES;
      2. its ASR checkpoint variable is set -- EXCEPT for the default
         language, whose checkpoint agent/asr.py already locates by search
         when the variable is unset.

    Both gates are about SPEECH. On a written channel (use_text_channel)
    there is no audio to hear, so text_languages() applies instead.

    Gate 2 exists so "we support Hindi" can never be true in configuration
    while being false in fact. A caller answered in a language the ASR
    cannot hear is worse off than a caller answered in Bengali, because the
    system sounds like it understood.
    """
    if _TEXT_CHANNEL.get():
        return text_languages()

    raw = os.environ.get("VOICE_AGENT_LANGUAGES", "").strip()
    wanted = [c.strip().lower() for c in raw.split(",") if c.strip()] if raw else [default_lang()]

    out = []
    for code in wanted:
        spec = SPECS.get(code)
        if spec is None:
            continue
        if code == default_lang() or os.environ.get(spec.asr_checkpoint_env, "").strip():
            out.append(code)
    if default_lang() not in out:
        out.insert(0, default_lang())
    return tuple(dict.fromkeys(out))


def is_enabled(lang: str) -> bool:
    return lang in enabled()


def resolve(lang: str | None) -> str:
    """-> a language this pod can serve. NEVER raises, never returns an
    unsupported code.

    Every caller-facing lookup goes through here, so a bad language code
    anywhere degrades to answering in the default language rather than to a
    KeyError mid-call.
    """
    if lang and lang.lower() in SPECS and is_enabled(lang.lower()):
        return lang.lower()
    return default_lang()


# ---------------------------------------------------------------------------
# Script detection
# ---------------------------------------------------------------------------
def script_counts(text: str) -> dict[str, int]:
    """Characters per script. Digits and punctuation are ignored: they are
    shared, and a string of them says nothing about the language."""
    counts = {BN: 0, HI: 0, EN: 0}
    for ch in text or "":
        cp = ord(ch)
        if _BENGALI_RANGE[0] <= cp <= _BENGALI_RANGE[1]:
            counts[BN] += 1
        elif _DEVANAGARI_RANGE[0] <= cp <= _DEVANAGARI_RANGE[1]:
            counts[HI] += 1
        elif ch.isascii() and ch.isalpha():
            counts[EN] += 1
    return counts


def detect_from_text(text: str, fallback: str | None = None) -> str:
    """-> the language whose script dominates `text`.

    Ties and empty input fall back rather than guessing. A caller who says
    a single English word inside a Bengali sentence ("রিপোর্ট ready তো?")
    must not flip the whole call into English, which is why this is a
    majority test and not an any-match test.
    """
    counts = script_counts(text)
    total = sum(counts.values())
    if total == 0:
        return resolve(fallback)
    best = max(counts, key=lambda k: counts[k])
    if counts[best] * 2 <= total:          # no majority
        return resolve(fallback)
    return resolve(best) if is_enabled(best) else resolve(fallback)


# ---------------------------------------------------------------------------
# Explicit switching
# ---------------------------------------------------------------------------
# A caller asking for a language by name, in any of the three. Matched as
# substrings because these arrive mid-sentence ("can you speak English",
# "বাংলায় বলুন"), unlike slot_parse.py's yes/no sets which gate whole-turn
# decisions and are exact-match for that reason.
_SWITCH_PHRASES: dict[str, tuple[str, ...]] = {
    BN: ("বাংলা", "বাংলায়", "bangla", "bengali", "बांग्ला", "बंगाली"),
    HI: ("हिंदी", "हिन्दी", "hindi", "हिंदी में", "হিন্দি"),
    EN: ("english", "ইংরেজি", "ইংলিশ", "अंग्रेज़ी", "अंग्रेजी", "इंग्लिश"),
}


def requested_switch(text: str) -> str | None:
    """-> the language the caller just asked to be served in, or None.

    Only returns a language this pod can actually serve. Asking for Hindi
    on a pod with no Hindi checkpoint returns None, and the caller keeps
    being understood in the language that works -- see reply_templates'
    language_unavailable_reply() for what they are told.
    """
    if not text:
        return None
    low = text.lower()
    for code, phrases in _SWITCH_PHRASES.items():
        if any(p in low for p in phrases) and is_enabled(code):
            return code
    return None


def requested_switch_unavailable(text: str) -> str | None:
    """-> a language the caller asked for that this pod CANNOT serve.

    Kept separate from requested_switch() so the caller can be told the
    truth ("we only have Bengali on this line") instead of being silently
    ignored, which reads as the system not having heard them.
    """
    if not text:
        return None
    low = text.lower()
    for code, phrases in _SWITCH_PHRASES.items():
        if any(p in low for p in phrases) and not is_enabled(code):
            return code
    return None


# ---------------------------------------------------------------------------
# Digits
# ---------------------------------------------------------------------------
def digit_translation() -> dict[int, int]:
    """One table folding EVERY supported script's digits to ASCII.

    Deliberately combined rather than per-language: a caller speaking Hindi
    to an ASR model that emits Bengali numerals is a real mixed case on a
    shared pod, and a phone number half-parsed because the digits were in
    the "wrong" script is a booking that silently cannot be messaged.
    """
    table: dict[int, int] = {}
    for spec in SPECS.values():
        if spec.digits:
            table.update(str.maketrans(spec.digits, "0123456789"))
    return table


DIGITS_TO_ASCII = digit_translation()


def to_ascii_digits(text: str) -> str:
    return (text or "").translate(DIGITS_TO_ASCII)
