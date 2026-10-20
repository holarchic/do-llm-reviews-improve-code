---
name: codex-review
description: "Run Codex CLI as the independent reviewer over code OR plans. Codex is required — no fallback reviewer. Assembles structured prompts with project context + load-bearing claims + the bundled Code Architecture rubric (Karpathy 4 + 40 primitives + Cloudflare-style exclusions). Code review runs two passes (correctness over the diff, architecture over full touched modules). Returns a structured GREEN/YELLOW/RED verdict with file:line refs. Modes: /codex-review (code, auto-detect scope), /codex-review --uncommitted, /codex-review --base <branch-or-sha>, /codex-review --commit <sha>, /codex-review plan <path>. Called by the build-loop skill at Build Loop steps 5 and 7."
---

Codex is required: no installed and logged-in Codex, no review — stop and tell the user. Never substitute a Claude reviewer.
Every prompt: project context + load-bearing claims + specific concerns + bundled rubric + structured verdict format.
The reviewer is read-only: Codex on the host runs `--sandbox=read-only`; bypass only inside a container.
Editing in response to a verdict is not "addressed" — re-invoke until GREEN or surface to the user.
Cap iteration at 5 rounds per pass; surface disagreement, never loop forever.
Plans reviewed BEFORE execution, code reviewed AFTER implementation.

# Codex Review

An external second-opinion reviewer for plans (pre-execution) and code (post-implementation). The builder cannot grade its own work — self-grading drifts toward "acceptable". This skill makes the grade come from outside.

The rubric is `../build-loop/references/code-architecture-rules.md`, relative to this skill's base directory — the same file the `build-loop` skill builds against. Builder and reviewer apply the same rules from opposite ends.

## Reviewer: Codex, required

The loop rests on two independent reviewers: Claude builds, Codex grades. A different model family is the point — it catches what the builder's own blind spots hide. A Claude subagent shares those blind spots, however fresh its context, so it is not a substitute.

Before the first review in a session, check:

```bash
command -v codex && codex login status
```

If either fails, **stop** and tell the user: install Codex CLI (`npm i -g @openai/codex`) and run `codex login`. The review step stays blocked until it passes; never skip it or grade the work yourself.

## Modes

All Codex modes use `codex exec`, not `codex review`: `codex review` makes `--base` / `--commit` / `--uncommitted` mutually exclusive with a custom prompt, which would strip the rubric. The skill embeds the diff-fetch commands in the prompt; the reviewer runs them before applying the rubric.

| Invocation | Reviews | Diff fetch (embedded in prompt) |
|---|---|---|
| `/codex-review` | **Auto-detect** (see below) | Resolved per auto-detect |
| `/codex-review --uncommitted` | Staged + unstaged + untracked | `git diff HEAD && git diff --cached && git ls-files --others --exclude-standard` |
| `/codex-review --base <ref>` | All changes vs ref | `git diff <ref>..HEAD` + `git log --oneline <ref>..HEAD` |
| `/codex-review --commit <sha>` | One commit | `git show <sha>` |
| `/codex-review plan <path>` | Plan markdown file | Reviewer reads the file directly |
| `/codex-review plan` | Most recent `plans/*.md` modified this session | Reviewer reads the file directly |

### Scope resolution (no flag)

The build-loop skill commits each chunk, so by review time the changes are usually in HEAD and `--uncommitted` would see an empty diff:

1. `git status --short` non-empty → `--uncommitted`.
2. Else → `--commit HEAD`.

Phase-level scope is passed explicitly: the build-loop skill captures the phase-start SHA and calls `--base <sha>`.

## Prompt assembly — code review

Assemble these sections IN THIS ORDER. Don't skip sections; write "None" or "N/A" when empty — the reviewer needs the structure to see what the builder considered.

### 0. What to review

```markdown
## What to review

Run the following commands (read-only) and read the output in full before applying the rubric:

<diff-fetch commands for the scope>

<For --uncommitted: "`git ls-files --others` lists untracked files by name only — read each one in full; they are in scope for both passes.">

Apply the rubric below. Do not modify any files. Review only.
```

### 1. Header

```markdown
# Post-implementation review of <feature/phase/chunk name>

<1-3 sentences: what was implemented, plan reference, prior plan-review verdict if any.>
```

### 2. Commits to review

`git log --oneline <base>..HEAD` output.

### 3. Final state

```markdown
## Final state

- <N> tests pass (was <M>; +<delta> new)
- <linter command> clean
- <any invariant verified: schema version, dataset hash, public signature>
```

### 4. Load-bearing claims to verify

The most important section. The substantive assertions the implementation depends on, each something that COULD be wrong. Point at code by `file` + symbol (`loader.py::parse_row`); add line numbers only for spans a symbol can't name — symbols survive fixes between rounds, line numbers don't.

```markdown
## Load-bearing claims to verify

1. **<Claim name>** — <what's asserted>. Check `<file:line>`.
2. **<Claim name>** — <what's asserted>. Check `<file:line>` and `<file:line>`.
```

Good claims: "Legacy code path is byte-identical. Check `src/resolver.rs:485-495`." · "All 10 call sites switched — verify with `rg '\bresolve\(' src` against the plan table." · "Sidecar symbol/filename mismatch is rejected; test `load_rejects_sidecar_symbol_filename_mismatch`."

### 5. Specific concerns (fresh-eye spots)

Where the builder is LESS confident. Lettered (A/B/C) to keep them distinct from claims.

### 6. Hard constraints

What MUST NOT have regressed — test counts, lint clean, pinned invariants, public API signatures.

### 7. Rubric — two passes

Code review is **two separate reviewer runs**:

- **Correctness pass:** scope = the diff. Sections 0–6 + the rules file + **Profile A**.
- **Architecture pass:** scope = the FULL touched modules. Sections 0–6 + the rules file + **Profile B**; in section 0, instruct the reviewer to read each touched module in full, not just the diff.

Read the rules file into the prompt as bytes — never retype it from memory:

```bash
RULES="<this skill's base directory>/../build-loop/references/code-architecture-rules.md"
# each pass prompt = <sections 0-6> + "$(cat "$RULES")" + <Profile A | Profile B> + <section 8>
```

### 8. Verdict format

```markdown
## Verdict format

RESPOND IN THIS EXACT FORMAT. First line MUST start with `VERDICT:`. No prose before it.

VERDICT: <GREEN | YELLOW | RED>
SUMMARY: <one-sentence headline finding>

ISSUES:
<GREEN: "None" or LOW observations only. YELLOW/RED: numbered.>

1. [<HIGH | MEDIUM | LOW>] <file>:<line> — <issue>
   Fix: <concrete code or design change>

CLAIMS CHECKED:
1. <claim name> — ✓ verified | ✗ failed | ⚠ partial: <note>

CONCERNS CHECKED:
A. <concern name> — <clean | flagged-issue-N | note>

CONSTRAINTS VERIFIED:
- <constraint> — ✓ holds | ✗ violated: <note> | ⚠ trusting reporter's claim — needs operator verification: <why>

If you can verify a constraint mechanically (read files, rg, git history), use ✓ or ✗. If it needs a build or test run your sandbox can't do, mark ⚠ trusting reporter's claim — never ✓ from the diff alone.

LOW-priority notes (optional):
- <observation>

Priority: HIGH blocks ship · MEDIUM fix before the phase merges · LOW could defer.
```

## Prompt assembly — plan review

### 1. Header

```markdown
# Plan review: <plan file path>

<1-3 sentences: what the plan is for, what triggered it, what phase it follows.>

## Plan

Read: `<path/to/plan.md>` (relative to the project root, your working directory). Also read SPEC.md and spot-check any code the plan cites. Do not modify any files.
```

### 2. Context

```markdown
## Context

- **What this plan changes:** <one paragraph>
- **Explicit non-goals:** <list>
- **Preconditions:** <list>
- **What it enables downstream:** <list, if relevant>
```

### 3. Assumptions and tradeoffs

The high-value section for plan review.

```markdown
## Assumptions and tradeoffs

### Assumptions
1. **<Assumption>** — <what's assumed>. Risk if wrong: <consequence>.

### Tradeoffs considered
1. **<Decision>** — chose <A> over <B> because <reason>. Reversal cost: <high/medium/low>.
```

### 4. Open questions

```markdown
## Open questions for review

A. <Question the builder couldn't resolve>. Plan picks <answer>; is this right?
```

### 5. Plan rubric

```markdown
## Plan rubric

**Soundness:** Are the assumptions plausible? Is the tradeoff framing right, or is an option missing? Any hidden assumptions?
**Scope:** Scoped to what was asked, or accreting speculative work? What could be deferred? Is there a smaller version that ships value sooner?
**Completeness:** Missing steps (preconditions, postconditions, rollback)? Is verification accounted for at each step? Is "done" defined?
**Risks:** What could go wrong that the plan doesn't address? Are external dependencies explicit? Is there a rollback path if execution fails partway?
**Spec alignment:** Does the plan match SPEC.md — and does it update SPEC (Decisions, Acceptance Criteria, Failure Modes) before building?

**Exclusions (do NOT flag):**
- Theoretical risks requiring unlikely preconditions
- Style preferences without measurable impact
- Suggestions that expand scope beyond the request
```

### 6. Verdict format

```markdown
## Verdict format

RESPOND IN THIS EXACT FORMAT. First line MUST start with `VERDICT:`.

VERDICT: <GREEN | YELLOW | RED>
SUMMARY: <one-sentence headline finding>

PLAN EDITS:
<GREEN: "None". YELLOW: numbered.>
1. [<HIGH | MEDIUM | LOW>] <plan section/line> — <issue>
   Change: <concrete plan edit>

DESIGN ISSUE (RED only):
<root cause + suggested rework>

ASSUMPTIONS REVIEWED:
1. <assumption> — ✓ sound | ✗ flagged: <reason> | ⚠ caveat: <note>

TRADEOFFS REVIEWED:
1. <tradeoff> — ✓ correct framing | ⚠ alternative missed: <suggestion>

OPEN QUESTIONS ADDRESSED:
A. <question> — <answer, or "needs operator judgment: <reason>">

RISKS NOT IN PLAN:
- <risk> — Mitigation: <suggestion>

Priority: HIGH blocks execution · MEDIUM revise before executing · LOW could defer.
```

## Rubric profiles

Bug review and architecture review have **inverted error-cost asymmetry**. A missed bug is a point failure — it resurfaces via tests, production, or a later review. A missed architectural decision is a *rate* failure that compounds across every future change. One rubric can't serve both: a precision-tuned exclusion list ("don't flag style without measurable impact") is right for diffs and silently gags architecture review, because most structural violations have no failing test and read as "style".

| Pass | Scope | Append | Tuning |
|---|---|---|---|
| **Correctness** | the diff | rules file + **Profile A** | Precision — suppress noise |
| **Architecture** | the FULL touched modules (structure is invisible in a 20-line hunk) | rules file + **Profile B** | Recall — false positives are cheap, misses compound |

### Profile A — Correctness (precision)

```markdown
## Rubric — Correctness pass

Apply the prepended rule set to the diff. Flag violations; do NOT flag what the EXCLUSIONS rule out.

### EXCLUSIONS (do NOT flag)
- Theoretical risks requiring unlikely preconditions
- Defense-in-depth when the primary defense is adequate
- Style preferences without measurable impact
- Code in files outside the diff scope
```

### Profile B — Architecture (recall)

```markdown
## Rubric — Architecture pass

Work the prepended rule set as an EXPLICIT CHECKLIST over the full touched modules (read each touched module in full, not just the changed lines). For EACH rule, cite every violation with `file:line`, or write "none found". Enumerate rule by rule — silence on a rule is not allowed. A whole rule category that cannot apply to this code may be closed with one line and a reason (e.g. "Concurrency: N/A — single-threaded synchronous CLI, no event loop"); everything else is listed rule by rule.

### INVERTED EXCLUSIONS (recall over precision)
- Flag every structural violation **even if no test fails and nothing is currently broken**. Latent structural debt is the target.
- Do NOT suppress as "style preference without measurable impact" — for architecture, structure IS the measurable impact.
- A false positive costs a human seconds; a false negative compounds across every future change. When unsure, flag it.
- In scope: every module the diff touches, read in full. Out-of-diff modules are in scope when a touched module's structure implicates them.
```

## Invocation

Run from the project root. Codex inherits the working directory and loads the project's `AGENTS.md`.

**Sandbox — pick by environment, every time:**

```bash
# Bypass ONLY inside a container, where the container is the real sandbox. On the host, Codex's own
# read-only sandbox. One `=`-joined token so zsh's no-word-split can't mangle it.
if [ -f /.dockerenv ]; then CODEX_SANDBOX=--dangerously-bypass-approvals-and-sandbox; else CODEX_SANDBOX=--sandbox=read-only; fi
```

On the host, read-only means Codex can read files and run read-only commands (`git`, `rg`, linters with caching off, small probes) but **cannot write** — so tests that create files, and anything taking a build-tool lock, fail for it. The builder runs every gate itself and states the results in "Final state"; Codex marks those `⚠ trusting reporter's claim`, which is correct, not a gap to chase.

Write each assembled prompt to a file, and capture the reviewer's final message with `-o`. **Parse the verdict from the `-o` file, never from stdout** — stdout echoes the prompt, so its first `VERDICT:` line is the template's placeholder. Keep the prompt and verdict files outside the repo — `TMP=$(mktemp -d)` once per phase — so they never get committed.

**Code review** (one run per pass; the two passes are independent and can run in parallel):

```bash
codex "$CODEX_SANDBOX" -c model_reasoning_effort=high exec -o "$TMP/arch-r1.verdict" "$(cat "$TMP/arch-r1.prompt.md")" < /dev/null > "$TMP/arch-r1.log" 2>&1
```

**Plan review** — add `--skip-git-repo-check`, since plans can live outside a git repo, where `codex exec` refuses to start:

```bash
codex "$CODEX_SANDBOX" -c model_reasoning_effort=high exec --skip-git-repo-check -o "$TMP/plan-r1.verdict" "$(cat "$TMP/plan-r1.prompt.md")" < /dev/null > "$TMP/plan-r1.log" 2>&1
```


**Model and effort.** The model comes from `~/.codex/config.toml`; pass `-m <model>` only when overriding. Reasoning effort is pinned to `high` on every call rather than inherited: higher settings burn the usage quota fast for a gate that runs every phase, and a fixed effort keeps verdicts comparable across runs. A review reads the diff *plus* every file it spot-checks, so a small-context model aborts mid-run on large phases (`remote compaction failed`, stream disconnect). For anything past a few thousand lines, use the largest-context model your account has, and **chunk standing-code review to ≤~4K LOC per run** — one run per subsystem, then synthesise the verdicts yourself. A run that dies mid-way is almost always context pressure: check model and chunk size before blaming anything else. Check `codex --version` and update the CLI before blaming a model error.

**Authentication.** Codex uses its own auth (`codex login`). On an auth error, surface the exact error text to the user and stop.

## Iteration loop

Code review returns **two verdicts**. The phase is blocked until BOTH are GREEN or have findings addressed.

1. Parse each pass's VERDICT line from its `-o` file.
2. **GREEN** — proceed. **LOW issues never block and never need adjudication, under any verdict**: fix them in a round that will be re-run, or leave them and list them to the user when the phase lands. A change after the final GREEN is a new round.
3. **YELLOW** — for each HIGH/MEDIUM issue: check the `file:line` and the claim. Valid → fix it; a different fix from the one suggested is fine — the re-run grades it. Disagree that it's a problem at all → push back with reasoning and surface the issue + counter-position to the user. Never silently skip.
4. **RED** — surface immediately. Don't patch in-loop; a design-level error needs the user's judgment.
5. **Architecture findings don't need a failing test to be blocking.** "Nothing is broken yet" is not a dismissal. Dismiss only with a substantive structural counter-argument, surfaced to the user.
6. **After fixes you MUST re-invoke.** A fix that changes code or contract (SPEC decisions, criteria, failure modes) re-runs **both** passes — the other pass has to grade the new state even if it was GREEN. Only a fix confined to ARCHITECTURE.md or plan prose re-runs just the pass that raised it. The re-review prompt — plan or code — adds a `## Previous round` section just before the rubric: each prior issue with its disposition (fixed at `file:line`, or pushed back with the reason). Instruct the reviewer to verify each disposition independently, then review fresh — not just check the list. The loop has exactly three exits: (a) GREEN on a re-run, (b) a disagreement the user adjudicated, (c) the round-5 cap surfaced to the user. Proceeding past YELLOW/RED on "I made the edits, so it's resolved" is a process violation — that self-grade is exactly the drift this gate exists to stop.
7. **Cap at 5 rounds per pass.** Still YELLOW/RED after round 5 → surface remaining issues and disagreements to the user; stop looping.

The cap exists because reviewer bias drifts toward "acceptable" across rounds, and genuine disagreements need a human. The caller drifts the same way: "edits applied → proceed" feels done but skips the only external check.

## When to invoke

**From the build-loop skill:** plan review at Build Loop step 5 (after the final plan, before any code); phase review at step 7 (`--base <phase-start-sha>`, before claiming the phase landed).

**On request:** architecture decisions where a second opinion matters; before merging a feature branch; sanity-checking someone else's code.

**Skip** (discretionary reviews only — the build-loop skill's mandatory gates take precedence: step 5 skips only on its five stated conditions, step 7 never): mechanical refactors (rename, extract, format); test-only or doc-only changes; typo and comment fixes; plans under ~50 lines unless foundational.

---

Plan review BEFORE execution; code review AFTER. Structured prompts — load-bearing claims + concerns + bundled rubric. Two passes for code. Re-invoke after fixes. 5-round cap. VERDICT first.
