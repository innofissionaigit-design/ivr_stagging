# Audio Quality Floor, Clarification Ladder & Noisy-Bucket Reporting — Implementation

> **Requirement:** "Noise suppression and level normalisation run at the edge, and below a
> measured audio-quality floor the agent takes the clarification path rather than answering
> confidently. After two failed turns the keypad is offered. Accuracy in the noisy bucket is
> reported separately."

> ⚠️ **GPU / real noisy-audio validation: PENDING.** No GPU, microphone, real Bengali audio,
> STT accuracy, latency or real-world noisy-bucket accuracy result is claimed anywhere in this
> document. Section 9 lists exactly what still has to be run.

> **Relationship to the earlier work:** `noisy_environment_implementation.md` fixed *where* the
> ASR clip is cut (`utterance_start_s`). That was segmentation. This document covers *what is
> inside* the clip and *whether the agent should trust it* — a different problem, described in
> section 1.

---

## 1. Problem

### 1.1 What the previous fix did and did not solve

The `utterance_start_s` fix changed the clip's **start timestamp** from "where the agent's
previous sentence ended" to "where the caller's first syllable is". It removed a lead-in of pure
room noise whose length equalled the caller's hesitation.

What it did **not** touch is the noise that is *simultaneous with the speech*. Cutting at the
right timestamp cannot separate a fan from a vowel when both occupy the same three seconds.

| | Answers the question | Status before this change |
|---|---|---|
| Segmentation | "Which samples do we send?" | ✅ Solved by `utterance_start_s` |
| Suppression | "What is mixed into those samples?" | ❌ Browser only, unmeasurable |
| Level | "How loud does it arrive?" | ❌ Absent |
| Confidence | "Should we trust the result at all?" | ❌ Absent |

A caller in a market who speaks **immediately** — no hesitation — got *zero* benefit from the
previous fix. Their old and new clips are byte-identical. That caller is squarely inside the
population this requirement is about.

### 1.2 The failure this change exists to prevent

`_dispatch_turn` transcribed every clip and acted on whatever came back. Downstream — the fast
path, the semantic cache, the LLM, slot filling — cannot tell a transcript of speech from a
transcript of a bus. There was no point in the pipeline where the audio's condition was known.

So a garbled transcript was acted on **with full confidence**: it booked the wrong slot, quoted
the wrong test, and the caller only found out later. An admitted failure is strictly better than
a confident wrong answer, and the system had no way to admit one.

### 1.3 Three things were missing

1. A **measurable** noise suppression stage (the browser's is invisible to us and differs per
   browser).
2. **Level control**, so a quiet caller in a loud room does not arrive at whatever level the
   microphone happened to produce.
3. A **number** describing the clip, and a threshold to compare it against.

Everything else in the requirement — the ladder, the keypad, the bucketed metrics — depends on
(3) existing first.

---

## 2. Old Code 🔴 → New Code 🟢

### 2.1 Edge capture constraints

**Files:** `static/index.html`, `static/pcm/index.html` — function `startCall()`

```js
// 🔴 OLD
stream = await navigator.mediaDevices.getUserMedia({
  audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
});
```

```js
// 🟢 NEW
stream = await navigator.mediaDevices.getUserMedia({
  audio: {
    channelCount: 1,
    echoCancellation: true,
    noiseSuppression: true,
    autoGainControl: false,
  },
});
```

**Why:** `autoGainControl` was never specified, so the browser default (on) applied. AGC rides
the gain continuously, which **lifts the noise floor during the pauses between words** — and
that inter-word quiet is exactly the signal `TurnDetector` uses to decide the caller has
finished. AGC was actively working against turn detection.

Turning it off is only safe because levelling moved server-side, where it is applied as **one
constant gain to an already-segmented clip** and therefore cannot move a turn boundary.

This closes known issue #5 in `noisy_environment_implementation.md` section 7.

---

### 2.2 The turn dispatch — the quality gate

**File:** `main.py` (and its generated twin `main_pcm.py`) — function `_dispatch_turn()`

```python
# 🔴 OLD
async def _dispatch_turn(session: CallSession, utterance_wav: str):
    async with session.dispatch_lock:
        try:
            async with asr_gate:
                asr_result = await _asr.transcribe_utterance(utterance_wav)
        finally:
            with contextlib.suppress(OSError):
                os.remove(utterance_wav)

        text = asr_result.text.strip()
        if not text:
            logger.info("[%s] ASR returned empty text", session.call_id)
            await _speak(session, "দুঃখিত, শুনতে পাইনি। আবার বলবেন?",
                         fallback_reason="asr_empty")
            return
        await session.send_json("User", text)
```

```python
# 🟢 NEW
async def _dispatch_turn(session: CallSession, utterance_wav: str,
                         text_override: str | None = None):
    async with session.dispatch_lock:
        quality = None

        if text_override is not None:
            text = text_override.strip()
            if not text:
                return
        else:
            try:
                try:
                    conditioned = await asyncio.to_thread(condition_wav_file, utterance_wav)
                    quality = conditioned.quality
                    logger.info(
                        "[%s] clip: %.2fs snr=%.1fdB speech=%.0f%% gain=%+.1fdB %s%s", ...)
                except Exception as e:
                    # FAIL OPEN -- see below
                    logger.warning("[%s] conditioning failed (%s) -- sending raw clip", ...)

                if quality is not None and not quality.usable:
                    METRICS.record_turn(quality, success=False)
                    await _clarify_or_offer_keypad(session, quality,
                                                   reason=quality.reasons[0])
                    return                       # <-- ASR is NEVER called

                async with asr_gate:
                    asr_result = await _asr.transcribe_utterance(utterance_wav)
            finally:
                with contextlib.suppress(OSError):
                    os.remove(utterance_wav)

            text = asr_result.text.strip()
            if not text:
                logger.info("[%s] ASR returned empty text", session.call_id)
                if quality is not None:
                    METRICS.record_turn(quality, success=False)
                await _clarify_or_offer_keypad(session, quality, reason="asr_empty")
                return

            if quality is not None:
                METRICS.record_turn(quality, success=True)
            session.failures.record_success()

        await session.send_json("User", text)
```

**Four decisions, each with a reason:**

1. **The gate sits before `asr_gate`, not after.** Downstream cannot distinguish speech from
   traffic once it is text, so the decision has to be made here, on the audio, while that
   distinction still exists. Secondary benefit: a rejected clip costs **zero GPU inference**,
   which matters most at peak — exactly when noisy callers are most likely.

2. **Conditioning runs in `asyncio.to_thread`.** It is numpy over the whole clip — tens of
   milliseconds of CPU that would otherwise block every other call's socket on this event loop.

3. **It fails OPEN, not closed.** A bug in the conditioner must not degrade the whole service to
   "sorry, say again" on every turn. On exception, `quality` stays `None`, the gate is skipped,
   and the raw clip goes to ASR — exactly the behaviour that shipped before this stage existed.

4. **Empty ASR now routes to the same ladder.** The old code had a hardcoded
   `"দুঃখিত, শুনতে পাইনি"` that reset nothing and counted nothing. A clip that passes the floor
   and still yields no text is a failed turn like any other — the caller was not understood, and
   *which stage* failed to understand them is our problem, not theirs.

**`text_override`** is new. It skips audio entirely and is how a keypad digit enters this
function. The alternative — a second dispatcher for DTMF — would have to re-implement the fast
path, the cache, slot filling and `_continue_pending`, and would drift out of step with the
spoken path the first time either was touched.

---

### 2.3 The clarification ladder

**File:** `main.py` — **new function** `_clarify_or_offer_keypad()`

```python
# 🔴 OLD — no equivalent. Two blind, unconnected fallbacks existed:
#   main.py  empty ASR      -> "দুঃখিত, শুনতে পাইনি। আবার বলবেন?"
#   main.py  intent unclear -> "দুঃখিত, বুঝতে পারিনি। আবার একটু বলবেন?"
# Neither counted anything, neither escalated, neither reset.
```

```python
# 🟢 NEW
async def _clarify_or_offer_keypad(session: CallSession, quality=None,
                                   reason: str = "low_quality"):
    action = session.failures.record_failure()

    if action == ACTION_KEYPAD:
        METRICS.record_keypad_offer()
        logger.info("[%s] %d consecutive failed turns (%s) -- offering keypad", ...)
        await session.send_json("_keypad", "on")
        await _speak(session, KEYPAD_PROMPT_BN, fallback_reason="keypad_offer")
        return

    METRICS.record_clarification()
    logger.info("[%s] turn unusable (%s) -- asking again (failure %d/%d)", ...)
    await _speak(session, CLARIFY_PROMPT_BN, fallback_reason=reason)
```

**Why one funnel:** every way of failing to understand the caller — a clip below the floor, and
empty ASR — routes through here, so the counter tracks **real consecutive failures** rather than
one particular failure mode. Two different silent failures in a row are still two failures to
the caller.

**Why two rungs, not repetition:** asking the same question twice in the same market is not a
retry, it is a loop. The room has not got quieter between the two attempts, so nothing about
repeating the request makes the next clip any better. Rung 1 names the actual problem and asks
for the one thing the caller *can* change (volume, distance to the phone). Rung 2 stops asking
for speech at all and moves to a channel the room cannot corrupt.

**Why the control frame carries no display text:** `send_json("_keypad", "on")` puts the keys on
screen; the spoken line that follows is the one the caller reads in the log. Sending text in
both would print it twice.

---

### 2.4 Keypad input

**File:** `main.py` — function `_handle_control()`

```python
# 🔴 OLD -- one message type in the whole control channel
    if msg.get("type") == "playback_done":
        session.release_gate()
```

```python
# 🟢 NEW
    if msg.get("type") == "playback_done":
        session.release_gate()
    elif msg.get("type") == "dtmf":
        asyncio.create_task(_handle_keypad_digit(session, str(msg.get("digit", ""))))
```

**Why `create_task` and not `await`:** `_handle_keypad_digit` runs a full turn — LLM, clinic API,
TTS — and `_handle_control` is the socket's *receive* path. Awaiting it here would stop reading
audio, and the caller may well keep talking while the keypad turn is in flight.

**File:** `main.py` — **new function** `_handle_keypad_digit()`

```python
# 🟢 NEW
async def _handle_keypad_digit(session: CallSession, digit: str):
    text = KEYPAD_MENU_BN.get(digit.strip())
    if text is None:
        logger.info("[%s] keypad: ignoring unmapped key %r", session.call_id, digit)
        return

    METRICS.record_keypad_entry()
    session.failures.record_success()
    logger.info("[%s] keypad: %r -> %r", session.call_id, digit, text)
    await _dispatch_turn(session, "", text_override=text)
```

**Why digits map to TEXT, not to intent ids:** the digit then enters the *same* reasoning path as
speech — fast path, semantic cache, LLM, slot filling, `_continue_pending`. Mapping to an intent
id would need a second, parallel dispatcher that would drift out of step with the spoken one.

**Why a digit counts as a success:** the fallback did its job and the caller got through. The
next isolated misheard turn deserves an ordinary clarification, not the keypad again.

---

### 2.5 Session state

**File:** `main.py` — `CallSession.__init__`

```python
# 🔴 OLD -- no turn-level failure state anywhere.
# The only counter was pending["retries"] inside _continue_pending, which is
# BOOKING-SLOT state: it resets on every slot change and its escape hatch is
# "give a fresh LLM classification a chance", not a channel change.
```

```python
# 🟢 NEW
        self.failures = TurnFailureTracker()
```

**Why per-session:** the ladder is per caller. The person in the market who has now failed twice
needs the keypad; the person on the next line who failed once does not.

---

### 2.6 Reporting

**File:** `main.py` — `stats()`, plus **new endpoint** `quality_stats()`

```python
# 🔴 OLD
@app.get("/api/stats")
async def stats():
    return {
        "fast_path": _fast_path.snapshot() if _fast_path else None,
        "intent_cache": _intent_cache.snapshot() if _intent_cache else None,
        "tts_cache": _tts.snapshot() if _tts else None,
    }
```

```python
# 🟢 NEW
@app.get("/api/stats")
async def stats():
    return {
        "fast_path": _fast_path.snapshot() if _fast_path else None,
        "intent_cache": _intent_cache.snapshot() if _intent_cache else None,
        "tts_cache": _tts.snapshot() if _tts else None,
        "audio_quality": METRICS.snapshot(),
    }


@app.get("/api/quality")
async def quality_stats():
    return METRICS.snapshot()
```

**Why its own endpoint as well:** this is the number the noisy-environment work is answerable to,
and it should be fetchable without pulling cache internals along with it.

---

### 2.7 The PCM transport

`main_pcm.py` is **generated**, never hand-edited. It was regenerated with:

```bash
python tools/make_pcm_variant.py
```

The generator re-verified that the reasoning half (`_resolve_intent` → `_resync_after_playback`)
is byte-identical between the two files. All of this change lands inside that region, so it
propagates automatically. `test_both_transports_carry_the_quality_gate` fails if anyone forgets
to regenerate — without it, PCM callers would silently keep the old confident-on-noise
behaviour.

---

## 3. New Files

### 3.1 `agent/audio_quality.py` — DSP and the floor (516 lines)

Pure numpy, plus `soundfile` for the file round-trip. **No new dependency, no model, no torch, no
CUDA.** Both are already pinned in `requirements.txt`. This is a requirement, not an accident:
the floor has to be computable *before* deciding whether to spend a GPU inference, and it has to
be unit-testable on a laptop.

| Function / class | Purpose |
|---|---|
| `QualityConfig` | Frozen dataclass, every tunable in one place |
| `QualityConfig.from_env()` | Reads all 17 env overrides; a bad value logs and falls back |
| `CONFIG` | Module-level instance the service uses |
| `AudioQuality` | Frozen result: `snr_db`, `speech_ratio`, `clipping_ratio`, `rms_dbfs`, `duration_s`, `usable`, `reasons` |
| `AudioQuality.noisy` | Bucket membership — `snr_db <= noisy_snr_db` |
| `AudioQuality.bucket()` | `"noisy"` / `"clean"` |
| `AudioQuality.as_dict()` | JSON-safe, for logs and `/api/quality` |
| `assess()` | **The metric.** Raw clip → `AudioQuality` |
| `_frame_dbfs()` | Per-frame level, 25ms frames at 10ms hop |
| `suppress_noise()` | **Spectral gating** |
| `_stft()` / `_istft()` | Framing and weighted overlap-add |
| `normalize_level()` | **One constant gain**, returns `(audio, gain_db)` |
| `Conditioned` | `audio`, `sample_rate`, `quality`, `gain_db`, `denoised` |
| `condition()` | The pipeline: assess → suppress → normalise |
| `condition_wav_file()` | Read, condition, **write back in place**, report raw quality |
| `rms_dbfs()` / `_to_dbfs()` / `_rms()` | dB helpers, `-120.0` floor instead of `-inf` |

### 3.2 `agent/quality_metrics.py` — ladder and accounting (224 lines)

| Function / class | Purpose |
|---|---|
| `TurnFailureTracker` | **Per call.** The two-rung ladder |
| `.record_failure()` | Returns `ACTION_CLARIFY` or `ACTION_KEYPAD` |
| `.record_success()` | Resets the streak **and** the keypad latch |
| `.max_retries` | Reads `max_clarify_retries` from config |
| `QualityMetrics` | **Process-wide.** Bucketed turn accounting, lock-protected |
| `.record_turn(quality, success)` | Files the turn in the noisy or clean bucket |
| `.record_clarification()` / `.record_keypad_offer()` / `.record_keypad_entry()` | Counters |
| `.snapshot()` | The reporting dict |
| `.reset()` | Tests only |
| `METRICS` | Module-level singleton |
| `ACTION_CLARIFY` / `ACTION_KEYPAD` | Ladder return values |

---

## 4. Variables / Configuration 🟡

Every value is env-overridable. **None has been validated against real noisy Bengali audio.**
They are reasoned starting points, chosen to be safe in the direction that matters: an over-eager
clarification costs one extra question, an under-eager one books the wrong appointment.

### 4.1 Noise suppression

| Variable | Env var | Default | What it does and why |
|---|---|---|---|
| `denoise_enabled` | `VOICE_AGENT_DENOISE` | `True` | Kill switch. Also the A/B lever for GPU validation |
| `frame_len` | `VOICE_AGENT_DENOISE_FRAME` | `512` | 32ms @16k — long enough to resolve a hum, short enough not to smear a plosive |
| `hop` | `VOICE_AGENT_DENOISE_HOP` | `128` | 4× overlap |
| `noise_percentile` | `VOICE_AGENT_DENOISE_PERCENTILE` | `15.0` | Per-bin noise estimate = this percentile **over time**. Speech is intermittent, room noise is not, so the low percentile of a bin *is* the room |
| `oversubtraction` | `VOICE_AGENT_DENOISE_OVERSUB` | `1.5` | Subtract slightly more than the estimate — the percentile is a floor, not a mean |
| `spectral_floor` | `VOICE_AGENT_DENOISE_FLOOR` | `0.08` | Never gate a bin below 8% of its own magnitude. Gating to zero produces **musical noise** — isolated tonal blips that ASR reads as onsets, which is worse than the hum they replaced |

### 4.2 Level normalisation

| Variable | Env var | Default | What it does and why |
|---|---|---|---|
| `normalize_enabled` | `VOICE_AGENT_NORMALIZE` | `True` | Kill switch |
| `target_rms_dbfs` | `VOICE_AGENT_TARGET_DBFS` | `-20.0` | Conventional speech operating level |
| `max_gain_db` | `VOICE_AGENT_MAX_GAIN_DB` | `20.0` | Cap on amplification. Past this a quiet clip is mostly amplified **into its own noise floor** — and would then look well-levelled, defeating the floor that just measured it |
| `max_attenuation_db` | `VOICE_AGENT_MAX_ATTEN_DB` | `12.0` | Cap the other way, so a loud clip is tamed rather than flattened |
| `peak_ceiling_dbfs` | `VOICE_AGENT_PEAK_CEILING_DBFS` | `-1.0` | After gain, back off if any peak would exceed this. Clipping is unrecoverable distortion; quietness is not |

### 4.3 The quality floor

A clip is **unusable if ANY** of these trips. Reasons accumulate rather than short-circuit, so
one log line names every reason the clip failed.

| Variable | Env var | Default | Reason code |
|---|---|---|---|
| `min_snr_db` | `VOICE_AGENT_MIN_SNR_DB` | `8.0` | `low_snr` |
| `min_speech_ratio` | `VOICE_AGENT_MIN_SPEECH_RATIO` | `0.12` | `little_speech` |
| `max_clipping_ratio` | `VOICE_AGENT_MAX_CLIPPING` | `0.02` | `clipped` |
| `min_duration_s` | `VOICE_AGENT_MIN_CLIP_S` | `0.30` | `too_short` |
| `min_rms_dbfs` | `VOICE_AGENT_MIN_RMS_DBFS` | `-50.0` | `too_quiet` |
| *(empty input)* | — | — | `empty` |

### 4.4 Bucketing and retries

| Variable | Env var | Default | What it does and why |
|---|---|---|---|
| `noisy_snr_db` | `VOICE_AGENT_NOISY_SNR_DB` | `15.0` | **At or below** this a turn goes in the noisy bucket. Deliberately *above* `min_snr_db`: the 8–15 dB band is "noisy but still usable", which is the population noisy-bucket accuracy is actually about. If the two were equal, the bucket would only ever contain rejected turns |
| `max_clarify_retries` | `VOICE_AGENT_MAX_CLARIFY_RETRIES` | `2` | Consecutive failures before the keypad replaces another question |

### 4.5 Prompts and the keypad menu

**File:** `main.py`

| Constant | Content |
|---|---|
| `CLARIFY_PROMPT_BN` | "দুঃখিত, আশেপাশে খুব আওয়াজ হচ্ছে। আর একটু জোরে, ফোনের কাছে এসে বলবেন?" |
| `KEYPAD_PROMPT_BN` | "এখনও পরিষ্কার শোনা যাচ্ছে না। কী-প্যাড ব্যবহার করুন — পরীক্ষার রেটের জন্য ১, ডাক্তারের সময়ের জন্য ২, অ্যাপয়েন্টমেন্টের জন্য ৩ টিপুন।" |
| `KEYPAD_MENU_BN` | `{"1": "পরীক্ষার রেট জানতে চাই", "2": "ডাক্তারের সময় জানতে চাই", "3": "অ্যাপয়েন্টমেন্ট বুক করতে চাই"}` |

### 4.6 Counters

**Per call** — `session.failures`:

| Field | Meaning |
|---|---|
| `consecutive_failures` | Drives the ladder. **Reset to 0 by any usable turn** |
| `total_failures` | History for the call. Never escalates anything |
| `keypad_offered` | Latch, cleared on success |

**Process-wide** — `METRICS`, served at `/api/quality`:

```json
{
  "overall_turns": 0,
  "overall_successful_turns": 0,
  "overall_accuracy": null,
  "noisy_bucket":  { "turns": 0, "successful_turns": 0, "accuracy": null, "mean_snr_db": null },
  "clean_bucket":  { "turns": 0, "successful_turns": 0, "accuracy": null, "mean_snr_db": null },
  "rejected_low_quality": 0,
  "clarifications": 0,
  "keypad_offers": 0,
  "keypad_entries": 0,
  "thresholds": { "...": "echoed so a reading can be interpreted later" },
  "accuracy_definition": "turn_completed_with_usable_text"
}
```

**Empty buckets report `null`, not `0.0`.** Zero reads as "we tried and failed every time"; null
is the truth before any noisy caller has rung.

**What "accuracy" means here:** the turn completed without falling into the clarification path —
the clip passed the floor **and** ASR returned non-empty text. It is **not** a claim about
transcript correctness. Word error rate needs reference transcripts and a GPU, and is PENDING.
The `accuracy_definition` field is in the payload so nobody misreads it later.

---

## 5. How It Works

### 5.1 Order of operations — and why it is not optional

```
        assess(RAW)  ──►  suppress_noise  ──►  normalize_level  ──►  ASR
             │
             └──► the number the gate and the metrics both use
```

Quality is measured on the **raw** clip, before suppression and before normalisation.

Measuring afterwards would be measuring this module's own output: spectral gating raises the
apparent SNR *by construction*, and normalisation moves the level to the target *by
construction*. A clip that arrived unusable would score as clean and then be acted on
confidently — recreating the exact failure this whole change exists to prevent.

Pinned by `test_quality_is_measured_before_processing_not_after`.

### 5.2 The SNR estimate

There is no reference signal — nobody hands us the clean speech and the noise separately — so a
true SNR is not computable. The standard substitute is the spread of the clip's own frame-level
distribution:

```
noise floor  = 10th percentile of frame levels   (quiet frames = the room between words)
speech level = 90th percentile of frame levels   (loud frames  = the caller)
snr_db       = speech level - noise floor
```

In a quiet room the percentiles are far apart and the number is large. In a noisy one the
background lifts the 10th percentile toward the 90th and the number collapses — exactly the
condition the agent needs to detect.

**Deliberately not used: an absolute noise-level threshold.** A loud room with a loud caller is
fine; a quiet room with a whisperer is not. Only the ratio tells those apart.

### 5.3 How the noise is estimated for suppression

For each frequency bin, take a low percentile of its magnitude across time. Speech is
intermittent — any given bin is loud only while a phoneme occupies it — whereas a fan, traffic
hum or air-conditioner is present in every frame. The low percentile of a bin is therefore the
room, and the peaks above it are the caller.

That is why it needs no calibration step, and equally why it **cannot** remove a passing shout or
a door slam: those are not stationary, so they never sit in the low percentile.

### 5.4 The ladder

| Event | Action | Counter |
|---|---|---|
| Clip below the floor | rung 1 → `CLARIFY_PROMPT_BN` | `consecutive_failures = 1` |
| Second consecutive failure | rung 2 → `_keypad` frame + `KEYPAD_PROMPT_BN` | `consecutive_failures = 2` |
| Third, fourth… | **stays** on keypad | keeps counting |
| Any usable turn | normal reply | **reset to 0**, latch cleared |
| Keypad digit pressed | normal reply | **reset to 0** |

**Why failures must be consecutive:** failures are counted to detect a caller who is *currently*
not getting through, not to build a permanent record against them. Someone who fails once,
succeeds, then fails again ten turns later is having an ordinary conversation with two bad
moments; escalating them on that second isolated failure would be punishing them for the length
of the call.

**Why rung 2 does not cycle back:** a caller who has failed three times is not helped by being
asked a fourth question.

### 5.5 Why the buckets are never merged

A single "accuracy" over all traffic is dominated by whichever bucket is larger — in practice the
quiet one. A system that works perfectly for quiet callers and fails half the time for noisy ones
reports as "95% accurate" if 90% of callers are quiet, and the number **goes up** if quiet
traffic grows, with nothing having improved for the people who were already struggling.

That is precisely the population this change exists for, so their number is kept separate.
`overall_accuracy` is published *alongside* the split, never instead of it. Pinned by
`test_noisy_accuracy_is_not_diluted_by_clean_traffic`.

---

## 6. Tests

**File:** `tests/test_audio_quality.py` — 64 new tests, 792 lines.

| Group | Tests | What is covered |
|---|---|---|
| A. Noise processing | 8 | Floor lowered; attenuation is **selective**; SNR improves; length/dtype preserved; no-op when too short; disable switch; introduces no clipping; steady-tone limitation pinned |
| B. Level normalisation | 9 | Up to target; down to target; **never clips at any amplitude**; peak ceiling honoured; gain capped; attenuation capped; silence untouched; disable switch; target configurable |
| C. Quality calculation | 11 | SNR ordering; **monotonic across 4 noise levels**; pure noise ≈0; silence; empty; clipping; too-short; speech ratio; duration/level; JSON safety; unbroken-speech limitation |
| D. Threshold decision | 6 | Clean passes; noisy fails; **same audio → opposite verdicts under different configs**; env parsing; bad env falls back to default; reasons accumulate |
| E. Order of operations | 3 | Quality matches raw not processed; conditioned audio hits target; file round-trip |
| F. Retry ladder | 7 | 1st → clarify; 2nd → keypad; further stay keypad; success resets; latch clears; **non-consecutive never escalates**; limit configurable |
| G. Noisy-bucket metrics | 8 | Boundary inclusive; noisy-and-usable coexist; buckets separate; **not diluted by clean traffic**; null vs zero; all counters; required fields present; mean SNR |
| H. End-to-end routing | 12 | **Low-quality clip never reaches ASR**; temp file deleted; good clip resets ladder; two bad → keypad; good-between-bad prevents keypad; empty ASR counts as failure; metrics recorded; keypad digit enters text path; digit resets ladder; unmapped digit ignored; **conditioning failure falls open**; both transports carry the gate |

### 6.1 What these tests deliberately do NOT cover

Whether the thresholds are the **right** numbers, and whether suppression actually improves
IndicConformer's transcripts. Both need real Bengali speech, real background noise and the GPU.

A test asserting "8 dB is the correct floor" would be asserting an assumption, not a fact. These
assert the **shape** of the behaviour — ordering, monotonicity, clamping, routing — which is what
code-level tests can actually establish.

### 6.2 Two real bugs the tests caught

**1. ISTFT edge explosion — a code bug, fixed.**
A hann window starts at exactly 0, so the summed squared window tends to zero at the clip edges.
Dividing by it amplified the first and last samples to roughly **25× full scale** while the
interior stayed correct — silent unless the output level is actually checked. Fixed by
reflect-padding one full frame before the STFT, trimming after the ISTFT, and using a
*relative* normalisation floor rather than an absolute epsilon.

**2. A steady tone is not speech — a test bug, fixed.**
The first version of the suppressor test used a pure sine as "speech" and measured the suppressor
removing it 14 dB harder than noise. That is **correct behaviour on the wrong stimulus**: a
constant tone is stationary, and stationary content is exactly what spectral gating removes.
Replaced with `speech_like()` — bursts at varying pitch with gaps between them — and added
`test_suppression_cannot_remove_a_steady_tone_and_that_is_by_design` to pin the limitation rather
than hide it.

---

## 7. Test Results

```bash
python -m pytest tests/ -v
```

```
76 passed, 4 warnings in 3.46s
```

| Suite | Result |
|---|---|
| `tests/test_audio_quality.py` | **64 passed** (new) |
| `tests/test_noisy_turn_boundaries.py` | **12 passed** (pre-existing, unchanged) |
| `python -m py_compile` on all touched Python files | clean |
| `python tools/make_pcm_variant.py` | regenerated; reasoning half verified byte-identical |

The 4 warnings are pre-existing FastAPI `on_event` deprecations, unrelated to this change.

---

## 8. Known Limitations — identified, deliberately NOT fixed

1. **Non-stationary noise survives.** A passing shout, a door slam or a car horn is not in the
   low percentile of any bin, so spectral gating leaves it alone. Removing it is a different and
   much harder problem.

2. **Unbroken speech collapses the SNR estimate.** When speech fills essentially every frame,
   the 10th and 90th percentiles both land inside the speech and the estimate tends to zero — so
   a perfectly clean clip could be scored unusable. Natural speech is safe (stop closures,
   inter-word gaps, plus the 0.15 s `UTTERANCE_PAD_S` at both ends), but whether real Bengali
   utterance clips ever approach this is a real-speech-statistics question and is **PENDING**.
   Pinned by `test_unbroken_speech_with_no_pauses_collapses_the_snr_estimate`.
   **Do not widen the percentiles without that data** — the current values are the standard ones.

3. **`decoder_agreement` is still computed and never read.** `agent/asr.py` calculates
   CTC-vs-RNNT word agreement on every turn and discards it. Low agreement is exactly what a
   noisy clip produces, making it the cheapest available confidence signal — a natural second
   input to the floor, alongside SNR. Not wired in here to keep this change to one mechanism.

4. **The turn may still never end in continuous noise.** When noise reads as speech, trailing
   silence never reaches `silence_confirm_s`, so the turn only ends at the 20 s force-cut.
   Carried over from `noisy_environment_implementation.md` section 7.

5. **The VAD threshold still does not adapt to the room.** `get_speech_timestamps` is called
   with no `threshold`, so Silero's fixed default applies regardless of how loud the background
   is. Also carried over.

6. **The keypad menu is intent-level only.** Digits cannot answer a slot question mid-booking
   (a date, a phone number). A caller who cannot be heard *during* a booking flow still has to
   speak. Worth revisiting once the menu proves useful.

---

## 9. GPU Validation Plan — PENDING

**Nothing in this section has been run. No GPU, microphone, real-audio, STT, latency or
real-world accuracy result is claimed anywhere in this document.**

| # | Check | Status |
|---|---|---|
| 1 | Real microphone in a noisy environment | **PENDING** |
| 2 | Real Bengali speech + background noise → STT | **PENDING** |
| 3 | Noisy-bucket accuracy | **PENDING** |
| 4 | Real audio-quality behaviour / threshold placement | **PENDING** |
| 5 | GPU latency | **PENDING** |
| 6 | Concurrent callers | **PENDING** |

### Step 1 — Real microphone in a noisy environment
Market, street, ceiling fan, waiting room. Both transports (`/` and `/pcm`). Confirm
`autoGainControl: false` did not make quiet callers worse, and that the keypad appears when it
should and disappears when speech gets through again.

### Step 2 — Real Bengali speech + background noise → STT
Fixed utterance set mixed at controlled SNRs (20 / 15 / 10 / 5 dB). Measure WER with and without
suppression on identical clips.

> **The critical open question:** does spectral gating actually help IndicConformer, or do its
> artefacts hurt more than the hum they replace? Plausible in both directions and only settleable
> empirically. `VOICE_AGENT_DENOISE=off` is the A/B switch.

### Step 3 — Noisy-bucket accuracy
```bash
curl -s localhost:8000/api/quality | jq .noisy_bucket
```
Read `noisy_bucket.accuracy` **on its own**. Confirm `noisy_snr_db=15.0` actually separates the
two populations, and that the 8–15 dB "noisy but usable" band is populated rather than empty — if
it is empty, the bucket only ever describes rejected turns and the threshold needs moving.

### Step 4 — Real audio-quality behaviour
Is `min_snr_db=8.0` the right floor? Find the crossover where WER degrades unacceptably and set
it there. Also measure the **false-reject rate on clean callers** — an over-eager floor is its own
failure mode — and check limitation #2 above against real clips.

### Step 5 — GPU latency
Added CPU cost of conditioning per turn (numpy STFT over the clip), and end-to-end turn latency.
Expected net *negative* because rejected clips skip inference entirely — but that is reasoning,
not measurement.

### Step 6 — Concurrent callers
`tools/bench_transport.py` at 5 / 10 / `MAX_CONCURRENT_CALLS`. Conditioning runs in the default
thread pool alongside `_model_lock` (VAD) and `_infer_lock` (ASR); watch for pool starvation.

### Also still open from the earlier fix
Real Silero onset accuracy on noisy audio, and whether `UTTERANCE_PAD_S = 0.15` is enough lead-in
on real speech.

---

## 10. Files Changed

| File | Change | Why |
|---|---|---|
| `agent/audio_quality.py` | **new**, 516 lines | Suppression, normalisation, the quality metric, and all config |
| `agent/quality_metrics.py` | **new**, 224 lines | The two-rung ladder and bucketed accounting |
| `tests/test_audio_quality.py` | **new**, 792 lines | 64 tests across 8 groups |
| `main.py` | +301 / −21 | Quality gate in `_dispatch_turn`; `_clarify_or_offer_keypad`; `_handle_keypad_digit`; DTMF in `_handle_control`; `/api/quality`; prompts; `CallSession.failures` |
| `main_pcm.py` | +301 / −21 | **Generated — never hand-edited.** `python tools/make_pcm_variant.py` |
| `static/index.html` | +54 / −1 | `autoGainControl: false`; keypad UI |
| `static/pcm/index.html` | +54 / −1 | Same |
| `audio_quality_implementation.md` | **new** | This document |

**Not changed:** the ASR model or its decoding strategy, the VAD thresholds, `UTTERANCE_PAD_S`,
the client-side capture format or sample rate, the booking flow, the semantic cache, the fast
path, or any clinic-API behaviour.

**No new dependencies.** `numpy` was already in use via `agent/pcm_buffer.py`; `soundfile==0.12.1`
was already pinned in `requirements.txt`.

**Nothing committed** — the work sits uncommitted on the `dev-chakravardhan` branch.

---

## 11. Note on the earlier document

`noisy_environment_implementation.md` is now **stale in two ways** and was deliberately left
untouched by this change:

1. It describes `agent/vad_stream.py` as changing only for `utterance_start_s` "plus a short
   docstring note". The file also contains a `threading.Lock` serialising Silero inference, and
   a ~30-line `THREAD SAFETY` docstring, neither of which that document mentions.
2. Its section 7 "Diff minimality" row claims *"68 changed lines total across 3 files… roughly 13
   functional lines"*. `agent/vad_stream.py` alone is +70 / −6.

Neither affects the correctness of the segmentation fix; both would mislead a reviewer.
