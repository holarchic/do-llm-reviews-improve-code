# Task: next cron fire times

Write a Python module `cron_next.py` at the repository root (standard library only). Tests import it as `import cron_next`.

The module evaluates classic 5-field cron expressions against naive `datetime` values. There are no time zones and no daylight-saving changes: a naive datetime is taken at face value.

## Interface

- `FIELDS = ("minute", "hour", "day_of_month", "month", "day_of_week")`
- `class CronSyntaxError(ValueError)`, constructed as `CronSyntaxError(field, text)`, with attributes `field: str` and `text: str`.
- `class NoFireTimeError(Exception)`, constructed as `NoFireTimeError(after)`, with attribute `after: datetime`.
- `class CronSchedule`: the parsed form of an expression, with attributes
  - `minutes`, `hours`, `days_of_month`, `months`, `days_of_week`: each a `frozenset[int]` of the values the field allows;
  - `dom_restricted: bool`, `dow_restricted: bool`.
- `parse(expr: str) -> CronSchedule`
- `matches(expr: str, dt: datetime) -> bool`
- `next_times(expr: str, after: datetime, count: int) -> list[datetime]`

## Behaviour

### Syntax

1. An expression has exactly five fields, in the order of `FIELDS`, separated by runs of whitespace (as `str.split()` with no arguments splits them). Leading and trailing whitespace is ignored. Any other number of fields, including an empty or all-whitespace expression, raises `CronSyntaxError` with `field` `"expression"` and `text` the `expr` argument exactly as passed.
2. Allowed values per field: minute 0–59, hour 0–23, day_of_month 1–31, month 1–12, day_of_week 0–7. A value outside its field's range is an error.
3. A field is a comma-separated list of one or more items. Every item must be non-empty: `1,,2`, `,5` and `5,` are errors, and the offending item is the empty string.
4. An item is one of:
   - `*`: every value of the field (for day_of_week, 0–6);
   - a single value `a`;
   - a range `a-b`: every value from `a` to `b` inclusive;
   - any of these three followed by `/s`, a step (rule 7).

   Anything else is an error, for example `*-5`, `-5`, `5-`, `1-2-3`, `*/2/3`.
5. A value is either a decimal number written with ASCII digits `0`–`9` only (leading zeros allowed: `05` is 5) or, in the month and day_of_week fields only, a name. `+5`, `5.0` and `٥` are errors.
6. Month names are `JAN`…`DEC` (1–12) and day-of-week names are `SUN`, `MON`, `TUE`, `WED`, `THU`, `FRI`, `SAT` (0–6). Names are exactly these three letters, matched case-insensitively (`jan`, `Jan` and `JAN` are all January); `MONDAY` and `JANUARY` are errors. A name is valid only in its own field: `MON` in the month field, or any name in the minute, hour or day_of_month fields, is an error. Names can be used as range endpoints, mixed freely with numbers: `MON-FRI`, `jan-3` and `2-FRI` are valid.
7. Steps. The step `s` is a decimal number of ASCII digits and must be at least 1; `*/0`, `*/`, `*/x` and `*/-1` are errors.
   - `*/s` takes every `s`-th value counting from the field's lowest value: minute `*/15` is {0, 15, 30, 45}; day_of_month `*/10` is {1, 11, 21, 31}; month `*/5` is {1, 6, 11}; day_of_week `*/2` is {0, 2, 4, 6}.
   - `a-b/s` takes `a, a+s, a+2s, …` up to and including `b` when reached: `10-50/20` is {10, 30, 50}; `10-12/5` is {10}.
   - `a/s` means `a-max/s`, where `max` is the field's highest value from rule 2: minute `5/20` is {5, 25, 45}; day_of_month `20/5` is {20, 25, 30}.
   - A step larger than the span is valid: minute `*/90` is {0}.
8. A range whose start is greater than its end is an error, compared after names are converted to numbers. `SUN` is 0, so `FRI-SUN` is an error; write `FRI-7` or `5-7` instead.
9. In day_of_week, 7 also means Sunday. In `CronSchedule.days_of_week` every 7 becomes 0, so the set only ever holds 0–6: `5-7` is {5, 6, 0}, `0-7` is {0, 1, 2, 3, 4, 5, 6}, and `7` is {0}.
10. Overlapping or repeated items are allowed and count once: minute `1-5,3-7` is {1, …, 7}.
11. A syntax error raises `CronSyntaxError` with `field` the name of the field (from `FIELDS`) and `text` the whole comma-separated item that is invalid, exactly as written in the expression (original letter case, including any range and step). For example, minute `0,1-70/5` gives `field` `"minute"` and `text` `"1-70/5"`; month `Janu` gives `text` `"Janu"`. When several items are invalid, the one reported is in the leftmost field, and within that field the leftmost item.

### Matching

12. A field is *unrestricted* when its text is exactly `*`. Any other text is *restricted*, even when it allows every value: `*/1`, `1-31`, `0-7` and `*,5` are all restricted. `CronSchedule.dom_restricted` and `dow_restricted` report this for day_of_month and day_of_week.
13. Day rule. When day_of_month and day_of_week are both restricted, a day matches if its day of the month is in `days_of_month` OR its weekday is in `days_of_week`. Otherwise a day matches only if both are in their sets (an unrestricted field allows every day, so in effect only the restricted one counts). The month must match in every case. Example: `0 0 13 * FRI` fires on every 13th and on every Friday; `0 0 13 * *` fires only on the 13th; `0 0 */1 * MON` fires every day.
14. Weekdays are numbered Sunday 0, Monday 1, …, Saturday 6.
15. `matches(expr, dt)` returns whether `dt` lies in a minute the expression fires in: its minute, hour, month and day (rule 13) all match. The seconds and microseconds of `dt` are ignored. `matches` and `next_times` raise `CronSyntaxError` for an invalid expression, exactly as `parse` does.
16. Only real calendar dates fire. A day_of_month value that a month does not have (30 February, 31 April, 29 February in a year that is not a leap year) never fires in that month. Leap years follow the Gregorian calendar: 2000 is a leap year, 2100 is not.

### Next fire times

17. Fire times are whole minutes (seconds and microseconds 0). `next_times(expr, after, count)` returns, in ascending order and without duplicates, the first `count` fire times that are strictly later than `after`. Each is a naive `datetime` with `second == 0` and `microsecond == 0`. For `* * * * *`: after `10:00:00` the first fire time is `10:01`; after `10:00:30` it is also `10:01`.
18. Argument checks. `count == 0` returns `[]` without searching (so even an expression that never fires returns `[]`). `count < 0` raises `ValueError`. An `after` with a `tzinfo` raises `ValueError`. The expression is parsed first, so a `CronSyntaxError` takes precedence over these errors.
19. No fire time. If no fire time exists strictly after the point a search starts from, `next_times` raises `NoFireTimeError` and returns nothing, not a partial list. The error's `after` is that starting point: the `after` argument (exactly as passed) when searching for the first fire time, otherwise the previous fire time found. Examples of expressions that never fire: `0 0 30 2 *`, `0 0 31 4,6,9,11 *`.
20. The calendar ends at 9999-12-31 23:59, the last fire time that can exist. A search that would need a later minute raises `NoFireTimeError` (rule 19); it never raises `OverflowError`. For example, `next_times("* * * * *", datetime(9999, 12, 31, 23, 58), 2)` raises `NoFireTimeError` with `after` `datetime(9999, 12, 31, 23, 59)`.
21. Speed. Any call with `count` up to 1000 must finish in well under one second, and so must detecting that an expression never fires. Checking one minute at a time is too slow. Every expression that fires at all fires at least once in any 8 consecutive calendar years (the rarest date is 29 February; 2096 is followed by 2104), so a search may stop once it has passed the end of the eighth year after the year it started in.

Include your own tests.
