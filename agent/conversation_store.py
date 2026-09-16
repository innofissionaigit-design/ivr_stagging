"""Conversation state that survives a switch between the phone and a message.

Author: Chakravardhan
Story:  "As a patient, I want to ask the same questions by message and get the
         same answers, so that I can use the channel I already have open."

WHAT IS KEPT, AND WHAT IS NOT
-----------------------------
One row per patient number: the language they are being served in, the
booking they are part-way through (if any), which channel they were last on,
and when they last wrote to us. That is exactly enough for a booking started
on a call to be finished by message, or the other way round.

NEVER kept here: a verification token, a patient's history or timeline, or
any step of the verification flow. Those die with the conversation that
opened them (main.py CallSession.cleanup()), because the handset is shared --
and a message thread on a shared handset is read by whoever picks it up.

THE NUMBER IS NOT THE KEY
-------------------------
Rows are keyed by an HMAC of the last ten digits under VOICE_AGENT_STATE_KEY,
never by the number itself. A plain hash would not do: there are only ten
billion Indian mobile numbers, and every one of their SHA-256 digests can be
computed in minutes. Without the key, a row cannot be found from a number.

What a row DOES hold is the booking in progress, and a booking's fields are
the name and number the patient gave for it. That is the minimum a booking
needs to be continued at all, and it is why rows are short-lived.

With no key configured, a random one is drawn for this process. A
conversation still carries from one message to the next, but the voice agent
and the message service can no longer read each other's rows -- `shared` is
False, and /api/health says so rather than letting it pass unnoticed.

SHORT-LIVED ON PURPOSE
----------------------
A booking in progress may hold the name the patient gave. Rows stop counting
after VOICE_AGENT_STATE_TTL_S (30 minutes by default). Nothing here is a
record of anything -- the call audit and clinic-api are.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import threading
import time

log = logging.getLogger("agent.conversation_store")

# The flows another channel may pick up. Only the plain booking fields: each
# has a prompt that makes sense on its own when the patient comes back
# (reply_templates.missing_slot_prompt), and none of them is a secret.
# "doctor_choice" needs the list of doctors that was just read out on the
# other channel, and "department_date" the department question around it.
PORTABLE_STATES = frozenset({"date", "time_slot", "patient_name", "phone"})

# Never written at all, on any channel. Both belong to verification: the
# challenge is answered where it was asked, or not at all.
NEVER_STORED_STATES = frozenset({"history_verify", "record_phone"})

# The keys of session.pending that are written. `from_record` is left out on
# purpose: it says the name came from a record verified on THAT conversation,
# and the next conversation has verified nobody.
_STORED_KEYS = ("awaiting", "slots", "candidates", "offered_date", "retries")
_BOOKING_SLOTS = ("doctor_name", "department", "date", "time_slot", "patient_name", "phone")

DEFAULT_TTL_S = 1800.0

# Provider message ids are remembered this long, so a webhook delivered twice
# is answered once. Meta redelivers an unacknowledged webhook for up to seven
# days.
MESSAGE_ID_TTL_S = 7 * 24 * 3600.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    number_key      TEXT PRIMARY KEY,
    lang            TEXT,
    pending         TEXT,
    channel         TEXT NOT NULL,
    updated_at      REAL NOT NULL,
    last_inbound_at REAL
);
CREATE TABLE IF NOT EXISTS seen_messages (
    message_id TEXT PRIMARY KEY,
    seen_at    REAL NOT NULL
);
"""


def default_path() -> str:
    """Beside the call audit on the persistent volume, unless overridden."""
    return os.environ.get("VOICE_AGENT_STATE_DB", "/workspace/conversation_state.db")


def _last10(number: str | None) -> str:
    digits = "".join(ch for ch in str(number or "") if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else ""


def _stored(pending: dict | None) -> dict | None:
    """-> the part of session.pending that may be written down, or None."""
    if not pending or pending.get("awaiting") in NEVER_STORED_STATES:
        return None
    out = {k: pending.get(k) for k in _STORED_KEYS}
    out["slots"] = {k: v for k, v in (pending.get("slots") or {}).items() if k in _BOOKING_SLOTS and v}
    out["retries"] = int(out.get("retries") or 0)
    return out


def portable(pending: dict | None) -> dict | None:
    """-> the booking another channel may continue, or None.

    A fresh retry count: the patient's earlier unparseable answers were on a
    different channel and say nothing about this one."""
    stored = _stored(pending)
    if stored is None or stored.get("awaiting") not in PORTABLE_STATES:
        return None
    return {
        "awaiting": stored["awaiting"],
        "slots": stored["slots"],
        "candidates": None,
        "offered_date": stored.get("offered_date"),
        "retries": 0,
    }


@dataclasses.dataclass(frozen=True)
class Snapshot:
    """What one number left behind. `pending` is already filtered for the
    channel that asked -- see ConversationStore.load()."""

    lang: str | None
    pending: dict | None
    channel: str
    last_inbound_at: float | None


class ConversationStore:
    """The shared state, in one SQLite file every process opens.

    Never raises into a conversation. A database that cannot be opened or
    written is logged, counted in health(), and the conversation carries on
    without memory -- which is what it did before this store existed.
    """

    def __init__(self, path: str | None = None, key: bytes | None = None, ttl_s: float | None = None):
        self.path = path or default_path()
        if key is None:
            key = os.environ.get("VOICE_AGENT_STATE_KEY", "").strip().encode() or None
        self.shared = key is not None
        self._key = key or secrets.token_bytes(32)
        self.ttl_s = (
            float(os.environ.get("VOICE_AGENT_STATE_TTL_S", DEFAULT_TTL_S)) if ttl_s is None else ttl_s
        )
        self._lock = threading.Lock()
        self.write_failures = 0
        self.last_error: str | None = None
        self._conn: sqlite3.Connection | None = None
        try:
            self._conn = sqlite3.connect(self.path, timeout=5.0, check_same_thread=False)
            self._conn.execute("PRAGMA busy_timeout = 5000")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        except sqlite3.Error as e:
            self._fail("open", e)
        if not self.shared:
            log.warning(
                "VOICE_AGENT_STATE_KEY is unset -- conversation state is private to this "
                "process and will not follow a patient between the phone and a message"
            )

    # -- plumbing ------------------------------------------------------------
    def _fail(self, what: str, exc: Exception) -> None:
        self.write_failures += 1
        self.last_error = f"{what}: {type(exc).__name__}: {exc}"[:300]
        log.error("conversation state %s failed: %s", what, type(exc).__name__)

    def key_for(self, number: str | None) -> str | None:
        """-> the row key for a number, or None if it is not a number."""
        digits = _last10(number)
        if not digits:
            return None
        return hmac.new(self._key, digits.encode(), hashlib.sha256).hexdigest()

    def _execute(self, what: str, sql: str, params: tuple) -> list[tuple] | None:
        if self._conn is None:
            return None
        try:
            with self._lock:
                rows = self._conn.execute(sql, params).fetchall()
                self._conn.commit()
            return rows
        except sqlite3.Error as e:
            self._fail(what, e)
            return None

    # -- the conversation ----------------------------------------------------
    def load(self, number: str | None, channel: str) -> Snapshot | None:
        """-> what this number left behind, as `channel` may see it.

        The same channel gets its own flow back whole -- a message thread
        continues exactly where the last message left it. A DIFFERENT channel
        gets only portable(): the plain booking fields, never a list of
        doctors read out somewhere else, never anything of verification.
        A row older than the TTL has no language and no flow any more.
        """
        key = self.key_for(number)
        if key is None:
            return None
        rows = self._execute(
            "load",
            "SELECT lang, pending, channel, updated_at, last_inbound_at "
            "FROM conversations WHERE number_key = ?",
            (key,),
        )
        if not rows:
            return None
        lang, raw, last_channel, updated_at, last_inbound_at = rows[0]
        pending: dict | None = None
        if time.time() - float(updated_at) > self.ttl_s:
            lang = None
        elif raw:
            try:
                decoded = json.loads(raw)
            except ValueError:
                decoded = None
            pending = _stored(decoded) if last_channel == channel else portable(decoded)
        return Snapshot(lang=lang, pending=pending, channel=last_channel, last_inbound_at=last_inbound_at)

    def save(self, number: str | None, *, lang: str | None, pending: dict | None, channel: str) -> bool:
        """Write where this conversation stands. -> False if nothing was written.

        A finished or abandoned booking writes `pending` as NULL, so another
        channel is never offered a flow the patient already closed."""
        key = self.key_for(number)
        if key is None:
            return False
        stored = _stored(pending)
        rows = self._execute(
            "save",
            "INSERT INTO conversations (number_key, lang, pending, channel, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(number_key) DO UPDATE SET "
            "lang = excluded.lang, pending = excluded.pending, "
            "channel = excluded.channel, updated_at = excluded.updated_at",
            (key, lang, json.dumps(stored, ensure_ascii=False) if stored else None, channel, time.time()),
        )
        return rows is not None

    def note_inbound(self, number: str | None, sent_at: float, channel: str) -> None:
        """The patient wrote to us at `sent_at`. Kept as the LATEST such time,
        which is what opens a messaging provider's customer-service window."""
        key = self.key_for(number)
        if key is None:
            return
        self._execute(
            "note_inbound",
            "INSERT INTO conversations (number_key, channel, updated_at, last_inbound_at) "
            "VALUES (?, ?, 0, ?) ON CONFLICT(number_key) DO UPDATE SET "
            "last_inbound_at = MAX(COALESCE(last_inbound_at, 0), excluded.last_inbound_at)",
            (key, channel, sent_at),
        )

    def first_sight(self, message_id: str) -> bool:
        """-> True the first time a provider message id is seen, False after.

        Answers True if the store is unavailable: a patient answered twice is
        a smaller failure than a patient not answered at all."""
        now = time.time()
        self._execute("purge", "DELETE FROM seen_messages WHERE seen_at < ?", (now - MESSAGE_ID_TTL_S,))
        rows = self._execute(
            "first_sight",
            "INSERT INTO seen_messages (message_id, seen_at) VALUES (?, ?) "
            "ON CONFLICT(message_id) DO NOTHING RETURNING message_id",
            (message_id, now),
        )
        return rows is None or len(rows) == 1

    def health(self) -> dict:
        return {
            "available": self._conn is not None,
            "shared_between_channels": self.shared,
            "ttl_s": self.ttl_s,
            "write_failures": self.write_failures,
            "last_error": self.last_error,
        }

    def close(self) -> None:
        if self._conn is not None:
            with self._lock:
                self._conn.close()
            self._conn = None
