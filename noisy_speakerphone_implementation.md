# Speakerphone Support Implementation

> ⚠️ **GPU / real-audio validation: PENDING.** No real speakerphone, microphone,
> Bengali STT, latency or accuracy result is claimed anywhere in this document.
> Section 13 lists exactly what still has to be run.

---

## 1. User Story

> **As a caller using a speakerphone, I want to be understood and to be able to
> interrupt, so that the call works like any other.**

Someone rings the clinic from a phone lying on a desk, or held out flat in a
shared room. Two things are different from a handset call:

1. **The agent's own voice comes back.** The reply plays out of a loudspeaker a
   few inches from an open microphone, so the system hears itself.
2. **The caller does not wait politely.** They are not holding a handset to
   their ear — they talk across the room, and they talk *over* the agent, the
   way people do on every other phone call.

Before this change the system handled (1) by making (2) impossible. It was
**half-duplex**: while the agent spoke, the browser muted the microphone and the
server refused to run turn detection at all. The page said so in Bengali —
*"মাঝপথে থামানো যাবে না"*, you cannot interrupt.

That is a working answer to the echo problem and a complete failure of the
story. This change removes the mute and replaces it with arbitration.

---

## 2. Acceptance Criteria

### AC1 — "Echo cancellation holds on a speakerphone path and barge-in still works within its target."

Two halves that pull against each other, which is what makes this hard:

* **Echo must not become caller speech.** With the microphone open during
  playback, the agent's own reply lands in the same buffer the turn detector
  watches. If it is mistaken for the caller, VAD fires on the agent's voice,
  ASR transcribes the agent, and the agent answers itself — a self-sustaining
  loop, and the original reason the mute existed.
* **Real speech must still get through, quickly.** "Within its target" means a
  budget on detection latency. Detection cannot be faster than the polling
  rate, so the poll cadence during playback has to sit well under the target.
  Here: `barge_in_target_s = 0.30`, polled at `barge_in_poll_s = 0.10`.

Technically this reduces to one decision, made repeatedly while the agent
speaks: **is this microphone audio our own echo, or a second voice?**

### AC2 — "Recognition accuracy on speakerphone audio is measured as its own bucket and the gap against handset audio is stated."

* **Its own bucket** — speakerphone turns are counted separately, not folded
  into a single headline number. A blended figure is dominated by whichever
  population is larger and can improve while speakerphone callers get worse.
* **The gap is stated** — `accuracy_gap = handset_accuracy − speakerphone_accuracy`
  is computed and published, not left for a reader to work out.
* **Classification must not need the caller's cooperation**, because a browser
  cannot reliably report whether the phone is on speaker.

**Status: the plumbing is built and unit-tested; the numbers are PENDING.** No
accuracy figure exists yet, and none is invented here.

---

## 3. Existing / OLD Flow

```
Caller Audio (mic)
    |
    |  [client] MUTED while the agent is speaking  <-- the problem
    v
WebSocket  ->  Call buffer (PcmCallBuffer / WebM file)
    |
    v
_turn_poll_loop
    |
    |  if session.agent_speaking:  continue   <-- turn detection SKIPPED entirely
    v
TurnDetector.poll  (Silero VAD)
    |
    v
_slice_utterance  ->  condition_wav_file (noise floor)  ->  ASR  ->  LLM  ->  TTS
    |
    v
reply audio  ->  client plays it  ->  "playback_done"  ->  gate reopens
    |
    v
_resync_after_playback: processed_until_s jumps to the END of the buffer
                        (everything captured during the reply is discarded)
```

### Where speakerphone echo affected the old flow

It did not — and that was the whole point of the design. The echo was
prevented from ever entering the system, by two gates that both had to hold:

| Gate | Where | What it did |
|---|---|---|
| Client mute | `setMicMuted(true)` in `playAudio()` | Track `enabled = false`, so the mic emitted digital silence for the whole reply |
| Server gate | `if session.agent_speaking: continue` | Turn detection never ran on audio captured during a reply |
| Resync | `_resync_after_playback` | Anything captured anyway was skipped by jumping `processed_until_s` to the buffer end |

The cost was absolute: **there was no path by which caller speech during a
reply could reach the pipeline.** It was not a weak barge-in, it was no
barge-in. The module docstring in `main.py` said so explicitly, and named the
missing piece — "an acoustic echo canceller with the played audio as a
reference signal".

That reference signal is what this change supplies.

---

## 4. OLD CODE 🔴

### 4.1 `static/index.html` and `static/pcm/index.html` — `playAudio()`

```js
function playAudio(arrayBuffer) {
  pendingClips += 1;
  setMicMuted(true);                              // <-- barge-in dies here
  statusEl.classList.add('speaking');
  statusEl.textContent = 'AI বলছে... (মাইক বন্ধ)';   // "AI speaking (mic off)"
  ...
      const source = audioContext.createBufferSource();
      source.buffer = audioBuffer;
      source.connect(audioContext.destination);
      source.onended = resolve;
      source.start();                             // <-- no handle kept
  ...
  })).then(() => {
    pendingClips -= 1;
```

**What was wrong / limited**

* The microphone was off for the entire reply, so nothing the caller said
  during it existed anywhere. No server change could recover it.
* No reference to the playing `AudioBufferSourceNode` was kept, so even if the
  server asked for a stop, there was nothing to stop.
* The status text and the page note told the caller interruption was
  impossible, which was accurate and is now wrong.

**Why it had to change:** the story *is* interruption. Any design that keeps
the mute cannot satisfy it, and the brief explicitly ruled out "disable
TTS/VAD during playback" as the answer.

### 4.2 `main.py` / `main_pcm.py` — `_turn_poll_loop()`

```python
while True:
    await asyncio.sleep(POLL_INTERVAL_S)          # 0.5s, always
    ...
    # --- half-duplex gate: never run turn detection on our own voice ---
    if session.agent_speaking:
        if time.time() < session.speak_deadline:
            continue                              # <-- unconditional skip
        logger.warning("[%s] no playback-done from client, releasing gate on deadline",
                       session.call_id)
        session.release_gate()
```

**What was wrong / limited**

* `continue` is unconditional: during a reply the loop does nothing but wait.
* A 0.5s cadence cannot meet a 0.3s interruption budget even if it did look.
* The only way out of the gate was the client's `playback_done` or a 3s
  deadline — both meaning "the agent finished", never "the caller started".

**Why it had to change:** this is the server half of the mute. Opening the
client microphone without changing this would feed echo straight into VAD.

### 4.3 `main.py` — `CallSession`

```python
def release_gate(self):
    """Playback is over. Don't touch processed_until_s here -- the poll
    loop owns the decoded buffer and does the resync on its next tick."""
    self.agent_speaking = False
    self.resync_pending = True        # -> _resync_after_playback discards
                                      #    everything captured during playback
```

**What was wrong / limited:** there was exactly one way to open the gate, and
it *always* scheduled the resync that throws away audio captured during the
reply. For an interruption that audio **is the interruption**.

**Why it had to change:** reusing `release_gate()` for barge-in would stop the
agent and then discard what stopped it.

### 4.4 `main.py` — `_handle_control()`

```python
if msg.get("type") == "playback_done":
    session.release_gate()
```

**What was wrong / limited:** one message type. No way for the client to say
anything about the audio path, and no way for the server to say "stop
playing".

### 4.5 `agent/quality_metrics.py` — `record_turn()` / `snapshot()`

```python
def record_turn(self, quality: AudioQuality, success: bool) -> None:
    with self._lock:
        bucket = self._noisy if quality.noisy else self._clean
        ...
```

**What was wrong / limited:** turns were bucketed by *noise* only. There was
no notion of an audio *path*, so speakerphone accuracy could not be separated
from handset accuracy even in principle, and no gap could be computed.

---

## 5. NEW CODE 🟢

### 5.1 `agent/echo_guard.py` — NEW FILE (630 lines)

The reference-based arbitration the old docstring said was missing. numpy
only, reusing the STFT helpers already in `agent/audio_quality.py`. **No new
dependency, no model, no GPU.**

**`EchoGuard.assess()` — the decision, in the order the checks run:**

```python
# ONE reference slice, reaching BACK by a full round trip. The echo of
# something played up to echo_max_delay_s ago is still arriving now, so a
# lookup aligned only to the microphone window goes empty the instant
# playback stops -- while the room is still ringing.
ref_recent = self.reference.slice(start_s - self._cfg.echo_max_delay_s, end_s)

# Only the samples we actually played. PlaybackReference zero-fills the gaps
# so the window lines up with the microphone, but averaging those zeros into
# the level would UNDERSTATE how loud the echo may be -- and understating it
# is what lets echo pass as a caller.
ref_active = ref_recent[ref_recent != 0.0]

if ref_active.size == 0:
    # Nothing played recently enough to still be arriving.
    return ... "no_reference"    -> caller

# Record ERL FIRST, even for a window too quiet for anyone to be talking.
# A very quiet return is not an absence of evidence -- it is the strongest
# possible evidence of a handset.
erl = echo_return_loss_db(mic, ref_active)
self._record_erl(erl)                     # bounded + locked

if level_dbfs < self._cfg.barge_in_min_level_dbfs:
    return ... "below_level_floor"

# THE PRIMARY TEST -- the echo cannot be louder than what we played minus
# the room's attenuation, so a mic sitting above that ceiling means a
# second voice is present.
expected_echo_dbfs = rms_dbfs(ref_active) - self.erl_estimate()
excess_db = level_dbfs - expected_echo_dbfs

if excess_db < self._cfg.double_talk_margin_db:
    return ... "within_expected_echo_level"     -> echo

# SECONDARY VETO -- loud enough to be a second voice, but if it still looks
# like what we are playing, believe that and stay quiet. Correlated against
# the WIDENED reference so every lag has a full-length window.
corr, lag = best_lag_correlation(mic, ref_recent, sr, self._cfg.echo_max_delay_s)
if corr >= self._cfg.echo_correlation_threshold:
    return ... "correlates_with_playback"       -> echo

if mic.size < int(self._cfg.barge_in_min_speech_s * sr):
    return ... "too_brief"                      -> neither

return ... "double_talk"                        -> BARGE-IN
```

> **This is the post-review form.** The first version looked up the reference
> aligned to the microphone window only. That went empty the moment playback
> stopped, took the `no_reference` branch, and classified the agent's own
> decaying echo as the caller — making the agent interrupt itself, which is
> exactly the loop the old half-duplex gate prevented. See §12.

**What it does, and why this solves the problem**

* **Level is the primary signal, not correlation.** The first version used
  waveform correlation and the tests measured it failing: a room delays and
  low-passes the echo, collapsing correlation to **~0.30**, while an unrelated
  speaker over a lag search reaches **~0.23**. No threshold fits between those.
  Spectrogram correlation was no better. Level is arithmetic — the echo can
  never exceed `reference − attenuation` — so it can be reasoned about and
  tested exactly. Correlation survives only as a veto.
* **Uncertainty resolves to "echo".** A false barge-in truncates a reply the
  caller then never hears; a missed one costs them a repeat. The first is
  worse, so every ambiguous branch leaves the agent speaking.
* **ERL doubles as the path classifier**, so no client cooperation is needed.

**`PlaybackReference`** stores what was played against the call timeline and
zero-fills gaps, so a reference slice always lines up sample-for-sample with a
microphone slice over the same interval — silence in the reference is a true
statement, not missing data.

### 5.2 `main.py` / `main_pcm.py` — `_check_barge_in()`, NEW

```python
async def _check_barge_in(session: CallSession) -> bool:
    if not ECHO_CFG.barge_in_enabled:
        return False

    tail = await _recent_mic_tail(session, ECHO_CFG.barge_in_window_s)
    if tail is None:
        return False
    samples, sr = tail

    now_s = session.call_time_s()
    verdict = await asyncio.to_thread(
        session.echo.assess, samples, now_s - ECHO_CFG.barge_in_window_s, sr)

    if not verdict.is_barge_in:
        return False

    session.barge_in()
    METRICS.record_barge_in()
    logger.info("[%s] barge-in: %s", session.call_id, verdict.as_dict())

    with contextlib.suppress(Exception):
        await session.send_json("_stop_audio", "on")
    return True
```

`assess` runs in `asyncio.to_thread` — it is numpy over the window and would
otherwise block every other call's socket on the event loop.

### 5.3 `main.py` / `main_pcm.py` — the gate

```python
# Faster cadence WHILE the agent is speaking: barge-in cannot be detected
# sooner than the poll rate, so the interval has to sit well under
# barge_in_target_s. Outside playback the original cadence is unchanged.
# Gated on TAIL_READ_IS_CHEAP: on the WebM transport each tail read
# re-decodes the entire call, so the fast cadence would trade barge-in
# latency for the O(T^2) CPU blowup the PCM transport was built to avoid.
fast = session.agent_speaking and TAIL_READ_IS_CHEAP
await asyncio.sleep(ECHO_CFG.barge_in_poll_s if fast else POLL_INTERVAL_S)
...
# --- full-duplex gate: tell our own echo apart from the caller ---
if session.agent_speaking:
    if await _check_barge_in(session):
        pass          # gate opened by barge_in(); fall through and detect
                      # the caller's turn from the same audio
    elif time.time() < session.speak_deadline:
        continue
    else:
        logger.warning("[%s] no playback-done from client, releasing gate on deadline",
                       session.call_id)
        session.release_gate()
```

**Why this solves it:** the unconditional `continue` becomes a decision.
Echo still stops the loop dead; only a real interruption falls through. The
idle cadence is untouched, so nothing gets busier when no one is speaking.

### 5.4 `main.py` — `CallSession.barge_in()`, NEW

```python
def barge_in(self):
    """The caller talked over the agent. Deliberately NOT release_gate()."""
    now_s = self.call_time_s()

    # Stash what we were playing across the barge-in window -- the clip that
    # follows contains the caller talking OVER this.
    self._pending_echo_ref = self.echo.reference.slice(
        now_s - ECHO_CFG.barge_in_window_s, now_s)

    # Skip forward to the barge-in window, but NO further. Everything before
    # it is the agent's own reply; leaving processed_until_s behind it would
    # hand the turn detector a tail containing our echo -- it would place
    # utterance_start_s at the echo's onset and send ASR a clip of the agent
    # talking, which is exactly the self-answering loop the old half-duplex
    # gate existed to prevent.
    self.processed_until_s = max(
        self.processed_until_s,
        now_s - ECHO_CFG.barge_in_window_s - UTTERANCE_PAD_S,
    )

    self.agent_speaking = False
    self.resync_pending = False      # <-- the point: do NOT discard the
                                     #     interruption
    self.speak_deadline = 0.0
    self.echo.barge_in_count += 1
```

This is the single most important function in the change, and it is where the
new feature meets the existing utterance-boundary fix. `max(...)` never
rewinds into consumed audio, and subtracting `UTTERANCE_PAD_S` keeps the lead-in
the boundary fix already relies on. Within that window, `utterance_start_s`
still refines the true onset exactly as it does for any other turn.

### 5.5 `main.py` — `_speak()`

```python
# Timestamped at the point this clip will actually START playing, which is
# NOT now when a reply is already in flight -- replies queue on the client.
session.echo.note_playback(session.playback_start_s(),
                           pcm_from_wav_bytes(wav, session.echo.sample_rate))

session.hold_gate_for(_wav_duration_s(wav))
await session.send_audio(wav)
```

with the new helper:

```python
def playback_start_s(self) -> float:
    now_s = self.call_time_s()
    if not self.agent_speaking:
        return now_s
    queued_s = (self.speak_deadline - PLAYBACK_GUARD_S) - self.started_at
    return max(now_s, queued_s)
```

**Why:** using send time for a *queued* clip put its reference earlier than
the sound it describes. During that clip's real playback the lookup returned
silence, `no_reference` fired, and the agent's own echo would have been read as
the caller interrupting. (Found in review — see §11.)

### 5.6 `main.py` — `_handle_control()` and echo suppression

```python
elif msg.get("type") == "audio_mode":
    # A hint only. EchoGuard treats it as a starting point and lets the
    # measured Echo Return Loss override it.
    session.echo.declare_path(msg.get("mode"))
```

```python
# OUTSIDE the try below on purpose. That try fails OPEN -- it drops the
# quality floor and sends the raw clip -- so a fault raised inside it would
# silently disarm an unrelated feature rather than surfacing.
echo_ref = getattr(session, "take_echo_reference", lambda: None)()
if echo_ref is not None and ECHO_CFG.echo_suppression_enabled:
    await asyncio.to_thread(_suppress_echo_in_place, utterance_wav, echo_ref)
```

### 5.7 `agent/quality_metrics.py` — the path dimension

```python
def record_turn(self, quality, success, path: str | None = None) -> None:
    with self._lock:
        bucket = self._noisy if quality.noisy else self._clean
        ...
        path_bucket = {"handset": self._handset,
                       "speakerphone": self._speakerphone}.get(path)
        if path_bucket is not None:
            path_bucket.turns += 1
            ...

def _accuracy_gap(self) -> float | None:
    """handset_accuracy - speakerphone_accuracy.

    None whenever either bucket is empty -- a gap computed against a bucket
    with no turns in it is not a small gap, it is no measurement."""
    h, s = self._accuracy(self._handset), self._accuracy(self._speakerphone)
    return None if h is None or s is None else round(h - s, 4)
```

`path` is optional so every existing call site keeps working. Noise and path
are **independent** dimensions — a turn is filed in one of each, never crossed.

### 5.8 Clients

```js
function stopPlayback() {
  bargedIn = true;
  pendingClips = 0;
  try { currentSource && currentSource.stop(); } catch (e) { /* already ended */ }
  currentSource = null;
  ...
}
...
function playAudio(arrayBuffer) {
  pendingClips += 1;
  bargedIn = false;                              // no setMicMuted(true)
  statusEl.textContent = 'AI বলছে... (থামাতে বলুন)';   // "AI speaking (say to stop)"
  ...
      if (bargedIn) { resolve(); return; }       // interrupted before this clip began
      const source = audioContext.createBufferSource();
      currentSource = source;                    // now stoppable
  ...
  })).then(() => {
    // Clamped: stopPlayback() zeroes this while a clip's promise is still in
    // flight, and an unclamped decrement would leave the counter permanently
    // negative.
    pendingClips = Math.max(0, pendingClips - 1);
```

### 5.9 `tools/make_pcm_variant.py`

`main_pcm.py` is generated. A new rule swaps the body of `_recent_mic_tail`
for the PCM version, because the WebM version calls `_decode_to_wav` — which
the generator **deletes** from that file.

---

## 6. Functions Modified

| File | Function/Class | OLD Responsibility | NEW Responsibility | Why Changed |
|------|----------------|-------------------|-------------------|-------------|
| `main.py`, `main_pcm.py` | `_turn_poll_loop` | Skipped all detection while `agent_speaking`; fixed 0.5s cadence | Arbitrates each playback window; polls at 0.10s during playback | The `continue` was the server half of the mute |
| `main.py`, `main_pcm.py` | `_check_barge_in` | *(new)* | Reads the recent mic window, asks `EchoGuard`, stops playback on a real interruption | Somewhere had to make the echo-vs-caller call |
| `main.py`, `main_pcm.py` | `_recent_mic_tail` | *(new)* | Last N seconds of captured audio; **transport-specific**, swapped by the generator | WebM decodes, PCM slices |
| `main.py`, `main_pcm.py` | `_suppress_echo_in_place` | *(new)* | Subtracts the stashed reference out of a barge-in clip, in place | Barge-in clips are the only ones with our audio genuinely mixed in |
| `main.py`, `main_pcm.py` | `_speak` | Send text, synthesize, hold gate, send audio | Also records the reply as the echo reference, timestamped at its real playback start | Barge-in needs a reference to compare against |
| `main.py`, `main_pcm.py` | `_dispatch_turn` | Condition → gate → ASR → intent | Also subtracts the barge-in echo reference and tags the turn with its path bucket | AC2 needs a path on every recorded turn |
| `main.py`, `main_pcm.py` | `_handle_control` | `playback_done` only | Also `audio_mode` (path hint); `dtmf` was added by the earlier change | Client needed a way to declare the path |
| `main.py`, `main_pcm.py` | `CallSession.__init__` | Buffers, gate flags, booking state, failure ladder | Also `started_at`, `echo`, `_pending_echo_ref` | Per-call echo state |
| `main.py`, `main_pcm.py` | `CallSession.barge_in` | *(new)* | Opens the gate WITHOUT the discard-everything resync; moves `processed_until_s` to the window start | `release_gate()` would have thrown away the interruption |
| `main.py`, `main_pcm.py` | `CallSession.call_time_s` | *(new)* | Wall-clock seconds since the call started | One clock for reference timestamps |
| `main.py`, `main_pcm.py` | `CallSession.playback_start_s` | *(new)* | When the next reply will *actually* start playing, accounting for queueing | Queued replies were timestamped wrongly |
| `main.py`, `main_pcm.py` | `CallSession.take_echo_reference` | *(new)* | Hands over the barge-in reference exactly once | Stale playback must not be subtracted from a later turn |
| `main.py` | module docstring | Documented half-duplex as permanent, "the cost is no barge-in" | Documents the echo gate and barge-in arbitration | It described behaviour that no longer exists |
| `agent/quality_metrics.py` | `QualityMetrics.__init__` | Noise buckets only | Adds handset/speakerphone buckets and `barge_ins` | AC2 |
| `agent/quality_metrics.py` | `QualityMetrics.record_turn` | Bucketed by noise | Also bucketed by path (optional arg) | AC2 |
| `agent/quality_metrics.py` | `QualityMetrics.snapshot` | Noise buckets, ladder counters | Adds `path_buckets` with `accuracy_gap`, plus `barge_ins` | AC2 |
| `agent/quality_metrics.py` | `QualityMetrics._accuracy_gap` | *(new)* | `handset − speakerphone`, `None` if either bucket is empty | AC2 says "gap stated" |
| `agent/quality_metrics.py` | `QualityMetrics.record_barge_in` | *(new)* | Counts interruptions | Operational visibility |
| `static/*.html` | `playAudio` | Muted the mic; no handle on the source | Keeps the mic live; stores `currentSource`; skips a clip if already barged in | The mute was the blocker |
| `static/*.html` | `stopPlayback` | *(new)* | Stops the current source, clears the queue, resets status | Server needs a way to cut playback |
| `static/*.html` | `detectAudioMode` | *(new)* | Best-effort path hint from the track label | A starting point for classification |
| `static/*.html` | `ws.onmessage` | `_ping`, `_keypad`, chat | Also `_stop_audio` | Delivery mechanism for the stop |
| `agent/echo_guard.py` | `EchoGuard._record_erl` / `_finite_observations` | *(new)* | Append and snapshot the ERL history under a lock | `assess` runs on a worker thread; readers run on the event loop |
| `agent/echo_guard.py` | `EchoGuard.assess` | Aligned reference; unbounded history | Widened reference, `ref_active` level, locked bounded history | Findings 1, 3, 4, 5 (§12) |
| `agent/echo_guard.py` | `best_lag_correlation` | Accepted overlaps down to 50ms | Requires 60% of the window | Finding 7 — short overlaps manufacture spurious matches |
| `agent/echo_guard.py` | `PlaybackReference.has_audio_in` | Built a full slice, tested for non-zero | Segment-bounds test, no allocation | Finding 6 |
| `tools/make_pcm_variant.py` | `main` | 9 transport rules | 11 — `_recent_mic_tail` body swap and the `TAIL_READ_IS_CHEAP` flip | PCM has no `_decode_to_wav`; and only PCM can afford the fast cadence |

---

## 7. Variables / Parameters Changed

Only variables that actually changed. All are env-overridable; **none has been
validated against a real speakerphone.**

| Variable/Parameter | Old Value/Behavior | New Value/Behavior | Purpose |
|--------------------|--------------------|--------------------|---------|
| `barge_in_enabled` | did not exist (barge-in impossible) | `True`, `VOICE_AGENT_BARGE_IN` | Kill switch back to half-duplex behaviour |
| `barge_in_target_s` | did not exist | `0.30`, `VOICE_AGENT_BARGE_IN_TARGET_S` | The budget the cadence is derived from. **A budget, not a measurement** |
| `barge_in_poll_s` | poll was always `POLL_INTERVAL_S = 0.5` | `0.10` during playback only, `VOICE_AGENT_BARGE_IN_POLL_S` | Detection cannot beat the poll rate; must sit under the target |
| `barge_in_window_s` | did not exist | `0.60`, `VOICE_AGENT_BARGE_IN_WINDOW_S` | How much recent mic audio each check examines |
| `barge_in_min_speech_s` | did not exist | `0.20`, `VOICE_AGENT_BARGE_IN_MIN_SPEECH_S` | A cough must not cut the agent off mid-sentence |
| `barge_in_min_level_dbfs` | did not exist | `-45.0`, `VOICE_AGENT_BARGE_IN_MIN_DBFS` | Below this nobody is talking |
| `double_talk_margin_db` | did not exist | `6.0`, `VOICE_AGENT_DOUBLE_TALK_MARGIN_DB` | **The primary decision.** How far above the expected echo level the mic must sit to be a second voice |
| `bootstrap_erl_db` | did not exist | `6.0`, `VOICE_AGENT_BOOTSTRAP_ERL_DB` | ERL assumed before enough is measured. Deliberately LOW = expect a loud echo = demand a louder caller = fewer false interruptions while learning |
| `echo_correlation_threshold` | did not exist | `0.55`, `VOICE_AGENT_ECHO_CORR` | Secondary **veto** only. Started at 0.30 as the primary test; the tests showed waveform correlation cannot carry that load (§11) |
| `echo_max_delay_s` | did not exist | `0.50`, `VOICE_AGENT_ECHO_MAX_DELAY_S` | Widest round trip the lag search covers (network + client buffering + room + capture) |
| `echo_suppression_enabled` | did not exist | `True`, `VOICE_AGENT_ECHO_SUPPRESS` | A/B lever for the barge-in clip cleanup |
| `echo_oversubtraction` | did not exist | `1.4`, `VOICE_AGENT_ECHO_OVERSUB` | Reference is scaled by this before subtraction |
| `echo_spectral_floor` | did not exist | `0.05`, `VOICE_AGENT_ECHO_FLOOR` | Never gate a bin to zero — musical noise reads to ASR as onsets |
| `speakerphone_erl_db` | did not exist | `20.0`, `VOICE_AGENT_SPEAKERPHONE_ERL_DB` | **The bucket boundary.** ERL at or below this = speakerphone |
| `classification_min_observations` | did not exist | `2`, `VOICE_AGENT_PATH_MIN_OBS` | Never classify a call off one sample |
| `reference_retention_s` | did not exist | `15.0`, `VOICE_AGENT_ECHO_RETENTION_S` | Only the last few seconds can still be echoing |
| `erl_history` | did not exist (list grew unbounded) | `200`, `VOICE_AGENT_ERL_HISTORY` | The median over this is recomputed every poll. Unbounded it cost 3.07 ms/poll after 3000 observations — ~368 ms/s of CPU at 12 calls. Bounded: 0.27 ms/poll, ~33 ms/s |
| `TAIL_READ_IS_CHEAP` (`main.py`) | did not exist | `False` on WebM, `True` on PCM (generator-flipped) | Gates the fast barge-in cadence. On WebM each tail read re-decodes the whole call and spawns ffmpeg |
| `min_window` in `best_lag_correlation` | `0.05 s` flat | `0.6 × mic window` | A large lag against a short reference left a sliver of overlap; two speech signals correlate spuriously over a sliver (measured 0.826) |
| `session.started_at` | did not exist | wall-clock origin of the call | One clock for reference timestamps |
| `session.echo` | did not exist | `EchoGuard()` per call | Owns reference, arbitration, classification |
| `session._pending_echo_ref` | did not exist | reference across the barge-in window, read once | Subtracted from the barge-in clip only |
| `session.resync_pending` (on barge-in) | always `True` after the gate opened | `False` on the barge-in path | The resync discards the interruption |
| `session.processed_until_s` (on barge-in) | untouched by gate changes | advanced to `window_start − UTTERANCE_PAD_S` | Otherwise the next turn transcribes the agent's own echo |
| `pendingClips` (client) | `pendingClips -= 1` | `Math.max(0, pendingClips - 1)` | Went permanently negative after a barge-in, killing `playback_done` for the rest of the call |
| `METRICS.barge_ins` | did not exist | counter | Operational visibility |
| `path_buckets` in `/api/quality` | did not exist | handset + speakerphone + `accuracy_gap` | AC2 |

**Unchanged on purpose:** `POLL_INTERVAL_S` (0.5) outside playback,
`PLAYBACK_GUARD_S`, `RESYNC_REWIND_S`, `UTTERANCE_PAD_S`, every VAD threshold,
every audio-quality threshold, and the ASR model and decoding strategy.

---

## 8. Echo Cancellation

**Where it happens — three layers, in order:**

1. **In the browser.** `echoCancellation: true` in `getUserMedia`, unchanged.
   It holds the true render reference and runs before samples are encoded, so
   it does the heavy lifting. `autoGainControl: false` (set by the earlier
   noisy-environment change) stays off, because AGC lifts the noise floor
   between words and works against turn detection.
2. **In the server's arbitration** — `EchoGuard.assess()`. This does not
   *remove* echo; it decides whether what arrived **is** echo, and refuses to
   let it become a turn.
3. **In the barge-in clip** — `suppress_echo()`, spectral subtraction of the
   stashed reference, applied only to the one clip where the caller's speech is
   genuinely mixed with our playback.

**What signal is being cancelled:** the agent's own TTS reply, returning
through the caller's loudspeaker into their microphone.

**What reference is used:** the exact PCM of the reply we sent, decoded once
in `_speak` via `pcm_from_wav_bytes()` and stored in `PlaybackReference`
against the call timeline. This is the "played audio as a reference signal"
the old `main.py` docstring named as the missing prerequisite.

**How playback echo is prevented from becoming caller speech:**

```
expected_echo_dbfs = level(reference) - erl_estimate()
excess_db          = level(microphone) - expected_echo_dbfs

excess_db < double_talk_margin_db   ->  it is our echo, stay gated
```

The echo can never be louder than what we played minus the room's attenuation.
A microphone reading meaningfully above that ceiling therefore contains a
second voice. Correlation with the reference acts as a veto on top.

**What happens when the agent is speaking:**

| Situation | Decision | Effect |
|---|---|---|
| Echo only, any attenuation | `within_expected_echo_level` | Gate stays shut, agent finishes |
| **Echo tail, playback just ended** | `within_expected_echo_level` | Gate stays shut — see finding 1, §12 |
| Mic effectively silent | `below_level_floor` | Nothing happens; ERL still recorded |
| Loud but looks like our audio | `correlates_with_playback` | Gate stays shut |
| Loud, unlike our audio, < 0.2s | `too_brief` | Gate stays shut (cough, chair scrape) |
| Loud, unlike our audio, sustained | `double_talk` | **Barge-in** |
| Nothing played within one round trip | `no_reference` | Treated as the caller |

**The reference reaches back `echo_max_delay_s` (0.5 s), not just across the
microphone window.** That is what makes row 2 correct: the client only reports
`playback_done` after a 250 ms guard, so there is a stretch where the agent is
still "speaking", the reply has finished, and the room is still ringing with
it. An aligned-only lookup is empty there and called our own echo the caller.

Every ambiguous case resolves to "echo". A false barge-in truncates a reply the
caller never hears; a missed one costs a repeat.

---

## 9. Barge-In

**How caller speech is detected while the agent is speaking.** The poll loop
no longer skips playback. Every `barge_in_poll_s` (0.10s) it takes the last
`barge_in_window_s` (0.60s) of microphone audio via `_recent_mic_tail` and
hands it to `EchoGuard.assess()` with the reference for the same interval.

**How the interruption is triggered.** A `double_talk` verdict calls
`session.barge_in()`, which:

1. stashes the reference across the window for later subtraction,
2. moves `processed_until_s` forward to the window start (minus
   `UTTERANCE_PAD_S`) — past the agent's reply, but not past the interruption,
3. clears `agent_speaking` **without** setting `resync_pending`,
4. increments the barge-in counters.

`_check_barge_in` then returns `True`, and the poll loop **falls through** to
ordinary turn detection on the same audio — so the caller's interrupting
sentence is handled like any other turn.

**How TTS playback is stopped.** The server sends `_stop_audio`. The client's
`stopPlayback()` calls `currentSource.stop()`, clears the queue, sets
`bargedIn = true` so any clip still decoding is discarded before it starts, and
resets the status line.

**How the existing target is preserved — and where it is not.**
`barge_in_poll_s` (0.10) sits well under `barge_in_target_s` (0.30), leaving
headroom for the window, the numpy work in a thread, and the WebSocket hop.
`test_poll_cadence_is_faster_while_the_agent_speaks` asserts the ordering so it
cannot silently regress.

> **The fast cadence runs on the PCM transport only.** On WebM every tail read
> re-decodes the whole call from byte 0 and spawns ffmpeg to do it, so polling
> ten times a second during each reply would reintroduce the O(T²) blowup
> `agent/pcm_buffer.py` exists to remove — measured as the transport's hard
> concurrency ceiling. `TAIL_READ_IS_CHEAP` gates it, and WebM therefore stays
> at `POLL_INTERVAL_S`: barge-in still works there, but **cannot meet the
> 0.30 s target.** PCM is what production serves; WebM is the legacy/bench
> client. `test_fast_barge_in_cadence_is_gated_on_transport_cost` pins this.

**The actual end-to-end latency is unmeasured on either transport and is
PENDING.**

**Why this works for speakerphone calls.** A handset leaks so little that the
mic rarely rises above the level floor at all. A speakerphone leaks a lot —
which is exactly why the mute existed, and exactly why a naive "any sound
during playback is the caller" rule would fire constantly. The level test is
anchored to the *measured* ERL of the actual room, so the ceiling it compares
against adapts to how leaky that particular path is.

**Known limitation, pinned by a test:** a caller quieter than the expected
echo cannot interrupt. At that point the microphone genuinely does not contain
evidence of a second voice. The failure is in the safe direction — the agent
finishes its sentence — but on a loud speakerphone the caller must speak up.
Whether that bites in practice needs a real room.

---

## 10. Speakerphone Accuracy Bucket

**How speakerphone audio is identified.** Two sources, one authoritative:

* **Client hint (advisory).** `detectAudioMode()` inspects the audio track
  label and sends `{type: "audio_mode", mode: ...}`. Track labels are
  inconsistent across browsers and absent on most desktops, so this is only a
  starting point.
* **Acoustic measurement (authoritative).** Every playback window yields an
  Echo Return Loss observation. The **median** classifies the call:

```python
median_erl <= speakerphone_erl_db (20.0)  ->  speakerphone
otherwise                                 ->  handset
```

Median, not mean, so one loud noise cannot reclassify a call. Observations are
recorded **even when the window is below the level floor** — a very quiet
return is the strongest possible evidence of a handset, and omitting it left
handset calls permanently unclassifiable (a real bug the tests caught).

**Where the metric is recorded.** `_dispatch_turn` tags every recorded turn:

```python
METRICS.record_turn(quality, success=True, path=session.echo.reporting_path())
```

`reporting_path()` collapses `unknown` to `handset`, deliberately: guessing
speakerphone would move ordinary callers into the very bucket whose accuracy we
are trying to measure.

**How handset accuracy is recorded.** Identically — same call site, same
counters, different bucket. The two are symmetrical, so neither is a special
case that could drift.

**How the gap is calculated.**

```
accuracy_gap = handset_accuracy - speakerphone_accuracy
```

Positive means the speakerphone path is worse, which is the expected direction.
`None` whenever either bucket is empty — a gap computed against a bucket with
no turns is not a small gap, it is no measurement.

Read it at `GET /api/quality`:

```json
"path_buckets": {
  "handset":      { "turns": 0, "successful_turns": 0, "accuracy": null, "mean_snr_db": null },
  "speakerphone": { "turns": 0, "successful_turns": 0, "accuracy": null, "mean_snr_db": null },
  "accuracy_gap": null
}
```

**Those nulls are the honest current state.** No call has been made, so no
accuracy and no gap exist. Note also `accuracy_definition:
"turn_completed_with_usable_text"` — this counts turns that completed without
falling into the clarification path. **It is not a word-error-rate claim**;
WER needs reference transcripts and a GPU.

---

## 11. Tests Added/Modified

`tests/test_speakerphone.py` — **NEW**, 873 lines, 66 tests. Every test below
was actually run.

| Test | What It Verifies | Result |
|------|------------------|--------|
| `test_identical_signals_correlate_at_one` | Correlation baseline | ✅ |
| `test_waveform_correlation_does_not_separate_echo_from_a_stranger` | **Pins why level is the primary signal**: echo ~0.30 vs stranger ~0.23, no usable margin | ✅ |
| `test_correlation_is_scale_invariant` | Quiet echo is not called a stranger | ✅ |
| `test_correlation_of_empty_input_is_zero_not_an_error` | Empty input safety | ✅ |
| `test_lag_search_finds_a_delayed_echo` | Recovers a 0.12s delay | ✅ |
| `test_lag_search_does_not_manufacture_a_match_for_the_caller` | Many lags ≠ spurious peak | ✅ |
| `test_erl_is_large_for_a_handset_and_small_for_a_speakerphone` | ERL separates the paths | ✅ |
| `test_erl_is_infinite_when_nothing_comes_back` | Perfect isolation | ✅ |
| `test_erl_is_zero_when_there_was_no_reference` | No-reference case | ✅ |
| `test_suppression_attenuates_the_echo_it_is_given` | Echo suppression works | ✅ |
| `test_suppression_preserves_length_and_dtype` | Shape contract | ✅ |
| `test_suppression_is_a_noop_when_disabled` | Kill switch | ✅ |
| `test_suppression_is_a_noop_without_a_usable_reference` | Degrades safely | ✅ |
| `test_suppression_does_not_clip` | No distortion introduced | ✅ |
| `test_reference_returns_what_was_playing_at_that_moment` | Timeline lookup | ✅ |
| `test_reference_is_zero_filled_where_nothing_was_playing` | Sample alignment | ✅ |
| `test_reference_slice_outside_any_playback_is_silent` | Out-of-range | ✅ |
| `test_reference_prunes_old_playback` | Memory bound | ✅ |
| `test_reference_slice_of_an_inverted_range_is_empty` | Degenerate range | ✅ |
| `test_our_own_echo_is_never_a_barge_in[8.0/18.0/28.0]` | **AC1**: echo never interrupts, swept over attenuation | ✅ ×3 |
| `test_a_caller_quieter_than_the_expected_echo_cannot_interrupt` | Pins the known limitation | ✅ |
| `test_the_caller_talking_over_the_agent_is_a_barge_in` | **AC1**: real interruption gets through | ✅ |
| `test_silence_during_playback_is_neither` | Quiet window | ✅ |
| `test_a_brief_noise_does_not_interrupt_the_agent` | Cough rejection | ✅ |
| `test_speech_when_we_were_not_playing_is_the_caller` | No-reference path | ✅ |
| `test_verdict_is_json_safe_including_infinite_erl` | Logging safety | ✅ |
| `test_a_loud_echo_classifies_the_path_as_speakerphone` | **AC2** classification | ✅ |
| `test_a_quiet_echo_classifies_the_path_as_handset` | **AC2**; caught the missing-observation bug | ✅ |
| `test_erl_estimate_starts_conservative_then_follows_the_room` | Bootstrap then adapt | ✅ |
| `test_one_observation_is_not_enough_to_classify` | Min observations | ✅ |
| `test_a_client_hint_is_used_until_there_is_evidence` | Hint as starting point | ✅ |
| `test_the_measurement_overrides_a_wrong_client_hint` | Measurement wins | ✅ |
| `test_a_nonsense_hint_is_ignored` | Input validation | ✅ |
| `test_unknown_reports_as_handset` | Bucket not contaminated | ✅ |
| `test_path_buckets_are_counted_separately` | **AC2** separate buckets | ✅ |
| `test_accuracy_gap_is_handset_minus_speakerphone` | **AC2** gap arithmetic (0.30) | ✅ |
| `test_accuracy_gap_is_none_until_both_buckets_have_turns` | No false zero | ✅ |
| `test_path_and_noise_dimensions_do_not_interfere` | Independent dimensions | ✅ |
| `test_a_turn_recorded_without_a_path_still_counts` | Backwards compatible | ✅ |
| `test_barge_ins_are_counted` | Counter | ✅ |
| `test_barge_in_does_not_schedule_the_resync_that_would_discard_it` | **Regression guard for the utterance-boundary fix** | ✅ |
| `test_release_gate_still_schedules_a_resync_for_ordinary_playback` | Normal path unchanged | ✅ |
| `test_barge_in_skips_past_the_agents_own_reply_but_not_past_the_caller` | Regression guard (bug 2, §12) | ✅ |
| `test_barge_in_never_rewinds_the_marker` | `max()` guard | ✅ |
| `test_a_queued_reply_reference_is_timestamped_when_it_will_actually_play` | Regression guard (bug 1, §12) | ✅ |
| `test_playback_start_is_now_when_nothing_is_playing` | Non-queued case | ✅ |
| `test_barge_in_stashes_the_reference_exactly_once` | No stale subtraction | ✅ |
| `test_ordinary_turn_has_no_echo_reference_to_subtract` | Untouched turns | ✅ |
| `test_check_barge_in_stops_playback_when_the_caller_talks_over` | End-to-end: `_stop_audio` sent | ✅ |
| `test_check_barge_in_ignores_our_own_echo` | End-to-end: agent keeps talking | ✅ |
| `test_barge_in_can_be_switched_off` | Kill switch reads no mic at all | ✅ |
| `test_poll_cadence_is_faster_while_the_agent_speaks` | **AC1** target ordering | ✅ |
| `test_both_transports_carry_the_speakerphone_path` | Generated file parity | ✅ |
| `test_pcm_variant_does_not_call_the_deleted_webm_decoder` | Regression guard (bug 4, §12) | ✅ |
| `test_clients_no_longer_mute_the_microphone_during_playback` | The mute cannot come back | ✅ |

**Review regressions — each of these failed before its fix (§12):**

| Test | What It Verifies | Result |
|------|------------------|--------|
| `test_our_own_echo_tail_after_playback_ends_is_not_a_barge_in` | **Finding 1** — the seam where the reply has ended but `agent_speaking` is still true | ✅ |
| `test_a_real_caller_at_that_same_seam_still_gets_through` | The fix does not deafen the agent at the moment a caller is most likely to speak | ✅ |
| `test_speech_long_after_playback_is_still_the_caller` | The reference reaches back one round trip, not indefinitely | ✅ |
| `test_reference_level_ignores_the_silence_it_is_padded_with` | **Finding 4** — zero-padding must not dilute the echo ceiling | ✅ |
| `test_fast_barge_in_cadence_is_gated_on_transport_cost` | **Finding 2** — WebM must not poll at 0.1s | ✅ |
| `test_erl_history_is_bounded` | **Finding 3** — history cannot grow without limit | ✅ |
| `test_erl_history_is_guarded_by_a_lock` | **Finding 5** — snapshot is internally consistent | ✅ |
| `test_has_audio_in_does_not_build_the_slice` | **Finding 6** — no allocation for a boolean | ✅ |
| `test_lag_search_requires_a_substantial_overlap` | **Finding 7** — a sliver of overlap manufactures spurious matches | ✅ |

**Modified:**

| File | Change | Why |
|---|---|---|
| `tests/test_audio_quality.py` | `FakeSession` gains `echo` + `take_echo_reference` | `_dispatch_turn` now asks every turn for its path bucket |
| `tests/test_audio_quality.py` | `hiss()` seeded from its arguments, module-level `rng` removed | Shared generator made signal content depend on test order |
| `tests/test_audio_quality.py` | `write_clip` uses `itertools.count()` | `id()` of temporaries is reused, producing colliding filenames |
| `tests/test_noisy_turn_boundaries.py` | Restores `POLL_INTERVAL_S` in `finally` | It leaked 0.01 into every later test |
| `tests/test_speakerphone.py` | `test_check_barge_in_stops_playback_when_the_caller_talks_over` rewritten | It was passing through the `no_reference` branch — it recorded playback as starting "now" then asked about the preceding 0.6s, a window in which the agent had made no sound. It never exercised double-talk detection at all |
| `tests/test_speakerphone.py` | `test_pcm_variant_does_not_call_the_deleted_webm_decoder` now asserts on a CALL | It matched a mere mention, and the new `TAIL_READ_IS_CHEAP` comment names `_decode_to_wav` |

---

## 12. Test Results

**Command**

```bash
python -m pytest tests/ -q
```

**Result**

```
142 passed, 4 warnings in 7.83s
```

| Suite | Tests | Result |
|---|---|---|
| `tests/test_speakerphone.py` | 66 | **66 passed** |
| `tests/test_audio_quality.py` | 64 | **64 passed** |
| `tests/test_noisy_turn_boundaries.py` | 12 | **12 passed** |
| **Total** | **142** | **142 passed, 0 failed** |

**Stability:** 8 consecutive full runs after the review fixes, **0 failing runs
out of 8** (and 14/14 before them). This matters because the suite *was*
intermittently red at roughly 1 run in 10 before the three test-quality fixes
in §11.

**Warnings:** 4, all pre-existing FastAPI `on_event` deprecations, unrelated to
this change.

**Also run:** `python -m py_compile` on every touched Python file (clean), and
`python tools/make_pcm_variant.py` (regenerated; generator independently
verified the shared reasoning half byte-identical).

### Bugs found and fixed during this work — none hidden

**First pass — during implementation:**

| # | Bug | Found by | Severity |
|---|---|---|---|
| 1 | Echo reference timestamped at *send* time, but replies queue on the client — a queued reply's reference sat before the sound it described, so `no_reference` fired and the agent's own echo would have read as a barge-in | Manual review | High |
| 2 | `barge_in()` left `processed_until_s` behind the agent's reply — the next turn would transcribe the agent's own echo, **reintroducing the self-answering loop the half-duplex gate existed to prevent** | Manual review | High |
| 3 | `main_pcm.py`'s `_recent_mic_tail` called `_decode_to_wav`, which the generator deletes from that file — `NameError` on the first barge-in, compiles cleanly, fails only in production | Compile + grep | High |
| 4 | Client `pendingClips` went permanently negative after a barge-in, so `playback_done` was never sent again for the rest of the call | Manual review | Medium |
| 5 | ERL not recorded for windows below the level floor, leaving handset calls permanently unclassifiable | Unit test | Medium |
| 6 | Waveform correlation as the primary echo discriminator had no usable margin (0.30 vs 0.23) | Unit test | Design |
| 7 | Three test-quality defects: shared RNG, `id()` filename collisions, leaked `POLL_INTERVAL_S` | Flaky suite | Medium |

**Second pass — a dedicated review of the finished feature.** All seven were
reproduced by running code before being fixed, and all seven now have a
regression test:

| # | Bug | Evidence | Severity |
|---|---|---|---|
| 8 | **The agent's own echo tail was classified as a barge-in.** The client reports `playback_done` only after a 250 ms guard, so there is a window where the reply has ended, `agent_speaking` is still true, and the room is still ringing. An aligned-only reference lookup was empty there and took the `no_reference` branch — **the agent interrupted itself**, the exact loop the half-duplex gate prevented | Reproduced: `reason=no_reference, is_barge_in=True` at −29.8 dBFS. Now `within_expected_echo_level` | **High** |
| 9 | **The fast barge-in cadence re-decoded the whole call 10×/sec on WebM.** `_recent_mic_tail` there goes through `_decode_to_wav` (full re-decode from byte 0 plus an ffmpeg spawn); raising the poll from 0.5 s to 0.1 s multiplied that fivefold for the duration of every reply — ~10 ffmpeg spawns/second/call, reintroducing the O(T²) ceiling `pcm_buffer.py` exists to remove | Now gated on `TAIL_READ_IS_CHEAP` | **High** |
| 10 | `_erl_observations` grew unbounded while `erl_estimate()`/`classify()` re-medianed it every poll | Measured 3.07 ms/poll at 3000 observations = ~368 ms/s of CPU at 12 calls. Bounded: **0.27 ms/poll, ~33 ms/s — 11× better** | Medium |
| 11 | The level test — the *primary* decision — was not delay-compensated, while correlation was. Root cause of #8 | Reference now widened by `echo_max_delay_s`; level taken from `ref_active`, not diluted by zero-padding | Medium |
| 12 | `_erl_observations` mutated from a worker thread and read from the event loop with no lock, so `erl_estimate()` and `classify()` could disagree within one decision | `threading.Lock` + snapshot, matching `_model_lock`/`_infer_lock` | Medium |
| 13 | `has_audio_in` allocated a full 9600-sample window to answer a boolean | Segment-bounds test; `slice()` calls now **0** | Low |
| 14 | **The lag search accepted overlaps down to 50 ms.** Against a reference no longer than the microphone window, a large lag left a sliver — and two speech signals correlate spuriously over a sliver. A genuine caller scored **0.826**, tripping the echo veto and **suppressing a real barge-in**. Only surfaced because fixing #8/#11 changed the slice geometry | Minimum overlap now 60% of the window, and correlation uses the widened reference. Now 0.442 | **High** |

Two of my own tests were also wrong and were corrected — see §11. One of them,
`test_check_barge_in_stops_playback_when_the_caller_talks_over`, had been
passing through the `no_reference` branch and so **never exercised double-talk
detection at all**.

**CodeRabbit: NOT RUN.** No `coderabbit` CLI is installed in this environment
— searched `PATH` under both shells, npm globals, `~/.local/bin`, scoop, choco,
winget, VS Code and VS Code Insiders extensions, `~/.coderabbit`, `LOCALAPPDATA`,
`APPDATA`, JetBrains config, and WSL (not installed). A manual senior-engineer
review was performed instead, in two passes: bugs 1, 2 and 4 came from the
first, and bugs 8–14 from the second. **No CodeRabbit result is claimed.**

---

## 13. GPU / Real Audio Validation

### Completed locally (no GPU)

* 133 unit and integration tests, 14/14 clean full runs.
* `EchoGuard.assess()` decision table exercised across echo attenuations of
  8/18/28 dB, silence, brief noise, no-reference, and a loud caller.
* ERL classification for handset and speakerphone, including the bootstrap
  estimate and the median-over-observations behaviour.
* Echo suppression: attenuation, shape contract, kill switch, no clipping.
* `PlaybackReference` timeline arithmetic: alignment, zero-fill, pruning,
  degenerate ranges.
* Barge-in wiring end to end against a fake transport: `_stop_audio` is sent
  for a real interruption and is **not** sent for our own echo.
* Regression guards proving the utterance-boundary fix still holds — the
  barge-in path does not schedule the discard-everything resync, and the marker
  never rewinds.
* Both transports verified to carry the feature; the PCM variant verified not
  to call the deleted WebM decoder.
* The post-playback seam: the agent's own echo tail is not a barge-in, a real
  caller at that same instant still is, and speech long after the reply is
  still the caller.
* CPU cost of the ERL estimate measured before and after bounding the history
  (3.07 → 0.27 ms/poll).
* The correlation lag search verified not to manufacture a match from a short
  overlap.
* `py_compile` clean; PCM variant regenerated and verified byte-identical in
  its shared half.

### PENDING — GPU / Real Audio

**None of the following has been run. No number below exists yet.**

| Check | Status |
|---|---|
| Real speakerphone test (phone on a desk, real room) | **PENDING** |
| Real echo cancellation test — does the level ceiling hold with a real loudspeaker and a real mic? | **PENDING** |
| Real Bengali speakerphone audio → STT | **PENDING** |
| Speakerphone recognition accuracy | **PENDING** |
| Handset recognition accuracy | **PENDING** |
| Accuracy gap (handset − speakerphone) | **PENDING** |
| Real barge-in latency against the 0.30s target | **PENDING** |
| Concurrent speakerphone callers | **PENDING** |

**Specific questions only real audio can answer:**

1. Is `double_talk_margin_db = 6.0` right? Too low → the agent interrupts
   itself; too high → callers cannot get a word in.
2. Is `speakerphone_erl_db = 20.0` the real boundary between the two paths, or
   does it mis-bucket a large fraction of calls?
3. Does the browser's AEC already remove most of the echo — in which case the
   measured ERL will look like a handset's even on speaker, and the classifier
   needs rethinking?
4. Is `barge_in_poll_s = 0.10` enough headroom once the window read, the numpy
   work and the WebSocket round trip are all real?
5. How often does the quiet-caller limitation (§9) actually bite?
6. Does spectral echo suppression help or hurt IndicConformer on barge-in clips?
   `VOICE_AGENT_ECHO_SUPPRESS=off` is the A/B lever.

---

## 14. Files Changed

**Created**

* `agent/echo_guard.py` — new, 630 lines
* `tests/test_speakerphone.py` — new, 873 lines, 66 tests
* `noisy_speakerphone_implementation.md` — this document

**Modified**

* `main.py` — +550 / −47 (includes the earlier audio-quality work in the same
  uncommitted diff)
* `main_pcm.py` — regenerated, **never hand-edited**
* `agent/quality_metrics.py` — path buckets, `accuracy_gap`, `record_barge_in`
* `static/index.html` — +102 / −6
* `static/pcm/index.html` — +102 / −6
* `tools/make_pcm_variant.py` — two new transport rules (`_recent_mic_tail`
  body swap, `TAIL_READ_IS_CHEAP` flip)
* `tests/test_audio_quality.py` — fixture and determinism fixes
* `tests/test_noisy_turn_boundaries.py` — restores `POLL_INTERVAL_S`

**Not changed:** the ASR model or its decoding strategy, any VAD threshold,
`UTTERANCE_PAD_S`, the audio-quality floor, the clarification/keypad ladder, the
booking flow, the semantic cache, the fast path, or any clinic-API behaviour.

**No new dependencies.** `numpy` was already in use via `agent/pcm_buffer.py`;
`soundfile` is already pinned in `requirements.txt`.

**Nothing committed.** The work sits uncommitted on `dev-chakravardhan`.

---

## 15. Final Acceptance Criteria Status

| Acceptance Criteria | Status | Evidence |
|----------------------|--------|----------|
| Echo cancellation holds on speakerphone | ⚠️ **Implemented, NOT validated on real audio** | `EchoGuard.assess()` refuses to treat playback as caller speech across 8/18/28 dB attenuation (`test_our_own_echo_is_never_a_barge_in`) **and at the post-playback seam** (`test_our_own_echo_tail_after_playback_ends_is_not_a_barge_in`). `_check_barge_in` does not send `_stop_audio` for echo. **All synthetic.** Whether a real loudspeaker in a real room stays under the level ceiling is PENDING. **Note: the first implementation of this criterion was wrong, not merely unvalidated — see finding 8, §12** |
| Barge-in works | ✅ **Implemented and unit-tested** | The half-duplex `continue` and the client mute are both gone. `test_check_barge_in_stops_playback_when_the_caller_talks_over` shows the gate opening and `_stop_audio` going out; `test_clients_no_longer_mute_the_microphone_during_playback` prevents the mute returning. **That test had to be rewritten — it was passing through the `no_reference` branch and never exercised double-talk detection (§11)** |
| Barge-in **within its target** | ⚠️ **Budgeted on PCM only; NOT measured anywhere** | `barge_in_poll_s` (0.10) < `barge_in_target_s` (0.30) is asserted by `test_poll_cadence_is_faster_while_the_agent_speaks`. That proves the *cadence* allows the target **on the PCM transport**. **On WebM the fast cadence is deliberately disabled** (finding 9) because each tail read re-decodes the whole call, so barge-in there takes up to `POLL_INTERVAL_S` and **cannot meet the target**. Actual end-to-end latency is unmeasured on either transport and is PENDING |
| Speakerphone accuracy measured separately | ✅ **Implemented** for the measurement path; ⚠️ **no data yet** | `path_buckets.speakerphone` exists at `/api/quality` and is populated per turn by `record_turn(..., path=...)`; `test_path_buckets_are_counted_separately` and `test_path_and_noise_dimensions_do_not_interfere` verify the accounting. **`accuracy` is `null` — no call has been made** |
| Handset vs speakerphone gap stated | ✅ **Implemented** for the calculation; ⚠️ **no value yet** | `accuracy_gap = handset − speakerphone`, verified by `test_accuracy_gap_is_handset_minus_speakerphone` (0.30 on synthetic counts) and `test_accuracy_gap_is_none_until_both_buckets_have_turns`. **Currently `null`, correctly** |

### Honest summary

**The code is complete and the mechanisms are unit-tested. The story is not yet
proven.**

Everything that can be settled without a GPU or a real microphone has been:
the decision logic, the arithmetic, the wiring, both transports, and the
guarantee that the earlier utterance-boundary fix still holds.

**Fourteen real defects were found and fixed across two review passes**, and
the pattern in them is worth stating rather than burying. Three separate bugs
(#2, #8, #14) each independently reintroduced a version of the failure this
feature exists to prevent — the agent hearing, or silencing, itself. Removing
a safety gate is easy; replacing what it guaranteed is not, and each of those
was invisible until a test or a measurement was pointed straight at it.

Two of the defects were only found by **running** code rather than reading it
(the CPU cost of the ERL median, the 0.826 spurious correlation), and one
(#14) surfaced only because the fix for another changed the geometry it
depended on. The suite passing is not, on its own, evidence that this works.

What has **not** been established is whether the thresholds are right for a
real room, and no accuracy number or latency figure exists. **AC1 and AC2
cannot be marked fully satisfied until section 13 is run on real audio** — and
given finding 8, the honest reading of AC1 is that it has now been implemented
*correctly for the first time*, not that it has been confirmed.
