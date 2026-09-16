<<<<<<< HEAD
#!/usr/bin/env python3
"""ADDED BY SOURAV -- the single source of truth for this repo's
Pre-Human-Review Quality Gate (see PreHumanReviewQualityGateBlueprint,
delivered to the team 2026-09-10, and .claude/CLAUDE.md, which is the
version of this design a human/Claude session actually reads day to
day).

HONESTY NOTE -- read this before changing anything below. The
blueprint's own governing principle is "Claude's assertion that a check
passed is never evidence that it passed." Applied to THIS script: every
check below either (a) actually runs a real tool against this real repo
and reports what it really found, or (b) is marked "not_configured" when
this repo has no such tool today. Nothing here is hardcoded to "pass"
for a check this project doesn't actually have -- faking a green check
here would be exactly the "gate is theatre" anti-pattern (blueprint
section 6) this script exists to prevent. Concretely, as of this
writing:
  * No formatter or type-checker is pinned in requirements.txt. If one
    is available on the machine running this (e.g. this cloud sandbox
    has black/mypy installed globally; the actual dev machine may not),
    it runs and reports real results -- ADVISORY ONLY (see MANDATORY_
    CHECKS below), not "not_configured", because the tool DID run.
    Genuinely missing tools report "not_configured", never "pass".
  * mypy currently reports 37 real pre-existing errors and black would
    reformat nearly every file in this codebase (measured directly,
    2026-09-11) -- neither has ever been enforced here. Enforcing either
    as MANDATORY today would fail every single PR for pre-existing
    reasons unrelated to whatever it actually changed, which is not what
    a gate is for. They run and are REPORTED (so the numbers are visible
    and trackable) but do not block READY_FOR_HUMAN_REVIEW yet -- turning
    that on is a deliberate future decision for whoever owns this gate,
    not something to flip silently. This mirrors the blueprint's own
    rollout sequence (section 5): "Week 1 -- nothing blocks yet; measure
    how often G0 catches things."
  * There is no separate "golden regression" / "multilingual smoke" /
    "safety" / "handoff-fallback" / "API contract" test SUITE in this
    repo -- those are all real, already-existing tests, just not
    physically separated into their own folders. The report below
    buckets pytest's own test IDs by filename pattern into those
    categories for visibility (see _CATEGORY_PATTERNS), but every one of
    those tests is ALSO counted once in the plain "unit"/"integration"
    total -- the category breakdown is informational, not a second,
    double-counted gate.
  * dependency vulnerability scanning uses `pip-audit` if installed;
    "not_configured" otherwise (it is not installed anywhere in this
    project's environment today).

USAGE
    python scripts/gate-report.py --mode fast   # PostToolUse hook: lint + full test suite + secret/PHI scans. No report file written -- this is the noise-reduction pass, not the evidence artifact (blueprint 2.6).
    python scripts/gate-report.py --mode full   # Stop hook / pre-PR gate: everything --fast does, PLUS format/typecheck/dependency/PHI-in-code/build/git-diff checks, and writes gate-report.json + gate-report.md.

Exit code is 0 only when every MANDATORY check (see MANDATORY_CHECKS)
passed; both modes exit nonzero otherwise so the calling shell / hook can
block on it, per blueprint 4.2.
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REPORT_JSON = REPO_ROOT / "gate-report.json"
REPORT_MD = REPO_ROOT / "gate-report.md"

_EXCLUDE_PARTS = {"__pycache__", ".git", ".pytest_cache", "node_modules", ".venv", "venv"}

# Checks that actually block "ready for human review" today. Everything
# else in the report is real (never fabricated) but advisory-only for
# now -- see the module docstring's HONESTY NOTE for exactly why format/
# typecheck/dependency_scan/phi_in_code are excluded from this set.
MANDATORY_CHECKS = ("lint", "unit_integration", "secrets_scan", "phi_in_logs", "build")


def _run(cmd, cwd=REPO_ROOT, timeout=180):
    """-> (returncode, stdout, stderr). returncode is None when the
    command itself could not be found or timed out -- both distinct from
    a real nonzero exit, and both reported as "not_configured", never as
    a silent pass or an unhandled crash."""
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        return None, "", "command not found"
    except subprocess.TimeoutExpired:
        return None, "", f"timed out after {timeout}s"


def _iter_source_files(extensions=(".py",), roots=None):
    roots = roots or [REPO_ROOT]
    for root in roots:
        if not root.exists():
            continue
        for ext in extensions:
            for path in root.rglob(f"*{ext}"):
                if any(part in _EXCLUDE_PARTS for part in path.parts):
                    continue
                yield path


# --------------------------------------------------------------------- #
# G1 mechanical checks
# --------------------------------------------------------------------- #

def check_lint():
    rc, out, err = _run([sys.executable, "-m", "pyflakes", "."])
    if rc is None:
        return {"status": "not_configured", "detail": err}
    issues = [l for l in out.splitlines() if l.strip()]
    return {"status": "pass" if rc == 0 and not issues else "fail",
            "issue_count": len(issues), "issues": issues[:50]}


def check_format():
    if shutil.which("black") is None:
        return {"status": "not_configured", "detail": "no formatter pinned in requirements.txt -- black not installed"}
    rc, out, err = _run(["black", "--check", "-q", "."])
    return {"status": "pass" if rc == 0 else "fail", "detail": "black --check found unformatted files" if rc else ""}


def check_typecheck():
    if shutil.which("mypy") is None:
        return {"status": "not_configured", "detail": "no type-checker pinned in requirements.txt -- mypy not installed"}
    rc, out, err = _run(["mypy", "--ignore-missing-imports", "agent", "main.py", "main_pcm.py", "clinic-api"])
    if rc is None:
        return {"status": "not_configured", "detail": err}
    m = re.search(r"Found (\d+) error", out)
    error_count = int(m.group(1)) if m else (0 if rc == 0 else None)
    return {"status": "pass" if rc == 0 else "fail", "error_count": error_count}


def _run_pytest(args, timeout=120):
    """-> (status, passed, total, raw_summary_line). Uses plain `-q
    --tb=no` output rather than a JSON-report plugin (pytest-json-report
    is not installed in this project) -- the final summary line
    ("N passed", "N passed, M failed", ...) is stable pytest output this
    parses directly, so no extra dependency is required for this gate to
    run anywhere pytest already does."""
    rc, out, err = _run([sys.executable, "-m", "pytest", "-q", "--tb=no", *args], timeout=timeout)
    if rc is None:
        return "not_configured", 0, 0, err
    lines = [l for l in out.splitlines() if l.strip()]
    summary = lines[-1] if lines else ""
    passed = int(m.group(1)) if (m := re.search(r"(\d+) passed", summary)) else 0
    failed = int(m.group(1)) if (m := re.search(r"(\d+) failed", summary)) else 0
    errored = int(m.group(1)) if (m := re.search(r"(\d+) error", summary)) else 0
    total = passed + failed + errored
    status = "pass" if rc == 0 and total > 0 else ("not_configured" if total == 0 else "fail")
    return status, passed, total, summary


def check_unit_integration():
    status, passed, total, summary = _run_pytest(["tests/"])
    return {"status": status, "passed": passed, "total": total, "summary": summary}


# Filename-pattern -> category, for the INFORMATIONAL breakdown only
# (see module docstring). A test file matching more than one pattern is
# counted in the first one it matches, in this order.
_CATEGORY_PATTERNS = [
    ("golden_regression", ["tests/test_live_*.py"]),
    ("multilingual_smoke", ["tests/test_language_detection_dispatch.py", "tests/test_hinglish_fidelity.py",
                             "tests/test_multilingual_templates.py"]),
    ("safety_tests", ["tests/test_insufficient_verified_information.py", "tests/test_otp_not_hardcoded.py",
                       "tests/test_finish_booking_insufficient_information.py"]),
    ("handoff_fallback", ["tests/test_human_fallback.py"]),
    ("api_contract", ["tests/test_clinic_api_*.py", "tests/test_test_preparation_api.py"]),
]


def check_categories():
    results = {}
    for name, patterns in _CATEGORY_PATTERNS:
        files = []
        for pattern in patterns:
            files.extend(str(p.relative_to(REPO_ROOT)) for p in REPO_ROOT.glob(pattern))
        if not files:
            results[name] = {"status": "not_configured", "detail": "no matching test files found"}
            continue
        status, passed, total, summary = _run_pytest(files)
        results[name] = {"status": status, "passed": passed, "total": total, "files": files}
    return results


_MODULE_NOT_FOUND = re.compile(r"ModuleNotFoundError: No module named '([\w.]+)'")


def _classify_import_failure(stderr: str) -> tuple[str, str]:
    """A bare `import X` failing with ModuleNotFoundError means a
    third-party dependency isn't installed in WHICHEVER environment is
    running this gate right now (this cloud sandbox has no torch/
    torchaudio/NeMo stack at all -- the actual deploy target does, per
    README.md's "shares an environment with voice-to-rx-repo"). That is
    an environment gap, not a defect in this project's own code, so it
    is reported "not_configured" here -- never silently "pass" (the
    import genuinely did not succeed) and never "fail" (that would wrongly
    imply this change broke something). Any OTHER failure at import time
    (SyntaxError, a real NameError/AttributeError in this project's own
    module-level code, etc.) is a real "fail" -- this project's own bug,
    not an environment gap."""
    m = _MODULE_NOT_FOUND.search(stderr)
    if m:
        return "not_configured", f"third-party dependency {m.group(1)!r} not installed in this environment"
    return "fail", stderr[-2000:]


def check_build():
    """Real, cheap sanity check for a Python service with no compiled
    artifact: does every top-level module this project ships actually
    IMPORT cleanly? Catches a syntax error or a genuinely missing LOCAL
    dependency -- the closest honest analog to "build" this repo has.

    Bootstraps via tests/conftest.py's own existing stub-module machinery
    before importing main/main_pcm -- that file already solves exactly
    this problem for pytest collection (see its own module docstring):
    torch/torchaudio/nemo/omegaconf get lightweight stand-ins on any
    machine that doesn't have the real GPU stack, so this check reports a
    REAL pass anywhere the test suite itself already runs, instead of
    reusing _classify_import_failure()'s not_configured escape hatch for
    a gap this repo has already closed once. That escape hatch is kept
    for any OTHER third-party dependency conftest.py doesn't stub."""
    modules = [
        ("main", REPO_ROOT),
        ("main_pcm", REPO_ROOT),
    ]
    bootstrap = "import sys; sys.path.insert(0, 'tests'); import conftest; "
    results = {}
    saw_real_failure = False
    for mod_name, cwd in modules:
        rc, out, err = _run([sys.executable, "-c", f"{bootstrap}import {mod_name}"], cwd=cwd, timeout=60)
        if rc == 0:
            results[mod_name] = {"status": "pass", "detail": ""}
        else:
            status, detail = _classify_import_failure(err)
            saw_real_failure = saw_real_failure or (status == "fail")
            results[mod_name] = {"status": status, "detail": detail}
    rc, out, err = _run([sys.executable, "-c", "import main"], cwd=REPO_ROOT / "clinic-api", timeout=60)
    if rc == 0:
        results["clinic-api/main"] = {"status": "pass", "detail": ""}
    else:
        status, detail = _classify_import_failure(err)
        saw_real_failure = saw_real_failure or (status == "fail")
        results["clinic-api/main"] = {"status": status, "detail": detail}
    overall = "fail" if saw_real_failure else (
        "pass" if all(r["status"] == "pass" for r in results.values()) else "not_configured")
    return {"status": overall, "modules": results}


# --------------------------------------------------------------------- #
# Secrets / PHI scans -- real regex scans, deliberately narrow (see
# module docstring for why phi_in_code stays out of MANDATORY_CHECKS).
# --------------------------------------------------------------------- #

_SECRET_PATTERNS = [
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("generic_private_key", re.compile(r"-----BEGIN (RSA |EC |OPENSSH |)PRIVATE KEY-----")),
    ("slack_token", re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,}")),
    ("hardcoded_password_assignment", re.compile(r"(?i)\b(password|passwd|secret)\s*=\s*[\"'][^\"'\s]{4,}[\"']")),
    ("generic_api_key_assignment", re.compile(r"(?i)\bapi[_-]?key\s*=\s*[\"'][A-Za-z0-9_\-]{16,}[\"']")),
]

# Files that legitimately contain fixture-shaped strings that would
# otherwise trip the generic patterns above (e.g. a test asserting on
# the LITERAL string "password" as a slot name) -- excluded by path, not
# by weakening the pattern itself.
_SECRET_SCAN_EXCLUDE_DIRS = {"tests"}


def check_secrets_scan():
    findings = []
    for path in _iter_source_files(extensions=(".py", ".sh", ".json", ".yml", ".yaml")):
        if any(part in _SECRET_SCAN_EXCLUDE_DIRS for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for name, pattern in _SECRET_PATTERNS:
            for m in pattern.finditer(text):
                line_no = text.count("\n", 0, m.start()) + 1
                findings.append(f"{path.relative_to(REPO_ROOT)}:{line_no}: {name}")
    return {"status": "pass" if not findings else "fail", "finding_count": len(findings), "findings": findings[:50]}


# PHI-in-logs: this codebase already has an established, informally-
# enforced rule (see agent/tools_client.py's own "DoD gate 6" comment and
# agent/reply_templates.py's RULE 9/10) that an OTP or a full,
# un-masked phone number must never reach a log line. This check makes
# that rule MECHANICAL rather than relying on every future PR remembering
# it by hand.
_LOGGER_CALL = re.compile(r"logger\.\w+\(")
_RAW_PHONE_IN_STRING = re.compile(r"\b\d{10}\b")
_OTP_VAR_NAME = re.compile(r"\botp_code\b|\botp\b", re.IGNORECASE)


def _logger_call_spans(text):
    """Yield (start, end) character spans covering one full logger.X(...)
    call, by matching parens -- good enough for this codebase's own
    logger.*(...) call style (no nested logger calls as arguments)."""
    for m in _LOGGER_CALL.finditer(text):
        depth = 1
        i = m.end()
        while i < len(text) and depth > 0:
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
            i += 1
        yield m.start(), i


# A logger call's leading argument is its human-readable message string
# (e.g. "OTP messaging provider call failed for phone ending in %s: %s")
# -- the word "OTP" appearing there is prose, not a leaked secret. Only
# the ARGUMENTS after that first string literal are real interpolated
# values, so that is the only part these two regexes should ever see.
_LOGGER_CALL_PREFIX = re.compile(r"^\s*logger\.\w+\(")
_LEADING_STRING_LITERAL = re.compile(r'^\s*(\'\'\'|"""|\'|")')


def _args_after_format_string(call_text: str) -> str:
    """call_text is a FULL logger call span as yielded by
    _logger_call_spans(), e.g. `logger.error("msg %s", arg1, arg2)`
    (including the `logger.X(` prefix and the closing `)`). Strips both
    the call wrapper and the leading message-string literal, returning
    only what follows it -- the actual interpolated arguments. Falls
    back to the arguments-with-wrapper-stripped text if no leading
    string literal is found (an f-string or non-literal first argument
    -- rare in this codebase's own logger.*() call style, and safer to
    still scan than to silently skip)."""
    prefix = _LOGGER_CALL_PREFIX.match(call_text)
    if prefix:
        call_text = call_text[prefix.end():]
        if call_text.endswith(")"):
            call_text = call_text[:-1]
    m = _LEADING_STRING_LITERAL.match(call_text)
    if not m:
        return call_text
    quote = m.group(1)
    start = m.end()
    end = call_text.find(quote, start)
    if end == -1:
        return call_text
    return call_text[end + len(quote):]


def check_phi_in_logs():
    findings = []
    for path in _iter_source_files(extensions=(".py",), roots=[REPO_ROOT / "agent", REPO_ROOT / "clinic-api", REPO_ROOT]):
        if path.parent == REPO_ROOT and path.name not in ("main.py", "main_pcm.py"):
            continue  # only scan the two dispatch files at repo root, not every scratch script
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for start, end in _logger_call_spans(text):
            call_text = _args_after_format_string(text[start:end])
            line_no = text.count("\n", 0, start) + 1
            if _OTP_VAR_NAME.search(call_text):
                findings.append(f"{path.relative_to(REPO_ROOT)}:{line_no}: possible OTP reference in a logger call")
            if _RAW_PHONE_IN_STRING.search(call_text):
                findings.append(f"{path.relative_to(REPO_ROOT)}:{line_no}: possible un-masked 10-digit phone in a logger call")
    return {"status": "pass" if not findings else "fail", "finding_count": len(findings), "findings": findings[:50]}


def check_phi_in_code():
    """Deliberately narrower than phi_in_logs and NOT mandatory (see
    module docstring): clinic-api/seed.py legitimately ships fixture
    patient phone numbers as demo data, and flagging every 10-digit
    literal repo-wide would be constant false positives against that
    file. This scans for the same secret-shaped patterns as
    check_secrets_scan() but is kept as its own named check because the
    blueprint lists it separately -- currently redundant with
    secrets_scan until this repo has a real PHI-shaped pattern (e.g. a
    real patient record format) distinct from a credential."""
    return {"status": "advisory_only", "detail": "no PHI pattern distinct from secrets_scan exists in this "
            "repo yet -- clinic-api/seed.py's demo patient data is fixture content, not a scannable secret shape"}


def check_dependency_scan():
    if shutil.which("pip-audit") is None:
        return {"status": "not_configured", "detail": "pip-audit not installed"}
    rc, out, err = _run(["pip-audit", "-r", "requirements.txt"], timeout=120)
    if rc is None:
        return {"status": "not_configured", "detail": err}
    return {"status": "pass" if rc == 0 else "fail", "detail": out[-4000:]}


# --------------------------------------------------------------------- #
# git-based checks -- gate_config_touched / tests_weakened (blueprint
# 4.5). Best-effort: if this working copy has no git history to diff
# against (e.g. a fresh clone with one commit, or git itself absent),
# reports "unknown" rather than a fabricated False.
# --------------------------------------------------------------------- #

_GATE_PROTECTED_PATHS = ("scripts/gate.sh", "scripts/gate-report.py", ".github/workflows/")


def check_git_diff_guards():
    rc, out, err = _run(["git", "rev-parse", "--is-inside-work-tree"])
    if rc != 0:
        return {"gate_config_touched": "unknown", "tests_weakened": "unknown", "detail": "not a git repository"}
    rc, out, err = _run(["git", "diff", "--name-only", "HEAD"])
    if rc is None:
        rc, out, err = _run(["git", "diff", "--name-only"])
    if rc is None:
        return {"gate_config_touched": "unknown", "tests_weakened": "unknown", "detail": "git diff unavailable"}
    changed = [l.strip() for l in out.splitlines() if l.strip()]
    gate_touched = any(c.startswith(_GATE_PROTECTED_PATHS) for c in changed)
    tests_touched = [c for c in changed if c.startswith("tests/")]
    # "weakened" (not just "touched") would need a real diff-content
    # check (e.g. a removed assert, an added skip decorator) -- this
    # reports which test files changed so a human/G2 reviewer can look,
    # rather than claiming to have verified WHETHER they were weakened,
    # which this simple name-only diff cannot honestly claim.
    return {"gate_config_touched": gate_touched, "tests_files_changed": tests_touched,
            "tests_weakened": "unknown -- see tests_files_changed; content-level weakening is not auto-detected"}


# --------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------- #

def run(mode: str) -> dict:
    checks = {
        "lint": check_lint(),
        "unit_integration": check_unit_integration(),
        "secrets_scan": check_secrets_scan(),
        "phi_in_logs": check_phi_in_logs(),
        "build": check_build(),
    }
    if mode == "full":
        checks["format"] = check_format()
        checks["typecheck"] = check_typecheck()
        checks["dependency_scan"] = check_dependency_scan()
        checks["phi_in_code"] = check_phi_in_code()
        checks["categories"] = check_categories()
        checks.update(check_git_diff_guards())
    return checks


def _mandatory_ok(checks: dict) -> bool:
    for name in MANDATORY_CHECKS:
        entry = checks.get(name)
        if not entry or entry.get("status") != "pass":
            return False
    return True


def build_report(checks: dict, mode: str) -> dict:
    ok = _mandatory_ok(checks)
    gate_touched = checks.get("gate_config_touched")
    report = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "mode": mode,
        "mandatory_checks": list(MANDATORY_CHECKS),
        "checks": checks,
        "gate_config_touched": gate_touched,
        "status": "READY_FOR_HUMAN_REVIEW" if (ok and gate_touched is not True) else "NOT_READY",
    }
    return report


def render_markdown(report: dict) -> str:
    lines = [f"# Gate Report ({report['mode']})", "", f"Generated: {report['generated_at']}", "",
              f"**Status: {report['status']}**", ""]
    lines.append("## Mandatory checks")
    for name in report["mandatory_checks"]:
        entry = report["checks"].get(name, {})
        lines.append(f"- `{name}`: **{entry.get('status', 'missing')}**")
    lines.append("")
    lines.append("## All checks (including advisory-only)")
    for name, entry in report["checks"].items():
        if name == "categories":
            continue
        if isinstance(entry, dict) and "status" in entry:
            lines.append(f"- `{name}`: {entry['status']}")
    if "categories" in report["checks"]:
        lines.append("")
        lines.append("## Category breakdown (informational -- already counted in unit_integration above)")
        for name, entry in report["checks"]["categories"].items():
            lines.append(f"- `{name}`: {entry.get('status')} ({entry.get('passed', 0)}/{entry.get('total', 0)})")
    lines.append("")
    lines.append("_Claude's word that this gate passed is not evidence -- this file is generated by "
                 "scripts/gate-report.py itself; re-run it to verify._")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["fast", "full"], default="full")
    args = parser.parse_args()

    checks = run(args.mode)
    report = build_report(checks, args.mode)

    if args.mode == "full":
        REPORT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")
        REPORT_MD.write_text(render_markdown(report), encoding="utf-8")
        print(render_markdown(report))
    else:
        for name in MANDATORY_CHECKS:
            entry = checks.get(name, {})
            print(f"[{entry.get('status', 'missing').upper():^13}] {name}")

    sys.exit(0 if _mandatory_ok(checks) else 1)


if __name__ == "__main__":
    main()
=======
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
>>>>>>> dev_chakravardhan
