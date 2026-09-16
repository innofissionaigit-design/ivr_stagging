"""Speakerphone support: echo arbitration, barge-in, and path classification.

THE PROBLEM
-----------
This line was HALF-DUPLEX by construction. While the agent spoke, the
browser muted the microphone (`setMicMuted(true)`) and the server skipped
turn detection outright (`if session.agent_speaking: continue`). The client
told the caller so: "মাঝপথে থামানো যাবে না" -- you cannot interrupt.

That design exists for a real reason. On a speakerphone the agent's own TTS
comes straight back into the microphone. A turn detector listening during
playback would hear the agent, transcribe the agent, and answer the agent.
Muting the microphone makes that impossible.

It also makes barge-in impossible -- which is exactly what a speakerphone
caller needs most, because they are not holding a handset to their ear,
they are talking across a room at it. So the mute has to go, and something
has to take its place.

WHAT REPLACES IT
----------------
The server knows exactly what it played and roughly when. That is a
reference signal, and having one turns "is this the agent or the caller?"
from a guess into a measurement:

  * ECHO DETECTION -- correlate the microphone tail against what we just
    sent. Our own voice returning is a delayed, attenuated copy of a signal
    we still hold in memory. The caller's voice is not correlated with it.

  * ECHO SUPPRESSION -- subtract the reference's magnitude spectrum from
    the microphone's, so whatever the browser's AEC left behind is
    attenuated before ASR sees it.

  * PATH CLASSIFICATION -- how much of what we played comes back is itself
    the measurement that separates a speakerphone from a handset. A handset
    at an ear returns almost nothing; a speakerphone on a table returns a
    lot. That is Echo Return Loss, and it needs nothing from the client --
    no device labels, no browser API, no caller toggle.

LAYERS, NOT REPLACEMENT
-----------------------
The browser's own AEC (`echoCancellation: true`) stays exactly where it is
and does the heavy lifting: it holds the true render reference and runs
before the samples are ever encoded. Everything here is the SECOND line. It
assumes AEC already ran, and imperfectly, and decides what to do about the
residue. Nothing here tries to be an adaptive echo canceller.

NO GPU, NO NEW DEPENDENCIES
---------------------------
numpy only, reusing the STFT helpers already in agent/audio_quality.py.
Every decision here runs on CPU while a call is in flight, and is
unit-testable on a laptop.
"""
from __future__ import annotations

import collections
import dataclasses
import logging
import threading

import numpy as np

from agent.audio_quality import (
    _env_bool, _env_float, _istft, _stft, assess as assess_quality, rms_dbfs,
)

logger = logging.getLogger("echo_guard")

_EPS = 1e-10

# Path classifications. "unknown" until there is evidence.
PATH_HANDSET = "handset"
PATH_SPEAKERPHONE = "speakerphone"
PATH_UNKNOWN = "unknown"


@dataclasses.dataclass(frozen=True)
class EchoConfig:
    """All speakerphone tunables. NONE has been validated against a real
    speakerphone in a real room -- that is GPU/real-audio work and is
    PENDING. These are reasoned starting points."""

    # --- barge-in ---------------------------------------------------------
    barge_in_enabled: bool = True
    barge_in_target_s: float = 0.30      # budget: how quickly an interruption
                                         # must stop the agent. Not a measurement
    barge_in_poll_s: float = 0.10        # poll cadence WHILE the agent speaks.
                                         # Must be well under the target --
                                         # detection cannot beat the poll rate
    barge_in_window_s: float = 0.60      # how much recent mic audio each barge-in
                                         # check examines
    barge_in_min_speech_s: float = 0.20  # sustained speech required to interrupt.
                                         # A cough should not cut the agent off
    barge_in_min_level_dbfs: float = -45.0   # below this nobody is talking

    # --- echo detection ---------------------------------------------------
    # DOUBLE-TALK DETECTION -- the PRIMARY signal.
    #
    # Correlating waveforms against the reference turned out to be a weak
    # discriminator, and the tests are what showed it: the room low-passes
    # and delays the echo, which collapses waveform correlation to ~0.30,
    # while an unrelated speaker reaches ~0.23 over a lag search. That is no
    # margin at all. Spectrogram correlation was no better.
    #
    # Level is far more robust, and is what double-talk detectors actually
    # use. The echo can never be louder than the reference minus the room's
    # attenuation, so a microphone reading meaningfully above that ceiling
    # means a second voice is present. It is arithmetic, so it can be tested
    # exactly; correlation stays on as a secondary veto.
    double_talk_margin_db: float = 3.0   # how far above the expected echo level
                                         # the mic must sit to be a second voice.
                                         #
                                         # Was 6.0, lowered on measurement. At
                                         # 6.0 a NORMAL speaking voice could not
                                         # interrupt a loud speakerphone: at an
                                         # ERL of 8dB the caller produced only
                                         # 3.9dB of excess and was ignored. The
                                         # gap between echo-only (-4.7dB) and a
                                         # normal caller leaves room for a
                                         # smaller margin, and the speech gate
                                         # below now blocks what the larger
                                         # margin used to be protecting against.
    barge_in_min_speech_ratio: float = 0.25   # a barge-in must LOOK LIKE A VOICE.
                                         # The level test alone answers "is
                                         # something here beyond our echo?", not
                                         # "is someone talking?" -- and on a quiet
                                         # handset line ambient room noise clears
                                         # the level bar easily (measured 15.5dB
                                         # of excess, more than a normal caller
                                         # produces on a loud speakerphone). No
                                         # single dB margin separates those two,
                                         # so a second, independent question is
                                         # needed. Room noise measures 0.000 here
                                         # and speech 0.41-0.90.
    bootstrap_erl_db: float = 6.0        # ERL assumed before enough is measured.
                                         # Deliberately LOW = expect a loud echo
                                         # = demand a louder caller = fewer false
                                         # interruptions while still learning
    echo_correlation_threshold: float = 0.55   # correlation above which the tail
                                         # is our own voice. Now a VETO on top of
                                         # the level test, not the decision
    echo_max_delay_s: float = 0.50       # widest round trip the lag search covers
    echo_suppression_enabled: bool = True
    echo_oversubtraction: float = 1.4
    echo_spectral_floor: float = 0.05    # same musical-noise reasoning as
                                         # audio_quality.suppress_noise

    # --- path classification ----------------------------------------------
    speakerphone_erl_db: float = 20.0    # Echo Return Loss AT OR BELOW which the
                                         # path is a speakerphone. Small ERL =
                                         # loud echo = open speaker
    classification_min_observations: int = 2   # never classify off one sample
    estimate_min_observations: int = 1   # the ERL ESTIMATE may use a single clean
                                         # observation, unlike the path
                                         # CLASSIFICATION above which wants more.
                                         # They answer different questions: the
                                         # estimate only has to beat the
                                         # deliberately-pessimistic bootstrap, and
                                         # every poll it stays on that bootstrap is
                                         # a poll in which the caller cannot
                                         # interrupt. Classification decides which
                                         # accuracy bucket a whole call lands in
                                         # and deserves more evidence.
    erl_history: int = 200               # how many ERL observations to keep. The
                                         # room does not change during a call, so
                                         # a bounded window is as informative as
                                         # an unbounded one -- and the median over
                                         # it is recomputed on every playback poll,
                                         # so its length is a per-call CPU cost

    reference_retention_s: float = 15.0

    @classmethod
    def from_env(cls) -> "EchoConfig":
        return cls(
            barge_in_enabled=_env_bool("VOICE_AGENT_BARGE_IN", True),
            barge_in_target_s=_env_float("VOICE_AGENT_BARGE_IN_TARGET_S", 0.30),
            barge_in_poll_s=_env_float("VOICE_AGENT_BARGE_IN_POLL_S", 0.10),
            barge_in_window_s=_env_float("VOICE_AGENT_BARGE_IN_WINDOW_S", 0.60),
            barge_in_min_speech_s=_env_float("VOICE_AGENT_BARGE_IN_MIN_SPEECH_S", 0.20),
            barge_in_min_level_dbfs=_env_float("VOICE_AGENT_BARGE_IN_MIN_DBFS", -45.0),
            double_talk_margin_db=_env_float("VOICE_AGENT_DOUBLE_TALK_MARGIN_DB", 3.0),
            barge_in_min_speech_ratio=_env_float(
                "VOICE_AGENT_BARGE_IN_MIN_SPEECH_RATIO", 0.25),
            bootstrap_erl_db=_env_float("VOICE_AGENT_BOOTSTRAP_ERL_DB", 6.0),
            echo_correlation_threshold=_env_float("VOICE_AGENT_ECHO_CORR", 0.55),
            echo_max_delay_s=_env_float("VOICE_AGENT_ECHO_MAX_DELAY_S", 0.50),
            echo_suppression_enabled=_env_bool("VOICE_AGENT_ECHO_SUPPRESS", True),
            echo_oversubtraction=_env_float("VOICE_AGENT_ECHO_OVERSUB", 1.4),
            echo_spectral_floor=_env_float("VOICE_AGENT_ECHO_FLOOR", 0.05),
            speakerphone_erl_db=_env_float("VOICE_AGENT_SPEAKERPHONE_ERL_DB", 20.0),
            classification_min_observations=int(
                _env_float("VOICE_AGENT_PATH_MIN_OBS", 2)),
            estimate_min_observations=int(
                _env_float("VOICE_AGENT_ERL_MIN_OBS", 1)),
            erl_history=int(_env_float("VOICE_AGENT_ERL_HISTORY", 200)),
            reference_retention_s=_env_float("VOICE_AGENT_ECHO_RETENTION_S", 15.0),
        )


CONFIG = EchoConfig.from_env()


# ---------------------------------------------------------------------------
# signal comparisons
# ---------------------------------------------------------------------------
def normalized_correlation(a: np.ndarray, b: np.ndarray) -> float:
    """Zero-lag normalised cross-correlation, magnitude only, in [0, 1].

    Magnitude, because the acoustic path can invert phase and that says
    nothing about whose voice it is. Normalised, because the answer must be
    about SHAPE and not loudness -- the echo is far quieter than what we
    played, and a similarity measure that cared about level would call every
    echo a stranger."""
    a = np.asarray(a, dtype=np.float64).reshape(-1)
    b = np.asarray(b, dtype=np.float64).reshape(-1)
    n = min(a.size, b.size)
    if n == 0:
        return 0.0
    a, b = a[:n] - a[:n].mean(), b[:n] - b[:n].mean()
    denom = np.sqrt(float(a @ a) * float(b @ b))
    if denom <= _EPS:
        return 0.0
    return float(min(1.0, abs(float(a @ b)) / denom))


def best_lag_correlation(mic: np.ndarray, ref: np.ndarray, sr: int,
                         max_delay_s: float) -> tuple[float, float]:
    """Highest correlation over a lag search, and the lag that produced it.

    The lag search is what makes this usable in production. The echo does
    not arrive when we sent it: there is network delay to the client, the
    client's own playback buffering, acoustic flight time across the room,
    and capture buffering on the way back. That total is variable and
    unknowable, so it is measured per comparison rather than assumed.

    Returns (correlation, lag_seconds); (0.0, 0.0) when either side is too
    short to compare."""
    mic = np.asarray(mic, dtype=np.float64).reshape(-1)
    ref = np.asarray(ref, dtype=np.float64).reshape(-1)
    if mic.size == 0 or ref.size == 0:
        return 0.0, 0.0

    # Step in ~5ms hops rather than per sample. Speech correlation is broad
    # enough that a finer search buys nothing, and this runs on every
    # playback poll of every concurrent call.
    step = max(1, int(0.005 * sr))

    # A lag is only considered when it still leaves MOST of the microphone
    # window to compare against. The earlier bound was a flat 50ms, and it
    # made the search actively harmful: when the reference is no longer than
    # the microphone window, a large lag leaves a sliver of overlap, and two
    # speech signals correlate spuriously over a sliver. Measured: a caller
    # scored 0.826 against our playback on ~105ms of overlap at lag 0.495s,
    # tripping the echo veto and suppressing a genuine barge-in.
    #
    # Callers should pass a reference that EXTENDS BACK past the microphone
    # window by max_delay_s, so every lag in the search has a full-length
    # window and this bound never truncates the search.
    min_window = max(1, int(0.6 * mic.size))

    best_corr, best_lag = 0.0, 0
    for lag in range(0, int(max_delay_s * sr) + 1, step):
        window = min(mic.size, ref.size - lag)
        if window < min_window:
            break
        corr = normalized_correlation(mic[:window], ref[lag:lag + window])
        if corr > best_corr:
            best_corr, best_lag = corr, lag
    return best_corr, best_lag / float(sr)


def echo_return_loss_db(mic: np.ndarray, ref: np.ndarray) -> float:
    """How much QUIETER the returning echo is than what we played, in dB.

        ERL = level(reference) - level(microphone)

    A handset pressed to an ear leaks almost nothing back: large ERL. A
    speakerphone on a table returns a great deal: small ERL. This is the
    measurement that classifies the path.

    Returns +inf when the microphone is silent (perfect isolation) and 0.0
    when there was no reference to compare against."""
    ref_db = rms_dbfs(np.asarray(ref, dtype=np.float32))
    mic_db = rms_dbfs(np.asarray(mic, dtype=np.float32))
    if ref_db <= -119.0:
        return 0.0
    if mic_db <= -119.0:
        return float("inf")
    return float(ref_db - mic_db)


def suppress_echo(mic: np.ndarray, ref: np.ndarray, sr: int,
                  cfg: EchoConfig | None = None) -> np.ndarray:
    """Residual echo suppression in the spectral domain.

    Subtracts the reference's magnitude spectrum from the microphone's, bin
    by bin, keeping the microphone's phase. Same machinery as
    audio_quality.suppress_noise -- deliberately, since it is already
    written, tested and understood -- with one difference that matters: what
    is being subtracted is a signal we HOLD, not a statistical estimate of
    the room.

    What this is NOT: an adaptive echo canceller. It does not model the
    room's impulse response. The browser's AEC is the real canceller and
    runs first with the true render reference; this cleans up after it.

    Length- and dtype-preserving. No-op when disabled or when either side is
    too short to frame."""
    cfg = cfg or CONFIG
    mic = np.asarray(mic, dtype=np.float32).reshape(-1)
    ref = np.asarray(ref, dtype=np.float32).reshape(-1)

    frame_len, hop = 512, 128
    if not cfg.echo_suppression_enabled or mic.size < frame_len * 2 or ref.size < frame_len:
        return mic

    pad = frame_len
    padded = np.pad(mic.astype(np.float64), pad, mode="reflect")
    spec, win = _stft(padded, frame_len, hop)
    if spec.shape[0] < 2:
        return mic

    # Align the reference to the same framing. A shorter reference means
    # playback ended partway through; the remainder is genuinely echo-free,
    # so zero-padding it is a true statement rather than missing data.
    ref_padded = np.pad(ref.astype(np.float64), pad, mode="reflect")
    if ref_padded.size < padded.size:
        ref_padded = np.pad(ref_padded, (0, padded.size - ref_padded.size))
    else:
        ref_padded = ref_padded[:padded.size]
    ref_spec, _ = _stft(ref_padded, frame_len, hop)

    mag = np.abs(spec)
    ref_mag = np.abs(ref_spec[:mag.shape[0]])

    # Scale the reference to the microphone's level before subtracting. The
    # acoustic path attenuates the echo by an unknown amount, so the raw
    # reference magnitude is far too large -- subtracting it unscaled would
    # gate the caller's voice away along with the echo.
    mic_level = float(np.sqrt(np.mean(mag ** 2)) + _EPS)
    ref_level = float(np.sqrt(np.mean(ref_mag ** 2)) + _EPS)
    ref_mag = ref_mag * (mic_level / ref_level)

    gain = np.clip((mag - cfg.echo_oversubtraction * ref_mag) / np.maximum(mag, _EPS),
                   cfg.echo_spectral_floor, 1.0)
    cleaned = _istft(spec * gain, win, hop, padded.size)
    return cleaned[pad:pad + mic.size].astype(np.float32)


# ---------------------------------------------------------------------------
# what we played, and when
# ---------------------------------------------------------------------------
class PlaybackReference:
    """The agent's own recent output, placed on the CALL timeline.

    Stored against call-time seconds -- the same clock `processed_until_s`
    counts in -- so a reference slice can be looked up with the offsets the
    turn detector already works in. Old audio is pruned: a call runs for
    minutes and only the last few seconds can still be echoing.
    """

    def __init__(self, sample_rate: int = 16000, cfg: EchoConfig | None = None):
        self.sample_rate = sample_rate
        self._cfg = cfg or CONFIG
        self._segments: list[tuple[float, np.ndarray]] = []   # (start_s, samples)

        # Locked for the same reason EchoGuard's ERL history is: assess()
        # reads this from a thread-pool worker (main.py dispatches it through
        # asyncio.to_thread) while _speak() adds to it and barge_in()
        # truncates it, both on the event loop. Rebinding _segments is
        # atomic, but add() mutates in place, and a reader iterating it
        # during an append has no defined behaviour.
        self._lock = threading.Lock()

    def add(self, start_s: float, samples: np.ndarray) -> None:
        samples = np.asarray(samples, dtype=np.float32).reshape(-1)
        if samples.size:
            with self._lock:
                self._segments.append((float(start_s), samples))

    def truncate_after(self, cut_s: float) -> None:
        """Forget everything we had queued to play beyond `cut_s`.

        Called when playback is STOPPED early. Without it the reference goes
        on claiming we played the whole reply, and the level test then judges
        later windows against sound that never left the speaker: the expected
        echo ceiling stays high, and the caller cannot interrupt a second
        time. For a caller who already knows the answer -- who interrupts
        repeatedly by nature -- that is the difference between the feature
        working once and working at all."""
        with self._lock:
            trimmed: list[tuple[float, np.ndarray]] = []
            for seg_start, samples in self._segments:
                if seg_start >= cut_s:
                    continue                    # never played at all
                keep = int(max(0.0, cut_s - seg_start) * self.sample_rate)
                if keep <= 0:
                    continue
                trimmed.append((seg_start, samples[:keep]))
            self._segments = trimmed

    def prune(self, now_s: float) -> None:
        cutoff = now_s - self._cfg.reference_retention_s
        with self._lock:
            self._segments = [(t, s) for t, s in self._segments
                              if t + s.size / self.sample_rate >= cutoff]

    def slice(self, start_s: float, end_s: float) -> np.ndarray:
        """What we were playing over [start_s, end_s) on the call timeline.

        Zero-filled where nothing was playing, so the result always lines up
        sample-for-sample with a microphone slice over the same interval --
        silence in the reference is a true statement (we were making no
        sound), not missing data."""
        if end_s <= start_s:
            return np.zeros(0, dtype=np.float32)
        n = int((end_s - start_s) * self.sample_rate)
        out = np.zeros(n, dtype=np.float32)

        with self._lock:
            segments = list(self._segments)     # snapshot; the copy is shallow,
                                                # the arrays themselves are never
                                                # mutated in place
        for seg_start, samples in segments:
            if seg_start + samples.size / self.sample_rate <= start_s or seg_start >= end_s:
                continue
            dst_a = max(0, int((seg_start - start_s) * self.sample_rate))
            src_a = max(0, int((start_s - seg_start) * self.sample_rate))
            count = min(n - dst_a, samples.size - src_a)
            if count > 0:
                out[dst_a:dst_a + count] += samples[src_a:src_a + count]
        return out

    def has_audio_in(self, start_s: float, end_s: float) -> bool:
        """Overlap test only -- does NOT build the slice.

        The obvious implementation, `np.any(self.slice(...))`, allocates and
        fills a whole window (9600 float32 for 0.6s at 16kHz) to answer a
        boolean. Comparing segment bounds answers the same question in
        O(number of segments)."""
        with self._lock:
            segments = list(self._segments)
        return any(
            seg_start < end_s and seg_start + samples.size / self.sample_rate > start_s
            for seg_start, samples in segments
        )

    def __len__(self) -> int:
        with self._lock:
            return len(self._segments)


@dataclasses.dataclass(frozen=True)
class EchoVerdict:
    """One decision about one slice of microphone audio taken while the
    agent was speaking."""

    is_echo: bool
    is_barge_in: bool
    correlation: float
    lag_s: float
    erl_db: float
    level_dbfs: float
    reason: str

    def as_dict(self) -> dict:
        return {
            "is_echo": self.is_echo,
            "is_barge_in": self.is_barge_in,
            "correlation": round(self.correlation, 3),
            "lag_s": round(self.lag_s, 3),
            "erl_db": None if self.erl_db == float("inf") else round(self.erl_db, 1),
            "level_dbfs": round(self.level_dbfs, 1),
            "reason": self.reason,
        }


class EchoGuard:
    """One per call. Owns the playback reference, arbitrates barge-in, and
    accumulates the evidence that classifies the path."""

    def __init__(self, sample_rate: int = 16000, cfg: EchoConfig | None = None):
        self._cfg = cfg or CONFIG
        self.sample_rate = sample_rate
        self.reference = PlaybackReference(sample_rate, self._cfg)

        # BOUNDED, and LOCKED.
        #
        # Bounded because the median over this is recomputed on every
        # playback poll (10/s per call) and on every turn: an unbounded list
        # made that O(n log n) against an n that only grows -- measured at
        # 3.07ms per poll after 3000 observations, ~368ms/s of CPU across 12
        # concurrent calls. The room does not change mid-call, so the last
        # `erl_history` observations are as informative as all of them.
        #
        # Locked because assess() runs on a thread-pool worker (main.py
        # dispatches it through asyncio.to_thread) while reporting_path() and
        # classify() read it from the event loop. list.append is GIL-atomic so
        # nothing corrupts, but without this an estimate and a classification
        # taken for the same decision could see different histories. Both
        # TurnDetector and TurnASR already lock cross-thread state this way.
        self._lock = threading.Lock()
        self._erl_observations: collections.deque[float] = collections.deque(
            maxlen=max(1, self._cfg.erl_history))

        self.declared_path = PATH_UNKNOWN
        self.barge_in_count = 0

    def _record_erl(self, erl: float) -> None:
        with self._lock:
            self._erl_observations.append(erl)

    def _finite_observations(self) -> list[float]:
        """A snapshot, taken under the lock, so every consumer in one
        decision reasons about the same history."""
        with self._lock:
            return [e for e in self._erl_observations if np.isfinite(e)]

    # -- what the client told us -------------------------------------------
    def declare_path(self, hint: str | None) -> None:
        """A hint from the browser (track settings, or the caller's toggle).
        A STARTING point only: the acoustic measurement overrides it,
        because a hint can be absent or wrong and Echo Return Loss cannot."""
        if hint in (PATH_HANDSET, PATH_SPEAKERPHONE):
            self.declared_path = hint

    # -- the reference -----------------------------------------------------
    def note_playback(self, start_s: float, samples: np.ndarray) -> None:
        self.reference.add(start_s, samples)
        self.reference.prune(start_s)

    # -- the decision ------------------------------------------------------
    def assess(self, mic: np.ndarray, start_s: float, sr: int) -> EchoVerdict:
        """Is this microphone audio our own echo, or the caller interrupting?

        The order of checks is deliberate, and every one can only make the
        answer MORE conservative:

          1. Too quiet for anyone to be talking   -> neither
          2. We were not playing anything         -> not echo, so: caller
          3. Correlates with what we played       -> echo
          4. Too brief to be an interruption      -> neither
          5. Loud, uncorrelated, sustained        -> barge-in

        When uncertain the answer is "echo" -- i.e. do NOT interrupt the
        agent. A false barge-in cuts the agent off mid-sentence for no
        reason, which is worse than a missed one: the caller can always
        speak again, but they cannot un-hear a reply that stopped halfway.
        """
        mic = np.asarray(mic, dtype=np.float32).reshape(-1)
        level_dbfs = rms_dbfs(mic)
        end_s = start_s + mic.size / float(sr)

        # ONE reference slice, reaching BACK by a full round trip. The echo of
        # something played up to echo_max_delay_s ago is still arriving now, so
        # a lookup aligned only to the microphone window goes empty the instant
        # playback stops -- while the room is still ringing. That gap
        # classified the agent's own decaying echo as the caller and made the
        # agent interrupt itself, which is the exact loop the old half-duplex
        # gate prevented. The same widening gives the correlation search a
        # full-length window at every lag.
        ref_recent = self.reference.slice(start_s - self._cfg.echo_max_delay_s, end_s)

        # Only the samples we actually played. PlaybackReference zero-fills
        # the gaps so the window lines up with the microphone, but averaging
        # those zeros into the level would understate how loud the echo may
        # be -- and understating it is what lets echo pass as a caller.
        ref_active = ref_recent[ref_recent != 0.0]

        if ref_active.size == 0:
            # Nothing played recently enough to still be arriving, so nothing
            # here can be our echo.
            if mic.size == 0 or level_dbfs < self._cfg.barge_in_min_level_dbfs:
                return EchoVerdict(False, False, 0.0, 0.0, 0.0, level_dbfs,
                                   "below_level_floor")
            # Still has to be a VOICE. This branch is reached while the gate
            # is open but playback has already finished -- the window between
            # the last sample leaving the speaker and the client's
            # playback_done arriving. Without this check, ambient room noise
            # in that window stopped playback, opened the gate and advanced
            # the marker, and the caller then got an unprompted "sorry, it is
            # noisy" for a turn they never took.
            if assess_quality(mic, sr).speech_ratio < self._cfg.barge_in_min_speech_ratio:
                return EchoVerdict(False, False, 0.0, 0.0, 0.0, level_dbfs,
                                   "not_speech")
            return EchoVerdict(False, True, 0.0, 0.0, 0.0, level_dbfs, "no_reference")

        # Echo Return Loss for THIS window. Whether it is worth remembering
        # depends on the verdict below, so the recording happens at each
        # return rather than here -- see _verdict().
        erl = echo_return_loss_db(mic, ref_active)

        if mic.size == 0 or level_dbfs < self._cfg.barge_in_min_level_dbfs:
            # Recorded: a very quiet return is not an absence of evidence, it
            # is the strongest possible evidence of a handset. Omitting it
            # left handset calls with no observations at all and therefore
            # permanently unclassifiable.
            self._record_erl(erl)
            return EchoVerdict(False, False, 0.0, 0.0, erl, level_dbfs,
                               "below_level_floor")

        # THE PRIMARY TEST. The echo cannot be louder than what we played
        # minus the room's attenuation, so a microphone sitting meaningfully
        # above that ceiling means a second voice is in the room.
        expected_echo_dbfs = rms_dbfs(ref_active) - self.erl_estimate()
        excess_db = level_dbfs - expected_echo_dbfs

        # Correlated against the WIDENED reference, not the aligned one. It
        # reaches back a full round trip, so every lag the search tries still
        # has a full microphone-length window to compare against -- see the
        # min_window note in best_lag_correlation for what a short overlap
        # does to this number.
        if excess_db < self._cfg.double_talk_margin_db:
            self._record_erl(erl)          # echo only -- a clean measurement
            # corr/lag reported as 0.0 here, NOT measured: the lag search is
            # by far the most expensive thing in this function (2.27ms of a
            # 2.61ms poll) and this branch is the common one -- almost every
            # window during a reply is plain echo. Computing a veto before
            # discovering it is not needed cost ~273ms/s of CPU across 12
            # concurrent calls. The two fields stay in the verdict for the
            # branches that do measure them.
            return EchoVerdict(True, False, 0.0, 0.0, erl, level_dbfs,
                               "within_expected_echo_level")

        # Only now is correlation worth computing: the level test has already
        # said a second voice may be present, and this is the veto on that.
        corr, lag = best_lag_correlation(mic, ref_recent, sr,
                                         self._cfg.echo_max_delay_s)

        # SECONDARY VETO. Loud enough to be a second voice -- but if it still
        # looks like what we are playing, believe that and stay quiet rather
        # than interrupt ourselves.
        if corr >= self._cfg.echo_correlation_threshold:
            self._record_erl(erl)          # echo only -- a clean measurement
            return EchoVerdict(True, False, corr, lag, erl, level_dbfs,
                               "correlates_with_playback")

        # NOT recorded from here down. The microphone contains a second voice,
        # so this window's "ERL" is not the room's echo return loss at all --
        # it is the caller's level relative to ours. Feeding it to the median
        # drags the estimate down, which raises the expected-echo ceiling,
        # which makes the NEXT interruption harder. Measured: 39.7dB falling
        # to 3.3dB after twelve double-talk windows. The system was getting
        # deafer the more the caller talked over it -- the exact opposite of
        # what a caller who already knows the answer needs.
        if mic.size < int(self._cfg.barge_in_min_speech_s * sr):
            return EchoVerdict(False, False, corr, lag, erl, level_dbfs, "too_brief")

        # IS IT A VOICE, or just something loud?
        #
        # Everything above answers "is there more here than our own echo?".
        # That is not the same question as "is someone talking", and the
        # difference is not academic: on a quiet handset line ambient room
        # noise clears the level bar by 15.5dB -- more excess than a normal
        # caller produces on a loud speakerphone. There is therefore NO single
        # dB margin that admits the caller and rejects the noise; the grid was
        # measured and the separating band is empty. A second, independent
        # test is required rather than a better-tuned first one.
        #
        # speech_ratio is the fraction of 25ms frames sitting above the
        # window's own noise floor. Reusing it costs no new model and no new
        # dependency -- agent/audio_quality.py already computes it on CPU for
        # the quality floor.
        speech_ratio = assess_quality(mic, sr).speech_ratio
        if speech_ratio < self._cfg.barge_in_min_speech_ratio:
            return EchoVerdict(False, False, corr, lag, erl, level_dbfs,
                               "not_speech")

        return EchoVerdict(False, True, corr, lag, erl, level_dbfs, "double_talk")

    # -- classification ----------------------------------------------------
    def classify(self) -> str:
        """handset / speakerphone / unknown, from measured Echo Return Loss.

        Median rather than mean: one loud noise while the agent speaks must
        not reclassify the call. Infinities (a silent microphone) are
        dropped first -- they mean we learned nothing from that observation,
        not that isolation was perfect."""
        finite = self._finite_observations()
        if len(finite) < self._cfg.classification_min_observations:
            return self.declared_path
        median_erl = float(np.median(finite))
        return PATH_SPEAKERPHONE if median_erl <= self._cfg.speakerphone_erl_db else PATH_HANDSET

    def reporting_path(self) -> str:
        """The bucket this call's turns are filed under.

        `unknown` collapses to handset deliberately. Guessing speakerphone
        would move ordinary callers into the very bucket whose accuracy we
        are trying to measure, and contaminate it with calls that do not
        belong -- in whichever direction happens to flatter the result."""
        path = self.classify()
        return PATH_HANDSET if path == PATH_UNKNOWN else path

    def erl_estimate(self) -> float:
        """Best current guess at the room's Echo Return Loss, in dB.

        Median of what has been observed, because a few double-talk windows
        -- where the mic is loud for a reason that is not echo -- would drag
        a mean down and make the detector steadily more willing to fire.

        Falls back to bootstrap_erl_db before enough has been measured: a
        deliberately LOW value meaning "expect a loud echo", which demands a
        louder caller before interrupting. Wrong in the safe direction."""
        finite = self._finite_observations()

        # `not finite` is not redundant with the threshold below.
        # estimate_min_observations is env-settable, and at 0 the comparison
        # passes on an EMPTY history -- np.median([]) is nan, nan < margin is
        # False, so the level test silently stops rejecting anything and the
        # agent can interrupt itself on its own echo. A misconfigured env var
        # must not be able to disarm the primary safety check.
        if not finite or len(finite) < self._cfg.estimate_min_observations:
            return self._cfg.bootstrap_erl_db
        return float(np.median(finite))

    def median_erl_db(self) -> float | None:
        finite = self._finite_observations()
        return round(float(np.median(finite)), 1) if finite else None

    def snapshot(self) -> dict:
        return {
            "declared_path": self.declared_path,
            "classified_path": self.classify(),
            "reporting_path": self.reporting_path(),
            "median_erl_db": self.median_erl_db(),
            "erl_observations": len(self._erl_observations),
            "barge_in_count": self.barge_in_count,
        }


def pcm_from_wav_bytes(wav_bytes: bytes, target_sr: int = 16000) -> np.ndarray:
    """Decode a TTS reply into mono float32 at the call's sample rate.

    The reply we send is the reference we later compare the microphone
    against, so it is decoded once, here, rather than guessed at.

    Returns an empty array on any failure. That degrades echo detection to
    "no reference", which is the conservative direction: uncorrelated audio
    is then treated as the caller, exactly as it was before this module
    existed."""
    try:
        import io

        import soundfile as sf

        samples, sr = sf.read(io.BytesIO(wav_bytes), dtype="float32", always_2d=False)
        if getattr(samples, "ndim", 1) > 1:
            samples = samples.mean(axis=1)
        samples = np.asarray(samples, dtype=np.float32).reshape(-1)

        if sr != target_sr and samples.size:
            # Linear resample. The reference is used for correlation and a
            # coarse level comparison, neither of which needs a
            # phase-accurate resampler.
            n_out = max(1, int(samples.size * target_sr / float(sr)))
            samples = np.interp(
                np.linspace(0.0, samples.size - 1, n_out, dtype=np.float64),
                np.arange(samples.size, dtype=np.float64),
                samples.astype(np.float64),
            ).astype(np.float32)
        return samples
    except Exception as e:  # noqa: BLE001
        logger.warning("could not decode TTS reply as an echo reference: %s", e)
        return np.zeros(0, dtype=np.float32)
