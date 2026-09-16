#!/usr/bin/env bash
# ADDED BY CHAKRAVARDHAN -- the single entry point for G0 and G1, and this
# script's canonical version: it supersedes an earlier, much simpler draft
# by Sourav (a thin wrapper that only ran gate-report.py) written before
# scripts/gate_checks.py, gate_lib.py, gate_hook.py, gate_contracts.py and
# the rest of this gate's supporting modules existed. Nothing else in the
# repo imports this script's internals (it is invoked only via `bash
# scripts/gate.sh ...`, from a shell, a Claude Code hook, or CI), and every
# module it calls into below (gate_checks.py, gate-report.py's own
# `import gate_lib`, gate_hook.py) is Chakravardhan's, so Sourav's draft is
# dropped rather than merged in piecemeal.
#
#   bash scripts/gate.sh --fast        after every edit (Claude PostToolUse hook)
#   bash scripts/gate.sh --full        before review; end of every Claude turn (Stop hook)
#   bash scripts/gate.sh --full --ci   G1 in GitHub Actions (authoritative)
#
# Exit status: 0 PASS; 1 FAIL; 3 BLOCKED (checks pass but gate files changed);
# 64 usage error. When Claude Code runs this as a hook it passes a JSON
# payload on stdin; the hook's own exit semantics then apply (see
# scripts/gate_hook.py): a red full gate at Stop exits 2, which forbids
# Claude from ending the turn.
#
# PROTECTED FILE. Editing it sets gate_config_touched and withholds the
# ready-for-human-review label until a code owner has reviewed the change.
set -uo pipefail

usage() {
    echo "usage: bash scripts/gate.sh --fast | --full [--ci]" >&2
}

MODE=""
for arg in "$@"; do
    case "$arg" in
        --fast) MODE=fast ;;
        --full) MODE=full ;;
        --ci) export GATE_CI=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "gate.sh: unknown argument: $arg" >&2; usage; exit 64 ;;
    esac
done
[ -n "$MODE" ] || { usage; exit 64; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
mkdir -p .gate
export GATE_BASH="${BASH:-bash}"

# ---- hook payload: Claude Code writes JSON to stdin; a terminal or CI does not
HOOK_EVENT=""
HOOK_SESSION=""
if [ ! -t 0 ]; then
    PAYLOAD="$(timeout 2 cat 2>/dev/null || true)"
    if [ -n "$PAYLOAD" ]; then
        HOOK_FIELDS="$(printf '%s' "$PAYLOAD" | python -c \
            'import json,re,sys
try: d = json.load(sys.stdin)
except Exception: d = {}
print(re.sub(r"[^A-Za-z]", "", str(d.get("hook_event_name", ""))))
print(re.sub(r"[^A-Za-z0-9_-]", "", str(d.get("session_id", "")))[:80])' 2>/dev/null || true)"
        HOOK_EVENT="$(printf '%s\n' "$HOOK_FIELDS" | sed -n 1p)"
        HOOK_SESSION="$(printf '%s\n' "$HOOK_FIELDS" | sed -n 2p)"
    fi
fi

# ---- one gate run at a time. Every run writes .gate/results/ and the
# report; two runs at once (a PostToolUse hook while a Stop hook is still
# going, or two Claude sessions in one working tree) would interleave their
# results and could report a verdict neither run produced. mkdir is atomic,
# so the lock directory is the mutex; a lock whose owner has exited is stale.
LOCK=.gate/run.lock
if [ "$MODE" = fast ]; then LOCK_WAIT="${GATE_LOCK_WAIT_S:-240}"; else LOCK_WAIT="${GATE_LOCK_WAIT_S:-3000}"; fi
waited=0
while ! mkdir "$LOCK" 2>/dev/null; do
    owner="$(cat "$LOCK/pid" 2>/dev/null || true)"
    if [ -n "$owner" ] && ! kill -0 "$owner" 2>/dev/null; then
        rm -rf "$LOCK"
        continue
    fi
    if [ "$waited" -ge "$LOCK_WAIT" ]; then
        echo "[gate] another gate run (pid ${owner:-?}) still holds $LOCK after ${LOCK_WAIT}s;" \
            "this run did not verify anything" >&2
        # Never fall through to a report another run wrote: nothing was checked.
        [ -n "$HOOK_EVENT" ] && exit 2
        exit 1
    fi
    [ "$waited" = 0 ] && echo "[gate] waiting for another gate run (pid ${owner:-?}) to finish" >&2
    sleep 2
    waited=$((waited + 2))
done
echo $$ > "$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT

# ---- interpreters: the app's python runs the tests; the gate venv holds the tools
PY="${GATE_PYTHON:-python}"
command -v "$PY" >/dev/null 2>&1 || PY=python3
export GATE_APP_PYTHON="${GATE_APP_PYTHON:-$(command -v "$PY")}"
if [ -z "${GATE_TOOL_PYTHON:-}" ]; then
    for cand in .gate-venv/Scripts/python.exe .gate-venv/bin/python; do
        if [ -x "$cand" ]; then export GATE_TOOL_PYTHON="$ROOT/$cand"; break; fi
    done
fi
export GATE_TOOL_PYTHON="${GATE_TOOL_PYTHON:-$GATE_APP_PYTHON}"
export PYTHONIOENCODING=utf-8

# ---- deterministic reuse: a full run is skipped only when NOTHING changed
# since the last full run (same HEAD, same working tree bytes, same gate
# config). The stored result is then reported again, red or green.
fingerprint() {
    {
        git rev-parse HEAD 2>/dev/null
        git status --porcelain=v1 --untracked-files=all 2>/dev/null
        git diff HEAD 2>/dev/null
        git ls-files --others --exclude-standard -z 2>/dev/null | xargs -0 -r cat 2>/dev/null
        echo "ci=${GATE_CI:-0} base=${GATE_BASE_REF:-HEAD}"
    } | python -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())'
}

run_gate() {
    # Nothing from an earlier run may survive into this one: if a check
    # crashes before writing its result, the report must see "did not run"
    # (a failure), never the previous run's verdict.
    rm -rf .gate/results .gate/context.json .gate/protect.json .gate/pytest-summary.json \
        gate-report.json gate-report.md
    "$PY" scripts/gate_checks.py --mode "$MODE"
    "$PY" scripts/gate-report.py --mode "$MODE" ${GATE_AI_REVIEW:+--ai-review "$GATE_AI_REVIEW"}
}

FP=""
REUSED=0
if [ "$MODE" = full ] && [ -z "${GATE_CI:-}" ]; then
    FP="$(fingerprint)"
    if [ -f .gate/last-full.fp ] && [ "$(cat .gate/last-full.fp)" = "$FP" ] \
        && [ -f .gate/last-full-report.json ] && [ -f .gate/last-full-report.md ]; then
        cp .gate/last-full-report.json gate-report.json
        cp .gate/last-full-report.md gate-report.md
        REUSED=1
    fi
fi

if [ "$REUSED" = 1 ]; then
    STATUS=$("$PY" -c 'import json; print(json.load(open("gate-report.json"))["final_status"])')
    echo "[gate] nothing changed since the last full run -- reusing its result: $STATUS" >&2
    case "$STATUS" in PASS) RC=0 ;; BLOCKED) RC=3 ;; *) RC=1 ;; esac
elif [ -n "$HOOK_EVENT" ]; then
    run_gate > .gate/last-run.log 2>&1
    RC=$?
else
    run_gate
    RC=$?
fi

if [ "$MODE" = full ] && [ -z "${GATE_CI:-}" ] && [ "$REUSED" = 0 ] && [ -f gate-report.json ]; then
    cp gate-report.json .gate/last-full-report.json
    cp gate-report.md .gate/last-full-report.md
    printf '%s' "$FP" > .gate/last-full.fp
fi

if [ -n "$HOOK_EVENT" ]; then
    "$PY" scripts/gate_hook.py --event "$HOOK_EVENT" --session "${HOOK_SESSION:-default}" --report gate-report.json
    exit $?
fi

[ -f gate-report.md ] && echo "[gate] report: $ROOT/gate-report.md" >&2
exit "$RC"
