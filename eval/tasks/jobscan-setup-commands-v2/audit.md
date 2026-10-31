# Fairness audit: jobscan-setup-commands

An independent agent (Sonnet 5.5, 2026-10-27) audited the task. It read TASK.md, the `base` snapshot, the hidden modules and defects.json, and saw no other version of the repository.

**Verdict:** needs changes; one review test rewritten, none dropped.

Feature tests: all fair.

Review tests:

| Test | Verdict | Note |
|---|---|---|
| `PreviewTest.test_preview_counts_only_boards_the_next_run_reads` | concern, kept | The norm is real: base's run counts only the boards it reads (the discovered list and the boards file). TASK.md never says the counts are scoped to those boards; stating it would give the defect away. |
| `EmptyFlagTest.test_init_with_a_flag_given_empty_asks_nothing_and_refuses` | fair | Any flag means no questions; `--name ""` is a flag given. |
| `EmptyFlagTest.test_email_with_an_empty_sender_is_refused_sending_and_saving_nothing` | concern, kept | An empty `--from` could be read as left out. The audit's fix (TASK.md saying an empty value counts as given) states the edge case outright, so it was not applied. |
| `BoardsFileTest.test_a_boards_file_holding_a_lone_surrogate_is_kept_or_refused_never_crashed_on` | unfair as first written, rewritten | It first demanded exit 2 and an unchanged file. But `json.dumps`' default escaping writes the lone surrogate back safely and keeps the file, which TASK.md asks for. It now accepts either outcome, a refusal that leaves the file as it was or a write that keeps the note and adds the board, and fails only an exception. The historical build raised `UnicodeEncodeError` (`crash_is_defect`). Checked: a reference build switched to default escaping passes. |

Also noted: a feature test's check for leftover `.boards-*` temporary files is vacuous for other names but can't fail a correct build.

## Spec–test consistency

An independent agent (Opus 5.5, 2026-10-31) checked every TASK.md statement against the reference code, the hidden feature and review tests, and the regression-set base tests that touch the same behaviour. It read the `base` and `reference` trees and probed literal readings in scored copies. The builder verified each finding against the cited test and code before applying it. Classes:
- **contradiction**: a build that follows the statement literally fails a kept test;
- **private reach**: a hidden test calls a name neither TASK.md nor base's public surface gives;
- **invitation**: loose wording about existing behaviour a regression test pins, inviting a change;
- **underdetermined**: a feature test asserts a choice the statement leaves open and base doesn't settle.

Silence alone is not a finding: keeping base behaviour is the default. No fix may reveal a review-caught defect.

| Statement | Class | Resolution |
|---|---|---|
| "An error prints `jobscan: <message>` on stderr and nothing on stdout" (with exit 1 = "something left undone") | contradiction | `run` prints its summary line on stdout when it exits 1, and `email` prints `Sending a test email …` on stdout. Both v1 A3 builds emptied stdout on exit 1 and moved "Sending" to stderr, citing this rule: 4 regression tests and 1 feature test. Now only a refusal before any work (exit 2) has an empty stdout, and exits 0 and 1 keep their usual output, named. |
| "Exit 1: … (a test email refused, a run holding the database)" | contradiction | `run` and `discover` refused on a held lock stay exit 2 (2 regression tests); only `preview` is exit 1. Now scoped to `preview`, with `run` and `discover` stated as unchanged. |
| `email`: "No flags and no terminal on stdin: exit 2 … no terminal to ask in" | contradiction (precedence) | With no install, base refuses first on the missing profile (`test_init.EmailSetupTest.test_no_install_no_questions`). Both v1 A3 builds checked the terminal first. Now stated: with no install, the missing-profile refusal comes first, as now. |
| `PreviewTest.test_a_run_holding_the_lock_is_exit_1_and_the_database_unopened` | private reach | It held the lock through `cli._run_lock`, a private class in base, and v1 A3 r2 failed it on the rename. It now takes `fcntl.flock(LOCK_EX \| LOCK_NB)` on `$JOBSCAN_HOME/.lock`, as base's own lock tests do, and TASK.md names that file as the run lock. It passes on reference and on v1 A3 r2's build, and fails on base. |
| `preview`: "No profile or a broken install is exit 2" beside "Before any run has made the database … exit 0" | underdetermined (precedence) | `PreviewTest.test_no_profile_is_exit_2` ran with no database, so it pinned which check comes first. The test now seeds the database first, so it no longer depends on the order. It passes on reference and fails on base. |

The other ~90 statements are consistent. The earlier audit's review-test verdicts (preview board scope, empty flags, surrogate) are untouched.
