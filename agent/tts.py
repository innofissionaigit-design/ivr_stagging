"""Bengali TTS via AI4Bharat's FastPitch + HiFi-GAN (Indic-TTS).

Confirmed real and Bengali-capable: github.com/AI4Bharat/Indic-TTS ships
monolingual FastPitch+HiFi-GAN-V1 checkpoints for 13 Indian languages
including Bengali, hosted on the Bhashini platform, with a `synthesize`
inference module. Run its own server process (see README.md "Deploying
Indic-TTS") and point TTS_URL at it -- this client does not embed the
model itself, the same way tools_client.py does not embed the clinic DB.

This is the one piece of the pipeline with NO precedent in voice-to-rx-repo
-- that system never talks back, so nothing here has been battle-tested on
this account the way ASR/LLM have. Treat the fallback path below as load-
bearing, not decorative: a diagnostics line that goes silent when TTS is
down is worse than one that plays a stiff canned apology.

Two things happen here before a single byte is synthesized:

1. bn_normalize.verbalize() spells every number into Bengali words. This
   is not optional polish -- the tokenizer drops Latin digits outright, so
   without it every price is silence. See that module's docstring for the
   measurement.
2. An exact-text WAV cache. Synthesis is a pure function of the text, and
   reply_templates.py deliberately produces a SMALL set of sentences, so
   the hit rate on greetings, apologies and repeat questions is high. This
   is the cheapest latency win in the whole pipeline.
"""
from __future__ import annotations

import asyncio
import collections
import hashlib
import logging
import os
import threading

import httpx

from agent import language as _lang_mod
from agent import speakability
from agent.reply_templates import UNSPEAKABLE_ESCALATION

# NOTE: dev_chakravardhan's own draft of this module imported
# unspeakable_spans/verbalize from agent.bn_normalize directly, to compute
# the speakable text itself. That is the same job agent/speakability.py's
# check() already does below (SPEAKABILITY_ENFORCE shadow mode, stats,
# UnspeakableReply) -- so the bn_normalize import is dropped as redundant
# rather than kept as a second, unused path to the same result.

logger = logging.getLogger("tts")

TTS_URL = os.environ.get("TTS_URL", "http://localhost:8002/synthesize")
TTS_TIMEOUT_S = float(os.environ.get("TTS_TIMEOUT_S", "30"))

# Caps how many synthesis requests this process will have in flight at once.
#
# tts_server.py holds a single global _synth_lock around inference (one GPU
# model, and _render() mutates its shared length_scale immediately before
# calling it), so the server does exactly ONE synthesis at a time no matter
# how many arrive. Requests beyond that do not get served faster by being
# sent -- they just sit on that lock holding a connection open at both ends.
#
# 4 keeps the server's queue primed so it is never idle between clips, while
# stopping a peak-hour burst from opening a connection per caller against a
# service that can only ever work on one of them.
TTS_CONCURRENCY = int(os.environ.get("TTS_CONCURRENCY", "4"))

# Process-wide, not per-client: the ceiling belongs to the TTS SERVER, and
# it does not care how many client objects this process happens to hold.
_tts_gate = asyncio.Semaphore(TTS_CONCURRENCY)

# STORY [Answer Quality and Grounding]
# As a patient, I want to hear the whole sentence, so that I am
# not left guessing what the agent tried to say.
# Whether an unspeakable span BLOCKS synthesis or is only counted.
#
# Off for the first deployment window on purpose. The spoken-form tables were
# just extended to cover every sample type and department in the seeded
# catalogue, but a live catalogue can hold rows nobody has reviewed, and the
# blocked path costs a caller their answer. Shadow mode makes the residual
# countable -- watch /api/stats' unspeakable_blocked against real traffic --
# before any caller is diverted by it.
#
# The reverse order is the one to avoid: switching enforcement on first would
# have escalated 8 of 8 department listings and 7 of 34 rate replies, which is
# a silent bug traded for a loud outage.
SPEAKABILITY_ENFORCE = os.environ.get("SPEAKABILITY_ENFORCE", "0") == "1"


# STORY [Answer Quality and Grounding]
# As a patient, I want to hear the whole sentence, so that I am
# not left guessing what the agent tried to say.
class UnspeakableReply(Exception):
    """Raised instead of synthesizing a sentence with a known hole in it.

    Distinct from a transport failure on purpose. main.py's _speak() catches
    Exception broadly and answers with the "system busy" clip, which is the
    right answer when the vocoder is down and the WRONG one here -- this is a
    content defect on our side, not an outage, and the caller should be sent
    to the counter rather than asked to hold. Callers must catch this one
    FIRST; see _speak().
    """

    def __init__(self, dropped: tuple[str, ...] | list[str], text: str):
        self.dropped = tuple(dropped)
        self.text = text
        super().__init__(f"unspeakable spans would be dropped: {self.dropped}")

FALLBACK_DIR = os.path.join(os.path.dirname(__file__), "..", "static", "fallback_audio")

# Pre-recorded once (see README.md "Recording the fallback set") and
# committed alongside the code, NOT generated at runtime -- if the live TTS
# service is what's broken, asking it to synthesize its own apology is
# exactly the failure this exists to route around.
FALLBACK_FILES = {
    "asr_empty": "sorry_repeat.wav",         # "দুঃখিত, শুনতে পাইনি, আবার বলুন"
    "llm_failure": "system_busy.wav",         # "একটু সমস্যা হচ্ছে, একটু ধরুন"
    "tool_failure": "check_failed.wav",       # "এখনই দেখতে পারছি না, স্টাফের কাছে দিচ্ছি"
    "tts_failure": "system_busy.wav",         # reused -- see note below
}

# Spoken when the process is already at its concurrent-call ceiling and is
# turning a caller away (see VOICE_AGENT_MAX_CALLS in main.py / main_pcm.py).
# Defined here, next to the other fixed lines, so both transports say the
# same words and it gets prewarmed like the rest -- a rejection that has to
# wait on the vocoder would consume the very capacity it is protecting.
BUSY_LINE = "দুঃখিত, এই মুহূর্তে সব লাইন ব্যস্ত। একটু পরে আবার ফোন করুন।"

# Sentences the agent says on fixed paths, synthesized once at startup so
# the caller never waits on the vocoder for them. The greeting especially:
# it is the first thing on every single call.
PREWARM_LINES = [
    "নমস্কার, কলকাতা কেয়ার ডায়াগনস্টিকসে স্বাগতম। কীভাবে সাহায্য করতে পারি?",
    BUSY_LINE,
    "দুঃখিত, শুনতে পাইনি। আবার বলবেন?",
    "দুঃখিত, বুঝতে পারিনি। আবার একটু বলবেন?",
    "একটু সমস্যা হচ্ছে, একটু ধরুন।",
    "কোন টেস্টের রেট জানতে চান, একটু বলবেন?",
    "কোন ডাক্তারের কথা জিজ্ঞেস করছেন?",
    "এই মুহূর্তে দেখতে পারছি না। কাউন্টারে যোগাযোগ করুন, দয়া করে।",
    "লাইনে কোনো সাড়া পাচ্ছি না, কল শেষ করছি। ধন্যবাদ।",
    # STORY [Answer Quality and Grounding]
    # As a patient, I want to hear the whole sentence, so that I am
    # not left guessing what the agent tried to say.
    # The line spoken when a reply is blocked. Prewarmed like the rest, but
    # for a second reason too: it is the one sentence that must never itself
    # be blocked, so having it in this list means startup exercises it.
    UNSPEAKABLE_ESCALATION,
]

# CallState.speech_rate -> tts_server's `speed` (which is FastPitch's
# length_scale; >1 is slower). tts_server defaults to 1.18 because 1.0 was
# measured as rushed for a service line; these sit above it.
#
# REASONED, not measured -- nobody has listened to a slow clip on a handset
# and confirmed it reads as considerate rather than sluggish. Same discipline
# as the confidence floors: a reasoned number is a to-do, not a setting.
SPEECH_RATE_SCALE = {
    "default": None,        # let tts_server use its own DEFAULT_LENGTH_SCALE
    "slow_normal": 1.26,
    "slow": 1.35,
}

# ~400 short clips at 22kHz mono. Bounded so a long-running process can't
# grow without limit on unique test names.
AUDIO_CACHE_MAX = 400


class TTSClient:
    def __init__(self, base_url: str = TTS_URL, timeout_s: float = TTS_TIMEOUT_S):
        # Sized to _tts_gate, not to httpx's default of 100. The gate already
        # caps in-flight requests at TTS_CONCURRENCY, and tts_server.py can
        # only synthesize one at a time behind its _synth_lock, so a larger
        # pool would only hold sockets open against work that cannot start.
        # The +2 is headroom for a connection being retried or torn down.
        self._client = httpx.AsyncClient(
            timeout=timeout_s,
            limits=httpx.Limits(
                max_connections=TTS_CONCURRENCY + 2,
                max_keepalive_connections=TTS_CONCURRENCY,
            ),
        )
        self.base_url = base_url
        self._audio_cache: collections.OrderedDict[str, bytes] = collections.OrderedDict()
        self._cache_lock = threading.Lock()
        # STORY [Answer Quality and Grounding]
        # As a patient, I want to hear the whole sentence, so that I am
        # not left guessing what the agent tried to say.
        # unspeakable_blocked counts REPLIES the gate caught, whether or not
        # enforcement was on -- it is the shadow-mode measurement, and the
        # number an alert rule would watch once there is somewhere to send
        # one. See /api/stats.
        self.stats = {"hits": 0, "misses": 0, "unspeakable_blocked": 0}

    async def aclose(self):
        await self._client.aclose()

    @staticmethod
    def _key(text: str, speech_rate: str = "default") -> str:
        # speech_rate is part of the key, not just the text. Synthesis is a
        # pure function of BOTH, and keying on text alone would hand a caller
        # who needs the slow voice whatever speed happened to be rendered
        # first -- silently defeating the whole speech_rate path.
        return hashlib.sha256(f"{speech_rate}|{text}".encode("utf-8")).hexdigest()

    def _cache_get(self, key: str) -> bytes | None:
        with self._cache_lock:
            wav = self._audio_cache.get(key)
            if wav is not None:
                self._audio_cache.move_to_end(key)
                self.stats["hits"] += 1
            return wav

    def _cache_put(self, key: str, wav: bytes):
        with self._cache_lock:
            self._audio_cache[key] = wav
            self._audio_cache.move_to_end(key)
            while len(self._audio_cache) > AUDIO_CACHE_MAX:
                self._audio_cache.popitem(last=False)

    async def synthesize(self, text_bn: str, speech_rate: str = "default",
                         language: str | None = None) -> bytes:
        """Returns WAV bytes, or raises. Callers should catch and fall back
        to `fallback_audio()` -- see main.py's _speak().

        speech_rate comes from CallState (Blueprint 4.5 / Appendix C). Until a
        detector sets caller_state or senior it is always "default", which
        sends no override and leaves tts_server on its own tuned value -- so
        this path is a no-op until Call Intelligence exists.

        language likewise comes from CallState and is likewise always None
        today. It is threaded through now because the speakability rule is
        only valid for a Bengali matrix -- see agent/speakability.py -- and
        because it is now also part of the cache key (see below).
        """
        # STORY [Answer Quality and Grounding]
        # As a patient, I want to hear the whole sentence, so that I am
        # not left guessing what the agent tried to say.
        # THE SINGLE CHOKE POINT for the speakability rule.
        #
        # Every reply reaches the tokenizer through here, including paths that
        # never touch main.py's _speak() -- prewarm() today, and whatever is
        # written next. So the count and the ERROR line live here rather than
        # in the orchestrator: one place to instrument, and no way to add a
        # code path that quietly skips it. _speak() only decides what the
        # caller HEARS instead, which is the part only it can do.
        verdict = speakability.check(text_bn, language=language)
        spoken = verdict.spoken

        if verdict.is_blocked:
            # Counted even when enforcement is off -- that is what makes
            # shadow mode a measurement rather than a log line.
            self.stats["unspeakable_blocked"] += 1
            # ERROR, not WARNING. This used to be a warning, which sits below
            # the default threshold of every alerting rule anyone would write,
            # and the gap analysis's verdict on it was "a log nobody is reading
            # during a call". Stable event name so a rule can match on it.
            logger.error("unspeakable_reply dropped=%s text_len=%d enforced=%s",
                         list(verdict.dropped), len(text_bn), SPEAKABILITY_ENFORCE)
            if SPEAKABILITY_ENFORCE:
                raise UnspeakableReply(verdict.dropped, text_bn)

        # ADDED BY CHAKRAVARDHAN -- THE LANGUAGE IS PART OF THE CACHE KEY,
        # not just the text (speech_rate already was, via _key()'s own
        # signature).
        #
        # Two languages routinely produce byte-identical spoken strings --
        # a bare time ("18:15"), a confirmation id, a digit sequence read
        # out one numeral at a time. Keyed on text alone, the first
        # caller's Bengali audio would be replayed to the next caller in
        # Hindi, and it would sound like a working system speaking the
        # wrong language rather than like a bug.
        key = self._key(f"{_lang_mod.resolve(language)}::{spoken}", speech_rate)
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        self.stats["misses"] += 1

        # WHAT IS AND IS NOT WORTH RETRYING
        # ---------------------------------
        # This used to retry on bare `Exception`, which meant it retried
        # TIMEOUTS -- and a timeout here is the one failure where a retry is
        # actively harmful.
        #
        # A timeout does not mean the request was lost. tts_server.py
        # serializes every synthesis behind one lock, so a slow response
        # means the server is BUSY and our request is still sitting in that
        # queue, holding its place. Retrying does not replace it; it appends
        # a SECOND request for the same text behind the first. Under load
        # that doubles the queue depth at exactly the moment the queue is
        # what is already hurting, which makes the next caller more likely to
        # time out, which makes them retry too. tts_server.py's own comment
        # on _synth_lock describes this feedback loop and notes it gets more
        # likely the longer a conversation runs.
        #
        # A ConnectError is different: nothing was queued, because nothing
        # was accepted. That is worth one retry -- it is what a restarting
        # TTS process looks like.
        #
        # Everything else (timeouts, 4xx/5xx) propagates immediately to
        # main.py's _speak(), which falls back to the pre-recorded clip. A
        # stiff canned apology now beats correct audio after the caller has
        # hung up.
        max_attempts = 2
        for attempt in range(1, max_attempts + 1):
            try:
                # Gate only the network call. Cache hits returned above are
                # never gated -- they are the fast path and cost nothing.
                #
                # ADDED BY CHAKRAVARDHAN -- "lang" is resolved to the actual
                # caller language rather than hardcoded "bn": the hardcoded
                # value spoke every reply in Bengali regardless of what
                # language was actually detected. "speed" (SPEECH_RATE_SCALE)
                # is kept from the pre-existing speech_rate story below it --
                # dropping it would silently lose that feature.
                async with _tts_gate:
                    r = await self._client.post(
                        self.base_url,
                        json={"text": spoken, "lang": _lang_mod.resolve(language),
                              "speed": SPEECH_RATE_SCALE.get(speech_rate)},
                    )
                r.raise_for_status()
                wav = r.content
                self._cache_put(key, wav)
                return wav
            except httpx.ConnectError as e:
                if attempt == max_attempts:
                    raise
                logger.warning("TTS connect failed (attempt %d/%d), retrying: %s",
                               attempt, max_attempts, e)
                await asyncio.sleep(0.5)

    # STORY [Answer Quality and Grounding]
    # As a patient, I want to hear the whole sentence, so that I am
    # not left guessing what the agent tried to say.
    @staticmethod
    def assert_canned_lines_speakable() -> None:
        """Every sentence this file can say without asking anyone must be
        sayable. Raises AssertionError at startup if one is not.

        Checked independently of SPEAKABILITY_ENFORCE, because a canned line
        with a hole in it is a code defect in both modes -- shadow mode exists
        to measure unknown CATALOGUE data, not to excuse a literal in the
        source. And it is checked before the first caller rather than
        discovered by them.

        UNSPEAKABLE_ESCALATION is in this set, which is also the recursion
        guard: _speak() falls back to that line when a reply is blocked, and
        this is what guarantees the fallback cannot be blocked in turn.
        """
        bad = {line: speakability.check(line).dropped
               for line in PREWARM_LINES if speakability.check(line).is_blocked}
        if bad:
            raise AssertionError(
                f"canned lines contain spans the synthesizer would drop: {bad}")

    async def prewarm(self):
        """Best-effort: a failure here must not stop the app from starting.
        Worst case the first caller pays normal synthesis latency.

        "Best-effort" covers TRANSPORT failures -- the vocoder being down or
        slow. It does not cover UnspeakableReply, which says a sentence in
        this file cannot be spoken; that is a defect to fix, not a condition
        to degrade around, so it propagates.
        """
        for line in PREWARM_LINES:
            try:
                await self.synthesize(line)
            # STORY [Answer Quality and Grounding]
            # As a patient, I want to hear the whole sentence, so that I am
            # not left guessing what the agent tried to say.
            except UnspeakableReply:
                raise
            except Exception as e:  # noqa: BLE001 - prewarm is advisory only
                logger.warning("prewarm failed for %r: %s", line[:32], e)
                return
        logger.info("TTS prewarm complete (%d lines cached)", len(self._audio_cache))

    def snapshot(self) -> dict:
        total = self.stats["hits"] + self.stats["misses"]
        return {
            **self.stats,
            "cached_clips": len(self._audio_cache),
            "hit_rate": round(self.stats["hits"] / total, 3) if total else 0.0,
        }

    @staticmethod
    def fallback_audio(reason: str) -> bytes:
        """reason in FALLBACK_FILES. Reads from disk every call (small
        files, infrequent path) rather than caching, so a corrected
        recording takes effect without a restart."""
        filename = FALLBACK_FILES.get(reason, FALLBACK_FILES["llm_failure"])
        path = os.path.join(FALLBACK_DIR, filename)
        try:
            with open(path, "rb") as f:
                return f.read()
        except FileNotFoundError:
            logger.error(
                "Fallback audio %s missing -- call will go silent on this "
                "failure path. Record it: see README.md.", path,
            )
            return b""
