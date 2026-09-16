"""Written patient confirmations: the hospital SMS gateway client and the
delivery ledger behind it.

WHAT THIS EXISTS FOR
--------------------
Until now the confirmation number reached the patient exactly once, as
synthesized Bengali speech, digit by digit, over a phone line -- and then
it was gone. reply_templates.missing_slot_prompt() has always ASKED for a
phone number on the stated grounds that we would "send the confirmation"
there; nothing ever did. This module is what makes that promise true.

THREE RULES SHAPE EVERY DESIGN DECISION BELOW
---------------------------------------------
1. A message failure must never become a booking failure.
   The appointment is committed first, in its own transaction. Sending is
   attempted afterwards and cannot roll it back. A patient with a
   confirmed slot and no SMS has a lesser problem than a patient whose
   booking was refused because an operator's API was slow.

2. Nothing is dropped silently.
   The ledger row is written INSIDE the booking transaction, as `queued`,
   before any network call is attempted. If this process is killed between
   the commit and the send, the row survives as `queued` and surfaces in
   the staff queue as stale. There is no code path that decides not to
   send and leaves no trace -- see STATUS_SKIPPED, which is a recorded
   outcome, not an early return.

3. The request path is not allowed to get slower.
   agent/tools_client.py gives up on this service after
   DEFAULT_TIMEOUT_S = 4.0 seconds, and a caller is holding a live phone
   line for every one of them. An SMS gateway is a third-party hop over
   the public internet with a multi-second tail. Putting that inline would
   spend the caller's entire budget on a message they are not waiting for.
   So delivery runs in a FastAPI BackgroundTask -- after the response has
   been returned -- and the endpoint's own latency is unchanged apart from
   one extra INSERT.

WHY urllib AND NOT httpx
------------------------
clinic-api/requirements.txt is fastapi, uvicorn, sqlalchemy, psycopg2 and
pydantic -- no HTTP client. agent/llm.py already faces the same choice for
its Ollama calls and answers it with urllib.request from the standard
library. Adding a dependency to a service deployed on a pod where package
installation has repeatedly been the failure mode is a real cost for no
gain here: this is one POST with a JSON body and a timeout.

NO RETRY LOOP, ON PURPOSE
-------------------------
Each background task makes exactly one attempt. A retry loop with backoff
would have to sleep inside a thread from the pool that also serves price
and availability lookups -- see tools_client.py's note on the 15-connection
ceiling this service works against. Occupying those threads on behalf of a
patient who is no longer on the phone is precisely the wrong trade at the
busy hour. A failed attempt is instead surfaced to staff, who can retry it
from the ledger. That is also what the acceptance criterion asks for:
failures surfaced, not silently absorbed.
"""
from __future__ import annotations

import dataclasses
import datetime
import json
import logging
import os
import urllib.error
import urllib.request

import message_templates as mt

log = logging.getLogger("clinic-api.notify")

# ---------------------------------------------------------------------------
# Ledger statuses. Persisted, so they are a stored data format.
# ---------------------------------------------------------------------------
# Row written, nothing sent yet. Also what a row is left as if this process
# dies mid-send -- which is why the staff queue treats an OLD `queued` row
# as a failure rather than as work in progress.
STATUS_QUEUED = "queued"
# The gateway accepted it. NOT proof the handset got it -- that only
# arrives as a delivery receipt, which is the whole reason `delivered`
# exists separately.
STATUS_SENT = "sent"
# A receipt confirmed handset delivery.
STATUS_DELIVERED = "delivered"
# The gateway rejected it, the network failed, or a receipt reported
# non-delivery. This is the status the staff queue is built around.
STATUS_FAILED = "failed"
# Deliberately not sent, with the reason recorded. Only reachable when the
# deployment has no gateway configured at all -- a bench pod. In a
# configured deployment a template problem is STATUS_FAILED, because there
# it is a misconfiguration someone must fix, not an expected state.
STATUS_SKIPPED = "skipped"

CHANNEL_SMS = "sms"

# Terminal for the purposes of "is anything still owed to this patient".
OPEN_STATUSES = (STATUS_QUEUED, STATUS_SENT)

# A `sent` row with no receipt after this long is treated as a failure by
# the staff queue. Operators deliver a receipt within seconds under normal
# conditions; fifteen minutes of silence means the message is not arriving
# and reception needs to know before the patient turns up without it.
#
# Configurable because it is provider-dependent in a way no default gets
# right everywhere -- notably, a deployment whose receipt callback is not
# reachable at all (see deploy/env.vast.sh) will never see a receipt, and
# wants this set high enough that the queue reports a genuine problem
# rather than every message ever sent.
DEFAULT_STALE_MINUTES = int(os.environ.get("HOSPITAL_GATEWAY_STALE_MINUTES", "15"))


class GatewayError(Exception):
    """The gateway hop itself failed. Caught at the boundary and recorded
    on the ledger row -- it is never allowed to escape into a request that
    is booking an appointment."""


@dataclasses.dataclass(frozen=True)
class GatewayConfig:
    """Everything the hospital gateway needs, all of it from the
    environment.

    This is the first credential in the repository. deploy/env.sh and
    deploy/env.vast.sh declare the NON-SECRET fields and deliberately leave
    api_key unset with a comment saying where it comes from, so that
    sourcing an env file never puts a live key in a shell history or a
    process listing captured in a bug report.
    """
    url: str
    api_key: str
    auth_header: str
    sender_id: str          # DLT "header", e.g. KCDIAG -- registered, 6 chars
    entity_id: str          # DLT Principal Entity ID
    country_code: str
    timeout_s: float
    dlr_token: str

    @property
    def configured(self) -> bool:
        """A gateway URL is what separates a real deployment from a bench
        pod. Everything else can be wrong and still be worth attempting;
        without a URL there is nowhere to attempt it."""
        return bool(self.url.strip())


def load_config() -> GatewayConfig:
    return GatewayConfig(
        url=os.environ.get("HOSPITAL_GATEWAY_URL", "").strip(),
        api_key=os.environ.get("HOSPITAL_GATEWAY_API_KEY", "").strip(),
        auth_header=os.environ.get("HOSPITAL_GATEWAY_AUTH_HEADER", "Authorization").strip(),
        sender_id=os.environ.get("HOSPITAL_GATEWAY_SENDER_ID", "").strip(),
        entity_id=os.environ.get("HOSPITAL_GATEWAY_ENTITY_ID", "").strip(),
        country_code=os.environ.get("HOSPITAL_GATEWAY_COUNTRY_CODE", "91").strip(),
        timeout_s=float(os.environ.get("HOSPITAL_GATEWAY_TIMEOUT_S", "8")),
        dlr_token=os.environ.get("HOSPITAL_GATEWAY_DLR_TOKEN", "").strip(),
    )


def msisdn(phone: str, country_code: str) -> str:
    """-> E.164 without the '+', which is what Indian gateways expect.

    agent/slot_parse.parse_phone() has already reduced whatever the caller
    said to the last 10 digits, stripping a spoken +91 or a leading 0. This
    puts the country code back on for the gateway, and leaves a number that
    is already longer than 10 digits alone -- a value that arrived from
    somewhere other than the voice path (staff entry, an import) may
    legitimately carry its own prefix.
    """
    digits = "".join(ch for ch in str(phone) if ch.isdigit())
    if len(digits) > 10:
        return digits
    return f"{country_code}{digits}"


def preflight(config: GatewayConfig, tpl: mt.MessageTemplate) -> str | None:
    """-> an error code if this message must not be sent, else None.

    Checked BEFORE the network call, because every condition here produces
    a rejection at the operator that we would pay for and then have to
    diagnose from a vendor error code. Registration state is knowable from
    here; there is no reason to learn it from them.
    """
    if not config.configured:
        return "gateway_not_configured"
    if not tpl.is_registered:
        return "template_not_registered"
    if not config.sender_id:
        return "sender_id_not_configured"
    if not config.entity_id:
        return "entity_id_not_configured"
    return None


@dataclasses.dataclass
class GatewayResult:
    """One attempt's outcome, in the shape the ledger row stores."""
    accepted: bool
    provider_message_id: str | None = None
    error_code: str | None = None
    error_detail: str | None = None


def send(config: GatewayConfig, *, to: str, text: str, tpl: mt.MessageTemplate,
         client_ref: str) -> GatewayResult:
    """POST one message to the hospital gateway.

    THE CONTRACT THIS ASSUMES, stated here the way tools_client.py states
    the clinic-api contract -- nothing below assumes the gateway already
    speaks it, and an adapter is a small change to this one function:

        POST <HOSPITAL_GATEWAY_URL>
        {"to": "919876543210", "sender_id": "KCDIAG",
         "entity_id": "<DLT PE ID>", "template_id": "<DLT content ID>",
         "message": "<rendered body>", "category": "transactional",
         "unicode": true, "client_ref": "<our ledger row id>"}

        200/202 -> {"message_id": "..."}     accepted for delivery
        4xx     -> {"error": "...", "code": "..."}   rejected, do not retry
        5xx/timeout                          -> transport failure

    `client_ref` is our own ledger row id, echoed back by the gateway. It
    is what lets a delivery receipt find its row even if the provider's
    message id never reached us because the response itself was lost.

    Never raises. Every failure mode is a GatewayResult, because the caller
    is a background task whose only job is to write the outcome down.
    """
    body = json.dumps({
        "to": to,
        "sender_id": config.sender_id,
        "entity_id": config.entity_id,
        "template_id": tpl.template_id,
        "message": text,
        "category": tpl.category,
        # Bengali is UCS-2 on the wire. A gateway told otherwise transcodes
        # it to GSM-7 and delivers a message of question marks -- which is
        # then reported as a SUCCESSFUL delivery, the worst possible
        # failure mode for a message a patient has to show at a counter.
        "unicode": tpl.lang != "en",
        "client_ref": client_ref,
    }).encode("utf-8")

    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if config.api_key:
        value = config.api_key
        if config.auth_header.lower() == "authorization" and not value.lower().startswith("bearer "):
            value = f"Bearer {value}"
        headers[config.auth_header] = value

    req = urllib.request.Request(config.url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=config.timeout_s) as resp:
            raw = resp.read().decode("utf-8", "replace")
            payload = _parse_json(raw)
            return GatewayResult(
                accepted=True,
                provider_message_id=_provider_id(payload),
            )
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500] if e.fp else ""
        payload = _parse_json(detail)
        return GatewayResult(
            accepted=False,
            error_code=str(payload.get("code") or f"http_{e.code}"),
            error_detail=(payload.get("error") or detail or str(e))[:500],
        )
    except urllib.error.URLError as e:
        # Connection refused, DNS failure, TLS failure, and -- because
        # urllib raises socket.timeout inside URLError -- the timeout too.
        return GatewayResult(accepted=False, error_code="transport",
                             error_detail=str(e.reason)[:500])
    except Exception as e:                      # noqa: BLE001
        # Deliberately broad. This runs in a background task; an
        # unhandled exception here would be logged by starlette and the
        # ledger row would stay `queued` forever with no reason recorded.
        return GatewayResult(accepted=False, error_code="unexpected",
                             error_detail=f"{type(e).__name__}: {e}"[:500])


def _parse_json(raw: str) -> dict:
    try:
        out = json.loads(raw)
        return out if isinstance(out, dict) else {}
    except (ValueError, TypeError):
        return {}


def _provider_id(payload: dict) -> str | None:
    """Gateways disagree about what to call this. Accept the four spellings
    seen in the wild rather than making the adapter a separate file."""
    for key in ("message_id", "messageId", "provider_message_id", "id"):
        value = payload.get(key)
        if value:
            return str(value)[:120]
    return None


# ---------------------------------------------------------------------------
# Delivery-receipt vocabulary
# ---------------------------------------------------------------------------
# Operators report a handful of states under many names. Anything not
# recognised is treated as a failure rather than as success, so an
# unmapped vendor code surfaces to staff instead of being read as
# "delivered" by default.
_DELIVERED_WORDS = {"delivered", "dlvrd", "delivrd", "success", "delivered_to_handset"}
_FAILED_WORDS = {"failed", "undeliv", "undelivered", "expired", "rejected",
                 "rejectd", "blocked", "unknown", "error", "dnd"}
_PENDING_WORDS = {"sent", "queued", "accepted", "enroute", "en_route", "pending", "submitted"}


def classify_receipt(raw_status: str) -> str:
    """-> one of STATUS_DELIVERED / STATUS_FAILED / STATUS_SENT.

    STATUS_SENT is returned for a genuinely in-flight report, which leaves
    the row open and lets the staleness rule catch it later if no terminal
    receipt ever arrives.
    """
    key = (raw_status or "").strip().lower().replace("-", "_")
    if key in _DELIVERED_WORDS:
        return STATUS_DELIVERED
    if key in _PENDING_WORDS:
        return STATUS_SENT
    return STATUS_FAILED
