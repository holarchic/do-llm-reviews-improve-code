# Follow-up: discounts

Add a keyword parameter: `split_bill(items, people, tax_percent, tip_percent, discount: Decimal = Decimal("0"))`.

`discount` is an amount taken off the table's subtotal before tax and tip are calculated. Each person's subtotal is reduced in proportion to their share of the table's subtotal, and every rule in TASK.md then applies to the reduced subtotals (the bill total is the reduced table subtotal plus tax and tip, rounded once). `discount` must be at least 0 and at most the table's subtotal; otherwise `ValueError`. Keep everything in TASK.md working, and update your tests.
