# History disclosed only after verification — implementation notes

**Author:** Chakravardhan

**Story**

> History disclosed only after verification

**Acceptance criteria**

> As a patient, I want my history spoken only to me, so that whoever else uses
> this handset cannot hear what tests I have had.

Branch `dev_chakravardhan`. **Committed locally, not pushed.** Suite: **350
passed** (294 pre-existing, 56 new).

---

## 1. What I found first

| Looked for | Found |
|---|---|
| Any history / past-tests flow | **none** |
| Any verification, auth, OTP, DOB or identity check | **none** |
| A record of tests a patient actually took | **none** — `appointments` are doctor visits |
| A `Patient` entity | **none** — only a `phone` column on `Appointment` |

So there was nothing to protect **and** no way to protect it. Two things had
to be built: the history itself, and the gate in front of it.

**The one thing already there that turned out to matter:** `agent/echo_guard.py`
classifies the audio path by Echo Return Loss — handset versus speakerphone.
That exists for barge-in, and it is the only signal in this system that says
anything about the room the caller is standing in. It became the "cannot
**hear**" half of the criterion.

---

## 2. The threat, stated precisely

**The handset is shared.** A family phone, a shop phone, a borrowed phone. The
attacker is not a hacker — it is an ordinary person holding a phone that is not
theirs, who dials the clinic and is treated as its owner.

Every decision below follows from that one sentence. Two consequences are
worth stating up front because both are counter-intuitive:

### The phone number is not a credential

It says which record to **look at**. It says nothing about who is holding the
phone. Treating "called from Meera's number" as "is Meera" is precisely the bug.

### An SMS OTP does not work here

This is the reflexive answer, and it fails this exact threat model. **The code
is delivered to the shared handset the caller is already holding.** It proves
possession of a phone that, by the story's own premise, proves nothing.
Whoever borrowed it reads the code off the screen and is now "verified" — with
a *stronger* claim than before, because the system now believes it checked
something.

So verification here is **knowledge-based**, and specifically knowledge that is
not sitting in that handset's message inbox.

### What is deliberately *not* accepted as proof

| Rejected factor | Why |
|---|---|
| `confirmation_id` | SMSed to this handset. Readable by whoever holds it |
| Patient name | A household member knows it, and it is spoken aloud during booking |
| Appointment date | Same objection, and it is in the SMS |

Accepting any of those would re-open the hole while *looking* like security.

---

## 3. Verdict against the acceptance criteria

| Clause | State |
|---|---|
| History disclosed **only after verification** | ✅ Nothing is fetched until a token exists |
| **"spoken only to me"** | ✅ Knowledge factor, not the phone number |
| **"whoever else uses this handset"** | ✅ Per-patient lockout, per-call tokens, no OTP |
| **"cannot hear"** | ✅ Speakerphone and unclassified paths block disclosure |

---

## 4. Old code → new code

### 4.1 `clinic-api/models.py` — three new tables

**Old:** no patient entity at all. `Appointment` carried a `phone` string.

**New:**

```python
class Patient(Base):
    phone = Column(String, nullable=False, unique=True, index=True)
    full_name = Column(String, nullable=False)
    date_of_birth = Column(String, nullable=True)      # weaker factor
    pin_hash = Column(String, nullable=True)           # set AT THE COUNTER
    pin_salt = Column(String, nullable=True)
    failed_attempts = Column(Integer, nullable=False, default=0)
    locked_until = Column(DateTime, nullable=True)
```

Note what is **absent**: no OTP column, no secret in plaintext.

```python
class TestRecord(Base):     # the thing the story protects — did not exist
class DisclosureAudit(Base) # every attempt, successful or not
```

`TestRecord` is deliberately thin — test name, date, whether the report is
ready. **No results, no values, no diagnoses.** A voice line that reads
clinical numbers aloud is a bigger disclosure surface than this story asks
anyone to build.

**No migration needed.** All three are new tables, so `create_all()` makes
them. Unlike the SMS story, nothing existing changes shape.

### 4.2 `clinic-api/verification.py` — new, the policy

ORM-free on purpose, so it can be reasoned about and tested with no database.

The core is one pure function, and its **ordering is the design**:

```python
def evaluate(*, patient_exists, locked, factor, matched, attempts_before):
    if locked:                     # 1. before the answer is even considered
        return Decision(REPLY_LOCKED, OUTCOME_LOCKED, ...)
    if not patient_exists:         # 2. same REPLY as a wrong answer
        return Decision(REPLY_FAILED, OUTCOME_NO_PATIENT, ...)
    if factor == FACTOR_NONE:      # 3. also the same reply
        return Decision(REPLY_FAILED, OUTCOME_NO_FACTOR, ...)
    if matched:
        return Decision(REPLY_VERIFIED, OUTCOME_VERIFIED, verified=True, ...)
    return Decision(REPLY_FAILED, OUTCOME_WRONG,
                    lock_now=(attempts_before + 1) >= MAX_ATTEMPTS, ...)
```

**`reply` and `outcome` are separate fields at deliberately different
resolutions.** The audit trail knows *why*; the caller learns only what they
can act on.

### 4.3 `agent/privacy.py` — new, the "cannot hear" half

**The one place this codebase deliberately disagrees with itself.**

`EchoGuard.reporting_path()` collapses `PATH_UNKNOWN` → `PATH_HANDSET`, and its
docstring says why: guessing speakerphone would contaminate the metrics bucket
whose accuracy is being measured.

That is right for metrics and **wrong here, in the most consequential
direction available**:

```python
path = echo_guard.classify()          # NOT reporting_path()
if path == PATH_SPEAKERPHONE: return False, UNSAFE_SPEAKERPHONE
if path == PATH_HANDSET:      return True,  SAFE
return False, UNSAFE_UNKNOWN          # not proven safe
```

"We have not gathered enough echo observations to tell" is not "this is a
handset held to an ear". Treating unknown as safe would disclose a medical
history on every call that ends before the classifier has enough data — which
is precisely the **short** calls, and "dial, ask for my history, hang up" is
exactly that shape.

Cost of being wrong this way: one extra sentence asking the caller to take the
phone off speaker. Cost of being wrong the other way: reading somebody's
medical history to a room.

### 4.4 `main.py` / `main_pcm.py` — the flow

```python
elif intent == "patient_history":
    # NOTHING IS FETCHED UNTIL VERIFICATION PASSES. The lookup is not
    # performed and then withheld -- it is not performed at all, so there
    # is nothing in this process to leak.
    if session.history_token:
        await _speak_history(session)
    else:
        await _start_history_verification(session, slots.get("phone"))
```

Three things worth noting:

- **The room is checked twice** — once before the challenge, and again
  immediately before speaking. A caller can put the phone on speaker between
  answering and hearing the answer, and that is the exact moment the private
  information is about to be spoken.
- **Verification is checked before the negative escape hatch** in
  `_continue_pending`. A wrong answer must burn an attempt, not be
  reinterpreted as a polite "না".
- **`cleanup()` drops the token.** The handset is shared; a token that
  outlived the conversation would be the hole this story closes.

### 4.5 `agent/tools_client.py` — note what is *absent*

Four methods added. **`set_pin()` is deliberately not one of them.** The PIN
endpoint exists on clinic-api for counter staff. A PIN settable from the voice
line is not a second factor — it is a button labelled "make me verified",
pressable by whoever holds the shared handset. The capability is withheld at
the client so no future turn-loop change can reach it by accident. A test
asserts the method does not exist.

---

## 5. The silence rules

Two behaviours exist purely to avoid leaking through the **failure** path,
which is how most verification systems betray the thing they protect.

### No enumeration

An unknown number is challenged **exactly** like a known one, and fails exactly
like a wrong answer.

Otherwise the line becomes a lookup service for *"does this person attend this
clinic"* — which is itself medical information about them, disclosed with no
verification at all.

There is also a dummy PBKDF2 hash on the no-patient path, so an unknown number
does not return visibly faster than a wrong PIN.

### No hints

The caller is never told which factor was expected beyond the question itself,
how many attempts remain, how long a lockout lasts, or what the right answer
looked like. Tests assert no reply contains a digit or a countdown.

**The wording is part of the security**, so it lives under the same review as
the code: one sentence covers a wrong PIN, an unknown number **and** a patient
with no factor on file.

---

## 6. Variables added

| Variable | Default | Purpose |
|---|---|---|
| `VOICE_AGENT_HISTORY_DISCLOSURE` | `1` | Master switch. `0` = never read history over the phone |
| `VOICE_AGENT_HISTORY_REQUIRE_PRIVATE_PATH` | `1` | Refuse on speakerphone / unknown path. **`0` only on a bench pod** |
| `VOICE_AGENT_VERIFY_MAX_ATTEMPTS` | `3` | Wrong answers before the number locks |
| `VOICE_AGENT_VERIFY_LOCKOUT_MINUTES` | `30` | How long it stays locked |
| `VOICE_AGENT_VERIFY_TTL_MINUTES` | `10` | Token life, within one call only |
| `VOICE_AGENT_VERIFY_PBKDF2_ROUNDS` | `120000` | PIN hashing cost |

**No secret is added to any env file.** The only secrets in this feature are
patients' PINs, and those live hashed in the database.

The bench override is named after what it weakens, and the audit row records
which mode a disclosure happened under — so it is visible after the fact, not
only in someone's memory of how the pod was configured.

---

## 7. New endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/history/verify/begin` | Which proof to ask for |
| `POST` | `/api/v1/history/verify` | One attempt |
| `POST` | `/api/v1/history/read` | The history itself |
| `POST` | `/api/v1/history/refusal` | Record an agent-side refusal |
| `POST` | `/api/v1/patients/pin` | **Counter staff only** |
| `GET` | `/api/v1/history/audit` | The trail, for staff |

**Every one is a POST, including the read.** A token in a query string lands in
the access log, the proxy log and anything scraping either — and a token is the
one value here that grants access on its own. Bodies are not logged; URLs are.

---

## 8. Tests — 56 new

**The policy, with no database:** PIN hashing and salting, normalisation of
spoken PINs and dates, factor preference, lockout arithmetic, token expiry.

**The enumeration rule** — the most important test in the file: an unknown
number and a wrong answer produce an identical caller-visible reply, while the
audit trail still tells them apart.

**The audio path:** speakerphone blocks; handset allows; **unknown blocks** —
the deliberate inversion of `reporting_path()`; the master switch is not
recoverable; the bench override works and is off by default.

**End to end through the real endpoints:** no token means no history; an
invented token opens nothing; a correct PIN discloses; a wrong one does not;
**the confirmation_id is rejected as proof**; three wrong answers lock the
number; **redialling does not refill the budget**; a revoked token stops
working; every attempt is audited; **the audit never stores the secret**; a
refused disclosure is audited; `set_pin` is absent from the client.

**The wording:** every failure sounds identical; no reply reveals attempts
remaining; the locked reply does not say for how long; the speakerphone reply
names the *fix* rather than a failure.

**Also inherited free:** `tests/test_no_smartphone.py` walks every string in
`agent/i18n.py`, so the 12 new history strings were checked for links, QR codes
and app references the moment they were added.

One test caught a false positive in **its own fixture** — `"0000"` is a
substring of the test phone number `9876500001`, so the "audit never stores the
secret" assertion failed on the data, not the code. Fixed in the test.

---

## 9. Known gaps — flagged, not hidden

1. **No patient records exist yet.** `Patient` and `TestRecord` are new and
   empty. Nothing populates them — booking still writes only `Appointment`.
   Wiring booking to create a `Patient`, and lab results to create
   `TestRecord`s, is the obvious next piece and is **not** in this change.
2. **No counter workflow for setting PINs.** The endpoint exists; nothing calls
   it. Staff need a screen or a script, and a policy on identity checks at the
   desk before a PIN is set.
3. **ERL thresholds are unvalidated.** `echo_guard.py`'s own docstring says so.
   These tests assert the decision is wired correctly *given* a classification,
   not that the classification is right in a real Kolkata room.
4. **ERL cannot detect everything.** A second person leaning in at an earpiece,
   a car speaker that returns little echo, a recorded call. This raises the
   floor; it does not seal the room.
5. **Date of birth is a weak factor** within a household. It stops a stranger
   or a shopkeeper, not a family member. The PIN is the real answer, and it
   requires the counter workflow in gap 2.
6. **Timing equalisation is asserted by construction, not measured.** The dummy
   hash is there; proving it needs a controlled environment.
7. **Nothing has run on a GPU** — no real ASR of a spoken PIN, no real
   speakerphone, no real call.
8. **Hindi and English wording is unreviewed**, as with the previous story.
