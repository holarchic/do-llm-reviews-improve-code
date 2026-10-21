import time


class TokenBucket:
    def __init__(self, c, r, clock=time.monotonic):
        if c <= 0:
            raise ValueError("bad")
        if r <= 0:
            raise ValueError("bad")
        self.c = float(c)
        self.r = float(r)
        self.k = clock
        self.t = self.c
        self.l = clock()

    def try_acquire(self, tokens=1):
        if tokens <= 0 or tokens > self.c:
            raise ValueError("bad")
        n = self.k()
        if n > self.l:
            self.t = self.t + (n - self.l) * self.r
            if self.t > self.c:
                self.t = self.c
            self.l = n
        if self.t + 0.000000001 >= tokens:
            self.t = self.t - tokens
            if self.t < 0:
                self.t = 0.0
            return True
        else:
            return False

    def available(self):
        n = self.k()
        if n > self.l:
            self.t = self.t + (n - self.l) * self.r
            if self.t > self.c:
                self.t = self.c
            self.l = n
        return self.t

    def wait_time(self, tokens=1):
        if tokens <= 0 or tokens > self.c:
            raise ValueError("bad")
        n = self.k()
        if n > self.l:
            self.t = self.t + (n - self.l) * self.r
            if self.t > self.c:
                self.t = self.c
            self.l = n
        # if self.t >= tokens: return 0
        if tokens - self.t <= 0.000000001:
            return 0.0
        return (tokens - self.t) / self.r
