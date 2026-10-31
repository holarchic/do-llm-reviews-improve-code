# Company notes

Let a profile carry the user's own note about companies they already know (applied, rejected, mid-conversation), shown on every digest entry from that company.

## The profile table

An optional `[company_notes]` table in the profile file, from a company name or board slug to free text:

```toml
[company_notes]
"Wise" = "rejected 14 Oct: only a clearly different role"
"Monzo" = "talking to the hiring manager: mention it to them"
```

- `jobscan/config.py` gains a record `CompanyNote(name, note)`, and `Profile` a `company_notes` field: a tuple of them in file order, empty when the table is absent.
- `config.profile_toml` writes the table only when there are notes, after every top-level key and before the custom tags' tables, as `"<name>" = "<note>"` lines quoted as TOML strings. `load_profile` reads back exactly what it wrote.
- A `company_notes` that isn't a table of non-blank names to non-blank text notes is refused with a `ConfigError` whose message contains "`company_notes` must be a table of company names to your notes". A table that names one company twice, compared stripped and case-folded, is refused with "`company_notes` names a company twice".

## Matching

`jobscan/watch.py` gains `noted(notes, company, board_id) -> tuple[str, ...]`: the note texts, in file order, of every note whose name matches the company as a `watch` term would (whole words of the company's name, or the board's slug).

## The digest

Each note naming an entry's company shows as its own `Your note: <note>` line, under the entry's link line and its `Company:` line, if it has one, before its tag lines; several notes for one company each get their own line, in file order. Entries without a note are unchanged.

## Elsewhere

- `jobscan check` lists the notes.
- The set-up page can't edit them: it keeps them as the profile file has them, and the summary counts them.
- Document `[company_notes]` in the README and in the profile example `--help` prints. A short name can catch another company (`Apple` also names Apple Leisure Group), so the README suggests the fuller name or the board's slug.
