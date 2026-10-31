# Fairness audit: jobscan-company-notes

An independent agent (Sonnet 5.5, 2026-10-27) audited this control task. It read TASK.md, the `base` snapshot and the hidden feature tests, and saw no later version of the repository.

**Verdict:** fair; no test needs removing. A control has no review tests.

Applied:
- `noted` returns the note texts.
- Several notes for one company each get their own line, in file order.

Also corrected outside the audit: the note lines follow the `Company:` line, when an entry has one, not the link line directly.

## Spec–test consistency

An independent agent (Opus 5.5, 2026-10-31) checked every TASK.md statement against the reference code, the hidden feature and review tests, and the regression-set base tests that touch the same behaviour. It read the `base` and `reference` trees and probed literal readings in scored copies. The builder verified each finding against the cited test and code before applying it. Classes:
- **contradiction**: a build that follows the statement literally fails a kept test;
- **private reach**: a hidden test calls a name neither TASK.md nor base's public surface gives;
- **invitation**: loose wording about existing behaviour a regression test pins, inviting a change;
- **underdetermined**: a feature test asserts a choice the statement leaves open and base doesn't settle.

Silence alone is not a finding: keeping base behaviour is the default. No fix may reveal a review-caught defect.

**Result:** all 21 statements consistent, no change.

Coverage gap, not a consistency issue: the two base tests that would check that saving through the set-up page keeps `company_notes` pin the old watchlist hint, so they fail on reference and are outside the regression set. A build whose page drops the notes on save is not caught. Adding a hidden test would be new coverage, outside this audit.
