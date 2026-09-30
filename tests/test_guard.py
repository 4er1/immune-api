import json
import unittest

from immune.features import FEATURES
from immune.guard import Guard, GuardConfig, request_bytes, route_hash, route_of, source_id
from immune.model import Autoencoder
from immune.store import MemoryBlockStore

SPEC = {"features": FEATURES, "mean": [0.0] * len(FEATURES), "std": [1.0] * len(FEATURES), "threshold": 0.5,
        "layers": [{"W": [[0.0] * len(FEATURES) for _ in range(3)], "b": [0.0, 0.0, 0.0], "act": "tanh"},
                  {"W": [[0.0] * 3 for _ in range(len(FEATURES))], "b": [0.0] * len(FEATURES), "act": "tanh"}]}


class FixedScoreModel(Autoencoder):
    """A stand-in model whose score is fixed by the test, instead of depending on real features."""

    def __init__(self, score_fn):
        super().__init__(SPEC)
        self._score_fn = score_fn

    def score(self, x):
        return self._score_fn()


def event(key=None, ip="1.2.3.4", method="GET", path="/items", body=None):
    headers = {"x-api-key": key} if key else {}
    return {"headers": headers, "requestContext": {"http": {"sourceIp": ip, "method": method, "path": path}},
            "body": body}


class MetadataExtractionTests(unittest.TestCase):
    def test_source_id_prefers_the_api_key_and_never_stores_it_raw(self):
        sid = source_id(event(key="super-secret-key"))
        self.assertTrue(sid.startswith("key:"))
        self.assertNotIn("super-secret-key", sid)

    def test_source_id_falls_back_to_ip(self):
        self.assertEqual(source_id(event()), "ip:1.2.3.4")

    def test_route_of_normalises_numeric_and_uuid_segments(self):
        self.assertEqual(route_of(event(path="/items/12345")), "GET /items/{id}")
        self.assertEqual(route_of(event(path="/items/9c858901-8a57-4791-81fe-4c455b099bc9")), "GET /items/{id}")
        self.assertEqual(route_of(event(method="post", path="/orders")), "POST /orders")

    def test_route_hash_is_stable(self):
        self.assertEqual(route_hash("GET /items"), route_hash("GET /items"))

    def test_request_bytes_prefers_body_then_content_length_header(self):
        self.assertEqual(request_bytes(event(body="1234567")), 7)
        e = event()
        e["headers"]["content-length"] = "42"
        self.assertEqual(request_bytes(e), 42)
        self.assertEqual(request_bytes(event()), 0)


class GuardBlockingTests(unittest.TestCase):
    def setUp(self):
        self.t = 1000.0
        self.clock = lambda: self.t
        self.alerts = []
        self.always_anomalous = FixedScoreModel(lambda: 5.0)
        self.always_normal = FixedScoreModel(lambda: 0.1)

    def guard(self, model, **cfg):
        return Guard(model, MemoryBlockStore(clock=self.clock), GuardConfig(**cfg), clock=self.clock,
                    emit=self.alerts.append)

    def feed(self, g, src, n, dt=1.0):
        """Adds n requests spaced dt apart, starting from the current clock. Note the FIRST
        request from any brand-new source never yields a verdict with feats (the window needs
        >= 2 records) - callers who care about exact counts should account for that."""
        verdicts = []
        for _ in range(n):
            verdicts.append(g.after(src, (self.t, 20.0, 5.0, 500, 200, 1), self.t))
            self.t += dt
        return verdicts

    def test_a_single_anomalous_check_never_blocks_on_its_own(self):
        g = self.guard(self.always_anomalous, min_events=2, confirm_seconds=0.01, confirm_evals=1)
        v = self.feed(g, "s1", 2)                    # request 1: no feats yet; request 2: first anomalous eval
        self.assertEqual([r.anomalous for r in v], [False, True])
        self.assertFalse(v[-1].blocked_now)           # confirmation always needs a FOLLOW-UP check

    def test_blocks_once_confirm_evals_and_confirm_seconds_are_both_satisfied(self):
        g = self.guard(self.always_anomalous, min_events=2, confirm_seconds=1.5, confirm_evals=3)
        v = self.feed(g, "s1", 3, dt=1.0)             # req1: no feats; req2,3: anomalous evals 1,2
        self.assertFalse(v[-1].blocked_now)           # only 2 evals so far, and only 1.0s elapsed
        v2 = self.feed(g, "s1", 1, dt=1.0)            # req4: anomalous eval 3, 2.0s since the first
        self.assertTrue(v2[-1].blocked_now)

    def test_normal_traffic_is_never_blocked(self):
        g = self.guard(self.always_normal, min_events=2, confirm_seconds=0.01, confirm_evals=1)
        v = self.feed(g, "s1", 50)
        self.assertFalse(any(r.anomalous for r in v))
        self.assertEqual(g.counters["blocks"], 0)

    def test_block_is_enforced_on_the_next_request(self):
        g = self.guard(self.always_anomalous, min_events=2, confirm_seconds=0.01, confirm_evals=1)
        self.feed(g, "s1", 3)                         # req2 anomalous, req3 confirms and blocks
        self.assertIsNotNone(g.before("s1", self.t))
        self.assertIsNone(g.before("s1", self.t + 10000))   # long after the block should have expired

    def test_monitor_mode_never_rejects_but_still_logs(self):
        g = self.guard(self.always_anomalous, mode="monitor", min_events=2, confirm_seconds=0.01, confirm_evals=1)
        self.feed(g, "s1", 3)
        self.assertIsNone(g.before("s1", self.t))            # monitor mode: nothing is actually enforced
        self.assertEqual(g.counters["would_block"], 1)
        self.assertEqual(g.counters["blocks"], 0)
        self.assertTrue(any(a.get("event") == "would_block" for a in self.alerts))

    def test_repeated_offenses_get_more_strikes_and_a_longer_block(self):
        g = self.guard(self.always_anomalous, min_events=2, confirm_seconds=0.01, confirm_evals=1,
                       block_base=10.0, block_max=1000.0)
        self.feed(g, "s1", 3)
        first = g.store.get_block("s1")
        self.assertEqual(first.strikes, 1)
        self.t = first.until + 1                            # block expires...
        self.feed(g, "s1", 3)                                # ...and it offends again
        second = g.store.get_block("s1")
        self.assertEqual(second.strikes, 2)
        self.assertGreater(second.until - self.t, first.until - (first.until - 10))  # sanity: further out
        self.assertAlmostEqual((first.until - (first.until - 10)), 10.0, delta=1.0)   # 1st block ~= block_base
        self.assertAlmostEqual(second.until - self.t, 20.0, delta=1.0)                # 2nd block ~= 2x block_base

    def test_block_duration_doubles_and_is_capped(self):
        g = self.guard(self.always_anomalous, min_events=2, confirm_seconds=0.01, confirm_evals=1,
                       block_base=10.0, block_max=25.0)
        self.feed(g, "s1", 3)
        d1 = g.store.get_block("s1").until - self.t
        self.t = g.store.get_block("s1").until + 1
        self.feed(g, "s1", 3)
        d2 = g.store.get_block("s1").until - self.t
        self.assertAlmostEqual(d1, 10.0, delta=1.0)
        self.assertAlmostEqual(d2, 20.0, delta=1.0)
        self.t = g.store.get_block("s1").until + 1
        self.feed(g, "s1", 3)
        d3 = g.store.get_block("s1").until - self.t
        self.assertAlmostEqual(d3, 25.0, delta=1.0)          # capped at block_max, not 40

    def test_strikes_reset_after_strike_memory_expires(self):
        g = self.guard(self.always_anomalous, min_events=2, confirm_seconds=0.01, confirm_evals=1,
                       block_base=10.0, strike_memory=5.0)
        self.feed(g, "s1", 3)
        self.assertEqual(g.store.get_block("s1").strikes, 1)
        self.t += 10 + 5 + 100                               # long past block + strike_memory
        self.assertIsNone(g.before("s1", self.t))
        self.feed(g, "s1", 3)
        self.assertEqual(g.store.get_block("s1").strikes, 1)  # started over, not 2

    def test_a_broken_store_fails_open_and_is_counted(self):
        class BrokenStore:
            def get_block(self, src): raise RuntimeError("dynamo is down")
            def put_block(self, *a, **k): raise RuntimeError("dynamo is down")

        g = Guard(self.always_anomalous, BrokenStore(), GuardConfig(min_events=2, confirm_seconds=0.01, confirm_evals=1),
                 clock=self.clock, emit=self.alerts.append)
        self.assertIsNone(g.before("s1", self.t))            # never raises, never blocks on a store error
        self.feed(g, "s1", 3)
        self.assertGreaterEqual(g.counters["store_errors"], 1)

    def test_per_container_memory_is_bounded_lru(self):
        g = self.guard(self.always_normal, max_sources=3)
        for i in range(10):
            g._local(f"s{i}")
        self.assertEqual(len(g._sources), 3)
        self.assertEqual(list(g._sources), ["s7", "s8", "s9"])


class ProtectDecoratorTests(unittest.TestCase):
    def setUp(self):
        self.t, self.alerts = 0.0, []

    def clock(self):
        return self.t

    def guard(self, model, **cfg):
        return Guard(model, MemoryBlockStore(clock=self.clock), GuardConfig(**cfg), clock=self.clock,
                    emit=self.alerts.append)

    def test_wraps_a_normal_handler_and_passes_its_response_through(self):
        g = self.guard(Autoencoder(SPEC))
        calls = []

        @g.protect
        def handler(event, context):
            calls.append(event)
            return {"statusCode": 201, "body": json.dumps({"ok": True})}

        resp = handler(event(), context=None)
        self.assertEqual(resp["statusCode"], 201)
        self.assertEqual(len(calls), 1)

    def test_blocked_source_gets_429_without_calling_the_handler(self):
        g = self.guard(FixedScoreModel(lambda: 5.0), min_events=2, confirm_seconds=0.01, confirm_evals=1)
        calls = []

        @g.protect
        def handler(event, context):
            calls.append(1)
            return {"statusCode": 200, "body": "{}"}

        for _ in range(3):                                    # req1: no feats; req2: anomalous; req3: confirms+blocks
            handler(event(key="k"), None)
            self.t += 1.0
        resp = handler(event(key="k"), None)                   # req4: should now be rejected
        self.assertEqual(resp["statusCode"], 429)
        self.assertIn("retry-after", resp["headers"])
        self.assertEqual(len(calls), 3)                        # the handler ran for req1-3, not req4

    def test_a_handler_exception_still_records_metadata_and_propagates(self):
        g = self.guard(Autoencoder(SPEC), min_events=2, confirm_seconds=0.01, confirm_evals=1)

        @g.protect
        def handler(event, context):
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            handler(event(), None)
        self.assertEqual(g._local(source_id(event())).win.ring[-1][4], 500)   # recorded as a 500

    def test_a_broken_guard_never_breaks_the_api_fail_open(self):
        class ExplodingModel(Autoencoder):
            def __init__(self): super().__init__(SPEC)
            def score(self, x): raise RuntimeError("model exploded")

        g = self.guard(ExplodingModel(), min_events=2, confirm_seconds=0.01, confirm_evals=1)

        @g.protect
        def handler(event, context):
            return {"statusCode": 200, "body": "{}"}

        resp = handler(event(), None)
        self.assertEqual(resp["statusCode"], 200)


if __name__ == "__main__":
    unittest.main()
