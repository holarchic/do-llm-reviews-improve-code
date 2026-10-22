# Census of review-caught defects: summary

Sample: 97 commits matching review-fix wording that touch code, seed 20261022. All 37 from jobscan, 30 of 230 from private repo A, 30 of 142 from private repo B. Each commit was classified from its diff by a model reading against `BRIEF.md`, one row per finding. The labels are model judgements. Spot-checks of two high-severity jobscan rows (`81285eae17`, `04b0be79b3`) matched the diffs.

## Counts

| | jobscan | private repo A | private repo B | All |
|---|---|---|---|---|
| Commits sampled | 37 | 30 | 30 | 97 |
| Real review fixes | 37 | 16 | 30 | 83 |
| Commits fixing ≥1 code defect | 35 | 12 | 28 | 75 |
| Commits fixing a high-severity code defect | 5 | 3 | 14 | 22 |
| Code defects | 115 | 40 | 93 | 248 |
| Test defects | 13 | 7 | 16 | 36 |
| Structure | 28 | 13 | 26 | 67 |
| Docs | 10 | 20 | 18 | 48 |

## Code defects (248)

| Class | n | Share |
|---|---|---|
| missed-edge-case | 77 | 31% |
| error-handling | 44 | 18% |
| data-integrity | 26 | 10% |
| unstated-invariant | 23 | 9% |
| wrong-logic | 22 | 9% |
| contract-mismatch | 20 | 8% |
| resource-concurrency | 19 | 8% |
| security-isolation | 17 | 7% |

- **Impact:** edge-only 214 (86%), wrong-output in normal use 24 (10%), latent 10 (4%).
- **Severity:** high 30 (12%), medium 122 (49%), low 96 (39%).
- **Regression tests:** the fix added one for 213 of 248 (86%). 222 are pinnable by a black-box test.

## Reading

- The reviews act mostly as an adversarial tester. They harden edge cases, error paths, secret handling and persistence that the writer's own tests did not exercise. A minority are main-path bugs or broken invariants: 24 wrong-output defects and 23 unstated-invariant ones.
- High-severity catches cluster in two places. One is secrets reaching logs or redirects, mostly in jobscan. The other is data or state integrity: validators that pass fabricated input, partial reads that close live records, and deletions of files the code did not create.
- **Not shown:**
  - whether solo Claude would have shipped these defects, since the loop's spec and plan steps also shaped the code;
  - whether a second Claude pass would have found them;
  - the reviewer's false-positive rate, because rejected findings leave no commit.

  The replay measures the first two.
