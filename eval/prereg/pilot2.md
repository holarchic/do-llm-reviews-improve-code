# Pre-registration: build-loop eval pilot 2

**Withdrawn before its freeze; no scored run.** Calibration and two writer screens showed that every open-weight writer that stays on task passes these tasks solo at 73–100%. The 40–70% band came only from writers derailing, a different failure from the one under test. The replay (`eval/prereg/replay.md`) replaces this design. The text below is the draft as it stood.

Fixed before any scored run. The frozen inputs are hashed in `eval/manifests/pilot2.json`: this file, the five tasks, the harness code (including model ids, arms, prompts, thresholds, prices and the schedule seed) and the shipped skill files the prompts are built from.

## Question

Does a script-driven spec-first loop make a weak open-weight model's code more correct, or its tests better, than the same model working alone? The loop is: plan review, then a correctness pass and an architecture pass over the build, against a fixed 40-rule rubric. A second question: does a reviewer from another model family add anything over the writer's own model as reviewer?

The first pilot could not answer this. Every build scored 100% on its hidden tests, and its LLM judges split along family lines. Pilot 2 changes three things: harder tasks calibrated against the solo arm, a planted-bug score instead of judges, and replicates in every cell.

## Arms

The writer in every arm is `qwen/qwen3-coder-30b-a3b-instruct` (open weights: `Qwen/Qwen3-Coder-30B-A3B-Instruct`), run through Claude Code and routed through OpenRouter. The first choice, DeepSeek V4.1 Flash, passed 100% of every calibrated task it was given solo, leaving no room to measure.

| Arm | Reviewer, through Codex CLI (effort `high`), routed through OpenRouter |
|---|---|
| A1 solo | none |
| A2 loop-same | `qwen/qwen3-coder-30b-a3b-instruct` |
| A3 loop-cross | `z-ai/glm-4.7-flash` (open weights: `zai-org/GLM-4.7-Flash`) |

Both reviewers run through the same CLI with the same tool policy: they read files and run commands, but cannot write. A2 and A3 therefore differ only in the reviewer model. The treatment bundles four things: the rules, the rubric, the review, and extra compute.

The loop, its prompts, round caps and terminal rules are the pilot's (`eval/PREREG.md`):

1. The writer plans.
2. The reviewer reviews the plan.
3. The writer revises until GREEN (cap 5).
4. The writer builds, with tests.
5. The reviewer runs a correctness pass and an architecture pass.
6. The writer fixes HIGH/MEDIUM findings until both passes are GREEN (cap 5 per pass).

A HIGH/MEDIUM finding without a `Basis:` counts as LOW. TASK.md is the authoritative contract for every reviewer.

## Tasks

There are five stdlib-Python tasks, each with numbered rules that state every behaviour the hidden tests check:

- `calc-expr`: expression evaluator.
- `cron-next`: cron next fire times.
- `ignore-match`: gitignore matching.
- `json-patch`: RFC 6902.
- `semver-range`: npm-style ranges.

Each hidden suite passes its reference and fails 2–3 plausible wrong implementations. Each task carries 12 planted bugs (`mutants/`): reference copies with one small bug each, each on a different rule. Each one fails the hidden suite but passes at least half of it.

**Calibration.**
- Before the freeze, solo builds of each task (the A1 configuration, two per round, up to three rounds) were scored. Tasks were retuned until the solo mean fell within 40–70%.
- Those runs are discarded.
- After the tasks were locked, one A2 and one A3 build ran on one task to measure cost. Their scores were not used for any decision.
- The calibration log (task hashes, changes and solo scores per round) is at the end of this file.

## Measures

- **Primary:** hidden-test pass fraction of the build, in percentage points. The denominator is the test IDs collected from the reference (`expected.json`). Missing, uncollected, errored, skipped or unfinished tests fail.
- **Planted-bug score:** the share of the task's planted bugs that the build's own tests catch.
  - Each planted bug is overlaid onto a fresh copy of the build, replacing the reference's files; the build's tests run as `python3 -m unittest discover` would run them.
  - A bug is caught when a test that passes on the reference fails on the mutant.
  - Tests that fail on the reference, or fail to load, are excluded and reported. The score therefore covers only the build's tests that transfer to the reference's public interface.
- **Descriptive only:**
  - A3 vs A2;
  - the spread across replicates;
  - per-task paired differences;
  - static metrics (ruff violations, LOC, the highest branch count of any function);
  - the builds' own test counts;
  - review rounds and caps;
  - flags;
  - model time;
  - tokens and estimated cost (list prices);
  - actual OpenRouter spend.

## Unit and decision rule

The unit is the **task mean**: the mean over replicates of one task × arm. An arm mean is the mean of its five task means. For each treatment T (A2, A3) against A1:

- **Correctness effect:** T's arm mean primary exceeds A1's by at least 10 points, AND T's task mean is strictly greater than A1's on at least 4 of the 5 tasks.
- **Test-quality effect:** T's arm mean planted-bug score exceeds A1's by at least 15 points, AND T's task mean is strictly greater on at least 4 of the 5 tasks, AND T's arm mean primary is no more than 5 points below A1's.
- **Ceiling guard:** if A1's arm mean primary is 90 or more, the correctness branch reads "uninformative (ceiling)" and cannot fire. There is no floor guard: a low baseline still leaves the comparison measurable, and the report states when A1 fell outside the calibration band.

**Verdict:**
- **LOOP EFFECT**, naming each arm and branch that fired;
- otherwise "no loop effect at the pre-registered thresholds";
- **INCOMPLETE**, with no verdict, if any build is missing or invalid after retries, a job was refused at the spend cap, or any routed session reported a model it was not routed to.

A routing violation is never silently excluded: recovery is corrected routing, a new freeze and fresh scored runs. A3 vs A2 is descriptive only.

These thresholds are an exploratory continuation rule for spending on ablations (T-001.8), not a significance test.

## Terminal classes

As in the pilot:

| Class | Handling |
|---|---|
| Completed | Normal. |
| Model timeout (writer or reviewer) | HEAD is scored. |
| Malformed reviewer verdict | Counts as YELLOW and uses its round. |
| Infrastructure failure (provider error, non-zero exit, missing final result, harness exception) | Retried from the last harness commit, up to 3 attempts. |

Still failing after 3 attempts → INCOMPLETE. RED is handled like YELLOW. A YELLOW whose findings are all LOW does not block. A plan review at the cap still goes on to the build. A phase review at the cap has HEAD scored.

## Isolation, routing and key handling

Isolation is the pilot's: no operator instructions; OS-level read denial of private material (hidden suites, references, mutants, other runs, the operator's configuration); unlistable, randomly named run dirs.

Routing goes through OpenRouter:
- **Claude Code** gets its key through `apiKeyHelper`, never through an environment. Every model alias it might use for background work is mapped to the routed model.
- **Codex** holds the key in its own process environment. Its config excludes the credential variables from its tool shells. It gets no operator ChatGPT login.
- **Key file:** its directory is a private root.

Before the scored runs, `python3 -m eval probe --experiment pilot2` shows:
- no credential variable reaches a tool shell;
- the key file is unreadable;
- every routed session reports only its routed model;
- no saved file holds the key.

Every scored session is checked for an unrouted model again. Every run's saved files are scanned and redacted after its record is written.

## Spend and schedule

- **Spend cap:** jobs are admitted only while the key's spend since the scored phase began is under $10. A ledger keeps the phase's baseline across resumes, and unreadable usage refuses a job. The cap is checked at admission: jobs already running finish, so final spend can exceed it by what they cost.
- **Calibration:** had its own $5 cap.
- **Schedule:** 5 tasks × 3 arms × 3 replicates, shuffled with seed 20261201, five jobs at a time, never two of the same task at once.

## Threats to validity

- **Selection on the solo arm.** Tasks were tuned until solo landed in band, so fresh solo runs can regress either way.
- **Routing variance.** OpenRouter routes requests across providers and quantisations (fp4/fp8). Every arm shares that routing.
- **Pilot scale.** 5 tasks × 3 replicates is a pilot, not a significance test.
- **Narrow test-quality measure.** The planted-bug score counts only tests that transfer to the reference's public interface.
- **Development exposure.** One A2 and one A3 cost sample ran on a locked task before the freeze; their scores were not used.
- **Text tool calls.** The writer sometimes writes a tool call as text instead of making it, which ends that session early. This is the writer's own weakness and hits every arm. But loop arms run more writer sessions, so they have more chances to recover from it.
- **Same-family authorship.** The tasks were authored and audited by Claude models, not by the writer's or reviewers' families.

## Freeze

The freeze commit is the one that adds `eval/manifests/pilot2.json`. `run` and `report` refuse to start if any frozen file differs from its hash.

## Calibration log

All runs were solo, discarded and unscored. Primary is the hidden-test pass percentage; planted is the planted-bug catch percentage.

**Round 1, DeepSeek V4.1 Flash** (2 reps):

| Task | Primary | Planted |
|---|---|---|
| calc-expr | 100 / 100 | 100 / 100 |
| semver-range | 100 / 100 | 91.7 / 100 |
| ignore-match | 100 / 100 | 100 / 100 |

About $0.06 per build.

**Writer screen 1** (1 build each, calc-expr / semver-range):

| Writer | calc-expr | semver-range |
|---|---|---|
| Qwen3-Coder-30B-A3B | 63 | 24 |
| Mistral Small 3.2 | ~0 | ~0 |

**Round 1, Qwen3-Coder-30B-A3B** (2 reps):

| Task | Primary |
|---|---|
| calc-expr | 82 / 0 |
| cron-next | 49 / 0 |
| ignore-match | 72 / 71 |
| json-patch | 66 / 41 |
| semver-range | 29 / 0 |

Four of the ten builds derailed. About $0.22 per build.

**Writer screen 2** (`:exacto` routing; json-patch / calc-expr / semver-range):

| Writer | Primary | Cost per build |
|---|---|---|
| MiMo-V2.6-Flash | 100 / 100 / 100 | about $0.05 |
| DeepSeek-V4-Flash-0731 | 100 / 100 / 0 | about $0.015 |
| Laguna-XS-2.1 | 95 / 94 / 84 | about $0.28 |
| Nemotron-3.5-Lightning | 73 / 87 / 77 | — |
| Nex-N2.5-Pro | 100 / 92 / 100 | — |

DeepSeek-V4-Flash-0731's 0 on semver-range was a runaway thinking session. Nex-N2.5-Pro hit its time limit on every build. GLM-5.3-Flash was rate-limited on every build and gave no data.
