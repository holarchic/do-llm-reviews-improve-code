import unittest

import ratelimit


class FakeClock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


class KeyedLimiterTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.limiter = ratelimit.KeyedLimiter(3, 1, clock=self.clock)

    def test_keys_are_independent(self):
        self.assertTrue(self.limiter.try_acquire("a", 3))
        self.assertFalse(self.limiter.try_acquire("a"))
        self.assertTrue(self.limiter.try_acquire("b", 3))

    def test_unseen_key_reports_capacity(self):
        self.assertAlmostEqual(self.limiter.available("never"), 3)
        self.assertEqual(self.limiter.forget_idle(), 0)

    def test_bucket_starts_at_first_use(self):
        self.clock.now = 50
        self.limiter.try_acquire("a", 3)
        self.clock.now = 51
        self.assertAlmostEqual(self.limiter.available("a"), 1)

    def test_forget_idle_removes_only_full_buckets(self):
        self.limiter.try_acquire("a", 3)
        self.limiter.try_acquire("b", 1)
        self.clock.now += 1
        self.assertEqual(self.limiter.forget_idle(), 1)
        self.assertAlmostEqual(self.limiter.available("a"), 1)

    def test_validation(self):
        with self.assertRaises(ValueError):
            ratelimit.KeyedLimiter(0, 1, clock=self.clock)
        with self.assertRaises(ValueError):
            self.limiter.try_acquire("a", 4)
