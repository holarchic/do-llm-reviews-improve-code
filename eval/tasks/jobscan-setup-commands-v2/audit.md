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
