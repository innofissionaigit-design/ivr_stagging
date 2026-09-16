"""Kolkata Care Diagnostics -- voice agent on the RAW PCM transport.

GENERATED FILE -- do not edit directly. Produced by
tools/make_pcm_variant.py from main.py; edit main.py and re-run that.

This variant changes only the TRANSPORT (how audio arrives and how the
turn detector reads it):
  * No ffmpeg, no WebM, no temp audio files, no subprocess per poll.
  * Appending is O(chunk) and reading the tail is O(tail), where the WebM
    path was O(call length) EVERY poll -- O(T^2) per call.
  * The sample index IS the timeline, exactly, so processed_until_s
    cannot drift away from real time.
See agent/pcm_buffer.py for the argument and the arithmetic.

----------------------------------------------------------------------


Turn loop, once a caller's utterance is judged complete (agent/vad_stream.py):

  utterance WAV -> ASR (agent/asr.py, IndicConformer)
                -> intent+slots (agent/llm.py, Ollama JSON-mode,
                   fronted by agent/semantic_cache.py)
                -> Spring Boot lookup (agent/tools_client.py) -- ALWAYS
                   live, never cached; see semantic_cache.py's docstring
                -> reply text, TEMPLATED from the API response, never
                   restated by the model (agent/reply_templates.py)
                -> TTS (agent/tts.py) -> WAV bytes back over the socket

Every stage has a named failure path (see _dispatch_turn) so a caller never
gets dead air: ASR-empty, LLM-failure, tool-failure and TTS-failure each
speak a distinct, pre-recorded apology rather than the process hanging or
the socket just going quiet. See README.md "Error handling" for the full
table and the reasoning behind each choice.

ECHO GATE AND BARGE-IN
----------------------
The mic is open for the entire call, and the agent's replies play out of
the caller's speaker. With no gate, the agent hears itself: its own
greeting lands in the same buffer the turn detector is watching, so VAD
fires a "the caller finished talking" on the agent's own voice, ASR
transcribes the agent, and processed_until_s advances past audio the
caller never produced. That is a self-sustaining loop, and it is what
made real calls cut the caller off in the first second and then run a
turn behind for the rest of the call.

This USED to be solved by going half-duplex: the client muted the mic
track while agent audio played, and the server refused to run turn
detection at all while `agent_speaking`. It worked, and it cost barge-in
entirely -- a caller could not interrupt the agent mid-sentence. For a
caller on a speakerphone, who is not holding a handset but talking across
a room at it, that is the single thing they most need.

The mute is gone. What replaces it is arbitration, in agent/echo_guard.py:
the server keeps the audio it just played as a REFERENCE signal, and every
window of microphone audio captured during playback is correlated against
it. Our own voice returning is a delayed, attenuated copy of something we
still hold; the caller's voice is not. So:

  * echo            -> stays gated, exactly as before;
  * real speech     -> stops playback (`_stop_audio`) and opens the gate;
  * anything unsure -> treated as echo, because a false barge-in truncates
                       a reply the caller then never hears.

Note the original objection to relying on the browser alone still stands:
its echoCancellation is built around a remote WebRTC peer's rendered
stream, and this audio is synthesized locally and played through Web
Audio, so how much of it the canceller sees as a far-end reference varies
by browser and platform. That is precisely why the arbitration above is
server-side and reference-based: it does not depend on the browser's AEC
having worked.

`barge_in()` deliberately does NOT go through `release_gate()`, because
the resync that follows release_gate discards everything captured during
playback -- which during a barge-in is the interruption itself.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime
import difflib
import hmac
import io
import json
import logging
import os
import tempfile
import time
import uuid
import wave

import torchaudio
from agent.pcm_buffer import PcmCallBuffer, SAMPLE_RATE
from fastapi import FastAPI, Header, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from agent import answer_ledger
from agent import turn_parts
from agent.turn_parts import ANSWERED, INTERACTIVE, UNANSWERABLE
from agent import call_state as call_state_mod
from agent import confidence, outcomes, speakability, tool_outcome, turn_log
from agent.asr import TurnASR
# ADDED BY SOURAV -- real production bug, reported directly by the
# caller: "why voice is giving response only in bengali... when the user
# asks in hindi aur hinglish or english." detect_language() already
# existed but was NEVER CALLED anywhere in this repo -- every reply
# function below already accepts a `language` argument (all default to
# "bengali"), but nothing ever passed anything else. See detect_language()
# below wherever it's called for the full writeup, and its own updated
# docstring in agent/bn_normalize.py for a second, real bug found and
# fixed in the function itself while wiring this in.
from agent.bn_normalize import detect_language
from agent.llm import extract_intent, ExtractionError
# ADDED BY CHAKRAVARDHAN -- audio-quality conditioning, echo suppression,
# the shared HTTP executor pool, patient verification/history disclosure,
# reschedule/cancel notifications and per-caller language switching.
from agent.audio_quality import condition_wav_file
from agent.echo_guard import (
    CONFIG as ECHO_CFG, EchoGuard, pcm_from_wav_bytes, suppress_echo,
)
from agent.executors import asr_gate, run_http, shutdown as _shutdown_http_pool
from agent import language as lang_mod
from agent import asr as asr_mod
from agent.i18n import t as _t
from agent import privacy
from agent.quality_metrics import ACTION_KEYPAD, METRICS, TurnFailureTracker
from agent.reply_templates import (
    missing_slot_prompt, test_rate_reply, sample_type_reply, test_duration_reply,
    doctor_availability_reply, booking_reply,
    booking_confirm_prompt, heard_confirm_prompt, doctors_by_department_reply,
    booking_correction_prompt, INSUFFICIENT_VERIFIED_INFORMATION_BN,
    date_range_confirm_prompt, UNSPEAKABLE_ESCALATION, with_change_notice,
    near_match_prompt, NEAR_MATCH_UNCLEAR_BN,
    DEFERRED_PART_BN, RESUMING_PART_BN, unanswered_part_prompt,
    # ADDED BY SOURAV -- "Caller asks when a doctor sits" story.
    doctor_schedule_reply, booking_confirmation_prompt,
    # ADDED BY SOURAV -- "Lab Report Status & Secure Delivery" combined story.
    # delivery_declined_reply / otp_disclosure_refusal_reply are the two new
    # reply functions _dispatch_turn/_continue_pending speak directly
    # (every other new reply function is only ever reached indirectly,
    # through agent/report_flow.py's interpret_*() functions -- see that
    # module for why the decision logic itself lives there and not here).
    delivery_declined_reply, otp_disclosure_refusal_reply,
    # ADDED BY SOURAV -- "Caller asks about a health package" combined
    # with "Caller asks opening hours, address or directions".
    health_package_reply, health_packages_list_reply, clinic_info_reply,
    # ADDED BY SOURAV -- "Caller asks how to prepare for a test" story,
    # plus its bundled human_fallback config (see human_fallback_reply's
    # own module-level comment in agent/reply_templates.py for why that
    # part lives here rather than as an actual call transfer).
    test_preparation_reply, human_fallback_reply,
    # ADDED BY SOURAV -- Phase 1: Database Schema & Policy Tables (Walk-in
    # Eligibility, Prescription Requirements, Insurance Coverage Policy,
    # Outstanding Balance / Billing stories).
    walkin_eligibility_reply, prescription_requirements_reply,
    insurance_coverage_reply, billing_balance_reply,
    # ADDED BY SOURAV -- "Caller asks something the agent does not cover"
    # story. human_fallback_reply above is reused verbatim for the
    # "connect me to a human" branch -- these two are only the new
    # initial-offer and declined-offer replies.
    out_of_scope_reply, out_of_scope_counter_reply,
    # ADDED BY SOURAV -- "Caller asks two questions in one breath" story.
    # These three back _resolve_combinable_intent_fragment()'s three
    # non-fabricating fallback fragments below -- every OTHER fragment in
    # a combined reply reuses an existing single-question reply function
    # from this same import block verbatim.
    multi_intent_missing_info_reply, multi_intent_out_of_scope_reply,
    multi_intent_needs_separate_flow_reply,
    # ADDED BY SOURAV -- "Caller asks the agent to compare two options"
    # story. Renders agent/compare_flow.py's build_comparison() output --
    # see that module and this function's own docstring for the
    # arithmetic/clinical-safety design.
    compare_options_reply,
    # ADDED BY SOURAV -- "Caller asks a follow-up that depends on the
    # previous answer" story. See agent/state.py's own module docstring
    # and this function's docstring for when this is spoken.
    ambiguous_reference_reply,
    # ADDED BY SOURAV -- "Caller asks to be called back" story. See
    # agent/callback_flow.py's module docstring and each function's own
    # docstring for when these are spoken.
    callback_unavailable_reply, callback_confirmation_prompt, callback_scheduled_reply,
    # ADDED BY CHAKRAVARDHAN -- payment/report-collection no-smartphone
    # flows, patient verification and history/bookings disclosure, and
    # per-caller language switching.
    payment_reply, report_collection_reply,
    counter_fallback, language_switch_reply, language_unavailable_reply,
    verification_prompt, verification_failed_reply, verification_locked_reply,
    disclosure_blocked_reply, history_reply,
    bookings_reply, PURPOSE_BOOKINGS, PURPOSE_HISTORY,
)
from agent.compare_flow import build_comparison
# ADDED BY SOURAV -- "Caller asks a follow-up that depends on the previous
# answer" story. Cross-turn entity memory (pronoun/elliptical follow-up
# resolution) -- see agent/state.py's own module docstring for the full
# design and exactly which intents this applies to.
from agent.state import DialogueState, resolve_follow_up, primary_slot_for_intent, kind_for_slot
from agent.fast_path import Catalogue, FastPath, COMMIT_MARGIN
from agent.semantic_cache import SemanticCache, embed as _embed_probe
# story title: The model never originates a fact
# user story: As a clinical lead, I want every price, date and identifier
#   to come from a verified system response, so that a wrong answer is a
#   data bug rather than a model bug.
# acceptance criteria: Every factual sentence is a template substitution
#   from a validated tool response and the model is never shown a figure
#   it could restate. An automated assertion on every commit proves no
#   model-composed span reaches synthesis on a factual intent.
#
# resolve_date is the story's core: the model's date becomes a candidate
# and slot_parse.py becomes the record. See its docstring.
from agent.slot_parse import (
    parse_date, parse_time, parse_phone, is_affirmative, is_negative,
    parse_correction_field,
    # ADDED BY SOURAV -- report_status/report_send combined story: OTP entry
    # is parsed deterministically here, never sent to the LLM or the
    # semantic cache (see agent/slot_parse.py's parse_otp() docstring and
    # RULE 9 -- the OTP must never appear in an Ollama prompt or a cache key).
    parse_otp, looks_like_otp_disclosure_request,
)
# story title: The model never originates a fact
# user story: As a clinical lead, I want every price, date and identifier to
#   come from a verified system response, so that a wrong answer is a data bug
#   rather than a model bug.
# acceptance criteria: Every factual sentence is a template substitution from a
#   validated tool response and the model is never shown a figure it could
#   restate. An automated assertion on every commit proves no model-composed
#   span reaches synthesis on a factual intent.
#
# date_calc owns the calendar. The model names what the caller MEANT
# ("next_week"); every digit is computed here. See that module's docstring for
# the division of authority and for why an expression, unlike an ISO date,
# cannot go stale in the semantic cache.
from agent import date_calc
from agent.date_calc import SOURCE_INTERPRETED, SOURCE_PARSED
from agent.tools_client import ClinicToolsClient, ToolCallError
from agent.outcomes import (
    missing_booking_write_fields,
    record_insufficient_verified_information,
    # ADDED BY SOURAV -- "Caller asks how to prepare for a test" story's
    # bundled human_fallback config -- see record_human_handoff()'s own
    # docstring in agent/outcomes.py.
    record_human_handoff,
    # ADDED BY SOURAV -- "Caller asks to be called back" story. Mirrors
    # missing_booking_write_fields() exactly -- see that function's own
    # docstring in agent/outcomes.py.
    missing_callback_write_fields,
)
# ADDED BY SOURAV -- "Caller asks to be called back" story. Pure
# availability-check/context-building logic -- see that module's own
# docstring for why "operating hours" means the clinic's own hours and why
# nothing here reads os.environ or datetime directly.
from agent.callback_flow import check_callback_availability, build_callback_reason
from agent.callback_config import CALLBACKS_ENABLED
# ADDED BY SOURAV -- new shared module holding the actual report-flow
# DECISIONS as pure functions, so main.py and main_pcm.py both get
# identical business logic for report_status/report_send without
# hand-duplicating the branching a second time. See agent/report_flow.py's
# module docstring for the full reasoning (it also explains the
# pre-existing test_sample drift this same story restores parity on, just
# below).
from agent.report_flow import (
    interpret_report_status_result, interpret_delivery_request_result,
    interpret_otp_verify_result, match_candidate_report,
)
# STORY [Answer Quality and Grounding]
# As a patient, I want to hear the whole sentence, so that I am
# not left guessing what the agent tried to say.
from agent import tts as tts_mod
# ADDED BY CHAKRAVARDHAN -- BUSY_LINE (the fixed clip played on _speak()'s
# already-busy-line fallback path) merged onto Sourav's existing TTSClient
# import rather than kept in dev_chakravardhan's separate, narrower import.
from agent.tts import TTSClient, UnspeakableReply, SPEAKABILITY_ENFORCE, BUSY_LINE
from agent.vad_stream import TurnDetector
from agent import call_audit
from agent import conversation_store
# MIXED-LANGUAGE SPEECH -- Author: Chakravardhan. See agent/code_mix.py.
from agent import code_mix
# GREETING AND CLOSING -- Author: Chakravardhan. See agent/call_script.py.
from agent import call_script

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("main")

POLL_INTERVAL_S = 0.5
IDLE_TIMEOUT_S = 90.0
UTTERANCE_PAD_S = 0.15  # small trailing pad so ASR doesn't clip the last phoneme

# A live call was observed closing itself ~26s after the last exchange --
# far short of IDLE_TIMEOUT_S, which only fires at 90s. That gap points to
# an intermediate proxy (RunPod's or an nginx in front of it) closing
# WebSocket connections that go quiet for a while, independent of this
# app's own idle logic. A small periodic heartbeat keeps real traffic
# flowing on the socket so no proxy in between decides it's abandoned.
HEARTBEAT_INTERVAL_S = 15.0

# Backstop for the half-duplex gate. Normally the client reports playback
# finished and the gate lifts immediately; this only fires when that
# message never arrives (JS error, stale cached page, a client that
# predates the control channel). Generous on purpose -- lifting the gate
# early puts the agent back to hearing itself, which is the bug.
PLAYBACK_GUARD_S = 3.0

# On resync, rewind slightly before the buffer's decoded end. The region
# being skipped is muted silence, so rewinding into it costs nothing,
# while NOT rewinding risks clipping the caller's first syllable if the
# WebM decode is running a beat behind real time.
RESYNC_REWIND_S = 0.25

# Is reading the recent microphone tail cheap on THIS transport?
#
# False here, and the generator flips it to True in main_pcm.py. It decides
# whether barge-in may poll at ECHO_CFG.barge_in_poll_s (0.10s) or has to stay
# on the ordinary POLL_INTERVAL_S (0.5s).
#
# On the WebM transport every tail read goes through _decode_to_wav, which
# re-decodes the WHOLE growing buffer from byte 0 and spawns an ffmpeg process
# to do it -- the O(T^2) behaviour agent/pcm_buffer.py exists to remove.
# Polling that ten times a second during every reply would mean ~10 ffmpeg
# spawns per second per speaking call, and at MAX_CONCURRENT_CALLS it
# saturates CPU long before the GPU is busy. On raw PCM the same read is a
# slice of a bytearray, so the fast cadence costs essentially nothing.
#
# CONSEQUENCE, stated plainly: barge-in can only meet barge_in_target_s on
# the PCM transport. On WebM it still works, but detection takes up to
# POLL_INTERVAL_S. WebM is the legacy/bench client; PCM is what production
# serves.
TAIL_READ_IS_CHEAP = True   # raw PCM: reading the tail is a slice, not a decode

# Which process a call record came from. main.py and main_pcm.py share one
# audit database (agent/call_audit.py), and crash recovery at startup must
# only ever close the records of ITS OWN transport. The generator flips it.
AUDIT_TRANSPORT = "pcm"

# THE RETRY LADDER, in words.
#
# Two rungs, because a caller who cannot be heard needs a different answer
# the second time than the first. Asking the same question twice in a row
# in the same market is not a retry, it is a loop -- the room has not got
# quieter between the two attempts, so nothing about repeating the request
# makes the next clip any better.
#
# Rung 1 names the actual problem (noise) and asks for the one thing the
# caller CAN change (volume, distance to the phone). Rung 2 stops asking
# for speech at all and moves to a channel the room cannot corrupt.
CLARIFY_PROMPT_BN = "দুঃখিত, আশেপাশে খুব আওয়াজ হচ্ছে। আর একটু জোরে, ফোনের কাছে এসে বলবেন?"
KEYPAD_PROMPT_BN = (
    "এখনও পরিষ্কার শোনা যাচ্ছে না। কী-প্যাড ব্যবহার করুন — "
    "পরীক্ষার রেটের জন্য ১, ডাক্তারের সময়ের জন্য ২, "
    "অ্যাপয়েন্টমেন্টের জন্য ৩ টিপুন।"
)

# What each key means, as the Bengali the caller would have spoken. Mapping
# to TEXT rather than to an intent id is deliberate: the digit then enters
# the SAME reasoning path as speech -- fast path, cache, LLM, slot filling,
# _continue_pending -- instead of needing a second, parallel dispatcher that
# would drift out of step with the spoken one.
KEYPAD_MENU_BN = {
    "1": "পরীক্ষার রেট জানতে চাই",
    "2": "ডাক্তারের সময় জানতে চাই",
    "3": "অ্যাপয়েন্টমেন্ট বুক করতে চাই",
}

CLINIC_API_BASE = os.environ.get("CLINIC_API_BASE", "http://localhost:8080")

# ADMISSION CONTROL -- the ceiling on simultaneous calls in this process.
#
# Every other limit added for peak hour (asr_gate, the TTS gate, the HTTP
# pool, Ollama's own queue) bounds ONE stage. None of them bounds how many
# callers are admitted in the first place, so without this the system's
# answer to overload is to accept everybody and let every caller degrade
# together -- longer ASR queues, longer Ollama queues, longer TTS queues, for
# all of them at once.
#
# That is the exact outcome the peak-hour requirement rules out. Quality
# stops being a function of when you ring only if the system is willing to
# say "not right now" to the caller who would push it past what it can serve
# well. A caller told plainly that the lines are busy can ring back in a
# minute; a caller silently placed in a queue that degrades everyone gets a
# worse experience AND makes it worse for the people already on the line.
#
# 12 is a starting point, not a measurement -- it is deliberately env-tunable
# so it can be set from a real load test (tools/bench_transport.py) rather
# than from this guess. Raise it once the stages behind it are known to keep
# up; lower it the moment they do not.
MAX_CONCURRENT_CALLS = int(os.environ.get("VOICE_AGENT_MAX_CALLS", "12"))

# Plain int, no lock: FastAPI runs one event loop, and every read/modify pair
# below is free of awaits, so it cannot interleave.
_active_calls = 0

# Order also doubles as PRIORITY: the field _next_missing() asks for next
# when several are still empty. doctor_name first because it is almost
# always already known by the time booking starts (named directly, or
# carried over from session.pending after a doctors_by_department /
# doctor_availability turn -- see _continue_pending below).
_BOOKING_FIELDS = ("doctor_name", "date", "time_slot", "patient_name", "phone")

# story title: A thing not existing is never confused with a system being down
# user story: As a caller, I want to know whether my test does not exist or the
#   system cannot be reached, so that I know whether to call back.
# acceptance criteria: The two produce different spoken sentences and different
#   metrics, and the distinction survives every refactor. This behaviour exists
#   today and gains a permanent regression case.
#
# THE SYSTEM-IS-DOWN SENTENCE, and the only one. It was written out at all
# seven ToolCallError handlers, which is seven chances for one of them to drift
# into a different wording -- or, worse, into a not-found wording -- during a
# refactor nobody reviewed carefully. A caller must be able to tell "your test
# does not exist" from "I could not reach the system" without knowing which
# code path they happened to hit, because the two carry opposite instructions:
# one means stop asking, the other means call back.
#
# The not-found sentences deliberately stay in agent/reply_templates.py, where
# every other sentence that NAMES something lives. The separation is the point:
# this file says what the system could not do, that file says what the clinic
# said. tests/test_outcome_distinction.py asserts the two sets never overlap.
SYSTEM_UNREACHABLE_BN = "এই মুহূর্তে দেখতে পারছি না। কাউন্টারে যোগাযোগ করুন, দয়া করে।"

# story title: Every critical value is read back before it is used
# user story: As a patient giving a phone number, I want it read back, so that
#   a misheard digit does not send my report to a stranger.
# acceptance criteria: Phone numbers, dates, times and names are confirmed
#   aloud before any write, and a rejection opens a correction path rather than
#   repeating the prompt. Readback is mandatory regardless of confidence for
#   values that affect a write.
#
# Said when the repair ladder runs out -- from confirm_booking, and now also
# from confirm_correction. Named once for the same reason SYSTEM_UNREACHABLE_BN
# is: two copies of a sentence are one refactor away from two different
# sentences, and this one carries a promise -- that nothing was written -- which
# must not drift.
BOOKING_NOT_CONFIRMED_BN = "এখনো কনফার্ম করতে পারলাম না। অ্যাপয়েন্টমেন্টটা করা হয়নি — কাউন্টারে একবার কথা বলে নেবেন।"

# Turns that died on an exception nobody expected. Counted because a metric
# that only tallies TIDY failures reads healthy during exactly the incident it
# exists for. Surfaced at /api/stats beside the per-tool outcomes.
_turn_crashes = 0

# story title: The same question gets the same answer within one call
# user story: As a caller who asks twice, I want the same answer, so that I
#   know which one to believe.
# acceptance criteria: Repeating a question in one call produces an identical
#   factual answer unless the underlying data changed, in which case the
#   change is stated. A test asserts consistency across three repeats with an
#   unchanged backend.
#
# Each AnswerLedger lives and dies with its call, which is the right scope for
# the behaviour and the wrong one for a metric -- nothing would ever read it.
# This is the process-level roll-up, accumulated as each call's verdicts come
# in, on the same argument as _turn_crashes above.
_consistency = {"repeats": 0, answer_ledger.SAME: 0, answer_ledger.CHANGED: 0}

# ADDED BY SOURAV -- "Caller asks to be called back" story. Deliberately
# its OWN tuple/helper, not a repurposing of _BOOKING_FIELDS/_next_missing
# above -- those two are hard-wired to each other (see _next_missing's own
# docstring) and to book_appointment's specific 5-field shape; a request-
# callback flow needs only two fields, and giving it a separate helper
# keeps this story's blast radius off the booking flow's already-tested
# behaviour entirely. Values here are the SLOT keys ("phone" -- the same
# slot key booking/report flows already use), not the scoped `awaiting`
# strings _continue_pending uses for its pending state (those are
# "callback_time_window" and "callback_phone" -- see that function's own
# comment on why "phone" alone would collide with the booking flow's
# existing "phone" awaiting state).
_CALLBACK_FIELDS = ("callback_time_window", "phone")

app = FastAPI()

# ---- process-wide singletons: loaded once, shared by every call ----
_asr: TurnASR | None = None
_turn_detector: TurnDetector | None = None
_tools: ClinicToolsClient | None = None
_tts: TTSClient | None = None
_intent_cache: SemanticCache | None = None
_fast_path: FastPath | None = None
_audit_store: call_audit.AuditStore | None = None
# What a patient left part-done on another channel, and what this call leaves
# for the next one. See agent/conversation_store.py.
_conversations: conversation_store.ConversationStore | None = None

# Stands in for a session with no CallAudit of its own (a test double built
# without CallSession). Writes nowhere. Exists so every capture point below
# can record unconditionally instead of each one guarding against it.
_NULL_AUDIT = call_audit.CallAudit(None, "no-call")


def _audit(session) -> call_audit.CallAudit:
    return getattr(session, "audit", None) or _NULL_AUDIT


async def _load_fast_path() -> FastPath | None:
    """Load the 74-row catalogue once so the fast path can identify a test
    or doctor locally. Optional: if the clinic API is not up yet, every
    turn simply goes to the LLM, which is the behaviour that existed
    before this path did.

    Its own function so the message service (start_text_services) loads it
    exactly the way the phone line does -- one catalogue, one matcher.

    # ADDED BY CHAKRAVARDHAN's merge: kept here (rather than only at the one
    # call site it originally guarded) since this function now backs BOTH
    # the phone line's startup AND the text-message service, mirroring the
    # same merge decision already made in main.py's _load_fast_path().
    """
    try:
        import httpx as _httpx
        async with _httpx.AsyncClient(timeout=10) as c:
            payload = (await c.get(f"{CLINIC_API_BASE}/api/v1/catalogue")).json()
        fast_path = FastPath(Catalogue(payload))
        # story title: A thing not existing is never confused with a system
        #   being down
        # user story: As a caller, I want to know whether my test does not
        #   exist or the system cannot be reached, so that I know whether to
        #   call back.
        # acceptance criteria: The two produce different spoken sentences and
        #   different metrics, and the distinction survives every refactor.
        #   This behaviour exists today and gains a permanent regression case.
        #
        # An EMPTY catalogue used to log this same line with a 0 in it and
        # carry on. It is not a quiet condition: the clinic API is up and
        # answering, so nothing is "unreachable", and every single caller is
        # about to be told in a well-formed sentence that their test does not
        # exist. That is the confusion this story is named after, arriving from
        # the data side rather than the code side.
        #
        # clinic-api's own /api/health reports these counts. Nobody was looking.
        if not len(fast_path.catalogue):
            logger.error("CLINIC CATALOGUE IS EMPTY -- the API is up and has no "
                         "rows, so every caller will be told their test does not "
                         "exist. Check the clinic database before taking calls.")
        else:
            logger.info("fast path ready over %d catalogue rows", len(fast_path.catalogue))
        return fast_path
    except Exception as e:  # noqa: BLE001 - degrade to LLM-only, never fail startup
        logger.warning("catalogue unavailable, fast path disabled: %s", e)
        return None


@app.on_event("startup")
async def _startup():
    global _asr, _turn_detector, _tools, _tts, _intent_cache, _fast_path, _audit_store, _conversations
    # FIRST, before any model loads: a caller accepted the moment startup
    # finishes must already have somewhere to be recorded. The store never
    # raises -- a database that cannot be opened is logged and reported under
    # /api/health's `audit` block, and calls are still served.
    _audit_store = call_audit.AuditStore()
    recovered = _audit_store.recover_unfinished(AUDIT_TRANSPORT)
    if recovered:
        logger.warning("audit: finalised %d call record(s) a previous run left open",
                       recovered)
    if not os.environ.get("VOICE_AGENT_AUDIT_TOKEN"):
        logger.warning("audit: VOICE_AGENT_AUDIT_TOKEN is unset -- /api/audit/* is "
                       "readable without a token. Set it on any non-bench deployment.")

    logger.info("loading IndicConformer...")
    _asr = await asyncio.to_thread(TurnASR)
    # Hand the singleton to the language registry under the pod's default
    # language, so a later per-language lookup reuses THIS instance instead
    # of loading a second copy of the same checkpoint onto the same card.
    asr_mod.register(lang_mod.default_lang(), _asr)
    logger.info("loading Silero VAD...")
    _turn_detector = await asyncio.to_thread(TurnDetector)
    _tools = ClinicToolsClient(CLINIC_API_BASE)
    _tts = TTSClient()
    _intent_cache = SemanticCache()
    # Shared with the message service through one SQLite file. Never raises:
    # a store that cannot open is reported in /api/health and calls go on.
    _conversations = conversation_store.ConversationStore()

    # Pull bge-m3 into VRAM before the first caller needs it. Cold-loading
    # it inside a live turn measured past the client's patience AND past
    # the embed timeout, which silently degraded the cache to exact-match
    # only for the opening minutes of the process -- healthy-looking logs,
    # zero semantic hits. OLLAMA_KEEP_ALIVE=-1 keeps it resident after.
    try:
        await run_http(_embed_probe, "warmup")
        logger.info("embedding model warm")
    except Exception as e:  # noqa: BLE001 - cache is optional, the call is not
        logger.warning("embedding warmup failed, cache starts L1-only: %s", e)

    # The catalogue behind the fast path -- see _load_fast_path().
    _fast_path = await _load_fast_path()

    # STORY [Answer Quality and Grounding]
    # As a patient, I want to hear the whole sentence, so that I am
    # not left guessing what the agent tried to say.
    # Before a single caller connects. Deliberately NOT wrapped in a try:
    # a canned line the synthesizer would mangle is a defect in this
    # repository, and the escalation line being sayable is what stops
    # _speak()'s blocked path recursing. Refusing to start is the correct
    # response to either -- and it is checked whether or not enforcement is
    # on, because a literal in the source is not the unknown data shadow
    # mode exists to measure.
    _tts.assert_canned_lines_speakable()
    logger.info("canned lines verified speakable (%d)", len(tts_mod.PREWARM_LINES))

    logger.info("prewarming TTS...")
    await _tts.prewarm()
    # GREETING AND CLOSING -- Author: Chakravardhan. The disclosed greeting and
    # every closing sentence, in every language this pod serves, so neither
    # costs synthesis latency on a live call.
    await call_script.prewarm(_tts)
    logger.info("startup complete -- ready for calls (speakability enforce=%s)",
                SPEAKABILITY_ENFORCE)


@app.on_event("shutdown")
async def _shutdown():
    if _tools:
        await _tools.aclose()
    if _tts:
        await _tts.aclose()
    # wait=False: a worker parked on a socket read to Ollama must not hold
    # the process open past shutdown.
    _shutdown_http_pool()
    # Last, so everything recorded above is committed. Anything a call
    # records after this is counted as dropped, and the call's record is
    # finalised by recover_unfinished() on the next start.
    if _audit_store:
        await asyncio.to_thread(_audit_store.close)
    if _conversations:
        _conversations.close()


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "asr_loaded": _asr is not None,
        "clinic_api_base": CLINIC_API_BASE,
        # Surfaced so deploy/status.sh shows headroom at a glance -- "are we
        # near the ceiling" is the first question at peak, and it should not
        # require reading logs to answer.
        "active_calls": _active_calls,
        "max_calls": MAX_CONCURRENT_CALLS,
        # Non-zero write_failures or dropped means some call records are
        # incomplete -- see /api/audit/calls/{id}'s `integrity` block.
        "audit": _audit_store.health() if _audit_store else {"available": False},
        # Greeting and closing -- Author: Chakravardhan. Which languages are
        # pre-warmed, and whether the clinical and legal leads approved THIS wording.
        "call_script": call_script.health(),
    }


@app.get("/api/stats")
async def stats():
    """Cache effectiveness, for tuning the similarity threshold against
    real traffic rather than against my assumptions about it.

    `speakability` is not a cache statistic and is here anyway, because this
    is the only endpoint anything scrapes. unspeakable_blocked is the number
    an alert rule would watch; while enforce is false it reads as "replies
    that WOULD have been blocked", which is the whole question shadow mode
    exists to answer. Any non-zero value is a missing spoken-form entry --
    agent/turn_log.py's unspeakable_reply rows name which one.
    """
    tts_snapshot = _tts.snapshot() if _tts else None
    return {
        "fast_path": _fast_path.snapshot() if _fast_path else None,
        "intent_cache": _intent_cache.snapshot() if _intent_cache else None,
        "tts_cache": tts_snapshot,
        # ADDED BY CHAKRAVARDHAN -- browser-side audio conditioning metrics
        # (agent/audio_quality.py / agent/quality_metrics.py).
        "audio_quality": METRICS.snapshot(),
        # STORY [Answer Quality and Grounding]
        # As a patient, I want to hear the whole sentence, so that I am
        # not left guessing what the agent tried to say.
        "speakability": {
            "enforced": SPEAKABILITY_ENFORCE,
            "blocked": tts_snapshot["unspeakable_blocked"] if tts_snapshot else None,
        },
        # story title: A thing not existing is never confused with a system
        #   being down
        # user story: As a caller, I want to know whether my test does not
        #   exist or the system cannot be reached, so that I know whether to
        #   call back.
        # acceptance criteria: The two produce different spoken sentences and
        #   different metrics, and the distinction survives every refactor.
        #   This behaviour exists today and gains a permanent regression case.
        #
        # The "different metrics" half of the criterion. Per tool: answered,
        # not_found, unreachable, and the derived rate. Alertable in BOTH
        # directions -- unreachable rising is the API or the network;
        # not_found_rate rising is either callers asking for things the clinic
        # does not stock, or the catalogue emptying itself, which today tells
        # every caller their test does not exist in a perfectly well-formed
        # sentence while nothing anywhere notices.
        #
        # turn_crashes sits beside them because a turn that died in our own
        # code is the system being down from the caller's seat, and a metric
        # that counts only tidy failures reads healthy during an incident.
        "clinic": {
            "tools": _tools.snapshot() if _tools else None,
            "turn_crashes": _turn_crashes,
        },
        # story title: The same question gets the same answer within one call
        # user story: As a caller who asks twice, I want the same answer, so
        #   that I know which one to believe.
        # acceptance criteria: Repeating a question in one call produces an
        #   identical factual answer unless the underlying data changed, in
        #   which case the change is stated. A test asserts consistency across
        #   three repeats with an unchanged backend.
        #
        # `changed` is the alertable one. On a catalogue nobody is editing it
        # should sit at zero, so a rising rate is either real churn in the
        # clinic's data or entity resolution landing on a different row for
        # the same words -- and the second of those is precisely the wrong-hit
        # auditing E9-S12 asks for and nothing in this repository has ever
        # been able to see. `repeats` is the denominator: a `changed` count
        # means nothing without knowing how many repeats there were at all.
        "consistency": dict(_consistency),
        # ADDED BY SOURAV -- reference-data TTL cache (agent/
        # reference_data_cache.py), for tuning cache_ttl_s against real
        # traffic the same way intent_cache's threshold was tuned above.
        "reference_cache": _tools.reference_cache_snapshot() if _tools else None,
    }


@app.get("/api/quality")
async def quality_stats():
    """Turn outcomes split by audio-quality bucket.

    Its own endpoint as well as a key in /api/stats, because this is the
    number the noisy-environment work is answerable to and it should be
    fetchable without pulling cache internals along with it.

    Read `noisy_bucket.accuracy` on its own. A blended figure is dominated
    by whichever bucket is larger -- in practice the quiet one -- so it can
    improve purely because quiet traffic grew, with nothing having got
    better for the callers this work exists for. `overall_accuracy` is
    published beside the split, never instead of it."""
    return METRICS.snapshot()


# ===========================================================================
# CALL RECORDS, FOR STAFF -- Author: Chakravardhan
# ===========================================================================
def _audit_read_denied(token: str | None):
    """The same conditional pattern clinic-api uses for delivery receipts:
    once VOICE_AGENT_AUDIT_TOKEN is set, a matching X-Audit-Token header is
    required; unset (a bench pod), the records are readable as /api/stats
    is, and startup logs a warning saying so. Read at request time so a
    token can be rotated without a restart."""
    expected = os.environ.get("VOICE_AGENT_AUDIT_TOKEN", "")
    if expected and not hmac.compare_digest((token or "").encode(), expected.encode()):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    return None


@app.get("/api/audit/calls")
async def audit_calls(limit: int = Query(50, ge=1, le=500),
                      status: str | None = Query(None),
                      x_audit_token: str | None = Header(default=None)):
    """Most recent calls first, one row each. Filter by final_status to find
    the ones that went wrong: ?status=failed, ?status=error."""
    denied = _audit_read_denied(x_audit_token)
    if denied is not None:
        return denied
    if _audit_store is None:
        return JSONResponse(status_code=503, content={"error": "audit store not initialised"})
    calls = await asyncio.to_thread(_audit_store.list_calls, limit, status)
    return {"count": len(calls), "calls": calls}


@app.get("/api/audit/calls/{call_id}")
async def audit_call(call_id: str, x_audit_token: str | None = Header(default=None)):
    """One call, every event in order, and whether the record is whole."""
    denied = _audit_read_denied(x_audit_token)
    if denied is not None:
        return denied
    if _audit_store is None:
        return JSONResponse(status_code=503, content={"error": "audit store not initialised"})
    record = await asyncio.to_thread(_audit_store.get_call, call_id)
    if record is None:
        return JSONResponse(status_code=404, content={"error": "no such call"})
    return record


def _wav_duration_s(wav_bytes: bytes) -> float:
    try:
        with contextlib.closing(wave.open(io.BytesIO(wav_bytes), "rb")) as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:  # noqa: BLE001 - a fallback clip may not be canonical WAV
        return 5.0


class CallSession:
    """One PCM buffer for the ENTIRE call, and a marker for how much of it
    has been consumed.

    The continuous-buffer design is inherited from the WebM version, where
    it was forced: MediaRecorder puts the container header only in the
    first chunk, so resetting the buffer mid-call produced audio that
    could never be decoded again. That constraint is GONE here -- raw PCM
    has no header and any byte range is independently valid.

    It is kept anyway, because the second reason for it was always the
    better one: `processed_until_s` gives every turn an absolute,
    monotonic position on one call-long timeline. Silero's segment
    boundaries move as more audio arrives, so a turn detector run against
    a buffer that keeps restarting drops any utterance straddling the
    seam. Each poll therefore looks only at the UNPROCESSED TAIL.

    What PCM changes is the cost and the accuracy of that lookup: the tail
    is a slice rather than a full re-decode, and sample index maps to
    wall-clock exactly, so the timeline cannot drift.
    """

    def __init__(self, ws: WebSocket):
        self.ws = ws
        # The full 128-bit uuid4, not the 8-hex-char prefix this used to be.
        # It is now the permanent key of a call's audit record, and 32 bits
        # is a coin-flip chance of two calls colliding within ~77,000 calls.
        self.call_id = uuid.uuid4().hex
        self.tmpdir = tempfile.mkdtemp(prefix=f"kcd_call_{self.call_id}_")
        self.last_activity = time.time()
        self.dispatch_lock = asyncio.Lock()
        self.processed_until_s = 0.0
        # The language this caller is being served in. Set once from the
        # pod default and only ever changed by the caller asking, so a
        # single mis-transcribed word cannot flip a call into a language
        # the caller does not speak. See agent/language.py. A pod that can HEAR
        # more than one language may also identify it from the first
        # utterance -- see _transcribe_in_caller_language().
        self.lang = lang_mod.default_lang()
        # One language probe per call, on the first turn only.
        self.language_probe_done = False

        # EVERY CALL LEAVES A COMPLETE RECORD -- Author: Chakravardhan
        #
        # Opened here, the moment the socket is accepted, so a call that
        # dies in its first second still has a record. See agent/call_audit.py.
        # GREETING AND CLOSING -- Author: Chakravardhan. The ordinary CallAudit,
        # also remembering what the clinic API answered, for the closing.
        self.audit = call_script.ObservedCallAudit(_audit_store, self.call_id,
                                                   transport=AUDIT_TRANSPORT, language=self.lang)
        self.closing_spoken = False
        # Set by code that decides to END the call itself (the idle timeout).
        # ws_audio falls back to what it observed when this is None.
        self.end_reason: str | None = None

        # HISTORY VERIFICATION STATE -- Author: Chakravardhan
        #
        # Scoped to ONE CALL and never persisted. The handset is shared:
        # the person who verified may have handed the phone to somebody
        # else before the next call, so a token that outlived the
        # conversation would be the exact hole this story closes.
        # _cleanup() revokes it when the socket drops.
        self.history_token: str | None = None
        self.history_phone: str | None = None

        # A SINGLE PATIENT TIMELINE -- Author: Chakravardhan
        #
        # The verified caller's record as clinic-api returned it (bookings
        # and tests in one list), fetched at most once per call by
        # _load_timeline() and read from memory after that -- so the caller
        # is never asked for what the hospital already holds. Same lifetime
        # as the token that opened it: this call only, dropped by cleanup().
        # `timeline_stale` is set when this call itself changes the record
        # (a new booking), so the next read fetches it again.
        self.timeline: dict | None = None
        self.timeline_stale = False
        self.utt_seq = 0
        self.last_heartbeat = time.time()
        self.audio = PcmCallBuffer()
        self.declared_rate: int | None = None

        # Starts True: the greeting goes out before the caller has said
        # anything, so the gate must already be closed when the first poll
        # tick runs, not opened a moment later by _speak().
        self.agent_speaking = True
        self.speak_deadline = time.time() + PLAYBACK_GUARD_S
        self.resync_pending = False

        # Cross-turn booking state. None outside a booking flow. See
        # _continue_pending's docstring for the shape and why this exists --
        # in short, it is the only thing that survives between turns, since
        # every _resolve_intent call otherwise starts from zero context.
        self.pending: dict | None = None

        # The single structure every downstream layer reads for caller signals
        # (Blueprint 4.5). Replaced wholesale each turn by Call Intelligence,
        # never mutated -- CallSession stays the transport bookkeeper it is,
        # and the caller picture lives in its own frozen object.
        #
        # Today no detector exists, so this is the Appendix C "normal" row on
        # every call with caller_state="unknown". That is deliberate: it is
        # the behaviour the agent already had, so the object changes nothing
        # until something actually detects.
        self.call_state = call_state_mod.build()

        # story title: The same question gets the same answer within one call
        # user story: As a caller who asks twice, I want the same answer, so
        #   that I know which one to believe.
        # acceptance criteria: Repeating a question in one call produces an
        #   identical factual answer unless the underlying data changed, in
        #   which case the change is stated. A test asserts consistency across
        #   three repeats with an unchanged backend.
        #
        # What this caller has already been told. Per-call by construction --
        # it is created here and nothing outlives the session, which is the
        # scope the story asks for ("within one call") and also the only scope
        # that is safe: a process-wide version would compare one caller's
        # answer against another's. Never holds a reply, only the facts a
        # reply was rendered from. See agent/answer_ledger.py.
        self.answer_ledger = answer_ledger.AnswerLedger()

        # story title: A multi-part question is answered in full
        # user story: As a caller who asked two things, I want both answered,
        #   so that I do not have to ask again.
        # acceptance criteria: Every answerable part of a turn is answered in
        #   the order asked, and any part that cannot be answered is
        #   explicitly addressed rather than dropped. Completeness is scored
        #   on a labelled multi-part set.
        #
        # Parts of an earlier turn that a question got in front of, the
        # utterance they came from, and how many turns they have waited.
        # Deliberately NOT stored inside session.pending: that dict is
        # cleared on a dozen paths, every one of which would silently bin a
        # question the caller actually asked.
        self.deferred: dict | None = None

        # Consecutive turns that were echoed back for confirmation because the
        # decoders disagreed. Reset by any turn that proceeds normally. Capped
        # so a caller on a bad line is offered a human instead of being asked
        # "did I hear you right?" indefinitely -- a repair ladder that never
        # ends is a D-grade outcome even though every individual turn is safe.
        self.confirm_attempts = 0

        # ADDED BY SOURAV -- "Caller asks a follow-up that depends on the
        # previous answer" story. A SEPARATE kind of cross-turn memory
        # from `pending` just above: `pending` tracks one IN-PROGRESS flow
        # waiting on one specific missing field; `state` tracks the last
        # entity (test/doctor/package) each already-FINISHED question was
        # actually about, so a brand-new question that refers back with a
        # pronoun ("eta-r jonno ki prescription lagbe?") can resolve
        # without making the caller repeat the name. See agent/state.py's
        # own module docstring for the full design.
        self.state = DialogueState()

        # The retry ladder for THIS caller. Per-session, not global: the
        # person in the market who has now failed twice needs the keypad,
        # and the person on the next line who failed once does not. See
        # agent/quality_metrics.py.
        self.failures = TurnFailureTracker()

        # Wall-clock origin for this call. The echo reference is timestamped
        # against it rather than against the decoded-buffer length, because
        # capture runs continuously at real time (see the client's
        # setMicMuted comment) and reading the buffer length would cost a
        # full WebM decode inside _speak. Any residual skew between the two
        # clocks is absorbed by the lag search in best_lag_correlation.
        self.started_at = time.time()

        # Holds what we played, decides echo vs barge-in, and classifies the
        # path. See agent/echo_guard.py.
        self.echo = EchoGuard()
        self._pending_echo_ref = None

    def call_time_s(self) -> float:
        return time.time() - self.started_at

    def playback_start_s(self) -> float:
        """Call-time at which the NEXT reply handed to the client will
        actually begin playing.

        Now, unless audio is already in flight -- in which case the client
        queues this clip behind it, and it starts when the current deadline
        (minus the guard that deadline carries) is reached."""
        now_s = self.call_time_s()
        if not self.agent_speaking:
            return now_s
        queued_s = (self.speak_deadline - PLAYBACK_GUARD_S) - self.started_at
        return max(now_s, queued_s)

    def take_echo_reference(self) -> object:
        """Hand over the reference for the window a barge-in was detected
        in, exactly once. Cleared on read so an ordinary turn that follows
        never has stale playback subtracted out of it."""
        ref, self._pending_echo_ref = self._pending_echo_ref, None
        return ref

    def barge_in(self):
        """The caller talked over the agent. Deliberately NOT release_gate().

        release_gate() sets resync_pending, and _resync_after_playback then
        jumps processed_until_s to the end of the buffer to throw away
        everything captured while the agent spoke. During a barge-in that
        region is precisely the caller's interruption -- discarding it would
        stop the agent and then ignore what stopped it."""
        # Stash what we were playing across the barge-in window. The clip
        # that follows contains the caller talking OVER this, so it is the
        # one turn in the call where subtracting our own audio is both
        # possible and worth doing.
        now_s = self.call_time_s()
        self._pending_echo_ref = self.echo.reference.slice(
            now_s - ECHO_CFG.barge_in_window_s, now_s)

        # Playback is about to be STOPPED, so the rest of this reply will
        # never leave the speaker. Forget it, or the level test keeps judging
        # later windows against sound that was never made -- the expected echo
        # ceiling stays high and the caller cannot interrupt a second time.
        self.echo.reference.truncate_after(now_s)

        # Skip forward to the barge-in window, but NO further. Everything
        # before it is the agent's own reply, and leaving processed_until_s
        # behind it would hand the turn detector a tail containing our echo --
        # it would then place utterance_start_s at the echo's onset and send
        # ASR a clip of the agent talking, which is exactly the self-answering
        # loop the old half-duplex gate existed to prevent.
        #
        # Not the full resync release_gate() would do: that jumps to the end
        # of the buffer and would discard the interruption itself. The
        # utterance-boundary fix then refines the true onset inside this
        # window, as it does for any other turn.
        self.processed_until_s = max(
            self.processed_until_s,
            now_s - ECHO_CFG.barge_in_window_s - UTTERANCE_PAD_S,
        )

        self.agent_speaking = False
        self.resync_pending = False
        self.speak_deadline = 0.0
        self.echo.barge_in_count += 1

    def hold_gate_for(self, audio_duration_s: float):
        """Called before each reply goes out. Extends rather than replaces
        the deadline: replies queue on the client, so a second clip starts
        playing only after the first finishes."""
        base = max(self.speak_deadline, time.time()) if self.agent_speaking else time.time()
        self.agent_speaking = True
        self.speak_deadline = base + audio_duration_s + PLAYBACK_GUARD_S

    def release_gate(self):
        """Playback is over. Don't touch processed_until_s here -- the poll
        loop owns the decoded buffer and does the resync on its next tick."""
        self.agent_speaking = False
        self.resync_pending = True

    async def append(self, chunk: bytes):
        self.last_activity = time.time()
        self.audio.append(chunk)

    async def send_json(self, sender: str, text: str):
        await self.ws.send_text(json.dumps({"sender": sender, "text": text}, ensure_ascii=False))

    async def send_audio(self, wav_bytes: bytes):
        if wav_bytes:
            await self.ws.send_bytes(wav_bytes)

    def cleanup(self):
        import shutil
        # The verification token dies with the call, deliberately. The
        # handset is shared -- the person who verified may hand the phone to
        # somebody else before the next call, and a token that outlived the
        # conversation would be the exact hole this story closes. Dropping
        # the local reference also stops it reaching any later log line.
        self.history_token = None
        self.history_phone = None
        # The record goes with the token that opened it.
        self.timeline = None
        with contextlib.suppress(OSError):
            shutil.rmtree(self.tmpdir, ignore_errors=True)


# STORY [Answer Quality and Grounding]
# As a patient, I want to hear the whole sentence, so that I am
# not left guessing what the agent tried to say.
async def _speak(session: CallSession, text_bn: str, fallback_reason: str | None = None,
                 audit_redact: str | None = None):
    """Say one line to the caller, or say why it could not be said.

    SYNTHESIZE FIRST, THEN SEND THE TRANSCRIPT.
    This used to push text_bn to the browser before synthesis, which read as
    snappier -- the line appeared while the vocoder was still working. It also
    meant the pane was a record of what the agent INTENDED to say. Once a reply
    can be blocked and replaced, that stops being a harmless discrepancy: the
    transcript would show the answer while the caller heard a referral to the
    counter, and the one artefact anyone would check afterwards would disagree
    with the call. The cost is that the text now appears with the audio instead
    of a beat before it; correctness of the record wins.

    # AUDIT: what the caller was told is recorded in `finally`, so it is
    # recorded whether or not it reached them -- `delivered` stays False if
    # the socket was already gone -- and records what they HEARD: when TTS
    # fails the caller hears a pre-recorded apology, not these words.
    # `audit_redact` withholds the words themselves (a patient's history)
    # while keeping the fact that something was said.
    # THE SAME ANSWER, WRITTEN -- Author: Chakravardhan. A message session
    # (agent/message_service.py) has no TTS, no echo reference and no
    # playback gate; the very same sentence goes out as text instead.
    """
    if getattr(session, "channel", privacy.CHANNEL_VOICE) != privacy.CHANNEL_VOICE:
        await _deliver_written(session, text_bn, fallback_reason, audit_redact)
        return

    audio, tts_error, delivered = "none", None, False
    wav = None
    try:
        try:
            # speech_rate is read from the call state rather than hardcoded. This
            # is the one place the object currently changes what a caller HEARS,
            # and it resolves to "default" until a detector sets caller_state or
            # senior -- so it is a no-op today by design, not by accident.
            # language is session.lang (the ADDED BY CHAKRAVARDHAN per-caller
            # language, switchable mid-call -- see language_switch_reply()),
            # not session.call_state.language, which stays None until a
            # detector that does not exist yet sets it.
            wav = await _tts.synthesize(text_bn,
                                        speech_rate=session.call_state.speech_rate,
                                        language=session.lang)
            audio = "synthesized" if wav else "none"
        # STORY [Answer Quality and Grounding]
        # As a patient, I want to hear the whole sentence, so that I am
        # not left guessing what the agent tried to say.
        except UnspeakableReply as e:
            # A hole was found in a reply we composed. Caught BEFORE the generic
            # handler below on purpose: that one answers with the "system busy"
            # clip, which is right when the vocoder is down and wrong here. This
            # is a defect on our side, not an outage, and the honest response is
            # to send the caller somewhere that can actually answer them.
            logger.error("[%s] reply blocked, dropped=%s -- escalating to counter",
                         session.call_id, list(e.dropped))
            turn_log.record_unspeakable(session.call_id, session.utt_seq,
                                        e.dropped, enforced=True)
            text_bn = UNSPEAKABLE_ESCALATION
            try:
                # Cannot recurse: this line is in PREWARM_LINES and startup asserts
                # every one of them is speakable, so it can never be blocked itself.
                # The broad catch is the belt to that braces -- if it somehow were,
                # the caller still gets the pre-recorded clip rather than silence.
                wav = await _tts.synthesize(text_bn,
                                            speech_rate=session.call_state.speech_rate)
                audio = "synthesized" if wav else "none"
            except Exception:  # noqa: BLE001 - last resort, never raise past here
                logger.exception("[%s] escalation line failed to synthesize", session.call_id)
                wav = _tts.fallback_audio("tool_failure")
                audio = "fallback_clip" if wav else "none"
        except Exception as e:  # noqa: BLE001 - TTS is the last mile, must not raise past here
            logger.warning("[%s] TTS failed (%s) -- using fallback audio", session.call_id, e)
            tts_error = f"{type(e).__name__}: {e}"
            wav = _tts.fallback_audio(fallback_reason or "tts_failure")
            audio = "fallback_clip" if wav else "none"

        # STORY [Answer Quality and Grounding]
        # As a patient, I want to hear the whole sentence, so that I am
        # not left guessing what the agent tried to say.
        if not SPEAKABILITY_ENFORCE:
            # SHADOW MODE. synthesize() counted and logged this, but only the
            # orchestrator knows the call and turn, so the exported row is written
            # here. Guarded so it cannot double up with the enforced branch above:
            # exactly one of the two runs.
            #
            # This whole block is temporary. It exists to answer one question --
            # how often would real traffic have been blocked -- and comes out when
            # SPEAKABILITY_ENFORCE becomes the default.
            _verdict = speakability.check(text_bn, language=session.lang)
            if _verdict.is_blocked:
                turn_log.record_unspeakable(session.call_id, session.utt_seq,
                                            _verdict.dropped, enforced=False)

        await session.send_json("AI", text_bn)

        # Record what we are about to play as the echo reference BEFORE the
        # bytes leave, for the same reason the gate closes first: the client can
        # start playing the moment they land, and a barge-in check that runs
        # before the reference exists would find "no reference" and treat our own
        # voice as the caller.
        #
        # Timestamped at the point this clip will actually START playing, which
        # is NOT now when a reply is already in flight -- replies queue on the
        # client (see hold_gate_for). Using send time for a queued clip puts its
        # reference earlier than the sound it describes, so the lookup during the
        # real playback returns silence, "no_reference" fires, and our own echo
        # is read as the caller interrupting.
        session.echo.note_playback(session.playback_start_s(),
                                   pcm_from_wav_bytes(wav, session.echo.sample_rate))

        # Close the gate BEFORE the bytes leave, never after: the client can
        # start playing the moment they land, and a poll tick that slips in
        # between send and gate is exactly the echo this prevents.
        session.hold_gate_for(_wav_duration_s(wav))
        await session.send_audio(wav)
        delivered = True
    finally:
        _audit(session).agent_response(
            text_bn, lang=getattr(session, "lang", None), audio=audio, delivered=delivered,
            fallback_reason=fallback_reason, tts_error=tts_error, redact=audit_redact)


async def _deliver_written(session, text: str, fallback_reason: str | None,
                           audit_redact: str | None) -> None:
    """_speak for the message channel. Recorded exactly like a spoken reply
    -- in `finally`, whether or not it reached the patient -- with
    call_audit.AUDIO_TEXT saying it went out as words rather than audio."""
    delivered = False
    try:
        delivered = await session.deliver_text(text)
    finally:
        _audit(session).agent_response(
            text, lang=getattr(session, "lang", None), audio=call_audit.AUDIO_TEXT,
            delivered=delivered, fallback_reason=fallback_reason, redact=audit_redact)


# story title: The same question gets the same answer within one call
# user story: As a caller who asks twice, I want the same answer, so that I
#   know which one to believe.
# acceptance criteria: Repeating a question in one call produces an identical
#   factual answer unless the underlying data changed, in which case the
#   change is stated. A test asserts consistency across three repeats with an
#   unchanged backend.
#
# WHY A WRAPPER AND NOT A LINE INSIDE _speak
# ------------------------------------------
# _speak knows the sentence and nothing else -- not which intent produced it,
# not which clinic response it was rendered from -- and both are needed to
# decide whether this answer contradicts an earlier one. Passing them into
# _speak would push tool responses into the transport layer for the sake of
# one caller in eight.
#
# WHY A WRAPPER AND NOT A LINE AT EACH CALL SITE
# ----------------------------------------------
# There are EIGHT places that speak a factual reply: three in
# _dispatch_turn_inner and five in _continue_pending. Eight places to remember
# is eight places to eventually forget, and the guarantee would then hold on
# the routes someone happened to think about. Same argument tool_outcome.py
# makes about its twelve, and tests/test_answer_consistency.py enforces it
# statically: a reply template handed straight to _speak fails the build.
# story title: Near matches are offered rather than guessed or refused
# user story: As a caller naming something loosely, I want the close matches
#   offered, so that I am not told my test does not exist when it does.
# acceptance criteria: When several catalogue rows fall within the match band
#   the agent offers up to three by name and asks which. Candidates are
#   generated across every supported language and romanised spelling. The
#   did-you-mean path covers the ambiguous case and not only total failure.
async def _offer_near_matches(session: CallSession, intent: str, result: dict,
                              offered_date: str | None) -> bool:
    """-> True if this response was an ambiguity and the turn is now finished.

    An ambiguous response is a QUESTION the clinic asked back, so nothing
    factual is spoken and nothing is recorded as an answer. The caller's
    reply lands in _continue_pending's entity_choice state, which re-runs the
    lookup against the canonical name they chose.

    `offered_date` is carried through so the second lookup asks about the
    same day as the first. Without it, "is Dr Sen in on Tuesday" answered
    with a choice, then resolved, would silently become a question about
    today.

    An EMPTY candidate list is not a bug: clinic-api sends one when several
    rows matched and at least one of them has no Bengali alias, because
    offering a partial list would be a guess wearing a question mark. There
    is nothing to choose from, so no choice state is opened -- the caller is
    asked to name it again and the next turn starts clean.
    """
    if not isinstance(result, dict) or not result.get("ambiguous"):
        return False

    candidates = result.get("candidates") or []
    logger.info("[%s] %s ambiguous (%d candidate(s)) -- offering instead of guessing",
                session.call_id, intent, len(candidates))

    session.pending = {
        "awaiting": "entity_choice", "intent": intent, "slots": {},
        "candidates": candidates, "offered_date": offered_date, "retries": 0,
    } if candidates else None

    await _speak(session, near_match_prompt(candidates))
    return True


async def _speak_fact(session: CallSession, intent: str, slots: dict,
                      result: dict, reply: str,
                      offered_date: str | None = None) -> bool:
    """Speak a factual reply, saying so if it contradicts an earlier one.

    `reply` is always rendered FRESH by the caller from a live clinic
    response. Nothing here substitutes a remembered answer -- the ledger only
    compares, and the most it can do is prepend one sentence. The
    Architecture Plan is explicit on this point ("do not optimise by caching
    replies"), and a reply cache is also the one change that would reintroduce
    the stale price this whole design avoids.

    story title: Near matches are offered rather than guessed or refused
    user story: As a caller naming something loosely, I want the close
        matches offered, so that I am not told my test does not exist when
        it does.
    acceptance criteria: When several catalogue rows fall within the match
        band the agent offers up to three by name and asks which. Candidates
        are generated across every supported language and romanised
        spelling. The did-you-mean path covers the ambiguous case and not
        only total failure.

    -> True when the response was ambiguous and an offer was spoken instead
    of the reply; every call site must then end the turn. The guard lives
    here rather than at the eight call sites for the same reason the
    consistency check does, and the return value exists because the callers
    set session.pending AFTER speaking -- an offer that set the choice state
    from in here would be overwritten a line later by the caller's own
    bookkeeping. tests/test_near_match_offers.py fails the build if a call
    site drops the guard.
    """
    if await _offer_near_matches(session, intent, result, offered_date):
        return True

    verdict, previous = session.answer_ledger.check(intent, slots, result)

    if verdict != answer_ledger.FIRST:
        _consistency["repeats"] += 1
        _consistency[verdict] += 1

    if verdict == answer_ledger.CHANGED:
        # Logged at WARNING, not INFO. On a catalogue nobody is editing this
        # should not happen, and when it does the two candidate causes -- the
        # clinic's data moved, or entity resolution landed on a different row
        # for the same words -- are told apart by whether the leading identity
        # in the two tuples matches. Facts only; no caller data reaches here.
        logger.warning("[%s] %s answer changed within the call: %s -> %s",
                       session.call_id, intent, previous,
                       answer_ledger.facts(intent, result))
        reply = with_change_notice(reply)

    await _speak(session, reply)
    return False


async def _slice_utterance(session: CallSession, start_s: float, end_s: float, seq: int) -> str:
    """Cuts [start_s, end_s+pad] -- both ABSOLUTE call-time offsets -- out
    of the call's decoded WAV into its own small file for ASR."""
    sr = session.audio.sample_rate
    clip = session.audio.slice_tensor(start_s, end_s + UTTERANCE_PAD_S)
    clip_path = os.path.join(session.tmpdir, f"utt{seq}.wav")

    def _write():
        wav = clip.unsqueeze(0)
        out_sr = sr
        if sr != SAMPLE_RATE:
            # Only reachable when the browser refused a 16kHz AudioContext.
            # ASR expects 16k, so convert here rather than letting it
            # silently transcribe pitch-shifted audio.
            wav = torchaudio.functional.resample(wav, sr, SAMPLE_RATE)
            out_sr = SAMPLE_RATE
        torchaudio.save(clip_path, wav, out_sr)

    await asyncio.to_thread(_write)
    return clip_path


def _record_intent(session, data: dict, source: str, **detail) -> None:
    """AUDIT: INTENT_DETECTED and SLOTS_EXTRACTED, from the very dict that
    is about to drive the turn -- and which of the three tiers produced it,
    because "the LLM decided" and "a string match decided" are different
    claims about how the system understood the caller."""
    _audit(session).intent(data.get("intent"), source, slots=data.get("slots") or {},
                           direct_reply_bn=data.get("direct_reply_bn"), **detail)


async def _resolve_intent(session: CallSession, text: str) -> dict:
    """Semantic cache in front of the LLM. A hit skips Ollama entirely --
    the slowest hop in the turn -- but the clinic lookup that follows still
    runs live, so a cached intent can never serve a stale price."""
    # Tier 1: decide it locally if we can. For a fixed catalogue the
    # entity is a string-matching problem with a 0.32 confidence margin,
    # where the embedding route had 0.03 -- see agent/fast_path.py. This
    # returns None whenever it is not sure, which is the common case for
    # anything except a routine price or availability question.
    # to_thread (default pool) for this one: it is local CPU work over the
    # 74-row catalogue with no network hop, so it belongs with the audio
    # path, not behind the blocking-HTTP pool. See agent/executors.py.
    if _fast_path is not None:
        # MIXED-LANGUAGE SPEECH -- Author: Chakravardhan. The fast path reads
        # Bengali; a caller's Hindi or English words (its GUARD words too:
        # "aur", "nahi", "monday") are shown to it in that Bengali. An
        # all-Bengali turn is shown unchanged. The cache and the model below
        # still get the caller's own words.
        mixed = code_mix.for_fast_path(text)
        hit = await asyncio.to_thread(_fast_path.resolve, mixed.text)
        if hit is not None:
            logger.info("[%s] fast path resolved %s (%.2f) -- no LLM call",
                        session.call_id, hit.intent, hit.confidence)
            data = hit.as_llm_shape()
            _record_intent(session, data, "fast_path", confidence=round(hit.confidence, 3),
                           matched_form=hit.matched_form,
                           **({"code_mix_words": mixed.changed} if mixed.changed else {}))
            return data

    # The three calls below all make BLOCKING urllib requests to Ollama --
    # cache.get/put embed via bge-m3, extract_intent generates via Qwen --
    # so they run on the dedicated HTTP pool. On the shared default pool a
    # burst of concurrent callers parks every worker on a socket read and
    # the audio path (VAD polls, torchaudio, ASR) stops running for EVERY
    # call, not just the slow ones. agent/executors.py has the full write-up.
    cached, how = await run_http(_intent_cache.get, text)
    if cached is not None:
        logger.info("[%s] intent cache %s hit", session.call_id, how)
        _record_intent(session, cached, f"intent_cache_{how}")
        return cached

    data, diag = await run_http(extract_intent, text)
    logger.info("[%s] intent extracted in %.2fs (%d attempt(s))",
                session.call_id, diag["total_time_s"], diag["attempts"])
    # Recorded BEFORE the cache write below, so a cache failure cannot cost
    # the record of what the model actually returned.
    _record_intent(session, data, "llm", attempts=diag["attempts"],
                   latency_s=round(diag["total_time_s"], 3), retry_errors=diag["errors"])
    await run_http(_intent_cache.put, text, data)
    return data


def _next_missing(slots: dict) -> str | None:
    """-> the first still-empty field in _BOOKING_FIELDS order, or None
    once every field a booking needs is filled."""
    for field in _BOOKING_FIELDS:
        if not slots.get(field):
            return field
    return None


def _next_missing_callback(slots: dict) -> str | None:
    """-> the scoped `awaiting` value (NOT the bare slot key -- see
    _CALLBACK_FIELDS' own comment) for the first still-empty field this
    story needs, or None once both are filled. Checks slots["phone"] (the
    real slot key) but returns "callback_phone" (the scoped awaiting
    name) so _continue_pending routes it through this story's own branch
    rather than the pre-existing booking flow's bare "phone" tail."""
    if not slots.get("callback_time_window"):
        return "callback_time_window"
    if not slots.get("phone"):
        return "callback_phone"
    return None


def _match_offered(text: str, candidates: list[dict]) -> dict | None:
    """-> the canonical `name` (the form book_appointment/get_doctor_
    availability need) of the doctor the caller just named out of a list
    session.pending offered a moment ago, or None if the utterance is not
    confidently one of them.

    Same trust model as fast_path.Catalogue.match: score every candidate
    against every spoken form (English surname AND the seeded Bengali
    alias, since the caller may answer in either script), and only commit
    above a floor rather than always taking the best of a bad field. 0.55
    is fast_path.ENTITY_MATCH_FLOOR -- reused here because the situation is
    the same shape (matching a short spoken name against a small local
    list), just with the candidate list narrowed to what was JUST spoken
    to the caller instead of the whole 74-row catalogue.
    """
    if not candidates:
        return None
    norm_text = text.strip().lower()
    if not norm_text:
        return None
    # story title: Near matches are offered rather than guessed or refused
    # user story: As a caller naming something loosely, I want the close
    #   matches offered, so that I am not told my test does not exist when it
    #   does.
    # acceptance criteria: When several catalogue rows fall within the match
    #   band the agent offers up to three by name and asks which. Candidates
    #   are generated across every supported language and romanised spelling.
    #   The did-you-mean path covers the ambiguous case and not only total
    #   failure.
    #
    # The runner-up is tracked now, and the margin applies here too. This is
    # the turn AFTER an offer -- the caller has just been read two names and
    # answered -- so an answer that fits both of them equally is the one
    # place where guessing would be least forgivable: the whole point of the
    # preceding turn was that the agent had stopped guessing.
    best, best_score, runner_up = None, 0.0, 0.0
    for c in candidates:
        forms = [c["name"], c["name"].split()[-1]]
        if c.get("name_bn"):
            forms.append(c["name_bn"])
        row_best = 0.0
        for form in forms:
            if not form:
                continue
            form_l = form.lower()
            score = difflib.SequenceMatcher(None, form_l, norm_text).ratio()
            if form_l in norm_text or norm_text in form_l:
                score = max(score, 0.85)
            row_best = max(row_best, score)
        if row_best > best_score:
            best, best_score, runner_up = c, row_best, best_score
        elif row_best > runner_up:
            runner_up = row_best
    # Returns the whole candidate, not just the canonical name. The API needs
    # the English `name`; the booking readback needs `name_bn`, because it is
    # SPOKEN. Returning one and looking the other up later is what put an
    # English name into a Bengali sentence -- see booking_confirm_prompt.
    if best_score < 0.55:
        return None
    if (best_score - runner_up) < COMMIT_MARGIN:
        # Two of the offered names fit what the caller just said equally
        # well. Re-asking is the only honest move; picking one would undo
        # the turn that produced the offer.
        return None
    return best


def _match_candidate_name(text: str, candidates: list[str]) -> str | None:
    """ADDED BY SOURAV -- "Caller asks a follow-up that depends on the
    previous answer" story. Same trust model and score floor as
    _match_offered()/agent/report_flow.py's match_candidate_report()
    just above/elsewhere -- matching a short spoken reply against a SMALL
    list just offered to the caller (here: the 2+ ambiguous names
    ambiguous_reference_reply() just spoke, from agent/state.py's
    EntitySlot.names) -- kept as its own function rather than reused
    because those two match against `{"name":..., "name_bn":...}` dicts
    while this one's candidates are already plain strings (agent/state.py
    never tracks a Bengali alias, only the catalogue's own canonical
    name -- see that module's own docstring)."""
    if not candidates:
        return None
    norm_text = text.strip().lower()
    if not norm_text:
        return None
    best_name, best_score = None, 0.0
    for candidate in candidates:
        form_l = candidate.lower()
        score = difflib.SequenceMatcher(None, form_l, norm_text).ratio()
        if form_l in norm_text or norm_text in form_l:
            score = max(score, 0.85)
        if score > best_score:
            best_name, best_score = candidate, score
    return best_name if best_score >= 0.55 else None


# Bare "নাম বলছি" prefixes a caller sometimes leads a name with. Stripped
# rather than relied upon -- most callers just say the name on its own.
_NAME_PREFIXES = ("আমার নাম ", "নাম ", "আমি ")


def _clean_patient_name(text: str) -> str | None:
    """Strip at most ONE leading filler phrase off a caller's spoken
    patient name, e.g. "আমার নাম রাহুল সেন" -> "রাহুল সেন".

    Bug fixed here: this used to re-check ALL of _NAME_PREFIXES in a
    plain `for` loop with no `break`, testing each prefix against the
    ALREADY-stripped text from the previous iteration. Real disfluent
    speech (or ASR output) that happens to start with more than one
    filler phrase in a row -- e.g. "নাম আমি সেন" ("name -- I'm Sen") --
    walked through BOTH matching prefixes one after another
    ("নাম আমি সেন" -> strip "নাম " -> "আমি সেন" -> strip "আমি " -> "সেন"),
    silently eating the caller's first name along with the filler words
    and leaving only the surname. Stopping after the first match means
    at most one filler phrase is ever removed -- the rest of whatever
    the caller said, first name included, is left alone."""
    t = text.strip().strip("।!?., ")
    if not t:
        return None
    for prefix in _NAME_PREFIXES:
        if t.startswith(prefix):
            t = t[len(prefix):].strip()
            break
    return t or None


async def _finish_booking(session: CallSession, slots: dict, from_record: bool = False, *,
                           confirmed: bool = False, language: str = "bengali"):
    """All 5 fields are filled -- place the booking and clear pending
    regardless of outcome. Failure here is reported the same way the old
    single-shot book_appointment branch reported it (tool_failure
    fallback audio), just reachable now from either that branch OR from
    the tail of a multi-turn _continue_pending flow.

    `confirmed` must be True, OR `from_record` must be True. This is the one
    irreversible thing the agent does, and the guard lives HERE rather than
    at the call sites on purpose: there are already several ways in (the
    book_appointment intent branch, the tail of _continue_pending, and the
    ADDED BY CHAKRAVARDHAN verified-record booking flow below), a guard that
    has to be remembered at each entry point is a guard that will eventually
    be forgotten at one of them. Refusing inside the function makes the write
    unreachable by omission rather than by discipline.

    `from_record` means the name and number came from the verified caller's
    record (_fill_from_record) rather than freshly-parsed slots -- the caller
    is told so, without the name being read back, and no separate spoken
    readback-confirmation step is required: the record was already reached
    through its own PIN/DOB verification (see verification_prompt() and
    PURPOSE_BOOKINGS), which is the confirmation this path relies on instead.

    UPDATED BY SOURAV -- `language` is new (default "bengali" so every
    pre-existing caller of this function keeps working unchanged): see
    the module-level detect_language import comment above for why this
    is threaded through now. Passed straight to booking_reply() below,
    which already accepted it and already had a full 4-language body --
    nothing in that function needed to change, only this call site. The
    insufficient-verified-information outcome below still only speaks
    INSUFFICIENT_VERIFIED_INFORMATION_BN, which stays Bengali-only by
    design for now (see that constant's own comment in
    agent/reply_templates.py) -- not yet part of this threading.

    "The agent says it cannot confirm rather than guessing": a
    success=True response is trusted only after confirming
    confirmation_id/date/time_slot actually came back non-empty. This is
    the ONE real trigger that story wires up -- deliberately just a
    presence check, not shape validation, not a business-rule check, not
    a database re-query (see agent/outcomes.py's module docstring for
    exactly which sibling stories those belong to instead). A caller is
    never read a confirmation number the code cannot itself verify it
    received.
    """
    if not (confirmed or from_record):
        # Not an error the caller caused -- most likely a new code path that
        # skipped the readback. Log it loudly, then do the safe thing rather
        # than the convenient one: ask, and write only if they say yes.
        logger.error("[%s] booking reached _finish_booking unconfirmed -- "
                     "refusing the write and asking the caller", session.call_id)
        session.pending = {
            "awaiting": "confirm_booking", "slots": slots,
            "candidates": None, "offered_date": slots.get("date"), "retries": 0,
        }
        await _speak(session, booking_confirm_prompt(slots))
        return

    session.pending = None
    try:
        result = await _tools.book_appointment(
            slots["doctor_name"], slots["date"], slots["time_slot"],
            slots["patient_name"], slots["phone"],
        )
    except ToolCallError as e:
        logger.error("[%s] clinic API call failed: %s", session.call_id, e)
        await _speak(session, SYSTEM_UNREACHABLE_BN,
                     fallback_reason="tool_failure")
        return
    # story title: The agent says it cannot confirm rather than guessing
    # user story: As a caller, I want to be told plainly when the system
    #   cannot verify something, so that I am not given a confident guess.
    # acceptance criteria: The insufficient-verified-information outcome has
    #   its own template per language, its own metric and its own escalation
    #   path, distinct from not-found and from an infrastructure apology. Its
    #   rate is reported per intent because a rise means a data or
    #   integration problem.
    #
    # THE WRITE HAPPENED. Whether the agent can say what it did is a separate
    # question, and this is where it is asked -- after the POST returned
    # success and before a single one of its values is read aloud.
    #
    # Routed to its own outcome rather than to either neighbour, because the
    # instruction to the caller is different from both: not "that does not
    # exist", not "call back", but "it IS booked, we will follow up, do not
    # rebook". Telling them to call back here is what produces the duplicate
    # appointment.
    # story title: Near matches are offered rather than guessed or refused
    # user story: As a caller naming something loosely, I want the close
    #   matches offered, so that I am not told my test does not exist when it
    #   does.
    # acceptance criteria: When several catalogue rows fall within the match
    #   band the agent offers up to three by name and asks which. Candidates
    #   are generated across every supported language and romanised spelling.
    #   The did-you-mean path covers the ambiguous case and not only total
    #   failure.
    #
    # NO WRITE UNDER AMBIGUITY. This is the worst version of the bug the story
    # names: booking the higher-scoring of two plausible doctors leaves the
    # caller believing they have an appointment, and they do -- with someone
    # else. clinic-api refuses the write and hands back the candidates, so
    # this asks instead of reporting "no such doctor" for a doctor who exists
    # twice over.
    #
    # The booking state is NOT resumed automatically after the choice. The
    # caller picks a doctor, hears their availability, and walks the booking
    # again with the readback intact -- longer, and the only version where
    # every field is re-confirmed against the doctor they actually chose.
    if result.get("reason") == "doctor_ambiguous":
        candidates = result.get("candidates") or []
        logger.info("[%s] booking refused: %r matches %d doctors",
                    session.call_id, slots.get("doctor_name"), len(candidates))
        session.pending = {
            "awaiting": "entity_choice", "intent": "doctor_availability",
            "slots": {}, "candidates": candidates,
            "offered_date": slots.get("date"), "retries": 0,
        } if candidates else None
        await _speak(session, near_match_prompt(candidates))
        return

    unverified = outcomes.missing_booking_write_fields(result)
    if unverified:
        logger.error("[%s] booking write is unverifiable -- missing %s. The "
                     "appointment WAS created; the response did not carry it back.",
                     session.call_id, unverified)
        if _tools is not None:
            _tools.outcomes.record("book_appointment", tool_outcome.INSUFFICIENT)
        turn_log.record_insufficient(session.call_id, session.utt_seq,
                                     "book_appointment", unverified)
        await _speak(session, INSUFFICIENT_VERIFIED_INFORMATION_BN)
        return
    reply = booking_reply(slots, result, language=language)
    if result.get("success"):
        # ADDED BY CHAKRAVARDHAN -- the record this call holds no longer
        # lists everything booked -- the next read fetches it again rather
        # than reading out a stale copy.
        session.timeline_stale = True
        if from_record:
            # _t() takes session.lang's short code ("bn"/"en"/"hi"), not the
            # `language` parameter's word form ("bengali"/"english"/...)
            # used elsewhere in this function -- they are different value
            # spaces, so this deliberately does not reuse `language` here.
            reply += _t(session.lang, "timeline.used_record")
    await _speak(session, reply)


# ADDED BY SOURAV -- "Caller asks to be called back" story. Same shape as
# _finish_booking() just above: place the write and clear pending
# regardless of outcome, catch ToolCallError for the infra-apology path,
# and withhold the confirmation via missing_callback_write_fields() rather
# than ever speaking a callback_id the response didn't actually confirm
# (mirrors _finish_booking()'s own missing_booking_write_fields() check --
# see agent/outcomes.py for both). The ONLY caller is the "confirm_callback"
# branch of _continue_pending, below.
async def _finish_callback(session: CallSession, slots: dict, language: str = "bengali"):
    session.pending = None
    try:
        result = await _tools.request_callback(
            slots["phone"], slots["callback_time_window"], slots.get("callback_reason"),
        )
    except ToolCallError as e:
        logger.error("[%s] clinic API call failed: %s", session.call_id, e)
        await _speak(session, "এই মুহূর্তে দেখতে পারছি না। কাউন্টারে যোগাযোগ করুন, দয়া করে।",
                     fallback_reason="tool_failure")
        return

    if result.get("success"):
        missing = missing_callback_write_fields(result)
        if missing:
            logger.error("[%s] callback request reported success but missing %s -- withholding confirmation",
                         session.call_id, missing)
            record_insufficient_verified_information(
                intent="request_callback", field=",".join(missing),
                reason="missing_after_success", call_id=session.call_id,
            )
            # INSUFFICIENT_VERIFIED_INFORMATION_BN is Bengali-only by design
            # (see its own comment in agent/reply_templates.py) -- `language`
            # is accepted here for signature parity with the rest of this
            # story's functions but not yet threaded into this one sentence.
            await _speak(session, INSUFFICIENT_VERIFIED_INFORMATION_BN,
                         fallback_reason="insufficient_verified_information")
            return

    await _speak(session, callback_scheduled_reply(slots, result, language=language))


# ADDED BY SOURAV -- "Lab Report Status & Secure Delivery" combined story
# (previously two separate stories, "is my report ready" / "send my
# report"). These two helpers are the only NEW glue _dispatch_turn and
# _continue_pending need: every actual decision (what to say, what pending
# state comes next) lives in agent/report_flow.py's pure interpret_*()
# functions -- see that module's docstring for why. These two functions
# exist only to do the I/O those pure functions cannot do themselves:
# await the tools client, then hand the response to the right interpret_*()
# call and speak/store whatever it returns.
async def _finish_report_flow(session: CallSession, phone: str, result: dict, flow: str,
                               language: str = "bengali"):
    """Common tail for BOTH a fresh report_status/report_send lookup and a
    caller resolving a "which report?" disambiguation (see the
    "which_report" pending state below, which reconstructs a `result`
    locally from the remembered candidate list rather than re-querying).

    UPDATED BY SOURAV -- `language` is new (default "bengali", so every
    pre-existing caller of this function keeps working unchanged); see
    the module-level detect_language import comment above. Passed
    straight to agent/report_flow.py's interpret_*() functions below,
    which already accepted it -- only this call site needed updating."""
    text, pending = interpret_report_status_result(result, flow, language=language)
    if pending and pending.get("awaiting") == "__request_delivery_now__":
        # flow == "report_send" on a READY + delivery-enabled report: the
        # caller already asked for delivery, so go straight to requesting
        # an OTP rather than asking "shall I send it?" first (that offer
        # question is only for flow == "report_status").
        report_number = pending["report_number"]
        try:
            delivery_result = await _tools.request_report_delivery(phone, report_number)
        except ToolCallError as e:
            logger.error("[%s] clinic API call failed: %s", session.call_id, e)
            session.pending = None
            await _speak(session, "এই মুহূর্তে দেখতে পারছি না। কাউন্টারে যোগাযোগ করুন, দয়া করে।",
                         fallback_reason="tool_failure")
            return
        text, pending = interpret_delivery_request_result(delivery_result, report_number, language=language)
    if pending is not None:
        # phone is never something the caller re-supplies mid-flow (RULE 15
        # -- identity was already resolved) -- carry it forward on every
        # pending dict this story introduces so later states never need to
        # re-ask for it.
        pending["phone"] = phone
    session.pending = pending
    if text:
        await _speak(session, text)


async def _handle_report_lookup(session: CallSession, phone: str, test_name: str | None, flow: str,
                                 language: str = "bengali"):
    """Entry point for BOTH the report_status and report_send intents (see
    _dispatch_turn below) once a phone number is in hand, and for the
    "phone" pending state once a caller who was first asked for one gives
    it. `flow` tells report_status and report_send apart -- same lookup,
    different thing to do once a READY+enabled report is found (see
    agent/report_flow.py's interpret_report_status_result).

    UPDATED BY SOURAV -- `language` is new (default "bengali", so every
    pre-existing caller keeps working unchanged); threaded straight
    through to _finish_report_flow. See the module-level detect_language
    import comment above."""
    try:
        result = await _tools.get_report_status(phone, test_name)
    except ToolCallError as e:
        logger.error("[%s] clinic API call failed: %s", session.call_id, e)
        session.pending = None
        await _speak(session, "এই মুহূর্তে দেখতে পারছি না। কাউন্টারে যোগাযোগ করুন, দয়া করে।",
                     fallback_reason="tool_failure")
        return
    await _finish_report_flow(session, phone, result, flow, language=language)


async def _resolve_comparable_entity(name: str) -> dict:
    """ADDED BY SOURAV -- "Caller asks the agent to compare two options"
    story. agent/llm.py deliberately never classifies whether a caller-
    named term is a TEST or a PACKAGE (see its own CLINICAL SAFETY NOTE
    and the "compare_option_a"/"compare_option_b" slot rule) -- it only
    ever copies the literal span the caller said, same discipline as
    "test_name"/"package_name" elsewhere in this file. Resolving WHICH
    catalogue a name belongs to is this function's only job, done the
    same way a human clinic-desk operator would: try the test catalogue
    first (get_test_rate), and only if that comes back not-found, try the
    package catalogue (search_health_package) -- tests significantly
    outnumber packages in this clinic's catalogue, so this order resolves
    the common case (comparing two tests) in a single tool call.

    Returns the underlying tool response dict, unmodified, plus one added
    key `"kind"`: "test" | "package" | "not_found" -- read by
    agent/compare_flow.py's build_comparison() (which fields it reads
    depends on this tag) and agent/reply_templates.py's
    compare_options_reply() (which alias field to prefer). Never raises
    ToolCallError itself -- a caller of this function (the compare_options
    dispatch branch below) awaits it for BOTH sides before deciding how to
    handle a tool failure, same as every other two-tool-call intent in
    this file.
    """
    test_result = await _tools.get_test_rate(name)
    if test_result.get("found"):
        return {**test_result, "kind": "test"}
    package_result = await _tools.search_health_package(name)
    if package_result.get("found"):
        return {**package_result, "kind": "package"}
    # Neither catalogue has it -- prefer the package lookup's own
    # did_you_mean suggestions (already computed by clinic-api) since a
    # caller who names something unfamiliar in a comparison is at least as
    # likely to mean a package as a plain test; the test lookup's own
    # did_you_mean is not lost, just not the one used here, since this
    # dict only needs to say "not found", not carry both catalogues' near
    # matches.
    return {**package_result, "kind": "not_found"}


# ADDED BY SOURAV -- "Caller asks a follow-up that depends on the previous
# answer" story. Which result-dict field carries the CATALOGUE's own
# canonical name for each trackable slot -- see _remember_primary_entity()
# below for why the canonical name, not the caller's raw words, is what
# gets remembered.
_CANONICAL_NAME_FIELD = {"test_name": "test_name", "doctor_name": "doctor_name", "package_name": "package_name"}


def _session_state(session: CallSession) -> DialogueState | None:
    """ADDED BY SOURAV -- "Caller asks a follow-up that depends on the
    previous answer" story. Every REAL CallSession always has `.state`
    (see its own __init__), but a large number of EXISTING tests written
    before this story build a lightweight `types.SimpleNamespace` fake
    session instead -- with only the specific attributes that story
    needed at the time, never `.state`. Rather than retrofit `.state=...`
    into every one of those pre-existing fakes (an unrelated change to
    18+ test files this story has no reason to touch), every call site
    below goes through this helper and treats "no `.state` attribute at
    all" the same as "follow-up resolution is simply not available this
    turn" -- the exact behaviour those tests already expect and pass
    with today, completely unaffected by this story. A real caller,
    which always has `.state`, is never affected by this fallback.
    """
    return getattr(session, "state", None)


def _remember_primary_entity(session: CallSession, intent: str, slots: dict, result: dict | None) -> None:
    """Called right after a single-primary-entity intent's lookup
    returns (see agent/state.py's primary_slot_for_intent() for exactly
    which intents this applies to), successful or not.

    Only a `result.get("found")` lookup updates agent/state.py's
    DialogueState -- and even then with the CATALOGUE's own canonical
    name (e.g. clinic-api's own `test_name`), never the caller's raw
    spoken words: a later follow-up backfills THIS value straight into
    the next tool call's argument (see main.py's dispatch, right after
    resolve_follow_up()), and the canonical name is guaranteed to still
    match on that next lookup the way an ASR-mangled or partial spoken
    form is not. A not-found result intentionally changes nothing --
    nothing new was actually confirmed to exist this turn, so clobbering
    a still-valid, previously-tracked entity with a miss would make the
    NEXT follow-up resolve to nothing instead of the last real one.
    """
    state = _session_state(session)
    if state is None:
        return
    slot_key = primary_slot_for_intent(intent)
    if slot_key is None or not result or not result.get("found"):
        return
    kind = kind_for_slot(slot_key)
    if kind is None:
        return
    canonical = result.get(_CANONICAL_NAME_FIELD[slot_key]) or slots.get(slot_key)
    if canonical:
        state.mark(kind, canonical)


def _remember_compared_entities(session: CallSession, entity_a: dict, entity_b: dict) -> None:
    """Called after compare_options resolves both sides (dispatch branch
    and its "compare_options_slot" continuation below both call this).

    If both sides turned out to be the SAME kind (two tests, or two
    packages) with DIFFERENT canonical names, that kind becomes AMBIGUOUS
    for the next turn's follow-up (see agent/state.py's mark_ambiguous())
    -- a caller who just asked to compare CBC and Lipid Profile, then
    says "does IT need a prescription?", cannot honestly have either one
    guessed. If both sides are the same kind with the SAME name (a caller
    comparing a test to itself), or only one side resolved to that kind
    at all, there is only one real candidate -- mark() as usual. A
    not_found side never contributes anything to remember, same
    not-found-changes-nothing rule as _remember_primary_entity() above.
    """
    state = _session_state(session)
    if state is None:
        return
    by_kind: dict[str, list[str]] = {}
    for entity in (entity_a, entity_b):
        kind = entity.get("kind")
        if kind not in ("test", "package"):
            continue
        # "test_name" or "package_name" -- the exact field
        # _resolve_comparable_entity() tags each result with.
        name = entity.get(f"{kind}_name")
        if name:
            by_kind.setdefault(kind, []).append(name)
    for kind, names in by_kind.items():
        distinct = list(dict.fromkeys(names))  # de-dupe, order-preserved
        if len(distinct) > 1:
            state.mark_ambiguous(kind, distinct)
        else:
            state.mark(kind, distinct[0])


async def _continue_pending(session: CallSession, text: str) -> bool:
    """The fix for "appointment pipeline breaking": every turn used to be
    classified from a bare transcript with ZERO memory of the turn before
    it (see _resolve_intent / agent/llm.py's docstring -- one utterance
    in, one classification out, nothing carried over). A caller who had
    just been asked "কোন দিন চান?" and replied "আজ" produced a fresh,
    context-free classification of the single word "আজ", which the model
    has no way to recognise as a date answer -- it almost always came
    back "unclear", and the booking that was three-quarters filled a
    moment ago silently died with no record it had ever started.

    This function is the session's memory. While session.pending is set,
    EVERY turn is routed here first (see _dispatch_turn), and is
    interpreted against exactly the one field pending["awaiting"] says was
    just asked for -- using agent/slot_parse.py's local parsers, not
    another LLM call (see that module's docstring for why a fresh
    classification is the wrong tool for a reply this short). The LLM is
    not consulted again until the flow ends, one way or another.

    pending shape: {
        "awaiting": "doctor_choice" | "department_date" | "date" | "time_slot"
                    | "patient_name" | "phone" | "confirm_booking" | "confirm_correction",
        "slots": {<whatever of the 5 booking fields is already known>},
        "candidates": [{"name", "name_bn"}, ...] | None,  # only for "doctor_choice"
        "offered_date": "<iso>" | None,  # the date main.py already SPOKE to
                                          # the caller ("today", or a
                                          # next-available date) -- lets a
                                          # bare "হ্যাঁ" confirm THAT date
                                          # instead of literally "today"
        "retries": int,
        "from_record": bool,  # name/phone came from the verified record
    }

    ADDED BY SOURAV -- "Lab Report Status & Secure Delivery" combined story
    adds FOUR more "awaiting" values, handled in their own block below
    (checked BEFORE the universal "না" escape hatch, same reason
    "confirm_booking"/"confirm_correction" already are: that hatch's
    booking-specific wording would be wrong mid-report-flow, and RULE 4/9
    need their own "না means decline delivery, not abandon the call"
    wording and their own OTP-disclosure-attempt handling instead):
        "report_phone"     -- report_status/report_send asked for a phone
                               number (RULE 15, identity-by-phone-first);
                               pending also carries "flow" and "test_name".
                               NOT plain "phone" -- that string is already
                               used by the booking flow below (a caller
                               correcting a booking's phone number
                               re-enters awaiting="phone"); a shared name
                               made a correcting-a-booking's-phone-number
                               caller get routed into the report flow
                               instead (caught by test_booking_readback.py).
        "which_report"     -- RULE 13, caller has more than one report and
                               was asked which; pending carries "flow" and
                               the remembered "candidates" list.
        "confirm_delivery" -- the "shall I send it to your phone?" offer
                               after a report_status lookup found a
                               READY + delivery-enabled report (RULE 4);
                               pending carries "report_number" and "phone".
        "otp_code"         -- RULE 4-9, waiting for the caller to speak
                               back the OTP just sent; pending carries
                               "report_number" and "phone". A caller who
                               asks to be TOLD the otp instead of speaking
                               it back is refused (RULE 9 / ATTACK 8) via
                               looks_like_otp_disclosure_request(), not
                               treated as an ordinary unparseable reply.
    All four pending dicts also carry "phone" (see _finish_report_flow /
    _handle_report_lookup above) so none of these states ever needs to
    re-ask for a phone number it already resolved identity with.

    ADDED BY SOURAV -- Phase 1: Database Schema & Policy Tables adds TWO
    more "awaiting" values:
        "billing_phone"          -- billing_balance asked for a phone
                                     number (RULE 14/15, same as
                                     "report_phone" above). Own distinct
                                     string for the same reason
                                     "report_phone" isn't just "phone".
        "insurance_coverage_slot" -- insurance_coverage is missing
                                     test_name and/or insurance_provider_
                                     name; pending also carries "slots"
                                     (whichever of the two is already
                                     known) and "missing_field" (which one
                                     this turn's reply fills). Unlike
                                     phone/date/time_slot, both fields are
                                     free-text named entities with no
                                     local grammar -- the caller's
                                     utterance is accepted verbatim for
                                     whichever field is missing, same as
                                     agent/llm.py's own extraction rule
                                     for these two slots ("copy the term
                                     as said, do not normalize").

    ADDED BY SOURAV -- "Caller asks something the agent does not cover"
    story adds ONE more "awaiting" value:
        "out_of_scope_choice" -- the caller was offered a choice (connect
                                  to a human, or contact the counter
                                  themselves) after an "out_of_scope"
                                  intent; carries no lookup state at all
                                  (unlike confirm_delivery above, there is
                                  no report_number/phone to remember), just
                                  "retries", same yes/no/unparseable shape
                                  as confirm_delivery.

    ADDED BY SOURAV -- "Caller asks the agent to compare two options"
    story adds ONE more "awaiting" value:
        "compare_options_slot" -- compare_options is missing
                                   compare_option_a and/or compare_option_b;
                                   pending also carries "slots" (whichever
                                   of the two is already known) and
                                   "missing_field" (which one this turn's
                                   reply fills). Same free-text, no-local-
                                   grammar, accept-verbatim shape as
                                   "insurance_coverage_slot" above, for the
                                   identical reason -- neither slot has a
                                   local grammar to parse against, and
                                   agent/llm.py never classifies which one
                                   is a test versus a package anyway (that
                                   happens downstream, in
                                   _resolve_comparable_entity(), only once
                                   both names are in hand).

    ADDED BY SOURAV -- "Caller asks a follow-up that depends on the
    previous answer" story adds ONE more "awaiting" value:
        "follow_up_clarification" -- a brand-new question's primary
                                       entity slot (test/doctor/package)
                                       was left null AND agent/state.py's
                                       tracked state for that kind was
                                       AMBIGUOUS (2+ different entities
                                       discussed a moment ago -- see that
                                       module's own docstring); pending
                                       carries "intent" and "slots" (the
                                       question to RESUME, exactly as
                                       extracted, once the ambiguity is
                                       resolved), "kind", and "candidates"
                                       (the names ambiguous_reference_
                                       reply() just spoke). Unlike every
                                       other pending state above, this one
                                       does not ask for a brand-new piece
                                       of information the caller never
                                       gave -- it asks them to pick which
                                       of two things they ALREADY said a
                                       moment ago they meant, so the reply
                                       is matched against `candidates`
                                       (via _match_candidate_name(), same
                                       trust model as _match_candidate_
                                       doctor()/agent/report_flow.py's
                                       match_candidate_report()) rather
                                       than accepted verbatim the way
                                       insurance_coverage_slot/compare_
                                       options_slot's free-text fields
                                       are -- a stray word or two around
                                       the real name should not silently
                                       fail to match.

    ADDED BY SOURAV -- "Caller asks to be called back" story adds THREE
    more "awaiting" values:
        "callback_time_window" -- request_callback is missing a time
                                   window; free-text, accepted verbatim
                                   (same as insurance_coverage_slot/
                                   compare_options_slot above -- a time
                                   window like "this evening" has no local
                                   grammar to parse against).
        "callback_phone"       -- request_callback is missing a phone
                                   number. NOT plain "phone" -- same
                                   collision reasoning as "report_phone"/
                                   "billing_phone" above, since the
                                   booking flow's own shared tail further
                                   down already owns bare "phone".
        "confirm_callback"     -- both fields are known; the caller is
                                   read back the number and time window
                                   and asked to confirm before the write
                                   (Answer Quality and Grounding, same
                                   shape as "confirm_booking" above).
    All three carry "slots" (whatever of "callback_time_window"/"phone"/
    "callback_reason" is already known) -- see agent/callback_flow.py's
    own module docstring for the availability check that runs BEFORE any
    of these three states is ever entered, and _next_missing_callback()
    just above _continue_pending's own definition for the field order.

    ADDED BY CHAKRAVARDHAN -- two further states belong to verification, not
    booking: "history_verify" (answering the challenge) and "record_phone"
    (saying which number the record is under). Both carry "purpose" --
    "history" or "bookings" -- which is what gets read out once the caller
    is verified.

    Returns True when the turn was fully handled here (caller must not
    also run intent extraction on top of it); False to fall through to
    the normal pipeline -- either because there was no pending flow, or
    because this one gave up on it after repeated unparseable replies.
    """
    pending = session.pending
    if pending is None:
        return False

    # ADDED BY SOURAV -- see the module-level detect_language import
    # comment above for the real bug this fixes. Detected fresh from
    # THIS turn's own utterance (not carried over from an earlier turn,
    # and not stored on `pending`) -- a caller can code-switch mid-call,
    # and every reply below should reflect what they just said, not what
    # they said several turns ago when the flow started.
    language = detect_language(text)

    awaiting = pending["awaiting"]

    # ADDED BY CHAKRAVARDHAN -- verification owns its turn completely, and is
    # checked BEFORE the negative escape hatch below. A caller answering a
    # PIN challenge with a digit string that happens to parse as "na" must
    # not silently abandon the flow, and more importantly a wrong answer
    # must burn an attempt rather than being reinterpreted as a polite
    # refusal.
    if awaiting == "history_verify":
        return await _continue_history_verification(session, text)

    # The caller was asked which number their record is under (see
    # _start_history_verification). Handled before the escape hatch below,
    # whose wording is about abandoning a BOOKING.
    # MIXED-LANGUAGE SPEECH -- Author: Chakravardhan. From here on each local
    # parser reads the caller's own words first, and a code_mix view of them
    # ("kal", "saat baje", "nine eight double zero", "nahi") only if that
    # found nothing -- see code_mix.first_parse. Verification (above) and the
    # patient's name (below) are never passed through it.
    if awaiting == "record_phone":
        purpose = pending.get("purpose") or PURPOSE_HISTORY
        if code_mix.first_parse(is_negative, text):
            session.pending = None
            await _speak(session, _t(session.lang, "fallback.greeting"))
            return True
        phone = code_mix.first_parse(parse_phone, text)
        _audit(session).slots("slot_parse", {"phone": phone}, awaiting="record_phone")
        if phone is None:
            pending["retries"] += 1
            if pending["retries"] > 2:
                session.pending = None
                return False
            await _speak(session, missing_slot_prompt(pending["flow"], "phone", language=language))
            return True
        await _handle_report_lookup(session, phone, pending.get("test_name"), pending["flow"],
                                     language=language)
        return True

    # NOTE: confirm_booking/confirm_correction are handled below, together
    # with the universal "না" escape hatch -- see that hatch's own comment
    # for why confirm_correction is deliberately NOT excluded from it (a
    # considered divergence from dev_sourav, where both states skip the
    # hatch and are handled in an earlier, language-aware pair of branches
    # here instead). That earlier pair duplicated this behaviour with a
    # regression -- it never reproduced the immediate-abandon-on-"না"
    # behaviour tests/test_booking_readback.py pins for confirm_correction
    # -- so it was removed rather than kept as a second, unreachable
    # implementation; its one real improvement, threading `language`
    # through the replies below, was folded into the surviving branches
    # instead.

    # ADDED BY SOURAV -- report_status/report_send combined story's four new
    # pending states. Checked here, BEFORE the universal booking escape
    # hatch just below, for the same reason "confirm_booking"/
    # "confirm_correction" already are (see this function's docstring):
    # a "না" here means something specific to a report flow, not
    # "abandon the appointment" (there is no appointment in this flow).
    #
    # "report_phone", NOT "phone": the pre-existing booking flow already
    # uses the bare string "phone" as an awaiting value (see the shared
    # date/time_slot/patient_name/phone tail further down, and its
    # "confirm_correction" re-entry) -- a caller correcting a BOOKING's
    # phone number was briefly being routed into this report-flow handler
    # instead, because this check ran first and matched on the same
    # string. Caught by test_booking_readback.py's correction-round-trip
    # test failing once these states were wired in. See
    # agent/report_flow.py's AWAITING_REPORT_PHONE comment for the same
    # note from the other side of this collision.
    if awaiting == "report_phone":
        if is_negative(text):
            session.pending = None
            await _speak(session, "ঠিক আছে, তাহলে থাক। আর কিছু জানতে চান?")
            return True
        phone = parse_phone(text)
        if phone is None:
            pending["retries"] += 1
            if pending["retries"] > 2:
                session.pending = None
                return False
            await _speak(session, missing_slot_prompt(pending["flow"], "phone", language=language))
            return True
        await _handle_report_lookup(session, phone, pending.get("test_name"), pending["flow"],
                                     language=language)
        return True

    # ADDED BY SOURAV -- Phase 1: Database Schema & Policy Tables.
    # Outstanding Balance / Billing story. Own distinct string, NOT
    # "phone" or "report_phone" -- same collision reasoning as
    # "report_phone" above: a caller correcting a BOOKING's phone number,
    # or resuming a report flow's phone ask, must never be routed here
    # instead just because the bare string matched.
    if awaiting == "billing_phone":
        if is_negative(text):
            session.pending = None
            await _speak(session, "ঠিক আছে, তাহলে থাক। আর কিছু জানতে চান?")
            return True
        phone = parse_phone(text)
        if phone is None:
            pending["retries"] += 1
            if pending["retries"] > 2:
                session.pending = None
                return False
            await _speak(session, missing_slot_prompt("billing_balance", "phone", language=language))
            return True
        # FIXED BY SOURAV -- Phase 2 end-to-end testing caught a real bug
        # here: this branch spoke the real answer but never cleared
        # session.pending, so the call stayed stuck in "awaiting a phone
        # number" afterward -- the caller's NEXT utterance, whatever it
        # was, would have been misinterpreted as another phone attempt
        # instead of a fresh question. Must be cleared BEFORE the tool
        # call, same ordering as the insurance_coverage_slot branch below,
        # so a slow/failing tool call never leaves pending in a stale
        # state either.
        session.pending = None
        result = await _tools.get_patient_billing(phone)
        await _speak(session, billing_balance_reply(result, language=language))
        return True

    # ADDED BY SOURAV -- Phase 1: Insurance Coverage Policy story. Unlike
    # phone/date/time_slot, "test_name" and "insurance_provider_name" are
    # free-text named entities with no local grammar to parse (per agent/
    # llm.py's own slot rule: copy the term as said, do not normalize --
    # the actual alias/fuzzy matching happens downstream in clinic-api's
    # _find_lab_test()/_find_insurance_provider()), so accepting the
    # caller's utterance verbatim for whichever field pending["missing_
    # field"] names IS the correct local equivalent of the LLM's own
    # extraction rule for these two slots, not a shortcut.
    if awaiting == "insurance_coverage_slot":
        if is_negative(text):
            session.pending = None
            await _speak(session, "ঠিক আছে, তাহলে থাক। আর কিছু জানতে চান?")
            return True
        value = text.strip()
        if not value:
            pending["retries"] += 1
            if pending["retries"] > 2:
                session.pending = None
                return False
            await _speak(session, missing_slot_prompt("insurance_coverage", pending["missing_field"],
                                                        language=language))
            return True
        pending["slots"][pending["missing_field"]] = value
        pending["retries"] = 0
        still_missing = next(
            (f for f in ("test_name", "insurance_provider_name") if not pending["slots"].get(f)), None,
        )
        if still_missing:
            pending["missing_field"] = still_missing
            await _speak(session, missing_slot_prompt("insurance_coverage", still_missing, language=language))
            return True
        final_slots = pending["slots"]
        session.pending = None
        result = await _tools.get_insurance_coverage(
            final_slots["test_name"], final_slots["insurance_provider_name"],
        )
        await _speak(session, insurance_coverage_reply(final_slots, result, language=language))
        return True

    if awaiting == "compare_options_slot":
        # Mirrors "insurance_coverage_slot" immediately above exactly --
        # same two-free-text-slots shape, same accept-verbatim discipline
        # (agent/llm.py never classifies test-vs-package for these two
        # slots either; see this file's own _resolve_comparable_entity()
        # for where that resolution actually happens, only once both
        # names are known).
        if is_negative(text):
            session.pending = None
            await _speak(session, "ঠিক আছে, তাহলে থাক। আর কিছু জানতে চান?")
            return True
        value = text.strip()
        if not value:
            pending["retries"] += 1
            if pending["retries"] > 2:
                session.pending = None
                return False
            await _speak(session, missing_slot_prompt("compare_options", pending["missing_field"],
                                                        language=language))
            return True
        pending["slots"][pending["missing_field"]] = value
        pending["retries"] = 0
        still_missing = next(
            (f for f in ("compare_option_a", "compare_option_b") if not pending["slots"].get(f)), None,
        )
        if still_missing:
            pending["missing_field"] = still_missing
            await _speak(session, missing_slot_prompt("compare_options", still_missing, language=language))
            return True
        final_slots = pending["slots"]
        session.pending = None
        name_a, name_b = final_slots["compare_option_a"], final_slots["compare_option_b"]
        entity_a, entity_b = await _resolve_comparable_entity(name_a), await _resolve_comparable_entity(name_b)
        comparison = build_comparison(entity_a, entity_b)
        await _speak(session, compare_options_reply(name_a, name_b, entity_a, entity_b, comparison, language=language))
        _remember_compared_entities(session, entity_a, entity_b)
        return True

    if awaiting == "follow_up_clarification":
        # ADDED BY SOURAV -- "Caller asks a follow-up that depends on the
        # previous answer" story. Unlike every OTHER pending state above,
        # this is not asking for a brand-new piece of information -- it
        # is asking the caller to pick which of two things they ALREADY
        # named a moment ago they meant (see agent/state.py's own
        # docstring on ambiguity, and this function's docstring above),
        # so the reply is matched against the small `candidates` list
        # rather than accepted verbatim.
        if is_negative(text):
            session.pending = None
            await _speak(session, "ঠিক আছে, তাহলে থাক। আর কিছু জানতে চান?")
            return True
        matched = _match_candidate_name(text, pending["candidates"])
        if not matched:
            pending["retries"] += 1
            if pending["retries"] > 2:
                session.pending = None
                return False
            await _speak(session, ambiguous_reference_reply(pending["kind"], pending["candidates"], language=language))
            return True
        session.pending = None
        # The caller just resolved the ambiguity themselves -- collapsing
        # the tracked state back down to this one name means the NEXT
        # follow-up (AC3's "three consecutive follow-ups without
        # re-prompting") does not need to ask again.
        state = _session_state(session)
        if state is not None:
            state.mark(pending["kind"], matched)
        resolved_slots = dict(pending["slots"])
        resolved_slots[primary_slot_for_intent(pending["intent"])] = matched
        # Reuses the exact same per-intent lookup-and-reply logic a solo
        # turn for this intent would use -- see that function's own
        # docstring; every intent agent/state.py can ever produce an
        # ambiguous_kind for is one _resolve_combinable_intent_fragment
        # already knows how to answer.
        reply = await _resolve_combinable_intent_fragment(pending["intent"], resolved_slots, language)
        if reply:
            await _speak(session, reply)
        return True

    # ADDED BY SOURAV -- "Caller asks to be called back" story. Three new
    # scoped states: "callback_time_window" and "callback_phone" (NOT
    # "phone" -- same collision reasoning as "report_phone"/"billing_phone"
    # above, since the pre-existing booking flow's own shared tail further
    # down already treats bare "phone" as ITS awaiting value) collect the
    # two fields this story needs, then "confirm_callback" reads them back
    # before the write -- same "every critical value is read back before
    # it is used" discipline as "confirm_booking" above.
    if awaiting == "callback_time_window":
        if is_negative(text):
            session.pending = None
            await _speak(session, "ঠিক আছে, তাহলে থাক। আর কিছু জানতে চান?")
            return True
        window = text.strip()
        if not window:
            pending["retries"] += 1
            if pending["retries"] > 2:
                session.pending = None
                return False
            await _speak(session, missing_slot_prompt("request_callback", "callback_time_window", language=language))
            return True
        pending["slots"]["callback_time_window"] = window
        pending["retries"] = 0
        missing = _next_missing_callback(pending["slots"])
        if missing is None:
            pending["awaiting"] = "confirm_callback"
            await _speak(session, callback_confirmation_prompt(pending["slots"], language=language))
            return True
        pending["awaiting"] = missing
        await _speak(session, missing_slot_prompt("request_callback", missing, language=language))
        return True

    if awaiting == "callback_phone":
        if is_negative(text):
            session.pending = None
            await _speak(session, "ঠিক আছে, তাহলে থাক। আর কিছু জানতে চান?")
            return True
        phone = parse_phone(text)
        if phone is None:
            pending["retries"] += 1
            if pending["retries"] > 2:
                session.pending = None
                return False
            await _speak(session, missing_slot_prompt("request_callback", "callback_phone", language=language))
            return True
        pending["slots"]["phone"] = phone
        pending["retries"] = 0
        missing = _next_missing_callback(pending["slots"])
        if missing is None:
            pending["awaiting"] = "confirm_callback"
            await _speak(session, callback_confirmation_prompt(pending["slots"], language=language))
            return True
        pending["awaiting"] = missing
        await _speak(session, missing_slot_prompt("request_callback", missing, language=language))
        return True

    if awaiting == "confirm_callback":
        if is_affirmative(text):
            await _finish_callback(session, pending["slots"], language=language)
            return True
        if is_negative(text):
            session.pending = None
            await _speak(session, "ঠিক আছে, তাহলে থাক। আর কিছু জানতে চান?")
            return True
        # Neither a clear yes nor a clear no -- bounded retries of the
        # SAME confirmation, same posture as "confirm_booking" above.
        pending["retries"] += 1
        if pending["retries"] > 2:
            session.pending = None
            return False
        await _speak(session, callback_confirmation_prompt(pending["slots"], language=language))
        return True

    if awaiting == "which_report":
        if is_negative(text):
            session.pending = None
            await _speak(session, "ঠিক আছে, তাহলে থাক। আর কিছু জানতে চান?")
            return True
        candidates = pending.get("candidates") or []
        report_number = match_candidate_report(text, candidates)
        if report_number is None:
            pending["retries"] += 1
            if pending["retries"] > 2:
                session.pending = None
                return False
            await _speak(session, "দুঃখিত, কোন টেস্টের রিপোর্টের কথা বলছেন, আরেকটু স্পষ্ট করে বলবেন?")
            return True
        # Reconstruct a report_status-shaped result LOCALLY from the
        # candidate the caller just picked, rather than re-querying
        # clinic-api a second time -- the candidate list came from that
        # same lookup moments ago and already carries every field
        # interpret_report_status_result needs (status, delivery_enabled,
        # report_number, test_name). Considered tradeoff, not an oversight:
        # a report's status could in principle change in the few seconds
        # between the ambiguous listing and this answer, same as any
        # read-then-act gap; request_report_delivery() re-checks
        # eligibility server-side regardless (RULE 3/16 defense in depth),
        # so this can never cause an unauthorized delivery, only a stale
        # status read in an already-rare multi-report case.
        chosen = next(c for c in candidates if c["report_number"] == report_number)
        result = {"patient_found": True, "found": True, **chosen}
        await _finish_report_flow(session, pending.get("phone"), result, pending["flow"],
                                   language=language)
        return True

    if awaiting == "confirm_delivery":
        if is_affirmative(text):
            phone = pending.get("phone")
            report_number = pending["report_number"]
            try:
                delivery_result = await _tools.request_report_delivery(phone, report_number)
            except ToolCallError as e:
                logger.error("[%s] clinic API call failed: %s", session.call_id, e)
                session.pending = None
                await _speak(session, "এই মুহূর্তে দেখতে পারছি না। কাউন্টারে যোগাযোগ করুন, দয়া করে।",
                             fallback_reason="tool_failure")
                return True
            text_out, new_pending = interpret_delivery_request_result(
                delivery_result, report_number, language=language)
            if new_pending is not None:
                new_pending["phone"] = phone
            session.pending = new_pending
            await _speak(session, text_out)
            return True
        if is_negative(text):
            session.pending = None
            await _speak(session, delivery_declined_reply(language=language))
            return True
        pending["retries"] += 1
        if pending["retries"] > 2:
            session.pending = None
            return False
        await _speak(session, "রিপোর্টটা কি আপনার ফোনে পাঠাব?")
        return True

    if awaiting == "otp_code":
        if is_negative(text):
            session.pending = None
            await _speak(session, delivery_declined_reply(language=language))
            return True
        otp = parse_otp(text)
        if otp is None:
            # RULE 9 / ATTACK 8: checked only AFTER parse_otp() already
            # failed on this same utterance, so "the otp is 482913" (which
            # DOES contain the word "otp" but is also a valid code) is
            # handled as a normal OTP attempt above, never misclassified
            # as a disclosure request.
            if looks_like_otp_disclosure_request(text):
                await _speak(session, otp_disclosure_refusal_reply(language=language))
                return True
            pending["retries"] += 1
            if pending["retries"] > 2:
                session.pending = None
                return False
            await _speak(session, "দুঃখিত, ওটিপিটা ঠিকমতো বুঝতে পারিনি, আবার বলবেন?")
            return True
        phone = pending.get("phone")
        report_number = pending["report_number"]
        try:
            result = await _tools.verify_report_otp(phone, report_number, otp)
        except ToolCallError as e:
            logger.error("[%s] clinic API call failed: %s", session.call_id, e)
            session.pending = None
            await _speak(session, "এই মুহূর্তে দেখতে পারছি না। কাউন্টারে যোগাযোগ করুন, দয়া করে।",
                         fallback_reason="tool_failure")
            return True
        text_out, new_pending = interpret_otp_verify_result(result, report_number, language=language)
        if new_pending is not None:
            new_pending["phone"] = phone
        session.pending = new_pending
        await _speak(session, text_out)
        return True

    # ADDED BY SOURAV -- "Caller asks something the agent does not cover"
    # story. Checked BEFORE the universal "না" escape hatch just below,
    # same reason confirm_booking/confirm_delivery/etc. all are: that
    # hatch's fixed "appointment bad thak" wording would be wrong here (no
    # appointment was ever in progress), and a plain "না" in this state
    # means "no, I'll contact the counter myself" -- a real, distinct
    # answer with its own reply, not an abandonment.
    if awaiting == "out_of_scope_choice":
        if is_affirmative(text):
            session.pending = None
            # Same honest "logged for a human, no real transfer capability"
            # handling as the "unclear" intent branch above -- see
            # agent/outcomes.record_human_handoff()'s own docstring.
            # intent="out_of_scope" here (not "unclear") so the two stay
            # distinguishable in the shared escalation ledger.
            record_human_handoff("out_of_scope", call_id=session.call_id)
            await _speak(session, human_fallback_reply(language=language))
            return True
        if is_negative(text):
            session.pending = None
            await _speak(session, out_of_scope_counter_reply(language=language))
            return True
        pending["retries"] += 1
        if pending["retries"] > 2:
            session.pending = None
            return False  # give a fresh LLM classification a chance instead
        await _speak(session, out_of_scope_reply(language=language))
        return True

    # Universal escape hatch, checked before any field-specific parsing:
    # a caller mid-flow who says "না" / "থাক" is abandoning the booking,
    # not answering whichever question was pending.
    # story title: Every critical value is read back before it is used
    # user story: As a patient giving a phone number, I want it read back, so
    #   that a misheard digit does not send my report to a stranger.
    # acceptance criteria: Phone numbers, dates, times and names are confirmed
    #   aloud before any write, and a rejection opens a correction path rather
    #   than repeating the prompt. Readback is mandatory regardless of
    #   confidence for values that affect a write.
    #
    # The hatch now SKIPS confirm_booking, and that exclusion is the story.
    # A "no" answering "did I get this right?" does not mean "cancel my
    # appointment" -- it means one of the five values is wrong. Letting the
    # universal hatch see it first threw the whole booking away at the exact
    # moment the caller was trying to repair it.
    #
    # confirm_correction is deliberately NOT excluded, which is a considered
    # divergence from dev_sourav, where both states skip the hatch. By the time
    # the agent has asked "which one should I fix -- doctor, date, time, name,
    # or phone?", a caller answering "no" is not naming a field; the likeliest
    # reading is that they have given up, and that is what the hatch does.
    #
    # ADDED BY CHAKRAVARDHAN -- code_mix.first_parse() reads the caller's own
    # words first and only falls back to a code-mixed view (Hindi/English
    # "nahi") if that found nothing, same MIXED-LANGUAGE SPEECH discipline
    # used above; the audit call records the abandon as a slot-parse intent
    # tagged with which flow it interrupted.
    if code_mix.first_parse(is_negative, text) and awaiting != "confirm_booking":
        _audit(session).intent("abandon_flow", "slot_parse", flow=awaiting)
        session.pending = None
        await _speak(session, "ঠিক আছে, অ্যাপয়েন্টমেন্ট বাদ থাক। আর কিছু জানতে চান?")
        return True

    if awaiting == "confirm_booking":
        # The whole booking has been read back to the caller and this turn is
        # their answer. ONLY an explicit affirmative writes.
        #
        # The negative case never reaches here -- is_negative() above already
        # cancels the flow, which is the correct outcome for "না" / "থাক".
        # Everything that is neither yes nor no falls through to a re-ask:
        # silence, a restatement of the details, a half-heard grunt. None of
        # those are consent, and treating an ambiguous reply as one would give
        # back exactly the guess-becomes-a-booking failure this state exists to
        # prevent.
        if is_affirmative(text):
            slots = pending["slots"]
            session.pending = None
            logger.info("[%s] booking confirmed by caller", session.call_id)
            await _finish_booking(session, slots, confirmed=True, language=language)
            return True

        if is_negative(text):
            # THE CORRECTION PATH. Before this, a rejection re-asked "just say
            # yes or no" twice and then abandoned the booking: the caller said
            # something was wrong and the agent's reply was to ask the same
            # question again, then hang up on it. The criterion names that
            # exact behaviour as the thing not to do.
            #
            # Nothing is discarded -- the four correct values stay in
            # pending["slots"], and only the named one is re-collected.
            pending["awaiting"] = "confirm_correction"
            pending["retries"] = 0
            logger.info("[%s] readback rejected -- opening the correction path",
                        session.call_id)
            await _speak(session, booking_correction_prompt(language=language))
            return True

        pending["retries"] += 1
        if pending["retries"] > 2:
            # Three unclear answers to a yes/no question is a handoff, not a
            # fourth attempt. Nothing has been written, and saying so plainly
            # is a B-grade outcome; looping again would trend towards D.
            session.pending = None
            logger.info("[%s] booking abandoned -- no clear confirmation", session.call_id)
            await _speak(session, BOOKING_NOT_CONFIRMED_BN)
            return True
        await _speak(session, "শুধু বলুন — হ্যাঁ, নাকি না?")
        return True

    # story title: Every critical value is read back before it is used
    # user story: As a patient giving a phone number, I want it read back, so
    #   that a misheard digit does not send my report to a stranger.
    # acceptance criteria: Phone numbers, dates, times and names are confirmed
    #   aloud before any write, and a rejection opens a correction path rather
    #   than repeating the prompt. Readback is mandatory regardless of
    #   confidence for values that affect a write.
    #
    # The caller rejected the readback and has been asked which single value
    # is wrong. This turn is that answer.
    #
    # Re-collecting ONE field and returning to the readback is what makes this
    # a correction rather than a restart: the tail of this function fills the
    # named field, finds nothing missing, and routes straight back to
    # confirm_booking -- so the corrected booking is read back IN FULL and
    # still needs an explicit affirmative. A correction never shortens the
    # path to the write.
    if awaiting == "confirm_correction":
        field = parse_correction_field(text)
        if field is None:
            pending["retries"] += 1
            if pending["retries"] > 2:
                session.pending = None
                logger.info("[%s] correction abandoned -- no field named",
                            session.call_id)
                await _speak(session, BOOKING_NOT_CONFIRMED_BN)
                return True
            await _speak(session, booking_correction_prompt(language=language))
            return True

        logger.info("[%s] correcting %s", session.call_id, field)
        pending["awaiting"] = field
        pending["retries"] = 0
        await _speak(session, missing_slot_prompt("book_appointment", field, language=language))
        return True

    # story title: Near matches are offered rather than guessed or refused
    # user story: As a caller naming something loosely, I want the close
    #   matches offered, so that I am not told my test does not exist when it
    #   does.
    # acceptance criteria: When several catalogue rows fall within the match
    #   band the agent offers up to three by name and asks which. Candidates
    #   are generated across every supported language and romanised spelling.
    #   The did-you-mean path covers the ambiguous case and not only total
    #   failure.
    #
    # The turn after an offer. The caller has been read up to three names and
    # has said one of them; this resolves which, then re-runs the SAME lookup
    # against the canonical name rather than against their words -- so the
    # second attempt cannot be ambiguous for the same reason the first was.
    #
    # Separate from doctor_choice, which looks nearly identical and is not:
    # that state follows a department LISTING, where every candidate is a
    # correct answer and the caller is choosing who to see. Here the
    # candidates are competing readings of one thing the caller already said,
    # and only one of them is what they meant.
    if awaiting == "entity_choice":
        intent = pending.get("intent")
        chosen = _match_offered(text, pending.get("candidates") or [])
        if chosen is None:
            pending["retries"] += 1
            if pending["retries"] > 2:
                # Two failed attempts at the same choice. Drop the state and
                # let a fresh classification try, rather than asking a third
                # time -- a repair ladder with no end is its own bad outcome,
                # the same cap doctor_choice uses.
                session.pending = None
                return False
            await _speak(session, NEAR_MATCH_UNCLEAR_BN)
            return True

        date_iso = pending.get("offered_date")
        session.pending = None
        logger.info("[%s] %s disambiguated to %r", session.call_id, intent,
                    chosen.get("name"))

        try:
            if intent == "test_rate":
                result = await _tools.get_test_rate(chosen["name"])
            elif intent == "doctor_availability":
                result = await _tools.get_doctor_availability(
                    chosen["name"], date_iso or datetime.date.today().isoformat())
            else:
                result = await _tools.get_doctors_by_department(chosen["name"], date_iso)
        except ToolCallError as e:
            logger.error("[%s] clinic API call failed: %s", session.call_id, e)
            await _speak(session, SYSTEM_UNREACHABLE_BN,
                         fallback_reason="tool_failure")
            return True

        # Three near-identical calls rather than one over a `reply` local,
        # and deliberately so: tests/test_fact_provenance.py requires the
        # sentence reaching _speak_fact to be a reply_templates CALL at the
        # call site, not a name that a local could have been reassigned to.
        # Collapsing these three would pass a variable and blind that gate --
        # which is the regression it exists to catch, so the duplication is
        # the cheaper half of the trade.
        #
        # A canonical name resolving to another ambiguity would mean two rows
        # share a name outright, a catalogue defect rather than a caller one.
        # _speak_fact would offer again and return True; session.pending was
        # cleared above, so it asks once and stops rather than looping.
        spoken_name = chosen.get("name_bn") or chosen["name"]
        if intent == "test_rate":
            asked = {"test_name": spoken_name}
            if await _speak_fact(session, intent, asked, result,
                                 test_rate_reply(asked, result),
                                 offered_date=date_iso):
                return True
        elif intent == "doctor_availability":
            asked = {"doctor_name": spoken_name}
            if await _speak_fact(session, intent, asked, result,
                                 doctor_availability_reply(asked, result),
                                 offered_date=date_iso):
                return True
        else:
            asked = {"department": spoken_name}
            if await _speak_fact(session, intent, asked, result,
                                 doctors_by_department_reply(asked, result),
                                 offered_date=date_iso):
                return True
        return True

    if awaiting == "doctor_choice":
        # ADDED BY CHAKRAVARDHAN's merge: code_mix.first_match() wraps
        # _match_offered() (dev_chakravardhan's own branch called this
        # _match_candidate_doctor(), which is not defined anywhere in this
        # file -- _match_offered() at line 1424 is the real, already-used
        # function), and the audit call records the parsed slot.
        match = code_mix.first_match(_match_offered, text, pending.get("candidates") or [])
        _audit(session).slots("slot_parse", {"doctor_name": match}, awaiting="doctor_choice")
        if match is None:
            pending["retries"] += 1
            if pending["retries"] > 2:
                session.pending = None
                return False  # give a fresh LLM classification a chance instead
            await _speak(session, "দুঃখিত, ডাক্তারের নামটা একটু স্পষ্ট করে বলবেন?")
            return True

        date_iso = pending.get("offered_date") or datetime.date.today().isoformat()
        try:
            result = await _tools.get_doctor_availability(match["name"], date_iso)
        except ToolCallError as e:
            logger.error("[%s] clinic API call failed: %s", session.call_id, e)
            session.pending = None
            await _speak(session, SYSTEM_UNREACHABLE_BN,
                         fallback_reason="tool_failure")
            return True

        # story title: The same question gets the same answer within one call
        # user story: As a caller who asks twice, I want the same answer, so that
        #   I know which one to believe.
        # acceptance criteria: Repeating a question in one call produces an
        #   identical factual answer unless the underlying data changed, in which
        #   case the change is stated. A test asserts consistency across three
        #   repeats with an unchanged backend.
        #
        # Every factual reply goes through _speak_fact rather than _speak, so the
        # consistency check cannot be forgotten on one route. The reply itself is
        # still rendered here, fresh, from this turn's live clinic response.
        asked = {"doctor_name": match.get("name_bn") or match["name"]}
        if await _speak_fact(session, "doctor_availability", asked, result,
                             doctor_availability_reply(asked, result, language=language),
                             offered_date=date_iso):
            return True

        offered = None
        if result.get("found"):
            offered = result.get("date") if result.get("available") else result.get("next_available_date")
        if offered:
            # doctor_availability_reply() just asked "today or another
            # day" (or, if not available today, "want that next date
            # instead?") -- stay in the flow so the caller's answer to
            # THAT question is picked up as the "date" field next.
            session.pending = {
                "awaiting": "date",
                # doctor_name is the canonical English label the booking API
                # needs; doctor_name_bn is the one that gets SPOKEN in the
                # readback. Both are carried from here on -- see
                # booking_confirm_prompt for what happens when they are not.
                "slots": {
                    "doctor_name": result.get("doctor_name") or match["name"],
                    "doctor_name_bn": result.get("doctor_name_bn") or match.get("name_bn"),
                },
                "candidates": None, "offered_date": offered, "retries": 0,
            }
        else:
            session.pending = None
        return True

    if awaiting == "confirm_date":
        # story title: The model never originates a fact
        # user story: As a clinical lead, I want every price, date and identifier
        #   to come from a verified system response, so that a wrong answer is a
        #   data bug rather than a model bug.
        # acceptance criteria: Every factual sentence is a template substitution
        #   from a validated tool response and the model is never shown a figure
        #   it could restate. An automated assertion on every commit proves no
        #   model-composed span reaches synthesis on a factual intent.
        #
        # The caller said something covering several days ("আগামী সপ্তাহে"),
        # date_calc worked out which days those are, and the agent read the
        # range back. This turn is the answer.
        #
        # হ্যাঁ  -> look up the FIRST day of the range. clinic-api answers
        #          "in that day?" and, when not, "next available day" computed
        #          from its own schedule table -- which is the honest answer to
        #          "is Dr Sen in next week?" and needs no new API surface.
        # anything else -> the range was wrong, so ask for one specific day.
        #          Deliberately NOT a re-ask of the same question: the caller
        #          already said no to it once.
        start = pending.get("offered_date")
        resume = pending.get("resume_intent")
        if is_affirmative(text) and start and resume:
            if resume == "doctor_availability":
                doctor_name = pending["slots"]["doctor_name"]
                try:
                    result = await _tools.get_doctor_availability(doctor_name, start)
                except ToolCallError as e:
                    logger.error("[%s] clinic API call failed: %s", session.call_id, e)
                    session.pending = None
                    await _speak(session, SYSTEM_UNREACHABLE_BN,
                                 fallback_reason="tool_failure")
                    return True
                asked = {"doctor_name": doctor_name}
                if await _speak_fact(session, "doctor_availability", asked, result,
                                     doctor_availability_reply(asked, result),
                                     offered_date=start):
                    return True
                offered = None
                if result.get("found"):
                    offered = result.get("date") if result.get("available") else result.get("next_available_date")
                session.pending = {
                    "awaiting": "date",
                    "slots": {
                        "doctor_name": result.get("doctor_name") or doctor_name,
                        "doctor_name_bn": result.get("doctor_name_bn"),
                    },
                    "candidates": None, "offered_date": offered, "retries": 0,
                } if offered else None
                return True

            department = pending["slots"]["department"]
            try:
                result = await _tools.get_doctors_by_department(department, start)
            except ToolCallError as e:
                logger.error("[%s] clinic API call failed: %s", session.call_id, e)
                session.pending = None
                await _speak(session, SYSTEM_UNREACHABLE_BN,
                             fallback_reason="tool_failure")
                return True
            asked = {"department": department}
            if await _speak_fact(session, "doctors_by_department", asked, result,
                                 doctors_by_department_reply(asked, result),
                                 offered_date=start):
                return True
            if result.get("found") and result.get("doctors"):
                session.pending = {
                    "awaiting": "doctor_choice", "slots": {},
                    "candidates": [
                        {"name": d["name"], "name_bn": d.get("doctor_name_bn")}
                        for d in result["doctors"]
                    ],
                    "offered_date": start, "retries": 0,
                }
            else:
                session.pending = None
            return True

        ask_state = ("availability_date" if resume == "doctor_availability"
                     else "department_date")
        pending["awaiting"] = ask_state
        pending["offered_date"] = None
        pending["retries"] = 0
        await _speak(session, missing_slot_prompt(resume or "book_appointment", "date"))
        return True

    if awaiting == "availability_date":
        # story title: The model never originates a fact
        # user story: As a clinical lead, I want every price, date and identifier
        #   to come from a verified system response, so that a wrong answer is a
        #   data bug rather than a model bug.
        # acceptance criteria: Every factual sentence is a template substitution
        #   from a validated tool response and the model is never shown a figure
        #   it could restate. An automated assertion on every commit proves no
        #   model-composed span reaches synthesis on a factual intent.
        #
        # The caller said something date-shaped the local parser could not
        # resolve, so rather than state the model's guess as fact the agent
        # asked which day they meant. This is that answer. Mirrors
        # "department_date" below exactly, one intent over: parse it locally,
        # re-run the same lookup, and never fall back to the model's original
        # guess -- an unparseable answer re-asks and then gives up to a fresh
        # classification, which is the same trust model every other state here
        # uses.
        value = parse_date(text, offered_date=pending.get("offered_date"))
        if value is None:
            pending["retries"] += 1
            if pending["retries"] > 2:
                session.pending = None
                return False
            await _speak(session, missing_slot_prompt("doctor_availability", "date"))
            return True

        doctor_name = pending["slots"]["doctor_name"]
        try:
            result = await _tools.get_doctor_availability(doctor_name, value)
        except ToolCallError as e:
            logger.error("[%s] clinic API call failed: %s", session.call_id, e)
            session.pending = None
            await _speak(session, SYSTEM_UNREACHABLE_BN,
                         fallback_reason="tool_failure")
            return True

        asked = {"doctor_name": doctor_name}
        if await _speak_fact(session, "doctor_availability", asked, result,
                             doctor_availability_reply(asked, result),
                             offered_date=value):
            return True

        offered = None
        if result.get("found"):
            offered = result.get("date") if result.get("available") else result.get("next_available_date")
        session.pending = {
            "awaiting": "date",
            "slots": {
                "doctor_name": result.get("doctor_name") or doctor_name,
                "doctor_name_bn": result.get("doctor_name_bn"),
            },
            "candidates": None, "offered_date": offered, "retries": 0,
        } if offered else None
        return True

    if awaiting == "department_date":
        # Mirrors "doctor_choice" above, one level up: the caller was just
        # told nobody in this department sits TODAY and asked for another
        # day. Parse that reply as a date and re-run the same department
        # lookup with it, rather than dropping back to a cold LLM
        # classification of a bare date phrase (see this function's
        # docstring for why that silently loses context).
        value = code_mix.first_parse(parse_date, text, offered_date=pending.get("offered_date"))
        _audit(session).slots("slot_parse", {"date": value}, awaiting="department_date")
        if value is None:
            pending["retries"] += 1
            if pending["retries"] > 2:
                session.pending = None
                return False
            await _speak(session, missing_slot_prompt("doctors_by_department", "date", language=language))
            return True

        department = pending["slots"]["department"]
        try:
            result = await _tools.get_doctors_by_department(department, value)
        except ToolCallError as e:
            logger.error("[%s] clinic API call failed: %s", session.call_id, e)
            session.pending = None
            await _speak(session, SYSTEM_UNREACHABLE_BN,
                         fallback_reason="tool_failure")
            return True

        asked = {"department": department}
        if await _speak_fact(session, "doctors_by_department", asked, result,
                             doctors_by_department_reply(asked, result, language=language),
                             offered_date=value):
            return True

        if result.get("found") and result.get("doctors"):
            session.pending = {
                "awaiting": "doctor_choice",
                "slots": {},
                "candidates": [
                    {"name": d["name"], "name_bn": d.get("doctor_name_bn")}
                    for d in result["doctors"]
                ],
                "offered_date": value, "retries": 0,
            }
        elif result.get("found"):
            # Still nobody that day either -- stay in the same state and
            # let the caller name yet another day, capped by the shared
            # retries counter above so this cannot loop forever.
            pending["retries"] += 1
            if pending["retries"] > 2:
                session.pending = None
            else:
                pending["awaiting"] = "department_date"
        else:
            session.pending = None
        return True

    # Remaining states (date / time_slot / patient_name / phone) all share
    # the same shape: parse the ONE field awaited, fill it in, ask for the
    # next missing one or finish the booking.
    value = None
    if awaiting == "date":
        value = code_mix.first_parse(parse_date, text, offered_date=pending.get("offered_date"))
    elif awaiting == "time_slot":
        value = code_mix.first_parse(parse_time, text)
    elif awaiting == "phone":
        value = code_mix.first_parse(parse_phone, text)
    elif awaiting == "patient_name":
        value = _clean_patient_name(text)
    _audit(session).slots("slot_parse", {awaiting: value}, awaiting=awaiting)

    if value is None:
        pending["retries"] += 1
        if pending["retries"] > 2:
            session.pending = None
            return False
        await _speak(session, missing_slot_prompt("book_appointment", awaiting, language=language))
        return True

    pending["slots"][awaiting] = value
    pending["retries"] = 0
    # A verified caller is not asked for the name and number the record holds,
    # and a patient writing from their own number is not asked for it.
    _fill_from_channel(session, pending["slots"])
    if _fill_from_record(session, pending["slots"]):
        pending["from_record"] = True
    missing = _next_missing(pending["slots"])
    if missing is None:
        # ADDED BY CHAKRAVARDHAN -- a booking whose name/phone came from the
        # verified caller's record was already confirmed through that
        # verification step (see _finish_booking()'s own docstring), so it
        # skips straight to the write rather than a second spoken readback.
        if pending.get("from_record"):
            await _finish_booking(session, pending["slots"], from_record=True,
                                  language=language)
            return True
        # Every field is filled, but nothing is written yet. Read the whole
        # thing back and wait for a yes -- see the "confirm_booking" state
        # above for why an affirmative is required rather than assumed.
        pending["awaiting"] = "confirm_booking"
        await _speak(session, booking_confirmation_prompt(pending["slots"], language=language))
        return True
    pending["awaiting"] = missing
    await _speak(session, missing_slot_prompt("book_appointment", missing, language=language))
    return True


# story title: A thing not existing is never confused with a system being down
# user story: As a caller, I want to know whether my test does not exist or the
#   system cannot be reached, so that I know whether to call back.
# acceptance criteria: The two produce different spoken sentences and different
#   metrics, and the distinction survives every refactor. This behaviour exists
#   today and gains a permanent regression case.
#
# THE THIRD OUTCOME, WHICH SHOULD NOT EXIST.
# _dispatch_turn used to be the whole turn with no outer handler, fired by
# asyncio.create_task() with nothing attached to it. Anything uncaught -- a
# KeyError in a template, a torchaudio failure slicing the clip, a bug in code
# not yet written -- became an unretrieved task exception and the caller heard
# NOTHING. Neither sentence: dead air, which this module's own docstring
# promises never happens.
#
# An unexpected exception is the SYSTEM failing, so it maps to the
# system-unreachable side of the distinction this story is about. It is never
# "your test does not exist": we do not know that, and telling a caller to stop
# asking because our code raised would be the exact confusion the story names.
# story title: A multi-part question is answered in full
# user story: As a caller who asked two things, I want both answered, so that
#   I do not have to ask again.
# acceptance criteria: Every answerable part of a turn is answered in the
#   order asked, and any part that cannot be answered is explicitly addressed
#   rather than dropped. Completeness is scored on a labelled multi-part set.
#
# How long a deferred part may wait. Two turns is one clarification plus its
# answer; past that the caller has moved on, and reviving a question they
# asked four turns ago reads as the agent losing the thread rather than
# keeping it. The queue is never silently binned -- see _drain_deferred.
MAX_DEFERRED_TURNS = 2


# story title: A multi-part question is answered in full
# user story: As a caller who asked two things, I want both answered, so that
#   I do not have to ask again.
# acceptance criteria: Every answerable part of a turn is answered in the
#   order asked, and any part that cannot be answered is explicitly addressed
#   rather than dropped. Completeness is scored on a labelled multi-part set.
async def _run_parts(session: CallSession, text: str, parts: list[dict]) -> bool:
    """Answer the parts in the order the caller asked them.

    -> True if a queue was created on THIS turn, which tells the caller not
    to immediately try to drain it.

    TWO RULES CARRY THIS FUNCTION.

    STOP AT THE FIRST INTERACTIVE PART. Not "skip it and do the rest": the
    criterion says in the order asked, and answering the second question
    while the first is still waiting on a clarification reorders the
    conversation from the caller's side. Whatever follows is deferred, which
    is a promise -- _drain_deferred keeps it.

    DISCARD A SOFT CONTINUATION WHEN ANOTHER PART FOLLOWS. Several answers
    leave session.pending set opportunistically: doctor_availability ends by
    asking "today or another day?" and stays in the flow so a bare date is
    understood next turn. That convenience belongs to the LAST thing said. If
    part two is about to speak, part one's open flow would catch the caller's
    reply to a question they have already stopped thinking about -- so it is
    dropped, deliberately, rather than left to misread the next utterance.
    """
    for index, part in enumerate(parts):
        remaining = parts[index + 1:]
        outcome = await _answer_part(session, text, part)

        if outcome == INTERACTIVE:
            if remaining:
                session.deferred = {"parts": remaining, "text": text, "age": 0}
                logger.info("[%s] deferring %d part(s) behind a question",
                            session.call_id, len(remaining))
                await _speak(session, DEFERRED_PART_BN)
                return True
            return False

        # ANSWERED or UNANSWERABLE: carry on. UNANSWERABLE has already said
        # something about itself inside _answer_part -- that is the whole
        # point of it being a third outcome rather than a silent skip.
        if remaining and session.pending is not None:
            session.pending = None

    return False


# story title: A multi-part question is answered in full
# user story: As a caller who asked two things, I want both answered, so that
#   I do not have to ask again.
# acceptance criteria: Every answerable part of a turn is answered in the
#   order asked, and any part that cannot be answered is explicitly addressed
#   rather than dropped. Completeness is scored on a labelled multi-part set.
async def _drain_deferred(session: CallSession, text: str) -> None:
    """Answer what was put aside, once the question in front of it is done.

    A queue that is only ever created is not a deferral, it is a drop with
    better manners. This is the half that makes DEFERRED_PART_BN a true
    sentence.
    """
    queue = session.deferred
    if not queue:
        return

    if session.pending is not None:
        # Still mid-flow. Wait, but not forever.
        queue["age"] += 1
        if queue["age"] <= MAX_DEFERRED_TURNS:
            return
        session.deferred = None
        logger.info("[%s] dropping %d deferred part(s) after %d turns",
                    session.call_id, len(queue["parts"]), queue["age"])
        # Said, not silently binned. The caller asked; they are owed the
        # information that it went unanswered even when the answer is that
        # too much has happened since.
        for part in queue["parts"]:
            await _speak(session, unanswered_part_prompt(turn_parts.subject_of(part)))
        return

    session.deferred = None
    logger.info("[%s] resuming %d deferred part(s)", session.call_id, len(queue["parts"]))
    await _speak(session, RESUMING_PART_BN)
    # The ORIGINAL utterance, not this turn's. date_calc and slot_parse read
    # the raw words, and "আগামীকাল" said once for two questions means it for
    # both -- re-resolving the second part against the caller's answer to the
    # first ("হ্যাঁ") would lose the day entirely.
    await _run_parts(session, queue["text"], queue["parts"])


# story title: A multi-part question is answered in full
# user story: As a caller who asked two things, I want both answered, so that
#   I do not have to ask again.
# acceptance criteria: Every answerable part of a turn is answered in the
#   order asked, and any part that cannot be answered is explicitly addressed
#   rather than dropped. Completeness is scored on a labelled multi-part set.
#
# LIFTED VERBATIM out of _dispatch_turn_inner, which is why it reads like a
# chain rather than like a function: every branch, comment and ordering
# decision below predates this story and none of them changed. What changed
# is that each of the thirteen `return` statements -- each of which used to
# mean "the turn is over" -- now names WHICH KIND of over it was, so a caller
# with a second question can be told apart from a caller who is owed an
# answer to the first.
#
# `text` is still passed in whole, not just the part: date_calc.resolve and
# slot_parse's parsers read the raw utterance, and a caller who says "আগামীকাল"
# once for two questions means it for both.
async def _answer_part(session: CallSession, text: str, part: dict) -> str:
    """Answer one part of a turn. -> ANSWERED | INTERACTIVE | UNANSWERABLE."""
    intent = part["intent"]
    slots = part["slots"]

    if intent == "smalltalk":
        await _speak(session, part.get("direct_reply_bn") or "নমস্কার, কী সাহায্য করতে পারি?")
        return ANSWERED

    if intent == "unclear":
        await _speak(session, "দুঃখিত, বুঝতে পারিনি। আবার একটু বলবেন?")
        return UNANSWERABLE

    try:
        if intent == "test_rate":
            if not slots.get("test_name"):
                await _speak(session, missing_slot_prompt(intent, "test_name"))
                return INTERACTIVE
            result = await _tools.get_test_rate(slots["test_name"])
            # story title: The same question gets the same answer within one call
            # user story: As a caller who asks twice, I want the same answer, so that
            #   I know which one to believe.
            # acceptance criteria: Repeating a question in one call produces an
            #   identical factual answer unless the underlying data changed, in which
            #   case the change is stated. A test asserts consistency across three
            #   repeats with an unchanged backend.
            #
            # Every factual reply goes through _speak_fact rather than _speak, so the
            # consistency check cannot be forgotten on one route. The reply itself is
            # still rendered here, fresh, from this turn's live clinic response.
            if await _speak_fact(session, intent, slots, result,
                                 test_rate_reply(slots, result)):
                return INTERACTIVE

        elif intent == "doctor_availability":
            if not slots.get("doctor_name"):
                await _speak(session, missing_slot_prompt(intent, "doctor_name"))
                return INTERACTIVE
            # Default to TODAY, not "whenever next available": a bare
            # "ডাক্তার সেন আছেন?" with no date mentioned is a caller
            # asking about right now, and the reply text below already
            # said " আজ" (today) for exactly this case -- the old code
            # passed date=None through to the API, which answers a
            # different question ("when next"), so a doctor who simply
            # wasn't in today got reported by their NEXT sitting date
            # instead of "not today, but they're on Tuesdays" etc.
            # story title: The model never originates a fact
            # user story: As a clinical lead, I want every price, date and
            #   identifier to come from a verified system response, so that
            #   a wrong answer is a data bug rather than a model bug.
            # acceptance criteria: Every factual sentence is a template
            #   substitution from a validated tool response and the model is
            #   never shown a figure it could restate. An automated
            #   assertion on every commit proves no model-composed span
            #   reaches synthesis on a factual intent.
            #
            # The model said what the caller MEANT; date_calc did the
            # calendar. Four outcomes, and they are genuinely different:
            #
            #   range      -> say which days it computed and ask. Safe to
            #                 read the dates out loud precisely because
            #                 code produced them.
            #   unmapped   -> the caller named a day nothing could express.
            #                 Ask which one. Never today: that was the old
            #                 silent wrong answer.
            #   single day -> answer it.
            #   absent     -> no day was mentioned; today is the question
            #                 the caller actually asked.
            span = date_calc.resolve(text, slots.get("date_expr"))
            if span.needs_confirmation:
                logger.info("[%s] %s -> %s..%s, confirming the range",
                            session.call_id, span.expression, span.start, span.end)
                session.pending = {
                    "awaiting": "confirm_date",
                    "slots": {"doctor_name": slots["doctor_name"]},
                    "candidates": None, "offered_date": span.start, "retries": 0,
                    "resume_intent": "doctor_availability", "span_end": span.end,
                }
                await _speak(session, date_range_confirm_prompt(span.start, span.end))
                return INTERACTIVE
            if span.source == date_calc.SOURCE_UNMAPPED:
                logger.info("[%s] caller named a day the vocabulary cannot express "
                            "-- asking instead of assuming", session.call_id)
                session.pending = {
                    "awaiting": "availability_date",
                    "slots": {"doctor_name": slots["doctor_name"]},
                    "candidates": None, "offered_date": None, "retries": 0,
                }
                await _speak(session, missing_slot_prompt(intent, "date"))
                return INTERACTIVE
            if span.source == SOURCE_INTERPRETED:
                logger.info("[%s] date interpreted: %s -> %s",
                            session.call_id, span.expression, span.start)
            date_iso = span.start or datetime.date.today().isoformat()
            result = await _tools.get_doctor_availability(slots["doctor_name"], date_iso)
            if await _speak_fact(session, intent, slots, result,
                                 doctor_availability_reply(slots, result),
                                 offered_date=date_iso):
                return INTERACTIVE

            # Keep the flow open for "yes, book that day" / "another
            # day" -- doctor_availability_reply() just asked exactly
            # that question. See _continue_pending's "date" state.
            offered = None
            if result.get("found"):
                offered = result.get("date") if result.get("available") else result.get("next_available_date")
            session.pending = {
                "awaiting": "date",
                "slots": {
                    "doctor_name": result.get("doctor_name") or slots["doctor_name"],
                    "doctor_name_bn": result.get("doctor_name_bn"),
                },
                "candidates": None, "offered_date": offered, "retries": 0,
            } if offered else None

        elif intent == "doctors_by_department":
            if not slots.get("department"):
                await _speak(session, missing_slot_prompt(intent, "department"))
                return INTERACTIVE
            # Default to TODAY when the caller didn't name a date, same
            # reasoning as doctor_availability above: "অর্থোতে কারা
            # আছেন" (who's in ortho) is almost always asking who is
            # actually in the chamber right now, not for a roster of
            # every doctor the department has ever employed regardless
            # of whether they sit this week. Only an EXPLICIT date
            # bypasses this (used as-is below).
            # story title: The model never originates a fact
            # user story: As a clinical lead, I want every price, date and
            #   identifier to come from a verified system response, so that
            #   a wrong answer is a data bug rather than a model bug.
            # acceptance criteria: Every factual sentence is a template
            #   substitution from a validated tool response and the model is
            #   never shown a figure it could restate. An automated
            #   assertion on every commit proves no model-composed span
            #   reaches synthesis on a factual intent.
            #
            # The model said what the caller MEANT; date_calc did the
            # calendar. Four outcomes, and they are genuinely different:
            #
            #   range      -> say which days it computed and ask. Safe to
            #                 read the dates out loud precisely because
            #                 code produced them.
            #   unmapped   -> the caller named a day nothing could express.
            #                 Ask which one. Never today: that was the old
            #                 silent wrong answer.
            #   single day -> answer it.
            #   absent     -> no day was mentioned; today is the question
            #                 the caller actually asked.
            span = date_calc.resolve(text, slots.get("date_expr"))
            if span.needs_confirmation:
                logger.info("[%s] %s -> %s..%s, confirming the range",
                            session.call_id, span.expression, span.start, span.end)
                session.pending = {
                    "awaiting": "confirm_date",
                    "slots": {"department": slots["department"]},
                    "candidates": None, "offered_date": span.start, "retries": 0,
                    "resume_intent": "doctors_by_department", "span_end": span.end,
                }
                await _speak(session, date_range_confirm_prompt(span.start, span.end))
                return INTERACTIVE
            if span.source == date_calc.SOURCE_UNMAPPED:
                logger.info("[%s] caller named a day the vocabulary cannot express "
                            "-- asking instead of assuming", session.call_id)
                session.pending = {
                    "awaiting": "department_date",
                    "slots": {"department": slots["department"]},
                    "candidates": None, "offered_date": None, "retries": 0,
                }
                await _speak(session, missing_slot_prompt(intent, "date"))
                return INTERACTIVE
            if span.source == SOURCE_INTERPRETED:
                logger.info("[%s] date interpreted: %s -> %s",
                            session.call_id, span.expression, span.start)
            date_iso = span.start or datetime.date.today().isoformat()
            result = await _tools.get_doctors_by_department(slots["department"], date_iso)
            if await _speak_fact(session, intent, slots, result,
                                 doctors_by_department_reply(slots, result),
                                 offered_date=date_iso):
                return INTERACTIVE

            # Continue straight into booking: offer the doctors just
            # listed as candidates, so the caller's very next utterance
            # -- which may be nothing but a bare doctor name -- is
            # matched against THIS list rather than sent to the LLM with
            # no context to interpret it against. See _continue_pending's
            # "doctor_choice" state.
            if result.get("found") and result.get("doctors"):
                session.pending = {
                    "awaiting": "doctor_choice",
                    "slots": {},
                    "candidates": [
                        {"name": d["name"], "name_bn": d.get("doctor_name_bn")}
                        for d in result["doctors"]
                    ],
                    "offered_date": date_iso,
                    "retries": 0,
                }
            elif result.get("found"):
                # Department exists but nobody sits that day --
                # doctors_by_department_reply() just told the caller
                # exactly that and invited another day ("অন্য কোনো
                # দিনের কথা জিজ্ঞেস করতে পারেন"). Stay in the flow so the
                # caller's next utterance is interpreted as THAT date
                # instead of needing to restate the whole department
                # question from scratch -- see _continue_pending's
                # "department_date" state.
                session.pending = {
                    "awaiting": "department_date",
                    "slots": {"department": slots["department"]},
                    "candidates": None, "offered_date": None, "retries": 0,
                }
            else:
                session.pending = None

        elif intent == "book_appointment":
            # Merge onto whatever session.pending already knows (e.g. a
            # doctor_name carried over from a doctor_availability or
            # doctors_by_department turn moments ago) rather than
            # requiring every field in one utterance -- that all-or-
            # nothing check was the other half of "pipeline breaking":
            # a caller who gave the doctor and date in one sentence and
            # the time in the next used to have the doctor/date silently
            # discarded the moment ANY field was still missing.
            merged = dict(session.pending["slots"]) if session.pending else {}
            for field in _BOOKING_FIELDS:
                if slots.get(field):
                    merged[field] = slots[field]

            # story title: The model never originates a fact
            # user story: As a clinical lead, I want every price, date and identifier
            #   to come from a verified system response, so that a wrong answer is a
            #   data bug rather than a model bug.
            # acceptance criteria: Every factual sentence is a template substitution
            #   from a validated tool response and the model is never shown a figure
            #   it could restate. An automated assertion on every commit proves no
            #   model-composed span reaches synthesis on a factual intent.
            #
            # The booking path is the one place that ALREADY had a
            # verifier: every field below is read back in full and an
            # explicit হ্যাঁ is required before _finish_booking writes
            # anything, so a mis-resolved date here is caught by the one
            # party who knows what "কাল" meant. That readback is not
            # touched by this story and must not be weakened by it.
            #
            # What this adds is removing the model from the loop wherever
            # a deterministic parser can do the same job on the same
            # words -- a date, a time and a phone number are all things
            # slot_parse.py resolves in code. Only fields the model
            # claimed from THIS utterance are corrected; a value carried
            # over from an earlier turn was already parsed locally by
            # _continue_pending and must not be re-derived from a
            # transcript that no longer mentions it.
            for field, parser in (("time_slot", parse_time), ("phone", parse_phone)):
                if not slots.get(field):
                    continue
                parsed = parser(text)
                if parsed and parsed != merged.get(field):
                    logger.warning("[%s] %s disagreement: model=%s parsed=%s -- using parsed",
                                   session.call_id, field, merged.get(field), parsed)
                    merged[field] = parsed

            # The date is not merged from `slots` at all any more, because
            # `slots["date"]` now holds the caller's WORDS ("১৫ তারিখ"),
            # not a calendar date -- llm.py stopped producing those. Only a
            # value date_calc computed may be stored, or the API would be
            # handed a Bengali phrase and _next_missing() would report the
            # field as filled while holding something unusable.
            #
            # A RANGE is dropped rather than confirmed here: a booking is
            # one slot on one day, so "আগামী সপ্তাহে অ্যাপয়েন্টমেন্ট চাই"
            # has to become a specific day, and leaving the field empty
            # makes _next_missing() ask for exactly that. The range
            # confirmation belongs to the two read-only intents, which can
            # actually answer about a span.
            if slots.get("date") or slots.get("date_expr"):
                span = date_calc.resolve(text, slots.get("date_expr"))
                if span.start and not span.is_range:
                    merged["date"] = span.start
                else:
                    merged.pop("date", None)

            missing = _next_missing(merged)
            if missing is None:
                # Everything arrived in one utterance. That is the case
                # MOST in need of a readback, not least: five fields pulled
                # from a single sentence of phone audio is where a
                # mishearing is likeliest and least visible. Route it
                # through the same confirmation state as the slow path.
                session.pending = {
                    "awaiting": "confirm_booking", "slots": merged,
                    "candidates": None,
                    "offered_date": merged.get("date"), "retries": 0,
                }
                await _speak(session, booking_confirm_prompt(merged))
                return INTERACTIVE

            session.pending = {
                "awaiting": missing, "slots": merged, "candidates": None,
                "offered_date": (session.pending or {}).get("offered_date"), "retries": 0,
            }
            await _speak(session, missing_slot_prompt(intent, missing))
            return INTERACTIVE

    except ToolCallError as e:
        logger.error("[%s] clinic API call failed: %s", session.call_id, e)
        await _speak(session, SYSTEM_UNREACHABLE_BN,
                     fallback_reason="tool_failure")
        return UNANSWERABLE

    return ANSWERED


def _suppress_echo_in_place(clip_path: str, reference) -> None:
    """Subtract the agent's own playback out of a barge-in clip, in place.

    Blocking; callers run it on a worker thread. Swallows its own errors on
    purpose -- echo suppression is an improvement to a clip that is already
    usable enough to have triggered a barge-in, so a failure here must leave
    the turn alone rather than lose it."""
    try:
        import soundfile as sf

        samples, sr = sf.read(clip_path, dtype="float32", always_2d=False)
        if getattr(samples, "ndim", 1) > 1:
            samples = samples.mean(axis=1)
        cleaned = suppress_echo(samples, reference, sr)
        sf.write(clip_path, cleaned, sr, subtype="PCM_16")
    except Exception as e:  # noqa: BLE001
        logger.warning("echo suppression skipped for %s: %s", clip_path, e)


async def _clarify_or_offer_keypad(session: CallSession, quality=None,
                                   reason: str = "low_quality"):
    """The turn could not be acted on. Decide WHICH way to say so.

    Everything that means "we did not understand this caller" funnels
    through here -- a clip below the quality floor, and ASR returning
    nothing -- so the ladder counts real consecutive failures rather than
    one particular failure mode. Two different silent failures in a row
    are still two failures to the caller.

    The rung is chosen by session.failures (agent/quality_metrics.py), not
    by anything about this turn: the caller's recent history is what says
    whether another question is worth asking."""
    action = session.failures.record_failure()

    if action == ACTION_KEYPAD:
        METRICS.record_keypad_offer()
        logger.info("[%s] %d consecutive failed turns (%s) -- offering keypad",
                    session.call_id, session.failures.consecutive_failures, reason)
        # Control frame first so the keys are on screen before the caller
        # hears why. Carries no display text of its own -- the spoken line
        # below is the one the caller reads in the log, and sending both
        # would print it twice.
        await session.send_json("_keypad", "on")
        await _speak(session, KEYPAD_PROMPT_BN, fallback_reason="keypad_offer")
        return

    METRICS.record_clarification()
    logger.info("[%s] turn unusable (%s) -- asking again (failure %d/%d)",
                session.call_id, reason, session.failures.consecutive_failures,
                session.failures.max_retries)
    await _speak(session, CLARIFY_PROMPT_BN, fallback_reason=reason)


async def _handle_keypad_digit(session: CallSession, digit: str):
    """A keypad press. Translated to the Bengali the caller would have said
    and pushed through the ordinary text path -- see KEYPAD_MENU_BN for why
    it is mapped to text rather than to an intent id.

    Counts as a success for the ladder: the fallback did its job, the
    caller got through, and the next isolated misheard turn deserves an
    ordinary clarification rather than the keypad again."""
    text = KEYPAD_MENU_BN.get(digit.strip())
    if text is None:
        logger.info("[%s] keypad: ignoring unmapped key %r", session.call_id, digit)
        return

    METRICS.record_keypad_entry()
    session.failures.record_success()
    logger.info("[%s] keypad: %r -> %r", session.call_id, digit, text)
    await answer_turn(session, text, source="keypad")



# ===========================================================================
# PATIENT HISTORY -- disclosed only after verification
# Author: Chakravardhan
# ===========================================================================
async def _history_guard(session: CallSession, phone: str) -> bool:
    """-> True if it is safe to speak private information on THIS call.

    Checked SEPARATELY from verification and BEFORE any history is fetched.
    The acceptance criterion says "so that whoever else uses this handset
    cannot HEAR" -- a correctly verified patient with the phone on
    loudspeaker is entitled to their history and must still not have it read
    out, because the criterion is about who ends up hearing it, not about
    entitlement.

    agent/privacy.py explains why this uses EchoGuard.classify() rather than
    reporting_path(), and why an unclassified path counts as unsafe.
    """
    # The CHANNEL first: a written message is never private, whatever the
    # room -- see privacy.channel_is_private(). On the phone line this is
    # exactly the audio-path check it always was.
    safe, reason = privacy.channel_is_private(
        getattr(session, "channel", privacy.CHANNEL_VOICE), session.echo)
    if safe:
        return True

    logger.info("[%s] history disclosure refused: %s", session.call_id, reason)
    # Audited on the clinic side, not only in this log: without a row,
    # "it would not tell me my history" has no explanation at the clinic and
    # the likeliest support response is to switch the check off.
    try:
        await _tools.record_disclosure_refusal(phone, reason, session.call_id)
    except Exception:                                  # noqa: BLE001
        pass
    await _speak(session, disclosure_blocked_reply(reason, session.lang))
    return False


async def _start_history_verification(session: CallSession, phone: str | None,
                                      purpose: str = PURPOSE_HISTORY):
    """Begin the challenge. Never says whether the number is known.

    `purpose` is what the caller asked for -- their history, or their
    bookings -- and is what gets read out once they are verified.
    """
    if not phone:
        # No number to look up yet. ASK for it rather than ending the flow:
        # the number only says which record to look at, and asking reveals
        # nothing about whether the clinic holds one. See _continue_pending's
        # "record_phone" state.
        session.pending = {
            "awaiting": "record_phone", "purpose": purpose, "slots": {},
            "candidates": None, "offered_date": None, "retries": 0,
        }
        await _speak(session, _t(session.lang, "timeline.ask_phone"))
        return

    if not await _history_guard(session, phone):
        return

    try:
        challenge = await _tools.begin_verification(phone, session.call_id)
    except ToolCallError as e:
        logger.error("[%s] verification start failed: %s", session.call_id, e)
        await _speak(session, _t(session.lang, "generic.tool_failure"),
                     fallback_reason="tool_failure")
        return

    if challenge.get("locked"):
        await _speak(session, verification_locked_reply(session.lang))
        return

    session.history_phone = phone
    session.pending = {
        "awaiting": "history_verify",
        "factor": challenge.get("factor") or "dob",
        # What to read out once verified -- see _continue_history_verification.
        "purpose": purpose,
        "slots": {},
        "candidates": None,
        "offered_date": None,
        # Counted here for the WORDING only ("try again" vs "go to the
        # counter"). The real lockout is counted per PATIENT on the clinic
        # side, so hanging up and redialling does not reset it.
        "retries": 0,
    }
    await _speak(session, verification_prompt(session.pending["factor"], session.lang, purpose))


async def _load_timeline(session: CallSession) -> dict | None:
    """-> the verified caller's single timeline, or None after telling the
    caller why there is nothing to read.

    FETCHED AT MOST ONCE PER CALL. The first read after verification is
    kept on the session and every later question -- "what have I booked",
    "what tests have I had", a booking that needs their name -- is answered
    from it. A caller who has proved who they are is not asked the hospital's
    own records back. Fetched again only when this call has itself changed
    the record (session.timeline_stale), so it never reads out stale data.

    Callers must have passed _history_guard() first; this only fetches.
    """
    if session.timeline is not None and not session.timeline_stale:
        return session.timeline
    try:
        result = await _tools.read_history(session.history_token, session.call_id)
    except ToolCallError as e:
        logger.error("[%s] history read failed: %s", session.call_id, e)
        await _speak(session, _t(session.lang, "generic.tool_failure"),
                     fallback_reason="tool_failure")
        return None

    if not result.get("found"):
        # Token expired or revoked mid-call. Treated as "not verified",
        # which is what it is -- and the copy held from before goes with it.
        session.history_token = None
        session.timeline = None
        await _speak(session, verification_failed_reply(True, session.lang))
        return None

    session.timeline, session.timeline_stale = result, False
    return result


async def _speak_history(session: CallSession):
    """Fetch and speak, re-checking the room immediately beforehand.

    The audio path is checked AGAIN here, not just at the start of
    verification. A caller can put the phone on speaker between answering
    the challenge and hearing the answer -- and that is the exact moment
    the private information is about to be spoken.
    """
    if not await _history_guard(session, session.history_phone or ""):
        return
    result = await _load_timeline(session)
    if result is None:
        return

    # Redacted in the audit: the fact of disclosure is recorded (here, in the
    # read_history API_RESPONSE, and in clinic-api's disclosure_audit), the
    # medical history itself is not copied into a second store.
    await _speak(session, history_reply(result, session.lang), audit_redact="patient_history")


async def _speak_bookings(session: CallSession):
    """What the caller has booked, from their timeline.

    The same two checks as _speak_history, in the same order and for the
    same reasons: verified (the caller of this function guarantees a token),
    and the room re-checked immediately before anything private is said.
    """
    if not await _history_guard(session, session.history_phone or ""):
        return
    result = await _load_timeline(session)
    if result is None:
        return
    # Redacted in the call audit like the history: a patient's bookings are
    # part of their record, and the audit keeps the fact they were read,
    # not a second copy of them.
    await _speak(session, bookings_reply(result, session.lang), audit_redact="patient_timeline")


def _fill_from_record(session: CallSession, slots: dict) -> bool:
    """Fill a booking's patient_name and phone from the verified caller's
    record, if the caller has not given them. -> True if anything was filled.

    THIS IS THE STORY'S PROMISE AT BOOKING TIME: a patient who has already
    proved who they are on this call is not asked for their name and number
    again. Only EMPTY fields are filled -- a caller who names somebody else
    ("book it for my mother, Iti Sen") is booking for that person, and their
    words win. Nothing is filled unless this call verified the caller.
    """
    record = session.timeline if session.history_token else None
    if not record:
        return False
    filled = False
    if not slots.get("patient_name") and record.get("patient_name"):
        slots["patient_name"] = record["patient_name"]
        filled = True
    if not slots.get("phone") and session.history_phone:
        slots["phone"] = session.history_phone
        filled = True
    return filled


def _fill_from_channel(session, slots: dict) -> bool:
    """Fill a booking's phone from the number the CHANNEL itself knows.

    A patient writing from WhatsApp is writing FROM their number -- the
    provider asserts it, and it is where the written confirmation goes -- so
    asking them to type it back is the "recite what you have already told
    us" this story exists to remove. Only an EMPTY field is filled: someone
    who gives another number (booking for a relative who will take the call)
    is answered with their own words. The phone line has no caller-ID, so
    nothing is ever filled there.
    """
    if getattr(session, "channel", privacy.CHANNEL_VOICE) == privacy.CHANNEL_VOICE:
        return False
    number = getattr(session, "history_phone", None)
    if not number or slots.get("phone"):
        return False
    slots["phone"] = number
    return True


async def _continue_history_verification(session: CallSession, text: str) -> bool:
    """The caller just answered the challenge. Always returns True -- this
    turn belongs to verification either way."""
    pending = session.pending
    factor = pending.get("factor") or "dob"
    purpose = (pending or {}).get("purpose") or PURPOSE_HISTORY

    # Folded to ASCII first: a Bengali or Devanagari numeral from the
    # matching ASR checkpoint is the same PIN as its ASCII form, and a
    # caller must not fail verification over which script their digits
    # arrived in.
    answer = lang_mod.to_ascii_digits(text)

    try:
        outcome = await _tools.verify_caller(
            session.history_phone or "", factor, answer, session.call_id)
    except ToolCallError as e:
        logger.error("[%s] verification failed to run: %s", session.call_id, e)
        session.pending = None
        await _speak(session, _t(session.lang, "generic.tool_failure"),
                     fallback_reason="tool_failure")
        return True

    reply = outcome.get("reply")
    if reply == "verified":
        session.pending = None
        session.history_token = outcome.get("token")
        logger.info("[%s] caller verified (factor=%s)", session.call_id, factor)
        # Read out what the caller ASKED for. Either way the timeline is
        # loaded once here and answers every later question on this call.
        if purpose == PURPOSE_BOOKINGS:
            await _speak_bookings(session)
        else:
            await _speak_history(session)
        # Now that the number is proved, a booking this patient left
        # part-done by message is picked up rather than started again.
        await _resume_from_other_channel(session)
        return True

    if reply == "locked":
        session.pending = None
        await _speak(session, verification_locked_reply(session.lang))
        return True

    # Failed. The caller is told the same sentence whatever the reason --
    # wrong answer, unknown number, no factor on file. Only whether ANOTHER
    # ATTEMPT IS POSSIBLE changes the wording, and that is not a hint.
    pending["retries"] += 1
    exhausted = pending["retries"] >= 2
    if exhausted:
        session.pending = None
    await _speak(session, verification_failed_reply(exhausted, session.lang))
    return True

# ===========================================================================
# WHICH LANGUAGE IS THE CALLER SPEAKING? -- Author: Chakravardhan
# ===========================================================================
# A written message announces its language in its own script. Speech does
# not: a Bengali-only checkpoint returns Bengali glyphs for whatever it is
# played, so reading the script of ITS output cannot tell Hindi from
# Bengali (that is STRATEGY_SCRIPT, and it is honest only with a checkpoint
# that can emit more than one script). The only way to identify a spoken
# language from audio is to let each checkpoint this pod actually has hear
# the same utterance and keep the best transcript -- STRATEGY_PARALLEL.
#
# Off by default (STRATEGY_FIXED), and a no-op on a pod with one checkpoint,
# so the Bengali line behaves exactly as it always has.
def _transcript_score(result) -> float:
    """How much this decode looks like the language it was decoded as.

    Agreement between the CTC and RNNT decoders is the honest signal: on
    audio a model was not trained for, the two diverge. Length only breaks
    ties -- a wrong-language decode tends to come back short and clipped."""
    text = (getattr(result, "text", "") or "").strip()
    if not text:
        return 0.0
    agreement = float(getattr(result, "decoder_agreement", 0.0) or 0.0)
    return agreement + min(len(text.split()), 10) / 100.0


def _adopt_language(session: CallSession, code: str | None, source: str = "asr_probe") -> None:
    """Serve the rest of this call in the language just identified."""
    if not code or code == session.lang or not lang_mod.is_enabled(code):
        return
    logger.info("[%s] caller language identified: %s -> %s", session.call_id, session.lang, code)
    _audit(session).record("LANGUAGE_DETECTED",
                           {"language_from": session.lang, "language_to": code, "source": source})
    session.lang = code


async def _decode_in_each(session: CallSession, utterance_wav: str, codes, label: str):
    """-> [(language, transcript, score)] for every checkpoint in `codes` this
    pod actually has. Shared by the first-turn probe and the re-probe below."""
    decoded = []
    for code in codes:
        candidate_node = asr_mod.for_language(code)
        if candidate_node is None:
            continue
        candidate = await candidate_node.transcribe_utterance(utterance_wav)
        score = _transcript_score(candidate)
        logger.info("[%s] %s: %s scored %.2f", session.call_id, label, code, score)
        decoded.append((code, candidate, score))
    return decoded


# ALL THREE LANGUAGES, ON EVERY TURN THAT NEEDS IT -- Author: Chakravardhan
# Story: "As a patient more comfortable speaking than typing, I want to send a
#         voice note in whatever mixture I speak, so that literacy is not a
#         barrier."
#
# The probe settles the call's language on the FIRST utterance, and every later
# turn used to be decoded by that one checkpoint. A caller who mixes languages
# does not stay in one: a Hindi question after a Bengali greeting, decoded by
# the Bengali checkpoint, comes back with its CTC and RNNT decoders disagreeing
# -- the very signal the probe scores on. Such a turn is now heard again by the
# other checkpoints, and a clearly better transcript replaces it.
#
# A turn the call's checkpoint heard well (the common case) costs nothing more.
# REPROBE_MARGIN stops a near-tie from flipping the call's language back and
# forth. Both only apply under STRATEGY_PARALLEL on a pod with more than one
# checkpoint, so the Bengali-only line is exactly as it was.
REPROBE_BELOW = float(os.environ.get("VOICE_AGENT_REPROBE_BELOW", "0.5"))
REPROBE_MARGIN = float(os.environ.get("VOICE_AGENT_REPROBE_MARGIN", "0.15"))


async def _reprobe(session: CallSession, utterance_wav: str, heard, current):
    """A turn the call's own language did not hear well -> the best transcript
    any checkpoint on this pod made of it."""
    current_score = _transcript_score(current)
    others = [code for code in heard if code != session.lang]
    decoded = await _decode_in_each(session, utterance_wav, others, "language re-probe")
    if not decoded:
        return current
    best_lang, best, best_score = max(decoded, key=lambda d: d[2])
    if best_score < current_score + REPROBE_MARGIN:
        return current
    _adopt_language(session, best_lang, source="asr_reprobe")
    return best


async def _transcribe_in_caller_language(session: CallSession, utterance_wav: str):
    """Transcribe this utterance, deciding WHICH language to hear it in.

    The probe runs at most once per call and only where it can mean
    anything: STRATEGY_PARALLEL, and a pod with more than one checkpoint.
    Everything else takes the single decode below -- the path this line has
    always taken.

    Falling back to the default node rather than failing is deliberate: a
    caller whose language this pod cannot hear is still a caller, and a
    Bengali transcript we can act on beats a dropped turn."""
    strategy = lang_mod.strategy()
    heard = lang_mod.enabled()

    if (strategy == lang_mod.STRATEGY_PARALLEL and not session.language_probe_done
            and len(heard) > 1):
        # THE PROBE. Every checkpoint on this pod hears the same clip once,
        # and the call is served in whichever heard it best. N decodes on
        # ONE turn of the call, none after it.
        session.language_probe_done = True
        decoded = await _decode_in_each(session, utterance_wav, heard, "language probe")
        if decoded:
            # The first of equal scores wins, as it always did: preference order.
            best_lang, best, _score = max(decoded, key=lambda d: d[2])
            _adopt_language(session, best_lang)
            return best

    node = asr_mod.for_language(session.lang) or _asr
    result = await node.transcribe_utterance(utterance_wav)
    if strategy == lang_mod.STRATEGY_SCRIPT and (result.text or "").strip():
        # Only honest with a checkpoint that can EMIT more than one script.
        _adopt_language(session, lang_mod.detect_from_text(result.text, fallback=session.lang))
    # A later turn in another language -- see REPROBE_BELOW above.
    if (strategy == lang_mod.STRATEGY_PARALLEL and len(heard) > 1
            and _transcript_score(result) < REPROBE_BELOW):
        return await _reprobe(session, utterance_wav, heard, result)
    return result


# ===========================================================================
# THE SAME QUESTIONS BY MESSAGE -- Author: Chakravardhan
# Story: "As a patient, I want to ask the same questions by message and get
#         the same answers, so that I can use the channel I already have open."
# ===========================================================================
# agent/message_service.py drives THIS turn loop with a message instead of
# an utterance. run_text_turn() enters _dispatch_turn exactly where a keypad
# digit does, so the fast path, the intent cache, the LLM, slot filling, the
# clinic calls and the reply templates are the phone line's own -- not a
# copy that could drift from them. Only two things differ by channel: how
# the answer leaves (_speak -> _deliver_written), and whether the channel
# may carry private information at all (privacy.channel_is_private).
def _save_for_other_channels(session: CallSession) -> None:
    """At the end of a call, leave behind what another channel may continue.

    Keyed by a VERIFIED number only. This transport has no caller-ID, so the
    only number a call can vouch for is the one proved at the history
    challenge -- a number merely said is no proof of holding that handset,
    and the message thread it would feed is read on that handset. Must run
    before cleanup(), which drops the verification it relies on."""
    if _conversations is None or not session.history_token or not session.history_phone:
        return
    _conversations.save(session.history_phone, lang=session.lang, pending=session.pending,
                        channel=privacy.CHANNEL_VOICE)


async def _resume_from_other_channel(session: CallSession) -> None:
    """A verified caller who began a booking by message picks it up here.

    Only after verification, for the reason _save_for_other_channels gives;
    only when nothing is already in progress on this call; and only the
    plain booking fields cross over (conversation_store.portable())."""
    if _conversations is None or session.pending is not None or not session.history_token:
        return
    snap = _conversations.load(session.history_phone, privacy.CHANNEL_VOICE)
    if snap is None or snap.pending is None or snap.channel == privacy.CHANNEL_VOICE:
        return
    session.pending = snap.pending
    awaiting = snap.pending["awaiting"]
    _audit(session).intent("resume_flow", "conversation_store", flow=awaiting,
                           from_channel=snap.channel)
    await _speak(session, _t(session.lang, "channel.resumed")
                 + missing_slot_prompt("book_appointment", awaiting, session.lang))


async def start_text_services(transport: str) -> None:
    """Bring up what a written conversation needs, and nothing it does not.

    The clinic client, the fast path, the intent cache and the call audit,
    made the way _startup() makes them. No ASR, no VAD, no TTS: a message
    service has no audio, and loading IndicConformer onto a GPU to answer
    text would be pure cost."""
    global _tools, _intent_cache, _fast_path, _audit_store
    _audit_store = call_audit.AuditStore()
    recovered = _audit_store.recover_unfinished(transport)
    if recovered:
        logger.warning("audit: finalised %d %s record(s) a previous run left open",
                       recovered, transport)
    _tools = ClinicToolsClient(CLINIC_API_BASE)
    _intent_cache = SemanticCache()
    _fast_path = await _load_fast_path()


async def stop_text_services() -> None:
    if _tools:
        await _tools.aclose()
    _shutdown_http_pool()
    if _audit_store:
        await asyncio.to_thread(_audit_store.close)


def text_audit_store() -> call_audit.AuditStore | None:
    """The audit store a message service files its records in."""
    return _audit_store


async def speak_to(session, text: str) -> None:
    """Something the CHANNEL says, outside any turn -- through _speak, so it
    is delivered and audited like every answer."""
    await _speak(session, text)


async def answer_turn(session, text: str, *, source: str) -> None:
    """THE SHARED ANSWER SERVICE -- Author: Chakravardhan.

    Every channel that has WORDS enters here: a keypad press, a WhatsApp
    message, and (through _dispatch_turn, once ASR has produced them) a
    caller's own. From this line on there is one path -- fast path, intent
    cache, model, slot filling, verification, clinic-api, reply templates --
    so the answer cannot depend on which channel asked.

    Where a channel is allowed to differ at all is written down in
    agent/answer_contract.py, and tests/test_channel_parity.py asks both
    channels the same questions and fails the build on any difference that
    is not on that list."""
    await _dispatch_turn(session, "", text_override=text, text_source=source)


async def run_text_turn(session, text: str) -> None:
    """One written message, through the shared answer service."""
    await answer_turn(session, text, source="message")


async def _dispatch_turn(session: CallSession, utterance_wav: str,
                         text_override: str | None = None, text_source: str = "keypad"):
    """_dispatch_turn_inner, with any exception it raises recorded rather
    than left to become dead air.

    A turn runs as a fire-and-forget task (see _turn_poll_loop), so an
    exception escaping it used to reach nothing but asyncio's "Task
    exception was never retrieved" at garbage collection -- the caller got
    dead air and there was no trace of which call it happened on (see this
    file's own "THE THIRD OUTCOME, WHICH SHOULD NOT EXIST" story above).
    ADDED BY CHAKRAVARDHAN: the failure is also recorded onto the call's own
    audit trail via _audit(session).error(), so auditing observes it
    alongside everything else that call said and heard.
    """
    global _turn_crashes
    try:
        await _dispatch_turn_inner(session, utterance_wav, text_override, text_source)
    except Exception as e:  # noqa: BLE001 - a turn must not die silently
        _turn_crashes += 1
        _audit(session).error("turn", e)
        logger.exception("[%s] turn crashed -- answering as unreachable", session.call_id)
        with contextlib.suppress(Exception):
            await _speak(session, SYSTEM_UNREACHABLE_BN, fallback_reason="tool_failure")


async def _dispatch_turn_inner(session: CallSession, utterance_wav: str,
                    text_override: str | None = None, text_source: str = "keypad"):
    """One full turn: ASR -> intent -> tool -> templated reply -> TTS.
    Serialized per-call via session.dispatch_lock so replies never
    interleave, even if the caller starts talking again immediately.

    text_override skips audio entirely. It is how a keypad digit enters
    this function: the alternative -- a second dispatcher for DTMF -- would
    have to re-implement the fast path, the cache, slot filling and
    _continue_pending, and would drift out of step with the spoken path the
    first time either was touched."""
    async with session.dispatch_lock:
        quality = None
        audit = _audit(session)
        audit.begin_turn()
        # The caller is answering a verification challenge, so what they say
        # IS the secret -- a PIN or a date of birth. Withheld from the record
        # exactly as clinic-api's disclosure_audit withholds it; the length is
        # kept so the record still shows an answer was given.
        secret = ("verification_answer"
                  if (session.pending or {}).get("awaiting") == "history_verify" else None)

        if text_override is not None:
            text = text_override.strip()
            if not text:
                return
            # "keypad" for a digit, "message" for a written message.
            audit.transcript(text, source=text_source, redacted=secret)
        else:
            try:
                # CONDITION BEFORE ASR, and gate before the GPU is asked for
                # anything. Two reasons, in order of importance:
                #
                #  1. A clip below the floor must not produce a confident
                #     answer. Downstream cannot tell a transcript of speech
                #     from a transcript of a bus, so the decision has to be
                #     made here, on the audio, while that distinction still
                #     exists.
                #  2. A rejected clip then costs no inference at all, which
                #     is the stage under most pressure at peak.
                #
                # to_thread because it is numpy over the whole clip -- tens
                # of milliseconds of CPU that would otherwise block every
                # other call's socket on this event loop.
                # A barge-in clip is the one turn where the caller's speech
                # is genuinely mixed with our own playback, and the only turn
                # where we hold the exact signal mixed into it. Subtract it
                # before anything else looks at the audio; every other turn
                # reads None here and is untouched.
                #
                # OUTSIDE the try below on purpose. That try fails OPEN --
                # it drops the quality floor and sends the raw clip -- so a
                # fault raised inside it would silently disarm an unrelated
                # feature rather than surfacing.
                echo_ref = getattr(session, "take_echo_reference", lambda: None)()
                if echo_ref is not None and ECHO_CFG.echo_suppression_enabled:
                    await asyncio.to_thread(
                        _suppress_echo_in_place, utterance_wav, echo_ref)

                try:
                    conditioned = await asyncio.to_thread(condition_wav_file, utterance_wav)
                    quality = conditioned.quality
                    logger.info(
                        "[%s] clip: %.2fs snr=%.1fdB speech=%.0f%% gain=%+.1fdB %s%s",
                        session.call_id, quality.duration_s, quality.snr_db,
                        100 * quality.speech_ratio, conditioned.gain_db,
                        quality.bucket(),
                        "" if quality.usable else f" REJECT{list(quality.reasons)}",
                    )
                except Exception as e:  # noqa: BLE001
                    # Fail OPEN. A bug in the conditioner must not take the
                    # whole service down to "sorry, say again" on every turn;
                    # an unconditioned clip still transcribes, which is the
                    # behaviour that shipped before this stage existed.
                    logger.warning("[%s] conditioning failed (%s) -- sending raw clip",
                                   session.call_id, e)
                    audit.error("audio_conditioning", e, handled=True, fail_open=True)

                if quality is not None and not quality.usable:
                    audit.transcript(None, source="speech", status="rejected_low_quality",
                                     audio=call_audit.describe_quality(quality))
                    METRICS.record_turn(quality, success=False,
                                        path=session.echo.reporting_path())
                    await _clarify_or_offer_keypad(
                        session, quality, reason=quality.reasons[0])
                    return

                # asr_gate bounds how many turns may occupy a thread-pool worker
                # waiting on the GPU. dispatch_lock above is per-CALL ordering;
                # this is process-wide admission control. See agent/executors.py.
                async with asr_gate:
                    # Which language to hear this in -- and, on the first
                    # turn of a pod that can hear several, which language the
                    # caller is actually speaking.
                    asr_result = await _transcribe_in_caller_language(session, utterance_wav)
            finally:
                with contextlib.suppress(OSError):
                    os.remove(utterance_wav)

            text = asr_result.text.strip()
            if not text:
                # Empty text from a clip that PASSED the floor. Counted as a
                # failed turn like any other: the caller was not understood,
                # and which stage failed to understand them is our problem,
                # not theirs.
                logger.info("[%s] ASR returned empty text", session.call_id)
                audit.transcript("", source="speech", status="empty",
                                 decoder_used=getattr(asr_result, "decoder_used", None),
                                 audio=call_audit.describe_quality(quality))
                if quality is not None:
                    METRICS.record_turn(quality, success=False,
                                        path=session.echo.reporting_path())
                await _clarify_or_offer_keypad(session, quality, reason="asr_empty")
                return

            if quality is not None:
                METRICS.record_turn(quality, success=True,
                                    path=session.echo.reporting_path())
            session.failures.record_success()
            audit.transcript(text, source="speech", redacted=secret,
                             decoder_used=getattr(asr_result, "decoder_used", None),
                             decoder_agreement=getattr(asr_result, "decoder_agreement", None),
                             audio=call_audit.describe_quality(quality))

        await session.send_json("User", text)

        # ADDED BY CHAKRAVARDHAN -- an explicit language request owns the
        # turn -------------------------------------------------------------
        #
        # Checked BEFORE _continue_pending and before intent extraction, and
        # deliberately so. A caller who says "can you speak English" halfway
        # through a booking is not answering the question they were just
        # asked; running that utterance through the date parser produces
        # either a wrong slot or a re-prompt, and either way the request is
        # ignored, which reads as the system not having heard them.
        #
        # The booking flow is NOT abandoned -- session.pending is untouched,
        # so the very next turn resumes exactly where it was, now in the new
        # language.
        switched = lang_mod.requested_switch(text)
        if switched and switched != session.lang:
            logger.info("[%s] caller switched language: %s -> %s",
                        session.call_id, session.lang, switched)
            audit.intent("language_switch", "keyword", language_from=session.lang,
                         language_to=switched)
            session.lang = switched
            await _speak(session, language_switch_reply(session.lang))
            return
        unavailable = lang_mod.requested_switch_unavailable(text)
        if unavailable:
            # Told the truth rather than ignored. A caller asking for a
            # language this pod has no ASR checkpoint for will otherwise ask
            # again, and again, burning turns on a line that can never say
            # yes -- see agent/language.py's enabled().
            logger.info("[%s] caller asked for unavailable language %s",
                        session.call_id, unavailable)
            audit.intent("language_unavailable", "keyword", requested=unavailable)
            await _speak(session, language_unavailable_reply(session.lang))
            return

        # ADDED BY SOURAV -- production bug: replies were spoken only in
        # Bengali no matter what language the caller actually used (see
        # detect_language()'s own docstring in agent/bn_normalize.py for
        # the full writeup). Detected fresh from THIS turn's own utterance
        # -- not carried over from a previous turn -- since a caller can
        # code-switch mid-call and every reply should reflect what they
        # just said. Every reply_templates.py function below already
        # accepted a language= argument; it was simply never passed.
        language = detect_language(text)

        # How much did the two decoders agree about what was said? Logged on
        # every turn -- not for debugging, but because the floors in
        # agent/confidence.py are REASONED and cannot become measured until
        # there is a body of these lines to sweep a threshold against. No
        # transcript is logged; only the score, the decoder and the zone.
        turn_zone = confidence.zone(asr_result)
        # "n/a" rather than a number when the decoders were not compared --
        # %.2f would raise on None, and printing 0.00 there would be the same
        # lie the sentinel used to tell.
        _agree = asr_result.decoder_agreement
        logger.info("[%s] asr agreement=%s decoder=%s words=%d/%d zone=%s",
                    session.call_id,
                    "n/a" if _agree is None else f"{_agree:.2f}",
                    asr_result.decoder_used,
                    asr_result.ctc_words, asr_result.rnnt_words, turn_zone)
        # Structured export for the correlation study. Signal and join key
        # only -- never the transcript. See agent/turn_log.py.
        turn_log.record(session.call_id, session.utt_seq, asr_result, turn_zone,
                        call_state=session.call_state)

        if turn_zone == confidence.REJECT:
            # Both decoders produced text and disagreed about nearly all of it.
            # Acting on either version is a guess, so the turn buys nothing and
            # is not worth reading back either -- ask again instead. Any booking
            # in progress is left intact: the caller has not withdrawn it, this
            # one utterance was simply not understood.
            logger.info("[%s] turn rejected on low decoder agreement", session.call_id)
            await _speak(session, "দুঃখিত, ভালো করে শুনতে পাইনি। আরেকবার বলবেন?")
            return

        # ---- answer to a "did I hear you right?" echo -----------------------
        # Handled HERE rather than in _continue_pending, and before its
        # universal is_negative() escape, because "না" means two different
        # things in the two places. In a booking flow it abandons the booking;
        # here it means "you misheard me", and must leave any booking in
        # progress exactly as it was.
        resumed_from_confirm = False
        if session.pending and session.pending.get("awaiting") == "confirm_transcript":
            echoed = session.pending
            session.pending = echoed.get("resume")   # put the real flow back
            if is_affirmative(text):
                # Confirmed. Continue this turn with what was originally heard,
                # not with the word "yes".
                logger.info("[%s] caller confirmed the transcript", session.call_id)
                text = echoed["heard"]
                resumed_from_confirm = True
            elif is_negative(text):
                logger.info("[%s] caller rejected the transcript", session.call_id)
                await _speak(session, "ঠিক আছে, আরেকবার বলবেন?")
                return
            else:
                # Neither yes nor no -- callers usually just say the thing
                # again rather than answering. Treat this utterance as a fresh
                # turn: it carries its own confidence score and gets judged on
                # its own merits below.
                logger.info("[%s] transcript echo answered with a restatement",
                            session.call_id)

        # ---- criterion 1: below the floor, check before acting -------------
        # The decoders disagreed enough that acting on this transcript would be
        # a guess. Reads are included, not just writes: a wrong price spoken
        # confidently is the same class of failure as a wrong booking, and
        # inside a booking flow this is the ONLY place a misheard field is
        # caught -- the final readback faithfully reads back whatever was
        # captured, so a name misheard three turns earlier is confirmed by a
        # caller who hears their own answer echoed correctly.
        skip_zones = ("confirm_transcript", "confirm_booking")
        already_confirming = bool(session.pending) and \
            session.pending.get("awaiting") in skip_zones
        if turn_zone == confidence.CONFIRM and not resumed_from_confirm \
                and not already_confirming:
            session.confirm_attempts += 1
            if session.confirm_attempts > 2:
                # Three in a row means the line, not the utterance, is the
                # problem. Offer a human rather than ask a fourth time.
                logger.info("[%s] repeated low-agreement turns -- offering handoff",
                            session.call_id)
                session.confirm_attempts = 0
                await _speak(session, "লাইনটা পরিষ্কার শোনা যাচ্ছে না। "
                                      "কাউন্টারে একবার কথা বলে নিলে ভালো হয়।")
                return
            logger.info("[%s] echoing transcript for confirmation (attempt %d)",
                        session.call_id, session.confirm_attempts)
            session.pending = {
                "awaiting": "confirm_transcript", "slots": {}, "candidates": None,
                "offered_date": None, "retries": 0,
                "heard": text,             # replayed verbatim once confirmed
                "resume": session.pending,  # the flow this interrupted
            }
            await _speak(session, heard_confirm_prompt(text))
            return

        # The turn is trusted from here on.
        session.confirm_attempts = 0

        # A booking (or the doctor-choice / date-confirm step just before
        # one) already in progress owns this turn -- see _continue_pending's
        # docstring for why intent extraction must NOT also run on top of it.
        if await _continue_pending(session, text):
            # story title: A multi-part question is answered in full
            # The flow that was holding the turn may have just finished, and
            # something the caller asked before it is still waiting.
            await _drain_deferred(session, text)
            return

        try:
            data = await _resolve_intent(session, text)
        except ExtractionError as e:
            logger.error("[%s] intent extraction failed: %s", session.call_id, e)
            audit.error("intent_extraction", e, handled=True)
            await _speak(session, _t(session.lang, "fallback.llm_failure"), fallback_reason="llm_failure")
            return

        # story title: A multi-part question is answered in full
        # user story: As a caller who asked two things, I want both answered,
        #   so that I do not have to ask again.
        # acceptance criteria: Every answerable part of a turn is answered in
        #   the order asked, and any part that cannot be answered is
        #   explicitly addressed rather than dropped. Completeness is scored
        #   on a labelled multi-part set.
        #
        # normalise() always returns at least one part and guarantees that
        # parts[0] is the top-level intent and slots -- so a model that never
        # emits `parts` produces exactly the single-part turn this agent had
        # before the story, through the same code path.
        #
        # ADDED BY SOURAV's "Caller asks two questions in one breath" story
        # originally dispatched multiple intents via its own `intents` array
        # and _dispatch_multi_intent_turn(); that array never reaches this
        # branch (agent/llm.py emits `parts`, not `intents` -- see this
        # branch's own "parts" wrapper story above), so multi-part turns are
        # routed through turn_parts/_run_parts() here instead. A single-part
        # turn falls through to the full single-intent chain below rather
        # than into _answer_part() (which backs _run_parts() and still only
        # covers the intents this branch had before dev_sourav's stories
        # landed) -- porting those stories into _answer_part() so a
        # multi-part turn can reach them too is follow-up work, not part of
        # this merge.
        parts = turn_parts.normalise(data)
        if turn_parts.is_multi(parts):
            logger.info("[%s] %d-part turn: %s", session.call_id, len(parts),
                        [p["intent"] for p in parts])
            if not await _run_parts(session, text, parts):
                await _drain_deferred(session, text)
            return

        intent = data["intent"]
        slots = data["slots"]

        # ADDED BY SOURAV -- "Caller asks a follow-up that depends on the
        # previous answer" story. Runs BEFORE any of the per-intent
        # branches below, for every intent (a no-op for the many intents
        # agent/state.py does not apply to at all -- see
        # primary_slot_for_intent()'s own comment). Two outcomes:
        #   - `slots` comes back with the intent's primary entity slot
        #     silently filled in from the last thing actually discussed,
        #     when the caller left it null this turn (a pronoun/elliptical
        #     follow-up) and exactly one candidate is tracked -- every
        #     branch below runs completely unaware anything was backfilled.
        #   - `ambiguous_kind` is set instead when that slot is null AND
        #     more than one different entity of that kind was discussed a
        #     moment ago (compare_options naming two tests, say) -- the
        #     turn stops HERE, asks which one was meant, and remembers
        #     enough (intent + already-known slots) to resume the exact
        #     same question once the caller answers (see
        #     _continue_pending's new "follow_up_clarification" state).
        _state = _session_state(session)
        ambiguous_kind = None
        if _state is not None:
            slots, ambiguous_kind = resolve_follow_up(_state, intent, slots)
        if ambiguous_kind:
            candidates = list(_state.slot_for(ambiguous_kind).names)
            session.pending = {
                "awaiting": "follow_up_clarification", "intent": intent, "slots": slots,
                "kind": ambiguous_kind, "candidates": candidates, "retries": 0,
            }
            await _speak(session, ambiguous_reference_reply(ambiguous_kind, candidates, language=language))
            return

        if intent == "smalltalk":
            await _speak(session, data.get("direct_reply_bn") or _t(session.lang, "fallback.greeting"))
            return

        if intent == "unclear":
            # UPDATED BY SOURAV -- wires up the business's own
            # human_fallback config (lab_tests_with_fallback_config sample
            # file's voice_agent_config.human_fallback block):
            # trigger_condition "query_unresolved_or_low_confidence" maps
            # onto this codebase's existing "unclear" intent (see
            # agent/llm.py's own docstring for exactly when the classifier
            # returns it) -- the one real, already-existing signal for
            # "the caller's query could not be resolved". This branch used
            # to speak a single fixed Bengali-only "sorry, please repeat"
            # line with no language selection at all; it now speaks the
            # business's own per-language "connecting you to an expert"
            # script instead, and records the handoff to the same
            # escalation ledger agent/outcomes.py already maintains -- see
            # human_fallback_reply()'s and record_human_handoff()'s own
            # docstrings for why the config's requested action
            # ("transfer_to_human_agent") is honestly logged rather than
            # literally transferred: this codebase has no telephony
            # transfer capability of any kind to actually do that.
            record_human_handoff(intent, call_id=session.call_id)
            await _speak(session, human_fallback_reply(language=language))
            return

        if intent == "out_of_scope":
            # ADDED BY SOURAV -- "Caller asks something the agent does not
            # cover" story. Distinct from "unclear" just above: the
            # classifier understood EXACTLY what the caller wants here
            # (see agent/llm.py's own "out_of_scope" vs "unclear"
            # distinction) -- it is simply not a service any intent above
            # covers. Rather than apologize-and-connect immediately (the
            # "unclear" path) or silently force it into a lookalike real
            # intent, this offers the caller an explicit choice and acts
            # on whichever they pick next turn -- see _continue_pending's
            # "out_of_scope_choice" branch below.
            await _speak(session, out_of_scope_reply(language=language))
            session.pending = {"awaiting": "out_of_scope_choice", "retries": 0}
            return

        try:
            if intent == "test_rate":
                if not slots.get("test_name"):
                    await _speak(session, missing_slot_prompt(intent, "test_name", language=language))
                    return
                result = await _tools.get_test_rate(slots["test_name"])
                await _speak(session, test_rate_reply(slots, result, language=language))
                _remember_primary_entity(session, intent, slots, result)

            elif intent == "test_sample":
                # UPDATED BY SOURAV -- restores parity with main_pcm.py,
                # which already had this branch (Story 5, "caller asks what
                # sample is needed") while main.py never did. Found while
                # wiring the report_status/report_send combined story:
                # main_pcm.py is a GENERATED file (see its own module
                # docstring and tools/make_pcm_variant.py) meant to be
                # produced FROM main.py, but this branch was added by hand
                # directly to main_pcm.py at some point without re-running
                # the generator off an updated main.py -- so a caller on
                # the WAV transport (main.py, ports 8080/8100 per
                # deploy/start_all.sh) asking only about sample type hit no
                # matching branch at all, even though agent/llm.py
                # classifies "test_sample" correctly on either transport.
                # Restoring it here BEFORE regenerating main_pcm.py from
                # this file closes that gap for good: from now on
                # main_pcm.py is only ever produced by re-running that
                # script against this file, so the two cannot drift apart
                # on this branch (or the three this story adds) again.
                # Same tool call as test_rate -- clinic-api's test lookup
                # already returns sample_type on every call, nothing new
                # was added to the API for this -- only the reply function
                # differs, so a caller who asked ONLY about the sample
                # hears just that, not the bundled rate+sample+duration
                # answer test_rate gives.
                if not slots.get("test_name"):
                    await _speak(session, missing_slot_prompt(intent, "test_name", language=language))
                    return
                result = await _tools.get_test_rate(slots["test_name"])
                await _speak(session, sample_type_reply(slots, result, language=language))
                _remember_primary_entity(session, intent, slots, result)

            elif intent == "test_duration":
                # ADDED BY SOURAV -- fixes a real production bug, reported
                # directly from a live call transcript:
                #   [User] How long does it take to get the urine test report?
                #   [AI]   Urine test rate is 200 taka.
                # A caller asking about REPORT TURNAROUND TIME was being
                # misclassified as "test_rate" and answered with the
                # test's PRICE instead. Root cause: an earlier story
                # ("Caller asks the price of a test") narrowed
                # test_rate_reply() to speak ONLY the price, but
                # agent/llm.py's intent prompt was never updated to match
                # -- it kept telling the classifier that "how long results
                # take" belongs to test_rate. See test_duration_reply()'s
                # own docstring for the full writeup, including a second,
                # related bug found and fixed in agent/fast_path.py.
                # Same tool call as test_rate/test_sample -- clinic-api's
                # test lookup already returns report_time_hours on every
                # call, nothing new was added to the API for this -- only
                # the reply function differs, so a caller who asked ONLY
                # about turnaround time hears just that, never the price
                # or the sample.
                if not slots.get("test_name"):
                    await _speak(session, missing_slot_prompt(intent, "test_name", language=language))
                    return
                result = await _tools.get_test_rate(slots["test_name"])
                await _speak(session, test_duration_reply(slots, result, language=language))
                _remember_primary_entity(session, intent, slots, result)

            elif intent == "test_preparation":
                # ADDED BY SOURAV -- "Caller asks how to prepare for a
                # test" story. Unlike test_rate/test_sample/test_duration
                # just above, this calls a DEDICATED new endpoint
                # (get_test_preparation -> GET /api/v1/tests/preparation)
                # rather than reusing get_test_rate's response, since
                # preparation data (fasting rules, medication holds,
                # per-language ready-to-speak scripts) is not part of that
                # payload at all -- see clinic-api/main.py's
                # _test_preparation_reply_dict() for the response shape.
                # Same test_name-required gate as those three: "how do I
                # prepare" has no "list every test's prep instructions"
                # analog the way health_package's bare "what packages do
                # you have" does, so a missing test_name always re-prompts
                # rather than trying to answer something unbounded.
                if not slots.get("test_name"):
                    await _speak(session, missing_slot_prompt(intent, "test_name", language=language))
                    return
                result = await _tools.get_test_preparation(slots["test_name"])
                await _speak(session, test_preparation_reply(slots, result, language=language))
                _remember_primary_entity(session, intent, slots, result)

            elif intent == "walkin_eligibility":
                # ADDED BY SOURAV -- Phase 1: Database Schema & Policy
                # Tables. Same single-required-slot gate as test_rate/
                # test_preparation above.
                if not slots.get("test_name"):
                    await _speak(session, missing_slot_prompt(intent, "test_name", language=language))
                    return
                result = await _tools.get_walkin_policy(slots["test_name"])
                await _speak(session, walkin_eligibility_reply(slots, result, language=language))
                _remember_primary_entity(session, intent, slots, result)

            elif intent == "prescription_requirements":
                if not slots.get("test_name"):
                    await _speak(session, missing_slot_prompt(intent, "test_name", language=language))
                    return
                result = await _tools.get_prescription_policy(slots["test_name"])
                await _speak(session, prescription_requirements_reply(slots, result, language=language))
                _remember_primary_entity(session, intent, slots, result)

            elif intent == "insurance_coverage":
                # Two required slots, not one -- ask for whichever is
                # still missing, mirroring book_appointment's own
                # merge-onto-pending pattern below (see agent/
                # semantic_cache.py's _is_l2_eligible for why this intent
                # is excluded from L2 entirely, the same reason
                # book_appointment is). A caller who names only the test
                # ("amar CBC insurance-e cover hobe?") gets asked for
                # their insurer next turn, and vice versa; either slot
                # already known this turn is kept.
                merged = dict(session.pending["slots"]) if session.pending else {}
                for field in ("test_name", "insurance_provider_name"):
                    if slots.get(field):
                        merged[field] = slots[field]
                missing = next((f for f in ("test_name", "insurance_provider_name") if not merged.get(f)), None)
                if missing:
                    session.pending = {
                        "awaiting": "insurance_coverage_slot", "slots": merged,
                        "missing_field": missing, "retries": 0,
                    }
                    await _speak(session, missing_slot_prompt(intent, missing, language=language))
                    return
                session.pending = None
                result = await _tools.get_insurance_coverage(merged["test_name"], merged["insurance_provider_name"])
                await _speak(session, insurance_coverage_reply(merged, result, language=language))

            elif intent == "compare_options":
                # ADDED BY SOURAV -- "Caller asks the agent to compare two
                # options" story. Same two-required-slots merge-onto-
                # pending pattern as insurance_coverage just above, for
                # the identical reason (either name can be missing this
                # turn). Once both names are in hand, EACH is resolved
                # independently via _resolve_comparable_entity() (tries
                # the test catalogue, then the package catalogue -- see
                # that function's own docstring) before
                # agent/compare_flow.py's build_comparison() computes the
                # actual price/component comparison in code -- this
                # branch itself does no arithmetic and makes no
                # recommendation; it only fetches both sides' live data
                # and hands it to compare_flow/reply_templates.
                merged = dict(session.pending["slots"]) if session.pending else {}
                for field in ("compare_option_a", "compare_option_b"):
                    if slots.get(field):
                        merged[field] = slots[field]
                missing = next((f for f in ("compare_option_a", "compare_option_b") if not merged.get(f)), None)
                if missing:
                    session.pending = {
                        "awaiting": "compare_options_slot", "slots": merged,
                        "missing_field": missing, "retries": 0,
                    }
                    await _speak(session, missing_slot_prompt(intent, missing, language=language))
                    return
                session.pending = None
                name_a, name_b = merged["compare_option_a"], merged["compare_option_b"]
                entity_a, entity_b = await _resolve_comparable_entity(name_a), await _resolve_comparable_entity(name_b)
                comparison = build_comparison(entity_a, entity_b)
                await _speak(session, compare_options_reply(name_a, name_b, entity_a, entity_b, comparison, language=language))
                _remember_compared_entities(session, entity_a, entity_b)

            elif intent == "billing_balance":
                # ADDED BY SOURAV -- Phase 1: Outstanding Balance / Billing
                # story. Identity resolved by PHONE (RULE 14/15), same
                # gate as report_status/report_send below -- deliberately
                # NOT behind OTP (see clinic-api/models.py's
                # PatientBilling docstring for that scoping decision).
                phone = parse_phone(slots.get("phone") or "")
                if not phone:
                    session.pending = {"awaiting": "billing_phone", "retries": 0}
                    await _speak(session, missing_slot_prompt(intent, "phone", language=language))
                    return
                result = await _tools.get_patient_billing(phone)
                await _speak(session, billing_balance_reply(result, language=language))

            elif intent == "report_status":
                # ADDED BY SOURAV -- "Lab Report Status & Secure Delivery"
                # combined story. Identity is resolved by PHONE, never by
                # name (RULE 14/15) -- if the caller's utterance didn't
                # carry one, ask for it and park in the "phone" pending
                # state above rather than guessing or proceeding without it.
                phone = parse_phone(slots.get("phone") or "")
                if not phone:
                    session.pending = {
                        "awaiting": "report_phone", "flow": "report_status",
                        "test_name": slots.get("test_name"), "retries": 0,
                    }
                    await _speak(session, missing_slot_prompt(intent, "phone", language=language))
                    return
                await _handle_report_lookup(session, phone, slots.get("test_name"), "report_status", language=language)

            elif intent == "report_send":
                # ADDED BY SOURAV -- same identity-by-phone gate as
                # report_status just above; the two intents share
                # _handle_report_lookup/_finish_report_flow and differ only
                # in `flow`, which agent/report_flow.py's
                # interpret_report_status_result() uses to decide whether a
                # READY+enabled report gets the "shall I send it?" offer
                # (report_status) or goes straight to requesting delivery
                # (report_send, since the caller already asked for it).
                phone = parse_phone(slots.get("phone") or "")
                if not phone:
                    session.pending = {
                        "awaiting": "report_phone", "flow": "report_send",
                        "test_name": slots.get("test_name"), "retries": 0,
                    }
                    await _speak(session, missing_slot_prompt(intent, "phone", language=language))
                    return
                await _handle_report_lookup(session, phone, slots.get("test_name"), "report_send", language=language)

            elif intent == "doctor_availability":
                if not slots.get("doctor_name"):
                    await _speak(session, missing_slot_prompt(intent, "doctor_name", language=language))
                    return
                # Default to TODAY, not "whenever next available": a bare
                # "ডাক্তার সেন আছেন?" with no date mentioned is a caller
                # asking about right now, and the reply text below already
                # said " আজ" (today) for exactly this case -- the old code
                # passed date=None through to the API, which answers a
                # different question ("when next"), so a doctor who simply
                # wasn't in today got reported by their NEXT sitting date
                # instead of "not today, but they're on Tuesdays" etc.
                date_iso = slots.get("date") or datetime.date.today().isoformat()
                result = await _tools.get_doctor_availability(slots["doctor_name"], date_iso)
                await _speak(session, doctor_availability_reply(slots, result, language=language))
                _remember_primary_entity(session, intent, slots, result)

                # Keep the flow open for "yes, book that day" / "another
                # day" -- doctor_availability_reply() just asked exactly
                # that question. See _continue_pending's "date" state.
                offered = None
                if result.get("found"):
                    offered = result.get("date") if result.get("available") else result.get("next_available_date")
                session.pending = {
                    "awaiting": "date",
                    "slots": {"doctor_name": result.get("doctor_name") or slots["doctor_name"]},
                    "candidates": None, "offered_date": offered, "retries": 0,
                } if offered else None

            elif intent == "doctor_schedule":
                # ADDED BY SOURAV -- "Caller asks when a doctor sits" story.
                # Deliberately DATE-FREE, unlike doctor_availability just
                # above: this intent exists exactly for the caller who has
                # NOT named a day and wants the doctor's general recurring
                # weekly schedule instead (see agent/llm.py's SYSTEM_PROMPT
                # for how the two are told apart at classification time,
                # and clinic-api/main.py::doctor_schedule()'s docstring for
                # the response shape). No `date` slot is read or passed
                # here at all -- even if the LLM happened to also extract
                # one from the same utterance, it is not used, since a
                # date would silently turn this back into the OTHER
                # question this intent exists to be distinct from.
                #
                # Same known limitation as doctor_availability just above,
                # not introduced here: no pending state is opened when
                # doctor_name is missing, so the caller's next utterance
                # (e.g. a bare doctor's name in reply to the prompt) goes
                # through a fresh LLM classification rather than a
                # targeted single-slot fill. Flagged, not fixed -- fixing
                # it would mean touching doctor_availability's identical
                # gap too, which is out of this story's scope.
                if not slots.get("doctor_name"):
                    await _speak(session, missing_slot_prompt(intent, "doctor_name", language=language))
                    return
                result = await _tools.get_doctor_schedule(slots["doctor_name"])
                await _speak(session, doctor_schedule_reply(slots, result, language=language))
                _remember_primary_entity(session, intent, slots, result)

            elif intent == "doctors_by_department":
                if not slots.get("department"):
                    await _speak(session, missing_slot_prompt(intent, "department", language=language))
                    return
                # Default to TODAY when the caller didn't name a date, same
                # reasoning as doctor_availability above: "অর্থোতে কারা
                # আছেন" (who's in ortho) is almost always asking who is
                # actually in the chamber right now, not for a roster of
                # every doctor the department has ever employed regardless
                # of whether they sit this week. Only an EXPLICIT date
                # bypasses this (used as-is below).
                date_iso = slots.get("date") or datetime.date.today().isoformat()
                result = await _tools.get_doctors_by_department(slots["department"], date_iso)
                await _speak(session, doctors_by_department_reply(slots, result, language=language))

                # Continue straight into booking: offer the doctors just
                # listed as candidates, so the caller's very next utterance
                # -- which may be nothing but a bare doctor name -- is
                # matched against THIS list rather than sent to the LLM with
                # no context to interpret it against. See _continue_pending's
                # "doctor_choice" state.
                if result.get("found") and result.get("doctors"):
                    session.pending = {
                        "awaiting": "doctor_choice",
                        "slots": {},
                        "candidates": [
                            {"name": d["name"], "name_bn": d.get("doctor_name_bn")}
                            for d in result["doctors"]
                        ],
                        "offered_date": date_iso,
                        "retries": 0,
                    }
                elif result.get("found"):
                    # Department exists but nobody sits that day --
                    # doctors_by_department_reply() just told the caller
                    # exactly that and invited another day ("অন্য কোনো
                    # দিনের কথা জিজ্ঞেস করতে পারেন"). Stay in the flow so the
                    # caller's next utterance is interpreted as THAT date
                    # instead of needing to restate the whole department
                    # question from scratch -- see _continue_pending's
                    # "department_date" state.
                    session.pending = {
                        "awaiting": "department_date",
                        "slots": {"department": slots["department"]},
                        "candidates": None, "offered_date": None, "retries": 0,
                    }
                else:
                    session.pending = None

            elif intent == "payment":
                # THIS BRANCH MUST NOT BE ABLE TO FAIL.
                #
                # The story it serves is "every flow completes without a
                # smartphone", and a flow that answers "I couldn't check
                # that right now" has dead-ended just as surely as one that
                # sends a payment link -- the caller is left holding
                # nothing either way. So the rate lookup is decoration: if
                # the caller named a test we quote the amount, and if
                # clinic-api is down we still tell them HOW to pay, which
                # is what they actually asked and which never depended on
                # the database.
                result = {}
                if slots.get("test_name"):
                    try:
                        result = await _tools.get_test_rate(slots["test_name"])
                    except ToolCallError as e:
                        logger.warning("[%s] rate lookup failed during payment reply: %s",
                                       session.call_id, e)
                await _speak(session, payment_reply(slots, result, session.lang))

            elif intent == "report_collection":
                # Same rule as payment above: the collection path is clinic
                # policy, not a database row, so it survives clinic-api
                # being unreachable. Only the "ready in N hours" part needs
                # the lookup, and its absence is handled by the template.
                result = {}
                if slots.get("test_name"):
                    try:
                        result = await _tools.get_test_rate(slots["test_name"])
                    except ToolCallError as e:
                        logger.warning("[%s] rate lookup failed during report reply: %s",
                                       session.call_id, e)
                await _speak(session, report_collection_reply(slots, result, session.lang))

            elif intent == "patient_history":
                # NOTHING IS FETCHED UNTIL VERIFICATION PASSES. The lookup
                # is not performed and then withheld -- it is not performed
                # at all, so there is nothing in this process to leak.
                if session.history_token:
                    await _speak_history(session)
                else:
                    # A number this call was already given is reused, not
                    # asked for again.
                    await _start_history_verification(
                        session, slots.get("phone") or session.history_phone)

            elif intent == "my_bookings":
                # A SINGLE PATIENT TIMELINE. The same rule as history above:
                # nothing is fetched before verification, and a caller who
                # has verified on this call is answered from the timeline
                # already held -- no second challenge, no second fetch.
                if session.history_token:
                    await _speak_bookings(session)
                else:
                    await _start_history_verification(
                        session, slots.get("phone") or session.history_phone,
                        PURPOSE_BOOKINGS)

            elif intent == "book_appointment":
                # Merge onto whatever session.pending already knows (e.g. a
                # doctor_name carried over from a doctor_availability or
                # doctors_by_department turn moments ago) rather than
                # requiring every field in one utterance -- that all-or-
                # nothing check was the other half of "pipeline breaking":
                # a caller who gave the doctor and date in one sentence and
                # the time in the next used to have the doctor/date silently
                # discarded the moment ANY field was still missing.
                merged = dict(session.pending["slots"]) if session.pending else {}
                for field in _BOOKING_FIELDS:
                    if slots.get(field):
                        merged[field] = slots[field]
                # Neither a verified caller nor a patient writing from their
                # own number is asked for what is already known.
                _fill_from_channel(session, merged)
                from_record = _fill_from_record(session, merged)

                missing = _next_missing(merged)
                if missing is None:
                    # ADDED BY CHAKRAVARDHAN -- a booking whose name/phone
                    # came from the verified caller's record was already
                    # confirmed through that verification step (see
                    # _finish_booking()'s own docstring), so it skips
                    # straight to the write rather than a spoken readback.
                    if from_record:
                        await _finish_booking(session, merged, from_record=True,
                                              language=language)
                        return
                    # A caller who gave all 5 fields in one breath still
                    # gets the pre-write readback -- this is the SAME gap
                    # the multi-turn flow had (see _continue_pending's
                    # "confirm_booking" state): a single-shot utterance is
                    # exactly as capable of a misheard phone digit as one
                    # collected field-by-field.
                    session.pending = {
                        "awaiting": "confirm_booking", "slots": merged, "candidates": None,
                        "offered_date": (session.pending or {}).get("offered_date"), "retries": 0,
                    }
                    await _speak(session, booking_confirmation_prompt(merged, language=language))
                    return

                session.pending = {
                    "awaiting": missing, "slots": merged, "candidates": None,
                    "offered_date": (session.pending or {}).get("offered_date"), "retries": 0,
                    "from_record": from_record,
                }
                await _speak(session, missing_slot_prompt(intent, missing, language=language))

            elif intent == "request_callback":
                # ADDED BY SOURAV -- "Caller asks to be called back" story
                # (Evidence: "No outbound capability"). Availability is
                # checked FIRST, before asking for a single detail --
                # Acceptance Criterion 3: a caller is never walked through
                # collecting a time window and phone number only to be
                # refused at the very end. CALLBACKS_ENABLED is checked
                # BEFORE ever calling get_clinic_info() -- a deployment
                # that has turned the feature off entirely has no reason
                # to pay for that round-trip (cached or not) just to
                # decide something a config constant already answered.
                if not CALLBACKS_ENABLED:
                    await _speak(session, callback_unavailable_reply("disabled", language=language))
                    return

                # get_clinic_info() is the SAME already-cached call
                # clinic_info's own branch above makes (agent/
                # reference_data_cache.py) -- no new tool, no new network
                # round-trip pattern.
                hours_result = await _tools.get_clinic_info()
                hours = hours_result.get("hours") if hours_result.get("found") else None
                availability = check_callback_availability(
                    hours, datetime.date.today().weekday(),
                    datetime.datetime.now().strftime("%H:%M"), CALLBACKS_ENABLED,
                )
                if not availability["available"]:
                    await _speak(session, callback_unavailable_reply(availability["reason"], language=language))
                    return

                # Merge onto whatever session.pending already knows, same
                # "don't discard a field the caller already gave" reasoning
                # as book_appointment just above -- a caller who names a
                # time window AND a phone number in one breath should never
                # be asked for either again.
                merged = dict(session.pending["slots"]) if session.pending else {}
                for field in (*_CALLBACK_FIELDS, "callback_reason"):
                    if slots.get(field):
                        merged[field] = slots[field]

                # Acceptance Criterion 1's "preserving the conversation
                # context and reason" -- resolved ONCE, here, on the turn
                # that actually opens this flow (session.state reflects
                # whatever was discussed earlier in THIS call right now;
                # nothing about it changes while the rest of this flow
                # collects the remaining fields over the next turns, so
                # there is no benefit to re-resolving it later, only risk
                # of it drifting from what was true when the caller asked).
                # Always stored, even as null (build_callback_reason()'s own
                # "no reason given" case) -- see agent/callback_flow.py's
                # own docstring for why that null is never papered over
                # with an invented generic reason.
                if "callback_reason" not in merged:
                    state = _session_state(session)
                    merged["callback_reason"] = build_callback_reason(
                        slots.get("callback_reason"),
                        active_test=state.slot_for("test").primary if state else None,
                        active_doctor=state.slot_for("doctor").primary if state else None,
                        active_package=state.slot_for("package").primary if state else None,
                    )

                missing = _next_missing_callback(merged)
                if missing is None:
                    # Same "every critical value is read back before it is
                    # used" discipline as book_appointment's own
                    # confirm_booking state just above.
                    session.pending = {
                        "awaiting": "confirm_callback", "slots": merged, "candidates": None,
                        "offered_date": None, "retries": 0,
                    }
                    await _speak(session, callback_confirmation_prompt(merged, language=language))
                    return

                session.pending = {
                    "awaiting": missing, "slots": merged, "candidates": None,
                    "offered_date": None, "retries": 0,
                }
                await _speak(session, missing_slot_prompt("request_callback", missing, language=language))

            elif intent == "health_package":
                # ADDED BY SOURAV -- "Caller asks about a health package"
                # story. Deliberately NEVER re-prompts for a missing
                # "package_name" the way every single-entity intent above
                # does for its own required slot -- see agent/llm.py's own
                # comment on VALID_INTENTS: a caller who names no package
                # at all is asking a complete, different, equally valid
                # question ("what packages do you have"), backed by its
                # own clinic-api list endpoint, not an incomplete
                # extraction waiting on a re-prompt.
                if slots.get("package_name"):
                    result = await _tools.search_health_package(slots["package_name"])
                    await _speak(session, health_package_reply(slots, result, language=language))
                    _remember_primary_entity(session, intent, slots, result)
                else:
                    result = await _tools.get_health_packages()
                    await _speak(session, health_packages_list_reply(result, language=language))

            elif intent == "clinic_info":
                # ADDED BY SOURAV -- "Caller asks opening hours, address or
                # directions" story. No slot is required to call the tool
                # (clinic-api/models.py's ClinicInfo is a singleton table) --
                # "info_topic" only narrows which part of the already-
                # fetched answer gets SPOKEN, in clinic_info_reply() itself.
                # "today_weekday" resolves "which day" here, in dispatch,
                # the same way doctor_availability's date_iso default does
                # just above -- reply_templates.py never imports datetime
                # itself (see clinic_info_reply()'s own docstring).
                result = await _tools.get_clinic_info()
                info_slots = {
                    "info_topic": slots.get("info_topic"),
                    "today_weekday": datetime.date.today().weekday(),
                }
                await _speak(session, clinic_info_reply(info_slots, result, language=language))

        except ToolCallError as e:
            logger.error("[%s] clinic API call failed: %s", session.call_id, e)
            await _speak(session, "এই মুহূর্তে দেখতে পারছি না। কাউন্টারে যোগাযোগ করুন, দয়া করে।",
                         fallback_reason="tool_failure")


# ADDED BY SOURAV -- "Caller asks two questions in one breath" story. Sets
# of intents that never get a real inline answer in a COMBINED turn,
# regardless of slot completeness -- see _resolve_combinable_intent_fragment's
# own docstring just below for why each is excluded rather than composed.
_MULTI_INTENT_NEEDS_SEPARATE_FLOW = {"book_appointment", "report_status", "report_send"}
_MULTI_INTENT_NO_FRAGMENT = {"smalltalk", "unclear"}


async def _resolve_combinable_intent_fragment(intent: str, slots: dict, language: str) -> str | None:
    """ADDED BY SOURAV -- "Caller asks two questions in one breath" story.
    Resolves ONE intent (one entry of a multi-question turn's "intents"
    array) into its own spoken fragment for _dispatch_multi_intent_turn()
    below to join with the others, in order.

    Returns None ONLY for "smalltalk"/"unclear" -- see
    _MULTI_INTENT_NO_FRAGMENT above: neither is really a second QUESTION
    the caller needs an honest answer or acknowledgment for (a bare "ভালো
    আছেন?" tacked onto a real question is not something Criterion 2's
    "explicitly acknowledge the unanswerable one" was written for), so
    these two are the sole, deliberate exception to "nothing is silently
    dropped." Every other intent returns a real, non-empty fragment, one
    of:
      - The SAME reply_templates function a solo turn for that intent
        would use, called exactly the same way (a "found but not yet
        reviewed" policy row -- Criterion 2's "unreviewed" example --
        already gets an honest sentence for free from that same function,
        e.g. walkin_eligibility_reply(); zero new code needed for that
        case specifically).
      - multi_intent_missing_info_reply() when an otherwise-combinable
        intent is missing a slot it needs (test_name, doctor_name,
        department, or -- for insurance_coverage/billing_balance -- either
        of their two/one required fields). Deliberately generic rather
        than a targeted per-field re-prompt: a combined turn does not open
        a SECOND pending state on top of whatever the turn's other
        question may already need to report, so there is nowhere to
        attach a targeted follow-up question to (see
        _dispatch_multi_intent_turn's own docstring).
      - multi_intent_out_of_scope_reply() for "out_of_scope" -- a
        non-interactive acknowledgment, unlike out_of_scope_reply()'s
        solo interactive yes/no offer (again: no second pending state).
      - multi_intent_needs_separate_flow_reply() for book_appointment/
        report_status/report_send (_MULTI_INTENT_NEEDS_SEPARATE_FLOW) --
        ALWAYS, even when every slot they'd need already happens to be
        present. These three are multi-turn, sometimes security-sensitive
        (OTP) flows in their own right, not something to fold into a
        shared reply alongside an unrelated question.

    Deliberately narrower than the solo dispatch branches in _dispatch_turn's
    own if/elif chain above: this NEVER opens a follow-up pending state of
    any kind, even for an intent that would open one solo (doctor_
    availability's "book this day?" offer, doctors_by_department's
    candidate list, insurance_coverage's/billing_balance's slot-fill
    prompt) -- composing two independently-stateful sub-conversations into
    one reply is a different, larger problem than "answer both questions
    honestly in one turn."
    """
    if intent in _MULTI_INTENT_NO_FRAGMENT:
        return None

    if intent == "out_of_scope":
        return multi_intent_out_of_scope_reply(language=language)

    if intent in _MULTI_INTENT_NEEDS_SEPARATE_FLOW:
        return multi_intent_needs_separate_flow_reply(language=language)

    if intent == "test_rate":
        if not slots.get("test_name"):
            return multi_intent_missing_info_reply(language=language)
        result = await _tools.get_test_rate(slots["test_name"])
        return test_rate_reply(slots, result, language=language)

    if intent == "test_sample":
        if not slots.get("test_name"):
            return multi_intent_missing_info_reply(language=language)
        result = await _tools.get_test_rate(slots["test_name"])
        return sample_type_reply(slots, result, language=language)

    if intent == "test_duration":
        if not slots.get("test_name"):
            return multi_intent_missing_info_reply(language=language)
        result = await _tools.get_test_rate(slots["test_name"])
        return test_duration_reply(slots, result, language=language)

    if intent == "test_preparation":
        if not slots.get("test_name"):
            return multi_intent_missing_info_reply(language=language)
        result = await _tools.get_test_preparation(slots["test_name"])
        return test_preparation_reply(slots, result, language=language)

    if intent == "walkin_eligibility":
        if not slots.get("test_name"):
            return multi_intent_missing_info_reply(language=language)
        result = await _tools.get_walkin_policy(slots["test_name"])
        return walkin_eligibility_reply(slots, result, language=language)

    if intent == "prescription_requirements":
        if not slots.get("test_name"):
            return multi_intent_missing_info_reply(language=language)
        result = await _tools.get_prescription_policy(slots["test_name"])
        return prescription_requirements_reply(slots, result, language=language)

    if intent == "insurance_coverage":
        if not slots.get("test_name") or not slots.get("insurance_provider_name"):
            return multi_intent_missing_info_reply(language=language)
        result = await _tools.get_insurance_coverage(slots["test_name"], slots["insurance_provider_name"])
        return insurance_coverage_reply(slots, result, language=language)

    if intent == "billing_balance":
        phone = parse_phone(slots.get("phone") or "")
        if not phone:
            return multi_intent_missing_info_reply(language=language)
        result = await _tools.get_patient_billing(phone)
        return billing_balance_reply(result, language=language)

    if intent == "doctor_availability":
        if not slots.get("doctor_name"):
            return multi_intent_missing_info_reply(language=language)
        date_iso = slots.get("date") or datetime.date.today().isoformat()
        result = await _tools.get_doctor_availability(slots["doctor_name"], date_iso)
        return doctor_availability_reply(slots, result, language=language)

    if intent == "doctor_schedule":
        if not slots.get("doctor_name"):
            return multi_intent_missing_info_reply(language=language)
        result = await _tools.get_doctor_schedule(slots["doctor_name"])
        return doctor_schedule_reply(slots, result, language=language)

    if intent == "doctors_by_department":
        if not slots.get("department"):
            return multi_intent_missing_info_reply(language=language)
        date_iso = slots.get("date") or datetime.date.today().isoformat()
        result = await _tools.get_doctors_by_department(slots["department"], date_iso)
        return doctors_by_department_reply(slots, result, language=language)

    if intent == "health_package":
        if slots.get("package_name"):
            result = await _tools.search_health_package(slots["package_name"])
            return health_package_reply(slots, result, language=language)
        result = await _tools.get_health_packages()
        return health_packages_list_reply(result, language=language)

    if intent == "clinic_info":
        result = await _tools.get_clinic_info()
        info_slots = {
            "info_topic": slots.get("info_topic"),
            "today_weekday": datetime.date.today().weekday(),
        }
        return clinic_info_reply(info_slots, result, language=language)

    # Defensive: every member of VALID_INTENTS (agent/llm.py) is handled
    # explicitly somewhere above -- this is unreachable in practice, but
    # falls back to the same honest "ask that one separately" fragment
    # book_appointment/report_status/report_send get, rather than ever
    # silently dropping an intent this function does not recognize.
    return multi_intent_needs_separate_flow_reply(language=language)


def _remember_multi_intent_entities(session: CallSession, intents_list: list[dict]) -> None:
    """ADDED BY SOURAV -- "Caller asks a follow-up that depends on the
    previous answer" story. A multi-intent turn naming two DIFFERENT
    tests in the same breath (e.g. "CBC-r rate koto, ar Lipid Profile-er
    sample ki lagbe?") is exactly the "multiple entities discussed
    previously" ambiguity Acceptance Criterion 2 describes -- handled
    here, once, after the whole turn's fragments are resolved, so a bare
    pronoun in the NEXT turn cannot silently be guessed as one or the
    other (see agent/state.py's own docstring).

    Deliberately uses the CALLER'S OWN WORDS for each name, not a tool
    result's canonical spelling: _resolve_combinable_intent_fragment()
    above returns only a rendered reply string, not the resolved entity
    data, so reconfirming a canonical name here would mean a second,
    duplicate tool call purely to remember it. This is a smaller
    guarantee than the single-intent path's _remember_primary_entity()
    (which only ever remembers a name a lookup just confirmed exists) --
    flagged, not silently equated with it -- but clinic-api's own lookups
    already tolerate the caller's raw phrasing fine on their own, so a
    follow-up backfilled from a multi-intent turn's memory is no worse
    off than the ORIGINAL turn's own lookup was.
    """
    state = _session_state(session)
    if state is None:
        return
    by_kind: dict[str, list[str]] = {}
    for item in intents_list:
        slot_key = primary_slot_for_intent(item.get("intent"))
        if slot_key is None:
            continue
        name = (item.get("slots") or {}).get(slot_key)
        if not name:
            continue
        by_kind.setdefault(kind_for_slot(slot_key), []).append(name)

    for kind, names in by_kind.items():
        distinct = list(dict.fromkeys(names))  # de-dupe, order-preserved
        if len(distinct) > 1:
            state.mark_ambiguous(kind, distinct)
        else:
            state.mark(kind, distinct[0])


async def _dispatch_multi_intent_turn(session: CallSession, intents_list: list[dict], language: str) -> None:
    """ADDED BY SOURAV -- "Caller asks two questions in one breath" story.
    Only ever called from _dispatch_turn above, and only when
    `len(intents_list) > 1` -- a single-entry list (the overwhelming
    majority of turns: a fast_path hit, a plain single-question LLM
    extraction, or any pre-this-story test mock) takes the ORIGINAL,
    completely untouched if/elif chain in _dispatch_turn instead. Nothing
    about that existing path changes for this story.

    Story Criterion 1 (Order Preservation): `intents_list` is iterated
    below in order, exactly as agent/llm.py's SYSTEM_PROMPT_TEMPLATE
    instructs the model to return it, and each fragment is appended to
    `fragments` in that same order -- no sorting or reordering anywhere.
    The joined reply is strictly in the order asked, by construction.

    Story Criterion 2 (Honest Partial Handling): every intent in
    `intents_list` produces either a real, grounded fragment or one of the
    three honest, non-fabricating fallback fragments -- see
    _resolve_combinable_intent_fragment() above for exactly which and why.
    Nothing is silently dropped; smalltalk/unclear are the sole,
    deliberate exception (see that function's own docstring).

    Tool-failure isolation ("individual tool calls executed independently
    without one tool failure short-circuiting the second"): each intent's
    fragment is resolved inside its OWN try/except ToolCallError in the
    loop below -- unlike the solo dispatch's single try/except wrapping
    its ENTIRE if/elif chain -- so intent #1's clinic-api call failing can
    never prevent intent #2's from running at all.
    """
    fragments: list[str] = []
    for item in intents_list:
        intent = item.get("intent")
        slots = item.get("slots") or {}
        try:
            fragment = await _resolve_combinable_intent_fragment(intent, slots, language)
        except ToolCallError as e:
            logger.error("[%s] clinic API call failed for intent %s (multi-intent turn): %s",
                         session.call_id, intent, e)
            fragment = "এই মুহূর্তে দেখতে পারছি না। কাউন্টারে যোগাযোগ করুন, দয়া করে।"
        if fragment:
            fragments.append(fragment)

    if not fragments:
        # Defensive: every entry was smalltalk/unclear. agent/llm.py's own
        # prompt instructs the model to never split one real question into
        # several entries, so a genuine multi-question turn should not
        # reach this -- but rather than speak nothing at all, fall back to
        # the same honest "connecting you to an expert" handling the solo
        # "unclear" path gives above.
        record_human_handoff("unclear", call_id=session.call_id)
        await _speak(session, human_fallback_reply(language=language))
        return

    await _speak(session, " ".join(fragments))
    _remember_multi_intent_entities(session, intents_list)


async def _resync_after_playback(session: CallSession) -> bool:
    """Drop everything captured while the agent was talking, by moving
    processed_until_s to the current end of the decoded buffer. That region
    is muted silence from the client's side; skipping it keeps the turn
    detector from ever analysing it, and -- more importantly -- keeps
    processed_until_s anchored to real time instead of drifting a full
    reply behind, which is what made later turns surface late."""
    buffer_end_s = session.audio.duration_s
    session.processed_until_s = max(session.processed_until_s,
                                    buffer_end_s - RESYNC_REWIND_S)
    session.resync_pending = False
    logger.info("[%s] resynced to %.2fs after playback", session.call_id, session.processed_until_s)
    return True


async def _recent_mic_tail(session: CallSession, seconds: float):
    """The last `seconds` of captured microphone audio, as (samples, sr).

    TRANSPORT-SPECIFIC -- tools/make_pcm_variant.py swaps this body. Returns
    None when there is not yet enough audio to judge."""
    sr = session.audio.sample_rate
    n = int(seconds * sr)
    total = len(session.audio)
    if total < n:
        return None
    tail = session.audio.tail_tensor((total - n) / sr)
    if tail.numel() == 0:
        return None
    return tail.numpy(), sr


async def _check_barge_in(session: CallSession) -> bool:
    """Is the caller talking over the agent right now?

    This is what replaces the half-duplex mute. It runs only while
    `agent_speaking`, on the most recent window of microphone audio, and
    asks agent/echo_guard.py to separate our own echo from a real
    interruption -- using the audio we just played as the reference.

    Returns True when playback was stopped and the caller's turn should be
    detected normally from here.

    Deliberately conservative: EchoGuard.assess answers "echo" whenever it
    is unsure, so a doubtful case leaves the agent speaking. A false
    barge-in truncates a reply the caller then never hears, which is worse
    than a missed one -- they can always speak again."""
    if not ECHO_CFG.barge_in_enabled:
        return False

    tail = await _recent_mic_tail(session, ECHO_CFG.barge_in_window_s)
    if tail is None:
        return False
    samples, sr = tail

    # The reference is looked up over the window ENDING now, on the same
    # wall-clock the reply was timestamped with. Any skew between that clock
    # and the capture buffer is absorbed by the lag search inside assess().
    now_s = session.call_time_s()
    verdict = await asyncio.to_thread(
        session.echo.assess, samples, now_s - ECHO_CFG.barge_in_window_s, sr)

    if not verdict.is_barge_in:
        return False

    session.barge_in()
    METRICS.record_barge_in()
    logger.info("[%s] barge-in: %s", session.call_id, verdict.as_dict())
    # The reply in flight was cut off: the AGENT_RESPONSE before this event
    # was NOT heard in full, and the record must not imply it was.
    _audit(session).record(call_audit.AGENT_INTERRUPTED,
                           {"at_call_s": round(now_s, 3), "verdict": verdict.as_dict()})

    # Tell the client to stop playing immediately. Without this the agent
    # keeps talking into the caller's interruption -- the gate would be open
    # on the server while the speaker is still going, which is both rude and
    # a fresh source of echo.
    with contextlib.suppress(Exception):
        await session.send_json("_stop_audio", "on")
    return True


async def _turn_poll_loop(session: CallSession):
    """Runs for the lifetime of the call. Every POLL_INTERVAL_S, re-decodes
    the growing buffer -- ALWAYS from byte 0, since that's the only way
    the WebM container stays valid -- then asks the turn detector "is the
    caller done talking yet?" using only the slice of audio past
    session.processed_until_s (a prior turn's already-consumed audio).
    On yes: slice that utterance out for ASR, hand it to _dispatch_turn as
    a background task (so ingestion of the NEXT turn's audio is never
    blocked by this turn's ASR/LLM/TTS work), and advance the marker.
    """
    while True:
        # Faster cadence WHILE the agent is speaking: barge-in cannot be
        # detected sooner than the poll rate, so the interval has to sit well
        # under barge_in_target_s. Outside playback the original cadence is
        # unchanged -- this must not make idle calls busier.
        #
        # Gated on TAIL_READ_IS_CHEAP: on the WebM transport each tail read
        # re-decodes the entire call, so the fast cadence would trade barge-in
        # latency for the O(T^2) CPU blowup the PCM transport was built to
        # avoid. See that constant.
        fast = session.agent_speaking and TAIL_READ_IS_CHEAP
        await asyncio.sleep(ECHO_CFG.barge_in_poll_s if fast else POLL_INTERVAL_S)

        if time.time() - session.last_activity > IDLE_TIMEOUT_S:
            logger.info("[%s] idle timeout, closing", session.call_id)
            session.end_reason = call_audit.END_IDLE_TIMEOUT
            # CLOSING -- Author: Chakravardhan. What was done and what happens
            # next, then the line that already says the call is ending (and
            # already thanks the caller, so no second goodbye).
            await _speak_closing(session, include_goodbye=False)
            await _speak(session, "লাইনে কোনো সাড়া পাচ্ছি না, কল শেষ করছি। ধন্যবাদ।")
            with contextlib.suppress(Exception):
                await session.ws.close()
            return

        if time.time() - session.last_heartbeat > HEARTBEAT_INTERVAL_S:
            session.last_heartbeat = time.time()
            with contextlib.suppress(Exception):
                await session.ws.send_text('{"sender":"_ping","text":""}')

        # --- full-duplex gate: tell our own echo apart from the caller ---
        #
        # This used to be an unconditional `continue`: while the agent spoke,
        # turn detection did not run at all and the client muted the mic, so
        # nothing the caller said during a reply could ever be heard. That is
        # what made barge-in impossible.
        #
        # Now the window is examined and arbitrated. Only a real interruption
        # opens the gate early; our own echo still does not.
        if session.agent_speaking:
            if await _check_barge_in(session):
                pass          # gate opened by barge_in(); fall through and
                              # detect the caller's turn from the same audio
            elif time.time() < session.speak_deadline:
                continue
            else:
                logger.warning("[%s] no playback-done from client, releasing gate on deadline",
                               session.call_id)
                session.release_gate()

        if session.resync_pending:
            await _resync_after_playback(session)
            continue

        sr = session.audio.sample_rate
        tail = session.audio.tail_tensor(session.processed_until_s)
        if tail.numel() < int(0.2 * sr):
            continue  # not enough new audio to judge yet -- not an error

        result = await asyncio.to_thread(_turn_detector.poll, tail, sr)
        if result.utterance_end_s is None:
            continue

        absolute_end_s = session.processed_until_s + result.utterance_end_s

        # Cut the clip at the caller's first syllable, NOT at the end of the
        # previous turn. Those are the same thing only when the caller replies
        # instantly; every second they spend thinking sits between the two, and
        # in a noisy room that gap is not silence, it is traffic or a crowd.
        # Sending it to ASR turns a 3-second question into a mostly-noise clip
        # and gets longer the longer the caller hesitates -- which is exactly
        # when they are least able to be understood.
        #
        # UTTERANCE_PAD_S of lead-in for the same reason it is already added to
        # the far end: a hard cut at the detected boundary clips the first
        # phoneme. Clamped so the clip can never start before audio this call
        # has already consumed.
        absolute_start_s = max(
            session.processed_until_s,
            session.processed_until_s + result.utterance_start_s - UTTERANCE_PAD_S,
        )
        session.utt_seq += 1
        utterance_wav = await _slice_utterance(
            session, absolute_start_s, absolute_end_s, session.utt_seq,
        )
        # Still advances to the END, not the start: the skipped lead-in is
        # consumed, not left behind for the next poll to re-examine.
        session.processed_until_s = absolute_end_s
        # Second layer: the wrapper above handles everything it can while the
        # session is alive, but a failure in the wrapper itself -- or a
        # cancellation -- would still be swallowed by asyncio. Retrieving the
        # exception is what turns "silently discarded" into "in the log".
        task = asyncio.create_task(_dispatch_turn(session, utterance_wav))
        task.add_done_callback(_log_task_failure)


def _log_task_failure(task: asyncio.Task) -> None:
    """Retrieve a background turn's exception so asyncio cannot discard it."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("dispatch task failed after its own handler: %r", exc)


async def _handle_control(session: CallSession, raw: str):
    """Client -> server control channel. Only one message today, but it is
    the load-bearing half of the echo gate: the server cannot otherwise
    know when the caller's speaker actually stopped."""
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("[%s] unparseable control frame: %r", session.call_id, raw[:80])
        return
    if msg.get("type") == "playback_done":
        session.release_gate()
    elif msg.get("type") == "hello":
        # The browser may refuse the 16kHz AudioContext we ask for. Trust
        # what the client reports over what we requested: a wrong assumed
        # rate would not fail loudly, it would just make every timestamp
        # and every transcript quietly wrong.
        rate = int(msg.get("sampleRate") or SAMPLE_RATE)
        session.declared_rate = rate
        session.audio.sample_rate = rate
        if rate != SAMPLE_RATE:
            logger.warning("[%s] client capturing at %dHz, not %dHz -- resampling per utterance",
                           session.call_id, rate, SAMPLE_RATE)
        logger.info("[%s] transport: %s @ %dHz", session.call_id,
                    msg.get("format", "pcm_s16le"), rate)
    elif msg.get("type") == "audio_mode":
        # A hint only. EchoGuard treats it as a starting point and lets the
        # measured Echo Return Loss override it, because a hint can be absent
        # or simply wrong and the measurement cannot.
        session.echo.declare_path(msg.get("mode"))
        logger.info("[%s] client declares audio path: %r", session.call_id, msg.get("mode"))
    elif msg.get("type") == "dtmf":
        # Dispatched as a task, not awaited: _handle_keypad_digit runs a full
        # turn (LLM, clinic API, TTS) and this coroutine is the socket's
        # receive path. Awaiting it here would stop reading audio -- and the
        # caller may well keep talking while the keypad turn is in flight.
        asyncio.create_task(_handle_keypad_digit(session, str(msg.get("digit", ""))))
    elif msg.get("type") == "end_call":
        # CLOSING -- Author: Chakravardhan. The caller asks to end the call and
        # hears the closing first. A task, like dtmf, so the receive path keeps
        # reading -- including the playback_done that ends the wait below.
        asyncio.create_task(_end_call_with_closing(session))


# ===========================================================================
# THE CLOSING -- Author: Chakravardhan
# Story: "As a caller, I want to know immediately who I have reached and that
#         this is automated, so that I can decide how to use it."
# ===========================================================================
# How long the call waits for the closing to finish playing before it closes
# the socket anyway. The closing is a few short cached sentences.
CLOSING_PLAYBACK_CAP_S = 20.0


async def _speak_closing(session: CallSession, *, include_goodbye: bool = True) -> None:
    """What was done on this call and what happens next -- from what the clinic
    API actually answered (call_script.ObservedCallAudit), in the call's current
    language, one pre-warmed sentence at a time. Spoken at most once per call."""
    if getattr(session, "closing_spoken", False):
        return
    session.closing_spoken = True
    outcomes = getattr(session.audit, "outcomes", None) or call_script.CallOutcomes()
    for line in call_script.closing(outcomes, session.pending, session.lang,
                                    include_goodbye=include_goodbye):
        await _speak(session, line)


async def _wait_for_playback(session: CallSession, cap_s: float = CLOSING_PLAYBACK_CAP_S) -> None:
    """Until the client reports playback done, the clips' own duration has
    passed, or `cap_s` -- whichever is first -- so closing the socket does not
    cut off the last words the caller was meant to hear."""
    give_up = time.time() + cap_s
    while session.agent_speaking:
        played_by = session.speak_deadline - PLAYBACK_GUARD_S
        if time.time() >= min(give_up, played_by):
            return
        await asyncio.sleep(0.1)


async def _end_call_with_closing(session: CallSession) -> None:
    session.end_reason = call_script.END_CALLER_ENDED
    await _speak_closing(session)
    await _wait_for_playback(session)
    with contextlib.suppress(Exception):
        await session.ws.close()


def _note_task_crash(session: CallSession, stage: str, task: asyncio.Task) -> None:
    """Done-callback for a call's background task. Records a crash at the
    moment it happens, with its own traceback, rather than whenever -- if
    ever -- somebody awaits the task. Retrieving the exception here also
    retires asyncio's "never retrieved" warning; the crash is logged below
    instead, with the call id on it."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("[%s] %s crashed: %r", session.call_id, stage, exc, exc_info=exc)
        _audit(session).error(stage, exc)


async def _reject_at_capacity(ws: WebSocket):
    """Turn a caller away in words, not by dropping the socket.

    A bare close looks to the caller like the line is broken. Saying it --
    and saying it fast, from the prewarmed TTS cache -- is the difference
    between "this service is down" and "call back in a minute".

    A refused caller is still a caller, and the busiest minutes are exactly
    when a hospital will want to know how many were turned away -- so the
    refusal gets a call record of its own (final_status "rejected")."""
    audit = call_audit.CallAudit(_audit_store, transport=AUDIT_TRANSPORT,
                                 language=lang_mod.default_lang())
    logger.warning("[%s] at capacity (%d/%d active) -- refusing call",
                   audit.call_id, _active_calls, MAX_CONCURRENT_CALLS)
    delivered, audio = False, "none"
    with contextlib.suppress(Exception):
        await ws.send_text(json.dumps({"sender": "AI", "text": BUSY_LINE},
                                      ensure_ascii=False))
        delivered = True
    with contextlib.suppress(Exception):
        # Cache hit in the normal case (BUSY_LINE is prewarmed), so this does
        # not queue behind the TTS gate it is protecting. If TTS is down
        # entirely the text above already went out; audio is a bonus.
        await ws.send_bytes(await _tts.synthesize(BUSY_LINE))
        audio = "synthesized"
    audit.agent_response(BUSY_LINE, lang=lang_mod.default_lang(), audio=audio,
                         delivered=delivered)
    # Give the client a moment to receive both frames before the close lands.
    await asyncio.sleep(0.25)
    with contextlib.suppress(Exception):
        await ws.close()
    audit.end(call_audit.END_AT_CAPACITY, active_calls=_active_calls,
              max_calls=MAX_CONCURRENT_CALLS)


@app.websocket("/ws/audio")
async def ws_audio(ws: WebSocket):
    global _active_calls
    await ws.accept()

    # No await between this check and the increment below, so the count
    # cannot be overshot by a concurrently-arriving call.
    if _active_calls >= MAX_CONCURRENT_CALLS:
        await _reject_at_capacity(ws)
        return
    _active_calls += 1

    session = CallSession(ws)
    # Every task this call creates from here on (the poll loop, each turn,
    # each keypad press) inherits this binding. It is how the process-wide
    # ClinicToolsClient knows which call an API event belongs to, without two
    # concurrent calls ever seeing each other's. See agent/call_audit.py.
    call_audit.bind(session.audit)
    logger.info("[%s] call started (%d/%d active)",
                session.call_id, _active_calls, MAX_CONCURRENT_CALLS)
    poll_task = asyncio.create_task(_turn_poll_loop(session))
    poll_task.add_done_callback(lambda t: _note_task_crash(session, "turn_poll_loop", t))
    # How the call ended, as observed here. The idle timeout overrides it via
    # session.end_reason, because that path ends the call from the inside and
    # then arrives here looking like an ordinary disconnect.
    ending = call_audit.END_CLIENT_DISCONNECT

    try:
        # GREETING -- Author: Chakravardhan. Names the hospital and says this is
        # an automated system, in the call's language; pre-warmed at startup.
        await _speak(session, call_script.greeting(session.lang))
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                await session.append(message["bytes"])
            elif message.get("text"):
                await _handle_control(session, message["text"])
    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        # The server is stopping underneath the call.
        ending = call_audit.END_AGENT_SHUTDOWN
        raise
    except Exception as e:
        logger.exception("[%s] session crashed", session.call_id)
        session.audit.error("session", e)
        ending = call_audit.END_EXCEPTION
    finally:
        poll_task.cancel()
        # Exception as well as CancelledError: a poll loop that had already
        # crashed re-raises here, and used to skip everything below it -- the
        # temp-dir cleanup, the capacity slot, and now the call's final
        # record. The crash itself was logged and recorded by _note_task_crash.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await poll_task
        # Before cleanup(), which drops the verification this relies on.
        _save_for_other_channels(session)
        session.cleanup()
        # Must be in finally, and must pair with the increment above: a slot
        # leaked on a crash path is a permanent reduction in capacity that
        # only a restart clears.
        _active_calls -= 1
        # FINALISE THE RECORD. In finally, so every exit path reaches it:
        # a hang-up, the idle timeout, a crash, a shutdown.
        status = session.audit.end(
            session.end_reason or ending,
            pending_flow=(session.pending or {}).get("awaiting"),
            language=session.lang)
        logger.info("[%s] call ended (%d/%d active) -- %s",
                    session.call_id, _active_calls, MAX_CONCURRENT_CALLS, status)


app.mount("/", StaticFiles(directory="static/pcm", html=True), name="static")
