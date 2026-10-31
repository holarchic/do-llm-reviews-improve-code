# Set-up commands an agent can drive

jobscan is set up by answering questions in a terminal, and the only way to see what an install holds or matches is to run it. Make every set-up step work without a terminal, so a person or a coding agent can set jobscan up, inspect it and tune a search from the help text alone: new commands `tags`, `check`, `boards` and `preview`, flag forms of `init` and `email`, and help that lays the steps out in order. Keep the standard-library-only rule.

## Exit codes and errors

- Exit 0: done. Exit 1: finished with something left undone (a test email refused, a run holding the database). Exit 2: a config or usage error, before any work.
- An error prints `jobscan: <message>` on stderr and nothing on stdout.

## Help: `jobscan/arguments.py`

A new module holds every command, flag and help text; `cli.main` parses with it.
- `arguments.parser()` returns the `jobscan` parser. Its commands, in this order: `init`, `email`, `boards` (with a nested `add`), `check`, `run`, `preview`, `schedule`, `tags`, `discover`, `eval`.
- Every command, nested ones included, has a description and an epilog that starts `Examples:` and lists example invocations one per line, each indented two spaces; at least one starts with the command's full invocation (`  jobscan boards add https://job-boards.greenhouse.io/acme`). Between them, a command's examples use every flag it takes.
- `arguments.SET_UP` is the epilog of `jobscan --help`: the set-up steps, numbered, in the order above, each on a line of its own, `  1. jobscan init`, `  2. jobscan email`, `  3. jobscan boards add URL`, `  4. jobscan check`, `  5. jobscan run`, `  6. jobscan preview`, `  7. jobscan schedule --at 08:00`, with a short note on each and a flags example under `init` and `email`. It also says where the profile lives, that secrets never go on the command line, and the exit codes.
- No flag takes a secret: a flag whose name holds `key`, `password`, `secret` or `token` is a switch (`--key-stdin`, `--password-stdin`), and the secret is read from stdin or the environment.

## `jobscan tags [NAME]`

Lists the tags the model can answer, or prints one tag's definition. It works before `init` and writes nothing.
- The list: every built-in tag in `tags.toml` order, then the profile's custom tags. Names are padded to the longest built-in name; `*` marks a tag the profile asks; each built-in shows its summary and `[validated]`, `[not validated]` or `[validated for the UK only]`; each custom tag shows its definition's first line:
  ```
  Built-in tags (* = in your profile):
    * founding           Is this the first and only hire in the role at the company?  [validated]
    * location_eligible  Can you take the job from where you are?  [validated for the UK only]
      segment            Sales roles: enterprise, mid-market or SMB customers?  [not validated]
      ...
  Your custom tags (every one is asked; none is validated):
    * climate            The company's product reduces emissions.
  `jobscan tags NAME` shows a tag's full definition, as the model reads it.
  ```
  Before `init` the header is `Built-in tags:` and nothing is marked.
- `jobscan tags NAME` prints `NAME (<kind>; in your profile)` (or `; not in your profile`; nothing before `init`), then `Answers: yes, no, unknown` (the allowed values joined by `, `, ` (a list of these)` for a multi tag, `; default X` when it has one, or `free text`), a blank line, and the definition exactly as the model reads it, with the profile's `eligible_where` filled in (`Someone who wants to work from the UK can do this job.`). Before `init` the place reads `<where you can work from>`.
- The kind is `built-in tag, validated`, `built-in tag, not validated`, or `custom tag, not validated`. `location_eligible` is `built-in tag, validated for the UK only` before `init`; with a profile it is validated exactly when the digest marks it validated for that profile's place (`the UK` yes, `Germany` no).
- An unknown name is exit 2: `` no tag named 'nope'; `jobscan tags` lists them ``. A broken profile is exit 2, naming its file.

## `jobscan check`

Loads `config.toml`, the key, the profile and its tags, `email.toml` and the boards file exactly as `run` loads them, then the key as a scheduled run reads it (with no shell environment), and prints what the install holds. Reads only: no network, no database, no log.
```
Install: /home/me/.jobscan
Profile: /home/me/.jobscan/profiles/ae.toml
  titles: Account Executive; Enterprise Account Executive
  excluded titles: (none)
  locations: London
  where you can work from: the UK
  tags: founding, location_eligible; custom: climate
Model: deepseek/deepseek-v4-flash at https://openrouter.ai/api/v1 (key: openrouter.key)
Email: off (`jobscan email` sets it up)
Boards file: none, so discovered boards only (`jobscan boards add URL` starts one)
OK: `jobscan run` can start.
```
- Titles, excluded titles and locations join with `; `, tags with `, `; an empty list is `(none)`. `; custom: …` appears only when the profile has custom tags.
- With email set up: `Email: digests to you@gmail.com from you@gmail.com (smtp.gmail.com:465)`. With a boards file: `Boards file: /home/me/.jobscan/boards.json (2 boards)`, plus `, 1 not recognised` for entries no adapter knows.
- The key source: `openrouter.key`; `OPENROUTER_API_KEY` when the key is only in the environment; `OPENROUTER_API_KEY here, openrouter.key for scheduled runs` when both; `none needed` for a model that isn't OpenRouter.
- A key only in the environment adds `Warning: the key is only in OPENROUTER_API_KEY, which a scheduled run won't see: save it in <home>/openrouter.key, readable only by you (chmod 600)` before the OK line.
- The first problem is exit 2 with the loaders' message, which names the file at fault (`ae.toml`, `email.toml`, `config.toml`, `boards.json`): no key at all, a malformed key, a tag the profile can't ask (`unknown tags ['founder']`). A key file a scheduled run can't use is exit 2 even when `OPENROUTER_API_KEY` is set (the key file's `chmod 600` message).

## `jobscan init` by flags

`init` with no flags asks in the terminal as now (`init.init(home)` is unchanged). Given any of its flags, it asks nothing and writes the same files the same answers typed would write, byte for byte:
- `--name NAME`, `--title TITLE` (repeat), `--exclude-title TITLE` (repeat; default none), `--location PLACE` (repeat), `--eligible-where TEXT`, `--tag NAME` (repeat; default every built-in tag, in `tags.toml` order), `--key-stdin`.
- A list flag takes one value per use, so a value may hold a comma: `--title 'Head of Sales, EMEA'` is one title.
- The key: one line from stdin with `--key-stdin`, else `OPENROUTER_API_KEY`, saved to `openrouter.key` (mode 600) either way. Neither: `no OpenRouter key: pipe it in with --key-stdin, or set OPENROUTER_API_KEY`.
- Refused with exit 2, writing nothing, not even `$JOBSCAN_HOME`:
  - a required flag missing, all named at once: `` missing --title, --location, --eligible-where (see `jobscan init --help`) ``;
  - a name that isn't lower-case letters, digits and `-`, starting with a letter or digit, checked before any path is built from it: `profile name '../escape': use lower-case letters, digits and -, starting with a letter or digit`;
  - a value the questions wouldn't accept, named by its list and quoted: `titles: ' PM' is empty, padded with spaces or not valid text` (the lists are `titles`, `excluded titles`, `locations`, `tags`, `where you can work from`);
  - an unknown tag (`` unknown tags ['founder']; `jobscan tags` lists them ``), or a tag given twice (`` `tags` lists a name twice ``, as the profile file refuses it);
  - a key that isn't one: `that isn't an OpenRouter key: empty, or it holds spaces or control characters`;
  - an existing install, as now (`… is already set up …`).
- No flags and no terminal on stdin: exit 2, asking nothing: `` no terminal to ask in: give the flags instead (see `jobscan init --help`) ``.
- On success it prints what it wrote and the next steps, as `init` does now. Email is `jobscan email`'s step.

## `jobscan email` by flags

`email` with no flags asks in the terminal as now. Given any of `--to`, `--from`, `--host`, `--port`, `--password-stdin`, it asks nothing, prints `Sending a test email to <to> ...`, sends the same test email, and saves `email.toml` only once it is accepted:
- `--to ADDRESS`: where digests go. `--from ADDRESS`: the sending account and login; the `--to` address when `--from` is left out.
- Gmail and iCloud senders get their preset server and port. Any other sender needs `--host` and `--port` together.
- `--password-stdin`: the app password is one line from stdin, its line ending stripped; a Gmail password loses its spaces, as a typed one does, and any other is kept as given (`pass word`).
- Refused with exit 2 before anything is sent or saved:
  - `` --to: not an email address (see `jobscan email --help`) ``, and the same for `--from`;
  - an Outlook.com or Hotmail sender: `<sender>: Outlook.com lets mail apps sign in only with OAuth, which jobscan can't do`;
  - `give --host and --port together`; `--host must be a server name and --port a port number`;
  - a sender with no preset and no server: `no preset mail server for me@fastmail.com: give --host and --port`;
  - `the app password comes from stdin: add --password-stdin`;
  - `that isn't an app password jobscan can send (empty, or control characters)`;
  - `--off` with any other flag: `--off takes no other flags`.
- A refused test email is exit 1, `… nothing was saved`, as now.
- No flags and no terminal on stdin: exit 2, asking nothing: `` no terminal to ask in: give the flags instead (see `jobscan email --help`) ``.

## The boards file: `jobscan boards`, `jobscan boards add URL…`

- The boards file is `config.toml`'s `boards_file`, else `$JOBSCAN_HOME/boards.json`. `load_config` sets `Config.boards_file` to `boards.json` when config names none and that file exists (else `None`), so every run reads it.
- `jobscan boards` lists it, one entry per line, then exit 0:
  ```
  /home/me/.jobscan/boards.json: 3 entries
    Beta AI  https://jobs.ashbyhq.com/beta  [Ashby]
    (name looked up)  https://job-boards.greenhouse.io/acme  [Greenhouse]
    X  https://x.example  [not recognised: a run skips it]
  ```
  With no file: `` No boards file yet (/home/me/.jobscan/boards.json): runs read discovered boards only. `jobscan boards add URL` adds one. ``
- `jobscan boards add URL… [--company NAME]` appends `{"board_url": URL}` per URL, as given, or `{"company": NAME, "board_url": URL}` with `--company`. With no file yet, it creates one holding `{"companies": [...]}`. It makes no request.
  - Each URL prints `added greenhouse:acme`, or `already listed ashby:beta` for a board the file lists already (by board id, so `https://jobs.ashbyhq.com/BETA` is `ashby:beta`); that one is skipped. Then `Saved <file>; the next run reads it.` (`them` for several).
  - Everything else in the file (other keys, other entries, unrecognised ones) is kept.
  - Every URL is checked first: one no adapter knows adds nothing, exit 2: `https://example.com/careers: not a job board URL jobscan knows; nothing was added`.
  - `--company` with more than one URL is exit 2: `--company names one board: give one URL with it`.
  - A boards file it can't use is refused with exit 2 and left unchanged; one a run can't read gets the run's message (`<file>: not a readable boards file (…)`).
  - The file is replaced in one rename, so a run reads the old list or the new; no temporary file is left behind.

## `jobscan preview`

Says what the next run would do for the saved profile, from the postings already stored, without fetching anything, so a search can be tuned without a run:
```
ae, against the 1,204 open postings …:
  matching your titles and locations: 6 postings
  new jobs the next run would tag: 3 (about $0.00)
  already sent to you: 2
  left out by your excluded titles: 1
New jobs, newest first:
  Beta Inc — Senior Account Executive — London, UK
  Acme AI — Account Executive — London, UK
Left out by your excluded titles:
  Acme AI — Account Executive Assistant — London, UK
```
- The first line names the profile and how many open postings it counts: `<profile>, against the <N> open postings`, then a few words saying which postings those are. The sections follow one another with no blank lines.
- Matching: the profile's titles and locations match and no excluded title does, as the run's filter decides. Left out: titles and locations match, but an excluded title hits.
- New jobs are counted as the run counts them: one posting per job (the run's rule for a job on several boards), none of a job already sent under any key. The cost is $0.0005 a job, to the cent. Already sent: matching postings whose job was sent.
- Samples: up to 10 new jobs, newest first by when the posting was first stored, then up to 10 left out, as `  <company> — <title> — <location>` (`(no location)` when empty). The company is named as the digest names it: the boards file's name for the board, else the looked-up name, else the board's slug. Counts use thousands separators.
- Before any run has made the database: `` No postings yet: `jobscan run` fetches them, then `jobscan preview` can count them. ``, exit 0, creating nothing (no database, lock or log).
- It takes the run lock without waiting, before opening the database: while a run holds it, exit 1 and `a run is using the database; preview once it has finished`. Opening the database backs it up first, as a run does.
- No profile or a broken install is exit 2.

## Docs

Update the README: setting jobscan up with an agent (every step works without a terminal; read `jobscan --help`; `init` and `email` take flags; secrets from stdin; `check` confirms the install; `preview` tunes titles after the first run), the new commands in the commands table, and `jobscan boards add` as the way to add companies discovery misses. In CONTRIBUTING, say commands and help live in `jobscan/arguments.py`, each command's examples use all its flags, and no flag takes a secret.
