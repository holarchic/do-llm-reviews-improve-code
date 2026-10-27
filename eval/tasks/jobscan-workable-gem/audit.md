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
