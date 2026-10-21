import unittest
from decimal import Decimal as D

import split
from split import Item


class DiscountTest(unittest.TestCase):
    def test_discount_before_tax_and_tip(self):
        items = [Item("a", D("30.00"), ("alice",)), Item("b", D("10.00"), ("bob",))]
        result = split.split_bill(items, ["alice", "bob"], D("10"), D("10"), discount=D("10.00"))
        self.assertEqual(result, {"alice": D("27.00"), "bob": D("9.00")})

    def test_discount_rounding_rules_still_apply(self):
        result = split.split_bill([Item("x", D("10.10"))], ["a", "b", "c"], D(0), D(0), discount=D("0.10"))
        self.assertEqual(result, {"a": D("3.34"), "b": D("3.33"), "c": D("3.33")})

    def test_total_rounded_once_after_discount(self):
        result = split.split_bill([Item("x", D("0.30"))], ["a"], D(0), D("25"), discount=D("0.20"))
        self.assertEqual(result, {"a": D("0.13")})

    def test_full_discount(self):
        result = split.split_bill([Item("x", D("8.00"))], ["a", "b"], D("10"), D("10"), discount=D("8.00"))
        self.assertEqual(result, {"a": D("0.00"), "b": D("0.00")})

    def test_invalid_discount(self):
        for discount in (D("-1"), D("8.01")):
            with self.subTest(discount=discount), self.assertRaises(ValueError):
                split.split_bill([Item("x", D("8.00"))], ["a"], D(0), D(0), discount=discount)
