"""REST client for the clinic's Java (Spring Boot) + PostgreSQL service.

This service owns the actual facts -- prices, schedules, slot availability
-- and is the only thing allowed to state them. The contract below is what
that service needs to implement; nothing here assumes it exists yet.

Every method returns a plain dict and NEVER raises for a normal "not
found" / "unavailable" outcome -- those are valid, expected answers a
caller can be told. It only raises ToolCallError for actual infrastructure
failure (timeout, connection refused, 5xx), which main.py maps to a
distinct "I couldn't check that right now" reply instead of a false
"not found".
"""
from __future__ import annotations

<<<<<<< HEAD
import json

import httpx

# story title: The model never originates a fact
# user story: As a clinical lead, I want every price, date and identifier
#   to come from a verified system response, so that a wrong answer is a
#   data bug rather than a model bug.
# acceptance criteria: Every factual sentence is a template substitution
#   from a validated tool response and the model is never shown a figure
#   it could restate. An automated assertion on every commit proves no
#   model-composed span reaches synthesis on a factual intent.
#
# The "Expected response shape" comments above each method below used to be
# the ONLY statement of the contract. They are now checked: every response is
# validated at this boundary before any template is allowed to read a fact out
# of it. A violation is re-raised as ToolCallError so main.py's existing
# failure path handles it unchanged -- but the log line says "contract", not
# "connection", so a clinic-api schema regression stops looking like an outage.
from agent.tool_contract import ToolContractError, validate as _validate

# story title: A thing not existing is never confused with a system being down
# user story: As a caller, I want to know whether my test does not exist or the
#   system cannot be reached, so that I know whether to call back.
# acceptance criteria: The two produce different spoken sentences and different
#   metrics, and the distinction survives every refactor. This behaviour exists
#   today and gains a permanent regression case.
#
# THE CHOKE POINT. Every clinic call returns through this module and every
# failure is raised from it, so this is the one place that can tell "the thing
# does not exist" from "the clinic did not answer" without anyone remembering
# to say so. See agent/tool_outcome.py for why not main.py.
from agent import tool_outcome

# story title: Numbers are never rounded, reordered or approximated
# user story: As a patient, I want the exact figure, so that what I am quoted
#   is what I pay.
# acceptance criteria: Figures pass from the validated response into the
#   template unchanged and are verbalised digit-faithfully. A test asserts
#   byte-level equality between the tool value and the spoken value for a
#   corpus of amounts, dates and identifiers.
#
# Ported from dev_sourav. The whole fix is the parse_float argument, and it
# earns its place: httpx's r.json() uses json.loads' default float parsing, and
# float("100.00") == float("100.0") -- so a trailing zero is gone before any
# template, any verbaliser or any speech stage could possibly preserve it. No
# amount of care downstream can put a digit back that was discarded at the
# boundary. Money is precisely the kind of figure this loses digits on, on
# every value whose decimals happen to end in zero, silently, every time.
#
# Whole numbers are untouched: no decimal point in the source means no float in
# the first place, so 650 stays int 650 exactly as before.
#
# It also composes with this module's own failure handling rather than fighting
# it -- json.loads raises JSONDecodeError, which subclasses ValueError, so a
# malformed body still lands in the ValueError arm each method already has and
# is still reported as "the system cannot be reached".
#
# HONEST STATUS ON THIS BRANCH: dormant. clinic-api/models.py declares
# rate_inr as Column(Integer), so the live schema cannot emit a decimal rate
# today and nothing in production exercises this path. Correct and defensive,
# and it costs one keyword argument, so it goes in now rather than being
# remembered later when a rate first gains paise.
def _parse_exact(response: httpx.Response) -> dict:
    """Parse a JSON body exactly as httpx.Response.json() does, except that a
    number written with a decimal point keeps its literal source digits."""
    return json.loads(response.text, parse_float=str)

# Ported from dev_sourav -- backs reference_cache_snapshot()/_ref_key() below.
from agent.reference_data_cache import DEFAULT_TTL_S, TTLCache
=======
import functools
import inspect

import httpx

from agent import call_audit
>>>>>>> dev_chakravardhan

DEFAULT_TIMEOUT_S = 4.0  # a phone caller will not wait much longer than this per lookup

# httpx defaults to max_connections=100. That is far more than clinic-api can
# actually work on: its endpoints are sync `def`, so FastAPI runs them in a
# thread pool, and each one needs a SQLAlchemy connection from a pool of
# pool_size=5 + max_overflow=10 = 15. Opening 100 sockets against a service
# that can only process ~15 at once does not make anything faster -- it just
# moves the queue from here (where it is visible and bounded) into clinic-api
# (where it is neither), and every socket waiting there is still burning this
# caller's 4-second timeout.
#
# 16 sits just above that 15-connection ceiling, so the transport stops being
# the thing that oversubscribes the database. Keepalive is set to the same
# number because this is a hot localhost path -- several lookups per turn,
# every turn -- and there is nothing to gain from tearing connections down
# between them.
DEFAULT_LIMITS = httpx.Limits(max_connections=16, max_keepalive_connections=16)


def _parse_exact(response: httpx.Response) -> dict:
    """Parse a JSON body the way `httpx.Response.json()` does, except every
    JSON number with a decimal point is kept as the literal source text
    instead of being coerced to `float`.

    Bug this fixes: `float(100.00) == float(100.0)`, so once a value like a
    rate goes through the ordinary `float`, its trailing zero is gone for
    good -- "100.00" becomes "100.0" and stays that way through every
    template and TTS stage downstream, no matter how carefully they quote
    it. Money is exactly what this loses digits on, silently, on every
    figure whose decimals happen to end in zero. Parsing decimals as `str`
    keeps the exact digits the API sent, which is what "figures pass from
    the validated response into the template unchanged" requires.

    Whole numbers (no decimal point in the source, e.g. `650`) are
    unaffected -- they stay `int`, exactly as before this change.
    """
    return json.loads(response.text, parse_float=str)


class ToolCallError(Exception):
    """The backing service itself failed -- distinct from a normal
    not-found/unavailable result, which is not an error."""


# ===========================================================================
# EVERY CALL LEAVES A COMPLETE RECORD -- Author: Chakravardhan
# ===========================================================================
def _audited(action: str, *, redact: tuple[str, ...] = (), summarize=None):
    """Record this backend action in the current call's audit trail: the
    arguments actually sent, and the response or exception that actually
    came back. See agent/call_audit.py.

    Applied HERE, at the one boundary every backend action crosses, rather
    than at each call site in main.py. main.py has a dozen call sites and
    several of them deliberately swallow a ToolCallError (payment, report
    collection, the refusal record), so recording at the call sites would
    miss exactly the failures nobody is looking at. Nothing goes around this.

    `redact` names arguments whose VALUE must not be kept -- the PIN or date
    of birth being checked, an access token. The argument still appears, as
    "[redacted]", so the record shows it was sent. `summarize` reduces a
    response that carries something that must not be kept. Outside a call
    (startup's catalogue load, most unit tests) this is a pass-through.
    """
    def deco(fn):
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        async def wrapper(self, *args, **kwargs):
            audit = call_audit.current()
            if audit is None:
                return await fn(self, *args, **kwargs)
            try:
                bound = sig.bind(self, *args, **kwargs)
                bound.apply_defaults()
                request = {k: (call_audit.REDACTED if k in redact else v)
                           for k, v in bound.arguments.items() if k != "self"}
            except TypeError:
                request = {}
            return await audit.api_call(action, request,
                                        lambda: fn(self, *args, **kwargs), summarize)
        return wrapper
    return deco


def _without_token(result):
    """A verification response, minus the token. The token grants access to
    a medical history on its own, so it is never kept -- only whether one
    was issued."""
    if not isinstance(result, dict):
        return result
    out = {k: v for k, v in result.items() if k != "token"}
    out["token_issued"] = bool(result.get("token"))
    return out


def _history_summary(result):
    """WHETHER a history was read and how much of it, not WHAT it said.

    clinic-api already records every disclosure in disclosure_audit, keyed by
    this call_id. Copying the tests themselves into a second store would
    double the places a patient's history lives, for no audit gain -- the
    question an auditor asks is "was it disclosed, on which call, after which
    verification", and that is fully answered without the contents."""
    if not isinstance(result, dict):
        return result
    out = {"found": result.get("found"), "reason": result.get("reason"),
           "tests": len(result.get("tests") or []),
           "appointments": len(result.get("appointments") or [])}
    # The single timeline is counted, never copied -- the same rule, for the
    # same reason. Only when present, so an older response summarises as before.
    for key in ("timeline", "upcoming_appointments"):
        if key in result:
            out[key] = len(result.get(key) or [])
    return out


class ClinicToolsClient:
    # ADDED BY SOURAV -- "fetch from cache instead of DB directly" request.
    # `cache_ttl_s` covers only the RARELY-CHANGING, admin-set endpoints
    # listed in agent/reference_data_cache.py's docstring -- appointment
    # availability, billing, report status and every write stay live on
    # every call, unconditionally, exactly as before this change.
    def __init__(self, base_url: str, timeout_s: float = DEFAULT_TIMEOUT_S,
                 cache_ttl_s: float = DEFAULT_TTL_S):
        self.base_url = base_url.rstrip("/")
<<<<<<< HEAD
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout_s)
        self.outcomes = tool_outcome.OutcomeCounter()
        self._ref_cache = TTLCache(ttl_s=cache_ttl_s)
=======
        self._client = httpx.AsyncClient(
            base_url=self.base_url, timeout=timeout_s, limits=DEFAULT_LIMITS,
        )
>>>>>>> dev_chakravardhan

    async def aclose(self):
        await self._client.aclose()

    def snapshot(self) -> dict:
        return self.outcomes.snapshot()

    def _answered(self, tool: str, payload: dict) -> dict:
        self.outcomes.record(tool, tool_outcome.classify(tool, payload))
        return payload

    def _unreachable(self, tool: str) -> None:
        self.outcomes.record(tool, tool_outcome.UNREACHABLE)

    def reference_cache_snapshot(self) -> dict:
        """Cache effectiveness for the reference-data TTL cache -- exposed
        the same way agent/semantic_cache.py's snapshot() is, via main.py's
        /api/stats, so hit rate can be tuned against real traffic."""
        return self._ref_cache.snapshot()

    @staticmethod
    def _ref_key(*parts: str) -> tuple:
        # Exact-match key, normalized only enough to fold "CBC" / " cbc "
        # repeats together -- clinic-api's own ILIKE/difflib matching still
        # runs in full on any cache MISS, this only short-circuits an
        # identical repeat question.
        return tuple(p.strip().lower() for p in parts)

    # ---- Tool 1: GET /api/v1/tests/search?name=... ----
    # Expected response shape:
    #   found=true:  {"found": true, "test_name": "...", "rate_inr": 650,
    #                 "sample_type": "Blood", "report_time_hours": 24}
    #   found=false: {"found": false, "query": "...", "did_you_mean": ["..."]}
    @_audited("get_test_rate")
    async def get_test_rate(self, test_name: str) -> dict:
        # ADDED BY SOURAV -- reference-data cache. Price/sample type/report
        # time are admin-set, not caller-set -- see reference_data_cache.py.
        key = self._ref_key("test_rate", test_name)
        cached = self._ref_cache.get(key)
        if cached is not None:
            return cached
        try:
            r = await self._client.get("/api/v1/tests/search", params={"name": test_name})
            r.raise_for_status()
            data = self._answered("test_rate", _validate("test_rate", _parse_exact(r)))
        except httpx.HTTPError as e:
            self._unreachable("test_rate")
            raise ToolCallError(f"get_test_rate({test_name!r}): {e}") from e
        except ToolContractError as e:
            self._unreachable("test_rate")
            raise ToolCallError(f"get_test_rate: clinic-api contract violation: {e}") from e
        # story title: A thing not existing is never confused with a system being down
        # user story: As a caller, I want to know whether my test does not exist
        #   or the system cannot be reached, so that I know whether to call back.
        # acceptance criteria: The two produce different spoken sentences and
        #   different metrics, and the distinction survives every refactor. This
        #   behaviour exists today and gains a permanent regression case.
        #
        # json.JSONDecodeError subclasses ValueError, NOT httpx.HTTPError -- so a
        # proxy or gateway answering 200 with an HTML error page escaped every
        # handler above and propagated as an unhandled exception, which on this
        # deployment is the single MOST LIKELY real shape of "the system is
        # down". The caller then got neither sentence: the turn died and the
        # line went quiet. Caught here so it says "cannot be reached", which is
        # exactly what happened.
        except ValueError as e:
            self._unreachable("test_rate")
            raise ToolCallError(f"get_test_rate: malformed response body: {e}") from e
        self._ref_cache.set(key, data)
        return data

    # ---- Tool 2: GET /api/v1/doctors/availability?name=...&date=YYYY-MM-DD ----
    # date is OPTIONAL -- omit it to ask "when is this doctor next available".
    # Expected response shape:
    #   found=true:  {"found": true, "doctor_name": "...", "date": "...",
    #                 "available": true, "chamber_hours": "18:00-20:00",
    #                 "next_available_date": null}
    #                or, if not available that date:
    #                {"found": true, ..., "available": false,
    #                 "next_available_date": "2026-08-27"}
    #   found=false: {"found": false, "query": "..."}
    @_audited("get_doctor_availability")
    async def get_doctor_availability(self, doctor_name: str, date: str | None) -> dict:
        # ADDED BY SOURAV -- reference-data cache: deliberately EXCLUDED.
        # This resolves a SPECIFIC day's slot state, which another caller
        # can change (book/cancel) between two questions about the same
        # doctor -- see reference_data_cache.py's exclusion note. Always
        # live, unconditionally, same as before this change.
        params = {"name": doctor_name}
        if date:
            params["date"] = date
        try:
            r = await self._client.get("/api/v1/doctors/availability", params=params)
            r.raise_for_status()
            return self._answered("doctor_availability", _validate("doctor_availability", _parse_exact(r)))
        except httpx.HTTPError as e:
            self._unreachable("doctor_availability")
            raise ToolCallError(f"get_doctor_availability({doctor_name!r}, {date!r}): {e}") from e
        except ToolContractError as e:
            self._unreachable("doctor_availability")
            raise ToolCallError(f"get_doctor_availability: clinic-api contract violation: {e}") from e
        # story title: A thing not existing is never confused with a system being down
        # user story: As a caller, I want to know whether my test does not exist
        #   or the system cannot be reached, so that I know whether to call back.
        # acceptance criteria: The two produce different spoken sentences and
        #   different metrics, and the distinction survives every refactor. This
        #   behaviour exists today and gains a permanent regression case.
        #
        # json.JSONDecodeError subclasses ValueError, NOT httpx.HTTPError -- so a
        # proxy or gateway answering 200 with an HTML error page escaped every
        # handler above and propagated as an unhandled exception, which on this
        # deployment is the single MOST LIKELY real shape of "the system is
        # down". The caller then got neither sentence: the turn died and the
        # line went quiet. Caught here so it says "cannot be reached", which is
        # exactly what happened.
        except ValueError as e:
            self._unreachable("doctor_availability")
            raise ToolCallError(f"get_doctor_availability: malformed response body: {e}") from e

    # ADDED BY SOURAV -- "Caller asks when a doctor sits" story. Mirrors
    # get_doctor_availability() just above exactly (same error-handling
    # shape, same _parse_exact() passthrough), minus the `date` parameter
    # -- this calls the new DATE-FREE clinic-api endpoint that returns a
    # doctor's full recurring weekly schedule. See
    # clinic-api/main.py::doctor_schedule()'s docstring for the response
    # shape and why this is a genuinely separate question from
    # get_doctor_availability(), not just the same call with date=None.
    async def get_doctor_schedule(self, doctor_name: str) -> dict:
        # ADDED BY SOURAV -- reference-data cache. This is the RECURRING
        # weekly schedule (admin-set), never the specific-day availability
        # call just above (deliberately NOT cached -- see
        # reference_data_cache.py's exclusion note).
        key = self._ref_key("doctor_schedule", doctor_name)
        cached = self._ref_cache.get(key)
        if cached is not None:
            return cached
        try:
            r = await self._client.get("/api/v1/doctors/schedule", params={"name": doctor_name})
            r.raise_for_status()
            data = _parse_exact(r)
        except httpx.HTTPError as e:
            raise ToolCallError(f"get_doctor_schedule({doctor_name!r}): {e}") from e
        self._ref_cache.set(key, data)
        return data

    # ---- Tool 3: POST /api/v1/appointments ----
    # Body: {"doctor_name", "date", "time_slot", "patient_name", "phone"}
    # Expected response shape:
    #   success=true:  {"success": true, "confirmation_id": "KCD-20260824-0031",
    #                    "doctor_name": "...", "date": "...", "time_slot": "...",
    #                    "notification": {"id": 41, "event": "booked",
    #                                     "status": "queued", "error_code": null}}
    #   success=false: {"success": false, "reason": "slot_taken" | "missing_field" | "doctor_not_found",
    #                    "alternative_slots": ["17:30", "18:15"]}
    #
    # `notification` is the written confirmation the patient is owed. It is
    # NOT a delivery result -- at the moment this response is built the
    # message has not been attempted yet, and "queued" is the normal,
    # healthy value. reply_templates.booking_reply() reads it only to
    # decide whether it may PROMISE the caller a message; a status of
    # "skipped" or "failed" means it must not, because the caller would
    # then hang up waiting for an SMS that is never coming.
    @_audited("book_appointment")
    async def book_appointment(self, doctor_name: str, date: str, time_slot: str,
                                patient_name: str, phone: str) -> dict:
        # ADDED BY SOURAV -- reference-data cache: deliberately EXCLUDED.
        # A write, not a fetch -- never a cache candidate.
        body = {
            "doctor_name": doctor_name, "date": date, "time_slot": time_slot,
            "patient_name": patient_name, "phone": phone,
        }
        try:
            r = await self._client.post("/api/v1/appointments", json=body)
            r.raise_for_status()
            return self._answered("book_appointment", _validate("book_appointment", _parse_exact(r)))
        except httpx.HTTPError as e:
            self._unreachable("book_appointment")
            raise ToolCallError(f"book_appointment({body!r}): {e}") from e
        except ToolContractError as e:
            self._unreachable("book_appointment")
            raise ToolCallError(f"book_appointment: clinic-api contract violation: {e}") from e
        # story title: A thing not existing is never confused with a system being down
        # user story: As a caller, I want to know whether my test does not exist
        #   or the system cannot be reached, so that I know whether to call back.
        # acceptance criteria: The two produce different spoken sentences and
        #   different metrics, and the distinction survives every refactor. This
        #   behaviour exists today and gains a permanent regression case.
        #
        # json.JSONDecodeError subclasses ValueError, NOT httpx.HTTPError -- so a
        # proxy or gateway answering 200 with an HTML error page escaped every
        # handler above and propagated as an unhandled exception, which on this
        # deployment is the single MOST LIKELY real shape of "the system is
        # down". The caller then got neither sentence: the turn died and the
        # line went quiet. Caught here so it says "cannot be reached", which is
        # exactly what happened.
        except ValueError as e:
            self._unreachable("book_appointment")
            raise ToolCallError(f"book_appointment: malformed response body: {e}") from e

    # ---- POST /api/v1/appointments/{confirmation_id}/reschedule ----
    # Body: {"date", "time_slot"}
    # Expected response shape:
    #   success=true:  {"success": true, "confirmation_id": "<UNCHANGED>",
    #                    "doctor_name": "...", "doctor_name_bn": "...",
    #                    "date": "...", "time_slot": "...",
    #                    "previous_date": "...", "previous_time_slot": "...",
    #                    "notification": {...}}
    #   success=false: {"success": false,
    #                    "reason": "appointment_not_found" | "appointment_cancelled"
    #                              | "slot_taken" | "missing_field"
    #                              | "doctor_not_available_that_day",
    #                    "alternative_slots": [...]}
    #
    # The confirmation_id is deliberately NOT reissued -- see that
    # endpoint's docstring. A caller who kept the first message still holds
    # a valid reference.
    @_audited("reschedule_appointment")
    async def reschedule_appointment(self, confirmation_id: str, date: str,
                                      time_slot: str) -> dict:
        body = {"date": date, "time_slot": time_slot}
        try:
            r = await self._client.post(
                f"/api/v1/appointments/{confirmation_id}/reschedule", json=body)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as e:
            raise ToolCallError(
                f"reschedule_appointment({confirmation_id!r}, {body!r}): {e}") from e

    # ---- POST /api/v1/appointments/{confirmation_id}/cancel ----
    # Body: {"reason": "..." | null}   -- audit only, never sent to the patient
    # Expected response shape:
    #   success=true:  {"success": true, "already_cancelled": false,
    #                    "confirmation_id": "...", "doctor_name": "...",
    #                    "date": "...", "time_slot": "...", "notification": {...}}
    #   success=false: {"success": false, "reason": "appointment_not_found"
    #                                               | "cancel_failed"}
    #
    # already_cancelled=true is a SUCCESS, and no second message is sent.
    # Repeat cancellations are normal (a retry, a double-tap, a patient
    # ringing twice) and none of them is a reason to message somebody about
    # a cancellation they were already told about.
    @_audited("cancel_appointment")
    async def cancel_appointment(self, confirmation_id: str,
                                  reason: str | None = None) -> dict:
        try:
            r = await self._client.post(
                f"/api/v1/appointments/{confirmation_id}/cancel", json={"reason": reason})
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as e:
            raise ToolCallError(f"cancel_appointment({confirmation_id!r}): {e}") from e

    # =======================================================================
    # PATIENT HISTORY -- disclosed only after verification
    # Author: Chakravardhan
    # =======================================================================
    # NOTE WHAT IS ABSENT: there is no set_pin() method here, and that is
    # deliberate rather than unfinished. The PIN endpoint exists on
    # clinic-api for COUNTER STAFF only. A PIN settable from the voice line
    # is not a second factor -- it is a button labelled "make me verified",
    # settable by whoever is holding the shared handset, and it would undo
    # the entire story. The capability is withheld at the client, so no
    # future turn-loop change can reach it by accident.

    # ---- POST /api/v1/history/verify/begin ----
    # Body: {"phone", "call_id"}
    # Response: {"factor": "pin" | "dob" | "none", "locked": bool}
    #
    # Returns a challenge even for a number the clinic has never seen -- see
    # that endpoint's docstring on why "no such patient" is itself a
    # disclosure.
    @_audited("begin_verification")
    async def begin_verification(self, phone: str, call_id: str | None = None) -> dict:
        body = {"phone": phone, "call_id": call_id}
        try:
            r = await self._client.post("/api/v1/history/verify/begin", json=body)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as e:
            raise ToolCallError(f"begin_verification({phone!r}): {e}") from e

    # ---- POST /api/v1/history/verify ----
    # Body: {"phone", "factor", "answer", "call_id"}
    # Response: {"reply": "verified"|"failed"|"locked", "verified": bool,
    #            "factor": "...", "token": "..." | null}
    #
    # `reply` is coarser than the audit trail on purpose: a wrong answer, an
    # unknown number and a patient with no usable factor all come back
    # "failed". reply_templates.py must not try to explain the difference.
    # The ANSWER is redacted from the audit for the same reason it is left out
    # of the error message below; the token for the reason given in
    # _without_token.
    @_audited("verify_caller", redact=("answer",), summarize=_without_token)
    async def verify_caller(self, phone: str, factor: str, answer: str,
                             call_id: str | None = None) -> dict:
        body = {"phone": phone, "factor": factor, "answer": answer, "call_id": call_id}
        try:
            r = await self._client.post("/api/v1/history/verify", json=body)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as e:
            # The phone number and the ANSWER are both left out of this
            # message on purpose. A ToolCallError is logged, and a log line
            # carrying somebody's PIN or date of birth would leak the secret
            # through the one path nobody thinks to check.
            raise ToolCallError(f"verify_caller(factor={factor!r}): {e}") from e

    # ---- POST /api/v1/history/read ----
    # Body: {"token", "call_id"}   -- POST, not GET: a token in a query
    # string lands in the access log, and a token grants access on its own.
    # Response: {"found": true, "patient_name", "tests": [...],
    #            "appointments": [...], "timeline": [...],
    #            "upcoming_appointments": [...]}  |  {"found": false, "reason": ...}
    # `timeline` is the patient's single record -- bookings and tests in one
    # list, oldest first; see clinic-api history_service.build_timeline().
    @_audited("read_history", redact=("token",), summarize=_history_summary)
    async def read_history(self, token: str, call_id: str | None = None) -> dict:
        try:
            r = await self._client.post("/api/v1/history/read",
                                        json={"token": token, "call_id": call_id})
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as e:
            raise ToolCallError(f"read_history(call_id={call_id!r}): {e}") from e

    # ---- POST /api/v1/history/refusal ----
    # Records a disclosure the AGENT refused (speakerphone, unclassified
    # audio path). Never raises into the turn loop: failing to write an audit
    # row must not turn into the caller hearing an error.
    async def record_disclosure_refusal(self, phone: str, reason: str,
                                         call_id: str | None = None) -> None:
        try:
            await self._post_disclosure_refusal(phone, reason, call_id)
        except ToolCallError:
            pass

    # Split out so the failure is swallowed OUTSIDE the audited call. Were
    # the try/except inside it, a refusal that never reached clinic-api
    # would be recorded as a success -- the one thing the audit must not do.
    @_audited("record_disclosure_refusal")
    async def _post_disclosure_refusal(self, phone: str, reason: str,
                                       call_id: str | None) -> None:
        try:
            r = await self._client.post(
                "/api/v1/history/refusal",
                json={"phone": phone, "reason": reason, "call_id": call_id})
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise ToolCallError(f"record_disclosure_refusal(reason={reason!r}): {e}") from e

    # ---- Tool 4: GET /api/v1/doctors/by-department?department=...&date=YYYY-MM-DD ----
    # date is OPTIONAL -- omit it to list every doctor in the department
    # regardless of schedule. Pass it (main.py does, whenever the caller
    # named a date, e.g. "today") to filter down to doctors who actually
    # sit that day, each with their chamber hours.
    # Expected response shape:
    #   found=true:  {"found": true, "department": "...", "date": "..." | null,
    #                 "doctors": [{"name", "doctor_name_bn", "qualifications",
    #                              "chamber_hours"?}, ...]}
    #   found=false: {"found": false, "query": "..."}
    @_audited("get_doctors_by_department")
    async def get_doctors_by_department(self, department: str, date: str | None = None) -> dict:
        # ADDED BY SOURAV -- reference-data cache, but ONLY for the
        # date-free roster call. Passing a date filters to who is actually
        # sitting THAT day, which is the same "current schedule state" risk
        # as get_doctor_availability just above -- see
        # reference_data_cache.py's exclusion note. That call always stays
        # live; only the plain department list is cached below.
        key = self._ref_key("doctors_by_department", department) if not date else None
        if key is not None:
            cached = self._ref_cache.get(key)
            if cached is not None:
                return cached
        params = {"department": department}
        if date:
            params["date"] = date
        try:
            r = await self._client.get("/api/v1/doctors/by-department", params=params)
            r.raise_for_status()
            data = self._answered("doctors_by_department", _validate("doctors_by_department", _parse_exact(r)))
        except httpx.HTTPError as e:
            self._unreachable("doctors_by_department")
            raise ToolCallError(f"get_doctors_by_department({department!r}, {date!r}): {e}") from e
        except ToolContractError as e:
            self._unreachable("doctors_by_department")
            raise ToolCallError(f"get_doctors_by_department: clinic-api contract violation: {e}") from e
        # story title: A thing not existing is never confused with a system being down
        # user story: As a caller, I want to know whether my test does not exist
        #   or the system cannot be reached, so that I know whether to call back.
        # acceptance criteria: The two produce different spoken sentences and
        #   different metrics, and the distinction survives every refactor. This
        #   behaviour exists today and gains a permanent regression case.
        #
        # json.JSONDecodeError subclasses ValueError, NOT httpx.HTTPError -- so a
        # proxy or gateway answering 200 with an HTML error page escaped every
        # handler above and propagated as an unhandled exception, which on this
        # deployment is the single MOST LIKELY real shape of "the system is
        # down". The caller then got neither sentence: the turn died and the
        # line went quiet. Caught here so it says "cannot be reached", which is
        # exactly what happened.
        except ValueError as e:
            self._unreachable("doctors_by_department")
            raise ToolCallError(f"get_doctors_by_department: malformed response body: {e}") from e
        if key is not None:
            self._ref_cache.set(key, data)
        return data

    # =========================================================================
    # ADDED BY SOURAV -- "Lab Report Status & Secure Delivery" combined story.
    # Three new tool calls, mirroring the exact shape of the four above:
    # every method still returns a plain dict, still never raises for a
    # normal outcome (NOT_READY, OTP_INVALID, DELIVERY_DISABLED are all
    # valid answers a caller can be told, not errors), and only raises
    # ToolCallError for real infrastructure failure -- clinic-api/main.py's
    # new endpoints follow that same "named reason, not an exception"
    # convention for exactly this reason.
    # =========================================================================

    # ---- Tool 5: GET /api/v1/reports/status?phone=...&test_name=... ----
    # Expected response shape:
    #   patient not found:  {"patient_found": false}
    #   no/no-matching report: {"patient_found": true, "found": false, "reason": "NOT_FOUND"}
    #   multiple candidates: {"patient_found": true, "found": false, "reason": "AMBIGUOUS",
    #                         "candidates": [{"report_number", "test_name", "status", ...}]}
    #   single match: {"patient_found": true, "found": true, "report_number", "test_name",
    #                  "status", "delivery_enabled", "expected_ready_at", "ready_at"}
    async def get_report_status(self, phone: str, test_name: str | None = None) -> dict:
        # ADDED BY SOURAV -- reference-data cache: deliberately EXCLUDED.
        # Caller-identity-bound status that changes constantly (a report
        # finishing processing) -- see reference_data_cache.py's exclusion
        # note and semantic_cache.py's identical reasoning for report_status.
        params = {"phone": phone}
        if test_name:
            params["test_name"] = test_name
        try:
            r = await self._client.get("/api/v1/reports/status", params=params)
            r.raise_for_status()
            return _parse_exact(r)
        except httpx.HTTPError as e:
            raise ToolCallError(f"get_report_status({phone!r}, {test_name!r}): {e}") from e

    # ---- Tool 6: POST /api/v1/reports/delivery/request ----
    # Body: {"phone", "report_number"}
    # Expected response shape:
    #   success=true:  {"success": true, "reason": "OTP_REQUIRED", "masked_phone": "...1234"}
    #   success=false: {"success": false, "reason": "PATIENT_NOT_FOUND" | "NOT_FOUND" |
    #                    "NOT_READY" | "PROCESSING" | "CANCELLED" | "DELIVERY_DISABLED"}
    async def request_report_delivery(self, phone: str, report_number: str) -> dict:
        # ADDED BY SOURAV -- reference-data cache: deliberately EXCLUDED.
        # A write with security-sensitive side effects (triggers an OTP) --
        # never a cache candidate.
        body = {"phone": phone, "report_number": report_number}
        try:
            r = await self._client.post("/api/v1/reports/delivery/request", json=body)
            r.raise_for_status()
            return _parse_exact(r)
        except httpx.HTTPError as e:
            raise ToolCallError(f"request_report_delivery({body!r}): {e}") from e

    # ---- Tool 7: POST /api/v1/reports/otp/verify ----
    # Body: {"phone", "report_number", "otp_code"}
    # Expected response shape:
    #   success=true:  {"success": true, "reason": "DELIVERY_SENT", "masked_phone": "...1234",
    #                    "signed_link_expires_minutes": 15}
    #   success=false: {"success": false, "reason": "PATIENT_NOT_FOUND" | "NOT_FOUND" |
    #                    "NOT_READY" | "PROCESSING" | "CANCELLED" | "DELIVERY_DISABLED" |
    #                    "OTP_NOT_REQUESTED" | "OTP_ALREADY_USED" | "OTP_EXPIRED" |
    #                    "OTP_MAX_ATTEMPTS" | "OTP_INVALID" | "DELIVERY_FAILED"}
    #
    # RULE 9 (never reveal the OTP): note there is no code path anywhere in
    # this method, clinic-api's endpoint, or reply_templates.py's reply
    # function that reads `otp_code` back out of a response -- the caller
    # only ever finds out whether their guess was accepted, never what the
    # right value was.
    async def verify_report_otp(self, phone: str, report_number: str, otp_code: str) -> dict:
        # ADDED BY SOURAV -- reference-data cache: deliberately EXCLUDED.
        # A security check with side effects (attempt counting) -- never a
        # cache candidate, same reasoning as request_report_delivery above.
        body = {"phone": phone, "report_number": report_number, "otp_code": otp_code}
        try:
            r = await self._client.post("/api/v1/reports/otp/verify", json=body)
            r.raise_for_status()
            return _parse_exact(r)
        except httpx.HTTPError as e:
            # otp_code is deliberately NOT interpolated into this message --
            # DoD gate 6 ("no secret-shaped literal ... in a log line"), and
            # this exception's text is exactly what logger.error() below
            # (main_pcm.py / main.py) writes to the log on a tool failure.
            raise ToolCallError(
                f"verify_report_otp(phone={phone!r}, report_number={report_number!r}): {e}"
            ) from e

    # =========================================================================
    # ADDED BY SOURAV -- "Caller asks about a health package" combined with
    # "Caller asks opening hours, address or directions". Three new tool
    # calls, mirroring the exact same shape as every method above: a plain
    # dict returned, never raising for a normal not-found/empty outcome,
    # ToolCallError only for real infrastructure failure.
    # =========================================================================

    # ---- Tool 9: GET /api/v1/clinic/info ----
    # No parameters -- a singleton lookup (clinic-api/models.py's
    # ClinicInfo table has exactly one row).
    # Expected response shape:
    #   found=true:  {"found": true, "clinic_name": "...", "phone": "...",
    #                 "address": "...", "directions": "...",
    #                 "hours": {"monday": {"closed": false, "open": "08:00",
    #                                       "close": "20:00"}, ..., "sunday": {...}}}
    #   found=false: {"found": false}
    async def get_clinic_info(self) -> dict:
        # ADDED BY SOURAV -- reference-data cache. A singleton row that
        # changes essentially never; see reference_data_cache.py.
        key = self._ref_key("clinic_info")
        cached = self._ref_cache.get(key)
        if cached is not None:
            return cached
        try:
            r = await self._client.get("/api/v1/clinic/info")
            r.raise_for_status()
            data = _parse_exact(r)
        except httpx.HTTPError as e:
            raise ToolCallError(f"get_clinic_info(): {e}") from e
        self._ref_cache.set(key, data)
        return data

    # ---- Tool 10: GET /api/v1/health-packages ----
    # No parameters -- every ACTIVE package, for a caller who named none.
    # Expected response shape:
    #   {"packages": [{"found": true, "package_name": "...", "package_name_bn": "...",
    #                   "description": "...", "price_inr": 999, "tests": [...],
    #                   "tests_bn": [...]}, ...]}
    async def get_health_packages(self) -> dict:
        # ADDED BY SOURAV -- reference-data cache. Admin-set catalogue.
        key = self._ref_key("health_packages")
        cached = self._ref_cache.get(key)
        if cached is not None:
            return cached
        try:
            r = await self._client.get("/api/v1/health-packages")
            r.raise_for_status()
            data = _parse_exact(r)
        except httpx.HTTPError as e:
            raise ToolCallError(f"get_health_packages(): {e}") from e
        self._ref_cache.set(key, data)
        return data

    # ---- Tool 11: GET /api/v1/health-packages/search?name=... ----
    # Expected response shape:
    #   found=true:  {"found": true, "package_name": "...", "package_name_bn": "...",
    #                 "description": "...", "price_inr": 999, "tests": [...],
    #                 "tests_bn": [...]}
    #   found=false: {"found": false, "query": "...", "did_you_mean": ["..."]}
    async def search_health_package(self, package_name: str) -> dict:
        # ADDED BY SOURAV -- reference-data cache. Admin-set catalogue.
        key = self._ref_key("health_package_search", package_name)
        cached = self._ref_cache.get(key)
        if cached is not None:
            return cached
        try:
            r = await self._client.get("/api/v1/health-packages/search", params={"name": package_name})
            r.raise_for_status()
            data = _parse_exact(r)
        except httpx.HTTPError as e:
            raise ToolCallError(f"search_health_package({package_name!r}): {e}") from e
        self._ref_cache.set(key, data)
        return data

    # =========================================================================
    # ADDED BY SOURAV -- "Caller asks how to prepare for a test" story.
    # =========================================================================

    # ---- Tool 12: GET /api/v1/tests/preparation?name=... ----
    # Expected response shape:
    #   found=true, advisory_available=true:
    #     {"found": true, "test_name": "...", "test_name_bn": "...",
    #      "advisory_available": true, "fasting_required": bool,
    #      "fasting_hours": "...", "water_allowance": "...",
    #      "medication_hold": "...", "timing_rule": "...",
    #      "advisory_script_en"/"advisory_script_hinglish"/
    #      "advisory_script_banglish"/"advisory_script_bn": "..." (each
    #      still containing the literal "{test_name}" placeholder --
    #      substitution happens in agent/reply_templates.test_preparation_
    #      reply(), not here)}
    #   found=true, advisory_available=false: {"found": true, "test_name": "...",
    #     "test_name_bn": "...", "advisory_available": false} -- an honest
    #     "this test exists but we have no preparation content for it yet",
    #     never a guessed "no special preparation needed" (see clinic-api/
    #     models.py's own comment on why LabTest's advisory columns are
    #     nullable with no default).
    #   found=false: {"found": false, "query": "...", "did_you_mean": ["..."]}
    async def get_test_preparation(self, test_name: str) -> dict:
        # ADDED BY SOURAV -- reference-data cache. Advisory content is
        # admin-authored, not caller-specific.
        key = self._ref_key("test_preparation", test_name)
        cached = self._ref_cache.get(key)
        if cached is not None:
            return cached
        try:
            r = await self._client.get("/api/v1/tests/preparation", params={"name": test_name})
            r.raise_for_status()
            data = _parse_exact(r)
        except httpx.HTTPError as e:
            raise ToolCallError(f"get_test_preparation({test_name!r}): {e}") from e
        self._ref_cache.set(key, data)
        return data

    # =========================================================================
    # ADDED BY SOURAV -- Phase 1: Database Schema & Policy Tables.
    # =========================================================================

    # ---- Tool 13: GET /api/v1/tests/walkin-policy?name=... ----
    # Expected response shape:
    #   found=true, policy_available=true:
    #     {"found": true, "test_name": "...", "test_name_bn": "...",
    #      "policy_available": true, "walkin_eligible": bool,
    #      "walkin_hours": "..."}
    #   found=true, policy_available=false: {"found": true, "test_name": "...",
    #     "test_name_bn": "...", "policy_available": false} -- honest
    #     "nobody has reviewed walk-in policy for this test yet", never a
    #     guessed "walk-ins welcome" (see clinic-api/models.py's own
    #     comment on why LabTest's walkin_eligible is nullable with no
    #     default).
    #   found=false: {"found": false, "query": "...", "did_you_mean": ["..."]}
    async def get_walkin_policy(self, test_name: str) -> dict:
        # ADDED BY SOURAV -- reference-data cache. Walk-in policy is
        # admin-reviewed, not caller-specific.
        key = self._ref_key("walkin_policy", test_name)
        cached = self._ref_cache.get(key)
        if cached is not None:
            return cached
        try:
            r = await self._client.get("/api/v1/tests/walkin-policy", params={"name": test_name})
            r.raise_for_status()
            data = _parse_exact(r)
        except httpx.HTTPError as e:
            raise ToolCallError(f"get_walkin_policy({test_name!r}): {e}") from e
        self._ref_cache.set(key, data)
        return data

    # ---- Tool 14: GET /api/v1/tests/prescription-policy?name=... ----
    # Expected response shape:
    #   found=true, policy_available=true:
    #     {"found": true, "test_name": "...", "test_name_bn": "...",
    #      "policy_available": true, "prescription_required": bool,
    #      "prescription_channels": ["...", ...]}
    #   found=true, policy_available=false: same honest-gap shape as
    #     walkin-policy above, over prescription_required instead.
    #   found=false: same as walkin-policy above.
    async def get_prescription_policy(self, test_name: str) -> dict:
        # ADDED BY SOURAV -- reference-data cache. Prescription policy is
        # admin-reviewed, not caller-specific.
        key = self._ref_key("prescription_policy", test_name)
        cached = self._ref_cache.get(key)
        if cached is not None:
            return cached
        try:
            r = await self._client.get("/api/v1/tests/prescription-policy", params={"name": test_name})
            r.raise_for_status()
            data = _parse_exact(r)
        except httpx.HTTPError as e:
            raise ToolCallError(f"get_prescription_policy({test_name!r}): {e}") from e
        self._ref_cache.set(key, data)
        return data

    # ---- Tool 15: GET /api/v1/insurance/coverage?test_name=...&provider_name=... ----
    # Expected response shape:
    #   test_found=false: {"test_found": false, "query": "...", "did_you_mean": ["..."]}
    #   test_found=true, provider_found=false: {"test_found": true,
    #     "test_name": "...", "provider_found": false, "query_provider": "..."}
    #     -- honest "we don't recognise that insurer", never silently
    #     matched to the wrong one.
    #   test_found=true, provider_found=true, policy_available=false:
    #     {..., "provider_found": true, "provider_name": "...",
    #      "policy_available": false} -- no reviewed (test, provider) row
    #      yet, never a guessed COVERED/NOT_COVERED.
    #   test_found=true, provider_found=true, policy_available=true:
    #     {..., "policy_available": true, "coverage_status": "...",
    #      "pre_auth_required": bool}
    async def get_insurance_coverage(self, test_name: str, provider_name: str) -> dict:
        # ADDED BY SOURAV -- reference-data cache. A (test, provider)
        # coverage row is an admin-reviewed policy fact, not caller-
        # specific data -- distinct from get_patient_billing below, which
        # is excluded from this cache for exactly that reason.
        key = self._ref_key("insurance_coverage", test_name, provider_name)
        cached = self._ref_cache.get(key)
        if cached is not None:
            return cached
        try:
            r = await self._client.get(
                "/api/v1/insurance/coverage",
                params={"test_name": test_name, "provider_name": provider_name},
            )
            r.raise_for_status()
            data = _parse_exact(r)
        except httpx.HTTPError as e:
            raise ToolCallError(f"get_insurance_coverage({test_name!r}, {provider_name!r}): {e}") from e
        self._ref_cache.set(key, data)
        return data

    # ---- Tool 16: GET /api/v1/patient/billing?phone=... ----
    # Expected response shape:
    #   patient_found=false: {"patient_found": false}
    #   patient_found=true, found=false: {"patient_found": true, "found": false,
    #     "reason": "NOT_FOUND"} -- honest "no billing record for this
    #     patient", never a guessed/defaulted zero balance (see
    #     clinic-api/models.py's PatientBilling docstring).
    #   patient_found=true, found=true: {"patient_found": true, "found": true,
    #     "outstanding_amount": float, "due_date": "..." or null}
    async def get_patient_billing(self, phone: str) -> dict:
        # ADDED BY SOURAV -- reference-data cache: deliberately EXCLUDED.
        # Caller-identity-bound financial data that changes the moment a
        # bill is paid -- see reference_data_cache.py's exclusion note and
        # semantic_cache.py's identical reasoning for billing_balance.
        try:
            r = await self._client.get("/api/v1/patient/billing", params={"phone": phone})
            r.raise_for_status()
            return _parse_exact(r)
        except httpx.HTTPError as e:
            raise ToolCallError(f"get_patient_billing({phone!r}): {e}") from e

    # =========================================================================
    # ADDED BY SOURAV -- "Caller asks to be called back" story.
    # =========================================================================

    # ---- Tool 17: POST /api/v1/callbacks ----
    # Expected response shape:
    #   {"success": true, "callback_id": "CB-20260915-A1B2", "phone": "...",
    #    "time_window": "...", "reason": "..." or null, "status": "pending"}
    async def request_callback(self, phone: str, time_window: str, reason: str | None = None) -> dict:
        # ADDED BY SOURAV -- reference-data cache: deliberately EXCLUDED,
        # same reasoning as book_appointment() above -- a write, never a
        # cache candidate.
        body = {"phone": phone, "time_window": time_window, "reason": reason}
        try:
            r = await self._client.post("/api/v1/callbacks", json=body)
            r.raise_for_status()
            return _parse_exact(r)
        except httpx.HTTPError as e:
            raise ToolCallError(f"request_callback({body!r}): {e}") from e
