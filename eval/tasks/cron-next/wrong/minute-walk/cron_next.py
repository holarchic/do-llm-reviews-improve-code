from dataclasses import dataclass
from datetime import datetime, timedelta

FIELDS = ("minute", "hour", "day_of_month", "month", "day_of_week")
RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
DAYS = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"]


class CronSyntaxError(ValueError):
    def __init__(self, field, text):
        super().__init__(f"{field}: {text}")
        self.field = field
        self.text = text


class NoFireTimeError(Exception):
    def __init__(self, after):
        super().__init__(str(after))
        self.after = after


@dataclass(frozen=True)
class CronSchedule:
    minutes: frozenset
    hours: frozenset
    days_of_month: frozenset
    months: frozenset
    days_of_week: frozenset
    dom_restricted: bool
    dow_restricted: bool


def _num(text, idx, field_text):
    lo, hi = RANGES[idx]
    if text.isdigit():
        v = int(text)
    elif idx == 3 and text.upper() in MONTHS:
        v = MONTHS.index(text.upper()) + 1
    elif idx == 4 and text.upper() in DAYS:
        v = DAYS.index(text.upper())
    else:
        raise CronSyntaxError(FIELDS[idx], field_text)
    if v < lo or v > hi:
        raise CronSyntaxError(FIELDS[idx], field_text)
    return v


def _field(text, idx):
    lo, hi = RANGES[idx]
    if idx == 4:
        hi = 6
    out = set()
    for part in text.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            if not s.isdigit() or int(s) == 0:
                raise CronSyntaxError(FIELDS[idx], text)
            step = int(s)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            x, y = part.split("-", 1)
            a, b = _num(x, idx, text), _num(y, idx, text)
            if a > b:
                raise CronSyntaxError(FIELDS[idx], text)
        else:
            a = _num(part, idx, text)
            b = RANGES[idx][1] if step != 1 else a
        out.update(range(a, b + 1, step))
    if idx == 4 and 7 in out:
        out.discard(7)
        out.add(0)
    return frozenset(out)


def parse(expr):
    parts = expr.split()
    if len(parts) != 5:
        raise CronSyntaxError("expression", expr)
    sets = [_field(p, i) for i, p in enumerate(parts)]
    return CronSchedule(*sets, not parts[2].startswith("*"), not parts[4].startswith("*"))


def _match(s, t):
    if t.minute not in s.minutes or t.hour not in s.hours or t.month not in s.months:
        return False
    dom = t.day in s.days_of_month
    dow = t.isoweekday() % 7 in s.days_of_week
    if s.dom_restricted and s.dow_restricted:
        return dom or dow
    return dom and dow


def matches(expr, dt):
    return _match(parse(expr), dt)


def next_times(expr, after, count):
    s = parse(expr)
    if count < 0 or after.tzinfo is not None:
        raise ValueError("bad arguments")
    out = []
    t = after.replace(second=0, microsecond=0)
    while len(out) < count:
        start = t
        limit = start + timedelta(days=4 * 366)
        t = t + timedelta(minutes=1)
        while not _match(s, t):
            t += timedelta(minutes=1)
            if t > limit:
                raise NoFireTimeError(after if not out else out[-1])
        out.append(t)
    return out
