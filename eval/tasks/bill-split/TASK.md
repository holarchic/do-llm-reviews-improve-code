# Task: split a restaurant bill

Write a Python module `split.py` at the repository root (standard library only). Tests import it as `import split`.

## Interface

- `@dataclass(frozen=True) class Item` with fields `name: str`, `price: Decimal`, `shared_by: Sequence[str] | None = None`.
- `split_bill(items: Sequence[Item], people: Sequence[str], tax_percent: Decimal, tip_percent: Decimal) -> dict[str, Decimal]`

Use `Decimal` arithmetic throughout.

## Behaviour

1. `people` must be non-empty with no duplicate names; otherwise `ValueError`.
2. Each item's `price` must be at least 0; otherwise `ValueError`. The price is shared equally by the people in `shared_by`, or by everyone in `people` when `shared_by` is `None`. `shared_by` must be non-empty and name only people in `people`; otherwise `ValueError`. A name listed twice in `shared_by` counts once.
3. A person's subtotal is the exact sum of their shares, with no rounding.
4. Tax and tip are both percentages of the pre-tax subtotal (the tip is not charged on the tax). `tax_percent` and `tip_percent` must be at least 0; otherwise `ValueError`.
5. The bill total is the table's subtotal (the sum of all item prices) plus tax plus tip, rounded once to cents, with halves rounded up (`ROUND_HALF_UP`).
6. A person's exact amount is their subtotal × (1 + (tax_percent + tip_percent) / 100). The returned amounts are whole cents that add up exactly to the bill total: each person first gets their exact amount rounded down to the cent; the cents still missing from the total then go one each to the people whose rounding discarded the largest fractions. Ties go to the person listed earlier in `people`.
7. The result has one entry for every person, in the order of `people`. Each amount is a `Decimal` with exactly two decimal places; a person who shares nothing gets `Decimal("0.00")`.

Include your own tests.
