"""8 kHz / PSTN audio pipeline smoke test for the pre-human-review gate.

Author: Chakravardhan

Callers on a phone network arrive as 8 kHz narrowband audio, G.711 mu-law
companded, band-limited to about 300-3400 Hz, in 20 ms packets. The bench
transport captures 16 kHz, so nothing had checked that the pipeline still
behaves when a client declares 8 kHz: that the call timeline stays exact,
that each utterance is resampled to the 16 kHz ASR expects, and that the
quality floor still admits telephone speech and still rejects line noise.

No telephony adapter exists yet (a known blocker); this drives the PCM
transport exactly as one would.

    python -m pytest tests/test_gate_pstn_8khz.py -v
"""

from __future__ import annotations

import asyncio
import json
import math
import types

import gate_support
import numpy as np
import pytest
import soundfile as sf
import torch
from scipy import signal

from agent.audio_quality import condition_wav_file
from agent.pcm_buffer import PcmCallBuffer

app = gate_support.load_main_pcm()
SR8 = 8000
FRAME = 160  # 20 ms at 8 kHz, the usual RTP packet


def mulaw_roundtrip(x: np.ndarray, mu: float = 255.0) -> np.ndarray:
    """G.711 mu-law compress, quantise to 8 bits, expand."""
    y = np.sign(x) * np.log1p(mu * np.abs(x)) / np.log1p(mu)
    q = np.round(y * 127.0) / 127.0
    return np.sign(q) * ((1.0 + mu) ** np.abs(q) - 1.0) / mu


def telephone_speech(seconds: float, seed: int = 7) -> np.ndarray:
    """A voiced, syllabic signal in the telephone band, with pauses and a
    low line-noise floor -- deterministic, so the test is stable."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * SR8)) / SR8
    f0 = 140 + 20 * np.sin(2 * np.pi * 0.7 * t)
    phase = 2 * np.pi * np.cumsum(f0) / SR8
    voiced = sum(np.sin(k * phase) / k for k in range(1, 20) if k * 160 < 3400)
    syllables = np.clip(np.sin(2 * np.pi * 3.0 * t), 0, None) ** 0.6
    speech = voiced * syllables
    b, a = signal.butter(4, [300, 3400], btype="band", fs=SR8)
    speech = signal.lfilter(b, a, speech)
    speech = 0.35 * speech / np.max(np.abs(speech))
    speech += 0.003 * rng.standard_normal(speech.shape)
    return mulaw_roundtrip(np.clip(speech, -1, 1)).astype(np.float32)


def line_noise(seconds: float, seed: int = 11) -> np.ndarray:
    rng = np.random.default_rng(seed)
    noise = rng.standard_normal(int(seconds * SR8))
    b, a = signal.butter(4, [300, 3400], btype="band", fs=SR8)
    noise = signal.lfilter(b, a, noise)
    return mulaw_roundtrip(0.3 * noise / np.max(np.abs(noise))).astype(np.float32)


def to_pcm16(x: np.ndarray) -> bytes:
    return (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()


def upsample_to_16k(x: np.ndarray) -> np.ndarray:
    return signal.resample_poly(x, 2, 1).astype(np.float32)


def test_the_call_timeline_stays_exact_at_8khz():
    buf = PcmCallBuffer(sample_rate=SR8)
    pcm = to_pcm16(telephone_speech(3.0))
    for i in range(0, len(pcm), FRAME * 2):
        buf.append(pcm[i : i + FRAME * 2])
    assert len(buf) == 3 * SR8
    assert math.isclose(buf.duration_s, 3.0)
    assert buf.slice_tensor(1.0, 1.5).numel() == SR8 // 2
    assert buf.tail_tensor(2.5).numel() == SR8 // 2


def test_a_client_declaring_8khz_is_believed():
    session = app.CallSession(gate_support.FakeWS())
    try:
        asyncio.run(
            app._handle_control(
                session, json.dumps({"type": "hello", "sampleRate": SR8, "format": "pcm_s16le"})
            )
        )
        assert session.audio.sample_rate == SR8
        assert session.declared_rate == SR8
    finally:
        session.cleanup()


def test_each_8khz_utterance_is_resampled_to_16khz_before_asr(monkeypatch):
    captured = {}

    def resample(wav, orig, new):
        captured["rates"] = (orig, new)
        g = math.gcd(orig, new)
        return torch.from_numpy(
            signal.resample_poly(wav.numpy(), new // g, orig // g, axis=-1).astype(np.float32)
        )

    def save(path, wav, sr):
        captured["sr"], captured["samples"] = sr, wav.shape[-1]
        sf.write(path, wav.squeeze(0).numpy(), sr, subtype="PCM_16")

    monkeypatch.setattr(app.torchaudio, "functional", types.SimpleNamespace(resample=resample))
    monkeypatch.setattr(app.torchaudio, "save", save)
    session = app.CallSession(gate_support.FakeWS())
    try:
        asyncio.run(app._handle_control(session, json.dumps({"type": "hello", "sampleRate": SR8})))
        asyncio.run(session.append(to_pcm16(telephone_speech(2.0))))
        clip = asyncio.run(app._slice_utterance(session, 0.0, 1.8, 1))
        quality = condition_wav_file(clip).quality
    finally:
        session.cleanup()
    assert captured["rates"] == (SR8, 16000)
    assert captured["sr"] == 16000
    expected = (1.8 + app.UTTERANCE_PAD_S) * 16000
    assert abs(captured["samples"] - expected) <= 32
    assert quality.usable, quality


def test_narrowband_companded_speech_passes_the_quality_floor(tmp_path):
    path = tmp_path / "speech.wav"
    sf.write(path, upsample_to_16k(telephone_speech(2.0)), 16000, subtype="PCM_16")
    quality = condition_wav_file(str(path)).quality
    assert quality.usable, quality
    assert quality.snr_db >= 8.0


def test_line_noise_alone_is_still_rejected_at_8khz(tmp_path):
    path = tmp_path / "noise.wav"
    sf.write(path, upsample_to_16k(line_noise(2.0)), 16000, subtype="PCM_16")
    quality = condition_wav_file(str(path)).quality
    assert not quality.usable, quality


@pytest.mark.parametrize("seconds", [0.5, 1.0, 2.5])
def test_packetised_audio_round_trips_bit_exactly(seconds):
    pcm = to_pcm16(telephone_speech(seconds))
    buf = PcmCallBuffer(sample_rate=SR8)
    for i in range(0, len(pcm), FRAME * 2):
        buf.append(pcm[i : i + FRAME * 2])
    back = (buf.tail_tensor(0.0).numpy() * 32768.0).round().astype("<i2").tobytes()
    assert back == pcm
