# Barge-In for the Impatient Caller — Implementation

> ⚠️ **Real-audio validation: PENDING.** No real caller, microphone,
> speakerphone or GPU has touched any of this. Every threshold below is
> reasoned, not measured. Section 9 lists what still has to be run.

> **Commit:** `84733cf` on `dev-chakravardhan`, local only. Builds on `87ea688`.

---

## 1. The Story

> **As a caller who already knows the answer, I want to talk over the agent
> and be heard, so that I am not made to listen to the rest.**

This is a different person from the speakerphone caller. The speakerphone
caller had a *physical* problem: their phone was on a table and the agent's
voice came back into the microphone. This caller has an *impatience* problem.
They have rung before. They know the menu. They do not want to sit through
forty words of greeting to say four.

Two things follow, and both were untested:

* **They interrupt EARLY** — often during the greeting, which is the first
  reply of the call and the moment the system knows least about the room.
* **They interrupt REPEATEDLY** — that is what impatience means. Interrupting
  once and then being forced to listen to the next reply in full is not a
  solved story.

The speakerphone work made interruption *possible*. It did not make it
*usable* for this caller.

---

## 2. What Was Already There, and Why It Was Not Enough

`87ea688` had already replaced the half-duplex mute with arbitration:
`EchoGuard.assess()` compares the microphone against the ceiling the echo
could possibly reach, `_check_barge_in` stops playback on a real
interruption, and `CallSession.barge_in()` opens the gate without discarding
what the caller said.

All of that works — **once**. Three separate defects only appear on the
second interruption, or on the first one if it comes early enough. None was
visible in the speakerphone tests, because a caller who interrupts once, on
a call that has been running for a while, hits none of them.

Every one was found by **running** the code and measuring, not by reading it.

---

## 3. Gap A — The Reference Outlived the Playback

### OLD 🔴 — `agent/echo_guard.py`, `PlaybackReference`

```python
class PlaybackReference:
    def add(self, start_s: float, samples: np.ndarray) -> None: ...
    def prune(self, now_s: float) -> None: ...
    def slice(self, start_s: float, end_s: float) -> np.ndarray: ...
    # no way to say "we stopped early"
```

`_speak()` records the whole reply as the reference the moment it is sent.
When barge-in stops playback halfway, nothing tells the reference that the
rest never came out of the speaker.

**Measured, before the fix:**

```
agent starts a 5s reply at t=10.0
caller barges in at t=11.0 -> client stops playback
  reference audio present at t=13.0-13.6 ?  True     <-- never played
  second interruption at t=13.0:  barge_in=False  within_expected_echo_level
```

The level test compares the microphone against `reference level − ERL`. With
a reference that claims a loud reply is still playing, the ceiling stays high
and **the caller cannot interrupt again**. The feature worked once per call
and then silently stopped.

### NEW 🟢

```python
def truncate_after(self, cut_s: float) -> None:
    """Forget everything we had queued to play beyond `cut_s`.

    Called when playback is STOPPED early. Without it the reference goes on
    claiming we played the whole reply, and the level test then judges later
    windows against sound that never left the speaker: the expected echo
    ceiling stays high, and the caller cannot interrupt a second time. For a
    caller who already knows the answer -- who interrupts repeatedly by
    nature -- that is the difference between the feature working once and
    working at all."""
    with self._lock:
        trimmed: list[tuple[float, np.ndarray]] = []
        for seg_start, samples in self._segments:
            if seg_start >= cut_s:
                continue                    # never played at all
            keep = int(max(0.0, cut_s - seg_start) * self.sample_rate)
            if keep <= 0:
                continue
            trimmed.append((seg_start, samples[:keep]))
        self._segments = trimmed
```

and in `main.py`, `CallSession.barge_in()`:

```python
now_s = self.call_time_s()
self._pending_echo_ref = self.echo.reference.slice(
    now_s - ECHO_CFG.barge_in_window_s, now_s)

# Playback is about to be STOPPED, so the rest of this reply will never
# leave the speaker. Forget it, or the level test keeps judging later
# windows against sound that was never made -- the expected echo ceiling
# stays high and the caller cannot interrupt a second time.
self.echo.reference.truncate_after(now_s)
```

**Order matters:** the echo reference for the current clip is stashed
*before* truncation. That stash covers the window ending now, which
truncation does not touch — but reversing the two would hand the next clip an
empty reference to subtract.

**After the fix:** second interruption → `barge_in=True`. Verified across
four consecutive interruptions in one call.

**What it keeps:** only the unplayed remainder is forgotten. The part the
caller actually heard is still echoing and is still needed to recognise it —
`test_truncation_keeps_the_part_that_did_play` pins that.

---

## 4. Gap B — Talking Over the Agent Made It Deafer

### OLD 🔴 — `agent/echo_guard.py`, `EchoGuard.assess()`

```python
# Record ERL FIRST, and record it even for a window too quiet for anyone
# to be talking.
erl = echo_return_loss_db(mic, ref_active)
self._record_erl(erl)          # <-- EVERY window, including double-talk
```

Echo Return Loss means "how much quieter is the returning echo than what we
played". That is only true when the microphone contains **echo alone**.
During double-talk the microphone contains the caller, so the number measured
is the caller's level relative to ours — a completely different quantity,
and a much smaller one.

Recording it dragged the median down. A lower ERL estimate means a *higher*
expected-echo ceiling, which means the next interruption needs to be louder.

**Measured, before the fix:**

```
after 4 echo-only polls    : erl_est = 39.7 dB
after 12 double-talk polls : erl_est =  3.3 dB   <-- dragged down
```

**The system got deafer the more the caller talked over it.** For this story
that is precisely backwards, and it is the kind of defect no assertion would
have caught — nothing returns a wrong answer, the behaviour just drifts.

### NEW 🟢

Recording moved from one place at the top to each return, and only on the
returns that mean *echo*:

```python
erl = echo_return_loss_db(mic, ref_active)      # computed, not yet recorded

if level_dbfs < self._cfg.barge_in_min_level_dbfs:
    self._record_erl(erl)                       # RECORDED
    return EchoVerdict(..., "below_level_floor")

if excess_db < self._cfg.double_talk_margin_db:
    self._record_erl(erl)                       # RECORDED -- echo only
    return EchoVerdict(..., "within_expected_echo_level")

if corr >= self._cfg.echo_correlation_threshold:
    self._record_erl(erl)                       # RECORDED -- echo only
    return EchoVerdict(..., "correlates_with_playback")

# NOT recorded from here down. The microphone contains a second voice, so
# this window's "ERL" is not the room's echo return loss at all -- it is the
# caller's level relative to ours. Feeding it to the median drags the
# estimate down, which raises the expected-echo ceiling, which makes the NEXT
# interruption harder. Measured: 39.7dB falling to 3.3dB after twelve
# double-talk windows.
if mic.size < int(self._cfg.barge_in_min_speech_s * sr):
    return EchoVerdict(..., "too_brief")

return EchoVerdict(..., "double_talk")
```

**Note what is still recorded:** a window too quiet for anyone to be talking.
That is not an absence of evidence — it is the strongest possible evidence of
a handset, and dropping it left handset calls permanently unclassifiable (a
bug fixed in the previous round, deliberately not undone here).
`test_a_quiet_window_is_still_recorded_as_evidence` guards it.

**After the fix:** `39.7 dB` before and after twelve double-talk windows.

---

## 5. Gap C — The Bootstrap Held On Too Long

### OLD 🔴

```python
def erl_estimate(self) -> float:
    finite = self._finite_observations()
    if len(finite) < self._cfg.classification_min_observations:   # 2
        return self._cfg.bootstrap_erl_db                          # 6.0
    return float(np.median(finite))
```

Two different questions were sharing one threshold. `bootstrap_erl_db` is
deliberately low — it assumes a loud echo, which demands a louder caller,
which errs toward *not* interrupting. That is right while the room is
unknown. But it applied for two polls, and every poll on the bootstrap is a
poll in which the caller cannot interrupt.

**Measured, before the fix — a caller interrupting the greeting:**

```
caller normal speech  (0.15): barge_in=False   within_expected_echo_level
caller raised voice   (0.25): barge_in=False   within_expected_echo_level
caller loud           (0.40): barge_in=False   within_expected_echo_level
caller shouting       (0.60): barge_in=True    double_talk
```

They had to **shout**.

### NEW 🟢

```python
estimate_min_observations: int = 1   # the ERL ESTIMATE may use a single clean
                                     # observation, unlike the path
                                     # CLASSIFICATION which wants more. They
                                     # answer different questions: the estimate
                                     # only has to beat the
                                     # deliberately-pessimistic bootstrap, and
                                     # every poll it stays on that bootstrap is
                                     # a poll in which the caller cannot
                                     # interrupt. Classification decides which
                                     # accuracy bucket a whole call lands in
                                     # and deserves more evidence.
```

```python
def erl_estimate(self) -> float:
    finite = self._finite_observations()
    if not finite or len(finite) < self._cfg.estimate_min_observations:
        return self._cfg.bootstrap_erl_db
    return float(np.median(finite))
```

**After the fix — realistic poll sequence, normal speaking voice:**

```
after 0 echo-only polls (t+0.0s): barge_in=False   <-- known limitation
after 1 echo-only poll  (t+0.1s): barge_in=True
after 2 echo-only polls (t+0.2s): barge_in=True
```

**The remaining 0.1s is a genuine limitation, not an oversight.** Before any
echo has been observed there is nothing to estimate from, and the alternative
— assuming a quiet room — is a system that interrupts itself on the greeting.
Pinned by `test_the_very_first_poll_of_a_call_cannot_be_interrupted_quietly`.

---

## 6. Found in Code Review

Both fixed in the same commit, both with regression tests.

### R1 — A misconfigured env var could disarm the level test 🔴→🟢

`estimate_min_observations` is env-settable. At `0`:

```python
if len(finite) < 0:              # False on an EMPTY history
    ...
return float(np.median([]))      # nan
```

Then `expected_echo_dbfs = rms_dbfs(ref_active) - nan` → `nan`, and
`excess_db = nan`. Crucially **`nan < margin` is `False`**, so the level test
— the *primary* safety check — stops rejecting anything, falls through to the
correlation veto, and the agent can interrupt itself on its own echo.

Fixed by the `not finite or` guard above. A misconfigured environment
variable must not be able to silently disarm the primary check.

### R2 — `PlaybackReference` was unlocked 🔴→🟢

`assess()` reads the reference from a thread-pool worker (dispatched through
`asyncio.to_thread`), while `_speak()` adds to it and `barge_in()` truncates
it on the event loop. Rebinding `_segments` is atomic, but `add()` mutates in
place, and a reader iterating it during an append has no defined behaviour.

A `threading.Lock` now guards `add`, `prune`, `truncate_after`, `slice`,
`has_audio_in` and `__len__`. `slice` takes a shallow snapshot under the lock
and works outside it — the arrays themselves are never mutated in place.

This matches what `EchoGuard` already does for its ERL history, and what
`TurnDetector._model_lock` and `TurnASR._infer_lock` do elsewhere in the
codebase. Leaving one of the two shared structures unlocked was simply
inconsistent.

---

## 7. Functions and Variables Changed

| File | Function / Class | OLD | NEW | Why |
|---|---|---|---|---|
| `agent/echo_guard.py` | `PlaybackReference.truncate_after` | *(did not exist)* | Forgets audio queued past a cut point | Gap A |
| `agent/echo_guard.py` | `PlaybackReference.__init__` | no lock | `threading.Lock()` | R2 |
| `agent/echo_guard.py` | `PlaybackReference.add/prune/slice/has_audio_in/__len__` | unsynchronised | guarded by the lock | R2 |
| `agent/echo_guard.py` | `EchoGuard.assess` | recorded ERL for every window | records only on echo verdicts | Gap B |
| `agent/echo_guard.py` | `EchoGuard.erl_estimate` | used `classification_min_observations`; nan on empty | uses `estimate_min_observations`; guarded | Gap C, R1 |
| `main.py` / `main_pcm.py` | `CallSession.barge_in` | stashed the reference only | also calls `truncate_after(now_s)` | Gap A |

| Variable | OLD | NEW | Purpose |
|---|---|---|---|
| `estimate_min_observations` | *(did not exist — shared `classification_min_observations = 2`)* | `1`, `VOICE_AGENT_ERL_MIN_OBS` | How many clean observations before the ERL estimate leaves the bootstrap. Lower = the caller can interrupt sooner |
| `classification_min_observations` | `2` | `2`, unchanged | Still 2, but now only governs the handset/speakerphone bucket, not barge-in sensitivity |
| `bootstrap_erl_db` | `6.0` | `6.0`, unchanged | Still deliberately pessimistic. The fix was to leave it sooner, not to weaken it |

**Deliberately unchanged:** `double_talk_margin_db`, `echo_correlation_threshold`,
`barge_in_window_s`, `barge_in_poll_s`, `barge_in_target_s`,
`barge_in_min_speech_s`, `speakerphone_erl_db`, and every audio-quality and
VAD threshold. No tuning was done — these are structural fixes.

---

## 8. How It Behaves Now

* A caller can interrupt, be heard, and then interrupt the **next** reply too
  — verified across four consecutive interruptions in one call, with the ERL
  estimate holding steady at 34.7 dB throughout.
* Talking over the agent no longer degrades the system's ability to hear the
  next interruption.
* A normal speaking voice is enough after 0.1 s of any reply. No shouting.
* Playback that was cut short is forgotten; playback that was actually heard
  is still remembered, so its echo is still correctly rejected.
* A misconfigured `VOICE_AGENT_ERL_MIN_OBS` can no longer disarm the level
  test.

**Two limitations remain, both pinned by tests rather than hidden:**

1. The very first poll of a call's first reply is deaf to a quiet caller.
   ~0.1 s, and the alternative is a system that interrupts itself on the
   greeting.
2. A caller quieter than the expected echo still cannot interrupt at all.
   At that point the microphone genuinely holds no evidence that a second
   voice is present. Safe direction, real cost.

**One residual, documented in the test that touches it:** a quiet caller's
window is judged `within_expected_echo_level` and so *is* recorded as an ERL
observation, mildly contaminating the estimate. The strong case is excluded
and a median over `erl_history` absorbs the rest. Separating "quiet caller"
from "loud echo" needs evidence the microphone does not contain.

---

## 9. Testing

### Done

**153 tests passing** (75 speakerphone, 64 audio quality, 12 turn
boundaries), **6 consecutive clean full runs**. `py_compile` clean;
`main_pcm.py` regenerates byte-identical.

11 new tests:

| Test | Verifies |
|---|---|
| `test_a_second_interruption_works_after_the_first_cut_playback_short` | Gap A — the one that matters most |
| `test_truncation_keeps_the_part_that_did_play` | Truncation forgets only the unplayed remainder |
| `test_truncation_drops_a_reply_that_never_started` | A queued-but-unplayed reply is dropped entirely |
| `test_talking_over_the_agent_does_not_make_it_deafer` | Gap B — estimate stable under sustained double-talk |
| `test_a_quiet_window_is_still_recorded_as_evidence` | The opposite guard — handset evidence still counted |
| `test_a_normal_voice_can_interrupt_almost_immediately` | Gap C — no shouting required |
| `test_the_very_first_poll_of_a_call_cannot_be_interrupted_quietly` | Pins the remaining limitation |
| `test_the_estimate_and_the_classification_have_separate_thresholds` | The two thresholds answer different questions |
| `test_barge_in_truncates_the_reference_through_the_session` | The wiring, not just the primitive |
| `test_a_misconfigured_estimate_threshold_cannot_disarm_the_level_test` | R1 |
| `test_playback_reference_is_locked` | R2 |

Edge cases probed in review: empty reference, cut exactly at a segment start,
truncate combined with prune, cut beyond the segment end, negative cut, and
echo of the part that *did* play (correctly still rejected).

### PENDING — real audio

* A real caller interrupting a real greeting, on a handset and on a speaker
* Whether the 0.1 s first-poll deafness is ever actually hit
* How often the quiet-caller limitation bites in practice
* Whether repeated interruption stays reliable over a long real call
* Measured barge-in latency against the 0.30 s target
* Whether `bootstrap_erl_db = 6.0` is the right pessimism for a real room
* Concurrent callers, all interrupting

---

## 10. Risks and Do-Nots

**Risks**

* Every threshold remains unmeasured. `bootstrap_erl_db` too high and the
  agent interrupts itself on the greeting; too low and the impatient caller
  is ignored.
* Gap B was a *drift*, not a wrong answer — nothing failed, behaviour just
  degraded over a call. Other drifts of that shape would be equally invisible
  to the current tests.
* The residual contamination above is unquantified without real audio.

**Do NOT**

* **Do not remove `truncate_after` from `barge_in()`.** Barge-in reverts to
  working exactly once per call, silently.
* **Do not record ERL on the `double_talk` path.** That is the whole of Gap B.
* **Do not set `estimate_min_observations` to 0.** Guarded now, but the
  intent is at least one real observation.
* **Do not raise `bootstrap_erl_db` to make interruption easier.** It is
  pessimistic on purpose; the fix was to leave it sooner, not to weaken it.
* **Do not stash the echo reference after truncating** in `barge_in()` — the
  next clip would get an empty reference to subtract.
* **Do not hand-edit `main_pcm.py`.** Edit `main.py`, run
  `python tools/make_pcm_variant.py`.

---

## 11. Status

| | |
|---|---|
| Code | Complete, committed `84733cf` |
| Tests | 153 passing, 6/6 clean runs |
| Code review | Done — 2 findings, both fixed with regression tests |
| CodeRabbit | **NOT RUN** — not installed in this environment |
| Real-audio validation | **PENDING** |
| Pushed | **No** — `[ahead 2]` of `origin/dev-chakravardhan` |
| Deployed | **No** |

The mechanisms are correct as far as synthetic tests can establish. Three
defects were found here that the previous round's tests did not catch,
because they only appear on the second interruption or on a very early one —
which is a reminder that a passing suite describes the cases someone thought
to write, not the ones a real caller will produce.
