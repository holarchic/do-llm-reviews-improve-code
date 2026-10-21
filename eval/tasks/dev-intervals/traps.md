# Traps (private)

| Trap | TASK.md clause | Hidden tests |
|---|---|---|
| Adjacent spans merge (`(1,2),(3,4)` → `(1,4)`) | Behaviour 2 | `test_adjacent_merge` |
| Unsorted input | Behaviour 3 | `test_unsorted_input` |
| Input list not mutated | Behaviour 4 | `test_input_not_mutated` |
| Nested spans | Behaviour 2 | `test_nested` |
| Inclusive count (`(1,1)` covers 1 integer) | FOLLOWUP | `test_single_point_counts_one` |

Other assertion groups: basic overlap and disjoint (Behaviour 2, 3), empty input (5), `ValueError` (6); follow-up overlap counted once, empty, `ValueError`.
