"""Run the gate's checks and record one result per check.

Author: Chakravardhan

Called by scripts/gate.sh; see that file for the modes. Every check writes
.gate/results/<id>.json, and scripts/gate-report.py turns those into the
report and the final verdict. A check that cannot run -- a missing tool, a
timeout, a crash -- records ERROR, which the report treats exactly like a
failure. Nothing here can turn "could not check" into "passed".

    python scripts/gate_checks.py --mode fast|full
    python scripts/gate_checks.py --write-baseline     # gate-config change
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures as cf
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import traceback
import xml.etree.ElementTree as ET
from pathlib import Path

import gate_lib
import gate_protect
import gate_scan
from gate_lib import (
    GATE_DIR,
    GITLEAKS_CONFIG,
    MYPY_CONFIG,
    RESULTS_DIR,
    ROOT,
    RUFF_CONFIG,
    STATUS_ERROR,
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_SKIP,
    CheckResult,
    Timer,
    app_python,
    bash_exe,
    matches,
    ratchet,
    read_text,
    rel,
    sh,
    tool_python,
)

_PRINT_LOCK = threading.Lock()


def log(msg: str) -> None:
    with _PRINT_LOCK:
        sys.stderr.write(msg + "\n")
        sys.stderr.flush()


def tool(module: str, *args, timeout: float = 900, cwd: Path | None = None):
    env = {"RUFF_CACHE_DIR": str(GATE_DIR / "ruff-cache")}
    return sh([tool_python(), "-m", module, *args], timeout=timeout, cwd=cwd, env=env)


def gitleaks_exe() -> str | None:
    if os.environ.get("GATE_GITLEAKS"):
        return os.environ["GATE_GITLEAKS"]
    for cand in (ROOT / ".gate-venv" / "Scripts" / "gitleaks.exe", ROOT / ".gate-venv" / "bin" / "gitleaks"):
        if cand.exists():
            return str(cand)
    return shutil.which("gitleaks")


class Context:
    def __init__(self, mode: str):
        self.mode = mode
        self.cfg = gate_lib.config()
        self.baseline = gate_lib.baseline()
        self.approved = gate_lib.load_json(gate_lib.APPROVED_PATH, {})
        self.base = gate_lib.resolve_base()
        self.changed = gate_lib.changed_files(self.base)
        self.deleted = gate_lib.deleted_files(self.base)
        self.all_files = gate_lib.repo_files()
        self._pytest = None
        self._lock = threading.Lock()

    def scope(self) -> list[str]:
        """Fast mode looks at what changed; full mode looks at everything."""
        return self.changed if self.mode == "fast" else self.all_files

    def py(self, files=None) -> list[str]:
        return [f for f in (self.scope() if files is None else files) if f.endswith(".py")]

    def source_py(self, files=None) -> list[str]:
        roots = self.cfg["python_source"]
        return [f for f in self.py(files) if any(f == r or f.startswith(r + "/") for r in roots)]

    def scope_set(self):
        return set(self.scope()) if self.mode == "fast" else None


def _res(cid, status, summary, findings=None, metrics=None, details=""):
    return CheckResult(
        id=cid,
        status=status,
        summary=summary,
        findings=findings or [],
        metrics=metrics or {},
        details=details[-4000:] if details else "",
    )


def _ratchet_result(cid, current, allowed, ctx, label, findings_by_key=None):
    regressions, improvements = ratchet(current, allowed, ctx.scope_set())
    findings = []
    for r in regressions:
        findings.append(
            {
                "key": r["key"],
                "count": r["count"],
                "baseline": r["baseline"],
                "examples": (findings_by_key or {}).get(r["key"], [])[:3],
            }
        )
    metrics = {
        "violations": sum(current.values()),
        "baselined": sum(allowed.values()),
        "new": len(regressions),
        "improved_keys": len(improvements),
    }
    if regressions:
        return _res(cid, STATUS_FAIL, f"{len(regressions)} new {label} above baseline", findings, metrics)
    note = f"; {len(improvements)} baselined key(s) improved -- tighten the baseline" if improvements else ""
    return _res(cid, STATUS_PASS, f"no new {label} ({sum(current.values())} baselined){note}", [], metrics)


# ===========================================================================
# Collectors -- shared by the checks and by --write-baseline, so the two can
# never disagree about what counts as a violation.
# ===========================================================================
def collect_format(files):
    if not files:
        return True, [], ""
    rc, out, err = tool(
        "ruff", "format", "--check", "--output-format", "concise", "--config", str(RUFF_CONFIG), *files
    )
    if rc not in (0, 1):
        return False, [], out + err
    text = out + err
    # concise: "path:line:col: unformatted: ..."; older ruff: "Would reformat: path"
    found = {m.group(1) for m in re.finditer(r"^(.+?):\d+:\d+: unformatted:", text, re.M)}
    found |= {m.group(1) for m in re.finditer(r"^Would reformat: (.+)$", text, re.M)}
    if rc == 1 and not found:
        # ruff says something needs formatting but we could not tell what:
        # an unparsed answer is not a pass.
        return False, [], "ruff format --check exited 1 but its output could not be parsed:\n" + text
    return True, sorted({rel(f.strip()) for f in found}), ""


def collect_lint(files, select=None):
    if not files:
        return True, {}, {}, ""
    args = ["ruff", "check", "--config", str(RUFF_CONFIG), "--output-format", "json", "--exit-zero"]
    if select:
        args += ["--select", select]
    rc, out, err = tool(*args, *files)
    try:
        data = json.loads(out or "[]")
    except ValueError:
        return False, {}, {}, out + err
    counts, examples = collections.Counter(), collections.defaultdict(list)
    for d in data:
        key = f"{rel(d['filename'])}|{d.get('code') or 'syntax-error'}"
        counts[key] += 1
        examples[key].append(f"line {d.get('location', {}).get('row')}: {d.get('message')}")
    return True, dict(counts), dict(examples), ""


_MYPY_LINE = re.compile(r"^(.+?):(\d+): error: (.*?)(?:\s+\[([\w-]+)\])?$")


def collect_typecheck(cfg):
    counts, examples, errors = collections.Counter(), collections.defaultdict(list), []
    tc = cfg["typecheck"]
    runs = [(ROOT, [t for t in tc["root_targets"] if (ROOT / t).exists()], "", "mypy-cache")]
    clinic = ROOT / tc["clinic_api_dir"]
    runs.append(
        (clinic, sorted(p.name for p in clinic.glob("*.py")), tc["clinic_api_dir"] + "/", "mypy-cache-clinic")
    )
    for cwd, targets, prefix, cache in runs:
        rc, out, err = tool(
            "mypy",
            "--config-file",
            str(MYPY_CONFIG),
            "--cache-dir",
            str(GATE_DIR / cache),
            "--python-executable",
            app_python(),
            "--no-error-summary",
            *targets,
            timeout=2400,
            cwd=cwd,
        )
        if rc not in (0, 1):
            errors.append(f"mypy exited {rc} in {rel(cwd) or '.'}: {(out + err)[-1500:]}")
            continue
        for line in out.splitlines():
            m = _MYPY_LINE.match(line.strip())
            if m:
                key = f"{prefix}{m.group(1).replace(chr(92), '/')}|{m.group(4) or 'misc'}"
                counts[key] += 1
                examples[key].append(f"line {m.group(2)}: {m.group(3)}")
    return not errors, dict(counts), dict(examples), "\n".join(errors)


def collect_dead_code(cfg, files):
    roots = [r for r in cfg["python_source"] if (ROOT / r).exists()]
    rc, out, err = tool("vulture", *roots, "--min-confidence", "80")
    if rc not in (0, 3):
        return False, {}, {}, out + err
    counts, examples = collections.Counter(), collections.defaultdict(list)
    for line in out.splitlines():
        m = re.match(r"^(.+?):(\d+): (.+?) \(\d+% confidence\)$", line.strip())
        if m:
            key = f"{rel(m.group(1))}|vulture:{m.group(3)}"
            counts[key] += 1
            examples[key].append(f"line {m.group(2)}")
    ok, lint_counts, lint_examples, lint_err = collect_lint(files, select="F401,F811,F841")
    if not ok:
        return False, {}, {}, lint_err
    for key, n in lint_counts.items():
        counts[key] += n
        examples[key] += lint_examples.get(key, [])
    return True, dict(counts), dict(examples), ""


def collect_bandit(files):
    if not files:
        return True, {}, {}, ""
    rc, out, err = tool("bandit", "-q", "-f", "json", *files)
    try:
        data = json.loads(out or "{}")
    except ValueError:
        return False, {}, {}, out + err
    counts, examples = collections.Counter(), collections.defaultdict(list)
    for r in data.get("results", []):
        key = f"{rel(r['filename'])}|{r['test_id']}"
        counts[key] += 1
        examples[key].append(f"line {r['line_number']} [{r['issue_severity']}]: {r['issue_text']}")
    return True, dict(counts), dict(examples), ""


def collect_secrets(files, base: str):
    exe = gitleaks_exe()
    if not exe:
        return False, {}, {}, "gitleaks not found (set GATE_GITLEAKS or install it into .gate-venv)"
    counts, examples = collections.Counter(), collections.defaultdict(list)
    stage = Path(tempfile.mkdtemp(prefix="gate_secrets_"))
    report = stage.parent / f"{stage.name}.json"
    try:
        for f in files:
            src = ROOT / f
            if src.is_file():
                (stage / f).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, stage / f)
        scans = [["dir", str(stage)]]
        if gate_lib.is_ci() and base != gate_lib.EMPTY_TREE:
            scans.append(["git", str(ROOT), f"--log-opts={base}..HEAD"])
        for scan in scans:
            rc, out, err = sh(
                [
                    exe,
                    *scan,
                    "--no-banner",
                    "--exit-code",
                    "0",
                    "-c",
                    str(GITLEAKS_CONFIG),
                    "-f",
                    "json",
                    "-r",
                    str(report),
                ],
                timeout=600,
            )
            if rc != 0 or not report.exists():
                return False, {}, {}, f"gitleaks {scan[0]} failed rc={rc}: {(out + err)[-1500:]}"
            for item in json.loads(report.read_text(encoding="utf-8") or "[]"):
                path = item.get("File", "").replace("\\", "/")
                path = path.split(stage.as_posix() + "/", 1)[-1] if stage.as_posix() in path else rel(path)
                digest = hashlib.sha256((item.get("Secret") or "").encode()).hexdigest()[:16]
                key = f"{path}|{item.get('RuleID')}|{digest}"
                counts[key] += 1
                examples[key].append(
                    f"line {item.get('StartLine')} commit {item.get('Commit', '')[:8]}".strip()
                )
            report.unlink(missing_ok=True)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        report.unlink(missing_ok=True)
    return True, dict(counts), dict(examples), ""


_PIN = re.compile(r"^([A-Za-z0-9_.\-]+(?:\[[^\]]*\])?)\s*==\s*([^\s;#]+)")
_NAME = re.compile(r"^([A-Za-z0-9_.\-]+)")


def collect_dependencies(cfg):
    counts, examples, errors = collections.Counter(), collections.defaultdict(list), []
    for req in cfg["requirements_files"]:
        text = read_text(req)
        if text is None:
            errors.append(f"{req}: missing")
            continue
        pinned = []
        for raw in text.splitlines():
            line = raw.split(" #", 1)[0].strip()
            if not line or line.startswith(("#", "-")):
                continue
            m = _PIN.match(line)
            if m:
                pinned.append(line)
            else:
                name_match = _NAME.match(line)
                name = name_match.group(1) if name_match else line
                key = f"{req}|unpinned:{name}"
                counts[key] += 1
                examples[key].append(f"`{line}` is not pinned with ==")
        if not pinned:
            continue
        fd, name = tempfile.mkstemp(prefix="gate_req_", suffix=".txt")
        os.close(fd)  # an open descriptor keeps Windows from deleting the file afterwards
        tmp = Path(name)
        tmp.write_text("\n".join(pinned) + "\n", encoding="utf-8")
        try:
            rc, out, err = tool(
                "pip_audit",
                "-r",
                str(tmp),
                "--no-deps",
                "--disable-pip",
                "-f",
                "json",
                "--progress-spinner",
                "off",
                timeout=900,
            )
        finally:
            tmp.unlink(missing_ok=True)
        try:
            data = json.loads(out)
        except ValueError:
            errors.append(f"{req}: pip-audit rc={rc}: {(out + err)[-800:]}")
            continue
        for dep in data.get("dependencies", []):
            if dep.get("skip_reason"):
                key = f"{req}|unauditable:{dep.get('name')}"
                counts[key] += 1
                examples[key].append(dep["skip_reason"])
            for v in dep.get("vulns", []):
                key = f"{req}|{dep['name']}=={dep.get('version')}|{v['id']}"
                counts[key] += 1
                examples[key].append(f"fix: {', '.join(v.get('fix_versions') or []) or 'none'}")
    return not errors, dict(counts), dict(examples), "\n".join(errors)


def _key_counts(findings, keyfn):
    counts, examples = collections.Counter(), collections.defaultdict(list)
    for f in findings:
        k = keyfn(f)
        counts[k] += 1
        examples[k].append(f"line {f.get('line')}")
    return dict(counts), dict(examples)


# ===========================================================================
# Checks
# ===========================================================================
def check_compile(ctx):
    problems = []
    for f in ctx.py():
        text = read_text(f)
        if text is None:
            continue
        try:
            compile(text, f, "exec")
        except SyntaxError as e:
            problems.append({"file": f, "line": e.lineno, "error": e.msg})
    for f in [x for x in ctx.scope() if x.endswith(".sh")]:
        rc, out, err = sh([bash_exe(), "-n", f], timeout=60)
        if rc != 0:
            problems.append({"file": f, "error": (out + err).strip()[:300]})
    try:
        import yaml
    except ImportError:
        yaml = None
    for f in [x for x in ctx.scope() if x.endswith((".yml", ".yaml"))]:
        if yaml is None:
            problems.append({"file": f, "error": "PyYAML unavailable"})
            continue
        try:
            yaml.safe_load(read_text(f) or "")
        except yaml.YAMLError as e:
            problems.append({"file": f, "error": str(e)[:300]})
    for f in [x for x in ctx.scope() if x.endswith(".json")]:
        try:
            json.loads(read_text(f) or "")
        except ValueError as e:
            problems.append({"file": f, "error": str(e)[:300]})
    import tomllib

    for f in [x for x in ctx.scope() if x.endswith(".toml")]:
        try:
            tomllib.loads(read_text(f) or "")
        except tomllib.TOMLDecodeError as e:
            problems.append({"file": f, "error": str(e)[:300]})
    if problems:
        return _res("compile", STATUS_FAIL, f"{len(problems)} file(s) do not parse", problems)
    return _res("compile", STATUS_PASS, f"{len(ctx.scope())} file(s) in scope parse cleanly")


def check_format(ctx):
    files = ctx.py()
    ok, unformatted, err = collect_format(files)
    if not ok:
        return _res("format", STATUS_ERROR, "ruff format could not run", details=err)
    allowed = set(ctx.baseline.get("format_unformatted", []))
    new = [f for f in unformatted if f not in allowed]
    if new:
        return _res(
            "format",
            STATUS_FAIL,
            f"{len(new)} file(s) not formatted (not in baseline)",
            [{"file": f, "fix": "ruff format --config scripts/gate-ruff.toml " + f} for f in new],
        )
    return _res(
        "format",
        STATUS_PASS,
        f"{len(files)} file(s) checked; {len(unformatted)} baselined legacy file(s)",
        metrics={"checked": len(files), "baselined_unformatted": len(unformatted)},
    )


def check_lint(ctx):
    ok, counts, examples, err = collect_lint(ctx.py())
    if not ok:
        return _res("lint", STATUS_ERROR, "ruff check could not run", details=err)
    return _ratchet_result("lint", counts, ctx.baseline.get("lint", {}), ctx, "lint violation(s)", examples)


def check_typecheck(ctx):
    ok, counts, examples, err = collect_typecheck(ctx.cfg)
    if not ok:
        return _res("typecheck", STATUS_ERROR, "mypy could not complete", details=err)
    return _ratchet_result(
        "typecheck", counts, ctx.baseline.get("typecheck", {}), ctx, "type error(s)", examples
    )


def check_dead_code(ctx):
    ok, counts, examples, err = collect_dead_code(ctx.cfg, ctx.py(ctx.all_files))
    if not ok:
        return _res("dead-code", STATUS_ERROR, "dead-code analysis could not run", details=err)
    return _ratchet_result(
        "dead-code", counts, ctx.baseline.get("dead_code", {}), ctx, "dead-code finding(s)", examples
    )


def check_static(ctx):
    ok, counts, examples, err = collect_bandit(ctx.source_py())
    if not ok:
        return _res("static-analysis", STATUS_ERROR, "bandit could not run", details=err)
    return _ratchet_result(
        "static-analysis", counts, ctx.baseline.get("bandit", {}), ctx, "security finding(s)", examples
    )


def check_secrets(ctx):
    ok, counts, examples, err = collect_secrets(ctx.scope(), ctx.base)
    if not ok:
        return _res("secrets", STATUS_ERROR, "secrets scan could not run", details=err)
    return _ratchet_result("secrets", counts, ctx.baseline.get("secrets", {}), ctx, "secret(s)", examples)


def check_prod_credentials(ctx):
    findings = gate_scan.prod_credentials(ctx.scope())
    if findings:
        return _res(
            "prod-credentials",
            STATUS_FAIL,
            f"{len(findings)} credential(s) found -- remove AND rotate; never baseline",
            findings,
        )
    return _res("prod-credentials", STATUS_PASS, f"{len(ctx.scope())} file(s) scanned, no credentials")


def check_phi_in_code(ctx):
    findings = gate_scan.phi_in_code(ctx.scope(), ctx.approved)
    if findings:
        return _res(
            "phi-in-code",
            STATUS_FAIL,
            f"{len(findings)} real-looking patient identifier(s) outside tests",
            findings,
        )
    return _res("phi-in-code", STATUS_PASS, "no patient identifiers outside approved test data")


def check_phi_in_logs(ctx):
    findings = gate_scan.phi_in_logs(ctx.scope())
    counts, _ = _key_counts(findings, lambda f: f["key"])
    examples = collections.defaultdict(list)
    for f in findings:
        examples[f["key"]].append(f"line {f['line']} ({f['kind']} of `{f['identifier']}`)")
    return _ratchet_result(
        "phi-in-logs", counts, ctx.baseline.get("phi_logs", {}), ctx, "PHI-to-log path(s)", dict(examples)
    )


def check_approved_test_data(ctx):
    findings = gate_scan.approved_test_data(ctx.scope(), ctx.approved, ctx.cfg["test_data_paths"])
    if findings:
        return _res(
            "approved-test-data",
            STATUS_FAIL,
            f"{len(findings)} identifier(s) in test data are not in the approved fictional set",
            findings,
        )
    return _res(
        "approved-test-data", STATUS_PASS, "all test identifiers come from the approved fictional set"
    )


def check_debug_code(ctx):
    findings = gate_scan.debug_code(ctx.scope(), ctx.cfg["cli_files"])
    counts, examples = _key_counts(findings, lambda f: f"{f['file']}|{f['kind']}")
    return _ratchet_result(
        "debug-code", counts, ctx.baseline.get("debug", {}), ctx, "debug artefact(s)", examples
    )


def _generator_in_sync() -> tuple[bool, str]:
    """main_pcm.py is generated from main.py. Hand-editing it, or editing
    main.py without regenerating, ships two transports that disagree."""
    tmp = Path(tempfile.mkdtemp(prefix="gate_gen_"))
    try:
        (tmp / "tools").mkdir()
        shutil.copyfile(ROOT / "main.py", tmp / "main.py")
        shutil.copyfile(ROOT / "tools" / "make_pcm_variant.py", tmp / "tools" / "make_pcm_variant.py")
        rc, out, err = sh([app_python(), "tools/make_pcm_variant.py"], cwd=tmp, timeout=120)
        if rc != 0:
            return False, f"generator failed: {(out + err)[-500:]}"
        want = (tmp / "main_pcm.py").read_text(encoding="utf-8").replace("\r\n", "\n")
        have = (ROOT / "main_pcm.py").read_text(encoding="utf-8").replace("\r\n", "\n")
        return (
            want == have,
            "" if want == have else "main_pcm.py differs from `python tools/make_pcm_variant.py` output",
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_unexpected_files(ctx):
    cfg, problems = ctx.cfg, []
    for f in ctx.changed:
        if matches(f, cfg["forbidden_file_globs"]):
            problems.append(
                {"file": f, "reason": "forbidden file type (data, audio, model, key, archive, patch, log)"}
            )
        try:
            size = (ROOT / f).stat().st_size
        except OSError:
            size = 0
        if size > cfg["max_file_bytes"]:
            problems.append({"file": f, "reason": f"{size} bytes exceeds max_file_bytes"})
        top = f.split("/", 1)[0]
        if top not in cfg["allowed_top_level"] and not matches(top, cfg["allowed_top_level"]):
            problems.append({"file": f, "reason": f"unexpected top-level entry `{top}`"})
    if ctx.mode == "fast" and ("main_pcm.py" in ctx.changed or "main.py" in ctx.changed):
        ok, msg = _generator_in_sync()
        if not ok:
            problems.append({"file": "main_pcm.py", "reason": msg})
    if problems:
        return _res("unexpected-files", STATUS_FAIL, f"{len(problems)} unexpected change(s)", problems)
    return _res("unexpected-files", STATUS_PASS, f"{len(ctx.changed)} changed file(s), none unexpected")


def check_dependency_audit(ctx):
    ok, counts, examples, err = collect_dependencies(ctx.cfg)
    if not ok:
        return _res("dependency-audit", STATUS_ERROR, "dependency audit could not complete", details=err)
    return _ratchet_result(
        "dependency-audit", counts, ctx.baseline.get("dependencies", {}), ctx, "dependency issue(s)", examples
    )


def check_build(ctx):
    problems = []
    ok, msg = _generator_in_sync()
    if not ok:
        problems.append({"step": "generated-file sync", "error": msg})
    rc, out, err = sh([app_python(), "scripts/gate_contracts.py", "--import-smoke"], timeout=600)
    try:
        smoke = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        smoke = {"ok": False, "problems": [f"import smoke produced no result: {(out + err)[-800:]}"]}
    problems += [{"step": "import smoke", "error": p} for p in smoke.get("problems", [])]
    if problems:
        return _res("build", STATUS_FAIL, f"{len(problems)} build problem(s)", problems)
    return _res(
        "build",
        STATUS_PASS,
        "main_pcm.py in sync with generator; all three apps import with their routes",
        metrics={"routes": smoke.get("routes")},
    )


def check_api_contract(ctx):
    relevant = [
        "clinic-api/*.py",
        "agent/tools_client.py",
        "main.py",
        "main_pcm.py",
        "scripts/gate-contracts/*",
    ]
    if ctx.mode == "fast" and not any(matches(f, relevant) for f in ctx.changed):
        return _res("api-contract", STATUS_PASS, "no API-surface file changed")
    rc, out, err = sh([app_python(), "scripts/gate_contracts.py", "--check"], timeout=600)
    try:
        result = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return _res("api-contract", STATUS_ERROR, "contract check produced no result", details=out + err)
    if not result.get("ok"):
        return _res(
            "api-contract",
            STATUS_FAIL,
            f"{len(result['problems'])} contract problem(s)",
            [{"problem": p} for p in result["problems"]],
        )
    return _res(
        "api-contract",
        STATUS_PASS,
        f"contracts match snapshots; {result.get('client_calls_verified')} client call(s) verified",
        metrics={"operations": result.get("operations")},
    )


def check_gate_protection(ctx):
    analysis = gate_protect.analyze(ctx.base, ctx.changed, ctx.deleted)
    (GATE_DIR / "protect.json").write_text(json.dumps(analysis, indent=2), encoding="utf-8")
    findings = [{"type": "tests_weakened", "reason": r} for r in analysis["tests_weakened_reasons"]] + [
        {"type": "bypass", "reason": r} for r in analysis["bypass_reasons"]
    ]
    metrics = {
        k: analysis[k]
        for k in ("gate_config_touched", "gate_config_files", "tests_weakened", "bypass_detected", "info")
    }
    if findings:
        return _res(
            "gate-protection",
            STATUS_FAIL,
            f"{len(findings)} attempt(s) to weaken the gate",
            findings,
            metrics,
        )
    touched = (
        f"; gate configuration touched ({len(analysis['gate_config_files'])} file(s))"
        if analysis["gate_config_touched"]
        else ""
    )
    return _res("gate-protection", STATUS_PASS, f"no weakening detected{touched}", [], metrics)


# ---------------------------------------------------------------------------
# Test suites: one pytest run, many verdicts
# ---------------------------------------------------------------------------
def _nodeid(classname: str, name: str) -> str:
    parts = classname.split(".") if classname else []
    for i in range(len(parts), 0, -1):
        candidate = "/".join(parts[:i]) + ".py"
        if (ROOT / candidate).exists():
            return "::".join([candidate, *parts[i:], name])
    return f"{classname}::{name}"


def run_pytest(ctx) -> dict:
    with ctx._lock:
        if ctx._pytest is None:
            junit = GATE_DIR / "junit.xml"
            junit.unlink(missing_ok=True)
            rc, out, err = sh(
                [
                    app_python(),
                    "-m",
                    "pytest",
                    "tests",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    f"--junitxml={junit}",
                    "-o",
                    "junit_family=xunit2",
                    "-rs",
                ],
                timeout=2700,
            )
            cases = []
            if junit.exists():
                for tc in ET.parse(junit).getroot().iter("testcase"):
                    outcome = "passed"
                    for child in tc:
                        if child.tag in ("failure", "error"):
                            outcome = "failed" if child.tag == "failure" else "error"
                        elif child.tag == "skipped":
                            outcome = "skipped"
                    cases.append(
                        {"nodeid": _nodeid(tc.get("classname", ""), tc.get("name", "")), "outcome": outcome}
                    )
            summary = collections.Counter(c["outcome"] for c in cases)
            ctx._pytest = {"rc": rc, "cases": cases, "tail": (out + err)[-4000:], "summary": dict(summary)}
            (GATE_DIR / "pytest-summary.json").write_text(
                json.dumps(
                    {
                        "rc": rc,
                        "total": len(cases),
                        **{k: summary.get(k, 0) for k in ("passed", "failed", "error", "skipped")},
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
    return ctx._pytest


def make_suite_check(sid):
    def check(ctx):
        run = run_pytest(ctx)
        spec = ctx.cfg["suites"][sid]
        if not run["cases"]:
            return _res(
                sid, STATUS_ERROR, f"pytest produced no results (rc={run['rc']})", details=run["tail"]
            )
        chosen = [
            c
            for c in run["cases"]
            if matches(c["nodeid"], spec["select"]) and not matches(c["nodeid"], spec.get("exclude", []))
        ]
        bad = [c for c in chosen if c["outcome"] in ("failed", "error")]
        skipped = [c for c in chosen if c["outcome"] == "skipped"]
        metrics = {
            "tests": len(chosen),
            "passed": len(chosen) - len(bad) - len(skipped),
            "failed": len(bad),
            "skipped": len(skipped),
            "min_tests": spec.get("min_tests", 1),
        }
        findings = [{"test": c["nodeid"], "outcome": c["outcome"]} for c in bad + skipped]
        if sid == "unit-tests" and run["rc"] not in (0, 1):
            return _res(
                sid,
                STATUS_ERROR,
                f"pytest exited {run['rc']} (collection or internal error)",
                findings,
                metrics,
                run["tail"],
            )
        if bad or skipped:
            return _res(
                sid,
                STATUS_FAIL,
                f"{len(bad)} failed, {len(skipped)} skipped of {len(chosen)}",
                findings,
                metrics,
                run["tail"],
            )
        if len(chosen) < max(1, spec.get("min_tests", 1)):
            return _res(
                sid,
                STATUS_FAIL,
                f"only {len(chosen)} test(s) selected; suite requires "
                f"{spec.get('min_tests', 1)} -- tests were removed or renamed",
                [],
                metrics,
            )
        return _res(sid, STATUS_PASS, f"{len(chosen)} passed", [], metrics)

    return check


CHECKS = {
    "compile": check_compile,
    "format": check_format,
    "lint": check_lint,
    "typecheck": check_typecheck,
    "dead-code": check_dead_code,
    "static-analysis": check_static,
    "secrets": check_secrets,
    "prod-credentials": check_prod_credentials,
    "dependency-audit": check_dependency_audit,
    "build": check_build,
    "api-contract": check_api_contract,
    "debug-code": check_debug_code,
    "unexpected-files": check_unexpected_files,
    "phi-in-code": check_phi_in_code,
    "phi-in-logs": check_phi_in_logs,
    "approved-test-data": check_approved_test_data,
    "gate-protection": check_gate_protection,
}


def run(mode: str) -> int:
    os.chdir(ROOT)
    if RESULTS_DIR.exists():
        shutil.rmtree(RESULTS_DIR)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (GATE_DIR / "pytest-summary.json").unlink(missing_ok=True)
    ctx = Context(mode)
    (GATE_DIR / "context.json").write_text(
        json.dumps(
            {
                "mode": mode,
                "base": ctx.base,
                "head": gate_lib.head_sha(),
                "changed": ctx.changed,
                "deleted": ctx.deleted,
                "ci": gate_lib.is_ci(),
                "started_at": gate_lib.now_iso(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    log(
        f"[gate] {mode} gate: base {ctx.base[:12]}, {len(ctx.changed)} changed, "
        f"{len(ctx.deleted)} deleted file(s)"
    )

    selected = []
    for cid, spec in ctx.cfg["checks"].items():
        if mode not in spec.get("modes", []):
            gate_lib.write_result(_res(cid, STATUS_SKIP, f"not part of the {mode} gate"))
            continue
        selected.append(cid)

    def execute(cid):
        fn = CHECKS.get(cid) or (make_suite_check(cid) if cid in ctx.cfg.get("suites", {}) else None)
        with Timer() as t:
            if fn is None:
                res = _res(cid, STATUS_ERROR, "no implementation for this check id")
            else:
                try:
                    res = fn(ctx)
                except Exception as e:  # a crashing check is a failing check
                    res = _res(
                        cid,
                        STATUS_ERROR,
                        f"check crashed: {type(e).__name__}: {e}",
                        details=traceback.format_exc(),
                    )
        res.duration_s = t.elapsed
        gate_lib.write_result(res)
        log(f"[gate] {cid:22} {res.status.upper():5} {t.elapsed:7.1f}s  {res.summary}")
        return res

    workers = max(1, int(os.environ.get("GATE_JOBS", "6")))
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(execute, selected))
    return 0


def write_baseline() -> int:
    """Record today's violations as the adoption baseline. Running this is a
    gate-configuration change and is flagged as one."""
    os.chdir(ROOT)
    ctx = Context("full")
    files = ctx.all_files
    out = {
        "version": 1,
        "generated_at": gate_lib.now_iso(),
        "note": "Adoption baseline. Author: Chakravardhan. Counts may only go DOWN; any increase is "
        "detected as loosening the gate. Regenerate only in a reviewed gate-config change.",
    }
    ok, unformatted, err = collect_format(ctx.py(files))
    out["format_unformatted"] = unformatted
    for key, fn in (
        ("lint", lambda: collect_lint(ctx.py(files))),
        ("typecheck", lambda: collect_typecheck(ctx.cfg)),
        ("dead_code", lambda: collect_dead_code(ctx.cfg, ctx.py(files))),
        ("bandit", lambda: collect_bandit(ctx.source_py(files))),
        ("secrets", lambda: collect_secrets(files, ctx.base)),
        ("dependencies", lambda: collect_dependencies(ctx.cfg)),
    ):
        ok, counts, _examples, err = fn()
        if not ok:
            log(f"[baseline] {key} FAILED: {err}")
            return 1
        out[key] = dict(sorted(counts.items()))
        log(f"[baseline] {key}: {sum(counts.values())}")
    out["phi_logs"] = dict(
        sorted(collections.Counter(f["key"] for f in gate_scan.phi_in_logs(files)).items())
    )
    out["debug"] = dict(
        sorted(
            collections.Counter(
                f"{f['file']}|{f['kind']}" for f in gate_scan.debug_code(files, ctx.cfg["cli_files"])
            ).items()
        )
    )
    out["suppressions"] = dict(sorted(gate_scan.suppressions(files).items()))
    gate_lib.BASELINE_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    log(f"[baseline] written to {rel(gate_lib.BASELINE_PATH)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--mode", choices=("fast", "full"))
    g.add_argument("--write-baseline", action="store_true")
    args = ap.parse_args()
    return write_baseline() if args.write_baseline else run(args.mode)


if __name__ == "__main__":
    sys.exit(main())
