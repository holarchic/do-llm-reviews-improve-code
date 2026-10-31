# Fairness audit: jobscan-email

An independent agent (Sonnet 5.5, 2026-10-27) audited the task. It read TASK.md, the `base` snapshot, both hidden modules and defects.json, and saw no other version of the repository.

**Verdict:** fair to ship.

Feature tests: all 9 fair.
- Applied: From and To are compared by their address part, so a display-name From passes.

Review tests:

| Test | Verdict | Note |
|---|---|---|
| `SecretInReplyTest.test_a_refusal_quoting_the_password_never_reaches_any_output` | concern, kept | It rests on the norm that secrets never reach logs, and the server's reply quotes the password. TASK.md states that no message shows the password only for a broken setup. Widening that sentence would give the test away. |
| `SecretInReplyTest.test_a_server_that_keeps_challenging_never_gets_the_password_into_any_output` | concern, kept | Same basis. smtplib's own AUTH-loop error quotes the reply in a repr; the check strips backslashes, so any depth of escaping is caught. |
| `AcceptedMessageTest.test_a_refused_goodbye_after_the_message_is_accepted_is_not_a_failure` | fair | `with smtplib.SMTP(...)` raises on a 421 reply to QUIT, a common idiom. TASK.md does not give it away. |
| `RecipientTest.test_mail_never_goes_to_an_address_other_than_the_configured_one` | fair | Refusing `a:b@example.com`, or sending to it literally, both pass. Only letting the mail library read a group out of `to` fails. |

**Fixture soundness:** no blocking issues. These smtplib uses all pass when probed: `SMTP_SSL(host, port, context=…)`, `SMTP_SSL()` then `connect()`, a `timeout` argument, `with` blocks, explicit `ehlo()`, `sendmail` and `send_message`. The writer's TLS context is inspected (it must verify the certificate and host name) but never used.

## Spec–test consistency

An independent agent (Opus 5.5, 2026-10-31) checked every TASK.md statement against the reference code, the hidden feature and review tests, and the regression-set base tests that touch the same behaviour. It read the `base` and `reference` trees and probed literal readings in scored copies. The builder verified each finding against the cited test and code before applying it. Classes:
- **contradiction**: a build that follows the statement literally fails a kept test;
- **private reach**: a hidden test calls a name neither TASK.md nor base's public surface gives;
- **invitation**: loose wording about existing behaviour a regression test pins, inviting a change;
- **underdetermined**: a feature test asserts a choice the statement leaves open and base doesn't settle.

Silence alone is not a finding: keeping base behaviour is the default. No fix may reveal a review-caught defect.

**Result:** all 37 statements consistent, no change.

Near-misses, checked and left as they are (no kept test fails on a literal reading):
- "a server that doesn't offer STARTTLS is refused": the test expects the failed-send path (exit 1). The sending bullets' "a failed send … exits 1" settles it.
- stdout on a failed send: base's `run` output and its stderr convention settle it.
- "A run with no new digest sends nothing": the subject example `0 new for ae · 2 untagged` settles the 0-new case.

Fairness: "No message ever shows the password" is not widened to server replies, and nothing is said about QUIT or recipient parsing. Each would give a review test away.
