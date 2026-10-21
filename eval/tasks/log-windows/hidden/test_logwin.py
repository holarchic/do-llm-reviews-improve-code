import unittest
from datetime import datetime, timezone

import logwin

UTC = timezone.utc


def at(*args):
    return datetime(*args, tzinfo=UTC)


class ParseTest(unittest.TestCase):
    def test_basic_line(self):  # clauses 1-4
        event = logwin.parse_line("2026-03-01T10:00:05Z ERROR [api] request timed out\n")
        self.assertEqual(event, logwin.Event(at(2026, 3, 1, 10, 0, 5), "ERROR", "api", "request timed out"))

    def test_offset_converted_to_utc(self):  # clause 1
        event = logwin.parse_line("2026-03-01T10:00:05+02:00 INFO [api] ok")
        self.assertEqual(event.timestamp, at(2026, 3, 1, 8, 0, 5))
        self.assertEqual(event.timestamp.utcoffset().total_seconds(), 0)

    def test_negative_offset_crosses_midnight(self):  # clause 1
        event = logwin.parse_line("2026-03-01T22:30:00-05:00 INFO [api] late")
        self.assertEqual(event.timestamp, at(2026, 3, 2, 3, 30))

    def test_fractional_seconds(self):  # clause 1
        event = logwin.parse_line("2026-03-01T10:00:05.250Z INFO [api] x")
        self.assertEqual(event.timestamp, at(2026, 3, 1, 10, 0, 5, 250000))

    def test_missing_offset_is_invalid(self):  # clause 1
        self.assertIsNone(logwin.parse_line("2026-03-01T10:00:05 INFO [api] x"))

    def test_level_case_and_warning(self):  # clause 2
        self.assertEqual(logwin.parse_line("2026-03-01T10:00:05Z warning [api] x").level, "WARN")
        self.assertEqual(logwin.parse_line("2026-03-01T10:00:05Z Error [api] x").level, "ERROR")
        self.assertEqual(logwin.parse_line("2026-03-01T10:00:05Z debug [api] x").level, "DEBUG")

    def test_unknown_level_is_invalid(self):  # clause 2
        self.assertIsNone(logwin.parse_line("2026-03-01T10:00:05Z FATAL [api] x"))

    def test_service_charset(self):  # clause 3
        self.assertEqual(logwin.parse_line("2026-03-01T10:00:05Z INFO [auth-svc_2] x").service, "auth-svc_2")
        self.assertIsNone(logwin.parse_line("2026-03-01T10:00:05Z INFO [auth svc] x"))
        self.assertIsNone(logwin.parse_line("2026-03-01T10:00:05Z INFO api x"))

    def test_message_keeps_brackets_and_spaces(self):  # clause 4
        event = logwin.parse_line("2026-03-01T10:00:05Z INFO  [api]   user [42]  said  hi   \n")
        self.assertEqual(event.message, "user [42]  said  hi")

    def test_empty_message(self):  # clause 4
        self.assertEqual(logwin.parse_line("2026-03-01T10:00:05Z INFO [api]").message, "")

    def test_invalid_lines_return_none(self):  # clause 5
        for line in ("", "   \n", "garbage", "2026-13-45T99:00:00Z INFO [api] x", "INFO [api] x"):
            with self.subTest(line=line):
                self.assertIsNone(logwin.parse_line(line))


class WindowTest(unittest.TestCase):
    def test_rejects_bad_width(self):  # clause 6
        for width in (0, -60):
            with self.subTest(width=width), self.assertRaises(ValueError):
                logwin.window_counts([], width)

    def test_counts_and_order(self):  # clause 8
        lines = [
            "2026-03-01T10:00:59Z ERROR [web] b",
            "2026-03-01T10:00:01Z INFO [api] a",
            "2026-03-01T10:00:30Z ERROR [api] c",
            "2026-03-01T10:01:00Z ERROR [api] d",
            "2026-03-01T10:00:10Z ERROR [api] e",
        ]
        self.assertEqual(logwin.window_counts(lines, 60), [
            logwin.WindowCount(at(2026, 3, 1, 10, 0), "api", "ERROR", 2),
            logwin.WindowCount(at(2026, 3, 1, 10, 0), "api", "INFO", 1),
            logwin.WindowCount(at(2026, 3, 1, 10, 0), "web", "ERROR", 1),
            logwin.WindowCount(at(2026, 3, 1, 10, 1), "api", "ERROR", 1),
        ])

    def test_offsets_land_in_same_utc_window(self):  # clauses 1, 7
        lines = ["2026-03-01T15:30:00+05:30 INFO [api] a", "2026-03-01T10:00:00Z INFO [api] b"]
        self.assertEqual(logwin.window_counts(lines, 3600), [logwin.WindowCount(at(2026, 3, 1, 10), "api", "INFO", 2)])

    def test_epoch_alignment(self):  # clause 7
        # 7-minute windows do not divide an hour, so wall-clock alignment differs from epoch alignment.
        [count] = logwin.window_counts(["2026-03-01T10:00:00Z INFO [api] a"], 420)
        self.assertEqual(count.start, at(2026, 3, 1, 9, 54))

    def test_fraction_stays_in_window(self):  # clause 7
        [count] = logwin.window_counts(["2026-03-01T10:00:59.999Z INFO [api] a"], 60)
        self.assertEqual(count.start, at(2026, 3, 1, 10, 0))

    def test_before_epoch_floors_down(self):  # clause 7
        [count] = logwin.window_counts(["1969-12-31T23:59:30Z INFO [api] a"], 60)
        self.assertEqual(count.start, at(1969, 12, 31, 23, 59))

    def test_skips_invalid_lines(self):  # clause 8
        lines = ["junk", "2026-03-01T10:00:00Z INFO [api] a", "", "2026-03-01T10:00:00 INFO [api] no offset"]
        self.assertEqual(len(logwin.window_counts(lines, 60)), 1)

    def test_accepts_any_iterable(self):  # interface
        lines = (line for line in ["2026-03-01T10:00:00Z INFO [api] a\n"])
        self.assertEqual(logwin.window_counts(lines, 60)[0].count, 1)

    def test_starts_are_utc(self):  # interface, clause 7
        [count] = logwin.window_counts(["2026-03-01T10:00:00+01:00 INFO [api] a"], 60)
        self.assertEqual(count.start.utcoffset().total_seconds(), 0)
