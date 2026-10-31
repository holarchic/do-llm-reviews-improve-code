# Per-round scores: replay, arm A3

- Rule: `eval/prereg/replay-rounds.md` (sha256 `cb77d4744be2eff0176a238cc3961dc55dbe5a036aaade5e9b276a9b467a645f`)
- Command: `python3 -m eval rounds --experiment replay --arm A3`
- Builds under manifest digest `90fb8bc3d072aeefe752fad511540536df38b4d29921f6ce05773a44f32d7ceb`; round 0 = `build`, round k = `fix-k`

**A3-2 trigger:** met: 3 caught tasks contribute an eligible build (needs 3); mean round-1 share 0.833 (needs 0.75).

## A3-2 trigger

Eligible builds: caught task, not truncated, final gain in defects avoided above round 0 positive.

| Build | Gain at round 1 | Final gain | Ratio |
|---|---|---|---|
| jobscan-greenhouse--A3--r2 | +20.0 | +20.0 | 1.000 |
| jobscan-workable-gem--A3--r1 | +25.0 | +25.0 | 1.000 |
| jobscan-workable-gem--A3--r2 | +0.0 | +25.0 | 0.000 |
| jobscan-setup-commands--A3--r2 | +75.0 | +75.0 | 1.000 |

| Task | Mean ratio |
|---|---|
| jobscan-greenhouse | 1.000 |
| jobscan-setup-commands | 1.000 |
| jobscan-workable-gem | 0.500 |

- Mean of task means 0.833; median 1.000 (sensitivity).
- A ratio above 1 means later fixes erased part of an earlier gain. The trigger admits an arm; it is not evidence that two rounds suffice.

## Change from round 0, by round

Mean over the builds with a state at that round (n). Defects avoided and feature tests over caught tasks; existing tests broken over every task. Build-weighted and descriptive: after a truncation the remaining builds weight the tasks unequally.

| Round | Defects avoided | Feature tests passed | Existing tests broken |
|---|---|---|---|
| 0 | +0.0 (n=8) | +0.0 (n=8) | +0.0 (n=12) |
| 1 | +17.1 (n=7) | -0.4 (n=7) | +0.0 (n=11) |
| 2 | +24.2 (n=6) | -0.4 (n=6) | +0.0 (n=10) |
| 3 | +24.2 (n=6) | -0.4 (n=6) | +0.0 (n=10) |
| 4 | +24.2 (n=6) | -0.4 (n=6) | +0.0 (n=10) |

## Per build

Defects avoided / feature tests passed / existing tests broken, in points; a carried-forward state is marked *.

| Build | Stop | Round 0 | Round 1 | Round 2 | Round 3 | Round 4 |
|---|---|---|---|---|---|---|
| jobscan-email--A3--r1 | capped | 66.7 / 100.0 / 0.0 | 66.7 / 100.0 / 0.0 | 66.7 / 100.0 / 0.0 | 66.7 / 100.0 / 0.0 | 66.7 / 100.0 / 0.0 |
| jobscan-email--A3--r2 | truncated | 100.0 / 100.0 / 0.0 | 100.0 / 100.0 / 0.0 |  |  |  |
| jobscan-greenhouse--A3--r1 | capped | 80.0 / 100.0 / 0.0 | 80.0 / 100.0 / 0.0 | 80.0 / 100.0 / 0.0 | 80.0 / 100.0 / 0.0 | 80.0 / 100.0 / 0.0 |
| jobscan-greenhouse--A3--r2 | capped | 60.0 / 100.0 / 0.0 | 80.0 / 100.0 / 0.0 | 80.0 / 100.0 / 0.0 | 80.0 / 100.0 / 0.0 | 80.0 / 100.0 / 0.0 |
| jobscan-workable-gem--A3--r1 | capped | 25.0 / 70.8 / 0.0 | 50.0 / 70.8 / 0.0 | 50.0 / 70.8 / 0.0 | 50.0 / 70.8 / 0.0 | 50.0 / 70.8 / 0.0 |
| jobscan-workable-gem--A3--r2 | capped | 0.0 / 75.0 / 0.0 | 0.0 / 75.0 / 0.0 | 25.0 / 75.0 / 0.4 | 25.0 / 75.0 / 0.4 | 25.0 / 75.0 / 0.4 |
| jobscan-setup-commands--A3--r1 | truncated | 75.0 / 97.5 / 1.7 |  |  |  |  |
| jobscan-setup-commands--A3--r2 | capped | 25.0 / 97.5 / 1.7 | 100.0 / 95.0 / 1.7 | 100.0 / 95.0 / 1.7 | 100.0 / 95.0 / 1.7 | 100.0 / 95.0 / 1.7 |
| jobscan-watchlist--A3--r1 | capped | — / 100.0 / 0.0 | — / 100.0 / 0.0 | — / 100.0 / 0.0 | — / 100.0 / 0.0 | — / 100.0 / 0.0 |
| jobscan-watchlist--A3--r2 | capped | — / 100.0 / 0.0 | — / 100.0 / 0.0 | — / 100.0 / 0.0 | — / 100.0 / 0.0 | — / 100.0 / 0.0 |
| jobscan-company-notes--A3--r1 | capped | — / 100.0 / 0.0 | — / 100.0 / 0.0 | — / 100.0 / 0.0 | — / 100.0 / 0.0 | — / 100.0 / 0.0 |
| jobscan-company-notes--A3--r2 | capped | — / 100.0 / 0.0 | — / 100.0 / 0.0 | — / 100.0 / 0.0 | — / 100.0 / 0.0 | — / 100.0 / 0.0 |

## Reproduction

Each build's last round, re-scored, against the score its record holds, test by test.

- jobscan-email--A3--r1: matches
- jobscan-email--A3--r2: matches
- jobscan-greenhouse--A3--r1: matches
- jobscan-greenhouse--A3--r2: matches
- jobscan-workable-gem--A3--r1: matches
- jobscan-workable-gem--A3--r2: matches
- jobscan-setup-commands--A3--r1: matches
- jobscan-setup-commands--A3--r2: matches
- jobscan-watchlist--A3--r1: matches
- jobscan-watchlist--A3--r2: matches
- jobscan-company-notes--A3--r1: matches
- jobscan-company-notes--A3--r2: matches
