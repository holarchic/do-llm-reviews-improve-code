# Traps (private)

| Trap | TASK.md clause | Hidden tests |
|---|---|---|
| Exactly one clock read per call, including the constructor | 2, 3 | `test_one_clock_read_per_call` |
| Refill capped at capacity | 3 | `test_refill_capped_at_capacity` |
| Backward clock adds nothing and is not double-counted later | 4 | `test_backward_clock_adds_nothing`, `test_backward_step_not_counted_twice` |
| Refusal removes nothing | 5 | `test_refusal_removes_nothing` |
| Float tolerance at the refill boundary | 6 | `test_float_boundary_succeeds`, `test_float_boundary_after_many_steps`, `test_wait_then_acquire_succeeds` |

Other assertion groups: constructor validation (1); starts full (2); default clock (Interface); acquire/fractional tokens/whole capacity/invalid amounts (5, 7); refill (3); non-negative (6); wait_time zero, value, no consumption, validation (8).

Follow-up: per-key independence; unseen key; bucket starts at first use; forget_idle removes only full buckets; validation (FOLLOWUP).

## Degraded reference (judge calibration)

`degraded/` passes every hidden test. Injected defects: the refill block copied into all three methods; one-letter attribute names and a bare `1e-9` literal repeated in place of a named tolerance; commented-out code and uninformative `ValueError("bad")` messages.
