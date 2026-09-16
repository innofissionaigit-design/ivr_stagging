"""The retry ladder, and noisy-bucket accounting kept apart from overall.

TWO THINGS LIVE HERE
--------------------
1. TurnFailureTracker -- per call. Counts CONSECUTIVE failed turns and says
   what to do about the next one: ask again, or offer the keypad. One
   object per CallSession, because the ladder is per caller: the person in
   the market who has now failed twice needs the keypad, and the person on
   the next line who failed once an hour ago does not.

2. QualityMetrics -- process-wide. Counts turns, split by bucket, so the
   noisy population can be reported on its own.

WHY THE BUCKETS ARE NEVER MERGED
--------------------------------
A single "accuracy" number over all traffic is dominated by whichever
bucket is larger, and in practice that is the quiet one. A system that
works perfectly for quiet callers and fails half the time for noisy ones
reports as "95% accurate" if 90% of callers are quiet -- and the number
goes UP if quiet traffic grows, with nothing having improved for the
people who were already struggling. That is precisely the population this
whole change exists for, so their number is kept separate and reported
separately. There is deliberately no combined accuracy field that hides
them; `overall_accuracy` is reported alongside the split, never instead
of it.

WHAT "SUCCESSFUL" MEANS HERE
----------------------------
A turn is successful when the pipeline produced usable text the agent
could act on: the clip passed the quality floor AND ASR returned non-empty
text. It is NOT a claim about whether the transcript was CORRECT -- word
error rate needs reference transcripts and a GPU, and measuring it is
PENDING. This counter measures "did the turn complete without falling
into the clarification path", which is what the ladder above keys off.
"""
from __future__ import annotations

import dataclasses
import threading

from agent.audio_quality import CONFIG, AudioQuality, QualityConfig

# What the tracker tells the caller to do next.
ACTION_CLARIFY = "clarify"
ACTION_KEYPAD = "keypad"


class TurnFailureTracker:
    """One per call. Consecutive failures only -- a single good turn wipes
    the slate.

    That reset is the important half. Failures are counted to detect a
    caller who is CURRENTLY not getting through, not to build a permanent
    record against them. Someone who fails once, succeeds, then fails again
    ten turns later is having an ordinary conversation with two bad
    moments; escalating them to the keypad on that second isolated failure
    would be punishing them for the length of the call.
    """

    def __init__(self, cfg: QualityConfig | None = None):
        self._cfg = cfg or CONFIG
        self.consecutive_failures = 0
        self.total_failures = 0
        self.keypad_offered = False

    @property
    def max_retries(self) -> int:
        return self._cfg.max_clarify_retries

    def record_failure(self) -> str:
        """A turn the agent could not act on. Returns the action to take:
        ACTION_CLARIFY for the first failure, ACTION_KEYPAD once
        max_clarify_retries consecutive failures have accumulated.

        Stays on ACTION_KEYPAD for any further consecutive failures rather
        than cycling back to another question -- a caller who has failed
        three times is not helped by being asked a fourth."""
        self.consecutive_failures += 1
        self.total_failures += 1
        if self.consecutive_failures >= self.max_retries:
            self.keypad_offered = True
            return ACTION_KEYPAD
        return ACTION_CLARIFY

    def record_success(self) -> None:
        """A turn the agent acted on. Resets the streak AND the keypad
        latch: the caller is being understood again, so the next isolated
        failure should get an ordinary clarification, not the keypad."""
        self.consecutive_failures = 0
        self.keypad_offered = False

    def snapshot(self) -> dict:
        return {
            "consecutive_failures": self.consecutive_failures,
            "total_failures": self.total_failures,
            "keypad_offered": self.keypad_offered,
            "max_retries": self.max_retries,
        }


@dataclasses.dataclass
class _Counters:
    turns: int = 0
    successes: int = 0
    snr_sum: float = 0.0


class QualityMetrics:
    """Process-wide turn accounting, split by audio-quality bucket.

    Locked, unlike the other counters in this codebase. `_active_calls` in
    main.py can be a bare int because every read/modify pair around it is
    free of awaits on one event loop; these are incremented from
    _dispatch_turn, which awaits repeatedly, and the recording call sits
    after those awaits. A lock is cheaper than reasoning about that for
    every future edit.
    """

    def __init__(self, cfg: QualityConfig | None = None):
        self._cfg = cfg or CONFIG
        self._lock = threading.Lock()
        self._noisy = _Counters()
        self._clean = _Counters()
        # A SECOND, INDEPENDENT dimension. Every turn is filed in exactly one
        # noise bucket AND one path bucket; the two are never crossed.
        # Crossing them would give four buckets that each fill four times
        # more slowly, and the question being asked -- "does the speakerphone
        # path recognise worse?" -- is about the path regardless of the room.
        self._handset = _Counters()
        self._speakerphone = _Counters()
        self.rejected_low_quality = 0
        self.clarifications = 0
        self.keypad_offers = 0
        self.keypad_entries = 0
        self.barge_ins = 0

    # -- recording ---------------------------------------------------------
    def record_turn(self, quality: AudioQuality, success: bool,
                    path: str | None = None) -> None:
        """One completed turn. `quality` decides the noise bucket, `path`
        ("handset" / "speakerphone") the path bucket, and `success` whether
        it counts toward the accuracy of both.

        `path` is optional so every existing call site keeps working: a turn
        recorded without one still lands in its noise bucket and simply
        contributes nothing to the handset-vs-speakerphone comparison."""
        with self._lock:
            bucket = self._noisy if quality.noisy else self._clean
            bucket.turns += 1
            bucket.snr_sum += quality.snr_db
            if success:
                bucket.successes += 1
            if not quality.usable:
                self.rejected_low_quality += 1

            path_bucket = {"handset": self._handset,
                           "speakerphone": self._speakerphone}.get(path)
            if path_bucket is not None:
                path_bucket.turns += 1
                path_bucket.snr_sum += quality.snr_db
                if success:
                    path_bucket.successes += 1

    def record_barge_in(self) -> None:
        """The caller talked over the agent and playback was stopped."""
        with self._lock:
            self.barge_ins += 1

    def record_clarification(self) -> None:
        with self._lock:
            self.clarifications += 1

    def record_keypad_offer(self) -> None:
        with self._lock:
            self.keypad_offers += 1

    def record_keypad_entry(self) -> None:
        """The caller actually used the keypad -- the fallback worked, as
        opposed to merely having been offered."""
        with self._lock:
            self.keypad_entries += 1

    # -- reporting ---------------------------------------------------------
    @staticmethod
    def _accuracy(c: _Counters) -> float | None:
        """None, not 0.0, for an empty bucket. Zero would read as "we tried
        and failed every time"; None reads as "no data yet", which is the
        truth before any noisy caller has rung."""
        return round(c.successes / c.turns, 4) if c.turns else None

    @staticmethod
    def _mean_snr(c: _Counters) -> float | None:
        return round(c.snr_sum / c.turns, 2) if c.turns else None

    def snapshot(self) -> dict:
        with self._lock:
            noisy, clean = self._noisy, self._clean
            overall_turns = noisy.turns + clean.turns
            overall_successes = noisy.successes + clean.successes
            return {
                "overall_turns": overall_turns,
                "overall_successful_turns": overall_successes,
                # Reported alongside the split, never in place of it -- see
                # the module docstring.
                "overall_accuracy": (round(overall_successes / overall_turns, 4)
                                     if overall_turns else None),
                "noisy_bucket": {
                    "turns": noisy.turns,
                    "successful_turns": noisy.successes,
                    "accuracy": self._accuracy(noisy),
                    "mean_snr_db": self._mean_snr(noisy),
                },
                "clean_bucket": {
                    "turns": clean.turns,
                    "successful_turns": clean.successes,
                    "accuracy": self._accuracy(clean),
                    "mean_snr_db": self._mean_snr(clean),
                },
                # SPEAKERPHONE vs HANDSET -- the second dimension. Reported
                # as its own block with an explicit gap, because the whole
                # point of the story is the DIFFERENCE between the two paths.
                # A gap of None means at least one bucket is still empty; it
                # is never rendered as 0.0, which would read as "no gap
                # measured" when the truth is "not measured yet".
                "path_buckets": {
                    "handset": {
                        "turns": self._handset.turns,
                        "successful_turns": self._handset.successes,
                        "accuracy": self._accuracy(self._handset),
                        "mean_snr_db": self._mean_snr(self._handset),
                    },
                    "speakerphone": {
                        "turns": self._speakerphone.turns,
                        "successful_turns": self._speakerphone.successes,
                        "accuracy": self._accuracy(self._speakerphone),
                        "mean_snr_db": self._mean_snr(self._speakerphone),
                    },
                    "accuracy_gap": self._accuracy_gap(),
                },
                "barge_ins": self.barge_ins,
                "rejected_low_quality": self.rejected_low_quality,
                "clarifications": self.clarifications,
                "keypad_offers": self.keypad_offers,
                "keypad_entries": self.keypad_entries,
                "thresholds": {
                    "min_snr_db": self._cfg.min_snr_db,
                    "noisy_snr_db": self._cfg.noisy_snr_db,
                    "min_speech_ratio": self._cfg.min_speech_ratio,
                    "max_clipping_ratio": self._cfg.max_clipping_ratio,
                    "target_rms_dbfs": self._cfg.target_rms_dbfs,
                    "max_clarify_retries": self._cfg.max_clarify_retries,
                },
                # Accuracy here means "the turn completed without falling
                # into the clarification path". Transcript correctness (WER)
                # against reference Bengali audio is PENDING GPU validation.
                "accuracy_definition": "turn_completed_with_usable_text",
            }

    def _accuracy_gap(self) -> float | None:
        """handset_accuracy - speakerphone_accuracy.

        Positive means the speakerphone path is WORSE, which is the expected
        direction and the number the acceptance criterion asks to be stated.
        None whenever either bucket is empty -- a gap computed against a
        bucket with no turns in it is not a small gap, it is no measurement."""
        h = self._accuracy(self._handset)
        s = self._accuracy(self._speakerphone)
        if h is None or s is None:
            return None
        return round(h - s, 4)

    def reset(self) -> None:
        """Tests only -- the process-wide instance is never reset in service."""
        with self._lock:
            self._noisy = _Counters()
            self._clean = _Counters()
            self._handset = _Counters()
            self._speakerphone = _Counters()
            self.rejected_low_quality = 0
            self.clarifications = 0
            self.keypad_offers = 0
            self.keypad_entries = 0
            self.barge_ins = 0


# The one instance main.py imports. A module-level singleton for the same
# reason the caches are: these numbers describe the PROCESS, and a
# per-request object would have nothing to accumulate into.
METRICS = QualityMetrics()
