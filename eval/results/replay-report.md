# Eval replay report

**Loop (A3 vs A1):** no effect — review +27.5 points (needs +15); higher on 4/4 caught tasks (needs 3); feature -7.7 points (floor −5); regressions +0.3 points (must not rise)
**Cross-family (A3 vs A2):** not run — A2 runs only when the loop effect passes

- Manifest digest: `90fb8bc3d072aeefe752fad511540536df38b4d29921f6ce05773a44f32d7ceb`
- Caught tasks: jobscan-email, jobscan-greenhouse, jobscan-workable-gem, jobscan-setup-commands; controls: jobscan-watchlist, jobscan-company-notes
- Arms: A1, A2, A3; replicates per cell: 2

## Review-caught defects avoided (%)

Task mean ± SD over replicates; arm mean is the mean of task means.

| Arm | jobscan-email | jobscan-greenhouse | jobscan-workable-gem | jobscan-setup-commands | Arm mean |
|---|---|---|---|---|---|
| A1 | 33.3 ± 0.0 | 70.0 ± 14.1 | 25.0 ± 0.0 | 50.0 ± 0.0 | 44.6 |
| A3 | 83.3 ± 23.6 | 80.0 ± 0.0 | 37.5 ± 17.7 | 87.5 ± 17.7 | 72.1 |

## Feature tests passed (%)

Task mean ± SD over replicates; arm mean is the mean of task means.

| Arm | jobscan-email | jobscan-greenhouse | jobscan-workable-gem | jobscan-setup-commands | jobscan-watchlist | jobscan-company-notes | Arm mean |
|---|---|---|---|---|---|---|---|
| A1 | 100.0 ± 0.0 | 100.0 ± 0.0 | 100.0 ± 0.0 | 100.0 ± 0.0 | 100.0 ± 0.0 | 100.0 ± 0.0 | 100.0 |
| A3 | 100.0 ± 0.0 | 100.0 ± 0.0 | 72.9 ± 2.9 | 96.2 ± 1.8 | 100.0 ± 0.0 | 100.0 ± 0.0 | 94.9 |

## Existing tests broken (%)

Task mean ± SD over replicates; arm mean is the mean of task means.

| Arm | jobscan-email | jobscan-greenhouse | jobscan-workable-gem | jobscan-setup-commands | jobscan-watchlist | jobscan-company-notes | Arm mean |
|---|---|---|---|---|---|---|---|
| A1 | 0.0 ± 0.0 | 0.0 ± 0.0 | 0.0 ± 0.0 | 0.0 ± 0.0 | 0.0 ± 0.0 | 0.0 ± 0.0 | 0.0 |
| A3 | 0.0 ± 0.0 | 0.0 ± 0.0 | 0.2 ± 0.3 | 1.7 ± 0.0 | 0.0 ± 0.0 | 0.0 ± 0.0 | 0.3 |

- A2 not run: the loop effect did not pass.

## Design judgment: A3 vs A1 (descriptive)

Each caught task's builds, as patches from base: replicate r of A1 against replicate r of A3, each pair judged blind by each judge in both orders; a pick counts only when it survives the swap. Net is A3's wins minus A1's over the 4 judgments. Uncalibrated: a replayed phase has no degraded reference to check the judges against. It never enters the verdict.

| Task | fable r1 | fable r2 | astra r1 | astra r2 | Net A3 |
|---|---|---|---|---|---|
| jobscan-email | A3 | A1 | A3 | A3 | +2 |
| jobscan-greenhouse | A1 | A1 | A3 | A3 | +0 |
| jobscan-workable-gem | A3 | A1 | A3 | A3 | +2 |
| jobscan-setup-commands | A3 | A3 | A3 | A3 | +4 |

## Stages

| Arm | Built | CLI versions |
|---|---|---|
| A1 | 2026-10-28 to 2026-10-28 | claude 2.1.312 (Claude Code); codex codex-cli 0.164.0 |
| A3 | 2026-10-28 to 2026-10-28 | claude 2.1.312 (Claude Code); codex codex-cli 0.164.0 |

## Diagnostics

| Arm | Plan rounds | Phase rounds | Capped | Timeouts | Model minutes |
|---|---|---|---|---|---|
| A1 | 0.0 | 0.0 | 0 | 0 | 122.5 |
| A3 | 3.4 | 4.3 | 14 | 2 | 1352.5 |

## Limits

- One source repository, built with this same loop; its caught tasks are selected on phases where review found something, so controls check damage to existing behaviour, not selection.
- A3 vs A1 measures the whole loop package (plan, rubric, review, extra compute); A3 vs A2 measures the reviewer configuration, its CLI included.
- A few tasks with two replicates each is an exploratory continuation rule, not a significance test.

