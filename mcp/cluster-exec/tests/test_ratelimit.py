"""Tests for the sliding-window rate limiter behind `tail`/`status`."""

import unittest

from cluster_exec import ratelimit


class _FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class RateLimiterTests(unittest.TestCase):
    def test_allows_up_to_max_calls(self) -> None:
        clock = _FakeClock()
        rl = ratelimit.RateLimiter(3, 20.0, now=clock)
        for _ in range(3):
            self.assertTrue(rl.allow("s1"))
        self.assertFalse(rl.allow("s1"))

    def test_refused_call_does_not_consume_a_slot(self) -> None:
        clock = _FakeClock()
        rl = ratelimit.RateLimiter(3, 20.0, now=clock)
        for _ in range(3):
            self.assertTrue(rl.allow("s1"))
        self.assertFalse(rl.allow("s1"))
        # Still over the limit — the refused call didn't free anything.
        self.assertFalse(rl.allow("s1"))

    def test_window_expires_and_lets_calls_through(self) -> None:
        clock = _FakeClock()
        rl = ratelimit.RateLimiter(3, 20.0, now=clock)
        for _ in range(3):
            self.assertTrue(rl.allow("s"))
        clock.t = 20.0  # oldest hit (t=0) is exactly at the cutoff
        self.assertTrue(rl.allow("s"))

    def test_spaced_calls_stay_under_the_limit(self) -> None:
        # Three calls 10s apart only ever occupy 1-2 slots in the 20s window,
        # so a fourth call is admitted too.
        clock = _FakeClock()
        rl = ratelimit.RateLimiter(3, 20.0, now=clock)
        for t in (0.0, 10.0, 20.0, 20.0):
            clock.t = t
            self.assertTrue(rl.allow("s"))

    def test_keys_are_independent(self) -> None:
        clock = _FakeClock()
        rl = ratelimit.RateLimiter(3, 20.0, now=clock)
        for _ in range(3):
            self.assertTrue(rl.allow("s1"))
        self.assertTrue(rl.allow("s2"))  # other session is unaffected
        self.assertFalse(rl.allow("s1"))

    def test_backoff_seconds(self) -> None:
        clock = _FakeClock()
        rl = ratelimit.RateLimiter(3, 20.0, now=clock)
        self.assertEqual(rl.backoff_seconds("s"), 0.0)
        rl.allow("s")
        clock.t = 5.0
        self.assertAlmostEqual(rl.backoff_seconds("s"), 15.0)
        clock.t = 20.0
        self.assertAlmostEqual(rl.backoff_seconds("s"), 0.0)

    def test_validation(self) -> None:
        with self.assertRaises(ValueError):
            ratelimit.RateLimiter(0, 20.0)
        with self.assertRaises(ValueError):
            ratelimit.RateLimiter(3, 0.0)


class PollAllowedTests(unittest.TestCase):
    """Exercise `server._poll_allowed` with a controlled limiter."""

    def _limiter(self, clock: _FakeClock | None = None):
        return ratelimit.RateLimiter(
            3,
            20.0,
            recommended_sleep_s=10.0,
            now=clock or _FakeClock(),
        )

    def test_scoped_and_unscoped_buckets_are_separate(self) -> None:
        # Over the limit on a named session doesn't affect the unscoped
        # (no-filter / listing) bucket, and vice versa.
        limiter = self._limiter()
        keys = ("tail:sess1", "status:sess1", "tail:_unscoped", "status:_unscoped")
        for key in keys:
            for _ in range(limiter.max_calls):
                self.assertTrue(limiter.allow(key))

        def poll(tool, session):
            from cluster_exec import server
            return server._poll_allowed(tool, session, limiter=limiter)

        self.assertIn("RATE-LIMITED", poll("tail", "sess1"))
        self.assertIn("RATE-LIMITED", poll("status", "sess1"))
        self.assertIsNone(poll("tail", "sess2"))
        self.assertIn("RATE-LIMITED", poll("tail", None))
        self.assertIn("RATE-LIMITED", poll("status", None))
        self.assertIsNone(poll("status", "sess2"))

    def test_refusal_mentions_backoff(self) -> None:
        limiter = self._limiter()

        def poll(tool, session):
            from cluster_exec import server
            return server._poll_allowed(tool, session, limiter=limiter)

        for _ in range(limiter.max_calls):
            limiter.allow("tail:sess1")
        msg = poll("tail", "sess1")
        self.assertIn("sleep at least", msg)
        self.assertIn("sess1", msg)
        self.assertIn("tail", msg)

    def test_refusal_never_less_than_recommended_sleep(self) -> None:
        clock = _FakeClock()
        limiter = self._limiter(clock)

        def poll(tool, session):
            from cluster_exec import server
            return server._poll_allowed(tool, session, limiter=limiter)

        for _ in range(limiter.max_calls):
            limiter.allow("tail:sess1")
        # Advance the clock so only ~5s of the window remain — the refusal
        # still recommends at least the configured 10s, never less.
        clock.t = 15.0
        msg = poll("tail", "sess1")
        self.assertIn("sleep at least 10s", msg)


if __name__ == "__main__":
    unittest.main()
