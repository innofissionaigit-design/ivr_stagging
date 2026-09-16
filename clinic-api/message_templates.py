"""The registered message templates -- the ONLY text this service may send.

WHY THIS IS A SEPARATE FILE FROM THE SENDER
-------------------------------------------
Under TRAI's TCCCPR (the DLT regime every Indian operator enforces), a
transactional SMS is only delivered if three registered things line up:
the Principal Entity ID, the Header (sender ID), and a Content Template ID
whose approved body matches the message apart from its declared variables.
A message assembled from free text -- however correct -- is rejected by the
operator, not by us, and from the patient's point of view it is rejected
silently.

So the message bodies live here, in one file, in the same shape they are
registered in, and nothing else in the codebase is allowed to build a
message string. A compliance reviewer can read this file end to end without
reading any application logic, and diffing it against the DLT portal is a
text comparison rather than a code audit.

This is the same discipline reply_templates.py already applies to the
SPOKEN reply ("the LLM may decide what the caller wants, but the number in
the caller's ear always comes from the tool response"), extended to the
written one. There is no path by which a model-authored sentence reaches a
patient's handset.

VARIABLES
---------
DLT templates mark substitution points as `{#var#}`, positionally -- the
portal does not record variable NAMES. This module keeps a name list
alongside each body so the call sites are readable, and render() maps names
onto positions in the declared order. Two rules are enforced, both because
the operator enforces them and rejects the message otherwise:

  * every declared variable must be supplied, and the number of supplied
    values must equal the number of `{#var#}` markers in the body;
  * no single variable may exceed VAR_MAX_CHARS (30) -- the DLT limit.

TEMPLATE IDS ARE CONFIGURATION, NOT CODE
----------------------------------------
The 19-digit IDs below come from the hospital's own DLT registration and
differ per deployment, so they are read from the environment and default to
empty. An empty ID means "not registered yet", and notifications.py refuses
to send such a template in live mode rather than paying an operator to
reject it. See that module's preflight().

SEGMENT COUNT IS A REAL COST, NOT A DETAIL
------------------------------------------
These bodies are Bengali, so they encode as UCS-2: 70 characters per SMS
segment, not 160. Lengthening one of them silently multiplies the
hospital's per-message cost, so estimated_segments() exists to make that
visible in a test rather than on an invoice.
"""
from __future__ import annotations

import dataclasses
import os
import re

# The three moments the patient must be told about in writing. These
# strings are persisted on every NotificationAttempt row, so they are part
# of the stored data format -- change them and old rows stop matching.
EVENT_BOOKED = "booked"
EVENT_RESCHEDULED = "rescheduled"
EVENT_CANCELLED = "cancelled"

ALL_EVENTS = (EVENT_BOOKED, EVENT_RESCHEDULED, EVENT_CANCELLED)

# PREPARATION REMINDERS -- Author: Chakravardhan.
# Story: "As a patient with a fasting test tomorrow, I want a reminder tonight,
#         so that my visit is not wasted."
# Sent by reminder_service.py, not by an appointment event, and they carry
# different variables -- so they are deliberately NOT in ALL_EVENTS, whose
# members all render from the booking's own five values.
EVENT_REMINDER_VISIT = "reminder_visit"
EVENT_REMINDER_TEST = "reminder_test"

REMINDER_EVENTS = (EVENT_REMINDER_VISIT, EVENT_REMINDER_TEST)

# DLT's per-variable ceiling. A longer value is not truncated by the
# operator -- the whole message is rejected.
VAR_MAX_CHARS = 30

# UCS-2 (non-Latin) SMS segmenting. 70 chars for a single segment; a
# multi-segment message spends part of every segment on the concatenation
# header, leaving 67.
UCS2_SINGLE_SEGMENT = 70
UCS2_MULTI_SEGMENT = 67

_VAR_MARKER = "{#var#}"


class TemplateError(Exception):
    """The message could not be rendered into its registered form.

    Always a programming error at the call site (wrong variable count,
    oversized value, unknown event) -- never a patient-visible condition,
    and never a reason to fail the booking that triggered it.
    """


@dataclasses.dataclass(frozen=True)
class MessageTemplate:
    """One DLT-registered content template.

    `template_id` is the ID issued by the DLT portal for THIS body. It is
    submitted with the message; the operator matches the two and drops the
    message if they disagree.

    `category` is the registered category. Everything here is transactional
    -- a message about an appointment the patient themselves just made --
    which is why these are deliverable to DND-registered numbers at all.
    Nothing in this file may become promotional without a separate
    registration.
    """
    event: str
    template_id: str
    category: str
    body: str
    variables: tuple[str, ...]
    lang: str = "bn"

    @property
    def marker_count(self) -> int:
        return self.body.count(_VAR_MARKER)

    @property
    def is_registered(self) -> bool:
        """A template with no ID has not been through the DLT portal yet."""
        return bool(self.template_id.strip())


def _template_id(event: str) -> str:
    """Registered IDs come from the deployment, never from the repo.

    Committing a real 19-digit DLT ID would tie this source tree to one
    hospital's registration; committing a fake one would look registered
    while guaranteeing operator rejection. Empty is the honest default, and
    is what notifications.preflight() checks for.
    """
    return os.environ.get(f"HOSPITAL_GATEWAY_TEMPLATE_{event.upper()}", "").strip()


# ---------------------------------------------------------------------------
# The registered bodies.
#
# Each one ends by telling the patient that the message ITSELF is the thing
# to show at reception. That sentence is the whole point of the story this
# file implements: without it the patient still believes the reference
# number is what they are responsible for carrying.
# ---------------------------------------------------------------------------
_BODIES: tuple[MessageTemplate, ...] = (
    MessageTemplate(
        event=EVENT_BOOKED,
        template_id=_template_id(EVENT_BOOKED),
        category="transactional",
        body=(
            "{#var#}, আপনার অ্যাপয়েন্টমেন্ট কনফার্ম হয়েছে। "
            "ডাঃ {#var#}, {#var#}, {#var#}। "
            "রেফারেন্স {#var#}। রিসেপশনে এই মেসেজটি দেখান।"
        ),
        variables=("patient_name", "doctor_name", "date", "time_slot", "confirmation_id"),
    ),
    MessageTemplate(
        event=EVENT_RESCHEDULED,
        template_id=_template_id(EVENT_RESCHEDULED),
        category="transactional",
        body=(
            "{#var#}, অ্যাপয়েন্টমেন্ট বদলে {#var#}, {#var#} হয়েছে। "
            "ডাঃ {#var#}। রেফারেন্স {#var#} একই থাকছে। "
            "রিসেপশনে এই মেসেজটি দেখান।"
        ),
        variables=("patient_name", "date", "time_slot", "doctor_name", "confirmation_id"),
    ),
    MessageTemplate(
        event=EVENT_CANCELLED,
        template_id=_template_id(EVENT_CANCELLED),
        category="transactional",
        body=(
            "{#var#}, {#var#}, {#var#} সময়ের অ্যাপয়েন্টমেন্ট বাতিল হয়েছে। "
            "রেফারেন্স {#var#}। নতুন করে বুক করতে আবার ফোন করুন।"
        ),
        variables=("patient_name", "date", "time_slot", "confirmation_id"),
    ),
    # -- PREPARATION REMINDERS -- Author: Chakravardhan ----------------------
    # Each ends by saying how to stop them without a smartphone: tell the
    # phone line or the counter (reminder_service.opt_out is permanent).
    MessageTemplate(
        event=EVENT_REMINDER_VISIT,
        template_id=_template_id(EVENT_REMINDER_VISIT),
        category="transactional",
        body=(
            "{#var#}, রিমাইন্ডার: {#var#}, {#var#}-এ ডাঃ {#var#}। "
            "{#var#} রেফারেন্স {#var#}। "
            "আর না চাইলে ফোনে বা কাউন্টারে বলুন।"
        ),
        variables=("patient_name", "date", "time_slot", "doctor_name", "preparation", "confirmation_id"),
    ),
    MessageTemplate(
        event=EVENT_REMINDER_TEST,
        template_id=_template_id(EVENT_REMINDER_TEST),
        category="transactional",
        body=(
            "{#var#}, রিমাইন্ডার: {#var#}, {#var#}-এ পরীক্ষা। "
            "{#var#} {#var#} রেফারেন্স {#var#}। "
            "আর না চাইলে ফোনে বা কাউন্টারে বলুন।"
        ),
        variables=(
            "patient_name",
            "date",
            "time_slot",
            "preparation",
            "preparation_more",
            "confirmation_id",
        ),
    ),
)

TEMPLATES: dict[str, MessageTemplate] = {t.event: t for t in _BODIES}

# ---------------------------------------------------------------------------
# PREPARATION PHRASES -- Author: Chakravardhan
#
# The only words a preparation variable may hold. Each is short enough for one
# DLT variable (VAR_MAX_CHARS) with its value filled in, which
# tests/test_reminders.py asserts. Which tests need which phrase is decided in
# preparation.py; the WORDING lives here with every other patient-facing text.
#
# CLINICAL REVIEW REQUIRED before go-live: these are instructions to a patient
# about their body, and they are the clinic's policy, not this codebase's.
# ---------------------------------------------------------------------------
PREP_FAST_FROM = "fast_from"  # {time}: when the fast must begin
PREP_NO_ALCOHOL = "no_alcohol"
PREP_FIRST_URINE = "first_urine"
PREP_FULL_BLADDER = "full_bladder"
PREP_NONE = "none"
PREP_MORE_AT_COUNTER = "more_at_counter"
PREP_BRING_REPORTS = "bring_reports"
PREP_THANKS = "thanks"  # fills the second test variable when nothing more is needed

PREP_PHRASES: dict[str, str] = {
    PREP_FAST_FROM: "{time} থেকে শুধু জল খাবেন।",
    PREP_NO_ALCOHOL: "আগের ২৪ ঘণ্টা মদ্যপান নয়।",
    PREP_FIRST_URINE: "সকালের প্রথম প্রস্রাব আনবেন।",
    PREP_FULL_BLADDER: "ভরা মূত্রাশয়ে আসবেন।",
    PREP_NONE: "বিশেষ প্রস্তুতি লাগবে না।",
    PREP_MORE_AT_COUNTER: "বাকি নির্দেশ কাউন্টারে জানুন।",
    PREP_BRING_REPORTS: "পুরনো রিপোর্ট সঙ্গে আনবেন।",
    PREP_THANKS: "ধন্যবাদ।",
}


def prep_phrase(code: str, **values: str) -> str:
    """-> the registered wording for one preparation instruction."""
    phrase = PREP_PHRASES.get(code)
    if phrase is None:
        raise TemplateError(f"no preparation phrase {code!r}")
    return phrase.format(**values) if values else phrase


def _validate_registry() -> None:
    """Fail at import, not at the first patient.

    A body whose `{#var#}` count disagrees with its declared variable list
    renders a message the operator will reject for every single patient. An
    import-time check turns that from a production incident into a service
    that refuses to start.
    """
    for tpl in TEMPLATES.values():
        if tpl.marker_count != len(tpl.variables):
            raise TemplateError(
                f"template {tpl.event!r} declares {len(tpl.variables)} variables "
                f"but its body has {tpl.marker_count} {_VAR_MARKER} markers"
            )


_validate_registry()


def estimated_segments(text: str) -> int:
    """How many SMS segments `text` actually costs, UCS-2 encoded.

    Exposed so a test can assert a template did not silently grow past the
    segment count it was costed at. See the module docstring.
    """
    n = len(text)
    if n <= UCS2_SINGLE_SEGMENT:
        return 1
    return -(-n // UCS2_MULTI_SEGMENT)  # ceil


def render(event: str, values: dict) -> tuple[str, MessageTemplate]:
    """-> (message text, the template it was rendered from).

    Substitution is positional and single-pass: each `{#var#}` is replaced
    by the next declared variable's value, left to right. re.sub with a
    function is used rather than repeated str.replace so that a value which
    itself contains the literal `{#var#}` cannot shift the remaining
    positions -- a patient name is caller-supplied text that reached us
    through ASR, and it is not this function's job to trust it.

    Raises TemplateError for an unknown event, a missing variable, or an
    oversized one. Callers must treat that as "this message cannot be
    sent", NOT as "the appointment failed".
    """
    tpl = TEMPLATES.get(event)
    if tpl is None:
        raise TemplateError(f"no registered template for event {event!r}")

    ordered: list[str] = []
    for name in tpl.variables:
        if name not in values or values[name] is None:
            raise TemplateError(f"template {event!r} needs variable {name!r}")
        text = str(values[name]).strip()
        if not text:
            raise TemplateError(f"template {event!r} got an empty {name!r}")
        if len(text) > VAR_MAX_CHARS:
            raise TemplateError(
                f"template {event!r} variable {name!r} is {len(text)} chars, "
                f"over the DLT limit of {VAR_MAX_CHARS}"
            )
        ordered.append(text)

    it = iter(ordered)
    rendered = re.sub(re.escape(_VAR_MARKER), lambda _m: next(it), tpl.body)
    return rendered, tpl
