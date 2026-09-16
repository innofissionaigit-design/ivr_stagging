"""Shared plumbing for the pre-human-review quality gate.

Author: Chakravardhan

Every gate script (scripts/gate*.py) imports this module and nothing else of
the repository's own code, so the gate can be run -- and audited -- without
importing the application it is judging.

TWO ROOTS, ON PURPOSE
---------------------
`ROOT` is the working tree being judged. `CONFIG_DIR` is where the gate's own
configuration is read from. They are the same directory for a normal run, and
DIFFERENT in the label guard: there the configuration is read from a trusted
checkout of the default branch while the files being judged are the pull
request's. A pull request must never be able to hand the gate a more lenient
rulebook alongside the change it wants waved through.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import fnmatch
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("GATE_REPO") or Path(__file__).resolve().parent.parent).resolve()
CONFIG_DIR = Path(os.environ.get("GATE_CONFIG_DIR") or ROOT / "scripts").resolve()
GATE_DIR = ROOT / ".gate"
RESULTS_DIR = GATE_DIR / "results"

CONFIG_PATH = CONFIG_DIR / "gate-config.json"
BASELINE_PATH = CONFIG_DIR / "gate-baseline.json"
APPROVED_PATH = CONFIG_DIR / "gate-approved-test-data.json"
RUFF_CONFIG = CONFIG_DIR / "gate-ruff.toml"
MYPY_CONFIG = CONFIG_DIR / "gate-mypy.ini"
GITLEAKS_CONFIG = CONFIG_DIR / "gate-gitleaks.toml"
CONTRACTS_DIR = CONFIG_DIR / "gate-contracts"

# Git's well-known empty tree. Used as the base when there is no usable base
# commit, so "everything is new" rather than "nothing changed".
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_ERROR = "error"  # the check could not run -- counts as a failure
STATUS_SKIP = "skip"  # not part of this mode


def load_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        if default is not None:
            return default
        raise


def config() -> dict:
    return load_json(CONFIG_PATH)


def baseline() -> dict:
    return load_json(BASELINE_PATH, {})


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def is_ci() -> bool:
    return os.environ.get("GATE_CI") == "1"


def sh(
    cmd,
    *,
    cwd: Path | None = None,
    timeout: float = 900,
    env: dict | None = None,
    input_text: str | None = None,
) -> tuple[int, str, str]:
    """Run a command. Never raises: a missing tool or a timeout comes back as
    a return code, so the caller can record it as a check ERROR -- which the
    gate treats as a failure, never as a pass."""
    try:
        proc = subprocess.run(
            [str(c) for c in cmd],
            cwd=str(cwd or ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            input=input_text,
            env={**os.environ, **(env or {})},
        )
    except FileNotFoundError as e:
        return 127, "", f"not found: {e}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    return proc.returncode, proc.stdout, proc.stderr


def git(*args, cwd: Path | None = None) -> tuple[int, str]:
    rc, out, err = sh(["git", *args], cwd=cwd, timeout=120)
    return rc, out if rc == 0 else err


def head_sha() -> str:
    rc, out = git("rev-parse", "HEAD")
    return out.strip() if rc == 0 else "unknown"


def resolve_base() -> str:
    """-> the commit (or empty tree) changes are measured against.

    Local runs (G0) measure against HEAD: the developer commits, Claude does
    not, so HEAD is the last state a human signed off and everything
    uncommitted is what is under review. CI runs (G1) set GATE_BASE_REF to
    the pull request's base, and the merge-base is used so commits that
    landed on the base branch after the fork are not blamed on the PR.
    """
    ref = os.environ.get("GATE_BASE_REF", "").strip() or "HEAD"
    if ref == "HEAD":
        rc, out = git("rev-parse", "HEAD")
        return out.strip() if rc == 0 else EMPTY_TREE
    if set(ref) == {"0"}:  # a push that created the branch
        ref = "origin/main"
    rc, out = git("merge-base", ref, "HEAD")
    if rc == 0 and out.strip():
        return out.strip()
    rc, out = git("rev-parse", "--verify", f"{ref}^{{commit}}")
    return out.strip() if rc == 0 else EMPTY_TREE


def _lines(text: str) -> list[str]:
    return [line.strip().replace("\\", "/") for line in text.splitlines() if line.strip()]


def changed_files(base: str) -> list[str]:
    """Paths added, modified or renamed relative to `base`, INCLUDING
    untracked files -- a file Claude just created is a change even though
    git does not know about it yet. Deleted paths are in deleted_files()."""
    names: set[str] = set()
    rc, out = git("diff", "--name-only", "--no-renames", "--diff-filter=ACMRT", base)
    if rc == 0:
        names.update(_lines(out))
    rc, out = git("ls-files", "--others", "--exclude-standard")
    if rc == 0:
        names.update(_lines(out))
    return sorted(n for n in names if (ROOT / n).exists())


def deleted_files(base: str) -> list[str]:
    rc, out = git("diff", "--name-only", "--no-renames", "--diff-filter=D", base)
    return sorted(_lines(out)) if rc == 0 else []


def repo_files() -> list[str]:
    """Every tracked or untracked-but-not-ignored file that exists."""
    rc, out = git("ls-files", "--cached", "--others", "--exclude-standard")
    if rc != 0:
        return []
    return sorted({p for p in _lines(out) if (ROOT / p).is_file()})


def file_at(ref: str, path: str) -> str | None:
    """Contents of `path` at `ref`, or None if it did not exist there."""
    if ref == EMPTY_TREE:
        return None
    rc, out, _ = sh(["git", "show", f"{ref}:{path}"], timeout=60)
    return out if rc == 0 else None


def files_at(ref: str, prefix: str = "") -> list[str]:
    if ref == EMPTY_TREE:
        return []
    args = ["ls-tree", "-r", "--name-only", ref]
    if prefix:
        args += ["--", prefix]
    rc, out = git(*args)
    return _lines(out) if rc == 0 else []


def matches(path: str, patterns) -> bool:
    path = path.replace("\\", "/")
    return any(fnmatch.fnmatchcase(path, p) for p in patterns)


def read_text(path: str | Path, max_bytes: int = 2_000_000) -> str | None:
    """Text of a repo file, or None for binaries and oversized files."""
    p = ROOT / path if not Path(path).is_absolute() else Path(path)
    try:
        data = p.read_bytes()
    except OSError:
        return None
    if len(data) > max_bytes or b"\x00" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def rel(path: str | Path) -> str:
    p = Path(path)
    try:
        p = p.resolve().relative_to(ROOT)
    except (ValueError, OSError):
        pass
    return str(p).replace("\\", "/")


def tool_python() -> str:
    """The interpreter the static-analysis tools are installed in."""
    return os.environ.get("GATE_TOOL_PYTHON") or sys.executable


def app_python() -> str:
    """The interpreter the application (and its tests) run under."""
    return os.environ.get("GATE_APP_PYTHON") or sys.executable


def bash_exe() -> str:
    return os.environ.get("GATE_BASH") or "bash"


@dataclasses.dataclass
class CheckResult:
    id: str
    status: str
    summary: str
    findings: list = dataclasses.field(default_factory=list)
    metrics: dict = dataclasses.field(default_factory=dict)
    details: str = ""
    duration_s: float = 0.0

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def write_result(res: CheckResult) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / f"{res.id}.json").write_text(
        json.dumps(res.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )


def read_results() -> list[dict]:
    if not RESULTS_DIR.exists():
        return []
    return [load_json(p) for p in sorted(RESULTS_DIR.glob("*.json"))]


class Timer:
    def __enter__(self):
        self.t0 = time.monotonic()
        return self

    def __exit__(self, *exc):
        self.elapsed = round(time.monotonic() - self.t0, 2)
        return False


def ratchet(current: dict[str, int], allowed: dict[str, int], scope=None) -> tuple[list, list]:
    """Compare violation counts against the adoption baseline.

    -> (regressions, improvements). A key may never exceed its baseline
    count; a key absent from the baseline is allowed zero. `scope`, when
    given, limits the comparison to keys whose file part is in it -- the
    fast gate looks only at the files that changed.

    The baseline only ever shrinks. Raising a number in it is detected by
    gate_protect.py as loosening the gate, not accepted as a fix.
    """
    regressions, improvements = [], []
    keys = set(current) | set(allowed)
    for key in sorted(keys):
        if scope is not None and key.split("|", 1)[0] not in scope:
            continue
        cur, base = current.get(key, 0), allowed.get(key, 0)
        if cur > base:
            regressions.append({"key": key, "count": cur, "baseline": base})
        elif cur < base:
            improvements.append({"key": key, "count": cur, "baseline": base})
    return regressions, improvements
