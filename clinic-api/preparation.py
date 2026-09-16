"""What a patient must do before their tests -- one merged instruction.

Author: Chakravardhan
Story:  "As a patient with a fasting test tomorrow, I want a reminder tonight,
         so that my visit is not wasted."
Criterion: reminders carry the MERGED preparation instruction.

WHY "MERGED"
------------
A patient is rarely in for one test. A lipid profile (fast 12 hours, no
alcohol) and a fasting sugar (fast 10 hours) on the same morning are ONE
preparation, not two messages with two different fasting times -- a patient
told "10 hours" and "12 hours" in the same evening follows the shorter one and
the lipid profile is wasted. So the merge takes:

  * the LONGEST fasting time of any test, turned into a clock time the patient
    can act on ("20:00 থেকে শুধু জল"), counted back from the appointment;
  * every other instruction once, in a fixed order, however many tests need it.

It returns instruction CODES and the fasting time; the words themselves live in
message_templates.PREP_PHRASES with every other text a patient is sent.

CLINICAL REVIEW REQUIRED
------------------------
The table below is the clinic's preparation policy written down, not a medical
fact this codebase knows. Fasting hours and instructions differ between labs;
they must be confirmed by the lab before this is switched on for patients.
A test that is not listed needs no preparation.
"""

from __future__ import annotations

import dataclasses
import datetime

import message_templates as mt


@dataclasses.dataclass(frozen=True)
class TestPreparation:
    fasting_hours: int = 0
    instructions: tuple[str, ...] = ()  # message_templates.PREP_* codes, never words


# Keyed by LabTest.name exactly as seed.py spells it.
PREPARATION: dict[str, TestPreparation] = {
    "Blood Sugar Fasting": TestPreparation(fasting_hours=10),
    "Lipid Profile": TestPreparation(fasting_hours=12, instructions=(mt.PREP_NO_ALCOHOL,)),
    "Liver Function Test (LFT)": TestPreparation(fasting_hours=8),
    "USG Whole Abdomen": TestPreparation(fasting_hours=6),
    "USG Pregnancy Profile": TestPreparation(instructions=(mt.PREP_FULL_BLADDER,)),
    "Urine Routine Examination": TestPreparation(instructions=(mt.PREP_FIRST_URINE,)),
}

# The order instructions are given in, whatever order the tests were ordered.
_INSTRUCTION_ORDER = (mt.PREP_NO_ALCOHOL, mt.PREP_FULL_BLADDER, mt.PREP_FIRST_URINE)


@dataclasses.dataclass(frozen=True)
class MergedPreparation:
    """The one preparation for everything on an appointment."""

    fasting_hours: int
    fast_from: datetime.datetime | None  # when the fast begins; None if no fasting
    instructions: tuple[str, ...]  # PREP_* codes, deduplicated and ordered
    unknown_tests: tuple[str, ...]  # tests with no preparation entry

    @property
    def needs_fasting(self) -> bool:
        return self.fasting_hours > 0

    def phrases(self) -> list[str]:
        """Every instruction in its registered wording, fasting first."""
        out = []
        if self.fast_from is not None:
            out.append(mt.prep_phrase(mt.PREP_FAST_FROM, time=self.fast_from.strftime("%H:%M")))
        out.extend(mt.prep_phrase(code) for code in self.instructions)
        return out

    def test_variables(self) -> tuple[str, str]:
        """-> (preparation, preparation_more) for the reminder_test template.

        Two DLT variables, each at most VAR_MAX_CHARS. Every phrase fits one on
        its own, so two instructions go out whole. With more than two, the first
        is sent and the second variable tells the patient the rest is at the
        counter -- never a truncated instruction, which is worse than none."""
        phrases = self.phrases() or [mt.prep_phrase(mt.PREP_NONE)]
        if len(phrases) == 1:
            return phrases[0], mt.prep_phrase(mt.PREP_THANKS)
        if len(phrases) == 2:
            return phrases[0], phrases[1]
        return phrases[0], mt.prep_phrase(mt.PREP_MORE_AT_COUNTER)


def merge(test_names: list[str], appointment_at: datetime.datetime) -> MergedPreparation:
    """One preparation for all of `test_names`, for an appointment at `appointment_at`."""
    fasting = 0
    codes: set[str] = set()
    unknown = []
    for name in test_names:
        prep = PREPARATION.get(name)
        if prep is None:
            unknown.append(name)
            continue
        fasting = max(fasting, prep.fasting_hours)
        codes.update(prep.instructions)
    ordered = tuple(code for code in _INSTRUCTION_ORDER if code in codes)
    fast_from = appointment_at - datetime.timedelta(hours=fasting) if fasting else None
    return MergedPreparation(
        fasting_hours=fasting, fast_from=fast_from, instructions=ordered, unknown_tests=tuple(unknown)
    )
