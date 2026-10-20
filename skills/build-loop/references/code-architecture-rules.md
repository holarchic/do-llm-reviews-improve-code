# Code Architecture Rule Set — CANONICAL SINGLE SOURCE

> The one source of truth for the architecture rule set (Karpathy 4 + the 40 Code Architecture primitives).
> Consumers: the `build-loop` skill (builder) and the `codex-review` skill (reviewer, both passes). Reviewers read THIS file into their prompts — they do not carry their own copy.
> The condensed high-leverage subset is always in context via the `build-loop` skill's `## Code` section. This is the full set.
> Change the rules HERE; every consumer picks the change up automatically. Profile/tuning text (correctness exclusions, inverted exclusions) is deliberately NOT here — it lives with each consumer skill and evolves independently.

### Karpathy 4 rules

- **Think Before Coding** — were assumptions surfaced before implementing? Were multiple interpretations presented when they existed?
- **Simplicity First** — is every line necessary? Flag speculative abstractions, unrequested flexibility, error handling for impossible scenarios, 200-line code that could be 50, abstractions for single-use code (Inline > extract until 3+ call sites exist).
- **Surgical Changes** — does every changed line trace to the requested work? Flag adjacent-code "improvements", style refactors, drive-by formatting.
- **Goal-Driven Execution** — are success criteria stated and verifiable? Flag "make it work" vagueness.

### Code Architecture (universal)

**Architecture Invariants:**
- Modules implement a shared interface; they don't access each other's internal state.
- Background tasks never mutate state directly; async results arrive as typed messages.
- New modules must not require modifying existing ones.
- The application root is a thin dispatcher.

**State Ownership:**
- Never add module-specific state to the application root.
- Each module owns its input handlers; the root dispatches.
- Adding a module means adding a file; modifying existing modules requires explicit reason.

**Data Representation:**
- Data flows as typed records until the presentation boundary; never flatten into positional arrays or lists of strings.
- Identify fields by name, not position. Sort/filter/compare by named fields, never `row[3]`.
- Stringification only at the presentation boundary (render, format, serialize, log adapter).

**Concurrency:**
- One writer of state: the main event loop.
- Presentation functions (render, format, view) are pure — no side effects, no I/O.
- To update state from async work, define a new message variant.

**Interface Design:**
- A module's interface is simpler than its implementation; pull complexity downward.
- Adjacent layers use different abstractions; pass-through methods signal a layer to remove.
- If callers must know your internals to use you correctly, the interface is wrong.
- General-purpose interface is usually narrower than a specialized one.

**Dependencies & Layering:**
- Depend on abstractions, not concretions.
- Dependency flow is one-way; cycles are a refactor signal.
- Never use global mutable state; pass dependencies explicitly.
- A module's public surface is its explicit exports.
- Every design decision lives in exactly one place; the same decision in multiple modules is information leakage.

**Boundary Validation:**
- Validate at system boundaries (user input, external APIs, file/DB loads); internal code trusts its inputs.
- Parse, don't validate: convert raw input into a typed record or an error. Never mutate-then-check.
- One validation point per input type; never re-validate downstream.
- Reject malformed input at the boundary; no best-effort tolerance of silent corruption.

**Error Handling:**
- Define errors out of existence where possible (delete-if-exists; clamping over throwing).
- Mask exceptions at the layer that can resolve them.
- When unavoidable, errors are explicit typed values; never signal failure with null/None/zero-value.
- Never catch errors broadly; catch the specific failure.
- Fail at the point of detection; no "validate now, use later" gaps.

**Logging & Observability:**
- Logs are structured key-value records, not formatted strings.
- Log at boundaries; never inside tight loops, pure functions, or hot paths.
- Never log secrets, PII, tokens, or credentials.
- Levels carry meaning: ERROR (action required), WARN (anomaly), INFO (significant transition), DEBUG (development).

**Comments:**
- Comments capture what code cannot: design intent, hidden invariants, public API contract, gotchas.
- Never comment WHAT code does at the same abstraction level — that's a renaming or refactoring signal.
- Stale comments lie; when code changes, the comment changes or dies.
- Never leave commented-out code.
