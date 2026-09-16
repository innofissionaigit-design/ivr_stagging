# The Phone and the Message Channel Agree — Old vs New Code and Variables

**Author:** Chakravardhan
**Story:** *As a patient, I want the phone and the message channel to agree, so that I know which one to believe.*
**Acceptance criteria:** A shared answer service backs every channel, and a regression suite asks the same question set on each, asserting equivalent answers. A divergence fails the build.
**Branch:** `dev_chakravardhan`. **Old** = the working tree after the message-channel story (on top of commit `932dbb2`). **New** = the working tree now.
**Status:** implemented and tested **locally — not committed, not pushed.**

---

## 0. What the story means

| Part of the story | In plain words | What it forces in the code |
|---|---|---|
| "the phone and the message channel agree" | The same question gets the same answer, whichever way it is asked. | One answering path, not two that resemble each other. |
| "so that I know which one to believe" | A patient must never have to judge which channel is right. | Equality of **words**, not merely of facts. |
| "a shared answer service backs every channel" | One door into the answering logic. | A named entry point every channel goes through. |
| "a regression suite asks the same question set on each" | Prove it, question by question, every build. | A question set, run twice, compared. |
| "a divergence fails the build" | Drift is caught by the build, not by a patient. | An undeclared `if this is a message` **breaks the tests**. |

**The risk this guards against.** The two channels already shared a pipeline — but a shared pipeline can be forked one `if` at a time. Each fork is defensible alone; three forks later a patient is quoted one price on the phone and another by message, and the clinic finds out from a complaint.

---

## 1. Summary

| | Old | New |
|---|---|---|
| How a message is answered | drives the voice turn loop (`run_text_turn` → `_dispatch_turn`) | the same, through a **named** shared entry, `answer_turn()` |
| How a keypad press is answered | `_dispatch_turn` directly | `answer_turn()` — the same door |
| Where channel differences are recorded | nowhere — you had to read `main.py` and find them | **`agent/answer_contract.py`**, each with a reason |
| Proof the channels agree | none | **19 tests**: the same 8 questions on both channels, compared word for word |
| A new channel branch | passed silently | **fails the build** until it is declared |
| A channel-only sentence | any `channel.*` key could appear | the 3 declared keys, and no others |

**Nothing about how answers are produced changed.** No new intent, no new template, no new endpoint, no new dependency, no clinic-api change.

---

## 2. What I found first

| Looked for | Found |
|---|---|
| A shared answer path | **Already there** — a message enters `_run_turn`, the phone line's own turn. The story's first clause was satisfied but unnamed. |
| A proof the answers match | **None.** The message tests asserted one reply matched the phone line; nothing covered the answer set. |
| Places that branch on the channel | **Exactly 3** in `main.py` (`_speak`, `_history_guard`, `_fill_from_channel`) — all deliberate, none written down. |
| Sentences belonging to a channel | 3 `channel.*` keys in `agent/i18n.py`. |
| A way for the build to catch drift | None. The gate runs test suites, so a test in `tests/` is enough — **no gate configuration change needed**. |

### The decision that shaped the work

The honest gap was not "the channels answer differently" — they do not. It was that **nothing would notice if they started to**. So the work is: name the shared service, write the allowed differences down with their reasons, and make anything else fail.

---

## 3. Architecture of the agreement

```text
   PHONE                    KEYPAD                    MESSAGE
     │ speech                 │ digit                   │ text
     ▼                        ▼                         ▼
  ASR text ──────────►  answer_turn(session, text, source=…)  ◄── the ONE door
                                   │
                                   ▼
                      _dispatch_turn ──► _run_turn
                       fast path → cache → model → slots → verification
                                   │
                                   ▼
                   tools_client ──► clinic-api      (the facts)
                                   │
                                   ▼
                reply_templates + i18n              (the words)
                                   │
              ┌────────────────────┴────────────────────┐
              ▼                                         ▼
        _speak → audio                          _deliver_written → text
              └──────────── the SAME sentence ───────────┘

   Allowed to differ, and only here:
     · delivery              audio vs text          (the carrier, not the words)
     · private_information   history refused by message
     · number_already_known  the sender's number is not asked for

   agent/answer_contract.py declares those three.
   tests/test_channel_parity.py asks both channels the same questions,
   and counts the branches in main.py. Anything else → the build fails.
```

---

## 4. Files at a glance

| File | Status | What it does for the story |
|---|---|---|
| `agent/answer_contract.py` | **new** (115 lines) | The shared entry's name, the declared divergences with reasons, the channel-only sentence keys, and `describe()` for reviewers |
| `tests/test_channel_parity.py` | **new** (407 lines) | The question set, the same-answer comparison, the declared-divergence checks, and the guards that fail the build |
| `main.py` | modified | `answer_turn()` — the named shared entry; the keypad and message paths now go through it |
| `main_pcm.py` | regenerated | same change (`python tools/make_pcm_variant.py`) |

**Untouched:** clinic-api, every reply template, every i18n sentence, the model prompt, the fast path, ASR/TTS/VAD, the message service, the WhatsApp client, the conversation store, gate configuration, the golden set, and every existing test.

---

## 5. Variables — Old vs New

### 5.1 `agent/answer_contract.py` (all new)

| Name | Kind | Value / meaning |
|---|---|---|
| `SHARED_ANSWER_ENTRY` | constant | `"answer_turn"` — the one door, named so a test can assert it is still the only one |
| `Divergence` | dataclass | `id`, `where`, `marker`, `why` |
| `DIVERGENCES` | tuple | the **complete** list: `delivery`, `private_information`, `number_already_known` |
| `DIVERGENCE_IDS` | frozenset | their ids, for tests to reference by name |
| `CHANNEL_SENTENCE_KEYS` | frozenset | `channel.private_by_message`, `channel.resumed`, `channel.text_only` |
| `CHANNEL_TESTS_IN_MAIN` | int | `len(DIVERGENCES)` = **3** — how many places in `main.py` may ask which channel it is |
| `describe()` | function | the contract as a reviewer wants to read it |

### 5.2 `main.py` (same in `main_pcm.py`)

| Name | Old | New |
|---|---|---|
| `answer_turn(session, text, *, source)` | — | **new** — the shared answer service |
| `run_text_turn(session, text)` | called `_dispatch_turn` itself | delegates to `answer_turn(..., source="message")` |
| `_handle_keypad_digit` | called `_dispatch_turn` itself | delegates to `answer_turn(..., source="keypad")` |
| `_dispatch_turn`, `_run_turn` | unchanged | unchanged |

### 5.3 Environment variables

**None added.** This story is a guarantee about existing behaviour, not a new feature to configure.

---

## 6. `main.py` — Old vs New

### 6.1 The shared answer service

```python
# OLD
async def run_text_turn(session, text: str) -> None:
    """One written message, through the phone line's own turn."""
    await _dispatch_turn(session, "", text_override=text, text_source="message")
```

```python
# NEW
async def answer_turn(session, text: str, *, source: str) -> None:
    """THE SHARED ANSWER SERVICE -- Author: Chakravardhan.

    Every channel that has WORDS enters here: a keypad press, a WhatsApp
    message, and (through _dispatch_turn, once ASR has produced them) a
    caller's own. From this line on there is one path -- fast path, intent
    cache, model, slot filling, verification, clinic-api, reply templates --
    so the answer cannot depend on which channel asked.

    Where a channel is allowed to differ at all is written down in
    agent/answer_contract.py, and tests/test_channel_parity.py asks both
    channels the same questions and fails the build on any difference that
    is not on that list."""
    await _dispatch_turn(session, "", text_override=text, text_source=source)


async def run_text_turn(session, text: str) -> None:
    """One written message, through the shared answer service."""
    await answer_turn(session, text, source="message")
```

### 6.2 The keypad uses the same door

```python
# OLD  (in _handle_keypad_digit)
    await _dispatch_turn(session, "", text_override=text)
# NEW
    await answer_turn(session, text, source="keypad")
```

That is the whole change to `main.py`: **one new function and one redirected call.** No answering logic moved.

---

## 7. `agent/answer_contract.py` — the new file

```python
SHARED_ANSWER_ENTRY = "answer_turn"


@dataclasses.dataclass(frozen=True)
class Divergence:
    id: str
    where: str
    marker: str        # what the parity suite looks for in main.py
    why: str


DIVERGENCES: tuple[Divergence, ...] = (
    Divergence(
        id="delivery",
        where="main._speak",
        marker="await _deliver_written(session, text_bn, fallback_reason, audit_redact)",
        why="The same sentence leaves as audio on a call and as text in a message. "
            "The words are identical; only the carrier differs.",
    ),
    Divergence(
        id="private_information",
        where="main._history_guard -> privacy.channel_is_private",
        marker="safe, reason = privacy.channel_is_private(",
        why="A written answer stays on a shared handset's screen for whoever picks it "
            "up next, so history and bookings are refused by message ... Required by CLAUDE.md.",
    ),
    Divergence(
        id="number_already_known",
        where="main._fill_from_channel",
        marker='if getattr(session, "channel", privacy.CHANNEL_VOICE) == privacy.CHANNEL_VOICE:',
        why="A patient writing from WhatsApp is writing FROM their number, which the "
            "provider asserts, so the booking does not ask for it ... One question fewer, "
            "the same booking.",
    ),
)

CHANNEL_SENTENCE_KEYS = frozenset({"channel.private_by_message", "channel.resumed", "channel.text_only"})
CHANNEL_TESTS_IN_MAIN = len(DIVERGENCES)
```

**Why a declaration and not only a test:** a test proves today's behaviour; the declaration makes tomorrow's fork a deliberate act, with a reason a reviewer can weigh, instead of a quiet drift.

---

## 8. The question set

Put to both channels in the same process, against the same faked clinic responses and the same faked model output. The **only** difference between the two runs is the channel.

| # | Asked | Intent | About |
|---|---|---|---|
| 1 | লিপিড প্রোফাইলের রেট কত | `test_rate` | a price |
| 2 | রেট কত | `test_rate` (no test named) | a question missing a slot |
| 3 | ডাক্তার সেন কি আছেন | `doctor_availability` | a doctor's hours |
| 4 | কার্ডিওলজিতে কারা আছেন | `doctors_by_department` | who sits in a department |
| 5 | কীভাবে টাকা দেব | `payment` | how to pay |
| 6 | রিপোর্ট কবে পাব | `report_collection` | when a report is ready |
| 7 | নমস্কার | `smalltalk` | a greeting |
| 8 | … | `unclear` | something not understood |

Plus a **tool failure** put to both channels, because failure wording is where drift hides — nobody reads it until something is wrong.

A further test asserts the set still covers **every** answering intent, so the set cannot quietly stop covering one.

---

## 9. The declared divergences, and what is asserted about each

| id | Where | The difference | What the suite asserts |
|---|---|---|---|
| `delivery` | `_speak` | audio on a call, text in a message | the **words are identical**; only the carrier differs |
| `private_information` | `_history_guard` | "what have I booked?" is refused by message | the replies differ, the message reply is exactly `channel.private_by_message`, and it names the counter |
| `number_already_known` | `_fill_from_channel` | a message booking is not asked for the number | both channels ask for the **name** at the same point, and the booking placed is identical, number included |

---

## 10. How a divergence fails the build

Two guards, both in `tests/test_channel_parity.py`, which the gate runs as part of `unit-tests` — **no gate configuration was touched**.

1. **The answers are compared**, question by question, for every question in the set.
2. **`main.py` is counted**: every place that asks which channel it is must be one of the declared divergences.

```python
def test_nothing_branches_on_the_channel_without_being_declared():
    for name, src in _main_sources().items():
        tests = src.count('getattr(session, "channel"')
        assert tests == answer_contract.CHANNEL_TESTS_IN_MAIN, (...)
```

### Proved, not assumed

I injected a plausible-looking, undeclared branch into `main.py`:

```python
def _shorter_on_a_message(session, text: str) -> str:
    if getattr(session, "channel", privacy.CHANNEL_VOICE) != privacy.CHANNEL_VOICE:
        return text[:80]
    return text
```

and the build failed with:

```text
AssertionError: main.py asks which channel it is 4 times, but 3 divergences are
declared in agent/answer_contract.py. Declare it (with the reason) or remove it.
```

`main.py` was then restored and the suite passed again (19/19).

Two further guards close the loop: every declared divergence must still exist in the code (a stale declaration is a lie), and the `channel.*` sentences must be exactly the three declared.

---

## 11. Tests — `tests/test_channel_parity.py` (19, all passing)

| Area | Tests |
|---|---|
| **The same question set on each channel** (9) | 8 questions compared word for word · a tool failure says the same thing on both |
| **The sharpest cases** (3) | a price quoted identically to the digit · every channel reads its words from the same table · the question set still covers every answering intent |
| **The declared divergences** (2) | private information is the declared difference · the number already known is the declared difference (same booking at the end) |
| **A divergence fails the build** (5) | nothing branches on the channel without being declared · every declared divergence is actually in the code · both channels enter through the one answer service · only the declared sentences belong to a channel · the contract reads as a list a reviewer can check |

```bash
python -m pytest tests/test_channel_parity.py -v
```

**Whole suite:** 631 tests — **629 passed, 2 failed**. The 2 are the pre-existing golden-set entry `fp-abstain-unknown-doctor`, which fails identically on a clean checkout of HEAD.

---

## 12. Quality gate (`bash scripts/gate.sh --full`)

| Check | Result |
|---|---|
| compile, lint, dead-code, static-analysis, secrets, prod-credentials, debug-code, unexpected-files | **PASS** — no new findings |
| typecheck | **PASS** |
| **api-contract** | **PASS** — no route added or changed; snapshots match |
| **build** | **PASS** — `main_pcm.py` in sync with the generator; all three apps import with their routes |
| phi-in-code, phi-in-logs, approved-test-data | **PASS** |
| gate-protection | **PASS** — no test weakened, no suppression added, no gate file touched |
| integration, safety-policy, phi-boundary, multilingual, handoff, telephony, pstn-8khz, dependency-audit | **PASS** |
| format | FAIL — the same **42** pre-existing unformatted files as before; every new file is formatted |
| golden-set, unit-tests, escalation-abstention | FAIL — the same pre-existing golden entry |

**This story adds no new gate failure, breaks no API, and changes no build output.**

---

## 13. What did NOT change

| Area | How it is known |
|---|---|
| Every answer a patient gets | the parity suite compares them; the reply templates and i18n files are untouched |
| The voice agent's HTTP API | `api-contract` PASS against the committed snapshots |
| clinic-api | no file touched |
| The message service, WhatsApp client, conversation store | no file touched |
| `main_pcm.py` | regenerated; the generator verified the reasoning half byte-identical |
| Gate configuration | untouched — the new tests are picked up by the existing `unit-tests` selection |
| Dependencies | none added |

---

## 14. Risks and limitations

1. **The guard counts one spelling.** It looks for `getattr(session, "channel"`. Somebody could branch on the channel by another route — reading `session.sender`, say — and the count would not move. The declared markers and the answer comparison would still catch most of it, but not all.
2. **Equality is asserted in Bengali.** The question set runs in the pod's default language. Hindi and English answers are covered by the message-channel and multilingual suites, not here.
3. **The model is faked** in the parity suite, deliberately: the same intent is given to both channels so the comparison isolates the channel. It does **not** prove the model classifies a spoken and a written sentence the same way — that is a different story, and the phrasing differs anyway (speech has ASR errors).
4. **Booking turn counts differ** between channels by design (`number_already_known`). The suite asserts the resulting booking is identical, not the number of turns.
5. **A third channel** (SMS, a web form) would need its divergences declared before it could pass — which is the intent, but it means the contract must be maintained, not just written.
6. **The tool-failure case is one path.** Other failure wordings (verification lockout, capacity refusal) are not in the set yet.

## 15. To-dos

1. Add the verification-lockout and capacity-refusal wordings to the question set.
2. Run the question set in Hindi and English once those replies are reviewed by speakers.
3. When a third channel is added, declare its divergences in `agent/answer_contract.py` before wiring it.
4. Consider asserting the parity of the **audit** record shape as well as the words.
5. Code owner: regenerate `tests/golden/golden_set.json` (pre-existing, unrelated) so the gate can go green.
