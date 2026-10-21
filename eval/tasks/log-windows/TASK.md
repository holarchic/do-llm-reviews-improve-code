# Task: log counts per time window

Write a Python module `logwin.py` at the repository root (standard library only). Tests import it as `import logwin`.

## Interface

- `@dataclass(frozen=True) class Event` with fields `timestamp: datetime` (timezone-aware, in UTC), `level: str`, `service: str`, `message: str`.
- `parse_line(line: str) -> Event | None`
- `@dataclass(frozen=True) class WindowCount` with fields `start: datetime` (timezone-aware, in UTC), `service: str`, `level: str`, `count: int`.
- `window_counts(lines: Iterable[str], window_seconds: int) -> list[WindowCount]`

## Line format

A valid line is `<timestamp> <level> [<service>] <message>`, fields separated by one or more spaces. A trailing newline is allowed.

1. `timestamp` is `YYYY-MM-DDTHH:MM:SS`, optionally followed by fractional seconds (`.` and digits), and then a required UTC offset: `Z` or `+HH:MM` / `-HH:MM`. A timestamp without an offset makes the line invalid. The event's timestamp is that instant converted to UTC.
2. `level` is one of `DEBUG`, `INFO`, `WARN`, `WARNING`, `ERROR`, in any letter case. It is stored upper-case, and `WARNING` is stored as `WARN`. Any other level makes the line invalid.
3. `service` is one or more of `A-Z a-z 0-9 _ -`, inside square brackets.
4. `message` is the rest of the line after the whitespace that follows `]`, with trailing whitespace removed. It may contain spaces and brackets, and may be empty.
5. `parse_line` returns `None` for any invalid line, including a blank one. It never raises.

## Windows

6. `window_seconds` must be a positive integer; otherwise `window_counts` raises `ValueError`.
7. Windows are aligned to the Unix epoch in UTC: an event belongs to the window starting at `floor(t / window_seconds) × window_seconds` seconds after `1970-01-01T00:00:00Z`, where `t` is its UTC time in seconds (fractions included, negative before 1970).
8. `window_counts` skips invalid lines, accepts lines in any order, and returns one `WindowCount` per (window start, service, level) that has at least one event, sorted by `start`, then `service`, then `level`.

Include your own tests.
