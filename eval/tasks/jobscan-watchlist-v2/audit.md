# Fairness audit: jobscan-watchlist

An independent agent (Sonnet 5.5, 2026-10-27) audited this control task. It read TASK.md, the `base` snapshot and the hidden feature tests, and saw no later version of the repository.

**Verdict:** fair; no test needs removing. A control has no review tests.

Applied:
- The left-out list's star is placed: `- ★ Acme — …`.
- One refusal message, "`watch` must be a list of strings", for a non-list and for a list holding something else. Base's own list check says it this way, and the test accepts either form.

## Spec–test consistency

An independent agent (Opus 5.5, 2026-10-31) checked every TASK.md statement against the reference code, the hidden feature and review tests, and the regression-set base tests that touch the same behaviour. It read the `base` and `reference` trees and probed literal readings in scored copies. The builder verified each finding against the cited test and code before applying it. Classes:
- **contradiction**: a build that follows the statement literally fails a kept test;
- **private reach**: a hidden test calls a name neither TASK.md nor base's public surface gives;
- **invitation**: loose wording about existing behaviour a regression test pins, inviting a change;
- **underdetermined**: a feature test asserts a choice the statement leaves open and base doesn't settle.

Silence alone is not a finding: keeping base behaviour is the default. No fix may reveal a review-caught defect.

| Statement | Class | Resolution |
|---|---|---|
| "A digest with any ★ carries one legend line after its title" | underdetermined | `WatchRunTest.test_watched_companies_lead_the_digest_and_a_ruled_out_one_is_starred` requires `WATCH_LEGEND` straight after the title, ahead of the existing ⓘ legend. A probe with the two swapped fails it. Now it says where: right under the title (below the unread-boards line when there is one), ahead of the ⓘ legend (`digest.LEGEND`). |

The other 21 statements are consistent. Left as an ordinary build error, not a spec issue: appending `watch = […]` after the help example's `[custom_tags…]` table puts it inside that table and fails `test_arguments.HelpTest.test_the_example_profile_loads_and_matches_as_the_help_says`.
