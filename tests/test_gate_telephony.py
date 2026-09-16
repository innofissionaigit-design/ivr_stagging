"""Telephony-behaviour tests for the pre-human-review gate.

Author: Chakravardhan

The line has no SIP/RTP adapter yet (a known blocker), but it already has
telephone behaviour a caller depends on: keypad (DTMF) input, a spoken busy
line at capacity, a heartbeat that outlives proxy idle cut-offs, and an echo
gate driven by the client's playback reports. These tests pin that
behaviour on the transport that exists.

    python -m pytest tests/test_gate_telephony.py -v
"""

from __future__ import annotations

import asyncio

import gate_support

from agent import language as lang_mod
from agent.tts import BUSY_LINE

app = gate_support.load_main_pcm()


def test_the_keypad_menu_covers_every_digit_the_prompt_offers():
    offered = {d for d in lang_mod.to_ascii_digits(app.KEYPAD_PROMPT_BN) if d.isdigit()}
    assert offered == set(app.KEYPAD_MENU_BN)


def test_a_dtmf_frame_starts_a_keypad_turn(monkeypatch):
    pressed = []

    async def keypad(session, digit):
        pressed.append(digit)

    monkeypatch.setattr(app, "_handle_keypad_digit", keypad)
    session = app.CallSession(gate_support.FakeWS())

    async def run():
        await app._handle_control(session, '{"type": "dtmf", "digit": "2"}')
        await asyncio.sleep(0)

    try:
        asyncio.run(run())
    finally:
        session.cleanup()
    assert pressed == ["2"]


def test_an_unparseable_control_frame_is_ignored():
    session = app.CallSession(gate_support.FakeWS())
    try:
        asyncio.run(app._handle_control(session, "not json at all"))
    finally:
        session.cleanup()


def test_playback_done_releases_the_echo_gate():
    session = app.CallSession(gate_support.FakeWS())
    try:
        assert session.agent_speaking
        asyncio.run(app._handle_control(session, '{"type": "playback_done"}'))
        assert not session.agent_speaking
        assert session.resync_pending
    finally:
        session.cleanup()


def test_the_heartbeat_outlives_the_observed_proxy_idle_cutoff():
    """A live call was seen closing ~26 s after the last exchange."""
    assert app.HEARTBEAT_INTERVAL_S < 26
    assert app.HEARTBEAT_INTERVAL_S < app.IDLE_TIMEOUT_S


def test_admission_control_turns_callers_away_in_words():
    assert BUSY_LINE.strip()
    assert app.MAX_CONCURRENT_CALLS >= 1
