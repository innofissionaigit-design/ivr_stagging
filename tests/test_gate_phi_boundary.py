"""PHI-boundary tests for the pre-human-review gate.

Author: Chakravardhan

Where patient data is allowed to go is decided by the design: to clinic-api,
and to the caller once verified. These tests pin the places it must NOT go --
error messages (which get logged), audit tables, and anything that outlives
the call.

    python -m pytest tests/test_gate_phi_boundary.py -v
"""

from __future__ import annotations

import asyncio
import importlib
import re
import sys

import gate_support
import httpx
import pytest

from agent import call_audit
from agent.tools_client import ClinicToolsClient, ToolCallError

PHONE = "9000000001"


def _unreachable_client() -> ClinicToolsClient:
    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    client = ClinicToolsClient("http://clinic.test")
    client._client = httpx.AsyncClient(base_url="http://clinic.test", transport=httpx.MockTransport(refuse))
    return client


def _error_text(coro_factory) -> str:
    async def run():
        client = _unreachable_client()
        try:
            with pytest.raises(ToolCallError) as info:
                await coro_factory(client)
            return str(info.value)
        finally:
            await client.aclose()

    return asyncio.run(run())


def test_a_verification_answer_never_appears_in_an_error_message():
    """ToolCallError messages are logged by main.py. A PIN or date of birth
    in one would leak the secret through the log."""
    text = _error_text(lambda c: c.verify_caller(PHONE, "dob", "1990-05-14", "call-1"))
    assert "1990-05-14" not in text
    assert PHONE not in text


def test_a_history_token_never_appears_in_an_error_message():
    secret = "tok-" + "Q7" * 12
    text = _error_text(lambda c: c.read_history(secret, "call-1"))
    assert secret not in text


def test_recording_a_refusal_never_raises_into_the_call():
    async def run():
        client = _unreachable_client()
        try:
            return await client.record_disclosure_refusal(PHONE, "speakerphone", "call-1")
        finally:
            await client.aclose()

    assert asyncio.run(run()) is None


def test_the_history_token_dies_with_the_call():
    app = gate_support.load_main_pcm()
    session = app.CallSession(gate_support.FakeWS())
    session.history_token = "tok-verified"
    session.history_phone = PHONE
    session.cleanup()
    assert session.history_token is None
    assert session.history_phone is None


def _clinic_models():
    clinic = str(gate_support.ROOT / "clinic-api")
    if clinic not in sys.path:
        sys.path.insert(0, clinic)
    return importlib.import_module("models")


def test_the_disclosure_audit_has_no_column_that_could_hold_a_secret():
    columns = set(_clinic_models().DisclosureAudit.__table__.columns.keys())
    assert not columns & {"pin", "pin_hash", "answer", "date_of_birth", "token"}


def test_a_pin_is_only_ever_stored_as_a_hash():
    columns = set(_clinic_models().Patient.__table__.columns.keys())
    assert "pin_hash" in columns and "pin_salt" in columns
    assert "pin" not in columns


def test_the_call_audit_schema_has_no_column_that_could_hold_a_secret():
    names = set(re.findall(r"^\s+(\w+)\s+(?:TEXT|INTEGER|REAL)", call_audit._SCHEMA, re.M))
    assert names, "schema parse found no columns"
    assert not names & {"pin", "answer", "token", "date_of_birth", "history"}
