# Census of review-caught defects

Question: what do Codex reviews catch in real build-loop work that the writer's own tests missed?

Each commit in the sample was made in response to a Codex plan or phase review (correctness pass or architecture pass) on code written by Claude, after the writer's own tests passed. Classify every distinct finding the commit fixes.

## Rules

- **Read-only.** In the source repo use only `git show`, `git log`, `git diff`, `git grep` and file reads. Never `checkout`, `switch`, `stash`, `reset`, `commit` or anything that writes; another session owns that repo.
- Read the commit message and the full diff (`git show <sha>`). Read surrounding code at `<sha>^` (`git show <sha>^:<path>`) when the diff alone doesn't show what was wrong. The review verdict may be quoted in `plans/` or `NOTES.md` at that commit; use it when it helps.
- One row per distinct finding. A commit that fixes four things gives four rows. A commit that is not a review fix (wrap-up, docs only, a feature) gives one row with `kind: "not-review"`.
- Judge from the code, not the commit message's wording. If a change is described as a fix but behaviour is identical, it is `structure` or `docs`.

## Row (JSON, one per line)

```
{"repo": "...", "sha": "...", "finding": "<one plain sentence: what was wrong>",
 "kind": "code-defect | test-defect | structure | docs | not-review",
 "defect_class": "wrong-logic | missed-edge-case | unstated-invariant | error-handling | resource-concurrency | data-integrity | security-isolation | contract-mismatch | null",
 "impact": "wrong-output | edge-only | latent | cosmetic",
 "severity": "high | medium | low",
 "fix_adds_test": true|false,
 "pinnable": true|false,
 "replay_candidate": true|false,
 "note": "<one line: evidence, file:line at <sha>^>"}
```

- `kind`: `code-defect` = the shipped code behaved wrongly or could; `test-defect` = a test was wrong, vacuous or missing an assertion while code was fine; `structure` = design, naming, duplication, layering with no behaviour change; `docs` = comments or docs only.
- `defect_class` (code-defect only, else null):
  - `wrong-logic`: the main path computes the wrong thing.
  - `missed-edge-case`: wrong on an unusual but valid input.
  - `unstated-invariant`: breaks a property the system relies on that no rule or test stated (ordering, idempotence, no lookahead, atomicity, state consistency across modules).
  - `error-handling`: failures swallowed, wrong exit code, crash instead of a typed error.
  - `resource-concurrency`: leaks, races, unbounded growth, timeouts.
  - `data-integrity`: persistence or schema writes something wrong or loses data.
  - `security-isolation`: secrets, permissions, sandbox escapes.
  - `contract-mismatch`: code disagrees with its SPEC, docs or CLI help.
- `impact`: `wrong-output` = normal use gives a wrong result; `edge-only` = only unusual inputs or conditions; `latent` = nothing wrong today, a likely future change breaks it; `cosmetic` = no behavioural effect.
- `severity`: high = would mislead the user or corrupt results/data; medium = real but bounded; low = minor.
- `pinnable`: a black-box behavioural test through a public interface could fail on the pre-fix code and pass after.
- `replay_candidate`: a `code-defect` with impact `wrong-output` or `edge-only`, `pinnable`, and inside a feature whose pre-feature state could be rebuilt from history.

## Output

Append rows to `eval/census/raw/<part>.jsonl` at the eval repo root (write with a single file write at the end, valid JSON per line). Return a five-line summary: rows written, counts by kind, the two most severe findings.
