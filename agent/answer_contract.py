"""What every channel promises to answer identically -- and the short, named
list of places it may not.

Author: Chakravardhan
Story:  "As a patient, I want the phone and the message channel to agree, so
         that I know which one to believe."
Criteria: a shared answer service backs every channel, and a regression suite
          asks the same question set on each, asserting equivalent answers. A
          divergence fails the build.

WHY A DECLARATION AND NOT JUST A TEST
-------------------------------------
The phone line and the message channel already run the same turn
(main.py answer_turn -> _run_turn): one fast path, one intent cache, one
model, one clinic client, one table of sentences. Nothing has to be kept
in step by hand, and tests/test_channel_parity.py asks both channels the
same questions to prove it.

But a shared pipeline can still be forked one `if` at a time. Each of those
`if`s is defensible on its own -- and three of them later, a patient is told
one price on the phone and another by message, which is the thing this story
exists to prevent.

So every place an answer is allowed to differ by channel is written down
HERE, with its reason. The parity suite reads this list: a divergence that
is on it is checked to behave as described, and a divergence that is NOT on
it fails the build. Adding a branch is therefore a deliberate act with a
reviewer's name on it, not a quiet drift.

WHAT "EQUIVALENT" MEANS
-----------------------
The same words. Not merely the same facts: a price quoted as "650 টাকা" on
the phone and "Rs 650" by message would pass a facts-only test and still
leave a patient wondering which channel to believe. Both channels take
their wording from agent/reply_templates.py and agent/i18n.py, so equality
is the honest assertion.
"""

from __future__ import annotations

import dataclasses

# The one function every channel enters to have a turn answered. Voice
# reaches it with an utterance, a keypad press and a message reach it with
# text; from there the path is identical. Named here so the parity suite can
# assert that it is still the only door.
SHARED_ANSWER_ENTRY = "answer_turn"


@dataclasses.dataclass(frozen=True)
class Divergence:
    """One place an answer may legitimately differ by channel.

    `marker` is what the parity suite looks for in main.py -- the code that
    implements this divergence and nothing else.
    """

    id: str
    where: str
    marker: str
    why: str


# ---------------------------------------------------------------------------
# THE COMPLETE LIST. Nothing else may branch on the channel.
# ---------------------------------------------------------------------------
DIVERGENCES: tuple[Divergence, ...] = (
    Divergence(
        id="delivery",
        where="main._speak",
        marker="await _deliver_written(session, text_bn, fallback_reason, audit_redact)",
        why="The same sentence leaves as audio on a call and as text in a message. "
        "The words are identical; only the carrier differs.",
    ),
    Divergence(
        id="private_information",
        where="main._history_guard -> privacy.channel_is_private",
        marker="safe, reason = privacy.channel_is_private(",
        why="A written answer stays on a shared handset's screen for whoever picks it "
        "up next, so history and bookings are refused by message and the patient is "
        "sent to the phone line or the counter. Required by CLAUDE.md.",
    ),
    Divergence(
        id="number_already_known",
        where="main._fill_from_channel",
        marker='if getattr(session, "channel", privacy.CHANNEL_VOICE) == privacy.CHANNEL_VOICE:',
        why="A patient writing from WhatsApp is writing FROM their number, which the "
        "provider asserts, so the booking does not ask for it. The phone line has no "
        "caller-ID and must still ask. One question fewer, the same booking.",
    ),
)

DIVERGENCE_IDS: frozenset[str] = frozenset(d.id for d in DIVERGENCES)

# Sentences that belong to a CHANNEL rather than to an answer: the privacy
# refusal, the note that a booking is being picked up from the other channel,
# and the reply to something that is not typed text. Everything else in
# agent/i18n.py must read the same whichever channel asks for it.
CHANNEL_SENTENCE_KEYS: frozenset[str] = frozenset(
    {"channel.private_by_message", "channel.resumed", "channel.text_only"}
)

# How many places in main.py may ask which channel this is. One per declared
# divergence and not one more -- the parity suite counts them, so a new
# `if the channel is ...` fails the build until it is declared above.
CHANNEL_TESTS_IN_MAIN = len(DIVERGENCES)


def describe() -> str:
    """The list as a reviewer would want to read it."""
    lines = [f"shared answer entry: {SHARED_ANSWER_ENTRY}()", "declared divergences:"]
    for d in DIVERGENCES:
        lines.append(f"  - {d.id} ({d.where}): {d.why}")
    lines.append("channel sentences: " + ", ".join(sorted(CHANNEL_SENTENCE_KEYS)))
    return "\n".join(lines)
