# Kolkata Care Diagnostics -- Voice Agent (Web Prototype)

Bengali phone-style voice agent: test rates, doctor availability, and
appointment booking, spoken over a browser WebSocket. Callers never touch
telephony or a VPN -- this is a bench prototype reachable from a RunPod
HTTP proxy URL in any browser.

This is a companion service to **voice-to-rx-repo** on the same account,
not a replacement or a from-scratch design. It shares that project's
already-debugged IndicConformer (ASR) and Ollama/Qwen2.5 (LLM) stack
outright, and adds only what that project never needed: real-time turn
detection on a live stream, and speaking back.

---

## What changed from the original blueprint, and why

The pasted plan (vLLM + native tool-calling + MediaRecorder chunks decoded
independently) was internally reasonable but untested on this account's
actual infrastructure, and voice-to-rx-repo already has hard evidence
about what breaks. Corrections below, each backed by either a web check or
this account's own deployed code.

| # | Blueprint said | Changed to | Why |
|---|---|---|---|
| 1 | vLLM serving Qwen2.5-7B fp16 at `--gpu-memory-utilization 0.55` | **Ollama, `qwen2.5:7b` (Q4_K_M GGUF, 4.68GB)** -- already running on the account's pod | fp16 Qwen2.5-7B needs **~17GB VRAM** ([spheron.network measurement](https://www.spheron.network/tools/gpu-recommender/Qwen/Qwen2.5-7B-Instruct/)) -- more than the stated 0.55×24GB=13.2GB budget, *before* IndicConformer and TTS get a share of the same card. The Q4 GGUF already deployed sidesteps this entirely and is proven working. |
| 2 | Native tool-calling via `--enable-auto-tool-choice --tool-call-parser hermes` | **JSON-mode + strict schema + code-side dispatch** (`agent/llm.py`, `agent/reply_templates.py`) | The flags themselves are correct for Qwen2.5 on vLLM ([vLLM docs](https://docs.vllm.ai/en/stable/features/tool_calling/)) but unproven on this stack. voicerx/extract.py already hardened the JSON-mode pattern against a **reproduced hallucination** (a loose prompt turned a garbled fragment into "Naloxone", a real wrong drug). The model here is never allowed to state a price or confirmation number itself -- see `reply_templates.py`'s docstring. |
| 3 | "Send audio to IndicConformer" (unspecified integration) | **Reuse `voicerx.asr.ASRNode` directly**, via `PYTHONPATH` | Mainline NeMo cannot load this checkpoint at all (`KeyError: 'dir'`), and RNNT's default decode strategy silently returns empty text on real audio. Both fixes are already in `voicerx/asr.py`. Reimplementing risks reintroducing either bug from scratch. |
| 4 | MediaRecorder chunks sent and implicitly decoded per-chunk | **Chunks appended to one growing buffer, whole buffer re-decoded via ffmpeg each poll** (`main.py:_decode_to_wav`) | Confirmed against this account's own `server.py`: "MediaRecorder only puts the container header in the FIRST chunk... running ffmpeg on chunk 2 alone fails." This is a real, already-hit bug, not a hypothetical. |
| 5 | Silero VAD, unspecified mode | **Re-run `get_speech_timestamps()` per turn, not per file** (`agent/vad_stream.py`) | This account's `server.py` already hit and fixed the adjacent bug: re-running VAD over a whole growing file "orphaned any segment straddling the processed boundary... a real 3-minute recording yielded 2 segments instead of 37." Resetting the buffer after every completed turn sidesteps that class of bug rather than re-deriving the fix. |
| 6 | 2 tools (`get_doctor_availability`, `get_test_rate`) | **3 tools** -- added `book_appointment` | Section 1 of the original brief lists "Appointment Booking: validating slots and logging records into PostgreSQL" as a core capability, but no tool implemented it. Added with the fields that capability implies (doctor, date, slot, patient, phone). |
| 7 | RunPod HTTP proxy assumed to just work for WebSocket | Confirmed, with a caveat | RunPod's proxy does support WS via the `/ws` path on the HTTP proxy URL; if long-idle connections get dropped in practice, the fallback is TCP port exposure instead (`docs.runpod.io/pods/configuration/expose-ports`). Build the reconnect path either way -- don't assume the happy case. |
| 8 | (not addressed) | **Flagged: NeMo currently fails to import on the live pod** | `voice-to-rx-repo/HANDOFF.md` records this as a known, unresolved issue ("No module named 'nemo'", `PYTHONPATH` not persisting across restarts). This new service depends on the identical import. **Fix that first** -- `setup_addon.sh` checks for it and refuses to proceed silently past it. |

## The clinic data service (`clinic-api/`)

Implements the exact 3-endpoint contract `agent/tools_client.py` expects,
in **FastAPI + PostgreSQL** rather than the Java/Spring Boot originally
sketched -- same REST contract, no JVM/Maven toolchain to add to an
otherwise all-Python pod. Swap it for the real hospital backend later
without touching `main.py` or `agent/tools_client.py` at all; only this
service's queries would change.

Seeded with fictional dummy data for bench testing: **8 departments, 32
doctors (4 each), a rotating weekly chamber schedule per doctor, and 34
lab tests with realistic Kolkata pricing.** See `clinic-api/seed.py` for
the exact data.

```bash
cd clinic-api
bash setup_db.sh          # installs Postgres, creates db/user, seeds data
python3 -m uvicorn main:app --host 0.0.0.0 --port 8080
```

### History disclosed only after verification

Past tests are never read out on the strength of the phone number alone --
the handset is shared, so the number says which record to LOOK AT and nothing
about who is holding the phone. Verification is knowledge-based (a PIN set at
the counter, or a date of birth); **an SMS OTP is deliberately not used**,
because the code goes to the same shared handset the caller is holding.

Failures are indistinguishable to the caller -- a wrong PIN, an unknown number
and a patient with no factor on file all get one sentence -- so the line cannot
be used to find out whether somebody attends this clinic. Attempts are counted
per patient, so redialling does not refill the budget, and every attempt lands
in `disclosure_audit`.

Disclosure is also blocked when the room is not private: `agent/privacy.py`
uses the speakerphone classification `echo_guard.py` already computes, and
treats an unclassified path as unsafe -- the one place this codebase
deliberately disagrees with `reporting_path()`. See
`history_verification_implementation.md`.

### Every flow completes without a smartphone

Payment and report collection now exist as flows, and both complete at the
counter -- no link, no QR, no app, no portal. `tests/test_no_smartphone.py`
walks **every** caller-facing string in every language and fails if one tells
a caller to tap, scan or download anything, so the guarantee holds for flows
that do not exist yet. Both branches survive clinic-api being unreachable:
*how* you pay is clinic policy, not a database row.

### Bengali, Hindi and English

The reply strings live in `agent/i18n.py` (62 keys x 3 languages) and the
language machinery in `agent/language.py`. **The default is Bengali-only and
that is not a placeholder** -- the ASR checkpoint on this pod is
`indicconformer_stt_bn_*`, and `language.enabled()` refuses to advertise a
language whose ASR checkpoint is unset, so configuration cannot claim a
capability the models do not have. See
`no_smartphone_multilingual_implementation.md` for what turning Hindi on
actually requires.

### Written patient confirmations

The confirmation number used to reach the patient exactly once, as spoken
Bengali, and then it was gone -- while `reply_templates.py` had always asked
for a phone number on the stated grounds that we would "send the
confirmation" there. clinic-api now sends an SMS through the hospital
gateway on **booking, reschedule and cancellation**, from DLT-registered
templates in `clinic-api/message_templates.py`, and records every delivery
receipt in a ledger (`notification_attempts`). Failures -- including the
silent kind, where the gateway accepts a message and no receipt ever
arrives -- surface at `GET /api/v1/notifications/failures` and in the
`notifications` block of `/api/health`.

Leaving `HOSPITAL_GATEWAY_URL` unset is a supported state: every message is
recorded as `skipped` with the reason attached, and the voice agent stops
promising callers an SMS. See `written_confirmation_implementation.md` for
the full design, the config, and the one-shot schema migration an existing
`clinic.db` needs (`clinic-api/migrate_notifications.py`).

**A real bug caught in local testing, not hypothetical:** the first version
matched a caller's doctor name against the *full* formatted name ("Dr. A.
Sen"), and a query for a doctor who doesn't exist ("Doctor Nobody")
fuzzy-matched to "Dr. N. Roy" at a *higher* similarity score than a
legitimately garbled real name would score against its own doctor. That is
this system's own miniature version of the "Naloxone" bug from
voice-to-rx-repo's README -- confidently answering with the wrong doctor's
real schedule instead of saying "not found". Fixed by matching against the
surname only (`main.py:_find_doctor`, `FUZZY_SURNAME_FLOOR = 0.60`) and
verified with a local SQLite-backed test suite before this was written up.
That threshold is reasoned from a handful of test pairs, not measured
against real call audio -- treat it with the same "LOW confidence, needs
real samples" caution `gate.py`'s own floors are labelled with.

### Every call leaves a complete record

Every call -- answered, failed, abandoned, crashed, or turned away at capacity
-- leaves a record in `call_audit.db`, beside `clinic.db` on the persistent
volume: one row per call, and a numbered event for each step (what ASR heard,
what intent and slots were detected and by which component, each clinic-api
request and the response that **actually** came back, every sentence spoken
and whether the caller heard it, errors, and how the call ended). Events are
written where they happen -- in `main.py` and at the `ClinicToolsClient`
boundary -- and never summarised by the LLM. A PIN or date of birth being
checked, the history token, and the history itself are withheld.

Staff read it at `GET /api/audit/calls` (`?status=failed`, `?status=error`)
and `GET /api/audit/calls/{call_id}`, whose `integrity` block says whether the
record is whole. Set `VOICE_AGENT_AUDIT_TOKEN` to require an `X-Audit-Token`
header. Write failures never reach the caller; they surface under `audit` in
`/api/health`. See `IMPLEMENTATION_CALL_AUDIT.md`.

## What's genuinely new here (no precedent to lean on)

- **Real-time turn detection** (`agent/vad_stream.py`). voicerx's VAD is
  built for an already-finished recording; this polls a live, growing
  buffer instead. Bench-test this against real pauses/fillers before
  trusting the 0.8s silence threshold -- it is a reasonable default, not a
  measured one (contrast with, e.g., `gate.py`'s thresholds, which *are*
  measured against real audio and say so).
- **TTS** (`agent/tts.py`). voicerx never speaks. AI4Bharat's
  [Indic-TTS](https://github.com/AI4Bharat/Indic-TTS) (FastPitch +
  HiFi-GAN, Bengali checkpoint on Bhashini) is confirmed real, but nothing
  in this account has deployed it before -- treat it with the same
  suspicion the rest of this stack earned before it was trusted.
- **The Spring Boot contract** (`agent/tools_client.py`). Nothing in this
  account currently implements `/api/v1/tests/search`,
  `/api/v1/doctors/availability`, or `/api/v1/appointments`. The docstrings
  in that file are the spec to hand to whoever builds it.

## Architecture

```
Browser (MediaRecorder, same capture settings as voicerx's UI)
   │  binary WS frames, ~250ms webm/opus chunks
   ▼
FastAPI /ws/audio  (main.py)
   │
   ├─ growing-buffer ffmpeg decode, every 0.5s   (proven pattern, #4 above)
   ├─ TurnDetector.poll()                         (agent/vad_stream.py)
   │     "has the caller stopped talking?"
   ▼  (on yes)
TurnASR.transcribe_utterance()                    (agent/asr.py -> voicerx.asr.ASRNode)
   ▼
extract_intent()                                  (agent/llm.py -> Ollama, qwen2.5:7b)
   ▼  {intent, slots}
ClinicToolsClient.*()                              (agent/tools_client.py -> Spring Boot)
   ▼  real data
reply_templates.*()                                 -- text ASSEMBLED from the API response
   ▼
TTSClient.synthesize()                             (agent/tts.py -> Indic-TTS)
   ▼  WAV bytes, or a pre-recorded fallback on any failure above
binary WS frame back to the browser
```

## Deployment

On the **same RunPod pod** as voice-to-rx-repo (shares its GPU, its NeMo
install, its Ollama instance):

```bash
git clone <this repo> /workspace/kolkata-care-voice-agent
cd /workspace/kolkata-care-voice-agent
bash setup_addon.sh 2>&1 | tee /workspace/setup_addon.log
```

Then, per the script's final instructions, export `PYTHONPATH`,
`CLINIC_API_BASE`, `TTS_URL`, and start uvicorn on **port 8100** --
voice-to-rx-repo already owns 8000 on this pod. Expose 8100 separately in
the RunPod console.

**Running two GPU services on one pod means IndicConformer is loaded into
VRAM twice** (~1.2GB each, per `HANDOFF.md`'s measured figure) -- once by
voice-to-rx-repo's `server.py`, once by this service. On a 24GB card
that's still comfortable alongside Ollama's 4.68GB and TTS's footprint,
but if that stops being true, the fix is merging both FastAPI apps into
one process sharing one `ASRNode` singleton -- not a day-one requirement
for a prototype, but flag it before scaling call volume.

## Error handling

Every stage fails into a distinct, spoken response -- a caller never gets
dead air. See `main.py:_dispatch_turn` and `agent/tts.py`'s fallback set.

| Failure | Caller hears | Why this response specifically |
|---|---|---|
| ASR returns empty text | "দুঃখিত, শুনতে পাইনি, আবার বলুন" | Distinct from "didn't understand" -- nothing was transcribed at all, most likely silence/noise, not a real question. |
| LLM extraction fails validation after retries | "একটু সমস্যা হচ্ছে, একটু ধরুন" | Never surfaces a malformed/partial answer -- silence-then-apology beats a wrong price. |
| Spring Boot call times out / errors | "এখনই দেখতে পারছি না, স্টাফের কাছে দিচ্ছি" | Deliberately different wording from "not found" (a valid, informative answer) -- an infra failure should not read to the caller as "no such test exists". |
| TTS service itself is down | Same apology text, spoken from a **pre-recorded** file, not synthesized live | If TTS is what's broken, asking it to say so is circular. `tts.py`'s fallback set must be recorded once, ahead of time, and never regenerated live. |
| Caller doesn't give a required slot (e.g. no doctor name) | A specific clarifying question per missing field (`reply_templates.missing_slot_prompt`) | Never calls the Spring Boot API with a null/guessed field. |
| One caller's WS connection throws | Logged, socket closed, temp files cleaned up | A bug in one call must not take down the process for every other line. |

## Known limitations (stated plainly, not hidden)

1. **No barge-in.** While the agent is speaking, the caller's audio is
   still being captured and will be dispatched as the *next* turn once
   they stop -- the caller cannot interrupt mid-reply. Acceptable for a
   prototype; a real deployment should stop TTS playback if new speech is
   detected.
2. **Silero's 0.8s silence threshold is a default, not a measured
   value** -- unlike `gate.py`'s thresholds elsewhere in this account,
   which are calibrated against real samples with stated confidence.
   Budget a bench session with real Bengali speakers before trusting it.
3. **MediaRecorder + `audio/webm` does not work on Safari/iOS at all.**
   The bench client detects this and disables the button rather than
   failing silently, but a real phone-browser deployment reaching iOS
   users needs a different capture path.
4. **TTS is entirely unproven on this account.** Everything else in this
   README leans on code that has already run against real audio; the TTS
   integration has not. Do not treat it as equally trustworthy until it
   has been.
5. **The Spring Boot service does not exist yet.** `tools_client.py`
   documents the contract; nothing here builds that service.

## Sources consulted

- [Qwen2.5-7B-Instruct VRAM requirements (fp16 ≈17GB)](https://www.spheron.network/tools/gpu-recommender/Qwen/Qwen2.5-7B-Instruct/)
- [vLLM tool calling docs -- `--enable-auto-tool-choice --tool-call-parser hermes` for Qwen2.5](https://docs.vllm.ai/en/stable/features/tool_calling/)
- [RunPod: exposing ports / proxy WebSocket behaviour](https://docs.runpod.io/pods/configuration/expose-ports)
- [AI4Bharat IndicConformer, Bengali checkpoint](https://huggingface.co/ai4bharat/indicconformer_stt_bn_hybrid_ctc_rnnt_large)
- [AI4Bharat Indic-TTS (FastPitch + HiFi-GAN)](https://github.com/AI4Bharat/Indic-TTS)
- This account's own `voice-to-rx-repo/{README,MODELS,HANDOFF}.md`, `server.py`, `voicerx/{asr,vad,extract}.py` -- primary source for everything marked "already proven" above.
