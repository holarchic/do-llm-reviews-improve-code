# Fairness audit: jobscan-company-notes

An independent agent (Sonnet 5.5, 2026-10-27) audited this control task. It read TASK.md, the `base` snapshot and the hidden feature tests, and saw no later version of the repository.

**Verdict:** fair; no test needs removing. A control has no review tests.

Applied:
- `noted` returns the note texts.
- Several notes for one company each get their own line, in file order.

Also corrected outside the audit: the note lines follow the `Company:` line, when an entry has one, not the link line directly.
