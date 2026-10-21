# Pre-registration: build-loop eval pilot

Fixed before any model sees a pilot task. The frozen inputs (tasks, prompt sources, harness code, judge prompt, model settings, schedule seed) are hashed in `MANIFEST.json`.

## Question

Does a script-driven spec-first loop — plan review, then a correctness pass and an architecture pass over the build, against a fixed 40-rule rubric — make a weak coding model's code measurably better than the same model working alone? Does a reviewer from another model family add anything over a same-model reviewer?

This is an exploratory pilot that decides whether to spend on larger models. Five tasks with one run each cannot establish statistical significance, and the report says so.

## Arms

Writer in every arm: `claude-haiku-5-5`.

| Arm | Reviewer | Writer sees |
|---|---|---|
| A1 solo | none | TASK.md |
| A1r solo replicate | none | TASK.md (primary metric only, to show run-to-run spread) |
| A2 loop-same | `claude-haiku-5-5` via Claude Code, read-only tools | TASK.md + the build-loop `## Code` rules + the rubric |
| A3 loop-cross | `gpt-6-luna` via Codex CLI, read-only sandbox, effort `high` | same as A2 |

The treatment bundles rules, rubric, review and extra compute. A2 vs A3 compares reviewer configurations (model, CLI and tool policy together), not model families in isolation.

Loop: writer plans → reviewer plan review → revise until GREEN (cap 5) → writer builds with tests → reviewer correctness pass and architecture pass → writer fixes HIGH/MEDIUM until both GREEN (cap 5 per pass). TASK.md is the authoritative contract for every reviewer.

## Tasks

Five stdlib-Python tasks, each with a writer-visible TASK.md that states the exact interface and every behaviour the hidden tests check. Hidden suites pass their reference solution and fail every known-wrong variant. Each task has a follow-up change (FOLLOWUP.md) with its own hidden suite.

## Measures

- **Primary:** hidden-test pass fraction of the build, in percentage points. The denominator is the test IDs collected from the reference (`expected.json`). Missing, uncollected, errored, skipped or unfinished tests fail.
- **Secondary:**
  - follow-up new-behaviour pass fraction;
  - follow-up regressions: base test IDs that passed before the follow-up and fail after it. Persistent failures and repaired failures are reported separately;
  - pairwise judge preference;
  - static metrics: ruff violations, LOC, and the highest branch count of any one function.
- **Diagnostic:** wall time, review rounds, terminal classes, tokens, the A1 vs A1r spread.

## Judging

Judges are `claude-fable-5-1` and `gpt-6-astra`. Each sees TASK.md plus the source and tests of two builds, labelled X and Y. SPEC, plans, reviews and git history are removed. The judge prompt is frozen. Judges rank builds on four things: correctness risk against TASK.md, clarity, structure and ease of change, and test quality. Test count and rubric-like comments earn nothing by themselves.

Each pair (A1–A2, A1–A3, A2–A3) is judged in both orders. A consistent pick is a win; otherwise it is a tie. Malformed output is retried once, then counts as a tie for that order.

**Calibration:** for each task the judge compares the reference with a degraded reference, which has three injected quality defects but still passes the hidden suite. A judge must prefer the reference in both orders on at least 4 of the 5 tasks. A judge that fails is excluded, and the report says so.

## Decision rule

GO to the next tier if either holds:

- A2 or A3 beats A1 on the primary measure by at least 10 percentage points (arm means).
- Both judges passed calibration, and A2 or A3 beats A1 on at least 4 of 5 tasks where both judges agree on the win, without losing the primary measure by more than 5 points. Ties are not wins, and the denominator is 5.

Otherwise the verdict is "did not meet the continuation threshold". The report also shows:

- the A1–A1r spread;
- primary-trigger sensitivity: whether substituting A1r for A1 changes the primary branch's outcome;
- per-task paired differences.

## Terminal classes

| Class | Treatment |
|---|---|
| Completed | Normal |
| Model timeout (writer, reviewer, follow-up) | HEAD is scored |
| Malformed reviewer verdict | Counts as YELLOW and uses up the round |
| Malformed judge output | Retried once, then a tie for that order |
| Infrastructure failure: provider error, non-zero exit, missing final result, judge timeout, harness exception, or the auditor flagging access to private material | The step is retried from the last harness commit, up to 3 attempts in total |

With no operator in the loop, a RED verdict is handled like YELLOW (the writer revises), and a YELLOW whose findings are all LOW does not block, as in the product. A plan review that reaches the cap without GREEN still goes on to the build. A phase review that reaches the cap without GREEN has HEAD scored. If a step still fails after 3 attempts, the pilot is INCOMPLETE and gets no verdict. Every task stays in every arm's denominator.

## Schedule

Runs are shuffled and interleaved with seed 20261021, three at a time.

## Freeze

`MANIFEST.json` hashes this file too. The freeze commit is the one that adds `MANIFEST.json` (`git log --diff-filter=A -- eval/MANIFEST.json`). `run`, `judge` and `report` refuse to start if any frozen file differs from its hash.
