# Noisy Environment Fix — Implementation

> **Story:** "As a caller from a noisy place, I want to be understood, so that I do not have to find somewhere quiet first."

> ⚠️ **GPU / real noisy-audio validation: PENDING.** Everything in section 7 marked *Pending* has **not** been run. See section 9 for the exact steps.

---

## 1. Problem

**The audio clip sent to speech recognition started in the wrong place.**

It started where the **agent's previous sentence ended**, not where the **caller started speaking**. Everything in between was sent to the recogniser as well.

In a quiet room that in-between region is silence, so nothing breaks. In a noisy room it is traffic, a fan, or a crowd — and it grows with every second the caller spends thinking before replying.

A 3-second question could arrive at IndicConformer as a 15-second clip that is 80% background noise.

**The root cause was not noise filtering.** The information needed to fix this already existed: `TurnDetector.poll()` calculated exactly where speech started, used it for one small internal length check, and then **threw it away** — because the object it returns had no field to carry it.

---

## 2. Old Code 🔴

### File: `agent/vad_stream.py`

**Class:** `TurnResult`

```python
@dataclasses.dataclass
class TurnResult:
    utterance_end_s: float | None   # relative to the slice; None => still talking
    had_any_speech: bool
```

**Function:** `TurnDetector.poll()`

```python
first_speech_start = float(spans[0]["start"])     # <-- CALCULATED
last_speech_end = float(spans[-1]["end"])

if duration_s >= self.max_utterance_s:
    return TurnResult(utterance_end_s=last_speech_end, had_any_speech=True)
                                          # ^-- start is NOT returned

if (last_speech_end - first_speech_start) < self.min_speech_s:   # only use
    return TurnResult(utterance_end_s=None, had_any_speech=True)

...
if trailing_silence >= self.silence_confirm_s:
    return TurnResult(utterance_end_s=last_speech_end, had_any_speech=True)
                                          # ^-- start is NOT returned here either
```

### File: `main.py` (and its generated twin `main_pcm.py`)

**Function:** `_turn_poll_loop()`

```python
absolute_end_s = session.processed_until_s + result.utterance_end_s
session.utt_seq += 1
utterance_wav = await _slice_utterance(
    session, session.processed_until_s, absolute_end_s, session.utt_seq,
)                #  ^^^^^^^^^^^^^^^^^^^^^^^^^^ clip starts HERE
session.processed_until_s = absolute_end_s
```

### What the old code did

1. VAD found where speech started and ended inside the unprocessed tail.
2. It returned **only the end**.
3. The poll loop cut the clip from `session.processed_until_s` — the point where the **previous turn** finished — through to the detected end.
4. That clip went to ASR.

### Why it failed with background noise

| | Quiet room | Noisy room |
|---|---|---|
| Caller thinks for 12s before replying | 12s of **silence** prepended to the clip | 12s of **market noise** prepended |
| Effect on the recogniser | none — silence is harmless | the caller's speech is buried |
| Gets worse when… | never | the caller hesitates, or the agent replies slowly |

Three reasons this survived until now:

1. **It cannot fail in testing.** A test caller replies instantly in a quiet room, so "end of previous turn" and "start of speech" are the same instant, and the bug is invisible.
2. **It punishes exactly the wrong people.** Hesitation is more likely in a noisy place, and hesitation is what grows the noise prefix.
3. **It is silent.** Nothing logs it. The clip is just longer and the transcript is just worse.

There is a second, compounding effect: this codebase notes at `agent/vad_stream.py` that *"IndicConformer's RNNT decoder silently drops content on long unsegmented audio."* So the padded clip is not only noisier, it is also long enough to hit a known decoder weakness.

---

## 3. New Code 🟢

### File: `agent/vad_stream.py`

**Class:** `TurnResult` — one new field

```python
@dataclasses.dataclass
class TurnResult:
    utterance_end_s: float | None
    had_any_speech: bool
    utterance_start_s: float = 0.0     # NEW
```

**Function:** `TurnDetector.poll()` — the two returns that report a **completed turn**

```python
# force-cut path (the path a noisy call actually takes)
if duration_s >= self.max_utterance_s:
    return TurnResult(utterance_end_s=last_speech_end, had_any_speech=True,
                      utterance_start_s=first_speech_start)

# normal end-of-turn path
if trailing_silence >= self.silence_confirm_s:
    return TurnResult(utterance_end_s=last_speech_end, had_any_speech=True,
                      utterance_start_s=first_speech_start)
```

The four "still talking / no speech" returns are **unchanged** — they keep the `0.0` default.

### File: `main.py` (regenerated into `main_pcm.py`)

**Function:** `_turn_poll_loop()`

```python
absolute_end_s = session.processed_until_s + result.utterance_end_s

absolute_start_s = max(
    session.processed_until_s,
    session.processed_until_s + result.utterance_start_s - UTTERANCE_PAD_S,
)
session.utt_seq += 1
utterance_wav = await _slice_utterance(
    session, absolute_start_s, absolute_end_s, session.utt_seq,
)
session.processed_until_s = absolute_end_s     # unchanged — still the END
```

### What changed

| | Before | After |
|---|---|---|
| `TurnResult` fields | 2 | 3 (one new, with a safe default) |
| Returns that report the onset | 0 | 2 (both completed-turn paths) |
| Clip start | `session.processed_until_s` | `absolute_start_s` |
| Clip end | unchanged | unchanged |
| `processed_until_s` advance | to the end | to the end (unchanged) |
| Thresholds retuned | — | **none** |

### How the new logic works

1. The detector now reports **both** ends of the utterance, not just the end.
2. The poll loop converts the onset from "seconds into this slice" to "seconds into the whole call".
3. It subtracts `UTTERANCE_PAD_S` so the first syllable is not clipped.
4. `max(...)` clamps the result so the clip can never reach back into audio a previous turn already consumed.
5. `processed_until_s` still advances to the **end**, so the skipped noise is *consumed*, not left for the next poll to re-examine. This keeps the call timeline anchored to real time.

**Worked example** — caller hesitates 12s in a noisy room, then speaks for 3s:

| | Clip start | Clip end | Length | Content |
|---|---|---|---|---|
| 🔴 Old | 0.00s | 15.15s | **15.15s** | 12s noise + 3s speech |
| 🟢 New | 11.85s | 15.15s | **3.30s** | 0.15s noise + 3s speech |

Same speech, **4.6× less audio**, noise prefix gone.

### Why this fixes the problem

The recogniser now receives (almost) only what the caller actually said. The speech-to-noise ratio of the clip improves directly, with no filtering, no new model, and no threshold guessing — purely by not sending audio that was never speech.

---

## 4. Variables / Parameters 🟡

### `utterance_start_s` — NEW field on `TurnResult`

| | |
|---|---|
| **Meaning** | Where the caller's first syllable is, in seconds from the start of the audio slice passed to `poll()`. |
| **Why we need it** | It is the only thing that tells the caller of `poll()` where the speech begins. Without it there is no way to separate the caller's words from the room noise before them. It was already being computed — this just returns it. |
| **Too high** (later than the real onset) | The clip starts *inside* the utterance and the first word is cut off — the recogniser hears "…াক্তার সেন" instead of "ডাক্তার সেন". Trades a noise problem for a truncation problem. |
| **Too low** (earlier than the real onset) | More background noise is included. At `0.0` the behaviour is exactly the old, broken behaviour. |
| **Default** | `0.0`, chosen deliberately: it means "cut from the start of the slice", i.e. precisely what the old code did. That keeps the four unchanged return paths correct and makes the field backward-compatible. |

### `absolute_start_s` — NEW local in `_turn_poll_loop`

| | |
|---|---|
| **Meaning** | The same onset, converted from "seconds into this slice" into "seconds into the whole call" — the coordinate space `_slice_utterance` and the audio buffer use. |
| **Why we need it** | `processed_until_s` and the call buffer are indexed on one call-long timeline; the VAD result is relative to the slice it was handed. This is the conversion between the two, plus the safety clamp. |
| **Too high** | Clip starts too late; the caller's opening word is lost. |
| **Too low** | Clip starts too early; more noise is included. It **cannot** go below `processed_until_s` because of the `max()` clamp — that is what the clamp is for. |

### `UTTERANCE_PAD_S = 0.15` — EXISTING constant, now applied at both ends

| | |
|---|---|
| **Meaning** | Padding in seconds around the detected speech boundary. It already padded the **end**; it now also pads the **start**. |
| **Why we need it** | Voice detectors mark the boundary slightly *inside* the speech. Cutting exactly on the boundary shaves the first consonant. The pad buys that margin back. |
| **Too high** (e.g. `1.0`) | Defeats the fix — a full second of noise returns to every clip. |
| **Too low** (e.g. `0.0`) | The caller's first phoneme is clipped. |
| **Changed?** | **No.** Value untouched; only reused at the near end so the clip is padded consistently on both sides. |

### Thresholds deliberately NOT changed

`silence_confirm_s` (0.8), `tail_guard_s` (0.4), `min_speech_s` (0.3), `max_utterance_s` (20.0) and Silero's own detection threshold are all **untouched**. This fix does not retune the detector — it stops discarding one of the detector's existing outputs.

---

## 5. Why This Fix

In plain words: **the system used to send the recogniser everything it had heard since it last spoke. Now it sends only what the caller actually said.**

For someone calling from a noisy place this helps three ways:

- **Less noise reaches the recogniser.** The proportion of the clip that is actually speech goes up, with no filtering involved.
- **The clip gets shorter.** IndicConformer's RNNT decoder is documented in this codebase as silently dropping content on long audio. Shorter clips are more reliable clips.
- **It stops punishing hesitation.** Before, the longer someone paused to think, the worse their recognition got. Now the pause is discarded no matter how long it was.

It is also the *smallest* correct fix: nothing retuned, no model added, no threshold guessed. One already-computed value is now returned and used.

**Quiet-room callers are provably unaffected.** When someone replies immediately the onset is `0.0`, the clamp puts the start back exactly where it was, and the clip is byte-identical. There is a dedicated test for this.

---

## 6. Tests

**New file:** `tests/test_noisy_turn_boundaries.py` — 12 tests. The repository had **no test suite before**; this adds one.

| # | Test | What it checks | Result |
|---|---|---|---|
| 1 | `test_turnresult_defaults_to_zero_start` | Default is `0.0`, i.e. old behaviour, so unchanged paths stay correct | ✅ PASS |
| 2 | `test_poll_reports_speech_onset_on_normal_turn_end` | Normal turn end returns onset (12.0) with the end (15.0) | ✅ PASS |
| 3 | `test_poll_reports_speech_onset_on_force_cut` | The 20s force-cut path returns it too — **the path a noisy call actually takes** | ✅ PASS |
| 4 | `test_poll_still_talking_leaves_start_at_default` | Incomplete turns do not report a bogus onset | ✅ PASS |
| 5 | `test_poll_no_speech_returns_nothing` | Silence produces no turn | ✅ PASS |
| 6 | `test_clip_starts_at_speech_not_at_previous_turn` | **THE REGRESSION TEST.** 12s noise + 3s speech → clip starts at 11.85s, not 0.0 | ✅ PASS |
| 7 | `test_clip_duration_collapses_to_the_speech` | Clip shrinks 15.15s → 3.30s (asserts < ¼ of old length) | ✅ PASS |
| 8 | `test_processed_until_still_advances_to_the_end` | Marker still moves to 15.0 — skipped noise is consumed, not re-examined | ✅ PASS |
| 9 | `test_clip_start_never_precedes_consumed_audio` | The clamp works — onset 0.05s with pad 0.15s does not rewind | ✅ PASS |
| 10 | `test_quiet_caller_who_replies_instantly_is_unchanged` | **No regression** for quiet-room callers — identical clip | ✅ PASS |
| 11 | `test_force_cut_path_also_trims_the_lead_in` | End-to-end force-cut path through the real poll loop | ✅ PASS |
| 12 | `test_both_transports_slice_from_the_speech_onset` | `main.py` and generated `main_pcm.py` have not drifted apart | ✅ PASS |

Tests 6–11 drive the **real** `_turn_poll_loop` with a real `CallSession` and a real `PcmCallBuffer`. Only the VAD model's opinion about where speech is, is scripted; every threshold and branch in `poll()` is the real one. `nemo`, `omegaconf` and `torchaudio` are stubbed at import so the suite runs on a laptop with no GPU.

### Result

```
python -m pytest tests/ -v

12 passed, 4 warnings in 3.68s
```

- **Passed: 12**
- **Failed: 0**
- **Warnings: 4** — all pre-existing FastAPI `on_event is deprecated` notices from `main_pcm.py:173` and `:214` (the startup/shutdown handlers). Unrelated to this change and present before it.

### These tests were proven to catch the bug

A test that passes both before and after proves nothing, so the fix was reverted in two stages and the suite re-run:

| Reverted | Result |
|---|---|
| `main.py` + `main_pcm.py` only | **4 failed**, 8 passed — the consumer-side tests |
| `agent/vad_stream.py` only | **5 failed**, 7 passed — the detector-side tests |
| Nothing (final state) | **12 passed** |

Both halves of the change are pinned by tests. Files were restored from backup and MD5-verified identical afterwards.

---

## 7. Validation

### ✅ Completed now

| Check | Result |
|---|---|
| **Unit tests** | 12/12 pass; verified to fail on the pre-fix code (see above) |
| **Existing test suite** | There was none; this change introduces the first one |
| **Syntax / compile** | `python -m py_compile` clean on all 4 touched files |
| **Static review** — `TurnResult` construction | All 6 construction sites use keyword arguments, so adding a defaulted field is safe. No construction exists outside `agent/vad_stream.py` |
| **Static review** — read safety | `utterance_start_s` is read only in `main.py` / `main_pcm.py`, both guarded by the existing `if result.utterance_end_s is None: continue` |
| **Static review** — ordering | `first_speech_start` is assigned before both modified return sites |
| **Static review** — bounds | `start ≤ end` always holds (`spans[0].start ≤ spans[-1].end`), so the clip can never invert |
| **Static review** — units | Both offsets are in **seconds**, so the sample-rate resampling inside `poll()` cannot corrupt them |
| **Transport consistency** | `main_pcm.py` regenerated via `tools/make_pcm_variant.py`; the generator independently verified the shared half is byte-identical |
| **Diff minimality** | 68 changed lines total across 3 files, of which ~55 are explanatory comments — roughly **13 functional lines** |
| **Quiet-path preservation** | Test 10 asserts a byte-identical clip for a caller who replies immediately |
| **CodeRabbit** | **Not available** — no `coderabbit` CLI installed in this environment. Manual static review performed instead (rows above) |

### Performance / latency impact

**Neutral to positive, by construction — reasoned, not measured on GPU:**

- Added work is two float operations and one `max()`, once per turn. Not measurable.
- The clip handed to ASR is **shorter**, so `torchaudio.save` writes less and IndicConformer decodes less (3.30s vs 15.15s in the worked example). ASR should get **faster**, not slower.
- No extra model, no extra pass over the audio, no new dependency.
- VAD poll cost and memory are unchanged — `poll()` still receives exactly the same tail.

### ⏳ Pending — NOT RUN, NOT PASSED

| Check | Status |
|---|---|
| Real microphone test from a noisy place | **PENDING** |
| GPU / STT end-to-end test (real Bengali speech + real noise → transcript) | **PENDING** |
| Measured transcript accuracy improvement | **PENDING** |
| Real Silero VAD onset accuracy on real noisy audio | **PENDING** |
| Measured latency on GPU | **PENDING** |
| Production-like concurrent-caller test | **PENDING** |

The improvement is currently demonstrated **arithmetically and by unit test**. The clip provably contains less noise; **how much the transcript improves is unmeasured.** Section 9 has the plan.

> A local sanity run with real Silero VAD was attempted and **abandoned** — it needed `torchaudio`, which is not installed locally, and the GPU box was lost mid-session. No sanity result is claimed.

### Remaining issues — identified, deliberately NOT fixed here

Found during inspection, left alone to keep this change minimal:

1. **The turn may never end in continuous noise.** When noise reads as speech, trailing silence never reaches `silence_confirm_s` (0.8s), so the turn only ends at the 20s force-cut. This fix makes that path much better (it trims the lead-in) but does not prevent it. The caller can still wait up to 20s for a reply.
2. **The VAD threshold does not adapt to the room.** `get_speech_timestamps` is called with no `threshold`, so Silero's fixed default applies regardless of how loud the background is.
3. **`decoder_agreement` is computed and never read.** CTC-vs-RNNT disagreement is a free confidence signal, and low agreement is exactly what a noisy clip produces. It could trigger "sorry, could you repeat that?" instead of acting on a garbled transcript.
4. **No audio normalisation before ASR.** A quiet caller in a loud room arrives at whatever level the microphone produced.
5. **`autoGainControl` is not specified** in `getUserMedia`, so the browser default applies — and AGC raises the noise floor during pauses, working against the detector.

Items 1–3 are the natural next steps and are independent of one another.

---

## 8. Files Changed

| File | Why it changed |
|---|---|
| `agent/vad_stream.py` | **The root cause.** Added `utterance_start_s` to `TurnResult` and populated it from the already-computed `first_speech_start` on both completed-turn return paths. Plus a short docstring note on `poll()`. |
| `main.py` | **The consumer.** `_turn_poll_loop` now computes `absolute_start_s` and passes it to `_slice_utterance` instead of `session.processed_until_s`. |
| `main_pcm.py` | **Generated — never hand-edited.** Produced by `python tools/make_pcm_variant.py` from `main.py`. Regenerated so the raw-PCM transport carries the identical fix. |
| `tests/test_noisy_turn_boundaries.py` | **New.** 12 tests pinning the fix, including a no-regression test for quiet-room callers and a guard against the two transports drifting apart. |
| `noisy_environment_implementation.md` | **New.** This document. |

**Not changed:** no thresholds, no VAD tuning, no ASR code, no client-side audio capture, no architecture, no unrelated functionality. Nothing was committed — the work sits uncommitted on the `dev-chakravardhan` branch.

---

## 9. GPU Validation Plan

Run these when the GPU environment is back. **Nothing below has been run.**

### Step 1 — Start the services

```bash
export VOICE_AGENT_ENV_FILE=env.vast.sh
bash /workspace/kolkata-care-voice-agent/deploy/start_all.sh
bash /workspace/kolkata-care-voice-agent/deploy/status.sh
```

Confirm all five are up: Ollama (11434), TTS (8002), clinic-api (8081), agent (10100), PCM agent (10200).

**Deploy this fix first** — copy `agent/vad_stream.py`, `main.py`, `main_pcm.py` to `/workspace/kolkata-care-voice-agent/` and restart the two agent processes. Verify with:

```bash
grep -c absolute_start_s /workspace/kolkata-care-voice-agent/main.py \
                         /workspace/kolkata-care-voice-agent/main_pcm.py
```

### Step 2 — Get a real Bengali speech sample

```bash
curl -s -X POST http://localhost:8002/synthesize \
  -H 'Content-Type: application/json' \
  -d '{"text":"ডাক্তার সেন কি আজ আছেন","lang":"bn"}' -o /tmp/speech_22k.wav
ffmpeg -y -i /tmp/speech_22k.wav -ac 1 -ar 16000 -c:a pcm_s16le /tmp/speech.wav
```

Better still, record a **real human** saying it — TTS speech is unnaturally clean and will flatter the result.

### Step 3 — Add realistic background noise

Build the shape a real noisy call has: `[12s noise][speech + noise][2s noise]`, at **20, 15, 10, 5 and 0 dB SNR**. Use real recordings (traffic, market babble, ceiling fan) rather than white noise — Silero responds differently to broadband hiss than to speech-shaped babble.

The 12-second lead-in is the point of the test: it is what the old code sent to ASR and the new code does not.

### Step 4 — Send through the complete pipeline

Stream over the real WebSocket at real-time pace, as a browser does:

```
ws://localhost:10200/ws/audio     # PCM transport
```

Send the `hello` frame, then 250ms PCM chunks, and reply `playback_done` after each audio frame received.

### Step 5 — Compare old vs new

Run each SNR condition twice:
- **NEW:** the deployed fix.
- **OLD:** revert just the slice call to `session.processed_until_s` and restart.

Same audio, same seed, both directions.

### Step 6 — Check VAD boundaries

Log `utterance_start_s` and `utterance_end_s` per turn and compare against the known onset (12.0s):
- Onset error should stay small (target < 0.3s) as SNR drops.
- Record how often the **20s force-cut** fires instead of a normal turn end — if that is not near zero, remaining issue #1 is biting and needs its own fix.

### Step 7 — Check STT transcript accuracy

Compare each transcript to the known text `ডাক্তার সেন কি আজ আছেন`:
- Character/word overlap per SNR, old vs new.
- Empty-transcript rate per SNR, old vs new.
- Mean `decoder_agreement` per SNR (should fall as SNR falls — that would confirm it is usable as a confidence gate, remaining issue #3).

**This is the number that decides whether the fix worked.**

### Step 8 — Measure latency

- Time from speech end to audio reply, per SNR.
- ASR time per turn, old vs new (expect **new to be faster** — shorter clips).
- Confirm no regression at 20 dB SNR, where the fix should be a no-op.

### Step 9 — Record the results

Fill this in and append it to section 7 under *Completed*:

| SNR | Onset err (new) | Force-cuts | Clip len old→new | Overlap old→new | Empty old→new | ASR time old→new |
|---|---|---|---|---|---|---|
| 20 dB | | | | | | |
| 15 dB | | | | | | |
| 10 dB | | | | | | |
| 5 dB | | | | | | |
| 0 dB | | | | | | |

**Pass criteria:**
- Transcript overlap **improves or is unchanged** at every SNR (never worse).
- No regression at 20 dB — the quiet-room path must stay identical.
- Onset error < 0.3s down to 10 dB.
- ASR time per turn does not increase.

**If overlap does not improve at low SNR:** the lead-in trim alone is insufficient and remaining issues #1 and #2 (turn never ending, and the non-adaptive threshold) are the next things to fix.
