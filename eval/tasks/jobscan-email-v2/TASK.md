# Email each digest

jobscan writes a markdown digest when a run finds new postings. Add emailing: when the user has set up email, every `jobscan run` that publishes a digest emails it from the user's own mail account, with an app password. Keep the standard-library-only rule.

## Settings: `email.toml`

The settings live in `email.toml` in the jobscan home (`~/.jobscan`, or `$JOBSCAN_HOME`), a TOML file with exactly these keys:

```toml
to = "you@example.com"        # where digests go: one email address
from = "me@gmail.com"         # the sending account: one email address, also the login
host = "smtp.gmail.com"       # the account's mail server
port = 465                    # 1–65535
password = "abcd efgh ijkl mnop"  # the app password, as typed
```

- No `email.toml`: the install doesn't email, and `run` behaves as before.
- The file holds a password, so, like an ssh key, it is refused when anyone but its owner can read it (any group or other permission bit): the user is told to `chmod 600` it.
- A missing, extra or malformed key, or a file that can't be read or parsed, is a broken setup. `run` and `schedule` refuse a broken setup before any other work, with exit 2 and a message (and, with `--notify`, a notification) that names `email.toml`. No message ever shows the password.

## Sending

- Use the standard library's `smtplib`, opening the connection through the `smtplib` module at send time (`smtplib.SMTP_SSL(...)` or `smtplib.SMTP(...)`): the hidden tests replace those two classes to point them at a local server.
- Port 465 is TLS from the start (`smtplib.SMTP_SSL`). Any other port connects with `smtplib.SMTP` and must upgrade with STARTTLS before the login; a server that doesn't offer STARTTLS is refused, and never sees the login. Both verify the server's certificate and host name (`ssl.create_default_context()`).
- Log in with the `from` address and the password, then send one message:
  - Subject `jobscan: <new> new for <search>`, plus ` · <n> untagged` when the digest carries untagged postings (for example `jobscan: 0 new for ae · 2 untagged`);
  - From the `from` address, To the `to` address;
  - the body is the published digest file's text, as written.
- A failed send never stops the run: the digest stays on disk, the run exits 1, `runs.log` gets an `email_failed` line saying why, and the `--notify` notification, after its usual text, adds `; the digest couldn't be emailed; see runs.log` (for example `2 new postings in /home/me/.jobscan/digests/ae; the digest couldn't be emailed; see runs.log`). A digest whose email failed is never resent by a later run.
- A sent digest logs an `email_sent` line. A run with no new digest sends nothing.

## `jobscan email`

A new command that sets up emailing, and `init` offers it as its last step:

- It asks for the address and the app password. Gmail and iCloud addresses get their server and port preset (`smtp.gmail.com` 465; `smtp.mail.me.com` 587). Personal Outlook.com and Hotmail addresses are refused, since Microsoft allows only OAuth sign-in there; any other address is asked for its server and port.
- It sends a test email with those settings and saves `email.toml` (mode 600) only once the test is accepted, replacing any previous settings in one rename; a failed test saves nothing and keeps the previous file.
- `jobscan email --off` removes `email.toml`, even a broken one, and succeeds when there is none.

## Docs

Update the README: the email setup (Gmail needs 2-Step Verification for an app password; iCloud has app-specific passwords), that personal Outlook.com accounts can't be used, that a digest whose email fails isn't resent, and exit 1 for a digest that couldn't be emailed.
