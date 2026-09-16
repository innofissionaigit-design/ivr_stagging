# CLAUDE.md -- Kolkata Care Diagnostics voice agent

A Bengali/Hindi/English phone line for a diagnostic clinic: ASR (`agent/asr.py`),
intent extraction (`agent/llm.py`, fast path `agent/fast_path.py`), clinic API
calls (`agent/tools_client.py` -> `clinic-api/`), templated replies
(`agent/reply_templates.py`, `agent/i18n.py`), TTS. `main.py` is the WebM
transport; `main_pcm.py` is GENERATED from it by `python tools/make_pcm_variant.py`
-- edit `main.py`, then regenerate. See `README.md` for the architecture.

This is a healthcare system. Patient safety and patient privacy outrank speed.

## Pre-human-review quality gate -- required workflow

Every change passes these gates, in order, before a senior engineer reviews it:

| Gate | Where | What |
|---|---|---|
| G0 | local, Claude hooks | `bash scripts/gate.sh --fast` after every edit; `--full` at the end of every turn |
| G1 | CI, `.github/workflows/g1-gate.yml` | the full gate; **authoritative** for mechanical correctness |
| G2 | CI, `.github/workflows/g2-ai-review.yml` | fresh-context AI engineering review; not production approval |
| Label | CI, `.github/workflows/label-guard.yml` | applies `ready-for-human-review` only when all of the above pass |

**The rules. None of them has an exception.**

1. **Run the full gate before requesting review:** `bash scripts/gate.sh --full`.
2. **If a check fails, diagnose and fix the cause.** Read `gate-report.md`, find the
   root cause, change the code under test -- not the test and not the gate.
3. **Maximum 3 remediation cycles per failing check.** The Stop hook counts
   consecutive failures of each check.
4. **On the 4th failure, stop and escalate to a human.** The Stop hook writes
   `.gate/ESCALATION.md` and ends the turn as RED. Say plainly that the gate is red and
   what you tried; do not claim success.
5. **Never modify gate configuration to make a check pass.** That means `scripts/gate*`,
   `scripts/gate-contracts/`, `.github/workflows/`, `.claude/settings.json`, this file,
   and `tests/golden/`. A change there sets `gate_config_touched`, which withholds the
   label until a code owner reviews it. Never edit or delete `.gate/state.json` to reset
   the remediation counter.
6. **Never weaken or delete tests to make them pass.** No removed tests or assertions,
   no `skip`/`xfail`, no always-true assertions, no lowered thresholds, no widened
   tolerances, no early `return`. `gate_protect.py` detects each of these.
7. **Never add lint-ignore / type-ignore / security-ignore directives simply to bypass
   failures.** The gate counts suppression directives per file; any increase fails.
8. **Never commit real PHI, credentials or real patient data.** Test data must come from
   `scripts/gate-approved-test-data.json` (fictional). Credentials come from the
   environment and are never written into the repository.
9. **Paste `gate-report.md` into the pull request** (the template has a section for it).
10. **Wait for the CI-generated `ready-for-human-review` label.** Only CI applies it; a
    label added by hand is removed. Without it, senior review does not start.

Claude does not commit or push unless the user explicitly asks.

## What the gate checks

`scripts/gate-config.json` is the list; `gate-report.md` shows each result.

- Mechanical: syntax/compile, formatting, lint, type checking, unused imports and dead
  code, static security analysis, secrets, dependency vulnerabilities, build (generated
  `main_pcm.py` in sync, apps import), API contracts (`scripts/gate-contracts/`), debug
  code / console output / untracked work markers, unexpected files.
- Healthcare: PHI in code, PHI in logs and exception messages, production credentials,
  approved test data only, safety policy, PHI boundaries, multilingual routing, clinical
  golden set (`tests/golden/golden_set.json`), escalation/abstention, human
  handoff/fallback, 8 kHz/PSTN audio, telephony behaviour.
- Gate protection: config touched, tests weakened, suppressions added, CI jobs disabled,
  pytest collection tampering, baseline loosened.

Pre-existing violations are recorded in `scripts/gate-baseline.json`. **The baseline only
shrinks.** A new violation fails even in a file that already has old ones; raising a
number in the baseline is detected as loosening the gate.

## Project safety rules (reviewed by G2 as well)

- PHI -- phone numbers, names, dates of birth, PINs, transcripts, medical history,
  tokens -- never reaches logs, exception messages or audit records beyond the existing
  design (`agent/call_audit.py` redacts verification answers, tokens and history).
- The LLM never states a price, schedule, confirmation number or medical fact. Replies
  are templated from backend data; `direct_reply_bn` exists only for smalltalk.
- Patient history is disclosed only after verification and only on a private audio path
  (`agent/privacy.py`); an unclassified path counts as unsafe.
- Every flow completes without a smartphone: no links, QR codes, apps or online payment.
  The clinic counter is the fallback for everything.
- A backend failure is never worded as "not found", and "not found" is never worded as
  a failure.

## Commands

```bash
bash scripts/gate.sh --fast                       # changed files only, seconds
bash scripts/gate.sh --full                       # everything; writes gate-report.json/.md
python -m pytest tests -q                         # the test suite on its own
python tools/make_pcm_variant.py                  # after any edit to main.py
python scripts/gate_golden.py --check             # clinical golden-set drift
```

Local tools live in `.gate-venv/` (gitignored):
`python -m venv .gate-venv && .gate-venv/Scripts/python -m pip install -r scripts/gate-requirements.txt`
(use `.gate-venv/bin/python` on Linux/macOS), plus the gitleaks binary in the same
`Scripts`/`bin` folder or on `PATH`.
