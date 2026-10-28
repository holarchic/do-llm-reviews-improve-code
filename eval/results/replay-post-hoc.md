# Replay: post-hoc analysis

Not pre-registered. The verdict in `replay-report.md` stands as the pre-registered rule gives it: **no effect**. This file explains why the two harm guards failed, and what the failures say about the method.

## Every non-review test A3 failed

Each failing feature or regression test was rerun on the scored copy of its build. A test counts once, whatever its subtests. A1 failed none.

| Build | Tests | Cause | Class |
|---|---|---|---|
| workable-gem r1, r2 | 6 feature each | TASK.md: "text fields are strings". The real Workable payload, the hidden fixture and the historical code all carry `region: null`; both builds reject it, failing the board. | spec artefact |
| workable-gem r1 | 1 feature | A Gem job with id `" "` is accepted. TASK.md says "`id` is a non-empty string"; the historical code rejects a blank one. | ambiguous |
| workable-gem r2 | 1 regression | Every HTTP 429 response is no longer logged. Not in TASK.md. | genuine |
| setup-commands r1, r2 | 4 regression each | TASK.md: "Exit 1: finished with something left undone"; "An error prints … nothing on stdout". Both builds treat exit 1 as an error, so `run` prints no summary line when it leaves work undone. | spec artefact |
| setup-commands r1, r2 | 2 regression each | TASK.md lists "a run holding the database" under exit 1; both builds apply it to `run` and `discover`, which stay at exit 2 historically (only `preview` moved). | spec artefact |
| setup-commands r1, r2 | 1 regression each | `email` with no install and no terminal names the terminal before the missing profile. TASK.md sets neither order. | ambiguous |
| setup-commands r1, r2 | 1 feature each | "Sending a test email to … ..." goes to stderr; TASK.md says it prints, and the test reads stdout. | genuine |
| setup-commands r2 | 1 feature | The hidden test holds the lock through `cli._run_lock`, a private name TASK.md never gives; the build renamed it. | test artefact |

Of 31 failed tests, 25 trace to TASK.md wording or a hidden test's reach into a private name, 3 to ambiguity, and 3 are genuine.

## Why A3 and not A1

TASK.md paraphrases each historical phase; the hidden feature and regression tests come from the code itself. Where the paraphrase overstates, a solo build keeps the existing code's conventions and passes. The loop's reviewer holds the build to the written text over up to five rounds, and the build complies. The fairness audit checked each review test against TASK.md; it never checked TASK.md against the feature and regression tests.

## The guards without the artefacts

Excluding the spec and test artefacts and the ambiguous tests:

| Guard | Pre-registered | Without artefacts |
|---|---|---|
| Feature, caught tasks (A3 − A1) | −7.7 points (floor −5): fail | −0.6 points: pass |
| Regressions, all tasks (A3 − A1) | +0.3 points (must not rise): fail | +0.03 points (one 429 test): fail |

The regression guard has zero tolerance against a comparator that broke nothing, so one test in 239 decides it.

## Cost and convergence

A3 used 1,352 model minutes to A1's 122.5 (11×). Twelve builds hit a round cap 14 times: Sol rarely returned GREEN within five rounds. Two sessions hit the wall clock; one was the writer's build step, so setup-commands r1 was scored without any phase review, at `review` 75.

## Design judgment

Astra, a Codex model like the reviewer, picked A3 in all 8 surviving judgments; Fable split 4–4. Net A3: email +2, greenhouse 0, workable-gem +2, setup-commands +4. Uncalibrated, and the judge that agrees with the reviewer is from its family.

## For a second run

- Check every TASK.md statement against the feature and regression tests, not only the review tests; fix the `region` and exit-code passages.
- Keep hidden tests to interfaces TASK.md names or base defines.
- Give the regression guard a tolerance (for example 1% of the regression set) and the feature floor one in tests, not points.
- A reviewer from a third family (Gemini) separates the loop from the historical reviewer's family.
