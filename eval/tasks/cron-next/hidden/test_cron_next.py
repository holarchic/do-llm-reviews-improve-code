import unittest
from datetime import datetime, timedelta, timezone

from cron_next import CronSyntaxError, NoFireTimeError, matches, next_times, parse


def dt(*args):
    return datetime(*args)


class SyntaxErrorAssertions(unittest.TestCase):
    def assert_syntax_error(self, expr, field, text):
        with self.assertRaises(CronSyntaxError) as cm:
            parse(expr)
        self.assertEqual(cm.exception.field, field)
        self.assertEqual(cm.exception.text, text)


class FieldCountTest(SyntaxErrorAssertions):  # rule 1

    def test_six_fields(self):
        self.assert_syntax_error("0 0 * * * 2024", "expression", "0 0 * * * 2024")

    def test_empty_and_blank(self):
        self.assert_syntax_error("", "expression", "")
        self.assert_syntax_error("   ", "expression", "   ")

    def test_text_is_expr_as_passed(self):
        self.assert_syntax_error("  0 0 *  * ", "expression", "  0 0 *  * ")

    def test_surrounding_and_tab_whitespace_accepted(self):
        schedule = parse("  5\t4  *\t* *\n")
        self.assertEqual(schedule.minutes, {5})
        self.assertEqual(schedule.hours, {4})


class BasicParseTest(unittest.TestCase):  # rules 4, 5, 10
    def test_star_fields(self):
        schedule = parse("* * * * *")
        self.assertEqual(schedule.minutes, set(range(60)))
        self.assertEqual(schedule.hours, set(range(24)))
        self.assertEqual(schedule.days_of_month, set(range(1, 32)))
        self.assertEqual(schedule.months, set(range(1, 13)))
        self.assertEqual(schedule.days_of_week, set(range(7)))


    def test_leading_zeros(self):
        schedule = parse("05 007 09 * *")
        self.assertEqual(schedule.minutes, {5})
        self.assertEqual(schedule.hours, {7})
        self.assertEqual(schedule.days_of_month, {9})


    def test_mixed_list(self):
        self.assertEqual(parse("1-3,10-20/5,59 * * * *").minutes, {1, 2, 3, 10, 15, 20, 59})


class StepTest(unittest.TestCase):  # rule 7

    def test_star_step_starts_at_field_lowest(self):
        schedule = parse("* * */10 */5 *")
        self.assertEqual(schedule.days_of_month, {1, 11, 21, 31})
        self.assertEqual(schedule.months, {1, 6, 11})

    def test_star_step_day_of_week(self):
        self.assertEqual(parse("* * * * */2").days_of_week, {0, 2, 4, 6})

    def test_range_step_includes_end_when_reached(self):
        self.assertEqual(parse("10-50/20 * * * *").minutes, {10, 30, 50})
        self.assertEqual(parse("0 1-23/11 * * *").hours, {1, 12, 23})

    def test_range_step_not_reaching_end(self):
        self.assertEqual(parse("10-12/5 * * * *").minutes, {10})
        self.assertEqual(parse("0-10/4 * * * *").minutes, {0, 4, 8})

    def test_single_value_step_runs_to_field_max(self):
        self.assertEqual(parse("5/20 * * * *").minutes, {5, 25, 45})
        self.assertEqual(parse("* * 20/5 * *").days_of_month, {20, 25, 30})

    def test_single_value_step_day_of_week_reaches_seven(self):
        self.assertEqual(parse("* * * * 5/1").days_of_week, {5, 6, 0})

    def test_step_larger_than_span(self):
        self.assertEqual(parse("*/90 * * * *").minutes, {0})
        self.assertEqual(parse("* */24 * * *").hours, {0})

    def test_single_value_step_next_times(self):
        self.assertEqual(next_times("50/5 * * * *", dt(2024, 1, 1, 10, 56), 2),
                         [dt(2024, 1, 1, 11, 50), dt(2024, 1, 1, 11, 55)])

    def test_name_range_step(self):
        self.assertEqual(parse("* * * JAN-DEC/3 *").months, {1, 4, 7, 10})


class NameTest(SyntaxErrorAssertions):  # rule 6
    def test_month_names_any_case(self):
        self.assertEqual(parse("0 0 1 JAN,mar,Dec *").months, {1, 3, 12})

    def test_day_names_any_case(self):
        self.assertEqual(parse("0 0 * * sun,Wed,SAT").days_of_week, {0, 3, 6})


    def test_names_mixed_with_numbers(self):
        self.assertEqual(parse("0 0 * * 2-FRI").days_of_week, {2, 3, 4, 5})
        self.assertEqual(parse("0 0 1 jan-3 *").months, {1, 2, 3})

    def test_full_names_rejected(self):
        self.assert_syntax_error("0 0 * * MONDAY", "day_of_week", "MONDAY")
        self.assert_syntax_error("0 0 1 JANUARY *", "month", "JANUARY")

    def test_name_in_wrong_field(self):
        self.assert_syntax_error("0 0 1 MON *", "month", "MON")
        self.assert_syntax_error("0 0 * * JAN", "day_of_week", "JAN")

    def test_name_in_numeric_field(self):
        self.assert_syntax_error("JAN 0 * * *", "minute", "JAN")
        self.assert_syntax_error("0 0 MON * *", "day_of_month", "MON")


class SundaySevenTest(SyntaxErrorAssertions):  # rules 8, 9
    def test_seven_is_sunday(self):
        self.assertEqual(parse("0 0 * * 7").days_of_week, {0})

    def test_range_to_seven(self):
        self.assertEqual(parse("0 0 * * 5-7").days_of_week, {5, 6, 0})
        self.assertEqual(parse("0 0 * * FRI-7").days_of_week, {5, 6, 0})

    def test_zero_to_seven(self):
        self.assertEqual(parse("0 0 * * 0-7").days_of_week, set(range(7)))

    def test_sun_to_seven(self):
        self.assertEqual(parse("0 0 * * SUN-7").days_of_week, set(range(7)))

    def test_fri_sun_reversed(self):
        self.assert_syntax_error("0 0 * * FRI-SUN", "day_of_week", "FRI-SUN")

    def test_seven_matches_sunday(self):
        self.assertTrue(matches("0 0 * * 7", dt(2024, 1, 7, 0, 0)))
        self.assertFalse(matches("0 0 * * 7", dt(2024, 1, 6, 0, 0)))


class RangeErrorTest(SyntaxErrorAssertions):  # rules 2, 8
    def test_numeric_field_bounds(self):
        self.assert_syntax_error("60 0 * * *", "minute", "60")
        self.assert_syntax_error("0 24 * * *", "hour", "24")
        self.assert_syntax_error("0 0 0 * *", "day_of_month", "0")
        self.assert_syntax_error("0 0 32 * *", "day_of_month", "32")

    def test_month_and_weekday_bounds(self):
        self.assert_syntax_error("0 0 1 0 *", "month", "0")
        self.assert_syntax_error("0 0 1 13 *", "month", "13")
        self.assert_syntax_error("0 0 * * 8", "day_of_week", "8")

    def test_range_endpoint_out_of_bounds(self):
        self.assert_syntax_error("50-60 * * * *", "minute", "50-60")

    def test_reversed_range(self):
        self.assert_syntax_error("0 10-5 * * *", "hour", "10-5")
        self.assert_syntax_error("0 0 1 DEC-JAN *", "month", "DEC-JAN")


class MalformedItemTest(SyntaxErrorAssertions):  # rules 3, 4, 5, 7
    def test_empty_middle_item(self):
        self.assert_syntax_error("1,,2 * * * *", "minute", "")

    def test_trailing_comma(self):
        self.assert_syntax_error("0 5, * * *", "hour", "")

    def test_leading_comma(self):
        self.assert_syntax_error("0 0 ,5 * *", "day_of_month", "")

    def test_step_zero(self):
        self.assert_syntax_error("*/0 * * * *", "minute", "*/0")
        self.assert_syntax_error("0 1-10/0 * * *", "hour", "1-10/0")

    def test_bad_steps(self):
        self.assert_syntax_error("*/ * * * *", "minute", "*/")
        self.assert_syntax_error("*/x * * * *", "minute", "*/x")
        self.assert_syntax_error("*/-1 * * * *", "minute", "*/-1")
        self.assert_syntax_error("*/2/3 * * * *", "minute", "*/2/3")
        self.assert_syntax_error("*/\u0665 * * * *", "minute", "*/\u0665")

    def test_bad_ranges(self):
        self.assert_syntax_error("*-5 * * * *", "minute", "*-5")
        self.assert_syntax_error("-5 * * * *", "minute", "-5")
        self.assert_syntax_error("5- * * * *", "minute", "5-")
        self.assert_syntax_error("1-2-3 * * * *", "minute", "1-2-3")

    def test_bad_numbers(self):
        self.assert_syntax_error("+5 * * * *", "minute", "+5")
        self.assert_syntax_error("5.0 * * * *", "minute", "5.0")
        self.assert_syntax_error("\u0665 * * * *", "minute", "\u0665")


class ErrorReportTest(SyntaxErrorAssertions):  # rule 11
    def test_text_is_whole_item_with_step(self):
        self.assert_syntax_error("0,1-70/5 * * * *", "minute", "1-70/5")
        self.assert_syntax_error("0 0 1-40/2 * *", "day_of_month", "1-40/2")

    def test_text_keeps_case(self):
        self.assert_syntax_error("0 0 1 Janu *", "month", "Janu")
        self.assert_syntax_error("0 0 * * mon-Fry", "day_of_week", "mon-Fry")

    def test_leftmost_field_reported(self):
        self.assert_syntax_error("70 25 * * *", "minute", "70")
        self.assert_syntax_error("0 0 0 13 8", "day_of_month", "0")

    def test_leftmost_item_reported(self):
        self.assert_syntax_error("0,5,99,100 * * * *", "minute", "99")
        self.assert_syntax_error("0 0 * * MON,xx,8", "day_of_week", "xx")


class RestrictedTest(unittest.TestCase):  # rule 12
    def test_star_unrestricted(self):
        schedule = parse("0 0 * * *")
        self.assertFalse(schedule.dom_restricted)
        self.assertFalse(schedule.dow_restricted)

    def test_star_step_restricted(self):
        schedule = parse("0 0 */1 * */1")
        self.assertTrue(schedule.dom_restricted)
        self.assertTrue(schedule.dow_restricted)

    def test_full_ranges_restricted(self):
        schedule = parse("0 0 1-31 * 0-7")
        self.assertTrue(schedule.dom_restricted)
        self.assertTrue(schedule.dow_restricted)

    def test_star_in_list_restricted(self):
        schedule = parse("0 0 *,5 * *")
        self.assertTrue(schedule.dom_restricted)
        self.assertFalse(schedule.dow_restricted)


class DayRuleTest(unittest.TestCase):  # rules 13, 14
    def test_or_rule_both_restricted(self):
        expr = "0 0 13 * FRI"
        self.assertTrue(matches(expr, dt(2024, 9, 13, 0, 0)))   # Friday 13th
        self.assertTrue(matches(expr, dt(2024, 9, 6, 0, 0)))    # a Friday
        self.assertTrue(matches(expr, dt(2024, 8, 13, 0, 0)))   # a Tuesday 13th
        self.assertFalse(matches(expr, dt(2024, 9, 12, 0, 0)))  # Thursday 12th

    def test_dom_only(self):
        self.assertTrue(matches("0 0 13 * *", dt(2024, 8, 13, 0, 0)))
        self.assertFalse(matches("0 0 13 * *", dt(2024, 9, 6, 0, 0)))

    def test_dow_only(self):
        self.assertTrue(matches("0 0 * * FRI", dt(2024, 9, 6, 0, 0)))
        self.assertFalse(matches("0 0 * * FRI", dt(2024, 9, 12, 0, 0)))

    def test_star_step_dom_with_dow_fires_daily(self):
        self.assertTrue(matches("0 0 */1 * MON", dt(2024, 9, 12, 0, 0)))
        self.assertTrue(matches("0 0 1-31 * 1", dt(2024, 9, 14, 0, 0)))

    def test_star_in_list_dom_uses_or_rule(self):
        self.assertTrue(matches("0 0 *,5 * MON", dt(2024, 9, 12, 0, 0)))

    def test_month_restricts_or_rule(self):
        self.assertTrue(matches("0 0 1 6 MON", dt(2024, 6, 3, 0, 0)))
        self.assertFalse(matches("0 0 1 6 MON", dt(2024, 5, 6, 0, 0)))

    def test_or_rule_next_times(self):
        times = next_times("0 0 13 * FRI", dt(2024, 9, 1), 4)
        self.assertEqual(times, [dt(2024, 9, 6), dt(2024, 9, 13), dt(2024, 9, 20), dt(2024, 9, 27)])

    def test_weekday_numbering(self):
        self.assertTrue(matches("0 0 * * 0", dt(2024, 1, 7, 0, 0)))   # Sunday
        self.assertTrue(matches("0 0 * * 1", dt(2024, 1, 1, 0, 0)))   # Monday
        self.assertTrue(matches("0 0 * * 6", dt(2024, 1, 6, 0, 0)))   # Saturday
        self.assertFalse(matches("0 0 * * 1", dt(2024, 1, 7, 0, 0)))


class MatchesTest(unittest.TestCase):  # rule 15
    def test_seconds_ignored(self):
        self.assertTrue(matches("30 10 * * *", dt(2024, 1, 1, 10, 30, 45, 123)))

    def test_each_field_checked(self):
        expr = "30 10 15 6 *"
        self.assertTrue(matches(expr, dt(2024, 6, 15, 10, 30)))
        self.assertFalse(matches(expr, dt(2024, 6, 15, 10, 31)))
        self.assertFalse(matches(expr, dt(2024, 6, 15, 11, 30)))
        self.assertFalse(matches(expr, dt(2024, 6, 16, 10, 30)))
        self.assertFalse(matches(expr, dt(2024, 7, 15, 10, 30)))

    def test_invalid_expression(self):
        with self.assertRaises(CronSyntaxError):
            matches("61 * * * *", dt(2024, 1, 1))


class CalendarTest(unittest.TestCase):  # rule 16
    def test_31st_skips_short_months(self):
        times = next_times("0 0 31 * *", dt(2024, 1, 1), 4)
        self.assertEqual(times, [dt(2024, 1, 31), dt(2024, 3, 31), dt(2024, 5, 31), dt(2024, 7, 31)])

    def test_feb_29_leap_years(self):
        self.assertEqual(next_times("0 0 29 2 *", dt(2024, 3, 1), 2), [dt(2028, 2, 29), dt(2032, 2, 29)])

    def test_feb_29_skips_2100(self):
        self.assertEqual(next_times("0 0 29 2 *", dt(2096, 3, 1), 1), [dt(2104, 2, 29)])

    def test_feb_29_in_2000(self):
        self.assertEqual(next_times("0 0 29 2 *", dt(1999, 3, 1), 1), [dt(2000, 2, 29)])

    def test_30th_with_or_rule_fires_on_weekday(self):
        self.assertEqual(next_times("0 0 30 2 MON", dt(2026, 2, 1), 2), [dt(2026, 2, 2), dt(2026, 2, 9)])

    def test_matches_nonexistent_never(self):
        self.assertFalse(matches("0 0 29 2 *", dt(2023, 3, 1)))
        self.assertTrue(matches("0 0 29 2 *", dt(2024, 2, 29)))


class NextTimesTest(unittest.TestCase):  # rule 17
    def test_strictly_after_whole_minute(self):
        self.assertEqual(next_times("* * * * *", dt(2024, 1, 1, 10, 0), 1), [dt(2024, 1, 1, 10, 1)])

    def test_after_with_seconds(self):
        self.assertEqual(next_times("* * * * *", dt(2024, 1, 1, 10, 0, 30), 1), [dt(2024, 1, 1, 10, 1)])
        self.assertEqual(next_times("* * * * *", dt(2024, 1, 1, 10, 0, 0, 1), 1), [dt(2024, 1, 1, 10, 1)])

    def test_matching_after_excluded(self):
        self.assertEqual(next_times("30 10 * * *", dt(2024, 1, 1, 10, 30), 1), [dt(2024, 1, 2, 10, 30)])

    def test_same_day_later_minute(self):
        self.assertEqual(next_times("30 10 * * *", dt(2024, 1, 1, 10, 29, 59), 1), [dt(2024, 1, 1, 10, 30)])

    def test_sequence(self):
        times = next_times("*/15 * * * *", dt(2024, 1, 1, 10, 7), 5)
        expected = [dt(2024, 1, 1, 10, 15), dt(2024, 1, 1, 10, 30), dt(2024, 1, 1, 10, 45),
                    dt(2024, 1, 1, 11, 0), dt(2024, 1, 1, 11, 15)]
        self.assertEqual(times, expected)

    def test_results_are_whole_naive_minutes(self):
        for t in next_times("*/7 * * * *", dt(2024, 1, 1, 10, 0, 31, 99), 3):
            self.assertEqual((t.second, t.microsecond), (0, 0))
            self.assertIsNone(t.tzinfo)

    def test_year_rollover(self):
        self.assertEqual(next_times("0 0 1 1 *", dt(2024, 12, 31, 23, 59), 1), [dt(2025, 1, 1)])

    def test_hour_and_day_carry(self):
        self.assertEqual(next_times("0 */6 * * *", dt(2024, 2, 29, 23, 0), 2), [dt(2024, 3, 1, 0, 0), dt(2024, 3, 1, 6, 0)])

    def test_earlier_minute_next_hour(self):
        self.assertEqual(next_times("5,50 * * * *", dt(2024, 1, 1, 10, 51), 2),
                         [dt(2024, 1, 1, 11, 5), dt(2024, 1, 1, 11, 50)])

    def test_duplicate_items_no_duplicate_times(self):
        self.assertEqual(next_times("0,0,0 * * * *", dt(2024, 1, 1, 10, 0), 2),
                         [dt(2024, 1, 1, 11, 0), dt(2024, 1, 1, 12, 0)])


class ArgumentCheckTest(unittest.TestCase):  # rule 18
    def test_count_zero_never_firing(self):
        self.assertEqual(next_times("0 0 30 2 *", dt(2024, 1, 1), 0), [])

    def test_count_zero_still_validates(self):
        with self.assertRaises(CronSyntaxError):
            next_times("0 0 32 * *", dt(2024, 1, 1), 0)

    def test_negative_count(self):
        with self.assertRaises(ValueError):
            next_times("* * * * *", dt(2024, 1, 1), -1)

    def test_aware_after(self):
        with self.assertRaises(ValueError):
            next_times("* * * * *", datetime(2024, 1, 1, tzinfo=timezone.utc), 1)

    def test_syntax_error_precedes_count_error(self):
        with self.assertRaises(CronSyntaxError):
            next_times("* * * *", dt(2024, 1, 1), -1)

    def test_syntax_error_precedes_tz_error(self):
        with self.assertRaises(CronSyntaxError):
            next_times("* * * * 9", datetime(2024, 1, 1, tzinfo=timezone(timedelta(hours=2))), 1)


class NoFireTimeTest(unittest.TestCase):  # rule 19
    def test_never_firing_expressions(self):
        with self.assertRaises(NoFireTimeError):
            next_times("0 0 30 2 *", dt(2024, 1, 1), 1)
        with self.assertRaises(NoFireTimeError):
            next_times("0 0 31 4,6,9,11 *", dt(2024, 1, 1), 1)


    def test_error_after_is_argument_as_passed(self):
        start = dt(2024, 1, 1, 10, 0, 30, 5)
        with self.assertRaises(NoFireTimeError) as cm:
            next_times("0 0 30 2 *", start, 3)
        self.assertEqual(cm.exception.after, start)


class CalendarEndTest(unittest.TestCase):  # rules 19, 20
    def test_last_minute_fires(self):
        self.assertEqual(next_times("* * * * *", dt(9999, 12, 31, 23, 58), 1), [dt(9999, 12, 31, 23, 59)])

    def test_past_last_minute(self):
        with self.assertRaises(NoFireTimeError) as cm:
            next_times("* * * * *", dt(9999, 12, 31, 23, 58), 2)
        self.assertEqual(cm.exception.after, dt(9999, 12, 31, 23, 59))

    def test_after_inside_last_minute(self):
        start = dt(9999, 12, 31, 23, 59, 30)
        with self.assertRaises(NoFireTimeError) as cm:
            next_times("* * * * *", start, 1)
        self.assertEqual(cm.exception.after, start)

    def test_after_is_previous_fire_time(self):
        with self.assertRaises(NoFireTimeError) as cm:
            next_times("0 0 1 1 *", dt(9998, 6, 1), 3)
        self.assertEqual(cm.exception.after, dt(9999, 1, 1))


class SpeedTest(unittest.TestCase):  # rule 21
    def test_thousand_minutes(self):
        times = next_times("* * * * *", dt(2024, 1, 1), 1000)
        self.assertEqual(len(times), 1000)
        self.assertEqual(times[-1], dt(2024, 1, 1) + timedelta(minutes=1000))

    def test_many_never_firing_expressions(self):
        exprs = ("0 0 30 2 *", "* * 31 4,6,9,11 *", "*/5 * 30,31 2 *", "0 0 31 2 *", "59 23 30 2 *",
                 "0 12 31 4,6 *", "30 * 30-31 2 *", "0 0 31 9,11 *")
        for expr in exprs:
            with self.assertRaises(NoFireTimeError):
                next_times(expr, dt(2024, 3, 1), 1)

    def test_rare_dates_many(self):
        times = next_times("0 0 29 2 *", dt(2000, 1, 1), 30)
        self.assertEqual(times[0], dt(2000, 2, 29))
        self.assertEqual(times[-1], dt(2120, 2, 29))
        self.assertTrue(all(t.year != 2100 for t in times))

    def test_friday_13th_thousand(self):
        times = next_times("0 0 13 * FRI", dt(2024, 1, 1), 1000)
        self.assertEqual(len(times), 1000)
        self.assertTrue(all(t.day == 13 or t.weekday() == 4 for t in times))
        self.assertTrue(all(a < b for a, b in zip(times, times[1:])))


if __name__ == "__main__":
    unittest.main()
