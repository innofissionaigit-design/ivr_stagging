"""API contract validation and import smoke test.

Author: Chakravardhan

Three services speak HTTP to each other here -- the voice agent calls
clinic-api through agent/tools_client.py, and both expose their own REST
surface -- and nothing checked that the two sides still agree. A renamed
query parameter in clinic-api would pass every unit test and fail on the
first real call.

  --check        compare each app's normalised OpenAPI contract with the
                 committed snapshot in scripts/gate-contracts/, and check
                 every endpoint the voice agent calls exists in clinic-api
  --update       rewrite the snapshots (a gate-configuration change: it
                 sets gate_config_touched and needs owner review)
  --import-smoke import both apps and assert their key routes exist

Normalisation keeps what a caller depends on -- method, path, parameter
names, where they go and whether they are required, request-body fields --
and drops what varies harmlessly between FastAPI versions.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import os
import re
import sys
import tempfile
import types

from gate_lib import CONTRACTS_DIR, ROOT

APPS = {
    "clinic-api": ("clinic_api_main_contract", ROOT / "clinic-api" / "main.py", [ROOT / "clinic-api"]),
    "voice-agent-webm": ("main", ROOT / "main.py", [ROOT]),
    "voice-agent-pcm": ("main_pcm", ROOT / "main_pcm.py", [ROOT]),
}
REQUIRED_ROUTES = {
    "clinic-api": ["/api/health", "/api/v1/catalogue", "/api/v1/appointments"],
    "voice-agent-webm": ["/ws/audio", "/api/health", "/api/audit/calls"],
    "voice-agent-pcm": ["/ws/audio", "/api/health", "/api/audit/calls"],
}


def _install_stubs() -> None:
    """The same stand-ins tests/test_speakerphone.py uses: NeMo and
    torchaudio are GPU-pod dependencies and are not needed to read a
    route table."""
    for name in (
        "nemo",
        "nemo.collections",
        "nemo.collections.asr",
        "nemo.collections.asr.parts",
        "nemo.collections.asr.parts.submodules",
        "nemo.collections.asr.parts.submodules.rnnt_decoding",
    ):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["nemo.collections.asr"].models = types.SimpleNamespace(
        ASRModel=types.SimpleNamespace(restore_from=lambda **kw: None)
    )
    sys.modules["nemo.collections.asr.parts.submodules.rnnt_decoding"].RNNTDecodingConfig = object
    omegaconf = sys.modules.setdefault("omegaconf", types.ModuleType("omegaconf"))
    omegaconf.OmegaConf = types.SimpleNamespace(structured=lambda x: x)
    if importlib.util.find_spec("torchaudio") is None:
        ta = sys.modules.setdefault("torchaudio", types.ModuleType("torchaudio"))
        ta.save = lambda *a, **k: None
        ta.load = lambda *a, **k: (None, 16000)
        ta.functional = types.SimpleNamespace(resample=lambda w, a, b: w)


def load_app(key: str):
    module_name, path, extra = APPS[key]
    for p in extra:
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module.app


def load_all() -> dict:
    tmp = tempfile.mkdtemp(prefix="gate_contract_")
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp}/contract.db"
    os.environ["CLINIC_DB_PATH"] = f"{tmp}/contract.db"
    os.environ.setdefault("VOICE_AGENT_AUDIT_DB", f"{tmp}/audit.db")
    os.chdir(ROOT)
    _install_stubs()
    return {key: load_app(key) for key in APPS}


def normalize(spec: dict) -> dict:
    schemas = spec.get("components", {}).get("schemas", {})

    def resolve(schema):
        seen = 0
        while isinstance(schema, dict) and "$ref" in schema and seen < 20:
            schema = schemas.get(schema["$ref"].rsplit("/", 1)[-1], {})
            seen += 1
        return schema or {}

    out = {}
    for path, ops in sorted(spec.get("paths", {}).items()):
        for method, op in sorted(ops.items()):
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            params = sorted(
                f"{p.get('in')}:{p.get('name')}:{'required' if p.get('required') else 'optional'}"
                for p in op.get("parameters", [])
            )
            body = None
            content = (op.get("requestBody") or {}).get("content") or {}
            if content:
                schema = resolve(next(iter(content.values())).get("schema", {}))
                body = {
                    "required": sorted(schema.get("required", [])),
                    "properties": sorted(schema.get("properties", {})),
                }
            out[f"{method.upper()} {path}"] = {"params": params, "body": body}
    return out


def diff_contract(old: dict, new: dict) -> tuple[list[str], list[str]]:
    """-> (breaking, other). Removing an operation, a parameter or a body
    field, or making something newly required, breaks existing callers."""
    breaking, other = [], []
    for op in sorted(set(old) - set(new)):
        breaking.append(f"operation removed: {op}")
    for op in sorted(set(new) - set(old)):
        other.append(f"operation added: {op}")
    for op in sorted(set(old) & set(new)):
        o, n = old[op], new[op]
        o_names = {p.rsplit(":", 1)[0]: p for p in o["params"]}
        n_names = {p.rsplit(":", 1)[0]: p for p in n["params"]}
        for name in sorted(set(o_names) - set(n_names)):
            breaking.append(f"{op}: parameter removed: {name}")
        for name in sorted(set(n_names) - set(o_names)):
            (breaking if n_names[name].endswith(":required") else other).append(
                f"{op}: parameter added: {n_names[name]}"
            )
        for name in sorted(set(o_names) & set(n_names)):
            if o_names[name] != n_names[name]:
                (breaking if n_names[name].endswith(":required") else other).append(
                    f"{op}: parameter changed: {o_names[name]} -> {n_names[name]}"
                )
        ob, nb = (
            o["body"] or {"required": [], "properties": []},
            n["body"] or {"required": [], "properties": []},
        )
        for f in sorted(set(ob["properties"]) - set(nb["properties"])):
            breaking.append(f"{op}: body field removed: {f}")
        for f in sorted(set(nb["required"]) - set(ob["required"])):
            breaking.append(f"{op}: body field newly required: {f}")
        for f in sorted(set(nb["properties"]) - set(ob["properties"])):
            if f not in nb["required"]:
                other.append(f"{op}: optional body field added: {f}")
    return breaking, other


def _template(path: str) -> str:
    return re.sub(r"\{[^}]*\}", "{}", path)


def client_calls() -> list[tuple[str, str, str]]:
    """(method, path template, source) for every clinic-api call the voice
    agent makes -- ClinicToolsClient's methods, and main.py's startup
    catalogue fetch."""
    calls = []
    src = (ROOT / "agent" / "tools_client.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in ("get", "post", "put", "patch", "delete") or not node.args:
            continue
        if "_client" not in ast.unparse(node.func.value):
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            path = arg.value
        elif isinstance(arg, ast.JoinedStr):
            path = "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in arg.values)
        else:
            continue
        calls.append((node.func.attr.upper(), _template(path), f"agent/tools_client.py:{node.lineno}"))
    main_src = (ROOT / "main.py").read_text(encoding="utf-8")
    for m in re.finditer(r'\{CLINIC_API_BASE\}(/api/[^"\']+)', main_src):
        calls.append(("GET", _template(m.group(1)), "main.py"))
    return calls


def check(apps: dict) -> dict:
    problems, notes, contracts = [], [], {}
    for key, app in apps.items():
        contracts[key] = normalize(app.openapi())
        snap_path = CONTRACTS_DIR / f"{key}.json"
        if not snap_path.exists():
            problems.append(f"{key}: no committed contract snapshot at {snap_path.name}")
            continue
        old = json.loads(snap_path.read_text(encoding="utf-8"))
        breaking, other = diff_contract(old, contracts[key])
        problems += [f"{key}: BREAKING {b}" for b in breaking]
        if other:
            problems += [
                f"{key}: snapshot out of date ({o}) -- update deliberately with "
                f"`python scripts/gate_contracts.py --update` (a gate-config change)"
                for o in other
            ]
    served = {(op.split(" ", 1)[0], _template(op.split(" ", 1)[1])) for op in contracts.get("clinic-api", {})}
    for method, path, where in client_calls():
        if (method, path) not in served:
            problems.append(f"voice agent calls {method} {path} ({where}) but clinic-api does not serve it")
        else:
            notes.append(f"{method} {path} ok")
    return {
        "ok": not problems,
        "problems": problems,
        "client_calls_verified": len(notes),
        "operations": {k: len(v) for k, v in contracts.items()},
    }


def import_smoke(apps: dict) -> dict:
    problems = []
    for key, app in apps.items():
        paths = {getattr(r, "path", None) for r in app.routes}
        for need in REQUIRED_ROUTES[key]:
            if need not in paths:
                problems.append(f"{key}: route {need} missing")
    return {"ok": not problems, "problems": problems, "routes": {k: len(a.routes) for k, a in apps.items()}}


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true")
    g.add_argument("--update", action="store_true")
    g.add_argument("--import-smoke", action="store_true")
    args = ap.parse_args()
    try:
        apps = load_all()
    except Exception as e:  # an import failure IS the finding
        sys.stdout.write(
            json.dumps({"ok": False, "problems": [f"import failed: {type(e).__name__}: {e}"]}) + "\n"
        )
        return 1
    if args.update:
        CONTRACTS_DIR.mkdir(parents=True, exist_ok=True)
        for key, app in apps.items():
            (CONTRACTS_DIR / f"{key}.json").write_text(
                json.dumps(normalize(app.openapi()), indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        sys.stdout.write(json.dumps({"ok": True, "updated": sorted(apps)}) + "\n")
        return 0
    result = check(apps) if args.check else import_smoke(apps)
    sys.stdout.write(json.dumps(result) + "\n")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
