"""Semantic cache for the intent-extraction step.

WHAT IS CACHED, AND WHAT DELIBERATELY IS NOT
--------------------------------------------
Cached: the transcript -> {intent, slots} extraction. That is a pure
function of the caller's words, and it is the slowest hop in the turn
(Ollama, ~1-3s warm; llm.py already had to raise its timeout to 90s for
the cold case).

NOT cached: the clinic lookup or the final reply. Those are ~5ms against
Postgres and they are the part that must never go stale -- a price that
changed, a doctor who cancelled, a slot that just got booked. Caching a
finished reply would trade the one guarantee this system is built around
(reply_templates.py: "the actual number in the caller's ear always comes
from the Spring Boot response") for a few milliseconds. Not worth it.

So a cache hit still does the live lookup. It just skips the LLM.

WHY TWO TIERS
-------------
L1 is an exact match on normalized text. Free, and always safe.

L2 is cosine similarity over bge-m3 embeddings, served by the Ollama
instance already running for Qwen -- no new venv, no new GPU process, no
new Python dependency (this module imports nothing that llm.py doesn't).
bge-m3 is genuinely multilingual, Bengali included, which matters: the
whole point is that "ইউরিক অ্যাসিড টেস্ট কত" and "ইউরিক এসিদ টেস্ট এ কত টাকা
পড়বে" are the same question with different ASR output, and a string
comparison can never see that.

THE PII RULE
------------
An L2 hit is a *fuzzy* hit -- similar, not identical. That is fine for
"which test did they ask about". It is dangerous for a phone number or a
patient name, where a near-miss means confidently reading back someone
else's details. So entries whose slots carry caller-specific data are
stored for L1 (exact) retrieval only and are never eligible for L2. See
`_is_l2_eligible`.
"""
from __future__ import annotations

import difflib
import json
import logging
import math
import re
import threading
import time
import urllib.error
import urllib.request

import numpy as np

logger = logging.getLogger("semantic_cache")

OLLAMA_EMBED_URL = "http://localhost:11434/api/embed"
EMBED_MODEL = "bge-m3"
# Generous: a COLD bge-m3 has to be pulled into VRAM before it answers,
# which measured well past a 10s ceiling and made every early turn of the
# process report the cache as unavailable. Kept resident by
# OLLAMA_KEEP_ALIVE=-1 and warmed at startup, so this ceiling is a
# backstop rather than the normal path.
EMBED_TIMEOUT_S = 45

# MEASURED, not guessed -- and the measurement overturned the value this
# started at. Against bge-m3 on the live pod, anchored on
# "ইউরিক অ্যাসিড টেস্টের রেট কত":
#
#   same question, reworded / ASR-garbled   0.7809 .. 0.8156
#   DIFFERENT test, same sentence frame     0.7492  ("থাইরয়েড টেস্টের রেট কত")
#   different intent entirely               0.2952 .. 0.3554
#
# Two things follow, and both matter:
#
# 1. An initial 0.90 would have hit NOTHING. Every real paraphrase sits in
#    the 0.78-0.82 band, so the cache would have reported a 0% semantic
#    hit rate forever while looking perfectly healthy.
# 2. Cosine ALONE cannot be trusted here at any threshold. The margin
#    between "same question reworded" and "different test, same phrasing"
#    is 0.03. The embedding is dominated by the sentence frame ("X
#    টেস্টের রেট কত"); the test name -- the only part that decides which
#    price the caller is quoted -- is a small share of the signal. A
#    threshold tuned to fire on real paraphrases will also fire across
#    different tests, and the failure mode is quoting a confident wrong
#    price. That is precisely the class of bug reply_templates.py exists
#    to prevent, so it does not get reintroduced here.
#
# Hence: threshold set where paraphrases actually land, AND every hit
# carrying an entity slot must clear _entity_guard() below.
DEFAULT_THRESHOLD = 0.78

# Slots naming a THING whose identity decides which record gets looked up.
# A semantic hit may not cross one of these without character-level proof.
# "department" belongs here for exactly the same reason as the other two:
# "কোন কোন ডাক্তার আছেন" and "অর্থোতে কোন ডাক্তার আছেন" are the same sentence
# frame with only the department word different, so cosine alone cannot
# tell them apart (see fast_path.py's docstring for the measured version of
# this same finding on test_name) -- omitting it here was the actual root
# cause of "doctors by department alias" intermittently returning nothing
# or the wrong department: a cache hit on a similarly-framed department
# query was never checked for whether it named the SAME department.
# ADDED BY SOURAV -- "Caller asks about a health package" story.
# "package_name" belongs here for the exact same reason "department" was
# added above it: a semantic hit on "health_package" must not cross from
# one package to a differently-named one just because the sentence frame
# rhymes (e.g. "diabetes package e ki ki ache" vs "full body package e ki
# ki ache"). Deliberately NOT added to _REQUIRED_ENTITY_FOR_INTENT below --
# unlike test_name/doctor_name/department, a MISSING package_name is not
# an incomplete extraction here (it means "list every package", a
# complete, safe-to-cache answer on its own -- see llm.py's own comment on
# why "health_package" is the one single-entity-shaped intent that does
# not re-prompt when its entity slot is empty).
_ENTITY_SLOTS = ("test_name", "doctor_name", "department", "package_name", "insurance_provider_name")

# Bengali-vs-Bengali character similarity, so ASR garble ("ইউরিক এসিদ" vs
# "ইউরিক অ্যাসিড") still matches while a genuinely different test does not.
# Both sides are always the same script here, which is what makes this
# work -- the cross-script version of this comparison is the one that can
# never succeed (see clinic-api's aliases_bn).
#
# Measured against entity "ইউরিক অ্যাসিড" on the live pod:
#
#   same test, ASR-garbled / reworded   0.696 .. 1.000
#   different test entirely             0.231 .. 0.381
#     (থাইরয়েড, ভিটামিন ডি, সুগার, লিপিড প্রোফাইল, ক্রিয়েটিনিন)
#
# 0.55 sits in the middle of that gap. Note how much wider this separation
# is (0.315) than the cosine one (0.03) on the SAME distinction: character
# overlap identifies WHICH test far more sharply than sentence embeddings
# do, which is exactly why the entity check is the authority here and
# cosine is only the recall filter that precedes it.
#
# A floor of 0.70 was tried first and rejected 0.696 -- a real ASR variant
# of the right test -- by four thousandths. Tune from measurements, not
# from round numbers.
ENTITY_MATCH_FLOOR = 0.55

# Caller-specific slots. An L2 (fuzzy) hit must never carry these across
# from a different caller's utterance.
_PII_SLOTS = ("phone", "patient_name")

# For these intents, the answer is meaningless without the named slot --
# main.py immediately re-prompts for it when missing (see its
# missing_slot_prompt() calls). Indexing THAT kind of incomplete extraction
# for L2 is unsafe in a way _entity_guard() cannot catch on its own:
# _entity_guard only rejects a hit whose CACHED entity fails to appear in
# the NEW utterance, but has nothing to check when the cached entry has no
# entity at all -- an entry with e.g. department=None passes trivially,
# so a later utterance that DOES name a department (worded similarly --
# "কোন কোন ডাক্তার আছেন" vs "অর্থোতে কোন ডাক্তার আছেন" is exactly the kind
# of frame-dominated near-duplicate fast_path.py's docstring measured)
# gets served the old, entity-less answer instead of running its own
# extraction. This was the actual mechanism behind "doctors by department
# alias sometimes returns nothing" and "asks which doctor again after the
# caller already named one": a prior turn's incomplete classification of a
# similarly-phrased utterance got reused wholesale. Simplest safe fix: an
# extraction missing its intent's defining slot never enters the L2 index
# at all, so nothing can ever be reused FROM it.
_REQUIRED_ENTITY_FOR_INTENT = {
    "test_rate": "test_name",
    "test_sample": "test_name",
    # ADDED BY SOURAV -- "Caller asks how long results take" bug fix. Same
    # missing-slot cache-poisoning guard as test_rate/test_sample above.
    "test_duration": "test_name",
    "doctor_availability": "doctor_name",
    # ADDED BY SOURAV -- "Caller asks when a doctor sits" story. Same
    # defining-slot guard as doctor_availability just above, for the same
    # reason: an extraction that classified "doctor_schedule" but missed
    # doctor_name must never enter the L2 index, or a later, differently-
    # worded "which days does he sit" for a DIFFERENT doctor could
    # fuzzy-match onto it and silently reuse the wrong (missing) slot.
    "doctor_schedule": "doctor_name",
    "doctors_by_department": "department",
    # ADDED BY SOURAV -- "Caller asks how to prepare for a test" story.
    # Same guard as test_rate/test_sample/test_duration above, and for the
    # same reason: unlike "health_package" (deliberately EXCLUDED from
    # this dict -- see the comment on _ENTITY_SLOTS above), a missing
    # test_name here has no "list every test's preparation instructions"
    # analog -- it is always an incomplete extraction that main.py
    # re-prompts for, so it must never enter the L2 index.
    "test_preparation": "test_name",
    # ADDED BY SOURAV -- Phase 1: Database Schema & Policy Tables.
    # Walk-in Eligibility / Prescription Requirements stories. Same
    # single-required-slot guard as test_rate/test_sample/test_duration/
    # test_preparation above -- neither has a "list every test's
    # walk-in/prescription policy" analog for a bare question with
    # nothing named, so a missing test_name is always an incomplete
    # extraction that must never enter the L2 index.
    "walkin_eligibility": "test_name",
    "prescription_requirements": "test_name",
    # "insurance_coverage" and "billing_balance" are deliberately NOT
    # listed here -- see _is_l2_eligible()'s own intent-exclusion list
    # below for why each is excluded from L2 entirely instead of via a
    # single required slot.
}

_RE_WS = re.compile(r"\s+")
_RE_STRIP = re.compile(r"[।?!,.‌‍]+")


def normalize_text(text: str) -> str:
    return _RE_WS.sub(" ", _RE_STRIP.sub(" ", text.strip().lower())).strip()


class EmbeddingUnavailable(Exception):
    pass


def embed(text: str, timeout_s: int = EMBED_TIMEOUT_S) -> list[float]:
    payload = json.dumps({"model": EMBED_MODEL, "input": text}).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_EMBED_URL, data=payload, headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise EmbeddingUnavailable(str(e)) from e
    vectors = body.get("embeddings") or []
    if not vectors or not vectors[0]:
        raise EmbeddingUnavailable(f"empty embedding response for {text!r}")
    return vectors[0]


def _unit(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm else vec


class SemanticCache:
    """Small, in-process, thread-safe. Brute-force scan on purpose: no
    vector store to stand up for what is at most a few thousand short
    questions, and eviction stays trivial to reason about.

    ON "MICROSECONDS" -- THE ESTIMATE THIS CLASS WAS BUILT AROUND
    ------------------------------------------------------------
    That was the original justification for scanning while holding the
    lock, and the arithmetic does not support it. At max_entries=2000 and
    bge-m3's 1024 dimensions a full pass is ~2M multiply-adds, and in pure
    Python -- `sum(a * b for a, b in zip(...))` over a list of floats -- that
    is on the order of 100-300ms, not microseconds. Three to four orders of
    magnitude out.

    That mattered because the scan ran INSIDE self._lock, alongside
    _entity_guard's difflib passes, which are also not cheap. So every
    concurrent caller queued behind every other caller's scan, on a lock
    taken once per turn. The cache built to save the slowest hop in the turn
    was quietly adding a serialization point in front of it.

    Two changes fix it without changing any cached-value semantics:

      * vectors are kept as one contiguous float32 matrix, so the scan is a
        single numpy matmul in C (~1-2ms at this size) instead of a Python
        loop;
      * the scan and the entity guard run OUTSIDE the lock. The lock is now
        only ever held for O(1) dict work and an O(n) matrix rebuild that
        happens at most once per mutation.

    The matrix is rebuilt lazily, flagged by _matrix_dirty, because
    _vectors is already rewritten wholesale on every put and drop.
    """

    def __init__(self, threshold: float = DEFAULT_THRESHOLD,
                 max_entries: int = 2000, ttl_s: float = 6 * 3600):
        self.threshold = threshold
        self.max_entries = max_entries
        self.ttl_s = ttl_s
        self._lock = threading.Lock()
        self._exact: dict[str, dict] = {}          # normalized text -> entry
        self._vectors: list[tuple[list[float], str]] = []   # (unit vec, normalized text)
        # Contiguous float32 mirror of _vectors, so the similarity scan is one
        # numpy matmul instead of a Python loop. Rebuilt lazily on the first
        # get() after any mutation; _matrix_dirty is set by every writer.
        self._matrix: np.ndarray | None = None
        self._matrix_keys: list[str] = []
        self._matrix_dirty = False
        # Vectors computed by a get() that missed, held so the put() that
        # follows doesn't pay for the same embedding twice. Keyed by text,
        # NOT a single slot: concurrent calls interleave get/put freely, and
        # a one-slot stash would hand one call's vector to another call's
        # entry -- indexing an utterance under the wrong meaning.
        self._pending: dict[str, list[float]] = {}
        self.stats = {"l1_hits": 0, "l2_hits": 0, "misses": 0, "embed_failures": 0}

    # -- internals -------------------------------------------------------

    def _expired(self, entry: dict) -> bool:
        return (time.time() - entry["stored_at"]) > self.ttl_s

    def _evict_locked(self):
        while len(self._exact) > self.max_entries:
            oldest = min(self._exact, key=lambda k: self._exact[k]["last_used"])
            self._drop_locked(oldest)

    def _drop_locked(self, key: str):
        self._exact.pop(key, None)
        self._vectors = [(v, k) for v, k in self._vectors if k != key]
        self._matrix_dirty = True

    def _rebuild_matrix_locked(self):
        """Mirror _vectors into one contiguous array. Callers hold the lock.

        Rebuilt wholesale rather than patched incrementally: _vectors is
        already rewritten wholesale by every writer, numpy has no cheap
        in-place row delete, and at 2000x1024 float32 this is ~8MB of C-level
        copying -- a few milliseconds, paid at most once per mutation.
        """
        if self._vectors:
            self._matrix = np.asarray(
                [v for v, _ in self._vectors], dtype=np.float32,
            )
            self._matrix_keys = [k for _, k in self._vectors]
        else:
            self._matrix = None
            self._matrix_keys = []
        self._matrix_dirty = False

    @staticmethod
    def _is_l2_eligible(value: dict) -> bool:
        # ADDED BY SOURAV -- "Caller asks two questions in one breath"
        # story. A multi-question turn's `value["intents"]` array has more
        # than one entry -- a fuzzy (L2) hit matches on OVERALL wording
        # similarity, not on "the caller asked exactly these N questions
        # in exactly this order," so reusing one multi-intent extraction
        # for a differently-phrased-but-similar-sounding later turn risks
        # silently answering the wrong SET of questions, or in the wrong
        # order, for that caller. Purely additive and backward-safe: a
        # single-intent `value` (everything before this story, and every
        # ordinary one-question turn after it) has no "intents" key at
        # all, so `.get("intents") or []` is `[]`, `len([]) == 0`, and
        # this check falls through unchanged for those. L1 (exact-match)
        # caching is untouched by this -- only fuzzy reuse is excluded.
        if len((value or {}).get("intents") or []) > 1:
            return False

        slots = (value or {}).get("slots") or {}
        if any(slots.get(field) for field in _PII_SLOTS):
            return False

        # story title: A multi-part question is answered in full
        # user story: As a caller who asked two things, I want both answered,
        #   so that I do not have to ask again.
        # acceptance criteria: Every answerable part of a turn is answered in
        #   the order asked, and any part that cannot be answered is
        #   explicitly addressed rather than dropped. Completeness is scored
        #   on a labelled multi-part set.
        #
        # A multi-part extraction is L1 (exact) only. An L2 hit is already a
        # fuzzy judgement about what one question meant; reusing it for an
        # utterance that happens to rhyme means betting the same way twice in
        # a row, on a sentence whose SHAPE -- two requests joined by "আর" --
        # is exactly the frame-dominated similarity the measurements at the
        # top of this file warn about. Every guard below reasons about a
        # single intent and a single entity and has nothing to say about the
        # second part, so the honest move is to keep it out of the index
        # rather than to extend three guards to cover a case none of them was
        # measured on.
        #
        # The cost is small and known: multi-part turns are the rare ones, so
        # this gives up cache value where there was least of it.
        parts = (value or {}).get("parts")
        if isinstance(parts, list) and len(parts) > 1:
            return False

        intent = (value or {}).get("intent")

        # ADDED BY SOURAV -- "Lab Report Status & Secure Delivery" combined
        # story. report_status/report_send resolve identity by PHONE
        # (already an excluded _PII_SLOTS field above whenever the caller
        # states it in the same utterance), but a caller can also just say
        # "is my report ready" with no phone at all -- main_pcm.py's new
        # "phone" pending state asks for it on a LATER turn, outside this
        # extraction entirely. That means an L2 (fuzzy) hit on THIS turn's
        # bare "is my report ready" utterance could reuse a cached
        # extraction that carries no phone, is fine on its own, but a fuzzy
        # match is "similar wording", not "same caller, same report" -- and
        # unlike a test's price (the same fact for every caller who asks),
        # report status is caller-specific data behind an identity check.
        # Excluded entirely, same treatment as book_appointment just below,
        # rather than trying to reason about which report_status/report_send
        # phrasing IS safe to fuzzy-match.
        # ADDED BY SOURAV -- Phase 1: Database Schema & Policy Tables.
        # Outstanding Balance / Billing story. "do I have any pending
        # dues" is caller-specific identity-bound data, same as
        # report_status/report_send just above (not a fact that's the
        # same for every caller who asks, unlike a test's price) -- a
        # bare "amar kono bill baki ache" with no phone stated yet is
        # exactly as unsafe to fuzzy-reuse across callers as "is my
        # report ready" is.
        if intent in ("report_status", "report_send", "billing_balance"):
            return False

        # book_appointment carries up to three entity-shaped slots at once
        # (doctor_name, date, time_slot), any subset of which can be
        # missing on a given turn -- unlike the single-entity intents
        # below, there is no one required field whose presence makes the
        # rest of the extraction trustworthy to reuse. fast_path.py already
        # refuses to handle this intent at all for the same reason (see its
        # docstring); the semantic cache defers to the LLM here too.
        #
        # ADDED BY SOURAV -- Phase 1: Insurance Coverage Policy story.
        # "insurance_coverage" has the same shape problem: it needs BOTH
        # test_name and insurance_provider_name, either of which can be
        # missing on a given turn, so there is no single required field
        # the way test_rate/test_preparation/etc. have one. Same
        # treatment as book_appointment, for the same reason.
        #
        # ADDED BY SOURAV -- "Caller asks the agent to compare two
        # options" story. "compare_options" has the identical two-
        # required-slots shape (compare_option_a / compare_option_b),
        # either of which can be missing on a given turn -- same
        # treatment as book_appointment/insurance_coverage above, for the
        # same reason. (_ENTITY_SLOTS below is untouched by this story on
        # purpose: _entity_guard() only ever runs on an L2 hit -- see its
        # own call site in get() -- so an intent excluded from L2 entirely
        # never needs entries added there; they would be dead code.)
        if intent in ("book_appointment", "insurance_coverage", "compare_options"):
            return False

        required = _REQUIRED_ENTITY_FOR_INTENT.get(intent)
        if required and not slots.get(required):
            # An extraction that couldn't even fill the ONE slot that
            # decides what the caller is asking about is not a safe thing
            # to hand to a future, differently-worded caller just because
            # the sentence frame rhymes -- see the comment on
            # _REQUIRED_ENTITY_FOR_INTENT for the exact bug this prevents.
            return False

        return True

    @staticmethod
    def _entity_guard(value: dict, text: str) -> bool:
        """Does the new utterance actually mention the entity the cached
        answer is about? Cosine says "same kind of question"; this says
        "same test / same doctor", which is the part that decides which
        row gets read out. An entry with no entity slot (smalltalk,
        unclear, an unfilled slot) has no fact to get wrong and passes."""
        slots = (value or {}).get("slots") or {}
        entities = [str(slots[f]) for f in _ENTITY_SLOTS if slots.get(f)]
        if not entities:
            return True

        words = text.split()
        for entity in entities:
            span = len(entity.split())
            best = 0.0
            # Compare the entity against every window of the utterance near
            # its own length -- the entity is a phrase inside a sentence,
            # so a whole-string ratio would be diluted by the surrounding
            # words and reject valid matches.
            for width in {max(1, span - 1), span, span + 1}:
                for i in range(max(1, len(words) - width + 1)):
                    window = " ".join(words[i:i + width])
                    best = max(best, difflib.SequenceMatcher(None, entity, window).ratio())
            if best < ENTITY_MATCH_FLOOR:
                logger.info("semantic hit rejected: entity %r not in %r (best %.2f)",
                            entity, text, best)
                return False
        return True

    # -- public ----------------------------------------------------------

    def get(self, text: str) -> tuple[dict | None, str]:
        """Returns (value, how) where how is one of exact | semantic | miss.
        Never raises: if the embedding service is down the cache silently
        degrades to L1-only rather than taking the call down with it."""
        key = normalize_text(text)
        if not key:
            return None, "miss"

        with self._lock:
            entry = self._exact.get(key)
            if entry and not self._expired(entry):
                entry["last_used"] = time.time()
                self.stats["l1_hits"] += 1
                return entry["value"], "exact"
            if entry:
                self._drop_locked(key)

        try:
            probe = _unit(embed(text))
        except EmbeddingUnavailable as e:
            with self._lock:
                self.stats["embed_failures"] += 1
            logger.warning("embedding unavailable, L1-only this turn: %s", e)
            with self._lock:
                self.stats["misses"] += 1
            return None, "miss"

        # SNAPSHOT under the lock, SCAN outside it. Taking the matrix is an
        # O(1) reference grab: writers REPLACE these attributes rather than
        # mutating in place (see _rebuild_matrix_locked), so the array read
        # below stays valid even if another thread swaps in a new one
        # meanwhile. Worst case this turn scores against a snapshot that is
        # one entry stale, which costs a cache miss -- never a wrong answer.
        with self._lock:
            if self._matrix_dirty:
                self._rebuild_matrix_locked()
            matrix, keys = self._matrix, self._matrix_keys

        best_score, best_key = 0.0, None
        if matrix is not None and keys:
            # Both sides are unit vectors (_unit() on store and on probe), so
            # this dot product IS cosine similarity -- nothing to normalize.
            scores = matrix @ np.asarray(probe, dtype=np.float32)
            idx = int(np.argmax(scores))
            best_score, best_key = float(scores[idx]), keys[idx]

        if best_key is not None and best_score >= self.threshold:
            with self._lock:
                entry = self._exact.get(best_key)
                if entry and self._expired(entry):
                    self._drop_locked(best_key)
                    entry = None
                candidate = entry["value"] if entry else None

            # _entity_guard runs difflib over every window of the utterance,
            # and it is the second slowest thing that used to hold the lock.
            # It only reads its two arguments, so it is safe out here.
            if candidate is not None and self._entity_guard(candidate, text):
                with self._lock:
                    # Re-check: the entry could have been evicted while the
                    # guard ran. Gone means treat this as a miss.
                    still = self._exact.get(best_key)
                    if still is not None:
                        still["last_used"] = time.time()
                        self.stats["l2_hits"] += 1
                        logger.info("semantic cache hit (%.3f): %r ~= %r",
                                    best_score, key, best_key)
                        return still["value"], "semantic"

        with self._lock:
            self.stats["misses"] += 1
            # Stash the vector we just paid for, so put() doesn't re-embed.
            # Bounded: a get() whose put() never arrives (LLM failed, caller
            # hung up) must not pin memory forever.
            if len(self._pending) > 64:
                self._pending.clear()
            self._pending[key] = probe
        return None, "miss"

    def put(self, text: str, value: dict):
        key = normalize_text(text)
        if not key or not value:
            return

        with self._lock:
            vector = self._pending.pop(key, None)
        if vector is None and self._is_l2_eligible(value):
            try:
                vector = _unit(embed(text))
            except EmbeddingUnavailable:
                vector = None  # L1-only entry; still worth storing

        with self._lock:
            self._exact[key] = {"value": value, "stored_at": time.time(), "last_used": time.time()}
            self._vectors = [(v, k) for v, k in self._vectors if k != key]
            if vector is not None and self._is_l2_eligible(value):
                self._vectors.append((vector, key))
            # Set unconditionally: _evict_locked only marks it dirty when it
            # actually drops something, and the common put changes _vectors
            # without evicting anything at all.
            self._matrix_dirty = True
            self._evict_locked()

    def snapshot(self) -> dict:
        with self._lock:
            total = sum((self.stats["l1_hits"], self.stats["l2_hits"], self.stats["misses"]))
            return {
                **self.stats,
                "entries": len(self._exact),
                "l2_indexed": len(self._vectors),
                "hit_rate": round((self.stats["l1_hits"] + self.stats["l2_hits"]) / total, 3) if total else 0.0,
            }
