"""Tests for "The same questions by message".

Author: Chakravardhan
Story:  "As a patient, I want to ask the same questions by message and get the
         same answers, so that I can use the channel I already have open."
Criteria: the intent and tool layer is shared with voice rather than
          duplicated, and conversation state survives a switch between
          channels. Integration is through an official business provider
          with template compliance.

WHAT THESE COVER
----------------
  * THE SAME ANSWERS -- a message is answered by the phone line's own turn
    loop, word for word; the message service holds no reasoning of its own.
  * STATE ACROSS CHANNELS -- a booking is built up over several messages; a
    booking begun on a verified call is finished by message, and one begun
    by message is picked up by a call once the caller is verified. Nothing
    of verification ever crosses, and the number is never the row's key.
  * NOT LOOSENED -- history and bookings are never written into a message,
    whatever the bench switch says, and the patient is sent to the phone
    line and the counter.
  * THE OFFICIAL PROVIDER -- Meta's signed webhook and registration
    handshake, the reply payload, and template compliance: free-form only
    inside the 24-hour window, the approved template (or nothing) outside it.

The network is faked at exactly one place, the httpx transport the WhatsApp
client posts through. Everything else -- the turn loop, slot filling, the
privacy guard, the reply templates, the audit -- is the real code.

    python -m pytest tests/test_message_channel.py -v
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import hmac
import json
import sqlite3
import time
import types
import uuid

import gate_support
import httpx
import pytest
from fastapi.testclient import TestClient
from test_no_smartphone import _offences

from agent import call_audit, conversation_store, i18n, message_service, privacy, whatsapp
from agent import language as lang_mod
from agent.echo_guard import PATH_HANDSET, PATH_SPEAKERPHONE, PATH_UNKNOWN

# Aliased: a name starting with "test_" would be collected by pytest as a test.
from agent.reply_templates import booking_reply, missing_slot_prompt
from agent.reply_templates import test_rate_reply as rate_reply

PHONE = "9000000101"
OTHER = "9000000102"
SENDER = "91" + PHONE
DOB = "1990-05-14"
APP_SECRET = "wa-s3cret"
VERIFY = "verify-me"

_SLOT_KEYS = ("test_name", "doctor_name", "department", "date", "time_slot", "patient_name", "phone")

_RATE = {
    "found": True,
    "rate_inr": 650,
    "test_name": "CBC",
    "test_name_bn": "সিবিসি",
    "sample_type": "Blood",
    "report_time_hours": 24,
}
_BOOKED = {
    "success": True,
    "confirmation_id": "KCD-20260920-MC01",
    "doctor_name": "Dr. A. Sen",
    "doctor_name_bn": "সেন",
    "date": "2026-09-20",
    "time_slot": "18:15",
    "notification": {"status": "queued"},
}
_RECORD = {
    "found": True,
    "patient_name": "Iti Sen",
    "tests": [],
    "appointments": [],
    "timeline": [],
    "upcoming_appointments": [],
}


def _intent(name: str, **slots) -> dict:
    full = {k: None for k in _SLOT_KEYS}
    full.update(slots)
    return {"intent": name, "slots": full, "direct_reply_bn": None}


# ===========================================================================
# Fixtures
# ===========================================================================
@pytest.fixture
def store(tmp_path):
    s = conversation_store.ConversationStore(path=str(tmp_path / "state.db"), key=b"bench-key")
    yield s
    s.close()


@pytest.fixture
def voice(monkeypatch):
    """The voice agent's module, with the clinic and the model faked and the
    spoken half of _speak recorded. The WRITTEN half of _speak is the real
    one -- it is part of what is under test."""
    app = gate_support.load_main_pcm()
    monkeypatch.delenv("VOICE_AGENT_HISTORY_DISCLOSURE", raising=False)
    tools = gate_support.FakeTools(
        responses={
            "get_test_rate": _RATE,
            "book_appointment": _BOOKED,
            "begin_verification": {"factor": "dob", "locked": False},
            "verify_caller": {"reply": "verified", "verified": True, "factor": "dob", "token": "tok-mc"},
            "read_history": _RECORD,
        }
    )
    intents: dict[str, dict] = {}
    spoken: list[str] = []
    real_speak = app._speak

    async def resolve(session, text):
        data = intents[text]
        app._record_intent(session, data, "test")
        return data

    async def speak(session, text, fallback_reason=None, audit_redact=None):
        if getattr(session, "channel", privacy.CHANNEL_VOICE) != privacy.CHANNEL_VOICE:
            await real_speak(session, text, fallback_reason, audit_redact)
        else:
            spoken.append(text)

    monkeypatch.setattr(app, "_tools", tools)
    monkeypatch.setattr(app, "_resolve_intent", resolve)
    monkeypatch.setattr(app, "_speak", speak)
    monkeypatch.setattr(app, "_conversations", None)
    monkeypatch.setattr(app, "_audit_store", None)
    return types.SimpleNamespace(app=app, tools=tools, intents=intents, spoken=spoken)


def _channel(voice, store, *, configured=True, template="", simulator=False, status=200):
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        if status == 200:
            return httpx.Response(200, json={"messages": [{"id": f"wamid.{len(sent)}"}]})
        return httpx.Response(status, json={"error": {"code": 131047, "message": "refused"}})

    config = whatsapp.Config(
        access_token="bench-token" if configured else "",
        phone_number_id="1234" if configured else "",
        app_secret=APP_SECRET,
        verify_token=VERIFY,
        api_base="https://graph.test",
        graph_version="v21.0",
        country_code="91",
        timeout_s=2.0,
        reply_expired_template=template,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    channel = message_service.MessageChannel(voice.app, store, config, client, simulator=simulator)
    return channel, sent


def _write(channel, text, *, sender=SENDER, sent_at=None, kind="text"):
    msg = whatsapp.Inbound(
        message_id=f"wamid.in-{uuid.uuid4().hex}",
        sender=sender,
        kind=kind,
        text=text,
        sent_at=time.time() if sent_at is None else sent_at,
    )
    return asyncio.run(channel.answer(msg))


def _call(voice, text):
    session = voice.app.CallSession(gate_support.FakeWS())
    asyncio.run(voice.app._run_turn(session, "", text_override=text))
    session.cleanup()


# ===========================================================================
# THE SAME ANSWERS -- shared with voice, not duplicated
# ===========================================================================
def test_a_message_gets_the_answer_the_phone_line_speaks(voice, store):
    question = "CBC-র দাম কত"
    voice.intents[question] = _intent("test_rate", test_name="CBC")
    channel, sent = _channel(voice, store)

    session = _write(channel, question)
    _call(voice, question)

    expected = rate_reply(voice.intents[question]["slots"], _RATE, "bn")
    assert session.replies == [expected]
    assert voice.spoken == [expected]  # the phone line, same words
    assert sent[-1]["text"]["body"] == expected


def test_a_number_the_patient_types_wins_over_the_one_they_write_from(voice, store):
    """Booking for a relative who will take the call. Their words win."""
    ask = "মায়ের জন্য বুক করুন"
    voice.intents[ask] = _intent(
        "book_appointment",
        doctor_name="সেন",
        date="2026-09-20",
        time_slot="18:15",
        patient_name="Jaya Sen",
        phone=OTHER,
    )
    channel, _sent = _channel(voice, store)
    _write(channel, ask)
    _name, args, _kw = voice.tools.calls[-1]
    assert args[3:] == ("Jaya Sen", OTHER)


def test_the_phone_line_never_fills_a_number_from_the_channel(voice):
    """There is no caller-ID on the phone line: a number the caller merely
    SAID is not the number they are calling from, and must not be assumed."""
    call = voice.app.CallSession(gate_support.FakeWS())
    call.history_phone = PHONE
    slots: dict = {}
    assert voice.app._fill_from_channel(call, slots) is False
    assert slots == {}
    call.cleanup()


def test_the_message_service_holds_no_reasoning_of_its_own():
    """Structural: the service may not import the layers it is meant to share.
    If it did, a second copy of the reasoning could start to drift."""
    tree = ast.parse((gate_support.ROOT / "agent" / "message_service.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(f"{node.module}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    reasoning = {
        "agent.llm",
        "agent.fast_path",
        "agent.semantic_cache",
        "agent.tools_client",
        "agent.reply_templates",
        "agent.slot_parse",
    }
    assert not imported & reasoning


def test_a_booking_is_built_up_over_several_messages(voice, store):
    """Each message is a separate webhook and a separate session. What
    carries the booking from one to the next is the conversation store."""
    first = "সেনের কাছে ২০ তারিখ ১৮:১৫ বুক করুন"
    voice.intents[first] = _intent(
        "book_appointment", doctor_name="সেন", date="2026-09-20", time_slot="18:15"
    )
    channel, _sent = _channel(voice, store)

    s1 = _write(channel, first)
    assert s1.replies == [missing_slot_prompt("book_appointment", "patient_name")]
    # The NUMBER is never asked for here: the patient is writing from it.
    s2 = _write(channel, "Iti Sen")

    name, args, _kw = voice.tools.calls[-1]
    assert name == "book_appointment"
    assert args == ("সেন", "2026-09-20", "18:15", "Iti Sen", PHONE)
    assert s2.replies[-1] == booking_reply({"doctor_name": "সেন"}, _BOOKED, "bn")
    assert store.load(SENDER, whatsapp.CHANNEL).pending is None  # finished, so nothing left to offer


def test_the_language_follows_the_script_of_the_first_message_then_stays(voice, store, monkeypatch):
    gate_support.trilingual(monkeypatch)
    hindi = "सीबीसी की कीमत क्या है"
    voice.intents[hindi] = _intent("test_rate", test_name="CBC")
    voice.intents["আর একটা"] = _intent("test_rate", test_name="CBC")
    channel, _sent = _channel(voice, store)

    s1 = _write(channel, hindi)
    assert s1.lang == "hi"
    assert s1.replies == [rate_reply(voice.intents[hindi]["slots"], _RATE, "hi")]
    # One Bengali-script message does not flip the conversation; asking does.
    assert _write(channel, "আর একটা").lang == "hi"


def _as_written(fn, *args, **kwargs):
    """Render an expected sentence inside a written turn, so the expectation
    is not folded back to Bengali by the SPEECH gate (language.enabled)."""
    token = lang_mod.use_text_channel()
    try:
        return fn(*args, **kwargs)
    finally:
        lang_mod.reset_text_channel(token)


def _bengali_only_pod(monkeypatch):
    """The pod as it really is: one Bengali speech model, nothing else."""
    for var in ("VOICE_AGENT_LANGUAGES", "VOICE_AGENT_NEMO_FILE_HI", "VOICE_AGENT_NEMO_FILE_EN"):
        monkeypatch.delenv(var, raising=False)


@pytest.mark.parametrize(
    ("written", "code"),
    [
        ("सीबीसी की कीमत क्या है", "hi"),
        ("what is the price of CBC", "en"),
        ("সিবিসি টেস্টের রেট কত", "bn"),
    ],
)
def test_a_message_is_answered_in_the_language_it_was_written_in(voice, store, monkeypatch, written, code):
    """On a pod whose ONLY speech model is Bengali. Speech cannot be heard in
    Hindi there, but a typed Hindi message needs no speech model at all, and
    agent/i18n.py has every sentence in all three."""
    _bengali_only_pod(monkeypatch)
    voice.intents[written] = _intent("test_rate", test_name="CBC")
    channel, _sent = _channel(voice, store)

    session = _write(channel, written)

    assert session.lang == code
    assert session.replies == [_as_written(rate_reply, voice.intents[written]["slots"], _RATE, code)]


def test_one_english_word_does_not_flip_a_bengali_conversation(voice, store, monkeypatch):
    """Code-switching is normal here -- "রিপোর্ট ready তো?" is a Bengali
    sentence. The script of the MAJORITY decides, not any match."""
    _bengali_only_pod(monkeypatch)
    mixed = "রিপোর্ট ready তো?"
    voice.intents[mixed] = _intent("report_collection")
    channel, _sent = _channel(voice, store)
    assert _write(channel, mixed).lang == "bn"


def test_the_patient_can_ask_for_another_language_by_message(voice, store, monkeypatch):
    _bengali_only_pod(monkeypatch)
    channel, _sent = _channel(voice, store)

    switched = _write(channel, "hindi")
    assert switched.lang == "hi"
    assert switched.replies == [_as_written(i18n.t, "hi", "language.switched")]
    # ...and it holds for the next message, from the conversation store.
    assert store.load(SENDER, whatsapp.CHANNEL).lang == "hi"


def test_the_phone_line_is_still_bengali_only_on_that_pod(monkeypatch):
    """The ASR gate is about SPEECH and must stay exactly as it was: a pod
    with no Hindi checkpoint cannot hear Hindi, whatever the message channel
    may write."""
    _bengali_only_pod(monkeypatch)
    assert lang_mod.enabled() == ("bn",)
    assert not lang_mod.is_enabled("hi")
    assert lang_mod.requested_switch("hindi me batao") is None
    assert lang_mod.requested_switch_unavailable("hindi me batao") == "hi"
    # Only inside a written turn do the other two become answerable.
    token = lang_mod.use_text_channel()
    assert lang_mod.enabled() == ("bn", "hi", "en")
    lang_mod.reset_text_channel(token)
    assert lang_mod.enabled() == ("bn",)


# ===========================================================================
# NOT LOOSENED -- nothing private is ever written into a message
# ===========================================================================
@pytest.mark.parametrize("intent", ["my_bookings", "patient_history"])
def test_history_and_bookings_are_never_written_into_a_message(voice, store, intent):
    voice.intents["আমার রেকর্ড"] = _intent(intent)
    channel, sent = _channel(voice, store)

    session = _write(channel, "আমার রেকর্ড")

    assert session.replies == [i18n.t("bn", "channel.private_by_message")]
    # Refused BEFORE verification: no challenge, no record read -- only the
    # refusal itself, audited on the clinic side.
    assert voice.tools.called() == ["record_disclosure_refusal"]
    assert voice.tools.calls[0][1][:2] == (PHONE, privacy.UNSAFE_TEXT_CHANNEL)
    assert len(sent) == 1


def test_the_bench_weakening_does_not_open_the_message_channel(voice, store, monkeypatch):
    """VOICE_AGENT_HISTORY_REQUIRE_PRIVATE_PATH=0 lets a bench pod past the
    ROOM check. It must not let anything past the CHANNEL check."""
    monkeypatch.setenv("VOICE_AGENT_HISTORY_REQUIRE_PRIVATE_PATH", "0")
    assert privacy.channel_is_private(whatsapp.CHANNEL, None) == (False, privacy.UNSAFE_TEXT_CHANNEL)
    voice.intents["আমার বুকিং"] = _intent("my_bookings")
    channel, _sent = _channel(voice, store)
    assert _write(channel, "আমার বুকিং").replies == [i18n.t("bn", "channel.private_by_message")]
    assert "read_history" not in voice.tools.called()


def test_the_phone_line_is_judged_exactly_as_before():
    for path in (PATH_HANDSET, PATH_SPEAKERPHONE, PATH_UNKNOWN):
        guard = types.SimpleNamespace(classify=lambda p=path: p)
        assert privacy.channel_is_private(privacy.CHANNEL_VOICE, guard) == privacy.audio_path_is_private(
            guard
        )
    assert not privacy.recoverable(privacy.UNSAFE_TEXT_CHANNEL)


@pytest.mark.parametrize("code", ["bn", "hi", "en"])
def test_the_refusal_points_to_the_phone_line_and_the_counter(code):
    text = i18n.t(code, "channel.private_by_message")
    assert gate_support.mentions_counter(text)
    assert not _offences(text)


def test_nothing_this_channel_sends_needs_a_smartphone():
    for code, body in whatsapp.REPLY_EXPIRED_BODY.items():
        assert not _offences(body), code
    for key in ("channel.resumed", "channel.text_only", "channel.private_by_message"):
        for code in ("bn", "hi", "en"):
            assert not _offences(i18n.t(code, key)), (key, code)


# ===========================================================================
# STATE ACROSS CHANNELS
# ===========================================================================
def test_a_booking_begun_on_a_verified_call_is_finished_by_message(voice, store, monkeypatch):
    monkeypatch.setattr(voice.app, "_conversations", store)
    call = voice.app.CallSession(gate_support.FakeWS())
    call.history_token, call.history_phone = "tok-mc", PHONE
    call.pending = {
        "awaiting": "time_slot",
        "slots": {"doctor_name": "সেন", "date": "2026-09-20", "patient_name": "Iti Sen", "phone": PHONE},
        "candidates": None,
        "offered_date": None,
        "retries": 0,
        "from_record": True,
    }
    voice.app._save_for_other_channels(call)  # what ws_audio's finally does at hang-up
    call.cleanup()

    channel, _sent = _channel(voice, store)
    session = _write(channel, "18:15")

    assert session.replies[0] == i18n.t("bn", "channel.resumed")
    assert voice.tools.calls[-1][1] == ("সেন", "2026-09-20", "18:15", "Iti Sen", PHONE)
    # `from_record` was a fact about the CALL's verification; it does not cross.
    assert not session.replies[-1].endswith(i18n.t("bn", "timeline.used_record"))


def test_a_booking_begun_by_message_is_picked_up_by_a_verified_call(voice, store, monkeypatch):
    monkeypatch.setenv("VOICE_AGENT_HISTORY_REQUIRE_PRIVATE_PATH", "0")
    monkeypatch.setattr(voice.app, "_conversations", store)
    ask = "সেনের কাছে ২০ তারিখ বুক করুন"
    voice.intents[ask] = _intent("book_appointment", doctor_name="সেন", date="2026-09-20")
    channel, _sent = _channel(voice, store)
    _write(channel, ask)

    call = voice.app.CallSession(gate_support.FakeWS())
    call.history_phone = PHONE
    call.pending = {
        "awaiting": "history_verify",
        "factor": "dob",
        "purpose": "history",
        "slots": {},
        "candidates": None,
        "offered_date": None,
        "retries": 0,
    }
    asyncio.run(voice.app._run_turn(call, "", text_override=DOB))

    assert voice.spoken[-1] == i18n.t("bn", "channel.resumed") + missing_slot_prompt(
        "book_appointment", "time_slot", "bn"
    )
    # The number the patient wrote FROM crosses with the booking -- it is the
    # number the booking is for, not something the call learned about them.
    assert call.pending["slots"] == {"doctor_name": "সেন", "date": "2026-09-20", "phone": PHONE}
    # And the verified record fills the rest, as on any verified call.
    asyncio.run(voice.app._run_turn(call, "", text_override="18:15"))
    assert voice.tools.calls[-1][1] == ("সেন", "2026-09-20", "18:15", "Iti Sen", PHONE)
    call.cleanup()


def test_an_unverified_call_leaves_nothing_for_another_channel(voice, store, monkeypatch):
    """A number merely SAID on a call is no proof of holding that handset."""
    monkeypatch.setattr(voice.app, "_conversations", store)
    call = voice.app.CallSession(gate_support.FakeWS())
    call.history_phone = PHONE
    call.pending = {"awaiting": "time_slot", "slots": {"doctor_name": "সেন"}, "retries": 0}
    voice.app._save_for_other_channels(call)
    call.cleanup()
    assert store.load(PHONE, whatsapp.CHANNEL) is None


def test_nothing_of_verification_is_ever_stored(store, tmp_path):
    store.save(
        PHONE,
        lang="bn",
        pending={"awaiting": "history_verify", "factor": "dob", "purpose": "history", "slots": {}},
        channel=privacy.CHANNEL_VOICE,
    )
    assert store.load(PHONE, privacy.CHANNEL_VOICE).pending is None
    raw = (tmp_path / "state.db").read_bytes()
    assert b"history_verify" not in raw and b"dob" not in raw


def test_the_number_is_not_the_key(store, tmp_path):
    store.save(PHONE, lang="bn", pending=None, channel=whatsapp.CHANNEL)
    with sqlite3.connect(str(tmp_path / "state.db")) as conn:
        keys = [row[0] for row in conn.execute("SELECT number_key FROM conversations")]
    assert keys == [store.key_for(PHONE)] and PHONE not in keys[0]
    # Every way the number is written finds the same row...
    assert store.key_for("+91 90000 00101") == store.key_for(SENDER) == store.key_for(PHONE)
    # ...and a store without the key cannot find it at all.
    other = conversation_store.ConversationStore(path=str(tmp_path / "state.db"), key=b"another-key")
    assert other.load(PHONE, whatsapp.CHANNEL) is None
    other.close()


def test_state_expires(tmp_path):
    s = conversation_store.ConversationStore(path=str(tmp_path / "state.db"), key=b"k", ttl_s=-1.0)
    s.save(PHONE, lang="hi", pending={"awaiting": "date", "slots": {"doctor_name": "সেন"}}, channel="voice")
    snap = s.load(PHONE, whatsapp.CHANNEL)
    assert snap.pending is None and snap.lang is None
    s.close()


def test_a_list_of_doctors_read_out_on_one_channel_does_not_cross(store):
    pending = {
        "awaiting": "doctor_choice",
        "slots": {},
        "candidates": [{"name": "Dr. A. Sen", "name_bn": "সেন"}],
        "offered_date": "2026-09-20",
        "retries": 1,
    }
    store.save(PHONE, lang="bn", pending=pending, channel=whatsapp.CHANNEL)
    assert store.load(PHONE, privacy.CHANNEL_VOICE).pending is None
    assert store.load(PHONE, whatsapp.CHANNEL).pending["candidates"] == pending["candidates"]


def test_without_a_shared_key_state_does_not_follow_the_patient(tmp_path, monkeypatch):
    monkeypatch.delenv("VOICE_AGENT_STATE_KEY", raising=False)
    a = conversation_store.ConversationStore(path=str(tmp_path / "state.db"))
    b = conversation_store.ConversationStore(path=str(tmp_path / "state.db"))
    a.save(PHONE, lang="bn", pending={"awaiting": "date", "slots": {}}, channel="voice")
    assert not a.shared and b.load(PHONE, whatsapp.CHANNEL) is None
    assert a.health()["shared_between_channels"] is False
    a.close()
    b.close()


# ===========================================================================
# THE OFFICIAL PROVIDER -- Meta's webhook, and template compliance
# ===========================================================================
def _payload(*messages, statuses=()):
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": "1234"},
                            "messages": list(messages),
                            "statuses": list(statuses),
                        },
                    }
                ],
            }
        ],
    }


def _text(mid, body):
    return {
        "from": SENDER,
        "id": mid,
        "timestamp": str(int(time.time())),
        "type": "text",
        "text": {"body": body},
    }


def _signed(body: bytes, secret: str = APP_SECRET) -> dict:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": f"sha256={digest}", "Content-Type": "application/json"}


@pytest.fixture
def web(voice, store, monkeypatch):
    channel, _sent = _channel(voice, store)
    scheduled: list[str] = []

    async def _nothing():
        return None

    monkeypatch.setattr(channel, "answer", lambda msg: (scheduled.append(msg.message_id), _nothing())[1])
    monkeypatch.setattr(message_service, "_channel", channel)
    return types.SimpleNamespace(client=TestClient(message_service.app), scheduled=scheduled)


def test_an_unsigned_or_wrongly_signed_webhook_is_refused(web):
    body = json.dumps(_payload(_text("wamid.A", "হ্যালো"))).encode()
    assert web.client.post("/webhook/whatsapp", content=body).status_code == 403
    assert (
        web.client.post("/webhook/whatsapp", content=body, headers=_signed(body, "wrong")).status_code == 403
    )
    assert web.scheduled == []


def test_a_signed_webhook_is_answered_once_however_often_it_arrives(web):
    body = json.dumps(_payload(_text("wamid.A", "হ্যালো"))).encode()
    first = web.client.post("/webhook/whatsapp", content=body, headers=_signed(body))
    again = web.client.post("/webhook/whatsapp", content=body, headers=_signed(body))
    assert first.json() == {"received": 1} and again.json() == {"received": 0}
    assert web.scheduled == ["wamid.A"]


def test_the_registration_handshake(web):
    ok = web.client.get(
        "/webhook/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": VERIFY, "hub.challenge": "4711"},
    )
    assert ok.status_code == 200 and ok.text == "4711"
    bad = web.client.get(
        "/webhook/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": "guess", "hub.challenge": "4711"},
    )
    assert bad.status_code == 403


def test_the_reply_is_a_plain_free_form_message_with_no_preview(voice, store):
    voice.intents["CBC"] = _intent("test_rate", test_name="CBC")
    channel, sent = _channel(voice, store)
    session = _write(channel, "CBC")
    assert sent == [
        {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": SENDER,
            "type": "text",
            "text": {"preview_url": False, "body": session.replies[0]},
        }
    ]


def test_outside_the_24_hour_window_only_the_approved_template_is_sent(voice, store):
    question = "সিবিসি টেস্টের রেট কত"
    voice.intents[question] = _intent("test_rate", test_name="CBC")
    channel, sent = _channel(voice, store, template="kcd_reply_expired")
    session = _write(channel, question, sent_at=time.time() - 25 * 3600)  # redelivered late
    assert session.replies == []  # the answer itself was never sent
    assert sent == [
        {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": SENDER,
            "type": "template",
            "template": {"name": "kcd_reply_expired", "language": {"code": "bn"}},
        }
    ]


def test_outside_the_window_with_no_approved_template_nothing_is_sent(voice, store):
    voice.intents["CBC"] = _intent("test_rate", test_name="CBC")
    channel, sent = _channel(voice, store)
    assert _write(channel, "CBC", sent_at=time.time() - 25 * 3600).replies == []
    assert sent == []


def test_a_patient_who_wrote_again_recently_reopened_the_window(voice, store):
    voice.intents["CBC"] = _intent("test_rate", test_name="CBC")
    store.note_inbound(SENDER, time.time() - 60, whatsapp.CHANNEL)
    channel, sent = _channel(voice, store)
    session = _write(channel, "CBC", sent_at=time.time() - 25 * 3600)
    assert len(session.replies) == 1 and sent[0]["type"] == "text"


def test_a_voice_note_is_asked_to_be_typed(voice, store):
    channel, _sent = _channel(voice, store)
    session = _write(channel, "", kind="audio")
    assert session.replies == [i18n.t("bn", "channel.text_only")]
    assert voice.tools.called() == []


def test_a_reply_the_provider_refuses_is_recorded_as_undelivered(voice, store):
    voice.intents["CBC"] = _intent("test_rate", test_name="CBC")
    channel, _sent = _channel(voice, store, status=400)
    assert _write(channel, "CBC").replies == []


def test_the_client_never_raises_on_a_network_failure():
    def handler(request):
        raise httpx.ConnectError("down", request=request)

    config = whatsapp.Config("t", "1234", APP_SECRET, VERIFY, "https://graph.test", "v21.0", "91", 2.0, "")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await whatsapp.send_text(client, config, SENDER, "x")

    result = asyncio.run(go())
    assert not result.accepted and result.error_code == "transport:ConnectError"


def test_the_webhook_parser_is_tolerant():
    assert whatsapp.parse_webhook({"object": "page"}) == ([], [])
    assert whatsapp.parse_webhook("nonsense") == ([], [])
    inbound, statuses = whatsapp.parse_webhook(
        _payload(
            _text("wamid.A", "CBC"),
            {"from": SENDER, "id": "wamid.B", "timestamp": "1", "type": "image"},
            statuses=[{"id": "wamid.out", "status": "failed", "errors": [{"code": 131047}]}],
        )
    )
    assert [(m.message_id, m.kind, m.text) for m in inbound] == [
        ("wamid.A", "text", "CBC"),
        ("wamid.B", "image", ""),
    ]
    assert statuses == [whatsapp.DeliveryStatus("wamid.out", "failed", "131047")]


def test_the_number_the_clinic_keeps():
    assert whatsapp.local_number(SENDER, "91") == PHONE
    assert whatsapp.local_number(PHONE, "91") == PHONE
    assert whatsapp.local_number("14155550100", "91") is None  # another country


def test_the_simulator_is_for_a_bench_pod_only(voice, store, monkeypatch):
    monkeypatch.setenv("MESSAGE_SERVICE_SIMULATOR", "1")
    live, _sent = _channel(voice, store)
    assert not message_service.simulator_enabled(live.config)  # never on a live number

    voice.intents["CBC-র দাম কত"] = _intent("test_rate", test_name="CBC")
    bench, sent = _channel(voice, store, configured=False, simulator=True)
    monkeypatch.setattr(message_service, "_channel", bench)
    reply = TestClient(message_service.app).post(
        "/api/messages/simulate", json={"sender": SENDER, "text": "CBC-র দাম কত"}
    )
    assert reply.status_code == 200
    assert reply.json()["replies"] == [rate_reply(voice.intents["CBC-র দাম কত"]["slots"], _RATE, "bn")]
    assert sent == []  # nothing left the building

    monkeypatch.setattr(message_service, "_channel", live)
    assert (
        TestClient(message_service.app)
        .post("/api/messages/simulate", json={"sender": SENDER, "text": "x"})
        .status_code
        == 404
    )


# ===========================================================================
# THE AUDIT -- every message leaves its own record
# ===========================================================================
def test_each_message_leaves_a_complete_record(voice, store, monkeypatch, tmp_path):
    audit_store = call_audit.AuditStore(str(tmp_path / "audit.db"))
    monkeypatch.setattr(voice.app, "_audit_store", audit_store)
    voice.intents["CBC-র দাম কত"] = _intent("test_rate", test_name="CBC")
    channel, _sent = _channel(voice, store)

    session = _write(channel, "CBC-র দাম কত")
    audit_store.flush()
    record = audit_store.get_call(session.call_id)
    audit_store.close()

    assert record["call"]["transport"] == whatsapp.CHANNEL
    assert record["call"]["final_status"] == call_audit.STATUS_COMPLETED
    assert record["integrity"]["complete"]
    by_type = {e["event_type"]: e["data"] for e in record["events"]}
    assert by_type[call_audit.TRANSCRIPT]["source"] == "message"
    assert by_type[call_audit.AGENT_RESPONSE]["audio"] == call_audit.AUDIO_TEXT
    assert by_type[call_audit.AGENT_RESPONSE]["delivered"] is True
