# ADDED BY CHAKRAVARDHAN -- this script supersedes an earlier standalone
# draft by Sourav that predated gate_lib.py, gate_checks.py and the rest
# of this gate's supporting modules (see scripts/gate.sh's own note on
# the same history). Nothing outside scripts/gate.sh and this file
# references gate-report.py's internals, so Sourav's draft is dropped
# here rather than merged in piecemeal.
"""Turn the gate's check results into gate-report.json and gate-report.md.

Author: Chakravardhan

    python scripts/gate-report.py --mode fast|full [--ai-review ai-review.json]

Exit status: 0 PASS, 1 FAIL, 3 BLOCKED (every check passed, but the change
touches the gate's own configuration and needs an owner's review).

THE VERDICT IS COMPUTED, NEVER ASSERTED. Any mandatory check that failed,
errored or did not run -- in a mode it belongs to -- makes the gate FAIL.
There is no flag, environment variable or argument that turns a failure
into a pass.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import gate_lib  # scripts/ is sys.path[0] when this file is run as a script

PASS, FAIL, BLOCKED = "PASS", "FAIL", "BLOCKED"
SEVERITIES_BLOCKING = ("Critical", "High")


def _build_id(head: str) -> str:
    if os.environ.get("GATE_BUILD_ID"):
        return os.environ["GATE_BUILD_ID"]
    if os.environ.get("GITHUB_RUN_ID"):
        return f"gha-{os.environ['GITHUB_RUN_ID']}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}"
    return f"local-{gate_lib.now_iso().replace(':', '').replace('-', '')}-{head[:8]}"


def load_ai_review(path: str | None) -> dict:
    if not path:
        return {
            "status": "not_run",
            "reason": "G2 runs only in CI, after G1 passes",
            "findings": [],
            "unresolved_critical_high": None,
        }
    try:
        data = gate_lib.load_json(Path(path))
    except (OSError, ValueError) as e:
        return {
            "status": "failed",
            "reason": f"unreadable AI review: {e}",
            "findings": [],
            "unresolved_critical_high": None,
        }
    findings = data.get("findings", [])
    data["unresolved_critical_high"] = sum(1 for f in findings if f.get("severity") in SEVERITIES_BLOCKING)
    return data


def build(mode: str, ai_review_path: str | None) -> dict:
    cfg = gate_lib.config()
    ctx = gate_lib.load_json(gate_lib.GATE_DIR / "context.json", {})
    results = {r["id"]: r for r in gate_lib.read_results()}
    head = ctx.get("head") or gate_lib.head_sha()

    checks, failing = [], []
    for cid, spec in cfg["checks"].items():
        r = results.get(cid)
        in_mode = mode in spec.get("modes", [])
        if r is None:
            r = {
                "id": cid,
                "status": gate_lib.STATUS_ERROR if in_mode else gate_lib.STATUS_SKIP,
                "summary": "did not run" if in_mode else f"not part of the {mode} gate",
                "findings": [],
                "metrics": {},
                "duration_s": 0,
            }
        if in_mode and r["status"] == gate_lib.STATUS_SKIP:
            r = {**r, "status": gate_lib.STATUS_ERROR, "summary": "skipped in a mode it is mandatory for"}
        entry = {
            "id": cid,
            "name": spec["name"],
            "category": spec["category"],
            "mandatory": spec.get("mandatory", True),
            "in_mode": in_mode,
            "status": r["status"],
            "summary": r["summary"],
            "duration_s": r.get("duration_s", 0),
            "metrics": r.get("metrics", {}),
            "findings": r.get("findings", [])[:200],
        }
        checks.append(entry)
        if (
            entry["mandatory"]
            and in_mode
            and entry["status"] in (gate_lib.STATUS_FAIL, gate_lib.STATUS_ERROR)
        ):
            failing.append(cid)

    protect = (
        gate_lib.load_json(gate_lib.GATE_DIR / "protect.json", {})
        if (gate_lib.GATE_DIR / "protect.json").exists()
        else {}
    )
    pytest_summary = (
        gate_lib.load_json(gate_lib.GATE_DIR / "pytest-summary.json", {})
        if (gate_lib.GATE_DIR / "pytest-summary.json").exists()
        else {}
    )
    suites = {c["id"]: c["metrics"] for c in checks if c["id"] in cfg.get("suites", {}) and c["in_mode"]}

    changed = ctx.get("changed", [])
    unresolved = []
    for c in checks:
        if c["id"] in failing:
            for f in c["findings"] or [{"summary": c["summary"]}]:
                unresolved.append({"check": c["id"], **f})
    baseline = gate_lib.baseline()
    debt = {
        k: (sum(v.values()) if isinstance(v, dict) else len(v))
        for k, v in baseline.items()
        if isinstance(v, (dict, list))
    }

    ai = load_ai_review(ai_review_path)
    config_touched = bool(protect.get("gate_config_touched"))
    weakened = bool(protect.get("tests_weakened"))
    bypass = bool(protect.get("bypass_detected"))
    if not protect and "gate-protection" not in failing:
        failing.append("gate-protection")  # no protection analysis means nothing was proven

    if failing:
        final = FAIL
    elif config_touched:
        final = BLOCKED
    else:
        final = PASS

    label_blockers = []
    if mode != "full":
        label_blockers.append("only a full gate run can make a change eligible")
    if failing:
        label_blockers.append(f"mandatory checks not passing: {', '.join(sorted(set(failing)))}")
    if config_touched:
        label_blockers.append("gate configuration touched -- needs code-owner review, never auto-labelled")
    if weakened:
        label_blockers.append("tests weakened")
    if bypass:
        label_blockers.append("gate bypass detected")
    if ai.get("status") != "completed":
        label_blockers.append(f"AI review {ai.get('status')}: {ai.get('reason', '')}".strip())
    elif ai.get("unresolved_critical_high"):
        label_blockers.append(f"{ai['unresolved_critical_high']} unresolved Critical/High AI finding(s)")

    return {
        "schema_version": 1,
        "gate": "G1-ci" if gate_lib.is_ci() else "G0-local",
        "mode": mode,
        "generated_at": gate_lib.now_iso(),
        "commit_sha": head,
        "base_sha": ctx.get("base"),
        "build_id": _build_id(head),
        "final_status": final,
        "checks": checks,
        "failing_checks": sorted(set(failing)),
        "test_counts": {
            **{k: pytest_summary.get(k) for k in ("total", "passed", "failed", "error", "skipped")},
            "by_suite": suites,
        }
        if pytest_summary
        else {"total": None, "by_suite": suites, "note": f"tests do not run in the {mode} gate"},
        "changed_files": changed,
        "deleted_files": ctx.get("deleted", []),
        "high_risk_files": sorted(
            f for f in changed + ctx.get("deleted", []) if gate_lib.matches(f, cfg["high_risk_paths"])
        ),
        "gate_config_touched": config_touched,
        "gate_config_files": protect.get("gate_config_files", []),
        "tests_weakened": weakened,
        "tests_weakened_reasons": protect.get("tests_weakened_reasons", []),
        "bypass_detected": bypass,
        "bypass_reasons": protect.get("bypass_reasons", []),
        "unresolved_findings": {
            "blocking": unresolved[:500],
            "blocking_count": len(unresolved),
            "baselined_debt": debt,
        },
        "ai_review": ai,
        "label_eligible": not label_blockers,
        "label_blockers": label_blockers,
        "known_blockers": cfg.get("known_blockers", []),
    }


_ICON = {"pass": "PASS", "fail": "**FAIL**", "error": "**ERROR**", "skip": "skip"}


def render_md(r: dict) -> str:
    lines = [
        f"## Quality gate: {r['final_status']}",
        "",
        f"| | |\n|---|---|\n| Gate | {r['gate']} ({r['mode']}) |\n| Commit | `{r['commit_sha']}` |\n"
        f"| Base | `{r['base_sha']}` |\n| Build | `{r['build_id']}` |\n| Generated | {r['generated_at']} |\n"
        f"| Label `ready-for-human-review` | {'eligible' if r['label_eligible'] else 'NOT eligible'} |",
        "",
    ]
    if r["failing_checks"]:
        lines += [f"**Failing mandatory checks:** {', '.join(r['failing_checks'])}", ""]
    lines += ["### Checks", "", "| Check | Category | Status | Time | Summary |", "|---|---|---|---|---|"]
    for c in r["checks"]:
        lines.append(
            f"| {c['name']} (`{c['id']}`) | {c['category']} | {_ICON.get(c['status'], c['status'])} "
            f"| {c['duration_s']}s | {c['summary'].replace('|', '/')} |"
        )
    tc = r["test_counts"]
    lines += ["", "### Tests", ""]
    if tc.get("total") is not None:
        lines.append(
            f"{tc['total']} total: {tc['passed']} passed, {tc['failed']} failed, "
            f"{tc['error']} errors, {tc['skipped']} skipped."
        )
    else:
        lines.append(tc.get("note", "no test run"))
    if tc.get("by_suite"):
        lines += ["", "| Suite | Tests | Passed | Failed | Skipped | Minimum |", "|---|---|---|---|---|---|"]
        for sid, m in tc["by_suite"].items():
            lines.append(
                f"| {sid} | {m.get('tests', '-')} | {m.get('passed', '-')} | {m.get('failed', '-')} "
                f"| {m.get('skipped', '-')} | {m.get('min_tests', '-')} |"
            )
    lines += [
        "",
        "### Gate protection",
        "",
        f"- gate_config_touched: **{str(r['gate_config_touched']).lower()}**"
        + (
            f" ({', '.join(r['gate_config_files'][:15])}{' ...' if len(r['gate_config_files']) > 15 else ''})"
            if r["gate_config_files"]
            else ""
        ),
        f"- tests_weakened: **{str(r['tests_weakened']).lower()}**",
        f"- bypass_detected: **{str(r['bypass_detected']).lower()}**",
    ]
    for reason in (r["tests_weakened_reasons"] + r["bypass_reasons"])[:30]:
        lines.append(f"  - {reason}")
    ai = r["ai_review"]
    lines += [
        "",
        "### G2 AI review (fresh context, not production approval)",
        "",
        f"Status: **{ai.get('status')}**" + (f" -- {ai.get('reason')}" if ai.get("reason") else ""),
    ]
    for f in ai.get("findings", [])[:30]:
        lines.append(
            f"- [{f.get('severity')}] {f.get('category')}: {f.get('title')} "
            f"({f.get('file', '?')}:{f.get('line', '?')})"
        )
    uf = r["unresolved_findings"]
    lines += ["", f"### Unresolved blocking findings ({uf['blocking_count']})", ""]
    for f in uf["blocking"][:40]:
        detail = ", ".join(f"{k}={v}" for k, v in f.items() if k not in ("check", "examples"))[:220]
        lines.append(f"- `{f['check']}` {detail}")
    if uf["blocking_count"] > 40:
        lines.append(f"- ... {uf['blocking_count'] - 40} more in gate-report.json")
    lines += [
        "",
        "Baselined pre-existing debt (may only shrink): "
        + ", ".join(f"{k} {v}" for k, v in uf["baselined_debt"].items()),
        "",
    ]
    lines += [
        f"### Changed files ({len(r['changed_files'])}; {len(r['high_risk_files'])} high-risk)",
        "",
        "<details><summary>show</summary>",
        "",
    ]
    lines += [f"- {'**[high-risk]** ' if f in r['high_risk_files'] else ''}`{f}`" for f in r["changed_files"]]
    lines += [f"- (deleted) `{f}`" for f in r["deleted_files"]]
    lines += ["", "</details>", "", "### Label blockers", ""]
    lines += [f"- {b}" for b in r["label_blockers"]] or ["- none"]
    lines += ["", "### Known infrastructure blockers", ""] + [f"- {b}" for b in r["known_blockers"]]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("fast", "full"), required=True)
    ap.add_argument("--ai-review", default=None)
    ap.add_argument("--out-dir", default=str(gate_lib.ROOT))
    args = ap.parse_args()
    report = build(args.mode, args.ai_review)
    out = Path(args.out_dir)
    (out / "gate-report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (out / "gate-report.md").write_text(render_md(report), encoding="utf-8")
    sys.stderr.write(
        f"[gate] {report['final_status']}  ({len(report['failing_checks'])} failing mandatory check(s)) "
        f"-> gate-report.json, gate-report.md\n"
    )
    return {PASS: 0, FAIL: 1, BLOCKED: 3}[report["final_status"]]


if __name__ == "__main__":
    sys.exit(main())
