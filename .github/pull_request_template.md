## What and why

<!-- What changed, and the user story or issue it serves. -->

## Gate report

<!--
Paste the FULL contents of gate-report.md produced by:

    bash scripts/gate.sh --full

G1 regenerates the report in CI; the pasted copy is context for reviewers,
not evidence. The CI-generated report and the `ready-for-human-review` label
are what count.
-->

## Checklist

- [ ] `bash scripts/gate.sh --full` reports PASS locally
- [ ] No test deleted, skipped or weakened; no lint/type/security ignore directive added
- [ ] No real PHI, credentials or patient data anywhere in the change
- [ ] Gate files untouched -- or this PR intentionally changes the gate and needs code-owner review

Senior review starts only after CI applies `ready-for-human-review`. That label means G1, G2 and gate
protection passed; it is not production approval.
