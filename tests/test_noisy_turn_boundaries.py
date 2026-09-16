"""Regression tests for the noisy-caller fix: the ASR clip must start at the
caller's first syllable, not at the end of the agent's previous turn.

THE BUG THESE PIN
-----------------
_turn_poll_loop used to slice from session.processed_until_s, which is where
the PREVIOUS turn ended. Everything between that point and the caller
actually speaking went to ASR too. In a quiet room that lead-in is silence
and costs nothing, which is why it survived every test -- but in a noisy one
it is traffic or a crowd, and it grows with every second the caller spends
thinking. A 3-second question could reach IndicConformer as a 15-second,
mostly-noise clip.

vad_stream.poll() already computed the speech onset (`first_speech_start`)
and threw it away; TurnResult had no field to return it in. The fix returns
it and cuts there.

RUNNING THESE
-------------
    python -m pytest tests/ -v          (from the repo root)

nemo, omegaconf and torchaudio are stubbed below so the suite runs on a
laptop with no GPU and no 500MB checkpoint. Nothing under test touches them:
the ASR model is never constructed, and _slice_utterance is patched out in
the loop tests so the assertion is about the OFFSETS it is handed.
"""
from __future__ import annotations

import asyncio
import sys
import types

import pytest
import torch


# ---------------------------------------------------------------------------
# Stubs for the GPU-only imports, installed before main_pcm is imported.
# ---------------------------------------------------------------------------
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

    ta = sys.modules.setdefault("torchaudio", types.ModuleType("torchaudio"))
    ta.save = lambda *a, **k: None
    ta.load = lambda *a, **k: (torch.zeros(1, 16000), 16000)
    ta.functional = types.SimpleNamespace(resample=lambda w, a, b: w)


_install_stubs()

from agent.pcm_buffer import PcmCallBuffer  # noqa: E402
from agent.vad_stream import TurnDetector, TurnResult  # noqa: E402

SR = 16000


def make_detector(spans, **kwargs) -> TurnDetector:
    """A real TurnDetector with a scripted VAD, built without touching
    torch.hub. Every threshold and every branch in poll() is the real one --
    only the model's opinion about where speech is, is scripted."""
    d = object.__new__(TurnDetector)
    d.silence_confirm_s = kwargs.get("silence_confirm_s", 0.8)
    d.tail_guard_s = kwargs.get("tail_guard_s", 0.4)
    d.min_speech_s = kwargs.get("min_speech_s", 0.3)
    d.max_utterance_s = kwargs.get("max_utterance_s", 20.0)
    d.model = None
    d._model_lock = __import__("threading").Lock()
    d._get_speech_timestamps = lambda wav, model, **kw: spans
    return d


def tone(seconds: float, amplitude: float = 0.3) -> torch.Tensor:
    t = torch.arange(int(seconds * SR), dtype=torch.float32) / SR
    return torch.sin(2 * torch.pi * 220.0 * t) * amplitude


def noise(seconds: float, amplitude: float = 0.02) -> torch.Tensor:
    return torch.randn(int(seconds * SR)) * amplitude


def pcm_bytes(wav: torch.Tensor) -> bytes:
    return (wav.clamp(-1, 1) * 32767).to(torch.int16).numpy().tobytes()


# ===========================================================================
# A. TurnResult carries the onset, and defaults to the old behaviour
# ===========================================================================
def test_turnresult_defaults_to_zero_start():
    """The 'still talking' returns omit the start; 0.0 must mean 'cut from
    the beginning of the slice', i.e. exactly what the code did before."""
    r = TurnResult(utterance_end_s=None, had_any_speech=False)
    assert r.utterance_start_s == 0.0


def test_poll_reports_speech_onset_on_normal_turn_end():
    """17s tail, caller speaks 12.0-15.0. Trailing silence is 1.6s, past the
    0.8s confirm, so the turn ends -- and the onset must come back with it."""
    d = make_detector([{"start": 12.0, "end": 15.0}])
    r = d.poll(noise(17.0), SR)
    assert r.utterance_end_s == pytest.approx(15.0)
    assert r.utterance_start_s == pytest.approx(12.0)


def test_poll_reports_speech_onset_on_force_cut():
    """The 20s force-cut path is the one a noisy call actually takes, because
    noise keeps the trailing-silence test from ever passing. It must report
    the onset too, or the fix does nothing where it is needed most."""
    d = make_detector([{"start": 15.0, "end": 18.0}])
    r = d.poll(noise(20.0), SR)
    assert r.utterance_end_s == pytest.approx(18.0)
    assert r.utterance_start_s == pytest.approx(15.0)


def test_poll_still_talking_leaves_start_at_default():
    """Speech with only 0.1s of trailing quiet: not a complete turn."""
    d = make_detector([{"start": 1.0, "end": 4.5}])
    r = d.poll(noise(5.0), SR)
    assert r.utterance_end_s is None
    assert r.utterance_start_s == 0.0


def test_poll_no_speech_returns_nothing():
    d = make_detector([])
    r = d.poll(noise(10.0), SR)
    assert r.utterance_end_s is None
    assert r.had_any_speech is False


# ===========================================================================
# B. The poll loop cuts the clip where the VAD says speech started
# ===========================================================================
@pytest.fixture
def app_module():
    import main_pcm
    return main_pcm


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_text(self, t):
        self.sent.append(t)

    async def send_bytes(self, b):
        self.sent.append(b)

    async def close(self):
        pass


async def run_one_turn(app, buffer_wav, spans, processed_until_s=0.0):
    """Drive the REAL _turn_poll_loop until it slices one utterance, and
    return the (start_s, end_s) it asked for plus the session afterwards."""
    session = app.CallSession(FakeWS())
    session.agent_speaking = False
    session.resync_pending = False
    session.processed_until_s = processed_until_s
    session.audio.append(pcm_bytes(buffer_wav))

    captured = {}
    done = asyncio.Event()

    async def fake_slice(sess, start_s, end_s, seq):
        captured["start_s"] = start_s
        captured["end_s"] = end_s
        done.set()
        return "/tmp/does-not-exist.wav"

    async def fake_dispatch(sess, path):
        return None

    app._turn_detector = make_detector(spans)
    app._slice_utterance = fake_slice
    app._dispatch_turn = fake_dispatch
    # Restored in the finally below. Leaving it at 0.01 leaks a global into
    # every test that runs afterwards -- which is exactly how it was found.
    original_poll = app.POLL_INTERVAL_S
    app.POLL_INTERVAL_S = 0.01

    task = asyncio.create_task(app._turn_poll_loop(session))
    try:
        await asyncio.wait_for(done.wait(), timeout=5.0)
    finally:
        app.POLL_INTERVAL_S = original_poll
        task.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await task
        session.cleanup()
    return captured, session


def test_clip_starts_at_speech_not_at_previous_turn(app_module):
    """THE REGRESSION TEST.

    12s of room noise, then 3s of speech, then 2s of noise. Before the fix
    the clip started at 0.0 and carried all 12 seconds of noise into ASR.
    """
    wav = torch.cat([noise(12.0), tone(3.0), noise(2.0)])
    captured, _ = asyncio.run(run_one_turn(
        app_module, wav, [{"start": 12.0, "end": 15.0}],
    ))

    pad = app_module.UTTERANCE_PAD_S
    assert captured["start_s"] == pytest.approx(12.0 - pad)
    assert captured["end_s"] == pytest.approx(15.0)
    assert captured["start_s"] > 1.0, "clip still starts at the previous turn"


def test_clip_duration_collapses_to_the_speech(app_module):
    """The point of the fix, stated as the number that matters: what ASR is
    handed shrinks from 15s of mostly-noise to ~3s of mostly-speech."""
    wav = torch.cat([noise(12.0), tone(3.0), noise(2.0)])
    captured, _ = asyncio.run(run_one_turn(
        app_module, wav, [{"start": 12.0, "end": 15.0}],
    ))

    pad = app_module.UTTERANCE_PAD_S
    clip_s = (captured["end_s"] + pad) - captured["start_s"]
    old_clip_s = captured["end_s"] + pad          # what the old code cut
    assert clip_s == pytest.approx(3.0 + 2 * pad)
    assert clip_s < old_clip_s / 4


def test_processed_until_still_advances_to_the_end(app_module):
    """The skipped lead-in must be CONSUMED, not left for the next poll to
    re-examine -- otherwise the marker stops tracking real time and the
    tail grows without bound."""
    wav = torch.cat([noise(12.0), tone(3.0), noise(2.0)])
    _, session = asyncio.run(run_one_turn(
        app_module, wav, [{"start": 12.0, "end": 15.0}],
    ))
    assert session.processed_until_s == pytest.approx(15.0)


def test_clip_start_never_precedes_consumed_audio(app_module):
    """Onset earlier than the lead-in pad must clamp, not rewind into audio
    a previous turn already used."""
    wav = torch.cat([noise(5.0), tone(3.0), noise(2.0)])
    captured, _ = asyncio.run(run_one_turn(
        app_module, wav, [{"start": 0.05, "end": 3.0}], processed_until_s=5.0,
    ))
    assert captured["start_s"] == pytest.approx(5.0)


def test_quiet_caller_who_replies_instantly_is_unchanged(app_module):
    """No regression for the quiet-room path: onset at 0 clamps back to the
    old start, so those callers get byte-identical clips."""
    wav = torch.cat([tone(3.0), noise(2.0)])
    captured, _ = asyncio.run(run_one_turn(
        app_module, wav, [{"start": 0.0, "end": 3.0}],
    ))
    assert captured["start_s"] == pytest.approx(0.0)
    assert captured["end_s"] == pytest.approx(3.0)


def test_force_cut_path_also_trims_the_lead_in(app_module):
    """A noisy call reaches the 20s force-cut rather than the normal turn
    end, so that path has to trim too."""
    wav = torch.cat([noise(15.0), tone(3.0), noise(2.0)])
    captured, _ = asyncio.run(run_one_turn(
        app_module, wav, [{"start": 15.0, "end": 18.0}],
    ))
    pad = app_module.UTTERANCE_PAD_S
    assert captured["start_s"] == pytest.approx(15.0 - pad)


# ===========================================================================
# C. Both transports carry the fix (main_pcm.py is generated from main.py)
# ===========================================================================
def test_both_transports_slice_from_the_speech_onset():
    """main_pcm.py is generated by tools/make_pcm_variant.py. If someone
    edits main.py and forgets to regenerate, the WebM and PCM paths silently
    disagree about something this important."""
    import io
    for path in ("main.py", "main_pcm.py"):
        src = io.open(path, encoding="utf-8").read()
        assert "absolute_start_s" in src, f"{path} lost the onset-aware slice"
        assert "session, absolute_start_s, absolute_end_s, session.utt_seq," in src, (
            f"{path} does not pass the onset to _slice_utterance"
        )
