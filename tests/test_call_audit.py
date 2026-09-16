"""Tests for "Every call leaves a complete record".

Author: Chakravardhan
Story:  "As a hospital, I want each call to leave an accurate entry, so that
         the agent is auditable in the same way a member of staff is."

WHAT THESE TESTS DRIVE
----------------------
The REAL code paths -- main_pcm.py's _dispatch_turn and ws_audio, the real
ClinicToolsClient with its real error mapping, and a real SQLite file -- with
only the GPU/LLM/TTS edges replaced. clinic-api is an httpx.MockTransport, so
"what the backend returned" in each test is a response the test controls and
can compare the record against exactly.

EVERY ASSERTION IS MADE AGAINST WHAT WAS READ BACK FROM THE DATABASE, never
against in-memory state. The requirement is about the record the hospital
will have, not about what this process believed at the time.

    python -m pytest tests/test_call_audit.py -v
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import queue
import sqlite3
import sys
import types
import wave
import io

import httpx
import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)


def _install_stubs() -> None:
    """Same stubs as tests/test_speakerphone.py: nemo, omegaconf and
    torchaudio are not importable on a CPU dev box, and nothing here needs
    them to be real."""
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

import main_pcm as app                                        # noqa: E402
from agent import call_audit                                  # noqa: E402
from agent.asr import ASRResult                               # noqa: E402
from agent.audio_quality import AudioQuality                  # noqa: E402
from agent.call_audit import (                                # noqa: E402
    AGENT_INTERRUPTED, AGENT_RESPONSE, API_REQUEST, API_RESPONSE, CALL_ENDED,
    CALL_STARTED, ERROR, INTENT_DETECTED, SLOTS_EXTRACTED, TRANSCRIPT,
)
from agent.tools_client import ClinicToolsClient              # noqa: E402


# ===========================================================================
# Doubles for the edges -- GPU, LLM, TTS, clinic-api
# ===========================================================================
def _wav_bytes(seconds: float = 0.2, sr: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"\x00\x00" * int(seconds * sr))
    return buf.getvalue()


class FakeTTS:
    def __init__(self):
        self.fail = False
        self.spoken: list[str] = []

    async def synthesize(self, text, lang=None):
        if self.fail:
            raise RuntimeError("tts down: connection refused")
        self.spoken.append(text)
        return _wav_bytes()

    def fallback_audio(self, reason):
        return _wav_bytes()

    async def aclose(self):
        pass


class FakeASR:
    def __init__(self):
        self.text = ""
        self.exc: BaseException | None = None

    async def transcribe_utterance(self, path):
        if self.exc is not None:
            raise self.exc
        return ASRResult(text=self.text, decoder_used="rnnt", decoder_agreement=0.93)


class FakeCache:
    def get(self, text):
        return None, "miss"

    def put(self, text, data):
        pass


class Backend:
    """clinic-api as far as these tests need it: path -> a JSON body, an
    exception to raise instead of answering, or an async callable."""

    def __init__(self):
        self.routes: dict = {}
        self.requests: list[httpx.Request] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        spec = self.routes.get(request.url.path)
        if isinstance(spec, BaseException):
            raise spec
        if callable(spec):
            spec = await spec(request)
        if spec is None:
            return httpx.Response(404, json={"detail": "Not Found"})
        return httpx.Response(200, json=spec)


class FakeWS:
    def __init__(self):
        self.frames: list[str] = []

    async def send_text(self, t):
        self.frames.append(t)

    async def send_bytes(self, b):
        pass

    async def close(self):
        pass


HANGUP = {"type": "websocket.disconnect"}


def dtmf(digit: str) -> dict:
    return {"type": "websocket.receive", "text": json.dumps({"type": "dtmf", "digit": digit})}


class ScriptedWS:
    """A WebSocket whose caller follows a script: messages to deliver,
    exceptions to raise, and pauses (floats, seconds). An exhausted script
    is a caller who stays on the line in silence until the server closes."""

    def __init__(self, script):
        self.script = list(script)
        self.frames: list[str] = []
        self.closed = False

    async def accept(self):
        pass

    async def send_text(self, t):
        if self.closed:
            raise RuntimeError("websocket is closed")
        self.frames.append(t)

    async def send_bytes(self, b):
        if self.closed:
            raise RuntimeError("websocket is closed")

    async def close(self):
        self.closed = True

    async def receive(self):
        while True:
            if self.closed:
                return HANGUP
            if not self.script:
                await asyncio.sleep(0.02)
                continue
            item = self.script.pop(0)
            if isinstance(item, (int, float)):
                await asyncio.sleep(item)
                continue
            if isinstance(item, BaseException):
                raise item
            return item


GOOD_CLIP = types.SimpleNamespace(
    quality=AudioQuality(snr_db=24.0, speech_ratio=0.55, clipping_ratio=0.0,
                         rms_dbfs=-21.0, duration_s=1.4, usable=True),
    gain_db=0.0, denoised=False)

LLM_DIAG = {"attempts": 1, "total_time_s": 0.012, "errors": []}
_SLOT_KEYS = ("test_name", "doctor_name", "department", "date", "time_slot",
              "patient_name", "phone")


def llm_says(intent: str, **slots) -> dict:
    full = {k: None for k in _SLOT_KEYS}
    full.update(slots)
    return {"intent": intent, "slots": full, "direct_reply_bn": None}


@pytest.fixture
def env(monkeypatch, tmp_path):
    store = call_audit.AuditStore(str(tmp_path / "call_audit.db"))
    backend = Backend()
    tools = ClinicToolsClient("http://clinic.test")
    tools._client = httpx.AsyncClient(base_url="http://clinic.test",
                                      transport=httpx.MockTransport(backend.handle))
    tts, asr = FakeTTS(), FakeASR()
    llm = {"result": llm_says("unclear")}

    def fake_extract(text):
        r = llm["result"]
        return (r(text) if callable(r) else copy.deepcopy(r)), dict(LLM_DIAG)

    monkeypatch.setattr(app, "_audit_store", store)
    monkeypatch.setattr(app, "_tools", tools)
    monkeypatch.setattr(app, "_tts", tts)
    monkeypatch.setattr(app, "_asr", asr)
    monkeypatch.setattr(app, "_intent_cache", FakeCache())
    monkeypatch.setattr(app, "_fast_path", None)
    monkeypatch.setattr(app, "extract_intent", fake_extract)
    monkeypatch.setattr(app, "condition_wav_file", lambda path: GOOD_CLIP)
    app.METRICS.reset()

    yield types.SimpleNamespace(store=store, backend=backend, tools=tools, tts=tts,
                                asr=asr, llm=llm, path=str(tmp_path / "call_audit.db"))
    store.close()
    asyncio.run(tools.aclose())


def run_call(fn, end: str = call_audit.END_CLIENT_DISCONNECT):
    """One call, run the way ws_audio runs one: a fresh CallSession, its
    audit bound for the task, finalised in `finally`."""
    async def main():
        session = app.CallSession(FakeWS())
        call_audit.bind(session.audit)
        try:
            await fn(session)
        finally:
            session.audit.end(end, pending_flow=(session.pending or {}).get("awaiting"),
                              language=session.lang)
            session.cleanup()
        return session
    return asyncio.run(main())


def run_ws(ws, timeout: float = 10.0):
    asyncio.run(asyncio.wait_for(app.ws_audio(ws), timeout))


def record_of(store, call_id: str) -> dict:
    assert store.flush(), "audit writer did not drain"
    rec = store.get_call(call_id)
    assert rec is not None, f"no record for call {call_id}"
    return rec


def only_call(store) -> dict:
    assert store.flush()
    calls = store.list_calls(limit=10)
    assert len(calls) == 1, calls
    return record_of(store, calls[0]["call_id"])


def types_of(rec) -> list[str]:
    return [e["event_type"] for e in rec["events"]]


def events(rec, event_type) -> list[dict]:
    return [e for e in rec["events"] if e["event_type"] == event_type]


# ===========================================================================
# 1. A SUCCESSFUL CALL -- every stage, in order, from real values
# ===========================================================================
def test_successful_call_records_every_stage_in_order(env):
    env.asr.text = "সিবিসি টেস্টের দাম কত"
    env.llm["result"] = llm_says("test_rate", test_name="CBC")
    backend_json = {"found": True, "test_name": "CBC", "rate_inr": 650,
                    "sample_type": "Blood", "report_time_hours": 24}
    env.backend.routes["/api/v1/tests/search"] = backend_json

    session = run_call(lambda s: app._dispatch_turn(s, "utt1.wav"))
    rec = record_of(env.store, session.call_id)

    assert types_of(rec) == [CALL_STARTED, TRANSCRIPT, INTENT_DETECTED, SLOTS_EXTRACTED,
                             API_REQUEST, API_RESPONSE, AGENT_RESPONSE, CALL_ENDED]

    transcript = events(rec, TRANSCRIPT)[0]["data"]
    assert transcript["text"] == "সিবিসি টেস্টের দাম কত"      # verbatim ASR output
    assert transcript["source"] == "speech"
    assert transcript["decoder_used"] == "rnnt"
    assert transcript["audio"]["usable"] is True

    intent = events(rec, INTENT_DETECTED)[0]["data"]
    assert intent["intent"] == "test_rate" and intent["source"] == "llm"
    assert intent["attempts"] == 1
    assert events(rec, SLOTS_EXTRACTED)[0]["data"]["slots"]["test_name"] == "CBC"

    req = events(rec, API_REQUEST)[0]
    assert req["data"] == {"action": "get_test_rate", "request": {"test_name": "CBC"}}
    resp = events(rec, API_RESPONSE)[0]
    assert resp["data"]["result"] == backend_json               # exactly what came back
    assert resp["data"]["outcome"] == "found" and resp["success"] is True
    assert resp["data"]["request_event_id"] == req["event_id"]

    said = events(rec, AGENT_RESPONSE)[0]["data"]
    assert said["text"] == env.tts.spoken[-1]                   # exactly what was spoken
    assert said["audio"] == "synthesized" and said["delivered"] is True

    call = rec["call"]
    assert call["call_id"] == session.call_id and len(call["call_id"]) == 32
    assert call["final_status"] == "completed"
    assert call["termination_reason"] == "client_disconnect"
    assert call["transport"] == "pcm"
    assert (call["turns"], call["served_turns"], call["api_calls"], call["api_failures"]) == (1, 1, 1, 0)
    assert call["ended_at"] >= call["started_at"] and call["duration_s"] >= 0
    assert [e["seq"] for e in rec["events"]] == list(range(1, 9))
    assert all(e["turn"] == 1 for e in rec["events"][1:-1])
    timestamps = [e["ts"] for e in rec["events"]]
    assert timestamps == sorted(timestamps)
    assert rec["integrity"]["complete"] is True


# ===========================================================================
# 2. BACKEND TRUTH -- a refusal and an outage are never recorded as success
# ===========================================================================
def test_backend_refusal_is_recorded_as_the_failure_it_was(env):
    """The LLM understood a complete booking; clinic-api refused it. The
    record carries clinic-api's verdict, not the LLM's intention."""
    env.llm["result"] = llm_says("book_appointment", doctor_name="Sen", date="2026-09-14",
                                 time_slot="18:15", patient_name="Ranu Das",
                                 phone="9830012345")
    backend_json = {"success": False, "reason": "slot_taken",
                    "alternative_slots": ["18:30", "18:45"]}
    env.backend.routes["/api/v1/appointments"] = backend_json

    session = run_call(lambda s: app._dispatch_turn(
        s, "", text_override="কাল সওয়া ছটায় ডক্টর সেনের কাছে বুক করুন"))
    rec = record_of(env.store, session.call_id)

    req = events(rec, API_REQUEST)[0]["data"]
    assert req["action"] == "book_appointment"
    assert req["request"] == {"doctor_name": "Sen", "date": "2026-09-14", "time_slot": "18:15",
                              "patient_name": "Ranu Das", "phone": "9830012345"}
    resp = events(rec, API_RESPONSE)[0]
    assert resp["success"] is False
    assert resp["data"]["outcome"] == "failure"
    assert resp["data"]["reason"] == "slot_taken"
    assert resp["data"]["result"] == backend_json
    assert rec["call"]["api_failures"] == 1
    assert rec["call"]["caller_phone"] == "9830012345"          # WHO called


def test_unreachable_backend_is_recorded_with_the_real_error(env):
    env.llm["result"] = llm_says("test_rate", test_name="CBC")
    env.backend.routes["/api/v1/tests/search"] = httpx.ConnectError("connection refused")

    session = run_call(lambda s: app._dispatch_turn(s, "", text_override="সিবিসি কত"))
    rec = record_of(env.store, session.call_id)

    resp = events(rec, API_RESPONSE)[0]
    assert resp["success"] is False
    assert resp["data"]["outcome"] == "error"
    assert resp["data"]["error_type"] == "ToolCallError"
    assert "connection refused" in resp["data"]["error"]
    assert "result" not in resp["data"]
    assert events(rec, AGENT_RESPONSE)[0]["data"]["fallback_reason"] == "tool_failure"
    assert rec["call"]["final_status"] == "failed"
    assert rec["call"]["api_failures"] == 1


def test_a_backend_failure_main_py_swallows_is_still_recorded(env):
    """payment deliberately answers even when the rate lookup fails, so the
    caller never hears about it. The record must still show it happened --
    which is why capture sits at the client boundary, not the call sites."""
    env.llm["result"] = llm_says("payment", test_name="CBC")
    env.backend.routes["/api/v1/tests/search"] = httpx.ReadTimeout("timed out")

    session = run_call(lambda s: app._dispatch_turn(s, "", text_override="সিবিসির টাকা কীভাবে দেব"))
    rec = record_of(env.store, session.call_id)

    resp = events(rec, API_RESPONSE)[0]
    assert resp["success"] is False and resp["data"]["outcome"] == "error"
    assert "timed out" in resp["data"]["error"]
    said = events(rec, AGENT_RESPONSE)[0]["data"]
    assert said["fallback_reason"] is None and "কাউন্টার" in said["text"]
    assert rec["call"]["final_status"] == "completed"           # the caller WAS served
    assert rec["call"]["api_failures"] == 1                     # and the failure is not hidden


def test_tts_failure_records_what_the_caller_actually_heard(env):
    env.tts.fail = True
    env.llm["result"] = llm_says("test_rate", test_name="CBC")
    env.backend.routes["/api/v1/tests/search"] = {"found": True, "test_name": "CBC",
                                                   "rate_inr": 650, "sample_type": "Blood",
                                                   "report_time_hours": 24}

    session = run_call(lambda s: app._dispatch_turn(s, "", text_override="সিবিসি কত"))
    rec = record_of(env.store, session.call_id)

    said = events(rec, AGENT_RESPONSE)[0]
    assert said["data"]["audio"] == "fallback_clip"
    assert "tts down" in said["data"]["tts_error"]
    assert said["success"] is False
    assert rec["call"]["final_status"] == "failed"


# ===========================================================================
# 3. EXCEPTIONS -- recorded, and the call still finalised
# ===========================================================================
def test_unexpected_exception_in_a_turn_is_recorded_and_call_finalised(env):
    env.asr.exc = RuntimeError("CUDA error: device-side assert triggered")

    async def turn(s):
        with pytest.raises(RuntimeError):
            await app._dispatch_turn(s, "utt1.wav")           # re-raised unchanged

    session = run_call(turn)
    rec = record_of(env.store, session.call_id)

    err = events(rec, ERROR)[0]
    assert err["success"] is False
    assert err["data"]["stage"] == "turn" and err["data"]["handled"] is False
    assert err["data"]["error_type"] == "RuntimeError"
    assert "device-side assert" in err["data"]["error"]
    assert "transcribe_utterance" in err["data"]["where"]
    assert types_of(rec)[-1] == CALL_ENDED
    assert rec["call"]["final_status"] == "error"
    assert rec["call"]["errors"] == 1
    assert rec["integrity"]["complete"] is True


def test_session_crash_is_finalised_through_ws_audio(env):
    before = app._active_calls
    run_ws(ScriptedWS([0.05, RuntimeError("socket read failed: ECONNRESET")]))
    rec = only_call(env.store)

    assert types_of(rec)[0] == CALL_STARTED
    greeting = events(rec, AGENT_RESPONSE)[0]["data"]
    assert greeting["delivered"] is True and greeting["text"].startswith("নমস্কার")
    err = events(rec, ERROR)[0]["data"]
    assert err["stage"] == "session" and "ECONNRESET" in err["error"]
    ended = events(rec, CALL_ENDED)[0]["data"]
    assert ended["termination_reason"] == "exception" and ended["final_status"] == "error"
    assert rec["call"]["final_status"] == "error"
    assert app._active_calls == before                          # slot released too


# ===========================================================================
# 4. MULTIPLE CALLS -- never mixed
# ===========================================================================
def test_concurrent_calls_never_share_events(env):
    names = ["CBC", "TSH", "HbA1c"]
    env.llm["result"] = lambda text: llm_says("test_rate", test_name=text.split()[0])

    async def search(request):
        name = request.url.params["name"]
        # The FIRST call's answer comes back LAST, so the three are truly
        # interleaved inside the shared client when their events are filed.
        await asyncio.sleep({"CBC": 0.20, "TSH": 0.01, "HbA1c": 0.08}[name])
        return {"found": True, "test_name": name, "rate_inr": len(name) * 100,
                "sample_type": "Blood", "report_time_hours": 24}

    env.backend.routes["/api/v1/tests/search"] = search

    async def one(name):
        session = app.CallSession(FakeWS())
        call_audit.bind(session.audit)
        await app._dispatch_turn(session, "", text_override=f"{name} কত")
        session.audit.end(call_audit.END_CLIENT_DISCONNECT)
        session.cleanup()
        return session.call_id

    async def main():
        return await asyncio.gather(*(one(n) for n in names))

    ids = asyncio.run(main())
    assert len(set(ids)) == 3

    for call_id, name in zip(ids, names):
        rec = record_of(env.store, call_id)
        api = [e for e in rec["events"] if e["event_type"] in (API_REQUEST, API_RESPONSE)]
        assert [e["event_type"] for e in api] == [API_REQUEST, API_RESPONSE]
        assert api[0]["data"]["request"] == {"test_name": name}
        assert api[1]["data"]["result"]["test_name"] == name
        assert events(rec, TRANSCRIPT)[0]["data"]["text"] == f"{name} কত"
        assert [e["seq"] for e in rec["events"]] == list(range(1, len(rec["events"]) + 1))
        assert rec["integrity"]["complete"] is True
        blob = json.dumps(rec, ensure_ascii=False)
        for other in set(names) - {name}:
            assert other not in blob, f"{other}'s data leaked into {name}'s record"


# ===========================================================================
# 5. DISCONNECTS, ABANDONMENT, TIMEOUTS, REFUSALS
# ===========================================================================
def test_hang_up_before_saying_anything_is_abandoned(env):
    run_ws(ScriptedWS([HANGUP]))
    rec = only_call(env.store)

    assert types_of(rec) == [CALL_STARTED, AGENT_RESPONSE, CALL_ENDED]
    assert rec["call"]["final_status"] == "abandoned"
    assert rec["call"]["termination_reason"] == "client_disconnect"
    assert rec["call"]["turns"] == 0
    assert rec["integrity"]["complete"] is True


def test_hang_up_mid_booking_is_abandoned_and_names_the_open_step(env):
    env.llm["result"] = llm_says("book_appointment", doctor_name="Sen")
    run_ws(ScriptedWS([0.05, dtmf("3"), 0.5, HANGUP]))
    rec = only_call(env.store)

    transcript = events(rec, TRANSCRIPT)[0]["data"]
    assert transcript["source"] == "keypad"
    assert events(rec, INTENT_DETECTED)[0]["data"]["intent"] == "book_appointment"
    ended = events(rec, CALL_ENDED)[0]["data"]
    assert ended["pending_flow"] == "date"                      # where the caller left
    assert rec["call"]["final_status"] == "abandoned"


def test_idle_timeout_is_recorded_as_such(env, monkeypatch):
    monkeypatch.setattr(app, "IDLE_TIMEOUT_S", 0.3)
    monkeypatch.setattr(app, "POLL_INTERVAL_S", 0.05)
    run_ws(ScriptedWS([]))                                      # silent, never hangs up
    rec = only_call(env.store)

    assert rec["call"]["termination_reason"] == "idle_timeout"
    goodbye = events(rec, AGENT_RESPONSE)[-1]["data"]["text"]
    assert "কল শেষ করছি" in goodbye
    assert rec["call"]["final_status"] == "abandoned"


def test_a_caller_refused_at_capacity_still_leaves_a_record(env, monkeypatch):
    monkeypatch.setattr(app, "_active_calls", app.MAX_CONCURRENT_CALLS)
    run_ws(ScriptedWS([]))
    rec = only_call(env.store)

    assert types_of(rec) == [CALL_STARTED, AGENT_RESPONSE, CALL_ENDED]
    assert events(rec, AGENT_RESPONSE)[0]["data"]["text"] == app.BUSY_LINE
    assert rec["call"]["final_status"] == "rejected"
    assert rec["call"]["termination_reason"] == "at_capacity"


# ===========================================================================
# 6. AUDIT FAILURE -- never reaches the caller, never silent
# ===========================================================================
def test_audit_database_failure_does_not_break_the_call(env, monkeypatch, caplog):
    def broken(conn, writes):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(env.store, "_write", broken)
    env.llm["result"] = llm_says("test_rate", test_name="CBC")
    env.backend.routes["/api/v1/tests/search"] = {"found": True, "test_name": "CBC",
                                                   "rate_inr": 650, "sample_type": "Blood",
                                                   "report_time_hours": 24}

    with caplog.at_level("ERROR", logger="call_audit"):
        run_call(lambda s: app._dispatch_turn(s, "", text_override="সিবিসি কত"))
        assert env.store.flush()

    assert env.tts.spoken, "the caller must still have been answered"
    health = env.store.health()
    assert health["write_failures"] > 0
    assert "disk I/O error" in health["last_error"]
    assert any("AUDIT RECORD LOST" in r.getMessage() for r in caplog.records)


def test_a_bug_in_the_recorder_itself_never_reaches_the_caller(env, monkeypatch):
    def explode(*a, **k):
        raise RuntimeError("recorder bug")

    for name in ("add_event", "start_call", "end_call"):
        monkeypatch.setattr(env.store, name, explode)
    env.llm["result"] = llm_says("test_rate", test_name="CBC")
    env.backend.routes["/api/v1/tests/search"] = {"found": True, "test_name": "CBC",
                                                   "rate_inr": 650, "sample_type": "Blood",
                                                   "report_time_hours": 24}

    run_call(lambda s: app._dispatch_turn(s, "", text_override="সিবিসি কত"))

    assert env.tts.spoken
    assert env.store.health()["write_failures"] >= 8


def test_a_full_queue_drops_are_counted_not_raised(tmp_path):
    store = call_audit.AuditStore(str(tmp_path / "a.db"))
    real_queue = store._q
    full = queue.Queue(maxsize=1)
    full.put_nowait(("SELECT 1", (), "x"))
    store._q = full                                  # the writer stays parked on the real one
    try:
        audit = call_audit.CallAudit(store, transport="pcm")
        audit.record(TRANSCRIPT, {"text": "x"})
        assert store.health()["dropped"] >= 2
    finally:
        store._q = real_queue
        store.close()


def test_an_unopenable_database_is_reported_and_the_call_still_runs(tmp_path):
    store = call_audit.AuditStore(str(tmp_path / "no" / "such" / "dir" / "audit.db"))
    try:
        assert store.health()["available"] is False
        assert "cannot open audit database" in store.health()["last_error"]
        audit = call_audit.CallAudit(store, transport="pcm")
        audit.record(TRANSCRIPT, {"text": "x"})
        assert audit.end(call_audit.END_CLIENT_DISCONNECT) == "abandoned"
        assert store.flush()
        assert store.health()["write_failures"] >= 3
    finally:
        store.close()


# ===========================================================================
# 7. PRIVACY -- the secret and the history never reach the record
# ===========================================================================
def test_verification_answer_token_and_history_never_reach_the_record(env, monkeypatch):
    monkeypatch.setenv("VOICE_AGENT_HISTORY_REQUIRE_PRIVATE_PATH", "0")
    dob, token = "1987-03-14", "TOKEN-SECRET-VALUE"
    env.backend.routes["/api/v1/history/verify"] = {
        "reply": "verified", "verified": True, "factor": "dob", "token": token}
    env.backend.routes["/api/v1/history/read"] = {
        "found": True, "patient_name": "Ranu Das", "appointments": [],
        "tests": [{"test_name": "Lipid Profile", "test_name_bn": "লিপিড প্রোফাইল",
                   "taken_on": "2026-08-01", "report_ready": True}]}

    async def answer_challenge(s):
        s.history_phone = "9830012345"
        s.pending = {"awaiting": "history_verify", "factor": "dob", "slots": {},
                     "candidates": None, "offered_date": None, "retries": 0}
        await app._dispatch_turn(s, "", text_override=dob)

    session = run_call(answer_challenge)
    rec = record_of(env.store, session.call_id)

    # The real values DID go where they belong -- to clinic-api, and to the caller.
    sent = [json.loads(r.content) for r in env.backend.requests]
    assert sent[0]["answer"] == dob and sent[1]["token"] == token
    assert "লিপিড প্রোফাইল" in env.tts.spoken[-1]

    # ...and nowhere in the audit file, byte for byte.
    raw = open(env.path, "rb").read()
    for secret in (dob, token, "Lipid Profile", "লিপিড প্রোফাইল"):
        assert secret.encode("utf-8") not in raw, f"{secret!r} reached the audit database"

    # What IS recorded is enough to audit the disclosure.
    t = events(rec, TRANSCRIPT)[0]["data"]
    assert t["text"] is None and t["redacted"] == "verification_answer" and t["chars"] == len(dob)
    verify_req, read_req = [e["data"] for e in events(rec, API_REQUEST)]
    assert verify_req["request"]["answer"] == "[redacted]"
    assert verify_req["request"]["factor"] == "dob"
    assert read_req["request"]["token"] == "[redacted]"
    verify_resp, read_resp = [e["data"] for e in events(rec, API_RESPONSE)]
    assert verify_resp["outcome"] == "verified"
    assert verify_resp["result"]["token_issued"] is True and "token" not in verify_resp["result"]
    assert read_resp["result"] == {"found": True, "reason": None, "tests": 1, "appointments": 0}
    spoken = events(rec, AGENT_RESPONSE)[-1]["data"]
    assert spoken["redacted"] == "patient_history" and spoken["text"] is None


# ===========================================================================
# 8. FINALISATION -- write-once, and crash recovery
# ===========================================================================
def test_finalisation_is_write_once(env):
    audit = call_audit.CallAudit(env.store, transport="pcm")
    first = audit.end(call_audit.END_CLIENT_DISCONNECT)
    second = audit.end(call_audit.END_EXCEPTION)
    # Even a direct second UPDATE cannot rewrite the outcome.
    env.store.end_call(audit.call_id, ended_at="2099-01-01T00:00:00.000+00:00", duration_s=1.0,
                       final_status="completed", termination_reason="exception",
                       caller_phone=None, language="bn", turns=9, served_turns=9,
                       api_calls=0, api_failures=0, errors=0)
    rec = record_of(env.store, audit.call_id)

    assert first == second == "abandoned"
    assert len(events(rec, CALL_ENDED)) == 1
    assert rec["call"]["final_status"] == "abandoned"
    assert rec["call"]["termination_reason"] == "client_disconnect"


def test_calls_left_open_by_a_crash_are_finalised_on_restart(tmp_path):
    path = str(tmp_path / "a.db")
    first_run = call_audit.AuditStore(path)
    pcm = call_audit.CallAudit(first_run, transport="pcm", language="bn")
    pcm.begin_turn()
    pcm.transcript("হ্যালো", source="speech")
    webm = call_audit.CallAudit(first_run, transport="webm", language="bn")
    first_run.close()                     # the process dies: nobody calls end()

    second_run = call_audit.AuditStore(path)
    try:
        assert second_run.recover_unfinished("pcm") == 1
        rec = second_run.get_call(pcm.call_id)
        assert rec["call"]["final_status"] == "error"
        assert rec["call"]["termination_reason"] == "agent_restarted"
        last_evidence = events(rec, TRANSCRIPT)[0]["ts"]
        assert rec["call"]["ended_at"] == last_evidence     # not "now"
        assert events(rec, CALL_ENDED)[0]["data"]["ended_at_source"] == "last_recorded_event"
        assert rec["integrity"]["complete"] is True

        # The OTHER transport's call belongs to a process that may still be
        # live -- restarting this one must not touch it.
        assert second_run.get_call(webm.call_id)["call"]["ended_at"] is None
        assert second_run.recover_unfinished("pcm") == 0
    finally:
        second_run.close()


def test_a_missing_event_is_visible_in_the_integrity_block(env):
    audit = call_audit.CallAudit(env.store, transport="pcm")
    audit.record(TRANSCRIPT, {"text": "one"})
    env.store.flush()
    with sqlite3.connect(env.path) as conn:
        conn.execute("DELETE FROM call_events WHERE call_id = ? AND seq = 2", (audit.call_id,))
    audit.end(call_audit.END_CLIENT_DISCONNECT)
    rec = record_of(env.store, audit.call_id)

    assert rec["integrity"]["missing_seq"] == [2]
    assert rec["integrity"]["complete"] is False


# ===========================================================================
# 9. BARGE-IN, STAFF READ ENDPOINTS, TRANSPORT PARITY
# ===========================================================================
def test_barge_in_marks_the_reply_as_cut_off(env, monkeypatch):
    if not app.ECHO_CFG.barge_in_enabled:
        pytest.skip("barge-in disabled in this configuration")

    async def interrupted(s):
        import numpy as np

        async def tail(session, seconds):
            return np.zeros(int(seconds * 16000), dtype=np.float32), 16000

        monkeypatch.setattr(app, "_recent_mic_tail", tail)
        s.echo.assess = lambda *a, **k: types.SimpleNamespace(
            is_barge_in=True, as_dict=lambda: {"decision": "speech"})
        await app._speak(s, "একটা লম্বা উত্তর")
        assert await app._check_barge_in(s) is True

    session = run_call(interrupted)
    rec = record_of(env.store, session.call_id)
    assert types_of(rec)[-3:] == [AGENT_RESPONSE, AGENT_INTERRUPTED, CALL_ENDED]
    assert events(rec, AGENT_INTERRUPTED)[0]["data"]["verdict"] == {"decision": "speech"}


def test_staff_read_endpoints(env, monkeypatch):
    monkeypatch.delenv("VOICE_AGENT_AUDIT_TOKEN", raising=False)
    env.llm["result"] = llm_says("unclear")
    session = run_call(lambda s: app._dispatch_turn(s, "", text_override="হুম"))
    env.store.flush()

    record = asyncio.run(app.audit_call(session.call_id, x_audit_token=None))
    assert record["call"]["call_id"] == session.call_id
    assert record["integrity"]["complete"] is True
    listing = asyncio.run(app.audit_calls(limit=10, status=None, x_audit_token=None))
    assert listing["count"] == 1
    missing = asyncio.run(app.audit_call("does-not-exist", x_audit_token=None))
    assert missing.status_code == 404
    assert asyncio.run(app.health())["audit"]["available"] is True

    monkeypatch.setenv("VOICE_AGENT_AUDIT_TOKEN", "s3cret")
    assert asyncio.run(app.audit_call(session.call_id, x_audit_token="wrong")).status_code == 401
    assert asyncio.run(app.audit_calls(limit=10, status=None, x_audit_token=None)).status_code == 401
    ok = asyncio.run(app.audit_call(session.call_id, x_audit_token="s3cret"))
    assert ok["call"]["call_id"] == session.call_id


def test_both_transports_carry_identical_instrumentation():
    """main_pcm.py is generated from main.py. The capture points must exist
    in both, and each must label its own records."""
    src = open(os.path.join(_ROOT, "main.py"), encoding="utf-8").read()
    pcm = open(os.path.join(_ROOT, "main_pcm.py"), encoding="utf-8").read()
    assert 'AUDIT_TRANSPORT = "webm"' in src and 'AUDIT_TRANSPORT = "pcm"' in pcm
    a, b = "async def _resolve_intent(", "async def _resync_after_playback("
    assert src[src.index(a):src.index(b)] == pcm[pcm.index(a):pcm.index(b)]
    for marker in ("call_audit.bind(session.audit)", "session.audit.end(",
                   "_audit(session).agent_response(", "_record_intent(session"):
        assert marker in src and marker in pcm
