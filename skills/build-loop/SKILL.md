---
name: build-loop
description: "Structured, spec-first code building + Code Architecture rules. Two modes: 'plan' (Build Loop steps 1-2 — review SPEC, write the plan file, stop) and 'build'/'implement' (all steps — SPEC update, external plan review + two-pass phase review via the codex-review skill, execution, ARCHITECTURE update). Creates SPEC.md and ARCHITECTURE.md from templates when missing. Invoke with 'plan', 'build', or 'implement'. TRIGGER when: editing .py/.rs/.ts/.tsx/.go/.java/.cs/.cpp/.rb files; writing code blocks for the user; user asks for code implementation, refactor, or architecture review. The full Build Loop only runs on explicit 'plan'/'build'/'implement' invocation; auto-trigger loads the Code Architecture rules for any code work."
---

Spec-first: never land code without updating SPEC.md.
Docs before review: ARCHITECTURE and SPEC are current before the phase review grades them.
The plan file is the phase's working record — SHA, chunks, verdicts, dead ends.
Plan and phase reviews are externally graded — editing in response is not "addressed" until a re-review says GREEN.
Codex CLI is the required reviewer — if it is missing or logged out, stop at the first review step and tell the user; never self-review.
Three strikes then surface — don't loop on cascading failures.

# Build

A spec-first Build Loop with an external reviewer at both ends, and the Code Architecture rules applied from both sides — the builder writes to them, the reviewer grades against them.

Paths below are relative to this skill's base directory: `templates/` and `references/` sit next to this file. The reviewer is the sibling `codex-review` skill.

## Modes

The trigger word selects the mode:

| Trigger | Mode | Runs |
|---|---|---|
| `plan` | **Plan-only** | Doc check + Build Loop steps 1–2: review SPEC, write the plan file against the current contract. Stop. Do NOT update SPEC, do NOT build. Wait for the user. |
| `build` / `implement` | **Full** | Doc check + all steps: capture SHA → review SPEC → plan → update SPEC → final plan → **plan review** → build → update ARCHITECTURE → **phase review** → land. |

Plan-only mode is the structured analog of Claude Code Plan mode — it anchors against SPEC.md before producing a plan. After the plan is delivered, the user invokes `build` or `implement` to continue from the same plan file.

## When NOT to invoke /build-loop

The full Build Loop earns its overhead only when the change adds a new contract element — a Y-statement decision, an acceptance criterion, an architectural section, or a durable invariant. For everything else the ceremony is friction.

**Invoke /build-loop for**:

- New capability landings (a feature that didn't exist before).
- Behaviour changes that warrant a SPEC Decision (Y-statement) or a new Failure-Mode row.
- Structural edits that touch the public-ish surface enough to deserve an ARCHITECTURE update.

**Do NOT invoke /build-loop for**:

- **Review-feedback bug fixes on an already-declared capability.** Reviewer RED items, PR comments, YELLOW remediation — the contract is already in SPEC; the implementation just doesn't match it yet. Direct edit + verify + atomic commit.
- **Test additions, doc-sync, lint/format cleanup, comment edits.**
- **Mechanical refactors** (rename, extract, split) that preserve the contract.
- **Stale-reference cleanups** flagged by review.
- **Throwaway spikes and prototypes** — code written to answer a question ("does this library do X?", "is this idea worth building?") and then deleted. Just build it. If it graduates into something kept, the first real capability goes through the loop, starting with SPEC.

If you're already inside a `/build-loop` invocation and the next chunk is one of the "do not" cases, drop the ceremony for that chunk: direct edit + verify + commit, then return to Build-Loop discipline when the next genuine capability chunk starts. Do NOT re-run steps 1–4 per bug fix.

Common failure mode: `/build-loop` is invoked once for a capability, that capability gets several rounds of review feedback, and every round gets folded into the same `/build-loop` frame. The first round was correct; later rounds should drop ceremony. Recognise it and switch.

## Docs

Two permanent docs and one working file per phase. **SPEC is design intent; ARCHITECTURE is what got built** — a formal split, not duplicates.

| File | Holds | Never holds |
|---|---|---|
| `SPEC.md` | The contract: objective, non-goals, interface, data model, design-time approach, Y-statement Decisions, Acceptance Criteria, Failure Modes. | An as-built module tree (that's ARCHITECTURE). |
| `ARCHITECTURE.md` | As-built structure: modules, key exports, wiring — by subsystem, present tense. | Status, phase history, test counts, commit ranges, review verdicts, Y-statements. |
| `plans/<phase>.md` | One phase's working record: the plan, phase-start SHA, chunk checklist, review verdicts, deviations and dead ends. | Anything durable once the phase lands — that gets promoted to SPEC. |

**Doc check (after Resume, before step 1).** Docs are created from `templates/<FILE>.template.md`, with the template's guidance text replaced as you fill it — never committed as boilerplate.
- **Existing codebase**, `SPEC.md` or `ARCHITECTURE.md` missing: create it now, reverse-engineered from the code. Mark what you cannot establish — especially objective and non-goals — `TODO(build): confirm` rather than inventing it. Commit separately (`docs:`), before capability work.
- **Greenfield** (no source code yet — a README alone doesn't count): create nothing now. The plan's `## SPEC changes` proposes the whole contract in SPEC's section order; step 3 creates `SPEC.md` from it at version 0.1.0, where the template minimums (3 non-goals, 2 failure modes) apply. The docs chunk creates `ARCHITECTURE.md`.

Never overwrite an existing doc; if one exists under another name (`docs/design.md` acting as SPEC), ask before creating a second. No `.gitignore` covering the language's build artifacts (`__pycache__/`, `.venv/`, `target/`, `node_modules/`)? Add one in the first commit that writes code.

**Resume.** After resolving the mode, check `plans/` for a plan whose `Status:` is not `landed` — the phase in flight. Never start a second one.
- **Plan-only mode:** revise that plan within steps 1–2. Capture no SHA. If the revision changes anything, set `Status: planning` — the earlier approval no longer covers it, so the next full run re-enters at step 3 (SPEC, then a fresh plan review) and resumes building at the first unchecked chunk. The recorded SHA stays; ticks stay only on chunks the revision didn't touch. Un-tick every chunk whose requirements changed, and the docs chunk whenever the revision changes implementation or a durable decision.
- **Full mode:** continue from its status — `planning`: capture the SHA (step 0) if none is recorded, then step 3 · `reviewing`: the step-5 loop, round count from `## Reviews` · `building`: the first unchecked chunk; all checked → step 7 · `phase-review`: the step-7 loop, round count from `## Reviews`.

**Full mode requires a resolvable HEAD** — the phase review diffs against the step-0 SHA. If `git rev-parse HEAD` fails (not a repo, or a repo with no commits), plan-only mode proceeds without commits; full mode stops and asks the user to initialise the repo and make a baseline commit that includes SPEC and ARCHITECTURE. Don't do either unasked.

**Open questions.** A question is blocking when its answer would change the plan's structure or chunks — ask it now. Questions the plan can proceed on go in the plan's `## Open questions`, each with the default the plan assumes — never decided silently. Plan-only mode ends by presenting them; full mode asks any still open before step 5, and surfaces any that arise mid-build at landing.

**Commits.** Separate commits for: the doc check (`docs:`); the step-3 SPEC update, with any step-0–4 plan edits (`spec:`); each round of plan-review edits, SPEC edits included (`plan: review rN`). Each chunk is one commit with its plan tick; each round of phase-review fixes is one commit. A status change or a no-edit verdict rides with the next commit — never a commit just for that. Message prefixes beyond these are the project's own convention.

## Build Loop (capability-level)

For each new capability, behaviour change, or structural edit:

0. **Capture phase-start SHA** — `git rev-parse HEAD` at the start of full mode, before step 3. Commits from an earlier plan-only session fall before it; the SPEC update and everything after are in the reviewed diff. Record it in the plan file.
1. **Review SPEC** — read `SPEC.md`. Anchor to the current contract. Greenfield has none yet — the plan proposes it.
2. **Plan** — write `plans/<phase>.md` (kebab-case phase name) from `templates/PLAN.template.md` with `Status: planning`: goal, non-goals, approach, the `## SPEC changes` it needs, draft chunks ending with the docs chunk, open questions. Plan-only mode commits it (`plan: <phase>`) if HEAD resolves, then stops and gives the user a short summary, the open questions with their assumed defaults, and the next command (`/build-loop build`).
3. **Update SPEC** — write the plan's `## SPEC changes` into SPEC: a Y-statement in `## Decisions`, `## Acceptance Criteria`, `## Failure Modes` if relevant. Bump the SPEC version (patch/minor/major) once per phase, here; later edits in the same phase keep it. Then reduce `## SPEC changes` in the plan to a pointer to the SPEC version, so the two never drift.
4. **Final plan** — re-plan against the updated spec and finalise the chunk checklist. Collapses into a no-op when step 3 wrote exactly what the plan proposed.
5. **Plan review** — set `Status: reviewing`. **MUST invoke the `codex-review` skill in plan mode on the plan file.** Resolve YELLOW/RED to GREEN before step 6. **"Address" ≠ "edited the plan"** — it means a re-review returned GREEN, or a disagreement / round-5 cap was surfaced to the user for adjudication (the three loop exits in the codex-review skill). Proceeding on an un-re-reviewed YELLOW — "edits applied, so resolved" — is a process violation; the verdict is externally graded for exactly this reason. A HIGH or MEDIUM finding without a `Basis:` (the requirement or rubric rule it breaks) counts as LOW and doesn't block. Record each round's verdict, and any user ruling on a disagreement, in the plan's `## Reviews` as it happens.

   **Skip ONLY when ALL of these hold** (state each explicitly, don't judge implicitly):
   - No Y-statement Decision added in step 3, AND
   - No new Acceptance Criteria added in step 3, AND
   - No new Failure Modes added in step 3, AND
   - Total expected diff < 20 LOC, AND
   - No new file, no new module, no architectural shift

   If even one doesn't hold → invoke. When in doubt → invoke.

6. **Build** — set `Status: building`; execute the plan via the inner increment loop below. A decision made mid-build, or a user ruling, goes into the plan's `## Log` when it happens. **The plan's last chunk is always the docs chunk** — it brings the docs up to date before review, so the phase review grades them rather than a stale copy, and like any chunk it is committed and ticked, so resume never skips it:
   a. **ARCHITECTURE.md** — module surface (new files, types, wiring), present tense, by subsystem. No history or status — it grows without bound and goes stale. At most one `updated:` line.
   b. **Promote from the plan file** — a Log item that changes the contract (a mid-build decision, a user ruling) goes into SPEC: a Y-statement if it's a real choice between options, otherwise the relevant section (User Interface, Acceptance Criteria). A dead end worth remembering becomes a SPEC Failure Mode. Everything else stays in the plan file.
7. **Phase review** — set `Status: phase-review`. **MUST invoke the `codex-review` skill with `--base <phase-start-sha>`.** **NO SKIP CONDITIONS.** It runs **two passes** (correctness over the diff, architecture over the full touched modules) and returns two verdicts. Resolve YELLOW/RED on **both** to GREEN before step 8, with the same externally-graded exits as step 5; record each pass's verdict per round in `## Reviews`. A review fix is a direct edit + verify + commit, not a new loop — but if it adds contract (a new output rule, a new failure behaviour), the same commit adds the SPEC Y-statement or criterion and keeps ARCHITECTURE current. A durable user ruling made during review (accepting a tradeoff, overruling a finding) is committed to SPEC as a Y-statement before leaving step 7 — even when no code changes — and, being a contract change, re-runs both passes. The re-run grades the SPEC entry; it can't reopen the ruling itself. **The architecture pass is the highest-leverage thing this loop does** — a missed bug is a point failure that resurfaces via tests or production; a missed architectural decision is a *rate* failure that taxes every future change. An architecture finding is blocking **even with no failing test** — "nothing is broken yet" is not a dismissal; dismiss only with a substantive structural counter-argument surfaced to the user.
8. **Land the phase**:
   a. **Coherence pass** — re-read SPEC, ARCHITECTURE, the plan and the diff together: does the as-built match the contract? Does any doc describe something that no longer exists? Fixes here are limited to plan prose; an edit to code, SPEC or ARCHITECTURE after the final GREEN goes back to step 7.
   b. **Close the plan** — set `Status: landed`, commit it.
   c. **Git tree clean** — `git status --short` empty.
   d. **Report** — what landed, the review verdicts, LOW notes left unfixed, and any open questions that arose mid-build.

   Pre-completion checklist (state it before claiming complete; `[⊘]` is a violation, not a valid mark):
   - [ ] SPEC.md Y-statement Decision added (step 3)
   - [ ] SPEC.md Failure-Mode rows added if applicable (step 3)
   - [ ] **Plan review GREEN (or issues addressed via a re-review) — step 5.** `[⊘]` only if ALL 5 skip conditions were met AND stated.
   - [ ] Docs chunk committed — ARCHITECTURE.md updated, durable plan-file content promoted to SPEC — before the phase review (step 6)
   - [ ] **Phase review `--base <phase-start-sha>` — BOTH passes GREEN (or addressed).** `[⊘]` is NEVER valid.
   - [ ] Plan file `Status: landed`
   - [ ] Git tree clean

Steps 3, 5, 6's doc update, 7 and 8 are non-negotiable for capability changes. Step 2 may collapse for trivial edits. **Skipping step 7 is a process violation — surface it to the user with the reason rather than self-approve.**

Without step 3: the agent suggests X, the user agrees, code lands, the spec drifts — the most common LLM-assisted failure. Without the step-6 doc update: code is built, nobody updates ARCHITECTURE, and it becomes fiction.

## Inner increment loop (step 6)

Per chunk:

1. **Plan** — what this chunk does and how. One paragraph max.
2. **Build** — the smallest working increment.
3. **Verify** — run tests, the linter and the type-checker — the project's configured ones, or the language default if none and it's installed (`ruff`, `clippy`, `eslint`/`tsc`, `go vet`); don't add a tool unasked. Read the output yourself. For a load-bearing test, run a **mutation check**: break the code it guards, clear caches (`__pycache__`, build dirs), confirm the test fails, restore.
4. **Review** — re-read the diff. Matches codebase patterns? Minimal footprint? No dead code? No stray artifacts staged?
5. **Adapt** — if requirements shifted or something broke, update the plan; log the deviation or dead end in the plan's `## Log`.
6. **Check off** — tick the chunk in the plan file.
7. **Commit** — one atomic commit holding the chunk's code AND the plan-file tick, so the checklist never lags the history. Stage paths explicitly; don't `git add -A` blind.

## Discipline

Applies to every chunk, before code.

### Think Before Coding

- State assumptions explicitly before implementing. If uncertain, ask.
- Present multiple interpretations when they exist. Never pick silently.
- If a simpler approach exists, say so. Push back when the user is wrong — say "X is wrong because Y", not "have you considered".
- If something is unclear, stop. Name what's confusing. Ask.
- **If you can name a gap, you may not fill it.** A gap you noticed ("the user didn't say whether…") is a question, not a licence to pick.
- Explain an unfamiliar concept before asking the user to choose between options that depend on it.

### Simplicity First

- Minimum code that solves the problem. Nothing speculative. Scope down until embarrassingly small; iterate when it breaks.
- No features beyond what was asked. Name deferred extras in the plan file; don't build them.
- No abstractions for single-use code. Inline > extract until 3+ call sites exist.
- No flexibility or configurability that wasn't requested.
- No error handling for scenarios that can't happen.
- If 200 lines could be 50, rewrite. Prefer industry-standard patterns over custom solutions; flag complexity creep.
- Names are architecture — treat naming as design.

### Surgical Changes

- Touch only what the request requires. Every changed line traces to it.
- Don't improve adjacent code, comments, or formatting while editing.
- Match existing style even if you'd do it differently.
- Unrelated dead code: mention it, don't delete it. Suggest removing obsolete artifacts rather than letting cruft accumulate.
- Remove orphans YOUR changes created (unused imports/vars/fns).

### Goal-Driven Execution

- Turn tasks into verifiable goals. "Add validation" → "Write tests for invalid inputs, then make them pass."
- For multi-step work, state the plan as `[Step] → verify: [check]` lines.
- Compound instructions run as a sequence — finish the full chain; pause only if genuinely blocked.
- Never announce an action you are not taking in the same turn. Either call the tool or say nothing about it.

## Evidence

- **Green ≠ verified.** A passing suite is evidence only if the tests drive the real value through each consumer over real data and assert the deep path actually ran. A test that passes when the feature is deleted proves nothing. Quantitative output too: tables that recompute consistently are not the same as a model that generates the right numbers.
- **Read state from the thing, never from a proxy.** A file count surviving is not the edit landing (grep the content). A tool's success return is not the write surviving. A prior session's "blocked on X" is a claim — open the file. Visible rows are not the schema — read the validator.
- **Absence is a claim too.** "Unsupported / unused / doesn't exist" needs a positive check — the grep, the code path, the docs page — not silence from a search that may not have run.
- **Facts that change get looked up.** Library APIs, versions, CLI flags, platform limits: check current docs or the installed source, don't recall them.
- **Performance claims are measured, not inferred.** Op-count × assumed per-op cost is a hypothesis — probe it before designing around it. Benchmarks consume their full output (or dead-code elimination fakes the result). Forecast parallel throughput from a saturated whole-machine run, not a single-lane probe.
- **Report outcomes faithfully.** Tests failed → say so with the output. Step skipped → say so. Done and verified → say it plainly.

## Code

The high-leverage subset of the Code Architecture rules, always in context. **Full set (Karpathy 4 + 40 primitives): `references/code-architecture-rules.md`** — read it for any structural change. It is exactly what the `codex-review` skill grades against, so builder and reviewer enforce the same architecture.

- **Comments capture what code cannot** — design intent, hidden invariants, gotchas. Never comment WHAT code does at the same abstraction level; that's a renaming or refactoring signal.
- **Never leave commented-out code.** Version control remembers.
- **No changelogs or provenance in code or docs** ("formerly", "changed in", "per plan", "as of"). State what IS. History → plan files / git.
- **Stringification only at the presentation boundary** (render, format, serialize, log adapter). No prints scattered through business logic.
- **Never flatten typed data into positional arrays or lists of strings.** Identify fields by name; sort/filter/compare by named field, never `row[3]`.
- **Adding a new module means adding a file.** If the change requires modifying an existing module, stop and ask.
- **Define errors out of existence where possible** (delete-if-exists; clamping over throwing). When unavoidable, errors are explicit typed values.
- **A module's interface is simpler than its implementation.** Pull complexity downward. Pass-through methods signal a layer to remove.
- **Audit call-site completeness by symbol, not by name.** A grep for the name misses aliased imports, superstring matches, method calls and raw SQL. Grep the bound usage (`.post(`, `INSERT INTO`) and enumerate aliases before calling a refactor complete. The same sweep covers docs: when a decision or data shape changes, fix every reference in SPEC/plans in one pass.

### Workflow

- Match existing patterns — read neighbouring code first.
- Functions over classes. Guard clauses, early returns, happy path last.
- Add types/signatures where the codebase uses them.
- Parameterize all SQL. Internal code trusts internal code — never string interpolation.
- Mark incomplete work with `TODO(build):` so it's greppable.
- Semantic versioning (patch/minor/major). SPEC, ARCHITECTURE and plan files carry version metadata in YAML front matter at the top of the file. Never on AGENTS.md / CLAUDE.md (line 1 is prime context) or a public README.md (GitHub renders front matter as a table above the title).

## Tooling gotchas (Claude Code)

- **Read a file with the Read tool before editing it.** Edit's read-tracking counts only Read-tool reads — `cat`/`grep` output doesn't satisfy it.
- **Never delete text with an empty `new_string`.** It eats the adjacent newline and joins neighbouring lines. Keep anchor text in the replacement; after a batch of deletions, check line counts.
- **If `old_string` crosses a structural boundary (heading, list item, entry separator), `new_string` must reproduce it** — or everything below gets silently re-parented. Verify by structure (`grep -n '^## '`), not diff size.
- **zsh doesn't word-split unquoted `$var`.** Never store a command or file list in a scalar (`S="ssh -i k host"; $S` fails). Use arrays (`cmd=(…); "${cmd[@]}"`) or functions.
- **Synced folders (Dropbox, iCloud, OneDrive) fork files mid-edit.** A success return isn't proof the write survived — after an edit burst, look for `conflicted copy` files and grep that each edit landed. Exclude `.venv/`, `target/`, `node_modules/` and large `data/` from sync.

## Recovery

- First failure: fix and retry.
- Second failure: change approach.
- Third failure: **stop and surface the blocker to the user.** Do not loop.
- Cascading fixes: `git stash` and start the chunk fresh.

## Subagents and context

- Offload research, file discovery and exploration to subagents. Summaries back, not raw dumps. One subagent per concern; merge results yourself.
- Route by task: fact extraction / scanning → a small fast model; analysis → a mid model; code writing and final synthesis → the strongest model.
- Quality degrades late in a long context. When the session feels past ~70% full, tell the user and suggest a fresh session; the plan file is the handoff.
- Persist outputs into the project as they land — scratch directories are ephemeral.

## Boundaries

| Always | Ask first | NEVER |
|--------|-----------|-------|
| Create missing SPEC/ARCHITECTURE before step 1 | Skip Build Loop steps | Land code without a SPEC update |
| Update SPEC before building (step 3) | Modify public APIs or DB schemas | Delete or skip tests to pass a build |
| Update ARCHITECTURE before the phase review (step 6) | Add dependencies | Commit secrets or `.env` files |
| Run tests before committing | Alter CI/CD config | Force-push to shared branches |
| Follow existing naming | Anything that leaves the machine: push, publish, send, deploy | Refactor outside the task scope |
| Commit atomically; keep functions <50 lines | Delete anything git can't restore | Write outside the project directory |

Reversibility decides who acts: if git can undo it, act and say how to undo it; if it can't, ask first.

---

Spec first. Plan file per phase. Build atomic. Docs current before review. Externally reviewed plan and phase. Three strikes then surface.
