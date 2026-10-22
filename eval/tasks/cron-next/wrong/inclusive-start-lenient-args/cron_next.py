import re
from dataclasses import dataclass
from datetime import datetime, timedelta

FIELDS = ("minute", "hour", "day_of_month", "month", "day_of_week")
BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
NAMES = (
    {},
    {},
    {},
    {n: i + 1 for i, n in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())},
    {n: i for i, n in enumerate("SUN MON TUE WED THU FRI SAT".split())},
)
ITEM = re.compile(r"^(\*|\w+(?:-\w+)?)(?:/(\d+))?$")


class CronSyntaxError(ValueError):
    def __init__(self, field, text):
        super().__init__(field + ": " + text)
        self.field = field
        self.text = text


class NoFireTimeError(Exception):
    def __init__(self, after):
        super().__init__(f"no fire time after {after}")
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


def _value(i, tok, item):
    if tok.isdigit():
        v = int(tok)
    else:
        v = NAMES[i].get(tok.upper())
        if v is None:
            raise CronSyntaxError(FIELDS[i], item)
    lo, hi = BOUNDS[i]
    if not lo <= v <= hi:
        raise CronSyntaxError(FIELDS[i], item)
    return v


def _parse_field(i, text):
    values = set()
    for item in text.split(","):
        m = ITEM.match(item)
        if not m:
            raise CronSyntaxError(FIELDS[i], item)
        base, step = m.group(1), int(m.group(2) or 1)
        if step == 0:
            raise CronSyntaxError(FIELDS[i], item)
        lo, hi = BOUNDS[i]
        if base == "*":
            a, b = lo, (6 if i == 4 else hi)
        elif "-" in base:
            x, y = base.split("-")
            a, b = _value(i, x, item), _value(i, y, item)
            if a > b:
                raise CronSyntaxError(FIELDS[i], item)
        else:
            a = _value(i, base, item)
            b = hi if m.group(2) else a
        values.update(range(a, b + 1, step))
    if i == 4:
        values = {v % 7 for v in values}
    return frozenset(values)


def parse(expr):
    parts = expr.split()
    if len(parts) != 5:
        raise CronSyntaxError("expression", expr)
    sets = [_parse_field(i, p) for i, p in enumerate(parts)]
    return CronSchedule(*sets, dom_restricted=parts[2] != "*", dow_restricted=parts[4] != "*")


def _day(s, d):
    if d.month not in s.months:
        return False
    dom = d.day in s.days_of_month
    dow = (d.weekday() + 1) % 7 in s.days_of_week
    return (dom or dow) if (s.dom_restricted and s.dow_restricted) else (dom and dow)


def matches(expr, dt):
    s = parse(expr)
    return _day(s, dt) and dt.hour in s.hours and dt.minute in s.minutes


def _ceil_minute(t):
    whole = t.replace(second=0, microsecond=0)
    return whole if whole == t else whole + timedelta(minutes=1)


def _first_from(s, start):
    d = start.date()
    last = d.replace(year=min(d.year + 8, 9999), month=12, day=31)
    first = True
    while d <= last:
        if _day(s, d):
            for h in sorted(s.hours):
                for m in sorted(s.minutes):
                    if not first or (h, m) >= (start.hour, start.minute):
                        return datetime(d.year, d.month, d.day, h, m)
        first = False
        if d == last:
            break
        d += timedelta(days=1)
    return None


def next_times(expr, after, count):
    s = parse(expr)
    after = after.replace(tzinfo=None)
    out = []
    start = _ceil_minute(after)
    while len(out) < max(count, 0):
        found = _first_from(s, start)
        if found is None:
            raise NoFireTimeError(after)
        out.append(found)
        start = found + timedelta(minutes=1)
    return out
