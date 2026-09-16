"""Tests for edge conditioning, the audio-quality floor, and the retry ladder.

WHAT THESE COVER
----------------
Everything in the noisy-environment requirement that can be settled on a
laptop: the DSP maths, the threshold DECISION, the two-rung ladder, and
the bucketed accounting.

WHAT THEY DELIBERATELY DO NOT COVER
-----------------------------------
Whether the thresholds are set to the RIGHT numbers, and whether
suppression actually improves IndicConformer's transcripts. Both need real
Bengali speech, real background noise and the GPU, and both are PENDING.
A test here that asserted "8dB is the correct floor" would be asserting an
assumption, not a fact -- so these assert the SHAPE of the behaviour
(ordering, monotonicity, clamping, routing) which is what code-level tests
can actually establish.

    python -m pytest tests/ -v          (from the repo root)

Synthetic signals only: speech_like() bursts stand in for the caller,
gaussian noise for the room, and the mixtures are built so the expected
ordering is arithmetic rather than perceptual. See speech_like's docstring
for why a plain tone is the wrong stimulus for the suppressor tests.
"""
from __future__ import annotations

import asyncio
import itertools
import os
import sys
import types

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import audio_quality as aq  # noqa: E402
from agent import language as lang_mod  # noqa: E402
from agent.quality_metrics import (  # noqa: E402
    ACTION_CLARIFY, ACTION_KEYPAD, QualityMetrics, TurnFailureTracker,
)

SR = 16000
# No module-level generator: see hiss(). A shared one made signal content
# depend on test order, which is what made this suite flaky.


# ---------------------------------------------------------------------------
# signal builders
# ---------------------------------------------------------------------------
def tone(seconds: float, amplitude: float = 0.3, freq: float = 220.0) -> np.ndarray:
    t = np.arange(int(seconds * SR), dtype=np.float32) / SR
    return (np.sin(2 * np.pi * freq * t) * amplitude).astype(np.float32)


def hiss(seconds: float, amplitude: float = 0.02) -> np.ndarray:
    """Seeded FROM THE ARGUMENTS, not from a shared generator.

    The first version drew from one module-level rng. That makes every
    test's noise depend on how many tests ran before it, so a borderline
    assertion passes or fails according to collection order -- and the suite
    was intermittently red at roughly one run in ten because of it. Deriving
    the seed from the arguments makes each signal a pure function of its own
    inputs, and the order of tests irrelevant."""
    seed = (int(seconds * 1000), int(amplitude * 1_000_000))
    return (np.random.default_rng(seed).standard_normal(int(seconds * SR))
            * amplitude).astype(np.float32)


def speech_like(seconds: float, amplitude: float = 0.3,
                gap_ratio: float = 0.3) -> np.ndarray:
    """Syllable-like bursts at varying pitch, separated by short gaps.

    A steady sine is NOT a usable stand-in for speech in this file, and the
    reason is the mechanism under test: spectral gating removes whatever is
    STATIONARY, and a constant tone is stationary -- it occupies the same
    bin in every frame, so its own energy lands in that bin's low percentile
    and is subtracted as if it were a fan. A first attempt at these tests
    used `tone()` and measured the suppressor removing "speech" 14dB harder
    than noise, which is correct behaviour on the wrong stimulus.

    Real speech has the two properties this builds in: pitch and energy that
    MOVE from frame to frame, and quiet moments between words -- which is
    also what the percentile SNR estimate reads as its noise floor.
    """
    syllable_s = 0.18
    out: list[np.ndarray] = []
    elapsed = 0.0
    i = 0
    while elapsed < seconds:
        pitch = 140.0 + 60.0 * ((i * 7) % 5)          # deterministic wobble
        out.append(tone(syllable_s, amplitude, pitch))
        gap = syllable_s * gap_ratio
        if gap > 0:
            out.append(np.zeros(int(gap * SR), dtype=np.float32))
        elapsed += syllable_s + gap
        i += 1
    return np.concatenate(out)[: int(seconds * SR)].astype(np.float32)


def utterance(speech_s: float = 1.0, gap_s: float = 0.5, speech_amp: float = 0.3,
              noise_amp: float = 0.005) -> np.ndarray:
    """A realistic clip shape: quiet room, speech, quiet room. The gaps are
    what the SNR estimate reads as the noise floor."""
    parts = [hiss(gap_s, noise_amp), tone(speech_s, speech_amp), hiss(gap_s, noise_amp)]
    clip = np.concatenate(parts)
    return (clip + hiss(len(clip) / SR, noise_amp)[: clip.size]).astype(np.float32)


def noisy_utterance(noise_amp: float) -> np.ndarray:
    """Same utterance, louder room. Only the background changes."""
    return utterance(noise_amp=noise_amp)


# ===========================================================================
# A. NOISE PROCESSING
# ===========================================================================
def test_suppression_lowers_the_noise_floor_of_a_noise_only_clip():
    """The whole point: steady background gets quieter."""
    noisy = hiss(1.0, 0.05)
    cleaned = aq.suppress_noise(noisy, SR)
    assert aq.rms_dbfs(cleaned) < aq.rms_dbfs(noisy) - 3.0


def test_suppression_preserves_speech_far_better_than_it_preserves_noise():
    """Attenuation must be SELECTIVE. A gate that quietens everything
    equally would pass the test above while destroying the signal, so this
    compares the two attenuations against each other.

    Note the stimulus: speech_like(), not tone(). See its docstring -- a
    steady tone is stationary and this suppressor is SUPPOSED to remove
    stationary content."""
    speech = speech_like(1.0, 0.3)
    noise = hiss(1.0, 0.05)

    speech_loss = aq.rms_dbfs(speech) - aq.rms_dbfs(aq.suppress_noise(speech, SR))
    noise_loss = aq.rms_dbfs(noise) - aq.rms_dbfs(aq.suppress_noise(noise, SR))

    assert noise_loss > speech_loss + 3.0


def test_suppression_cannot_remove_a_steady_tone_and_that_is_by_design():
    """Pinning the documented limitation rather than hiding it: anything
    stationary is treated as room noise, a held tone included. A passing
    shout or a door slam is the opposite case and survives untouched.
    Neither is a bug; both are consequences of estimating noise as the low
    percentile of each bin over time."""
    steady = tone(1.0, 0.3)
    loss = aq.rms_dbfs(steady) - aq.rms_dbfs(aq.suppress_noise(steady, SR))
    assert loss > 6.0


def test_suppression_improves_measured_snr_of_a_noisy_utterance():
    noisy = noisy_utterance(noise_amp=0.05)
    before = aq.assess(noisy, SR).snr_db
    after = aq.assess(aq.suppress_noise(noisy, SR), SR).snr_db
    assert after > before


def test_suppression_preserves_length_and_dtype():
    clip = utterance()
    out = aq.suppress_noise(clip, SR)
    assert out.shape == clip.shape
    assert out.dtype == np.float32


def test_suppression_is_a_noop_on_a_clip_too_short_to_frame():
    """Nothing to estimate a noise spectrum from -- must pass through
    rather than raise or return garbage."""
    tiny = tone(0.01)
    assert np.array_equal(aq.suppress_noise(tiny, SR), tiny)


def test_suppression_can_be_disabled_by_config():
    cfg = aq.QualityConfig(denoise_enabled=False)
    clip = hiss(1.0, 0.05)
    assert np.array_equal(aq.suppress_noise(clip, SR, cfg), clip)


def test_suppression_does_not_introduce_clipping():
    loud = np.clip(tone(1.0, 0.95) + hiss(1.0, 0.05), -1, 1).astype(np.float32)
    out = aq.suppress_noise(loud, SR)
    assert np.max(np.abs(out)) <= 1.0 + 1e-6


# ===========================================================================
# B. LEVEL NORMALISATION
# ===========================================================================
def test_normalisation_brings_a_quiet_clip_up_to_target():
    # 0.05 amplitude is ~-29 dBFS, so reaching -20 needs ~+9dB -- inside the
    # 20dB cap. A quieter clip would be CLAMPED and land short of target,
    # which is correct behaviour with its own test below, not this one.
    quiet = tone(1.0, 0.05)
    out, gain_db = aq.normalize_level(quiet)
    assert gain_db > 0
    assert aq.rms_dbfs(out) == pytest.approx(aq.CONFIG.target_rms_dbfs, abs=1.0)


def test_normalisation_brings_a_loud_clip_down_to_target():
    loud = tone(1.0, 0.5)
    out, gain_db = aq.normalize_level(loud)
    assert gain_db < 0
    assert aq.rms_dbfs(out) == pytest.approx(aq.CONFIG.target_rms_dbfs, abs=1.0)


def test_normalisation_never_clips():
    """The guard that matters most: clipping is unrecoverable distortion,
    where quietness is not."""
    for amp in (0.001, 0.01, 0.1, 0.5, 0.9, 0.999):
        out, _ = aq.normalize_level(tone(0.5, amp))
        assert np.max(np.abs(out)) <= 1.0, f"clipped at amplitude {amp}"


def test_normalisation_respects_the_peak_ceiling():
    """A clip whose RMS is far below target but whose peaks are already near
    full scale must be held at the ceiling, not lifted to the RMS target."""
    spiky = np.zeros(SR, dtype=np.float32)
    spiky[::100] = 0.98                      # sparse peaks, very low RMS
    out, _ = aq.normalize_level(spiky)
    ceiling = 10.0 ** (aq.CONFIG.peak_ceiling_dbfs / 20.0)
    assert np.max(np.abs(out)) <= ceiling + 1e-6


def test_normalisation_gain_is_capped_for_near_silence():
    """Unbounded gain would amplify the room and make a hopeless clip look
    well-levelled, defeating the floor that just measured it."""
    almost_silent = tone(1.0, 1e-5)
    _, gain_db = aq.normalize_level(almost_silent)
    assert gain_db <= aq.CONFIG.max_gain_db + 1e-6


def test_normalisation_attenuation_is_capped():
    cfg = aq.QualityConfig(max_attenuation_db=6.0, peak_ceiling_dbfs=0.0)
    _, gain_db = aq.normalize_level(tone(1.0, 0.9), cfg)
    assert gain_db >= -6.0 - 1e-6


def test_normalisation_leaves_digital_silence_alone():
    silence = np.zeros(SR, dtype=np.float32)
    out, gain_db = aq.normalize_level(silence)
    assert gain_db == 0.0
    assert not np.any(out)


def test_normalisation_can_be_disabled_by_config():
    cfg = aq.QualityConfig(normalize_enabled=False)
    clip = tone(1.0, 0.01)
    out, gain_db = aq.normalize_level(clip, cfg)
    assert gain_db == 0.0
    assert np.array_equal(out, clip)


def test_target_level_is_configurable():
    cfg = aq.QualityConfig(target_rms_dbfs=-25.0)
    out, _ = aq.normalize_level(tone(1.0, 0.05), cfg)
    assert aq.rms_dbfs(out) == pytest.approx(-25.0, abs=1.0)


# ===========================================================================
# C. AUDIO-QUALITY CALCULATION
# ===========================================================================
def test_snr_is_higher_in_a_quiet_room_than_a_noisy_one():
    """The core property. Everything downstream depends on this ordering
    holding, and nothing else about the absolute values matters here."""
    quiet = aq.assess(noisy_utterance(0.002), SR).snr_db
    loud = aq.assess(noisy_utterance(0.08), SR).snr_db
    assert quiet > loud


def test_snr_decreases_monotonically_as_the_room_gets_louder():
    snrs = [aq.assess(noisy_utterance(a), SR).snr_db
            for a in (0.002, 0.01, 0.04, 0.10)]
    assert snrs == sorted(snrs, reverse=True), snrs


def test_pure_noise_scores_near_zero_snr():
    """No speech means no spread between the percentiles."""
    assert aq.assess(hiss(2.0, 0.05), SR).snr_db < 6.0


def test_digital_silence_is_not_usable():
    q = aq.assess(np.zeros(SR, dtype=np.float32), SR)
    assert not q.usable
    assert "too_quiet" in q.reasons


def test_empty_clip_is_not_usable_and_does_not_raise():
    q = aq.assess(np.zeros(0, dtype=np.float32), SR)
    assert not q.usable
    assert q.reasons == ("empty",)


def test_clipping_is_detected():
    railed = np.ones(SR, dtype=np.float32)          # 100% at full scale
    q = aq.assess(railed, SR)
    assert q.clipping_ratio == pytest.approx(1.0)
    assert "clipped" in q.reasons


def test_short_clip_is_rejected_as_too_short():
    q = aq.assess(tone(0.1), SR)
    assert "too_short" in q.reasons
    assert not q.usable


def test_speech_ratio_is_higher_for_a_clip_that_is_mostly_speech():
    talkative = np.concatenate([hiss(0.2, 0.005), speech_like(1.6, 0.3), hiss(0.2, 0.005)])
    sparse = np.concatenate([hiss(1.5, 0.005), speech_like(0.4, 0.3), hiss(1.5, 0.005)])
    assert aq.assess(talkative, SR).speech_ratio > aq.assess(sparse, SR).speech_ratio


def test_unbroken_speech_with_no_pauses_collapses_the_snr_estimate():
    """A REAL LIMITATION, pinned so it cannot be discovered in production.

    The SNR estimate reads its noise floor from the clip's own quiet
    frames. When speech fills essentially every frame there are no quiet
    frames, the 10th and 90th percentiles both land inside the speech, and
    the estimate collapses toward zero -- so a perfectly clean clip can be
    scored as unusable.

    Natural speech is safe here: stop closures and inter-word gaps supply
    the quiet frames, and the turn detector's 0.15s pad adds more at both
    ends. A held vowel with no pauses is the pathological case.

    Whether real Bengali utterance clips ever get close to this is a
    question about real speech statistics, and it is on the PENDING
    GPU/real-audio list. Do not "fix" this by widening the percentiles
    without that data -- the current values are the standard ones.
    """
    unbroken = tone(2.0, 0.3)          # no gaps at all
    assert aq.assess(unbroken, SR).snr_db < 3.0


def test_quality_reports_duration_and_level():
    q = aq.assess(tone(1.5, 0.1), SR)
    assert q.duration_s == pytest.approx(1.5, abs=0.01)
    assert -30 < q.rms_dbfs < -10


def test_as_dict_is_json_safe():
    d = aq.assess(utterance(), SR).as_dict()
    assert set(d) >= {"snr_db", "speech_ratio", "clipping_ratio", "usable", "bucket"}
    assert isinstance(d["reasons"], list)


# ===========================================================================
# D. THE THRESHOLD DECISION
# ===========================================================================
def test_clean_utterance_passes_the_floor():
    assert aq.assess(utterance(noise_amp=0.001), SR).usable


def test_very_noisy_utterance_fails_the_floor():
    q = aq.assess(noisy_utterance(0.15), SR)
    assert not q.usable
    assert "low_snr" in q.reasons


def test_the_floor_is_configurable_in_both_directions():
    """Same audio, opposite verdicts -- proving the decision is driven by
    the config value and not by anything baked into assess()."""
    clip = noisy_utterance(0.03)
    lenient = aq.assess(clip, SR, aq.QualityConfig(min_snr_db=0.0))
    strict = aq.assess(clip, SR, aq.QualityConfig(min_snr_db=60.0))
    assert lenient.usable
    assert not strict.usable
    assert "low_snr" in strict.reasons


def test_config_reads_thresholds_from_environment(monkeypatch):
    monkeypatch.setenv("VOICE_AGENT_MIN_SNR_DB", "3.5")
    monkeypatch.setenv("VOICE_AGENT_MAX_CLARIFY_RETRIES", "4")
    monkeypatch.setenv("VOICE_AGENT_DENOISE", "off")
    cfg = aq.QualityConfig.from_env()
    assert cfg.min_snr_db == 3.5
    assert cfg.max_clarify_retries == 4
    assert cfg.denoise_enabled is False


def test_unparseable_env_value_falls_back_to_the_default(monkeypatch):
    """A typo in a deploy script must not take the floor to 0 silently."""
    monkeypatch.setenv("VOICE_AGENT_MIN_SNR_DB", "not-a-number")
    assert aq.QualityConfig.from_env().min_snr_db == 8.0


def test_reasons_accumulate_rather_than_short_circuiting():
    """Operationally useful: one log line naming every reason the clip
    failed, not just the first."""
    q = aq.assess(np.ones(1000, dtype=np.float32), SR)   # short AND railed
    assert "too_short" in q.reasons and "clipped" in q.reasons


# ===========================================================================
# E. THE ORDER OF OPERATIONS
# ===========================================================================
def test_quality_is_measured_before_processing_not_after():
    """The most important structural test in this file.

    If quality were measured on the CONDITIONED audio, suppression and
    normalisation would raise the score by construction and a clip that
    arrived unusable would be acted on confidently -- the exact failure
    this whole change exists to prevent. So the reported quality must match
    an assessment of the RAW input, not of the output."""
    noisy = noisy_utterance(0.05)
    result = aq.condition(noisy, SR)

    assert result.quality.snr_db == pytest.approx(aq.assess(noisy, SR).snr_db)
    assert result.quality.snr_db < aq.assess(result.audio, SR).snr_db


def test_condition_returns_normalised_audio_and_the_raw_verdict():
    result = aq.condition(utterance(speech_amp=0.02), SR)
    assert aq.rms_dbfs(result.audio) == pytest.approx(aq.CONFIG.target_rms_dbfs, abs=2.0)
    assert isinstance(result.quality, aq.AudioQuality)


def test_condition_wav_file_round_trip(tmp_path):
    """The path main.py actually calls: read, condition, write back in place."""
    sf = pytest.importorskip("soundfile")
    path = str(tmp_path / "utt1.wav")
    sf.write(path, utterance(speech_amp=0.02), SR, subtype="PCM_16")

    result = aq.condition_wav_file(path)

    written, sr = sf.read(path, dtype="float32")
    assert sr == SR
    assert aq.rms_dbfs(written) == pytest.approx(aq.CONFIG.target_rms_dbfs, abs=3.0)
    assert result.quality.duration_s == pytest.approx(2.0, abs=0.05)


# ===========================================================================
# F. THE RETRY LADDER
# ===========================================================================
def test_first_failed_turn_asks_for_clarification():
    assert TurnFailureTracker().record_failure() == ACTION_CLARIFY


def test_second_consecutive_failed_turn_offers_the_keypad():
    t = TurnFailureTracker()
    assert t.record_failure() == ACTION_CLARIFY
    assert t.record_failure() == ACTION_KEYPAD
    assert t.keypad_offered


def test_further_failures_stay_on_the_keypad():
    """A caller who has failed three times is not helped by a fourth
    question -- the ladder must not cycle back to clarification."""
    t = TurnFailureTracker()
    t.record_failure()
    assert [t.record_failure() for _ in range(3)] == [ACTION_KEYPAD] * 3


def test_a_successful_turn_resets_the_counter():
    t = TurnFailureTracker()
    t.record_failure()
    t.record_success()
    assert t.consecutive_failures == 0
    assert t.record_failure() == ACTION_CLARIFY   # back to rung 1, not keypad


def test_success_clears_the_keypad_latch():
    t = TurnFailureTracker()
    t.record_failure()
    t.record_failure()
    assert t.keypad_offered
    t.record_success()
    assert not t.keypad_offered


def test_failures_must_be_consecutive_to_escalate():
    """Two isolated bad moments in a long call are an ordinary
    conversation, not a caller who cannot be heard."""
    t = TurnFailureTracker()
    for _ in range(5):
        assert t.record_failure() == ACTION_CLARIFY
        t.record_success()
    assert t.total_failures == 5          # history is kept...
    assert t.consecutive_failures == 0    # ...but never escalates


def test_retry_limit_is_configurable():
    t = TurnFailureTracker(aq.QualityConfig(max_clarify_retries=3))
    assert t.record_failure() == ACTION_CLARIFY
    assert t.record_failure() == ACTION_CLARIFY
    assert t.record_failure() == ACTION_KEYPAD


# ===========================================================================
# G. NOISY-BUCKET CLASSIFICATION AND METRICS
# ===========================================================================
def make_quality(snr_db: float, usable: bool = True) -> aq.AudioQuality:
    return aq.AudioQuality(snr_db=snr_db, speech_ratio=0.5, clipping_ratio=0.0,
                           rms_dbfs=-20.0, duration_s=1.0, usable=usable)


def test_bucket_boundary_uses_the_configured_noisy_threshold():
    assert make_quality(aq.CONFIG.noisy_snr_db - 1).noisy
    assert make_quality(aq.CONFIG.noisy_snr_db).noisy          # inclusive
    assert not make_quality(aq.CONFIG.noisy_snr_db + 1).noisy


def test_a_turn_can_be_noisy_and_still_usable():
    """The band between min_snr_db and noisy_snr_db is the population the
    noisy-bucket accuracy is actually about -- if nothing landed there the
    metric would only ever describe rejected turns."""
    q = make_quality(aq.CONFIG.min_snr_db + 1, usable=True)
    assert q.noisy and q.usable


def test_metrics_keep_the_buckets_separate():
    m = QualityMetrics()
    m.record_turn(make_quality(30.0), success=True)    # clean, ok
    m.record_turn(make_quality(30.0), success=True)    # clean, ok
    m.record_turn(make_quality(5.0), success=True)     # noisy, ok
    m.record_turn(make_quality(5.0), success=False)    # noisy, failed

    snap = m.snapshot()
    assert snap["overall_turns"] == 4
    assert snap["clean_bucket"]["turns"] == 2
    assert snap["clean_bucket"]["accuracy"] == 1.0
    assert snap["noisy_bucket"]["turns"] == 2
    assert snap["noisy_bucket"]["successful_turns"] == 1
    assert snap["noisy_bucket"]["accuracy"] == 0.5


def test_noisy_accuracy_is_not_diluted_by_clean_traffic():
    """The failure mode this split exists to prevent: a headline number
    that improves because quiet traffic grew, while the noisy callers it
    is meant to describe got no better at all."""
    m = QualityMetrics()
    for _ in range(98):
        m.record_turn(make_quality(30.0), success=True)
    m.record_turn(make_quality(4.0), success=False)
    m.record_turn(make_quality(4.0), success=False)

    snap = m.snapshot()
    assert snap["overall_accuracy"] == 0.98      # flattering
    assert snap["noisy_bucket"]["accuracy"] == 0.0   # and honest


def test_empty_bucket_reports_none_not_zero():
    """Zero would read as "we tried and failed every time"; None is the
    truth before any noisy caller has rung."""
    snap = QualityMetrics().snapshot()
    assert snap["noisy_bucket"]["accuracy"] is None
    assert snap["overall_accuracy"] is None


def test_metrics_count_rejections_clarifications_and_keypad():
    m = QualityMetrics()
    m.record_turn(make_quality(4.0, usable=False), success=False)
    m.record_clarification()
    m.record_keypad_offer()
    m.record_keypad_entry()

    snap = m.snapshot()
    assert snap["rejected_low_quality"] == 1
    assert snap["clarifications"] == 1
    assert snap["keypad_offers"] == 1
    assert snap["keypad_entries"] == 1


def test_snapshot_reports_every_field_the_requirement_asks_for():
    snap = QualityMetrics().snapshot()
    assert snap["overall_turns"] == 0
    assert "turns" in snap["noisy_bucket"]
    assert "successful_turns" in snap["noisy_bucket"]
    assert "accuracy" in snap["noisy_bucket"]
    assert "clarifications" in snap
    assert "keypad_offers" in snap
    assert snap["thresholds"]["min_snr_db"] == aq.CONFIG.min_snr_db


def test_mean_snr_is_tracked_per_bucket():
    m = QualityMetrics()
    m.record_turn(make_quality(4.0), success=True)
    m.record_turn(make_quality(6.0), success=True)
    assert m.snapshot()["noisy_bucket"]["mean_snr_db"] == pytest.approx(5.0)


# ===========================================================================
# H. END-TO-END ROUTING THROUGH _dispatch_turn
#
# The wiring, not the DSP: a clip that fails the floor must reach the
# clarification path and must NEVER reach ASR. Stubs stand in for the GPU
# model and for TTS -- see tests/test_noisy_turn_boundaries.py for the same
# approach.
# ===========================================================================
def _install_stubs() -> None:
    for name in (
        "nemo", "nemo.collections", "nemo.collections.asr",
        "nemo.collections.asr.parts", "nemo.collections.asr.parts.submodules",
        "nemo.collections.asr.parts.submodules.rnnt_decoding",
    ):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["nemo.collections.asr"].models = types.SimpleNamespace(
        ASRModel=types.SimpleNamespace(restore_from=lambda **kw: None),
    )
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


class FakeSession:
    """Only what _dispatch_turn touches."""

    def __init__(self):
        self.call_id = "test"
        self.dispatch_lock = asyncio.Lock()
        self.pending = None
        self.failures = TurnFailureTracker()
        self.spoken: list[str] = []
        # _dispatch_turn now asks the session which language to answer in
        # (agent/language.py). The default is what a real CallSession gets.
        self.lang = lang_mod.default_lang()

        # Speakerphone support: _dispatch_turn asks every turn which path
        # bucket to file the result under, and whether a barge-in left a
        # reference to subtract. A real EchoGuard with no playback recorded
        # answers "handset" and "nothing to subtract", which is what these
        # tests want -- none of them involves the agent speaking.
        from agent.echo_guard import EchoGuard

        self.echo = EchoGuard()
        self.take_echo_reference = lambda: None
        self.json_frames: list[tuple[str, str]] = []

    async def send_json(self, sender, text):
        self.json_frames.append((sender, text))


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """A dispatch environment with the GPU and TTS replaced, and process
    metrics reset so each test counts only its own turns."""
    app.METRICS.reset()

    spoken: list[str] = []

    async def fake_speak(session, text_bn, fallback_reason=None):
        spoken.append(text_bn)
        session.spoken.append(text_bn)

    asr_calls: list[str] = []

    class FakeASR:
        text = "পরীক্ষার রেট কত"

        async def transcribe_utterance(self, path):
            asr_calls.append(path)
            return types.SimpleNamespace(text=self.text, decoder_used="rnnt",
                                         decoder_agreement=1.0)

    monkeypatch.setattr(app, "_speak", fake_speak)
    monkeypatch.setattr(app, "_asr", FakeASR())
    # Stop the turn after transcription -- intent resolution is a different
    # subsystem with its own tests and needs Ollama.
    monkeypatch.setattr(app, "_continue_pending", lambda session, text: _true())

    # A plain counter, NOT id(samples) and NOT len(asr_calls).
    #
    # The first version of this used both, and was intermittently flaky:
    # CPython reuses the id of a temporary numpy array once it is collected
    # (five throwaway arrays here yield two distinct ids), and len(asr_calls)
    # does not advance for a turn the quality floor rejected. Two clips in
    # one test could therefore land on the same filename, and _dispatch_turn
    # deletes the clip it consumed -- so the second write raced the first
    # delete and the test failed with "Error opening ...wav".
    clip_seq = itertools.count()

    def write_clip(samples):
        sf = pytest.importorskip("soundfile")
        path = str(tmp_path / f"utt{next(clip_seq)}.wav")
        sf.write(path, samples, SR, subtype="PCM_16")
        return path

    return types.SimpleNamespace(spoken=spoken, asr_calls=asr_calls,
                                 write_clip=write_clip, asr_cls=FakeASR)


async def _true():
    return True


def test_low_quality_clip_never_reaches_asr(wired):
    """The requirement in one assertion: below the floor the agent does not
    answer confidently, because it never even asks the model."""
    session = FakeSession()
    clip = wired.write_clip(hiss(1.5, 0.2))          # pure loud noise

    asyncio.run(app._dispatch_turn(session, clip))

    assert wired.asr_calls == []
    assert wired.spoken == [app.CLARIFY_PROMPT_BN]


def test_low_quality_clip_deletes_its_temp_file(wired):
    session = FakeSession()
    clip = wired.write_clip(hiss(1.5, 0.2))
    asyncio.run(app._dispatch_turn(session, clip))
    assert not os.path.exists(clip)


def test_good_clip_reaches_asr_and_resets_the_ladder(wired):
    session = FakeSession()
    session.failures.record_failure()                # one prior failure
    clip = wired.write_clip(utterance(speech_amp=0.3, noise_amp=0.001))

    asyncio.run(app._dispatch_turn(session, clip))

    assert len(wired.asr_calls) == 1
    assert session.failures.consecutive_failures == 0


def test_two_consecutive_low_quality_turns_offer_the_keypad(wired):
    session = FakeSession()

    asyncio.run(app._dispatch_turn(session, wired.write_clip(hiss(1.5, 0.2))))
    asyncio.run(app._dispatch_turn(session, wired.write_clip(hiss(1.5, 0.2))))

    assert wired.spoken == [app.CLARIFY_PROMPT_BN, app.KEYPAD_PROMPT_BN]
    assert ("_keypad", "on") in session.json_frames
    assert app.METRICS.snapshot()["keypad_offers"] == 1


def test_a_good_turn_between_two_bad_ones_prevents_the_keypad(wired):
    session = FakeSession()
    good = utterance(speech_amp=0.3, noise_amp=0.001)

    asyncio.run(app._dispatch_turn(session, wired.write_clip(hiss(1.5, 0.2))))
    asyncio.run(app._dispatch_turn(session, wired.write_clip(good)))
    asyncio.run(app._dispatch_turn(session, wired.write_clip(hiss(1.5, 0.2))))

    assert wired.spoken == [app.CLARIFY_PROMPT_BN, app.CLARIFY_PROMPT_BN]
    assert app.METRICS.snapshot()["keypad_offers"] == 0


def test_empty_asr_on_a_good_clip_also_counts_as_a_failed_turn(wired, monkeypatch):
    """A clip can pass the floor and still yield nothing. The caller was
    not understood either way, so it belongs on the same ladder."""
    wired.asr_cls.text = ""
    try:
        session = FakeSession()
        clip = wired.write_clip(utterance(speech_amp=0.3, noise_amp=0.001))
        asyncio.run(app._dispatch_turn(session, clip))

        assert wired.spoken == [app.CLARIFY_PROMPT_BN]
        assert session.failures.consecutive_failures == 1
    finally:
        wired.asr_cls.text = "পরীক্ষার রেট কত"


def test_rejected_turn_is_recorded_in_the_noisy_bucket(wired):
    session = FakeSession()
    asyncio.run(app._dispatch_turn(session, wired.write_clip(hiss(1.5, 0.2))))

    snap = app.METRICS.snapshot()
    assert snap["noisy_bucket"]["turns"] == 1
    assert snap["noisy_bucket"]["successful_turns"] == 0
    assert snap["rejected_low_quality"] == 1
    assert snap["clarifications"] == 1


def test_keypad_digit_enters_the_normal_text_path(wired):
    session = FakeSession()
    asyncio.run(app._handle_keypad_digit(session, "1"))

    assert ("User", app.KEYPAD_MENU_BN["1"]) in session.json_frames
    assert wired.asr_calls == []            # no audio involved
    assert app.METRICS.snapshot()["keypad_entries"] == 1


def test_keypad_digit_resets_the_failure_ladder(wired):
    session = FakeSession()
    session.failures.record_failure()
    session.failures.record_failure()

    asyncio.run(app._handle_keypad_digit(session, "2"))

    assert session.failures.consecutive_failures == 0
    assert not session.failures.keypad_offered


def test_unmapped_keypad_digit_is_ignored(wired):
    session = FakeSession()
    asyncio.run(app._handle_keypad_digit(session, "9"))
    assert session.json_frames == []
    assert app.METRICS.snapshot()["keypad_entries"] == 0


def test_conditioning_failure_falls_open_to_asr(wired, monkeypatch):
    """A bug in the conditioner must not turn every turn into "say again".
    Failing open restores exactly the behaviour that shipped before this
    stage existed."""
    def boom(path):
        raise RuntimeError("simulated conditioner bug")

    monkeypatch.setattr(app, "condition_wav_file", boom)
    session = FakeSession()
    asyncio.run(app._dispatch_turn(session, wired.write_clip(utterance())))

    assert len(wired.asr_calls) == 1
    assert wired.spoken == []


def test_both_transports_carry_the_quality_gate():
    """main_pcm.py is generated from main.py. If the gate were added to one
    and not regenerated into the other, PCM callers would silently keep the
    old confident-on-noise behaviour."""
    import io
    for path in ("main.py", "main_pcm.py"):
        src = io.open(path, encoding="utf-8").read()
        assert "condition_wav_file" in src, f"{path} does not condition the clip"
        assert "_clarify_or_offer_keypad" in src, f"{path} has no clarification ladder"
        assert 'msg.get("type") == "dtmf"' in src, f"{path} does not accept keypad input"
