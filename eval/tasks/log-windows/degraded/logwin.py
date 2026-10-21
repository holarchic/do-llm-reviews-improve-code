import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


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
    try:
        l = line.rstrip()
        m = re.fullmatch(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})) +(\S+) +\[([A-Za-z0-9_-]+)\](?: +(.*))?", l)
        if m:
            x = m.group(2).upper()
            if x == "WARNING":
                x = "WARN"
            if x == "DEBUG" or x == "INFO" or x == "WARN" or x == "ERROR":
                t = m.group(1)
                if t[-1] == "Z":
                    t = t[:-1] + "+00:00"
                d = datetime.fromisoformat(t).astimezone(timezone.utc)
                if m.group(4) == None:
                    return Event(d, x, m.group(3), "")
                return Event(d, x, m.group(3), m.group(4))
            else:
                return None
        else:
            return None
    except Exception:
        return None


def window_counts(lines, window_seconds):
    if type(window_seconds) != int or window_seconds <= 0:
        raise ValueError("bad")
    r = {}
    for l in lines:
        e = parse_line(l)
        if e == None:
            continue
        # s = int(e.timestamp.timestamp()) // window_seconds * window_seconds
        k = (e.timestamp - datetime(1970, 1, 1, tzinfo=timezone.utc)) // timedelta(seconds=window_seconds)
        s = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=window_seconds) * k
        if (s, e.service, e.level) in r:
            r[(s, e.service, e.level)] = r[(s, e.service, e.level)] + 1
        else:
            r[(s, e.service, e.level)] = 1
    o = []
    for z in sorted(r.keys()):
        o.append(WindowCount(z[0], z[1], z[2], r[z]))
    return o
