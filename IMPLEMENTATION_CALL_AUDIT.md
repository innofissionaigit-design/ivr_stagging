# Every Call Leaves a Complete Record — Implementation

**Author:** Chakravardhan
**Branch:** `dev_chakravardhan` (base commit `76c370c`), changes uncommitted and unpushed
**Story:** *As a hospital, I want each call to leave an accurate entry, so that the agent is auditable in the same way a member of staff is.*

---

## 1. Requirement

Every call must leave a durable, structured record from which the hospital can reconstruct:

```text
WHO called → WHEN it started → WHAT the caller said → WHAT the system understood
→ WHICH intent/slots → WHICH backend action → WHAT the backend ACTUALLY returned
→ WHAT the agent told the caller → HOW the call ended
```

That covers successful, failed, misunderstood and abandoned calls, API failures, timeouts, disconnects and exceptions. A failed call must not vanish.

In plain technical terms, that means:

* A **per-call record** (one row) plus an **append-only, numbered event list** (one row per step).
* Each event is written **at the point in the code where it happened, from the value the code was holding**. Nothing is reconstructed afterwards, and nothing is summarised by the LLM.
* Backend outcomes come **from the backend's response or exception**, never from what the LLM intended.
* **Auditing can never break a call.** Audit failures are reported, not hidden.

---

## 2. Repository Findings

### 2.1 Existing call flow

| Stage | Where | Notes |
|---|---|---|
| Call start | `main.py:ws_audio` → `CallSession.__init__` | admission control first (`_reject_at_capacity`) |
| Caller audio in | `ws_audio` receive loop → `CallSession.append` | WebM (`main.py`) or raw PCM (`main_pcm.py`) |
| Turn detection | `_turn_poll_loop` → `TurnDetector.poll` | spawns `_dispatch_turn` as a fire-and-forget task |
| Quality gate | `_dispatch_turn` → `condition_wav_file` | a rejected clip never reaches ASR |
| Transcript | `_dispatch_turn` → `TurnASR.transcribe_utterance` | returns `ASRResult(text, decoder_used, decoder_agreement)` |
| Keypad | `_handle_control` (`dtmf`) → `_handle_keypad_digit` → `_dispatch_turn(text_override=…)` | |
| Intent + slots | `_resolve_intent`: fast path → semantic cache → `extract_intent` (Ollama) | |
| Mid-flow slots | `_continue_pending` → `slot_parse.parse_*` | the LLM is **not** consulted mid-flow |
| Backend actions | `ClinicToolsClient.*` (`agent/tools_client.py`) | raises `ToolCallError` on infrastructure failure |
| Reply + TTS | `reply_templates.*` → `_speak` → `TTSClient.synthesize` | pre-recorded fallback clip if TTS fails |
| Barge-in | `_check_barge_in` | stops playback part-way through |
| Call end | `ws_audio` `finally` | hang-up, idle timeout (`_turn_poll_loop`), exception, shutdown |

### 2.2 Existing logging and audit

* Python `logging` with a `[call_id]` prefix. The logs are prose for engineers, get rotated, and can't be queried per call.
* `METRICS` (`agent/quality_metrics.py`): process-wide counters, not per call.
* **clinic-api already has two narrow audit trails.** `disclosure_audit` records history-verification attempts and already has a `call_id` column. `notification_attempts` is the SMS ledger. Neither covers the conversation.
* **`call_id` was `uuid.uuid4().hex[:8]`**, which is 32 bits of randomness.

### 2.3 Existing persistence

* **clinic-api:** SQLAlchemy + SQLite on `/workspace/clinic.db` (`clinic-api/db.py`), with the journal mode chosen per provider (DELETE on RunPod, WAL on vast.ai).
* **Voice agent:** no persistence at all. `requirements.txt` has no SQLAlchemy.
* **Deployment** (`deploy/start_all.sh`): `main.py` (:8100) and `main_pcm.py` (:8101) run **side by side**, one process each. `main_pcm.py` is **generated** from `main.py` by `tools/make_pcm_variant.py`, which refuses to write if the reasoning half diverges.

### 2.4 Existing privacy model, followed rather than replaced

* PIN and date-of-birth answers are never logged (`tools_client.verify_caller` keeps the answer out of its error text), and `disclosure_audit` stores no secret.
* The history token dies with the call, and every clinic-api history route is a POST so the token never lands in an access log.
* Phone numbers and patient names **are** stored as plain columns (`appointments`, `notification_attempts`, `disclosure_audit`). Retention is flagged as outstanding work.

---

## 3. OLD Implementation

```python
# OLD: main.py

class CallSession:
    def __init__(self, ws):
        self.call_id = uuid.uuid4().hex[:8]          # 32-bit, log-prefix only
        ...

async def _speak(session, text_bn, fallback_reason=None):
    await session.send_json("AI", text_bn)
    try:
        wav = await _tts.synthesize(text_bn, session.lang)
    except Exception as e:
        logger.warning("[%s] TTS failed (%s) -- using fallback audio", session.call_id, e)
        wav = _tts.fallback_audio(fallback_reason or "tts_failure")
    ...
    await session.send_audio(wav)                     # nothing records what was said

async def _dispatch_turn(session, utterance_wav, text_override=None):
    async with session.dispatch_lock:
        ...
        asr_result = await node.transcribe_utterance(utterance_wav)   # transcript: log only
        ...
        data = await _resolve_intent(session, text)                   # intent: log only
        ...
        result = await _tools.get_test_rate(slots["test_name"])       # backend result: not kept
        await _speak(session, test_rate_reply(slots, result, session.lang))
    # an exception here dies inside a fire-and-forget task:
    # "Task exception was never retrieved", with no call attached

@app.websocket("/ws/audio")
async def ws_audio(ws):
    ...
    except Exception:
        logger.exception("[%s] session crashed", session.call_id)
    finally:
        poll_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poll_task            # a CRASHED poll loop re-raises here and
        session.cleanup()              # skips cleanup, the capacity slot, everything
        _active_calls -= 1
        logger.info("[%s] call ended ...", session.call_id)   # a log line, no outcome
```

```python
# OLD: agent/tools_client.py

async def get_test_rate(self, test_name):
    try:
        r = await self._client.get("/api/v1/tests/search", params={"name": test_name})
        r.raise_for_status()
        return r.json()                          # the backend's answer, kept nowhere
    except httpx.HTTPError as e:
        raise ToolCallError(...) from e          # sometimes swallowed by main.py (payment)
```

**Problem:** there was no persistent audit trail at all. A call left only log lines keyed by an 8-character id. What the caller said, what was understood, what clinic-api actually returned and what the caller heard could not be reconstructed.

Failures were the worst covered:
* A swallowed backend error (payment, report collection) left nothing.
* An exception in a turn surfaced only as an anonymous asyncio warning.
* A crashed poll loop skipped the end-of-call path entirely.
* A caller refused at capacity left one log line.

---

## 4. NEW Implementation

```python
# NEW: main.py

class CallSession:
    def __init__(self, ws):
        self.call_id = uuid.uuid4().hex                              # full 128-bit
        ...
        self.audit = call_audit.CallAudit(_audit_store, self.call_id,  # CALL_STARTED
                                          transport=AUDIT_TRANSPORT, language=self.lang)
        self.end_reason = None

async def _speak(session, text_bn, fallback_reason=None, audit_redact=None):
    audio, tts_error, delivered = "none", None, False
    try:
        ...                                   # unchanged, plus: note which audio played
        await session.send_audio(wav)
        delivered = True
    finally:
        _audit(session).agent_response(text_bn, ..., audio=audio,      # AGENT_RESPONSE
                                       delivered=delivered, tts_error=tts_error, ...)

async def _resolve_intent(session, text):
    ... fast path  -> _record_intent(session, data, "fast_path", confidence=...)
    ... cache hit  -> _record_intent(session, cached, "intent_cache_exact|semantic")
    ... LLM        -> _record_intent(session, data, "llm", attempts=..., retry_errors=...)
                      # INTENT_DETECTED + SLOTS_EXTRACTED

async def _dispatch_turn(session, utterance_wav, text_override=None):
    try:
        await _run_turn(session, utterance_wav, text_override)       # the old body
    except Exception as e:
        _audit(session).error("turn", e)                             # ERROR, then
        raise                                                        # re-raised unchanged

async def _run_turn(...):                                            # old _dispatch_turn body
    async with session.dispatch_lock:
        audit.begin_turn()
        ...
        audit.transcript(text, source="speech", decoder_used=..., audio=...)  # TRANSCRIPT
        ...

@app.websocket("/ws/audio")
async def ws_audio(ws):
    ...
    session = CallSession(ws)
    call_audit.bind(session.audit)             # every task of this call inherits it
    poll_task.add_done_callback(lambda t: _note_task_crash(session, "turn_poll_loop", t))
    ending = call_audit.END_CLIENT_DISCONNECT
    try:
        ...
    except asyncio.CancelledError:
        ending = call_audit.END_AGENT_SHUTDOWN; raise
    except Exception as e:
        session.audit.error("session", e); ending = call_audit.END_EXCEPTION
    finally:
        ...
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await poll_task                    # a crashed poll loop no longer skips the rest
        ...
        session.audit.end(session.end_reason or ending,                   # CALL_ENDED
                          pending_flow=(session.pending or {}).get("awaiting"), ...)
```

```python
# NEW: agent/tools_client.py -- one decorator at the only boundary every backend call crosses

@_audited("get_test_rate")
async def get_test_rate(self, test_name): ...                # body unchanged

@_audited("verify_caller", redact=("answer",), summarize=_without_token)
async def verify_caller(self, phone, factor, answer, call_id=None): ...

# _audited -> CallAudit.api_call():
#   API_REQUEST  {action, request}                  <- the actual arguments
#   API_RESPONSE {outcome, success, reason, result} <- the actual JSON, or
#                {outcome:"error", error_type, error} <- the actual exception
```

**What changed, exactly:**

1. **A new module, `agent/call_audit.py`,** with `AuditStore` (durable SQLite, one writer thread, bounded queue) and `CallAudit` (the per-call recorder).
2. **Backend truth is captured once, at `ClinicToolsClient`.** Every method is decorated, so a failure `main.py` deliberately swallows is still recorded.
3. **Every other capture point is in `main.py`, where it happens:** transcript, intent, slots (including the mid-flow parsers), agent response, barge-in, errors, and call end.
4. **Every exit path finalises the record:** hang-up, idle timeout, exception, shutdown and capacity refusal. Records left open by a killed process are finalised at the next startup.
5. **Staff read endpoints and health reporting** were added.

---

## 5. Files Modified

| File | Function/Class | Change | Reason |
|---|---|---|---|
| `main.py` | imports, `AUDIT_TRANSPORT` | added `call_audit`, `hmac`, `Header`, `Query`, `JSONResponse`; `AUDIT_TRANSPORT = "webm"` | labels records per process; scopes crash recovery |
| `main.py` | `_audit_store`, `_NULL_AUDIT`, `_audit()` | new globals/helper | one store per process; a no-op recorder for test doubles without `.audit` |
| `main.py` | `_startup` | opens the store **first**, runs `recover_unfinished(AUDIT_TRANSPORT)`, warns if `VOICE_AGENT_AUDIT_TOKEN` is unset | a call accepted right after startup must have somewhere to go |
| `main.py` | `_shutdown` | `_audit_store.close()` (flush) | commit what is queued |
| `main.py` | `health` | `"audit"` block | audit write failures visible where staff already look |
| `main.py` | `audit_calls`, `audit_call`, `_audit_read_denied` | **new** `GET /api/audit/calls`, `GET /api/audit/calls/{call_id}` | staff can read records; conditional token, same pattern as clinic-api's delivery-receipt auth |
| `main.py` | `CallSession.__init__` | `call_id` is now full `uuid4().hex`; `self.audit`, `self.end_reason` | permanent unique key; `CALL_STARTED` at accept |
| `main.py` | `_speak` | records `AGENT_RESPONSE` in `finally`; new `audit_redact` kwarg | records what was said, how it was heard, and whether it was delivered |
| `main.py` | `_record_intent` (new), `_resolve_intent` | `INTENT_DETECTED` + `SLOTS_EXTRACTED` with source (`fast_path` / `intent_cache_*` / `llm`) | what was understood, and by which component |
| `main.py` | `_continue_pending` | `SLOTS_EXTRACTED` (source `slot_parse`) at the three parse points; `abandon_flow` intent | mid-flow slots never pass through the LLM |
| `main.py` | `_speak_history` | `audit_redact="patient_history"` | the history is not copied into a second store |
| `main.py` | `_dispatch_turn` → wrapper; body renamed `_run_turn` | records `ERROR` for any escaping exception, re-raises unchanged | a turn crash used to leave no trace tied to a call |
| `main.py` | `_run_turn` | `begin_turn`, `TRANSCRIPT` (ok / empty / rejected_low_quality / keypad), redaction during verification, language-switch intents, `ERROR` on conditioning failure and `ExtractionError` | what the caller said, as received |
| `main.py` | `_check_barge_in` | `AGENT_INTERRUPTED` | the previous reply was not heard in full |
| `main.py` | `_turn_poll_loop` | sets `session.end_reason = END_IDLE_TIMEOUT` | an idle timeout otherwise looks like a hang-up |
| `main.py` | `_note_task_crash` (new) | done-callback for the poll task | records a crash when it happens, with the call id |
| `main.py` | `_reject_at_capacity` | a `CallAudit` of its own, `final_status="rejected"` | a refused caller is still a caller |
| `main.py` | `ws_audio` | `call_audit.bind`, termination tracking, `suppress(Exception)` on the poll-task await, `session.audit.end(...)` in `finally` | every exit path finalises the record |
| `main_pcm.py` | (generated) | regenerated by `tools/make_pcm_variant.py` | identical instrumentation; reasoning half verified byte-identical |
| `tools/make_pcm_variant.py` | `main()` | one more `rep`: `AUDIT_TRANSPORT = "pcm"` | the PCM process labels its own records |
| `agent/tools_client.py` | `_audited`, `_without_token`, `_history_summary` (new) | decorator plus two response reducers | single capture point for backend truth |
| `agent/tools_client.py` | all 10 backend methods | `@_audited(...)`; the verify answer and history token are redacted | every backend action recorded |
| `agent/tools_client.py` | `record_disclosure_refusal` → `_post_disclosure_refusal` | failure swallowed *outside* the audited call | previously a failed refusal POST was indistinguishable from success |
| `README.md` | new section | short pointer to this document | |

> **Not part of this story:** the working tree also holds uncommitted changes from the previous story ("every flow completes without a smartphone": `agent/i18n.py`, `tests/test_no_smartphone.py`, `no_smartphone_multilingual_implementation.md`). The untracked `fix.patch`, `fix2.b64` and `fix2.tgz` predate this work and were not touched.

---

## 6. New Files

| File | Purpose |
|---|---|
| `agent/call_audit.py` | Event model, `AuditStore` (SQLite, writer thread, crash recovery, reads, health), `CallAudit` (per-call recorder), and the `ContextVar` that ties the shared tools client to the current call. Header: *Author: Chakravardhan*. |
| `tests/test_call_audit.py` | 23 tests of the behaviour listed in §10. Header: *Author: Chakravardhan*. |
| `IMPLEMENTATION_CALL_AUDIT.md` | This document. *Author: Chakravardhan*. |

**No new dependency.** Storage uses the standard library's `sqlite3`.

---

## 7. Data Model

**Storage:** `/workspace/call_audit.db` (override with `VOICE_AGENT_AUDIT_DB`), on the same persistent volume as `clinic.db`, with the same journal-mode rule (`CLINIC_DB_JOURNAL_MODE` / `VOICE_AGENT_PROVIDER`).

**Why not clinic-api's database?**
1. The audit must capture clinic-api **being down**. Posting events to it would lose exactly those calls.
2. In DELETE journal mode every write locks the whole of `clinic.db`, so an audit write per event would contend with bookings.

### `call_records` — one row per call

| Column | Meaning |
|---|---|
| `call_id` (PK) | `uuid4().hex`, the same id as the log prefix and `disclosure_audit.call_id` |
| `transport` | `webm` / `pcm`: which process took the call |
| `language` | the language at the end of the call |
| `caller_phone` | **WHO**: the first phone number the caller stated and the backend acted on (no caller-ID on this transport; see §12) |
| `started_at`, `ended_at` | UTC ISO-8601, milliseconds |
| `duration_s` | monotonic-clock duration |
| `final_status` | `completed` · `failed` · `abandoned` · `error` · `rejected` |
| `termination_reason` | `client_disconnect` · `idle_timeout` · `exception` · `agent_shutdown` · `at_capacity` · `agent_restarted` |
| `turns`, `served_turns`, `api_calls`, `api_failures`, `errors` | counters at finalisation |

**`final_status`** is derived from recorded events, in this order:

| Status | Rule |
|---|---|
| `error` | the call ended by exception, shutdown or restart, **or** the last turn crashed and nothing answered it |
| `rejected` | refused at capacity |
| `failed` | the last answer was a backend or LLM failure apology, **or** it was not heard (TTS down, fallback clip played) |
| `abandoned` | nothing the caller asked was served, **or** a multi-step flow (booking, verification) was still open |
| `completed` | otherwise |

There is no `transferred` status, because nothing in this system hands a call to a person.

### `call_events` — append-only, numbered per call

| Column | Meaning |
|---|---|
| `event_id` (PK) | `uuid4().hex` |
| `call_id` | owning call |
| `seq` | 1, 2, 3 … within the call; `UNIQUE(call_id, seq)` |
| `ts` | UTC ISO-8601, milliseconds |
| `event_type` | see below |
| `turn` | caller turn number (0 = before the caller spoke) |
| `success` | the event's own verdict (`NULL` where not applicable) |
| `data` | JSON, serialised **when the event happened** |

| Event | Written from | Key fields |
|---|---|---|
| `CALL_STARTED` | `CallSession.__init__` | transport, language |
| `TRANSCRIPT` | `_run_turn` | `text` (verbatim ASR / keypad), `source`, `status` (`ok`/`empty`/`rejected_low_quality`), `decoder_used`, `audio` quality; or `redacted` + `chars` |
| `INTENT_DETECTED` | `_resolve_intent`, `_run_turn`, `_continue_pending` | `intent`, `source`, LLM `attempts`/`latency_s`/`retry_errors`, fast-path `confidence` |
| `SLOTS_EXTRACTED` | same | `slots`, `source` (`llm`/`fast_path`/`intent_cache_*`/`slot_parse`), `awaiting` |
| `API_REQUEST` | `ClinicToolsClient` via `_audited` | `action`, `request` (actual arguments; secrets `[redacted]`) |
| `API_RESPONSE` | same | `outcome`, `success`, `reason`, `result` (actual JSON) **or** `error_type`/`error`; `latency_ms`; `request_event_id` |
| `AGENT_RESPONSE` | `_speak`, `_reject_at_capacity` | `text`, `audio` (`synthesized`/`fallback_clip`/`none`), `delivered`, `fallback_reason`, `tts_error` |
| `AGENT_INTERRUPTED` | `_check_barge_in` | barge-in verdict: the reply above was cut off |
| `ERROR` | `_dispatch_turn`, `ws_audio`, `_note_task_crash`, `_run_turn` | `stage`, `handled`, `error_type`, `error`, `where` (file:line in function, read from the traceback) |
| `CALL_ENDED` | `CallAudit.end`, `recover_unfinished` | `final_status`, `termination_reason`, `duration_s`, counters, `pending_flow` |

### A real record

The record below was produced by running the real `_dispatch_turn` with the test harness from §10 (ASR, LLM and TTS stubbed; clinic-api answering over `httpx.MockTransport`), then read back from SQLite:

```json
{
  "call": {"call_id": "2b31e1862c4f4b7b9f556f6057ce1316", "transport": "pcm", "language": "bn",
           "caller_phone": null, "started_at": "2026-09-11T08:21:36.701+00:00",
           "ended_at": "2026-09-11T08:21:36.716+00:00", "duration_s": 0.015,
           "final_status": "completed", "termination_reason": "client_disconnect",
           "turns": 1, "served_turns": 1, "api_calls": 1, "api_failures": 0, "errors": 0},
  "events": [
    {"seq": 1, "event_type": "CALL_STARTED",    "turn": 0, "data": {"transport": "pcm", "language": "bn"}},
    {"seq": 2, "event_type": "TRANSCRIPT",      "turn": 1, "success": true,
     "data": {"text": "সিবিসি টেস্টের দাম কত", "source": "speech", "status": "ok",
              "decoder_used": "rnnt", "decoder_agreement": 0.93,
              "audio": {"snr_db": 24.0, "speech_ratio": 0.55, "usable": true, "bucket": "clean"}}},
    {"seq": 3, "event_type": "INTENT_DETECTED", "turn": 1, "success": true,
     "data": {"intent": "test_rate", "source": "llm", "attempts": 1, "latency_s": 0.012, "retry_errors": []}},
    {"seq": 4, "event_type": "SLOTS_EXTRACTED", "turn": 1,
     "data": {"source": "llm", "slots": {"test_name": "CBC", "doctor_name": null, "date": null}}},
    {"seq": 5, "event_type": "API_REQUEST",     "turn": 1, "event_id": "704f37c0…",
     "data": {"action": "get_test_rate", "request": {"test_name": "CBC"}}},
    {"seq": 6, "event_type": "API_RESPONSE",    "turn": 1, "success": true,
     "data": {"action": "get_test_rate", "request_event_id": "704f37c0…", "outcome": "found", "latency_ms": 1.0,
              "result": {"found": true, "test_name": "CBC", "rate_inr": 650, "sample_type": "Blood", "report_time_hours": 24}}},
    {"seq": 7, "event_type": "AGENT_RESPONSE",  "turn": 1, "success": true,
     "data": {"text": "CBC টেস্টের রেট 650 টাকা। স্যাম্পল: Blood। রিপোর্ট 24 ঘণ্টার মধ্যে পাবেন।",
              "audio": "synthesized", "delivered": true, "fallback_reason": null}},
    {"seq": 8, "event_type": "CALL_ENDED",      "turn": 1, "success": true,
     "data": {"final_status": "completed", "termination_reason": "client_disconnect", "duration_s": 0.015}}
  ],
  "integrity": {"event_count": 8, "missing_seq": [], "has_call_started": true,
                "has_call_ended": true, "finalized": true, "complete": true}
}
```

Some fields in the record above are trimmed for space. Here, from the same run, is clinic-api refusing a booking the LLM had fully understood:

```json
{"event_type": "API_RESPONSE", "success": false,
 "data": {"action": "book_appointment", "outcome": "failure", "reason": "slot_taken",
          "result": {"success": false, "reason": "slot_taken", "alternative_slots": ["18:30", "18:45"]}}}
```

---

## 8. Call Flow

```text
WebSocket accepted ─────────────────────────── ws_audio
   │  (at capacity? → CALL_STARTED, AGENT_RESPONSE busy line, CALL_ENDED rejected)
   ▼
CallSession.__init__ ── call_id = uuid4().hex ─► CALL_STARTED
   │  call_audit.bind(session.audit)  ← every task of this call inherits it
   ▼
greeting ─► _speak ───────────────────────────► AGENT_RESPONSE (turn 0)
   ▼
caller speaks / presses a key ─► begin_turn()
   ▼
quality gate ── rejected? ─────────────────────► TRANSCRIPT status=rejected_low_quality
   ▼
ASR ───────────────────────────────────────────► TRANSCRIPT text=… (redacted if a PIN/DOB)
   ▼
intent: fast path | cache | LLM ───────────────► INTENT_DETECTED + SLOTS_EXTRACTED (source)
   │  (mid-flow: slot_parse ───────────────────► SLOTS_EXTRACTED source=slot_parse)
   ▼
ClinicToolsClient.<action>() ── @_audited ─────► API_REQUEST
   ▼
clinic-api JSON  |  ToolCallError ─────────────► API_RESPONSE outcome=found/success/failure/error
   ▼
reply_templates → _speak → TTS ────────────────► AGENT_RESPONSE audio=synthesized|fallback_clip
   │  (caller talks over it ───────────────────► AGENT_INTERRUPTED)
   │  (anything raises ────────────────────────► ERROR stage/where)
   ▼
hang-up | idle timeout | exception | shutdown ─► CALL_ENDED final_status, termination_reason
                                                  + call_records row finalised (write-once)
process killed mid-call? next startup ─────────► CALL_ENDED agent_restarted (recover_unfinished)
```

---

## 9. Accuracy Guarantees

**Event-based recording, not reconstruction.** Each event is written by the line of code that produced the value, from that value. The transcript is `asr_result.text`, the intent is the dict that drives the branch, and the response is the `r.json()` that `ClinicToolsClient` returns. The payload is serialised at that moment, so a dict the application mutates later (`session.pending`, booking slots) is captured as it was.

**No LLM summary anywhere.** The LLM's only contribution to the record is its own output (`INTENT_DETECTED`), labelled `source: "llm"` next to its attempt count. The one LLM-authored sentence the agent ever speaks (smalltalk) appears in `AGENT_RESPONSE` because it *was* spoken.

**The backend's verdict, never the intent.** `API_RESPONSE.success` and `outcome` come only from `classify_result(response)` (`success`, `found` or `reply` fields) or from the exception. A fully understood booking that clinic-api refuses is `outcome: "failure", reason: "slot_taken"` (see §7). Capture sits at the client, so failures `main.py` swallows on purpose (payment, report collection, the refusal record) are recorded too. `record_disclosure_refusal` was restructured because its old in-method `except: pass` would have made a failed POST look like a success.

**What the caller actually heard.** `AGENT_RESPONSE` records whether TTS synthesised the words or a pre-recorded apology played instead, and whether the frames left the socket at all. `AGENT_INTERRUPTED` marks a reply cut off by barge-in.

**Unique call ID.** The full 128-bit `uuid4` is also the `call_records` primary key. Every event carries it, and `seq` is unique per call. The shared `ClinicToolsClient` gets the call identity through a `ContextVar` bound in `ws_audio`. asyncio copies the context into each task, so concurrent calls cannot see each other's recorder (tested with three interleaved calls).

**Timestamps.** Every event gets a UTC timestamp in fixed-width ISO format with milliseconds, which sorts chronologically. Duration comes from the monotonic clock, so it is immune to NTP steps.

**Ordering.** There is one queue and one writer thread, so rows commit in the order they were recorded. `CALL_STARTED` always precedes its events, and `CALL_ENDED` precedes the finalising `UPDATE`.

**Failure recording.** A backend error, backend refusal, LLM failure, TTS failure, conditioning failure, turn exception, session exception and poll-loop crash each produce an event carrying the real exception type, message and source frame.

**Call finalisation.** `CallAudit.end()` runs in `ws_audio`'s `finally`, so a hang-up, idle timeout, exception or shutdown all reach it. A crashed poll loop no longer skips it. Finalisation is idempotent in memory, and write-once in SQL via `WHERE ended_at IS NULL`. A process killed mid-call leaves an open record. The next startup of the **same transport** closes it as `error / agent_restarted`, with `ended_at` set to the call's last recorded event rather than to the restart time.

**A partial record cannot pass as whole.** `GET /api/audit/calls/{id}` returns an `integrity` block computed from the data. It checks that `seq` runs 1…N with no gap, that the first event is `CALL_STARTED`, that a `CALL_ENDED` exists, and that the row is finalised.

**Auditing never costs the caller anything.** Every recorder method swallows its own exceptions. Writes are queued and committed off the event loop. Failures are logged at ERROR as `AUDIT RECORD LOST` (rate-limited, with running totals) and counted under `/api/health → audit` (`write_failures`, `dropped`, `last_error`).

**Privacy follows the existing model.** The following are withheld from the record:
* the PIN or date of birth being checked (both in `TRANSCRIPT` and in `API_REQUEST.answer`)
* the history token
* the history itself (only counts are kept; clinic-api's `disclosure_audit` already records the disclosure against this `call_id`)

Phone numbers and patient names are stored, as `appointments` and `disclosure_audit` already store them. A test checks the raw database **file bytes** for the secrets.

---

## 10. Tests

`tests/test_call_audit.py` drives the **real** `_dispatch_turn`, `ws_audio` (with a scripted socket), `ClinicToolsClient` and a real SQLite file. Only the ASR, LLM and TTS edges are faked, and clinic-api is an `httpx.MockTransport`. Every assertion is made against data **read back from the database**.

| # | Test | Verifies | Result |
|---|---|---|---|
| 1 | `test_successful_call_records_every_stage_in_order` | STARTED → TRANSCRIPT → INTENT → SLOTS → API_REQUEST → API_RESPONSE → AGENT_RESPONSE → ENDED; verbatim values; `result == backend JSON`; seq 1..8; `completed` | PASSED |
| 2 | `test_backend_refusal_is_recorded_as_the_failure_it_was` | clinic-api's `slot_taken` recorded as failure despite a complete LLM booking; caller phone captured | PASSED |
| 3 | `test_unreachable_backend_is_recorded_with_the_real_error` | `ConnectError` → `outcome: error`, the real message; `tool_failure` reply; `failed` | PASSED |
| 4 | `test_a_backend_failure_main_py_swallows_is_still_recorded` | payment's swallowed timeout is still in the record; call `completed` | PASSED |
| 5 | `test_tts_failure_records_what_the_caller_actually_heard` | `audio: fallback_clip`, `tts_error`; `failed` | PASSED |
| 6 | `test_unexpected_exception_in_a_turn_is_recorded_and_call_finalised` | ASR raises → `ERROR` with `where`, re-raised unchanged; call finalised `error` | PASSED |
| 7 | `test_session_crash_is_finalised_through_ws_audio` | socket read raises → `ERROR stage=session`, `CALL_ENDED exception`, capacity slot released | PASSED |
| 8 | `test_concurrent_calls_never_share_events` | 3 interleaved calls through the shared client: each record contains only its own data | PASSED |
| 9 | `test_hang_up_before_saying_anything_is_abandoned` | immediate disconnect → `abandoned`, record complete | PASSED |
| 10 | `test_hang_up_mid_booking_is_abandoned_and_names_the_open_step` | keypad booking, hang-up → `abandoned`, `pending_flow: date` | PASSED |
| 11 | `test_idle_timeout_is_recorded_as_such` | `termination_reason: idle_timeout`, goodbye recorded | PASSED |
| 12 | `test_a_caller_refused_at_capacity_still_leaves_a_record` | `rejected` / `at_capacity`, busy line recorded | PASSED |
| 13 | `test_audit_database_failure_does_not_break_the_call` | writes raise `disk I/O error` → caller still answered; failure counted and logged | PASSED |
| 14 | `test_a_bug_in_the_recorder_itself_never_reaches_the_caller` | recorder methods raise → caller still answered; failures counted | PASSED |
| 15 | `test_a_full_queue_drops_are_counted_not_raised` | queue full → `dropped` counted, no exception | PASSED |
| 16 | `test_an_unopenable_database_is_reported_and_the_call_still_runs` | bad path → `available: false`, `last_error`, calls unaffected | PASSED |
| 17 | `test_verification_answer_token_and_history_never_reach_the_record` | DOB, token and history absent from the **raw DB bytes**; real values did reach clinic-api and the caller; redaction markers present | PASSED |
| 18 | `test_finalisation_is_write_once` | second `end()` and a direct second `UPDATE` cannot rewrite the outcome | PASSED |
| 19 | `test_calls_left_open_by_a_crash_are_finalised_on_restart` | `agent_restarted`, `ended_at` = last event; the other transport's open call untouched; idempotent | PASSED |
| 20 | `test_a_missing_event_is_visible_in_the_integrity_block` | a deleted event → `missing_seq: [2]`, `complete: false` | PASSED |
| 21 | `test_barge_in_marks_the_reply_as_cut_off` | `AGENT_RESPONSE` followed by `AGENT_INTERRUPTED` | PASSED |
| 22 | `test_staff_read_endpoints` | list, detail, 404, 401 with the wrong token, 200 with the right token; health `audit` block | PASSED |
| 23 | `test_both_transports_carry_identical_instrumentation` | `main.py` / `main_pcm.py` labelled `webm` / `pcm`; reasoning half identical; capture points present in both | PASSED |

```bash
python -m pytest tests/test_call_audit.py -v
```

---

## 11. Existing Tests

Run locally on Windows (Python 3.14.7, pytest 9.1.1, httpx 0.28.1) on 2026-09-11:

```text
Existing tests (tests/ excluding test_call_audit.py): 353 passed
New tests (tests/test_call_audit.py):                   23 passed
Full suite in one process:                             376 passed
Failed: 0
```

`python tools/make_pcm_variant.py` reported: *main_pcm.py regenerated; reasoning half verified byte-identical*.

---

## 12. Risks / Limitations

1. **Not run on the GPU pod.** No real IndicConformer, Ollama, Indic-TTS, uvicorn WebSocket or browser was involved. The tests replace those edges and drive `ws_audio` with a scripted socket. The capture points sit on code paths the existing suite already exercises, but the first live calls should be checked via `GET /api/audit/calls`.
2. **"WHO" is the number the caller stated, not a verified identity.** This browser transport has no caller-ID. `caller_phone` is the first number the backend acted on, and stays `null` for a call that never gave one. When telephony arrives, its ANI belongs in this field.
3. **A transcript is ASR's output, not ground truth.** The record says what the system heard. Audio is not retained, so a mis-transcription can't be re-checked afterwards. That would need call recording, which has its own consent and retention questions.
4. **Writes are asynchronous.** An event becomes durable milliseconds after it is recorded. On `kill -9`, the last queued events of in-flight calls can be lost. Those records are then finalised as `agent_restarted` and their `integrity` shows the gap. Nothing is lost on a clean shutdown (`close()` flushes).
5. **Crash recovery assumes one process per transport per file**, which is what `deploy/start_all.sh` runs. Two processes of the *same* transport sharing one `call_audit.db` could finalise each other's live calls at startup, and are unsupported.
6. **Events after `CALL_ENDED`.** A turn still in flight when the caller hangs up (e.g. a booking that completes afterwards) is recorded with later `seq` numbers, because it really happened. The `call_records` counters and `final_status`, computed at finalisation, do not include it.
7. **`final_status` is a derived classification.** It follows the rules in §7, and each rule is backed by recorded events, but it is not a judgement of caller satisfaction. For example, a caller told "slot taken, here are others" counts as `completed`, with `api_failures: 1`.
8. **No retention policy.** Transcripts contain patient names, phone numbers and whatever callers say. As with `appointments` and `notification_attempts`, retention and purging are not implemented and remain outstanding.
9. **Read access.** `/api/audit/*` requires `X-Audit-Token` only when `VOICE_AGENT_AUDIT_TOKEN` is set. Unset (the bench default), the records are as readable as `/api/stats`, and startup logs a warning. It must be set on any real deployment.
10. **Barge-in granularity.** `AGENT_INTERRUPTED` records *that* a reply was cut off and when (`at_call_s`), not how many words were heard.
11. **Small behaviour changes, all deliberate:**
    * `call_id` is now 32 hex characters instead of 8. Log lines get longer, and clinic-api's `disclosure_audit.call_id` receives the full id.
    * `ws_audio` now suppresses a crashed poll task's exception when awaiting it. The exception used to propagate out of `finally`, skipping cleanup and leaking a capacity slot. It is now logged and recorded by `_note_task_crash`.
    * `_dispatch_turn`'s body moved into `_run_turn`. The wrapper re-raises unchanged, so failure behaviour is identical.
