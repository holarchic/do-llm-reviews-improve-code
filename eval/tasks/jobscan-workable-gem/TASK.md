# Read Workable and Gem boards

jobscan reads Ashby and Greenhouse boards. Add two more applicant-tracking systems, Workable and Gem, as adapters like `jobscan/ashby.py` and `jobscan/greenhouse.py`: new modules `jobscan/workable.py` and `jobscan/gem.py`, each with `ATS`, `DISPLAY_NAME`, `CRAWL_HOSTS`, `api_board(company, url)`, `page_board(company, url)` and `parse(board, data)`, with the meanings those names have today. Register both in `fetch.ADAPTERS`, so `fetch.board_for`, boards files, discovery and `run` handle them. Keep the standard-library-only rule.

## Slugs

Workable's and Gem's APIs are case-sensitive, so their slugs keep their case, unlike Greenhouse's, which fold. The slug check Greenhouse does today (percent-decoded, printable ASCII, free of `/:?#`, since `:` separates posting keys) moves to `jobscan/payload.py`, shared by every adapter. Each adapter decodes a slug for the board id and re-encodes it for its API URL, so `ep%2Bco` is board `workable:ep+co` and API `…/ep%2Bco…`.

## Workable

- **Board URLs.** A board page is `https://apply.workable.com/<account>`, with or without a trailing slash or a job path after it (`/acme/j/599813E1DD/`), and with any case of host and an explicit `:443`. The API is `https://apply.workable.com/api/v1/widget/accounts/<account>`, with or without `?details=true`, on any host: tests point it at a local server and keep that host. Every form of one account is one board, `workable:<account>`, whose API URL is `…/api/v1/widget/accounts/<account>?details=true`.
- **Not boards.** The bare host; `apply.workable.com/j/<shortcode>`, which links a job without naming its account; the API path without an account; a slug that fails the shared check; and any other Workable host (`jobs.workable.com`, `<company>.workable.com`).
- **Payload.** `{"jobs": [...]}`. Each entry has `shortcode`, `title`, `url`, `description` (HTML), `telecommuting` (bool), the job's own `city`, `state`, `country`, and `locations`, a list of `{city, region, country, countryCode, hidden}`.
- **One posting per shortcode.** The widget lists a job once per location. Entries sharing a shortcode merge into one posting carrying every entry's locations, a place repeated across entries listed once, and repeats that disagree on title or description fail the board (`BoardError` with "repeats with a different" in its message).
- **Postings.** Key `workable:<account>:<shortcode>`; title stripped; URL the entry's `url`; description its HTML as text, like Greenhouse's.
- **Location.** Each location record as `city, region, country` (empty parts left out), hidden records included, then `Remote` when the job is telecommuting, joined with ` / `. A job without location records (`locations` empty, missing or null) uses its own `city, state, country`.
- **Failures.** A malformed entry, a payload that isn't `{"jobs": [...]}`, or text with lone surrogates fails the board with `BoardError`. Every listed field is type-checked whether or not it is used: `shortcode` is a non-empty string (a number fails), text fields are strings, `telecommuting` is a bool, and `locations` is a list of objects or null. No jobs is an empty board.

## Gem

- **Board URLs.** A board page is `https://jobs.gem.com/<slug>`, with any path after it and any host case or `:443`. The API is `https://api.gem.com/job_board/v0/<slug>/job_posts/`, or without `job_posts/`, on any host, which is kept. Board `gem:<slug>`, API URL `…/job_board/v0/<slug>/job_posts/`.
- **Not boards.** The bare host, the API path without a slug, a slug failing the shared check, and other Gem hosts (`www.gem.com`).
- **Payload.** A list of jobs. Each has `id`, `title`, `absolute_url`, `content_plain`, `content` (HTML), `location` (`{"name"}`), `offices` (a list of `{"name", "location": {"name"}}`), and `location_type`.
- **Postings.** Key `gem:<slug>:<id>`; title stripped; URL `absolute_url`; description the stripped plain text, or the HTML as text when the plain text is blank. Two jobs with one id fail the board.
- **Location.** The job's location name, then each office's name and its location's name, leaving out empty ones and repeats, joined with ` / `; a null or missing `location` or `offices` is empty. A remote job (`location_type` `remote`) adds `Remote` unless a name already says Remote.
- **Failures.** As for Workable: malformed fields, a payload that isn't a list, or lone surrogates fail the board. Every listed field is type-checked whether or not it is used: `id` is a non-empty string (a number fails); `title`, `absolute_url`, `content_plain`, `content` and `location_type` are strings; `location` and each office's `location` are objects; `offices` is a list of objects.

## Routing and discovery

- API paths win over board pages, on any host: `fetch.board_for` asks every adapter's `api_board` before any `page_board`. For example, `https://jobs.ashbyhq.com/api/v1/widget/accounts/acme` is Workable, and `https://jobs.gem.com/posting-api/job-board/acme` is Ashby.
- Discovery walks `apply.workable.com` and `jobs.gem.com` in the crawl index, slugs keeping their case. An install whose discovered list predates an adapter rediscovers on its next run, as it does for Greenhouse today.
- In the digest, a board's open-role count counts its postings, not Workable's per-location entries.
