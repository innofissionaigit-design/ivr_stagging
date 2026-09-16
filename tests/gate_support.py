"""Shared helpers for the gate's healthcare suites (tests/test_gate_*.py).

Author: Chakravardhan

Not a test module (no `test_` prefix): pytest puts tests/ on sys.path, so the
suites import this directly. It provides the same NeMo/torchaudio stand-ins
the existing wiring tests use, a loader for the gate's own scripts, and small
fakes for the WebSocket, TTS and clinic-api client.
"""

from __future__ import annotations

import importlib
import importlib.util
import io
import sys
import types
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
for _p in (ROOT, SCRIPTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

TRILINGUAL_ENV = {
    "VOICE_AGENT_LANGUAGES": "bn,hi,en",
    "VOICE_AGENT_NEMO_FILE_HI": "/fake/hi.nemo",
    "VOICE_AGENT_NEMO_FILE_EN": "/fake/en.nemo",
}
COUNTER_WORDS = ("কাউন্টার", "काउंटर", "counter")


def install_stubs() -> None:
    for name in (
        "nemo",
        "nemo.collections",
        "nemo.collections.asr",
        "nemo.collections.asr.parts",
        "nemo.collections.asr.parts.submodules",
        "nemo.collections.asr.parts.submodules.rnnt_decoding",
    ):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["nemo.collections.asr"].models = types.SimpleNamespace(
        ASRModel=types.SimpleNamespace(restore_from=lambda **kw: None)
    )
    sys.modules["nemo.collections.asr.parts.submodules.rnnt_decoding"].RNNTDecodingConfig = object
    omegaconf = sys.modules.setdefault("omegaconf", types.ModuleType("omegaconf"))
    omegaconf.OmegaConf = types.SimpleNamespace(structured=lambda x: x)

    import torch

    ta = sys.modules.setdefault("torchaudio", types.ModuleType("torchaudio"))
    if not hasattr(ta, "save"):
        ta.save = lambda *a, **k: None
    if not hasattr(ta, "load"):
        ta.load = lambda *a, **k: (torch.zeros(1, 16000), 16000)
    if not hasattr(ta, "functional"):
        ta.functional = types.SimpleNamespace(resample=lambda w, a, b: w)


def load_main_pcm():
    install_stubs()
    return importlib.import_module("main_pcm")


def load_script(name: str):
    """Import scripts/<name>.py (hyphenated names included) as a module."""
    module_name = "gate_script_" + name.replace("-", "_")
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def trilingual(monkeypatch) -> None:
    for key, value in TRILINGUAL_ENV.items():
        monkeypatch.setenv(key, value)


def wav_bytes(seconds: float = 0.2, sr: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"\x00\x00" * int(seconds * sr))
    return buf.getvalue()


def mentions_counter(text: str) -> bool:
    return any(word in text for word in COUNTER_WORDS)


class FakeWS:
    def __init__(self):
        self.frames: list[str] = []

    async def accept(self):
        pass

    async def send_text(self, text):
        self.frames.append(text)

    async def send_bytes(self, data):
        pass

    async def close(self):
        pass


class FakeTools:
    """Stands in for ClinicToolsClient: records every call, returns canned
    responses, raises canned exceptions."""

    def __init__(self, responses=None, raises=None):
        self.calls: list[tuple[str, tuple, dict]] = []
        self.responses = responses or {}
        self.raises = raises or {}

    def __getattr__(self, name):
        async def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if name in self.raises:
                raise self.raises[name]
            return self.responses.get(name, {})

        return call

    def called(self) -> list[str]:
        return [c[0] for c in self.calls]


class SpeechLog:
    """Replacement for main_pcm._speak that records what the caller would hear."""

    def __init__(self):
        self.lines: list[tuple[str, str | None]] = []

    async def __call__(self, session, text, fallback_reason=None, audit_redact=None):
        self.lines.append((text, fallback_reason))

    @property
    def texts(self) -> list[str]:
        return [t for t, _ in self.lines]
