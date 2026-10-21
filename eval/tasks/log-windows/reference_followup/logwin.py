import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
LEVELS = {"DEBUG": "DEBUG", "INFO": "INFO", "WARN": "WARN", "WARNING": "WARN", "ERROR": "ERROR"}
_LINE = re.compile(
    r"(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))"
    r" +(?P<level>\S+) +\[(?P<service>[A-Za-z0-9_-]+)\](?: +(?P<message>.*))?"
)


@dataclass(frozen=True)
class Event:
    timestamp: datetime
    level: str
    service: str
    message: str


@dataclass(frozen=True)
class WindowCount:
    start: datetime
    service: str
    level: str
    count: int


def parse_line(line):
    match = _LINE.fullmatch(line.rstrip())
    if match is None:
        return None
    level = LEVELS.get(match["level"].upper())
    if level is None:
        return None
    try:
        timestamp = datetime.fromisoformat(match["timestamp"].replace("Z", "+00:00"))
    except ValueError:
        return None
    return Event(timestamp.astimezone(timezone.utc), level, match["service"], match["message"] or "")


def window_counts(lines, window_seconds):
    if not isinstance(window_seconds, int) or isinstance(window_seconds, bool) or window_seconds <= 0:
        raise ValueError(f"window_seconds must be a positive integer, got {window_seconds!r}")
    width = timedelta(seconds=window_seconds)
    counts = Counter()
    for line in lines:
        event = parse_line(line)
        if event is not None:
            start = EPOCH + width * ((event.timestamp - EPOCH) // width)
            counts[(start, event.service, event.level)] += 1
    return [WindowCount(start, service, level, count) for (start, service, level), count in sorted(counts.items())]


def top_services(lines, level, n):
    wanted = LEVELS.get(level.upper())
    if wanted is None:
        raise ValueError(f"unknown level {level!r}")
    if n < 1:
        raise ValueError(f"n must be at least 1, got {n}")
    counts = Counter()
    for line in lines:
        event = parse_line(line)
        if event is not None and event.level == wanted:
            counts[event.service] += 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:n]
