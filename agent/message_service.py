"""The message channel: a patient's WhatsApp messages, answered by the phone
line's own reasoning.

Author: Chakravardhan
Story:  "As a patient, I want to ask the same questions by message and get the
         same answers, so that I can use the channel I already have open."
Criteria: the intent and tool layer is shared with voice rather than
          duplicated, and conversation state survives a switch between
          channels. Integration is through an official business provider
          with template compliance.

SHARED, NOT DUPLICATED
----------------------
This service holds no reasoning of its own. Each message becomes one turn
of the voice agent's turn loop -- main_pcm.py by default; its reasoning half
is byte-identical to main.py's, which tools/make_pcm_variant.py enforces --
entered through run_text_turn() at exactly the point a keypad digit enters.
The fast path, the intent cache, the LLM, slot filling, the clinic-api calls
and the reply templates are therefore the phone line's own, and the answer to
"CBC-র দাম কত" by message is the sentence the phone line speaks, from the
same clinic-api response.

WHY A SEPARATE SERVICE
----------------------
The voice agent's HTTP surface is pinned by scripts/gate-contracts/, and this
story must not change it. A separate process also keeps a burst of messages
off the event loop that paces live audio. It loads no ASR, VAD or TTS.

WHAT A MESSAGE CANNOT DO
------------------------
Carry a patient's history or bookings. A written answer stays on a shared
handset's screen for whoever picks it up next; agent/privacy.py
channel_is_private() refuses before any verification is attempted, and the
patient is pointed to the phone line and the counter.

STATE ACROSS CHANNELS
---------------------
agent/conversation_store.py, keyed by the number the patient writes from. A
booking begun on a verified call is continued here; one begun here is picked
up by a call once the caller is verified (main.py _resume_from_other_channel).

RUNNING IT
----------
From the repository root, beside the voice agent and clinic-api:

    uvicorn agent.message_service:app --host 0.0.0.0 --port 8090

with WHATSAPP_ACCESS_TOKEN, WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_APP_SECRET and
WHATSAPP_VERIFY_TOKEN set for a live number, and VOICE_AGENT_STATE_KEY set to
the same value the voice agent has. On a bench pod with no WhatsApp number,
MESSAGE_SERVICE_SIMULATOR=1 enables POST /api/messages/simulate, which
returns the replies instead of sending them.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from types import ModuleType

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

from agent import call_audit, conversation_store, whatsapp
from agent import language as lang_mod
from agent.i18n import t

log = logging.getLogger("agent.message_service")

# The channel in conversation state, and the transport in the call audit.
CHANNEL = whatsapp.CHANNEL

# The turn loop this service drives. main_pcm is what production serves.
DEFAULT_VOICE_MODULE = "main_pcm"


def simulator_enabled(config: whatsapp.Config) -> bool:
    """Whether POST /api/messages/simulate may run.

    Never on a live number, whatever the flag says: the simulator names its
    own sender, so on a live deployment it would let anybody write as any
    patient -- including booking under their number."""
    flag = os.environ.get("MESSAGE_SERVICE_SIMULATOR", "0").strip().lower() in ("1", "true", "yes", "on")
    return flag and not config.configured


class MessageSession:
    """Stands where CallSession stands in main.py's turn loop, for one message.

    Only what that loop reads on its text path. What it does NOT have is the
    point: no audio buffer, no echo reference, no playback gate, and never a
    verification token -- privacy.channel_is_private() refuses before one
    could be asked for.
    """

    def __init__(
        self,
        *,
        sender: str,
        number: str | None,
        lang: str,
        pending: dict | None,
        window_open: bool,
        audit: call_audit.CallAudit,
        deliver: Callable[[MessageSession, str], Awaitable[bool]],
    ):
        self.channel = CHANNEL
        self.call_id = audit.call_id
        self.sender = sender
        self.lang = lang
        self.pending = pending
        self.window_open = window_open
        self.audit = audit
        self.dispatch_lock = asyncio.Lock()
        # The number this patient writes from, in the form clinic-api keeps.
        # The turn loop uses it only to file the privacy refusal against the
        # right record -- no challenge is ever started on this channel.
        self.history_phone = number
        self.history_token: str | None = None
        self.timeline: dict | None = None
        self.timeline_stale = False
        self.echo = None
        self.replies: list[str] = []
        self.template_sent = False
        self._deliver = deliver

    async def send_json(self, sender: str, text: str) -> None:
        """The voice client's on-screen transcript. A message thread already
        is one, so there is nothing to send."""
        del sender, text

    async def deliver_text(self, text: str) -> bool:
        """-> whether the patient will see these words."""
        delivered = await self._deliver(self, text)
        if delivered:
            self.replies.append(text)
        return delivered


class MessageChannel:
    """Everything one running service holds. Built by the startup hook, or
    directly by a test with fakes in place of the network."""

    def __init__(
        self,
        voice: ModuleType,
        store: conversation_store.ConversationStore,
        config: whatsapp.Config,
        client: httpx.AsyncClient,
        *,
        simulator: bool = False,
    ):
        self.voice = voice
        self.store = store
        self.config = config
        self.client = client
        self.simulator = simulator
        # One lock per sender, so a patient's messages are answered in order:
        # the second must see the state the first left. Bounded by the number
        # of distinct patients who write, which is the clinic's patient list.
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()

    # -- inbound -------------------------------------------------------------
    def accept(self, inbound: list[whatsapp.Inbound]) -> int:
        """Schedule each message not seen before. -> how many were new.

        The webhook is answered before any of them is processed: Meta wants a
        quick 200 and redelivers otherwise, and a turn can take as long as
        the LLM does."""
        new = 0
        for msg in inbound:
            if not self.store.first_sight(msg.message_id):
                continue
            new += 1
            task = asyncio.create_task(self.answer(msg))
            self._tasks.add(task)
            task.add_done_callback(self._finished)
        return new

    def _finished(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("a message turn crashed: %s", type(task.exception()).__name__)

    async def answer(self, msg: whatsapp.Inbound) -> MessageSession:
        """One message, answered in the patient's turn order."""
        lock = self._locks.setdefault(msg.sender, asyncio.Lock())
        async with lock:
            return await self._answer(msg)

    async def _answer(self, msg: whatsapp.Inbound) -> MessageSession:
        # FIRST, before the language is decided: a written turn is answered in
        # the language it was WRITTEN in, even on a pod whose only speech model
        # is Bengali. See language.use_text_channel().
        lang_token = lang_mod.use_text_channel()
        self.store.note_inbound(msg.sender, msg.sent_at, CHANNEL)
        snap = self.store.load(msg.sender, CHANNEL)
        # A new conversation is answered in the script the patient wrote in
        # -- typed text is a far better signal than ASR output. After that the
        # language only changes when they ask, exactly as on the phone line.
        lang = snap.lang if snap and snap.lang else lang_mod.detect_from_text(msg.text)
        resumed = snap is not None and snap.pending is not None and snap.channel != CHANNEL
        last_inbound = max(msg.sent_at, (snap.last_inbound_at or 0.0) if snap else 0.0)

        audit = call_audit.CallAudit(self.voice.text_audit_store(), transport=CHANNEL, language=lang)
        session = MessageSession(
            sender=msg.sender,
            number=whatsapp.local_number(msg.sender, self.config.country_code),
            lang=lang,
            pending=snap.pending if snap else None,
            window_open=whatsapp.window_open(last_inbound, time.time()),
            audit=audit,
            deliver=self._deliver,
        )
        # So the shared ClinicToolsClient files every API event of this turn
        # under this message's record -- see call_audit.bind().
        call_audit.bind(audit)
        ending = call_audit.END_MESSAGE_TURN
        try:
            if msg.kind != "text" or not msg.text.strip():
                audit.begin_turn()
                audit.transcript(None, source="message", status="unsupported", kind=msg.kind)
                await self.voice.speak_to(session, t(session.lang, "channel.text_only"))
            else:
                if resumed:
                    await self.voice.speak_to(session, t(session.lang, "channel.resumed"))
                await self.voice.run_text_turn(session, msg.text)
        except BaseException:
            ending = call_audit.END_EXCEPTION
            raise
        finally:
            lang_mod.reset_text_channel(lang_token)
            self.store.save(msg.sender, lang=session.lang, pending=session.pending, channel=CHANNEL)
            audit.end(ending, language=session.lang, open_flow=(session.pending or {}).get("awaiting"))
        return session

    # -- outbound ------------------------------------------------------------
    async def _deliver(self, session: MessageSession, text: str) -> bool:
        """How one reply leaves. -> whether the patient will see these words."""
        if self.simulator:
            return True
        if not self.config.configured:
            log.info("[%s] no WhatsApp number configured -- reply recorded, not sent", session.call_id)
            return False
        if not session.window_open:
            await self._outside_window(session)
            return False
        result = await whatsapp.send_text(self.client, self.config, session.sender, text)
        if not result.accepted:
            log.warning("[%s] WhatsApp did not accept a reply: %s", session.call_id, result.error_code)
        return result.accepted

    async def _outside_window(self, session: MessageSession) -> None:
        """TEMPLATE COMPLIANCE. The 24-hour window has closed, so the answer
        itself may not be sent. The approved `reply_expired` template may --
        once per message -- and with none approved, nothing is sent at all."""
        if session.template_sent:
            return
        session.template_sent = True
        name = self.config.reply_expired_template
        if not name:
            log.warning(
                "[%s] reply falls outside the 24-hour window and no template is approved -- nothing sent",
                session.call_id,
            )
            session.audit.record(
                "TEMPLATE_MESSAGE", {"template": "reply_expired", "sent": False}, success=False
            )
            return
        result = await whatsapp.send_template(self.client, self.config, session.sender, name, session.lang)
        session.audit.record(
            "TEMPLATE_MESSAGE",
            {"template": "reply_expired", "sent": result.accepted, "error_code": result.error_code},
            success=result.accepted,
        )


# ===========================================================================
# HTTP
# ===========================================================================
app = FastAPI(title="Kolkata Care Diagnostics -- message channel")
_channel: MessageChannel | None = None


def _require_channel() -> MessageChannel:
    if _channel is None:
        raise HTTPException(status_code=503, detail="message channel is starting")
    return _channel


@app.on_event("startup")
async def _startup() -> None:
    global _channel
    voice = importlib.import_module(os.environ.get("MESSAGE_SERVICE_VOICE_MODULE", DEFAULT_VOICE_MODULE))
    await voice.start_text_services(CHANNEL)
    config = whatsapp.load_config()
    if config.configured and not config.app_secret:
        log.error("WHATSAPP_APP_SECRET is unset -- every webhook will be refused until it is set")
    _channel = MessageChannel(
        voice,
        conversation_store.ConversationStore(),
        config,
        httpx.AsyncClient(),
        simulator=simulator_enabled(config),
    )


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _channel is None:
        return
    await _channel.client.aclose()
    _channel.store.close()
    await _channel.voice.stop_text_services()


@app.get("/api/health")
async def health() -> dict:
    channel = _require_channel()
    return {
        "status": "ok",
        "channel": CHANNEL,
        "whatsapp_configured": channel.config.configured,
        "webhook_secret_set": bool(channel.config.app_secret),
        "reply_expired_template_approved": bool(channel.config.reply_expired_template),
        "simulator": channel.simulator,
        "conversation_state": channel.store.health(),
        "in_flight": len(channel._tasks),
    }


@app.get("/webhook/whatsapp")
async def verify_webhook(
    mode: str | None = Query(None, alias="hub.mode"),
    verify_token: str | None = Query(None, alias="hub.verify_token"),
    challenge: str | None = Query(None, alias="hub.challenge"),
):
    """Meta's one-time registration handshake."""
    answer = whatsapp.challenge_response(_require_channel().config, mode, verify_token, challenge)
    if answer is None:
        return JSONResponse(status_code=403, content={"error": "verification failed"})
    return PlainTextResponse(answer)


@app.post("/webhook/whatsapp")
async def receive_webhook(request: Request):
    """Messages and delivery statuses from Meta. Refused unless signed with
    the app secret: an unsigned webhook could book under any number."""
    channel = _require_channel()
    raw = await request.body()
    if not whatsapp.signature_valid(
        channel.config.app_secret, raw, request.headers.get("x-hub-signature-256")
    ):
        log.warning("webhook refused: signature missing or wrong")
        return JSONResponse(status_code=403, content={"error": "bad signature"})
    try:
        payload = json.loads(raw)
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "not JSON"})
    inbound, statuses = whatsapp.parse_webhook(payload)
    for delivery in statuses:
        if delivery.status == "failed":
            log.warning("WhatsApp reports a reply was not delivered (error %s)", delivery.error_code)
    return {"received": channel.accept(inbound)}


class SimulatedMessage(BaseModel):
    sender: str
    text: str


@app.post("/api/messages/simulate")
async def simulate(req: SimulatedMessage):
    """The bench entry point: one message in, the replies back, nothing sent.
    404 unless MESSAGE_SERVICE_SIMULATOR is on AND no live number is set."""
    channel = _require_channel()
    if not channel.simulator:
        return JSONResponse(status_code=404, content={"error": "simulator disabled"})
    msg = whatsapp.Inbound(
        message_id=f"sim-{uuid.uuid4().hex}",
        sender="".join(ch for ch in req.sender if ch.isdigit()),
        kind="text",
        text=req.text,
        sent_at=time.time(),
    )
    session = await channel.answer(msg)
    return {
        "replies": session.replies,
        "lang": session.lang,
        "awaiting": (session.pending or {}).get("awaiting"),
    }
