# The Same Questions by Message — Old vs New Code and Variables

**Author:** Chakravardhan
**Story:** *As a patient, I want to ask the same questions by message and get the same answers, so that I can use the channel I already have open.*
**Acceptance criteria:** The intent and tool layer is shared with voice rather than duplicated, and conversation state survives a switch between channels. Integration is through an official business provider with template compliance.
**Branch:** `dev_chakravardhan`. **Old** = the working tree as it stood after the patient-timeline story (itself on top of commit `932dbb2`). **New** = the working tree now.
**Status:** implemented and tested **locally only — not committed, not pushed.**

---

## 0. What the story means

| Part of the story | In plain words | What it forces in the code |
|---|---|---|
| "ask the same questions by message" | A patient can type what they would have said on the phone — the price of CBC, when Dr. Sen sits, book an appointment. | A second **channel** into the agent, reading text instead of audio. |
| "and get the same answers" | The reply by message is the reply the phone line would speak — same facts, same wording. | The message must go through **the same reasoning**, not a copy of it. |
| "the channel I already have open" | Most patients already have WhatsApp open; they should not have to ring. | An **official messaging provider** — WhatsApp Business Platform. |
| "intent and tool layer is shared with voice rather than duplicated" | One brain, two mouths. | No second intent extractor, no second clinic client, no second reply table. |
| "conversation state survives a switch between channels" | Start booking on the phone, finish by message — or the other way round — without starting again. | A **shared conversation store** both processes read and write. |
| "official business provider with template compliance" | Use Meta's official API and follow its rules about what may be sent when. | Signed webhooks; free-form replies only inside the 24-hour window; outside it, only a pre-approved template. |

What it does **not** mean: that private information can now be sent by message. `CLAUDE.md` allows patient history only after verification **and only on a private audio path**. A written message is not an audio path — and it stays on a shared handset's screen for whoever picks it up next. So history and bookings are **refused by message**, before any verification, and the patient is pointed to the phone line and the counter.

---

## 1. Summary

| | Old | New |
|---|---|---|
| Ways to reach the agent | phone line (WebSocket audio), keypad | phone line, keypad, **WhatsApp** |
| Reasoning for a typed message | — | the phone line's **own** turn loop (`main.py _run_turn`), entered where a keypad digit enters |
| Where a reply goes | always TTS → audio | TTS → audio on voice; **the same sentence as text** on a message |
| Conversation memory | inside one call only (`session.pending`) | also in `conversation_state.db`, keyed by an **HMAC of the number**, 30-minute life |
| Booking started on a call, finished by message | impossible | works, **once the caller verified** on the call |
| Booking started by message, finished on a call | impossible | works, **once the caller verifies** on the call |
| Booking by message asks for a number | — | **no** — filled from the number they write from; a number they type wins |
| History / bookings by message | — | **refused**, audited, patient sent to phone line and counter |
| Provider | — | Meta WhatsApp Cloud API: signed webhook, registration handshake, de-duplicated deliveries |
| Template compliance | — | free-form only inside the 24-hour window; outside it only the approved `reply_expired` template, or nothing |
| Languages by message | — | **answered in the language written** — Bengali, Hindi or English, mixed script decided by majority — without needing a speech model for it |
| Languages by voice | the caller must ASK for a language | can also be **identified from the first utterance**, where the pod has the checkpoints (`VOICE_AGENT_LANG_STRATEGY=parallel`); off by default |
| Audit | one record per call | also **one record per inbound message**, transport `whatsapp` |

**The voice agent's HTTP API is unchanged** — `api-contract` passes with the committed snapshots. **clinic-api is unchanged.** No new dependency (httpx and FastAPI were already required).

---

## 2. Architecture — the working process

### 2.1 The pieces, and who owns what

```text
  PATIENT on WhatsApp                              PATIENT on the phone
        │ types "CBC-র দাম কত"                            │ speaks
        ▼                                                ▼
  Meta WhatsApp Cloud API                        browser / handset
        │ signed webhook (HTTPS)                         │ WebSocket, raw PCM
        ▼                                                ▼
  agent/message_service.py                        main_pcm.py transport half
   · X-Hub-Signature-256 checked                   · VAD finds the turn
   · message id de-duplicated                      · ASR transcribes it
   · MessageSession built                          · CallSession
        └─────────────────┬──────────────────────────────┘
                          ▼
        ══════════ THE SHARED TURN LOOP  (_run_turn) ══════════
         fast path → intent cache → LLM → slot filling → verification
                          │
                          ▼
             agent/tools_client.py ──► clinic-api
             (prices, doctors, availability, bookings, history)
                          │
                          ▼
        agent/reply_templates.py + agent/i18n.py   ← THE WORDS, always
                          │
            ┌─────────────┴──────────────┐
            ▼                            ▼
   _deliver_written                   _speak
   → WhatsApp text                    → TTS → audio
            │                            │
            └────────► agent/call_audit.py ◄────────┘
                       one record per message, one per call
                          ▲
        agent/conversation_store.py — shared state, HMAC-keyed, 30 min
        (read and written by BOTH processes, one SQLite file)
```

Everything inside the double lines is written once and used by both channels. A channel owns only how a turn **arrives** and how an answer **leaves**.

### 2.2 One message, step by step

| # | Step | Where |
|---|---|---|
| 1 | Patient sends a WhatsApp message | Meta |
| 2 | Meta POSTs the webhook; the signature is verified against the app secret | `whatsapp.signature_valid` |
| 3 | The message id is checked and recorded, so a redelivery is answered once | `ConversationStore.first_sight` |
| 4 | HTTP 200 returns **immediately**; the turn runs in the background | `MessageChannel.accept` |
| 5 | The sender's last-inbound time is stored — this is what opens the 24-hour window | `ConversationStore.note_inbound` |
| 6 | Their state is loaded: language, and any booking in progress | `ConversationStore.load` |
| 7 | A `MessageSession` is built — no audio, no echo, never a verification token | `MessageSession` |
| 8 | The message enters the **phone line's own turn**, where a keypad digit enters | `main.run_text_turn` → `_dispatch_turn` |
| 9 | Fast path, then cache, then the model; slots filled; clinic-api called live | the shared layer |
| 10 | The answer is composed from templates — never written by the model | `reply_templates` |
| 11 | `_speak` sees a non-voice channel and writes instead of speaking; the window is checked, then Meta is called | `_deliver_written` → `whatsapp.send_text` |
| 12 | State is saved and the record closed, transport `whatsapp` | `ConversationStore.save`, `CallAudit.end` |

Messages from one number are answered one at a time, in arrival order, so step 6 always sees what step 12 last wrote.

### 2.3 What is shared, and what each channel owns

| Layer | Voice | Message | Shared? |
|---|---|---|---|
| Arrival | WebSocket PCM → VAD → ASR | signed webhook → text | per channel |
| Intent | fast path → cache → LLM | *the same code* | **shared** |
| Slot filling, booking flow | `_continue_pending` | *the same code* | **shared** |
| Clinic calls | `tools_client` | *the same code* | **shared** |
| Wording | `reply_templates` + `i18n` | *the same code* | **shared** |
| Privacy rule | room judged by echo | refused outright | one function, different answer |
| Delivery | TTS → audio | text to Meta | per channel |
| Audit | one record per call | one record per message | **shared** |

### 2.4 What is stored, where, and for how long

| Data | Where | Lifetime | Notes |
|---|---|---|---|
| Booking in progress, language, last channel | `conversation_state.db` | 30 minutes | keyed by an HMAC of the last 10 digits |
| Provider message ids | the same file | 7 days | de-duplication; Meta retries that long |
| Verification token, patient timeline | memory of one conversation | that conversation only | never written to disk |
| What was asked and answered | `call_audit.db` | kept | history and bookings redacted |
| Appointments, patients, records | clinic-api | the clinic's record | untouched by this story |

### 2.5 The two channel-crossings

```text
PHONE → MESSAGE                          MESSAGE → PHONE
caller verifies (DOB / PIN)              patient books by message, stops
starts a booking, hangs up               rings, asks for history, verifies
   │ _save_for_other_channels               │ _resume_from_other_channel
   │ (verified number only)                 │ (after verification only)
   ▼                                        ▼
conversation_state.db ─────────────────► "Let us continue the booking
   │                                        you started earlier…"
   ▼
message: "18:15" → booked
```

Only the plain booking fields cross (`portable()`): doctor, date, time, name, number. A list of doctors read out on the other channel does not, and nothing from verification ever does.

### 2.6 Three languages, and mixtures of them

| What the patient writes | Answered in | Why |
|---|---|---|
| `সিবিসি টেস্টের রেট কত` | Bengali | majority script |
| `सीबीसी की कीमत क्या है` | Hindi | majority script |
| `what is the price of CBC` | English | majority script |
| `রিপোর্ট ready তো?` (code-switched) | Bengali | one English word must not flip a conversation |
| "hindi", "বাংলায় বলুন" | the language asked for | an explicit request, exactly as on the phone line |

Once chosen, the language is kept in the conversation store and holds for the
next message; only an explicit request changes it.

**The speech gate does not apply to writing.** `enabled()` will not use Hindi
or English until an ASR checkpoint for them exists — right for a call, since a
pod with no Hindi model genuinely cannot hear Hindi, and wrong for a message,
where nothing is heard and `agent/i18n.py` holds every sentence in all three.
A written turn is marked with `use_text_channel()` and answered from
`text_languages()` instead. It is a ContextVar, so one service answers many
patients in different languages at the same time without them colliding.

The model was already told that callers mix languages (`agent/llm.py`'s
prompt). What is new is that the ANSWER may now come back in the language the
patient actually wrote in.

**On the phone line it is a different problem.** Speech carries no script: a
Bengali-only checkpoint returns Bengali glyphs for whatever it is played, so
nothing about its output says "this was Hindi". The only way to tell from
audio is to let each checkpoint the pod HAS hear the same utterance and keep
the best transcript.

| What the pod has | A caller who speaks Hindi |
|---|---|
| Bengali checkpoint only (today) | heard as Bengali. If they ask for Hindi they are told the line cannot serve it — never silently ignored. |
| Bengali + Hindi checkpoints, `VOICE_AGENT_LANG_STRATEGY=fixed` (default) | served in Hindi the moment they **ask**; the checkpoint loads on first use |
| Bengali + Hindi checkpoints, `…=parallel` | **identified from their first utterance**, no asking |

The probe scores each decode by how far the CTC and RNNT decoders agreed —
they diverge on audio a model was not trained for — with transcript length
only breaking ties. It runs on **one turn** of the call and never again.

### 2.7 Where each failure goes

| Failure | What the patient gets |
|---|---|
| Webhook unsigned or wrongly signed | refused; nothing runs |
| Non-text message (voice note, photo) | "I can only read typed messages here… or call us" |
| History or bookings asked by message | privacy refusal, pointed to the phone line and the counter |
| clinic-api down | the phone line's own "technical problem" sentence |
| Reply outside the 24-hour window | the approved template, or nothing — never a free-form reply |
| Meta rejects the send | the reply is recorded as undelivered in the audit |
| Service dies mid-turn | the record is closed at the next start; the state file keeps the booking |

---

## 3. What I found first

| Looked for | Found |
|---|---|
| Any WhatsApp / messaging / cross-channel code | **none** — the only outbound message path is the DLT SMS gateway for confirmations (`clinic-api/notifications.py`) |
| A way for text to enter the reasoning | **yes** — `_run_turn(session, "", text_override=...)`, used by keypad digits. It was written precisely so a second input would not need a second dispatcher. |
| Where the reasoning lives | all in `main.py` (fast path → cache → LLM → slot filling → clinic API → reply template), generated into `main_pcm.py` |
| How tests reach it | they patch `main_pcm._speak`, `main_pcm._tools`, `main_pcm._resolve_intent` directly |
| API contract gate | pins every route of clinic-api and both voice apps (`scripts/gate-contracts/*.json`); a new route there fails until a code owner updates the snapshot |
| Privacy rule | `agent/privacy.py` judges the **audio path**; nothing covered a channel with no audio |
| Caller-ID | none on this transport — a call only knows a number the caller says, or proves at the history challenge |

### Constraints that shaped the design

1. **Share, don't copy.** Moving the reasoning into a new module would break every test that patches `main_pcm._speak` / `_tools`. So the reasoning stays where it is and the message channel **drives it** through one entry point.
2. **Don't touch the voice API.** The message endpoints live in a **separate service** (`agent/message_service.py`), so the voice agent's pinned routes are untouched.
3. **Privacy outranks the story.** History and bookings are refused on a written channel, whatever the bench weakening switch says.
4. **SMS cannot give "the same answers"** in India without registering every reply as a DLT template. WhatsApp's rule (free-form inside the 24-hour window) is the one that makes the story possible.
5. **No number as a key at rest.** The shared store is keyed by HMAC; a plain hash of a 10-digit mobile number is reversible in minutes.

---

## 4. Files at a glance

| File | Status | What it does for the story |
|---|---|---|
| `agent/message_service.py` | **new** (~395 lines) | The WhatsApp service: webhook, handshake, simulator, `MessageSession`, `MessageChannel`. Holds **no reasoning**. |
| `agent/whatsapp.py` | **new** (~323 lines) | Meta Cloud API client: signature check, handshake, webhook parser, send text/template, 24-hour window, registered template body. |
| `agent/conversation_store.py` | **new** (~301 lines) | Cross-channel state in SQLite, HMAC-keyed, 30-minute TTL, message de-duplication. |
| `main.py` | modified | `_speak` writes text on a message channel; channel-aware privacy guard; save at hang-up, resume after verification; `start_text_services` / `run_text_turn` seam. |
| `main_pcm.py` | regenerated | same changes, via `python tools/make_pcm_variant.py` (reasoning half verified byte-identical) |
| `agent/privacy.py` | modified | `channel_is_private()` — a written channel is never private |
| `agent/call_audit.py` | modified | `AUDIO_TEXT`, `END_MESSAGE_TURN`; delivered text counts as heard |
| `agent/reply_templates.py` | modified | `disclosure_blocked_reply` names the message refusal |
| `agent/i18n.py` | modified | 3 new `channel.*` sentences in bn / hi / en |
| `agent/language.py` | modified | a written turn may answer in any of the three languages, with no ASR checkpoint needed (+42 lines, none removed) |
| `tests/test_multilingual.py` | modified | 6 tests for identifying the language a caller SPEAKS |
| `tests/test_message_channel.py` | **new** | 34 tests |
| `IMPLEMENTATION_MESSAGE_CHANNEL_OLD_VS_NEW.md` | **new** | this document |

**Not touched:** clinic-api (all of it), `agent/llm.py`, `agent/fast_path.py`, `agent/tools_client.py`, `agent/slot_parse.py`, ASR, TTS, VAD, echo guard, every gate file, `tests/golden/`, every existing test, both `requirements.txt`, `deploy/`.

---

## 5. Variables — Old vs New

### 5.1 Environment variables (all new, all optional)

| Variable | Default | Secret? | Purpose |
|---|---|---|---|
| `VOICE_AGENT_STATE_KEY` | *(unset → random per process)* | **yes** | HMAC key for the state store. **Must be the same** in the voice agent and the message service, or state does not cross. |
| `VOICE_AGENT_STATE_DB` | `/workspace/conversation_state.db` | no | Shared SQLite file, beside the call audit. |
| `VOICE_AGENT_STATE_TTL_S` | `1800` | no | How long a half-done conversation is offered again. |
| `WHATSAPP_ACCESS_TOKEN` | *(unset)* | **yes** | Meta system-user token. |
| `WHATSAPP_PHONE_NUMBER_ID` | *(unset)* | no | The clinic's WhatsApp business number id. With the token, makes the channel "configured". |
| `WHATSAPP_APP_SECRET` | *(unset)* | **yes** | Verifies `X-Hub-Signature-256`. **Unset → every webhook is refused.** |
| `WHATSAPP_VERIFY_TOKEN` | *(unset)* | yes | Echoed by Meta at webhook registration. |
| `WHATSAPP_TEMPLATE_REPLY_EXPIRED` | *(unset)* | no | Approved template name for replies outside the 24-hour window. Unset → nothing is sent outside it. |
| `WHATSAPP_API_BASE` | `https://graph.facebook.com` | no | Graph API base. |
| `WHATSAPP_GRAPH_VERSION` | `v21.0` | no | Graph API version. |
| `WHATSAPP_COUNTRY_CODE` | `91` | no | Turns the sender's number into the 10 digits clinic-api keeps. |
| `WHATSAPP_TIMEOUT_S` | `8` | no | Send timeout. |
| `MESSAGE_SERVICE_SIMULATOR` | `0` | no | Enables `POST /api/messages/simulate` — **only when no live number is configured**. |
| `MESSAGE_SERVICE_VOICE_MODULE` | `main_pcm` | no | Which voice module's turn loop to drive. |

Secrets are read from the environment only and are never written into the repository.

### 5.2 Constants

| Name | File | Value | Why |
|---|---|---|---|
| `CHANNEL_VOICE` | `agent/privacy.py` | `"voice"` | The only channel with an audio path to judge |
| `UNSAFE_TEXT_CHANNEL` | `agent/privacy.py` | `"text_channel"` | Refusal reason on a written channel |
| `AUDIO_TEXT` | `agent/call_audit.py` | `"text"` | A reply delivered as words, not audio |
| `END_MESSAGE_TURN` | `agent/call_audit.py` | `"message_turn"` | End of a record that covers one message |
| `CHANNEL` | `agent/whatsapp.py` | `"whatsapp"` | Channel in state, transport in audit |
| `CUSTOMER_SERVICE_WINDOW_S` | `agent/whatsapp.py` | `86400` | Meta's 24-hour window |
| `TEXT_BODY_MAX` | `agent/whatsapp.py` | `4096` | Meta's text limit |
| `REPLY_EXPIRED_BODY` | `agent/whatsapp.py` | bn/hi/en text | Body to submit for approval, verbatim |
| `PORTABLE_STATES` | `agent/conversation_store.py` | `date, time_slot, patient_name, phone` | The only flows that cross channels |
| `NEVER_STORED_STATES` | `agent/conversation_store.py` | `history_verify, record_phone` | Verification never stored |
| `DEFAULT_TTL_S` | `agent/conversation_store.py` | `1800` | 30 minutes |
| `MESSAGE_ID_TTL_S` | `agent/conversation_store.py` | 7 days | Meta redelivers for up to 7 days |

### 5.3 `agent/language.py`

| Name | Kind | Old | New |
|---|---|---|---|
| `_TEXT_CHANNEL` | ContextVar | — | **new** — is this turn written? one copy per task |
| `use_text_channel(flag)` / `reset_text_channel(token)` | functions | — | **new** — mark a turn as written |
| `text_languages()` | function | — | **new** — the languages a written turn may use; no ASR gate |
| `enabled()` | function | ASR-gated always | ASR-gated **for speech**; `text_languages()` for writing |

### 5.3 `main.py` (same in `main_pcm.py`)

| Name | Kind | Old | New |
|---|---|---|---|
| `_conversations` | module global | — | `ConversationStore \| None`, opened in `_startup` |
| `_load_fast_path()` | function | inline in `_startup` | **moved out unchanged**, so both services load the catalogue the same way |
| `_speak(...)` | function | always TTS | text branch first: non-voice session → `_deliver_written` |
| `_deliver_written(...)` | function | — | **new** — sends the text, audits it with `AUDIO_TEXT` |
| `_history_guard(...)` | function | `audio_path_is_private(echo)` | `channel_is_private(channel, echo)` |
| `_continue_history_verification` | function | read out history/bookings | …then `_resume_from_other_channel` |
| `_save_for_other_channels(session)` | function | — | **new** — at hang-up, verified callers only |
| `_resume_from_other_channel(session)` | function | — | **new** — after verification |
| `_fill_from_channel(session, slots)` | function | — | **new** — fills a booking's phone from the number the patient writes FROM; never on the phone line |
| `start_text_services(transport)` | function | — | **new** — clinic client, fast path, cache, audit; no ASR/VAD/TTS |
| `stop_text_services()` | function | — | **new** |
| `text_audit_store()` | function | — | **new** — the audit store a message service files into |
| `speak_to(session, text)` | function | — | **new** — the channel's own sentences, through `_speak` |
| `run_text_turn(session, text)` | function | — | **new** — `_dispatch_turn(..., text_source="message")` |
| `_dispatch_turn` / `_run_turn` | functions | `text_override` | `+ text_source="keypad"` (default unchanged) |
| `ws_audio` `finally` | — | cleanup | `_save_for_other_channels(session)` **before** cleanup |

### 5.4 New HTTP routes — on the **message service only**

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/webhook/whatsapp` | Meta's registration handshake (`hub.mode`, `hub.verify_token`, `hub.challenge`) |
| `POST` | `/webhook/whatsapp` | Messages and delivery statuses; **refused unless signed** |
| `POST` | `/api/messages/simulate` | Bench only: one message in, replies back, nothing sent |
| `GET` | `/api/health` | Configuration and state-store health |

No route was added to clinic-api or to either voice app.

---

## 6. `agent/privacy.py` — Old vs New

```python
# OLD
def audio_path_is_private(echo_guard) -> tuple[bool, str]:
    ...                                  # judges the ROOM: handset / speakerphone / unknown
```

```python
# NEW (added; audio_path_is_private unchanged)
CHANNEL_VOICE = "voice"
UNSAFE_TEXT_CHANNEL = "text_channel"


def channel_is_private(channel: str, echo_guard) -> tuple[bool, str]:
    """A spoken answer is gone once said; a written one stays on the handset's
    screen for whoever picks it up next. So on anything but the phone line the
    answer is no -- before verification, and whatever
    VOICE_AGENT_HISTORY_REQUIRE_PRIVATE_PATH says."""
    if channel != CHANNEL_VOICE:
        return False, UNSAFE_TEXT_CHANNEL
    return audio_path_is_private(echo_guard)
```

`recoverable(UNSAFE_TEXT_CHANNEL)` is `False`: the patient is sent to the phone line and the counter, not asked to try again.

---

## 7. `agent/call_audit.py` — Old vs New

```python
# OLD
        heard = delivered and audio == "synthesized"
```

```python
# NEW
AUDIO_TEXT = "text"
END_MESSAGE_TURN = "message_turn"
...
        # On the message channel the words themselves are what reach the
        # patient, so delivered text counts as heard (AUDIO_TEXT).
        heard = delivered and audio in ("synthesized", AUDIO_TEXT)
```

Voice records are judged exactly as before.

---

## 8. `agent/reply_templates.py` — Old vs New

```python
# OLD
    if reason == "disclosure_disabled":
        return t(code, "history.disclosure_off")
    return t(code, "history.speakerphone")
```

```python
# NEW
    if reason == "disclosure_disabled":
        return t(code, "history.disclosure_off")
    if reason == "text_channel":
        # "Pick the phone up" would be the wrong fix; calling is the right one.
        return t(code, "channel.private_by_message")
    return t(code, "history.speakerphone")
```

Every existing reason returns the same sentence as before, so the golden set is unaffected.

---

## 9. `agent/i18n.py` — new sentences (bn / hi / en)

| Key | English | Used for |
|---|---|---|
| `channel.private_by_message` | "For your privacy, I cannot share your records or bookings in a message. Please call us and ask, or contact the counter." | History / bookings asked by message |
| `channel.resumed` | "Let us continue the booking you started earlier." | A booking picked up from the other channel |
| `channel.text_only` | "I can only read typed messages here. Please type your question, or call us." | Voice note, photo, sticker |

All three pass `tests/test_no_smartphone.py` automatically (it walks every i18n string). **Every other answer on the message channel is an existing sentence, unchanged.**

---

## 10. `agent/language.py` — Old vs New

```python
# OLD -- one rule, built for speech
def enabled() -> tuple[str, ...]:
    raw = os.environ.get("VOICE_AGENT_LANGUAGES", "").strip()
    wanted = [...] if raw else [default_lang()]
    out = []
    for code in wanted:
        if code == default_lang() or os.environ.get(spec.asr_checkpoint_env, "").strip():
            out.append(code)          # Hindi needs a Hindi ASR checkpoint
    ...
```

```python
# NEW -- the same rule for speech, a different one for writing
_TEXT_CHANNEL: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "language_text_channel", default=False)


def use_text_channel(flag: bool = True) -> contextvars.Token:
    """Mark this turn as WRITTEN. Reset with the token it returns."""
    return _TEXT_CHANNEL.set(flag)


def text_languages() -> tuple[str, ...]:
    """Every language a WRITTEN turn may be answered in ... No ASR gate --
    there is no audio to transcribe."""
    ...


def enabled() -> tuple[str, ...]:
    if _TEXT_CHANNEL.get():
        return text_languages()
    ...                               # unchanged for the phone line
```

`agent/message_service.py` sets it as the FIRST thing in a turn, before the
language is decided, and resets it in `finally`:

```python
async def _answer(self, msg):
    lang_token = lang_mod.use_text_channel()
    ...
    lang = snap.lang if snap and snap.lang else lang_mod.detect_from_text(msg.text)
```

**Measured on a Bengali-only pod** (no `VOICE_AGENT_NEMO_FILE_HI`, no
`VOICE_AGENT_LANGUAGES`):

```text
phone line enabled():      ('bn',)
inside a written turn:     ('bn', 'hi', 'en')
सीबीसी की कीमत क्या है  -> hi   "सीबीसी टेस्ट का रेट 650 रुपये है..."
what is the price of CBC -> en   "The CBC test costs 650 rupees..."
সিবিসি টেস্টের রেট কত    -> bn   "সিবিসি টেস্টের রেট 650 টাকা..."
রিপোর্ট ready তো?        -> bn   (code-switched: the majority decides)
back on the phone line:    ('bn',)
```

The file is a **pure addition** — 42 lines added, none removed, nothing about
speech touched.

---

## 11. `main.py` — Old vs New

### 11.1 Imports and the shared store

```python
# OLD
from agent import call_audit
...
_audit_store: call_audit.AuditStore | None = None
```

```python
# NEW
from agent import call_audit
from agent import conversation_store
...
_audit_store: call_audit.AuditStore | None = None
_conversations: conversation_store.ConversationStore | None = None
```

### 11.2 `_startup` — the catalogue moved into a function both services use

```python
# OLD (inside _startup)
    try:
        import httpx as _httpx
        async with _httpx.AsyncClient(timeout=10) as c:
            payload = (await c.get(f"{CLINIC_API_BASE}/api/v1/catalogue")).json()
        _fast_path = FastPath(Catalogue(payload))
        logger.info("fast path ready over %d catalogue rows", len(_fast_path.catalogue))
    except Exception as e:  # noqa: BLE001 - degrade to LLM-only, never fail startup
        logger.warning("catalogue unavailable, fast path disabled: %s", e)
        _fast_path = None
```

```python
# NEW
async def _load_fast_path() -> FastPath | None:
    try:
        import httpx as _httpx
        async with _httpx.AsyncClient(timeout=10) as c:
            payload = (await c.get(f"{CLINIC_API_BASE}/api/v1/catalogue")).json()
        fast_path = FastPath(Catalogue(payload))
        logger.info("fast path ready over %d catalogue rows", len(fast_path.catalogue))
        return fast_path
    except Exception as e:  # noqa: BLE001 - degrade to LLM-only, never fail startup
        logger.warning("catalogue unavailable, fast path disabled: %s", e)
        return None

# inside _startup
    _conversations = conversation_store.ConversationStore()
    ...
    _fast_path = await _load_fast_path()

# inside _shutdown
    if _conversations:
        _conversations.close()
```

The suppression directive moved with the code — the per-file count is unchanged (7).

### 11.3 `_speak` — the same answer, written

```python
# OLD
async def _speak(session, text_bn, fallback_reason=None, audit_redact=None):
    audio, tts_error, delivered = "none", None, False
    try:
        await session.send_json("AI", text_bn)
        ...TTS, echo reference, playback gate...
```

```python
# NEW
async def _speak(session, text_bn, fallback_reason=None, audit_redact=None):
    if getattr(session, "channel", privacy.CHANNEL_VOICE) != privacy.CHANNEL_VOICE:
        await _deliver_written(session, text_bn, fallback_reason, audit_redact)
        return
    audio, tts_error, delivered = "none", None, False
    ...unchanged...


async def _deliver_written(session, text, fallback_reason, audit_redact) -> None:
    delivered = False
    try:
        delivered = await session.deliver_text(text)
    finally:
        _audit(session).agent_response(
            text, lang=getattr(session, "lang", None), audio=call_audit.AUDIO_TEXT,
            delivered=delivered, fallback_reason=fallback_reason, redact=audit_redact)
```

A `CallSession` has no `channel` attribute, so every voice call takes the old path, byte for byte.

### 11.4 `_history_guard` — the channel first

```python
# OLD
    safe, reason = privacy.audio_path_is_private(session.echo)
```

```python
# NEW
    safe, reason = privacy.channel_is_private(
        getattr(session, "channel", privacy.CHANNEL_VOICE), session.echo)
```

Because the guard runs **before** `begin_verification`, a message never starts a challenge and never fetches a record.

### 11.5 State across channels

```python
# NEW
def _save_for_other_channels(session: CallSession) -> None:
    """Keyed by a VERIFIED number only -- a number merely said is no proof of
    holding that handset. Must run before cleanup()."""
    if _conversations is None or not session.history_token or not session.history_phone:
        return
    _conversations.save(session.history_phone, lang=session.lang, pending=session.pending,
                        channel=privacy.CHANNEL_VOICE)


async def _resume_from_other_channel(session: CallSession) -> None:
    if _conversations is None or session.pending is not None or not session.history_token:
        return
    snap = _conversations.load(session.history_phone, privacy.CHANNEL_VOICE)
    if snap is None or snap.pending is None or snap.channel == privacy.CHANNEL_VOICE:
        return
    session.pending = snap.pending
    awaiting = snap.pending["awaiting"]
    _audit(session).intent("resume_flow", "conversation_store", flow=awaiting,
                           from_channel=snap.channel)
    await _speak(session, _t(session.lang, "channel.resumed")
                 + missing_slot_prompt("book_appointment", awaiting, session.lang))
```

```python
# OLD (_continue_history_verification, verified branch)
        if purpose == PURPOSE_BOOKINGS:
            await _speak_bookings(session)
        else:
            await _speak_history(session)
        return True
# NEW
        ...same...
        await _resume_from_other_channel(session)
        return True
```

```python
# OLD (ws_audio finally)
        session.cleanup()
# NEW
        _save_for_other_channels(session)   # before cleanup(), which drops verification
        session.cleanup()
```

### 11.6 Not asked for the number they are writing from

```python
# NEW
def _fill_from_channel(session, slots: dict) -> bool:
    """A patient writing from WhatsApp is writing FROM their number -- the
    provider asserts it, and it is where the written confirmation goes -- so
    asking them to type it back is the "recite what you have already told us"
    this story exists to remove. Only an EMPTY field is filled; the phone line
    has no caller-ID, so nothing is ever filled there."""
    if getattr(session, "channel", privacy.CHANNEL_VOICE) == privacy.CHANNEL_VOICE:
        return False
    number = getattr(session, "history_phone", None)
    if not number or slots.get("phone"):
        return False
    slots["phone"] = number
    return True
```

Called at both booking sites, immediately before `_fill_from_record`:

```python
# OLD                                   # NEW
from_record = _fill_from_record(...)    _fill_from_channel(session, merged)
                                        from_record = _fill_from_record(session, merged)
```

So a booking by message asks for the doctor, the date, the time and the name
-- and stops. A patient booking for a relative who will take the call names
that number themselves, and their words win.

### 11.7 Which language the caller is speaking

```python
# OLD -- the session's language, which only changed when the caller asked
                    node = asr_mod.for_language(session.lang) or _asr
                    asr_result = await node.transcribe_utterance(utterance_wav)
```

```python
# NEW -- the same, unless this pod can hear more than one language
                    asr_result = await _transcribe_in_caller_language(session, utterance_wav)


async def _transcribe_in_caller_language(session, utterance_wav):
    strategy, heard = lang_mod.strategy(), lang_mod.enabled()
    if (strategy == lang_mod.STRATEGY_PARALLEL and not session.language_probe_done
            and len(heard) > 1):
        session.language_probe_done = True          # one probe per CALL
        best, best_lang, best_score = None, session.lang, -1.0
        for code in heard:
            candidate_node = asr_mod.for_language(code)
            if candidate_node is None:
                continue
            candidate = await candidate_node.transcribe_utterance(utterance_wav)
            score = _transcript_score(candidate)    # decoder agreement, length breaks ties
            if score > best_score:
                best, best_lang, best_score = candidate, code, score
        if best is not None:
            _adopt_language(session, best_lang)
            return best

    node = asr_mod.for_language(session.lang) or _asr
    result = await node.transcribe_utterance(utterance_wav)
    if strategy == lang_mod.STRATEGY_SCRIPT and (result.text or "").strip():
        _adopt_language(session, lang_mod.detect_from_text(result.text, fallback=session.lang))
    return result
```

`CallSession` gains one field, `language_probe_done`. **The default path is
unchanged**: with `STRATEGY_FIXED` (the default) or a single checkpoint, this
is the one decode it always was, and the tests assert exactly that.

### 11.8 The seam the message service uses

```python
# NEW
async def start_text_services(transport: str) -> None:
    global _tools, _intent_cache, _fast_path, _audit_store
    _audit_store = call_audit.AuditStore()
    recovered = _audit_store.recover_unfinished(transport)
    ...
    _tools = ClinicToolsClient(CLINIC_API_BASE)
    _intent_cache = SemanticCache()
    _fast_path = await _load_fast_path()

async def stop_text_services() -> None: ...
def text_audit_store() -> call_audit.AuditStore | None: return _audit_store
async def speak_to(session, text: str) -> None: await _speak(session, text)

async def run_text_turn(session, text: str) -> None:
    await _dispatch_turn(session, "", text_override=text, text_source="message")
```

```python
# OLD
async def _dispatch_turn(session, utterance_wav, text_override=None):
    ...await _run_turn(session, utterance_wav, text_override)
async def _run_turn(session, utterance_wav, text_override=None):
    ...audit.transcript(text, source="keypad", redacted=secret)
# NEW
async def _dispatch_turn(session, utterance_wav, text_override=None, text_source="keypad"):
    ...await _run_turn(session, utterance_wav, text_override, text_source)
async def _run_turn(session, utterance_wav, text_override=None, text_source="keypad"):
    ...audit.transcript(text, source=text_source, redacted=secret)
```

The default `"keypad"` keeps every existing caller identical.

---

## 12. The new modules — key code

### 12.1 `agent/conversation_store.py`

```python
class ConversationStore:
    def key_for(self, number):                 # HMAC-SHA256(key, last 10 digits)
    def load(self, number, channel) -> Snapshot | None
        # same channel  -> its own flow back whole (a list of doctors, retries...)
        # other channel -> portable(): only date / time_slot / patient_name / phone
        # older than TTL -> no language, no flow
    def save(self, number, *, lang, pending, channel) -> bool
        # never stores history_verify / record_phone / from_record / tokens
    def note_inbound(self, number, sent_at, channel)   # latest inbound -> 24-hour window
    def first_sight(self, message_id) -> bool          # de-duplicates Meta redeliveries
    def health(self) -> dict
```

Never raises into a conversation: a database error is logged by type only (no PHI), counted, and the conversation continues without memory.

### 12.2 `agent/whatsapp.py`

```python
def signature_valid(app_secret, raw_body, header) -> bool   # X-Hub-Signature-256, constant-time
def challenge_response(config, mode, token, challenge)      # registration handshake
def parse_webhook(payload) -> (list[Inbound], list[DeliveryStatus])   # tolerant, never raises
def local_number(sender, country_code) -> str | None        # "919000000101" -> "9000000101"
def window_open(last_inbound_at, now) -> bool               # Meta's 24-hour window
async def send_text(client, config, to, text) -> SendResult      # preview_url False, never raises
async def send_template(client, config, to, name, lang) -> SendResult
```

The reply payload:

```json
{"messaging_product": "whatsapp", "recipient_type": "individual", "to": "<sender>",
 "type": "text", "text": {"preview_url": false, "body": "<the same sentence the phone line speaks>"}}
```

### 12.3 `agent/message_service.py`

```python
class MessageSession:     # stands where CallSession stands, for one message
    channel = "whatsapp"; history_token = None; echo = None; pending, lang, audit ...
    async def deliver_text(self, text) -> bool

class MessageChannel:
    def accept(self, inbound) -> int          # de-duplicate, schedule, answer the webhook fast
    async def answer(self, msg)               # one sender at a time, in order
    async def _answer(self, msg):
        store.note_inbound(...)
        snap = store.load(sender, "whatsapp")
        lang = snap.lang or detect_from_text(msg.text)      # script of the first message
        session = MessageSession(...)
        if not text:   speak_to(channel.text_only)
        else:
            if resumed from the other channel: speak_to(channel.resumed)
            await voice.run_text_turn(session, msg.text)    # <- THE PHONE LINE'S TURN
        finally: store.save(...); audit.end(END_MESSAGE_TURN)
    async def _deliver(self, session, text):
        simulator -> keep; not configured -> record only;
        window closed -> _outside_window(); else whatsapp.send_text(...)
    async def _outside_window(self, session):
        approved template -> send it once; none -> send nothing; both audited
```

---

## 13. How a conversation works now

```text
PATIENT (WhatsApp): "CBC-র দাম কত"
  └─ Meta ─► POST /webhook/whatsapp  (signature checked, de-duplicated, 200 at once)
      └─ MessageChannel.answer ─► main_pcm.run_text_turn
          └─ fast path / cache / LLM ─► clinic-api /tests/search ─► test_rate_reply
              └─ _speak ─► _deliver_written ─► whatsapp.send_text
PATIENT sees: the sentence the phone line would have spoken

PATIENT: "সেনের কাছে ২০ তারিখ ১৮:১৫ বুক করুন"   -> "নামটা বলবেন?"          (state saved)
PATIENT: "Iti Sen"                               -> "ফোন নম্বর দেবেন?"      (state saved)
PATIENT: "9000000101"                            -> booking confirmed        (state cleared)

PATIENT: "আমার কী বুকিং আছে"
  └─ my_bookings ─► _start_history_verification ─► _history_guard
      └─ channel_is_private("whatsapp") = (False, "text_channel")
          └─ refusal audited at clinic-api; no challenge, no record read
PATIENT sees: "…cannot share your records or bookings in a message. Please call us…counter."

PHONE ─► MESSAGE
  Caller verifies (DOB) on a call, starts booking, hangs up at "which time?"
    └─ ws_audio finally ─► _save_for_other_channels (verified number only)
  Same patient writes "18:15" on WhatsApp
    └─ "Let us continue the booking you started earlier." + booking confirmed

MESSAGE ─► PHONE
  Patient starts booking by message, stops at "which time?"
  Rings, asks for their history, verifies
    └─ history read out ─► _resume_from_other_channel
        └─ "Let us continue the booking you started earlier. Which time?"
  "18:15" ─► booked; name and number filled from the verified record

LATE DELIVERY (Meta redelivers a 25-hour-old message)
  └─ window closed ─► the answer is NOT sent free-form
      └─ approved reply_expired template sent once — or nothing, if none approved
```

---

## 14. What did NOT change

| Area | How it is known |
|---|---|
| Voice agent HTTP API (both transports) | `api-contract` PASS — snapshots match |
| clinic-api | no file touched |
| Voice behaviour | a `CallSession` has no `channel`; every branch added is skipped. 562 of 564 existing tests pass; the 2 failures fail identically at HEAD. |
| `main_pcm.py` | regenerated; generator verified the reasoning half byte-identical; `build` PASS |
| Golden set | no pinned sentence changed |
| Dependencies | none added |

---

## 15. Tests — 42 in `tests/test_message_channel.py`, 6 more in `tests/test_multilingual.py`

The network is faked in exactly one place (the httpx transport the WhatsApp client posts through). The turn loop, slot filling, the privacy guard, the reply templates, the store and the audit are the real code. Test data is from the approved fictional set.

| Area | Tests |
|---|---|
| **Same answers, shared layer** (6) | a message gets the phone line's answer word for word · the message service imports none of the reasoning modules · a booking built over three messages · language follows the first message's script, then stays · a number the patient types wins over the one they write from · the phone line never fills a number from the channel |
| **Not loosened** (8) | history and bookings never written into a message (×2) · the bench weakening does not open the channel · the phone line is judged exactly as before · the refusal names the phone line and the counter (bn/hi/en) · nothing this channel sends needs a smartphone |
| **State across channels** (8) | verified call → finished by message · message → picked up by a verified call · an unverified call leaves nothing · nothing of verification is ever stored · the number is not the key · state expires · a list of doctors does not cross · without a shared key state does not follow the patient |
| **Official provider & template compliance** (13) | unsigned / wrongly signed webhook refused · a signed webhook answered once however often it arrives · registration handshake · reply payload is free-form with no preview · outside the window only the approved template · outside the window with none approved, nothing · a recent message reopens the window · a voice note is asked to be typed · a refused send is recorded undelivered · the client never raises on a network failure · the webhook parser is tolerant · the number the clinic keeps · the simulator is bench-only |
| **Three languages** (6) | a message is answered in the language it was written in (bn/hi/en, on a pod with only a Bengali speech model) · one English word does not flip a Bengali conversation · the patient can ask for another language by message · the phone line stays Bengali-only on that same pod |
| **Spoken language** (6, in `test_multilingual.py`) | the spoken language is identified from the audio · probed once per call, not per turn · nothing is probed by default · a pod with one checkpoint never probes · the script strategy believes the transcript · a wrong-language decode scores below a right one |
| **Audit** (1) | each message leaves a complete record, transport `whatsapp`, answer delivered as text |

```bash
python -m pytest tests/test_message_channel.py -v
```

**Whole suite:** 612 tests — 610 passed, 2 failed. The 2 are the pre-existing golden-set entry `fp-abstain-unknown-doctor`, which fails identically on a clean checkout of HEAD.

A note on `tests/test_audio_quality.py::test_a_good_turn_between_two_bad_ones_prevents_the_keypad`: it failed once in one `-x` run (a Windows temp-file error). Run 10 times in isolation with this change it passed 10/10; it is the flaky test already documented in `written_confirmation_implementation.md`.

---

## 16. Quality gate (`bash scripts/gate.sh --full`)

| Check | Result |
|---|---|
| compile, lint, dead-code, static-analysis, secrets, prod-credentials, debug-code, unexpected-files | **PASS** — no new findings |
| typecheck | **PASS** — one new error was found in `agent/whatsapp.py` and fixed at the cause (a narrowing mypy could not see), not suppressed |
| api-contract | **PASS** — no route added to any contracted app |
| build | **PASS** — `main_pcm.py` in sync |
| phi-in-code, phi-in-logs, approved-test-data | **PASS** |
| gate-protection | **PASS** — no test weakened, no suppression added |
| integration, safety-policy, phi-boundary, multilingual, handoff, telephony, pstn-8khz, dependency-audit | **PASS** |
| format | FAIL — the same **42** pre-existing unformatted files as before this story (reformat on hold by owner decision); every new file is formatted |
| golden-set, unit-tests, escalation-abstention | FAIL — the same pre-existing golden entry; a code owner must regenerate `tests/golden/golden_set.json` |

**This story adds no new gate failure.** The gate is red for the same four reasons it was red before it started.

---

## 17. Running it

**Bench (no WhatsApp account), from the repository root:**

```bash
MESSAGE_SERVICE_SIMULATOR=1 VOICE_AGENT_STATE_KEY=bench-shared-key VOICE_AGENT_STATE_DB=/tmp/state.db uvicorn agent.message_service:app --port 8090
```

```bash
curl -s -X POST localhost:8090/api/messages/simulate -H 'Content-Type: application/json' -d '{"sender": "919000000101", "text": "CBC-র দাম কত"}'
```

**Live:** set `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN` (and `VOICE_AGENT_STATE_KEY` identical to the voice agent's), expose `/webhook/whatsapp` over public HTTPS, and register that URL in the Meta app with the verify token.

**What needs the GPU pod:** the LLM (Ollama/Qwen) for anything the fast path cannot resolve, and bge-m3 for the intent cache — the same as the phone line. **No ASR, no TTS, no VAD** is loaded by the message service. clinic-api must be running.

---

## 18. Risks and limitations

1. **History and bookings cannot be asked by message.** By design (`CLAUDE.md`). A patient may read this as "not the same answers"; the refusal says why and where to go instead.
2. **Phone → message crosses only after verification.** No caller-ID on this transport, so a call can vouch only for a number proved at the history challenge. Most bookers never verify, so today this path is rare. With telephony caller-ID it becomes general.
3. **PHI at rest for up to 30 minutes.** A half-done booking in `conversation_state.db` holds the name and number the patient gave for it. Keys are HMAC'd, rows expire, but expired rows are ignored rather than deleted, and the file is not encrypted.
4. **Shared key is essential.** If `VOICE_AGENT_STATE_KEY` differs (or is unset) between the two processes, state silently stays per-process — `/api/health` reports `shared_between_channels: false`.
5. **Message service imports the voice module.** It needs the voice agent's Python environment (torch, torchaudio, NeMo packages installed) even though it loads no model.
6. **Its routes are not contract-checked.** `scripts/gate_contracts.py` knows three apps; this is a fourth. Adding it is a gate-configuration change for a code owner.
7. **Not wired into `deploy/`.** `start_all.sh` does not start it, and on vast.ai a public HTTPS webhook URL needs a tunnel or reverse proxy (ports are fixed at instance creation).
8. **Meta's API not exercised live.** Payloads follow Meta's documented Cloud API shapes; the Graph version is configurable. No real number, token or template yet.
9. **`reply_expired` template not approved yet.** Until `WHATSAPP_TEMPLATE_REPLY_EXPIRED` is set, a reply outside the window sends nothing (audited).
10. **De-duplication before processing.** If the service dies mid-turn, Meta's redelivery is ignored; the unfinished audit record is closed at the next start.
11. **Spoken wording in text.** Some prompts say "বলবেন" ("will you say"); they read naturally enough but were written for speech.
12. **WhatsApp needs a WhatsApp-capable phone.** It is an extra channel; the phone line and the counter remain the no-smartphone paths.
13. **Hindi and English SPEECH still needs checkpoints on the pod.** The probe and the registry are ready, but `ai4bharat` Hindi/English IndicConformer checkpoints are not on this pod, so today a caller speaking Hindi is still heard by a Bengali model. This is infrastructure, not code.
14. **The probe costs N decodes on the first turn** of a call, on the GPU that also holds the LLM and TTS. That is why it is off by default and runs once per call.
15. **A message can be answered in a language the phone line cannot hear.** On a pod with only the Bengali speech model, a Hindi message is answered in Hindi, but the same patient ringing the clinic is answered in Bengali. That is deliberate — writing needs no speech model — but it is a difference between the channels until the Hindi and English checkpoints are on the pod.
16. **Hindi and English wording unreviewed**, as with earlier stories.

## 19. To-dos

1. Code owner: regenerate `tests/golden/golden_set.json` (pre-existing, unrelated) so the gate can go green.
2. Code owner: add the message service to `scripts/gate_contracts.py` with a snapshot (gate-config change).
3. Register the business number, generate a system-user token, set the app secret and verify token; register the webhook URL over HTTPS.
4. Submit `REPLY_EXPIRED_BODY` (bn/hi/en, category UTILITY) for approval; set `WHATSAPP_TEMPLATE_REPLY_EXPIRED`.
5. Set the same `VOICE_AGENT_STATE_KEY` in the voice agent and the message service.
6. Add the service to `deploy/start_all.sh` / `status.sh`, on its own port.
7. Live test on the GPU pod: a price question, a three-message booking, phone → message and message → phone hand-overs, a history request refused.
8. Purge expired rows from `conversation_state.db` on a schedule.
9. When telephony caller-ID exists, use it to save and resume state for unverified callers' own bookings.
10. Native-speaker review of the Hindi and English `channel.*` sentences.
