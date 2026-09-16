"""Tests for the gate itself.

Author: Chakravardhan

A gate that silently stopped detecting what it claims to detect would look
exactly like a clean repository. These tests feed each detector a known
violation and assert it is caught -- and a known-clean input, and assert it
is not. Values that the gate's own scanners would flag in this file (phone
numbers, keys, suppression directives) are assembled at runtime.

    python -m pytest tests/test_gate_selfcheck.py -v
"""

from __future__ import annotations

import json

import gate_support

protect = gate_support.load_script("gate_protect")
scan = gate_support.load_script("gate_scan")
label = gate_support.load_script("gate_label")
hook = gate_support.load_script("gate_hook")
lib = gate_support.load_script("gate_lib")

BASE_TEST = """
import pytest

def test_accuracy():
    acc = measure()
    assert acc >= 0.9
    assert acc <= 1.0

def test_rounding():
    assert value() == pytest.approx(1.0, rel=0.01)

def test_other():
    assert other() == 2
"""


def _reasons(head):
    return protect.compare_tests("tests/test_x.py", BASE_TEST, head)


# ---------------------------------------------------------------------------
# weakened tests
# ---------------------------------------------------------------------------
def test_an_unchanged_test_file_is_clean():
    assert _reasons(BASE_TEST) == []


def test_a_new_test_file_weakens_nothing():
    assert protect.compare_tests("tests/test_new.py", None, BASE_TEST) == []


def test_a_deleted_test_file_is_detected():
    assert any("deleted" in r for r in protect.compare_tests("tests/test_x.py", BASE_TEST, None))


def test_a_removed_test_is_detected():
    head = BASE_TEST.replace("def test_other():\n    assert other() == 2\n", "")
    assert any("removed" in r and "test_other" in r for r in _reasons(head))


def test_a_removed_assertion_is_detected():
    head = BASE_TEST.replace("    assert acc <= 1.0\n", "")
    assert any("assertions reduced" in r for r in _reasons(head))


def test_an_always_true_assertion_is_detected():
    head = BASE_TEST.replace("assert other() == 2", "assert other() == 2 or True")
    assert any("always-true" in r for r in _reasons(head))


def test_an_added_skip_is_detected():
    head = BASE_TEST.replace("def test_other", "@pytest.mark.skip\ndef test_other")
    assert any("skip" in r for r in _reasons(head))


def test_a_neutralised_body_is_detected():
    head = BASE_TEST.replace("    assert other() == 2", "    return\n    assert other() == 2")
    assert any("neutralised" in r for r in _reasons(head))


def test_a_lowered_threshold_is_detected():
    head = BASE_TEST.replace("acc >= 0.9", "acc >= 0.5")
    assert any("threshold lowered" in r for r in _reasons(head))


def test_a_widened_tolerance_is_detected():
    head = BASE_TEST.replace("rel=0.01", "rel=0.5")
    assert any("tolerance widened" in r for r in _reasons(head))


# ---------------------------------------------------------------------------
# gate configuration and baseline
# ---------------------------------------------------------------------------
def test_loosening_the_gate_config_is_detected():
    base = {
        "checks": {"lint": {"mandatory": True, "modes": ["fast", "full"]}},
        "suites": {"unit-tests": {"select": ["tests/*::*"], "min_tests": 10}},
        "protected_paths": ["scripts/gate*"],
        "max_file_bytes": 100,
        "max_remediation_cycles": 3,
    }
    head = {
        "checks": {"lint": {"mandatory": False, "modes": ["full"]}},
        "suites": {"unit-tests": {"select": ["tests/*::*"], "min_tests": 5, "exclude": ["tests/x.py::*"]}},
        "protected_paths": [],
        "max_file_bytes": 200,
        "max_remediation_cycles": 9,
    }
    reasons = " | ".join(protect.compare_config(base, head))
    for needle in (
        "made optional",
        "no longer runs",
        "min_tests lowered",
        "exclusions added",
        "protected_paths entries removed",
        "max_file_bytes raised",
        "max_remediation_cycles raised",
    ):
        assert needle in reasons, needle


def test_loosening_the_baseline_is_detected_and_tightening_is_not():
    base = {"lint": {"a.py|E501": 2}, "format_unformatted": ["a.py"]}
    looser = {"lint": {"a.py|E501": 3, "b.py|F401": 1}, "format_unformatted": ["a.py", "b.py"]}
    tighter = {"lint": {"a.py|E501": 1}, "format_unformatted": []}
    assert len(protect.compare_baseline(base, looser)) == 3
    assert protect.compare_baseline(base, tighter) == []


def test_a_disabled_ci_job_is_detected(tmp_path):
    wf = tmp_path / "g1.yml"
    wf.write_text(
        "on:\n  pull_request:\njobs:\n  gate:\n    if: false\n    runs-on: x\n    steps:\n"
        "      - run: bash scripts/gate.sh --full --ci || true\n",
        encoding="utf-8",
    )
    cfg = {
        "workflow_policy": {
            str(wf): {
                "required_jobs": ["gate"],
                "required_triggers": ["pull_request"],
                "required_commands": ["bash scripts/gate.sh --full --ci"],
            }
        }
    }
    reasons = " | ".join(protect.workflow_policy(cfg))
    assert "disabled" in reasons
    assert "non-fatal" in reasons


def test_the_suppression_pattern_catches_every_directive_family():
    directives = [
        "# no" + "qa: E501",
        "# type" + ": ignore[attr]",
        "# no" + "sec B101",
        "# pragma: no" + " cover",
        "# fmt: " + "off",
        "# ruff: no" + "qa",
        "# pylint: " + "disable=x",
        "// eslint-" + "disable",
        "gitleaks" + ":allow",
    ]
    for d in directives:
        assert scan.SUPPRESSION_RE.search(d), d
    assert not scan.SUPPRESSION_RE.search("x = 1  # a normal comment")


# ---------------------------------------------------------------------------
# healthcare scanners
# ---------------------------------------------------------------------------
def test_an_unapproved_phone_number_is_caught_and_masked():
    number = "98" + "31234567"
    findings = scan.phi_in_text("x.py", f"call {number} now", {"phones": []})
    assert len(findings) == 1
    assert number not in json.dumps(findings)
    assert findings[0]["value"].endswith(number[-2:])


def test_an_approved_fictional_number_passes():
    assert scan.phi_in_text("x.py", "9000000001", {"phone_ranges": [["9000000000", "9000009999"]]}) == []


def test_an_aadhaar_number_needs_a_valid_checksum():
    stem = "23456789012"
    valid = next(stem + str(d) for d in range(10) if scan.verhoeff_valid(stem + str(d)))
    invalid = next(stem + str(d) for d in range(10) if not scan.verhoeff_valid(stem + str(d)))
    assert [f["kind"] for f in scan.phi_in_text("x.py", valid, {})] == ["aadhaar"]
    assert scan.phi_in_text("x.py", invalid, {}) == []


def test_real_shaped_credentials_are_caught(tmp_path):
    key = "sk-ant-" + "a1B2" * 8
    hf = "hf_" + "Zx9" * 12
    path = tmp_path / "deploy.sh"
    path.write_text(f"export ANTHROPIC_API_KEY={key}\nHF={hf}\nexport TOKEN=${{TOKEN}}\n", encoding="utf-8")
    kinds = {f["kind"] for f in scan.prod_credentials([str(path)])}
    assert "anthropic-key" in kinds and "huggingface-token" in kinds
    assert not any(key in json.dumps(f) for f in scan.prod_credentials([str(path)]))


def test_a_logged_phone_number_is_caught(tmp_path):
    path = tmp_path / "svc.py"
    path.write_text(
        "import logging\nlog = logging.getLogger('x')\n\ndef book(phone):\n"
        "    log.info('booking %s', phone)\n",
        encoding="utf-8",
    )
    findings = scan.phi_in_logs([str(path)])
    assert [(f["kind"], f["identifier"], f["function"]) for f in findings] == [("log", "phone", "book")]


def test_unapproved_names_in_test_data_are_caught(tmp_path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    path = tests_dir / "test_y.py"
    path.write_text('BODY = {"patient_name": "' + "Real" + ' Person"}\n', encoding="utf-8")
    findings = scan.approved_test_data([str(path).replace("\\", "/")], {"names": ["Riya Das"]}, [])
    assert [f["kind"] for f in findings] == ["unapproved-name"]


# ---------------------------------------------------------------------------
# verdicts: label decision, hooks, ratchet
# ---------------------------------------------------------------------------
HEAD = "a" * 40


def _eligible_inputs():
    g1 = {
        "commit_sha": HEAD,
        "mode": "full",
        "final_status": "PASS",
        "checks": [{"id": "lint", "mandatory": True, "in_mode": True, "status": "pass"}],
    }
    ai = {"status": "completed", "reviewed_head_sha": HEAD, "findings": [{"severity": "Low"}]}
    prot = {"gate_config_touched": False, "tests_weakened": False, "bypass_detected": False}
    return g1, ai, prot


def test_the_label_needs_every_condition():
    g1, ai, prot = _eligible_inputs()
    assert label.decide(HEAD, "success", g1, ai, prot)["eligible"]
    assert not label.decide(HEAD, "failure", g1, ai, prot)["eligible"]
    assert not label.decide(HEAD, "success", {**g1, "commit_sha": "b" * 40}, ai, prot)["eligible"]
    assert not label.decide(HEAD, "success", g1, ai, {**prot, "gate_config_touched": True})["eligible"]
    assert not label.decide(HEAD, "success", g1, ai, {**prot, "tests_weakened": True})["eligible"]
    assert not label.decide(HEAD, "success", g1, ai, {**prot, "bypass_detected": True})["eligible"]
    assert not label.decide(HEAD, "success", g1, {**ai, "findings": [{"severity": "High"}]}, prot)["eligible"]
    assert not label.decide(HEAD, "success", g1, {**ai, "status": "failed"}, prot)["eligible"]
    assert not label.decide(HEAD, "success", g1, None, prot)["eligible"]


RED = {
    "final_status": "FAIL",
    "failing_checks": ["lint"],
    "commit_sha": HEAD,
    "checks": [{"id": "lint", "summary": "1 new lint violation", "findings": []}],
}
GREEN = {"final_status": "PASS", "failing_checks": [], "commit_sha": HEAD, "checks": []}


def _isolated_hook(monkeypatch, tmp_path):
    monkeypatch.setattr(hook, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(hook, "ESCALATION", tmp_path / "ESCALATION.md")


def test_the_stop_hook_blocks_three_times_then_escalates(monkeypatch, tmp_path, capsys):
    _isolated_hook(monkeypatch, tmp_path)
    assert [hook.stop(RED, 3) for _ in range(3)] == [2, 2, 2]
    assert not (tmp_path / "ESCALATION.md").exists()
    assert hook.stop(RED, 3) == 0
    assert (tmp_path / "ESCALATION.md").exists()
    out = capsys.readouterr().out
    assert "ESCALATED TO A HUMAN" in json.loads(out.strip().splitlines()[-1])["systemMessage"]


def test_a_green_gate_resets_the_remediation_counter(monkeypatch, tmp_path):
    _isolated_hook(monkeypatch, tmp_path)
    assert hook.stop(RED, 3) == 2
    assert hook.stop(GREEN, 3) == 0
    assert [hook.stop(RED, 3) for _ in range(3)] == [2, 2, 2]


def test_a_blocked_gate_goes_straight_to_a_human_and_is_never_green(monkeypatch, tmp_path, capsys):
    """BLOCKED = every check passed but gate files changed. Only a code owner
    can clear it, so the hook escalates at once instead of asking Claude to
    'remediate' files it is forbidden to edit."""
    _isolated_hook(monkeypatch, tmp_path)
    blocked = {**GREEN, "final_status": "BLOCKED", "gate_config_files": ["scripts/gate-config.json"]}
    assert hook.stop(RED, 3) == 2
    assert hook.stop(blocked, 3) == 0
    escalation = (tmp_path / "ESCALATION.md").read_text(encoding="utf-8")
    assert "scripts/gate-config.json" in escalation and "BLOCKED" in escalation
    message = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["systemMessage"]
    assert "HUMAN REVIEW REQUIRED" in message and "NOT a green gate" in message
    assert hook.post_tool_use(blocked) == 2
    stored = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert stored["sessions"]["default"]["counts"] == {}


def test_remediation_cycles_are_counted_per_claude_session(monkeypatch, tmp_path):
    """Two sessions in one working tree must not spend each other's cycles."""
    _isolated_hook(monkeypatch, tmp_path)
    assert [hook.stop(RED, 3, "session-a") for _ in range(3)] == [2, 2, 2]
    assert hook.stop(RED, 3, "session-b") == 2
    assert not (tmp_path / "ESCALATION.md").exists()
    assert hook.stop(RED, 3, "session-a") == 0
    assert "session-a" in (tmp_path / "ESCALATION.md").read_text(encoding="utf-8")
    assert hook.stop(GREEN, 3, "session-b") == 0
    assert (tmp_path / "ESCALATION.md").exists(), "session-b's green run withdrew session-a's escalation"
    assert hook.stop(GREEN, 3, "session-a") == 0
    assert not (tmp_path / "ESCALATION.md").exists()


def _settings(tmp_path, hooks):
    (tmp_path / ".claude").mkdir(exist_ok=True)
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps({"hooks": hooks}), encoding="utf-8")


def _command(cmd, matcher=None):
    entry = {"hooks": [{"type": "command", "command": cmd}]}
    return [{**entry, "matcher": matcher}] if matcher else [entry]


def test_the_hook_policy_accepts_the_real_quoted_hooks(monkeypatch, tmp_path):
    monkeypatch.setattr(protect, "read_text", lambda p: (tmp_path / p).read_text(encoding="utf-8"))
    _settings(
        tmp_path,
        {
            "PostToolUse": _command(
                'bash "$CLAUDE_PROJECT_DIR/scripts/gate.sh" --fast', "Edit|Write|MultiEdit"
            ),
            "Stop": _command('bash "$CLAUDE_PROJECT_DIR/scripts/gate.sh" --full'),
        },
    )
    assert protect.hook_policy() == []


def test_the_hook_policy_catches_removed_narrowed_and_defanged_hooks(monkeypatch, tmp_path):
    monkeypatch.setattr(protect, "read_text", lambda p: (tmp_path / p).read_text(encoding="utf-8"))
    _settings(
        tmp_path,
        {
            "PostToolUse": _command('bash "$CLAUDE_PROJECT_DIR/scripts/gate.sh" --fast', "Write"),
            "Stop": _command('bash "$CLAUDE_PROJECT_DIR/scripts/gate.sh" --full || true'),
        },
    )
    reasons = " | ".join(protect.hook_policy())
    assert "PostToolUse hook no longer runs" in reasons
    assert "made non-fatal" in reasons
    _settings(tmp_path, {"PostToolUse": _command("bash scripts/gate.sh --fast", "Edit|Write|MultiEdit")})
    assert any("Stop hook no longer runs" in r for r in protect.hook_policy())


def test_a_failing_check_is_never_masked_as_blocked(monkeypatch, tmp_path):
    """Touching a gate file must not become a way out of a red check: the
    report computes FAIL before BLOCKED, and the hook keeps blocking."""
    _isolated_hook(monkeypatch, tmp_path)
    red_and_touched = {**RED, "gate_config_files": ["scripts/gate-config.json"]}
    assert hook.stop(red_and_touched, 3) == 2


def test_the_post_edit_hook_reports_red_but_green_is_silent():
    assert hook.post_tool_use(GREEN) == 0
    assert hook.post_tool_use(RED) == 2


def test_the_ratchet_blocks_increases_only():
    regressions, improvements = lib.ratchet({"a|E1": 3, "b|E2": 1}, {"a|E1": 2, "b|E2": 2})
    assert [r["key"] for r in regressions] == ["a|E1"]
    assert [r["key"] for r in improvements] == ["b|E2"]
    assert lib.ratchet({"c|E3": 1}, {}, scope={"a"}) == ([], [])
