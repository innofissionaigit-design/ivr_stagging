"""Healthcare-specific scanners for the quality gate.

Author: Chakravardhan

Ordinary linting cannot see the failures that matter most on a clinic phone
line: a patient's number pasted into a fixture, a transcript written to a log,
a live gateway token in a deploy script. Each scanner here looks for one of
those, and each is deterministic -- regular expressions and the Python AST,
nothing probabilistic -- so the same tree always gives the same answer.

VALUES ARE MASKED IN EVERY FINDING. A scanner that reported the phone number
it found would copy PHI into gate-report.json, a CI artifact, and the pull
request -- the exact leak it exists to stop.

Pattern literals that would match this file's own source (the markers for
unfinished work, console debugging and suppression directives) are assembled
by concatenation, so the scanner does not flag itself and does not need an
exemption to avoid it.
"""

from __future__ import annotations

import ast
import json
import re

from gate_lib import matches, read_text

TEXT_EXTS = (
    ".py",
    ".js",
    ".html",
    ".sh",
    ".yml",
    ".yaml",
    ".toml",
    ".ini",
    ".cfg",
    ".json",
    ".md",
    ".txt",
    ".env",
    ".csv",
)
CODE_EXTS = (".py", ".js", ".html", ".sh", ".yml", ".yaml", ".toml", ".ini", ".cfg")


def is_test_path(path: str) -> bool:
    return path.startswith("tests/") or "/tests/" in path


def text_files(files, exts=TEXT_EXTS):
    for f in files:
        if f.endswith(exts) or f.rsplit("/", 1)[-1].startswith(".env"):
            text = read_text(f)
            if text is not None:
                yield f, text


def mask(value: str) -> str:
    if "@" in value:
        local, _, domain = value.partition("@")
        return f"{local[:1]}***@{domain}"
    keep = 2 if len(value) > 4 else 0
    return "*" * (len(value) - keep) + value[len(value) - keep :]


# ===========================================================================
# PHI patterns
# ===========================================================================
PHONE_RE = re.compile(r"(?<!\w)(?:\+?91[ -]?)?([6-9]\d{9})(?!\w)")
AADHAAR_RE = re.compile(r"(?<!\w)([2-9]\d{3})[ -]?(\d{4})[ -]?(\d{4})(?!\w)")
PAN_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z]{3}[PCHFATBLJG][A-Z]\d{4}[A-Z](?![A-Za-z0-9])")
EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
ABHA_RE = re.compile(r"(?<!\d)\d{2}-\d{4}-\d{4}-\d{4}(?!\d)")

# Verhoeff tables: every genuine Aadhaar number carries a Verhoeff check
# digit, so requiring it removes ~90% of random 12-digit false positives
# without missing a single real one.
_VD = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 2, 3, 4, 0, 6, 7, 8, 9, 5],
    [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
    [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
    [4, 0, 1, 2, 3, 9, 5, 6, 7, 8],
    [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2],
    [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
    [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
    [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]
_VP = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 5, 7, 6, 2, 8, 3, 0, 9, 4],
    [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
    [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
    [9, 4, 5, 3, 1, 2, 6, 8, 7, 0],
    [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5],
    [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]


def verhoeff_valid(digits: str) -> bool:
    c = 0
    for i, ch in enumerate(reversed(digits)):
        c = _VD[c][_VP[i % 8][int(ch)]]
    return c == 0


def phone_approved(number: str, approved: dict) -> bool:
    if number in approved.get("phones", []):
        return True
    return any(lo <= number <= hi for lo, hi in approved.get("phone_ranges", []))


def email_approved(domain: str, approved: dict) -> bool:
    domain = domain.lower()
    return any(domain == d or domain.endswith("." + d) for d in approved.get("email_domains", []))


def phi_in_text(path: str, text: str, approved: dict) -> list[dict]:
    out = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for m in PHONE_RE.finditer(line):
            if not phone_approved(m.group(1), approved):
                out.append({"file": path, "line": lineno, "kind": "phone", "value": mask(m.group(1))})
        for m in AADHAAR_RE.finditer(line):
            digits = "".join(m.groups())
            if digits.startswith("91") and digits[2] in "6789":
                continue  # a +91 mobile number, handled above
            if verhoeff_valid(digits):
                out.append({"file": path, "line": lineno, "kind": "aadhaar", "value": mask(digits)})
        for m in PAN_RE.finditer(line):
            out.append({"file": path, "line": lineno, "kind": "pan", "value": mask(m.group(0))})
        for m in EMAIL_RE.finditer(line):
            if not email_approved(m.group(1), approved):
                out.append({"file": path, "line": lineno, "kind": "email", "value": mask(m.group(0))})
        for m in ABHA_RE.finditer(line):
            out.append({"file": path, "line": lineno, "kind": "abha", "value": mask(m.group(0))})
    return out


def phi_in_code(files, approved: dict) -> list[dict]:
    """Real-looking identifiers anywhere OUTSIDE the tests. Production code,
    docs and deploy scripts have no reason to hold a patient identifier at
    all, so there is no baseline for this: any finding fails."""
    out = []
    for path, text in text_files([f for f in files if not is_test_path(f)]):
        out += phi_in_text(path, text, approved)
    return out


# ===========================================================================
# Approved test data
# ===========================================================================
_NAME_KEYS = {"patient_name", "full_name", "caller_name", "patient"}
_DOB_KEYS = {"date_of_birth", "dob"}


def _const_str(node) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _identity_literals(tree: ast.AST):
    """(kind, value, lineno) for every literal patient name / date of birth."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                value = _const_str(kw.value)
                if kw.arg in _NAME_KEYS and value is not None:
                    yield "name", value, node.lineno
                elif kw.arg in _DOB_KEYS and value is not None:
                    yield "dob", value, node.lineno
        elif isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values, strict=True):
                key, value = _const_str(k), _const_str(v)
                if key in _NAME_KEYS and value is not None:
                    yield "name", value, v.lineno
                elif key in _DOB_KEYS and value is not None:
                    yield "dob", value, v.lineno
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            value = _const_str(node.value)
            if isinstance(target, ast.Name) and value is not None:
                if target.id.lower() in _DOB_KEYS:
                    yield "dob", value, node.lineno


def _json_identity(obj, found: list):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and k in _NAME_KEYS:
                found.append(("name", v))
            elif isinstance(v, str) and k in _DOB_KEYS:
                found.append(("dob", v))
            else:
                _json_identity(v, found)
    elif isinstance(obj, list):
        for v in obj:
            _json_identity(v, found)


def approved_test_data(files, approved: dict, data_paths) -> list[dict]:
    """Every identifier in test data must come from the approved fictional
    set. Real patient data must never be used to make a test realistic."""
    out = []
    targets = [f for f in files if is_test_path(f) or matches(f, data_paths)]
    for path, text in text_files(targets):
        out += phi_in_text(path, text, approved)
        literals = []
        if path.endswith(".py"):
            try:
                literals = list(_identity_literals(ast.parse(text)))
            except SyntaxError:
                continue
        elif path.endswith(".json"):
            found: list = []
            try:
                _json_identity(json.loads(text), found)
            except ValueError:
                pass
            literals = [(k, v, 0) for k, v in found]
        for kind, value, lineno in literals:
            pool = approved.get("names" if kind == "name" else "dates_of_birth", [])
            if value not in pool:
                out.append({"file": path, "line": lineno, "kind": f"unapproved-{kind}", "value": mask(value)})
    return out


# ===========================================================================
# PHI reaching logs
# ===========================================================================
_LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log", "fatal"}
PHI_IDENTIFIERS = {
    "phone",
    "phone_number",
    "mobile",
    "patient_name",
    "full_name",
    "date_of_birth",
    "dob",
    "pin",
    "answer",
    "token",
    "history_token",
    "transcript",
    "patient",
    "body",
    "address",
    "aadhaar",
}


def _is_logger(expr: ast.AST) -> bool:
    if isinstance(expr, ast.Name):
        return "log" in expr.id.lower()
    if isinstance(expr, ast.Attribute):
        return "log" in expr.attr.lower() or _is_logger(expr.value)
    if isinstance(expr, ast.Call):
        func = expr.func
        return isinstance(func, (ast.Attribute, ast.Name)) and "getlogger" in ast.unparse(func).lower()
    return False


def _phi_refs(nodes) -> set[str]:
    refs = set()
    for root in nodes:
        for n in ast.walk(root):
            if isinstance(n, ast.Name) and n.id.lower() in PHI_IDENTIFIERS:
                refs.add(n.id)
            elif isinstance(n, ast.Attribute) and n.attr.lower() in PHI_IDENTIFIERS:
                refs.add(n.attr)
            elif isinstance(n, ast.Subscript) and _const_str(n.slice) in PHI_IDENTIFIERS:
                refs.add(_const_str(n.slice))
    return refs


def _enclosing_functions(tree: ast.AST) -> dict[int, str]:
    owner: dict[int, str] = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for n in ast.walk(fn):
                if hasattr(n, "lineno"):
                    owner.setdefault(id(n), fn.name)
    return owner


def phi_in_logs(files) -> list[dict]:
    """A PHI-bearing identifier passed to a logger or print(), or baked into
    an exception message (which main.py logs verbatim). Keyed by file,
    function and identifier -- not line -- so unrelated edits do not churn
    the baseline."""
    out = []
    for path, text in text_files([f for f in files if f.endswith(".py") and not is_test_path(f)]):
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        owner = _enclosing_functions(tree)
        for node in ast.walk(tree):
            kind, args = None, []
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr in _LOG_METHODS and _is_logger(func.value):
                    kind, args = "log", list(node.args) + [k.value for k in node.keywords]
                elif isinstance(func, ast.Name) and func.id == "print":
                    kind, args = "print", list(node.args)
            elif isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
                kind, args = "exception-message", list(node.exc.args)
            if not kind:
                continue
            for ident in sorted(_phi_refs(args)):
                out.append(
                    {
                        "file": path,
                        "line": node.lineno,
                        "kind": kind,
                        "function": owner.get(id(node), "<module>"),
                        "identifier": ident,
                        "key": f"{path}|{owner.get(id(node), '<module>')}|{kind}:{ident}",
                    }
                )
    return out


# ===========================================================================
# Production credentials
# ===========================================================================
_CRED_RULES = [
    (
        "private-key",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP |ENCRYPTED )?PRIVATE KEY-----"),
        True,
    ),
    ("aws-access-key", re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])"), True),
    ("anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), True),
    ("openai-key", re.compile(r"(?<![\w-])sk-(?:proj-)?[A-Za-z0-9]{32,}"), True),
    ("github-token", re.compile(r"(?<!\w)(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})"), True),
    ("slack-token", re.compile(r"(?<!\w)xox[abprs]-[A-Za-z0-9-]{10,}"), True),
    ("huggingface-token", re.compile(r"(?<!\w)hf_[A-Za-z0-9]{30,}"), True),
    ("google-api-key", re.compile(r"(?<!\w)AIza[0-9A-Za-z_\-]{35}"), True),
    ("jwt", re.compile(r"(?<![\w-])eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), True),
    ("url-with-password", re.compile(r"[a-z][a-z0-9+.-]*://[^\s:/@'\"]+:([^\s@'\"/]+)@[^\s'\"]+"), False),
]
_SECRET_NAME = (
    r"[A-Za-z0-9_]*(?:API_?KEY|APIKEY|SECRET|TOKEN|PASSWORD|PASSWD|PRIVATE_KEY|ACCESS_KEY)[A-Za-z0-9_]*"
)
_QUOTED_ASSIGN = re.compile(r"(?i)\b(" + _SECRET_NAME + r")\b\s*[:=]\s*['\"]([^'\"\s]{12,})['\"]")
_SHELL_ASSIGN = re.compile(r"(?i)^\s*(?:export\s+)?(" + _SECRET_NAME + r")=([^\s'\"#;]{12,})")
_PLACEHOLDER = re.compile(
    r"(?i)^(\$|<|\{\{|x{4,}|\*{3,}|your[-_]|example|dummy|placeholder|changeme|redacted)"
    r"|x{4,}|\.\.\."
)


def _is_placeholder(value: str) -> bool:
    return bool(_PLACEHOLDER.search(value)) or len(set(value)) <= 2


def prod_credentials(files) -> list[dict]:
    """Credentials that would work against a real system. Tests may hold
    obviously fake tokens, so the generic `SECRET = "..."` rule is skipped
    there -- but a string shaped like a real provider key is flagged
    wherever it appears. No baseline: a committed credential must be
    rotated, not grandfathered."""
    out = []
    for path, text in text_files(files):
        in_tests = is_test_path(path)
        shellish = path.endswith((".sh", ".env")) or path.rsplit("/", 1)[-1].startswith(".env")
        for lineno, line in enumerate(text.splitlines(), 1):
            for rule, rx, everywhere in _CRED_RULES:
                if in_tests and not everywhere:
                    continue
                m = rx.search(line)
                if not m:
                    continue
                secret = m.group(1) if rx.groups else m.group(0)
                if rule == "url-with-password" and _is_placeholder(secret):
                    continue
                out.append({"file": path, "line": lineno, "kind": rule, "value": mask(secret)})
            if in_tests:
                continue
            for rx in [_QUOTED_ASSIGN, _SHELL_ASSIGN] if shellish else [_QUOTED_ASSIGN]:
                m = rx.search(line)
                if m and not _is_placeholder(m.group(2)):
                    out.append(
                        {
                            "file": path,
                            "line": lineno,
                            "kind": f"hardcoded:{m.group(1)}",
                            "value": mask(m.group(2)),
                        }
                    )
    return out


# ===========================================================================
# Debug code, stray console output, untracked work markers
# ===========================================================================
_WORK_MARKER = re.compile(
    r"\b(" + "TO" + "DO" + "|" + "FIX" + "ME" + "|" + "X" + "XX" + "|" + "HA" + "CK" + r")\b"
)
_TRACKED_MARKER = re.compile(r"\((?:#\d+|[A-Z][A-Z0-9]+-\d+|https?://[^)]+)\)")
_CONSOLE = re.compile(r"\bconsole\." + r"(?:log|debug|trace|dir)\s*\(")
_JS_DEBUGGER = re.compile(r"(?<![\w.])" + "debug" + r"ger\s*;")


def debug_code(files, cli_globs) -> list[dict]:
    out = []
    for path, text in text_files([f for f in files if f.endswith(CODE_EXTS)]):
        in_tests = is_test_path(path)
        for lineno, line in enumerate(text.splitlines(), 1):
            m = _WORK_MARKER.search(line)
            if m and not _TRACKED_MARKER.search(line):
                out.append({"file": path, "line": lineno, "kind": "untracked-work-marker"})
            if path.endswith((".js", ".html")):
                if _CONSOLE.search(line):
                    out.append({"file": path, "line": lineno, "kind": "console-output"})
                if _JS_DEBUGGER.search(line):
                    out.append({"file": path, "line": lineno, "kind": "js-debugger"})
        if not path.endswith(".py"):
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "breakpoint":
                    out.append({"file": path, "line": node.lineno, "kind": "breakpoint"})
                elif node.func.id == "print" and not in_tests and not matches(path, cli_globs):
                    out.append({"file": path, "line": node.lineno, "kind": "print"})
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                if any(n.split(".")[0] in ("pdb", "ipdb", "pudb") for n in names):
                    out.append({"file": path, "line": node.lineno, "kind": "debugger-import"})
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "set_trace"
            ):
                out.append({"file": path, "line": node.lineno, "kind": "set-trace"})
    return out


# ===========================================================================
# Suppression directives
# ===========================================================================
SUPPRESSION_RE = re.compile(
    r"#\s*(?:" + "no" + r"qa\b|" + "type" + r":\s*ignore|" + "no" + r"sec\b|pragma:\s*no\s*cover"
    r"|fmt:\s*(?:off|skip)|ruff:\s*" + "no" + r"qa|mypy:\s*ignore|pylint:\s*disable"
    r"|pyright:\s*ignore|vulture:\s*ignore|shellcheck\s+disable)"
    r"|" + "gitleaks" + r":allow|eslint-" + "disable|@ts-" + r"(?:ignore|nocheck)|NO" + "SONAR"
)


def suppressions(files) -> dict[str, int]:
    """Suppression directives per file. The gate never asks WHY a directive
    was added -- only whether the count went up. Every new one is a human
    decision to be made in review, not something to slip past a gate."""
    counts: dict[str, int] = {}
    for path, text in text_files([f for f in files if f.endswith(CODE_EXTS)]):
        n = len(SUPPRESSION_RE.findall(text))
        if n:
            counts[path] = n
    return counts
