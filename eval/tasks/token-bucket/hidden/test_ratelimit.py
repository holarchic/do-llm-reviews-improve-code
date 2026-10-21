import unittest

import ratelimit


class FakeClock:
    def __init__(self, now=100.0):
        self.now = now
        self.reads = 0

    def __call__(self):
        self.reads += 1
        return self.now


def bucket(capacity=10, rate=2, now=100.0):
    clock = FakeClock(now)
    return ratelimit.TokenBucket(capacity, rate, clock=clock), clock


class ConstructionTest(unittest.TestCase):
    def test_rejects_non_positive_parameters(self):  # clause 1
        for capacity, rate in ((0, 1), (-1, 1), (1, 0), (1, -2)):
            with self.subTest(capacity=capacity, rate=rate), self.assertRaises(ValueError):
                ratelimit.TokenBucket(capacity, rate, clock=FakeClock())

    def test_starts_full(self):  # clause 2
        b, _ = bucket(capacity=5)
        self.assertAlmostEqual(b.available(), 5)

    def test_default_clock_works(self):  # interface
        b = ratelimit.TokenBucket(3, 1)
        self.assertTrue(b.try_acquire())


class AcquireTest(unittest.TestCase):
    def test_acquire_removes_tokens(self):  # clause 5
        b, _ = bucket(capacity=10)
        self.assertTrue(b.try_acquire(4))
        self.assertAlmostEqual(b.available(), 6)

    def test_refusal_removes_nothing(self):  # clause 5
        b, _ = bucket(capacity=10)
        self.assertTrue(b.try_acquire(8))
        self.assertFalse(b.try_acquire(3))
        self.assertAlmostEqual(b.available(), 2)

    def test_invalid_amounts_raise(self):  # clause 5
        b, _ = bucket(capacity=10)
        for amount in (0, -1, 10.5):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                b.try_acquire(amount)

    def test_whole_capacity_can_be_acquired(self):  # clause 5
        b, _ = bucket(capacity=10)
        self.assertTrue(b.try_acquire(10))
        self.assertFalse(b.try_acquire(1))

    def test_fractional_tokens(self):  # clauses 5, 7
        b, _ = bucket(capacity=2.5, rate=1)
        self.assertTrue(b.try_acquire(1.25))
        self.assertAlmostEqual(b.available(), 1.25)


class RefillTest(unittest.TestCase):
    def test_refills_with_time(self):  # clause 3
        b, clock = bucket(capacity=10, rate=2)
        b.try_acquire(10)
        clock.now += 1.5
        self.assertAlmostEqual(b.available(), 3)

    def test_refill_capped_at_capacity(self):  # clause 3
        b, clock = bucket(capacity=10, rate=2)
        b.try_acquire(1)
        clock.now += 1000
        self.assertAlmostEqual(b.available(), 10)
        self.assertTrue(b.try_acquire(10))
        self.assertFalse(b.try_acquire(1))

    def test_one_clock_read_per_call(self):  # clauses 2, 3
        b, clock = bucket()
        self.assertEqual(clock.reads, 1)
        b.try_acquire()
        b.available()
        b.wait_time()
        self.assertEqual(clock.reads, 4)

    def test_backward_clock_adds_nothing(self):  # clause 4
        b, clock = bucket(capacity=10, rate=1)
        b.try_acquire(10)
        clock.now -= 50
        self.assertAlmostEqual(b.available(), 0)

    def test_backward_step_not_counted_twice(self):  # clause 4
        b, clock = bucket(capacity=10, rate=1, now=100)
        b.try_acquire(10)
        clock.now = 90
        b.available()
        clock.now = 102
        self.assertAlmostEqual(b.available(), 2)

    def test_float_boundary_succeeds(self):  # clause 6
        b, clock = bucket(capacity=1, rate=3, now=0.0)
        self.assertTrue(b.try_acquire(1))
        clock.now += 1 / 3
        self.assertTrue(b.try_acquire(1))

    def test_float_boundary_after_many_steps(self):  # clause 6
        b, clock = bucket(capacity=1, rate=10, now=0.0)
        self.assertTrue(b.try_acquire(1))
        for _ in range(10):
            clock.now += 0.01
            b.available()
        self.assertTrue(b.try_acquire(1))

    def test_available_never_negative(self):  # clause 6
        b, clock = bucket(capacity=1, rate=3, now=0.0)
        b.try_acquire(1)
        clock.now += 1 / 3
        b.try_acquire(1)
        self.assertGreaterEqual(b.available(), 0.0)


class WaitTimeTest(unittest.TestCase):
    def test_zero_when_available(self):  # clause 8
        b, _ = bucket(capacity=10, rate=2)
        self.assertEqual(b.wait_time(5), 0.0)

    def test_time_until_enough(self):  # clause 8
        b, _ = bucket(capacity=10, rate=2)
        b.try_acquire(10)
        self.assertAlmostEqual(b.wait_time(3), 1.5)

    def test_does_not_consume(self):  # clause 8
        b, _ = bucket(capacity=10, rate=2)
        b.wait_time(10)
        self.assertAlmostEqual(b.available(), 10)

    def test_wait_then_acquire_succeeds(self):  # clauses 6, 8
        b, clock = bucket(capacity=4, rate=3, now=0.0)
        b.try_acquire(4)
        clock.now += b.wait_time(2)
        self.assertTrue(b.try_acquire(2))

    def test_invalid_amounts_raise(self):  # clause 8
        b, _ = bucket(capacity=10)
        for amount in (0, 11):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                b.wait_time(amount)
