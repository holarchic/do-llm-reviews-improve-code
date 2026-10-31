# do-llm-reviews-improve-code

Codex reviewing Claude's code avoided +27.5 points more real review-caught defects, but broke features and existing tests.

> **Status:** v1 replay complete (October 2026); the v2 program is in preparation. The harness, tasks and v1 records are reusable; the v2 tasks and pre-registration can still change.

| Arm (Opus 5.5 writes) | Review-caught defects avoided | Feature tests passed (caught tasks) | Existing tests broken |
|---|---|---|---|
| A1 — solo | 44.6% | 100.0% | 0.0% |
| A3 — reviewed by Codex `gpt-6.1-sol`, spec-first loop | 72.1% | 92.3% | 0.3% |

Pre-registered verdict: **no effect**. The review gain passed (+27.5 points, needed +15; higher on 4 of 4 caught tasks), but the loop failed both harm guards: feature −7.7 points against a −5 floor, and regressions +0.3 points against a rule that they must not rise. Four caught and two control tasks from one repo, two replicates each, 24 builds. Full report: [`eval/results/replay-report.md`](eval/results/replay-report.md).

## The question

What does an LLM reviewer add to an LLM builder's code, and at what cost? Benchmarks score reviewers by the comments they write. This repo scores the code that ships after review, on defects that real reviews caught, against the damage the review loop does along the way.

## How

- **Replay.** Each task rebuilds a real phase of a repo built with a Claude-writes, Codex-reviews loop ([jobscan](https://github.com/holarchic/job-board-scanner), public 2026-10-11, after the writer's knowledge cutoff). The session starts from the phase's pre-feature commit with no history, and its tool commands get no network beyond localhost.
- **Hidden tests.** Each defect the historical review caught gets a test proven to fail on the historical pre-review build. Feature tests and the repo's existing suite measure what the build does and what it breaks.
- **Pre-registration.** The rule, tasks, prompts, harness and the loop's skills (`skills/`) are hashed into a manifest before the first scored run; `run` and `report` refuse to start if anything changed.
- **Isolation.** Every session runs with project-only settings, OS-level read denial of hidden material, and probes with positive controls.

Structure: [ARCHITECTURE.md](ARCHITECTURE.md). Post-hoc analysis, kept apart from the verdict: [`eval/results/replay-post-hoc.md`](eval/results/replay-post-hoc.md). It traces 25 of A3's 31 harm failures to task wording or a test of a private name, which the v2 program corrects before re-running.

## Reproduce

Python 3, standard library only. Scoring and reports make no model calls. Model runs need Claude Code and Codex CLI logged in.

```bash
python3 -m unittest discover -s eval/tests -t .     # expect: OK
python3 -m eval report --experiment replay          # needs the run records in eval/data/
python3 -m eval validate | probe | calibrate | freeze | run | judge | report [--experiment NAME]
```

The v1 builds took 1,475 model minutes in all (A3 1,352.5, A1 122.5) on subscription quota.

## Limits

- One source repo, built with the same loop; caught tasks are selected on phases where review found something. Controls check damage to existing behaviour, not selection.
- A3 vs A1 measures the whole loop package (plan, rubric, review, extra compute). The same-model review control (A2) ran only if the loop passed, so it didn't.
- Four tasks with two replicates is a pre-registered continuation rule, not a significance test.
- The closest published work, arXiv [2607.21656](https://arxiv.org/abs/2607.21656), found Codex reviewing Claude *lowered* LiveCodeBench pass rates (one static round, no test execution).

## Attribution

For those who care about such things, the majority of the code in this repo was produced by Claude Opus 5.5 (Anthropic), with david guiding Claude, providing insights, direction and critical review throughout the process.

## License

MIT
