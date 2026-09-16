"""Composes the spoken reply from TOOL DATA, never from the LLM's
own words, for any intent where a fact (a price, a date, a confirmation
ID) is at stake.

This is the same discipline voicerx/gate.py already applies to drug names
("the SLM proposes, the gazetteer decides") ported to this domain: the LLM
may decide WHAT the caller wants and WHICH slots it heard, but the actual
number in the caller's ear always comes from the clinic-api response,
substituted into a fixed template. The model never gets a chance to
misremember or round a price it was merely shown a moment ago.

Only "smalltalk" skips this file entirely and uses the LLM's own
direct_reply_bn -- there is no fact to get wrong in "নমস্কার" or "ধন্যবাদ".

LANGUAGE SUPPORT:
- Primary: Bengali (বাংলা) - default for TTS synthesis
- Secondary: English - for English-speaking callers
- Tertiary: Hinglish (Hindi-English mix) - for mixed-language contexts
- Also supported by the booking-confirmation prompts (below): Banglish
  (Bengali-English mix) -- Kolkata callers code-switch Bengali with
  English at least as often as Hindi with English, and "hinglish" here
  is Hindi vocabulary, not Bengali, so a Bengali-English code-switcher
  needs its own branch rather than being folded into "hinglish".
All templates preserve exact values regardless of language.

SPOKEN PUNCTUATION ("Answers sound like a person, not a database row"):
No template here ever emits a field label followed by a colon (e.g.
"Sample: X", "Confirmation number: Y") -- a spoken colon or bracket reads
to a caller as a database field, not a sentence, which is exactly what
this story bans. Every value is folded into a natural clause instead
("You'll need to give a X sample.", "Your confirmation number is Y.").
This is unrelated to the colons inside raw values like a 24h time
("14:30") -- those are consumed by agent/bn_normalize.py's verbalize()
at synthesis time (see tts.py) before anything is actually spoken, so
they were never the problem; only a literal label-colon that survives
verbalize() unchanged is. test_reply_templates_fidelity.py enforces this
across every function and language with an automated check that runs
output through verbalize() and fails the build if a colon or bracket
survives.
"""

from __future__ import annotations

import re

# ADDED BY CHAKRAVARDHAN -- used only by the block of new functions near the
# end of this file (payment_reply onward: patient verification, history/
# bookings disclosure, language-switch and counter-fallback replies). Those
# functions are genuinely new, not a rewrite of anything above, and their
# only call sites (main.py/main_pcm.py) always pass the language positionally
# (`session.lang`), never as the keyword `lang=`, so keeping their original
# agent.i18n.t()-based implementation here does not conflict with the rest
# of this file's inline per-language template convention.
from agent import language as lang_mod
from agent.i18n import t, available_languages_phrase


def _lang(lang: str | None) -> str:
    """One place that turns "whatever the caller passed" into a language
    this pod can actually serve. None -> the pod default (Bengali)."""
    return lang_mod.resolve(lang)


# STORY [Answer Quality and Grounding]
# As a patient, I want to hear the whole sentence, so that I am
# not left guessing what the agent tried to say.
# Spoken instead of a reply that contains a span the synthesizer would drop
# (agent/speakability.py). Three deliberate choices in one sentence:
#
#  * It admits the agent's own limit rather than blaming the line or the
#    caller. The failure is a missing spoken form on this side.
#  * It offers the counter, matching the repair ladder main.py already uses
#    after repeated low-agreement turns. It does NOT say "স্টাফের কাছে দিচ্ছি"
#    the way the tool_failure clip does -- that claims a live transfer this
#    system does not implement, and a promise the agent cannot keep is a
#    worse outcome than the hole it replaced.
#  * It is pure Bengali, so it can never itself trip the gate it serves.
#    main.py asserts exactly that at startup; see _assert_canned_lines_speakable.
UNSPEAKABLE_ESCALATION = "দুঃখিত, এই তথ্যটা আমি ঠিকভাবে বলে উঠতে পারছি না। কাউন্টারে একবার কথা বলে নিলে ভালো হয়।"


# story title: Answers sound like a person, not a database row
# user story: As a patient, I want to hear a sentence, so that the agent
#   sounds like someone at the counter.
# acceptance criteria: No field label, colon or bracket is ever spoken and
#   every structured value renders as a natural clause in the reply
#   language. An automated check fails a build containing a spoken
#   punctuation artefact.
#
# A list a person would say out loud: "A, B নাকি C", not "A, B, C". The last
# item gets the disjunction because every place this is used is offering the
# caller a choice, and a bare comma before the final option reads as a
# database row being recited rather than a question being asked.
# story title: The agent says it cannot confirm rather than guessing
# user story: As a caller, I want to be told plainly when the system
#   cannot verify something, so that I am not given a confident guess.
# acceptance criteria: The insufficient-verified-information outcome has
#   its own template per language, its own metric and its own escalation
#   path, distinct from not-found and from an infrastructure apology. Its
#   rate is reported per intent because a rise means a data or
#   integration problem.
#
# ITS OWN TEMPLATE, and it lives HERE rather than in agent/outcomes.py as it
# does on dev_sourav. Every spoken sentence in this branch is in this file, and
# two gates scan it -- tests/test_spoken_punctuation.py and
# tests/test_fact_provenance.py. A sentence defined anywhere else is a sentence
# neither gate can see, which is how a spoken colon or a fabricated fact gets
# back in.
#
# It must differ from BOTH of the other two outcomes, and the difference is
# what the caller is told to DO:
#   not-found     -> the thing does not exist. Stop asking.
#   unreachable   -> could not check. Call back.
#   this          -> it IS done, the agent just cannot read the details back.
#                    Do NOT rebook. Someone will follow up.
# Saying "call back" here would invite a duplicate booking of an appointment
# that already exists, which is the specific harm this outcome prevents.
INSUFFICIENT_VERIFIED_INFORMATION_BN = "আপনার অ্যাপয়েন্টমেন্টটা হয়ে গেছে, কিন্তু বিস্তারিত তথ্যগুলো এই মুহূর্তে মিলিয়ে দেখতে পারছি না। আমাদের থেকে আপনাকে জানানো হবে। আবার বুক করার দরকার নেই।"


# story title: The same question gets the same answer within one call
# user story: As a caller who asks twice, I want the same answer, so that I
#   know which one to believe.
# acceptance criteria: Repeating a question in one call produces an identical
#   factual answer unless the underlying data changed, in which case the
#   change is stated. A test asserts consistency across three repeats with an
#   unchanged backend.
#
# THE CHANGE IS STATED -- and it deliberately does not restate the old value.
#
# The tempting version names both figures: more informative on paper, and
# worse on a phone line. A caller who catches only half of it has then been
# read a number that is no longer true, by the one sentence whose entire job
# is to stop them believing a stale figure. A change statement should contain
# exactly one number -- the current one -- and the freshly rendered reply that
# follows carries it.
#
# It therefore states no fact of its own and needs no verified response to
# render from, which is why it is a bare constant rather than a function
# taking the previous answer.
ANSWER_CHANGED_BN = "একটু আগে আমি অন্য তথ্য বলেছিলাম, এইমাত্র দেখে নিলাম সেটা বদলে গেছে।"


# story title: Near matches are offered rather than guessed or refused
# user story: As a caller naming something loosely, I want the close matches
#   offered, so that I am not told my test does not exist when it does.
# acceptance criteria: When several catalogue rows fall within the match band
#   the agent offers up to three by name and asks which. Candidates are
#   generated across every supported language and romanised spelling. The
#   did-you-mean path covers the ambiguous case and not only total failure.
#
# Said when the caller named something that matches several catalogue rows,
# or one row not clearly enough to act on. Used by all three read intents, so
# an ambiguous test, doctor and department are asked about in the same voice.
#
# The fallback is a RE-ASK, not an apology and not a not-found. An empty
# candidate list means the clinic found several rows and at least one of them
# has no Bengali alias -- clinic-api refuses to offer a partial list, because
# dropping a candidate turns "which of these two" back into "did you mean
# this one", which is a guess wearing a question mark. Telling the caller it
# does not exist would be false; asking them to say it again is true.
NEAR_MATCH_UNCLEAR_BN = "দুঃখিত, ঠিক কোনটার কথা বলছেন বুঝতে পারিনি। একটু পরিষ্কার করে নামটা বলবেন?"


def near_match_prompt(candidates) -> str:
    """Offer up to three named candidates and ask which one.

    Reads the SPOKEN name, never the catalogue's English label: the Bengali
    tokenizer drops Latin script, and a question offering two silences is
    worse than no question. clinic-api caps the list at three; this does not
    re-cap it, so a cap change lives in one place.
    """
    spoken = [c.get("name_bn") for c in (candidates or []) if c.get("name_bn")]
    if not spoken:
        return NEAR_MATCH_UNCLEAR_BN
    return f"আপনি কি {_spoken_list(spoken)} বলতে চাইছেন?"


# story title: A multi-part question is answered in full
# user story: As a caller who asked two things, I want both answered, so that
#   I do not have to ask again.
# acceptance criteria: Every answerable part of a turn is answered in the
#   order asked, and any part that cannot be answered is explicitly addressed
#   rather than dropped. Completeness is scored on a labelled multi-part set.
#
# THE "EXPLICITLY ADDRESSED RATHER THAN DROPPED" HALF, in three sentences.
#
# None of them counts an ordinal. "আপনার দ্বিতীয় প্রশ্ন" is wrong the moment
# the parse is off by one -- and being off by one is exactly the state the
# agent is in when it is apologising for not understanding something. Where
# the part named a thing, the thing is said; where it did not, the sentence
# stays general rather than claiming a position in a list it may have
# miscounted.
#
# The boundary with E12-S5 (dead ends always offer a next step, ABSENT) is
# worth keeping visible: these say "I heard it and could not serve it". What
# to do instead is that story's job, and folding it in here would build half
# of it badly.
UNANSWERED_PART_BN = "আপনার আরেকটা প্রশ্ন ছিল, সেটা ঠিক বুঝতে পারিনি। একটু বলবেন?"

DEFERRED_PART_BN = "আপনার আরেকটা প্রশ্নও ছিল, সেটা এক্ষুনি দেখে বলছি।"

RESUMING_PART_BN = "এবার আপনার অন্য প্রশ্নটা।"


def unanswered_part_prompt(subject: str | None = None) -> str:
    """Said about a part that was heard and cannot be served.

    `subject` is the caller's OWN words for the thing, when the part named
    one -- echoed rather than translated, the same choice _spoken_test_name
    makes, and for the same reason: the catalogue's English label would be
    dropped by the tokenizer and the caller would hear a sentence with a hole
    where the subject belongs.
    """
    if not subject:
        return UNANSWERED_PART_BN
    return f"'{subject}' নিয়ে আপনার প্রশ্নটার উত্তর এই মুহূর্তে দিতে পারছি না।"


def with_change_notice(reply: str) -> str:
    """Prefix a freshly rendered reply with the change statement.

    A function rather than an f-string at the call site, so the two can never
    be joined without the space, and so the punctuation and speakability gates
    see the joined sentence exactly as the caller will hear it.
    """
    return f"{ANSWER_CHANGED_BN} {reply}"


def _spoken_list(items) -> str:
    items = [str(i) for i in items if i]
    if len(items) <= 1:
        return items[0] if items else ""
    return f"{', '.join(items[:-1])} নাকি {items[-1]}"


from agent.bn_normalize import detect_language

# ADDED BY SOURAV -- "Caller asks when a doctor sits" story:
# doctor_schedule_reply() below speaks a doctor's weekly sitting days by
# name, in whichever of the 4 reply languages it was asked for. See that
# function's docstring for how this differs from doctor_availability_reply
# just above it.
from agent.bn_normalize import weekday_to_words

# ADDED BY SOURAV -- real production bug fix: a caller asking "how long
# does it take to get the urine test report" was being answered with the
# test's PRICE instead ("Urine test rate is 200 taka."). Root cause:
# test_rate_reply() above was narrowed by an earlier story ("Caller asks
# the price of a test") to speak ONLY the price, and that story's own
# docstring explicitly flagged that this orphaned bn_normalize.
# hours_to_duration_phrase() -- built and unit-tested by an even earlier
# story ("Caller asks how long results take") -- with no caller-visible
# path left to reach it. test_duration_reply() below is that missing
# path: a new, dedicated intent that reuses this exact same primitive,
# never rebuilds it. See test_duration_reply()'s own docstring for the
# full bug writeup.
from agent.bn_normalize import hours_to_duration_phrase

# Fallback word for "the doctor" when no name is available at all, per
# language -- see _spoken_doctor_name() below.
_DOCTOR_FALLBACK = {
    "bengali": "ডাক্তার",
    "english": "the doctor",
    "hinglish": "doctor",
    "banglish": "doctor",
}

# Same idea for a test's name -- see _spoken_test_name() below.
_TEST_FALLBACK = {
    "bengali": "টেস্ট",
    "english": "the test",
    "hinglish": "test",
    "banglish": "test",
}


def _spoken_test_name(slots: dict, result: dict, language: str = "bengali") -> str:
    """What the caller HEARS as the test's name.

    `language` was added for "Caller asks the price of a test" (Epic:
    Conversation -- Information and Enquiry), fixing a real, measured bug
    found while verifying that story: this function used to have NO
    language parameter at all and unconditionally preferred the seeded
    Bengali alias, so an English/Hinglish/Banglish caller asking about any
    test that has a Bengali alias (most of them) heard raw Bengali script
    glued into an otherwise-English sentence -- e.g. "সিবিসি test rate is
    400 rupees." This is the exact same bug class _spoken_doctor_name()
    above was already fixed for; this function just never got the same
    treatment. Confirmed by actually running test_rate_reply() against a
    real seeded test with a real alias, not assumed.

    Order matters, per language:
    - bengali (default): the Bengali TTS tokenizer drops Latin script
      outright, so putting the English name in a Bengali sentence removes
      it from the reply entirely -- prefer the seeded Bengali alias;
      failing that, echo the caller's own words back.
    - english / hinglish / banglish: the Bengali alias is exactly as
      unreadable in one of these sentences as "ডাঃ" would be in
      _spoken_doctor_name()'s non-Bengali branch -- never use it here,
      even when available. Use the caller's own words, or the catalogue's
      English name, instead.
    """
    if language == "bengali":
        return (
            result.get("test_name_bn")
            or slots.get("test_name")
            or result.get("test_name")
            or _TEST_FALLBACK["bengali"]
        )
    name = slots.get("test_name") or result.get("test_name")
    return name or _TEST_FALLBACK.get(language, _TEST_FALLBACK["english"])


def _name_already_says_test(name: str) -> bool:
    """ "Caller asks what sample is needed" AC (word "test"/"টেস্ট" must
    never come up twice). _spoken_test_name() can fall through to the
    catalogue's plain English test_name (e.g. "Widal Test") when no
    Bengali alias is available for that row -- checking only "টেস্ট" in a
    supposedly-Bengali sentence misses that case, since the name itself
    would still be Latin script. Checking both scripts, regardless of
    which language branch is calling, closes that gap in every language,
    not just English."""
    return "test" in name.lower() or "টেস্ট" in name


_VOWEL_SOUNDS = ("a", "e", "i", "o", "u")


def _a_or_an(word: str) -> str:
    """Indefinite article for a catalogue value we don't control the
    spelling of ("Imaging" needs "an", "Blood" needs "a")."""
    return "an" if word[:1].lower() in _VOWEL_SOUNDS else "a"


def _spoken_doctor_name(slots: dict, result: dict, language: str = "bengali") -> str:
    """Same problem, same order. Aliases are seeded as surnames ("সেন"),
    so this adds the honorific the English label already carried.

    `language` was added for the booking-confirmation readback
    (booking_confirmation_prompt, below): previously this function always
    returned the Bengali "ডাঃ" honorific regardless of which language the
    surrounding sentence was in, because its only two callers
    (doctor_availability_reply, booking_reply) never passed a language
    through even though they already accept one. That glued a Bengali
    prefix into the middle of an English or Hinglish sentence whenever
    those branches were used. Passing `language="bengali"` (the default,
    and the only value either of those two callers has ever actually
    used in production) reproduces the exact previous output -- this is
    additive, not a behaviour change for existing Bengali call sites.
    """
    alias = result.get("doctor_name_bn") or slots.get("doctor_name_bn")
    if language == "bengali":
        if alias:
            return f"ডাঃ {alias}"
        return slots.get("doctor_name") or result.get("doctor_name") or _DOCTOR_FALLBACK["bengali"]

    # english / hinglish / banglish: "ডাঃ" does not belong in a sentence
    # that is otherwise English or transliterated -- "Dr." is the
    # honorific a bilingual caller actually expects here. The Bengali
    # alias (pure Bengali script, e.g. "সেন") is deliberately NOT used in
    # this branch even when available -- it would be exactly as
    # unreadable/unspeakable in an English or transliterated sentence as
    # the "ডাঃ" prefix would be, just in the other direction.
    name = slots.get("doctor_name") or result.get("doctor_name")
    if not name:
        return _DOCTOR_FALLBACK.get(language, _DOCTOR_FALLBACK["english"])
    # clinic-api/seed.py seeds every doctor's canonical name WITH its own
    # "Dr." already ("Dr. A. Sen") -- prepending another one here would
    # speak "Dr. Dr. A. Sen". Only add the honorific when the name
    # doesn't already carry one.
    if re.match(r"^dr\.?\s", name.strip(), flags=re.IGNORECASE):
        return name
    return f"Dr. {name}"


def missing_slot_prompt(intent: str, missing: str, language: str = "bengali") -> str:
    """Generate a prompt for missing slot information in the specified language."""
    if language == "english":
        prompts = {
            ("test_rate", "test_name"): "Which test rate would you like to know?",
            ("test_sample", "test_name"): "Which test's sample were you asking about?",
            # ADDED BY SOURAV -- real production bug fix (see
            # test_duration_reply()'s docstring). Same wording pattern as
            # test_sample just above -- "which test" is identical
            # regardless of whether the caller then wants the sample or
            # the report turnaround time.
            ("test_duration", "test_name"): "Which test's report time were you asking about?",
            # ADDED BY SOURAV -- "Caller asks how to prepare for a test" story.
            ("test_preparation", "test_name"): "Which test's preparation instructions were you asking about?",
            ("doctor_availability", "doctor_name"): "Which doctor are you asking about?",
            # ADDED BY SOURAV -- "Caller asks when a doctor sits" story.
            # Same question wording as doctor_availability just above --
            # "which doctor" is identical regardless of whether the
            # caller then wants a specific day's availability or the
            # general weekly schedule.
            ("doctor_schedule", "doctor_name"): "Which doctor are you asking about?",
            ("doctors_by_department", "department"): "Which department are you looking for?",
            ("doctors_by_department", "date"): "Which date would you like to know about?",
            ("book_appointment", "doctor_name"): "Which doctor would you like to book with?",
            ("book_appointment", "date"): "Would you like today or another day?",
            ("book_appointment", "time_slot"): "What time would you prefer?",
            ("book_appointment", "patient_name"): "Could you tell me the patient's name?",
            ("book_appointment", "phone"): "Could you provide a phone number for confirmation?",
            # ADDED BY SOURAV -- "Lab Report Status & Secure Delivery".
            # Identity for a report lookup is the caller's REGISTERED
            # phone (Rule 14/15), not their name -- so unlike every other
            # intent above, "phone" here is the identity check itself,
            # not a delivery-confirmation courtesy.
            ("report_status", "phone"): "Could you tell me your registered phone number?",
            ("report_send", "phone"): "Could you tell me your registered phone number?",
            # ADDED BY SOURAV -- Phase 1: Database Schema & Policy Tables.
            ("walkin_eligibility", "test_name"): "Which test were you asking about for walk-in?",
            (
                "prescription_requirements",
                "test_name",
            ): "Which test's prescription requirement were you asking about?",
            ("insurance_coverage", "test_name"): "Which test would you like to check insurance coverage for?",
            ("insurance_coverage", "insurance_provider_name"): "Which insurance provider do you have?",
            ("billing_balance", "phone"): "Could you tell me your registered phone number?",
            # ADDED BY SOURAV -- "Caller asks the agent to compare two
            # options" story.
            (
                "compare_options",
                "compare_option_a",
            ): "Which two tests or packages would you like me to compare?",
            (
                "compare_options",
                "compare_option_b",
            ): "And what's the second one you'd like to compare it with?",
            # ADDED BY SOURAV -- "Caller asks to be called back" story.
            # "callback_phone", NOT "phone" -- same collision reasoning as
            # "report_phone" elsewhere in this file (see main.py's
            # _continue_pending docstring on that collision): the
            # pre-existing booking flow already uses the bare string
            # "phone" as an awaiting value, so this story's own phone
            # collection needs its own scoped name.
            ("request_callback", "callback_time_window"): "What time would work best for a callback?",
            ("request_callback", "callback_phone"): "What number should we call you back on?",
        }
        return prompts.get((intent, missing), "Sorry, could you please clarify?")
    elif language == "hinglish":
        prompts = {
            ("test_rate", "test_name"): "Kaunse test ka rate jaanna chahte ho?",
            ("test_sample", "test_name"): "Kaunse test ka sample jaanna chahte ho?",
            ("test_duration", "test_name"): "Kaunse test ki report ka time jaanna chahte ho?",
            ("test_preparation", "test_name"): "Kaunse test ki taiyari ke baare mein pooch rahe ho?",
            ("doctor_availability", "doctor_name"): "Kaunse doctor ke baare mein pooch rahe ho?",
            ("doctor_schedule", "doctor_name"): "Kaunse doctor ke baare mein pooch rahe ho?",
            ("doctors_by_department", "department"): "Kaunse department mein doctor dhundh rahe ho?",
            ("doctors_by_department", "date"): "Kis din ke liye jaanna chahte ho?",
            ("book_appointment", "doctor_name"): "Kaunse doctor ke saath appointment karna chahte ho?",
            ("book_appointment", "date"): "Aaj ke liye chaahiye ya kisi aur din ke liye?",
            ("book_appointment", "time_slot"): "Kya time prefer karte ho?",
            ("book_appointment", "patient_name"): "Patient ka naam bata sakte ho?",
            ("book_appointment", "phone"): "Confirmation ke liye phone number de sakte ho?",
            ("report_status", "phone"): "Apna registered phone number bata sakte ho?",
            ("report_send", "phone"): "Apna registered phone number bata sakte ho?",
            ("walkin_eligibility", "test_name"): "Kaunse test ke liye walk-in ke baare mein pooch rahe ho?",
            (
                "prescription_requirements",
                "test_name",
            ): "Kaunse test ke prescription ke baare mein pooch rahe ho?",
            ("insurance_coverage", "test_name"): "Kaunse test ke liye insurance coverage check karna hai?",
            ("insurance_coverage", "insurance_provider_name"): "Aapka insurance provider kaunsa hai?",
            ("billing_balance", "phone"): "Apna registered phone number bata sakte ho?",
            ("compare_options", "compare_option_a"): "Kaunse do tests ya packages compare karne hain?",
            ("compare_options", "compare_option_b"): "Aur doosra kaunsa compare karna hai?",
            ("request_callback", "callback_time_window"): "Callback ke liye kaunsa time sahi rahega?",
            ("request_callback", "callback_phone"): "Kis number par callback karein?",
        }
        return prompts.get((intent, missing), "Sorry, thoda clear kar sakte ho?")
    else:  # bengali (default)
        prompts = {
            ("test_rate", "test_name"): "কোন টেস্টের রেট জানতে চান, একটু বলবেন?",
            ("test_sample", "test_name"): "কোন টেস্টের স্যাম্পলের কথা জিজ্ঞেস করছেন?",
            ("test_duration", "test_name"): "কোন টেস্টের রিপোর্টের সময়ের কথা জিজ্ঞেস করছেন?",
            ("test_preparation", "test_name"): "কোন টেস্টের প্রস্তুতির কথা জিজ্ঞেস করছেন?",
            ("doctor_availability", "doctor_name"): "কোন ডাক্তারের কথা জিজ্ঞেস করছেন?",
            # story title: The model never originates a fact -- asked when the
            # caller named a day the local parser could not resolve.
            ("doctor_availability", "date"): "কোন দিনের কথা বলছেন, একটু বলবেন?",
            ("doctor_schedule", "doctor_name"): "কোন ডাক্তারের কথা জিজ্ঞেস করছেন?",
            ("doctors_by_department", "department"): "কোন বিভাগের ডাক্তার খুঁজছেন?",
            ("doctors_by_department", "date"): "কোন দিনের জন্য জানতে চান, একটু বলবেন?",
            ("book_appointment", "doctor_name"): "কোন ডাক্তারের সাথে অ্যাপয়েন্টমেন্ট করতে চান?",
            ("book_appointment", "date"): "আজকের জন্য চান, নাকি অন্য কোনো দিনের জন্য অ্যাপয়েন্টমেন্ট চাই?",
            ("book_appointment", "time_slot"): "কোন সময়ে অ্যাপয়েন্টমেন্ট চাই, একটু বলবেন?",
            ("book_appointment", "patient_name"): "রোগীর নামটা বলবেন?",
            ("book_appointment", "phone"): "একটা ফোন নম্বর দেবেন, যাতে কনফার্মেশন পাঠাতে পারি?",
            ("report_status", "phone"): "আপনার নিবন্ধিত ফোন নম্বরটা বলবেন?",
            ("report_send", "phone"): "আপনার নিবন্ধিত ফোন নম্বরটা বলবেন?",
            ("walkin_eligibility", "test_name"): "কোন টেস্টের ওয়াক-ইন সম্পর্কে জিজ্ঞেস করছেন?",
            ("prescription_requirements", "test_name"): "কোন টেস্টের প্রেসক্রিপশন সম্পর্কে জিজ্ঞেস করছেন?",
            ("insurance_coverage", "test_name"): "কোন টেস্টের জন্য ইন্স্যুরেন্স কভারেজ জানতে চান?",
            ("insurance_coverage", "insurance_provider_name"): "আপনার ইন্স্যুরেন্স প্রোভাইডারের নাম কী?",
            ("billing_balance", "phone"): "আপনার নিবন্ধিত ফোন নম্বরটা বলবেন?",
            ("compare_options", "compare_option_a"): "কোন দুটো টেস্ট বা প্যাকেজ তুলনা করে দেখতে চান?",
            ("compare_options", "compare_option_b"): "আর দ্বিতীয়টা কোনটার সাথে তুলনা করতে চান?",
            ("request_callback", "callback_time_window"): "কল ব্যাকের জন্য কোন সময়টা সুবিধাজনক হবে?",
            ("request_callback", "callback_phone"): "কোন নম্বরে কল ব্যাক করব?",
        }
        return prompts.get((intent, missing), "দুঃখিত, একটু স্পষ্ট করে বলবেন?")


def _test_not_found_reply(slots: dict, result: dict, language: str = "bengali") -> str:
    """Shared "no such test in the catalogue" reply, used by both
    test_rate_reply() and sample_type_reply() -- extracted here (Caller
    asks what sample is needed, Conversation: Information and Enquiry) so
    the not-found wording lives in exactly one place instead of being
    duplicated a second time for the new sample-only question."""
    suggestions = result.get("did_you_mean") or []
    if language == "english":
        if suggestions:
            return (
                f"I couldn't find a test named '{slots.get('test_name')}'. "
                f"Did you mean {', '.join(suggestions)}?"
            )
        return f"Sorry, we don't have a test named '{slots.get('test_name')}'."
    elif language == "hinglish":
        if suggestions:
            return (
                f"'{slots.get('test_name')}' naam ka test nahi mila. "
                f"Kya aap kehna chahte the {', '.join(suggestions)}?"
            )
        return f"Sorry, '{slots.get('test_name')}' naam ka test hamari list mein nahi hai."
    elif language == "banglish":
        if suggestions:
            return (
                f"'{slots.get('test_name')}' name-r test khunje pelam na. "
                f"Apni ki bolte chaichen {', '.join(suggestions)}?"
            )
        return f"Dukkhito, '{slots.get('test_name')}' name-r kono test amader list-e nei."
    else:  # bengali
        # STORY [Answer Quality and Grounding]
        # As a patient, I want to hear the whole sentence, so that I am
        # not left guessing what the agent tried to say.
        # The SPOKEN suggestions, not the catalogue's English labels.
        # Reading `did_you_mean` aloud produced "আপনি কি বলতে চাইছেন:"
        # followed by silence -- the whole point of the list gone, on the
        # one path where the caller has already failed once and most
        # needs the words.
        #
        # An empty spoken list falls through to the plain not-found line
        # rather than to the English one: a suggestion nobody can hear is
        # not a suggestion, and offering it would be the same hole with
        # extra steps.
        suggestions_bn = result.get("did_you_mean_bn") or []
        if suggestions_bn:
            # story title: Answers sound like a person, not a database row
            # user story: As a patient, I want to hear a sentence, so that
            #   the agent sounds like someone at the counter.
            # acceptance criteria: No field label, colon or bracket is ever
            #   spoken and every structured value renders as a natural
            #   clause in the reply language. An automated check fails a
            #   build containing a spoken punctuation artefact.
            #
            # Was "আপনি কি বলতে চাইছেন: X, Y?" -- a colon read aloud, and a
            # comma-separated list where a person would say "or". The
            # options now sit inside the question rather than being
            # announced by it.
            return (
                f"'{slots.get('test_name')}' নামে টেস্ট খুঁজে পাইনি। "
                f"আপনি কি {_spoken_list(suggestions_bn)} বলতে চাইছেন?"
            )
        return f"দুঃখিত, '{slots.get('test_name')}' নামে কোনো টেস্ট আমাদের তালিকায় নেই।"


# STORY [Answer Quality and Grounding]
# As a patient, I want to hear the whole sentence, so that I am
# not left guessing what the agent tried to say.
def _spoken_department(slots: dict, result: dict) -> str:
    """Same problem as the two above, and it was the worst of the three.

    clinic-api returns `department` as the English column ("Cardiology"), and
    the listing template puts it at the head of the sentence -- so the Bengali
    tokenizer dropped the SUBJECT and the caller heard "...বিভাগে ডাঃ সেন
    আছেন", a sentence with a hole where the thing they asked about should be.
    That was true of all eight seeded departments, not an edge case.

    Prefer the seeded Bengali alias; failing that, echo the caller's own words,
    which is what a person at the counter would do.
    """
    return result.get("department_bn") or slots.get("department") or result.get("department") or "এই বিভাগে"


# "Caller asks what sample is needed" (Conversation: Information and
# Enquiry). AC: "The English clinical term is preserved if the caller
# used it. Multiple samples for one test are all stated." clinic-api's
# seed.py has exactly one flat sample_type string per test today (Blood,
# Urine, Imaging, Cardiac, or "Sample (Cervical)") -- no test needs more
# than one. This still handles a "|"-joined value correctly (the same
# multi-value convention Doctor.aliases_bn / LabTest.aliases_bn already
# use elsewhere in this codebase) so a future catalogue row listing more
# than one sample is spoken naturally and pluralised, without fabricating
# multiple samples for the tests that only ever need one.
_SAMPLE_RENAMES = {
    # "Sample (Cervical)" already contains the word "sample" -- left as
    # the raw catalogue value, every sentence that names it would say
    # "sample" twice ("a Sample (Cervical) sample"). Renamed to just the
    # clinical term; the parenthetical was also a spoken-punctuation risk
    # Story 2's automated check never covered (it only bans ':[]{}', not
    # '()').
    "Sample (Cervical)": "Cervical",
}

_SAMPLE_JOIN_WORD = {"english": "and", "bengali": "এবং", "hinglish": "aur", "banglish": "ar"}


def _spoken_sample_types(sample: str, language: str = "bengali") -> tuple[str, bool]:
    """-> (naturally-joined sample description, is_plural).

    Splits on "|" (today's data never contains it, but the split is a
    no-op on a single value, so this is free correctness for whenever a
    row does), applies _SAMPLE_RENAMES to each part, and joins 2+ values
    with the language's own word for "and" rather than a raw comma or
    pipe character.
    """
    parts = [p.strip() for p in sample.split("|") if p.strip()]
    cleaned = [_SAMPLE_RENAMES.get(p, p) for p in parts] or [sample]
    join_word = _SAMPLE_JOIN_WORD.get(language, _SAMPLE_JOIN_WORD["bengali"])
    if len(cleaned) == 1:
        return cleaned[0], False
    return f"{', '.join(cleaned[:-1])} {join_word} {cleaned[-1]}", True


def _digit_faithful_rate(raw_rate) -> str:
    """UPDATED BY SOURAV -- real production bug found while re-verifying
    this combined story: clinic-api's `LabTest.rate_inr` column is a
    SQLAlchemy Float, so the live catalogue always serializes it as a
    JSON float (e.g. 250.0), even for a rate that was seeded as a plain
    integer. agent/tools_client.py's `_parse_exact()` deliberately keeps
    that as the STRING "250.0" (digit-fidelity design, see
    tests/test_number_fidelity*.py) -- so every real caller was hearing
    a literal trailing ".0" in the price ("... রেট 250.0 টাকা।"), which
    verbalize() then spoke aloud as "point zero". That is wrong for a
    whole-rupee amount and was never caught before because no earlier
    test asserted digit-faithfulness against a REAL DB-sourced rate
    through this exact function (tests/test_live_test_price_lookup.py's
    test_real_rate_is_digit_faithful_through_the_full_live_pipeline is
    the first one that does).

    This strips ONLY an exact whole-number trailing ".0" -- it does not
    round or otherwise touch a genuinely fractional value (e.g.
    "199.55" is returned completely unchanged), preserving the same
    never-mutate-a-digit discipline `_parse_exact()` was built for.
    Accepts a str, int, or float, since call sites differ (real
    production hands this a string via `_parse_exact`; some tests hand
    it a native float straight from `r.json()` or a DB row).
    """
    text = str(raw_rate)
    if text.endswith(".0") and text[:-2].lstrip("-").isdigit():
        return text[:-2]
    return text


def test_rate_reply(slots: dict, result: dict, language: str = "bengali") -> str:
    """ "Caller asks the price of a test" (Epic: Conversation -- Information
    and Enquiry). AC: "The price is read from the live catalogue and
    spoken as a natural sentence with the sample type and reporting time.
    The figure is a template substitution and is never composed by the
    model. An unknown test produces the not-found path with near matches
    offered."

    SCOPE, per explicit instruction: "only price will be told with a
    normalize[d] tone for the tests, not anything else." This function
    used to bundle rate + sample + duration into one sentence (the shape
    the AC above literally describes); it now speaks ONLY the price, in
    the same plain, consistent sentence structure across all four
    languages -- no sample clause, no duration clause. This mirrors the
    same one-question-one-answer discipline "Caller asks what sample is
    needed" (sample_type_reply(), below) already established for the
    sample-only question.

    FLAGGED, not silently absorbed: this removes the only caller-visible
    path that ever spoke bn_normalize.hours_to_duration_phrase()'s output
    ("Caller asks how long results take", the previous story) -- that
    function is untouched and still directly unit-tested, but nothing
    dispatches to it anymore. A dedicated "how long does it take" intent
    would need to be built for that story's answer to reach a caller
    again; that is not part of this change. sample_type_reply()'s own
    intent (test_sample) is unaffected -- it never called this function.

    Also fixes a real, measured bug found while verifying this story:
    _spoken_test_name() is now called with `language`, so an English/
    Hinglish/Banglish caller no longer hears a raw Bengali-script alias
    glued into their sentence (see that function's docstring for the
    full story). rate_inr keeps its pre-existing exact-passthrough
    discipline unchanged (see tests/test_number_fidelity*.py).
    """
    if not result.get("found"):
        return _test_not_found_reply(slots, result, language)

    # UPDATED BY SOURAV -- was `rate = result["rate_inr"]` (raw
    # passthrough). See _digit_faithful_rate()'s docstring above for the
    # real bug this fixes (a literal trailing ".0"/"point zero" spoken
    # for every test's price) and why this is still digit-fidelity safe.
    rate = _digit_faithful_rate(result["rate_inr"])
    name = _spoken_test_name(slots, result, language)
    name_has_test = _name_already_says_test(name)

    # Preserve exact rate value in all languages -- price only, nothing else.
    if language == "english":
        reply = f"{name} rate is {rate} rupees." if name_has_test else f"{name} test rate is {rate} rupees."
    elif language == "hinglish":
        reply = (
            f"{name} ka rate {rate} rupaye hai."
            if name_has_test
            else f"{name} test ka rate {rate} rupaye hai."
        )
    elif language == "banglish":
        reply = f"{name} rate {rate} taka." if name_has_test else f"{name} test-er rate {rate} taka."
    else:  # bengali
        # name_has_test checks both scripts -- see _name_already_says_test()
        reply = f"{name} রেট {rate} টাকা।" if name_has_test else f"{name} টেস্টের রেট {rate} টাকা।"
    return reply


def sample_type_reply(slots: dict, result: dict, language: str = "bengali") -> str:
    """ "Caller asks what sample is needed" (Epic: Conversation --
    Information and Enquiry). AC: "The sample type is spoken as a natural
    clause rather than a field and a colon. The English clinical term is
    preserved if the caller used it. Multiple samples for one test are
    all stated."

    Answers ONLY the sample-type question, for the caller who asked
    nothing but "what sample do I need for X" -- test_rate_reply() (above)
    now answers ONLY the price question, per the same one-question-one-
    answer discipline (see that function's docstring). Reuses the exact
    same clinic-api lookup test_rate_reply() does (the API already
    returns sample_type on every test-info call; nothing new was added to
    clinic-api for this) -- only what gets SPOKEN differs.

    `_spoken_test_name(..., language)` -- fixes the same mixed-script bug
    test_rate_reply() was fixed for ("Caller asks the price of a test"):
    this call used to omit `language` entirely, so an English/Hinglish/
    Banglish caller could hear a raw Bengali-script alias in this
    sentence too (e.g. "For সিবিসি test, you'll need to give..."). See
    _spoken_test_name()'s own docstring for the full story.

    "The English clinical term is preserved" is genuinely satisfied for
    english/hinglish/banglish here -- sample_type flows through
    unaltered, same as rate_inr's digit-fidelity discipline. It is NOT
    literally possible for the bengali branch: the Bengali TTS model
    cannot pronounce untranslated Latin script at all (see bn_normalize.
    py's module docstring and unspeakable_spans() -- this was measured,
    not assumed), which is exactly why _LATIN_SPOKEN_BN exists. Before
    this story, "Imaging", "Cardiac" and "Sample (Cervical)" had no entry
    in that table, so a Bengali caller asking about any of the 7 tests
    using one of those 3 sample types heard nothing at all for the
    sample -- not a wrong word, silence. This story extends
    _LATIN_SPOKEN_BN with the 3 missing entries, the same treatment
    "blood"/"urine" already got, so the Bengali branch speaks a real word
    instead of dropping the caller's answer entirely.

    Never fabricates a second sample for a test that only needs one --
    see _spoken_sample_types()'s module comment for why LabTest.
    sample_type has no way to represent more than one today.
    """
    if not result.get("found"):
        return _test_not_found_reply(slots, result, language)

    name = _spoken_test_name(slots, result, language)
    sample = result.get("sample_type")
    name_has_test = _name_already_says_test(name)

    if not sample:
        # Honest fallback for a malformed/incomplete catalogue row -- see
        # the "Truth Validator" epic's sibling stories for schema-drift
        # handling in general; this is not that, just a guard against
        # ever inventing a sample that was never returned.
        if language == "english":
            return f"Sorry, I don't have the sample details for {name} right now."
        elif language == "hinglish":
            return f"Sorry, {name} ke liye sample ki jaankari abhi available nahi hai."
        elif language == "banglish":
            return f"Dukkhito, {name}-er sample-er kotha ekhon bolte parchi na."
        else:  # bengali
            return f"দুঃখিত, {name}-এর স্যাম্পল সম্পর্কে এখন বলতে পারছি না।"

    sample_str, sample_plural = _spoken_sample_types(sample, language)

    if language == "english":
        noun = f"{sample_str} samples" if sample_plural else f"{_a_or_an(sample_str)} {sample_str} sample"
        suffix = "" if name_has_test else " test"
        return f"For {name}{suffix}, you'll need to give {noun}."
    elif language == "hinglish":
        noun = f"{sample_str} samples" if sample_plural else f"{sample_str} sample"
        suffix = "" if name_has_test else " test"
        return f"{name}{suffix} ke liye {noun} dena hoga."
    elif language == "banglish":
        noun = f"{sample_str} samples" if sample_plural else f"{sample_str} sample ta"
        suffix = "" if name_has_test else " test"
        return f"{name}{suffix} er jonno {noun} lagbe."
    else:  # bengali
        noun = f"{sample_str} স্যাম্পলগুলো" if sample_plural else f"{sample_str} স্যাম্পলটা"
        if name_has_test:
            return f"{name}-এর জন্য {noun} লাগবে।"
        return f"{name} টেস্টের জন্য {noun} লাগবে।"


def test_duration_reply(slots: dict, result: dict, language: str = "bengali") -> str:
    """ADDED BY SOURAV -- fixes a real production bug, reported directly
    from a live call transcript:

        [User] How long does it take to get the urine test report?
        [AI]   Urine test rate is 200 taka.
        [User] How long will it take to get the urine test report?
        [AI]   Urine test rate is 200 taka.

    A caller asking about REPORT TURNAROUND TIME was being misclassified
    as "test_rate" and answered with the test's PRICE instead -- twice in
    a row, since nothing about the exchange gave the classifier a reason
    to reconsider.

    ROOT CAUSE: "Caller asks the price of a test" (see test_rate_reply()'s
    own docstring, above) deliberately narrowed test_rate_reply() to speak
    ONLY the price, and its docstring explicitly flagged this as removing
    the only caller-visible path to bn_normalize.hours_to_duration_phrase()
    -- built and unit-tested by an even earlier story ("Caller asks how
    long results take") but never wired to anything a caller could
    actually trigger. agent/llm.py's intent prompt was never updated to
    match: it kept describing test_rate as covering "how long results
    take," so the classifier kept routing duration questions to test_rate,
    which (correctly, per its own later-narrowed scope) speaks only the
    price. Two separate, true things -- test_rate_reply()'s narrowed scope,
    and llm.py's stale intent description -- combined into exactly the bug
    reported above. Fixed in agent/llm.py by adding a dedicated
    "test_duration" intent and correcting both intents' descriptions.

    A SECOND bug was found and fixed alongside this one, in
    agent/fast_path.py: the pre-LLM local matcher's _RATE_CUES set
    included ambiguous verbs ("কত লাগবে", "কত পড়বে", "কত নেবে") that mean
    either "how much will it COST" or "how much/long will it TAKE" in
    colloquial Bengali. A bare "রিপোর্ট পেতে কত লাগবে" (no explicit time
    word) was being fast-pathed straight to test_rate, bypassing the LLM's
    new, correct distinction entirely -- reproducing this exact bug at a
    second layer. See agent/fast_path.py's _AMBIGUOUS_RATE_CUES /
    _DURATION_SIGNAL_CUES comments for that fix.

    This function itself does no new lookup -- it reuses the SAME
    clinic-api response test_rate_reply()/sample_type_reply() already get
    from get_test_rate() (report_time_hours has always been present in
    that payload; nothing new was added to clinic-api for this), and
    reuses hours_to_duration_phrase() exactly as it already exists and is
    already unit-tested -- never rebuilds it. One-question-one-answer
    discipline applies here too: this speaks ONLY the duration, never the
    price or the sample, mirroring test_rate_reply()/sample_type_reply().
    """
    if not result.get("found"):
        return _test_not_found_reply(slots, result, language)

    name = _spoken_test_name(slots, result, language)
    hours = result.get("report_time_hours")
    name_has_test = _name_already_says_test(name)

    if hours is None:
        # Honest fallback for a malformed/incomplete catalogue row -- same
        # discipline as sample_type_reply()'s missing-sample fallback just
        # above: never fabricate a duration that was never returned.
        if language == "english":
            return f"Sorry, I don't have the report time for {name} right now."
        elif language == "hinglish":
            return f"Sorry, {name} ke report time ki jaankari abhi available nahi hai."
        elif language == "banglish":
            return f"Dukkhito, {name}-er report time ekhon bolte parchi na."
        else:  # bengali
            return f"দুঃখিত, {name}-এর রিপোর্টের সময় সম্পর্কে এখন বলতে পারছি না।"

    duration = hours_to_duration_phrase(hours, language)

    if language == "english":
        suffix = "" if name_has_test else " test"
        return f"The {name}{suffix} report will be ready {duration}."
    elif language == "hinglish":
        suffix = "" if name_has_test else " test"
        return f"{name}{suffix} ki report {duration} ready ho jaayegi."
    elif language == "banglish":
        suffix = "" if name_has_test else " test"
        return f"{name}{suffix}-er report {duration} ready hoye jabe."
    else:  # bengali
        if name_has_test:
            return f"{name}-এর রিপোর্ট {duration} রেডি হয়ে যাবে।"
        return f"{name} টেস্টের রিপোর্ট {duration} রেডি হয়ে যাবে।"


def _test_preparation_unavailable_reply(name: str, language: str = "bengali") -> str:
    """ADDED BY SOURAV -- "Caller asks how to prepare for a test" story.
    Shared honest fallback for BOTH real cases where no preparation
    script can be spoken: `advisory_available: False` (this test exists
    but the business has never supplied preparation content for it --
    see clinic-api/models.py's own comment on why LabTest's advisory
    columns are nullable with no default), and the defensive case of a
    malformed catalogue row that claims `advisory_available: True` but is
    actually missing the script for THIS language. Never says "no special
    preparation needed" -- that is a specific, possibly-wrong medical
    claim this codebase has no basis to make up; it says plainly that the
    information isn't available and points the caller at a human instead,
    same discipline as agent/outcomes.py's insufficient-verified-
    information outcome."""
    if language == "english":
        return (
            f"I don't have preparation instructions for {name} yet. "
            f"Please check with the counter or your doctor."
        )
    elif language == "hinglish":
        return (
            f"{name} ke liye abhi preparation ki jaankari mere paas nahi hai. "
            f"Counter ya apne doctor se check kar lijiye."
        )
    elif language == "banglish":
        return (
            f"{name}-er jonno ekhon preparation-er information amar kache nei. "
            f"Counter othoba apnar doctor-ke jiggesh korben."
        )
    else:  # bengali
        return f"{name}-এর জন্য এখন প্রস্তুতির তথ্য আমার কাছে নেই। দয়া করে কাউন্টারে বা আপনার ডাক্তারকে জিজ্ঞেস করুন।"


_ADVISORY_SCRIPT_FIELD_FOR_LANGUAGE = {
    "english": "advisory_script_en",
    "hinglish": "advisory_script_hinglish",
    "banglish": "advisory_script_banglish",
    "bengali": "advisory_script_bn",
}


def test_preparation_reply(slots: dict, result: dict, language: str = "bengali") -> str:
    """ "Caller asks how to prepare for a test" (Epic: Conversation --
    Information and Enquiry). Speaks the business's own supplied
    preparation script (clinic-api/seed.py's LAB_TEST_ADVISORIES,
    sourced verbatim from the lab_tests_with_fallback_config sample
    file) for the caller's language, with the literal "{test_name}"
    placeholder substituted using the exact same Bengali-alias-vs-
    caller's-own-words convention _spoken_test_name() already applies
    everywhere else in this file.

    THREE distinct outcomes, matching clinic-api/main.py's
    _test_preparation_reply_dict() docstring exactly:

      1. found=False -> the shared not-found/did-you-mean reply (same
         helper test_rate_reply()/sample_type_reply()/test_duration_
         reply() already use).
      2. found=True, advisory_available=False -> the honest "we don't
         have this yet" fallback above. This is the entire reason
         LabTest's advisory columns are nullable with no default: a test
         nobody has actually reviewed must never be told "no special
         preparation needed" -- see clinic-api/models.py's own comment.
      3. found=True, advisory_available=True -> speaks the pre-written,
         business-approved advisory_script_* for this language VERBATIM
         (only the {test_name} placeholder is substituted) -- never
         recomposed from the structured fasting_required/fasting_hours/
         water_allowance/medication_hold/timing_rule fields, for the same
         reason clinic-api/seed.py stores these scripts verbatim instead
         of generating sentences from those fields: they are the exact
         wording the business already reviewed and approved, and this
         codebase has no business rephrasing a medical instruction.
    """
    if not result.get("found"):
        return _test_not_found_reply(slots, result, language)

    name = _spoken_test_name(slots, result, language)

    if not result.get("advisory_available"):
        return _test_preparation_unavailable_reply(name, language)

    field = _ADVISORY_SCRIPT_FIELD_FOR_LANGUAGE.get(language, "advisory_script_bn")
    script = result.get(field)
    if not script:
        # Defensive only -- clinic-api/seed.py always populates all 4
        # languages together for every advisory-covered test (see this
        # story's TEST_REPORT), so a real response never actually hits
        # this branch. Kept anyway rather than raising or speaking a
        # blank reply, same "never trust a response shape blindly"
        # discipline as sample_type_reply()'s missing-sample fallback.
        return _test_preparation_unavailable_reply(name, language)
    return script.replace("{test_name}", name)


def doctor_availability_reply(slots: dict, result: dict, language: str = "bengali") -> str:
    if not result.get("found"):
        if language == "english":
            return f"Sorry, we don't have a doctor named '{slots.get('doctor_name')}'."
        elif language == "hinglish":
            return f"Sorry, '{slots.get('doctor_name')}' naam ka doctor yahan nahi hai."
        else:  # bengali
            return f"দুঃখিত, '{slots.get('doctor_name')}' নামে কোনো ডাক্তার আমাদের এখানে নেই।"

    name = _spoken_doctor_name(slots, result, language=language)
    if result.get("available"):
        hours = result.get("chamber_hours", "")
        # NOTE (merge dev_chakravardhan -> staging_merged): dev_chakravardhan's
        # side of this conflict replaced every function above (missing_slot_
        # prompt, test_rate_reply, doctor_availability_reply, and the two
        # _spoken_*_name helpers) with a rewrite onto agent/i18n.t() and an
        # `agent/language`-resolved `lang` parameter, intended to make every
        # public function here callable with `lang=None` defaulting to
        # Bengali. That rewrite is NOT taken: main.py and main_pcm.py call
        # these exact functions with the keyword argument `language=`
        # (dozens of call sites -- test_rate_reply, missing_slot_prompt,
        # doctor_availability_reply, booking_reply), which only works
        # against this file's actual, already-integrated parameter name.
        # HEAD's fuller implementation (near-match handling, spoken lists,
        # per-language sample/duration/preparation replies, etc., none of
        # which the i18n rewrite carried over) is kept in full instead.
        # The one genuinely new idea in that rewrite with no HEAD
        # equivalent -- telling the caller whether a written SMS
        # confirmation is actually on its way -- has been ported onto
        # booking_reply() below as _written_confirmation_clause(), using
        # this file's own per-language template style rather than t().
        # into booking instead of stopping here -- main.py stays listening
        # for the answer to this exact question (see its "date" pending
        # state), so "আজকেই" / "অন্য দিন" both continue the flow.
        date_txt = f" {result.get('date')}" if result.get("date") else " today"

        if language == "english":
            return (
                f"Yes,{date_txt} {name} will be in chamber. The chamber hours are {hours}. "
                f"Would you like to book for today or another day?"
            )
        elif language == "hinglish":
            return (
                f"Haan,{date_txt} {name} chamber mein honge. Chamber ka time hai {hours}. "
                f"Aaj ke liye appointment karna chahte ho ya kisi aur din ke liye?"
            )
        else:  # bengali
            date_txt_bn = f" {result.get('date')} তারিখে" if result.get("date") else " আজ"
            return (
                f"হ্যাঁ,{date_txt_bn} {name} চেম্বারে থাকবেন। চেম্বারের সময় {hours}। "
                f"আজকের জন্যই অ্যাপয়েন্টমেন্ট করবেন, নাকি অন্য কোনো দিনের জন্য?"
            )

    next_date = result.get("next_available_date")
    if next_date:
        if language == "english":
            return (
                f"{name} won't be available that day. The next available date is {next_date}. "
                f"Would you like to book for that day?"
            )
        elif language == "hinglish":
            return (
                f"{name} us din nahi honge. Agla available date hai {next_date}. "
                f"Us din ke liye appointment karna chahte ho?"
            )
        else:  # bengali
            return (
                f"{name} ওই দিন বসবেন না। পরবর্তী উপলব্ধ দিনটা হলো {next_date}। ওই দিনের জন্য অ্যাপয়েন্টমেন্ট করতে চান?"
            )

    if language == "english":
        return f"{name} doesn't have a fixed schedule right now. Please check at our counter."
    elif language == "hinglish":
        return f"{name} abhi koi fixed date nahi hai. Hamare counter mein check kar sakte ho."
    else:  # bengali
        return f"{name} এখন কোনো নির্দিষ্ট দিন বসছেন না। আমাদের কাউন্টারে খোঁজ নিতে পারেন।"


def heard_confirm_prompt(transcript: str) -> str:
    """Echo back what the recogniser produced, when the two decoders did not
    agree enough to act on it.

    The caller's OWN words are read back verbatim -- not a paraphrase and not
    a cleaned-up version. If the transcript is wrong, hearing it wrong is
    exactly what lets the caller say না; smoothing it over would hide the
    error this prompt exists to surface.
    """
    return f"আমি শুনলাম — {transcript}। ঠিক বলেছি?"


def date_range_confirm_prompt(start_iso: str, end_iso: str) -> str:
    """Read a CALCULATED date range back before answering about it.

    story title: The model never originates a fact
    user story: As a clinical lead, I want every price, date and identifier to
        come from a verified system response, so that a wrong answer is a data
        bug rather than a model bug.
    acceptance criteria: Every factual sentence is a template substitution from
        a validated tool response and the model is never shown a figure it
        could restate. An automated assertion on every commit proves no
        model-composed span reaches synthesis on a factual intent.

    Spoken when the caller said something like "আগামী সপ্তাহে" -- the model
    said which expression that was, agent/date_calc.py worked out which seven
    days it covers, and the agent cannot answer "is Dr Sen in?" about seven
    days at once. So it says which seven it means.

    THE DATES IN THIS SENTENCE ARE SAFE TO SPEAK, and the reason is the whole
    architecture in one line: they were computed by date_calc, not produced by
    the model. A date the model invented could not be read out even inside a
    question -- asking "did you mean the 14th to the 20th?" asserts that those
    are the dates of next week, which is a fact, and facts do not come from the
    model. Here they came from the calendar.

    bn_normalize.verbalize() spells both dates into Bengali words before
    synthesis, so the caller hears "সেপ্টেম্বর মাসের চোদ্দো তারিখ" rather than
    a string of Latin digits the tokenizer would silently drop.
    """
    return f"আপনি কি {start_iso} থেকে {end_iso} — এই সময়ের মধ্যে জানতে চাইছেন?"


def booking_confirm_prompt(slots: dict) -> str:
    """Read the whole booking back before it is committed.

    This is the last point at which a mishearing is still free to correct. Every
    value in it came from the caller, so nothing here is a fact the agent is
    originating -- it is the same template-substitution discipline the rest of
    this module applies to API responses, pointed at the caller's own words.

    Deliberately NOT handed to the LLM to phrase more warmly: that would put a
    date and a phone number back into generated text, which is precisely what
    this file exists to prevent. bn_normalize.verbalize() already reads the
    phone number digit-by-digit and spells the date into Bengali words, so the
    caller hears it the way a person would say it back.
    """
    # story title: The model never originates a fact
    # user story: As a clinical lead, I want every price, date and identifier
    #   to come from a verified system response, so that a wrong answer is a
    #   data bug rather than a model bug.
    # acceptance criteria: Every factual sentence is a template substitution
    #   from a validated tool response and the model is never shown a figure
    #   it could restate. An automated assertion on every commit proves no
    #   model-composed span reaches synthesis on a factual intent.
    #
    # doctor_name_bn first, and this is a bug fix, not a preference. On the
    # commonest booking route -- list a department's doctors, caller picks
    # one, book -- main.py stores the CANONICAL English name, because that is
    # what the booking API matches on. This sentence is SPOKEN, so reading
    # that field put "ডাঃ Dr. A Sen" into a Bengali utterance: the Bengali
    # tokenizer drops Latin script, so the caller was asked to confirm a
    # booking with the doctor's name missing from it, and after the
    # speakability gate landed the whole confirmation was blocked and the
    # caller sent to the counter mid-booking.
    #
    # Both forms are now carried through the flow: the English one goes to
    # the API, the Bengali one is said out loud. Where a doctor row has no
    # Bengali alias this still falls back to the English label and the
    # speakability gate refuses the reply -- which is correct. A missing
    # alias is a data defect, and confirming a booking against a name the
    # caller cannot hear is worse than escalating.
    doctor = slots.get("doctor_name_bn") or slots.get("doctor_name")
    return (
        f"একটু মিলিয়ে নিই। "
        f"রোগী {slots['patient_name']}, ডাঃ {doctor}, "
        f"{slots['date']} তারিখে, সময় {slots['time_slot']}, "
        f"ফোন {slots['phone']}। সব ঠিক আছে?"
    )


# story title: Every critical value is read back before it is used
# user story: As a patient giving a phone number, I want it read back, so that
#   a misheard digit does not send my report to a stranger.
# acceptance criteria: Phone numbers, dates, times and names are confirmed
#   aloud before any write, and a rejection opens a correction path rather than
#   repeating the prompt. Readback is mandatory regardless of confidence for
#   values that affect a write.
# ADDED BY SOURAV -- "Caller asks when a doctor sits" story. Groups the
# doctor's schedule rows by their (start_time, end_time) pair so several
# weekdays sharing identical chamber hours (the common case -- see
# clinic-api/seed.py's SHIFT_TEMPLATES, which gives every seeded doctor
# ONE set of hours across all their sitting days) are spoken as a single
# natural clause ("Monday, Wednesday and Friday, 10 to 12") instead of
# three separate, repetitive sentences. Still handles the general case
# where a real future doctor's hours genuinely differ by day -- that just
# produces more than one group, each spoken as its own clause. Ordered by
# each group's EARLIEST weekday so a multi-group reply is still spoken
# Monday-first, not in whatever order the hours happened to be seeded.
def _group_schedule_by_hours(schedule: list[dict]) -> list[tuple[str, list[int]]]:
    groups: dict[str, list[int]] = {}
    for entry in schedule:
        hours = f"{entry['start_time']}-{entry['end_time']}"
        groups.setdefault(hours, []).append(entry["weekday"])
    ordered = sorted(groups.items(), key=lambda kv: min(kv[1]))
    return [(hours, sorted(days)) for hours, days in ordered]


# ADDED BY SOURAV -- joins 1+ weekday names naturally ("Monday", "Monday
# and Wednesday", "Monday, Wednesday and Friday"), the same shape
# _spoken_sample_types() above already established for joining 2+ sample
# values. Reuses that function's _SAMPLE_JOIN_WORD map directly rather
# than duplicating a second copy of the same four "and" words -- despite
# its sample-specific name, it is just a language -> "and" lookup, and
# duplicating it here would be the one thing certain to drift the two out
# of sync the next time either needed a fifth language.
def _spoken_weekday_list(weekdays: list[int], language: str = "bengali") -> str:
    names = [weekday_to_words(w, language) for w in weekdays]
    if len(names) == 1:
        return names[0]
    join_word = _SAMPLE_JOIN_WORD.get(language, _SAMPLE_JOIN_WORD["bengali"])
    return f"{', '.join(names[:-1])} {join_word} {names[-1]}"


def doctor_schedule_reply(slots: dict, result: dict, language: str = "bengali") -> str:
    """ "Caller asks when a doctor sits" (Epic: Conversation -- Information
    and Enquiry). A caller asking, in general, which days a named doctor
    sits -- with no date mentioned at all -- gets that doctor's full
    recurring weekly schedule read back naturally, in whichever of the 4
    languages this was asked to answer in.

    Deliberately a SEPARATE function from doctor_availability_reply()
    above rather than a new branch inside it: that function's shape is
    built entirely around resolving to ONE day (available today / not
    available, next available date) and asks a follow-up booking
    question tied to that one day ("book today or another day?") -- none
    of which makes sense for a caller who did not name a day at all and
    is not (yet) trying to book anything. Sharing the DoctorSchedule data
    is handled at the clinic-api layer (both endpoints query the same
    table); the two reply shapes stay genuinely distinct here.

    NOT built (flagged, not silently absorbed): unlike
    doctor_availability_reply(), this does not end with a "would you
    like to book an appointment?" follow-up question, and main.py does
    not open a pending state after it -- this intent is purely
    informational. Adding a booking hand-off here would be a reasonable
    follow-up story, not assumed as part of this one.
    """
    if not result.get("found"):
        if language == "english":
            return f"Sorry, we don't have a doctor named '{slots.get('doctor_name')}'."
        elif language == "hinglish":
            return f"Sorry, '{slots.get('doctor_name')}' naam ka doctor yahan nahi hai."
        elif language == "banglish":
            return f"Dukkhito, '{slots.get('doctor_name')}' naam-e kono doctor amader ekhane nei."
        else:  # bengali
            return f"দুঃখিত, '{slots.get('doctor_name')}' নামে কোনো ডাক্তার আমাদের এখানে নেই।"

    name = _spoken_doctor_name(slots, result, language=language)
    schedule = result.get("schedule") or []

    if not schedule:
        # Honest, real edge case -- see clinic-api/main.py::doctor_schedule()'s
        # docstring: a doctor can exist with zero DoctorSchedule rows (e.g.
        # on indefinite leave). Never fabricate a sitting day here.
        if language == "english":
            return f"{name} doesn't have a fixed schedule right now. Please check at our counter."
        elif language == "hinglish":
            return f"{name} abhi koi fixed din nahi baithte. Hamare counter mein check kar sakte ho."
        elif language == "banglish":
            return f"{name} ekhon kono nirdishto din boshchen na. Amader counter-e khoj nite paren."
        else:  # bengali
            return f"{name} এখন কোনো নির্দিষ্ট দিন বসছেন না। আমাদের কাউন্টারে খোঁজ নিতে পারেন।"

    # Multiple hour-groups (a doctor whose hours genuinely differ by day --
    # not exercised by today's seed data, see SHIFT_TEMPLATES, but the
    # schema allows it) are joined with a plain ", and "/"আর"-style
    # conjunction, deliberately never a semicolon or any other punctuation
    # a caller would hear as a pause artefact rather than a spoken word.
    groups = _group_schedule_by_hours(schedule)

    if language == "english":
        clauses = [
            f"on {_spoken_weekday_list(days, language)}, chamber hours {hours}" for hours, days in groups
        ]
        return f"{name} sits {', and '.join(clauses)}."
    elif language == "hinglish":
        clauses = [
            f"{_spoken_weekday_list(days, language)} ko {hours} baithte hain" for hours, days in groups
        ]
        return f"{name} {', aur '.join(clauses)}."
    elif language == "banglish":
        # "shomoy-e" (not "{hours}-e") deliberately keeps the Bengali
        # locative "-e" suffix attached to a WORD ("shomoy" = "time"),
        # never hyphenated directly onto the raw "HH:MM-HH:MM" digits --
        # verbalize() rewrites that span before synthesis, and a suffix
        # glued straight onto digits it is about to rewrite is exactly
        # the kind of artefact "Answers sound like a person" story 2 was
        # about eliminating.
        clauses = [
            f"{_spoken_weekday_list(days, language)} {hours} shomoy-e boshen" for hours, days in groups
        ]
        return f"{name} {', ar '.join(clauses)}."
    else:  # bengali
        clauses = [f"{_spoken_weekday_list(days, language)} {hours} সময়ে বসেন" for hours, days in groups]
        return f"{name} {', আর '.join(clauses)}।"


# ADDED BY CHAKRAVARDHAN -- "no flow may dead-end on a smartphone".
def _written_confirmation_clause(result: dict, language: str = "bengali") -> str:
    """The one clause that tells a caller whether a written copy is coming.

    ONLY PROMISES A MESSAGE WHEN ONE IS ACTUALLY ON ITS WAY. clinic-api
    reports the notification ledger row's status on every appointment
    response (see clinic-api/main.py's _schedule_delivery()); "queued"
    means the row is committed and a send has been scheduled, and that is
    the only value that earns this promise.

    "skipped" and "failed" mean nothing will arrive. Promising an SMS then
    is worse than saying nothing: the caller stops noting the number down,
    hangs up satisfied, and finds out at the reception desk.

    THE STORY'S RULE APPLIES HERE TOO. When no message is coming, the
    caller is NOT simply left with a number to memorise -- they are told
    they can be found at reception by name and phone number. That is what
    makes this flow complete without a smartphone AND without a good
    memory, which is the same requirement wearing a different hat.
    """
    status = (result.get("notification") or {}).get("status")
    if status == "queued":
        if language == "english":
            return " A confirmation message is also on its way to your phone."
        elif language == "hinglish":
            return " Aapke phone par confirmation message bhi aa raha hai."
        else:  # bengali
            return " আপনার ফোনে একটা কনফার্মেশন মেসেজও যাচ্ছে।"
    if language == "english":
        return " If no message arrives, our reception can confirm your booking by your name and phone number."
    elif language == "hinglish":
        return " Agar message na aaye, hamara reception aapka naam aur phone number se booking confirm kar sakta hai."
    else:  # bengali
        return " মেসেজ না পেলে, আমাদের কাউন্টারে নাম ও ফোন নম্বর দিয়ে আপনার বুকিং কনফার্ম করে নিতে পারবেন।"


def booking_reply(slots: dict, result: dict, language: str = "bengali") -> str:
    if result.get("success"):
        # Preserve exact values in all languages
        doctor = _spoken_doctor_name(slots, result, language=language)
        date = result["date"]
        time_slot = result["time_slot"]
        confirmation_id = result["confirmation_id"]

        if language == "english":
            reply = (
                f"Your appointment is confirmed. "
                f"{doctor}, {date}, time {time_slot}. "
                f"Your confirmation number is {confirmation_id}."
            )
        elif language == "hinglish":
            reply = (
                f"Aapka appointment confirm ho gaya. "
                f"{doctor}, {date}, time {time_slot}. "
                f"Aapka confirmation number hai {confirmation_id}."
            )
        else:  # bengali
            reply = (
                f"আপনার অ্যাপয়েন্টমেন্ট কনফার্ম হয়েছে। "
                f"{doctor}, {date}, সময় {time_slot}। "
                f"আপনার কনফার্মেশন নম্বর হলো {confirmation_id}।"
            )
        # ADDED BY CHAKRAVARDHAN -- "no flow may dead-end on a smartphone":
        # tell the caller plainly whether a written copy is actually on its
        # way, using the notification ledger status clinic-api's
        # book_appointment() now returns (see _schedule_delivery() there).
        # Rewritten onto this file's inline per-language template style
        # (rather than dev_chakravardhan's agent/i18n.t()-based version of
        # this function) because main.py/main_pcm.py call every function in
        # this module -- including this one -- with the keyword argument
        # `language=`, dozens of times; the i18n version renamed that
        # parameter to `lang`, which would break every one of those call
        # sites.
        return reply + _written_confirmation_clause(result, language)

    reason = result.get("reason")
    if reason == "slot_taken":
        alts = result.get("alternative_slots") or []
        if alts:
            # story title: Answers sound like a person, not a database row
            # user story: As a patient, I want to hear a sentence, so that
            #   the agent sounds like someone at the counter.
            # acceptance criteria: No field label, colon or bracket is ever
            #   spoken and every structured value renders as a natural
            #   clause in the reply language. An automated check fails a
            #   build containing a spoken punctuation artefact.
            if language == "english":
                return f"That time is already booked, but {', '.join(alts)} are available. Which would you prefer?"
            elif language == "hinglish":
                return f"Wo time already book ho gaya, lekin {', '.join(alts)} available hain. Kaunsa prefer karte ho?"
            else:  # bengali
                return f"ওই সময়টা বুক হয়ে গেছে, তবে {_spoken_list(alts)} সময়গুলো ফাঁকা আছে। কোনটা চান?"
        if language == "english":
            return "That time is already booked, and there are no nearby available times."
        elif language == "hinglish":
            return "Wo time already book ho gaya, aur paas mein koi available time nahi hai."
        else:  # bengali
            return "ওই সময়টা বুক হয়ে গেছে, এবং কাছাকাছি কোনো সময় ফাঁকা নেই।"
    if reason == "doctor_not_found":
        if language == "english":
            return f"Sorry, I couldn't find a doctor named '{slots.get('doctor_name')}'."
        elif language == "hinglish":
            return f"Sorry, '{slots.get('doctor_name')}' naam ka doctor nahi mila."
        else:  # bengali
            return f"দুঃখিত, '{slots.get('doctor_name')}' নামে কোনো ডাক্তার খুঁজে পেলাম না।"

    if language == "english":
        return "Sorry, couldn't book the appointment. Please try again later, or contact our counter."
    elif language == "hinglish":
        return "Sorry, appointment book nahi ho paya. Thodi der baad phir try karein, ya hamare counter se contact karein."
    else:  # bengali
        return "দুঃখিত, অ্যাপয়েন্টমেন্ট বুক করা গেল না। একটু পরে আবার চেষ্টা করুন, অথবা কাউন্টারে যোগাযোগ করুন।"


def booking_confirmation_prompt(slots: dict, language: str = "bengali") -> str:
    """Every critical value is read back before it is used (Answer Quality
    and Grounding): this is spoken once all five booking fields are known,
    BEFORE main.py ever calls book_appointment(). A misheard phone digit
    or date gets caught here, not after the write.

    Same passthrough discipline test_reply_templates_fidelity.py already
    locks in for booking_reply(): values are dropped into the sentence
    exactly as slot_parse.py produced them (ISO date, 24h "HH:MM", raw
    phone digits) with no reformatting here. agent/tts.py's synthesize()
    runs bn_normalize.verbalize() on this text before it reaches the
    caller's ear, which is what turns the ISO date/time and the phone
    digits into spoken words digit-faithfully -- nothing in this function
    needs to do that itself.

    `language` covers all four this system speaks: "bengali" (default),
    "english", "hinglish" (Hindi-English), and "banglish" (Bengali-English
    -- Kolkata's own code-switch, distinct from Hindi-English and not the
    same as "hinglish"). The doctor's name goes through
    _spoken_doctor_name() rather than being read straight off `slots`, so
    the honorific matches the sentence's language ("ডাঃ" only in Bengali,
    "Dr." otherwise) instead of a Bengali prefix landing in the middle of
    an English or transliterated sentence.

    KNOWN LIMITATION, inherited from booking_reply()/doctor_availability_
    reply() and not introduced here: `slots` never carries a
    `doctor_name_bn` alias today -- main.py/main_pcm.py only ever store
    the catalogue's plain `doctor_name` in pending["slots"], even on the
    turns where a Bengali alias WAS available (see _continue_pending's
    "doctor_choice" state, which has the alias in `candidates` and drops
    it once matched). _spoken_doctor_name() checks for the alias first
    and uses it the moment some future call site starts threading it
    through, but until that data-flow gap is closed, the Bengali branch
    below may still speak the doctor's Latin-script catalogue name inside
    an otherwise-Bengali sentence. That gap is pre-existing and shared
    with booking_reply(); closing it needs pending["slots"] to start
    carrying the alias, which is a data-flow change beyond this story.
    """
    doctor = _spoken_doctor_name(slots, {}, language=language)
    date = slots.get("date") or ""
    time_slot = slots.get("time_slot") or ""
    patient_name = slots.get("patient_name") or ""
    phone = slots.get("phone") or ""

    if language == "english":
        return (
            f"Let me confirm before I book this. "
            f"{doctor}, {date}, time {time_slot}, patient {patient_name}, "
            f"phone number {phone}. Is that all correct?"
        )
    elif language == "hinglish":
        return (
            f"Book karne se pehle confirm kar lete hain. "
            f"{doctor}, {date}, time {time_slot}, patient {patient_name}, "
            f"phone number {phone}. Sab sahi hai?"
        )
    elif language == "banglish":
        return (
            f"Book korar age ekbar confirm kore nin. "
            f"{doctor}, {date}, time {time_slot}, patient-er naam {patient_name}, "
            f"phone number {phone}. Sob thik ache to?"
        )
    else:  # bengali
        return (
            f"বুক করার আগে একবার শুনে নিন। "
            f"{doctor}, {date}, সময় {time_slot}, রোগীর নাম {patient_name}, "
            f"ফোন নম্বর {phone}। সব ঠিক আছে তো?"
        )


def booking_correction_prompt(language: str = "bengali") -> str:
    """Asked when the caller rejects booking_confirmation_prompt() above.
    Acceptance criterion: "a rejection opens a correction path rather than
    repeating the prompt" -- this is a DIFFERENT question (which field is
    wrong?), never a re-read of the same five values, and it hands the
    caller a specific menu instead of restarting the whole booking flow.

    Same four languages as booking_confirmation_prompt() above.
    """
    if language == "english":
        return "No problem -- which one should I fix, the doctor, date, time, name, or phone number?"
    elif language == "hinglish":
        return "Koi baat nahi -- kya theek karna hai, doctor, date, time, naam, ya phone number?"
    elif language == "banglish":
        return "Kono problem nei -- ki thik korte hobe, doctor, date, time, naam, na ki phone number?"
    else:  # bengali
        return "ঠিক আছে, কোনটা ঠিক করে দেব - ডাক্তার, তারিখ, সময়, নাম, নাকি ফোন নম্বর?"


# ADDED BY CHAKRAVARDHAN -- "Booking, reschedule and cancellation" story.
# No caller flow in main.py/main_pcm.py invokes these two functions yet
# within this merge's scope (only clinic-api/main.py's reschedule_appointment/
# cancel_appointment endpoints are wired up so far), but they are kept as
# genuinely new, non-duplicated functionality rather than dropped. Rewritten
# onto this file's own inline per-language template style, matching
# booking_reply() immediately above, instead of dev_chakravardhan's original
# agent/i18n.t()-based version -- consistent with every other function in
# this file and with how _written_confirmation_clause() was already adapted.
def reschedule_reply(slots: dict, result: dict, language: str = "bengali") -> str:
    """Spoken confirmation for a moved appointment.

    States explicitly that the reference number has NOT changed. A caller
    who is still holding the first message needs to hear that the paper in
    their hand is still valid, otherwise the natural assumption is that it
    is not.
    """
    if result.get("success"):
        doctor = _spoken_doctor_name(slots, result, language=language)
        date = result["date"]
        time_slot = result["time_slot"]
        confirmation_id = result["confirmation_id"]
        if language == "english":
            reply = (
                f"Your appointment has been moved. "
                f"{doctor}, {date}, time {time_slot}. "
                f"Your reference number stays the same: {confirmation_id}."
            )
        elif language == "hinglish":
            reply = (
                f"Aapka appointment reschedule ho gaya. "
                f"{doctor}, {date}, time {time_slot}. "
                f"Aapka reference number same hai: {confirmation_id}."
            )
        else:  # bengali
            reply = (
                f"আপনার অ্যাপয়েন্টমেন্ট পরিবর্তন করা হয়েছে। "
                f"{doctor}, {date}, সময় {time_slot}। "
                f"রেফারেন্স নম্বর একই থাকছে: {confirmation_id}।"
            )
        return reply + _written_confirmation_clause(result, language)

    reason = result.get("reason")
    if reason == "slot_taken":
        alts = result.get("alternative_slots") or []
        if alts:
            if language == "english":
                return f"That time is already booked, but {', '.join(alts)} are available. Which would you prefer?"
            elif language == "hinglish":
                return f"Wo time already book ho gaya, lekin {', '.join(alts)} available hain. Kaunsa prefer karte ho?"
            else:  # bengali
                return f"ওই সময়টা বুক হয়ে গেছে, তবে {_spoken_list(alts)} সময়গুলো ফাঁকা আছে। কোনটা চান?"
        if language == "english":
            return "That time is already booked, and there are no nearby available times."
        elif language == "hinglish":
            return "Wo time already book ho gaya, aur paas mein koi available time nahi hai."
        else:  # bengali
            return "ওই সময়টা বুক হয়ে গেছে, এবং কাছাকাছি কোনো সময় ফাঁকা নেই।"
    if reason == "appointment_not_found":
        if language == "english":
            return "Sorry, I couldn't find an appointment with that reference number."
        elif language == "hinglish":
            return "Sorry, is reference number se koi appointment nahi mila."
        else:  # bengali
            return "দুঃখিত, এই রেফারেন্স নম্বরে কোনো অ্যাপয়েন্টমেন্ট পাওয়া যায়নি।"
    if reason == "appointment_cancelled":
        if language == "english":
            return "That appointment has already been cancelled, so there's nothing to reschedule."
        elif language == "hinglish":
            return (
                "Wo appointment pehle hi cancel ho chuka hai, isliye reschedule karne ke liye kuch nahi hai."
            )
        else:  # bengali
            return "এই অ্যাপয়েন্টমেন্টটা আগেই বাতিল হয়ে গেছে, তাই নতুন করে সময় দেওয়ার কিছু নেই।"
    if reason == "doctor_not_available_that_day":
        if language == "english":
            return "The doctor isn't available that day. Please choose another day."
        elif language == "hinglish":
            return "Doctor us din available nahi hain. Kripya koi aur din choose karein."
        else:  # bengali
            return "ডাক্তার ওই দিন বসেন না। অন্য কোনো দিন বেছে নিন।"
    if language == "english":
        return "Sorry, couldn't reschedule the appointment. Please try again later, or contact our counter."
    elif language == "hinglish":
        return "Sorry, appointment reschedule nahi ho paya. Thodi der baad phir try karein, ya hamare counter se contact karein."
    else:  # bengali
        return "দুঃখিত, অ্যাপয়েন্টমেন্টের সময় পরিবর্তন করা গেল না। একটু পরে আবার চেষ্টা করুন, অথবা কাউন্টারে যোগাযোগ করুন।"


def cancel_reply(slots: dict, result: dict, language: str = "bengali") -> str:
    """Spoken confirmation for a cancellation.

    `already_cancelled` is reported as the plain fact rather than as an
    error, because from the caller's side it is the outcome they asked for
    -- the appointment is not going to happen. clinic-api sends no second
    message in that case, so no message is promised here either.
    """
    if result.get("success"):
        date = result["date"]
        time_slot = result["time_slot"]
        confirmation_id = result["confirmation_id"]
        if result.get("already_cancelled"):
            if language == "english":
                return "That appointment was already cancelled."
            elif language == "hinglish":
                return "Wo appointment pehle hi cancel ho chuka tha."
            else:  # bengali
                return "এই অ্যাপয়েন্টমেন্টটা আগেই বাতিল করা হয়েছিল।"
        if language == "english":
            reply = f"Your appointment on {date} at {time_slot} has been cancelled. Reference number {confirmation_id}."
        elif language == "hinglish":
            reply = f"Aapka {date} ka {time_slot} appointment cancel kar diya gaya hai. Reference number {confirmation_id}."
        else:  # bengali
            reply = (
                f"আপনার {date} তারিখের {time_slot} সময়ের অ্যাপয়েন্টমেন্টটা বাতিল করা হয়েছে। "
                f"রেফারেন্স নম্বর {confirmation_id}।"
            )
        return reply + _written_confirmation_clause(result, language)

    if result.get("reason") == "appointment_not_found":
        if language == "english":
            return "Sorry, I couldn't find an appointment with that reference number."
        elif language == "hinglish":
            return "Sorry, is reference number se koi appointment nahi mila."
        else:  # bengali
            return "দুঃখিত, এই রেফারেন্স নম্বরে কোনো অ্যাপয়েন্টমেন্ট পাওয়া যায়নি।"
    if language == "english":
        return "Sorry, couldn't cancel the appointment. Please try again later, or contact our counter."
    elif language == "hinglish":
        return "Sorry, appointment cancel nahi ho paya. Thodi der baad phir try karein, ya hamare counter se contact karein."
    else:  # bengali
        return "দুঃখিত, অ্যাপয়েন্টমেন্ট বাতিল করা গেল না। একটু পরে আবার চেষ্টা করুন, অথবা কাউন্টারে যোগাযোগ করুন।"


def doctors_by_department_reply(slots: dict, result: dict, language: str = "bengali") -> str:
    if not result.get("found"):
        if language == "english":
            return f"Sorry, we don't have a department named '{slots.get('department')}'."
        elif language == "hinglish":
            return f"Sorry, '{slots.get('department')}' naam ka department yahan nahi hai."
        else:  # bengali
            return f"দুঃখিত, '{slots.get('department')}' নামে কোনো বিভাগ আমাদের এখানে নেই।"

    department = _spoken_department(slots, result)
    doctors = result.get("doctors", [])
    filtered_by_date = bool(result.get("date"))

    if not doctors:
        if filtered_by_date:
            if language == "english":
                return f"Sorry, there are no doctors in {department} today. You can ask about another day."
            elif language == "hinglish":
                return (
                    f"Sorry, {department} department mein aaj koi doctor nahi hai. "
                    f"Aur din ke baare mein pooch sakte ho."
                )
            else:  # bengali
                return f"দুঃখিত, {department} বিভাগে আজ কোনো ডাক্তার নেই। অন্য কোনো দিনের কথা জিজ্ঞেস করতে পারেন।"
        if language == "english":
            return f"There are no doctors in {department}."
        elif language == "hinglish":
            return f"{department} department mein koi doctor nahi hai."
        else:  # bengali
            return f"{department} বিভাগে কোনো ডাক্তার নেই।"

    doctor_names = []
    for doc in doctors:
        name_bn = doc.get("doctor_name_bn")
        if language == "english":
            doctor_names.append(doc.get("name", "Doctor"))
        elif language == "hinglish":
            doctor_names.append(f"Dr. {name_bn}" if name_bn else doc.get("name", "Doctor"))
        else:  # bengali
            if name_bn:
                doctor_names.append(f"ডাঃ {name_bn}")
            else:
                doctor_names.append(doc.get("name", "ডাক্তার"))

    if language == "english":
        if len(doctor_names) == 1:
            listing = f"{department} has {doctor_names[0]}."
        elif len(doctor_names) == 2:
            listing = f"{department} has {doctor_names[0]} and {doctor_names[1]}."
        else:
            all_names = ", ".join(doctor_names[:-1]) + ", and " + doctor_names[-1]
            listing = f"{department} has {all_names}."
        return listing + " Which doctor would you like to book with?"
    elif language == "hinglish":
        if len(doctor_names) == 1:
            listing = f"{department} mein {doctor_names[0]} hain."
        elif len(doctor_names) == 2:
            listing = f"{department} mein {doctor_names[0]} aur {doctor_names[1]} hain."
        else:
            all_names = ", ".join(doctor_names[:-1]) + ", aur " + doctor_names[-1]
            listing = f"{department} mein {all_names} hain."
        return listing + " Appointment ke liye kaunse doctor ka naam batayenge?"
    else:  # bengali
        if len(doctor_names) == 1:
            listing = f"{department} বিভাগে {doctor_names[0]} আছেন।"
        elif len(doctor_names) == 2:
            listing = f"{department} বিভাগে {doctor_names[0]} এবং {doctor_names[1]} আছেন।"
        else:
            all_names = ", ".join(doctor_names[:-1]) + " এবং " + doctor_names[-1]
            listing = f"{department} বিভাগে {all_names} আছেন।"
        return listing + " অ্যাপয়েন্টমেন্টের জন্য কোন ডাক্তারের নাম বলবেন?"


# =============================================================================
# ADDED BY SOURAV -- "Lab Report Status & Secure Delivery" combined story
# (previously two separate stories: "is my report ready" and "send my
# report"). Every function below composes the reply exactly the same way
# every function above it does -- a template substitution over a real
# clinic-api response, never a fact the model states on its own -- see
# this file's own module docstring. FLOWS INTO: agent/report_flow.py's
# interpret_*() functions decide WHICH of these to call and what the next
# pending state should be; main_pcm.py / main.py call report_flow.py and
# then _speak() whatever text comes back.
#
# RULE 9 (never reveal the OTP) as a structural property of this file:
# search this whole block -- no function below ever reads an `otp_code`
# key out of any `result` dict, or receives one as a parameter. There is
# no code path here that COULD speak the correct OTP even by accident.
#
# RULE 10 (never expose the full registered phone number): every function
# that mentions the caller's phone uses `_last4()` below, never the full
# value clinic-api's `masked_phone` field already is (clinic-api masks
# to 4 digits itself -- `_last4` here exists for the one caller-supplied
# phone this file ever touches directly: the raw digits main_pcm.py/
# main.py parsed with agent.slot_parse.parse_phone(), before any tool
# call has happened yet, e.g. while composing the "is my report ready"
# offer question. Once a tool response comes back, its OWN
# `masked_phone` field is used instead, kept in the same masked shape.
# =============================================================================


def _last4(phone: str | None) -> str:
    """Rule 10. A local copy of clinic-api/main.py's `_mask_phone_last4`
    -- duplicated rather than imported because this file (the voice
    agent) and clinic-api are two separate deployables that do not share
    a Python import path; see agent/tools_client.py's module docstring."""
    if not phone:
        return "----"
    digits = "".join(c for c in phone if c.isdigit())
    return digits[-4:] if len(digits) >= 4 else digits


def patient_not_found_reply(language: str = "bengali") -> str:
    """The phone number the caller gave does not match any registered
    patient. RULE 1's "never invent" extends to identity too -- this
    never guesses which patient they might mean."""
    if language == "english":
        return "I couldn't find a patient registered with that phone number. Could you double-check it?"
    elif language == "hinglish":
        return "Is phone number se koi patient registered nahi mila. Number ek baar check kar lenge?"
    elif language == "banglish":
        return "Ei number diye kono patient registered pelam na. Number ta ektu check korben?"
    else:  # bengali
        return "এই ফোন নম্বর দিয়ে কোনো রোগী নিবন্ধিত পাইনি। নম্বরটা একটু দেখে বলবেন?"


def report_not_found_reply(language: str = "bengali") -> str:
    """RULE 1: an honest NOT_FOUND, whether it's "no reports at all"
    (Patient G) or "no report matching that test name". Never claims a
    report exists, is ready, or offers delivery/OTP for it."""
    if language == "english":
        return "I couldn't find a matching report for you. Please check with the counter."
    elif language == "hinglish":
        return "Aapka koi matching report nahi mila. Counter mein ek baar check kar lijiye."
    elif language == "banglish":
        return "Apnar matching kono report khunje pelam na. Counter e ektu check kore nin."
    else:  # bengali
        return "আপনার সাথে মেলে এমন কোনো রিপোর্ট খুঁজে পাইনি। দয়া করে কাউন্টারে খোঁজ নিন।"


def report_ambiguous_reply(result: dict, language: str = "bengali") -> str:
    """RULE 13: multiple reports match -- ask, using SAFE identifying
    information (test name only, per the plan's own suggestion), never
    guess or pick the first one. Candidates come straight from
    clinic-api's `candidates` list (see main.py's report_status())."""
    names = [c["test_name"] for c in (result.get("candidates") or [])]
    if language == "english":
        listing = (
            ", ".join(names[:-1]) + f", and {names[-1]}" if len(names) > 1 else (names[0] if names else "")
        )
        return f"You have more than one report on file -- {listing}. Which one do you mean?"
    elif language == "hinglish":
        listing = (
            ", ".join(names[:-1]) + f", aur {names[-1]}" if len(names) > 1 else (names[0] if names else "")
        )
        return f"Aapke naam pe ek se zyada report hai -- {listing}. Kaunsi wali chahiye?"
    elif language == "banglish":
        listing = (
            ", ".join(names[:-1]) + f", ar {names[-1]}" if len(names) > 1 else (names[0] if names else "")
        )
        return f"Apnar naame ekadhik report ache -- {listing}. Konta bolchen?"
    else:  # bengali
        listing = (
            ", ".join(names[:-1]) + f" এবং {names[-1]}" if len(names) > 1 else (names[0] if names else "")
        )
        return f"আপনার নামে একাধিক রিপোর্ট আছে -- {listing}। কোনটার কথা বলছেন?"


_STATUS_WORDS = {
    "NOT_READY": {
        "english": "not ready yet",
        "hinglish": "abhi ready nahi hai",
        "banglish": "ekhono ready hoyni",
        "bengali": "এখনো তৈরি হয়নি",
    },
    "PROCESSING": {
        "english": "still being processed",
        "hinglish": "process ho raha hai",
        "banglish": "processing chalche",
        "bengali": "এখনো প্রসেস হচ্ছে",
    },
    "CANCELLED": {
        "english": "cancelled",
        "hinglish": "cancel ho gaya hai",
        "banglish": "cancel hoye geche",
        "bengali": "বাতিল হয়ে গেছে",
    },
}


def report_status_reply(result: dict, language: str = "bengali") -> str:
    """RULE 2 (NOT_READY means no delivery), RULE 3 (READY required
    before delivery), RULE 16 (delivery_enabled gate). This function is
    also where RULE 2's "no clinical value is read aloud" holds
    structurally: `result` (clinic-api's report_status() response) never
    contains a clinical value at all -- no LabReport column stores one,
    per models.py's own docstring ("NO clinical value is read aloud
    under this story") -- there is nothing here that COULD leak one.

    Only offers delivery (appends the offer question) when the report is
    READY *and* delivery_enabled -- Patient I (READY, delivery_enabled
    False) hears the true status but is never asked if they want it sent.
    """
    test_name = result.get("test_name", "")
    status = result.get("status")

    if status == "READY":
        if result.get("delivery_enabled"):
            if language == "english":
                return (
                    f"Good news -- your {test_name} report is ready. "
                    f"Would you like me to send it to your registered phone?"
                )
            elif language == "hinglish":
                return (
                    f"Achi khabar -- aapka {test_name} report ready hai. "
                    f"Kya aapke registered phone pe bhej doon?"
                )
            elif language == "banglish":
                return (
                    f"Bhalo khobor -- apnar {test_name} report ready hoye geche. "
                    f"Apnar registered phone e pathiye debo?"
                )
            else:  # bengali
                return f"সুখবর -- আপনার {test_name} রিপোর্ট তৈরি হয়ে গেছে। আপনার নিবন্ধিত ফোনে পাঠিয়ে দেব?"
        # READY but delivery_enabled is False (Patient I) -- true status,
        # no offer, and no explanation of WHY (that is an internal flag,
        # not something a caller-facing reply should describe -- see
        # RULE 12/ATTACK 13's "don't expose internal implementation").
        if language == "english":
            return f"Your {test_name} report is ready. Please collect it in person from the clinic."
        elif language == "hinglish":
            return f"Aapka {test_name} report ready hai. Please clinic se khud collect kar lein."
        elif language == "banglish":
            return f"Apnar {test_name} report ready. Please clinic theke nijei collect korben."
        else:  # bengali
            return f"আপনার {test_name} রিপোর্ট তৈরি হয়ে গেছে। দয়া করে ক্লিনিক থেকে সশরীরে সংগ্রহ করুন।"

    status_words = _STATUS_WORDS.get(status, _STATUS_WORDS["PROCESSING"])
    words = status_words.get(language, status_words["bengali"])
    if language == "english":
        return f"Your {test_name} report is {words}. You don't need to travel yet -- please check back later."
    elif language == "hinglish":
        return f"Aapka {test_name} report {words}. Abhi aane ki zaroorat nahi -- thodi der baad check kariye."
    elif language == "banglish":
        return f"Apnar {test_name} report {words}. Ekhon asar dorkar nei -- pore abar check korben."
    else:  # bengali
        return f"আপনার {test_name} রিপোর্ট {words}। এখনই আসার দরকার নেই -- একটু পরে আবার খোঁজ নেবেন।"


def delivery_blocked_reply(reason: str, language: str = "bengali") -> str:
    """Used by the "report_send" flow (caller opened directly with "send
    my report") when the report cannot enter delivery at all -- RULE 3 /
    RULE 16, and TEST 12-15 in the plan's final matrix
    (DELIVERY_BLOCKED / DELIVERY_DISABLED). Deliberately never says WHY
    beyond the plain status word for NOT_READY/PROCESSING/CANCELLED, and
    for DELIVERY_DISABLED never mentions the internal flag name at all
    (ATTACK 13)."""
    if reason == "DELIVERY_DISABLED":
        if language == "english":
            return (
                "This report isn't available for phone delivery. Please collect it in person from the clinic."
            )
        elif language == "hinglish":
            return "Yeh report phone pe deliver nahi ho sakti. Please clinic se khud collect kar lein."
        elif language == "banglish":
            return "Ei report phone e deliver kora jabe na. Please clinic theke nijei collect korben."
        else:  # bengali
            return "এই রিপোর্টটা ফোনে পাঠানো যাচ্ছে না। দয়া করে ক্লিনিক থেকে সশরীরে সংগ্রহ করুন।"

    status_words = _STATUS_WORDS.get(reason, {})
    words = status_words.get(language, status_words.get("bengali", ""))
    if language == "english":
        return f"I can't send that report yet -- it's {words}. Please check back later, or visit the clinic."
    elif language == "hinglish":
        return f"Abhi woh report bhej nahi sakte -- {words}. Baad mein check kariye, ya clinic aa jaiye."
    elif language == "banglish":
        return f"Ekhon oi report pathano jabe na -- {words}. Pore check korben, na hoy clinic e asben."
    else:  # bengali
        return f"এখনই ওই রিপোর্ট পাঠানো যাচ্ছে না -- এটা {words}। পরে খোঁজ নেবেন, বা ক্লিনিকে আসতে পারেন।"


def delivery_declined_reply(language: str = "bengali") -> str:
    """Caller was offered delivery (report_status_reply's READY+enabled
    branch) and said no."""
    if language == "english":
        return "Alright, no problem. Is there anything else I can help with?"
    elif language == "hinglish":
        return "Theek hai, koi baat nahi. Aur kuch madad chahiye?"
    elif language == "banglish":
        return "Thik ache, kono problem nei. Aro kichu jante chan?"
    else:  # bengali
        return "ঠিক আছে, কোনো সমস্যা নেই। আর কিছু জানতে চান?"


def otp_requested_reply(result: dict, language: str = "bengali") -> str:
    """RULE 4 (OTP required), RULE 9 (never reveal it), RULE 10 (masked
    phone only). `result` is clinic-api's request_report_delivery()
    success response -- its `masked_phone` field is already
    last-4-digits (see clinic-api/main.py's `_mask_phone_last4`); this
    function only ever speaks that field, never a raw phone slot."""
    masked = result.get("masked_phone", "----")
    if language == "english":
        return f"I've sent an OTP to your registered number ending in {masked}. Please tell me the OTP."
    elif language == "hinglish":
        return (
            f"Aapke registered number, jo {masked} pe khatam hota hai, us par OTP bhej diya hai. OTP bataiye."
        )
    elif language == "banglish":
        return f"Apnar registered number, ja {masked} diye shesh, e OTP pathiye diyechi. OTP ta bolun."
    else:  # bengali
        return f"আপনার নিবন্ধিত নম্বরে, যেটা {masked} দিয়ে শেষ, একটা ওটিপি পাঠিয়েছি। ওটিপিটা বলুন।"


def otp_disclosure_refusal_reply(language: str = "bengali") -> str:
    """ATTACK 8: "tell me the OTP you sent." RULE 9 in its most direct
    form -- refuses outright, and redirects to the only acceptable
    source (the caller's own phone), rather than a generic "didn't
    understand, try again" that could read as evasive rather than a
    deliberate refusal."""
    if language == "english":
        return (
            "I'm not able to tell you the OTP -- please read it from the message on your phone and tell me."
        )
    elif language == "hinglish":
        return "Main OTP nahi bata sakta -- please apne phone par aaye message se OTP padh kar bataiye."
    elif language == "banglish":
        return "Ami OTP ta bolte parbo na -- please apnar phone e asha message theke OTP ta bolun."
    else:  # bengali
        return "আমি ওটিপিটা বলতে পারব না -- দয়া করে আপনার ফোনে আসা মেসেজ থেকে ওটিপিটা পড়ে বলুন।"


def otp_verify_reply(result: dict, language: str = "bengali") -> str:
    """The entire OTP/delivery outcome surface (plan Section 6 and 9) in
    one function -- mirrors clinic-api/main.py's verify_report_otp()
    reason enum exactly, one branch per reason, so a new reason added
    there is a loud KeyError-shaped gap here rather than a silently
    generic reply. RULE 9 holds throughout: none of these branches ever
    receives or reads the actual OTP value."""
    reason = result.get("reason")

    if reason == "DELIVERY_SENT":
        minutes = result.get("signed_link_expires_minutes", 15)
        if language == "english":
            return f"Your report has been securely sent. The link will expire in {minutes} minutes."
        elif language == "hinglish":
            return f"Aapka report securely bhej diya gaya hai. Link {minutes} minute mein expire ho jayega."
        elif language == "banglish":
            return f"Apnar report securely pathiye deoya hoyeche. Link {minutes} minute e expire hoye jabe."
        else:  # bengali
            return f"আপনার রিপোর্ট নিরাপদে পাঠানো হয়েছে। লিংকটা {minutes} মিনিটের মধ্যে মেয়াদ শেষ হয়ে যাবে।"

    if reason == "OTP_INVALID":
        if language == "english":
            return "That OTP doesn't match. Please check your phone and tell me the OTP again."
        elif language == "hinglish":
            return "Yeh OTP match nahi kar raha. Phone check karke dobara OTP bataiye."
        elif language == "banglish":
            return "Ei OTP ta mile na. Phone check kore abar OTP ta bolun."
        else:  # bengali
            return "এই ওটিপিটা মিলছে না। ফোন দেখে আবার ওটিপিটা বলুন।"

    if reason == "OTP_EXPIRED":
        if language == "english":
            return "That OTP has expired. Let me know if you'd still like the report sent, and I'll send a new one."
        elif language == "hinglish":
            return (
                "Yeh OTP expire ho gaya hai. Agar abhi bhi report chahiye toh bataiye, naya OTP bhej dunga."
            )
        elif language == "banglish":
            return "Ei OTP ta expire hoye geche. Ekhono report chan ki na bolun, notun OTP pathiye debo."
        else:  # bengali
            return "এই ওটিপিটার মেয়াদ শেষ হয়ে গেছে। এখনো রিপোর্ট চান কি না বলুন, নতুন ওটিপি পাঠিয়ে দেব।"

    if reason == "OTP_ALREADY_USED":
        if language == "english":
            return "That OTP has already been used. Please ask me to send the report again if you need a new one."
        elif language == "hinglish":
            return "Yeh OTP pehle hi use ho chuka hai. Naya chahiye toh dobara report bhejne ko boliye."
        elif language == "banglish":
            return "Ei OTP ta age e use hoye geche. Notun lagle abar report pathate bolun."
        else:  # bengali
            return "এই ওটিপিটা আগেই ব্যবহার হয়ে গেছে। নতুন লাগলে আবার রিপোর্ট পাঠাতে বলুন।"

    if reason == "OTP_MAX_ATTEMPTS":
        # RULE 8: locked out. Never reveals the correct value, and
        # explicitly points to a fresh flow rather than repeating the
        # same OTP prompt (which would be pointless -- the row is dead).
        if language == "english":
            return (
                "You've entered the wrong OTP too many times, so I can't verify it right now. "
                "Please ask me to send the report again to get a new OTP."
            )
        elif language == "hinglish":
            return (
                "Bahut baar galat OTP diya gaya hai, isliye abhi verify nahi kar sakte. "
                "Naya OTP ke liye dobara report bhejne ko boliye."
            )
        elif language == "banglish":
            return (
                "Onek bar bhul OTP deoya hoyeche, tai ekhon verify kora jabe na. "
                "Notun OTP er jonno abar report pathate bolun."
            )
        else:  # bengali
            return "অনেকবার ভুল ওটিপি দেওয়া হয়েছে, তাই এখন যাচাই করা যাচ্ছে না। নতুন ওটিপির জন্য আবার রিপোর্ট পাঠাতে বলুন।"

    if reason == "OTP_NOT_REQUESTED":
        if language == "english":
            return "I haven't sent an OTP for this report yet. Would you like me to send one?"
        elif language == "hinglish":
            return "Iss report ke liye abhi OTP bheja hi nahi hai. Bhej doon?"
        elif language == "banglish":
            return "Ei report er jonno ekhono OTP pathano hoyni. Pathiye debo?"
        else:  # bengali
            return "এই রিপোর্টের জন্য এখনো ওটিপি পাঠানো হয়নি। পাঠিয়ে দেব?"

    if reason == "DELIVERY_FAILED":
        # RULE 17: OTP succeeded, but delivery itself failed -- never
        # claim success. Offers the honest fallback (collection in
        # person / try again), matching RULE 17's exact wording.
        if language == "english":
            return "We couldn't send the report right now. Please try again later or collect it in person from the clinic."
        elif language == "hinglish":
            return "Abhi report bhej nahi paye. Please thodi der baad try kariye ya clinic se khud collect kar lein."
        elif language == "banglish":
            return "Ekhon report pathate parlam na. Please pore abar try korben ba clinic theke nijei collect korben."
        else:  # bengali
            return "এই মুহূর্তে রিপোর্টটা পাঠাতে পারলাম না। দয়া করে পরে আবার চেষ্টা করুন, বা ক্লিনিক থেকে সশরীরে সংগ্রহ করুন।"

    # Defensive re-checks: the report's state changed between the
    # delivery offer and the OTP being verified (RULE 3/RULE 16 checked
    # again server-side -- see clinic-api's verify_report_otp()).
    if reason in ("NOT_READY", "PROCESSING", "CANCELLED"):
        return delivery_blocked_reply(reason, language)
    if reason == "DELIVERY_DISABLED":
        return delivery_blocked_reply(reason, language)
    if reason in ("PATIENT_NOT_FOUND",):
        return patient_not_found_reply(language)
    # NOT_FOUND -- the report vanished/mismatched between calls.
    return report_not_found_reply(language)


# =============================================================================
# ADDED BY SOURAV -- "Caller asks about a health package" combined with
# "Caller asks opening hours, address or directions" (Epic: Conversation --
# Information and Enquiry). Same discipline as every function above: every
# fact spoken here (a price, an address, a set of hours) comes straight
# from clinic-api's response, never invented or guessed by this file or
# the LLM. See agent/llm.py's own comment on VALID_INTENTS for why
# "health_package" carries an OPTIONAL package_name (a caller asking "what
# packages do you have" is a complete, valid question, not an incomplete
# one waiting on a missing_slot_prompt) and why "clinic_info" bundles
# hours/address/directions as one intent narrowed by "info_topic".
# =============================================================================

_PACKAGE_FALLBACK = {
    "bengali": "প্যাকেজ",
    "english": "the package",
    "hinglish": "package",
    "banglish": "package",
}


def _join_natural(items: list[str], language: str) -> str:
    """ "a, b and c" -- shared list-joining helper for the health-package
    replies below. Reuses _SAMPLE_JOIN_WORD's per-language "and" word
    (already used by _spoken_sample_types() above for exactly this
    purpose) rather than inventing a second word list."""
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    join_word = _SAMPLE_JOIN_WORD.get(language, _SAMPLE_JOIN_WORD["bengali"])
    return f"{', '.join(items[:-1])} {join_word} {items[-1]}"


def _spoken_package_name(slots: dict, result: dict, language: str = "bengali") -> str:
    """Mirrors _spoken_test_name()'s own bengali-vs-other split exactly,
    for the same reason: the Bengali TTS tokenizer drops Latin script
    outright (see that function's docstring), so the bengali branch
    prefers the seeded Bengali alias (clinic-api's package_name_bn,
    computed the same way _first_alias_bn() already picks one for a
    LabTest/Doctor); every other language always uses the English
    catalogue name, never the Bengali alias."""
    if language == "bengali":
        return (
            result.get("package_name_bn")
            or slots.get("package_name")
            or result.get("package_name")
            or _PACKAGE_FALLBACK["bengali"]
        )
    name = slots.get("package_name") or result.get("package_name")
    return name or _PACKAGE_FALLBACK.get(language, _PACKAGE_FALLBACK["english"])


def _spoken_package_tests(result: dict, language: str = "bengali") -> list[str]:
    """Same bengali-vs-other split as _spoken_package_name() above,
    applied per included test: bengali prefers each test's own Bengali
    alias (clinic-api's tests_bn, positionally paired with tests); every
    other language speaks the English catalogue name. Falls back to the
    English name for any position where no Bengali alias was returned,
    rather than silently dropping that test from the spoken list."""
    names_en = result.get("tests") or []
    if language != "bengali":
        return list(names_en)
    names_bn = result.get("tests_bn") or []
    return [(bn or en) for bn, en in zip(names_bn, names_en)] or list(names_en)


def _package_not_found_reply(slots: dict, result: dict, language: str = "bengali") -> str:
    """Mirrors _test_not_found_reply() above exactly, for health packages
    instead of lab tests -- same found=false/did_you_mean shape, same
    "did you mean" phrasing pattern, just a different catalogue."""
    suggestions = result.get("did_you_mean") or []
    query = slots.get("package_name") or result.get("query") or ""
    if language == "english":
        if suggestions:
            return (
                f"I couldn't find a health package named '{query}'. "
                f"Did you mean {_join_natural(suggestions, language)}?"
            )
        return f"Sorry, we don't have a health package named '{query}'."
    elif language == "hinglish":
        if suggestions:
            return (
                f"'{query}' naam ka health package nahi mila. "
                f"Kya aap kehna chahte the {_join_natural(suggestions, language)}?"
            )
        return f"Sorry, '{query}' naam ka koi health package hamari list mein nahi hai."
    elif language == "banglish":
        if suggestions:
            return (
                f"'{query}' name-r health package khunje pelam na. "
                f"Apni ki bolte chaichen {_join_natural(suggestions, language)}?"
            )
        return f"Dukkhito, '{query}' name-r kono health package amader list-e nei."
    else:  # bengali
        if suggestions:
            return (
                f"'{query}' নামে হেলথ প্যাকেজ খুঁজে পাইনি। "
                f"আপনি কি বলতে চাইছেন {_join_natural(suggestions, language)}?"
            )
        return f"দুঃখিত, '{query}' নামে কোনো হেলথ প্যাকেজ আমাদের তালিকায় নেই।"


def health_package_reply(slots: dict, result: dict, language: str = "bengali") -> str:
    """ "Caller asks about a health package" -- ONE named package's full
    details, from clinic-api's GET /api/v1/health-packages/search. The
    caller already named (or fast_path/the LLM already matched) a
    specific package -- see health_packages_list_reply() just below for
    the companion "what packages do you have" case with none named.

    Price uses the exact same digit-fidelity helper (_digit_faithful_rate)
    test_rate_reply() already established for LabTest.rate_inr -- clinic-
    api's HealthPackage.price_inr is the identical SQLAlchemy Float
    column shape, so it carries the identical trailing-".0" risk, fixed
    the identical way rather than re-solving it.

    `description` (clinic-api's HealthPackage.description) is English-
    only in the database -- there is no Bengali/Hinglish/Banglish
    translation of it anywhere in the seed data. Spoken ONLY in the
    english branch for exactly that reason: injecting untranslated
    English prose into a Bengali/Hinglish/Banglish sentence is the same
    mixed-script risk _spoken_test_name()'s own docstring already
    describes, and for Bengali specifically the TTS tokenizer would
    silently drop most of it rather than mispronounce it (see
    bn_normalize.py's module docstring). Flagged, not silently worked
    around -- a real per-language description would need real translated
    content in the database, the same gap class this codebase's other
    stories have flagged before rather than fabricating a translation.
    """
    if not result.get("found"):
        return _package_not_found_reply(slots, result, language)

    name = _spoken_package_name(slots, result, language)
    price = _digit_faithful_rate(result.get("price_inr"))
    tests = _join_natural(_spoken_package_tests(result, language), language)
    description = (result.get("description") or "").strip()

    if language == "english":
        reply = f"{name} costs {price} rupees."
        if description:
            reply += f" {description}"
        if tests:
            reply += f" It includes {tests}."
        return reply
    elif language == "hinglish":
        reply = f"{name} ka price {price} rupaye hai."
        if tests:
            reply += f" Isme {tests} shamil hain."
        return reply
    elif language == "banglish":
        reply = f"{name}-er price {price} taka."
        if tests:
            reply += f" Ete {tests} include kora ache."
        return reply
    else:  # bengali
        reply = f"{name}-এর মূল্য {price} টাকা।"
        if tests:
            reply += f" এর মধ্যে {tests} আছে।"
        return reply


def health_packages_list_reply(result: dict, language: str = "bengali") -> str:
    """Companion to health_package_reply() above, for the OTHER real
    caller phrasing clinic-api/models.py's own HealthPackage docstring
    gives as the FIRST example: "What health packages do you have?" -- a
    caller who did not name any specific package at all. main.py's
    dispatch calls this (via clinic-api's GET /api/v1/health-packages, no
    name filter) whenever "package_name" was left null, instead of
    re-prompting for a package name the caller never intended to give --
    see agent/llm.py's own comment on VALID_INTENTS for why this is a
    deliberate design choice, not the missing-slot re-prompt every other
    single-entity intent in this file uses.
    """
    packages = result.get("packages") or []
    if not packages:
        # Honest edge case -- every active package was withdrawn, or the
        # catalogue is empty. Never fabricates a package that doesn't exist.
        if language == "english":
            return "Sorry, we don't have any health packages available right now."
        elif language == "hinglish":
            return "Sorry, abhi koi health package available nahi hai."
        elif language == "banglish":
            return "Dukkhito, ekhon kono health package available nei."
        else:  # bengali
            return "দুঃখিত, এই মুহূর্তে কোনো হেলথ প্যাকেজ নেই।"

    entries = []
    for pkg in packages:
        name = pkg.get("package_name_bn") if language == "bengali" else pkg.get("package_name")
        name = (
            name or pkg.get("package_name") or _PACKAGE_FALLBACK.get(language, _PACKAGE_FALLBACK["english"])
        )
        price = _digit_faithful_rate(pkg.get("price_inr"))
        if language == "english":
            entries.append(f"{name} at {price} rupees")
        elif language == "hinglish":
            entries.append(f"{name}, {price} rupaye")
        elif language == "banglish":
            entries.append(f"{name}, {price} taka")
        else:  # bengali
            entries.append(f"{name}, {price} টাকা")

    listing = _join_natural(entries, language)
    if language == "english":
        return f"We have {listing}. Which one would you like to know more about?"
    elif language == "hinglish":
        return f"Hamare paas {listing} hain. Kis package ke baare mein aur jaanna chahenge?"
    elif language == "banglish":
        return f"Amader kache {listing} ache. Kon package ta niye aro janben?"
    else:  # bengali
        return f"আমাদের কাছে {listing} আছে। কোন প্যাকেজ সম্পর্কে আরও জানতে চান?"


# Ordered Monday-first (0=Monday..6=Sunday), matching main.py's own
# `datetime.date.today().weekday()` convention (the same one
# DoctorSchedule/doctor_schedule_reply() already use) and clinic-api/
# main.py's own `_CLINIC_WEEKDAYS` tuple -- duplicated here rather than
# imported, same as _last4()/_mask_phone_last4() above: this file and
# clinic-api are two separate deployables (see agent/tools_client.py's
# module docstring).
_CLINIC_WEEKDAY_KEYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


def clinic_info_reply(slots: dict, result: dict, language: str = "bengali") -> str:
    """ "Caller asks opening hours, address or directions" -- one intent
    backing all three of that story's own bundled phrasings (models.py's
    own ClinicInfo docstring: "When do you open?" / "Where is the
    clinic?" / "Give me directions."), matching the single ClinicInfo
    table backing all three. `slots["info_topic"]` (agent/llm.py's
    extraction -- "hours"/"address"/"directions"/None) narrows which part
    gets spoken; None (caller asked generally, e.g. "tell me about your
    clinic", or asked more than one of the three at once) speaks all
    three together rather than guessing which one to leave out.

    `slots["today_weekday"]` is main.py's own resolved
    `datetime.date.today().weekday()` -- resolving "which day" is main.py's
    job everywhere else in this file (see doctor_availability's own
    date_iso default), so this function only renders whatever day it's
    given rather than importing datetime itself.

    KNOWN, FLAGGED GAP: clinic-api's `address`/`directions` fields are
    English-only free text in the database -- there is no Bengali/
    Hinglish/Banglish translation of either anywhere in the seed data.
    They are still spoken as-is in every language (an address is
    information a caller needs regardless of language, so staying silent
    is worse than speaking it in English), but for the bengali branch
    specifically, the actual Bengali TTS synthesis step will silently
    DROP the Latin-script portions of that text (street names, area
    names -- confirmed directly: `bn_normalize.unspeakable_spans()` flags
    exactly this against the real seeded address/directions strings).
    Fixing this for real would need genuinely translated Bengali-script
    address/directions text seeded in the database -- fabricating one
    here would violate this whole file's "never invent a fact" discipline
    (see module docstring), so it is flagged, not silently worked around.
    """
    if not result.get("found"):
        if language == "english":
            return "Sorry, I don't have our clinic's information available right now. Please contact our counter."
        elif language == "hinglish":
            return (
                "Sorry, abhi clinic ki jaankari available nahi hai. Please hamare counter se contact kariye."
            )
        elif language == "banglish":
            return "Dukkhito, ekhon clinic-er tottho available nei. Please amader counter-e jogajog korun."
        else:  # bengali
            return "দুঃখিত, এই মুহূর্তে ক্লিনিকের তথ্য পাওয়া যাচ্ছে না। দয়া করে কাউন্টারে যোগাযোগ করুন।"

    topic = slots.get("info_topic")
    weekday_idx = slots.get("today_weekday")
    hours = result.get("hours") or {}
    today_key = (
        _CLINIC_WEEKDAY_KEYS[weekday_idx] if weekday_idx is not None and 0 <= weekday_idx <= 6 else None
    )
    today_hours = hours.get(today_key) if today_key else None

    def hours_sentence() -> str:
        if not today_hours:
            # today_weekday wasn't resolved (or is out of range) -- an
            # honest fallback, never guesses a day's hours.
            if language == "english":
                return "Sorry, I don't have today's hours right now."
            elif language == "hinglish":
                return "Sorry, aaj ke hours abhi available nahi hain."
            elif language == "banglish":
                return "Dukkhito, ajker hours ekhon bolte parchi na."
            else:  # bengali
                return "দুঃখিত, আজকের সময়সূচি এখন বলতে পারছি না।"
        if today_hours.get("closed"):
            if language == "english":
                return "We are closed today."
            elif language == "hinglish":
                return "Aaj hum band hain."
            elif language == "banglish":
                return "Aj amra bondho achi."
            else:  # bengali
                return "আজ আমরা বন্ধ আছি।"
        open_, close_ = today_hours.get("open"), today_hours.get("close")
        if language == "english":
            return f"We're open today from {open_} to {close_}."
        elif language == "hinglish":
            return f"Aaj hum {open_} se {close_} tak khule hain."
        elif language == "banglish":
            return f"Aj amra {open_} theke {close_} porjonto khola achi."
        else:  # bengali
            return f"আজ আমরা {open_} থেকে {close_} পর্যন্ত খোলা আছি।"

    def address_sentence() -> str:
        address = result.get("address") or ""
        if language == "english":
            return f"Our address is {address}."
        elif language == "hinglish":
            return f"Hamara address hai {address}."
        elif language == "banglish":
            return f"Amader address {address}."
        else:  # bengali
            return f"আমাদের ঠিকানা হলো {address}।"

    def directions_sentence() -> str:
        directions = result.get("directions") or ""
        if language == "english":
            return f"Here's how to find us. {directions}"
        elif language == "hinglish":
            return f"Humein aise dhundh sakte hain. {directions}"
        elif language == "banglish":
            return f"Amader emon vabe khuje paben. {directions}"
        else:  # bengali
            return f"আমাদের এভাবে খুঁজে পাবেন। {directions}"

    if topic == "hours":
        return hours_sentence()
    if topic == "address":
        return address_sentence()
    if topic == "directions":
        return directions_sentence()

    # No specific topic -- speak all three together, since the caller may
    # have asked more than one of these in the same breath (e.g. "when do
    # you open and what's the address"), and guessing which one to leave
    # out would silently drop half the answer.
    return " ".join([hours_sentence(), address_sentence(), directions_sentence()])


# =============================================================================
# ADDED BY SOURAV -- "Caller asks how to prepare for a test" story's bundled
# human_fallback config (lab_tests_with_fallback_config sample file's
# voice_agent_config.human_fallback block).
#
# The config's own trigger_condition is "query_unresolved_or_low_confidence"
# -- this codebase's one real, already-existing signal for exactly that is
# agent/llm.py's "unclear" intent (see its own docstring for exactly when
# the classifier returns it; there is no separate numeric confidence score
# threaded through to main.py's dispatch to check against, so this does not
# invent one). The config's action is "transfer_to_human_agent" -- but there
# is no telephony transfer capability anywhere in this codebase (no SIP/PSTN
# library, no call-control API of any kind), so an ACTUAL call transfer is
# not something this file can honestly build. See agent/outcomes.py's
# record_human_handoff() for the other, buildable half of that action (an
# escalation-ledger entry a human follow-up process can act on, the same
# pattern already established there for the insufficient-verified-
# information outcome) -- this function is ONLY the spoken half.
# =============================================================================


def human_fallback_reply(language: str = "bengali") -> str:
    """The business's own "connecting you to an expert" script, stored
    verbatim (same "genuine, business-reviewed translation -- store as
    given, don't recompose" discipline as clinic-api/seed.py's
    LAB_TEST_ADVISORIES scripts). No dynamic value of any kind -- always
    exactly one of these four fixed sentences."""
    if language == "english":
        return "I understand. Let me connect you with one of our experts right away."
    elif language == "hinglish":
        return "Acha samjh gaya! Mai aapko humare expert ke saath connect kar deta hoon."
    elif language == "banglish":
        return "Acha, bujhte perechi! Ami apnake amader ekjon expert-er sathe connect kore dichi."
    else:  # bengali
        return "আচ্ছা, বুঝতে পেরেছি! আমি আপনাকে আমাদের একজন এক্সপার্টের সাথে কানেক্ট করে দিচ্ছি।"


# =============================================================================
# ADDED BY SOURAV -- "Caller asks something the agent does not cover" story.
#
# Distinct from human_fallback_reply() just above: that one fires for
# "unclear" (garbled/ambiguous ASR, could not classify at all) and connects
# straight to a human with no question asked. This story's trigger --
# agent/llm.py's "out_of_scope" intent -- is the opposite kind of turn: the
# classifier understood the caller PERFECTLY, it just is not a service this
# assistant's intents cover. Silently forcing that into human_fallback's
# "connecting you now" would misrepresent what actually happened (nothing
# was unclear), so this gets its own two-step exchange instead: offer the
# caller an explicit choice, then act on whichever they pick.
#
#   1. out_of_scope_reply()          -- the initial offer (main.py speaks
#                                        this, then waits in a dedicated
#                                        "out_of_scope_choice" pending state).
#   2a. caller says yes  -> reuses human_fallback_reply() verbatim (same
#       honest "logged for a human, not a real transfer" handling as the
#       "unclear" path -- see agent/outcomes.record_human_handoff(), called
#       with intent="out_of_scope" so the two stay distinguishable in the
#       escalation ledger) -- main.py's dispatch, not a new function here.
#   2b. caller says no   -> out_of_scope_counter_reply() below.
# =============================================================================


def out_of_scope_reply(language: str = "bengali") -> str:
    """The initial offer for a request no intent covers at all: connect to
    a person, or the caller contacts the counter themselves. Phrased as an
    explicit either/or so the caller's next turn is a plain yes/no,
    parseable by agent/slot_parse.py's is_affirmative()/is_negative() the
    same way every other confirm-style prompt in this codebase already is
    (see main.py's "confirm_delivery" pending state for the identical
    shape)."""
    if language == "english":
        return (
            "That's not something I'm able to help with here. Would you like me to "
            "connect you with one of our staff, or would you rather contact our counter directly?"
        )
    elif language == "hinglish":
        return (
            "Ye main yahan handle nahi kar sakta. Kya aapko hamare staff se connect "
            "karwa doon, ya aap seedhe counter par contact karna chahenge?"
        )
    elif language == "banglish":
        return (
            "Eta ami ekhane help korte parbo na. Apnake ki amader staff-er sathe connect "
            "kore debo, naki apni nijei counter-e jogajog korben?"
        )
    else:  # bengali
        return (
            "এটা আমি এখানে সাহায্য করতে পারব না। আপনাকে কি আমাদের স্টাফের সাথে সংযুক্ত করে "
            "দেব, নাকি আপনি নিজে কাউন্টারে যোগাযোগ করবেন?"
        )


def out_of_scope_counter_reply(language: str = "bengali") -> str:
    """Caller declined the human-connect offer above -- they'll contact
    the counter themselves. Mirrors delivery_declined_reply()'s "close the
    loop, then reopen the floor" shape (acknowledge, then ask if anything
    else is needed) rather than just ending on the decline."""
    if language == "english":
        return (
            "Alright, please contact our counter directly for that. Is there anything else I can help with?"
        )
    elif language == "hinglish":
        return "Theek hai, iske liye seedhe hamare counter se contact kijiye. Aur kuch madad chahiye?"
    elif language == "banglish":
        return "Thik ache, erjonyo shorashori amader counter-e jogajog korun. Aro kichu jante chan?"
    else:  # bengali
        return "ঠিক আছে, এর জন্য সরাসরি আমাদের কাউন্টারে যোগাযোগ করুন। আর কিছু জানতে চান?"


# =============================================================================
# ADDED BY SOURAV -- Phase 1: Database Schema & Policy Tables (Walk-in
# Eligibility, Prescription Requirements, Insurance Coverage Policy,
# Outstanding Balance / Billing stories).
#
# All four follow the exact found/policy_available honesty split
# test_preparation_reply() above established: a row that doesn't exist is
# a different, distinct outcome from a row that exists but has never been
# reviewed for this particular policy -- the latter NEVER gets a guessed
# answer (see clinic-api/models.py's own comments on why every new column
# these stories added is nullable with no default).
# =============================================================================


def walkin_eligibility_reply(slots: dict, result: dict, language: str = "bengali") -> str:
    """ "Walk-in Eligibility" story. THREE outcomes, matching clinic-api/
    main.py's _walkin_policy_reply_dict() docstring:

      1. found=False -> the shared not-found/did-you-mean reply (same
         helper test_rate_reply()/test_preparation_reply()/etc. use --
         the response shape is identical: {"found", "query", "did_you_mean"}).
      2. found=True, policy_available=False -> honest "we haven't
         reviewed walk-in policy for this test yet".
      3. found=True, policy_available=True -> speaks walkin_eligible
         (yes/no) plus walkin_hours when eligible and a real hours string
         is on file.
    """
    if not result.get("found"):
        return _test_not_found_reply(slots, result, language)

    name = _spoken_test_name(slots, result, language)

    if not result.get("policy_available"):
        if language == "english":
            return f"I don't have walk-in policy information for {name} yet. Please check with the counter."
        elif language == "hinglish":
            return (
                f"{name} ke walk-in policy ki jaankari abhi mere paas nahi hai. Counter se check kar lijiye."
            )
        elif language == "banglish":
            return f"{name}-er walk-in policy-r information amar kache ekhon nei. Counter-e jiggesh korben."
        else:  # bengali
            return f"{name}-এর ওয়াক-ইন নীতি সম্পর্কে এখন তথ্য আমার কাছে নেই। দয়া করে কাউন্টারে জিজ্ঞেস করুন।"

    eligible = bool(result.get("walkin_eligible"))
    hours = result.get("walkin_hours")

    if language == "english":
        if not eligible:
            return f"Sorry, {name} is not available on a walk-in basis -- you'll need an appointment."
        if hours:
            return f"Yes, you can walk in for {name}. Walk-in hours are {hours}."
        return f"Yes, you can walk in for {name}."
    elif language == "hinglish":
        if not eligible:
            return f"Sorry, {name} ke liye walk-in possible nahi hai -- appointment leni hogi."
        if hours:
            return f"Haan, {name} ke liye walk-in kar sakte ho. Walk-in timing hai {hours}."
        return f"Haan, {name} ke liye walk-in kar sakte ho."
    elif language == "banglish":
        if not eligible:
            return f"Dukkhito, {name}-er jonno walk-in kora jabe na -- appointment lagbe."
        if hours:
            return f"Han, {name}-er jonno walk-in korte paren. Walk-in-er shomoy {hours}."
        return f"Han, {name}-er jonno walk-in korte paren."
    else:  # bengali
        if not eligible:
            return f"দুঃখিত, {name}-এর জন্য ওয়াক-ইন করা যাবে না -- অ্যাপয়েন্টমেন্ট লাগবে।"
        if hours:
            return f"হ্যাঁ, {name}-এর জন্য ওয়াক-ইন করতে পারেন। ওয়াক-ইনের সময় {hours}।"
        return f"হ্যাঁ, {name}-এর জন্য ওয়াক-ইন করতে পারেন।"


def _spoken_channel(raw: str) -> str:
    """A prescription_channels entry is stored as whatever identifier the
    catalogue uses (e.g. "whatsapp_photo", "counter_in_person") -- see
    clinic-api/seed.py's PRESCRIPTION_POLICY (Phase 2 sample data) for
    the values currently on file, and its own comment for why those are
    sample/demo values rather than reviewed business content. There is
    still no reviewed controlled vocabulary/label mapping to translate
    these identifiers against. Rather than inventing one (the same
    "don't guess a business fact" discipline as everywhere else in this
    file), this renders whatever string is actually on file as plain
    words -- underscores to spaces, unchanged casing otherwise -- so any
    value already on file, sample or real, is spoken intelligibly
    without this file pretending to know channel names it was never
    given a controlled vocabulary for."""
    return raw.replace("_", " ").strip()


def _spoken_channel_list(channels: list, language: str = "bengali") -> str:
    words = [_spoken_channel(c) for c in channels if c]
    if not words:
        return ""
    if len(words) == 1:
        return words[0]
    joiner = _SAMPLE_JOIN_WORD.get(language, "এবং")
    return f"{', '.join(words[:-1])} {joiner} {words[-1]}"


def prescription_requirements_reply(slots: dict, result: dict, language: str = "bengali") -> str:
    """ "Prescription Requirements" story. Same found/policy_available
    split as walkin_eligibility_reply() above, over prescription_required/
    prescription_channels instead of walkin_eligible/walkin_hours."""
    if not result.get("found"):
        return _test_not_found_reply(slots, result, language)

    name = _spoken_test_name(slots, result, language)

    if not result.get("policy_available"):
        if language == "english":
            return f"I don't have prescription requirement information for {name} yet. Please check with the counter or your doctor."
        elif language == "hinglish":
            return f"{name} ke prescription requirement ki jaankari abhi mere paas nahi hai. Counter ya apne doctor se check kar lijiye."
        elif language == "banglish":
            return f"{name}-er prescription requirement-er information amar kache ekhon nei. Counter othoba apnar doctor-ke jiggesh korben."
        else:  # bengali
            return f"{name}-এর প্রেসক্রিপশন প্রয়োজনীয়তা সম্পর্কে এখন তথ্য আমার কাছে নেই। দয়া করে কাউন্টারে বা আপনার ডাক্তারকে জিজ্ঞেস করুন।"

    required = bool(result.get("prescription_required"))
    channels = result.get("prescription_channels") or []
    channel_text = _spoken_channel_list(channels, language)

    if language == "english":
        if not required:
            return f"No, {name} does not require a doctor's prescription."
        if channel_text:
            return f"Yes, {name} requires a doctor's prescription -- you can submit it via {channel_text}."
        return f"Yes, {name} requires a doctor's prescription."
    elif language == "hinglish":
        if not required:
            return f"Nahi, {name} ke liye doctor ka prescription nahi chahiye."
        if channel_text:
            return f"Haan, {name} ke liye doctor ka prescription chahiye -- aap ise {channel_text} se submit kar sakte ho."
        return f"Haan, {name} ke liye doctor ka prescription chahiye."
    elif language == "banglish":
        if not required:
            return f"Na, {name}-er jonno doctor-er prescription lagbe na."
        if channel_text:
            return f"Han, {name}-er jonno doctor-er prescription lagbe -- apni eta {channel_text}-e submit korte paren."
        return f"Han, {name}-er jonno doctor-er prescription lagbe."
    else:  # bengali
        if not required:
            return f"না, {name}-এর জন্য ডাক্তারের প্রেসক্রিপশন লাগবে না।"
        if channel_text:
            return (
                f"হ্যাঁ, {name}-এর জন্য ডাক্তারের প্রেসক্রিপশন লাগবে -- আপনি এটা {channel_text}-এর মাধ্যমে জমা দিতে পারেন।"
            )
        return f"হ্যাঁ, {name}-এর জন্য ডাক্তারের প্রেসক্রিপশন লাগবে।"


def _insurance_provider_not_found_reply(slots: dict, result: dict, language: str = "bengali") -> str:
    """Honest "we don't recognise that insurer" -- never silently matched
    to the wrong provider, and never assumed NOT_COVERED just because the
    name wasn't recognised (that would be indistinguishable from a real
    reviewed denial, which RULE 1's "never invent" extends to here too)."""
    queried = result.get("query_provider") or slots.get("insurance_provider_name") or ""
    if language == "english":
        return f"I don't recognise '{queried}' as an insurance provider we have on file. Could you double-check the name?"
    elif language == "hinglish":
        return f"'{queried}' naam ka insurance provider hamare paas registered nahi hai. Naam ek baar check kar lenge?"
    elif language == "banglish":
        return f"'{queried}' name-r insurance provider amader kache nei. Naam-ta ektu check korben?"
    else:  # bengali
        return f"'{queried}' নামে কোনো ইন্স্যুরেন্স প্রোভাইডার আমাদের তালিকায় নেই। নামটা একটু দেখে বলবেন?"


def insurance_coverage_reply(slots: dict, result: dict, language: str = "bengali") -> str:
    """ "Insurance Coverage Policy" story. FOUR outcomes, matching
    clinic-api/main.py's insurance_coverage() docstring:

      1. test_found=False -> shared not-found/did-you-mean reply (same
         {"found"/"test_found" naming quirk aside, the payload shape
         reused by _test_not_found_reply is identical: "did_you_mean").
      2. test_found=True, provider_found=False -> honest "we don't
         recognise that insurer".
      3. provider_found=True, policy_available=False -> honest "no
         reviewed (test, provider) row yet" -- never a guessed
         COVERED/NOT_COVERED (see models.py's InsurancePolicy.coverage_
         status comment).
      4. policy_available=True -> speaks coverage_status plus whether
         pre-authorization is required.
    """
    if not result.get("test_found"):
        # _test_not_found_reply reads slots.get("test_name") and
        # result.get("did_you_mean") -- both present here under the same
        # keys test_rate/test_preparation/etc. already use.
        return _test_not_found_reply(slots, result, language)

    test_name = result.get("test_name") or slots.get("test_name") or ""

    if not result.get("provider_found"):
        return _insurance_provider_not_found_reply(slots, result, language)

    provider_name = result.get("provider_name") or ""

    if not result.get("policy_available"):
        if language == "english":
            return f"I don't have reviewed coverage information for {test_name} under {provider_name} yet. Please check with the counter or your insurer."
        elif language == "hinglish":
            return f"{provider_name} ke under {test_name} ki coverage information abhi mere paas nahi hai. Counter ya apni insurance company se check kar lijiye."
        elif language == "banglish":
            return f"{provider_name}-er under-e {test_name}-er coverage information amar kache ekhon nei. Counter othoba apnar insurance company-ke jiggesh korben."
        else:  # bengali
            return f"{provider_name}-এর আওতায় {test_name}-এর কভারেজ সম্পর্কে এখন তথ্য আমার কাছে নেই। দয়া করে কাউন্টারে বা আপনার ইন্স্যুরেন্স কোম্পানিকে জিজ্ঞেস করুন।"

    status = result.get("coverage_status")
    pre_auth = bool(result.get("pre_auth_required"))
    covered = status == "COVERED"

    if language == "english":
        base = (
            f"Good news -- {test_name} is covered under {provider_name}."
            if covered
            else f"{test_name} is not covered under {provider_name}."
            if status == "NOT_COVERED"
            else f"{test_name} is partially covered under {provider_name}."
        )
        if pre_auth:
            return base + " Pre-authorization is required before the test."
        return base
    elif language == "hinglish":
        base = (
            f"Achi khabar -- {provider_name} mein {test_name} cover hota hai."
            if covered
            else f"{provider_name} mein {test_name} cover nahi hota."
            if status == "NOT_COVERED"
            else f"{provider_name} mein {test_name} partially cover hota hai."
        )
        if pre_auth:
            return base + " Test se pehle pre-authorization chahiye hoga."
        return base
    elif language == "banglish":
        base = (
            f"Bhalo khobor -- {provider_name}-e {test_name} cover hoy."
            if covered
            else f"{provider_name}-e {test_name} cover hoy na."
            if status == "NOT_COVERED"
            else f"{provider_name}-e {test_name} partially cover hoy."
        )
        if pre_auth:
            return base + " Test-er age pre-authorization lagbe."
        return base
    else:  # bengali
        base = (
            f"সুখবর -- {provider_name}-এর আওতায় {test_name} কভার হয়।"
            if covered
            else f"{provider_name}-এর আওতায় {test_name} কভার হয় না।"
            if status == "NOT_COVERED"
            else f"{provider_name}-এর আওতায় {test_name} আংশিকভাবে কভার হয়।"
        )
        if pre_auth:
            return base + " টেস্টের আগে প্রি-অথোরাইজেশন লাগবে।"
        return base


def billing_balance_reply(result: dict, language: str = "bengali") -> str:
    """ "Outstanding Balance / Billing" story. THREE outcomes, matching
    clinic-api/main.py's patient_billing() docstring:

      1. patient_found=False -> patient_not_found_reply() (reused
         directly -- identical phone-not-registered outcome as the
         report flows).
      2. patient_found=True, found=False -> honest "no billing record on
         file for you yet" -- never a guessed/defaulted zero balance (see
         clinic-api/models.py's PatientBilling docstring: no row and a
         real 0.0 balance are different, both real, outcomes).
      3. found=True -> speaks the real outstanding_amount (digit-faithful,
         same helper test_rate_reply() uses for a price), plus the due
         date when one is on file.
    """
    if not result.get("patient_found"):
        return patient_not_found_reply(language)

    if not result.get("found"):
        if language == "english":
            return "I don't have a billing record on file for you yet. Please check with the counter."
        elif language == "hinglish":
            return "Aapke liye abhi koi billing record mere paas nahi hai. Counter se check kar lijiye."
        elif language == "banglish":
            return "Apnar jonno ekhon kono billing record amar kache nei. Counter-e jiggesh korben."
        else:  # bengali
            return "আপনার জন্য এখন কোনো বিলিং রেকর্ড আমার কাছে নেই। দয়া করে কাউন্টারে জিজ্ঞেস করুন।"

    amount = _digit_faithful_rate(result["outstanding_amount"])
    due_date = result.get("due_date")

    if language == "english":
        base = f"You have an outstanding balance of {amount} rupees."
        return base + f" It is due by {due_date}." if due_date else base
    elif language == "hinglish":
        base = f"Aapka {amount} rupaye ka outstanding balance hai."
        return base + f" Yeh {due_date} tak due hai." if due_date else base
    elif language == "banglish":
        base = f"Apnar {amount} taka outstanding balance ache."
        return base + f" Eta {due_date}-er modhye due." if due_date else base
    else:  # bengali
        base = f"আপনার {amount} টাকা বকেয়া আছে।"
        return base + f" এটা {due_date}-এর মধ্যে দিতে হবে।" if due_date else base


# =============================================================================
# ADDED BY SOURAV -- "Caller asks two questions in one breath" story.
#
# main.py's new multi-intent dispatch path (see _dispatch_multi_intent_turn's
# own docstring) resolves each question in a combined turn independently and
# joins the results into ONE spoken reply, strictly in the order asked
# (Criterion 1). Most branches reuse an EXISTING reply_templates function
# verbatim for their fragment -- test_rate_reply(), walkin_eligibility_reply(),
# etc. already speak an honest, self-contained sentence for their intent, so
# nothing new is needed for those, including the "found but not yet reviewed"
# case (Criterion 2's "unreviewed" example): a policy row with
# policy_available=False already gets an honest "not reviewed yet" sentence
# from e.g. walkin_eligibility_reply() today, so calling that same function
# again here needs zero new code to be honest about it.
#
# The three functions below are for the cases that have NO existing
# single-question analog, because a single-question turn never needed one:
#   1. multi_intent_missing_info_reply() -- an otherwise-combinable intent
#      (e.g. test_rate) is missing its one required slot. A solo turn
#      re-prompts and waits (see missing_slot_prompt() + the matching
#      pending state); a combined turn does NOT open a second pending
#      state on top of whatever the FIRST question's tool call needs to
#      report -- see _dispatch_multi_intent_turn()'s own docstring for why
#      juggling two independent pending flows from one turn is out of
#      scope for this story -- so this speaks a generic, honest
#      "I need more detail, please ask that one again on its own" fragment
#      instead of guessing the missing field.
#   2. multi_intent_out_of_scope_reply() -- the caller's OTHER question was
#      "out_of_scope" (agent/llm.py's intent). A solo turn offers an
#      interactive yes/no choice (out_of_scope_reply() + the
#      "out_of_scope_choice" pending state) -- again, not stackable behind
#      a first question's own follow-up state in a combined turn -- so this
#      is a single non-interactive line naming both options at once
#      (counter or staff) rather than asking the caller to pick.
#   3. multi_intent_needs_separate_flow_reply() -- the caller's OTHER
#      question was book_appointment, report_status, or report_send.
#      These three are deliberately NEVER answered inline here, even when
#      every slot they need happens to already be present -- each is a
#      multi-turn, sometimes security-sensitive (OTP) flow of its own
#      (see _dispatch_multi_intent_turn()'s docstring for the full
#      reasoning), so this always speaks an honest "that one needs its own
#      conversation, please ask it right after this" fragment rather than
#      attempting to compose that whole flow into a shared reply.
#
# All three take no dynamic slot content -- deliberately generic rather than
# per-intent/per-field wording, so one fragment covers every intent in its
# bucket without a combinatorial explosion of new per-language strings.
# =============================================================================


def multi_intent_missing_info_reply(language: str = "bengali") -> str:
    """Fragment for the OTHER question in a combined turn when it is an
    otherwise-answerable intent that is missing its required slot. See the
    module-level note above for why this doesn't reuse missing_slot_prompt()
    or open a pending state."""
    if language == "english":
        return (
            "For your other question, I'll need a bit more detail to answer that properly. "
            "Could you ask that one again on its own?"
        )
    elif language == "hinglish":
        return (
            "Aapke doosre sawaal ke liye, mujhe thoda aur detail chahiye hoga. "
            "Kya aap wo sawaal alag se dubara pooch sakte hain?"
        )
    elif language == "banglish":
        return (
            "Apnar onno prashner jonno, thik moto uttor dite amar aro ektu details lagbe. "
            "Apni ki oita alada kore abar jiggesh korte parben?"
        )
    else:  # bengali
        return (
            "আপনার অন্য প্রশ্নটির জন্য, ঠিকমতো উত্তর দিতে আমার আরেকটু বিস্তারিত তথ্য দরকার। "
            "আপনি কি প্রশ্নটা আলাদা করে আবার জিজ্ঞেস করতে পারবেন?"
        )


def multi_intent_out_of_scope_reply(language: str = "bengali") -> str:
    """Fragment for the OTHER question in a combined turn when it is
    "out_of_scope" (agent/llm.py). Non-interactive, unlike
    out_of_scope_reply() -- see the module-level note above."""
    if language == "english":
        return (
            "As for your other question, that's not something I'm able to help with here. "
            "Please contact our counter, or ask to be connected with our staff for that one."
        )
    elif language == "hinglish":
        return (
            "Aapke doosre sawaal ke liye, wo main yahan handle nahi kar sakta. "
            "Uske liye seedhe counter par contact karein, ya hamare staff se connect karne ko bolein."
        )
    elif language == "banglish":
        return (
            "Apnar onno prashner jonno, oita ami ekhane help korte parbo na. "
            "Oi bepare shorashori counter-e jogajog korun, na hole amader staff-er sathe connect korte bolun."
        )
    else:  # bengali
        return (
            "আপনার অন্য প্রশ্নটির জন্য, সেটা আমি এখানে সাহায্য করতে পারব না। "
            "এর জন্য সরাসরি কাউন্টারে যোগাযোগ করুন, অথবা আমাদের স্টাফের সাথে সংযুক্ত হতে বলুন।"
        )


def multi_intent_needs_separate_flow_reply(language: str = "bengali") -> str:
    """Fragment for the OTHER question in a combined turn when it is
    book_appointment, report_status, or report_send -- always given this
    fragment regardless of slot completeness. See the module-level note
    above for why these three are never composed inline."""
    if language == "english":
        return (
            "As for your other question, that needs a bit more back-and-forth to sort out properly. "
            "Could you ask me that one separately, right after this?"
        )
    elif language == "hinglish":
        return (
            "Aapke doosre sawaal ke liye, uske liye thoda aur baat-cheet karni padegi taaki sahi tarike se ho sake. "
            "Kya aap wo sawaal iske turant baad alag se pooch sakte hain?"
        )
    elif language == "banglish":
        return (
            "Apnar onno prashner jonno, oita thik moto shomadhan korte aro kotha bolte hobe. "
            "Apni ki oita ei kothar por alada kore jiggesh korte parben?"
        )
    else:  # bengali
        return (
            "আপনার অন্য প্রশ্নটির জন্য, সেটা ঠিকমতো সমাধান করতে আরেকটু কথা বলা দরকার। "
            "আপনি কি এর পরেই প্রশ্নটা আলাদা করে জিজ্ঞেস করতে পারবেন?"
        )


# ADDED BY SOURAV -- "Caller asks the agent to compare two options" story.
# Everything below renders agent/compare_flow.py's build_comparison() dict
# (pure FACTS ONLY, computed with decimal.Decimal -- see that module's own
# docstring) into one spoken sentence per language. Exactly like every
# other function in this file, no new fact is invented here: this only
# picks WORDING for values compare_flow.py already computed. In
# particular there is no code path here, in any language, that can ever
# say one option is "better" -- see this file's own module docstring
# ("the LLM never gets a chance...") and agent/llm.py's CLINICAL SAFETY
# NOTE for why that judgment is structurally absent everywhere upstream
# too, not just skipped here.


def _spoken_compare_entity_name(given_name: str, entity: dict, language: str = "bengali") -> str:
    """Mirrors _spoken_test_name()/_spoken_package_name()'s own bengali-
    alias-vs-caller's-words split (see those functions' docstrings for the
    full mixed-script reasoning) for compare_options, where either side
    can turn out to be EITHER a test or a package -- entity["kind"] (set
    by main.py's _resolve_comparable_entity()) says which alias field to
    read. `given_name` is always the caller's own words for this side
    (agent/llm.py's "compare_option_a"/"compare_option_b" slot, copied
    literally, never classified by the model) -- used whenever no better
    name is available, and in every non-bengali branch regardless of kind,
    for the same reason those two functions never speak a Bengali alias
    into a non-Bengali sentence."""
    kind = entity.get("kind")
    if language == "bengali":
        if kind == "test":
            return (
                entity.get("test_name_bn")
                or given_name
                or entity.get("test_name")
                or _TEST_FALLBACK["bengali"]
            )
        if kind == "package":
            return (
                entity.get("package_name_bn")
                or given_name
                or entity.get("package_name")
                or _PACKAGE_FALLBACK["bengali"]
            )
        return given_name or _TEST_FALLBACK["bengali"]
    if kind == "test":
        return (
            given_name or entity.get("test_name") or _TEST_FALLBACK.get(language, _TEST_FALLBACK["english"])
        )
    if kind == "package":
        return (
            given_name
            or entity.get("package_name")
            or _PACKAGE_FALLBACK.get(language, _PACKAGE_FALLBACK["english"])
        )
    return given_name or _TEST_FALLBACK.get(language, _TEST_FALLBACK["english"])


def _compare_not_found_reply(name_a: str, name_b: str, comparison: dict, language: str = "bengali") -> str:
    """Both-not-found / one-not-found branches of compare_options_reply()
    below -- pulled out separately because neither case has anything to
    compute (build_comparison() itself returns early with no arithmetic at
    all whenever either side is not_found -- see that function's own
    docstring), only something to SAY honestly instead of fabricating a
    comparison for an entity that was never found."""
    a_found, b_found = comparison["a_found"], comparison["b_found"]
    if not a_found and not b_found:
        if language == "english":
            return f"I couldn't find either '{name_a}' or '{name_b}' in our catalogue."
        elif language == "hinglish":
            return f"Mujhe '{name_a}' ya '{name_b}', dono hamare catalogue mein nahi mile."
        elif language == "banglish":
            return f"'{name_a}' ba '{name_b}', duitar kono ta-i amader list-e khunje pelam na."
        else:  # bengali
            return f"'{name_a}' বা '{name_b}' কোনোটাই আমাদের তালিকায় খুঁজে পাইনি।"
    missing_name, other_name = (name_a, name_b) if not a_found else (name_b, name_a)
    if language == "english":
        return f"I couldn't find '{missing_name}' in our catalogue, so I can't compare it with {other_name}."
    elif language == "hinglish":
        return f"Mujhe '{missing_name}' hamare catalogue mein nahi mila, isliye {other_name} ke saath compare nahi kar sakta."
    elif language == "banglish":
        return f"'{missing_name}' amader list-e khunje pelam na, tai {other_name}-r sathe compare korte parchi na."
    else:  # bengali
        return f"'{missing_name}' আমাদের তালিকায় খুঁজে পাইনি, তাই {other_name}-এর সাথে তুলনা করতে পারছি না।"


# Per-language "N more" suffix for a capped extra-tests list -- see
# _capped_extra_tests_phrase() below. Kept to a small cap (2 named tests)
# so the "single concise sentence" AC holds even for a package with many
# extra components -- a caller wants the headline fact (how many more,
# and a couple of examples), not every row of a database table read aloud.
_MORE_TESTS_SUFFIX = {
    "english": " and {n} more",
    "hinglish": " aur {n} zyada",
    "banglish": " ar {n} ta beshi",
    "bengali": " এবং আরও {n}টি",
}
_EXTRA_TESTS_CAP = 2


def _capped_extra_tests_phrase(extra: list[dict], language: str) -> str:
    """extra: one of build_comparison()'s "extra_tests_a"/"extra_tests_b"
    lists ([{"name": en, "name_bn": bn or None}, ...], already positionally
    paired and set-differenced by compare_flow.py -- no matching or
    lookup happens here, only picking which of the two names to speak,
    same bengali-vs-other split every other list in this file uses."""
    names = [(e.get("name_bn") or e.get("name")) if language == "bengali" else e.get("name") for e in extra]
    names = [n for n in names if n]
    if not names:
        return ""
    shown = names[:_EXTRA_TESTS_CAP]
    phrase = _join_natural(shown, language)
    remaining = len(names) - len(shown)
    if remaining > 0:
        suffix = _MORE_TESTS_SUFFIX.get(language, _MORE_TESTS_SUFFIX["english"]).format(n=remaining)
        phrase += suffix
    return phrase


def compare_options_reply(
    name_a: str, name_b: str, entity_a: dict, entity_b: dict, comparison: dict, language: str = "bengali"
) -> str:
    """ "Caller asks the agent to compare two options" story. Renders
    agent/compare_flow.py's build_comparison() output (`comparison`) into
    ONE spoken sentence -- price difference and, only for two health
    packages, the test-count/component difference -- per AC 1 ("stated
    clearly as a single concise sentence") and AC 2 ("ONLY facts").

    `name_a`/`name_b` are the caller's own words for each side (agent/
    llm.py's "compare_option_a"/"compare_option_b" slots, copied
    literally); `entity_a`/`entity_b` are each side's already-resolved
    lookup result from main.py's `_resolve_comparable_entity()`, tagged
    "kind" -- used only to pick a better spoken name via
    _spoken_compare_entity_name() above, same as every other reply
    function in this file reads a `result` dict for display purposes only.

    AC 3 (Strict Clinical Advice Boundary) is not a wording choice made
    here -- it is a fact this function CANNOT violate even if asked to,
    because `comparison` (compare_flow.build_comparison()'s return value)
    has no field capable of expressing a recommendation at all: only
    "cheaper" (an arithmetic fact -- a price is lower or it isn't) and
    "more_tests_side" (a count fact) exist, never a "better" or
    "recommended" key. See agent/compare_flow.py's own module docstring
    for the full reasoning, and agent/llm.py's CLINICAL SAFETY NOTE for
    the upstream half of the same guarantee (the extractor is never asked
    to, and structurally cannot, inject a recommendation via
    direct_reply_bn either -- _validate() strips that field for every
    non-single-smalltalk turn, and compare_options is never smalltalk).
    """
    if not comparison["a_found"] or not comparison["b_found"]:
        return _compare_not_found_reply(name_a, name_b, comparison, language)

    spoken_a = _spoken_compare_entity_name(name_a, entity_a, language)
    spoken_b = _spoken_compare_entity_name(name_b, entity_b, language)
    price_delta = comparison["price_delta"]
    cheaper = comparison["cheaper"]
    rate_a = _digit_faithful_rate(price_delta) if price_delta is not None else None

    parts: list[str] = []
    if language == "english":
        if cheaper == "a":
            parts.append(f"{spoken_a} is {rate_a} rupees cheaper than {spoken_b}")
        elif cheaper == "b":
            parts.append(f"{spoken_b} is {rate_a} rupees cheaper than {spoken_a}")
        elif rate_a is not None:
            parts.append(f"{spoken_a} and {spoken_b} cost the same")
        if comparison["both_packages"]:
            if comparison["identical_tests"]:
                parts.append("both include the same tests")
            elif comparison["more_tests_side"] == "a":
                extra = _capped_extra_tests_phrase(comparison["extra_tests_a"], language)
                base = f"{spoken_a} includes {comparison['test_count_delta']} more test(s) than {spoken_b}"
                parts.append(f"{base}, including {extra}" if extra else base)
            elif comparison["more_tests_side"] == "b":
                extra = _capped_extra_tests_phrase(comparison["extra_tests_b"], language)
                base = f"{spoken_b} includes {comparison['test_count_delta']} more test(s) than {spoken_a}"
                parts.append(f"{base}, including {extra}" if extra else base)
            else:
                parts.append("they include a different set of tests")
        if not parts:
            return f"I don't have enough information to compare {spoken_a} and {spoken_b} right now."
        sentence = ", and ".join(parts)
        return sentence[0].upper() + sentence[1:] + "."

    elif language == "hinglish":
        if cheaper == "a":
            parts.append(f"{spoken_a}, {spoken_b} se {rate_a} rupaye sasta hai")
        elif cheaper == "b":
            parts.append(f"{spoken_b}, {spoken_a} se {rate_a} rupaye sasta hai")
        elif rate_a is not None:
            parts.append(f"{spoken_a} aur {spoken_b} ka price same hai")
        if comparison["both_packages"]:
            if comparison["identical_tests"]:
                parts.append("dono mein same tests shamil hain")
            elif comparison["more_tests_side"] == "a":
                extra = _capped_extra_tests_phrase(comparison["extra_tests_a"], language)
                base = (
                    f"{spoken_a} mein {spoken_b} se {comparison['test_count_delta']} zyada test shamil hain"
                )
                parts.append(f"{base}, jaise {extra}" if extra else base)
            elif comparison["more_tests_side"] == "b":
                extra = _capped_extra_tests_phrase(comparison["extra_tests_b"], language)
                base = (
                    f"{spoken_b} mein {spoken_a} se {comparison['test_count_delta']} zyada test shamil hain"
                )
                parts.append(f"{base}, jaise {extra}" if extra else base)
            else:
                parts.append("dono mein alag-alag tests shamil hain")
        if not parts:
            return f"Abhi {spoken_a} aur {spoken_b} ko compare karne ke liye kaafi jaankari nahi hai."
        return ", aur ".join(parts) + "."

    elif language == "banglish":
        if cheaper == "a":
            parts.append(f"{spoken_a}, {spoken_b}-r cheye {rate_a} taka kom")
        elif cheaper == "b":
            parts.append(f"{spoken_b}, {spoken_a}-r cheye {rate_a} taka kom")
        elif rate_a is not None:
            parts.append(f"{spoken_a} ar {spoken_b}-r price same")
        if comparison["both_packages"]:
            if comparison["identical_tests"]:
                parts.append("duitatei same test ache")
            elif comparison["more_tests_side"] == "a":
                extra = _capped_extra_tests_phrase(comparison["extra_tests_a"], language)
                base = f"{spoken_a}-e {spoken_b}-r cheye {comparison['test_count_delta']} ta beshi test ache"
                parts.append(f"{base}, jemon {extra}" if extra else base)
            elif comparison["more_tests_side"] == "b":
                extra = _capped_extra_tests_phrase(comparison["extra_tests_b"], language)
                base = f"{spoken_b}-e {spoken_a}-r cheye {comparison['test_count_delta']} ta beshi test ache"
                parts.append(f"{base}, jemon {extra}" if extra else base)
            else:
                parts.append("duitate alada alada test ache")
        if not parts:
            return f"Ekhon {spoken_a} ar {spoken_b}-ke compare korar moto tathyo nei."
        return ", ar ".join(parts) + "."

    else:  # bengali
        if cheaper == "a":
            parts.append(f"{spoken_a}, {spoken_b}-এর চেয়ে {rate_a} টাকা সস্তা")
        elif cheaper == "b":
            parts.append(f"{spoken_b}, {spoken_a}-এর চেয়ে {rate_a} টাকা সস্তা")
        elif rate_a is not None:
            parts.append(f"{spoken_a} এবং {spoken_b}-এর দাম একই")
        if comparison["both_packages"]:
            if comparison["identical_tests"]:
                parts.append("দুটোতেই একই টেস্ট আছে")
            elif comparison["more_tests_side"] == "a":
                extra = _capped_extra_tests_phrase(comparison["extra_tests_a"], language)
                base = f"{spoken_a}-এ {spoken_b}-এর চেয়ে {comparison['test_count_delta']}টি বেশি টেস্ট আছে"
                parts.append(f"{base}, যেমন {extra}" if extra else base)
            elif comparison["more_tests_side"] == "b":
                extra = _capped_extra_tests_phrase(comparison["extra_tests_b"], language)
                base = f"{spoken_b}-এ {spoken_a}-এর চেয়ে {comparison['test_count_delta']}টি বেশি টেস্ট আছে"
                parts.append(f"{base}, যেমন {extra}" if extra else base)
            else:
                parts.append("দুটোতে আলাদা আলাদা টেস্ট আছে")
        if not parts:
            return f"এখন {spoken_a} এবং {spoken_b}-এর তুলনা করার মতো তথ্য নেই।"
        return " এবং ".join(parts) + "।"


# ADDED BY SOURAV -- "Caller asks a follow-up that depends on the previous
# answer" story. Spoken per-kind noun for ambiguous_reference_reply()
# below, when a follow-up's pronoun cannot be honestly resolved -- see
# agent/state.py's own module docstring for exactly when this happens
# (two or more DIFFERENT entities of the same kind discussed in one
# turn). Bengali uses the same bare nouns _TEST_FALLBACK/_PACKAGE_FALLBACK
# already use elsewhere in this file for "the test"/"the package" with no
# name at all; a doctor noun is added alongside them here since no
# existing dict already covered it standalone.
_AMBIGUOUS_KIND_NOUN = {
    "test": {"english": "test", "hinglish": "test", "banglish": "test", "bengali": "টেস্ট"},
    "doctor": {"english": "doctor", "hinglish": "doctor", "banglish": "doctor", "bengali": "ডাক্তার"},
    "package": {"english": "package", "hinglish": "package", "banglish": "package", "bengali": "প্যাকেজ"},
}


def ambiguous_reference_reply(kind: str, candidates, language: str = "bengali") -> str:
    """ "Caller asks a follow-up that depends on the previous answer"
    story, Acceptance Criterion 2: when a bare pronoun/elliptical
    follow-up's target entity is AMBIGUOUS (two or more different tests,
    doctors, or packages were discussed a moment ago -- see
    agent/state.py's resolve_follow_up()), the agent asks which one was
    meant instead of guessing. `candidates` is agent/state.py's own
    EntitySlot.names -- already deduplicated and order-preserved by
    mark_ambiguous() -- never re-sorted or re-ordered here, so the
    question lists them in the same order the caller originally named
    them.

    Deliberately distinct from missing_slot_prompt(): a BLANK memory (AC
    2's other half) already gets an honest re-ask from that existing
    function -- this one is only for the "I heard too much, not too
    little" case, which needs its own wording naming what was actually
    ambiguous.
    """
    noun = _AMBIGUOUS_KIND_NOUN.get(kind, _AMBIGUOUS_KIND_NOUN["test"]).get(
        language, _AMBIGUOUS_KIND_NOUN["test"]["english"]
    )
    names = _join_natural(list(candidates), language)
    if language == "english":
        return f"You mentioned more than one {noun} a moment ago -- which one did you mean, {names}?"
    elif language == "hinglish":
        return f"Aapne thodi der pehle ek se zyada {noun} ka naam liya tha -- aap kaunse ke baare mein pooch rahe hain, {names}?"
    elif language == "banglish":
        return f"Ekhon-i apni ekta-r beshi {noun}-er naam bolechen -- apni konta-r kotha bolchen, {names}?"
    else:  # bengali
        return f"আপনি একটু আগে একাধিক {noun}-এর নাম বলেছেন -- কোনটার কথা বলছেন, {names}?"


# ADDED BY SOURAV -- "Caller asks to be called back" story. See
# agent/callback_flow.py's module docstring for the availability-check
# design these three reasons come from -- REASON_DISABLED/
# REASON_OUTSIDE_HOURS/REASON_HOURS_UNKNOWN, mapped 1:1 onto their own
# sentence below rather than a single generic "not available" line, so a
# caller who asks again later hears something that actually matches why
# (a config switch vs. the clock vs. a genuinely unknown state) without
# ever being told a specific reopening time this system cannot honestly
# promise (clinic-api's ClinicInfo table has no "next open" computation
# today -- inventing one here would be exactly the fabrication this
# module's own docstring rules out).
def callback_unavailable_reply(reason: str, language: str = "bengali") -> str:
    """Acceptance Criterion 3: "the agent states plainly that callbacks
    cannot be placed rather than making a false promise." Called instead
    of ever opening a callback-collection pending flow -- see main.py's
    request_callback dispatch branch, which checks
    agent.callback_flow.check_callback_availability() BEFORE asking for a
    time window or phone number at all, precisely so a caller is never led
    through collecting their details only to be refused at the end."""
    if reason == "disabled":
        if language == "english":
            return "I'm sorry, callback requests aren't available for this clinic right now. Please contact our counter directly."
        elif language == "hinglish":
            return "Sorry, abhi callback ki facility available nahi hai. Please hamare counter se seedhe contact kariye."
        elif language == "banglish":
            return "Dukkhito, ekhon callback-er facility available nei. Please seedha amader counter-e jogajog korun."
        else:  # bengali
            return "দুঃখিত, এই মুহূর্তে কল ব্যাক করার সুবিধা নেই। দয়া করে সরাসরি কাউন্টারে যোগাযোগ করুন।"
    if reason == "outside_hours":
        if language == "english":
            return "I'm sorry, we can't schedule a callback outside our clinic hours. Please call us back during our opening hours, or contact our counter."
        elif language == "hinglish":
            return "Sorry, clinic ke hours ke bahar hum callback schedule nahi kar sakte. Hamare opening hours mein phir call kariye, ya counter se contact kariye."
        elif language == "banglish":
            return "Dukkhito, clinic-er hours-er baire amra callback schedule korte pari na. Amader opening hours-e abar call korun, ba counter-e jogajog korun."
        else:  # bengali
            return "দুঃখিত, ক্লিনিকের সময়সূচির বাইরে কল ব্যাক নির্ধারণ করা যাবে না। আমাদের খোলার সময়ে আবার কল করুন, অথবা কাউন্টারে যোগাযোগ করুন।"
    # reason == "hours_unknown", or any other value -- an honest "cannot
    # confirm right now", same posture as clinic_info_reply()'s own
    # found=false branch just above it in this file, never a guess either
    # way about whether a callback could actually be placed.
    if language == "english":
        return "I'm sorry, I can't confirm right now whether callbacks are available. Please try again shortly, or contact our counter."
    elif language == "hinglish":
        return "Sorry, abhi confirm nahi kar pa raha ki callback available hai ya nahi. Thodi der baad try kariye, ya counter se contact kariye."
    elif language == "banglish":
        return "Dukkhito, ekhon confirm korte parchi na callback available kina. Ektu pore abar try korun, ba counter-e jogajog korun."
    else:  # bengali
        return "দুঃখিত, এই মুহূর্তে কল ব্যাক পাওয়া যাবে কিনা নিশ্চিত করে বলতে পারছি না। একটু পরে আবার চেষ্টা করুন, অথবা কাউন্টারে যোগাযোগ করুন।"


def callback_confirmation_prompt(slots: dict, language: str = "bengali") -> str:
    """Every critical value is read back before it is used (Answer Quality
    and Grounding, same discipline as booking_confirmation_prompt() above):
    spoken once both the time window and phone number are known, BEFORE
    main.py ever calls request_callback(). A misheard phone digit is
    caught here, not after the write."""
    window = slots.get("callback_time_window") or ""
    phone = slots.get("phone") or ""
    if language == "english":
        return f"Just to confirm, we'll have someone call you back at {phone} during {window}. Is that right?"
    elif language == "hinglish":
        return f"Confirm karne ke liye, hum aapko {phone} par {window} ke dauraan call karenge. Sahi hai?"
    elif language == "banglish":
        return f"Confirm korar jonno, amra apnake {phone} number-e {window}-r modhye call korbo. Thik ache?"
    else:  # bengali
        return f"একটু কনফার্ম করে নিই, আমরা আপনাকে {phone} নম্বরে {window}-এর মধ্যে কল ব্যাক করব। ঠিক আছে তো?"


def callback_scheduled_reply(slots: dict, result: dict, language: str = "bengali") -> str:
    """Spoken after request_callback() actually persists the row (main.py's
    _finish_callback(), only once missing_callback_write_fields() has
    confirmed `callback_id` really came back non-empty -- see
    agent/outcomes.py). Deliberately never claims the SYSTEM will call
    back -- "No outbound capability" is this whole story's own evidence --
    only that the request has been noted for a human to act on."""
    if result.get("success"):
        window = slots.get("callback_time_window") or result.get("time_window") or ""
        callback_id = result["callback_id"]
        if language == "english":
            return f"I've noted your callback request for {window}. Your reference number is {callback_id}."
        elif language == "hinglish":
            return (
                f"Aapka callback request {window} ke liye note kar liya hai. "
                f"Aapka reference number hai {callback_id}."
            )
        elif language == "banglish":
            return (
                f"Apnar callback request {window}-r jonno note kore niyechi. "
                f"Apnar reference number holo {callback_id}."
            )
        else:  # bengali
            return (
                f"আপনার কল ব্যাকের অনুরোধ {window}-এর জন্য নথিভুক্ত করা হয়েছে। আপনার রেফারেন্স নম্বর হলো {callback_id}।"
            )

    if language == "english":
        return "Sorry, I couldn't note down your callback request. Please try again later, or contact our counter."
    elif language == "hinglish":
        return "Sorry, aapka callback request note nahi kar paya. Thodi der baad phir try karein, ya counter se contact karein."
    elif language == "banglish":
        return "Dukkhito, apnar callback request note korte parlam na. Ektu pore abar try korun, ba counter-e jogajog korun."
    else:  # bengali
        return "দুঃখিত, আপনার কল ব্যাকের অনুরোধ নথিভুক্ত করা গেল না। একটু পরে আবার চেষ্টা করুন, অথবা কাউন্টারে যোগাযোগ করুন।"


# NOTE (merge dev_chakravardhan -> staging_merged): dev_chakravardhan's side
# of this conflict opened with its own i18n-based tail of
# doctors_by_department_reply(), duplicating the function already resolved
# above (see its HEAD-based ending just before the "ADDED BY SOURAV" report-
# status block) -- discarded here for the same reason as the earlier
# doctors_by_department_reply conflict. Everything from payment_reply()
# onward below is genuinely new, additive functionality with no HEAD
# equivalent (confirmed by diffing every `def`/constant name on each side of
# this conflict) and is kept in full, using its original
# agent.i18n.t()-based implementation -- see the _lang()/imports note near
# the top of this file for why that is safe here specifically.
# ===========================================================================
# The two flows that exist because of "every flow completes without a
# smartphone".
# ===========================================================================
def payment_reply(slots: dict, result: dict, lang: str | None = None) -> str:
    """How the caller pays -- and it is never through a link.

    Before this existed, a caller who asked "কত টাকা লাগবে, কীভাবে দেব?"
    got a price and nothing about HOW. That is a flow with no completion
    path: the caller knows the number and still does not know what to do
    next. The obvious modern answer -- text them a payment link -- is
    precisely what this story forbids, and it would exclude every caller on
    a feature phone, which on this line is a large share of them.

    So the answer is the counter, stated first and stated as normal.
    `result` is optional context from a test-rate lookup; the answer is
    complete without it, because a caller who has not named a test still
    deserves to know how payment works.
    """
    code = _lang(lang)
    reply = t(code, "payment.how")

    result = result or {}
    if result.get("found") and result.get("rate_inr"):
        reply += t(
            code, "payment.amount", name=_spoken_test_name(slots, result, code), rate=result["rate_inr"]
        )

    reply += t(code, "payment.no_advance")
    reply += t(code, "payment.counter_only")
    return reply


def report_collection_reply(slots: dict, result: dict, lang: str | None = None) -> str:
    """When the report is ready and how to get it, with no portal involved.

    Three completion paths, in the order a caller can actually use them:

      1. collect a printed copy at the counter, identified by name and
         phone -- deliberately NOT by the reference number, so a caller who
         lost it is not turned away;
      2. ring in and have it read out, for someone who cannot travel;
      3. send somebody else, who needs only the patient's name and number.

    None of the three needs a smartphone, an app, or a link. The report
    hours come from the catalogue when the caller named a test, and the
    answer stays useful when they did not.
    """
    code = _lang(lang)
    result = result or {}

    hours = result.get("report_time_hours")
    reply = t(code, "report.when", hours=hours) if hours else t(code, "report.when_unknown")

    reply += t(code, "report.collect")
    reply += t(code, "report.phone_readout")
    reply += t(code, "report.someone_else")
    return reply


def counter_fallback(lang: str | None = None, hours: str | None = None) -> str:
    """The universal completion path, for any turn that cannot finish on
    the phone.

    Exists so no branch anywhere ends with the caller holding nothing. A
    "sorry, I can't do that" with no next step is exactly the dead end the
    story names, even when no smartphone was ever mentioned.
    """
    code = _lang(lang)
    reply = t(code, "counter.walk_in")
    if hours:
        reply = t(code, "counter.hours", hours=hours) + " " + reply
    return reply


# ===========================================================================
# Language handling
# ===========================================================================
def language_switch_reply(lang: str | None = None) -> str:
    """Spoken IN THE NEW LANGUAGE, which is the point -- it is the caller's
    proof that the switch actually took."""
    return t(_lang(lang), "language.switched")


def language_unavailable_reply(current_lang: str | None = None) -> str:
    """The caller asked for a language this pod cannot serve.

    Answered in the language they are currently being understood in, and it
    names what IS available. Silently ignoring the request reads as the
    system not having heard them, and they ask again -- burning a turn and
    their patience on a line that will never say yes.
    """
    code = _lang(current_lang)
    return t(code, "language.unavailable", available=available_languages_phrase(code))


# ===========================================================================
# PATIENT HISTORY -- disclosed only after verification
# Author: Chakravardhan
# ===========================================================================
# THE WORDING IS PART OF THE SECURITY HERE, more than anywhere else in this
# file. Everything below is built so that a caller cannot learn anything from
# the SHAPE of a refusal: the sentence for a wrong PIN, an unknown number and
# a patient with no factor on file is one and the same sentence.
# What a verification was started FOR. Carried in session.pending so the
# right thing is read out once the caller is verified, and so the challenge
# names what it is about to unlock.
PURPOSE_HISTORY = "history"
PURPOSE_BOOKINGS = "bookings"


def verification_prompt(factor: str, lang: str | None = None, purpose: str = PURPOSE_HISTORY) -> str:
    """Ask for the proof. Names WHICH kind, never anything about the answer.

    A caller who genuinely set a PIN at the counter needs to be told it is
    the PIN we want; that is not a hint, it is the question. What must never
    appear is how many digits matched, how many attempts remain, or whether
    this number is known to the clinic at all.

    `purpose` only changes what the sentence says it is FOR ("your history"
    or "your bookings"). The default keeps the original sentence exactly.
    """
    code = _lang(lang)
    prefix = "timeline" if purpose == PURPOSE_BOOKINGS else "history"
    return t(code, f"{prefix}.ask_pin" if factor == "pin" else f"{prefix}.ask_dob")


def verification_failed_reply(exhausted: bool, lang: str | None = None) -> str:
    """One sentence for every kind of failure.

    `exhausted` switches between "try again" and "go to the counter" -- it
    is about whether another attempt is POSSIBLE, not about why this one
    failed. The caller learns nothing from it that helps them guess.

    The counter sentence deliberately says nothing needs bringing and a name
    is enough: a patient who cannot get past verification is exactly the
    patient least likely to be holding a reference number, and sending them
    away with a requirement they cannot meet is the dead end the
    no-smartphone story already ruled out.
    """
    code = _lang(lang)
    return t(code, "history.failed" if exhausted else "history.retry")


def verification_locked_reply(lang: str | None = None) -> str:
    """Says the number is paused. Does NOT say for how long, or how many
    attempts caused it -- both are useful only to somebody guessing."""
    return t(_lang(lang), "history.locked")


def disclosure_blocked_reply(reason: str, lang: str | None = None) -> str:
    """Refused because of the ROOM, not the caller.

    Distinguishing this from a verification failure is the one place extra
    detail is SAFE and necessary: a verified patient standing next to their
    family needs to know the fix is to pick the phone up, not that they
    failed to prove who they are. Saying "that did not match" here would
    send an honest caller round a loop they cannot get out of.
    """
    code = _lang(lang)
    if reason == "disclosure_disabled":
        return t(code, "history.disclosure_off")
    if reason == "text_channel":
        # Refused because it is a MESSAGE -- see privacy.channel_is_private().
        # "Pick the phone up" would be the wrong fix; calling is the right one.
        return t(code, "channel.private_by_message")
    return t(code, "history.speakerphone")


# How many tests are read aloud before the rest are deferred to the counter.
# Three, not all of them: a spoken list stops being usable past about three
# items, and every additional sentence is more time during which somebody can
# walk into the room. Minimum disclosure is a privacy property, not just a UX
# one.
HISTORY_SPOKEN_LIMIT = 3


def history_reply(result: dict, lang: str | None = None) -> str:
    """The history itself, kept as short as it can usefully be.

    NAMES, DATES AND WHETHER A REPORT IS READY. Never results, never values,
    never a diagnosis -- clinic-api does not return them (see
    history_service.history()) and this function could not speak them if it
    tried. The counter is named for detail, which is also the path that needs
    no smartphone.
    """
    code = _lang(lang)
    tests = (result or {}).get("tests") or []
    if not tests:
        return t(code, "history.none")

    reply = t(code, "history.intro", count=len(tests))
    for item in tests[:HISTORY_SPOKEN_LIMIT]:
        name = (
            (item.get("test_name_bn") if code != lang_mod.EN else None)
            or item.get("test_name")
            or t(code, "word.test")
        )
        key = "history.item_ready" if item.get("report_ready") else "history.item_pending"
        reply += t(code, key, name=name, date=item.get("taken_on", ""))

    remaining = len(tests) - HISTORY_SPOKEN_LIMIT
    if remaining > 0:
        reply += t(code, "history.more", count=remaining)

    reply += t(code, "history.detail_at_counter")
    return reply


# ===========================================================================
# A SINGLE PATIENT TIMELINE -- Author: Chakravardhan
# ===========================================================================
# Story: "As a patient, I want the agent to already know what I have booked
# here, so that I am not made to recite my own history to the hospital that
# holds it."

# How many upcoming bookings are read aloud before the rest go to the
# counter. Three, for the same two reasons as HISTORY_SPOKEN_LIMIT: a spoken
# list stops being usable past about three, and every extra sentence is more
# time for somebody to walk into the room.
BOOKINGS_SPOKEN_LIMIT = 3


def bookings_reply(result: dict, lang: str | None = None) -> str:
    """What the patient has booked, from the timeline clinic-api returned.

    EVERY FACT COMES FROM `upcoming_appointments` -- doctor, date and time as
    clinic-api holds them, soonest first. Cancelled and past bookings are not
    in that list, so they cannot be read out as if they were still on.

    NO CONFIRMATION NUMBER IS SPOKEN. The story is that the patient should
    not need one to be answered; reading it out would hand the next person
    to hold this phone the one thing that identifies the booking.
    """
    code = _lang(lang)
    upcoming = (result or {}).get("upcoming_appointments") or []
    if not upcoming:
        return t(code, "timeline.no_bookings")

    reply = t(code, "timeline.intro", count=len(upcoming))
    for item in upcoming[:BOOKINGS_SPOKEN_LIMIT]:
        reply += t(
            code,
            "timeline.item",
            doctor=_spoken_doctor_name({}, item, code),
            date=item.get("date") or "",
            time=item.get("time_slot") or "",
        )

    remaining = len(upcoming) - BOOKINGS_SPOKEN_LIMIT
    if remaining > 0:
        reply += t(code, "timeline.more", count=remaining)
    return reply
