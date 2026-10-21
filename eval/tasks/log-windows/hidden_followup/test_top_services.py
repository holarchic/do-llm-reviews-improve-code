import unittest

import logwin

LINES = [
    "2026-03-01T10:00:00Z ERROR [web] a",
    "2026-03-01T10:00:00Z ERROR [api] a",
    "2026-03-01T11:00:00+01:00 ERROR [api] b",
    "2026-03-01T10:00:00Z WARNING [db] c",
    "2026-03-01T10:00:00Z warn [db] d",
    "2026-03-01T10:00:00Z ERROR [cache] e",
    "not a line",
]


class TopServicesTest(unittest.TestCase):
    def test_sorted_by_count_then_name(self):
        self.assertEqual(logwin.top_services(LINES, "ERROR", 3), [("api", 2), ("cache", 1), ("web", 1)])

    def test_n_limits(self):
        self.assertEqual(logwin.top_services(LINES, "ERROR", 1), [("api", 2)])

    def test_level_rules_apply(self):
        self.assertEqual(logwin.top_services(LINES, "warning", 5), [("db", 2)])

    def test_invalid_arguments(self):
        with self.assertRaises(ValueError):
            logwin.top_services(LINES, "FATAL", 3)
        with self.assertRaises(ValueError):
            logwin.top_services(LINES, "ERROR", 0)
