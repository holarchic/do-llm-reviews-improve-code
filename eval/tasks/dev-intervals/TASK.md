# Task: merge integer intervals

Write a Python module `intervals.py` at the repository root (standard library only, Python 3.11+). Tests import it as `import intervals`.

## Interface

`merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]`

## Behaviour

1. Each span `(start, end)` is an inclusive range of integers.
2. Spans that overlap, nest, or are adjacent (one ends at `n`, the next starts at `n + 1`) merge into one span.
3. The result is sorted by start; input order does not matter.
4. The input list is not modified.
5. An empty input returns an empty list.
6. A span with `start > end` raises `ValueError`.

Include your own tests.
