"""Whether it is safe to SAY something out loud on this particular call.

Author: Chakravardhan
Story:  History disclosed only after verification
        "As a patient, I want my history spoken only to me, so that whoever
         else uses this handset cannot hear what tests I have had."

WHY THIS IS A SEPARATE CONCERN FROM VERIFICATION
------------------------------------------------
clinic-api/verification.py answers "is this person entitled to the
history". This module answers a different question that the acceptance
criterion asks just as directly: "cannot HEAR".

Those come apart. A correctly verified patient standing in a shared room
with the phone on loudspeaker is entitled to their history and must still
not have it read out, because the criterion is not about entitlement -- it
is about who ends up hearing it. Verification cannot see the room. This
module can.

WHAT IT USES
------------
agent/echo_guard.py already classifies the audio path by Echo Return Loss:
a handset at an ear returns almost nothing of our own TTS to the
microphone, a speakerphone on a table returns a great deal. That
measurement exists for barge-in, and it happens to be the only signal in
this system that says anything about the room the caller is standing in.

THE ONE PLACE THIS DELIBERATELY DISAGREES WITH echo_guard
---------------------------------------------------------
EchoGuard.reporting_path() collapses PATH_UNKNOWN to PATH_HANDSET, and
says why: guessing speakerphone would contaminate the metrics bucket whose
accuracy is being measured.

That is right for metrics and WRONG here, in the most consequential
direction available. "We have not gathered enough echo observations to
tell" is not "this is a handset held to an ear". Treating unknown as safe
would disclose a medical history on every call that ends before the
classifier has enough data -- which is precisely the SHORT calls, and a
caller who dials, asks for their history and hangs up is exactly that.

So this module calls classify() directly and treats PATH_UNKNOWN as
NOT PROVEN SAFE. The cost of being wrong in this direction is one extra
sentence asking the caller to take the phone off speaker. The cost of
being wrong in the other direction is reading somebody's medical history
to a room.

WHAT THIS IS NOT
----------------
Not a guarantee. ERL cannot detect a second person leaning in to listen at
an earpiece, a call on a car system that returns little echo, or a
recorded call. It raises the floor; it does not seal the room. The
counter remains the path for anything that genuinely must be private --
which is also the non-smartphone path, and that is not a coincidence.
"""
from __future__ import annotations

import os

from agent.echo_guard import PATH_HANDSET, PATH_SPEAKERPHONE, PATH_UNKNOWN

# Why a caller was refused. Returned to the dispatcher so it can pick the
# right sentence, and written to the audit trail.
SAFE = "safe"
UNSAFE_SPEAKERPHONE = "speakerphone"
UNSAFE_UNKNOWN = "path_unknown"
UNSAFE_DISABLED = "disclosure_disabled"


def _flag(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def disclosure_enabled() -> bool:
    """Master switch, defaulting to ON.

    Exists so a deployment can turn spoken history off entirely -- a clinic
    that decides no medical history should ever go down a phone line is
    making a defensible call, and should not have to delete code to make it.
    """
    return _flag("VOICE_AGENT_HISTORY_DISCLOSURE", "1")


def require_private_audio_path() -> bool:
    """Whether an unproven audio path blocks disclosure. Default ON.

    Turning this off is a deliberate weakening and is named accordingly. It
    exists because on a bench pod -- browser microphone, laptop speakers --
    EVERY call classifies as speakerphone, and a developer testing the
    verification flow would otherwise never get past this gate.

    It must not be off in production, and the audit row records which mode
    a disclosure happened under so that is visible after the fact rather
    than only in someone's memory of how the pod was configured.
    """
    return _flag("VOICE_AGENT_HISTORY_REQUIRE_PRIVATE_PATH", "1")


def audio_path_is_private(echo_guard) -> tuple[bool, str]:
    """-> (safe to speak private information, reason).

    `echo_guard` is the session's EchoGuard. Passed in rather than imported
    from a global so this stays a pure function of one call's state and can
    be tested without a session.

    NOTE classify(), not reporting_path() -- see the module docstring for
    why the difference matters more here than anywhere else in the codebase.
    """
    if not disclosure_enabled():
        return False, UNSAFE_DISABLED

    if not require_private_audio_path():
        # Explicitly weakened. Reported as safe, but the reason string is
        # carried into the audit row by the caller so the mode is on record.
        return True, SAFE

    path = getattr(echo_guard, "classify", lambda: PATH_UNKNOWN)()

    if path == PATH_SPEAKERPHONE:
        return False, UNSAFE_SPEAKERPHONE
    if path == PATH_HANDSET:
        return True, SAFE
    # PATH_UNKNOWN, or anything unrecognised. Not proven safe.
    return False, UNSAFE_UNKNOWN


# ---------------------------------------------------------------------------
# A WRITTEN CHANNEL IS NEVER PRIVATE -- Author: Chakravardhan
# Story: "As a patient, I want to ask the same questions by message and get
#         the same answers, so that I can use the channel I already have open."
# ---------------------------------------------------------------------------
# The conversation's channel. Only the phone line has an audio path to judge.
CHANNEL_VOICE = "voice"
UNSAFE_TEXT_CHANNEL = "text_channel"


def channel_is_private(channel: str, echo_guard) -> tuple[bool, str]:
    """-> (safe to disclose private information on this channel, reason).

    A message is not an audio path, so audio_path_is_private() cannot judge
    it -- and it fails a stricter test than any room. A spoken answer is
    gone once said; a written one stays on the handset's screen for whoever
    picks it up next, however long after. So on anything but the phone line
    the answer is no, before verification is even attempted, and whatever
    VOICE_AGENT_HISTORY_REQUIRE_PRIVATE_PATH says: that switch weakens the
    ROOM check for a bench pod, not this one.
    """
    if channel != CHANNEL_VOICE:
        return False, UNSAFE_TEXT_CHANNEL
    return audio_path_is_private(echo_guard)


def recoverable(reason: str) -> bool:
    """-> whether the caller can DO something about this refusal.

    A speakerphone or an unclassified path is recoverable: pick the handset
    up, keep talking, and the next classification may pass. Disclosure being
    switched off clinic-wide is not, and the caller must be sent to the
    counter rather than asked to try something that cannot work.
    """
    return reason in (UNSAFE_SPEAKERPHONE, UNSAFE_UNKNOWN)
