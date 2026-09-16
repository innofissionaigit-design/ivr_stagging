"""Multilingual routing smoke tests for the pre-human-review gate.

Author: Chakravardhan

The line serves Bengali, Hindi and English. These tests pin how a caller is
routed between them: an explicit request switches the call, a request for a
language the pod cannot hear is answered honestly rather than ignored, and a
single foreign word never flips the language of a whole call.

    python -m pytest tests/test_gate_multilingual_routing.py -v
"""

from __future__ import annotations

import asyncio
import collections

import gate_support
import pytest

from agent import i18n
from agent import language as lang_mod
from agent.reply_templates import language_switch_reply


@pytest.fixture
def tri(monkeypatch):
    gate_support.trilingual(monkeypatch)


@pytest.mark.parametrize(
    "text,code",
    [
        ("can you speak english", "en"),
        ("please speak in hindi", "hi"),
        ("বাংলায় বলুন", "bn"),
        ("ইংরেজিতে বলবেন?", "en"),
        ("हिंदी में बात कीजिए", "hi"),
        ("अंग्रेजी में बोलिए", "en"),
    ],
)
def test_an_explicit_request_routes_to_that_language(text, code, tri):
    assert lang_mod.requested_switch(text) == code


def test_a_language_the_pod_cannot_hear_is_reported_not_ignored(monkeypatch):
    monkeypatch.delenv("VOICE_AGENT_LANGUAGES", raising=False)
    monkeypatch.delenv("VOICE_AGENT_NEMO_FILE_EN", raising=False)
    assert lang_mod.requested_switch("can you speak english") is None
    assert lang_mod.requested_switch_unavailable("can you speak english") == "en"


def test_one_english_word_does_not_flip_a_bengali_call(tri):
    assert lang_mod.detect_from_text("রিপোর্ট ready তো?", fallback="bn") == "bn"


def test_an_unsupported_code_resolves_to_the_default():
    assert lang_mod.resolve("fr") == lang_mod.default_lang()
    assert lang_mod.resolve(None) == lang_mod.default_lang()


def test_digits_from_every_script_fold_to_ascii():
    assert lang_mod.to_ascii_digits("০১২৩৪ ५६७८९") == "01234 56789"


def test_every_caller_facing_string_exists_in_all_three_languages():
    by_key = collections.defaultdict(dict)
    for key, code, text in i18n.all_strings():
        by_key[key][code] = text
    assert by_key, "no strings found"
    for key, langs in by_key.items():
        assert set(langs) == set(lang_mod.ALL_LANGS), key
        assert all(text.strip() for text in langs.values()), key


def test_the_call_continues_in_the_language_the_caller_asked_for(monkeypatch, tri):
    app = gate_support.load_main_pcm()
    speech = gate_support.SpeechLog()
    monkeypatch.setattr(app, "_speak", speech)
    session = app.CallSession(gate_support.FakeWS())
    try:
        asyncio.run(app._run_turn(session, "", text_override="can you speak english"))
    finally:
        session.cleanup()
    assert session.lang == "en"
    assert speech.texts == [language_switch_reply("en")]
