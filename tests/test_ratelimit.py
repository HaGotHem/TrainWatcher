"""Task 7: per-key sliding-window limiter (monatrain.ratelimit).

`SlidingWindowLimiter(limit, window_s, clock=time.monotonic)`; `hit(key) -> bool` records a hit and
returns True while the key has made at most `limit` hits within the last `window_s` seconds,
False beyond. A refused hit does not extend the window.
"""

from monatrain.ratelimit import SlidingWindowLimiter


class FakeMonotonic:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_allows_up_to_limit_then_refuses():
    clock = FakeMonotonic()
    limiter = SlidingWindowLimiter(30, 600, clock=clock)

    assert all(limiter.hit("1.2.3.4") for _ in range(30))
    assert limiter.hit("1.2.3.4") is False


def test_keys_are_independent():
    clock = FakeMonotonic()
    limiter = SlidingWindowLimiter(2, 600, clock=clock)

    assert limiter.hit("a") and limiter.hit("a")
    assert limiter.hit("a") is False
    assert limiter.hit("b") is True


def test_limiter_window_expires():
    clock = FakeMonotonic()
    limiter = SlidingWindowLimiter(3, 600, clock=clock)

    assert limiter.hit("ip")  # t = 1000
    clock.t += 100
    assert limiter.hit("ip")  # t = 1100
    assert limiter.hit("ip")  # t = 1100
    assert limiter.hit("ip") is False

    clock.t = 1000 + 599
    assert limiter.hit("ip") is False  # the first hit is still inside the window

    clock.t = 1000 + 601
    assert limiter.hit("ip") is True  # the first hit has left the window
    assert limiter.hit("ip") is False  # the two hits at t = 1100 are still counted

    clock.t = 1100 + 601
    assert limiter.hit("ip") is True


def test_refused_hits_do_not_extend_the_window():
    clock = FakeMonotonic()
    limiter = SlidingWindowLimiter(1, 10, clock=clock)

    assert limiter.hit("k")  # t = 1000
    for step in range(1, 10):
        clock.t = 1000 + step
        assert limiter.hit("k") is False
    clock.t = 1000 + 11
    assert limiter.hit("k") is True


def test_default_clock_is_monotonic():
    limiter = SlidingWindowLimiter(1, 600)
    assert limiter.hit("x") is True
    assert limiter.hit("x") is False
