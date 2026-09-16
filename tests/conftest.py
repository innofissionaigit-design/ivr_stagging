"""ADDED BY SOURAV -- shared pytest bootstrap for the whole `tests/` package.

Referenced by tests/test_test_sample_intent.py's own docstring ("See
tests/conftest.py for why main_pcm.py imports cleanly here without the
real ASR/VAD/LLM stack") but that file was never actually committed --
this gap meant every dispatch-level test in this suite (test_booking_
readback.py, test_test_sample_intent.py, and this combined story's new
main_pcm dispatch tests) could only ever run on a machine with the full
torch / nemo / omegaconf GPU stack installed, since importing main.py or
main_pcm.py at module level transitively imports agent.asr (torch, nemo,
omegaconf) and agent.vad_stream / agent.pcm_buffer (torch).

None of that stack is actually exercised by these tests: every dispatch
test monkeypatches _asr / _turn_detector / _tools / _tts directly (see
test_test_sample_intent.py's FakeASR / FakeToolsClient pattern) --
TurnASR / TurnDetector / TTSClient are never really instantiated. So the
only thing standing between "these tests only run on the GPU dev box" and
"these tests run anywhere, including plain CI" was three heavy imports
having nowhere to resolve. Fixed the general way: before any test module
gets a chance to `import main` / `import main_pcm`, install a bare
placeholder module for anything genuinely unavailable in sys.modules,
and leave anything already importable (e.g. on the real GPU box, where
this conftest changes nothing) strictly alone.

WHY THIS IS SAFE: agent/asr.py, agent/pcm_buffer.py and agent/vad_stream.py
all start with `from __future__ import annotations`, so every type
annotation referencing torch.Tensor etc. is a lazy string, never evaluated
at import time -- only actual torch.* CALLS inside function/method bodies
need the real thing, and this suite never reaches those bodies (nothing
here constructs a real TurnASR/TurnDetector or calls PcmCallBuffer's
tensor methods on real audio).
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

# gate_support.py lives in tests/ and is imported as a bare module name
# (``import gate_support``) by the test_gate_*.py suites.  With tests/__init__.py
# present, pytest imports every test file as ``tests.test_foo`` (package mode),
# so tests/ is NOT added to sys.path by pytest itself -- meaning bare
# ``import gate_support`` fails at collection time.
#
# The fix is sys.path.append (NOT insert(0,...)).  Pytest prepends the project
# root so ``tests.test_foo`` always resolves first; ``gate_support`` is found
# via the appended entry without ever shadowing the package-based imports.
# Using insert(0,...) instead would make ``test_foo`` resolvable BEFORE
# ``tests.test_foo``, triggering an "import file mismatch" error (exit code 2).
_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.append(_TESTS_DIR)


def _install_stub(name: str, **attrs) -> None:
    if name in sys.modules:
        return
    try:
        __import__(name)
        return  # importable for real (e.g. on the GPU dev box) -- leave it
    except ImportError:
        pass
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod


class _StubCuda:
    @staticmethod
    def is_available() -> bool:
        return False


def _stub_from_numpy(arr):
    return arr


def _stub_device(*args, **kwargs):
    return "cpu"


_install_stub("torch", Tensor=object, from_numpy=_stub_from_numpy,
              device=_stub_device, cuda=_StubCuda)
_install_stub("torchaudio", functional=types.ModuleType("torchaudio.functional"),
              load=lambda *a, **k: (None, None), save=lambda *a, **k: None)
_install_stub("omegaconf", OmegaConf=type("OmegaConf", (), {}))
_install_stub("nemo")
_install_stub("nemo.collections")
_install_stub("nemo.collections.asr", ASRModel=type("ASRModel", (), {}))
_install_stub("nemo.collections.asr.parts")
_install_stub("nemo.collections.asr.parts.submodules")
_install_stub(
    "nemo.collections.asr.parts.submodules.rnnt_decoding",
    RNNTDecodingConfig=type("RNNTDecodingConfig", (), {}),
)
