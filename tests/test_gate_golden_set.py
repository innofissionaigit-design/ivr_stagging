"""Clinical golden-set regression for the pre-human-review gate.

Author: Chakravardhan

tests/golden/golden_set.json pins what the deterministic layers of the
agent do: which utterances the fast path answers without the model (and
which it must abstain on), and the exact sentence a caller hears for each
backend answer in Bengali, Hindi and English. Any drift fails here and has
to be reviewed as a change in clinical behaviour. Regenerating the file is a
gate-configuration change (see scripts/gate_golden.py).

    python -m pytest tests/test_gate_golden_set.py -v
"""

from __future__ import annotations

import json
import re

import gate_support
import pytest

from agent import reply_templates
from agent.fast_path import Catalogue, FastPath

golden = gate_support.load_script("gate_golden")
STORED = json.loads(golden.GOLDEN_PATH.read_text(encoding="utf-8"))
DIGITS = re.compile(r"[0-9০-৯०-९]")


@pytest.fixture(scope="module")
def fast():
    return FastPath(Catalogue(golden.seed_catalogue()), today=golden.TODAY)


def _shape(hit):
    if hit is None:
        return None
    return {
        "intent": hit.intent,
        "slots": {k: v for k, v in hit.slots.items() if v is not None},
        "direct_reply_bn": hit.direct_reply_bn,
    }


def test_the_golden_set_covers_every_case_the_generator_defines():
    assert set(STORED["fast_path"]) == {c[0] for c in golden.FAST_PATH_CASES}
    assert set(STORED["replies"]) == {c[0] for c in golden.REPLY_CASES}
    assert len(STORED["fast_path"]) >= 20
    assert len(STORED["replies"]) >= 30


@pytest.mark.parametrize("case_id", sorted(STORED["fast_path"]))
def test_fast_path_matches_the_golden_set(case_id, fast):
    case = STORED["fast_path"][case_id]
    assert _shape(fast.resolve(case["utterance"])) == case["expected"]


@pytest.mark.parametrize("case_id", sorted(STORED["replies"]))
def test_reply_matches_the_golden_set(case_id, monkeypatch):
    gate_support.trilingual(monkeypatch)
    case = STORED["replies"][case_id]
    func = getattr(reply_templates, case["fn"])
    for lang, expected in case["expected"].items():
        assert func(*case["args"], **case["kwargs"], lang=lang) == expected, f"{case_id} [{lang}]"


def test_abstention_fast_path_hands_every_non_routine_turn_to_the_model():
    """Booking (PII), compound questions, dates it does not parse, unknown
    entities and negations must never be string-matched."""
    abstain = {cid: c for cid, c in STORED["fast_path"].items() if c["category"] == "abstention"}
    assert abstain
    for cid, case in abstain.items():
        assert case["expected"] is None, f"{cid} must abstain"


def test_abstention_replies_never_state_a_number_they_were_not_given():
    """When the backend has no answer, the caller is told so -- no price,
    no time, no date is ever invented to fill the gap."""
    abstain = {cid: c for cid, c in STORED["replies"].items() if c["category"] == "abstention"}
    assert abstain
    for cid, case in abstain.items():
        for lang, text in case["expected"].items():
            assert text.strip(), f"{cid} [{lang}] is empty"
            assert not DIGITS.search(text), f"{cid} [{lang}] states a number: {text}"


def test_every_handoff_reply_points_the_caller_to_the_counter():
    handoff = {cid: c for cid, c in STORED["replies"].items() if c["category"] == "handoff"}
    assert handoff
    for cid, case in handoff.items():
        for lang, text in case["expected"].items():
            assert gate_support.mentions_counter(text), f"{cid} [{lang}]: {text}"
