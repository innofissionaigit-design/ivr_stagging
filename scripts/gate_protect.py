"""Protect the gate from the changes it is judging.

Author: Chakravardhan

A gate that can be edited by the change under review is not a gate. This
module compares the working tree (or a pull request head) with its base and
answers three questions the label guard makes binding:

  gate_config_touched  -- did the change modify the gate's own machinery
                          (scripts/gate*, workflows, hooks, CLAUDE.md, the
                          golden set, the contract snapshots)?
  tests_weakened       -- did it delete tests, remove or trivialise
                          assertions, add skips, neutralise a test body, or
                          loosen a numeric threshold?
  bypass_detected      -- did it add suppression directives, disable CI
                          jobs, tamper with pytest collection, loosen the
                          baseline, or drop a mandatory check?

Everything here is static: files are parsed, never imported or executed, so
the label guard can safely run it against an untrusted pull request.

    python scripts/gate_protect.py --base <sha> [--out protect.json]
"""

from __future__ import annotations

import argparse
import ast
import json
import sys

import gate_lib
import gate_scan
from gate_lib import CONFIG_DIR, ROOT, file_at, files_at, matches, read_text

try:
    import yaml
except ImportError:  # PyYAML is pinned in every gate environment; absence is reported below
    yaml = None

PYTEST_CONFIG_FILES = ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini")
COLLECTION_TAMPERING = (
    "collect_ignore",
    "pytest_collection_modifyitems",
    "pytest_ignore_collect",
    "pytest_deselected",
    "deselect",
)
ADDOPTS_TAMPERING = ("-k ", "--deselect", "--ignore", "-p no:", "--co", "-m ", "--lf", "--sw")


# ===========================================================================
# Tests
# ===========================================================================
def _is_skip_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    name = ast.unparse(node.func)
    return name.split(".")[-1] in ("skip", "xfail", "importorskip", "skipTest") or name.endswith("SkipTest")


def _is_assertion(node: ast.AST) -> bool:
    if isinstance(node, ast.Assert):
        return True
    if isinstance(node, ast.Call):
        name = ast.unparse(node.func).split(".")[-1]
        return name in ("raises", "warns", "deprecated_call") or name.startswith("assert")
    return False


def _is_trivial(node: ast.Assert) -> bool:
    test = node.test
    if isinstance(test, ast.Constant):
        return bool(test.value)
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or):
        return any(isinstance(v, ast.Constant) and v.value for v in test.values)
    if isinstance(test, ast.Compare) and len(test.comparators) == 1:
        return ast.unparse(test.left) == ast.unparse(test.comparators[0])
    return False


def _bounds(fn: ast.AST) -> tuple[dict, dict, dict]:
    """Numeric bounds asserted in a test: {expr: value} for lower bounds
    (x >= 0.9), upper bounds (x <= 5) and approx tolerances."""
    lower, upper, tol = {}, {}, {}
    for node in ast.walk(fn):
        if isinstance(node, ast.Assert):
            for cmp in ast.walk(node.test):
                if not (isinstance(cmp, ast.Compare) and len(cmp.ops) == 1):
                    continue
                left, right, op = cmp.left, cmp.comparators[0], cmp.ops[0]
                num_right = isinstance(right, ast.Constant) and isinstance(right.value, (int, float))
                num_left = isinstance(left, ast.Constant) and isinstance(left.value, (int, float))
                if num_right and not num_left:
                    expr, value = ast.unparse(left), right.value
                    kind = (
                        "lower"
                        if isinstance(op, (ast.Gt, ast.GtE))
                        else "upper"
                        if isinstance(op, (ast.Lt, ast.LtE))
                        else None
                    )
                elif num_left and not num_right:
                    expr, value = ast.unparse(right), left.value
                    kind = (
                        "upper"
                        if isinstance(op, (ast.Gt, ast.GtE))
                        else "lower"
                        if isinstance(op, (ast.Lt, ast.LtE))
                        else None
                    )
                else:
                    continue
                if kind == "lower":
                    lower[expr] = min(lower.get(expr, value), value)
                elif kind == "upper":
                    upper[expr] = max(upper.get(expr, value), value)
        if isinstance(node, ast.Call) and ast.unparse(node.func).split(".")[-1] == "approx":
            for kw in node.keywords:
                if kw.arg in ("rel", "abs") and isinstance(kw.value, ast.Constant):
                    key = f"{ast.unparse(node.args[0]) if node.args else '?'}:{kw.arg}"
                    tol[key] = max(tol.get(key, kw.value.value), kw.value.value)
    return lower, upper, tol


def _neutralized(fn: ast.AST) -> bool:
    body = list(fn.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    if not body:
        return True
    first = body[0]
    if isinstance(first, (ast.Return, ast.Pass)):
        return True
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and first.value.value is Ellipsis
    ):
        return True
    if isinstance(first, ast.If) and isinstance(first.test, ast.Constant) and not first.test.value:
        return len(body) == 1
    return False


def test_inventory(src: str | None) -> dict:
    """-> {test_name: facts} for every test function in a module."""
    if src is None:
        return {}
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return {"<syntax-error>": {"asserts": 0}}
    out = {}

    def visit(fn, prefix=""):
        lower, upper, tol = _bounds(fn)
        out[prefix + fn.name] = {
            "asserts": sum(1 for n in ast.walk(fn) if _is_assertion(n)),
            "trivial": sum(1 for n in ast.walk(fn) if isinstance(n, ast.Assert) and _is_trivial(n)),
            "skips": sum(1 for n in ast.walk(fn) if _is_skip_call(n))
            + sum(1 for d in fn.decorator_list if any(w in ast.unparse(d) for w in ("skip", "xfail"))),
            "neutralized": _neutralized(fn),
            "lower": lower,
            "upper": upper,
            "tol": tol,
        }

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            visit(node)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name.startswith("test"):
                    visit(sub, node.name + ".")
    module_marks = sum(
        1
        for n in tree.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in n.targets)
        and any(w in ast.unparse(n.value) for w in ("skip", "xfail"))
    )
    out["<module-skips>"] = {"skips": module_marks}
    return out


def compare_tests(path: str, base_src: str | None, head_src: str | None) -> list[str]:
    if base_src is None:
        return []  # a new test file weakens nothing
    if head_src is None:
        return [f"test file deleted: {path}"]
    base, head = test_inventory(base_src), test_inventory(head_src)
    reasons = []
    mod_b, mod_h = base.pop("<module-skips>", {}), head.pop("<module-skips>", {})
    if mod_h.get("skips", 0) > mod_b.get("skips", 0):
        reasons.append(f"module-level skip/xfail added: {path}")
    for name, b in base.items():
        h = head.get(name)
        where = f"{path}::{name}"
        if h is None:
            reasons.append(f"test removed or renamed: {where}")
            continue
        if h["asserts"] < b["asserts"]:
            reasons.append(f"assertions reduced {b['asserts']} -> {h['asserts']}: {where}")
        if h["trivial"] > b["trivial"]:
            reasons.append(f"always-true assertion added: {where}")
        if h["skips"] > b["skips"]:
            reasons.append(f"skip/xfail added: {where}")
        if h["neutralized"] and not b["neutralized"]:
            reasons.append(f"test body neutralised (returns/passes before asserting): {where}")
        for expr, value in b["lower"].items():
            if expr in h["lower"] and h["lower"][expr] < value:
                reasons.append(f"threshold lowered on `{expr}` {value} -> {h['lower'][expr]}: {where}")
        for expr, value in b["upper"].items():
            if expr in h["upper"] and h["upper"][expr] > value:
                reasons.append(f"threshold raised on `{expr}` {value} -> {h['upper'][expr]}: {where}")
        for key, value in b["tol"].items():
            if key in h["tol"] and h["tol"][key] > value:
                reasons.append(f"tolerance widened on {key} {value} -> {h['tol'][key]}: {where}")
    return reasons


def is_test_module(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return path.startswith("tests/") and name.startswith("test_") and name.endswith(".py")


# ===========================================================================
# Gate configuration
# ===========================================================================
def _json_or_none(text):
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


def compare_config(base_cfg: dict | None, head_cfg: dict | None) -> list[str]:
    if base_cfg is None:
        return []
    if head_cfg is None:
        return ["gate-config.json deleted"]
    reasons = []
    for cid, b in base_cfg.get("checks", {}).items():
        h = head_cfg.get("checks", {}).get(cid)
        if h is None:
            reasons.append(f"check removed from gate config: {cid}")
            continue
        if b.get("mandatory", True) and not h.get("mandatory", True):
            reasons.append(f"check made optional: {cid}")
        dropped = set(b.get("modes", [])) - set(h.get("modes", []))
        if dropped:
            reasons.append(f"check {cid} no longer runs in mode(s): {sorted(dropped)}")
    for sid, b in base_cfg.get("suites", {}).items():
        h = head_cfg.get("suites", {}).get(sid)
        if h is None:
            reasons.append(f"test suite removed: {sid}")
            continue
        if h.get("min_tests", 0) < b.get("min_tests", 0):
            reasons.append(f"suite {sid} min_tests lowered {b.get('min_tests')} -> {h.get('min_tests')}")
        removed = set(b.get("select", [])) - set(h.get("select", []))
        if removed:
            reasons.append(f"suite {sid} selectors removed: {sorted(removed)}")
        added_excl = set(h.get("exclude", [])) - set(b.get("exclude", []))
        if added_excl:
            reasons.append(f"suite {sid} exclusions added: {sorted(added_excl)}")
    for key in ("protected_paths", "forbidden_file_globs", "required_files", "high_risk_paths"):
        removed = set(base_cfg.get(key, [])) - set(head_cfg.get(key, []))
        if removed:
            reasons.append(f"{key} entries removed: {sorted(removed)}")
    if head_cfg.get("max_file_bytes", 0) > base_cfg.get("max_file_bytes", 0):
        reasons.append("max_file_bytes raised")
    if head_cfg.get("max_remediation_cycles", 0) > base_cfg.get("max_remediation_cycles", 0):
        reasons.append("max_remediation_cycles raised")
    return reasons


def _flatten(obj, prefix="") -> dict:
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(_flatten(v, f"{prefix}{k}/"))
    elif isinstance(obj, list):
        for v in obj:
            out[f"{prefix}{v}"] = 1
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        out[prefix.rstrip("/")] = obj
    return out


def compare_baseline(base_bl: dict | None, head_bl: dict | None) -> list[str]:
    """The baseline may only shrink. A new entry or a higher count is the
    gate being loosened to admit a violation, whatever the commit says."""
    if base_bl is None or head_bl is None:
        return []
    b, h = (
        _flatten({k: v for k, v in base_bl.items() if k not in ("generated_at", "note", "version")}),
        _flatten({k: v for k, v in head_bl.items() if k not in ("generated_at", "note", "version")}),
    )
    return [f"baseline loosened: {k} {b.get(k, 0)} -> {v}" for k, v in sorted(h.items()) if v > b.get(k, 0)]


def workflow_policy(cfg: dict) -> list[str]:
    """Structural rules the CI definitions must satisfy in the HEAD tree,
    whatever the base looked like."""
    reasons = []
    if yaml is None:
        return ["PyYAML unavailable: workflow policy could not be verified"]
    for wf, rules in cfg.get("workflow_policy", {}).items():
        text = read_text(wf)
        if text is None:
            reasons.append(f"required workflow missing: {wf}")
            continue
        try:
            doc = yaml.safe_load(text) or {}
        except yaml.YAMLError as e:
            reasons.append(f"workflow does not parse: {wf}: {e}")
            continue
        jobs = doc.get("jobs") or {}
        for job_id in rules.get("required_jobs", []):
            job = jobs.get(job_id)
            if job is None:
                reasons.append(f"{wf}: required job removed: {job_id}")
                continue
            if job.get("continue-on-error") not in (None, False):
                reasons.append(f"{wf}: job {job_id} has continue-on-error")
            cond = str(job.get("if", "")).replace(" ", "").lower()
            if cond in ("false", "${{false}}", "0"):
                reasons.append(f"{wf}: job {job_id} disabled with if: {job.get('if')}")
        triggers = doc.get(True, doc.get("on")) or {}
        trig_names = set(triggers) if isinstance(triggers, (dict, list)) else {str(triggers)}
        for trig in rules.get("required_triggers", []):
            if trig not in trig_names:
                reasons.append(f"{wf}: required trigger removed: {trig}")
        for needle in rules.get("required_commands", []):
            if needle not in text:
                reasons.append(f"{wf}: required command missing: {needle}")
        for step_text in _run_steps(jobs):
            if "scripts/gate.sh" in step_text and any(
                s in step_text for s in ("|| true", "|| exit 0", "|| :", "set +e")
            ):
                reasons.append(f"{wf}: gate invocation made non-fatal: {step_text.strip()[:80]}")
        for job_id, job in jobs.items():
            for step in job.get("steps", []) or []:
                if (
                    isinstance(step, dict)
                    and step.get("continue-on-error") not in (None, False)
                    and "scripts/gate" in str(step.get("run", ""))
                ):
                    reasons.append(f"{wf}: gate step in {job_id} has continue-on-error")
    return reasons


def _run_steps(jobs: dict):
    for job in jobs.values():
        for step in (job or {}).get("steps", []) or []:
            if isinstance(step, dict) and "run" in step:
                yield str(step["run"])


def hook_policy() -> list[str]:
    text = read_text(".claude/settings.json")
    if text is None:
        return ["G0 hooks missing: .claude/settings.json not found"]
    try:
        hooks = json.loads(text).get("hooks", {})
    except ValueError:
        return [".claude/settings.json does not parse"]
    if not isinstance(hooks, dict):
        return [".claude/settings.json: `hooks` is not an object"]
    reasons = []
    post = _hook_commands(hooks.get("PostToolUse"))
    if not any(
        "scripts/gate.sh --fast" in cmd
        for matcher, cmd in post
        if {"Edit", "Write", "MultiEdit"} <= set(matcher.split("|"))
    ):
        reasons.append("G0 PostToolUse hook no longer runs `gate.sh --fast` after Edit|Write|MultiEdit")
    if not any("scripts/gate.sh --full" in cmd for _, cmd in _hook_commands(hooks.get("Stop"))):
        reasons.append("G0 Stop hook no longer runs `gate.sh --full`")
    for event in hooks:
        for _, cmd in _hook_commands(hooks.get(event)):
            for bad in ("|| true", "|| exit 0", "; exit 0", "|| :"):
                if "scripts/gate" in cmd and bad in cmd:
                    reasons.append(f"G0 {event} hook made non-fatal with `{bad}`")
    return reasons


def _hook_commands(entries) -> list[tuple[str, str]]:
    """(matcher, command) for every command hook under one event, with shell
    quoting removed so `bash "$DIR/scripts/gate.sh" --fast` reads as the
    plain command it runs."""
    out = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        matcher = str(entry.get("matcher", ""))
        for h in entry.get("hooks", []) or []:
            if isinstance(h, dict) and h.get("type") == "command":
                out.append((matcher, str(h.get("command", "")).replace('"', "").replace("'", "")))
    return out


# ===========================================================================
# The analysis
# ===========================================================================
def analyze(base: str, changed: list[str], deleted: list[str]) -> dict:
    cfg = gate_lib.config()
    protected = cfg["protected_paths"]
    touched = sorted({p for p in changed + deleted if matches(p, protected)})

    tests_weakened: list[str] = []
    base_tests = [p for p in files_at(base, "tests") if is_test_module(p)]
    for path in sorted(set(base_tests) | {p for p in changed + deleted if is_test_module(p)}):
        tests_weakened += compare_tests(
            path, file_at(base, path), read_text(path) if (ROOT / path).exists() else None
        )

    bypass: list[str] = []
    for path in changed:
        name = path.rsplit("/", 1)[-1]
        if name == "conftest.py" or name in PYTEST_CONFIG_FILES:
            head, base_src = read_text(path) or "", file_at(base, path) or ""
            needles = COLLECTION_TAMPERING if name == "conftest.py" else ADDOPTS_TAMPERING
            for needle in needles:
                if head.count(needle) > base_src.count(needle):
                    bypass.append(f"test collection tampering in {path}: `{needle.strip()}`")

    allowed = gate_lib.baseline().get("suppressions", {})
    current = gate_scan.suppressions(gate_lib.repo_files())
    for path, n in sorted(current.items()):
        if n > allowed.get(path, 0):
            bypass.append(f"suppression directive added in {path} ({allowed.get(path, 0)} -> {n})")

    bypass += compare_config(
        _json_or_none(file_at(base, "scripts/gate-config.json")),
        _json_or_none(read_text("scripts/gate-config.json")),
    )
    bypass += compare_baseline(
        _json_or_none(file_at(base, "scripts/gate-baseline.json")),
        _json_or_none(read_text("scripts/gate-baseline.json")),
    )
    for req in cfg.get("required_files", []):
        if not (ROOT / req).exists():
            bypass.append(f"gate file missing: {req}")
    bypass += workflow_policy(cfg)
    bypass += hook_policy()

    info = []
    base_approved = _json_or_none(file_at(base, "scripts/gate-approved-test-data.json"))
    head_approved = _json_or_none(read_text("scripts/gate-approved-test-data.json"))
    if base_approved and head_approved:
        for key in ("phones", "names", "dates_of_birth", "email_domains", "phone_ranges"):
            added = [v for v in head_approved.get(key, []) if v not in base_approved.get(key, [])]
            if added:
                info.append(f"approved test data expanded: {key} (+{len(added)})")

    return {
        "base": base,
        "config_dir": str(CONFIG_DIR),
        "gate_config_touched": bool(touched),
        "gate_config_files": touched,
        "tests_weakened": bool(tests_weakened),
        "tests_weakened_reasons": tests_weakened,
        "bypass_detected": bool(bypass),
        "bypass_reasons": bypass,
        "info": info,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default=None, help="base commit (default: resolved GATE_BASE_REF)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    base = args.base or gate_lib.resolve_base()
    result = analyze(base, gate_lib.changed_files(base), gate_lib.deleted_files(base))
    text = json.dumps(result, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
    sys.stdout.write(text + "\n")
    return 1 if (result["tests_weakened"] or result["bypass_detected"]) else 0


if __name__ == "__main__":
    sys.exit(main())
