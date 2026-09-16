# Barge-In: Level vs Correlation, and Why a Normal Voice Could Not Interrupt

> ⚠️ **Every number in this document comes from a synthetic room.** A fixed
> 24-tap low-pass and a constant delay — no reverberation tail, no speaker
> non-linearity, no browser AEC in front of it. Nothing here has met a real
> microphone. Section 9 lists what must be measured.

> **Commits:** `c196cf9` (the fix), `4d82c19` (review findings),
> `96a3b16` (this document). Local only, `[ahead 6]`.

---

## 1. The Story

> **As a caller who already knows the answer, I want to talk over the agent
> and be heard, so that I am not made to listen to the rest.**

This caller has rung before. They know the menu, they know the answer, and
they do not want to sit through forty words of greeting to say four. Two
things follow from that, and both are load-bearing:

* **"talk over the agent"** — they will not wait for a gap. They speak while
  the agent is mid-sentence, which means the microphone is open during
  playback and the system has to tell their voice apart from its own echo.
* **"and be heard"** — stopping the agent is only half of it. The words they
  interrupted with have to survive and be acted on. An interruption that
  silences the agent and then loses the sentence is not this story.
* **"not made to listen to the rest"** — playback must actually stop, not
  merely be noted.

### Why this document exists

`87ea688` built the barge-in machinery for the speakerphone story, and
`84733cf` fixed three defects that only appear when a caller interrupts
*early* or *repeatedly* — which is what impatience means.

This round started as a review question about the design (§2), and the answer
confirmed the design was right. But answering it properly surfaced something
worse than a design flaw: **a normal speaking voice could not interrupt a loud
speakerphone at all.** 3.9 dB of excess against a 6.0 dB margin, ignored. The
caller had to shout — which is exactly the thing "and be heard" rules out.

So the work here is not a refinement of the review question. It is a failing
case for the story, found while answering it.

---

## 2. The Question That Started It

A review asked, of `echo_guard.py`:

> The barge-in decision uses level excess above the expected echo as the
> primary discriminator, then applies a correlation veto. Is this the right
> safety trade-off? Should correlation become a stronger part of the primary
> decision?

Three specific worries: a loud non-echo signal that is somewhat correlated; a
caller with low correlation but a level clearly above the ceiling; and the
system being too conservative and missing valid interruptions.

The answer to the headline question is **no, correlation must stay a veto** —
and the evidence for that is unambiguous. But answering it properly surfaced
a failing case for the story itself, which turned out to matter far more than
the ordering did.

---

## 3. Why Correlation Cannot Be Primary

Twelve scenarios, three policies, scored on false-fires (the agent interrupts
itself) and misses (the caller is ignored):

| Policy | False-fires | Missed |
|---|---|---|
| **Level primary + correlation veto** (current) | **0** | 2 |
| Correlation-first | **3 of 3** | 0 |
| Level only | 0 | 2 |

**Correlation-first fires on pure echo, every time.** The reason is physical.
A room delays the reply and low-passes it, and that transformation destroys
waveform correlation: real echo measures **0.024** against the reference. So
the rule "not correlated with our playback, therefore it is the caller" is
*true of our own voice coming back*. A correlation-first agent would stop
itself on every reply — the self-answering loop the old half-duplex mute
existed to prevent.

Level survives the same transformation, because attenuation is a scalar that
can be estimated. That is the whole argument for the ordering.

### On the first worry — "loud but somewhat correlated"

Measured across all twelve scenarios, double-talk correlates **0.23–0.35**
against a veto threshold of **0.55**.

**The veto never fired once.** `level + veto` and `level only` produced
identical verdicts throughout. It is currently inert.

Kept anyway: it is cheap insurance against a pathological case — a caller
whose speech genuinely resembles our TTS — and the measured headroom between
0.35 and 0.55 means it is in no danger of misfiring. But nobody should
believe it is doing work today, and §6 shows what it was costing.

---

## 4. The Real Defect: A Normal Voice Could Not Interrupt

The third worry was correct, and understated. This is the story's own caller
failing:

```
double-talk, quiet  caller, ERL 8dB   excess -1.6 dB  ->  MISSED
double-talk, normal caller, ERL 8dB   excess +3.9 dB  ->  MISSED   (margin 6.0)
```

On a loud speakerphone, **a normal speaking voice produced 3.9 dB of excess
against a 6.0 dB margin and was ignored**. Nine of twenty scenarios missed.
The caller had to shout to be heard — which is precisely the thing the story
says should not be necessary.

### And a margin alone cannot fix it

Sweeping the margin looked promising until the grid was widened to include a
quiet handset line with ambient room noise:

```
      ERL |  echo only  +room noise |  quiet 0.10  normal 0.25  loud 0.45
        5 |       -4.7         -4.3 |        -2.9          1.5        5.7
        8 |       -4.7         -3.9 |        -1.6          3.9        8.5
       12 |       -4.7         -2.9 |         0.8          7.5       12.4
       20 |       -4.7          1.7 |         7.5         15.2       20.3
       35 |       -4.7         15.5 |        22.2         30.1       35.2

highest "must stay quiet" : 15.5 dB   (room noise on a handset)
lowest  "must fire"        :  1.5 dB   (normal caller on a speakerphone)
=> the separating band is EMPTY
```

**No single dB threshold admits the caller and rejects the noise.** Room noise
on a quiet handset produces *more* excess than a normal caller on a loud
speakerphone.

That is not a tuning failure. It is the level test being asked a question it
cannot answer. It measures *"is there more here than our echo?"* — which is
not *"is someone talking?"*

---

## 5. OLD CODE 🔴 → NEW CODE 🟢

### 5.1 A second, independent question

**File:** `agent/echo_guard.py` · **Function:** `EchoGuard.assess()`

```python
# 🔴 OLD -- one question, asked with a bigger and bigger margin
if excess_db < self._cfg.double_talk_margin_db:       # 6.0
    return EchoVerdict(..., "within_expected_echo_level")

if corr >= self._cfg.echo_correlation_threshold:
    return EchoVerdict(..., "correlates_with_playback")

if mic.size < int(self._cfg.barge_in_min_speech_s * sr):
    return EchoVerdict(..., "too_brief")

return EchoVerdict(..., "double_talk")                # BARGE-IN
```

```python
# 🟢 NEW -- a second question the first one cannot answer
if excess_db < self._cfg.double_talk_margin_db:       # now 3.0
    return EchoVerdict(..., "within_expected_echo_level")

if corr >= self._cfg.echo_correlation_threshold:
    return EchoVerdict(..., "correlates_with_playback")

if mic.size < int(self._cfg.barge_in_min_speech_s * sr):
    return EchoVerdict(..., "too_brief")

# IS IT A VOICE, or just something loud?
speech_ratio = assess_quality(mic, sr).speech_ratio
if speech_ratio < self._cfg.barge_in_min_speech_ratio:
    return EchoVerdict(..., "not_speech")

return EchoVerdict(..., "double_talk")                # BARGE-IN
```

**Why `speech_ratio`.** It is the fraction of 25 ms frames sitting above the
window's own noise floor, and `agent/audio_quality.py` already computes it on
CPU for the quality floor. **No new model, no new dependency.** Measured:

| Window | speech_ratio |
|---|---|
| echo + room noise, ERL 35 dB | **0.000** |
| echo + *loud* room noise, ERL 35 dB | **0.000** |
| double-talk, quiet caller | 0.414 |
| double-talk, normal caller | 0.845 |
| caller alone, no echo | 0.897 |

Clean separation, and it is the variable the level test was missing.

**Why the margin could then drop to 3.0.** With noise blocked by a test that
does not care about level, the margin no longer has to be large enough to
reject it — it only has to sit above the echo-only excess (−4.7 dB).

### 5.2 The result

```
before:  0 false-fires,  9 missed   (of 20 scenarios)
after:   0 false-fires,  1 missed
```

The one remaining miss is ERL 5 dB — a phone on a hard desk at high volume,
where a normal caller is comparable in level to the echo itself and the
microphone genuinely holds little evidence that a second voice is present. A
loud caller still gets through. Pinned as a known limitation.

---

## 6. Found in Code Review of the Above

Both fixed in `4d82c19`, both with regression tests.

### R1 — The expensive check ran on every poll to feed a veto that never fires

```python
# 🔴 OLD -- correlation computed before anything needed it
corr, lag = best_lag_correlation(mic, ref_recent, sr, self._cfg.echo_max_delay_s)

if excess_db < self._cfg.double_talk_margin_db:
    return EchoVerdict(True, False, corr, lag, ...)    # never reads corr
```

```python
# 🟢 NEW -- computed only once the level test says it might be needed
if excess_db < self._cfg.double_talk_margin_db:
    self._record_erl(erl)
    return EchoVerdict(True, False, 0.0, 0.0, erl, level_dbfs,
                       "within_expected_echo_level")

# Only now is correlation worth computing.
corr, lag = best_lag_correlation(mic, ref_recent, sr, self._cfg.echo_max_delay_s)
```

**Cost breakdown of one poll, before:**

| Component | Cost |
|---|---|
| `best_lag_correlation` | **2.27 ms** |
| speech gate | 0.20 ms |
| `reference.slice` | ~0.00 ms |

Almost every window during a reply is plain echo, so almost every poll paid
2.27 ms for a value it discarded — to feed a veto §3 measured as never firing.

```
echo-only poll:  2.61 ms -> 0.28 ms
at 12 calls:      314 ms/s -> 34 ms/s      (9x)
```

The cheap branch now reports `corr`/`lag` as `0.0` because it genuinely did
not measure them — and the regression test asserts on *that*, not on timing,
so it cannot flake on a busy machine.

### R2 — The speech gate covered one branch and not the other

```python
# 🔴 OLD -- no_reference returned a barge-in without asking if it was a voice
if ref_active.size == 0:
    if level_dbfs < self._cfg.barge_in_min_level_dbfs:
        return EchoVerdict(..., "below_level_floor")
    return EchoVerdict(False, True, ..., "no_reference")     # BARGE-IN
```

```python
# 🟢 NEW
    if assess_quality(mic, sr).speech_ratio < self._cfg.barge_in_min_speech_ratio:
        return EchoVerdict(False, False, 0.0, 0.0, 0.0, level_dbfs, "not_speech")
    return EchoVerdict(False, True, 0.0, 0.0, 0.0, level_dbfs, "no_reference")
```

Room noise was correctly rejected while the agent was audibly playing and
**incorrectly accepted a moment later when it was not**. That branch is
reached in the window between the last sample leaving the speaker and the
client's `playback_done` arriving. Ambient noise there stopped playback,
opened the gate, advanced `processed_until_s`, and earned the caller an
unprompted *"sorry, it is noisy"* for a turn they never took.

```
loud room noise, nothing playing:  barge_in=True  -> barge_in=False (not_speech)
a real voice,    nothing playing:  barge_in=True  -> unchanged
```

---

## 7. Functions and Variables Changed

| File | Function / Class | OLD | NEW | Why |
|---|---|---|---|---|
| `agent/echo_guard.py` | `EchoGuard.assess` | level + correlation veto only | adds a speech gate on **both** exit branches; correlation computed lazily | §4, R1, R2 |
| `agent/echo_guard.py` | module imports | `rms_dbfs`, `_stft`, `_istft`, `_env_*` | also `assess as assess_quality` | reuse the existing CPU speech metric |

| Variable | OLD | NEW | Purpose |
|---|---|---|---|
| `double_talk_margin_db` | `6.0` | **`3.0`**, `VOICE_AGENT_DOUBLE_TALK_MARGIN_DB` | How far above the expected echo the mic must sit. Lowered because a normal voice produced only 3.9 dB on a loud speakerphone and was ignored. Safe to lower only because the speech gate now blocks what the larger margin was protecting against |
| `barge_in_min_speech_ratio` | *(did not exist)* | **`0.25`**, `VOICE_AGENT_BARGE_IN_MIN_SPEECH_RATIO` | A barge-in must look like a voice. Room noise measures 0.000, speech 0.41–0.90 |
| `EchoVerdict.reason` | 6 values | adds **`"not_speech"`** | Distinguishes "something loud" from "someone talking" in the logs |
| `EchoVerdict.correlation` / `.lag_s` | always measured | `0.0` on the echo-only branch | Not a lost value — a value never computed, deliberately |

**Deliberately unchanged:** `echo_correlation_threshold` (0.55),
`bootstrap_erl_db`, `barge_in_window_s`, `barge_in_poll_s`,
`barge_in_target_s`, `barge_in_min_speech_s`, `speakerphone_erl_db`, and every
audio-quality and VAD threshold.

---

## 8. How It Behaves Now

* A **normal speaking voice** interrupts across the whole range of rooms
  tested — ERL 8, 12, 20 and 35 dB. No shouting.
* **Our own echo never fires**, at ERL 5, 8, 12, 20 or 35 dB.
* **Ambient room noise is not an interruption**, whether the agent is
  currently playing or in the gap just after it stopped.
* The common echo-only poll costs **0.28 ms** instead of 2.61 ms.
* Correlation still vetoes a loud window that looks like our own audio — it
  is simply no longer paid for on every poll.

**Known limitation, pinned by a test:** at ERL 5 dB — a phone on a hard desk
at high volume — a normal voice is still lost. A loud one gets through.

---

## 9. Testing

**169 passing.** `tests/test_speakerphone.py` (89) stable across 8
consecutive runs. `py_compile` clean; `main_pcm.py` regenerates
byte-identical.

New tests:

| Test | Verifies |
|---|---|
| `test_a_normal_voice_interrupts_across_the_whole_range_of_rooms` | **The story**, parametrised over ERL 8/12/20/35 dB |
| `test_our_own_echo_never_fires_at_any_room_leakiness` | The safety half, ERL 5/8/12/20/35 dB |
| `test_room_noise_is_not_an_interruption` | Why a second test exists at all |
| `test_room_noise_is_not_an_interruption_even_with_nothing_playing` | R2 |
| `test_a_voice_with_nothing_playing_is_still_the_caller` | R2's other half |
| `test_an_extreme_speakerphone_still_loses_a_normal_voice` | Pins the ERL 5 dB limitation |
| `test_correlation_must_stay_a_veto_not_the_primary_test` | Records the 0.024 measurement |
| `test_the_common_echo_only_path_does_not_run_the_lag_search` | R1, asserted via the verdict not timing |
| `test_the_veto_branch_still_measures_correlation` | Lazy must not mean absent |

### PENDING — real audio

1. **`double_talk_margin_db = 3.0`** — the single most important number here.
   Too low and the agent interrupts itself; too high and this whole change is
   undone.
2. **`barge_in_min_speech_ratio = 0.25`** — does real room noise really
   measure near zero, and does quiet real speech clear 0.25?
3. Whether a real room reaches ERL 5 dB often enough for that limitation to
   matter.
4. Whether the browser's AEC changes the ERL distribution enough to move
   everything.
5. Measured barge-in latency against the 0.30 s target.
6. Concurrent callers all interrupting.

---

## 10. Risks and Do-Nots

**Risks**

* The synthetic room is optimistic in a specific way: a fixed low-pass and
  constant delay produce a *cleaner* echo than reality. Real reverberation
  tails and speaker non-linearity push echo-only excess **upward**, which is
  the direction that causes false-fires — and this grid cannot show them.
  Lowering the margin from 6.0 to 3.0 spent headroom that real audio may
  demand back.
* `speech_ratio` on a 0.6 s window is a coarse instrument. Percussive noise
  with speech-like envelope structure could clear it.
* The correlation veto remains unproven — never fired in any measured
  scenario, so its threshold is set by argument rather than evidence.

**Do NOT**

* **Do not promote correlation to the primary test.** Measured: real echo
  correlates 0.024, so the agent would interrupt itself on every reply.
* **Do not remove the speech gate and raise the margin instead.** The
  separating band is empty; that was measured, not assumed.
* **Do not restore eager correlation** on the echo-only branch. 9× cost on
  the path taken almost every poll, for a value that branch never reads.
* **Do not treat `corr == 0.0` on `within_expected_echo_level` as a bug.**
  It means "not measured", by design.
* **Do not raise `double_talk_margin_db` back to 6.0** without re-checking
  the ERL 8 dB case — that is the value that made a normal voice inaudible.

---

## 11. Status

| | |
|---|---|
| Code | Complete — `c196cf9`, `4d82c19` |
| Tests | 169 passing, speakerphone suite 8/8 clean |
| Code review | Done — 2 findings, both fixed with regression tests |
| CodeRabbit | **NOT RUN** — extension installed but UI-only; no CLI, and I cannot drive the editor |
| Real-audio validation | **PENDING** |
| Pushed | **No** — `[ahead 5]` |
| Deployed | **No** |

**One unrelated flake, not fixed and not mine to hide:**
`tests/test_audio_quality.py::test_two_consecutive_low_quality_turns_offer_the_keypad`
fails roughly 1 run in 6. `soundfile` intermittently cannot open a clip just
written on Windows; the fail-open path then skips the quality gate and no
second keypad offer is recorded. It belongs to the audio-quality story, and
production degrades safely — but it is evidence that a transient file-open
failure silently disarms the quality floor for a turn, which is worth
hardening separately.
