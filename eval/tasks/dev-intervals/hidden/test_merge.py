import unittest

import intervals


class MergeTest(unittest.TestCase):
    def test_overlap(self):
        self.assertEqual(intervals.merge([(1, 5), (3, 8)]), [(1, 8)])

    def test_disjoint_stay_apart(self):
        self.assertEqual(intervals.merge([(1, 2), (5, 6)]), [(1, 2), (5, 6)])

    def test_adjacent_merge(self):
        self.assertEqual(intervals.merge([(1, 2), (3, 4)]), [(1, 4)])

    def test_unsorted_input(self):
        self.assertEqual(intervals.merge([(10, 12), (1, 3), (2, 4)]), [(1, 4), (10, 12)])

    def test_nested(self):
        self.assertEqual(intervals.merge([(1, 10), (3, 4)]), [(1, 10)])

    def test_input_not_mutated(self):
        spans = [(5, 6), (1, 2)]
        intervals.merge(spans)
        self.assertEqual(spans, [(5, 6), (1, 2)])

    def test_empty(self):
        self.assertEqual(intervals.merge([]), [])

    def test_reversed_span_raises(self):
        with self.assertRaises(ValueError):
            intervals.merge([(3, 1)])
