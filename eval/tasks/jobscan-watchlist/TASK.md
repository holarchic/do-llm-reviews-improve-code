# A watchlist of companies

Let a profile name companies the user cares about, so their postings lead each digest, marked ★.

## The profile key

An optional `watch` key in the profile file, a list of names:

```toml
watch = ["Monzo", "Wise", "hiive"]
```

- `Profile` gains a `watch` field, a tuple of the names, empty when the key is absent or `[]`.
- `config.profile_toml` writes `watch = [...]` only when the list is non-empty, quoting names as TOML strings, and `load_profile` reads back exactly what it wrote.
- A `watch` that isn't a list of non-empty strings is refused with a `ConfigError` whose message contains "`watch` must be a list of strings". A list that names one company twice, ignoring case, is refused with "`watch` lists a company twice".

## Matching: `jobscan/watch.py`

A new module with `watched(terms, company, board_id) -> bool`. A posting is watched when any term:
- matches whole words of the company's name, case-folded (`"Acme"` matches "Big Acme" and "Acme AI", not "Acmeco"; `"Straße"` matches "STRASSE GmbH"); or
- equals the slug of the posting's board (the part of the board id after `ats:`), case-folded.

## The digest

- Watched postings come first, in their usual order among themselves, then the rest. Each watched heading starts `★ ` (`## ★ Acme — Account Executive · London`).
- A digest with any ★ carries one legend line after its title, `digest.WATCH_LEGEND`: "★ marks a company on your watchlist (`watch` in your profile)."
- The title counts them: `# ae — 2026-10-12 · 4 new · 2 watched · 1 left out`.
- A watched posting that a requirement left out is starred in the left-out list too (`- ★ Acme — …`, the star right after the `- `), and it isn't counted as watched.
- A digest without watched postings is unchanged.

## Elsewhere

- `jobscan check` lists the watchlist (`  watchlist: Acme; hooli-eu`, or `(none)`).
- The set-up page can't edit the watchlist: it keeps it as the profile file has it, and the summary counts it ("Watchlist: 2 companies").
- Document `watch` in the README and in the profile example `--help` prints.
