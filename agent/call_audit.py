"""Every call leaves a complete record.

Author: Chakravardhan
Story:  Every call leaves a complete record
        "As a hospital, I want each call to leave an accurate entry, so that
         the agent is auditable in the same way a member of staff is."

WHAT THIS RECORDS, AND WHERE EACH PIECE COMES FROM
--------------------------------------------------
One row per call (`call_records`) and an append-only, numbered list of
events per call (`call_events`). Every event is written AT THE POINT IN THE
CODE WHERE THE THING HAPPENED, from the value the code is actually holding:

  CALL_STARTED       CallSession.__init__ -- the socket was accepted
  TRANSCRIPT         the string ASR returned (or the keypad text), verbatim,
                     or the fact that the clip was rejected / came back empty
  INTENT_DETECTED    the dict the fast path, the intent cache or the LLM
                     returned, and WHICH of the three produced it
  SLOTS_EXTRACTED    the slots that dict carried, or the value a local
                     parser (agent/slot_parse.py) pulled out mid-flow
  API_REQUEST        the arguments ClinicToolsClient was called with
  API_RESPONSE       the JSON clinic-api sent back -- or the exception that
                     came back instead
  AGENT_RESPONSE     the sentence handed to TTS, and whether the caller heard
                     it synthesized, as a pre-recorded fallback clip, or not
                     at all
  AGENT_INTERRUPTED  a barge-in stopped that sentence part-way through
  ERROR              an exception, with the stage and the frame it came from
  CALL_ENDED         how the call ended, and the outcome derived from the
                     events above

WHAT THIS DELIBERATELY IS NOT
-----------------------------
It is not a summary. Nothing here is written after the fact from memory of
the call, and nothing is written by the LLM. The one LLM-authored string the
agent ever speaks (a smalltalk reply) appears in AGENT_RESPONSE because it
WAS spoken, not because the model said so.

It is also not the logs. A log line is prose for an engineer, rotated away
in days, and has no notion of "this call". An event here is structured,
numbered within its call, and kept.

"SUCCESS" IS NEVER INFERRED FROM INTENT
---------------------------------------
API_RESPONSE carries the backend's own verdict. A booking the LLM correctly
classified, whose slots were all filled, which clinic-api then refused with
{"success": false, "reason": "slot_taken"}, is recorded as a failure with
that reason. A lookup that timed out is recorded as an error with the
exception text. Neither can be recorded as a success, because the only
input to that field is what came back over the wire.

AUDITING MUST NEVER COST THE CALLER ANYTHING
--------------------------------------------
Every public method here swallows its own exceptions. Writes go onto a
bounded queue and are committed by one background thread, so a slow or
locked database never blocks the event loop that every live call shares. A
failed write is not hidden: it is logged at ERROR, counted, and surfaced in
the voice agent's /api/health under `audit`, the same place staff already
look for the notification failure count.

WHY A SEPARATE SQLITE FILE, NOT clinic-api's DATABASE
-----------------------------------------------------
Two reasons, both about this particular requirement.

 1. The audit has to capture clinic-api FAILING. If events were POSTed to
    clinic-api, the calls where clinic-api was unreachable -- the ones a
    hospital most needs to reconstruct -- would be exactly the ones with no
    record.
 2. Lock contention. clinic-api/db.py runs SQLite in DELETE journal mode on
    RunPod, where a writer holds an exclusive lock on the whole file. An
    audit write per event into clinic.db would queue behind -- and in front
    of -- every booking.

So: the same engine (SQLite, stdlib `sqlite3`, no new dependency), the same
volume (/workspace, which survives a restart), the same journal-mode rule as
clinic-api/db.py, and its own file. VOICE_AGENT_AUDIT_DB overrides the path.

BOTH VOICE PROCESSES SHARE THE FILE
-----------------------------------
deploy/start_all.sh runs main.py (:8100) and main_pcm.py (:8101) side by
side. SQLite handles two writers with its own locking; the busy timeout
below covers the handoff, on a thread that no caller is waiting on. Staff
query one file, and `call_records.transport` says which process a call came
through -- which also scopes crash recovery (see recover_unfinished).
"""
from __future__ import annotations

import asyncio
import contextvars
import datetime
import json
import logging
import os
import queue
import sqlite3
import threading
import time
import traceback
import uuid

logger = logging.getLogger("call_audit")

# ---- event types. Persisted, so they are a data format. -------------------
CALL_STARTED = "CALL_STARTED"
TRANSCRIPT = "TRANSCRIPT"
INTENT_DETECTED = "INTENT_DETECTED"
SLOTS_EXTRACTED = "SLOTS_EXTRACTED"
API_REQUEST = "API_REQUEST"
API_RESPONSE = "API_RESPONSE"
AGENT_RESPONSE = "AGENT_RESPONSE"
AGENT_INTERRUPTED = "AGENT_INTERRUPTED"
ERROR = "ERROR"
CALL_ENDED = "CALL_ENDED"

# ---- call_records.final_status ---------------------------------------------
# Only outcomes this application can actually produce. There is no
# "transferred": nothing in this system hands a call to a human.
STATUS_COMPLETED = "completed"   # the caller's requests were answered
STATUS_FAILED = "failed"         # the last answer was a failure apology, or went unheard
STATUS_ABANDONED = "abandoned"   # nothing was answered, or a flow was left half-done
STATUS_ERROR = "error"           # an exception or a shutdown cut the call
STATUS_REJECTED = "rejected"     # admission control turned the caller away

# ---- call_records.termination_reason ---------------------------------------
END_CLIENT_DISCONNECT = "client_disconnect"
END_IDLE_TIMEOUT = "idle_timeout"
END_EXCEPTION = "exception"
END_AGENT_SHUTDOWN = "agent_shutdown"
END_AT_CAPACITY = "at_capacity"
END_AGENT_RESTARTED = "agent_restarted"   # written by recover_unfinished()

_ABNORMAL_ENDS = frozenset({END_EXCEPTION, END_AGENT_SHUTDOWN, END_AGENT_RESTARTED})

# AGENT_RESPONSE fallback reasons that mean "a backend failed and the caller's
# request was not served". The clarification reasons (low_quality, asr_empty,
# keypad_offer) are NOT here: those are the agent not understanding, which
# the TRANSCRIPT events already record, not a service failure.
FAILURE_FALLBACKS = frozenset({"tool_failure", "llm_failure"})

# THE MESSAGE CHANNEL -- Author: Chakravardhan. A reply that went out as
# written text rather than audio (agent/message_service.py), and the end of a
# record that covers one inbound message rather than one call.
AUDIO_TEXT = "text"
END_MESSAGE_TURN = "message_turn"

REDACTED = "[redacted]"

# Off the event loop, so it can afford to wait out another process's write.
BUSY_TIMEOUT_S = 10.0
QUEUE_MAX = 10_000
_BATCH_MAX = 256
_ERROR_LOG_INTERVAL_S = 10.0
_MAX_ERROR_TEXT = 500

_JOURNAL_MODES = {"DELETE", "WAL", "TRUNCATE", "PERSIST", "MEMORY"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS call_records (
    call_id            TEXT PRIMARY KEY,
    transport          TEXT NOT NULL,
    language           TEXT,
    caller_phone       TEXT,
    started_at         TEXT NOT NULL,
    ended_at           TEXT,
    duration_s         REAL,
    final_status       TEXT,
    termination_reason TEXT,
    turns              INTEGER NOT NULL DEFAULT 0,
    served_turns       INTEGER NOT NULL DEFAULT 0,
    api_calls          INTEGER NOT NULL DEFAULT 0,
    api_failures       INTEGER NOT NULL DEFAULT 0,
    errors             INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_call_records_started ON call_records (started_at);
CREATE INDEX IF NOT EXISTS ix_call_records_status ON call_records (final_status);

CREATE TABLE IF NOT EXISTS call_events (
    event_id   TEXT PRIMARY KEY,
    call_id    TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    ts         TEXT NOT NULL,
    event_type TEXT NOT NULL,
    turn       INTEGER NOT NULL DEFAULT 0,
    success    INTEGER,
    data       TEXT NOT NULL,
    UNIQUE (call_id, seq)
);
CREATE INDEX IF NOT EXISTS ix_call_events_type ON call_events (event_type);
"""

_INSERT_CALL = ("INSERT INTO call_records (call_id, transport, language, started_at) "
                "VALUES (?, ?, ?, ?)")
_INSERT_EVENT = ("INSERT INTO call_events (event_id, call_id, seq, ts, event_type, turn, "
                 "success, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?)")
# `ended_at IS NULL` makes finalisation write-once: a call cannot be
# re-finalised with a different outcome after the fact.
_FINALIZE_CALL = ("UPDATE call_records SET ended_at = ?, duration_s = ?, final_status = ?, "
                  "termination_reason = ?, caller_phone = COALESCE(?, caller_phone), "
                  "language = ?, turns = ?, served_turns = ?, api_calls = ?, "
                  "api_failures = ?, errors = ? WHERE call_id = ? AND ended_at IS NULL")


def default_path() -> str:
    """Beside clinic.db on the persistent volume, unless overridden."""
    return os.environ.get("VOICE_AGENT_AUDIT_DB", "/workspace/call_audit.db")


def _journal_mode() -> str:
    """The same rule as clinic-api/db.py, for the same reason: WAL on a local
    disk (vast.ai), DELETE on RunPod's network mount, where WAL's shared-memory
    index is the thing least likely to work. CLINIC_DB_JOURNAL_MODE overrides
    both files at once, because it is a fact about the filesystem, not about
    either database."""
    default = "WAL" if os.environ.get("VOICE_AGENT_PROVIDER") == "vast" else "DELETE"
    mode = os.environ.get("CLINIC_DB_JOURNAL_MODE", default).upper()
    return mode if mode in _JOURNAL_MODES else "DELETE"


def _utcnow() -> str:
    """UTC, millisecond precision, fixed width -- so ISO strings sort
    chronologically as text, which recover_unfinished() relies on."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")


def _where(exc: BaseException) -> str | None:
    """The innermost frame the exception came from: file:line in function.

    Recorded instead of a stage label somebody has to remember to keep up to
    date -- it is read off the traceback, so it is the place the failure
    actually happened."""
    tb = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ else None
    if not tb:
        return None
    frame = tb[-1]
    return f"{os.path.basename(frame.filename)}:{frame.lineno} in {frame.name}"


def _error_text(exc: BaseException) -> str:
    return str(exc)[:_MAX_ERROR_TEXT]


def classify_result(result) -> tuple[str, bool, str | None]:
    """-> (outcome, success, reason) for a clinic-api response, from the
    response's OWN fields. tools_client.py documents three shapes:

      * actions  -- {"success": bool, "reason": ...}
      * lookups  -- {"found": bool, ...}; not-found is a valid answer the
                    caller is told, so it is not a failure
      * verification -- {"reply": "verified" | "failed" | "locked"}
    """
    if not isinstance(result, dict):
        return "ok", True, None
    if "success" in result:
        ok = bool(result.get("success"))
        return ("success" if ok else "failure"), ok, result.get("reason")
    if "reply" in result:
        reply = str(result.get("reply"))
        return reply, reply == "verified", None
    if "found" in result:
        found = bool(result.get("found"))
        return ("found" if found else "not_found"), True, result.get("reason")
    return "ok", True, None


def describe_quality(quality) -> dict | None:
    """The audio-quality measurement a turn was judged on, as plain numbers.
    Duck-typed so a test double or an older AudioQuality still records."""
    if quality is None:
        return None
    out = {}
    for field in ("duration_s", "snr_db", "speech_ratio", "clipping_ratio", "rms_dbfs"):
        value = getattr(quality, field, None)
        if value is not None:
            out[field] = round(float(value), 3)
    out["usable"] = bool(getattr(quality, "usable", True))
    out["reasons"] = list(getattr(quality, "reasons", ()) or ())
    bucket = getattr(quality, "bucket", None)
    if callable(bucket):
        try:
            out["bucket"] = bucket()
        except Exception:  # noqa: BLE001 - descriptive only
            pass
    return out


# ===========================================================================
# THE STORE -- one per process
# ===========================================================================
_STOP = object()


class AuditStore:
    """Durable storage for call records. Owns one writer thread.

    Callers never touch SQLite: they enqueue, and the writer commits in
    batches, in order. Order matters and is guaranteed -- one queue, one
    consumer -- so a call's CALL_STARTED row always lands before its events,
    and its CALL_ENDED before the UPDATE that finalises the record.
    """

    def __init__(self, path: str | None = None):
        self.path = path or default_path()
        self._q: queue.Queue = queue.Queue(maxsize=QUEUE_MAX)
        self._lock = threading.Lock()
        self.write_failures = 0
        self.dropped = 0
        self.last_error: str | None = None
        self._last_error_log = float("-inf")   # the first failure always logs
        self._suppressed_errors = 0
        self._closed = False
        self.available = False
        try:
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA)
                conn.commit()
            finally:
                conn.close()
            self.available = True
        except Exception as e:  # noqa: BLE001 - the agent runs without audit, loudly
            self._fail(f"cannot open audit database {self.path}: {e}", rows=0)
        self._thread = threading.Thread(target=self._run, name="call-audit-writer", daemon=True)
        self._thread.start()

    # -- plumbing ------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_S, check_same_thread=False)
        conn.execute(f"PRAGMA busy_timeout={int(BUSY_TIMEOUT_S * 1000)}")
        mode = _journal_mode()
        conn.execute(f"PRAGMA journal_mode={mode}")
        if mode == "WAL":
            conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _fail(self, message: str, rows: int = 1, call_ids=()) -> None:
        """Count and report a failure. Never raises.

        Rate-limited, not silenced: the first failure is logged at once, and
        while the database stays broken one line per interval reports how
        many rows have been lost since the last one -- a dead volume must not
        turn every event of every call into a log line of its own."""
        with self._lock:
            self.write_failures += rows
            self.last_error = message
            now = time.monotonic()
            if now - self._last_error_log < _ERROR_LOG_INTERVAL_S:
                self._suppressed_errors += rows
                return
            suppressed, self._suppressed_errors = self._suppressed_errors, 0
            self._last_error_log = now
        calls = sorted({c for c in call_ids if c})
        logger.error("AUDIT RECORD LOST: %s%s%s", message,
                     f" (calls: {', '.join(calls[:10])})" if calls else "",
                     f" [+{suppressed} more row(s) lost since last report]" if suppressed else "")

    def _submit(self, sql: str, params: tuple, call_id: str) -> None:
        if self._closed:
            with self._lock:
                self.dropped += 1
            self._fail("audit store is closed; row dropped", rows=0, call_ids=(call_id,))
            return
        try:
            self._q.put_nowait((sql, params, call_id))
        except queue.Full:
            with self._lock:
                self.dropped += 1
            self._fail(f"audit queue full ({QUEUE_MAX}); row dropped", rows=0,
                       call_ids=(call_id,))

    def _write(self, conn: sqlite3.Connection, writes: list) -> None:
        """One transaction for the batch. If it fails, retry row by row so a
        single bad row cannot take its neighbours down with it -- and so the
        loss that IS reported is the loss that actually happened."""
        try:
            with conn:
                for sql, params, _ in writes:
                    conn.execute(sql, params)
            return
        except Exception:  # noqa: BLE001 - fall through to the row-by-row pass
            pass
        lost, calls, last = 0, set(), None
        for sql, params, call_id in writes:
            try:
                with conn:
                    conn.execute(sql, params)
            except Exception as e:  # noqa: BLE001
                lost += 1
                calls.add(call_id)
                last = e
        if lost:
            self._fail(f"{lost} of {len(writes)} row(s) failed to write: {last}",
                       rows=lost, call_ids=calls)

    def _run(self) -> None:
        conn = None
        while True:
            batch = [self._q.get()]
            while len(batch) < _BATCH_MAX:
                try:
                    batch.append(self._q.get_nowait())
                except queue.Empty:
                    break
            stop = False
            flushes, writes = [], []
            for item in batch:
                if item is _STOP:
                    stop = True
                elif isinstance(item, threading.Event):
                    flushes.append(item)
                else:
                    writes.append(item)
            if writes:
                try:
                    if conn is None:
                        conn = self._connect()
                    self._write(conn, writes)
                    self.available = True
                except Exception as e:  # noqa: BLE001 - cannot even connect
                    self.available = False
                    self._fail(f"audit database unavailable, {len(writes)} row(s) lost: {e}",
                               rows=len(writes), call_ids=[w[2] for w in writes])
                    if conn is not None:
                        try:
                            conn.close()
                        except Exception:  # noqa: BLE001
                            pass
                        conn = None
            for ev in flushes:
                ev.set()
            if stop:
                break
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    # -- writes --------------------------------------------------------------
    def start_call(self, call_id: str, transport: str, language: str, started_at: str) -> None:
        self._submit(_INSERT_CALL, (call_id, transport, language, started_at), call_id)

    def add_event(self, event_id: str, call_id: str, seq: int, ts: str, event_type: str,
                  turn: int, success: bool | None, data: str) -> None:
        self._submit(_INSERT_EVENT,
                     (event_id, call_id, seq, ts, event_type, turn,
                      None if success is None else int(bool(success)), data),
                     call_id)

    def end_call(self, call_id: str, *, ended_at: str, duration_s: float, final_status: str,
                 termination_reason: str, caller_phone: str | None, language: str,
                 turns: int, served_turns: int, api_calls: int, api_failures: int,
                 errors: int) -> None:
        self._submit(_FINALIZE_CALL,
                     (ended_at, duration_s, final_status, termination_reason, caller_phone,
                      language, turns, served_turns, api_calls, api_failures, errors, call_id),
                     call_id)

    def flush(self, timeout: float = 5.0) -> bool:
        """Block until everything enqueued so far is committed (or failed)."""
        if self._closed:
            return True
        ev = threading.Event()
        try:
            self._q.put(ev, timeout=timeout)
        except queue.Full:
            return False
        return ev.wait(timeout)

    def close(self, timeout: float = 5.0) -> None:
        if self._closed:
            return
        self.flush(timeout)
        self._closed = True
        try:
            self._q.put(_STOP, timeout=timeout)
        except queue.Full:
            pass
        self._thread.join(timeout)

    # -- crash recovery --------------------------------------------------------
    def recover_unfinished(self, transport: str) -> int:
        """Finalise records a previous run of THIS transport left open.

        A call in flight when the process died (OOM, kill -9, a pod restart)
        never reached CALL_ENDED. Left alone it would look like a call that
        is still going, forever. It gets finalised as `error` /
        `agent_restarted`, with ended_at set to its LAST RECORDED EVENT --
        the last moment there is evidence it was alive -- rather than to now,
        which would claim a call lasted through the outage.

        Scoped to `transport` because main.py and main_pcm.py share this file
        and start independently: without the scope, restarting one would
        finalise the other's live calls. Each transport runs as one process
        (deploy/start_all.sh); running two processes of the SAME transport
        against one file would break this assumption, and is documented as
        unsupported.

        Synchronous -- call it at startup, before any call is accepted.
        Returns the number of records finalised.
        """
        try:
            conn = self._connect()
        except Exception as e:  # noqa: BLE001
            self._fail(f"recovery skipped, cannot open {self.path}: {e}", rows=0)
            return 0
        recovered = 0
        try:
            with conn:
                rows = conn.execute(
                    "SELECT call_id, started_at FROM call_records "
                    "WHERE ended_at IS NULL AND transport = ?", (transport,)).fetchall()
                for call_id, started_at in rows:
                    last_seq, last_ts = conn.execute(
                        "SELECT MAX(seq), MAX(ts) FROM call_events WHERE call_id = ?",
                        (call_id,)).fetchone()
                    ended_at = last_ts or started_at
                    try:
                        duration = round((datetime.datetime.fromisoformat(ended_at)
                                          - datetime.datetime.fromisoformat(started_at))
                                         .total_seconds(), 3)
                    except ValueError:
                        duration = None
                    counts = dict(conn.execute(
                        "SELECT event_type, COUNT(*) FROM call_events WHERE call_id = ? "
                        "GROUP BY event_type", (call_id,)).fetchall())
                    data = {
                        "final_status": STATUS_ERROR,
                        "termination_reason": END_AGENT_RESTARTED,
                        "duration_s": duration,
                        "ended_at_source": "last_recorded_event",
                        "recovered_at": _utcnow(),
                    }
                    conn.execute(_INSERT_EVENT, (
                        uuid.uuid4().hex, call_id, (last_seq or 0) + 1, _utcnow(), CALL_ENDED,
                        0, 0, json.dumps(data, sort_keys=True)))
                    conn.execute(
                        "UPDATE call_records SET ended_at = ?, duration_s = ?, final_status = ?, "
                        "termination_reason = ?, api_calls = ?, errors = ? "
                        "WHERE call_id = ? AND ended_at IS NULL",
                        (ended_at, duration, STATUS_ERROR, END_AGENT_RESTARTED,
                         counts.get(API_REQUEST, 0), counts.get(ERROR, 0), call_id))
                    recovered += 1
        except Exception as e:  # noqa: BLE001
            self._fail(f"recovery of unfinished calls failed: {e}", rows=0)
        finally:
            conn.close()
        return recovered

    # -- reads (staff) ---------------------------------------------------------
    @staticmethod
    def _call_row(row: sqlite3.Row) -> dict:
        return {k: row[k] for k in row.keys()}

    def list_calls(self, limit: int = 50, status: str | None = None) -> list[dict]:
        conn = self._connect()
        conn.row_factory = sqlite3.Row
        try:
            if status:
                rows = conn.execute(
                    "SELECT * FROM call_records WHERE final_status = ? "
                    "ORDER BY started_at DESC LIMIT ?", (status, limit)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM call_records ORDER BY started_at DESC LIMIT ?",
                    (limit,)).fetchall()
            return [self._call_row(r) for r in rows]
        finally:
            conn.close()

    def get_call(self, call_id: str) -> dict | None:
        """The whole record: the call row, every event in order, and an
        integrity block that says whether anything is missing.

        `integrity.complete` is computed from the data, not asserted: event
        numbers must run 1..N without a gap, the first must be CALL_STARTED,
        and the call must have been finalised. A write the store failed to
        make shows up here as a missing number, so a partial record can never
        be mistaken for a whole one."""
        conn = self._connect()
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM call_records WHERE call_id = ?",
                               (call_id,)).fetchone()
            events = conn.execute("SELECT * FROM call_events WHERE call_id = ? ORDER BY seq",
                                  (call_id,)).fetchall()
        finally:
            conn.close()
        if row is None and not events:
            return None
        out_events = []
        for e in events:
            try:
                data = json.loads(e["data"])
            except ValueError:
                data = {"unparseable": e["data"]}
            out_events.append({
                "seq": e["seq"], "event_id": e["event_id"], "ts": e["ts"],
                "event_type": e["event_type"], "turn": e["turn"],
                "success": None if e["success"] is None else bool(e["success"]),
                "data": data,
            })
        seqs = [e["seq"] for e in out_events]
        missing = sorted(set(range(1, (max(seqs) if seqs else 0) + 1)) - set(seqs))
        has_start = bool(out_events) and out_events[0]["event_type"] == CALL_STARTED
        has_end = any(e["event_type"] == CALL_ENDED for e in out_events)
        finalized = bool(row is not None and row["ended_at"])
        return {
            "call": self._call_row(row) if row is not None else None,
            "events": out_events,
            "integrity": {
                "event_count": len(out_events),
                "missing_seq": missing,
                "has_call_started": has_start,
                "has_call_ended": has_end,
                "finalized": finalized,
                "complete": not missing and has_start and has_end and finalized,
            },
        }

    def health(self) -> dict:
        with self._lock:
            return {
                "path": self.path,
                "available": self.available,
                "queued": self._q.qsize(),
                "write_failures": self.write_failures,
                "dropped": self.dropped,
                "last_error": self.last_error,
            }


# ===========================================================================
# THE PER-CALL RECORDER
# ===========================================================================
class CallAudit:
    """Everything one call does, in order. One per CallSession.

    `store` may be None -- a session built by a unit test, or a process whose
    audit database could not be opened. Every method still runs (so the code
    path is identical) and simply has nowhere to write.
    """

    def __init__(self, store: AuditStore | None, call_id: str | None = None, *,
                 transport: str = "", language: str = ""):
        self.call_id = call_id or uuid.uuid4().hex
        self._store = store
        self._seq = 0
        self._seq_lock = threading.Lock()
        self.turn = 0
        self.started_at = _utcnow()
        self._t0 = time.monotonic()
        self.transport = transport
        self.language = language
        self.caller_phone: str | None = None
        self.api_calls = 0
        self.api_failures = 0
        self.errors = 0
        self.served_turns = 0
        self._turn_understood = False
        self._turn_answered = True
        self._turn_crashed = False
        self._last_response_failed = False
        self.ended = False
        self.final_status: str | None = None
        try:
            if store is not None:
                store.start_call(self.call_id, transport, language, self.started_at)
        except Exception as e:  # noqa: BLE001
            self._report(f"could not open call record: {e}")
        self.record(CALL_STARTED, {"transport": transport, "language": language,
                                   "started_at": self.started_at})

    # -- core ----------------------------------------------------------------
    def _report(self, message: str) -> None:
        if self._store is not None:
            self._store._fail(message, call_ids=(self.call_id,))
        else:
            logger.error("AUDIT RECORD LOST [%s]: %s", self.call_id, message)

    def record(self, event_type: str, data: dict | None = None, *,
               success: bool | None = None) -> str | None:
        """Append one event. -> its event_id, or None if it could not be made.

        The payload is serialised HERE, on the caller's side, not later by the
        writer thread -- so a dict the application goes on to mutate (the
        booking slots, session.pending) is captured with the values it had at
        this moment, not whatever it holds by the time the batch commits."""
        try:
            with self._seq_lock:
                self._seq += 1
                seq = self._seq
            event_id = uuid.uuid4().hex
            payload = json.dumps(data or {}, ensure_ascii=False, sort_keys=True, default=str)
            if self._store is not None:
                self._store.add_event(event_id, self.call_id, seq, _utcnow(), event_type,
                                      self.turn, success, payload)
            return event_id
        except Exception as e:  # noqa: BLE001 - auditing never breaks the call
            self._report(f"could not record {event_type}: {e}")
            return None

    # -- the vocabulary main.py uses -------------------------------------------
    def begin_turn(self) -> int:
        """The caller has said (or keyed) something. Everything recorded
        until the next begin_turn belongs to this turn."""
        self.turn += 1
        self._turn_understood = False
        self._turn_answered = False
        self._turn_crashed = False
        return self.turn

    def transcript(self, text: str | None, *, source: str, status: str = "ok",
                   redacted: str | None = None, **detail) -> None:
        """What the caller said, as the system received it.

        `redacted` names WHY the words are withheld (e.g. the caller is
        answering a PIN challenge). The length is kept, so the record still
        shows that something was said -- only the secret itself is dropped,
        the same rule clinic-api's disclosure_audit follows."""
        data = {"source": source, "status": status, **detail}
        if redacted:
            data.update(text=None, redacted=redacted, chars=len(text or ""))
        else:
            data["text"] = text
        self.record(TRANSCRIPT, data, success=(status == "ok"))

    def intent(self, intent: str | None, source: str, *, slots: dict | None = None,
               **detail) -> None:
        """What the system understood, and which component understood it."""
        if intent and intent != "unclear":
            self._turn_understood = True
        self.record(INTENT_DETECTED, {"intent": intent, "source": source, **detail},
                    success=intent != "unclear")
        if slots is not None:
            self.record(SLOTS_EXTRACTED, {"source": source, "slots": dict(slots)})

    def slots(self, source: str, values: dict, **detail) -> None:
        """Slots filled OUTSIDE intent extraction -- the mid-flow parsers."""
        if any(v not in (None, "") for v in values.values()):
            self._turn_understood = True
        self.record(SLOTS_EXTRACTED, {"source": source, "slots": dict(values), **detail},
                    success=any(v not in (None, "") for v in values.values()))

    def agent_response(self, text: str, *, lang: str | None, audio: str,
                       delivered: bool, fallback_reason: str | None = None,
                       tts_error: str | None = None, redact: str | None = None) -> None:
        """What the caller was told -- and how. `audio` is "synthesized",
        "fallback_clip" (TTS failed, a pre-recorded apology played instead of
        these words) or "none"; `delivered` is whether the frames actually
        left the socket."""
        data = {"lang": lang, "audio": audio, "delivered": delivered,
                "fallback_reason": fallback_reason, "tts_error": tts_error}
        if redact:
            data.update(text=None, redacted=redact, chars=len(text or ""))
        else:
            data["text"] = text
        # A reply whose audio was a fallback clip was NOT heard: the caller got
        # a pre-recorded "we're having trouble" instead of these words. That
        # is a failed answer however correct the text was, and the record
        # must not count it as served.
        # On the message channel the words themselves are what reach the
        # patient, so delivered text counts as heard (AUDIO_TEXT).
        heard = delivered and audio in ("synthesized", AUDIO_TEXT)
        failed = fallback_reason in FAILURE_FALLBACKS or (delivered and not heard)
        self.record(AGENT_RESPONSE, data, success=heard and not failed)
        if delivered:
            self._turn_answered = True
        if self.turn == 0:
            return  # the greeting -- not an answer to anything
        if failed:
            self._last_response_failed = True
        elif self._turn_understood and fallback_reason is None:
            self._last_response_failed = False
            self.served_turns += 1
            # Count a turn once, however many sentences answer it.
            self._turn_understood = False

    def error(self, stage: str, exc: BaseException, *, handled: bool = False,
              **detail) -> None:
        """An exception. `handled=True` means the code recovered and the
        caller was still answered (conditioning failing open, the LLM
        apology); False means the exception escaped and nothing answered
        the turn it happened in."""
        self.errors += 1
        if not handled:
            # Nothing has answered since this. Only a later response that
            # actually reaches the caller clears it (see agent_response).
            self._turn_crashed = True
            self._turn_answered = False
        self.record(ERROR, {"stage": stage, "handled": handled,
                            "error_type": type(exc).__name__,
                            "error": _error_text(exc), "where": _where(exc), **detail},
                    success=False)

    def note_caller_phone(self, phone: str | None) -> None:
        """WHO called. This transport has no network caller-ID, so the number
        is the one the caller stated and the backend acted on. The first one
        wins: a later number in the same call (booking for a relative) is in
        that API_REQUEST, not a correction of who rang."""
        if phone and not self.caller_phone:
            self.caller_phone = str(phone)

    # -- backend truth -----------------------------------------------------------
    async def api_call(self, action: str, request: dict, call, summarize=None):
        """Run one clinic-api call and record exactly what went out and what
        came back. Re-raises whatever the call raised, unchanged -- this is an
        observer, not a handler, and main.py's failure paths stay in charge.

        `call` is a zero-argument coroutine factory; `summarize` optionally
        reduces the response before it is recorded (used where the response
        carries something that must not be kept -- a token, a medical
        history). The outcome is always classified from the UNREDUCED
        response."""
        try:
            self.note_caller_phone(request.get("phone"))
            self.api_calls += 1
        except Exception:  # noqa: BLE001
            pass
        request_id = self.record(API_REQUEST, {"action": action, "request": request})
        t0 = time.monotonic()
        try:
            result = await call()
        except asyncio.CancelledError:
            self._api_done(action, request_id, t0, outcome="cancelled", success=False,
                           error="request cancelled before a response arrived")
            raise
        except Exception as e:
            self._api_done(action, request_id, t0, outcome="error", success=False,
                           error_type=type(e).__name__, error=_error_text(e))
            raise
        try:
            outcome, ok, reason = classify_result(result)
            recorded = summarize(result) if summarize else result
        except Exception as e:  # noqa: BLE001 - never let the observer break the call
            outcome, ok, reason, recorded = "ok", True, None, {"unrecordable": str(e)}
        self._api_done(action, request_id, t0, outcome=outcome, success=ok,
                       reason=reason, result=recorded)
        return result

    def _api_done(self, action: str, request_id: str | None, t0: float, *,
                  outcome: str, success: bool, **fields) -> None:
        if not success:
            self.api_failures += 1
        data = {"action": action, "request_event_id": request_id, "outcome": outcome,
                "latency_ms": round((time.monotonic() - t0) * 1000, 1)}
        data.update({k: v for k, v in fields.items() if v is not None})
        self.record(API_RESPONSE, data, success=success)

    # -- the end ---------------------------------------------------------------
    def _final_status(self, termination_reason: str, pending_flow: str | None) -> str:
        if termination_reason in _ABNORMAL_ENDS:
            return STATUS_ERROR
        if termination_reason == END_AT_CAPACITY:
            return STATUS_REJECTED
        if self._turn_crashed and not self._turn_answered:
            return STATUS_ERROR
        if self._last_response_failed:
            return STATUS_FAILED
        if self.served_turns == 0 or pending_flow:
            return STATUS_ABANDONED
        return STATUS_COMPLETED

    def end(self, termination_reason: str, *, pending_flow: str | None = None,
            language: str | None = None, **detail) -> str:
        """Finalise the record. Idempotent -- the first call wins -- because a
        crash path and the normal path can both reach it.

        Events that arrive AFTER this (a turn still in flight when the caller
        hung up, whose booking then completes) are still recorded with the
        next sequence numbers. They really happened, and a booking made after
        the caller left is precisely the kind of thing an audit must show."""
        if self.ended:
            return self.final_status or STATUS_ERROR
        self.ended = True
        try:
            status = self._final_status(termination_reason, pending_flow)
            self.final_status = status
            if language:
                self.language = language
            ended_at = _utcnow()
            duration = round(time.monotonic() - self._t0, 3)
            self.record(CALL_ENDED, {
                "final_status": status, "termination_reason": termination_reason,
                "started_at": self.started_at, "ended_at": ended_at, "duration_s": duration,
                "turns": self.turn, "served_turns": self.served_turns,
                "api_calls": self.api_calls, "api_failures": self.api_failures,
                "errors": self.errors, "pending_flow": pending_flow, **detail,
            }, success=status == STATUS_COMPLETED)
            if self._store is not None:
                self._store.end_call(
                    self.call_id, ended_at=ended_at, duration_s=duration, final_status=status,
                    termination_reason=termination_reason, caller_phone=self.caller_phone,
                    language=self.language, turns=self.turn, served_turns=self.served_turns,
                    api_calls=self.api_calls, api_failures=self.api_failures,
                    errors=self.errors)
            return status
        except Exception as e:  # noqa: BLE001
            self._report(f"could not finalise call: {e}")
            return self.final_status or STATUS_ERROR


# ===========================================================================
# Which call is this? -- for code shared by every call
# ===========================================================================
# ClinicToolsClient is one process-wide object shared by every live call, so
# it cannot hold "the current call" itself. A ContextVar can: ws_audio binds
# the session's CallAudit at the start of the call, and asyncio copies the
# context into every task that call creates (the poll loop, each turn, each
# keypad press). Two calls running concurrently therefore each see their own
# CallAudit, and an API event cannot be filed under the wrong call.
_current: contextvars.ContextVar[CallAudit | None] = contextvars.ContextVar(
    "call_audit", default=None)


def bind(audit: CallAudit | None) -> contextvars.Token:
    return _current.set(audit)


def current() -> CallAudit | None:
    return _current.get()
