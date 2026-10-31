# Pre-registration: per-round scoring of the replay's reviewed builds

Fixed before any round is scored. It applies to the builds already on record under the replay freeze (`9e569a2`, `eval/manifests/replay.json`): twelve A3 builds (Opus 5.5 writing, Sol reviewing), two replicates of each of the six tasks. No model is called. Every round is scored by the same function, against the same frozen hidden suites, that scored the builds' final states.

## Question

Where across the code-review rounds does the loop's gain in defects avoided arrive, and where do regressions arrive? A second question: does round 1 deliver enough of the final gain to admit A3-2 (Sol reviewing, code-review cap 2) into Phase B?

## Rounds

- **Round 0** is the `build` commit, the writer's code before any code review. **Round k** is the `fix-k` commit, the code after k code reviews and k fixes. The loop runs at most five code reviews and four fixes, so the last possible round is 4. Plan steps (`plan`, `plan-revise-k`) are not rounds.
- After every writer step the harness commits with the step's name as the subject. A round's commit is the one commit in the build's repository whose subject is the writer step named in its run record. It is never found by counting back from HEAD. A build whose recorded writer step has no commit, or more than one, is refused and listed.
- **Capped** builds (`review_capped`) have rounds 0–4.
- **Passed** builds stopped because the last round's reviews were non-blocking: GREEN, or LOW issues only. No timeout flag, not capped. Their last state carries forward flat to round 4, and they stay eligible for the ratios. None is on record.
- **Truncated** builds stopped on a timeout (any `timeout:` flag). They are plotted up to their last round and are absent after it. They are excluded from every ratio. On record: `jobscan-email--A3--r2` (last round 1) and `jobscan-setup-commands--A3--r1` (last round 0).

## Scores

- Every round of every build is re-scored, the last round included, with `source.score_build`: defects avoided (`review`), feature tests passed (`feature`) and base tests broken (`regressions`), in percentage points.
- The record's stored score is a reproduction check, compared test by test and defect by defect, not only on the aggregates. Any build whose last round differs from its record is listed, with the tests that differ. The curve and the trigger use the re-scored values throughout.
- Scoring refuses to start if any file frozen in `eval/manifests/replay.json` differs, except `eval/__main__.py` (the `rounds` command) and `eval/harness/loop.py` (an optional commit argument to the function that exports a build's files). The checked files include every task file, the scoring modules (`tasks.py`, `source.py`, `score.py`, `runner.py`, `procs.py`) and the experiment table that fixes the six tasks, two replicates and the A3 arm.
- **Completeness:** all twelve builds must be on record and mapped, and every one of their rounds scored. Otherwise the result is INCOMPLETE: the curve is shown for what was scored, no trigger is issued, and A3-2 is not admitted.

## Curve (descriptive)

For each round k, the mean change from round 0 is taken over the builds that have a state at k. Defects avoided and feature use caught tasks only. Regressions use caught tasks and controls, the scopes of the replay's guards. The number of builds is shown at every round, and the per-build values are tabled. After a truncation the remaining builds weight the tasks unequally, so the curve is build-weighted and descriptive. The trigger below is task-balanced.

## A3-2 trigger

- Caught tasks only: the controls have no defects.
- A build is **eligible** when it is not truncated and its final gain (defects avoided at its last state minus round 0) is positive.
- An eligible build's **ratio** is (gain at round 1) / (final gain). A negative gain at round 1 gives a negative ratio, which is kept.
- Ratios are averaged within a task, then across the caught tasks that have at least one eligible build.
- **The trigger is met when that mean is at least 0.75 and at least 3 caught tasks contribute an eligible build.** Otherwise it is not met, including when no build is eligible, and A3-2 is not scheduled in Phase B.
- Each eligible build's numerator, denominator and ratio are published. A ratio above 1 means later fixes erased earlier gains. Such ratios are kept and explained. A small final gain can make a ratio extreme.
- The trigger only admits an arm. It is never evidence that two rounds are enough.
- Sensitivity: the median of the task-level means is reported beside the mean.

## Reporting

`python3 -m eval rounds --experiment replay` writes `eval/data/replay/rounds.md`. That report is committed as `eval/results/replay-rounds.md`. It states this file's sha256 and is published whatever the outcome.
