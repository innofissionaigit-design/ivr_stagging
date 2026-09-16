# A Single Patient Timeline the Agent Can Read — Old vs New Code and Variables

**Author:** Chakravardhan
**Story:** *As a patient, I want the agent to already know what I have booked here, so that I am not made to recite my own history to the hospital that holds it.*
**Branch:** `dev_chakravardhan`. **Old** = commit `932dbb2` (same as `origin/dev_chakravardhan`). **New** = working tree.
**Status:** implemented and tested **locally only — not committed, not pushed.**

---

## 0. Summary

| | Old | New |
|---|---|---|
| Bookings and tests | two separate lists; appointments had **no doctor names** | **one timeline**, oldest first, with doctor names; plus the **upcoming** bookings, soonest first |
| Finding a patient's bookings | exact phone match — `+91 90000 00001` or `919000000001` were **missed** | matched on the **last 10 digits**, however the number was written |
| "What have I booked?" | **no such intent** — nothing on the line could answer it | new intent `my_bookings`, answered from the timeline |
| Asking for history with no phone number | immediate *"sorry, go to the counter"* | agent **asks which number** the record is under, then verifies |
| A verified caller booking | asked for **name and phone again** | name and phone **filled from the record**; caller is told, name is not read back |
| Reading the record | fetched on every history question | fetched **once per verified call**, re-fetched only after this call changes it |
| Safety | verification + private-room check | **unchanged and reused** — same token, same room check, same audit |

**No new endpoint, no new table, no new dependency, no new environment variable.**

---

## 1. What I found first

| Looked for | Found |
|---|---|
| A way for the agent to see a patient's bookings | only inside `POST /api/v1/history/read`, as a bare list with no doctor names |
| Reschedule / cancel on the phone line | endpoints, client methods and reply templates exist — **but `main.py` never calls them** |
| A "what have I booked" intent | **none** (`agent/llm.py` had 9 intents, none for this) |
| Booking for a known patient | always asks `patient_name` and `phone` (`_BOOKING_FIELDS`) |
| History with no phone given | ends the flow with `verification_failed_reply(True)` |
| Appointment ↔ patient link | `Appointment.phone == Patient.phone`, exact string match |

### Constraints that shaped the design

1. **Privacy rule (CLAUDE.md):** history is disclosed only after verification and only on a private audio path. Bookings are part of the record → the timeline **must** sit behind the same gate.
2. **API contract gate:** any new clinic-api endpoint fails `api-contract` until the snapshot is updated, and updating it is a gate-config change I may not make. → The timeline is returned by the **existing** `POST /api/v1/history/read` (response fields are not part of the contract).
3. **Golden set:** the wording of `verification_prompt`, `history_reply` and `booking_reply` is pinned. → Only **new** i18n keys and **optional** parameters with unchanged defaults.
4. **The LLM never states a fact:** the timeline is read by templates, never shown to the model.

---

## 2. Files at a glance

| File | Status | What it does for the story |
|---|---|---|
| `clinic-api/history_service.py` | modified | builds the single timeline; finds bookings by last 10 digits |
| `clinic-api/main.py` | modified (docstring) | documents the timeline in the read endpoint's response |
| `agent/tools_client.py` | modified | audit keeps timeline **counts**, never contents |
| `agent/llm.py` | modified | new intent `my_bookings` |
| `agent/reply_templates.py` | modified | `bookings_reply()`, purpose-aware `verification_prompt()` |
| `agent/i18n.py` | modified | 8 new `timeline.*` sentences in bn / hi / en |
| `main.py` | modified | loads the timeline once, answers from it, fills bookings from it |
| `main_pcm.py` | regenerated | same changes (`python tools/make_pcm_variant.py`) |
| `tests/test_patient_timeline.py` | **new** | 27 test functions (31 test cases) |
| `IMPLEMENTATION_PATIENT_TIMELINE_OLD_VS_NEW.md` | **new** | this document |

---

## 3. Variables — Old vs New

### 3.1 Environment variables
None added. The feature inherits the history feature's switches unchanged:
`VOICE_AGENT_HISTORY_DISCLOSURE`, `VOICE_AGENT_HISTORY_REQUIRE_PRIVATE_PATH`, `VOICE_AGENT_VERIFY_*`.

### 3.2 `clinic-api/history_service.py`

| Name | Kind | Old | New | Line |
|---|---|---|---|---|
| `KIND_APPOINTMENT` | constant | — | `"appointment"` | 205 |
| `KIND_TEST` | constant | — | `"test"` | 206 |
| `_today()` | function | — | today's date (patched in tests) | 209 |
| `_last10(phone)` | function | — | last 10 digits of any written number | 215 |
| `_spoken_alias(aliases_bn)` | function | — | first Bengali alias of a doctor | 219 |
| `patient_appointments(db, phone)` | function | — | all bookings under a number, any format | 229 |
| `_doctors_for(db, appointments)` | function | — | `{doctor_id: Doctor}` in one query | 247 |
| `build_timeline(...)` | function (pure) | — | `{"timeline", "upcoming_appointments"}` | 255 |
| `appointments` (in `history()`) | local | exact phone query | `patient_appointments(...)`, newest first | 309 |
| `line` (in `history()`) | local | — | the built timeline | 309 |

### 3.3 `POST /api/v1/history/read` response fields

| Field | Old | New |
|---|---|---|
| `found`, `patient_name`, `tests`, `appointments` | present | **unchanged** |
| `timeline` | — | every entry, oldest first (cancelled included) |
| `upcoming_appointments` | — | live bookings dated today or later, soonest first |

**Timeline entry — appointment:** `kind`, `date`, `time_slot`, `status`, `upcoming`, `confirmation_id`, `doctor_name`, `doctor_name_bn`
**Timeline entry — test:** `kind`, `date`, `time_slot` (null), `upcoming` (false), `test_name`, `test_name_bn`, `report_ready`, `report_ready_on`

`DisclosureAudit.detail` — old `"N tests, M appointments"` → new `"N tests, M appointments, K upcoming"`.

### 3.4 `agent/reply_templates.py`

| Name | Kind | Old | New | Line |
|---|---|---|---|---|
| `PURPOSE_HISTORY` | constant | — | `"history"` | 405 |
| `PURPOSE_BOOKINGS` | constant | — | `"bookings"` | 406 |
| `verification_prompt(factor, lang)` | function | 2 params | `+ purpose=PURPOSE_HISTORY` (default = old wording) | 409 |
| `BOOKINGS_SPOKEN_LIMIT` | constant | — | `3` | 512 |
| `bookings_reply(result, lang)` | function | — | speaks upcoming bookings | 515 |

### 3.5 `agent/llm.py`

| Name | Old | New |
|---|---|---|
| `VALID_INTENTS` | 9 intents | **10** — adds `"my_bookings"` |
| `SYSTEM_PROMPT_TEMPLATE` | — | new bullet for `my_bookings`; added to the JSON intent union |

### 3.6 `agent/tools_client.py`

| Name | Old | New |
|---|---|---|
| `_history_summary(result)` | `{found, reason, tests, appointments}` | same, **plus** `timeline` and `upcoming_appointments` counts **when present** |
| `out`, `key` | — | locals building that summary |

### 3.7 `agent/i18n.py` — new sentence keys (each in bn / hi / en)

| Key | Used for |
|---|---|
| `timeline.ask_pin` | PIN challenge before reading bookings |
| `timeline.ask_dob` | date-of-birth challenge before reading bookings |
| `timeline.ask_phone` | "Which phone number is your record under?" |
| `timeline.no_bookings` | no upcoming booking |
| `timeline.intro` | "Appointments booked for you: {count}." |
| `timeline.item` | "{doctor}, {date}, at {time}." |
| `timeline.more` | "{count} more — the counter can tell you the rest" |
| `timeline.used_record` | "I have booked it under the name and number already on your record." |

### 3.8 `main.py` (same in `main_pcm.py`)

**`CallSession` attributes**

| Attribute | Old | New | Line |
|---|---|---|---|
| `self.timeline` | — | `dict \| None` — the verified caller's record, this call only | 504 |
| `self.timeline_stale` | — | `bool` — set after this call books, forces a re-read | 505 |
| `cleanup()` | drops token, phone | also drops `self.timeline` | 646 |

**Functions**

| Function | Old | New | Line |
|---|---|---|---|
| `_finish_booking(session, slots)` | 2 params | `+ from_record=False`; marks timeline stale; appends `timeline.used_record` | 824 |
| `_start_history_verification(session, phone)` | 2 params | `+ purpose`; asks for the number instead of failing | 1178 |
| `_load_timeline(session)` | — | **new** — fetch once, cache, re-fetch if stale | 1229 |
| `_speak_history(session)` | fetched every time | room check → `_load_timeline` → speak | 1264 |
| `_speak_bookings(session)` | — | **new** — room check → `_load_timeline` → `bookings_reply` | 1284 |
| `_fill_from_record(session, slots)` | — | **new** — fills empty `patient_name`, `phone` | 1302 |
| `_continue_history_verification` | always `_speak_history` | speaks bookings or history by `purpose` | 1325 |

**Locals / keys**

| Name | Where | Values |
|---|---|---|
| `purpose` | `_continue_history_verification`, `record_phone` branch | `"history"` / `"bookings"` |
| `phone` | `record_phone` branch | parsed 10-digit number or `None` |
| `from_record` | `book_appointment` branch | `True` if the record filled a field |
| `reply` | `_finish_booking` | booking reply, maybe + `timeline.used_record` |
| `result` | `_load_timeline`, `_speak_*` | the history/timeline response |
| `record`, `filled` | `_fill_from_record` | the held timeline; whether anything was filled |

**`session.pending` keys and states**

| Key / state | Old | New |
|---|---|---|
| `"purpose"` | — | on `history_verify` and `record_phone` |
| `"from_record"` | — | on booking states |
| `awaiting = "record_phone"` | — | **new state**: caller says which number the record is under |

---

## 4. `clinic-api/history_service.py` — Old vs New

### 4.1 Import

```python
# OLD
from models import Appointment, DisclosureAudit, Patient, TestRecord
# NEW
from models import APPT_CANCELLED, Appointment, DisclosureAudit, Doctor, Patient, TestRecord
```

### 4.2 Finding the patient's bookings

```python
# OLD (inside history())
appointments = (db.query(Appointment).filter_by(phone=patient.phone)
                  .order_by(Appointment.date.desc()).all())
```

```python
# NEW
def _last10(phone: object) -> str:
    return "".join(ch for ch in str(phone or "") if ch.isdigit())[-10:]


def patient_appointments(db: Session, phone: object) -> list[Appointment]:
    digits = _last10(phone)
    if len(digits) < 10:
        return []
    rows = db.query(Appointment).filter(Appointment.phone.like(f"%{digits[-4:]}%")).all()
    return [a for a in rows if _last10(a.phone) == digits]
```

### 4.3 The single timeline — OLD: none. NEW:

```python
def build_timeline(appointments, tests, doctors: dict, today_iso: str) -> dict:
    entries: list[dict] = []
    for a in appointments:
        doctor = doctors.get(a.doctor_id)
        entries.append({
            "kind": KIND_APPOINTMENT, "date": a.date, "time_slot": a.time_slot,
            "status": a.status,
            "upcoming": a.status != APPT_CANCELLED and (a.date or "") >= today_iso,
            "confirmation_id": a.confirmation_id,
            "doctor_name": doctor.name if doctor else None,
            "doctor_name_bn": _spoken_alias(doctor.aliases_bn) if doctor else None,
        })
    for t in tests:
        entries.append({
            "kind": KIND_TEST, "date": t.taken_on, "time_slot": None, "upcoming": False,
            "test_name": t.test_name, "test_name_bn": t.test_name_bn,
            "report_ready": bool(t.report_ready), "report_ready_on": t.report_ready_on,
        })
    entries.sort(key=lambda e: (e["date"] or "", e["time_slot"] or ""))
    return {"timeline": entries,
            "upcoming_appointments": [e for e in entries if e["upcoming"]]}
```

### 4.4 `history()`

```python
# OLD
    tests = (db.query(TestRecord).filter_by(patient_id=patient.id)
               .order_by(TestRecord.taken_on.desc()).all())
    appointments = (db.query(Appointment).filter_by(phone=patient.phone)
                      .order_by(Appointment.date.desc()).all())

    _audit(db, ..., detail=f"{len(tests)} tests, {len(appointments)} appointments")
    db.commit()

    return {
        "patient_name": patient.full_name,
        "tests": [...],
        "appointments": [...],
    }
```

```python
# NEW
    tests = (db.query(TestRecord).filter_by(patient_id=patient.id)
               .order_by(TestRecord.taken_on.desc()).all())
    appointments = sorted(patient_appointments(db, patient.phone),
                          key=lambda a: a.date, reverse=True)
    line = build_timeline(appointments, tests, _doctors_for(db, appointments),
                          _today().isoformat())

    _audit(db, ..., detail=(f"{len(tests)} tests, {len(appointments)} appointments, "
                            f"{len(line['upcoming_appointments'])} upcoming"))
    db.commit()

    return {
        "timeline": line["timeline"],
        "upcoming_appointments": line["upcoming_appointments"],
        "patient_name": patient.full_name,
        "tests": [...],            # unchanged
        "appointments": [...],     # unchanged
    }
```

---

## 5. `agent/tools_client.py` — Old vs New

```python
# OLD
def _history_summary(result):
    if not isinstance(result, dict):
        return result
    return {"found": result.get("found"), "reason": result.get("reason"),
            "tests": len(result.get("tests") or []),
            "appointments": len(result.get("appointments") or [])}
```

```python
# NEW
def _history_summary(result):
    if not isinstance(result, dict):
        return result
    out = {"found": result.get("found"), "reason": result.get("reason"),
           "tests": len(result.get("tests") or []),
           "appointments": len(result.get("appointments") or [])}
    for key in ("timeline", "upcoming_appointments"):
        if key in result:
            out[key] = len(result.get(key) or [])
    return out
```

The call audit therefore records **how many** entries were read, never which doctors, dates or tests.

---

## 6. `agent/llm.py` — Old vs New

```python
# OLD
VALID_INTENTS = {"test_rate", "doctor_availability", "book_appointment",
                 "doctors_by_department", "payment", "report_collection",
                 "patient_history", "smalltalk", "unclear"}
# NEW
VALID_INTENTS = {"test_rate", "doctor_availability", "book_appointment",
                 "doctors_by_department", "payment", "report_collection",
                 "patient_history", "my_bookings", "smalltalk", "unclear"}
```

```text
# NEW prompt bullet
- "my_bookings": caller is asking what appointments they ALREADY have booked, or when their
  existing appointment is (e.g. "আমার কী কী বুকিং আছে", "আমার অ্যাপয়েন্টমেন্ট কবে",
  "मेरी अपॉइंटमेंट कब है", "what have I booked"). Asking to MAKE a new booking is
  "book_appointment", not this.
```

The fast path needs no change: "বুক" / "অ্যাপয়েন্টমেন্ট" are booking cues, so these questions always reach the model.

---

## 7. `agent/reply_templates.py` — Old vs New

### 7.1 `verification_prompt`

```python
# OLD
def verification_prompt(factor: str, lang: str | None = None) -> str:
    code = _lang(lang)
    return t(code, "history.ask_pin" if factor == "pin" else "history.ask_dob")
```

```python
# NEW  (default purpose -> identical output, so the golden set is untouched)
PURPOSE_HISTORY = "history"
PURPOSE_BOOKINGS = "bookings"

def verification_prompt(factor: str, lang: str | None = None,
                        purpose: str = PURPOSE_HISTORY) -> str:
    code = _lang(lang)
    prefix = "timeline" if purpose == PURPOSE_BOOKINGS else "history"
    return t(code, f"{prefix}.ask_pin" if factor == "pin" else f"{prefix}.ask_dob")
```

### 7.2 `bookings_reply` — OLD: none. NEW:

```python
BOOKINGS_SPOKEN_LIMIT = 3

def bookings_reply(result: dict, lang: str | None = None) -> str:
    code = _lang(lang)
    upcoming = (result or {}).get("upcoming_appointments") or []
    if not upcoming:
        return t(code, "timeline.no_bookings")
    reply = t(code, "timeline.intro", count=len(upcoming))
    for item in upcoming[:BOOKINGS_SPOKEN_LIMIT]:
        reply += t(code, "timeline.item",
                   doctor=_spoken_doctor_name({}, item, code),
                   date=item.get("date") or "", time=item.get("time_slot") or "")
    remaining = len(upcoming) - BOOKINGS_SPOKEN_LIMIT
    if remaining > 0:
        reply += t(code, "timeline.more", count=remaining)
    return reply
```

No confirmation number is ever spoken.

---

## 8. `main.py` — Old vs New

### 8.1 Imports

```python
# OLD
from agent.reply_templates import (
    verification_prompt, verification_failed_reply, verification_locked_reply,
    disclosure_blocked_reply, history_reply,
)
# NEW
from agent.reply_templates import (
    verification_prompt, verification_failed_reply, verification_locked_reply,
    disclosure_blocked_reply, history_reply,
    bookings_reply, PURPOSE_BOOKINGS, PURPOSE_HISTORY,
)
```

### 8.2 `CallSession.__init__` and `cleanup()`

```python
# OLD
        self.history_token: str | None = None
        self.history_phone: str | None = None
...
    def cleanup(self):
        self.history_token = None
        self.history_phone = None
```

```python
# NEW
        self.history_token: str | None = None
        self.history_phone: str | None = None
        self.timeline: dict | None = None      # the verified record, this call only
        self.timeline_stale = False            # this call changed the record
...
    def cleanup(self):
        self.history_token = None
        self.history_phone = None
        self.timeline = None                   # goes with the token that opened it
```

### 8.3 `_start_history_verification` — no phone given

```python
# OLD
async def _start_history_verification(session, phone):
    if not phone:
        await _speak(session, verification_failed_reply(True, session.lang))
        return
    ...
    session.pending = {"awaiting": "history_verify", "factor": ..., ...}
    await _speak(session, verification_prompt(session.pending["factor"], session.lang))
```

```python
# NEW
async def _start_history_verification(session, phone, purpose=PURPOSE_HISTORY):
    if not phone:
        session.pending = {
            "awaiting": "record_phone", "purpose": purpose, "slots": {},
            "candidates": None, "offered_date": None, "retries": 0,
        }
        await _speak(session, _t(session.lang, "timeline.ask_phone"))
        return
    ...
    session.pending = {"awaiting": "history_verify", "factor": ..., "purpose": purpose, ...}
    await _speak(session, verification_prompt(session.pending["factor"], session.lang, purpose))
```

### 8.4 New `record_phone` state in `_continue_pending`

```python
# NEW (before the booking escape hatch)
    if awaiting == "record_phone":
        purpose = pending.get("purpose") or PURPOSE_HISTORY
        if is_negative(text):
            session.pending = None
            await _speak(session, _t(session.lang, "fallback.greeting"))
            return True
        phone = parse_phone(text)
        _audit(session).slots("slot_parse", {"phone": phone}, awaiting="record_phone")
        if phone is None:
            pending["retries"] += 1
            if pending["retries"] > 2:
                session.pending = None
                return False
            await _speak(session, _t(session.lang, "timeline.ask_phone"))
            return True
        session.pending = None
        await _start_history_verification(session, phone, purpose)
        return True
```

### 8.5 Reading the record: `_load_timeline`, `_speak_history`, `_speak_bookings`

```python
# OLD
async def _speak_history(session):
    if not await _history_guard(session, session.history_phone or ""):
        return
    try:
        result = await _tools.read_history(session.history_token, session.call_id)
    except ToolCallError as e:
        ...speak generic.tool_failure; return
    if not result.get("found"):
        session.history_token = None
        await _speak(session, verification_failed_reply(True, session.lang))
        return
    await _speak(session, history_reply(result, session.lang), audit_redact="patient_history")
```

```python
# NEW
async def _load_timeline(session) -> dict | None:
    if session.timeline is not None and not session.timeline_stale:
        return session.timeline                          # read once per call
    try:
        result = await _tools.read_history(session.history_token, session.call_id)
    except ToolCallError as e:
        ...speak generic.tool_failure; return None
    if not result.get("found"):
        session.history_token = None
        session.timeline = None
        await _speak(session, verification_failed_reply(True, session.lang))
        return None
    session.timeline, session.timeline_stale = result, False
    return result


async def _speak_history(session):
    if not await _history_guard(session, session.history_phone or ""):   # room first
        return
    result = await _load_timeline(session)
    if result is None:
        return
    await _speak(session, history_reply(result, session.lang), audit_redact="patient_history")


async def _speak_bookings(session):
    if not await _history_guard(session, session.history_phone or ""):   # room first
        return
    result = await _load_timeline(session)
    if result is None:
        return
    await _speak(session, bookings_reply(result, session.lang), audit_redact="patient_timeline")
```

### 8.6 After verification

```python
# OLD
    if reply == "verified":
        session.pending = None
        session.history_token = outcome.get("token")
        await _speak_history(session)
```

```python
# NEW
    purpose = (pending or {}).get("purpose") or PURPOSE_HISTORY
    ...
    if reply == "verified":
        session.pending = None
        session.history_token = outcome.get("token")
        if purpose == PURPOSE_BOOKINGS:
            await _speak_bookings(session)
        else:
            await _speak_history(session)
```

### 8.7 Intent dispatch

```python
# OLD
            elif intent == "patient_history":
                if session.history_token:
                    await _speak_history(session)
                else:
                    await _start_history_verification(session, slots.get("phone"))
```

```python
# NEW
            elif intent == "patient_history":
                if session.history_token:
                    await _speak_history(session)
                else:
                    await _start_history_verification(
                        session, slots.get("phone") or session.history_phone)

            elif intent == "my_bookings":
                if session.history_token:
                    await _speak_bookings(session)
                else:
                    await _start_history_verification(
                        session, slots.get("phone") or session.history_phone,
                        PURPOSE_BOOKINGS)
```

### 8.8 Booking without reciting — `_fill_from_record` (new)

```python
def _fill_from_record(session, slots: dict) -> bool:
    record = session.timeline if session.history_token else None
    if not record:
        return False
    filled = False
    if not slots.get("patient_name") and record.get("patient_name"):
        slots["patient_name"] = record["patient_name"]
        filled = True
    if not slots.get("phone") and session.history_phone:
        slots["phone"] = session.history_phone
        filled = True
    return filled
```

Used in both booking paths:

```python
# OLD (book_appointment branch)
                missing = _next_missing(merged)
                if missing is None:
                    await _finish_booking(session, merged)
# NEW
                from_record = _fill_from_record(session, merged)
                missing = _next_missing(merged)
                if missing is None:
                    await _finish_booking(session, merged, from_record=from_record)
                    return
                session.pending = {..., "from_record": from_record}
```

```python
# OLD (_continue_pending tail)
    pending["slots"][awaiting] = value
    pending["retries"] = 0
    missing = _next_missing(pending["slots"])
    if missing is None:
        await _finish_booking(session, pending["slots"])
# NEW
    pending["slots"][awaiting] = value
    pending["retries"] = 0
    if _fill_from_record(session, pending["slots"]):
        pending["from_record"] = True
    missing = _next_missing(pending["slots"])
    if missing is None:
        await _finish_booking(session, pending["slots"],
                              from_record=bool(pending.get("from_record")))
```

### 8.9 `_finish_booking`

```python
# OLD
async def _finish_booking(session, slots):
    ...
    await _speak(session, booking_reply(slots, result, session.lang))
```

```python
# NEW
async def _finish_booking(session, slots, from_record=False):
    ...
    reply = booking_reply(slots, result, session.lang)
    if result.get("success"):
        session.timeline_stale = True                 # next read re-fetches
        if from_record:
            reply += _t(session.lang, "timeline.used_record")
    await _speak(session, reply)
```

---

## 9. How a call works now

```text
Caller: "আমার কী বুকিং আছে"            (what have I booked?)
  └─ intent my_bookings, not verified
      ├─ no number known ─► "Which phone number is your record under?"   [record_phone]
Caller: "9000000101"
      ├─ room check (speakerphone / unknown path → refuse, audited)
      └─ begin_verification ─► "Before I tell you your bookings… date of birth?"
Caller: "14-05-1990"
  └─ verify_caller ─► verified ─► room check again
      └─ read_history (ONCE) ─► session.timeline
          └─ "Appointments booked for you: 2. Dr. Ghosh, 2026-09-11, at 07:25. …"
Caller: "আমার আগের টেস্টগুলো"        (my past tests)  ─► from session.timeline, no fetch
Caller: "সেনের কাছে ২০ তারিখ ১৮:১৫ বুক করুন"
  └─ name + phone filled from the record ─► booked
      └─ "…confirmed… I have booked it under the name and number already on your record."
          └─ timeline_stale = True (next question re-reads)
Hang up ─► cleanup(): token, phone, timeline dropped
```

---

## 10. Tests — `tests/test_patient_timeline.py`

27 test functions, 31 cases, **all passing**. Test data from the approved fictional set (phones `9000000101`/`9000000102`, names *Iti Sen*, *Tapan Nandi*, *Jaya Sen*, DOB `1990-05-14`).

| Area | Tests |
|---|---|
| **clinic-api, real endpoints** | timeline opens only with a token · bookings and tests in one list, oldest first, with doctor names · upcoming = live, today onward, soonest first · `+91 …` and `91…` numbers still found · another patient's booking never appears · no clinical result in a test entry · disclosure audited with counts, not contents · `build_timeline` (pure): today counts as upcoming, cancelled never does |
| **What the caller hears** | read soonest first · **no confirmation number in any language** · only 3 read, rest to the counter · "no bookings" stated plainly · history wording unchanged, bookings challenge says "bookings" · every `timeline.*` key in bn/hi/en · extractor knows `my_bookings` · audit summary keeps counts only |
| **Agent flow** | "what have I booked" → ask number → challenge → bookings (redacted in audit) · record fetched once per call · nothing fetched before verification · nothing read on a speakerphone (refusal audited) · verified caller not asked name/phone · caller's own words win · multi-turn booking also filled · unverified caller still asked · booking on this call → next answer re-reads · record dies with the call |
| **End to end** | agent's **real** `ClinicToolsClient` against the **real** clinic-api: verification → timeline → spoken bookings in the right order, cancelled and past left out, no reference number |

```bash
python -m pytest tests/test_patient_timeline.py -v
```

**Full suite:** 562 passed, 2 failed. The 2 failures are the pre-existing golden-set entry for the unknown-doctor fast-path fix (`tests/golden/golden_set.json` needs a code owner to regenerate) — unrelated to this story.

---

## 11. Quality gate (`bash scripts/gate.sh --full`)

| Check | Result |
|---|---|
| lint, typecheck, dead-code, static-analysis, secrets, debug-code | **PASS** — no new findings |
| api-contract | **PASS** — no endpoint added, contracts unchanged |
| build | **PASS** — `main_pcm.py` in sync |
| phi-in-code, phi-in-logs, approved-test-data | **PASS** |
| gate-protection | **PASS** — no test weakened, no suppression added |
| safety-policy, phi-boundary, multilingual-smoke, integration, telephony, handoff, pstn-8khz | **PASS** |
| format | FAIL — 42 pre-existing unformatted files (reformat on hold by owner decision); the new test file **is** formatted |
| golden-set, escalation-abstention, unit-tests | FAIL — the same pre-existing golden entry above |

---

## 12. Risks and limitations

1. **Matched by phone, like the history before it.** A booking made from this number for a relative appears in the owner's timeline. The entry is read with doctor, date and time only — never the relative's name.
2. **"Upcoming" uses the server's date** (`datetime.date.today()`, as the rest of the agent does). On a server clock in UTC, from 00:00 to 05:30 IST the server still thinks it is yesterday, so yesterday's booking is read out as upcoming for those hours. The pod's timezone should be Asia/Kolkata.
3. **Reschedule and cancel are still not on the phone line.** The timeline now holds each booking's `confirmation_id` so they can be wired without the patient reciting it — that wiring is the natural next story, not done here.
4. **One phone-number question remains.** With no caller-ID on this transport, the agent must ask which number the record is under. When telephony arrives, the caller-ID should fill it.
5. **Not tested on the GPU pod.** No real ASR of a spoken phone number or date of birth, no real LLM classification of `my_bookings`.
6. **Hindi and English wording unreviewed**, as with the earlier stories.

## 13. To-dos

1. Code owner regenerates `tests/golden/golden_set.json` (pre-existing, unrelated) so the gate can go green.
2. Wire reschedule / cancel to use `upcoming_appointments` — with an explicit spoken "yes" before cancelling.
3. Put telephony caller-ID into `history_phone` when a SIP/PSTN adapter exists.
4. Live check on the pod: ask "আমার কী বুকিং আছে", verify, confirm the right bookings are read and the call audit shows `patient_timeline` redaction.
5. Review the Hindi and English `timeline.*` sentences with native speakers.
