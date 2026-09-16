"""Decide whether a pull request may carry `ready-for-human-review`.

Author: Chakravardhan

Run by .github/workflows/label-guard.yml from a TRUSTED checkout of the
default branch. The label may be applied only when ALL of these hold:

  1. G1 concluded `success` for exactly this head commit, and its report
     says every mandatory check passed (final_status PASS);
  2. gate_config_touched == false   -- recomputed here, not read from G1;
  3. tests_weakened == false        -- recomputed here, not read from G1;
  4. no gate bypass detected        -- recomputed here, not read from G1;
  5. the G2 AI review completed for exactly this head commit with zero
     unresolved Critical/High findings.

Conditions 2-4 are recomputed by gate_protect.py against the pull request's
files because a pull request can edit its own G1 workflow; a G1 report is
only trustworthy once we know the PR did not touch the gate that wrote it.

    python scripts/gate_label.py --head-sha SHA --g1-conclusion success \
        --g1-report g1/gate-report.json --ai-review g2/ai-review.json \
        --protect protect.json

Exit 0 when eligible, 1 when not. The decision is printed as JSON.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _load(path: str | None):
    if not path:
        return None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def decide(
    head_sha: str, g1_conclusion: str | None, g1: dict | None, ai: dict | None, protect: dict | None
) -> dict:
    reasons = []
    if g1_conclusion != "success":
        reasons.append(f"G1 conclusion is {g1_conclusion or 'missing'}, not success")
    if not g1:
        reasons.append("G1 gate-report.json missing")
    else:
        if g1.get("commit_sha") != head_sha:
            reasons.append(f"G1 report is for {g1.get('commit_sha')}, not head {head_sha}")
        if g1.get("mode") != "full":
            reasons.append("G1 report is not a full-gate report")
        if g1.get("final_status") != "PASS":
            reasons.append(f"G1 final_status is {g1.get('final_status')}")
        failing = [
            c["id"]
            for c in g1.get("checks", [])
            if c.get("mandatory") and c.get("in_mode") and c.get("status") != "pass"
        ]
        if failing:
            reasons.append(f"mandatory G1 checks not passing: {', '.join(failing)}")
    if not protect:
        reasons.append("trusted gate-protection analysis missing")
    else:
        if protect.get("gate_config_touched"):
            reasons.append("gate_config_touched: " + ", ".join(protect.get("gate_config_files", [])[:10]))
        if protect.get("tests_weakened"):
            reasons.append("tests_weakened: " + "; ".join(protect.get("tests_weakened_reasons", [])[:5]))
        if protect.get("bypass_detected"):
            reasons.append("bypass_detected: " + "; ".join(protect.get("bypass_reasons", [])[:5]))
    if not ai:
        reasons.append("G2 AI review missing")
    else:
        if ai.get("status") != "completed":
            reasons.append(f"G2 AI review status is {ai.get('status')}")
        if ai.get("reviewed_head_sha") != head_sha:
            reasons.append(f"G2 reviewed {ai.get('reviewed_head_sha')}, not head {head_sha}")
        blocking = [f for f in ai.get("findings", []) if f.get("severity") in ("Critical", "High")]
        if blocking:
            reasons.append(f"{len(blocking)} unresolved Critical/High AI finding(s)")
    return {
        "head_sha": head_sha,
        "eligible": not reasons,
        "reasons": reasons,
        "label": "ready-for-human-review",
        "note": "G2 is an engineering review, not production approval; senior review is still required.",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--head-sha", required=True)
    ap.add_argument("--g1-conclusion", default=None)
    ap.add_argument("--g1-report", default=None)
    ap.add_argument("--ai-review", default=None)
    ap.add_argument("--protect", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    decision = decide(
        args.head_sha, args.g1_conclusion, _load(args.g1_report), _load(args.ai_review), _load(args.protect)
    )
    text = json.dumps(decision, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    sys.stdout.write(text + "\n")
    return 0 if decision["eligible"] else 1


if __name__ == "__main__":
    sys.exit(main())
