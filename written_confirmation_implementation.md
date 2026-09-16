# Written patient confirmations — implementation notes

**Story**

> As a patient, I want a written confirmation I can show at reception, so
> that I do not have to remember a reference number.

**Acceptance criteria**

> A message is sent on booking, reschedule and cancellation through the
> hospital gateway, with templates registered under the applicable telecom
> regulations. Delivery receipts are recorded and failures surfaced to staff
> rather than silently dropped.

Branch `dev-chakravardhan`. **Nothing is committed** — every change below is in
the working tree only. Suite: **224 passed** (169 pre-existing, 55 new).

---

## 1. Verdict against the acceptance criteria

| Clause | State |
|---|---|
| A message is sent on **booking** | ✅ `book_appointment()` |
| …on **reschedule** | ✅ `reschedule_appointment()` — the endpoint had to be built |
| …on **cancellation** | ✅ `cancel_appointment()` — the endpoint had to be built |
| …through the **hospital gateway** | ✅ `notifications.send()`, provider-agnostic, config-driven |
| with **templates registered** under the applicable telecom regulations | ⚠️ **Mechanism complete. No template is actually registered yet** — §9 |
| **Delivery receipts are recorded** | ✅ `POST /api/v1/notifications/receipt` → ledger |
| **failures surfaced to staff** | ⚠️ **API, health counter and logs. No staff screen** — §14 |
| rather than **silently dropped** | ✅ — this is what defect 3 in §13 was about |

Two clauses are marked ⚠️ rather than ✅, and neither is a coding shortcut:

- **Template registration is an act performed on the operator's DLT portal** by
  whoever holds the hospital's account. Everything enforceable in code is
  enforced (§7); the three template IDs default to empty and `preflight()`
  refuses to send in live mode until they are set. §9 has the go-live checklist.
- **"Surfaced to staff" is served as an API**, not a screen. This repository has
  no UI to put a dashboard in. Whether that satisfies the clause depends on
  whether the clinic has something that polls it.

I re-read every clause against the code after the work looked finished. That
audit found **three real defects**, all now fixed, each with a regression test
confirmed to fail against the pre-fix code — §13.

---

## 2. Where the code actually started

I searched the whole repository, across every branch, before writing anything.
The result shapes every decision below:

| Looked for | Found |
|---|---|
| `sms`, `whatsapp`, `twilio`, `msg91`, `gupshup`, `smtp`, `sendgrid`, `email`, `notify`, `gateway`, `webhook`, `dlt` in any `.py/.sh/.txt/.html/.js/.json` | **0 matches** |
| Messaging or mail dependency in either `requirements.txt` | none |
| Any credential or gateway env var (55 env vars read by the codebase) | **none — the repo had no secrets mechanism at all** |
| Notification route in any service | none |
| Outbound HTTP beyond `localhost` | **none** — only Ollama, clinic-api, TTS |
| Notification code on `main`, `dev_rajarshee`, or anywhere in history | none |

No channel existed. This is the first outbound-internet call and the first
credential the project has ever needed.

Two things in the existing code pointed at **SMS** rather than email, and I built
to those rather than to the story text:

1. **`agent/reply_templates.py:53` already promised a message.** The prompt used
   when the phone slot is missing reads
   *"একটা ফোন নম্বর দেবেন, যাতে কনফার্মেশন পাঠাতে পারি?"* — "give me a phone
   number **so I can send the confirmation**". Nothing sent anything. That was a
   live unkept promise in the shipped build, independent of this story.
2. **`agent/slot_parse.py:233` already normalised the number for SMS** — Bengali
   numerals folded to ASCII, reduced to the last 10 digits, `+91`/`0` trunk
   prefix stripped. One country code away from E.164. **No email address is
   collected anywhere in the system.**

Three events were needed and only one existed: there was **no way to reschedule
or cancel an appointment at all**. Those endpoints had to be built before there
was anything to message about.

---

## 3. What changed, at a glance

### New files

| File | Lines | Purpose |
|---|---:|---|
| `clinic-api/message_templates.py` | 242 | The DLT-registered bodies. The only text that may be sent. |
| `clinic-api/notifications.py` | 323 | Gateway client, status vocabulary, receipt mapping. **No ORM imports** — unit-testable with no database. |
| `clinic-api/notify_service.py` | 399 | The ledger operations: the only place the gateway and the database meet. |
| `clinic-api/migrate_notifications.py` | 170 | One-shot schema migration for an existing `clinic.db`. Run by hand. |
| `tests/test_notifications.py` | 830 | 55 tests. |

### Modified files

| File | Δ | What |
|---|---:|---|
| `clinic-api/main.py` | +503 | Notification hook on all three events; reschedule + cancel endpoints; receipt callback; staff queue; schema check. |
| `clinic-api/models.py` | +116 | `NotificationAttempt` table; three columns on `Appointment`; the unique-constraint change. |
| `agent/reply_templates.py` | +81 | Conditional SMS promise; `reschedule_reply()`, `cancel_reply()`. |
| `agent/tools_client.py` | +64 | `reschedule_appointment()`, `cancel_appointment()`, documented `notification` field. |
| `deploy/env.sh` | +46 | Gateway config. |
| `deploy/env.vast.sh` | +36 | Same, plus one vast.ai-specific consequence. |
| `README.md` | +19 | Short section pointing here. |

844 insertions, 21 deletions across the seven modified files.

**Unchanged on purpose:** `main.py` and `main_pcm.py` (the voice agent turn
loops), `requirements.txt` (both of them), `agent/llm.py`, `agent/asr.py`,
`agent/tts.py`, `agent/slot_parse.py`.

---

## 4. The design, and why each part is shaped that way

### 4.1 One transaction carries the booking and the record that a message is owed

`agent/tools_client.py` gives up on clinic-api after **4.0 seconds**, and a
caller is holding a live phone line for every one of them. An SMS gateway is a
third-party hop over the public internet with a multi-second tail — putting it
inline would spend the caller's whole budget on a message they are not waiting
for.

So the sequence is fixed, and it is the sequence the acceptance criterion
depends on:

```
1. _prepare_notification()   stages the ledger row as `queued`   ─┐ ONE
2. db.commit()               persists the appointment AND the row ─┘ transaction
3. _schedule_delivery()      only now is the network touched, from a BackgroundTask
```

Steps 1 and 2 sharing a transaction is what makes *"silently dropped"*
unrepresentable. Crash after the commit → a `queued` row survives and the staff
queue reports it stale. Crash or rollback before it → there is no appointment
either, so nothing was owed. There is no third case.

This works because `confirmation_id` is generated in Python before the insert,
not by the database, so the message can be composed against an appointment row
that has not been written yet.

**A message failure never becomes a booking failure.** `_prepare_notification()`
cannot raise into its caller; it returns `None` and the booking proceeds.

> This is the part the audit caught me getting wrong the first time — the two
> were originally committed separately. See defect 3 in §13.

### 4.2 The endpoint's own latency is unchanged

One extra `INSERT`. The gateway POST happens after the response is on the wire.

### 4.3 No retry loop, deliberately

Each background task makes exactly one attempt. A retry-with-backoff would sleep
inside a thread from the pool that also serves price and availability lookups —
`tools_client.py` documents the 15-connection ceiling this service works
against. Occupying those threads on behalf of a patient who is no longer on the
phone is the wrong trade at the busy hour. Failures go to staff, who can retry
from the ledger. That is also what the criterion asks for.

### 4.4 `urllib`, not `httpx`

`clinic-api/requirements.txt` has no HTTP client, and package installation on
this pod has repeatedly been the failure mode. `agent/llm.py` faces the same
choice for Ollama and answers it with `urllib.request`. **No new dependency was
added to either requirements file.**

---

## 5. Old code → new code

### 5.1 `clinic-api/models.py` — the constraint change

The subtlest change in the set, and the one to review most carefully.

**Old**

```python
class Appointment(Base):
    __tablename__ = "appointments"
    id = Column(Integer, primary_key=True)
    confirmation_id = Column(String, nullable=False, unique=True)
    doctor_id = Column(Integer, ForeignKey("doctors.id"), nullable=False)
    date = Column(String, nullable=False)
    time_slot = Column(String, nullable=False)
    patient_name = Column(String, nullable=False)
    phone = Column(String, nullable=False)
    created_at = Column(DateTime, nullable=False)

    doctor = relationship("Doctor")

    __table_args__ = (UniqueConstraint("doctor_id", "date", "time_slot",
                                       name="uq_doctor_slot"),)
```

**New**

```python
APPT_BOOKED = "booked"
APPT_RESCHEDULED = "rescheduled"
APPT_CANCELLED = "cancelled"
SLOT_LOCK_ACTIVE = "ACTIVE"


class Appointment(Base):
    ...                                    # the eight original columns, unchanged
    status = Column(String, nullable=False, default=APPT_BOOKED)
    slot_lock = Column(String, nullable=False, default=SLOT_LOCK_ACTIVE)
    updated_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("doctor_id", "date", "time_slot", "slot_lock",
                         name="uq_doctor_slot"),
    )
```

**Why the fourth column.** The old constraint is what makes double-booking
impossible when two callers race — `book_appointment()`'s `IntegrityError`
handler turns that race into *"that slot just went, here are three others"*.

Cancellation breaks that arrangement:

- A cancelled row **must be kept** — it is the audit trail, and the
  cancellation's ledger row points at it. *"We have no record of that
  appointment"* is the worst answer a hospital counter can give.
- But a kept row goes on **occupying its slot** under the old constraint. The
  cancellation would free the patient and not the appointment, and the released
  slot could never be booked by anyone again.

With the fourth column: a live row is `slot_lock="ACTIVE"`, so at most one live
row holds a given doctor/date/slot — *exactly the old guarantee*. A cancelled row
sets `slot_lock` to its own `confirmation_id`, which is unique by its own
constraint, so any number of cancelled rows pile up on one slot without ever
colliding.

Rescheduling needs no tombstone: the row itself moves and stays `ACTIVE`, which
frees the old slot as a side effect of the `UPDATE`.

**The consequence to remember:** every *"is this slot taken"* query must filter
`slot_lock == SLOT_LOCK_ACTIVE`. All four now go through one helper,
`_live_slots()`, rather than repeating the filter — forgetting it in one place is
a bug that presents as *"the slot I cancelled can never be rebooked"*, and only
for the callers unlucky enough to want that slot.

### 5.2 `clinic-api/main.py` — the three repeated slot queries

**Old** — the same query inlined three times, none of them aware of cancellation:

```python
    def _free_slots() -> list[str]:
        taken = {
            a.time_slot for a in db.query(Appointment).filter_by(
                doctor_id=doctor.id, date=req.date,
            ).all()
        }
        return [s for s in valid_slots if s not in taken][:3]

    if req.time_slot in {
        a.time_slot for a in db.query(Appointment).filter_by(
            doctor_id=doctor.id, date=req.date,
        ).all()
    }:
        return {"success": False, "reason": "slot_taken",
                "alternative_slots": _free_slots()}
```

**New**

```python
def _live_slots(db: Session, doctor_id: int, date: str) -> set[str]:
    return {
        a.time_slot for a in db.query(Appointment).filter_by(
            doctor_id=doctor_id, date=date, slot_lock=SLOT_LOCK_ACTIVE,
        ).all()
    }

    # ... at the call sites:
    def _free_slots() -> list[str]:
        taken = _live_slots(db, doctor.id, req.date)
        return [s for s in valid_slots if s not in taken][:3]

    if req.time_slot in _live_slots(db, doctor.id, req.date):
        return {"success": False, "reason": "slot_taken",
                "alternative_slots": _free_slots()}
```

The third copy, inside the `IntegrityError` handler, was replaced the same way.

### 5.3 `clinic-api/main.py` — the booking response

**Old**

```python
@app.post("/api/v1/appointments")
def book_appointment(req: BookingRequest, db: Session = Depends(get_db)):
    ...
    return {
        "success": True, "confirmation_id": confirmation_id,
        "doctor_name": doctor.name,
        "doctor_name_bn": _first_alias_bn(doctor.aliases_bn),
        "date": req.date, "time_slot": req.time_slot,
    }
```

**New**

```python
@app.post("/api/v1/appointments")
def book_appointment(req: BookingRequest, background: BackgroundTasks,
                     db: Session = Depends(get_db)):
    ...
    appt = Appointment(
        ...,                                   # unchanged fields
        status=APPT_BOOKED, slot_lock=SLOT_LOCK_ACTIVE,
    )

    # staged BEFORE the commit, so one transaction carries the booking and
    # the record that a message is owed for it
    attempt = _prepare_notification(db, appt, mt.EVENT_BOOKED, doctor.name)
    try:
        db.add(appt)
        db.commit()                            # persists BOTH
    except IntegrityError:
        db.rollback()                          # discards BOTH
        ...
    notification = _schedule_delivery(background, attempt, mt.EVENT_BOOKED)

    return {
        "success": True, "confirmation_id": confirmation_id,
        "doctor_name": doctor.name,
        "doctor_name_bn": _first_alias_bn(doctor.aliases_bn),
        "date": req.date, "time_slot": req.time_slot,
        "notification": notification,          # ← new
    }
```

The pair is shared by all three events — staged before the commit, scheduled
after it:

```python
def _prepare_notification(db, appt, event, doctor_name) -> NotificationAttempt | None:
    """MUST be called BEFORE the commit that persists the appointment."""
    try:
        return notify_service.queue_message(db, appt, event, doctor_name)   # db.add, no commit
    except Exception:
        logging.getLogger("clinic-api").exception(
            "could not stage a %s notification for %s (patient %s on %s) -- "
            "the appointment itself stands",
            event, appt.confirmation_id, appt.patient_name, appt.phone)
        return None


def _schedule_delivery(background, attempt, event) -> dict:
    """Called AFTER the commit, so the row the task re-reads definitely exists."""
    if attempt is None:
        return {"status": "not_recorded", "event": event}
    if attempt.status == notify.STATUS_QUEUED:
        background.add_task(notify_service.deliver_now, attempt.id)
    return {"id": attempt.id, "event": event,
            "status": attempt.status, "error_code": attempt.error_code}
```

### 5.4 `agent/reply_templates.py` — the spoken reply

**Old**

```python
def booking_reply(slots: dict, result: dict) -> str:
    if result.get("success"):
        return (f"আপনার অ্যাপয়েন্টমেন্ট কনফার্ম হয়েছে। "
                f"{_spoken_doctor_name(slots, result)}, {result['date']}, "
                f"সময় {result['time_slot']}। "
                f"কনফার্মেশন নম্বর: {result['confirmation_id']}।")
```

**New**

```python
def _written_confirmation_clause(result: dict) -> str:
    status = (result.get("notification") or {}).get("status")
    if status == "queued":
        return " কনফার্মেশনের একটা মেসেজ আপনার ফোনে পাঠানো হচ্ছে, রিসেপশনে ওটা দেখালেই হবে।"
    return ""


def booking_reply(slots: dict, result: dict) -> str:
    if result.get("success"):
        return (f"আপনার অ্যাপয়েন্টমেন্ট কনফার্ম হয়েছে। "
                f"{_spoken_doctor_name(slots, result)}, {result['date']}, "
                f"সময় {result['time_slot']}। "
                f"কনফার্মেশন নম্বর: {result['confirmation_id']}।"
                f"{_written_confirmation_clause(result)}")
```

**The rule this encodes:** the caller is promised a message **only when one is
actually queued**. `skipped` (no gateway on this pod) and `failed` (rendering or
registration broken) both mean nothing will arrive, and promising an SMS in those
cases is *worse than saying nothing* — the caller stops noting the number down,
hangs up satisfied, and finds out at the reception desk. Silence leaves them
exactly where they were before this feature existed.

**The spoken number is still read out even when a message is going.** It costs
one sentence of TTS and it is the only thing the caller has if the SMS is delayed
or the handset is off. The *promise* is conditional; the number is not.

---

## 6. New endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/appointments/{confirmation_id}/reschedule` | Move an appointment, **keeping its confirmation_id**. |
| `POST` | `/api/v1/appointments/{confirmation_id}/cancel` | Cancel, release the slot, message the patient. |
| `POST` | `/api/v1/notifications/receipt` | The gateway's delivery-receipt callback. |
| `GET` | `/api/v1/notifications/failures` | **The staff queue.** |
| `GET` | `/api/v1/appointments/{confirmation_id}/notifications` | Every message ever owed for one appointment. |
| `POST` | `/api/v1/notifications/{id}/acknowledge` | Staff take a failure on. |
| `POST` | `/api/v1/notifications/{id}/retry` | Re-send the stored body. |

Three details worth knowing:

- **The confirmation_id is never reissued on reschedule.** The patient's whole
  complaint in the story is having to track a reference number; handing them a
  second one every time the clinic moves their slot makes it worse. The
  reschedule template says *"রেফারেন্স {#var#} একই থাকছে"* so the earlier message
  they may still be holding does not become misleading.
- **Cancelling twice is a success and sends no second message.** A gateway retry,
  a double-tap in a staff UI and a patient ringing twice all reach that endpoint;
  none is a reason to message someone about a cancellation they were already told
  about.
- **The receipt endpoint always answers `200`**, even for an unmatched receipt.
  Gateways retry a receipt that gets a non-2xx, often forever. A receipt we
  cannot match is our problem to see in the log, not a reason to make theirs a
  loop.

---

## 7. Telecom compliance (TRAI / DLT)

Under TCCCPR a transactional SMS is delivered only if the Principal Entity ID,
the Header (sender ID) and a registered Content Template ID all line up, and the
body matches the approved template apart from its declared variables. A message
assembled from free text is rejected **by the operator, silently from the
patient's point of view**.

What the code does about it:

| Rule | Where it is enforced |
|---|---|
| Bodies live in one file, in registered shape, and nothing else may build a message string | `message_templates.py` |
| `{#var#}` marker count must equal the declared variable list | `_validate_registry()`, **at import** — the service refuses to start otherwise |
| No variable over 30 chars (DLT limit) | `render()` raises `TemplateError`; `_variables()` clamps the patient name so it cannot get there |
| Every variable present and non-empty | `render()` |
| Unregistered template ID is never sent in live mode | `notifications.preflight()`, at queue time **and again in `deliver_now()`** |
| Missing sender ID / entity ID never sent | `notifications.preflight()` |
| A retry submits the template ID the body was composed under | `deliver_now()` uses `attempt.template_id` |
| Bengali sent as UCS-2, never transcoded to GSM-7 | the `unicode` flag in `send()` |

That last one matters more than it looks: a gateway told the message is GSM-7
delivers a message of question marks **and reports it as a successful delivery**
— the worst possible failure for something a patient has to show at a counter.

This is the same discipline `reply_templates.py` already applies to the spoken
reply, extended to the written one. **There is no path by which a model-authored
sentence reaches a patient's handset.**

Template IDs are configuration, not code. Committing a real 19-digit ID would tie
the source tree to one hospital's registration; committing a fake one would look
registered while guaranteeing rejection. They default to empty, and empty means
"not registered yet".

---

## 8. Delivery receipts and the staff queue

The ledger (`notification_attempts`) is written **before** anything is sent. The
rendered `body` is stored rather than re-derived: reception needs the exact text
the patient was or was not sent, and re-rendering a two-week-old failure through
today's template would show staff a message that was never composed.

### Statuses

| Status | Meaning |
|---|---|
| `queued` | Row written, nothing sent yet. Also what a row is left as if the process dies mid-send. |
| `sent` | The gateway accepted it. **Not** proof the handset got it. |
| `delivered` | A receipt confirmed handset delivery. |
| `failed` | Gateway rejected it, the network failed, or a receipt reported non-delivery. |
| `skipped` | Deliberately not sent, reason recorded. Only reachable with no gateway configured. |

### Three things count as a failure

```
status == failed                                    — said so outright
status == queued  and older than stale_minutes      — the task never ran
status == sent    and older than stale_minutes      — accepted, never delivered
```

**The third is the one this endpoint exists for.** Nothing errored, no log line
was written, and without that rule the patient simply turns up at reception with
nothing.

Two smaller rules that came out of how operators actually behave:

- **An unrecognised receipt status is treated as a failure, not a success.**
  Reading an unmapped vendor code as "delivered" would hide exactly the messages
  nobody has ever seen before.
- **A receipt never reopens a settled row.** Operators re-send receipts out of
  order; a stale `enroute` arriving after `delivered` must not push a delivered
  message back into the queue.

Acknowledging a failure **leaves `status` alone**. The message still failed, and
rewriting that to make a queue look tidy would falsify the record the table
exists to be.

`/api/health` now carries a `notifications` block, so whatever already polls it
(`deploy/status.sh`) sees the backlog for free.

---

## 9. Variables — every one added

All new configuration is in `deploy/env.sh` and `deploy/env.vast.sh`.

| Variable | Default | Purpose |
|---|---|---|
| `HOSPITAL_GATEWAY_URL` | *(empty)* | Gateway endpoint. **Empty is a supported state** — see below. |
| `HOSPITAL_GATEWAY_API_KEY` | *(unset)* | **Secret — not in any committed file.** |
| `HOSPITAL_GATEWAY_AUTH_HEADER` | `Authorization` | Header the key goes in. `Bearer ` is prepended for `Authorization`. |
| `HOSPITAL_GATEWAY_SENDER_ID` | *(empty)* | DLT header, registered, 6 chars. |
| `HOSPITAL_GATEWAY_ENTITY_ID` | *(empty)* | DLT Principal Entity ID. |
| `HOSPITAL_GATEWAY_TEMPLATE_BOOKED` | *(empty)* | Registered content template ID. |
| `HOSPITAL_GATEWAY_TEMPLATE_RESCHEDULED` | *(empty)* | " |
| `HOSPITAL_GATEWAY_TEMPLATE_CANCELLED` | *(empty)* | " |
| `HOSPITAL_GATEWAY_COUNTRY_CODE` | `91` | Put back on the 10 digits `parse_phone()` stored. |
| `HOSPITAL_GATEWAY_TIMEOUT_S` | `8` | Safe above the agent's 4s **because the send is a background task**. |
| `HOSPITAL_GATEWAY_STALE_MINUTES` | `15` | How long `sent` may go without a receipt before it is a failure. |
| `HOSPITAL_GATEWAY_DLR_TOKEN` | *(unset)* | **Secret.** Shared secret for the receipt callback. |

**The two secrets are deliberately absent from both env files**, which are
committed, read by every service, and routinely pasted into bug reports. Export
them from an uncommitted `deploy/env.secret.sh` sourced after the main one. These
are the first credentials this repository has ever needed.

**Unset `HOSPITAL_GATEWAY_URL` is correct behaviour, not a broken state.** Every
message is recorded as `skipped` with the reason attached, `reply_templates.py`
stops promising callers an SMS, and the staff queue stays empty (a queue that is
always full is a queue nobody reads). A bench pod behaves correctly and messages
nobody. Covered by three end-to-end tests.

### To actually go live

1. Register the Principal Entity ID and the 6-character Header on the operator's
   DLT portal.
2. Register the three bodies in `clinic-api/message_templates.py`, **verbatim**,
   as transactional templates; the portal returns a 19-digit ID for each.
3. Set those three IDs plus `HOSPITAL_GATEWAY_SENDER_ID` and
   `HOSPITAL_GATEWAY_ENTITY_ID`.
4. Set `HOSPITAL_GATEWAY_URL`, `HOSPITAL_GATEWAY_API_KEY` and
   `HOSPITAL_GATEWAY_DLR_TOKEN`, and point the gateway's receipt callback at
   `POST /api/v1/notifications/receipt` with that token in `X-Gateway-Token`.

Until step 3 is done, `preflight()` refuses to send in live mode rather than
paying for an operator rejection.

### Receipt authentication is conditional, on purpose

A deployment with a configured gateway **must** set `HOSPITAL_GATEWAY_DLR_TOKEN`
and receipts are rejected without it — anything on the network could otherwise
mark a patient's message delivered. A bench pod with no gateway has no token to
check and accepts receipts so the path can be tested.

---

## 10. The migration — read this before running anything

`Base.metadata.create_all()` creates tables that do not exist; **it does not
alter ones that do.** An existing `clinic.db` will come up with the old
three-column constraint and without `status` / `slot_lock` / `updated_at`. And
SQLite cannot add or drop a table-level `UNIQUE` with `ALTER` at all — it is
stored as an internal auto-index. So this is a **table rebuild**, not a column
addition.

**It is not run automatically.** This service's own startup already refuses to
reseed a non-empty database because an unconditional destructive step at boot
*"would wipe every appointment booked since the last boot, turning a convenience
into data loss"*. A silent table rebuild is that same hazard with a wider blast
radius.

Instead:

- **Startup detects the mismatch**, logs it at `error`, and exposes it as
  `schema_warning` on `/api/health`.
- **The rebuild is a script you run.**

```bash
cd clinic-api && python migrate_notifications.py --dry-run
```

```bash
cd clinic-api && python migrate_notifications.py
```

It takes a timestamped backup of the SQLite file first, does the standard
create-copy-drop-rename inside **one transaction** (an interruption leaves the
original table intact), and verifies the row count afterwards.

Every existing appointment becomes `status="booked"`, `slot_lock="ACTIVE"`,
`updated_at=NULL` — correct by construction, since before this change there was
no way to cancel one.

**No notification rows are backfilled.** Appointments booked before this change
were genuinely never messaged, and inventing ledger rows saying otherwise would
put a lie in the delivery record.

Verified on a synthetic old-schema database: dry-run reports correctly, the
migration rebuilds with the four-column constraint, the row survives, and a
second run reports *"Already migrated. Nothing to do."*

---

## 11. Tests

```bash
python -m pytest tests/ -q
```

**224 passed** — 169 pre-existing, 55 new. No existing test was modified.

The new file covers, end-to-end through the real endpoints with the gateway faked
at `notifications.send()` (the one function that touches the network):

- a message is queued on **booking, reschedule and cancellation**;
- the DLT rules — variable count, 30-char limit, unregistered template, missing
  sender/entity ID — are enforced *here*, not discovered at the operator;
- a marker inside a patient's name cannot shift the remaining variable positions
  (an ASR-supplied name is not trusted);
- templates stay within their costed UCS-2 segment count;
- receipts are recorded, matched by provider ID **and** by `client_ref` when the
  provider ID was lost, and a late duplicate cannot reopen a delivered row;
- an unrecognised receipt status is a failure, not a success;
- rejected messages reach the staff queue with their body attached;
- **an accepted message with no receipt becomes a failure once stale** — the
  silent case;
- acknowledging clears the queue without rewriting the status; retry re-sends the
  stored body;
- **cancelling frees the slot for somebody else** — the `slot_lock` rule, end to
  end;
- cancelling twice sends no second message;
- with no gateway configured: the booking still succeeds, the row says `skipped`
  with the reason, the staff queue stays empty, a staff retry still sends
  nothing, and the caller is not promised a message;
- the three regression tests in §13.

### A pre-existing flaky test, found while verifying — not mine

`tests/test_audio_quality.py::test_a_good_turn_between_two_bad_ones_prevents_the_keypad`
fails roughly **3 runs in 15**, in isolation, with no involvement from this
work. Measured both ways to be sure:

```
with my changes    : 3 failures / 15 isolated runs
my two agent files reverted to HEAD : 3 failures / 15 isolated runs
```

That file imports only `agent.audio_quality`, `agent.quality_metrics` and
`main_pcm`; the only modules I touched in that graph are `reply_templates.py`
(whose new clause is a no-op when a result carries no `notification` key) and
`tools_client.py`. The assertion that flakes is
`app.METRICS.snapshot()["keypad_offers"] == 0` — a module-level metrics
singleton — which suggests shared state between tests rather than the DSP. **I
have not fixed it**: it is unrelated to this story and touching the audio
metrics to chase it would put unreviewable noise in this diff. Flagging it so it
is not mistaken for a regression from this branch.

One test-harness note worth keeping: a plain `import main` is a trap in this repo
— there are **two** `main.py` files, and every other test module inserts the repo
root at `sys.path[0]` when collected. Running the new file alone resolved to
clinic-api's; running the whole suite resolved to the voice agent's, which
imports `agent/asr.py` and fails on a gated model checkpoint. The fixture loads
it by path under an unambiguous module name instead.

---

## 12. Deliberately out of scope

**The voice dialogue for reschedule and cancellation.** The agent cannot yet be
*asked* by phone to move or cancel an appointment — that needs new intents in
`agent/llm.py`, new pending states in both `main.py` and `main_pcm.py`, and a way
for a caller to speak a confirmation ID back. That is a different story ("as a
patient I want to cancel by phone"), and it is large.

What this change does is make the **events** real: the endpoints exist, the
agent's client can call them (`reschedule_appointment()`, `cancel_appointment()`),
the spoken replies are written (`reschedule_reply()`, `cancel_reply()`), and
**all three events send the patient a message**. Staff tooling and any other
caller of the API get the behaviour the criterion specifies today.

---

## 13. The three defects the audit found

Each was found by re-reading the criteria against the code after the work looked
finished. Each has a regression test **confirmed to fail against the pre-fix
code**.

### Defect 1 — a long patient name silently cost the patient their message

`_clean_patient_name()` hands over whatever the caller said. A perfectly ordinary
Bengali name — `রিয়া দাস চ্যাটার্জী মুখোপাধ্যায়` — is **33 characters**, over
DLT's 30-character variable ceiling. `render()` raised, the row was recorded
`failed`, and the patient got nothing *because their name was long*.

**Fix:** clamp the patient name to `VAR_MAX_CHARS` in `_variables()`. `render()`
stays strict — it is the compliance gate — but the layer that feeds it now
supplies compliant values. A trimmed name on an SMS beats no SMS.

**Test:** `test_a_long_patient_name_still_gets_a_message`
(pre-fix: `assert 'failed' == 'queued'`).

### Defect 2 — a retry could submit a body under the wrong template ID

`deliver_now()` re-sent the **stored body** (correct) paired with the template ID
read from the **live registry** (wrong). After the hospital re-registers its
templates, a staff retry of an older row pairs old text with a new ID — exactly
the mismatch the operator rejects, silently, from the patient's side. It also
contradicted `retry`'s own docstring.

**Fix:** submit `attempt.template_id`, falling back to the registry only when
nothing was recorded (which is what lets a retry succeed after IDs are finally
configured on a pod that had none). Plus a **preflight re-check inside
`deliver_now()`**, because `requeue()` will put a `skipped` row back to `queued`
and without it a staff retry on an unconfigured pod would hand an unregistered
template straight to the gateway.

**Tests:** `test_a_retry_submits_the_template_id_the_message_was_composed_under`,
`test_retrying_a_skipped_row_on_an_unconfigured_pod_still_sends_nothing`.

### Defect 3 — the code did not match its own documented transaction boundary

An earlier draft of these notes claimed the ledger row was committed *in the same
transaction* as the appointment. **It was not.** The appointment committed, then a
second, separate commit wrote the ledger row. If that second commit failed —
`database is locked` is entirely reachable, `db.py` sets `busy_timeout` to 3s
precisely because contention is expected — the booking stood with **no ledger row
at all**: nothing owed on record, nothing in the staff queue, patient told
nothing. That is the exact case the criterion forbids.

Measured both ways, with the commit forced to fail on its second call:

```
separate commits : booking success=True, ledger rows = 0   ← silently dropped
one transaction  : booking success=True, ledger rows = 1
```

**Fix:** `_announce()` was split into `_prepare_notification()` (staged **before**
the commit, in the caller's session) and `_schedule_delivery()` (after it). One
commit now carries the appointment change and the record that a message is owed.
§4.1 describes what the code actually does.

**Test:** `test_a_failing_second_commit_cannot_lose_the_ledger_row`
(pre-fix: *"a committed booking was left with nothing owed on record"*).

> A fourth test, `test_a_booking_that_loses_the_slot_race_leaves_no_orphan_ledger_row`,
> pins the other half of that invariant. It is labelled in the file as an
> invariant test rather than a regression: it passes against the old ordering
> too, because the early return on `IntegrityError` got there first.

---

## 14. Known gaps — flagged, not hidden

1. **No template is registered yet.** The three `HOSPITAL_GATEWAY_TEMPLATE_*` IDs
   are empty, so live sending is refused by `preflight()`. Registration is a
   portal task, not a code task — see §9's go-live checklist.
2. **"Surfaced to staff" is an API, not a screen.**
   `GET /api/v1/notifications/failures`, a counter on `/api/health`, and
   `WARNING`/`ERROR` log lines. There is no UI in this repository to put a
   dashboard in, and I have not built one. If the story owner means a
   human-readable screen, that is remaining work.
3. **The phone number is never read back to the caller.** `booking_reply()`
   doesn't echo it and there's no confirmation step. A single ASR digit error
   silently books against an unreachable number. That cost nothing before,
   because nothing was sent. It is now the most likely reason a patient gets no
   message, and the fix is a read-back turn in the booking flow.
4. **PII retention.** `notification_attempts` holds patient name and phone, in a
   column and again inside `body`. So does `appointments`. No retention or purge
   policy is implemented.
5. **The gateway JSON contract is assumed, not verified.** It is documented in
   `notifications.send()`'s docstring the same way `tools_client.py` documents the
   clinic-api contract. Adapting to the hospital's real gateway is a change to
   that one function.
6. **On vast.ai, receipts cannot arrive.** The callback lives on clinic-api, which
   is on an internal-only port, and vast.ai fixes published ports at instance
   creation. Every message will stick at `sent` and go stale. That is the staff
   queue correctly reporting a real gap — noted in `env.vast.sh`, not suppressed.
7. **`deploy/status.sh` truncates health output at 120 characters**, so the new
   `notifications` block is cut off in its one-line display. Nothing breaks; the
   full body is still on `/api/health`.
8. **Rescheduling to the slot it already has** succeeds and sends a "your
   appointment has been moved" message reporting no change. Harmless but silly.
   Left alone rather than adding an unrequested equality check, since staff
   tooling may legitimately re-confirm a slot.
9. **Nothing has run against a real gateway or a real handset.** Every test fakes
   that boundary. The Bengali wording has not been reviewed by a speaker.
