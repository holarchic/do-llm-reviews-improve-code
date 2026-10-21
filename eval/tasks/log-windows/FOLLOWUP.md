# Follow-up: busiest services

Add to `logwin.py`:

`top_services(lines: Iterable[str], level: str, n: int) -> list[tuple[str, int]]`

Counts valid events of the given level per service across all lines (no windows) and returns the `n` services with the most events as `(service, count)` pairs, sorted by count, highest first, then by service name. `level` follows the same rules as in a line: any letter case, and `WARNING` means `WARN`; any other level raises `ValueError`. `n` must be at least 1, otherwise `ValueError`. Keep everything in TASK.md working, and update your tests.
