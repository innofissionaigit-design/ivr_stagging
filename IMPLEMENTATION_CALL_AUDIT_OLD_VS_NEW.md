# Every Call Leaves a Complete Record — Old vs New Code and Variables

**Author:** Chakravardhan
**Story:** *As a hospital, I want each call to leave an accurate entry, so that the agent is auditable in the same way a member of staff is.*
**Branch:** `dev_chakravardhan`. **Old** = commit `76c370c` (`git show HEAD:<file>`). **New** = working tree (uncommitted).
**Companion document:** `IMPLEMENTATION_CALL_AUDIT.md` (design, data model, guarantees, risks).

This document lists every code change for this story as **OLD → NEW**, and every variable, constant, attribute, column and
environment variable that was added or changed. Line numbers refer to the new working tree.

---

## 0. Files at a glance

| File | Status | Lines | What it does for the story |
|---|---|---|---|
| `agent/call_audit.py` | **new** | 913 | the store (SQLite + writer thread) and the per-call recorder |
| `agent/tools_client.py` | modified | +99 | records every backend request and the real response |
| `main.py` | modified | +313 / −93 | records every other step and finalises every call |
| `main_pcm.py` | regenerated | same as `main.py` | identical instrumentation, `AUDIT_TRANSPORT = "pcm"` |
| `tools/make_pcm_variant.py` | modified | +2 | flips `AUDIT_TRANSPORT` for the PCM process |
| `README.md` | modified | +18 | short section pointing to the design document |
| `tests/test_call_audit.py` | **new** | 23 tests | proves each behaviour against the database |
| `IMPLEMENTATION_CALL_AUDIT.md` | **new** | — | design document |

---

## 1. Variables — Old vs New

### 1.1 Environment variables

| Variable | Old | New | Default | Used in |
|---|---|---|---|---|
| `VOICE_AGENT_AUDIT_DB` | did not exist | path of the audit database | `/workspace/call_audit.db` | `call_audit.default_path()` |
| `VOICE_AGENT_AUDIT_TOKEN` | did not exist | if set, `/api/audit/*` requires a matching `X-Audit-Token` header | unset (endpoints open, startup warns) | `main._audit_read_denied`, `main._startup` |
| `CLINIC_DB_JOURNAL_MODE` | used by clinic-api only | also chooses the audit DB journal mode | — | `call_audit._journal_mode()` |
| `VOICE_AGENT_PROVIDER` | used by deploy scripts | `vast` → WAL, otherwise DELETE for the audit DB | — | `call_audit._journal_mode()` |

### 1.2 Module-level variables in `main.py` (same in `main_pcm.py`)

| Variable | Old | New | Line | Purpose |
|---|---|---|---|---|
| `AUDIT_TRANSPORT` | — | `"webm"` (`"pcm"` in `main_pcm.py`) | `main.py:162` | labels each record; scopes crash recovery to this process |
| `_audit_store` | — | `call_audit.AuditStore \| None = None` | `main.py:237` | one audit store per process, opened in `_startup` |
| `_NULL_AUDIT` | — | `call_audit.CallAudit(None, "no-call")` | `main.py:242` | a recorder that writes nowhere, for test doubles without `.audit` |

### 1.3 `CallSession` attributes

| Attribute | Old | New | Line |
|---|---|---|---|
| `self.call_id` | `uuid.uuid4().hex[:8]` — 8 hex chars, 32 bits | `uuid.uuid4().hex` — 32 hex chars, 128 bits | `main.py:463` |
| `self.audit` | — | `call_audit.CallAudit(_audit_store, self.call_id, transport=AUDIT_TRANSPORT, language=self.lang)` | `main.py:478` |
| `self.end_reason` | — | `str \| None = None`; set to `END_IDLE_TIMEOUT` by the idle timeout | `main.py:482` |

### 1.4 Local variables and parameters added in `main.py`

| Name | Where | Type / values | Purpose |
|---|---|---|---|
| `audit_redact` (param) | `_speak` | `str \| None` | withholds the spoken words from the record (patient history) |
| `audio` | `_speak`, `_reject_at_capacity` | `"none"` / `"synthesized"` / `"fallback_clip"` | what the caller actually heard |
| `tts_error` | `_speak` | `str \| None` | `"TypeName: message"` when TTS failed |
| `delivered` | `_speak`, `_reject_at_capacity` | `bool` | `True` only after the frames left the socket |
| `data` | `_resolve_intent` (fast path) | `dict` | the fast-path result, captured so it can be recorded before return |
| `audit` | `_run_turn` | `CallAudit` | the call's recorder, fetched once per turn |
| `secret` | `_run_turn` | `"verification_answer"` or `None` | set while a PIN / DOB is being answered; triggers redaction |
| `audit` | `_reject_at_capacity` | `CallAudit` | a record of its own for a refused caller |
| `ending` | `ws_audio` | `END_CLIENT_DISCONNECT` / `END_AGENT_SHUTDOWN` / `END_EXCEPTION` | how the call ended, as observed |
| `status` | `ws_audio` `finally` | final status string | returned by `session.audit.end(...)`, written to the log line |

### 1.5 New in `agent/tools_client.py`

| Name | Kind | Purpose |
|---|---|---|
| `_audited(action, *, redact=(), summarize=None)` | decorator | records `API_REQUEST` / `API_RESPONSE` around a backend method |
| `sig`, `bound`, `request` | locals in `_audited` | the real arguments bound by signature; redacted names replaced by `"[redacted]"` |
| `_without_token(result)` | function | response minus `token`, plus `token_issued: bool` |
| `_history_summary(result)` | function | `{found, reason, tests: <count>, appointments: <count>}` only |
| `_post_disclosure_refusal(...)` | method | the audited POST, split out of `record_disclosure_refusal` |

### 1.6 Constants in `agent/call_audit.py` (all new)

| Constant | Value | Meaning |
|---|---|---|
| `CALL_STARTED` | `"CALL_STARTED"` | event: socket accepted |
| `TRANSCRIPT` | `"TRANSCRIPT"` | event: what the caller said / keyed |
| `INTENT_DETECTED` | `"INTENT_DETECTED"` | event: what was understood |
| `SLOTS_EXTRACTED` | `"SLOTS_EXTRACTED"` | event: slots filled |
| `API_REQUEST` | `"API_REQUEST"` | event: request to clinic-api |
| `API_RESPONSE` | `"API_RESPONSE"` | event: what clinic-api returned |
| `AGENT_RESPONSE` | `"AGENT_RESPONSE"` | event: what the agent said |
| `AGENT_INTERRUPTED` | `"AGENT_INTERRUPTED"` | event: barge-in cut the reply |
| `ERROR` | `"ERROR"` | event: exception |
| `CALL_ENDED` | `"CALL_ENDED"` | event: how the call ended |
| `STATUS_COMPLETED` | `"completed"` | the caller's requests were answered |
| `STATUS_FAILED` | `"failed"` | last answer was a failure apology, or went unheard |
| `STATUS_ABANDONED` | `"abandoned"` | nothing answered, or a flow left half-done |
| `STATUS_ERROR` | `"error"` | an exception or shutdown cut the call |
| `STATUS_REJECTED` | `"rejected"` | turned away at capacity |
| `END_CLIENT_DISCONNECT` | `"client_disconnect"` | caller hung up |
| `END_IDLE_TIMEOUT` | `"idle_timeout"` | silence timeout |
| `END_EXCEPTION` | `"exception"` | session crashed |
| `END_AGENT_SHUTDOWN` | `"agent_shutdown"` | server stopping |
| `END_AT_CAPACITY` | `"at_capacity"` | refused at capacity |
| `END_AGENT_RESTARTED` | `"agent_restarted"` | closed by crash recovery on the next start |
| `_ABNORMAL_ENDS` | `{exception, agent_shutdown, agent_restarted}` | always give `error` |
| `FAILURE_FALLBACKS` | `{"tool_failure", "llm_failure"}` | fallback reasons that mean a failed answer |
| `REDACTED` | `"[redacted]"` | placeholder for a withheld argument |
| `BUSY_TIMEOUT_S` | `10.0` | SQLite busy timeout (writer thread only) |
| `QUEUE_MAX` | `10_000` | bounded write queue |
| `_BATCH_MAX` | `256` | rows per transaction |
| `_ERROR_LOG_INTERVAL_S` | `10.0` | rate limit for `AUDIT RECORD LOST` logs |
| `_MAX_ERROR_TEXT` | `500` | max chars of an error message kept |
| `_JOURNAL_MODES` | `{DELETE, WAL, TRUNCATE, PERSIST, MEMORY}` | accepted journal modes |
| `_SCHEMA`, `_INSERT_CALL`, `_INSERT_EVENT`, `_FINALIZE_CALL` | SQL | table definitions and statements |
| `_STOP` | `object()` | sentinel that stops the writer thread |
| `_current` | `ContextVar[CallAudit \| None]` | the current call's recorder, per asyncio task |

### 1.7 `AuditStore` attributes (new)

| Attribute | Meaning |
|---|---|
| `path` | database file path |
| `_q` | `queue.Queue(maxsize=QUEUE_MAX)` of pending writes |
| `_lock` | protects the counters |
| `write_failures` | rows that failed to write |
| `dropped` | rows dropped (queue full or store closed) |
| `last_error` | last failure message |
| `_last_error_log`, `_suppressed_errors` | log rate limiting |
| `_closed` | set by `close()` |
| `available` | database could be opened / written |
| `_thread` | the `call-audit-writer` daemon thread |

### 1.8 `CallAudit` attributes (new)

| Attribute | Meaning |
|---|---|
| `call_id` | the call's id (from `CallSession`, or a new uuid for a refused call) |
| `_store` | the `AuditStore`, or `None` (writes nowhere) |
| `_seq`, `_seq_lock` | event sequence counter, 1, 2, 3 … |
| `turn` | current caller turn (0 = before the caller spoke) |
| `started_at`, `_t0` | UTC start time, monotonic start for duration |
| `transport`, `language` | `webm`/`pcm`, current language |
| `caller_phone` | first phone number the backend acted on |
| `api_calls`, `api_failures`, `errors`, `served_turns` | counters written at finalisation |
| `_turn_understood`, `_turn_answered`, `_turn_crashed`, `_last_response_failed` | inputs to the final-status rules |
| `ended`, `final_status` | finalisation state (first `end()` wins) |

### 1.9 Database columns (new file `call_audit.db`)

**`call_records`** — `call_id` (PK), `transport`, `language`, `caller_phone`, `started_at`, `ended_at`, `duration_s`,
`final_status`, `termination_reason`, `turns`, `served_turns`, `api_calls`, `api_failures`, `errors`.
Indexes: `ix_call_records_started`, `ix_call_records_status`.

**`call_events`** — `event_id` (PK), `call_id`, `seq`, `ts`, `event_type`, `turn`, `success`, `data` (JSON).
`UNIQUE(call_id, seq)`. Index: `ix_call_events_type`.

---

## 2. `main.py` — Old vs New

### 2.1 Imports

```python
# OLD
import difflib
import io
...
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
...
from agent.vad_stream import TurnDetector
```

```python
# NEW
import difflib
import hmac                                                     # constant-time token compare
import io
...
from fastapi import FastAPI, Header, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
...
from agent.vad_stream import TurnDetector
from agent import call_audit
```

### 2.2 Module constant and globals

```python
# OLD
_fast_path: FastPath | None = None
```

```python
# NEW  (main.py:162, 237-246)
AUDIT_TRANSPORT = "webm"          # the generator flips this to "pcm" in main_pcm.py

_fast_path: FastPath | None = None
_audit_store: call_audit.AuditStore | None = None

# Stands in for a session with no CallAudit of its own (a test double built
# without CallSession). Writes nowhere.
_NULL_AUDIT = call_audit.CallAudit(None, "no-call")


def _audit(session) -> call_audit.CallAudit:
    return getattr(session, "audit", None) or _NULL_AUDIT
```

### 2.3 `_startup`

```python
# OLD
async def _startup():
    global _asr, _turn_detector, _tools, _tts, _intent_cache, _fast_path
    logger.info("loading IndicConformer...")
    ...
```

```python
# NEW  (main.py:250)
async def _startup():
    global _asr, _turn_detector, _tools, _tts, _intent_cache, _fast_path, _audit_store
    # FIRST, before any model loads: a caller accepted the moment startup
    # finishes must already have somewhere to be recorded.
    _audit_store = call_audit.AuditStore()
    recovered = _audit_store.recover_unfinished(AUDIT_TRANSPORT)
    if recovered:
        logger.warning("audit: finalised %d call record(s) a previous run left open",
                       recovered)
    if not os.environ.get("VOICE_AGENT_AUDIT_TOKEN"):
        logger.warning("audit: VOICE_AGENT_AUDIT_TOKEN is unset -- /api/audit/* is "
                       "readable without a token. Set it on any non-bench deployment.")

    logger.info("loading IndicConformer...")
    ...                                                         # rest unchanged
```

### 2.4 `_shutdown`

```python
# OLD
async def _shutdown():
    if _tools:
        await _tools.aclose()
    if _tts:
        await _tts.aclose()
    _shutdown_http_pool()
```

```python
# NEW  (main.py:308)
async def _shutdown():
    if _tools:
        await _tools.aclose()
    if _tts:
        await _tts.aclose()
    _shutdown_http_pool()
    # Last, so everything recorded above is committed.
    if _audit_store:
        await asyncio.to_thread(_audit_store.close)
```

### 2.5 `health`

```python
# OLD
        "active_calls": _active_calls,
        "max_calls": MAX_CONCURRENT_CALLS,
    }
```

```python
# NEW  (main.py:336)
        "active_calls": _active_calls,
        "max_calls": MAX_CONCURRENT_CALLS,
        # Non-zero write_failures or dropped means some call records are incomplete.
        "audit": _audit_store.health() if _audit_store else {"available": False},
    }
```

### 2.6 Staff read endpoints — OLD: none. NEW (`main.py:371-411`)

```python
def _audit_read_denied(token: str | None):
    expected = os.environ.get("VOICE_AGENT_AUDIT_TOKEN", "")
    if expected and not hmac.compare_digest((token or "").encode(), expected.encode()):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    return None


@app.get("/api/audit/calls")
async def audit_calls(limit: int = Query(50, ge=1, le=500),
                      status: str | None = Query(None),
                      x_audit_token: str | None = Header(default=None)):
    denied = _audit_read_denied(x_audit_token)
    if denied is not None:
        return denied
    if _audit_store is None:
        return JSONResponse(status_code=503, content={"error": "audit store not initialised"})
    calls = await asyncio.to_thread(_audit_store.list_calls, limit, status)
    return {"count": len(calls), "calls": calls}


@app.get("/api/audit/calls/{call_id}")
async def audit_call(call_id: str, x_audit_token: str | None = Header(default=None)):
    denied = _audit_read_denied(x_audit_token)
    if denied is not None:
        return denied
    if _audit_store is None:
        return JSONResponse(status_code=503, content={"error": "audit store not initialised"})
    record = await asyncio.to_thread(_audit_store.get_call, call_id)
    if record is None:
        return JSONResponse(status_code=404, content={"error": "no such call"})
    return record
```

### 2.7 `CallSession.__init__`

```python
# OLD
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.call_id = uuid.uuid4().hex[:8]
        self.tmpdir = tempfile.mkdtemp(prefix=f"kcd_call_{self.call_id}_")
        ...
        self.lang = lang_mod.default_lang()

        # HISTORY VERIFICATION STATE -- Author: Chakravardhan
```

```python
# NEW  (main.py:463-482)
    def __init__(self, ws: WebSocket):
        self.ws = ws
        # The full 128-bit uuid4 -- now the permanent key of the audit record.
        self.call_id = uuid.uuid4().hex
        self.tmpdir = tempfile.mkdtemp(prefix=f"kcd_call_{self.call_id}_")
        ...
        self.lang = lang_mod.default_lang()

        # Opened here, the moment the socket is accepted -> CALL_STARTED
        self.audit = call_audit.CallAudit(_audit_store, self.call_id,
                                          transport=AUDIT_TRANSPORT, language=self.lang)
        # Set by code that ends the call itself (the idle timeout).
        self.end_reason: str | None = None

        # HISTORY VERIFICATION STATE -- Author: Chakravardhan
```

### 2.8 `_speak`

```python
# OLD
async def _speak(session: CallSession, text_bn: str, fallback_reason: str | None = None):
    await session.send_json("AI", text_bn)
    try:
        wav = await _tts.synthesize(text_bn, session.lang)
    except Exception as e:
        logger.warning("[%s] TTS failed (%s) -- using fallback audio", session.call_id, e)
        wav = _tts.fallback_audio(fallback_reason or "tts_failure")

    session.echo.note_playback(session.playback_start_s(),
                               pcm_from_wav_bytes(wav, session.echo.sample_rate))
    session.hold_gate_for(_wav_duration_s(wav))
    await session.send_audio(wav)
```

```python
# NEW  (main.py:636-680)
async def _speak(session: CallSession, text_bn: str, fallback_reason: str | None = None,
                 audit_redact: str | None = None):
    audio, tts_error, delivered = "none", None, False
    try:
        await session.send_json("AI", text_bn)
        try:
            wav = await _tts.synthesize(text_bn, session.lang)
            audio = "synthesized" if wav else "none"
        except Exception as e:
            logger.warning("[%s] TTS failed (%s) -- using fallback audio", session.call_id, e)
            tts_error = f"{type(e).__name__}: {e}"
            wav = _tts.fallback_audio(fallback_reason or "tts_failure")
            audio = "fallback_clip" if wav else "none"

        session.echo.note_playback(session.playback_start_s(),
                                   pcm_from_wav_bytes(wav, session.echo.sample_rate))
        session.hold_gate_for(_wav_duration_s(wav))
        await session.send_audio(wav)
        delivered = True
    finally:
        # AGENT_RESPONSE -- recorded whether or not it reached the caller
        _audit(session).agent_response(
            text_bn, lang=getattr(session, "lang", None), audio=audio, delivered=delivered,
            fallback_reason=fallback_reason, tts_error=tts_error, redact=audit_redact)
```

### 2.9 `_record_intent` (new) and `_resolve_intent`

```python
# OLD
    if _fast_path is not None:
        hit = await asyncio.to_thread(_fast_path.resolve, text)
        if hit is not None:
            logger.info(...)
            return hit.as_llm_shape()

    cached, how = await run_http(_intent_cache.get, text)
    if cached is not None:
        logger.info("[%s] intent cache %s hit", session.call_id, how)
        return cached

    data, diag = await run_http(extract_intent, text)
    logger.info(...)
    await run_http(_intent_cache.put, text, data)
    return data
```

```python
# NEW  (main.py:694-745)
def _record_intent(session, data: dict, source: str, **detail) -> None:
    """INTENT_DETECTED and SLOTS_EXTRACTED, from the dict that drives the turn."""
    _audit(session).intent(data.get("intent"), source, slots=data.get("slots") or {},
                           direct_reply_bn=data.get("direct_reply_bn"), **detail)


async def _resolve_intent(session: CallSession, text: str) -> dict:
    if _fast_path is not None:
        hit = await asyncio.to_thread(_fast_path.resolve, text)
        if hit is not None:
            logger.info(...)
            data = hit.as_llm_shape()
            _record_intent(session, data, "fast_path", confidence=round(hit.confidence, 3),
                           matched_form=hit.matched_form)
            return data

    cached, how = await run_http(_intent_cache.get, text)
    if cached is not None:
        logger.info("[%s] intent cache %s hit", session.call_id, how)
        _record_intent(session, cached, f"intent_cache_{how}")
        return cached

    data, diag = await run_http(extract_intent, text)
    logger.info(...)
    # Recorded BEFORE the cache write, so a cache failure cannot lose it.
    _record_intent(session, data, "llm", attempts=diag["attempts"],
                   latency_s=round(diag["total_time_s"], 3), retry_errors=diag["errors"])
    await run_http(_intent_cache.put, text, data)
    return data
```

### 2.10 `_continue_pending` — four capture points (mid-flow, no LLM)

```python
# OLD                                             # NEW
    if is_negative(text):                             if is_negative(text):
                                                          _audit(session).intent("abandon_flow", "slot_parse",
                                                                                 flow=awaiting)            # :884
        session.pending = None                            session.pending = None
```

```python
# OLD
        match = _match_candidate_doctor(text, pending.get("candidates") or [])
        if match is None:
# NEW  (main.py:891)
        match = _match_candidate_doctor(text, pending.get("candidates") or [])
        _audit(session).slots("slot_parse", {"doctor_name": match}, awaiting="doctor_choice")
        if match is None:
```

```python
# OLD
        value = parse_date(text, offered_date=pending.get("offered_date"))
        if value is None:
# NEW  (main.py:937)
        value = parse_date(text, offered_date=pending.get("offered_date"))
        _audit(session).slots("slot_parse", {"date": value}, awaiting="department_date")
        if value is None:
```

```python
# OLD
        value = _clean_patient_name(text)

# NEW  (main.py:993)
        value = _clean_patient_name(text)
    _audit(session).slots("slot_parse", {awaiting: value}, awaiting=awaiting)
```

### 2.11 `_speak_history`

```python
# OLD
    await _speak(session, history_reply(result, session.lang))
```

```python
# NEW  (main.py:1186)
    # The fact of disclosure is recorded; the medical history itself is not copied.
    await _speak(session, history_reply(result, session.lang), audit_redact="patient_history")
```

### 2.12 `_dispatch_turn` split into a wrapper and `_run_turn`

```python
# OLD
async def _dispatch_turn(session: CallSession, utterance_wav: str,
                         text_override: str | None = None):
    """One full turn: ASR -> intent -> tool -> templated reply -> TTS. ..."""
    async with session.dispatch_lock:
        quality = None
        ...
```

```python
# NEW  (main.py:1234-1265)
async def _dispatch_turn(session: CallSession, utterance_wav: str,
                         text_override: str | None = None):
    """_run_turn, with any exception it raises put on the call's record."""
    try:
        await _run_turn(session, utterance_wav, text_override)
    except Exception as e:
        _audit(session).error("turn", e)          # ERROR with call id + file:line
        raise                                     # re-raised unchanged


async def _run_turn(session: CallSession, utterance_wav: str,
                    text_override: str | None = None):
    """One full turn: ASR -> intent -> tool -> templated reply -> TTS. ..."""
    async with session.dispatch_lock:
        quality = None
        audit = _audit(session)
        audit.begin_turn()
        secret = ("verification_answer"
                  if (session.pending or {}).get("awaiting") == "history_verify" else None)
        ...
```

### 2.13 `_run_turn` — capture points (each is an insertion into the old body)

```python
# keypad input                                                          (main.py:1277)
            text = text_override.strip()
            if not text:
                return
+           audit.transcript(text, source="keypad", redacted=secret)
```

```python
# audio conditioning failed (fail-open)                                 (main.py:1326)
                    logger.warning("[%s] conditioning failed (%s) -- sending raw clip", ...)
+                   audit.error("audio_conditioning", e, handled=True, fail_open=True)
```

```python
# clip rejected by the quality floor                                    (main.py:1329)
                if quality is not None and not quality.usable:
+                   audit.transcript(None, source="speech", status="rejected_low_quality",
+                                    audio=call_audit.describe_quality(quality))
                    METRICS.record_turn(quality, success=False, ...)
```

```python
# ASR returned empty text                                               (main.py:1359)
                logger.info("[%s] ASR returned empty text", session.call_id)
+               audit.transcript("", source="speech", status="empty",
+                                decoder_used=getattr(asr_result, "decoder_used", None),
+                                audio=call_audit.describe_quality(quality))
```

```python
# a real transcript                                                     (main.py:1372)
            session.failures.record_success()
+           audit.transcript(text, source="speech", redacted=secret,
+                            decoder_used=getattr(asr_result, "decoder_used", None),
+                            decoder_agreement=getattr(asr_result, "decoder_agreement", None),
+                            audio=call_audit.describe_quality(quality))
```

```python
# language switch / unavailable language                                (main.py:1395, 1408)
+           audit.intent("language_switch", "keyword", language_from=session.lang,
+                        language_to=switched)
            session.lang = switched
...
+           audit.intent("language_unavailable", "keyword", requested=unavailable)
            await _speak(session, language_unavailable_reply(session.lang))
```

```python
# intent extraction failed                                              (main.py:1422)
            logger.error("[%s] intent extraction failed: %s", session.call_id, e)
+           audit.error("intent_extraction", e, handled=True)
            await _speak(session, _t(session.lang, "fallback.llm_failure"), fallback_reason="llm_failure")
```

### 2.14 `_check_barge_in`

```python
# OLD
    logger.info("[%s] barge-in: %s", session.call_id, verdict.as_dict())
```

```python
# NEW  (main.py:1668)
    logger.info("[%s] barge-in: %s", session.call_id, verdict.as_dict())
    # The AGENT_RESPONSE before this event was NOT heard in full.
    _audit(session).record(call_audit.AGENT_INTERRUPTED,
                           {"at_call_s": round(now_s, 3), "verdict": verdict.as_dict()})
```

### 2.15 `_turn_poll_loop` — idle timeout

```python
# OLD
            logger.info("[%s] idle timeout, closing", session.call_id)
            await _speak(session, "লাইনে কোনো সাড়া পাচ্ছি না, কল শেষ করছি। ধন্যবাদ।")
```

```python
# NEW  (main.py:1705)
            logger.info("[%s] idle timeout, closing", session.call_id)
            session.end_reason = call_audit.END_IDLE_TIMEOUT
            await _speak(session, "লাইনে কোনো সাড়া পাচ্ছি না, কল শেষ করছি। ধন্যবাদ।")
```

### 2.16 `_note_task_crash` — OLD: none. NEW (`main.py:1806`)

```python
def _note_task_crash(session: CallSession, stage: str, task: asyncio.Task) -> None:
    """Done-callback: records a background-task crash the moment it happens."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("[%s] %s crashed: %r", session.call_id, stage, exc, exc_info=exc)
        _audit(session).error(stage, exc)
```

### 2.17 `_reject_at_capacity`

```python
# OLD
async def _reject_at_capacity(ws: WebSocket):
    logger.warning("at capacity (%d/%d active) -- refusing call",
                   _active_calls, MAX_CONCURRENT_CALLS)
    with contextlib.suppress(Exception):
        await ws.send_text(json.dumps({"sender": "AI", "text": BUSY_LINE},
                                      ensure_ascii=False))
    with contextlib.suppress(Exception):
        await ws.send_bytes(await _tts.synthesize(BUSY_LINE))
    await asyncio.sleep(0.25)
    with contextlib.suppress(Exception):
        await ws.close()
```

```python
# NEW  (main.py:1820-1852)
async def _reject_at_capacity(ws: WebSocket):
    audit = call_audit.CallAudit(_audit_store, transport=AUDIT_TRANSPORT,
                                 language=lang_mod.default_lang())          # CALL_STARTED
    logger.warning("[%s] at capacity (%d/%d active) -- refusing call",
                   audit.call_id, _active_calls, MAX_CONCURRENT_CALLS)
    delivered, audio = False, "none"
    with contextlib.suppress(Exception):
        await ws.send_text(json.dumps({"sender": "AI", "text": BUSY_LINE},
                                      ensure_ascii=False))
        delivered = True
    with contextlib.suppress(Exception):
        await ws.send_bytes(await _tts.synthesize(BUSY_LINE))
        audio = "synthesized"
    audit.agent_response(BUSY_LINE, lang=lang_mod.default_lang(), audio=audio,
                         delivered=delivered)                               # AGENT_RESPONSE
    await asyncio.sleep(0.25)
    with contextlib.suppress(Exception):
        await ws.close()
    audit.end(call_audit.END_AT_CAPACITY, active_calls=_active_calls,
              max_calls=MAX_CONCURRENT_CALLS)                               # CALL_ENDED rejected
```

### 2.18 `ws_audio`

```python
# OLD
    session = CallSession(ws)
    logger.info("[%s] call started (%d/%d active)", ...)
    poll_task = asyncio.create_task(_turn_poll_loop(session))

    try:
        await _speak(session, "নমস্কার, ...")
        while True:
            ...
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("[%s] session crashed", session.call_id)
    finally:
        poll_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poll_task                # a crashed poll loop re-raised here and skipped the rest
        session.cleanup()
        _active_calls -= 1
        logger.info("[%s] call ended (%d/%d active)",
                    session.call_id, _active_calls, MAX_CONCURRENT_CALLS)
```

```python
# NEW  (main.py:1867-1922)
    session = CallSession(ws)
    call_audit.bind(session.audit)                       # every task of this call inherits it
    logger.info("[%s] call started (%d/%d active)", ...)
    poll_task = asyncio.create_task(_turn_poll_loop(session))
    poll_task.add_done_callback(lambda t: _note_task_crash(session, "turn_poll_loop", t))
    ending = call_audit.END_CLIENT_DISCONNECT

    try:
        await _speak(session, "নমস্কার, ...")
        while True:
            ...
    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        ending = call_audit.END_AGENT_SHUTDOWN           # server stopping under the call
        raise
    except Exception as e:
        logger.exception("[%s] session crashed", session.call_id)
        session.audit.error("session", e)                # ERROR
        ending = call_audit.END_EXCEPTION
    finally:
        poll_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await poll_task                              # a crashed poll loop no longer skips the rest
        session.cleanup()
        _active_calls -= 1
        status = session.audit.end(                      # CALL_ENDED + finalise the row
            session.end_reason or ending,
            pending_flow=(session.pending or {}).get("awaiting"),
            language=session.lang)
        logger.info("[%s] call ended (%d/%d active) -- %s",
                    session.call_id, _active_calls, MAX_CONCURRENT_CALLS, status)
```

---

## 3. `agent/tools_client.py` — Old vs New

### 3.1 Imports

```python
# OLD
from __future__ import annotations

import httpx
```

```python
# NEW
from __future__ import annotations

import functools
import inspect

import httpx

from agent import call_audit
```

### 3.2 The decorator and reducers — OLD: none. NEW (`tools_client.py:50-111`)

```python
def _audited(action: str, *, redact: tuple[str, ...] = (), summarize=None):
    def deco(fn):
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        async def wrapper(self, *args, **kwargs):
            audit = call_audit.current()
            if audit is None:                              # outside a call: pass-through
                return await fn(self, *args, **kwargs)
            try:
                bound = sig.bind(self, *args, **kwargs)
                bound.apply_defaults()
                request = {k: (call_audit.REDACTED if k in redact else v)
                           for k, v in bound.arguments.items() if k != "self"}
            except TypeError:
                request = {}
            return await audit.api_call(action, request,
                                        lambda: fn(self, *args, **kwargs), summarize)
        return wrapper
    return deco


def _without_token(result):
    if not isinstance(result, dict):
        return result
    out = {k: v for k, v in result.items() if k != "token"}
    out["token_issued"] = bool(result.get("token"))
    return out


def _history_summary(result):
    if not isinstance(result, dict):
        return result
    return {"found": result.get("found"), "reason": result.get("reason"),
            "tests": len(result.get("tests") or []),
            "appointments": len(result.get("appointments") or [])}
```

### 3.3 Backend methods — body unchanged, one decorator added

```python
# OLD                                             # NEW
    async def get_test_rate(self, test_name):         @_audited("get_test_rate")
        ...                                           async def get_test_rate(self, test_name):
                                                          ...                     # body unchanged
```

| Line | Method | Decorator added |
|---|---|---|
| 129 | `get_test_rate` | `@_audited("get_test_rate")` |
| 148 | `get_doctor_availability` | `@_audited("get_doctor_availability")` |
| 177 | `book_appointment` | `@_audited("book_appointment")` |
| 208 | `reschedule_appointment` | `@_audited("reschedule_appointment")` |
| 234 | `cancel_appointment` | `@_audited("cancel_appointment")` |
| 264 | `begin_verification` | `@_audited("begin_verification")` |
| 285 | `verify_caller` | `@_audited("verify_caller", redact=("answer",), summarize=_without_token)` |
| 305 | `read_history` | `@_audited("read_history", redact=("token",), summarize=_history_summary)` |
| 329 | `_post_disclosure_refusal` (new) | `@_audited("record_disclosure_refusal")` |
| 350 | `get_doctors_by_department` | `@_audited("get_doctors_by_department")` |

### 3.4 `record_disclosure_refusal`

```python
# OLD -- a failed POST was swallowed inside, so it looked like a success
    async def record_disclosure_refusal(self, phone: str, reason: str,
                                         call_id: str | None = None) -> None:
        try:
            r = await self._client.post(
                "/api/v1/history/refusal",
                json={"phone": phone, "reason": reason, "call_id": call_id})
            r.raise_for_status()
        except httpx.HTTPError:
            pass
```

```python
# NEW -- the POST raises inside the audited method; the public method swallows OUTSIDE it
    async def record_disclosure_refusal(self, phone: str, reason: str,
                                         call_id: str | None = None) -> None:
        try:
            await self._post_disclosure_refusal(phone, reason, call_id)
        except ToolCallError:
            pass

    @_audited("record_disclosure_refusal")
    async def _post_disclosure_refusal(self, phone: str, reason: str,
                                       call_id: str | None) -> None:
        try:
            r = await self._client.post(
                "/api/v1/history/refusal",
                json={"phone": phone, "reason": reason, "call_id": call_id})
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise ToolCallError(f"record_disclosure_refusal(reason={reason!r}): {e}") from e
```

Behaviour seen by callers is unchanged: it still never raises.

---

## 4. `agent/call_audit.py` — OLD: did not exist. NEW: summary of every function

| Line | Name | What it does |
|---|---|---|
| 197 | `default_path()` | `VOICE_AGENT_AUDIT_DB` or `/workspace/call_audit.db` |
| 202 | `_journal_mode()` | WAL on vast, DELETE otherwise; `CLINIC_DB_JOURNAL_MODE` overrides |
| 213 | `_utcnow()` | UTC ISO timestamp with milliseconds |
| 219 | `_where(exc)` | `file:line in function` from the traceback |
| 232 | `_error_text(exc)` | error message, max 500 chars |
| 236 | `classify_result(result)` | `(outcome, success, reason)` from the backend's own fields |
| 259 | `describe_quality(quality)` | audio-quality numbers as a dict |
| 286 | `AuditStore` | durable storage, one writer thread |
| 295 | `AuditStore.__init__` | open DB, create schema, start thread; failure → `available=False` |
| 320 | `_connect` | SQLite connection with busy timeout and journal mode |
| 329 | `_fail` | count and log (rate-limited) a lost write; never raises |
| 350 | `_submit` | non-blocking enqueue; full queue → `dropped += 1` |
| 364 | `_write` | batch transaction; on failure retry row by row |
| 388 | `_run` | the writer loop |
| 433 | `start_call` | enqueue the `call_records` INSERT |
| 436 | `add_event` | enqueue a `call_events` INSERT |
| 443 | `end_call` | enqueue the write-once finalising UPDATE |
| 452 | `flush` | wait until everything queued is committed |
| 463 | `close` | flush, stop the thread |
| 475 | `recover_unfinished(transport)` | close records left open by a killed process as `error / agent_restarted` |
| 548 | `list_calls(limit, status)` | newest calls first |
| 564 | `get_call(call_id)` | full record + `integrity` block |
| 614 | `health()` | `available`, `queued`, `write_failures`, `dropped`, `last_error` |
| 629 | `CallAudit` | the per-call recorder |
| 637 | `CallAudit.__init__` | insert the call row, record `CALL_STARTED` |
| 674 | `record` | number, serialise and enqueue one event; never raises |
| 697 | `begin_turn` | start a new caller turn |
| 706 | `transcript` | `TRANSCRIPT`, with optional redaction (length kept) |
| 721 | `intent` | `INTENT_DETECTED` + `SLOTS_EXTRACTED` |
| 731 | `slots` | `SLOTS_EXTRACTED` from mid-flow parsers |
| 738 | `agent_response` | `AGENT_RESPONSE`; counts served turns; fallback clip = failed |
| 770 | `error` | `ERROR` with stage, handled, type, message, where |
| 787 | `note_caller_phone` | first phone number wins |
| 796 | `api_call` | `API_REQUEST`, run the call, `API_RESPONSE`; re-raises unchanged |
| 832 | `_api_done` | builds the `API_RESPONSE` event with latency |
| 842 | `_final_status` | abnormal → error; capacity → rejected; unanswered crash → error; failed last reply → failed; nothing served / open flow → abandoned; else completed |
| 855 | `end` | `CALL_ENDED` + finalise the row; first call wins |
| 907 | `bind(audit)` | set the current call's recorder (ContextVar) |
| 911 | `current()` | get the current call's recorder |

---

## 5. `tools/make_pcm_variant.py` — Old vs New

```python
# OLD
    rep(... "tail cost")

    rep('app.mount("/", StaticFiles(directory="static", html=True), name="static")', ...)
```

```python
# NEW  (make_pcm_variant.py:193)
    rep(... "tail cost")

    rep('AUDIT_TRANSPORT = "webm"', 'AUDIT_TRANSPORT = "pcm"', "audit transport")

    rep('app.mount("/", StaticFiles(directory="static", html=True), name="static")', ...)
```

`main_pcm.py` is regenerated from `main.py` with this replacement, so it carries every change in section 2 with
`AUDIT_TRANSPORT = "pcm"` (`main_pcm.py:178`).

---

## 6. `README.md` — Old vs New

- **Old:** no mention of call records.
- **New (`README.md:128`):** section *"Every call leaves a complete record"* — what is recorded, that it lives in
  `call_audit.db` beside `clinic.db`, the two staff endpoints, `VOICE_AGENT_AUDIT_TOKEN`, what is withheld, and a
  pointer to `IMPLEMENTATION_CALL_AUDIT.md`.

---

## 7. Tests — Old vs New

- **Old:** no test of any call record (there was no record).
- **New:** `tests/test_call_audit.py`, 23 tests, all passing. They drive the real `_dispatch_turn`, `ws_audio`,
  `ClinicToolsClient` and a real SQLite file; only ASR, LLM and TTS are faked and clinic-api is an
  `httpx.MockTransport`. Every assertion reads back from the database.

```bash
python -m pytest tests/test_call_audit.py -v
```

---

## 8. Behaviour changes visible outside the audit

| Area | Old | New |
|---|---|---|
| Call id in logs and `disclosure_audit.call_id` | 8 hex chars | 32 hex chars |
| Crashed poll loop | re-raised in `finally`, skipped cleanup, leaked a capacity slot | suppressed, logged and recorded by `_note_task_crash`; cleanup runs |
| Crash inside a turn | anonymous "Task exception was never retrieved" | `ERROR` event with call id and `file:line`; re-raised unchanged |
| Capacity refusal log | `at capacity …` | `[<call_id>] at capacity …` |
| Call-ended log | `call ended (n/m active)` | `call ended (n/m active) -- <final_status>` |
| `/api/health` | no audit info | `audit` block |
| New endpoints | — | `GET /api/audit/calls`, `GET /api/audit/calls/{call_id}` |
| New file on disk | — | `/workspace/call_audit.db` |
