---
version: 0.1.0
updated: YYYY-MM-DD
---

# Plan: <phase name>

**Status:** planning | reviewing | building | phase-review | landed
**Phase-start SHA:** <captured at step 0 when full mode starts>
**SPEC version:** <before> → <after, set at step 3>

## Goal

What this phase delivers, against which SPEC Decisions and Acceptance Criteria.

## Non-goals

What this phase deliberately leaves out, including deferred extras.

## Approach

How it will be built. State assumptions and the tradeoffs considered (chose A over B because…).

## SPEC changes

Greenfield: the proposed contract. Otherwise: the Decisions, Acceptance Criteria and Failure Modes to add or change. Step 3 writes these into SPEC and replaces this section with "Written to SPEC <version>".

## Open questions

Each with the default this plan assumes until answered.

- Q1: <question> — default: <assumed answer>

## Chunks

Each chunk is one atomic commit. `[Step] → verify: [check]`.

- [ ] 1. <chunk> → verify: <check>
- [ ] 2. <chunk> → verify: <check>
- [ ] N. Docs: ARCHITECTURE.md current; durable Log items promoted to SPEC → verify: coherence read of SPEC + ARCHITECTURE vs code

## Reviews

- Plan review r1: <GREEN | YELLOW | RED> — <one-line summary; what changed>
- Phase review r1: correctness <verdict> · architecture <verdict> — <summary>

## Log

Deviations from the plan, mid-build decisions, user rulings, dead ends and blockers, with the reason — recorded when they happen. Durable items are promoted to SPEC by the docs chunk; rulings made during phase review are committed to SPEC before the phase review ends.
