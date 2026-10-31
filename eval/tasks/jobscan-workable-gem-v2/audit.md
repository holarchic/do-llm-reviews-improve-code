# Fairness audit: jobscan-workable-gem

An independent agent (Sonnet 5.5, 2026-10-27) audited the task. It read TASK.md, the `base` snapshot, the hidden modules and defects.json, and saw no other version of the repository.

**Verdict:** mostly fair. Its gaps were TASK.md wording, all applied:
- ids typed: a numeric Workable shortcode or Gem id fails the board;
- a null `locations` (Workable), and a null `location` or `offices` (Gem), count as empty;
- Gem fields are type-checked whether or not they are used;
- the Gem API host is kept.

Feature tests: fair once those were applied.

Review tests:

| Test | Verdict | Note |
|---|---|---|
| `DecodedLinkTest.test_an_encoded_job_link_is_not_a_board` | fair | The slug is decoded before it is judged, so `%6a` is `j`. |
| `DecodedLinkTest.test_a_greenhouse_token_is_folded_before_it_is_checked` | fair | This is existing behaviour: `base` lowercases before checking. The Unicode case is obscure, but it pins behaviour the snapshot already has. |
| `WorkablePlacesTest.test_a_hidden_record_repeating_a_place_lists_it_once` | giveaway, fixed | TASK.md said "listing each place once". It now scopes dedupe to places repeated across entries, so dedupe within one entry is left to judgment. |
| `GemRemoteTest.test_remote_is_added_unless_a_name_says_remote_as_a_word` | giveaway, fixed | The clause pointing at the prefilter's word match was removed. The behaviour is still derivable from `prefilter.py`'s word-adjacent matching and from what the Remote marker is for. |

## Spec–test consistency

An independent agent (Opus 5.5, 2026-10-31) checked every TASK.md statement against the reference code, the hidden feature and review tests, and the regression-set base tests that touch the same behaviour. It read the `base` and `reference` trees and probed literal readings in scored copies. The builder verified each finding against the cited test and code before applying it. Classes:
- **contradiction**: a build that follows the statement literally fails a kept test;
- **private reach**: a hidden test calls a name neither TASK.md nor base's public surface gives;
- **invitation**: loose wording about existing behaviour a regression test pins, inviting a change;
- **underdetermined**: a feature test asserts a choice the statement leaves open and base doesn't settle.

Silence alone is not a finding: keeping base behaviour is the default. No fix may reveal a review-caught defect.

| Statement | Class | Resolution |
|---|---|---|
| Workable: "text fields are strings" | contradiction | The widget, the hidden fixtures and the reference carry `"region": null`, and one review fixture omits `countryCode`. A literal build rejected every board: 6 feature failures in each v1 A3 build, plus a review test failing for the wrong reason. Now the optional text fields are "strings, null or missing", listed by name; `title` stays a required string. |
| Workable: "`shortcode` is a non-empty string" | contradiction | `{"shortcode": " "}` must fail. Now "non-blank". In v1 the `region` failure masked it. |
| Gem: "`id` is a non-empty string" | contradiction | `{"id": " "}` must fail, as a v1 A3 build showed. Now "non-blank". |
| Gem: "`location` … are objects; `offices` is a list of objects" | contradiction | The Location bullet and `test_missing_location_and_offices_are_empty` take null as empty. Now "objects, null or missing" and "a list of objects, null or missing". |

The other 38 statements are consistent. `test_fetch.FetchBoardTest.test_every_429_response_is_logged` (v1 A3 r2) is a genuine miss: the build rewrote base's log format, which TASK.md never touches.

Fairness: none of the changes bears on place dedupe, slug decoding or Gem's Remote marker. `test_a_greenhouse_token_is_folded_before_it_is_checked` stays unstated: base folds before checking, and saying so would give the defect away.
