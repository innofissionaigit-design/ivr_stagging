# Every flow completes without a smartphone — implementation notes

**Story**

> Every flow completes without a smartphone

**Acceptance criteria**

> Every flow has a non-link completion path including payment and report
> collection, even where that means attending the counter. No flow dead-ends on
> a smartphone requirement.

**Also asked for in the same request:** the voice agent should understand
**Hindi, English and Bengali**. That is not part of this story's criteria — it
is a second, larger piece of work — and it is scoped and reported separately in
§7 below.

Branch `dev-chakravardhan`. **Committed locally, not pushed.** Suite: **294
passed** (224 pre-existing, 70 new).

---

## 1. Two things to check before reading further

**The GitHub URL you gave points at the wrong branch.**
`.../tree/dev_chakravardhan` (underscore) is **9,626 lines behind** your local
`dev-chakravardhan` (hyphen) — it has none of the barge-in, noisy-environment,
speakerphone or written-confirmation work. I worked on the local hyphen branch,
which is the one with the real code.

**No payment or report-collection flow existed.** This story is not a fix. A
repo-wide search for `payment`, `pay`, `upi`, `invoice`, `report_ready`,
`collect` found nothing. Those flows had to be built before they could have a
non-link completion path.

The good news from the same audit: **nothing caller-facing already required a
smartphone.** No URL, link, QR or app reference existed in any spoken reply or
SMS template. So this work is about *closing the gaps*, not removing links.

---

## 2. Verdict against the acceptance criteria

| Clause | State |
|---|---|
| Every flow has a **non-link completion path** | ✅ Enforced by a test over every string, not by inspection |
| …including **payment** | ✅ New `payment` intent + flow. Did not exist |
| …including **report collection** | ✅ New `report_collection` intent + flow. Did not exist |
| "even where that means **attending the counter**" | ✅ Counter is named first, as normal, not as an apology |
| **No flow dead-ends** on a smartphone requirement | ✅ Both new flows survive clinic-api being down |

The counter instructions themselves — opening hours, whether card is really
accepted, whether reception will really read a report over the phone — are
**clinic facts I invented placeholders for**. They need someone from the clinic
to confirm before this goes live. See §8.

---

## 3. How "every flow" is actually guaranteed

A criterion phrased over *every* flow cannot be met by checking today's flows —
it is broken by the next one somebody adds. So the guard is not a checklist.

`tests/test_no_smartphone.py` walks **every caller-facing string in every
language** and asserts none of them requires a smartphone:

```python
def test_no_caller_facing_string_requires_a_smartphone():
    bad = []
    for key, code, text in i18n.all_strings():
        for why in _offences(text):
            bad.append(f"{key} [{code}]: {why} -> {text!r}")
    assert not bad
```

A sentence added next month by somebody who never read the file is caught the
moment it is added. The banned patterns each carry a reason, so a future failure
says *why*, not just that a regex matched:

| Banned | Reason |
|---|---|
| `http://`, `www.`, `.com/.in/.org/.net` | a URL the caller must open |
| `link` / `লিঙ্ক` / `लिंक` | a link to tap |
| `QR`, `scan`, `স্ক্যান`, `स्कैन` | something to scan |
| `\bapps?\b`, `অ্যাপ(?!য়েন্ট)`, `ऐप` | a mobile app |
| `download`, `ডাউনলোড`, `डाउनलोड` | something to download |
| `portal`, `website`, `পোর্টাল`, `पोर्टल` | a site to visit |
| `click`, `ক্লিক`, `क्लिक` | something to click |

**The `অ্যাপ(?!য়েন্ট)` lookahead is load-bearing** — `অ্যাপয়েন্টমেন্ট`
(appointment) legitimately starts with those letters, and a naive pattern would
fail on every booking reply. Same for `\bapps?\b` versus "appointment".

Three separate surfaces are covered:

- **`agent/i18n.py`** — every spoken string
- **`clinic-api/message_templates.py`** — the SMS bodies. An SMS is fine on a
  feature phone; a **link inside** one is not, and adding one is the most
  natural "improvement" for somebody to make later
- **The reply functions themselves** — so a sentence assembled at runtime from
  several keys plus glue is checked as the caller actually hears it

---

## 4. The two new flows

### 4.1 Payment — `payment_reply()`

Before this, a caller asking *"কত টাকা লাগবে, কীভাবে দেব?"* got a price and
nothing about **how**. That is a flow with no completion path: the caller knows
the number and still does not know what to do next.

The obvious modern answer — text them a payment link — is exactly what the story
forbids, and would exclude every caller on a feature phone, which on this line
is a large share of them.

**What it says instead:**

> টাকা দিতে হবে শুধু কাউন্টারে, আসার দিন — নগদ বা কার্ডে। ফোনে কোনো টাকা দিতে
> হবে না। *[+ amount if a test was named]* অ্যাপয়েন্টমেন্ট
> রাখতে আগাম টাকা লাগে না। রসিদ কাউন্টারেই ছাপিয়ে হাতে দেওয়া হবে।

**No online option at all.** An earlier draft offered "cash, card or UPI" and
"nothing to do online beforehand". Both are gone: UPI needs a smartphone and
data, so naming it — even as one choice of three — tells a caller on a basic
handset that the real route is one they cannot use. The counter, in cash or by
card, is the only way to pay. `tests/test_no_smartphone.py` now bans UPI,
"online", payment-app names and net banking in every caller-facing string, in
all three languages.

### 4.2 Report collection — `report_collection_reply()`

Three completion paths, in the order a caller can actually use them:

1. **Collect a printed copy at the counter** — identified by *name and phone*,
   deliberately **not** by the reference number, so a caller who lost it is not
   turned away
2. **Ring in and have it read out** — for someone who cannot travel
3. **Send somebody else** — who needs only the patient's name and number

None needs a smartphone, an app, or a link.

### 4.3 The rule that makes them dead-end-proof

Both branches in the turn loop look like this:

```python
elif intent == "payment":
    # THIS BRANCH MUST NOT BE ABLE TO FAIL.
    result = {}
    if slots.get("test_name"):
        try:
            result = await _tools.get_test_rate(slots["test_name"])
        except ToolCallError as e:
            logger.warning(...)
    await _speak(session, payment_reply(slots, result, session.lang))
```

**A flow that answers "I couldn't check that right now" has dead-ended just as
surely as one that sends a payment link** — the caller is left holding nothing
either way. So the rate lookup is decoration. *How* you pay is clinic policy,
not a database row, and it survives clinic-api being unreachable. Two tests pin
that.

### 4.4 A third dead end, closed quietly

The booking flow had one that had nothing to do with links. When no SMS is going
(no gateway configured, or a send failure), the caller used to be left holding
only a 17-character reference read out once — a flow that completes **only for
somebody who can write it down**.

`_written_confirmation_clause()` now has a fallback branch:

> নম্বরটা মনে রাখতে না পারলেও চিন্তা নেই — রিসেপশনে আপনার নাম আর ফোন নম্বর বললেই
> ওঁরা অ্যাপয়েন্টমেন্ট খুঁজে দেবেন।

Same requirement wearing a different hat: complete without a smartphone **and**
without a good memory.

---

## 5. Old code → new code

### 5.1 `agent/reply_templates.py` — the strings moved out

**Old** — a literal per branch:

```python
def booking_reply(slots: dict, result: dict) -> str:
    if result.get("success"):
        return (f"আপনার অ্যাপয়েন্টমেন্ট কনফার্ম হয়েছে। "
                f"{_spoken_doctor_name(slots, result)}, {result['date']}, সময় {result['time_slot']}। "
                f"কনফার্মেশন নম্বর: {result['confirmation_id']}।"
                f"{_written_confirmation_clause(result)}")
```

**New** — same decision, looked-up words, language defaulted:

```python
def booking_reply(slots: dict, result: dict, lang: str | None = None) -> str:
    code = _lang(lang)
    if result.get("success"):
        return (t(code, "booking.success",
                  doctor=_spoken_doctor_name(slots, result, code),
                  date=result["date"], time=result["time_slot"],
                  cid=result["confirmation_id"])
                + _written_confirmation_clause(result, code))
```

**`lang` is last and defaults to `None`**, which resolves to the pod default
(Bengali). Every existing call site keeps working unchanged and keeps producing
byte-identical Bengali — asserted directly:

```python
def test_bengali_booking_confirmation_is_unchanged():
    expected = ("আপনার অ্যাপয়েন্টমেন্ট কনফার্ম হয়েছে। ডাঃ সেন, 2026-09-14, সময় 18:15। "
                "কনফার্মেশন নম্বর: KCD-20260914-0031।"
                " কনফার্মেশনের একটা মেসেজ আপনার ফোনে পাঠানো হচ্ছে, রিসেপশনে ওটা দেখালেই হবে।")
    assert booking_reply({}, _RESULT) == expected
```

### 5.2 `agent/slot_parse.py` — digits and words

**Old**

```python
_BN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
...
t = text.translate(_BN_DIGITS).strip()
```

**New**

```python
_DIGITS = _lang.DIGITS_TO_ASCII      # every supported script folded to ASCII
...
t = text.translate(_DIGITS).strip()
```

Bengali numerals are a subset of that table, so every Bengali path is unchanged.
What is added is Devanagari — which a Hindi ASR emits, and which would otherwise
fall straight through `re.sub(r"\D")` and leave a phone number **one digit
short**: a booking that silently cannot be messaged.

`_RELATIVE_DAYS`, `_WEEKDAYS_BN`, `_AFFIRMATIVE` and `_NEGATIVE` gained Hindi and
English entries. Yes/no stays **exact whole-utterance match**, which is why
adding short English forms (`ok`, `no`) is safe — `নাসরিন` still does not read as
a refusal.

### 5.3 A real bug found while testing this

`_RELATIVE_DAYS` is matched as a **substring**, and iterated in table order.
`"tomorrow"` is a substring of `"day after tomorrow"` — so it matched first and
**a caller asking for the day after tomorrow was booked a day early**, with
nothing in the transcript to show why.

The same shadowing was latent in Bengali (`কাল` inside `আগামীকাল`); it just
happened to be harmless because both map to the same offset.

**Fix** — walk longest-first, sorted once at import:

```python
_RELATIVE_DAYS_BY_LENGTH = tuple(sorted(_RELATIVE_DAYS.items(),
                                        key=lambda kv: -len(kv[0])))
```

### 5.4 `agent/tts.py` — a cache bug the language work exposed

**Old**

```python
key = self._key(spoken)
```

Two languages routinely produce byte-identical spoken strings — a bare time
(`18:15`), a confirmation id, a digit sequence read out one numeral at a time.
Keyed on text alone, **the first caller's Bengali audio would be replayed to the
next caller in Hindi** — and it would sound like a working system speaking the
wrong language, not like a bug.

**New**

```python
key = self._key(f"{_lang_mod.resolve(lang)}::{spoken}")
```

### 5.5 `tts_server.py` — `lang` was accepted and ignored

`SynthesizeRequest` has had a `lang` field all along; `CKPT` was hardcoded to
`/workspace/tts_checkpoints/bn` and every request got the Bengali voice.

Now: per-language checkpoints, **lazily loaded** (each FastPitch+HiFiGAN pair is
VRAM on a card also holding Qwen2.5 and IndicConformer), and — the important
part — the response says which language it **actually spoke**:

```python
headers={"X-TTS-Lang": spoken_lang, "X-TTS-Lang-Requested": (req.lang or "bn")}
```

Differing values mean the pod had no voice for the request and substituted the
default. Silently substituting a voice and reporting success is how a system
ends up believing it is multilingual when it is not.

### 5.6 `agent/asr.py` — a per-language registry

75 added lines, **nothing existing changed**. `register()`, `available()`,
`for_language()`. `main.py` registers the singleton it already builds under the
default language, so the registry never loads a second copy of a checkpoint
already resident.

### 5.7 `agent/llm.py` — two intents, trilingual prompt

- `VALID_INTENTS` gained `payment` and `report_collection`
- The prompt now says callers speak Bengali, Hindi or English and **mix them** —
  "an English clinical term inside a Bengali sentence is normal, not an error"
- The date rule resolves relative words in all three
- Smalltalk replies are asked for **in the caller's own language**

### 5.8 `main.py` / `main_pcm.py` — identical changes to both

- `CallSession.lang`, set from the pod default
- The two new intent branches
- Every reply call passes `session.lang` (8 call sites each)
- Four hardcoded Bengali fallbacks became lookups
- Language-switch handling, **before** `_continue_pending` and intent extraction

On that last point:

```python
switched = lang_mod.requested_switch(text)
if switched and switched != session.lang:
    session.lang = switched
    await _speak(session, language_switch_reply(session.lang))
    return
```

A caller who says "can you speak English" halfway through a booking is not
answering the question they were just asked. Running that through the date
parser gives a wrong slot or a re-prompt, and either way the request is ignored —
which reads as the system not having heard them. **`session.pending` is
untouched**, so the next turn resumes exactly where it was, in the new language.

---

## 6. New files

| File | Lines | Purpose |
|---|---:|---|
| `agent/i18n.py` | 502 | Every caller-facing sentence, 62 keys × 3 languages |
| `tests/test_multilingual.py` | 303 | 54 tests |
| `agent/language.py` | 278 | Language registry, detection, switching, digits |
| `tests/test_no_smartphone.py` | 248 | 16 tests — the story's guardrail |

**Modified:** `main.py` +114, `main_pcm.py` +114, `tts_server.py` +115,
`agent/reply_templates.py` +356/−161, `deploy/env.sh` +88, `deploy/env.vast.sh`
+78, `agent/asr.py` +75, `agent/slot_parse.py` +75, `agent/tts.py` +17,
`agent/llm.py` +14, `tests/test_audio_quality.py` +4.

---

## 7. Multilingual — what is real and what is not

**This is the part to read carefully, because the code is complete and the
capability is not.**

### What works today

- Language **plumbing** end to end: registry, per-language ASR/TTS selection,
  62 strings in three languages, trilingual slot parsing, explicit switching
- **Hindi and English text** is parsed correctly — dates, weekdays, Devanagari
  numerals, yes/no
- Every reply builder answers in the language asked for

### What does not work, and cannot without models

**ASR is `ai4bharat/indicconformer_stt_bn_hybrid_ctc_rnnt_large` — a
Bengali-only checkpoint.** It cannot transcribe Hindi or English. No code here
changes that.

That is why `language.enabled()` has this gate:

```python
if code == default_lang() or os.environ.get(spec.asr_checkpoint_env, "").strip():
    out.append(code)
```

**Configuration cannot claim a language whose ASR checkpoint is not set.**
Listing `hi` in `VOICE_AGENT_LANGUAGES` without `VOICE_AGENT_NEMO_FILE_HI` does
nothing at all — deliberately. A caller answered in Hindi by a Bengali-only ASR
is worse off than one answered in Bengali, because the system *sounds like it
understood them*.

The default is therefore `VOICE_AGENT_LANG_STRATEGY=fixed`,
`VOICE_AGENT_LANGUAGES=bn` — **exactly what the system did before this work**.

### To actually turn Hindi on

1. Get `ai4bharat/indicconformer_stt_hi_*` onto the pod → set
   `VOICE_AGENT_NEMO_FILE_HI`
2. Get a Hindi FastPitch/HiFiGAN pair under `<TTS_CKPT_ROOT>/hi` → set
   `TTS_SPEAKER_HI`
3. `VOICE_AGENT_LANGUAGES=bn,hi`
4. Decide detection: `fixed` + caller asks by name (cheap), or `parallel`
   (N decodes on turn one, on a shared GPU)

### The unsolved design question

**How the language of the *first* utterance is determined**, before the caller
has said anything you can classify. Three strategies are defined
(`fixed` / `script` / `parallel`) and only `fixed` is honest today:

- `script` reads the transcript's script — but a Bengali-only model returns
  Bengali glyphs for Hindi speech, so it would report `bn` for every caller
- `parallel` is the only true audio-based detection, and costs N decodes on turn
  one

I have **not** picked between them, because the right answer depends on GPU
headroom I cannot measure from here. `fixed` plus "caller asks by name" is what
ships.

---

## 8. Known gaps — flagged, not hidden

1. **The counter instructions are placeholders I wrote.** Payment methods,
   whether reception reads reports over the phone, whether a proxy can collect —
   **all need clinic confirmation.** The tests assert the *shape* of the promise,
   never its accuracy.
2. **Counter opening hours are not in the data.** `counter_fallback()` accepts an
   `hours` argument and nothing supplies one. It belongs in clinic-api as clinic
   data; I kept it out to avoid a second schema migration in two stories.
3. **Hindi and English wording is unreviewed.** Structurally correct, safe to
   test with, **not safe to put in front of patients**. Needs a speaker of each.
4. **No Hindi or English ASR/TTS checkpoint exists.** §7.
5. **First-turn language detection is unsolved.** §7.
6. **Nothing has run on a GPU.** No ASR, no TTS, no real call — you said you would
   test later.
7. **`bn_normalize.py` is still Bengali-only.** It expands digits for TTS
   (`agent/bn_normalize.py:91` reads IDs numeral by numeral). A Hindi or English
   reply gets Bengali-shaped normalisation. Not fixed because it needs a native
   speaker to specify what correct sounds like in each language.
8. **The pre-existing flaky test is now diagnosed.** Its log says
   `Error opening ...utt1.wav: System error` — a **Windows temp-file handle
   issue** in `test_audio_quality.py`, not the metrics singleton I first
   suspected, and it hits several tests in that file intermittently. Still
   unrelated to this branch; still not fixed here.
