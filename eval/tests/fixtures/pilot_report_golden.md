# Eval pilot report

Manifest digest `d`. Five tasks, one run per task and arm: directional evidence for a spending decision, not a significance test.

**Verdict: did not meet the continuation threshold**


## Primary: hidden-test pass rate (%)

| task | A1 | A1r | A2 | A3 |
|---|---|---|---|---|
| bill-split | 100.0 | 95.0 | 90.0 | 85.0 |
| config-includes | 95.0 | 90.0 | 85.0 | 80.0 |
| dep-resolver | 90.0 | 85.0 | 80.0 | 100.0 |
| log-windows | 85.0 | 80.0 | 100.0 | 95.0 |
| token-bucket | 80.0 | 100.0 | 95.0 | 90.0 |
| **mean** | 90.0 | 90.0 | 90.0 | 90.0 |

A1 − A1r spread: 0.0 points; primary-trigger sensitivity: unchanged with A1r in place of A1.

Per-task paired differences (points):

| task | A2 − A1 | A3 − A1 |
|---|---|---|
| bill-split | -10.0 | -15.0 |
| config-includes | -10.0 | -15.0 |
| dep-resolver | -10.0 | 10.0 |
| log-windows | 15.0 | 10.0 |
| token-bucket | 15.0 | 10.0 |

## Follow-up change

| arm | new-behaviour pass % | regressions | persistent failures | repaired |
|---|---|---|---|---|
| A1 | 90.0 | 0 | 0 | 0 |
| A2 | 90.0 | 0 | 0 | 0 |
| A3 | 90.0 | 5 | 0 | 0 |

## Judges

Calibration (reference preferred in both orders):

| judge | bill-split | config-includes | dep-resolver | log-windows | token-bucket | passed |
|---|---|---|---|---|---|---|
| fable | no | yes | yes | yes | yes | yes |
| astra | yes | yes | yes | yes | no | yes |

A1 vs A2 (winner per task):

| judge | bill-split | config-includes | dep-resolver | log-windows | token-bucket |
|---|---|---|---|---|---|
| fable | A2 | tie | A1 | A2 | tie |
| astra | tie | A1 | A2 | tie | A1 |

A1 vs A3 (winner per task):

| judge | bill-split | config-includes | dep-resolver | log-windows | token-bucket |
|---|---|---|---|---|---|
| fable | tie | A1 | A3 | tie | A1 |
| astra | A1 | A3 | tie | A1 | A3 |

A2 vs A3 (winner per task):

| judge | bill-split | config-includes | dep-resolver | log-windows | token-bucket |
|---|---|---|---|---|---|
| fable | A2 | A3 | tie | A2 | A3 |
| astra | A3 | tie | A2 | A3 | tie |

Leak words in exports (review/rubric/plan/spec/round), mean per export: A1 2.0, A2 1.5, A3 2.5

## Static metrics (mean per task)

| arm | ruff violations | source LOC | test LOC | max branches/function |
|---|---|---|---|---|
| A1 | 1.0 | 50.0 | 40.0 | 4.0 |
| A1r | 1.0 | 50.0 | 40.0 | 4.0 |
| A2 | 1.0 | 50.0 | 40.0 | 4.0 |
| A3 | 1.0 | 50.0 | 40.0 | 4.0 |

## Diagnostics (per task mean)

| arm | model minutes | input tokens | output tokens | plan rounds | phase rounds | flags |
|---|---|---|---|---|---|---|
| A1 | 1.0 | 1000 | 200 | 0.0 | 0.0 | none |
| A1r | 1.0 | 1001 | 200 | 0.0 | 0.0 | none |
| A2 | 1.0 | 1002 | 200 | 2.0 | 2.0 | plan_capped |
| A3 | 1.0 | 1003 | 200 | 3.0 | 2.0 | none |

## Limits

- Isolation is enforced by macOS sandboxes (Claude Code's Bash sandbox, an outer profile around Codex) and proved by probes; a command that reaches private material by a route the probes do not cover would go unnoticed — the transcript audit is diagnostic only.
- Judge calibration shows the judges see injected defects, not that they rank close builds reliably.
- The treatment bundles rules, rubric, review and extra compute; A2 vs A3 compares reviewer configurations (model, CLI and tool policy together).
- Hidden tests run candidate code on the host without a sandbox, in the same process as the test runner: scores assume non-adversarial candidates (a build could forge its own results).
