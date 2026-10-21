# Eval pilot report

Manifest digest `4e63efb7cecb9652`. Five tasks, one run per task and arm: directional evidence for a spending decision, not a significance test.

**Verdict: did not meet the continuation threshold**


## Primary: hidden-test pass rate (%)

| task | A1 | A1r | A2 | A3 |
|---|---|---|---|---|
| bill-split | 100.0 | 100.0 | 100.0 | 100.0 |
| config-includes | 100.0 | 100.0 | 100.0 | 100.0 |
| dep-resolver | 100.0 | 100.0 | 100.0 | 100.0 |
| log-windows | 100.0 | 100.0 | 100.0 | 100.0 |
| token-bucket | 100.0 | 100.0 | 100.0 | 100.0 |
| **mean** | 100.0 | 100.0 | 100.0 | 100.0 |

A1 − A1r spread: 0.0 points; primary-trigger sensitivity: unchanged with A1r in place of A1.

Per-task paired differences (points):

| task | A2 − A1 | A3 − A1 |
|---|---|---|
| bill-split | 0.0 | 0.0 |
| config-includes | 0.0 | 0.0 |
| dep-resolver | 0.0 | 0.0 |
| log-windows | 0.0 | 0.0 |
| token-bucket | 0.0 | 0.0 |

## Follow-up change

| arm | new-behaviour pass % | regressions | persistent failures | repaired |
|---|---|---|---|---|
| A1 | 100.0 | 0 | 0 | 0 |
| A2 | 100.0 | 0 | 0 | 0 |
| A3 | 100.0 | 0 | 0 | 0 |

## Judges

Calibration (reference preferred in both orders):

| judge | bill-split | config-includes | dep-resolver | log-windows | token-bucket | passed |
|---|---|---|---|---|---|---|
| fable | yes | yes | yes | yes | yes | yes |
| astra | yes | yes | yes | no | yes | yes |

A1 vs A2 (winner per task):

| judge | bill-split | config-includes | dep-resolver | log-windows | token-bucket |
|---|---|---|---|---|---|
| fable | A1 | A1 | tie | tie | A1 |
| astra | A2 | A1 | A1 | tie | A1 |

A1 vs A3 (winner per task):

| judge | bill-split | config-includes | dep-resolver | log-windows | token-bucket |
|---|---|---|---|---|---|
| fable | tie | A3 | A3 | A1 | tie |
| astra | A3 | A3 | A1 | A3 | A3 |

A2 vs A3 (winner per task):

| judge | bill-split | config-includes | dep-resolver | log-windows | token-bucket |
|---|---|---|---|---|---|
| fable | A2 | A2 | tie | A2 | tie |
| astra | A3 | A3 | A3 | A3 | A3 |

Leak words in exports (review/rubric/plan/spec/round), mean per export: A1 0.0, A2 0.8, A3 1.2

## Static metrics (mean per task)

| arm | ruff violations | source LOC | test LOC | max branches/function |
|---|---|---|---|---|
| A1 | 6.4 | 80.4 | 145.6 | 8.6 |
| A1r | 5.6 | 86.6 | 142.2 | 9.4 |
| A2 | 14.6 | 127.6 | 301.2 | 6.6 |
| A3 | 11.6 | 107.2 | 253.6 | 5.8 |

## Diagnostics (per task mean)

| arm | model minutes | input tokens | output tokens | plan rounds | phase rounds | flags |
|---|---|---|---|---|---|---|
| A1 | 0.7 | 102758 | 8704 | 0.0 | 0.0 | none |
| A1r | 0.7 | 103072 | 8166 | 0.0 | 0.0 | none |
| A2 | 44.8 | 10105369 | 522969 | 5.0 | 5.0 | plan_capped; review_capped |
| A3 | 21.7 | 4323340 | 167890 | 4.8 | 3.0 | plan_capped |

## Limits

- Isolation is enforced by macOS sandboxes (Claude Code's Bash sandbox, an outer profile around Codex) and proved by probes; a command that reaches private material by a route the probes do not cover would go unnoticed — the transcript audit is diagnostic only.
- Judge calibration shows the judges see injected defects, not that they rank close builds reliably.
- The treatment bundles rules, rubric, review and extra compute; A2 vs A3 compares reviewer configurations (model, CLI and tool policy together).
- Hidden tests run candidate code on the host without a sandbox, in the same process as the test runner: scores assume non-adversarial candidates (a build could forge its own results).
