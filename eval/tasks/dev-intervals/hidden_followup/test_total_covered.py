import unittest

import intervals


class TotalCoveredTest(unittest.TestCase):
    def test_overlap_counted_once(self):
        self.assertEqual(intervals.total_covered([(1, 5), (3, 8)]), 8)

    def test_single_point_counts_one(self):
        self.assertEqual(intervals.total_covered([(4, 4)]), 1)

    def test_empty(self):
        self.assertEqual(intervals.total_covered([]), 0)

    def test_reversed_span_raises(self):
        with self.assertRaises(ValueError):
            intervals.total_covered([(3, 1)])
