import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta

FIELDS = ("minute", "hour", "day_of_month", "month", "day_of_week")
_LIMITS = {"minute": (0, 59), "hour": (0, 23), "day_of_month": (1, 31), "month": (1, 12), "day_of_week": (0, 7)}
_ALIASES = {
    "month": {m: i for i, m in enumerate(calendar.month_abbr) if m},
    "day_of_week": {"Sun": 0, "Mon": 1, "Tue": 2, "Wed": 3, "Thu": 4, "Fri": 5, "Sat": 6},
}


class CronSyntaxError(ValueError):
    def __init__(self, field, text):
        super().__init__(f"bad {field} {text!r}")
        self.field, self.text = field, text


class NoFireTimeError(Exception):
    def __init__(self, after):
        super().__init__(f"never fires after {after}")
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


def _atom(field, token, item):
    aliases = _ALIASES.get(field, {})
    if token.isascii() and token.isdigit():
        value = int(token)
    elif token.title() in aliases:
        value = aliases[token.title()]
    else:
        raise CronSyntaxError(field, item)
    lo, hi = _LIMITS[field]
    if not lo <= value <= hi:
        raise CronSyntaxError(field, item)
    return value


def _expand(field, text):
    result = set()
    for item in text.split(","):
        if not item:
            continue
        body, _, step = item.partition("/")
        if _:
            if not (step.isascii() and step.isdigit()) or int(step) < 1:
                raise CronSyntaxError(field, item)
            n = int(step)
        else:
            n = 1
        if body == "*":
            lo, hi = _LIMITS[field]
            if field == "day_of_week":
                hi = 6
        elif "-" in body:
            a, _, b = body.partition("-")
            lo, hi = _atom(field, a, item), _atom(field, b, item)
            if lo > hi:
                raise CronSyntaxError(field, item)
        else:
            lo = hi = _atom(field, body, item)
        result.update(range(lo, hi + 1, n))
    if field == "day_of_week":
        result = {0 if v == 7 else v for v in result}
    return frozenset(result)


def parse(expr):
    fields = expr.split()
    if len(fields) != 5:
        raise CronSyntaxError("expression", expr)
    s = {name: _expand(name, text) for name, text in zip(FIELDS, fields)}
    return CronSchedule(s["minute"], s["hour"], s["day_of_month"], s["month"], s["day_of_week"],
                        fields[2] != "*", fields[4] != "*")


def _day_ok(s, d):
    return (d.month in s.months and d.day in s.days_of_month
            and (d.weekday() + 1) % 7 in s.days_of_week)


def matches(expr, dt):
    s = parse(expr)
    return _day_ok(s, dt) and dt.hour in s.hours and dt.minute in s.minutes


def _search(s, after):
    t = after.replace(second=0, microsecond=0)
    if t >= datetime(9999, 12, 31, 23, 59):
        raise NoFireTimeError(after)
    t += timedelta(minutes=1)
    d = t.date()
    end = date(min(t.year + 8, 9999), 12, 31)
    first = True
    while d <= end:
        if _day_ok(s, d):
            for h in sorted(s.hours):
                for m in sorted(s.minutes):
                    if not first or (h, m) >= (t.hour, t.minute):
                        return datetime(d.year, d.month, d.day, h, m)
        first = False
        if d == end:
            break
        d += timedelta(days=1)
    raise NoFireTimeError(after)


def next_times(expr, after, count):
    s = parse(expr)
    if count < 0:
        raise ValueError("negative count")
    if after.tzinfo is not None:
        raise ValueError("aware datetime")
    out, cur = [], after
    for _ in range(count):
        cur = _search(s, cur)
        out.append(cur)
    return out
