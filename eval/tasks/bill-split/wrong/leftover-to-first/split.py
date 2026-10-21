from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Sequence

CENT = Decimal("0.01")


@dataclass(frozen=True)
class Item:
    name: str
    price: Decimal
    shared_by: Sequence[str] | None = None


def split_bill(items, people, tax_percent, tip_percent):
    people = list(people)
    if not people or len(set(people)) != len(people):
        raise ValueError("people must be non-empty and unique")
    if tax_percent < 0 or tip_percent < 0:
        raise ValueError("percentages must not be negative")
    subtotals = _subtotals(items, people)
    factor = 1 + (Decimal(tax_percent) + Decimal(tip_percent)) / 100
    table_subtotal = sum((Decimal(item.price) for item in items), Decimal(0))
    total = (table_subtotal * factor).quantize(CENT, rounding=ROUND_HALF_UP)
    return _allocate(total, {person: subtotal * factor for person, subtotal in subtotals.items()}, people)


def _subtotals(items, people):
    subtotals = {person: Decimal(0) for person in people}
    for item in items:
        if item.price < 0:
            raise ValueError(f"negative price for {item.name!r}")
        sharers = list(dict.fromkeys(people if item.shared_by is None else item.shared_by))
        if not sharers or any(name not in subtotals for name in sharers):
            raise ValueError(f"item {item.name!r} must be shared by known people")
        share = Decimal(item.price) / len(sharers)
        for name in sharers:
            subtotals[name] += share
    return subtotals


def _allocate(total, exact, people):
    """Largest-remainder rounding: floor every amount, then hand out the missing cents."""
    floored = {person: exact[person].quantize(CENT, rounding=ROUND_DOWN) for person in people}
    missing = int((total - sum(floored.values(), Decimal(0))) / CENT)
    by_fraction = people
    for person in by_fraction[:missing]:
        floored[person] += CENT
    return {person: floored[person] for person in people}
