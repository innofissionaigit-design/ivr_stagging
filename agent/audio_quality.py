"""Edge audio conditioning and the measurable audio-quality floor.

WHAT THIS IS FOR
----------------
The noisy-caller work so far (see noisy_environment_implementation.md)
fixed WHERE the ASR clip is cut. It did nothing about WHAT is inside the
clip, and it left the agent with no way to know it had been handed
something unusable: a transcript came back, and the turn proceeded as if
the caller had been understood. A garbled transcript acted on confidently
is worse than an admitted failure -- it books the wrong slot, quotes the
wrong test, and the caller only finds out later.

This module supplies the three things that were missing:

  1. NOISE SUPPRESSION -- spectral gating, so the stationary part of the
     room (fan, traffic hum, air-conditioning) is attenuated before ASR
     sees it. The browser already asks for its own WebRTC suppressor at
     capture time; this is the second stage, on the server, where it can
     be measured and tested.

  2. LEVEL NORMALISATION -- one deterministic RMS gain per clip, so a
     quiet caller in a loud room arrives at the level IndicConformer
     expects rather than at whatever the microphone produced. This is
     deliberately done HERE and not by the browser's autoGainControl:
     AGC adapts continuously and raises the noise floor during pauses,
     which works directly against the turn detector. One gain applied to
     an already-segmented clip cannot do that.

  3. A QUALITY FLOOR -- a number, computed on CPU, that says whether the
     clip is worth trusting. Below it the caller has NOT been understood
     and the agent must say so instead of guessing.

NO GPU, NO NEW DEPENDENCIES
---------------------------
Everything here is numpy over a float32 array, plus soundfile for the
file round-trip -- both already pinned in requirements.txt. No model, no
torch, no CUDA. That is a requirement, not an accident: the quality floor
has to be computable BEFORE deciding whether to spend a GPU inference on
the clip, and it has to be unit-testable on a laptop.

ORDER OF OPERATIONS (this matters)
----------------------------------
Quality is measured on the RAW clip, before suppression and before
normalisation. Measuring after would be measuring this module's own
output: spectral gating raises the apparent SNR by construction, and
normalisation moves the level to the target by construction, so a clip
that arrived unusable would score as clean and then be acted on
confidently. The metric has to describe the room the caller is standing
in, not the room after we have tidied it.

So: assess(raw) -> suppress -> normalise -> ASR.
"""
from __future__ import annotations

import dataclasses
import logging
import os

import numpy as np

logger = logging.getLogger("audio_quality")

_EPS = 1e-10
_SILENCE_DBFS = -120.0        # what digital silence reports instead of -inf
_CLIP_LEVEL = 0.99            # abs(sample) at or above this counts as clipped


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number -- using default %s", name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, float(default)))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclasses.dataclass(frozen=True)
class QualityConfig:
    """Every tunable in one place, every one overridable by environment
    variable, because NONE of these defaults has been validated against
    real noisy Bengali audio yet -- that is GPU work and is pending. They
    are reasoned starting points, chosen to be safe in the direction that
    matters: an over-eager clarification costs one extra question, an
    under-eager one books the wrong appointment.
    """

    # --- 1. noise suppression -------------------------------------------
    denoise_enabled: bool = True
    frame_len: int = 512              # 32ms @ 16k -- long enough to resolve a
                                      # hum, short enough not to smear a plosive
    hop: int = 128                    # 4x overlap
    noise_percentile: float = 15.0    # per-bin noise estimate = this percentile
                                      # over time. Speech is intermittent, room
                                      # noise is not, so the low percentile of
                                      # each bin IS the room.
    oversubtraction: float = 1.5      # subtract slightly more than the estimate;
                                      # the percentile is a floor, not a mean
    spectral_floor: float = 0.08      # never gate a bin below this fraction of
                                      # its own magnitude. Gating to zero makes
                                      # "musical noise" -- isolated tonal blips
                                      # that ASR reads as onsets, which is worse
                                      # than the hum they replaced.

    # --- 2. level normalisation -----------------------------------------
    normalize_enabled: bool = True
    target_rms_dbfs: float = -20.0    # conventional speech operating level
    max_gain_db: float = 20.0         # cap on amplification: past this a quiet
                                      # clip is mostly amplified INTO its own
                                      # noise floor
    max_attenuation_db: float = 12.0  # cap the other way, so a loud clip is
                                      # tamed rather than flattened
    peak_ceiling_dbfs: float = -1.0   # after gain, back off if any peak would
                                      # exceed this -- clipping is destructive
                                      # in a way quietness is not

    # --- 3. quality floor -----------------------------------------------
    min_snr_db: float = 8.0           # below this the caller is NOT understood
    min_speech_ratio: float = 0.12    # a clip that is 95% background is not an
                                      # utterance regardless of its SNR
    max_clipping_ratio: float = 0.02  # 2% of samples railed = distorted input
    min_duration_s: float = 0.30      # shorter than one syllable
    min_rms_dbfs: float = -50.0       # effectively nothing reached the mic

    # --- 4. bucketing ----------------------------------------------------
    noisy_snr_db: float = 15.0        # AT OR BELOW this a turn is filed in the
                                      # noisy bucket. Strictly above min_snr_db:
                                      # the band between the two is "noisy but
                                      # still usable", which is the population
                                      # noisy-bucket accuracy is actually about.

    # --- 5. retry ladder --------------------------------------------------
    max_clarify_retries: int = 2      # consecutive failures before the keypad is
                                      # offered instead of another question

    @classmethod
    def from_env(cls) -> "QualityConfig":
        return cls(
            denoise_enabled=_env_bool("VOICE_AGENT_DENOISE", True),
            frame_len=_env_int("VOICE_AGENT_DENOISE_FRAME", 512),
            hop=_env_int("VOICE_AGENT_DENOISE_HOP", 128),
            noise_percentile=_env_float("VOICE_AGENT_DENOISE_PERCENTILE", 15.0),
            oversubtraction=_env_float("VOICE_AGENT_DENOISE_OVERSUB", 1.5),
            spectral_floor=_env_float("VOICE_AGENT_DENOISE_FLOOR", 0.08),
            normalize_enabled=_env_bool("VOICE_AGENT_NORMALIZE", True),
            target_rms_dbfs=_env_float("VOICE_AGENT_TARGET_DBFS", -20.0),
            max_gain_db=_env_float("VOICE_AGENT_MAX_GAIN_DB", 20.0),
            max_attenuation_db=_env_float("VOICE_AGENT_MAX_ATTEN_DB", 12.0),
            peak_ceiling_dbfs=_env_float("VOICE_AGENT_PEAK_CEILING_DBFS", -1.0),
            min_snr_db=_env_float("VOICE_AGENT_MIN_SNR_DB", 8.0),
            min_speech_ratio=_env_float("VOICE_AGENT_MIN_SPEECH_RATIO", 0.12),
            max_clipping_ratio=_env_float("VOICE_AGENT_MAX_CLIPPING", 0.02),
            min_duration_s=_env_float("VOICE_AGENT_MIN_CLIP_S", 0.30),
            min_rms_dbfs=_env_float("VOICE_AGENT_MIN_RMS_DBFS", -50.0),
            noisy_snr_db=_env_float("VOICE_AGENT_NOISY_SNR_DB", 15.0),
            max_clarify_retries=_env_int("VOICE_AGENT_MAX_CLARIFY_RETRIES", 2),
        )


CONFIG = QualityConfig.from_env()


# ---------------------------------------------------------------------------
# small dB helpers
# ---------------------------------------------------------------------------
def _to_dbfs(amplitude: float) -> float:
    """Amplitude (0..1 full scale) -> dBFS, with a floor instead of -inf."""
    if amplitude <= _EPS:
        return _SILENCE_DBFS
    return float(20.0 * np.log10(amplitude))


def _rms(x: np.ndarray) -> float:
    if x.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))


def rms_dbfs(x: np.ndarray) -> float:
    return _to_dbfs(_rms(np.asarray(x, dtype=np.float32).reshape(-1)))


# ---------------------------------------------------------------------------
# 3. QUALITY MEASUREMENT  (runs FIRST -- see the module docstring)
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class AudioQuality:
    """What one clip looked like BEFORE anything was done to it."""

    snr_db: float
    speech_ratio: float
    clipping_ratio: float
    rms_dbfs: float
    duration_s: float
    usable: bool
    reasons: tuple[str, ...] = ()

    @property
    def noisy(self) -> bool:
        """Noisy-bucket membership. Independent of `usable`: a turn can be
        noisy and still usable (that band is the interesting one), and a
        turn can fail for a reason unrelated to noise -- clipping, silence,
        too short -- without being filed as noisy."""
        return self.snr_db <= CONFIG.noisy_snr_db

    def bucket(self) -> str:
        return "noisy" if self.noisy else "clean"

    def as_dict(self) -> dict:
        return {
            "snr_db": round(self.snr_db, 2),
            "speech_ratio": round(self.speech_ratio, 3),
            "clipping_ratio": round(self.clipping_ratio, 4),
            "rms_dbfs": round(self.rms_dbfs, 2),
            "duration_s": round(self.duration_s, 2),
            "usable": self.usable,
            "bucket": self.bucket(),
            "reasons": list(self.reasons),
        }


def _frame_dbfs(x: np.ndarray, sr: int) -> np.ndarray:
    """Per-frame level in dBFS over 25ms frames at a 10ms hop -- the framing
    speech-activity detection conventionally uses."""
    win = max(1, int(0.025 * sr))
    hop = max(1, int(0.010 * sr))
    if x.size < win:
        return np.array([_to_dbfs(_rms(x))], dtype=np.float64)
    n_frames = 1 + (x.size - win) // hop
    idx = np.arange(win)[None, :] + hop * np.arange(n_frames)[:, None]
    power = np.mean(np.square(x[idx], dtype=np.float64), axis=1)
    return 10.0 * np.log10(np.maximum(power, _EPS ** 2))


def assess(samples: np.ndarray, sr: int, cfg: QualityConfig | None = None) -> AudioQuality:
    """Measure a clip. Pure function, CPU only, no model.

    THE SNR ESTIMATE
    ----------------
    Nobody hands us the clean speech and the noise separately, so a true
    SNR is not computable here. What IS computable, and is the standard
    substitute, is the spread of the clip's own frame-level distribution:

        noise floor  = 10th percentile of frame levels -- the quiet frames,
                       which in a real utterance are the room between words
        speech level = 90th percentile of frame levels -- the loud frames,
                       which are the caller
        snr_db       = speech level - noise floor

    In a quiet room those percentiles are far apart and the number is
    large. In a noisy one the background lifts the 10th percentile toward
    the 90th and the number collapses -- exactly the condition the agent
    needs to detect. It degrades gracefully: steady noise with no speech
    gives a near-zero spread, and so does digital silence, and both are
    correctly unusable.

    Deliberately NOT used: an absolute noise-level threshold. A loud room
    with a loud caller is fine; a quiet room with a whisperer is not. Only
    the ratio tells those apart.
    """
    cfg = cfg or CONFIG
    samples = np.asarray(samples, dtype=np.float32).reshape(-1)
    duration_s = samples.size / float(sr) if sr > 0 else 0.0

    if samples.size == 0:
        return AudioQuality(snr_db=0.0, speech_ratio=0.0, clipping_ratio=0.0,
                            rms_dbfs=_SILENCE_DBFS, duration_s=0.0, usable=False,
                            reasons=("empty",))

    levels = _frame_dbfs(samples, sr)
    noise_floor_db = float(np.percentile(levels, 10))
    speech_level_db = float(np.percentile(levels, 90))
    snr_db = max(0.0, speech_level_db - noise_floor_db)

    # Frames counted as speech: at least 3dB above the floor, or halfway up
    # to the speech level, whichever is higher. The 3dB minimum stops a clip
    # of pure noise -- where the spread is ~0 -- from reporting most of
    # itself as speech.
    speech_gate_db = noise_floor_db + max(3.0, 0.5 * snr_db)
    speech_ratio = float(np.mean(levels >= speech_gate_db))

    clipping_ratio = float(np.mean(np.abs(samples) >= _CLIP_LEVEL))
    level_dbfs = rms_dbfs(samples)

    reasons: list[str] = []
    if duration_s < cfg.min_duration_s:
        reasons.append("too_short")
    if level_dbfs < cfg.min_rms_dbfs:
        reasons.append("too_quiet")
    if snr_db < cfg.min_snr_db:
        reasons.append("low_snr")
    if speech_ratio < cfg.min_speech_ratio:
        reasons.append("little_speech")
    if clipping_ratio > cfg.max_clipping_ratio:
        reasons.append("clipped")

    return AudioQuality(
        snr_db=snr_db, speech_ratio=speech_ratio, clipping_ratio=clipping_ratio,
        rms_dbfs=level_dbfs, duration_s=duration_s,
        usable=not reasons, reasons=tuple(reasons),
    )


# ---------------------------------------------------------------------------
# 1. NOISE SUPPRESSION
# ---------------------------------------------------------------------------
def _stft(x: np.ndarray, frame_len: int, hop: int) -> tuple[np.ndarray, np.ndarray]:
    win = np.hanning(frame_len + 1)[:-1].astype(np.float64)   # periodic hann
    n_frames = 1 + (x.size - frame_len) // hop
    idx = np.arange(frame_len)[None, :] + hop * np.arange(n_frames)[:, None]
    frames = x[idx].astype(np.float64) * win
    return np.fft.rfft(frames, axis=1), win


def _istft(spec: np.ndarray, win: np.ndarray, hop: int, length: int) -> np.ndarray:
    """Weighted overlap-add. The window is applied a SECOND time on synthesis
    and the result divided by the summed squared window, so reconstruction is
    exact for any overlapping hop -- no reliance on hann/hop happening to
    satisfy COLA, and no scaling error to chase later.

    THE EDGE GUARD
    --------------
    The first and last frame_len samples are covered by fewer windows than
    the interior, and a hann window starts at exactly 0, so the summed
    squared window tends to zero there. Dividing by it amplifies the edges
    by orders of magnitude -- the reconstruction blows up into samples of
    ~25 full-scale while the interior is correct, which is silent unless
    the level is checked.

    The floor below is RELATIVE to the interior coverage rather than an
    absolute epsilon: under-covered samples are left attenuated instead of
    being exploded. suppress_noise pads the signal by a full frame before
    calling this and trims afterwards, so in practice only padding lands in
    that region and no real sample is affected.
    """
    frames = np.fft.irfft(spec, n=win.size, axis=1) * win
    out = np.zeros(length, dtype=np.float64)
    norm = np.zeros(length, dtype=np.float64)
    for i in range(frames.shape[0]):
        a = i * hop
        b = a + win.size
        if b > length:
            break
        out[a:b] += frames[i]
        norm[a:b] += win ** 2
    floor = max(float(norm.max()) * 1e-3, _EPS)
    return out / np.maximum(norm, floor)


def suppress_noise(samples: np.ndarray, sr: int,
                   cfg: QualityConfig | None = None) -> np.ndarray:
    """Spectral gating. Attenuates the STATIONARY part of the spectrum.

    HOW THE NOISE IS ESTIMATED
    --------------------------
    With no separate noise-only recording, the estimate has to come from
    the clip itself. For each frequency bin, take a low percentile of its
    magnitude across time. Speech is intermittent -- any given bin is loud
    only while a phoneme occupies it -- whereas a fan, traffic hum or
    air-conditioner is in every frame. The low percentile of a bin is
    therefore the room, and the peaks above it are the caller.

    That is why this needs no calibration step, and equally why it CANNOT
    remove a passing shout or a door slam: those are not stationary, so
    they never sit in the low percentile. Removing them is a different and
    much harder problem, deliberately not attempted here.

    WHY THE SPECTRAL FLOOR EXISTS
    -----------------------------
    Subtracting all the way to zero leaves isolated surviving bins that
    reconstruct as short tonal blips -- "musical noise". ASR does worse on
    musical noise than on the honest hum it replaced, because the blips
    look like onsets. The floor keeps a fraction of every bin so the
    residual stays broadband and speech-like.

    Returns float32 of the same length. Returns the input unchanged when
    the clip is too short to frame -- there is nothing to estimate from.
    """
    cfg = cfg or CONFIG
    samples = np.asarray(samples, dtype=np.float32).reshape(-1)
    if not cfg.denoise_enabled or samples.size < cfg.frame_len * 2:
        return samples

    # Pad by a full frame so every REAL sample sits in the fully-covered
    # interior of the overlap-add (see _istft's edge guard). Reflect rather
    # than zeros: a zero pad injects an artificial onset at the clip edge,
    # which is exactly the kind of false transient ASR reads as a phoneme.
    pad = cfg.frame_len
    padded = np.pad(samples.astype(np.float64), pad, mode="reflect")

    spec, win = _stft(padded, cfg.frame_len, cfg.hop)
    if spec.shape[0] < 2:
        return samples

    mag = np.abs(spec)
    noise_mag = np.percentile(mag, cfg.noise_percentile, axis=0, keepdims=True)

    gain = (mag - cfg.oversubtraction * noise_mag) / np.maximum(mag, _EPS)
    gain = np.clip(gain, cfg.spectral_floor, 1.0)

    cleaned = _istft(spec * gain, win, cfg.hop, padded.size)
    return cleaned[pad:pad + samples.size].astype(np.float32)


# ---------------------------------------------------------------------------
# 2. LEVEL NORMALISATION
# ---------------------------------------------------------------------------
def normalize_level(samples: np.ndarray,
                    cfg: QualityConfig | None = None) -> tuple[np.ndarray, float]:
    """One constant gain for the whole clip. Returns (audio, gain_db).

    Three guards, in this order, and the order is the point:

      1. The gain needed to reach target_rms_dbfs is CLAMPED to
         [-max_attenuation_db, +max_gain_db]. Unbounded gain on a nearly
         silent clip amplifies the room, not the caller -- it would take a
         hopeless clip and make it look well-levelled, defeating the
         quality floor that just measured it.

      2. After clamping, if the loudest sample would land above
         peak_ceiling_dbfs, the gain is REDUCED so it lands exactly there.
         A too-quiet clip can still be recognised; a square-waved one
         cannot.

      3. A final hard clip to [-1, 1], against float error at the ceiling.

    Constant gain -- not compression, not AGC -- is deliberate. The clip
    has already been segmented by the turn detector, so its dynamics carry
    information (a trailing-off word is a trailing-off word). Riding the
    gain within the clip would flatten that and lift the inter-word noise
    floor: the exact behaviour this project avoids by turning the browser's
    autoGainControl off.
    """
    cfg = cfg or CONFIG
    samples = np.asarray(samples, dtype=np.float32).reshape(-1)
    if not cfg.normalize_enabled or samples.size == 0:
        return samples, 0.0

    current = _rms(samples)
    if current <= _EPS:
        return samples, 0.0        # digital silence: no gain can help it

    gain_db = cfg.target_rms_dbfs - _to_dbfs(current)
    gain_db = float(np.clip(gain_db, -cfg.max_attenuation_db, cfg.max_gain_db))
    gain = 10.0 ** (gain_db / 20.0)

    peak = float(np.max(np.abs(samples)))
    ceiling = 10.0 ** (cfg.peak_ceiling_dbfs / 20.0)
    if peak * gain > ceiling:
        gain = ceiling / max(peak, _EPS)
        gain_db = _to_dbfs(gain)

    return np.clip(samples * gain, -1.0, 1.0).astype(np.float32), float(gain_db)


# ---------------------------------------------------------------------------
# the pipeline, and the file round-trip main.py actually calls
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class Conditioned:
    audio: np.ndarray
    sample_rate: int
    quality: AudioQuality      # measured on the RAW clip, before processing
    gain_db: float
    denoised: bool


def condition(samples: np.ndarray, sr: int,
              cfg: QualityConfig | None = None) -> Conditioned:
    """assess(raw) -> suppress -> normalise, in that order. See the module
    docstring for why measuring first is not optional."""
    cfg = cfg or CONFIG
    samples = np.asarray(samples, dtype=np.float32).reshape(-1)

    quality = assess(samples, sr, cfg)
    processed = suppress_noise(samples, sr, cfg)
    processed, gain_db = normalize_level(processed, cfg)

    return Conditioned(audio=processed, sample_rate=sr, quality=quality,
                       gain_db=gain_db, denoised=cfg.denoise_enabled)


def condition_wav_file(path: str, cfg: QualityConfig | None = None) -> Conditioned:
    """Read the clip main.py just cut, condition it, write it back IN PLACE,
    and report what the RAW clip looked like.

    In place on purpose: the caller already owns this temp file and already
    removes it in a finally block. A second file would double the cleanup
    paths for no benefit.

    soundfile rather than torchaudio: this has to be importable and testable
    with no torch/CUDA stack present, and soundfile is already pinned in
    requirements.txt.
    """
    import soundfile as sf   # local import: keeps the pure-numpy functions
                             # above usable even where libsndfile is absent

    samples, sr = sf.read(path, dtype="float32", always_2d=False)
    if samples.ndim > 1:
        samples = samples.mean(axis=1)

    result = condition(samples, sr, cfg)
    sf.write(path, result.audio, sr, subtype="PCM_16")
    return result
