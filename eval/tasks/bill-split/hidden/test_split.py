import unittest
from decimal import Decimal as D

import split
from split import Item


class SplitTest(unittest.TestCase):
    def check_cents(self, result, total):
        for amount in result.values():
            self.assertIsInstance(amount, D)
            self.assertEqual(amount.as_tuple().exponent, -2)
        self.assertEqual(sum(result.values()), D(total))

    def test_even_split(self):  # clauses 2, 6
        result = split.split_bill([Item("pizza", D("30.00"))], ["a", "b", "c"], D(0), D(0))
        self.assertEqual(result, {"a": D("10.00"), "b": D("10.00"), "c": D("10.00")})
        self.check_cents(result, "30.00")

    def test_leftover_cent_tie_goes_to_first(self):  # clause 6
        result = split.split_bill([Item("cake", D("10.00"))], ["a", "b", "c"], D(0), D(0))
        self.assertEqual(result, {"a": D("3.34"), "b": D("3.33"), "c": D("3.33")})

    def test_tie_follows_people_order_not_alphabet(self):  # clause 6
        result = split.split_bill([Item("cake", D("10.00"))], ["zoe", "amy", "bo"], D(0), D(0))
        self.assertEqual(result["zoe"], D("3.34"))

    def test_leftover_goes_to_largest_fractions(self):  # clause 6
        items = [Item("chips", D("0.10")), Item("dip", D("0.05"), ("q", "r"))]
        result = split.split_bill(items, ["p", "q", "r"], D(0), D(0))
        self.assertEqual(result, {"p": D("0.03"), "q": D("0.06"), "r": D("0.06")})

    def test_shared_by_subsets(self):  # clause 2
        items = [Item("steak", D("20.00"), ("alice",)), Item("salad", D("10.00"), ("bob",)),
                 Item("wine", D("12.00"), ("alice", "bob"))]
        result = split.split_bill(items, ["alice", "bob"], D(0), D(0))
        self.assertEqual(result, {"alice": D("26.00"), "bob": D("16.00")})

    def test_tax_and_tip_on_pre_tax_subtotal(self):  # clauses 4, 6
        items = [Item("a", D("20.00"), ("alice",)), Item("b", D("10.00"), ("bob",))]
        result = split.split_bill(items, ["alice", "bob"], D("10"), D("15"))
        self.assertEqual(result, {"alice": D("25.00"), "bob": D("12.50")})

    def test_total_rounds_half_up(self):  # clause 5
        result = split.split_bill([Item("mint", D("0.10"))], ["a"], D(0), D("25"))
        self.assertEqual(result, {"a": D("0.13")})

    def test_total_half_up_with_two_people(self):  # clauses 5, 6
        result = split.split_bill([Item("tea", D("0.50"))], ["a", "b"], D("5"), D(0))
        self.check_cents(result, "0.53")

    def test_many_people_sum_exactly(self):  # clauses 5, 6
        people = [f"p{i}" for i in range(7)]
        items = [Item("x", D("99.99")), Item("y", D("13.37"), people[:3]), Item("z", D("0.01"), people[4:])]
        result = split.split_bill(items, people, D("8.875"), D("18"))
        total = ((D("99.99") + D("13.37") + D("0.01")) * (1 + (D("8.875") + D("18")) / 100)).quantize(D("0.01"), rounding="ROUND_HALF_UP")
        self.check_cents(result, total)

    def test_everyone_listed_in_order(self):  # clause 7
        result = split.split_bill([Item("x", D("5.00"), ("b",))], ["c", "a", "b"], D(0), D(0))
        self.assertEqual(list(result), ["c", "a", "b"])
        self.assertEqual(str(result["c"]), "0.00")

    def test_duplicate_sharer_counts_once(self):  # clause 2
        result = split.split_bill([Item("x", D("6.00"), ("a", "a", "b"))], ["a", "b"], D(0), D(0))
        self.assertEqual(result, {"a": D("3.00"), "b": D("3.00")})

    def test_zero_price_item(self):  # clause 2
        result = split.split_bill([Item("water", D("0"))], ["a"], D(10), D(10))
        self.assertEqual(result, {"a": D("0.00")})

    def test_no_items(self):  # clauses 5, 7
        self.assertEqual(split.split_bill([], ["a", "b"], D(0), D(0)), {"a": D("0.00"), "b": D("0.00")})

    def test_validation(self):  # clauses 1, 2, 4
        good = [Item("x", D("1.00"))]
        cases = [
            (good, [], D(0), D(0)),
            (good, ["a", "a"], D(0), D(0)),
            ([Item("x", D("-1"))], ["a"], D(0), D(0)),
            ([Item("x", D("1"), ("ghost",))], ["a"], D(0), D(0)),
            ([Item("x", D("1"), ())], ["a"], D(0), D(0)),
            (good, ["a"], D("-1"), D(0)),
            (good, ["a"], D(0), D("-5")),
        ]
        for items, people, tax, tip in cases:
            with self.subTest(people=people, tax=tax, tip=tip), self.assertRaises(ValueError):
                split.split_bill(items, people, tax, tip)
