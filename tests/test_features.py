import math
import unittest

from immune.features import FEATURES, SourceWindow


def rec(t, dur=30.0, cost=None, size=1000, status=200, route=1):
    return (t, dur, cost if cost is not None else dur * 256 / 1024, size, status, route)


class SourceWindowTests(unittest.TestCase):
    def test_not_enough_events_returns_none(self):
        win = SourceWindow()
        win.add(rec(0.0))
        self.assertIsNone(win.features(0.0))
        self.assertIsNone(win.features(0.0, min_events=1))    # still only 1 event

    def test_feature_vector_has_the_documented_length_and_order(self):
        win = SourceWindow()
        for i in range(5):
            win.add(rec(i * 1.0))
        f = win.features(4.0)
        self.assertEqual(len(f), len(FEATURES))
        self.assertTrue(all(math.isfinite(v) for v in f))

    def test_higher_rate_gives_a_higher_rate_feature(self):
        slow, fast = SourceWindow(), SourceWindow()
        for i in range(20):
            slow.add(rec(i * 1.0))
            fast.add(rec(i * 0.05))
        self.assertGreater(fast.features(fast.ring[-1][0])[0], slow.features(slow.ring[-1][0])[0])

    def test_regular_timing_gives_a_low_coefficient_of_variation(self):
        regular, bursty = SourceWindow(), SourceWindow()
        for i in range(20):
            regular.add(rec(i * 1.0))
        t = 0.0
        for i in range(20):
            t += 0.05 if i % 4 else 3.0
            bursty.add(rec(t))
        self.assertLess(regular.features(regular.ring[-1][0])[3], 0.05)
        self.assertGreater(bursty.features(bursty.ring[-1][0])[3], 0.5)

    def test_error_ratio_and_distinct_routes(self):
        win = SourceWindow()
        for i in range(10):
            win.add(rec(i * 1.0, status=404 if i < 6 else 200, route=i))
        f = win.features(9.0)
        idx = FEATURES.index("err_ratio")
        self.assertAlmostEqual(f[idx], 0.6)
        self.assertAlmostEqual(f[FEATURES.index("distinct_ratio")], 1.0)   # every route different

    def test_top_route_share_when_one_route_dominates(self):
        win = SourceWindow()
        for i in range(10):
            win.add(rec(i * 1.0, route=1 if i < 8 else 2))
        f = win.features(9.0)
        self.assertAlmostEqual(f[FEATURES.index("top_route_share")], 0.8)

    def test_window_forgets_old_requests(self):
        win = SourceWindow()
        for i in range(10):
            win.add(rec(i * 1.0))
        recent = win.recent(now=10 + 400)                # far beyond WINDOW_SECONDS
        self.assertEqual(recent, [])
        self.assertIsNone(win.features(10 + 400))

    def test_ring_buffer_is_bounded(self):
        win = SourceWindow()
        for i in range(200):
            win.add(rec(i * 0.01))
        self.assertLessEqual(len(win.ring), 64)

    def test_cost_rate_reflects_expensive_requests(self):
        cheap, pricey = SourceWindow(), SourceWindow()
        for i in range(20):
            cheap.add(rec(i * 1.0, dur=10, cost=0.01))
            pricey.add(rec(i * 1.0, dur=1000, cost=50.0))
        idx = FEATURES.index("log_cost_rate_60s")
        self.assertGreater(pricey.features(19.0)[idx], cheap.features(19.0)[idx])

    def test_decay_after_a_gap_in_traffic(self):
        win = SourceWindow()
        for i in range(20):
            win.add(rec(i * 0.1))
        f_live = win.features(2.0)
        f_after_gap = win.features(30.0)                # no more events, just time passing
        self.assertGreater(f_live[1], f_after_gap[1])    # short-window rate should have decayed


if __name__ == "__main__":
    unittest.main()
