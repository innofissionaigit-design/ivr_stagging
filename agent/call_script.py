"""The opening and closing words of every call -- disclosed, restated, pre-warmed.

Author: Chakravardhan
Story:  "As a caller, I want to know immediately who I have reached and that
         this is automated, so that I can decide how to use it."
Criteria: The greeting names the hospital and discloses the automated system
          in the caller language, and a closing restates what was done and
          what happens next. Both are pre-warmed so neither costs synthesis
          latency. The wording is reviewed by the clinical and legal leads.

WHAT CHANGED FROM THE OLD GREETING
----------------------------------
The line used to say "নমস্কার, কলকাতা কেয়ার ডায়াগনস্টিকসে স্বাগতম। কীভাবে
সাহায্য করতে পারি?" -- the hospital, but not that the caller is talking to a
machine, and only ever in Bengali. A caller who does not know it is automated
cannot decide whether to speak slowly, use the keypad, or go to the counter.
GREETING below names the hospital, says plainly that this is a voice AI bot
and not a person, and says what it can do, in each language the line
serves.

THE CLOSING IS BUILT FROM FIXED SENTENCES, NOT WRITTEN PER CALL
---------------------------------------------------------------
"Restates what was done" sounds like it needs a sentence composed for this
call -- and a composed sentence is a TTS cache miss, which is exactly the
synthesis latency the criterion rules out. So the closing is a short SEQUENCE
of fixed sentences, each pre-warmed on its own:

    what was done   one per outcome   booked / rescheduled / cancelled /
                                      records shared / questions answered /
                                      booking not finished / nothing done
    what happens    one                SMS on its way (only if one really is) /
    next                               give your name and number at the counter /
                                       call again or come to the counter
    goodbye         one                thanks for calling <hospital>

The details the caller needs (date, time, reference number) were already
spoken in the reply that confirmed them; the closing does not read them again,
so it needs nothing that is not in the cache. Which sentences are chosen comes
from what the clinic API actually returned on this call (CallOutcomes), never
from what the caller said or what the model guessed -- the closing must not
claim a booking the backend did not confirm, nor promise an SMS that is not on
its way (the same rule as reply_templates._written_confirmation_clause).

"IN THE CALLER LANGUAGE"
------------------------
The greeting is spoken in the call's language as it stands when the call
opens -- the pod's default, the only thing known before the caller has said a
word. The closing is spoken in the call's language as it stands at the end,
which follows the caller if they asked for, or were identified as speaking,
another language. Every line is pre-warmed in every language this pod serves.

CLINICAL AND LEGAL REVIEW
-------------------------
REVIEW records who approved the wording and the SHA-256 of exactly the
wording they approved. Change a single character and review_status() reports
the approval as no longer matching -- an approval is for words, not for a
file. Until both leads have signed, the line still works (a bench pod must be
usable) but startup logs a warning and /api/health shows the script as
pending review. See IMPLEMENTATION_CALL_GREETING_CLOSING_OLD_VS_NEW.md for the
wording as submitted for review.

Nothing here requires a smartphone, a link or an app -- tested with the same
checker as agent/i18n.py.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging

import httpx

from agent import call_audit
from agent import language as lang_mod

log = logging.getLogger("agent.call_script")

HOSPITAL_NAME = {
    "bn": "কলকাতা কেয়ার ডায়াগনস্টিকস",
    "hi": "कोलकाता केयर डायग्नोस्टिक्स",
    "en": "Kolkata Care Diagnostics",
}

# ---------------------------------------------------------------------------
# THE WORDING. Submitted for clinical and legal review -- see REVIEW below.
# ---------------------------------------------------------------------------
GREETING = {
    "bn": (
        "নমস্কার, কলকাতা কেয়ার ডায়াগনস্টিকস। এটি একটি ভয়েস এআই বট, "
        "আমি মানুষ নই। টেস্টের দাম, ডাক্তারের সময় আর অ্যাপয়েন্টমেন্টে সাহায্য করতে পারি। "
        "বলুন, কী জানতে চান?"
    ),
    "hi": (
        "नमस्ते, कोलकाता केयर डायग्नोस्टिक्स। यह एक वॉइस एआई बॉट है, "
        "मैं इंसान नहीं हूँ। टेस्ट की कीमत, डॉक्टर का समय और अपॉइंटमेंट में मदद कर सकता हूँ। "
        "बताइए, क्या जानना है?"
    ),
    "en": (
        "Hello, this is Kolkata Care Diagnostics. This is a voice AI bot, "
        "not a person. I can help with test prices, doctor timings and appointments. "
        "How can I help you?"
    ),
}

# What was done -- one sentence per outcome.
DONE_BOOKED = "done.booked"
DONE_RESCHEDULED = "done.rescheduled"
DONE_CANCELLED = "done.cancelled"
DONE_RECORDS = "done.records"
DONE_ANSWERED = "done.answered"
DONE_BOOKING_UNFINISHED = "done.booking_unfinished"
DONE_NOTHING = "done.nothing"
# What happens next.
NEXT_SMS = "next.sms"
NEXT_COUNTER = "next.counter"
NEXT_CALL_AGAIN = "next.call_again"
# Goodbye.
GOODBYE = "goodbye"

CLOSING: dict[str, dict[str, str]] = {
    DONE_BOOKED: {
        "bn": "আজ এই কলে আপনার অ্যাপয়েন্টমেন্ট বুক করা হয়েছে।",
        "hi": "आज इस कॉल में आपका अपॉइंटमेंट बुक किया गया है।",
        "en": "On this call, your appointment was booked.",
    },
    DONE_RESCHEDULED: {
        "bn": "আজ এই কলে আপনার অ্যাপয়েন্টমেন্টের সময় বদলানো হয়েছে।",
        "hi": "आज इस कॉल में आपके अपॉइंटमेंट का समय बदला गया है।",
        "en": "On this call, your appointment was moved.",
    },
    DONE_CANCELLED: {
        "bn": "আজ এই কলে আপনার অ্যাপয়েন্টমেন্ট বাতিল করা হয়েছে।",
        "hi": "आज इस कॉल में आपका अपॉइंटमेंट रद्द किया गया है।",
        "en": "On this call, your appointment was cancelled.",
    },
    DONE_RECORDS: {
        "bn": "আজ এই কলে আপনার রেকর্ডের তথ্য জানানো হয়েছে।",
        "hi": "आज इस कॉल में आपके रिकॉर्ड की जानकारी दी गई है।",
        "en": "On this call, you were told about your records.",
    },
    DONE_ANSWERED: {
        "bn": "আজ এই কলে আপনার প্রশ্নের উত্তর দেওয়া হয়েছে, কোনো বুকিং করা হয়নি।",
        "hi": "आज इस कॉल में आपके सवाल का जवाब दिया गया, कोई बुकिंग नहीं हुई।",
        "en": "On this call, your questions were answered; nothing was booked.",
    },
    DONE_BOOKING_UNFINISHED: {
        "bn": "আপনার বুকিং শেষ হয়নি, তাই কোনো অ্যাপয়েন্টমেন্ট করা হয়নি।",
        "hi": "आपकी बुकिंग पूरी नहीं हुई, इसलिए कोई अपॉइंटमेंट नहीं बना।",
        "en": "Your booking was not finished, so no appointment was made.",
    },
    DONE_NOTHING: {
        "bn": "আজ এই কলে কোনো বুকিং বা পরিবর্তন করা হয়নি।",
        "hi": "आज इस कॉल में कोई बुकिंग या बदलाव नहीं हुआ।",
        "en": "Nothing was booked or changed on this call.",
    },
    NEXT_SMS: {
        "bn": "নিশ্চিতকরণ মেসেজ আপনার ফোনে যাবে, কাউন্টারে সেটি দেখাবেন।",
        "hi": "पुष्टि का मैसेज आपके फ़ोन पर आएगा, काउंटर पर उसे दिखाइए।",
        "en": "A confirmation message will come to your phone; please show it at the counter.",
    },
    NEXT_COUNTER: {
        "bn": "আসার দিন কাউন্টারে আপনার নাম আর ফোন নম্বর বললেই হবে।",
        "hi": "आने के दिन काउंटर पर अपना नाम और फ़ोन नंबर बता दीजिए।",
        "en": "On the day, give your name and phone number at the counter.",
    },
    NEXT_CALL_AGAIN: {
        "bn": "আর কিছু লাগলে আবার ফোন করুন, অথবা কাউন্টারে আসুন।",
        "hi": "और कुछ चाहिए तो फिर से फ़ोन करें, या काउंटर पर आइए।",
        "en": "If you need anything else, call again or come to the counter.",
    },
    GOODBYE: {
        "bn": "কলকাতা কেয়ার ডায়াগনস্টিকসে ফোন করার জন্য ধন্যবাদ।",
        "hi": "कोलकाता केयर डायग्नोस्टिक्स को फ़ोन करने के लिए धन्यवाद।",
        "en": "Thank you for calling Kolkata Care Diagnostics.",
    },
}


# ---------------------------------------------------------------------------
# Clinical and legal review
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class ScriptReview:
    """Who approved the wording, and exactly which wording they approved."""

    version: str
    wording_sha256: str | None  # of wording_fingerprint() at approval; None = never approved
    clinical_lead: str | None
    legal_lead: str | None
    approved_on: str | None  # ISO date


# Filled in by the leads' sign-off, not by a developer. Until then: pending.
REVIEW = ScriptReview(
    version="2026-09-15-draft-1",
    wording_sha256=None,
    clinical_lead=None,
    legal_lead=None,
    approved_on=None,
)


def wording_fingerprint() -> str:
    """SHA-256 of every word a caller can hear from this module, in a stable order."""
    payload = json.dumps({"greeting": GREETING, "closing": CLOSING}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def review_status(review: ScriptReview = REVIEW) -> dict:
    current = wording_fingerprint()
    signed = bool(review.clinical_lead and review.legal_lead and review.approved_on)
    matches = review.wording_sha256 == current
    return {
        "version": review.version,
        "approved": signed and matches,
        "clinical_lead_signed": bool(review.clinical_lead),
        "legal_lead_signed": bool(review.legal_lead),
        "approved_on": review.approved_on,
        "wording_matches_approval": matches,
        "wording_sha256": current,
    }


# ---------------------------------------------------------------------------
# What the call actually did -- from the clinic API's own answers
# ---------------------------------------------------------------------------
_LOOKUPS = ("get_test_rate", "get_doctor_availability", "get_doctors_by_department")
_BOOKING_STATES = ("doctor_choice", "department_date", "date", "time_slot", "patient_name", "phone")


@dataclasses.dataclass
class CallOutcomes:
    booked: bool = False
    rescheduled: bool = False
    cancelled: bool = False
    records_shared: bool = False
    answered: bool = False
    # Only "queued" earns the SMS promise -- reply_templates' own rule.
    message_on_its_way: bool = False

    def note(self, action: str, result: object) -> None:
        """Record one clinic-API answer. Never raises: a closing is not worth a crash."""
        if not isinstance(result, dict):
            return
        if action in _LOOKUPS and "found" in result:
            self.answered = True
        elif action == "read_history" and result.get("found"):
            self.records_shared = True
        elif action in ("book_appointment", "reschedule_appointment", "cancel_appointment"):
            if not result.get("success"):
                return
            if action == "book_appointment":
                self.booked = True
            elif action == "reschedule_appointment":
                self.rescheduled = True
            else:
                self.cancelled = True
            status = (result.get("notification") or {}).get("status")
            if status == "queued":
                self.message_on_its_way = True


def closing_keys(outcomes: CallOutcomes, pending: dict | None) -> list[str]:
    """-> the sentences of this call's closing, in order."""
    done = []
    if outcomes.booked:
        done.append(DONE_BOOKED)
    if outcomes.rescheduled:
        done.append(DONE_RESCHEDULED)
    if outcomes.cancelled:
        done.append(DONE_CANCELLED)
    if outcomes.records_shared:
        done.append(DONE_RECORDS)
    unfinished = (pending or {}).get("awaiting") in _BOOKING_STATES
    if unfinished:
        done.append(DONE_BOOKING_UNFINISHED)
    if not done:
        done.append(DONE_ANSWERED if outcomes.answered else DONE_NOTHING)

    appointment_changed = outcomes.booked or outcomes.rescheduled
    if appointment_changed and outcomes.message_on_its_way:
        following = NEXT_SMS
    elif appointment_changed:
        following = NEXT_COUNTER
    else:
        following = NEXT_CALL_AGAIN
    return [*done, following, GOODBYE]


# ---------------------------------------------------------------------------
# The lines, and pre-warming them
# ---------------------------------------------------------------------------
def greeting(lang: str | None) -> str:
    return GREETING[lang_mod.resolve(lang)]


def closing(
    outcomes: CallOutcomes, pending: dict | None, lang: str | None, *, include_goodbye: bool = True
) -> list[str]:
    """The closing sentences in the call's language. `include_goodbye=False` is
    for a path that already ends with its own thanks (the idle timeout)."""
    code = lang_mod.resolve(lang)
    keys = closing_keys(outcomes, pending)
    if not include_goodbye:
        keys = [k for k in keys if k != GOODBYE]
    return [CLOSING[key][code] for key in keys]


# The termination reason recorded when the CALLER asks to end the call and hears
# the closing first (control message {"type": "end_call"}).
END_CALLER_ENDED = "caller_ended"


class ObservedCallAudit(call_audit.CallAudit):
    """The call's own CallAudit, which also remembers what the clinic API said.

    Every clinic-API call on a voice call already passes through
    CallAudit.api_call() (agent/tools_client.py's @_audited), so observing it
    here gives the closing the backend's own answers without touching the
    tools client or a single call site."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.outcomes = CallOutcomes()

    async def api_call(self, action: str, request: dict, call, summarize=None):
        async def observed():
            result = await call()
            self.outcomes.note(action, result)
            return result

        return await super().api_call(action, request, observed, summarize)


def lines_for(lang: str) -> list[str]:
    """Every sentence this module can speak in `lang` -- what pre-warm synthesizes."""
    return [GREETING[lang], *(CLOSING[key][lang] for key in CLOSING)]


# Which languages were fully synthesized at startup, for /api/health.
_WARM: dict[str, bool] = {}


async def prewarm(tts) -> dict[str, bool]:
    """Synthesize the greeting and every closing sentence, in every language the
    line serves, into the TTS cache. Best-effort like TTSClient.prewarm(): a
    failure is logged and the call still works, the first caller simply pays
    synthesis latency for that line."""
    # A fresh record each time: a language this pod no longer serves must not
    # go on being reported as warm from an earlier run.
    _WARM.clear()
    for code in lang_mod.enabled():
        warm = True
        for line in lines_for(code):
            try:
                await tts.synthesize(line, code)
            # What TTSClient.synthesize() is known to raise: the TTS server
            # unreachable, slow or refusing (httpx), or a socket-level failure.
            except (httpx.HTTPError, OSError, RuntimeError, ValueError) as e:
                log.warning("call script prewarm failed for %s: %s", code, type(e).__name__)
                warm = False
                break
        _WARM[code] = warm
    status = review_status()
    if not status["approved"]:
        log.warning(
            "call greeting/closing wording %s is NOT approved by the clinical and legal leads",
            status["version"],
        )
    return dict(_WARM)


def health() -> dict:
    return {"prewarmed": dict(_WARM), "review": review_status()}
