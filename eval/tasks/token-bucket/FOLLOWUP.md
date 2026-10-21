# Follow-up: per-key limits

Add to `ratelimit.py`:

`class KeyedLimiter(capacity: float, refill_rate: float, clock: Callable[[], float] = time.monotonic)`

- `try_acquire(key: str, tokens: float = 1) -> bool` — each key has its own independent bucket with the given capacity and rate, following every TokenBucket rule. A key's bucket is created full the first time the key is used, with the clock reading at that moment as its starting time.
- `available(key: str) -> float` — tokens held by the key's bucket now; for a key never used, `capacity` (without creating a bucket).
- `forget_idle() -> int` — removes every bucket that is currently full (it behaves exactly like a fresh bucket) and returns how many were removed. Buckets that are not full are kept.

Same validation as TokenBucket (`ValueError`). Keep TokenBucket behaving exactly as before, and update your tests.
