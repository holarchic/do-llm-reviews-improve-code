# Fairness audit: jobscan-greenhouse

An independent agent (Sonnet 5.5, 2026-10-27) audited the task. It read TASK.md, the `base` snapshot, the hidden modules and defects.json, and saw no other version of the repository.

**Verdict:** fair; no test needs removing.

Feature tests: all fair. Applied: an id that is a blank string, or a job that isn't an object, is named as malformed.

Review tests:

| Test | Verdict | Note |
|---|---|---|
| `BoardUrlTest.test_a_semicolon_stays_in_the_token_written_or_encoded` | fair | TASK.md allows `;` in a token. A writer who copies ashby.py's `urlparse` loses it, and testing the token rule against the URL form finds that. |
| `BoardUrlTest.test_host_case_and_the_default_port_are_one_board` | concern, kept | It rests on a URL norm that the in-repo precedent contradicts (ashby.py compares the host exactly). Kept as a judgment test; adding it to TASK.md would make it a feature requirement. |
| `LiteralTextTest.test_escaped_text_in_the_posting_survives_any_number_of_escapings` | fair | Well defined for all three forms ("unescape while the content has no literal tag" satisfies both this test and the feature test); TASK.md does not say how to detect the depth. |
| `MalformedJobTest.test_an_office_that_is_not_an_object_fails_the_board` | fair, partly given away | "offices that aren't a list of objects" is needed for the historical feature test's `offices: [3]` case, and it also covers `[None]`. |
| `MalformedJobTest.test_an_id_with_a_lone_surrogate_fails_the_board` | fair | The surrogate rule covers all text; the snapshot's `text_field` checks every field. |

## Spec–test consistency

An independent agent (Opus 5.5, 2026-10-31) checked every TASK.md statement against the reference code, the hidden feature and review tests, and the regression-set base tests that touch the same behaviour. It read the `base` and `reference` trees and probed literal readings in scored copies. The builder verified each finding against the cited test and code before applying it. Classes:
- **contradiction**: a build that follows the statement literally fails a kept test;
- **private reach**: a hidden test calls a name neither TASK.md nor base's public surface gives;
- **invitation**: loose wording about existing behaviour a regression test pins, inviting a change;
- **underdetermined**: a feature test asserts a choice the statement leaves open and base doesn't settle.

Silence alone is not a finding: keeping base behaviour is the default. No fix may reveal a review-caught defect.

| Statement | Class | Resolution |
|---|---|---|
| "Not boards. … another host …" | contradiction | The API bullet keeps the Job Board API "on any host", and `BoardTest.test_api_url_on_another_host_is_kept` follows it. Now "a board or embed page on another host". |

Rejected: "Tests point it at a local server, and that host is kept" (the test also keeps the scheme and port). Base settles it: TASK.md gives adapter names their `ashby.py` meanings, and `ashby.board` keeps `scheme://netloc`. A literal probe built from TASK.md alone passed every feature test.

The other 49 statements are consistent. The earlier verdict on `test_host_case_and_the_default_port_are_one_board` ("concern, kept") stands: stating the rule would turn the review test into a feature requirement.
