# Follow-up: count covered integers

Add to `intervals.py`:

`total_covered(spans: list[tuple[int, int]]) -> int`

Returns how many distinct integers the spans cover, counting each integer once even where spans overlap. It follows the same rules as `merge` (inclusive ends; `start > end` raises `ValueError`; empty input returns 0). Keep `merge` behaving exactly as before. Update your tests.
