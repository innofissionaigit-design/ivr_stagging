"""The WhatsApp Business Platform (Meta Cloud API) -- the official channel.

Author: Chakravardhan
Story:  "As a patient, I want to ask the same questions by message and get the
         same answers, so that I can use the channel I already have open."

WHY THE OFFICIAL PLATFORM, AND NOT SMS
--------------------------------------
Every outbound SMS in India must match a DLT-registered template
(clinic-api/message_templates.py). A reply that quotes a price, a doctor's
hours or a list of free slots would need every one of those sentences
registered before it could be sent -- "the same answers" would mean
registering the whole reply table. WhatsApp's rule is different, and it is
the rule this module enforces:

  * a FREE-FORM message may be sent only inside the 24-hour customer-service
    window that opens every time the patient writes to us;
  * outside that window, ONLY a pre-approved template may be sent.

Every answer this service gives is a reply to something the patient has
just written, so it goes out inside the window as the same templated
sentence the voice line would speak. The window is still checked at send
time, not assumed: Meta redelivers a webhook it could not hand over for up
to seven days, so a message can reach us long after it was written. A reply
that would land outside the window is never sent free-form -- the registered
`reply_expired` template goes instead, or nothing, and the audit says which.

NOTHING HERE NEEDS A SMARTPHONE TO BE UNDERSTOOD
------------------------------------------------
Link previews are switched off on every message, and no body sent from here
carries a link, a code or an app -- the same rule tests/test_no_smartphone.py
holds every spoken sentence to. The phone line and the counter stay the
paths for a patient without one.

CREDENTIALS COME FROM THE ENVIRONMENT
-------------------------------------
WHATSAPP_ACCESS_TOKEN and WHATSAPP_APP_SECRET are secrets and are never
written into the repository. WHATSAPP_VERIFY_TOKEN is the string Meta echoes
back when the webhook is registered.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import logging
import os
import time

import httpx

log = logging.getLogger("agent.whatsapp")

# The value this channel writes as `channel` in conversation state and as
# `transport` in the call audit.
CHANNEL = "whatsapp"

GRAPH_API_BASE = "https://graph.facebook.com"
DEFAULT_GRAPH_VERSION = "v21.0"

# Meta's customer-service window: free-form replies are allowed for 24 hours
# after the patient's most recent message.
CUSTOMER_SERVICE_WINDOW_S = 24 * 3600.0

# Meta rejects a text body over this many characters. Every reply template
# is far shorter; the clip is a guard, not a formatting step.
TEXT_BODY_MAX = 4096

# ---------------------------------------------------------------------------
# REGISTERED TEMPLATES
#
# The ONLY message that may be sent outside the customer-service window. Its
# approved name comes from the deployment (WHATSAPP_TEMPLATE_REPLY_EXPIRED),
# as the DLT ids do for SMS: a name committed here would tie this source tree
# to one hospital's WhatsApp Business account. Unset means "not approved
# yet", and nothing is sent outside the window at all.
#
# The body to submit for approval, category UTILITY, no variables, one per
# language -- verbatim, so it can be diffed against WhatsApp Manager:
REPLY_EXPIRED_BODY = {
    "bn": "আপনার মেসেজ পেয়েছিলাম, কিন্তু সময়মতো উত্তর দিতে পারিনি। দয়া করে আবার লিখুন, অথবা ফোন করুন।",
    "hi": "आपका मैसेज मिला था, पर समय पर जवाब नहीं दे पाए। कृपया फिर से लिखें, या फ़ोन करें।",
    "en": "We received your message but could not reply in time. Please write again, or call us.",
}
# WhatsApp's language codes for the three this line speaks.
TEMPLATE_LANGUAGE = {"bn": "bn", "hi": "hi", "en": "en"}


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclasses.dataclass(frozen=True)
class Config:
    """Everything the channel needs, all of it from the environment."""

    access_token: str
    phone_number_id: str
    app_secret: str
    verify_token: str
    api_base: str
    graph_version: str
    country_code: str
    timeout_s: float
    reply_expired_template: str

    @property
    def configured(self) -> bool:
        """A business number and a token are what separate a live
        deployment from a bench pod. Without both there is nowhere to send."""
        return bool(self.access_token and self.phone_number_id)

    @property
    def messages_url(self) -> str:
        return f"{self.api_base.rstrip('/')}/{self.graph_version}/{self.phone_number_id}/messages"


def load_config() -> Config:
    return Config(
        access_token=_env("WHATSAPP_ACCESS_TOKEN"),
        phone_number_id=_env("WHATSAPP_PHONE_NUMBER_ID"),
        app_secret=_env("WHATSAPP_APP_SECRET"),
        verify_token=_env("WHATSAPP_VERIFY_TOKEN"),
        api_base=_env("WHATSAPP_API_BASE", GRAPH_API_BASE),
        graph_version=_env("WHATSAPP_GRAPH_VERSION", DEFAULT_GRAPH_VERSION),
        country_code=_env("WHATSAPP_COUNTRY_CODE", "91"),
        timeout_s=float(_env("WHATSAPP_TIMEOUT_S", "8")),
        reply_expired_template=_env("WHATSAPP_TEMPLATE_REPLY_EXPIRED"),
    )


# ---------------------------------------------------------------------------
# Inbound: proving the webhook is Meta's, and reading it
# ---------------------------------------------------------------------------
def signature_valid(app_secret: str, raw_body: bytes, header: str | None) -> bool:
    """X-Hub-Signature-256 is "sha256=" + HMAC-SHA256(app secret, raw body).

    False without a configured secret: a webhook that cannot be proved to
    come from Meta is not one this service acts on -- it can book an
    appointment under the number it names."""
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256=") :].strip().lower())


def challenge_response(
    config: Config, mode: str | None, token: str | None, challenge: str | None
) -> str | None:
    """Meta's webhook registration handshake. -> the challenge to echo, or None."""
    if mode != "subscribe" or not config.verify_token or not token:
        return None
    if not hmac.compare_digest(token.encode(), config.verify_token.encode()):
        return None
    return challenge or ""


@dataclasses.dataclass(frozen=True)
class Inbound:
    """One message a patient sent."""

    message_id: str
    sender: str  # digits, E.164 without the '+', exactly as Meta reports it
    kind: str  # "text", or Meta's type for anything else (audio, image, ...)
    text: str
    sent_at: float  # when the patient sent it -- Meta's timestamp, not ours


@dataclasses.dataclass(frozen=True)
class DeliveryStatus:
    """A status Meta reports for a message we sent."""

    message_id: str
    status: str
    error_code: str | None


def _digits(value: object) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _sent_at(value: object) -> float:
    try:
        return float(str(value))
    except ValueError:
        return time.time()


def parse_webhook(payload: object) -> tuple[list[Inbound], list[DeliveryStatus]]:
    """-> (messages, delivery statuses) from one webhook body.

    Tolerant by design: an unexpected shape yields nothing rather than an
    exception, because Meta retries any webhook we fail -- a parser that
    raises would turn one odd payload into a week of redeliveries."""
    inbound: list[Inbound] = []
    statuses: list[DeliveryStatus] = []
    if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
        return inbound, statuses
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            value = change.get("value") if isinstance(change, dict) else None
            if not isinstance(value, dict):
                continue
            for msg in value.get("messages") or []:
                if not isinstance(msg, dict) or not msg.get("id") or not _digits(msg.get("from")):
                    continue
                kind = str(msg.get("type") or "unknown")
                text_part = msg.get("text")
                text = (
                    str(text_part.get("body") or "") if kind == "text" and isinstance(text_part, dict) else ""
                )
                inbound.append(
                    Inbound(
                        message_id=str(msg["id"]),
                        sender=_digits(msg.get("from")),
                        kind=kind,
                        text=text,
                        sent_at=_sent_at(msg.get("timestamp")),
                    )
                )
            for st in value.get("statuses") or []:
                if not isinstance(st, dict) or not st.get("id"):
                    continue
                errors = st.get("errors") or []
                first = errors[0] if errors and isinstance(errors[0], dict) else {}
                code = first.get("code")
                statuses.append(
                    DeliveryStatus(
                        message_id=str(st["id"]),
                        status=str(st.get("status") or ""),
                        error_code=str(code) if code is not None else None,
                    )
                )
    return inbound, statuses


def local_number(sender: str, country_code: str) -> str | None:
    """-> the ten-digit number clinic-api keeps (agent/slot_parse.parse_phone
    reduces every spoken number to the same form), or None for a number
    from another country, which this clinic's records cannot hold."""
    digits = _digits(sender)
    if len(digits) == len(country_code) + 10 and digits.startswith(country_code):
        return digits[-10:]
    if len(digits) == 10:
        return digits
    return None


def window_open(last_inbound_at: float | None, now: float) -> bool:
    """Whether a free-form message may be sent now."""
    return last_inbound_at is not None and now - last_inbound_at < CUSTOMER_SERVICE_WINDOW_S


# ---------------------------------------------------------------------------
# Outbound
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class SendResult:
    accepted: bool
    message_id: str | None = None
    error_code: str | None = None


async def send_text(client: httpx.AsyncClient, config: Config, to: str, text: str) -> SendResult:
    """One free-form reply. The caller has already checked the window."""
    return await _post(
        client,
        config,
        {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"preview_url": False, "body": text[:TEXT_BODY_MAX]},
        },
    )


async def send_template(
    client: httpx.AsyncClient, config: Config, to: str, name: str, lang: str
) -> SendResult:
    """One pre-approved template, with no variables."""
    return await _post(
        client,
        config,
        {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "template",
            "template": {"name": name, "language": {"code": TEMPLATE_LANGUAGE.get(lang, "en")}},
        },
    )


async def _post(client: httpx.AsyncClient, config: Config, payload: dict) -> SendResult:
    """Never raises: every failure is a SendResult, and the caller writes it
    into the audit as an undelivered reply."""
    try:
        resp = await client.post(
            config.messages_url,
            json=payload,
            headers={"Authorization": f"Bearer {config.access_token}"},
            timeout=config.timeout_s,
        )
    except httpx.HTTPError as e:
        return SendResult(accepted=False, error_code=f"transport:{type(e).__name__}")
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if 200 <= resp.status_code < 300:
        sent = data.get("messages") or []
        first = sent[0] if sent and isinstance(sent[0], dict) else {}
        return SendResult(accepted=True, message_id=str(first.get("id") or "") or None)
    raw_error = data.get("error")
    err = raw_error if isinstance(raw_error, dict) else {}
    return SendResult(accepted=False, error_code=str(err.get("code") or f"http_{resp.status_code}"))
