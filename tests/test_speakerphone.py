"""Tests for speakerphone support: echo arbitration, barge-in, path buckets.

WHAT THESE COVER
----------------
Everything in the story that can be settled on a laptop: the correlation
and ERL maths, the echo-vs-barge-in decision table, the fact that a
barge-in does NOT discard the caller's interruption, path classification,
and the handset/speakerphone accuracy gap arithmetic.

WHAT THEY DELIBERATELY DO NOT COVER
-----------------------------------
Whether the thresholds are right, whether a real room's echo actually
correlates above 0.30, and what the real barge-in latency is. All of that
needs a real speakerphone in a real room and is PENDING. Asserting a
number here would be asserting an assumption.

Synthetic signals only. "Echo" is the reference delayed, attenuated and
low-passed -- which is what a room does to it -- so the correlation test is
measuring the mechanism rather than an identity.
"""
from __future__ import annotations

import asyncio
import sys
import time
import types
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import echo_guard as eg  # noqa: E402
from agent.audio_quality import AudioQuality  # noqa: E402
from agent.quality_metrics import QualityMetrics  # noqa: E402

SR = 16000
# No module-level generator: see hiss(). A shared one makes each test's noise
# depend on how many ran before it, which turns borderline assertions into
# order-dependent flakes.


# ---------------------------------------------------------------------------
# signal builders
# ---------------------------------------------------------------------------
def voice(seconds: float, amplitude: float = 0.3, seed: int = 1) -> np.ndarray:
    """A speech-like signal: syllable bursts at moving pitch with gaps."""
    r = np.random.default_rng(seed)
    out, elapsed, i = [], 0.0, 0
    while elapsed < seconds:
        f = 140.0 + 70.0 * r.integers(0, 5)
        t = np.arange(int(0.18 * SR)) / SR
        out.append((np.sin(2 * np.pi * f * t) * amplitude).astype(np.float32))
        out.append(np.zeros(int(0.05 * SR), dtype=np.float32))
        elapsed += 0.23
        i += 1
    return np.concatenate(out)[: int(seconds * SR)].astype(np.float32)


def as_room_echo(ref: np.ndarray, delay_s: float = 0.12,
                 attenuation_db: float = 18.0) -> np.ndarray:
    """What a room does to the agent's voice on its way back: delays it,
    makes it much quieter, and smears it. NOT a copy -- if the test used a
    copy it would be proving that a signal correlates with itself."""
    gain = 10.0 ** (-attenuation_db / 20.0)
    delayed = np.concatenate([np.zeros(int(delay_s * SR), dtype=np.float32), ref])[: ref.size]
    kernel = np.ones(24, dtype=np.float32) / 24.0            # crude low-pass
    smeared = np.convolve(delayed, kernel, mode="same").astype(np.float32)
    return (smeared * gain).astype(np.float32)


def hiss(seconds: float, amplitude: float = 0.01) -> np.ndarray:
    """Seeded FROM THE ARGUMENTS, so each signal is a pure function of its
    own inputs and the order of tests is irrelevant."""
    seed = (int(seconds * 1000), int(amplitude * 1_000_000))
    return (np.random.default_rng(seed).standard_normal(int(seconds * SR))
            * amplitude).astype(np.float32)


# ===========================================================================
# A. CORRELATION AND ECHO RETURN LOSS
# ===========================================================================
def test_identical_signals_correlate_at_one():
    v = voice(0.5)
    assert eg.normalized_correlation(v, v) == pytest.approx(1.0, abs=1e-6)


def test_waveform_correlation_does_not_separate_echo_from_a_stranger():
    """WHY THE PRIMARY SIGNAL IS LEVEL, NOT CORRELATION.

    This pins the measurement that drove the design. A room delays and
    low-passes the echo, which collapses waveform correlation; meanwhile two
    different speakers over a lag search are not nearly as uncorrelated as
    one would hope. The two numbers end up close enough that no threshold
    sits safely between them.

    If someone later "fixes" barge-in by leaning on correlation again, this
    test tells them what they are trading away."""
    ref = voice(1.0)
    echo_corr, _ = eg.best_lag_correlation(as_room_echo(ref), ref, SR, 0.5)
    stranger_corr, _ = eg.best_lag_correlation(
        voice(1.0, seed=7), voice(1.0, seed=8), SR, 0.5)

    assert echo_corr < 0.45          # the echo does NOT look much like us
    assert stranger_corr > 0.15      # a stranger does not look nothing like us
    assert echo_corr - stranger_corr < 0.30   # no usable margin between them


def test_correlation_is_scale_invariant():
    """The echo is far quieter than what we played. A similarity measure
    that cared about level would call every echo a stranger."""
    v = voice(0.5)
    assert eg.normalized_correlation(v, v * 0.01) == pytest.approx(1.0, abs=1e-6)


def test_correlation_of_empty_input_is_zero_not_an_error():
    assert eg.normalized_correlation(np.zeros(0), voice(0.2)) == 0.0


def test_lag_search_finds_a_delayed_echo():
    """The echo does not arrive when we sent it. Without the lag search the
    zero-lag correlation of a delayed copy is near nothing."""
    ref = voice(1.0)
    mic = as_room_echo(ref, delay_s=0.12)

    zero_lag = eg.normalized_correlation(mic, ref)
    best, lag = eg.best_lag_correlation(mic, ref, SR, max_delay_s=0.5)

    assert best > zero_lag                       # the search does find the delay
    assert lag == pytest.approx(0.12, abs=0.03)  # and lands near the right one


def test_lag_search_requires_a_substantial_overlap():
    """REGRESSION GUARD -- finding 7, found while fixing findings 1 and 4.

    The lag search used to accept any overlap down to 50ms. When the
    reference is no more than the microphone window long, a large lag leaves
    only a sliver, and two speech signals correlate spuriously over a sliver.
    Measured: a genuine caller scored 0.826 against our playback on ~105ms of
    overlap at lag 0.495s, which tripped the echo veto and would have
    suppressed a real barge-in.

    A lag is now only considered while it leaves most of the window intact."""
    mic = voice(0.6, amplitude=0.5, seed=42)
    short_ref = voice(0.6, amplitude=0.4, seed=1)

    corr, _ = eg.best_lag_correlation(mic, short_ref, SR, 0.5)

    assert corr < eg.CONFIG.echo_correlation_threshold, (
        "a short overlap is manufacturing a spurious echo match")


def test_lag_search_does_not_manufacture_a_match_for_the_caller():
    """Scanning many lags gives many chances to find a spurious peak. The
    caller's voice must still not look like our playback."""
    best, _ = eg.best_lag_correlation(voice(1.0, seed=7), voice(1.0, seed=8), SR, 0.5)
    assert best < eg.CONFIG.echo_correlation_threshold + 0.25


def test_erl_is_large_for_a_handset_and_small_for_a_speakerphone():
    ref = voice(1.0, amplitude=0.5)
    handset = eg.echo_return_loss_db(as_room_echo(ref, attenuation_db=35.0), ref)
    speaker = eg.echo_return_loss_db(as_room_echo(ref, attenuation_db=8.0), ref)
    assert handset > speaker
    assert speaker < eg.CONFIG.speakerphone_erl_db < handset


def test_erl_is_infinite_when_nothing_comes_back():
    assert eg.echo_return_loss_db(np.zeros(SR, dtype=np.float32), voice(1.0)) == float("inf")


def test_erl_is_zero_when_there_was_no_reference():
    assert eg.echo_return_loss_db(voice(1.0), np.zeros(SR, dtype=np.float32)) == 0.0


# ===========================================================================
# B. ECHO SUPPRESSION
# ===========================================================================
def test_suppression_attenuates_the_echo_it_is_given():
    ref = voice(1.0)
    mic = as_room_echo(ref)
    out = eg.suppress_echo(mic, ref, SR)
    assert eg.rms_dbfs(out) < eg.rms_dbfs(mic) - 2.0


def test_suppression_preserves_length_and_dtype():
    ref = voice(1.0)
    out = eg.suppress_echo(as_room_echo(ref), ref, SR)
    assert out.shape == (SR,)
    assert out.dtype == np.float32


def test_suppression_is_a_noop_when_disabled():
    cfg = eg.EchoConfig(echo_suppression_enabled=False)
    mic = as_room_echo(voice(1.0))
    assert np.array_equal(eg.suppress_echo(mic, voice(1.0), SR, cfg), mic)


def test_suppression_is_a_noop_without_a_usable_reference():
    mic = voice(1.0)
    assert np.array_equal(eg.suppress_echo(mic, np.zeros(10, dtype=np.float32), SR), mic)


def test_suppression_does_not_clip():
    ref = voice(1.0, amplitude=0.9)
    out = eg.suppress_echo(as_room_echo(ref, attenuation_db=3.0), ref, SR)
    assert np.max(np.abs(out)) <= 1.0 + 1e-6


# ===========================================================================
# C. THE PLAYBACK REFERENCE
# ===========================================================================
def test_reference_returns_what_was_playing_at_that_moment():
    ref = eg.PlaybackReference(SR)
    v = voice(1.0)
    ref.add(5.0, v)
    got = ref.slice(5.0, 6.0)
    assert got.size == SR
    assert np.allclose(got, v, atol=1e-6)


def test_reference_is_zero_filled_where_nothing_was_playing():
    """Silence in the reference is a true statement -- we were making no
    sound -- not missing data. It has to line up sample-for-sample with the
    microphone slice over the same interval."""
    ref = eg.PlaybackReference(SR)
    ref.add(5.0, voice(0.5))
    got = ref.slice(4.0, 6.0)
    assert got.size == 2 * SR
    assert not np.any(got[: SR])              # before playback started
    assert np.any(got[SR: int(1.5 * SR)])     # during
    assert not np.any(got[int(1.5 * SR):])    # after it ended


def test_reference_slice_outside_any_playback_is_silent():
    ref = eg.PlaybackReference(SR)
    ref.add(1.0, voice(0.5))
    assert not np.any(ref.slice(10.0, 11.0))
    assert not ref.has_audio_in(10.0, 11.0)


def test_reference_prunes_old_playback():
    ref = eg.PlaybackReference(SR, eg.EchoConfig(reference_retention_s=2.0))
    ref.add(0.0, voice(0.5))
    ref.add(100.0, voice(0.5))
    ref.prune(100.0)
    assert len(ref) == 1


def test_reference_slice_of_an_inverted_range_is_empty():
    assert eg.PlaybackReference(SR).slice(5.0, 4.0).size == 0


# ===========================================================================
# D. THE ECHO / BARGE-IN DECISION
# ===========================================================================
@pytest.mark.parametrize("attenuation_db", [8.0, 18.0, 28.0])
def test_our_own_echo_is_never_a_barge_in(attenuation_db):
    """The whole reason the microphone can stay open.

    Swept across attenuations because a speakerphone on a table (8dB) and a
    phone held at arm's length (28dB) are the same decision at very
    different signal levels."""
    guard = eg.EchoGuard(SR)
    ref = voice(1.0, amplitude=0.4)
    guard.note_playback(10.0, ref)

    verdict = guard.assess(as_room_echo(ref, attenuation_db=attenuation_db), 10.0, SR)
    assert not verdict.is_barge_in, verdict.as_dict()


def test_a_caller_quieter_than_the_expected_echo_cannot_interrupt():
    """A KNOWN LIMITATION, pinned rather than hidden.

    The level test cannot separate a quiet caller from a loud echo, because
    at that point the microphone genuinely does not contain evidence that a
    second voice is present. The failure is in the safe direction -- the
    agent finishes its sentence -- but the caller has to speak up, and on a
    loud speakerphone that is a real cost. Whether it bites in practice
    needs a real room and is PENDING."""
    guard = eg.EchoGuard(SR)
    guard.note_playback(10.0, voice(1.0, amplitude=0.4, seed=1))
    verdict = guard.assess(voice(1.0, amplitude=0.02, seed=42), 10.0, SR)
    assert not verdict.is_barge_in


def test_the_caller_talking_over_the_agent_is_a_barge_in():
    guard = eg.EchoGuard(SR)
    guard.note_playback(10.0, voice(1.0, seed=1))

    verdict = guard.assess(voice(1.0, amplitude=0.4, seed=42), 10.0, SR)
    assert verdict.is_barge_in
    assert not verdict.is_echo
    assert verdict.reason == "double_talk"


def test_silence_during_playback_is_neither():
    guard = eg.EchoGuard(SR)
    guard.note_playback(10.0, voice(1.0))
    verdict = guard.assess(np.zeros(SR, dtype=np.float32), 10.0, SR)
    assert not verdict.is_barge_in and not verdict.is_echo
    assert verdict.reason == "below_level_floor"


def test_a_brief_noise_does_not_interrupt_the_agent():
    """A cough or a chair scrape should not cut the agent off mid-sentence."""
    guard = eg.EchoGuard(SR)
    guard.note_playback(10.0, voice(1.0, amplitude=0.05, seed=1))
    verdict = guard.assess(voice(0.05, amplitude=0.5, seed=77), 10.0, SR)
    assert not verdict.is_barge_in
    assert verdict.reason == "too_brief"


def test_speech_when_we_were_not_playing_is_the_caller():
    guard = eg.EchoGuard(SR)
    verdict = guard.assess(voice(1.0), 10.0, SR)
    assert verdict.is_barge_in
    assert verdict.reason == "no_reference"


def test_verdict_is_json_safe_including_infinite_erl():
    guard = eg.EchoGuard(SR)
    guard.note_playback(10.0, voice(1.0))
    d = guard.assess(np.zeros(SR, dtype=np.float32), 10.0, SR).as_dict()
    assert d["erl_db"] is None or isinstance(d["erl_db"], float)
    assert isinstance(d["reason"], str)


# ===========================================================================
# E. PATH CLASSIFICATION
# ===========================================================================
def test_a_loud_echo_classifies_the_path_as_speakerphone():
    guard = eg.EchoGuard(SR)
    ref = voice(1.0, amplitude=0.5)
    for t in (10.0, 20.0, 30.0):
        guard.note_playback(t, ref)
        guard.assess(as_room_echo(ref, attenuation_db=8.0), t, SR)
    assert guard.classify() == eg.PATH_SPEAKERPHONE


def test_a_quiet_echo_classifies_the_path_as_handset():
    """A handset returns so little that the window falls below the level
    floor. That is still an observation -- the strongest possible evidence
    of good isolation -- so it has to be recorded, or handset calls stay
    permanently unclassified. That was a real bug these tests caught."""
    guard = eg.EchoGuard(SR)
    ref = voice(1.0, amplitude=0.5)
    for t in (10.0, 20.0, 30.0):
        guard.note_playback(t, ref)
        guard.assess(as_room_echo(ref, attenuation_db=40.0), t, SR)
    assert guard.classify() == eg.PATH_HANDSET


def test_erl_estimate_starts_conservative_then_follows_the_room():
    """Before any evidence, assume a LOUD echo. That demands a louder caller
    and so errs toward not interrupting the agent."""
    guard = eg.EchoGuard(SR)
    assert guard.erl_estimate() == eg.CONFIG.bootstrap_erl_db

    ref = voice(1.0, amplitude=0.5)
    for t in (10.0, 20.0, 30.0):
        guard.note_playback(t, ref)
        guard.assess(as_room_echo(ref, attenuation_db=30.0), t, SR)
    assert guard.erl_estimate() > eg.CONFIG.bootstrap_erl_db


def test_one_observation_is_not_enough_to_classify():
    guard = eg.EchoGuard(SR, eg.EchoConfig(classification_min_observations=3))
    ref = voice(1.0)
    guard.note_playback(10.0, ref)
    guard.assess(as_room_echo(ref, attenuation_db=5.0), 10.0, SR)
    assert guard.classify() == eg.PATH_UNKNOWN


def test_a_client_hint_is_used_until_there_is_evidence():
    guard = eg.EchoGuard(SR)
    guard.declare_path("speakerphone")
    assert guard.classify() == eg.PATH_SPEAKERPHONE


def test_the_measurement_overrides_a_wrong_client_hint():
    """A hint can be absent or simply wrong. Echo Return Loss cannot."""
    guard = eg.EchoGuard(SR)
    guard.declare_path("handset")
    ref = voice(1.0, amplitude=0.5)
    for t in (10.0, 20.0, 30.0):
        guard.note_playback(t, ref)
        guard.assess(as_room_echo(ref, attenuation_db=8.0), t, SR)
    assert guard.classify() == eg.PATH_SPEAKERPHONE


def test_a_nonsense_hint_is_ignored():
    guard = eg.EchoGuard(SR)
    guard.declare_path("carrier pigeon")
    assert guard.declared_path == eg.PATH_UNKNOWN


def test_unknown_reports_as_handset():
    """Guessing speakerphone would move ordinary callers into the very
    bucket whose accuracy we are trying to measure."""
    guard = eg.EchoGuard(SR)
    assert guard.classify() == eg.PATH_UNKNOWN
    assert guard.reporting_path() == eg.PATH_HANDSET


# ===========================================================================
# F. HANDSET vs SPEAKERPHONE METRICS
# ===========================================================================
def q(snr_db: float = 30.0, usable: bool = True) -> AudioQuality:
    return AudioQuality(snr_db=snr_db, speech_ratio=0.5, clipping_ratio=0.0,
                        rms_dbfs=-20.0, duration_s=1.0, usable=usable)


def test_path_buckets_are_counted_separately():
    m = QualityMetrics()
    m.record_turn(q(), success=True, path="handset")
    m.record_turn(q(), success=True, path="handset")
    m.record_turn(q(), success=True, path="speakerphone")
    m.record_turn(q(), success=False, path="speakerphone")

    pb = m.snapshot()["path_buckets"]
    assert pb["handset"]["turns"] == 2
    assert pb["handset"]["accuracy"] == 1.0
    assert pb["speakerphone"]["turns"] == 2
    assert pb["speakerphone"]["accuracy"] == 0.5


def test_accuracy_gap_is_handset_minus_speakerphone():
    m = QualityMetrics()
    for _ in range(10):
        m.record_turn(q(), success=True, path="handset")
    for i in range(10):
        m.record_turn(q(), success=(i < 7), path="speakerphone")

    assert m.snapshot()["path_buckets"]["accuracy_gap"] == pytest.approx(0.30)


def test_accuracy_gap_is_none_until_both_buckets_have_turns():
    """A gap computed against an empty bucket is not a small gap -- it is no
    measurement, and must not read as one."""
    m = QualityMetrics()
    assert m.snapshot()["path_buckets"]["accuracy_gap"] is None
    m.record_turn(q(), success=True, path="handset")
    assert m.snapshot()["path_buckets"]["accuracy_gap"] is None
    m.record_turn(q(), success=True, path="speakerphone")
    assert m.snapshot()["path_buckets"]["accuracy_gap"] == 0.0


def test_path_and_noise_dimensions_do_not_interfere():
    """A turn is filed in one noise bucket AND one path bucket. Neither
    total may be distorted by the other split."""
    m = QualityMetrics()
    m.record_turn(q(snr_db=4.0), success=True, path="speakerphone")   # noisy
    m.record_turn(q(snr_db=30.0), success=True, path="speakerphone")  # clean

    snap = m.snapshot()
    assert snap["noisy_bucket"]["turns"] == 1
    assert snap["clean_bucket"]["turns"] == 1
    assert snap["path_buckets"]["speakerphone"]["turns"] == 2
    assert snap["overall_turns"] == 2


def test_a_turn_recorded_without_a_path_still_counts_in_the_noise_bucket():
    m = QualityMetrics()
    m.record_turn(q(), success=True)
    snap = m.snapshot()
    assert snap["overall_turns"] == 1
    assert snap["path_buckets"]["handset"]["turns"] == 0
    assert snap["path_buckets"]["speakerphone"]["turns"] == 0


def test_barge_ins_are_counted():
    m = QualityMetrics()
    m.record_barge_in()
    m.record_barge_in()
    assert m.snapshot()["barge_ins"] == 2


# ===========================================================================
# G. WIRING IN main_pcm.py
# ===========================================================================
def _install_stubs() -> None:
    for name in ("nemo", "nemo.collections", "nemo.collections.asr",
                 "nemo.collections.asr.parts",
                 "nemo.collections.asr.parts.submodules",
                 "nemo.collections.asr.parts.submodules.rnnt_decoding"):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["nemo.collections.asr"].models = types.SimpleNamespace(
        ASRModel=types.SimpleNamespace(restore_from=lambda **kw: None))
    sys.modules["nemo.collections.asr.parts.submodules.rnnt_decoding"].RNNTDecodingConfig = object
    omegaconf = sys.modules.setdefault("omegaconf", types.ModuleType("omegaconf"))
    omegaconf.OmegaConf = types.SimpleNamespace(structured=lambda x: x)

    import torch
    ta = sys.modules.setdefault("torchaudio", types.ModuleType("torchaudio"))
    ta.save = lambda *a, **k: None
    ta.load = lambda *a, **k: (torch.zeros(1, 16000), 16000)
    ta.functional = types.SimpleNamespace(resample=lambda w, a, b: w)


_install_stubs()

import main_pcm as app  # noqa: E402


class FakeWS:
    def __init__(self):
        self.frames = []

    async def send_text(self, t):
        self.frames.append(t)

    async def send_bytes(self, b):
        pass

    async def close(self):
        pass


def make_session():
    s = app.CallSession(FakeWS())
    s.agent_speaking = True
    s.speak_deadline = 9e9
    return s


def test_barge_in_does_not_schedule_the_resync_that_would_discard_it():
    """THE regression guard for the earlier utterance-boundary work.

    release_gate() sets resync_pending, and _resync_after_playback then
    jumps processed_until_s to the end of the buffer to throw away
    everything captured during playback. During a barge-in that region IS
    the caller's interruption. Stopping the agent and then discarding what
    stopped it would be worse than not supporting barge-in at all."""
    session = make_session()
    session.resync_pending = False
    session.processed_until_s = 3.0

    session.barge_in()

    assert session.agent_speaking is False
    assert session.resync_pending is False          # <-- the point
    assert session.processed_until_s == 3.0         # nothing skipped
    session.cleanup()


def test_release_gate_still_schedules_a_resync_for_ordinary_playback():
    """The normal path must keep its old behaviour: audio captured while the
    agent spoke uninterrupted is still discarded."""
    session = make_session()
    session.release_gate()
    assert session.resync_pending is True
    session.cleanup()


def test_barge_in_stashes_the_reference_exactly_once():
    session = make_session()
    session.echo.note_playback(session.call_time_s(), voice(1.0))
    session.barge_in()

    first = session.take_echo_reference()
    assert first is not None and np.any(first)
    assert session.take_echo_reference() is None   # not applied to later turns
    session.cleanup()


def test_ordinary_turn_has_no_echo_reference_to_subtract():
    session = app.CallSession(FakeWS())
    assert session.take_echo_reference() is None
    session.cleanup()


def test_check_barge_in_stops_playback_when_the_caller_talks_over(monkeypatch):
    """The caller speaks OVER a reply that is already playing.

    Note the setup: playback must have STARTED BEFORE the window being
    judged. An earlier version of this test recorded playback as beginning
    "now" and then asked about the preceding 0.6s -- a stretch in which the
    agent had not yet made a sound -- so it passed through the no_reference
    branch and never exercised double-talk detection at all."""
    session = make_session()
    caller = voice(app.ECHO_CFG.barge_in_window_s, amplitude=0.5, seed=42)

    async def fake_tail(sess, seconds):
        return caller, SR

    monkeypatch.setattr(app, "_recent_mic_tail", fake_tail)
    # A 2s reply that began 1s ago, so it spans the whole window under test.
    session.echo.note_playback(session.call_time_s() - 1.0,
                               voice(2.0, amplitude=0.4, seed=1))

    assert asyncio.run(app._check_barge_in(session)) is True
    assert session.agent_speaking is False
    assert any("_stop_audio" in f for f in session.ws.frames)
    session.cleanup()


def test_check_barge_in_ignores_our_own_echo(monkeypatch):
    session = make_session()
    ref = voice(1.0, amplitude=0.4, seed=1)
    session.echo.note_playback(session.call_time_s() - 0.2, ref)
    echo = as_room_echo(ref)[: int(app.ECHO_CFG.barge_in_window_s * SR)]

    async def fake_tail(sess, seconds):
        return echo, SR

    monkeypatch.setattr(app, "_recent_mic_tail", fake_tail)

    assert asyncio.run(app._check_barge_in(session)) is False
    assert session.agent_speaking is True             # agent keeps talking
    assert not any("_stop_audio" in f for f in session.ws.frames)
    session.cleanup()


def test_barge_in_can_be_switched_off(monkeypatch):
    session = make_session()

    async def fake_tail(sess, seconds):
        raise AssertionError("must not even read the microphone when disabled")

    monkeypatch.setattr(app, "_recent_mic_tail", fake_tail)
    monkeypatch.setattr(app, "ECHO_CFG", eg.EchoConfig(barge_in_enabled=False))
    assert asyncio.run(app._check_barge_in(session)) is False
    session.cleanup()


def test_barge_in_skips_past_the_agents_own_reply_but_not_past_the_caller():
    """REGRESSION GUARD -- found in review, not by a test.

    barge_in() originally left processed_until_s untouched. The poll loop
    then falls straight through to turn detection, so the very next tail
    still contained the agent's whole reply: the detector would place
    utterance_start_s at the ECHO's onset and hand ASR a clip of the agent
    talking. That is precisely the self-answering loop the half-duplex gate
    existed to prevent -- reintroduced by the feature meant to replace it.

    The marker must move forward past the reply, and no further than the
    barge-in window, or the interruption itself is discarded."""
    session = make_session()
    session.processed_until_s = 0.0
    session.started_at = time.time() - 30.0        # 30s into the call

    session.barge_in()

    window_start = session.call_time_s() - app.ECHO_CFG.barge_in_window_s
    assert session.processed_until_s > 25.0, "still behind the agent's reply"
    assert session.processed_until_s <= window_start, "skipped the interruption"
    session.cleanup()


def test_barge_in_never_rewinds_the_marker():
    """max(), not assignment: a barge-in must not reach back into audio a
    previous turn already consumed -- the same guard the utterance-boundary
    fix applies at the clip level."""
    session = make_session()
    session.started_at = time.time() - 30.0
    session.processed_until_s = 29.9

    session.barge_in()

    assert session.processed_until_s == 29.9
    session.cleanup()


def test_a_queued_reply_reference_is_timestamped_when_it_will_actually_play():
    """REGRESSION GUARD -- found in review, not by a test.

    Replies queue on the client (see hold_gate_for), so a second reply does
    not start when it is sent. Timestamping its echo reference at send time
    put it earlier than the sound it describes: during the second clip's
    real playback the reference lookup returned silence, "no_reference"
    fired, and the agent's own echo would have been read as the caller
    interrupting."""
    session = make_session()
    session.agent_speaking = False

    first = session.playback_start_s()
    session.hold_gate_for(4.0)                    # a 4-second reply is now in flight
    second = session.playback_start_s()

    assert second >= first + 3.5, "queued reply claims to start immediately"
    session.cleanup()


def test_playback_start_is_now_when_nothing_is_playing():
    session = app.CallSession(FakeWS())
    session.agent_speaking = False
    assert session.playback_start_s() == pytest.approx(session.call_time_s(), abs=0.1)
    session.cleanup()


def test_poll_cadence_is_faster_while_the_agent_speaks():
    """Barge-in cannot be detected sooner than the poll rate, so the
    interval used during playback has to sit well under the target."""
    assert eg.CONFIG.barge_in_poll_s < eg.CONFIG.barge_in_target_s

    # Compared against the DEFAULT read from source, not the module global:
    # tests/test_noisy_turn_boundaries.py drives the real poll loop and
    # temporarily sets that global to 0.01.
    default_poll = float(
        Path("main.py").read_text(encoding="utf-8")
        .split("POLL_INTERVAL_S = ")[1].split(chr(10))[0])
    assert eg.CONFIG.barge_in_poll_s < default_poll

    # ...and the fast cadence is only actually used where reading the tail is
    # cheap. See test_fast_barge_in_cadence_is_gated_on_transport_cost.
    assert app.TAIL_READ_IS_CHEAP is True        # main_pcm


def test_fast_barge_in_cadence_is_gated_on_transport_cost():
    """REGRESSION GUARD -- finding 2.

    Raising the playback poll to 0.10s is free on raw PCM (a bytearray
    slice) and ruinous on WebM, where every tail read re-decodes the entire
    call from byte 0 and spawns ffmpeg to do it. Ungated, that meant ~10
    ffmpeg spawns per second per speaking call -- reintroducing the O(T^2)
    blowup agent/pcm_buffer.py exists to remove.

    The consequence is deliberate and worth stating: barge-in can only meet
    barge_in_target_s on the PCM transport."""
    webm = Path("main.py").read_text(encoding="utf-8")
    pcm = Path("main_pcm.py").read_text(encoding="utf-8")

    assert "TAIL_READ_IS_CHEAP = False" in webm, "WebM must not claim cheap tail reads"
    assert "TAIL_READ_IS_CHEAP = True" in pcm, "PCM tail reads are a slice"

    # Both must actually consult the flag before choosing the fast cadence.
    for name, src in (("main.py", webm), ("main_pcm.py", pcm)):
        assert "session.agent_speaking and TAIL_READ_IS_CHEAP" in src, (
            f"{name} takes the fast cadence without checking transport cost")


# ===========================================================================
# H2. REVIEW REGRESSIONS -- each of these pins a bug found by review, and each
#     failed before the corresponding fix.
# ===========================================================================
def test_our_own_echo_tail_after_playback_ends_is_not_a_barge_in():
    """REGRESSION GUARD -- finding 1, the worst bug in this feature.

    The client only reports playback_done after a 250ms guard, so there is a
    window where the agent is still "speaking", the reference has ended, and
    the room is still ringing with the reply. An aligned-only reference
    lookup goes empty there, took the no_reference branch, and classified the
    agent's own decaying echo as the caller -- interrupting the agent with
    itself, which is precisely the loop the old half-duplex gate prevented.

    Measured before the fix: reason=no_reference, is_barge_in=True at
    -29.8 dBFS."""
    guard = eg.EchoGuard(SR)
    guard.note_playback(9.0, voice(1.0, amplitude=0.4))    # reply spans 9.0-10.0

    tail = voice(0.6, amplitude=0.05, seed=1)              # quiet residue of us
    verdict = guard.assess(tail, 10.0, SR)                 # window starts AT the end

    assert not verdict.is_barge_in, verdict.as_dict()
    assert verdict.reason == "within_expected_echo_level"


def test_a_real_caller_at_that_same_seam_still_gets_through():
    """The fix must not buy safety by deafening the agent at the one moment a
    caller is most likely to speak -- the instant the reply finishes."""
    guard = eg.EchoGuard(SR)
    guard.note_playback(9.0, voice(1.0, amplitude=0.4))

    verdict = guard.assess(voice(0.6, amplitude=0.45, seed=42), 10.0, SR)

    assert verdict.is_barge_in
    assert verdict.reason == "double_talk"


def test_speech_long_after_playback_is_still_the_caller():
    """The reference reaches back one round trip, not indefinitely. Audio
    arriving 20 seconds after the last reply cannot be its echo."""
    guard = eg.EchoGuard(SR)
    guard.note_playback(1.0, voice(1.0, amplitude=0.4))

    verdict = guard.assess(voice(0.6, amplitude=0.4, seed=42), 30.0, SR)

    assert verdict.is_barge_in
    assert verdict.reason == "no_reference"


def test_reference_level_ignores_the_silence_it_is_padded_with():
    """PlaybackReference zero-fills gaps so the window aligns with the
    microphone. Averaging those zeros into the level would UNDERSTATE how
    loud the echo may be, and understating it is exactly what lets echo pass
    as a caller."""
    guard = eg.EchoGuard(SR)
    # A short reply inside a much longer lookup window: mostly zero-padding.
    guard.note_playback(9.9, voice(0.1, amplitude=0.4))

    quiet_echo = voice(0.6, amplitude=0.02, seed=1)
    verdict = guard.assess(quiet_echo, 10.0, SR)

    assert not verdict.is_barge_in, verdict.as_dict()


def test_erl_history_is_bounded():
    """REGRESSION GUARD -- finding 3.

    The median over this is recomputed on every playback poll and every turn.
    Unbounded, that was measured at 3.07ms per poll after 3000 observations --
    ~368ms/s of CPU across 12 concurrent calls, spent re-medianing a list that
    only grows."""
    guard = eg.EchoGuard(SR, eg.EchoConfig(erl_history=50))
    for i in range(500):
        guard._record_erl(20.0 + i % 5)

    assert len(guard._erl_observations) == 50
    assert guard.erl_estimate() > 0


def test_erl_history_is_guarded_by_a_lock():
    """REGRESSION GUARD -- finding 5. assess() runs on a thread-pool worker
    while reporting_path() reads from the event loop; both TurnDetector and
    TurnASR already lock cross-thread state this way."""
    import threading as _threading

    guard = eg.EchoGuard(SR)
    assert isinstance(guard._lock, type(_threading.Lock()))

    # A snapshot must be internally consistent, not a live view of the deque.
    guard._record_erl(10.0)
    snapshot = guard._finite_observations()
    guard._record_erl(20.0)
    assert len(snapshot) == 1


def test_has_audio_in_does_not_build_the_slice():
    """REGRESSION GUARD -- finding 6. The obvious implementation allocates
    9600 float32 to answer a boolean."""
    ref = eg.PlaybackReference(SR)
    ref.add(5.0, voice(1.0))

    called = []
    original = ref.slice
    ref.slice = lambda a, b: (called.append((a, b)), original(a, b))[1]

    assert ref.has_audio_in(5.2, 5.4) is True
    assert ref.has_audio_in(50.0, 51.0) is False
    assert called == [], "has_audio_in allocated a slice"


# ===========================================================================
# H3. THE CALLER WHO ALREADY KNOWS THE ANSWER
#
#     "I want to talk over the agent and be heard, so that I am not made to
#      listen to the rest."
#
#     This caller interrupts EARLY and REPEATEDLY. The speakerphone work made
#     interruption possible at all; these pin the three ways it still failed
#     for someone who does it more than once.
# ===========================================================================
def test_a_second_interruption_works_after_the_first_cut_playback_short():
    """The one that matters most for this story.

    Barge-in stops playback, but the reference used to go on claiming the
    whole reply had played. Every later window was then judged against sound
    that never left the speaker, the expected-echo ceiling stayed high, and
    the caller could not interrupt again. A caller who already knows the
    answer interrupts repeatedly by nature -- so the feature worked once and
    then quietly stopped working."""
    guard = eg.EchoGuard(SR)
    guard.note_playback(10.0, voice(5.0, amplitude=0.4))    # a long reply
    guard.reference.truncate_after(11.0)                    # cut off at 11.0

    assert not guard.reference.has_audio_in(13.0, 13.6), "reference outlived playback"

    verdict = guard.assess(voice(0.6, amplitude=0.3, seed=42), 13.0, SR)
    assert verdict.is_barge_in, verdict.as_dict()


def test_truncation_keeps_the_part_that_did_play():
    """It must forget only the unplayed remainder. The part the caller
    actually heard is still echoing and is still needed to recognise it."""
    guard = eg.EchoGuard(SR)
    guard.note_playback(10.0, voice(5.0, amplitude=0.4))
    guard.reference.truncate_after(11.0)

    assert guard.reference.has_audio_in(10.2, 10.8), "forgot audio that did play"
    assert not guard.reference.has_audio_in(11.5, 12.0)


def test_truncation_drops_a_reply_that_never_started():
    guard = eg.EchoGuard(SR)
    guard.note_playback(20.0, voice(2.0))          # queued, not yet playing
    guard.reference.truncate_after(15.0)
    assert len(guard.reference) == 0


def test_talking_over_the_agent_does_not_make_it_deafer():
    """REGRESSION GUARD.

    `assess` used to record an ERL observation for every window, including
    ones it had just judged to be double-talk. But during double-talk the
    microphone holds a second voice, so that number is not the room's echo
    return loss at all -- it is the caller's level relative to ours. Feeding
    it to the median dragged the estimate down, which raised the
    expected-echo ceiling, which made the NEXT interruption harder.

    Measured before the fix: 39.7dB falling to 3.3dB over twelve double-talk
    windows. The system got deafer the more the caller talked over it, which
    is precisely backwards for this story."""
    guard = eg.EchoGuard(SR)
    reply = voice(4.0, amplitude=0.4)
    guard.note_playback(9.0, reply)
    echo = as_room_echo(reply, attenuation_db=35.0)

    for k in range(4):                              # quiet echo-only polls
        guard.assess(echo[: int(0.6 * SR)], 10.0 + k * 0.1, SR)
    before = guard.erl_estimate()

    for k in range(12):                             # the caller talks over it
        guard.assess(voice(0.6, amplitude=0.3, seed=42), 11.0 + k * 0.1, SR)
    after = guard.erl_estimate()

    assert after == pytest.approx(before), f"estimate drifted {before:.1f} -> {after:.1f}"


def test_a_quiet_window_is_still_recorded_as_evidence():
    """The opposite guard. Double-talk must not be recorded, but a window too
    quiet for anyone to be talking must be -- it is the strongest possible
    evidence of a handset, and dropping it left handset calls
    unclassifiable."""
    guard = eg.EchoGuard(SR)
    reply = voice(2.0, amplitude=0.4)
    guard.note_playback(9.0, reply)

    guard.assess(np.zeros(int(0.6 * SR), dtype=np.float32), 10.0, SR)
    assert len(guard._erl_observations) == 1


def test_a_normal_voice_can_interrupt_almost_immediately():
    """The caller who already knows the answer does not wait for a gap, and
    does not shout. One echo-only poll (0.1s) is enough to replace the
    deliberately-pessimistic bootstrap with a real measurement.

    The first poll of a call's first reply remains deaf -- there is genuinely
    no measurement yet -- and that limitation is pinned below."""
    reply = voice(4.0, amplitude=0.4)
    echo = as_room_echo(reply, attenuation_db=35.0)

    guard = eg.EchoGuard(SR)
    guard.note_playback(9.0, reply)
    guard.assess(echo[: int(0.6 * SR)], 10.0, SR)          # one echo-only poll

    verdict = guard.assess(voice(0.6, amplitude=0.15, seed=42), 10.1, SR)
    assert verdict.is_barge_in, verdict.as_dict()


def test_the_very_first_poll_of_a_call_cannot_be_interrupted_quietly():
    """A KNOWN LIMITATION, pinned rather than hidden.

    Before any echo has been observed the guard assumes a loud room
    (`bootstrap_erl_db`), which demands a louder caller. That is wrong in the
    safe direction -- the alternative is a system that interrupts itself on
    the greeting -- and it lasts one poll, about 0.1s. Whether a real caller
    ever lands inside that window is PENDING real-audio validation."""
    guard = eg.EchoGuard(SR)
    guard.note_playback(9.0, voice(4.0, amplitude=0.4))

    # The bootstrap is what the FIRST decision is made against.
    assert guard.erl_estimate() == eg.CONFIG.bootstrap_erl_db

    verdict = guard.assess(voice(0.6, amplitude=0.15, seed=42), 10.0, SR)
    assert not verdict.is_barge_in

    # A residual worth knowing about: that window was judged
    # `within_expected_echo_level`, so its ERL was recorded even though the
    # caller was in fact talking. It is contaminated, just mildly -- the
    # strong case (clear double-talk) is excluded, and a median over
    # `erl_history` observations absorbs the rest. Distinguishing "quiet
    # caller" from "loud echo" needs evidence the microphone does not
    # contain, which is the same limitation as
    # test_a_caller_quieter_than_the_expected_echo_cannot_interrupt.
    assert verdict.reason == "within_expected_echo_level"


def test_the_estimate_and_the_classification_have_separate_thresholds():
    """They answer different questions. The estimate only has to beat a
    pessimistic bootstrap, and every poll it stays on that bootstrap is a
    poll the caller cannot interrupt -- so one clean observation is enough.
    Classification decides which accuracy bucket a whole call lands in, and
    wants more."""
    assert eg.CONFIG.estimate_min_observations < eg.CONFIG.classification_min_observations

    guard = eg.EchoGuard(SR)
    guard._record_erl(30.0)
    assert guard.erl_estimate() == 30.0                 # estimate: uses it
    assert guard.classify() == eg.PATH_UNKNOWN          # classification: not yet


def test_barge_in_truncates_the_reference_through_the_session():
    """The wiring, not just the primitive: CallSession.barge_in() must call
    truncate_after, or none of the above reaches production."""
    session = make_session()
    session.echo.note_playback(session.call_time_s(), voice(5.0, amplitude=0.4))

    session.barge_in()

    now = session.call_time_s()
    assert not session.echo.reference.has_audio_in(now + 1.0, now + 2.0)
    session.cleanup()


def test_a_misconfigured_estimate_threshold_cannot_disarm_the_level_test():
    """REGRESSION GUARD -- found in code review.

    estimate_min_observations is env-settable. At 0 the length check passed
    on an EMPTY history, np.median([]) returned nan, and `nan < margin` is
    False -- so the level test stopped rejecting anything and the agent could
    interrupt itself on its own echo. A misconfigured env var must not be
    able to disarm the primary safety check."""
    guard = eg.EchoGuard(SR, eg.EchoConfig(estimate_min_observations=0))
    estimate = guard.erl_estimate()

    assert not np.isnan(estimate)
    assert estimate == eg.CONFIG.bootstrap_erl_db


def test_playback_reference_is_locked():
    """REGRESSION GUARD -- found in code review. assess() reads the reference
    from a thread-pool worker while _speak() adds to it and barge_in()
    truncates it on the event loop. add() mutates in place, so a reader
    iterating during an append had no defined behaviour."""
    import threading as _t
    ref = eg.PlaybackReference(SR)
    assert isinstance(ref._lock, type(_t.Lock()))

    ref.add(1.0, voice(0.5))
    snapshot_len = len(ref)
    ref.add(2.0, voice(0.5))
    assert len(ref) == snapshot_len + 1


def _guard_with_known_room(erl_db: float, reply: np.ndarray) -> "eg.EchoGuard":
    """An EchoGuard whose ERL estimate has already converged on a room."""
    guard = eg.EchoGuard(SR)
    guard.note_playback(9.0, reply)
    for _ in range(3):
        guard._record_erl(erl_db)
    return guard


def _double_talk(reply: np.ndarray, erl_db: float, amplitude: float) -> np.ndarray:
    """What the microphone ACTUALLY holds during an interruption: the echo
    AND the caller, summed. Earlier tests used the caller alone, which
    understates how hard the discrimination is."""
    n = int(eg.CONFIG.barge_in_window_s * SR)
    echo = as_room_echo(reply, attenuation_db=erl_db)[:n]
    return (echo + voice(eg.CONFIG.barge_in_window_s, amplitude=amplitude, seed=42)[:n]
            ).astype(np.float32)


@pytest.mark.parametrize("erl_db", [8.0, 12.0, 20.0, 35.0])
def test_a_normal_voice_interrupts_across_the_whole_range_of_rooms(erl_db):
    """THE STORY, as an assertion.

    "I want to talk over the agent and be heard." A caller should not have to
    raise their voice, and it should not depend on how leaky their phone is.

    This failed before the margin was lowered: at an ERL of 8dB -- a loud
    speakerphone -- a normal speaking voice produced only 3.9dB of excess
    against a 6.0dB margin and was ignored entirely."""
    reply = voice(3.0, amplitude=0.4)
    guard = _guard_with_known_room(erl_db, reply)

    verdict = guard.assess(_double_talk(reply, erl_db, 0.25), 10.0, SR)

    assert verdict.is_barge_in, verdict.as_dict()
    assert verdict.reason == "double_talk"


@pytest.mark.parametrize("erl_db", [5.0, 8.0, 12.0, 20.0, 35.0])
def test_our_own_echo_never_fires_at_any_room_leakiness(erl_db):
    """The safety half of the same sweep. Lowering the margin must not have
    bought sensitivity with false interruptions."""
    reply = voice(3.0, amplitude=0.4)
    guard = _guard_with_known_room(erl_db, reply)
    n = int(eg.CONFIG.barge_in_window_s * SR)

    verdict = guard.assess(as_room_echo(reply, attenuation_db=erl_db)[:n], 10.0, SR)
    assert not verdict.is_barge_in, verdict.as_dict()


def test_room_noise_is_not_an_interruption():
    """REGRESSION GUARD -- why a second test exists at all.

    The level test answers "is there more here than our echo?", which is not
    "is someone talking". On a quiet handset line ambient noise clears the
    level bar by 15.5dB -- MORE excess than a normal caller produces on a
    loud speakerphone. The separating band between those two is empty, so no
    amount of margin tuning fixes it; a second, independent question was
    needed."""
    reply = voice(3.0, amplitude=0.4)
    guard = _guard_with_known_room(35.0, reply)
    n = int(eg.CONFIG.barge_in_window_s * SR)

    noise = (as_room_echo(reply, attenuation_db=35.0)[:n]
             + np.random.default_rng(5).standard_normal(n).astype(np.float32) * 0.03)

    verdict = guard.assess(noise.astype(np.float32), 10.0, SR)
    assert not verdict.is_barge_in
    assert verdict.reason == "not_speech"


def test_an_extreme_speakerphone_still_loses_a_normal_voice():
    """A KNOWN LIMITATION, pinned rather than hidden.

    At an ERL of 5dB the echo is only 5dB below what we played -- a phone on
    a hard desk at high volume -- and a normal caller is then comparable in
    level to the echo itself. The microphone genuinely holds little evidence
    that a second voice is present. A loud caller still gets through.

    Whether real rooms reach an ERL that low is PENDING real-audio work."""
    reply = voice(3.0, amplitude=0.4)

    quiet = _guard_with_known_room(5.0, reply).assess(
        _double_talk(reply, 5.0, 0.25), 10.0, SR)
    loud = _guard_with_known_room(5.0, reply).assess(
        _double_talk(reply, 5.0, 0.45), 10.0, SR)

    assert not quiet.is_barge_in        # the limitation
    assert loud.is_barge_in             # but not a total failure


def test_correlation_must_stay_a_veto_not_the_primary_test():
    """MEASURED, not preferred.

    A correlation-first policy -- "uncorrelated with our playback, therefore
    the caller" -- fires on PURE ECHO, because a room delays and low-passes
    the echo until it correlates only ~0.02 with the reference. That is the
    self-answering loop the half-duplex gate existed to prevent.

    If anyone is tempted to promote correlation, this is the number."""
    reply = voice(3.0, amplitude=0.4)
    n = int(eg.CONFIG.barge_in_window_s * SR)
    echo = as_room_echo(reply, attenuation_db=20.0)[:n]

    guard = eg.EchoGuard(SR)
    guard.note_playback(9.0, reply)
    ref_recent = guard.reference.slice(
        10.0 - eg.CONFIG.echo_max_delay_s, 10.0 + eg.CONFIG.barge_in_window_s)
    corr, _ = eg.best_lag_correlation(echo, ref_recent, SR, eg.CONFIG.echo_max_delay_s)

    assert corr < 0.1, "real echo barely correlates -- correlation-first would fire on it"
    assert corr < eg.CONFIG.echo_correlation_threshold


def test_room_noise_is_not_an_interruption_even_with_nothing_playing():
    """REGRESSION GUARD -- found in code review.

    The speech gate was applied to the double-talk branch but NOT to the
    no_reference branch, so it was possible to have room noise rejected while
    the agent was audibly playing and accepted a moment later when it was not.

    That branch is reached while the gate is still open but playback has
    finished -- the window between the last sample leaving the speaker and
    the client's playback_done arriving. Ambient noise there stopped
    playback, opened the gate, advanced the marker, and earned the caller an
    unprompted "sorry, it is noisy" for a turn they never took."""
    guard = eg.EchoGuard(SR)                       # nothing ever played
    n = int(eg.CONFIG.barge_in_window_s * SR)
    noise = np.random.default_rng(5).standard_normal(n).astype(np.float32) * 0.05

    verdict = guard.assess(noise, 10.0, SR)
    assert not verdict.is_barge_in
    assert verdict.reason == "not_speech"


def test_a_voice_with_nothing_playing_is_still_the_caller():
    """The other half: the speech gate must not deafen the no_reference
    branch, only filter it."""
    guard = eg.EchoGuard(SR)
    n = int(eg.CONFIG.barge_in_window_s * SR)

    verdict = guard.assess(voice(eg.CONFIG.barge_in_window_s, amplitude=0.25,
                                 seed=42)[:n], 10.0, SR)
    assert verdict.is_barge_in
    assert verdict.reason == "no_reference"


def test_the_common_echo_only_path_does_not_run_the_lag_search():
    """REGRESSION GUARD -- found in code review.

    best_lag_correlation is the most expensive thing in assess() (2.27ms of a
    2.61ms poll) and it was computed on EVERY poll, including the
    overwhelmingly common echo-only case that returns before ever using it --
    ~273ms/s of wasted CPU across 12 concurrent calls, for a veto that never
    fires. It is now computed only once the level test says a second voice
    may be present.

    Asserted through the verdict rather than by timing, so the test cannot
    flake on a busy machine: the echo-only branch reports corr/lag as 0.0
    precisely because it never measured them."""
    reply = voice(3.0, amplitude=0.4)
    guard = _guard_with_known_room(20.0, reply)
    n = int(eg.CONFIG.barge_in_window_s * SR)

    verdict = guard.assess(as_room_echo(reply, attenuation_db=20.0)[:n], 10.0, SR)

    assert verdict.reason == "within_expected_echo_level"
    assert verdict.correlation == 0.0, "lag search ran on the cheap path"
    assert verdict.lag_s == 0.0


def test_the_veto_branch_still_measures_correlation():
    """Making it lazy must not make it absent where it is actually used."""
    reply = voice(3.0, amplitude=0.4)
    guard = _guard_with_known_room(20.0, reply)

    verdict = guard.assess(_double_talk(reply, 20.0, 0.25), 10.0, SR)
    assert verdict.reason == "double_talk"
    assert verdict.correlation > 0.0, "correlation not measured where it matters"


def test_both_transports_carry_the_speakerphone_path():
    """main_pcm.py is generated. A barge-in path present in one file and not
    the other would mean speakerphone callers on that transport silently
    keep the old half-duplex behaviour."""
    for path in ("main.py", "main_pcm.py"):
        src = Path(path).read_text(encoding="utf-8")
        assert "_check_barge_in" in src, f"{path} has no barge-in check"
        assert "session.barge_in()" in src, f"{path} never opens the gate early"
        assert '"_stop_audio"' in src, f"{path} never stops client playback"
        assert 'msg.get("type") == "audio_mode"' in src, f"{path} ignores the path hint"


def test_pcm_variant_does_not_call_the_deleted_webm_decoder():
    """tools/make_pcm_variant.py deletes _decode_to_wav from the PCM file.
    A transport-specific helper that still called it would raise NameError
    on the first barge-in check -- which compiles cleanly and only fails in
    production."""
    src = Path("main_pcm.py").read_text(encoding="utf-8")

    # A CALL, not a mention: the TAIL_READ_IS_CHEAP comment names
    # _decode_to_wav to explain why WebM cannot poll fast, and that comment is
    # copied into the generated file.
    assert "await _decode_to_wav(" not in src, "PCM variant calls the deleted decoder"
    assert "def _decode_to_wav" not in src, "PCM variant should not define it either"
    assert "session.audio.tail_tensor" in src


def test_clients_no_longer_mute_the_microphone_during_playback():
    """The mute is what made barge-in impossible. If it comes back, every
    server-side test above still passes and the feature is silently dead."""
    for path in ("static/index.html", "static/pcm/index.html"):
        src = Path(path).read_text(encoding="utf-8")
        assert "setMicMuted(true)" not in src, f"{path} still mutes during playback"
        assert "_stop_audio" in src, f"{path} cannot be interrupted by the server"
        assert "autoGainControl: false" in src, f"{path} lost the AGC setting"
