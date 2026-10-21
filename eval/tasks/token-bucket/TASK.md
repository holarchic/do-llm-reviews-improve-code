# Task: token-bucket rate limiter

Write a Python module `ratelimit.py` at the repository root (standard library only). Tests import it as `import ratelimit`.

## Interface

`class TokenBucket(capacity: float, refill_rate: float, clock: Callable[[], float] = time.monotonic)`

- `try_acquire(tokens: float = 1) -> bool`
- `available() -> float`
- `wait_time(tokens: float = 1) -> float`

`clock` returns the current time in seconds. Tests pass a fake clock.

## Behaviour

1. `capacity` and `refill_rate` (tokens per second) must both be greater than 0; otherwise the constructor raises `ValueError`.
2. The bucket starts full, and the constructor reads `clock()` once as its starting time.
3. Every method call reads `clock()` exactly once. Tokens refill continuously: the bucket adds `elapsed × refill_rate`, where `elapsed` is the time since the previous reading, and never holds more than `capacity`.
4. If `clock()` returns a value lower than the latest reading so far, no time has passed: no tokens are added or removed, and the bucket keeps measuring from the latest (higher) reading, so time is not counted twice when the clock moves forward again.
5. `try_acquire(tokens)`: `tokens` must be greater than 0 and at most `capacity`, otherwise `ValueError`. If the bucket holds at least `tokens`, it removes them and returns `True`. Otherwise it returns `False` and removes nothing.
6. Comparisons of token amounts allow a tolerance of `1e-9`: a bucket holding `tokens - 1e-9` or more counts as holding `tokens`. Floating-point refill (for example a rate of 3 per second after one third of a second) must therefore not cause a spurious refusal. `available()` never returns a negative number.
7. `available()` returns the number of tokens held now, after refilling.
8. `wait_time(tokens)`: the same `ValueError` rules as `try_acquire`. It returns how many seconds from now until `try_acquire(tokens)` would succeed if nothing else acquires, and `0.0` if it would succeed now. It does not remove tokens.

Include your own tests.
