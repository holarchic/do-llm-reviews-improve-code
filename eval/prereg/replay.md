# Pre-registration: replay of real phases

Fixed before any scored run. The frozen inputs are hashed in `eval/manifests/replay.json`: this file, the six tasks, the harness code (model ids, arms, prompts, thresholds, the judge prompt and the schedule seed) and the shipped skill files the prompts are built from.

## Question

Does the shipped loop's review lower the rate of review-caught defects that reach a finished build? Here the loop is Opus 5.5 writing with Sol (`gpt-6.1-sol`, Codex, effort high) reviewing, compared with Opus 5.5 alone. A second question: does a reviewer from another model family catch more than the writer's own model reviewing?

Pilot 2's fully specified tasks reduced building to transcription: any writer that stayed on task scored 73–100% solo. The defects a review loop exists to catch are the ones such tasks cannot produce: unstated invariants, edge cases and error paths. A census of the source repository's history found 248 code defects caught by Codex review in 75 of 97 sampled commits; 86% were edge-only. The replay rebuilds the phases where review found them.

## Source and tasks

The source is jobscan (`github.com/holarchic/job-board-scanner`): public, MIT-licensed, stdlib-only Python with unittest, built with this loop phase by phase. A task is one historical phase, defined by three commits:
- `base`: the parent of the phase's first feature commit. The run starts here, as a tree with no history, plus `TASK.md`.
- `pre_review`: the last feature commit before the first review fix, the historical writer's finished build.
- `reference`: the phase's last review fix, the landed state.

`TASK.md` is written from the phase's feature commits. It states what the change does and every new interface the hidden tests call, and never the findings review raised.

| Task | Kind | base…reference | Feature ids | Review ids (defects) | Regression ids |
|---|---|---|---|---|---|
| jobscan-email | caught | `00db5d1`…`0300dcd` | 9 | 4 (3) | 202 |
| jobscan-greenhouse | caught | `5daa1d2`…`71318e9` | 10 | 5 (5) | 155 |
| jobscan-workable-gem | caught | `59347ad`…`ccc76ae` | 24 | 4 (4) | 239 |
| jobscan-setup-commands | caught | `6b2b1a7`…`48203b7` | 40 | 4 (4) | 420 |
| jobscan-watchlist | control | `df2e2c7`…`0ce2e09` | 5 | — | 754 |
| jobscan-company-notes | control | `0ce2e09`…`81f1a3a` | 4 | — | 761 |

So n = 4 caught tasks and 2 controls. Each task folder holds its `source.json`, `TASK.md`, hidden modules, `defects.json`, `expected.json` and the fairness audit (`audit.md`).

**Hidden tests**, taken from history and adapted:
- **feature:** the tests the feature commits added. Each passes on `reference` and fails on `base`.
- **review:** the tests the review fixes added, grouped into defects. A review test is kept only if all of the following hold:
  - it calls interfaces named in `TASK.md` or present at `base`, with the same signature at `pre_review` and `reference`;
  - it fails on `pre_review` by assertion (an error counts only when the defect record marks the uncaught exception itself as the defect);
  - it passes on `reference`;
  - the phase introduced the defect;
  - an independent fairness audit finds its expected behaviour derivable from `TASK.md`, the snapshot, or a norm any careful engineer applies.
- **regression:** the ids of the frozen `base` suite that pass on both `base` and `reference`.

Validation ran every anchor three times, the third under the scored run's concurrency. No test varied.

## Arms

Native subscriptions, with the first pilot's loop, prompts and round caps:

| Arm | Writer | Reviewer |
|---|---|---|
| A1 solo | Opus 5.5 | none |
| A2 self | Opus 5.5 | Opus 5.5 |
| A3 plugin | Opus 5.5 | Sol (`gpt-6.1-sol`, effort high) |

**Staging:** A1 runs first, then A3. A2 runs only if the loop effect passes; otherwise its comparisons read "not run" and its builds are never made.

**Replicates:** 2 per cell, so 24 A1 and A3 builds, and 12 more for A2 when it runs.

## Measures

Each build is scored with its `tests/` replaced by the frozen `base` tests plus the hidden modules, so a build's own test edits never change its score:
- `review` (primary): the share of the task's defects avoided. A defect is avoided only when every one of its tests passes.
- `feature`: the pass fraction of the feature tests.
- `regressions`: the fraction of the regression set that fails.

Missing, errored or unfinished tests fail. A control's `review` is N/A.

## Decision rule

The unit is the task mean over replicates. A comparison of treatment T against comparator C, over the n = 4 caught tasks, finds an **effect** when all of these hold:
- T's mean `review` exceeds C's by at least 15 points;
- T is strictly higher on at least ⌈2n/3⌉ = 3 caught tasks;
- T's mean `feature` is no more than 5 points below C's;
- T's mean `regressions`, over caught and control tasks, is no worse than C's.

Otherwise it finds **no effect**. It is **uninformative (headroom)** when C's mean `review` is above 85, or when fewer than 3 caught tasks have a C task mean below 100.

The comparisons:
- **Loop effect:** A3 vs A1.
- **Cross-family effect:** A3 vs A2, run only when the loop effect passes; otherwise "not run".
- **A2 vs A1:** descriptive.

The run is **INCOMPLETE** while any A1 or A3 build is missing or invalid after retries, or, once the loop effect passes, any A2 build is.

These thresholds are an exploratory continuation rule, not a significance test.

## Design judgment (descriptive)

After the scored builds, each caught task's replicate r of A1 is judged blind against replicate r of A3 by both judges (Claude Fable 5.1 and Codex `gpt-6-astra`), in both orders. Only a pick that survives the swap counts. The judges see `TASK.md` and each build's patch from the starting tree, every changed file in full, with markdown and `plans/` left out. A pair over a fixed prompt budget is judged on its source alone, and the report says so. The judgment is uncalibrated, since a replayed phase has no degraded reference to check the judges against, and it never enters the decision rule.

## Isolation

The `base` README names the source repository, so no session may reach it:
- Claude sessions run their Bash tool in a sandbox with no allowed domains and local binding only, and have no web tools.
- Codex sessions run behind a per-session proxy that admits only `chatgpt.com` and `auth.openai.com`, inside a Seatbelt profile that denies every other outbound connection, Unix sockets included. Codex web search is disabled.
- Judges run under the same settings; the Claude judge's only tools are Read, Glob and Grep.
- A probe inside every writer and reviewer configuration proves that model calls work, that git and curl to the source are refused at the proxy, that a direct or Unix socket is refused by Seatbelt, that a localhost exchange works, and that no web tool is available. Each judge configuration is probed for its instructions.

## Threats to validity

- One source repository, built with this same loop. Caught tasks were selected on phases where review found something; the controls check damage to existing behaviour, not selection.
- The historical reviewer found these defects. A3 reviews with the same model family, so it may be better at finding them again than at finding defects in general.
- A3 vs A1 measures the whole loop package: plan, rubric, review and extra compute. A3 vs A2 measures the reviewer configuration, its CLI included.
- Four caught tasks with 2 replicates each give coarse resolution: on a task with 3 defects, one defect moves `review` by 33 points.
- Model CLIs update themselves between stages. Each record keeps its CLI versions, and the report lists each stage's.
- The harness runs a build's code outside the sessions' sandbox twice: the build's own tests, whose summary line reaches the next review, and the scoring. Only a build written to fetch the source at test time could exploit this, and it would show in its patch.

## Feasibility gate

Before the remaining tasks were authored, A1 built jobscan-email twice (calibration, discarded): `review` 66.7 and 0, feature 100, regressions 0. A mean above 85, or 100 on both builds, would have stopped the replay. The gate passed.

## Freeze

`python3 -m eval freeze --experiment replay` writes the manifest, after the phase review and the replicate decision. Any later change to a frozen input needs a new freeze.
