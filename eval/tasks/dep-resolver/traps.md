# Traps (private)

| Trap | TASK.md clause | Hidden tests |
|---|---|---|
| Only reachable packages | 2 | `test_only_reachable_packages`, `test_missing_outside_closure_ignored`, `test_cycle_outside_closure_ignored` |
| Alphabetically smallest order, not DFS order | 4 | `test_alphabetically_smallest_order`, `test_smallest_order_is_not_dfs_order`, `test_diamond_once` |
| Cycle members exclude packages that only depend on a cycle | 6 | `test_members_exclude_dependents`, `test_two_cycles` |
| Missing before cycle | 5 | `test_missing_reported_before_cycle` |

Other assertion groups: chain (2, 3); large graph validity and determinism (3, 4); duplicates and empty targets (7); iterables (Interface); graph unchanged (8); missing target and dependency attributes (5); self-dependency (6).

Follow-up: installed omitted; not traversed past installed; still needed elsewhere; installed not in graph; cycle behind installed ignored; installed target omitted (FOLLOWUP).

## Degraded reference (judge calibration)

`degraded/` passes every hidden test. Injected defects: everything in one long function with deep nesting; a quadratic rescan-and-sort ready loop in place of a heap; one-letter names and commented-out code.
