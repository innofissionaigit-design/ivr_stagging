"""Answer the common turn without waking the 7B model.

WHY THIS EXISTS
---------------
Measuring the semantic cache produced a finding that reaches further than
caching. Against bge-m3 on real Bengali clinic questions:

    same question, reworded / ASR-garbled     cosine 0.78 - 0.82
    DIFFERENT test, same sentence frame       cosine 0.7492
    same entity, character-level comparison   0.696 - 1.000
    different entity, character-level          0.231 - 0.381

The embedding separates "which test" by 0.03. Character overlap separates
it by 0.32 -- an order of magnitude better, because the embedding is
dominated by the sentence frame ("X টেস্টের রেট কত") while the part that
decides which price a patient is quoted is a handful of characters.

Follow that through and the conclusion is not "tune the cache". It is
that for a fixed 74-row catalogue, identifying the entity was never a
language-modelling problem. A caller asking "ইউরিক অ্যাসিড টেস্টের রেট কত"
needs two things recognised: an intent drawn from a set of four, and one
row out of 74. Both are decidable locally, in microseconds, with a wider
correctness margin than the 7B model's output was ever checked against.

So the LLM is demoted to what it is genuinely needed for: utterances this
module is NOT confident about.

WHAT IT DELIBERATELY REFUSES
----------------------------
`book_appointment` always goes to the LLM. It needs a date, a time, a
patient name and a phone number pulled out of free speech -- open-ended
extraction with caller-specific data in it, which is exactly the case
where a pattern-matcher's failure mode is silent and wrong. The fast path
handles the questions with one entity and no PII, and hands over anything
else. Abstaining is a first-class result here, not a failure.
"""

from __future__ import annotations

import datetime
import difflib
import logging
import re
import unicodedata

from agent.slot_parse import _bn_bounded

logger = logging.getLogger("fast_path")

# Same floor as semantic_cache.ENTITY_MATCH_FLOOR, and for the same
# measured reason -- 0.696 was a real ASR variant of the right test, so a
# floor of 0.70 would have rejected it by four thousandths.
ENTITY_MATCH_FLOOR = 0.55

# A second, higher bar for committing WITHOUT the model. The cache could
# afford 0.55 because a wrong hit there still ran a live lookup against a
# name the LLM had produced; here nothing downstream re-checks the entity,
# so the margin has to carry the whole decision. 0.72 sits above every
# measured different-entity score (max 0.381) by a wide margin while
# staying under the worst same-entity score (0.696) -- anything between
# the two abstains to the LLM rather than guessing.
COMMIT_FLOOR = 0.72

# story title: Near matches are offered rather than guessed or refused
# user story: As a caller naming something loosely, I want the close matches
#   offered, so that I am not told my test does not exist when it does.
# acceptance criteria: When several catalogue rows fall within the match band
#   the agent offers up to three by name and asks which. Candidates are
#   generated across every supported language and romanised spelling. The
#   did-you-mean path covers the ambiguous case and not only total failure.
#
# How far clear of the SECOND-best ROW the best one has to be before this
# path commits without the model.
#
# Without it, COMMIT_FLOOR asks "is the best good enough" and never "is the
# second one just as good" -- so a caller saying only "ভিটামিন" was committed
# to whichever of Vitamin D and Vitamin B12 the catalogue listed first. That
# commit is invisible downstream: it writes a CANONICAL name into the slot,
# which reaches clinic-api as an exact string and resolves to exactly one
# row, so the ambiguity never gets there and no later check can catch it.
# Abstaining hands the turn to the LLM, and clinic-api's own band then offers
# the two names properly.
#
# MEASURED, against the seeded catalogue, unlike most floors in this file.
# Every one of the 34 tests, asked by its own first alias inside a full
# sentence, commits correctly with a margin of at least 0.158 (the tightest
# are HbA1c and HBsAg at 0.158, whose aliases share most of their
# characters). The known-ambiguous partial "ভিটামিন" produces 0.087. 0.12
# sits between the two with headroom on both sides.
#
# DELIBERATELY NOT the same number as clinic-api/match_band.py's MARGIN
# (0.08), and the divergence is the point rather than drift: that module
# scores in tiers -- 1.0 exact, 0.90 containment -- so its margin has to stay
# BELOW the 0.10 gap between those two tiers or an exact alias would be
# treated as tying with a mere containment. This one scores raw difflib
# ratios on a continuum and needs more room. Same question, two scales.
COMMIT_MARGIN = 0.12

_RATE_CUES = ("রেট", "দাম", "খরচ", "চার্জ", "মূল্য", "কত টাকা", "প্রাইস", "টাকা লাগে")
# ADDED BY SOURAV -- real production bug fix (see test_duration_reply()'s
# docstring in agent/reply_templates.py for the full writeup: a caller
# asking "how long does it take to get the urine test report" was being
# answered with the test's PRICE instead). These three verbs are
# genuinely ambiguous in colloquial Bengali -- "কত লাগবে"/"কত পড়বে"/"কত
# নেবে" can mean either "how much will it COST" or "how much/long will it
# TAKE (time)"; which one a caller means depends on the noun nearby, not
# the verb itself. Used to live in _RATE_CUES above, which is exactly why
# the production bug reproduced at this layer too (see resolve() below):
# a bare "ইউরিন টেস্টের রিপোর্ট পেতে কত লাগবে" (a genuine duration
# question, no explicit time word) was being fast-pathed straight to
# test_rate, bypassing agent/llm.py's SYSTEM_PROMPT entirely -- even
# though the LLM, once agent/llm.py's test_rate/test_duration intents
# were fixed, would classify it correctly. Utterances with an EXPLICIT
# time word ("কতদিন লাগবে", "কত সময় লাগবে") were never affected -- no
# substring match, since a word sits between "কত" and "লাগবে"/"সময়".
# Kept separate from the unambiguous money-word cues above so resolve()
# can tell an unambiguous price question ("রেট কত", "কত টাকা লাগবে") from
# one of these three verbs alone next to a duration signal (see
# _DURATION_SIGNAL_CUES below) -- only the latter now abstains.
_AMBIGUOUS_RATE_CUES = ("কত পড়বে", "কত লাগবে", "কত নেবে")
# Words that signal the caller is asking about the REPORT/RESULT, not the
# test's price -- present in "রিপোর্ট পেতে কত লাগবে" ("how long to get the
# report") even though "কত...লাগবে" also appears verbatim in a genuine
# price question ("টেস্ট করাতে কত লাগবে"). See resolve() below: this only
# ever matters together with an _AMBIGUOUS_RATE_CUES hit and NO
# unambiguous _RATE_CUES hit -- "রিপোর্ট এর জন্য কত টাকা লাগবে" still
# resolves as test_rate, since "কত টাকা" is unambiguous on its own.
_DURATION_SIGNAL_CUES = ("রিপোর্ট", "ফলাফল", "রেজাল্ট")
_AVAIL_CUES = ("কখন", "বসবেন", "বসেন", "চেম্বার", "আছেন", "থাকবেন", "পাওয়া যাবে", "ভিজিট")
# ADDED BY SOURAV -- "Caller asks when a doctor sits" story. "কবে" (when/
# which day), "সময়সূচি" and "শিডিউল" (schedule) used to live in
# _AVAIL_CUES above, which meant "ডাক্তার সেন কবে বসেন" ("when/which days
# does Dr Sen sit" -- this story's OWN canonical phrasing) was already
# being confidently served by resolve() below as "doctor_availability"
# with date=None, which main.py then silently defaults to TODAY (see that
# branch's own comment on why a bare presence question like "আছেন" alone
# correctly defaults to today -- "কবে" is a different, day-level "when"
# question, not a presence check, and was miscategorized here before this
# story's "doctor_schedule" intent existed to answer it correctly).
# Fixed by pulling the genuinely schedule-signaling words into their own
# set and abstaining outright whenever one fires (see resolve() below) --
# fast_path has no doctor_schedule handling of its own (a deliberate,
# flagged scope decision, not an oversight -- see this story's test
# report), so the safe move is to defer to the LLM, which DOES now know
# the difference, rather than silently keep guessing the wrong intent.
_SCHEDULE_CUES = ("কবে", "সময়সূচি", "শিডিউল")
_BOOK_CUES = ("বুক", "বুকিং", "অ্যাপয়েন্টমেন্ট", "অ্যাপয়েনমেন্ট", "সিরিয়াল", "নাম লেখা", "স্লট")
_GREETING_CUES = ("নমস্কার", "নমষ্কার", "হ্যালো", "হ্যালো?", "শুভ সকাল", "আসসালামু")
_THANKS_CUES = ("ধন্যবাদ", "থ্যাঙ্ক", "থ্যাংক")

# Relative day words the fast path is willing to resolve itself. Anything
# else with a date in it (weekday names, "১৫ তারিখে", explicit dates) goes
# to the LLM, which already has date-resolution rules and today's date.
_RELATIVE_DAYS = {"আজ": 0, "আজকে": 0, "কাল": 1, "আগামীকাল": 1, "কালকে": 1, "পরশু": 2}

# Words that make an utterance more than a simple lookup: a comparison, a
# list request, a negation, a follow-up. Cheap insurance -- if any appear,
# abstain rather than answer half the question.
_COMPLEXITY_CUES = (
    "সব",
    "সবগুলো",
    "তালিকা",
    "কোন কোন",
    "আর",
    "এবং",
    "না",
    "নাকি",
    "বদলে",
    "চেয়ে",
    "ছাড়া",
    "কিন্তু",
    "অন্য",
)

_RE_WS = re.compile(r"\s+")
_RE_PUNCT = re.compile(r"[।?!,.;:'\"()\-]+")


def _normalize(text: str) -> str:
    """NFC first: Bengali conjuncts and vowel signs have multiple valid
    encodings, and two visually identical strings compare unequal if one
    is composed and the other is not. ASR output and seeded aliases come
    from different sources, so this is a live risk, not a theoretical one.
    """
    text = unicodedata.normalize("NFC", text)
    return _RE_WS.sub(" ", _RE_PUNCT.sub(" ", text)).strip().lower()


def _best_window_ratio(needle: str, haystack_words: list[str]) -> float:
    """Highest similarity between `needle` and any word-window of the
    utterance near its own length. A whole-string ratio would be diluted
    by the surrounding sentence and would reject valid matches."""
    span = len(needle.split())
    best = 0.0
    for width in {max(1, span - 1), span, span + 1}:
        for i in range(max(1, len(haystack_words) - width + 1)):
            window = " ".join(haystack_words[i : i + width])
            best = max(best, difflib.SequenceMatcher(None, needle, window).ratio())
    return best


class Catalogue:
    """The 74 rows, with every spoken form that maps to each."""

    def __init__(self, payload: dict):
        self.tests: list[tuple[str, list[str]]] = []
        self.doctors: list[tuple[str, list[str]]] = []

        for t in payload.get("tests", []):
            forms = [_normalize(a) for a in t.get("aliases_bn", [])]
            forms.append(_normalize(t["name"]))
            self.tests.append((t["name"], [f for f in forms if f]))

        for d in payload.get("doctors", []):
            forms = [_normalize(a) for a in d.get("aliases_bn", [])]
            forms.append(_normalize(d.get("surname") or d["name"].split()[-1]))
            self.doctors.append((d["name"], [f for f in forms if f]))

    def __len__(self) -> int:
        return len(self.tests) + len(self.doctors)

    # story title: Near matches are offered rather than guessed or refused
    # user story: As a caller naming something loosely, I want the close
    #   matches offered, so that I am not told my test does not exist when it
    #   does.
    # acceptance criteria: When several catalogue rows fall within the match
    #   band the agent offers up to three by name and asks which. Candidates
    #   are generated across every supported language and romanised spelling.
    #   The did-you-mean path covers the ambiguous case and not only total
    #   failure.
    #
    # The runner-up is returned now, and the reason it has to be is easy to
    # miss: clinic-api learned to detect ambiguity, and that does NOT cover
    # this path. When the fast path commits, it writes a CANONICAL name into
    # the slot, which reaches clinic-api as an exact string and resolves to
    # exactly one row. The ambiguity never gets there. A wrong fast-path
    # commit is invisible to every check downstream of it, so the abstention
    # has to happen here or not at all.
    def match(self, text: str, kind: str) -> tuple[str | None, str | None, float, float]:
        """-> (canonical_name, matched_spoken_form, best_score, runner_up).

        runner_up is the best score belonging to a DIFFERENT row -- not the
        second-best form of the same row, which is meaningless (a test with
        four aliases would look ambiguous with itself).
        """
        words = _normalize(text).split()
        rows = self.tests if kind == "test" else self.doctors
        best_name, best_form, best_score = None, None, 0.0
        runner_up = 0.0
        for name, forms in rows:
            row_best = 0.0
            row_form = None
            for form in forms:
                score = _best_window_ratio(form, words)
                if score > row_best:
                    row_best, row_form = score, form
            if row_best > best_score:
                runner_up = best_score
                best_name, best_form, best_score = name, row_form, row_best
            elif row_best > runner_up:
                runner_up = row_best
        return best_name, best_form, best_score, runner_up


class FastPathResult:
    __slots__ = ("intent", "slots", "direct_reply_bn", "confidence", "matched_form")

    def __init__(self, intent, slots, confidence, matched_form=None, direct_reply_bn=None):
        self.intent = intent
        self.slots = slots
        self.confidence = confidence
        self.matched_form = matched_form
        self.direct_reply_bn = direct_reply_bn

    def as_llm_shape(self) -> dict:
        """Same dict shape agent/llm.py returns, so callers cannot tell
        which path produced it and no downstream code needs a branch.

        story title: A multi-part question is answered in full
        user story: As a caller who asked two things, I want both answered,
            so that I do not have to ask again.
        acceptance criteria: Every answerable part of a turn is answered in
            the order asked, and any part that cannot be answered is
            explicitly addressed rather than dropped. Completeness is scored
            on a labelled multi-part set.

        `parts` is always a single element here, and that is correct rather
        than a limitation: this path ABSTAINS on anything multi-part already
        -- both cue sets firing, or a conjunction in _COMPLEXITY_CUES -- so
        a result that reaches this method is by construction one request.
        Emitting the field anyway keeps the two producers' shapes identical,
        which is the whole promise of this method.
        """
        return {
            "intent": self.intent,
            "slots": self.slots,
            "parts": [{"intent": self.intent, "slots": self.slots}],
            "direct_reply_bn": self.direct_reply_bn,
        }


def _empty_slots(**kw) -> dict:
    slots = {
        "test_name": None,
        "doctor_name": None,
        "date": None,
        "time_slot": None,
        "patient_name": None,
        "phone": None,
    }
    slots.update(kw)
    return slots


def _any_cue(text: str, cues) -> bool:
    """Substring match. Correct for the INTENT cues, which need to survive
    Bengali inflection -- "রেট" has to fire on "রেটটা", "রেটের", "রেটটি"."""
    return any(cue in text for cue in cues)


def _any_cue_word(text: str, cues) -> bool:
    """Whole-word match, for cues where a substring hit would be a false
    positive.

    A real one this caught: the complexity guard rejected
    "ডাক্তার সেন কবে চেম্বারে বসবেন" -- a textbook availability question --
    because "বসবেন" (will sit) contains "সব" (all) as a substring. Bengali
    writes without internal word boundaries, so short function words like
    সব / আর / না appear inside longer unrelated words constantly. Every
    cue in _COMPLEXITY_CUES is a standalone word, so matching them as
    whole words is both correct and strictly safer.
    """
    words = set(text.split())
    return any((cue in words) if " " not in cue else (cue in text) for cue in cues)


class FastPath:
    def __init__(self, catalogue: Catalogue, today: datetime.date | None = None):
        self.catalogue = catalogue
        self._today = today
        self.stats = {"served": 0, "abstained": 0}

    def _resolve_date(self, text: str) -> tuple[str | None, bool]:
        """-> (iso_date_or_None, is_confident). Not confident means the
        utterance contains date-ish language this module will not try to
        parse, so the whole turn must go to the LLM."""
        today = self._today or datetime.date.today()
        # story title: The model never originates a fact
        # user story: As a clinical lead, I want every price, date and identifier
        #   to come from a verified system response, so that a wrong answer is a
        #   data bug rather than a model bug.
        # acceptance criteria: Every factual sentence is a template substitution
        #   from a validated tool response and the model is never shown a figure
        #   it could restate. An automated assertion on every commit proves no
        #   model-composed span reaches synthesis on a factual intent.
        #
        # This had the same substring bug slot_parse.parse_date did -- "সকাল"
        # (morning) contains "কাল" (tomorrow) -- and it was WORSE here, because
        # this module returns is_confident=True and the turn never reaches the
        # LLM at all: "সকাল দশটায় ডাক্তার সেন আছেন?" was answered, confidently,
        # about tomorrow. Same Bengali-aware boundary, shared from slot_parse so
        # the two cannot drift apart again.
        for word in sorted(_RELATIVE_DAYS, key=len, reverse=True):
            if _bn_bounded(word, text):
                return (today + datetime.timedelta(days=_RELATIVE_DAYS[word])).isoformat(), True
        # Any digit or weekday name means a date we are not handling here.
        if re.search(r"\d", text) or any(
            d in text for d in ("সোম", "মঙ্গল", "বুধ", "বৃহস্পতি", "শুক্র", "শনি", "রবি", "তারিখ")
        ):
            return None, False
        return None, True

    def resolve(self, transcript: str) -> FastPathResult | None:
        """Returns None whenever it is not confident. None is the normal,
        expected outcome for anything non-routine -- the caller falls back
        to the semantic cache and then the LLM."""
        text = _normalize(transcript)
        if not text:
            self.stats["abstained"] += 1
            return None

        # Booking is never handled here: open-ended extraction with PII in
        # it. Check first, before any cue that might also appear in it.
        if _any_cue(text, _BOOK_CUES):
            self.stats["abstained"] += 1
            return None

        if _any_cue_word(text, _COMPLEXITY_CUES):
            self.stats["abstained"] += 1
            return None

        # ADDED BY SOURAV -- "Caller asks when a doctor sits" story. See
        # _SCHEDULE_CUES' own comment above for why this must be checked,
        # and abstained on, BEFORE wants_avail below: without this, "কবে"
        # (a _SCHEDULE_CUES word) would otherwise still reach the
        # wants_avail branch and be confidently served as
        # "doctor_availability" defaulting to today -- silently answering
        # a different question than the caller actually asked. No
        # doctor_schedule fast-path exists (module docstring: "abstaining
        # is a first-class result"), so this always defers to the LLM,
        # which does know the difference (see agent/llm.py's SYSTEM_PROMPT).
        if _any_cue(text, _SCHEDULE_CUES):
            self.stats["abstained"] += 1
            return None

        has_unambiguous_rate = _any_cue(text, _RATE_CUES)
        has_ambiguous_rate = _any_cue(text, _AMBIGUOUS_RATE_CUES)
        wants_rate = has_unambiguous_rate or has_ambiguous_rate
        wants_avail = _any_cue(text, _AVAIL_CUES)

        # ADDED BY SOURAV -- real production bug fix. See
        # _AMBIGUOUS_RATE_CUES' own comment above for the full story: when
        # the ONLY rate signal present is one of the ambiguous verbs (no
        # unambiguous money word also present) AND a duration-signal word
        # is also present, this is a "how long does it take" question, not
        # a price question -- abstain so the LLM's test_duration intent
        # (agent/llm.py) can answer it, rather than confidently serving
        # test_rate and repeating the exact bug a caller reported live.
        # Mirrors _SCHEDULE_CUES' abstain pattern just above.
        if has_ambiguous_rate and not has_unambiguous_rate and _any_cue(text, _DURATION_SIGNAL_CUES):
            self.stats["abstained"] += 1
            return None

        # Both cue sets firing means an utterance asking about more than
        # one thing. Let the model decide which.
        if wants_rate and wants_avail:
            self.stats["abstained"] += 1
            return None

        if wants_rate:
            name, form, score, runner_up = self.catalogue.match(text, "test")
            if name and score >= COMMIT_FLOOR and (score - runner_up) >= COMMIT_MARGIN:
                self.stats["served"] += 1
                logger.info("fast path: test_rate %r (%.2f) from %r", name, score, transcript)
                return FastPathResult(
                    "test_rate", _empty_slots(test_name=form or name), score, matched_form=form
                )
            self.stats["abstained"] += 1
            return None

        if wants_avail:
            # ADDED BY CHAKRAVARDHAN -- real production bug fix: the name is
            # looked for only among words that are not the question itself.
            # "সেন" scores 0.86 against the verb "বসেন" (sits) and 0.75
            # against "বসবেন", so "ডাক্তার ঠাকুর কবে বসেন" -- a doctor the
            # clinic does not have -- was answered with Dr. Sen's schedule.
            # No doctor's spoken form contains a cue. The COMMIT_MARGIN
            # check right below is kept from staging_merged unchanged --
            # dropping it would only trade this bug for the near-tie
            # mismatch it exists to guard against.
            name_text = " ".join(w for w in text.split() if not _any_cue(w, _AVAIL_CUES))
            name, form, score, runner_up = self.catalogue.match(name_text, "doctor")
            if not (name and score >= COMMIT_FLOOR and (score - runner_up) >= COMMIT_MARGIN):
                self.stats["abstained"] += 1
                return None
            date_iso, confident = self._resolve_date(text)
            if not confident:
                self.stats["abstained"] += 1
                return None
            self.stats["served"] += 1
            logger.info(
                "fast path: doctor_availability %r (%.2f) date=%s from %r", name, score, date_iso, transcript
            )
            return FastPathResult(
                "doctor_availability",
                _empty_slots(doctor_name=form or name, date=date_iso),
                score,
                matched_form=form,
            )

        # Pure greeting or thanks, with no entity and no question in it.
        if _any_cue(text, _GREETING_CUES) and len(text.split()) <= 4:
            self.stats["served"] += 1
            return FastPathResult(
                "smalltalk", _empty_slots(), 1.0, direct_reply_bn="নমস্কার, কী সাহায্য করতে পারি?"
            )
        if _any_cue(text, _THANKS_CUES) and len(text.split()) <= 4:
            self.stats["served"] += 1
            return FastPathResult("smalltalk", _empty_slots(), 1.0, direct_reply_bn="ধন্যবাদ। আর কিছু জানতে চান?")

        self.stats["abstained"] += 1
        return None

    def snapshot(self) -> dict:
        total = self.stats["served"] + self.stats["abstained"]
        return {
            **self.stats,
            "catalogue_rows": len(self.catalogue),
            "serve_rate": round(self.stats["served"] / total, 3) if total else 0.0,
        }
