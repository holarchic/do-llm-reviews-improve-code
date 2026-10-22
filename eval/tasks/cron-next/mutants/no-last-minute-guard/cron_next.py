import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

FIELDS = ("minute", "hour", "day_of_month", "month", "day_of_week")

# (lowest, highest) accepted value per field; "*" in day_of_week means 0-6.
_BOUNDS = {
    "minute": (0, 59),
    "hour": (0, 23),
    "day_of_month": (1, 31),
    "month": (1, 12),
    "day_of_week": (0, 7),
}
_STAR = {**_BOUNDS, "day_of_week": (0, 6)}
_NAMES = {
    "month": {name: i + 1 for i, name in enumerate(
        ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"))},
    "day_of_week": {name: i for i, name in enumerate(("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"))},
}
_DIGITS = re.compile(r"[0-9]+")
_LAST_MINUTE = datetime(9999, 12, 31, 23, 59)
# Feb 29 is the rarest date and recurs within any 8 consecutive years (2096 -> 2104).
_HORIZON_YEARS = 8


class CronSyntaxError(ValueError):
    def __init__(self, field, text):
        super().__init__(f"invalid {field}: {text!r}")
        self.field = field
        self.text = text


class NoFireTimeError(Exception):
    def __init__(self, after):
        super().__init__(f"no fire time after {after.isoformat()}")
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

    def day_matches(self, day):
        dom_ok = day.day in self.days_of_month
        dow_ok = (day.weekday() + 1) % 7 in self.days_of_week
        if self.dom_restricted and self.dow_restricted:
            return dom_ok or dow_ok
        return dom_ok and dow_ok

    def first_time_on_or_after(self, hour, minute):
        """Earliest (hour, minute) of a firing day at or after the given time, or None."""
        for h in sorted(self.hours):
            if h < hour:
                continue
            for m in sorted(self.minutes):
                if h > hour or m >= minute:
                    return h, m
        return None


def parse(expr):
    parts = expr.split()
    if len(parts) != len(FIELDS):
        raise CronSyntaxError("expression", expr)
    sets = {field: _parse_field(field, text) for field, text in zip(FIELDS, parts)}
    return CronSchedule(
        minutes=sets["minute"],
        hours=sets["hour"],
        days_of_month=sets["day_of_month"],
        months=sets["month"],
        days_of_week=sets["day_of_week"],
        dom_restricted=parts[2] != "*",
        dow_restricted=parts[4] != "*",
    )


def _parse_field(field, text):
    values = set()
    for item in text.split(","):
        values.update(_parse_item(field, item))
    if field == "day_of_week" and 7 in values:
        values.discard(7)
        values.add(0)
    return frozenset(values)


def _parse_item(field, item):
    base, slash, step_text = item.partition("/")
    if slash:
        if not _DIGITS.fullmatch(step_text) or int(step_text) == 0:
            raise CronSyntaxError(field, item)
        step = int(step_text)
    else:
        step = 1
    if base == "*":
        low, high = _STAR[field]
    elif "-" in base:
        start, _, end = base.partition("-")
        low, high = _value(field, start, item), _value(field, end, item)
        if low > high:
            raise CronSyntaxError(field, item)
    else:
        low = _value(field, base, item)
        high = _BOUNDS[field][1] if slash else low
    return range(low, high + 1, step)


def _value(field, text, item):
    if _DIGITS.fullmatch(text):
        value = int(text)
    else:
        # -1 is outside every field's range, so an unknown name fails the bounds check.
        value = _NAMES.get(field, {}).get(text.upper(), -1)
    low, high = _BOUNDS[field]
    if not low <= value <= high:
        raise CronSyntaxError(field, item)
    return value


def matches(expr, dt):
    schedule = parse(expr)
    return (dt.month in schedule.months and schedule.day_matches(dt.date())
            and dt.hour in schedule.hours and dt.minute in schedule.minutes)


def next_times(expr, after, count):
    schedule = parse(expr)
    if count < 0:
        raise ValueError("count must be at least 0")
    if after.tzinfo is not None:
        raise ValueError("after must be a naive datetime")
    times = []
    previous = after
    while len(times) < count:
        previous = _next_after(schedule, previous)
        times.append(previous)
    return times


def _next_after(schedule, after):
    whole = after.replace(second=0, microsecond=0)
    start = whole + timedelta(minutes=1)
    day, hour, minute = start.date(), start.hour, start.minute
    last_year = min(start.year + _HORIZON_YEARS, 9999)
    while day.year <= last_year:
        if day.month in schedule.months and schedule.day_matches(day):
            found = schedule.first_time_on_or_after(hour, minute)
            if found is not None:
                return datetime(day.year, day.month, day.day, *found)
        if day == date.max:
            break
        day += timedelta(days=1)
        hour = minute = 0
    raise NoFireTimeError(after)
