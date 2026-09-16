"""Claude Code hook semantics for the gate (G0).

Author: Chakravardhan

scripts/gate.sh calls this after a run when it was invoked as a hook:

  PostToolUse (after Edit|Write|MultiEdit, fast gate)
      exit 0 when green; otherwise exit 2 with the failures on stderr, which
      Claude Code feeds straight back to Claude.

  Stop (end of turn, full gate)
      FAIL: the turn is NOT allowed to end: exit 2, and Claude is told
      which checks failed and which remediation cycle this is.
      Consecutive failures are counted PER CHECK and PER CLAUDE SESSION in
      .gate/state.json (two sessions in one tree never spend each other's
      cycles). After
      max_remediation_cycles (3) failed remediation attempts on the same
      check -- i.e. on its 4th failure -- the loop stops: .gate/ESCALATION.md
      is written, the stop is allowed so Claude cannot loop forever, and the
      message says the gate is RED and a human must take over.
      BLOCKED (every check passed, but gate files changed): escalated to a
      human at once. No edit Claude is permitted to make can clear it, so
      there is nothing to remediate -- only a code owner's review.
      PASS: exit 0, and the counters reset.
      An escalation is never reported as success.

    python scripts/gate_hook.py --event Stop --report gate-report.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import gate_lib  # scripts/ is sys.path[0] when this file is run as a script

STATE = gate_lib.GATE_DIR / "state.json"
ESCALATION = gate_lib.GATE_DIR / "ESCALATION.md"


def _failing(report: dict) -> list[str]:
    ids = list(report.get("failing_checks", []))
    if report.get("final_status") == "BLOCKED":
        ids.append("gate-config-touched")
    return ids


def _describe(report: dict, ids: list[str]) -> str:
    by_id = {c["id"]: c for c in report.get("checks", [])}
    out = []
    for cid in ids:
        c = by_id.get(cid)
        if c is None:
            out.append(
                f"- {cid}: gate files changed ({', '.join(report.get('gate_config_files', [])[:8])}); "
                "a human must review gate changes -- do not edit them to get a green result"
            )
            continue
        out.append(f"- {cid}: {c['summary']}")
        for f in c.get("findings", [])[:5]:
            out.append("    " + json.dumps(f, ensure_ascii=False)[:300])
    return "\n".join(out)


def post_tool_use(report: dict) -> int:
    if report.get("final_status") == "PASS":
        return 0
    sys.stderr.write(
        f"QUALITY GATE (fast) is {report.get('final_status')} after this edit:\n"
        f"{_describe(report, _failing(report))}\n"
        "Fix the cause. Never weaken tests, add ignore/suppression directives or edit gate files "
        "to make a check pass. Full details: gate-report.md\n"
    )
    return 2


def stop(report: dict, max_cycles: int, session: str = "default") -> int:
    """Remediation cycles are counted per Claude session: two sessions open
    in one working tree must not spend each other's cycles."""
    store = _load_store()
    state = store["sessions"].setdefault(session, {"counts": {}, "history": []})
    failing = _failing(report)
    state["history"] = (
        state.get("history", [])
        + [
            {
                "at": gate_lib.now_iso(),
                "status": report.get("final_status"),
                "failing": failing,
                "commit": report.get("commit_sha"),
            }
        ]
    )[-50:]

    if report.get("final_status") == "PASS":
        state["counts"] = {}
        _save(store)
        _clear_escalation(session)
        return 0

    if report.get("final_status") == "BLOCKED":
        # Every check passed, but the change touches the gate's own files.
        # Nothing Claude may do fixes that -- the only permitted remedy is a
        # code owner's review -- so looping the remediation cycle here would
        # only push Claude towards reverting or tampering with gate files.
        # Hand it to a human at once; never report it as success.
        state["counts"] = {}
        _save(store)
        _escalate(
            report,
            session,
            "Every mandatory check passed, but the change modifies the gate's own configuration:\n\n"
            + "\n".join(f"- `{f}`" for f in report.get("gate_config_files", [])[:40])
            + "\n\nA code owner must review these files. Claude may not edit them to change this result.",
        )
        _system_message(
            "QUALITY GATE BLOCKED -- HUMAN REVIEW REQUIRED. All checks passed, but gate configuration was "
            "modified; only a code owner can clear this. This turn is NOT a green gate. "
            "See .gate/ESCALATION.md."
        )
        return 0

    counts = {cid: state.get("counts", {}).get(cid, 0) + 1 for cid in failing}
    state["counts"] = counts
    _save(store)

    exhausted = sorted(cid for cid, n in counts.items() if n > max_cycles)
    if exhausted:
        _escalate(
            report,
            session,
            f"These checks failed {max_cycles + 1} consecutive times; the {max_cycles} permitted "
            "remediation cycles are used up and Claude has stopped:\n\n"
            + "\n".join(f"- `{cid}` ({counts[cid]} consecutive failures)" for cid in exhausted)
            + "\n\nCurrent failures:\n\n"
            + _describe(report, failing),
        )
        _system_message(
            f"QUALITY GATE RED -- ESCALATED TO A HUMAN. {', '.join(exhausted)} failed on "
            f"{max_cycles + 1} consecutive attempts ({max_cycles} remediation cycles exhausted). "
            "Claude has stopped trying. This turn did NOT finish successfully. "
            "See .gate/ESCALATION.md and gate-report.md."
        )
        return 0

    worst = max(counts.values())
    sys.stderr.write(
        f"QUALITY GATE (full) is {report.get('final_status')} -- you may not finish this turn.\n"
        f"Remediation cycle {worst} of {max_cycles} (per failing check):\n"
        f"{_describe(report, failing)}\n\n"
        "Diagnose and fix the cause, then stop again to re-run the full gate. Do NOT weaken or delete "
        "tests, add lint/type ignore directives, or modify scripts/gate*, .github/workflows/, "
        ".claude/settings.json or CLAUDE.md to obtain a green result. After "
        f"{max_cycles} failed remediation cycles on the same check you must stop and escalate to a human.\n"
    )
    return 2


def _load_store() -> dict:
    """{"sessions": {session_id: {"counts": {...}, "history": [...]}}}. A
    file in any other shape (including the pre-session layout) starts over:
    counting from zero can only make the loop stop LATER than 3 cycles if the
    file was tampered with, and tampering is forbidden by CLAUDE.md."""
    store = gate_lib.load_json(STATE, {}) if STATE.exists() else {}
    sessions = store.get("sessions") if isinstance(store, dict) else None
    return {"sessions": sessions if isinstance(sessions, dict) else {}}


def _save(store: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(store, indent=2), encoding="utf-8")


def _session_marker(session: str) -> str:
    return f"<!-- gate-session: {session} -->"


def _clear_escalation(session: str) -> None:
    """A green gate withdraws this session's escalation -- never another
    session's, which is about that session's own loop."""
    try:
        text = ESCALATION.read_text(encoding="utf-8")
    except OSError:
        return
    if _session_marker(session) in text:
        ESCALATION.unlink(missing_ok=True)


def _escalate(report: dict, session: str, body: str) -> None:
    ESCALATION.parent.mkdir(parents=True, exist_ok=True)
    ESCALATION.write_text(
        "# Quality gate escalation -- human action required\n\n"
        f"{_session_marker(session)}\n"
        f"Generated {gate_lib.now_iso()} at commit `{report.get('commit_sha')}`, "
        f"final status **{report.get('final_status')}**, Claude session `{session}`.\n\n"
        + body
        + "\n\nNothing here is approved. See gate-report.md. Clear .gate/state.json "
        "only after a human has taken the issue on.\n",
        encoding="utf-8",
    )


def _system_message(text: str) -> None:
    """Claude Code reads a JSON object on stdout from a hook that exits 0."""
    sys.stdout.write(json.dumps({"systemMessage": text}) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--event", required=True)
    ap.add_argument("--report", default=str(gate_lib.ROOT / "gate-report.json"))
    ap.add_argument("--session", default="default", help="Claude Code session_id from the hook payload")
    args = ap.parse_args()
    try:
        report = gate_lib.load_json(Path(args.report))
    except (OSError, ValueError) as e:
        report = {
            "final_status": "FAIL",
            "failing_checks": ["gate-report"],
            "checks": [{"id": "gate-report", "summary": f"no readable gate report: {e}", "findings": []}],
        }
    max_cycles = int(gate_lib.config().get("max_remediation_cycles", 3))
    if args.event == "PostToolUse":
        return post_tool_use(report)
    if args.event in ("Stop", "SubagentStop"):
        return stop(report, max_cycles, args.session)
    return 0 if report.get("final_status") == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
