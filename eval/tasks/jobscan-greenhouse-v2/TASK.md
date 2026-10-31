# Read Greenhouse boards

jobscan reads Ashby boards only. Add Greenhouse as a second adapter: a new module `jobscan/greenhouse.py` with `ATS` (`"greenhouse"`), `DISPLAY_NAME` (`"Greenhouse"`), `CRAWL_HOSTS`, `API`, `board(company, url)` and `parse(board, data)`, with the meanings those names have in `jobscan/ashby.py`. Register it in `fetch.ADAPTERS`, so boards files, discovery and `run` handle it. Keep the standard-library-only rule.

## Board URLs

- **Every public form names one board.** These all name board `greenhouse:<token>`:
  - the board pages `https://job-boards.greenhouse.io/<token>` and `https://job-boards.eu.greenhouse.io/<token>`, and the legacy `https://boards.greenhouse.io/<token>` and `https://boards.eu.greenhouse.io/<token>`, each with or without a path after the token (`/jobs/123`);
  - the embed page `https://boards.greenhouse.io/embed/job_board?for=<token>`;
  - the Job Board API `https://boards-api.greenhouse.io/v1/boards/<token>/jobs`, with or without `?content=true`, on any host. Tests point it at a local server, and that host is kept.
- **The token.** It is percent-decoded and case-folded (Greenhouse tokens are case-insensitive), and must be printable ASCII without spaces or any of `/:?#`; `:` separates posting keys. It is encoded again for the API: board `greenhouse:ep+co` reads `…/v1/boards/ep%2Bco/jobs?content=true`.
- **The board.** The API URL is `https://boards-api.greenhouse.io/v1/boards/<token>/jobs?content=true` (or the kept host). The company is the one given, or the token when none is.
- **Not boards.** A bare host, an embed page without `for`, a token that fails the rules above, another host, a scheme other than http(s), and anything that isn't a URL.

## Payload

The API returns `{"jobs": [...]}`. Each job has:
- `id`, a number or a string;
- `title`;
- `location` (`{"name"}`);
- `offices` (a list of `{"name"}`);
- `content`, the description's HTML;
- `absolute_url`.

- **Postings.** Key `greenhouse:<token>:<id>`. The title is stripped. The URL is `absolute_url`. The location is the job's location name, then each office's name, each place once, joined with ` / `; a null `location` or `offices` is empty.
- **Descriptions.** `content` arrives entity-escaped: once from the live API, twice in Greenhouse's documented examples, and some boards send plain markup. All three give the same text, so the tagger's quotes verify against it.
- **Failures.** These fail the board (`BoardError`):
  - a malformed job: a job that isn't an object; an id that isn't a number or a string, or is a blank string (a bool is not a number); a title, content or URL that isn't a string; a location that isn't an object; offices that aren't a list of objects;
  - text with lone surrogates, which can't be stored;
  - two jobs with one id;
  - a payload that isn't `{"jobs": [...]}`.

## Discovery and runs

- Discovery reads both Greenhouse crawl hosts (`job-boards.greenhouse.io`, `job-boards.eu.greenhouse.io`) in every crawl it reads. A host that fails keeps the whole previous list, as a failing crawl does today.
- `run` also refreshes the discovered list when jobscan learns a new job site, so an install whose list predates Greenhouse rediscovers on its next run.
- A Greenhouse posting reaches the digest like an Ashby one, under the `Greenhouse` name.

## Docs

Update the README: discovery now finds Ashby and Greenhouse boards (about 4,000 and 4,800), companies that embed Greenhouse in their own careers page need the boards file, and a full run is heavier (Greenhouse's downloads alone are about 2 GB).
