# Replay: addendum from the run records

Not pre-registered, and no part of a verdict. The verdict in `replay-report.md` stands as the pre-registered rule gives it: **no effect**. This file reports what the run records hold and the frozen reports leave out.

- Builds: 24 (A1 and A3, 6 tasks, 2 replicates), the ones `replay-report.md` scored.
- Command: `python3 -B -m eval.results.replay_addendum`. It refuses records that don't reproduce the report's review means (44.6 and 72.1), its 14 round caps and 2 timeouts, or whose reviews today's verdict parser reads differently from the run.
- Every table is A3's unless it names A1: a solo build has no reviews and no rounds.

## Which review blocked, by round

Blocking reviews over reviews run. A review blocks when its verdict is not GREEN and it raises a HIGH or MEDIUM finding that cites a basis. The correctness and architecture reviews are the two passes of each code-review round.

| Review | Round 1 | Round 2 | Round 3 | Round 4 | Round 5 | All |
|---|---|---|---|---|---|---|
| plan | 10/12 | 9/10 | 6/9 | 4/6 | 4/4 | 33/41 |
| correctness | 2/11 | 3/11 | 2/10 | 5/10 | 3/10 | 15/52 |
| architecture | 11/11 | 10/10 | 10/10 | 10/10 | 10/10 | 51/51 |

- 10 builds ran code review to its round cap. In round 5 the architecture review blocked in 10 of them and the correctness review in 3.
- 4 of the 4 control builds are among them, though a control's phase carries no review-caught defect.

## Findings per review

HIGH and MEDIUM findings citing a basis, per review: median [lowest–highest].

| Review | Round 1 | Round 2 | Round 3 | Round 4 | Round 5 |
|---|---|---|---|---|---|
| plan | 2.5 [0–6] | 2 [0–3] | 1 [0–3] | 1.5 [0–3] | 1 [1–2] |
| correctness | 0 [0–3] | 0 [0–1] | 0 [0–2] | 0.5 [0–2] | 0 [0–2] |
| architecture | 8 [4–13] | 8 [1–13] | 7.5 [2–16] | 7.5 [1–13] | 7 [1–16] |

## What each fix changed

Lines added plus deleted by a fix, per build, as median [lowest–highest]; `plans/` and markdown are left out. A build's scores moved when its defects avoided, feature tests passed or existing tests broken differ from the round before.

| Fix | Builds | Source lines | Test lines | Builds whose scores moved |
|---|---|---|---|---|
| `fix-1` | 11 | 39 [16–765] | 18 [0–326] | 3 |
| `fix-2` | 10 | 122 [13–491] | 32 [19–177] | 1 |
| `fix-3` | 10 | 125.5 [35–326] | 39 [5–141] | 0 |
| `fix-4` | 10 | 74 [9–283] | 48 [6–550] | 0 |

## Where the reviewed arm stood before code review

Review-caught defects avoided (%), task mean over replicates. A3 at `build` is its `build` commit: after its plan and plan review, before any code review.

| Task | A1 | A3 at `build` | A3 at the end |
|---|---|---|---|
| jobscan-email | 33.3 | 83.3 | 83.3 |
| jobscan-greenhouse | 70.0 | 70.0 | 80.0 |
| jobscan-workable-gem | 25.0 | 12.5 | 37.5 |
| jobscan-setup-commands | 50.0 | 50.0 | 87.5 |
| Arm mean | 44.6 | 54.0 | 72.1 |

- Timed out, and scored where they stopped: jobscan-email r2 (architecture-review-2); jobscan-setup-commands r1 (build).

## Each defect, by build

Builds, of 2, that avoided the defect. The basis is the one its task's `defects.json` gives for the expected behaviour.

| Task | Defect | Basis | A1 | A3 |
|---|---|---|---|---|
| jobscan-email | password-in-server-reply | a norm | 0/2 | 2/2 |
| jobscan-email | accepted-message-reported-failed | a norm | 0/2 | 1/2 |
| jobscan-email | mail-to-another-address | a TASK.md statement | 2/2 | 2/2 |
| jobscan-greenhouse | semicolon-cuts-the-token | a TASK.md statement | 0/2 | 2/2 |
| jobscan-greenhouse | host-case-or-port-not-a-board | a TASK.md statement | 1/2 | 0/2 |
| jobscan-greenhouse | posting-text-lost-to-over-unescaping | a TASK.md statement | 2/2 | 2/2 |
| jobscan-greenhouse | office-that-is-not-an-object-accepted | a TASK.md statement | 2/2 | 2/2 |
| jobscan-greenhouse | id-with-a-lone-surrogate-accepted | a TASK.md statement | 2/2 | 2/2 |
| jobscan-workable-gem | encoded-job-link-is-a-board | a TASK.md statement | 0/2 | 2/2 |
| jobscan-workable-gem | greenhouse-token-checked-before-folding | existing behaviour | 0/2 | 0/2 |
| jobscan-workable-gem | workable-place-listed-twice | a TASK.md statement | 2/2 | 0/2 |
| jobscan-workable-gem | gem-remote-marker-lost-to-a-substring | a TASK.md statement | 0/2 | 1/2 |
| jobscan-setup-commands | preview-counts-boards-the-run-wont-read | a TASK.md statement | 0/2 | 2/2 |
| jobscan-setup-commands | init-empty-flag-falls-back-to-questions | a TASK.md statement | 2/2 | 2/2 |
| jobscan-setup-commands | email-empty-sender-taken-as-default | a TASK.md statement | 2/2 | 2/2 |
| jobscan-setup-commands | boards-add-crashes-on-unwritable-text | a TASK.md statement | 0/2 | 1/2 |

- A3 avoided the defect in more builds than A1 on 7 defects, in fewer on 2, and in as many on 7.
- The replicates of an arm had the same outcome on 15 of 16 defects for A1 and 13 of 16 for A3.

Builds that avoided the defect, by basis:

| Basis | Defects | A1 | A3 |
|---|---|---|---|
| a TASK.md statement | 13 | 15/26 | 20/26 |
| a norm | 2 | 0/4 | 3/4 |
| existing behaviour | 1 | 0/2 | 0/2 |

## How firm the gain is

The report's gain is +27.5 points. A sign-flip test asks how often differences of these sizes, each given a random sign, add up to at least what was seen; it is exact and one-sided.

- By task (4): differences +50.0, +10.0, +12.5, +37.5; t = 2.82 on 3 degrees of freedom; sign-flip p = 0.0625, the smallest value 4 tasks can give.
- By defect (16, treated as independent although they share tasks): mean difference +25.0 points; sign-flip p = 0.0781.
- These describe this sample. They are not a significance claim, and the pre-registered rule is a continuation rule.

## Reading

- The loop under test ends a review on GREEN, on the user's ruling over a disagreement, or at the round cap, and its re-review prompt carries the previous round's findings with what was done about each (`skills/codex-review/SKILL.md`). The harness has no user and its re-review prompts carry no previous round, so a reviewed build ends on GREEN or at the cap. The cost and the round caps in `replay-report.md` describe that unattended loop.
- A fix raised a build's defects avoided 4 times. 2 of them followed a round in which the correctness review did not block, so the fix answered the architecture review alone.
- Lines changed are a count, not a judgment: these tests cannot see whether the later fixes improved the design.
