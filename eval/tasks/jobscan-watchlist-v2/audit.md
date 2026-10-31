# Fairness audit: jobscan-watchlist

An independent agent (Sonnet 5.5, 2026-10-27) audited this control task. It read TASK.md, the `base` snapshot and the hidden feature tests, and saw no later version of the repository.

**Verdict:** fair; no test needs removing. A control has no review tests.

Applied:
- The left-out list's star is placed: `- ★ Acme — …`.
- One refusal message, "`watch` must be a list of strings", for a non-list and for a list holding something else. Base's own list check says it this way, and the test accepts either form.
