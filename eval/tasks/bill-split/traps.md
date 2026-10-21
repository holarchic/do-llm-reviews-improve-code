# Traps (private)

| Trap | TASK.md clause | Hidden tests |
|---|---|---|
| Leftover cents to the largest discarded fractions | 6 | `test_leftover_goes_to_largest_fractions` |
| Ties by `people` order, not alphabet | 6 | `test_leftover_cent_tie_goes_to_first`, `test_tie_follows_people_order_not_alphabet` |
| Total rounds half up, once, from the table subtotal | 5 | `test_total_rounds_half_up`, `test_total_half_up_with_two_people`, `test_many_people_sum_exactly` |
| Tip on the pre-tax subtotal | 4 | `test_tax_and_tip_on_pre_tax_subtotal` |
| Everyone present, in order, as `0.00` | 7 | `test_everyone_listed_in_order` |

Other assertion groups: even split (2, 6); subsets (2); duplicate sharer (2); zero price and no items (2, 5, 7); validation (1, 2, 4).

Follow-up: discount before tax and tip; rounding still applies; total rounded once; full discount; validation (FOLLOWUP).

## Degraded reference (judge calibration)

`degraded/` passes every hidden test. Injected defects: all logic in one long function; one-letter names, quadratic duplicate check and repeated `Decimal("0.01")` literals in place of a constant; uninformative `ValueError("bad")` messages and commented-out code.
