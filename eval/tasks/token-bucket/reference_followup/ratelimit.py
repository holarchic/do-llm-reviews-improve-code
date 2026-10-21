import time

TOLERANCE = 1e-9


class TokenBucket:
    def __init__(self, capacity, refill_rate, clock=time.monotonic):
        if capacity <= 0 or refill_rate <= 0:
            raise ValueError("capacity and refill_rate must be positive")
        self._capacity = float(capacity)
        self._rate = float(refill_rate)
        self._clock = clock
        self._tokens = self._capacity
        self._last = clock()

    def try_acquire(self, tokens=1):
        self._check(tokens)
        self._refill()
        if self._tokens + TOLERANCE < tokens:
            return False
        self._tokens = max(0.0, self._tokens - tokens)
        return True

    def available(self):
        self._refill()
        return self._tokens

    def wait_time(self, tokens=1):
        self._check(tokens)
        self._refill()
        missing = tokens - self._tokens
        return 0.0 if missing <= TOLERANCE else missing / self._rate

    def _check(self, tokens):
        if tokens <= 0 or tokens > self._capacity:
            raise ValueError(f"tokens must be in (0, {self._capacity}], got {tokens}")

    def _refill(self):
        now = self._clock()
        if now <= self._last:
            return
        self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._rate)
        self._last = now


class KeyedLimiter:
    def __init__(self, capacity, refill_rate, clock=time.monotonic):
        if capacity <= 0 or refill_rate <= 0:
            raise ValueError("capacity and refill_rate must be positive")
        self._capacity = capacity
        self._rate = refill_rate
        self._clock = clock
        self._buckets = {}

    def try_acquire(self, key, tokens=1):
        if key not in self._buckets:
            self._buckets[key] = TokenBucket(self._capacity, self._rate, self._clock)
        return self._buckets[key].try_acquire(tokens)

    def available(self, key):
        bucket = self._buckets.get(key)
        return float(self._capacity) if bucket is None else bucket.available()

    def forget_idle(self):
        full = [key for key, bucket in self._buckets.items() if bucket.available() + TOLERANCE >= self._capacity]
        for key in full:
            del self._buckets[key]
        return len(full)
