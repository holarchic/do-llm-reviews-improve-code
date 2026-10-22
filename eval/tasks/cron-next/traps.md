# Traps (private)

| Trap | TASK.md rule | Hidden tests | Why a hurried build misses it |
|---|---|---|---|
| Field-count error text is `expr` exactly as passed | 1 | `FieldCountTest.test_empty_and_blank`, `test_text_is_expr_as_passed` | Builds report the stripped or split expression, or use `field=None`. |
| Empty items are errors with text `""` | 3, 11 | `MalformedItemTest.test_empty_middle_item`, `test_trailing_comma`, `test_leading_comma` | `split(",")` plus `if not item: continue` silently accepts them. |
| ASCII digits only | 5, 7 | `MalformedItemTest.test_bad_numbers`, `test_bad_steps` | `str.isdigit()` and `int()` accept `٥`; `int()` accepts `+5`. |
| `a/s` runs to the field max | 7 | `StepTest.test_single_value_step_runs_to_field_max`, `test_single_value_step_day_of_week_reaches_seven`, `test_single_value_step_next_times` | Treated as the single value `a`, or rejected. |
| `*/s` counts from the field's lowest value | 7 | `StepTest.test_star_step_starts_at_field_lowest` | `range(0, max, s)` for every field gives day_of_month {0,10,20,30}. |
| 7 folds to 0; `FRI-SUN` reversed; `SUN-7` is the whole week | 8, 9 | `SundaySevenTest.*` | 7 left in the set (Sunday via 7 never matches), or SUN mapped to 7. |
| Error text is the whole item, original case | 11 | `ErrorReportTest.*`, `RangeErrorTest.test_range_endpoint_out_of_bounds` | Reports the bad token (`70` instead of `1-70/5`), the whole field, or upper-cased text. |
| Restricted means "not exactly `*`" | 12 | `RestrictedTest.*`, `DayRuleTest.test_star_step_dom_with_dow_fires_daily`, `test_star_in_list_dom_uses_or_rule` | Vixie-style `startswith("*")`, or "allows every value" detection. |
| OR rule only when both restricted | 13 | `DayRuleTest.test_or_rule_*`, `test_dom_only`, `test_dow_only`, `test_month_restricts_or_rule`, `CalendarTest.test_30th_with_or_rule_fires_on_weekday` | Always AND, or OR whenever either is restricted (then every day matches). |
| Sunday is 0, not Python's Monday 0 | 14 | `DayRuleTest.test_weekday_numbering`, OR-rule tests | `dt.weekday()` used directly. |
| Strictly after; seconds roll forward | 17 | `NextTimesTest.test_strictly_after_whole_minute`, `test_after_with_seconds`, `test_matching_after_excluded`, `SpeedTest.test_thousand_minutes` | Ceiling to the minute includes `after` itself when it is a whole minute. |
| Argument checks and their precedence | 18 | `ArgumentCheckTest.*` | `count=0` still searches (raises on never-firing), negative count returns `[]`, tzinfo stripped, checks done before parsing. |
| `NoFireTimeError.after` is the search start, not always the argument | 19 | `NoFireTimeTest.test_error_after_is_argument_as_passed`, `CalendarEndTest.test_after_is_previous_fire_time`, `test_past_last_minute` | Always reports the original `after`, or a minute-truncated one. |
| End of calendar at 9999-12-31 23:59 | 20 | `CalendarEndTest.*` | `after + timedelta(minutes=1)` raises `OverflowError`. |
| Horizon at least 8 years, and fast | 16, 21 | `CalendarTest.test_feb_29_skips_2100`, `SpeedTest.*` | A 4- or 5-year horizon misses 2104-02-29; minute-by-minute search times out on never-firing expressions. |

Other assertion groups: whitespace splitting (1); `*`, lists, leading zeros (4, 5, 10); name case and placement (6); bounds and reversed ranges (2, 8); `matches` field checks and seconds ignored (15); calendar lengths and Gregorian leap years (16); sequences, carries and duplicates (17).

Wrong variants: `minute-walk` (minute stepping, 4-year horizon, no calendar-end guard, Vixie restriction, field text in errors, `isdigit`); `and-day-rule` (always AND, `a/s` as single value, empty items skipped); `inclusive-start-lenient-args` (includes a whole-minute `after`, tzinfo stripped, negative count gives `[]`, no calendar-end guard, regex `\w` and `isdigit` accept non-ASCII digits).
