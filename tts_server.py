"""AI4Bharat Bengali TTS -- FastAPI wrapper around coqui-tts's Synthesizer.

Matches the contract agent/tts.py's TTSClient expects: POST /synthesize
with {"text": ..., "lang": "bn"} -> raw WAV bytes.

The Synthesizer is loaded ONCE at module import (not per-request) --
model load takes several seconds and holds real GPU memory, so this must
be a long-lived process, not spawned per call.

WHY THIS DOES MORE THAN CALL synthesizer.tts()
----------------------------------------------
A single FastPitch pass over a whole multi-clause reply is what makes this
voice sound like a machine, and it is fixable without changing models:

* No breaths. FastPitch renders one flat prosodic contour across an entire
  utterance. Real speakers stop between clauses. Splitting on Bengali
  sentence and clause boundaries and inserting real silence is the single
  largest naturalness gain available here.
* Ragged padding. Each pass emits its own leading/trailing near-silence of
  arbitrary length, so naive concatenation produces gaps that are too long
  in some places and absent in others. Each chunk is trimmed, then padded
  by an amount chosen for the punctuation that ended it.
* Rushed delivery. length_scale 1.0 is noticeably fast for a service line
  a caller is trying to write a price down from. Slightly above 1 reads as
  measured rather than sluggish.
* Inconsistent level. Peak varies per utterance, which on a phone sounds
  like the speaker keeps moving. Normalizing to a fixed peak fixes it.

Every one of these is tunable per-request (see SynthesizeRequest) so the
settings can be A/B'd against a real handset without a redeploy.
"""
import io
import logging
import os
import re
import threading

# The AI4Bharat checkpoint's speaker manager resolves a RELATIVE path
# ("models/v1/bn/fastpitch/speakers.pth") baked in at save time, against
# whatever the process's CWD happens to be -- not the checkpoint's own
# location. Pin CWD explicitly so this doesn't depend on how/where this
# script gets launched from.
os.chdir("/workspace/tts_checkpoints")

import numpy as np
import soundfile as sf
from fastapi import FastAPI
from fastapi.responses import Response
from pydantic import BaseModel
from TTS.utils.synthesizer import Synthesizer

app = FastAPI()

# MULTILINGUAL CHECKPOINTS
# ------------------------
# The Bengali checkpoint keeps the path it has always had, and keeps being
# loaded eagerly at import, so this server starts and behaves exactly as it
# did. Hindi and English are looked for under a sibling directory and are
# loaded ONLY IF PRESENT, lazily, on the first request that asks for them.
#
# Lazy on purpose: each FastPitch+HiFiGAN pair is VRAM on a card already
# shared with IndicConformer and Ollama (see deploy/env.sh on why
# OLLAMA_KEEP_ALIVE matters here). Loading three voices at boot to serve a
# line that may only ever hear Bengali is the wrong trade; loading one the
# first time a caller actually speaks Hindi is the right one.
CKPT_ROOT = os.environ.get("TTS_CKPT_ROOT", "/workspace/tts_checkpoints")
CKPT = os.environ.get("TTS_CKPT_BN", f"{CKPT_ROOT}/bn")

_CKPT_ENV = {"bn": "TTS_CKPT_BN", "hi": "TTS_CKPT_HI", "en": "TTS_CKPT_EN"}


def _checkpoint_dir(lang: str) -> str | None:
    """-> the checkpoint directory for `lang`, or None if this pod has none.

    None is a first-class answer, not an error. A pod with only the Bengali
    voice must say so rather than silently synthesizing Hindi text with a
    Bengali phonemizer, which produces confident nonsense -- the worst
    failure mode available here, because it sounds like it worked.
    """
    explicit = os.environ.get(_CKPT_ENV.get(lang, ""), "").strip()
    if explicit:
        return explicit if os.path.isdir(explicit) else None
    guess = os.path.join(CKPT_ROOT, lang)
    return guess if os.path.isdir(guess) else None


def _build(ckpt_dir: str) -> Synthesizer:
    return Synthesizer(
        tts_checkpoint=f"{ckpt_dir}/fastpitch/best_model.pth",
        tts_config_path=f"{ckpt_dir}/fastpitch/config.json",
        tts_speakers_file=f"{ckpt_dir}/fastpitch/speakers.pth",
        vocoder_checkpoint=f"{ckpt_dir}/hifigan/best_model.pth",
        vocoder_config=f"{ckpt_dir}/hifigan/config.json",
        use_cuda=True,
    )


synthesizer = _build(CKPT)

# lang -> Synthesizer. Bengali is present from boot; the others appear here
# the first time they are asked for and found.
_SYNTHS: dict[str, Synthesizer] = {"bn": synthesizer}
_SYNTH_LOAD_LOCK = threading.Lock()


def _synth_for(lang: str) -> tuple[Synthesizer, str]:
    """-> (synthesizer, the language it ACTUALLY speaks).

    The second element is the honest part. When a Hindi request arrives on
    a pod with no Hindi voice, this returns the Bengali synthesizer AND
    says "bn", and /synthesize reports that back in a response header so
    the agent knows the caller did not hear what was asked for. Silently
    substituting a voice and reporting success is how a system ends up
    believing it is multilingual when it is not.
    """
    lang = (lang or "bn").strip().lower()
    if lang in _SYNTHS:
        return _SYNTHS[lang], lang
    ckpt = _checkpoint_dir(lang)
    if ckpt is None:
        return synthesizer, "bn"
    with _SYNTH_LOAD_LOCK:
        if lang not in _SYNTHS:            # re-checked under the lock
            logging.getLogger("tts").info("loading %s voice from %s", lang, ckpt)
            _SYNTHS[lang] = _build(ckpt)
    return _SYNTHS[lang], lang

# One GPU model instance, shared by every request. FastAPI runs a sync
# `def` endpoint (see /synthesize below) in a thread-pool, so overlapping
# calls -- a genuinely concurrent second caller, OR agent/tts.py's own
# retry firing a second request while the first is still running after a
# client-side timeout -- would otherwise run two `.tts()` passes on the
# SAME Synthesizer/CUDA context at once, and both mutate its shared
# `length_scale` attribute right before calling it (see _render() below).
# That is a real mechanism for exactly "voice sometimes jamming/cracking
# in long conversations": longer calls mean more requests and more chances
# for the GPU to be briefly busy enough to trigger a client retry while the
# original synthesis is still in flight, so the corrupt-concurrent-call
# case gets MORE likely as a conversation runs longer, not less. A single
# lock around inference serializes those calls -- request queueing costs a
# little latency under real overlap, which is strictly better than
# occasionally handing back garbled or partially-overwritten audio.
_synth_lock = threading.Lock()

SAMPLE_RATE = synthesizer.output_sample_rate or 22050
DEFAULT_SPEAKER = os.environ.get("TTS_SPEAKER", "female")

# >1 slows delivery. 1.08 was the original measurement, but real callers
# on the opening greeting -- a caller's very first impression of the whole
# system -- still reported it reading like a rushed, robotic list of words
# rather than someone actually saying hello. Raised to 1.18: still a
# measured, unscientific bump rather than a re-measurement against real
# handset audio (see the module docstring's caveat on `speed` being
# per-request precisely so this can be A/B'd properly later), but a bigger
# step than 1.08 turned out to be, in the direction the actual complaint
# points. Tune via /synthesize's `speed` override before changing this
# default further.
DEFAULT_LENGTH_SCALE = float(os.environ.get("TTS_LENGTH_SCALE", "1.18"))

# Silence inserted AFTER a chunk, by the punctuation that ended it. Nudged
# up alongside the length_scale change above -- a slower voice with the
# same short gaps between clauses/sentences still runs its breaths
# together and can end up sounding just as machine-like, only slower.
PAUSE_S = {"sentence": 0.34, "clause": 0.20, "none": 0.08}

TARGET_PEAK = 0.89          # ~-1 dBFS; loud without clipping on phone speakers
TRIM_THRESHOLD = 0.012      # below this is padding, not speech

# Bengali sentence enders (danda + Latin punctuation, since clinic data
# mixes both) and clause separators.
_RE_SENTENCE = re.compile(r"([^।?!\n]+[।?!\n]?)")
_RE_CLAUSE = re.compile(r"([^,;:]+[,;:]?)")

MAX_CHUNK_CHARS = 90        # long single clauses still get a breath


def _split_for_prosody(text: str) -> list[tuple[str, str]]:
    """-> [(chunk_text, pause_kind)]. Sentences first, then clauses inside
    any sentence that either runs long OR already carries clause
    punctuation.

    That second condition is new, and it is what actually fixes the
    greeting specifically: "নমস্কার, কলকাতা কেয়ার ডায়াগনস্টিকসে স্বাগতম।" is
    well under MAX_CHUNK_CHARS, so the old rule fed the WHOLE sentence
    through FastPitch as one uninterrupted pass -- comma and all. A real
    person says "Hello," with a small breath before continuing, and a
    single flat pass with no internal pause reads as one run-on machine
    sentence, no matter how the length_scale is tuned. Every reply
    template in reply_templates.py has this same shape (short sentences
    with a comma in them), so this is not a greeting-only fix.
    """
    out: list[tuple[str, str]] = []
    for raw_sentence in _RE_SENTENCE.findall(text):
        sentence = raw_sentence.strip()
        if not sentence:
            continue
        has_clause_punct = any(p in sentence for p in ",;:")
        if len(sentence) <= MAX_CHUNK_CHARS and not has_clause_punct:
            out.append((sentence, "sentence"))
            continue
        clauses = [c.strip() for c in _RE_CLAUSE.findall(sentence) if c.strip()]
        for i, clause in enumerate(clauses):
            out.append((clause, "sentence" if i == len(clauses) - 1 else "clause"))
    if not out:
        out = [(text.strip(), "sentence")]
    return out


def _trim_silence(wav: np.ndarray) -> np.ndarray:
    loud = np.where(np.abs(wav) > TRIM_THRESHOLD)[0]
    if loud.size == 0:
        return wav[:0]
    return wav[loud[0]:loud[-1] + 1]


def _normalize_peak(wav: np.ndarray) -> np.ndarray:
    peak = float(np.max(np.abs(wav))) if wav.size else 0.0
    return wav * (TARGET_PEAK / peak) if peak > 1e-6 else wav


def _render(text: str, speaker: str, speed: float, pauses: bool,
            synth: Synthesizer | None = None) -> np.ndarray:
    if hasattr(synthesizer.tts_model, "length_scale"):
        synthesizer.tts_model.length_scale = speed

    chunks = _split_for_prosody(text) if pauses else [(text, "sentence")]
    pieces: list[np.ndarray] = []

    for chunk_text, pause_kind in chunks:
        # split_sentences: this module already decided the chunking, and
        # letting Coqui re-split would reintroduce the ragged joins -- but
        # the vendored AI4Bharat/gokulkarthik TTS fork we're pinned to
        # (mainline coqui-tts is Python <3.11 only; see deploy notes)
        # predates that kwarg entirely and its Synthesizer.tts() doesn't
        # accept it. It also never re-splits internally, so simply not
        # passing it is equivalent, not a behavior change.
        raw = (synth or synthesizer).tts(chunk_text, speaker_name=speaker)
        wav = _trim_silence(np.asarray(raw, dtype=np.float32))
        if wav.size == 0:
            # STORY [Answer Quality and Grounding]
            # As a patient, I want to hear the whole sentence, so that I am
            # not left guessing what the agent tried to say.
            # THE HOLE, at the moment it is made.
            #
            # A chunk that renders to nothing is dropped along with the pause
            # that would have followed it, so the sentence does not thin -- it
            # stops dead. This is how "স্যাম্পল: Blood।" reached callers as
            # "স্যাম্পল:" and then silence: _split_for_prosody splits on the
            # colon, which isolates the English word in a chunk of its own,
            # every character of it is outside the Bengali vocabulary, and the
            # whole chunk lands here.
            #
            # This ran silently for the entire life of the service. The agent
            # now blocks these upstream (agent/speakability.py), but that check
            # models Latin script only, and this one fires on ANYTHING outside
            # the checkpoint's vocabulary -- so it is the broader net, and the
            # only one that sees what the tokenizer actually did rather than
            # what we predicted it would do. Anything logged here that the
            # agent did not already block is a gap in the agent's detector.
            print(f"[tts] DROPPED CHUNK -- rendered to zero samples: {chunk_text!r}",
                  flush=True)
            continue
        pieces.append(wav)
        if pauses:
            pieces.append(np.zeros(int(PAUSE_S[pause_kind] * SAMPLE_RATE), dtype=np.float32))

    if not pieces:
        return np.zeros(int(0.2 * SAMPLE_RATE), dtype=np.float32)

    joined = np.concatenate(pieces)
    # A short lead-in stops the very first phoneme being clipped by
    # playback devices that ramp up on stream start.
    return _normalize_peak(
        np.concatenate([np.zeros(int(0.04 * SAMPLE_RATE), dtype=np.float32), joined]),
    )


class SynthesizeRequest(BaseModel):
    text: str
    lang: str = "bn"
    speaker: str | None = None
    speed: float | None = None      # length_scale; >1 slower
    pauses: bool = True


def _speaker_for(lang: str) -> str:
    """Each checkpoint ships its own speaker names; the Bengali default is
    meaningless to a Hindi model and picking it would raise inside TTS."""
    return os.environ.get({"bn": "TTS_SPEAKER", "hi": "TTS_SPEAKER_HI",
                           "en": "TTS_SPEAKER_EN"}.get(lang, "TTS_SPEAKER"),
                          DEFAULT_SPEAKER if lang == "bn" else "") or DEFAULT_SPEAKER


@app.get("/health")
def health():
    return {
        "status": "ok",
        "speaker": DEFAULT_SPEAKER,
        # Which voices this pod can actually produce. "loaded" are in VRAM
        # now; "available" also counts checkpoints on disk not yet loaded.
        "languages_loaded": sorted(_SYNTHS),
        "languages_available": sorted(
            {"bn"} | {c for c in ("hi", "en") if _checkpoint_dir(c)}),
        "sample_rate": SAMPLE_RATE,
        "length_scale": DEFAULT_LENGTH_SCALE,
    }


@app.post("/synthesize")
def synthesize(req: SynthesizeRequest):
    # `lang` used to be accepted and ignored -- every request got the
    # Bengali voice regardless. It is now honoured where a voice exists.
    synth, spoken_lang = _synth_for(req.lang)

    # See _synth_lock's comment above -- this is a real GPU model shared
    # across every request FastAPI's thread-pool might run concurrently.
    with _synth_lock:
        wav = _render(
            req.text,
            req.speaker or _speaker_for(spoken_lang),
            req.speed or DEFAULT_LENGTH_SCALE,
            req.pauses,
            synth,
        )
    buf = io.BytesIO()
    sf.write(buf, wav, SAMPLE_RATE, format="WAV", subtype="PCM_16")
    buf.seek(0)
    return Response(
        content=buf.read(), media_type="audio/wav",
        # The caller asked for req.lang; this says what they GOT. Differing
        # values mean this pod has no voice for the requested language and
        # substituted the default -- see _synth_for().
        headers={"X-TTS-Lang": spoken_lang, "X-TTS-Lang-Requested": (req.lang or "bn")},
    )
