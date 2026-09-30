"""Exercises the example Lambda handler end to end, with a real trained (tiny) model on disk."""
import importlib
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

from immune.model import save
from immune.train import train


class FakeClock:
    """Advances a bit every time it is read, so a tight request loop still looks spread out in
    time to the guard - like a real burst of ~40 req/s hitting a warm Lambda container."""

    def __init__(self, start: float = 1_700_000_000.0, step: float = 0.025) -> None:
        self.t, self.step = start, step

    def __call__(self) -> float:
        self.t += self.step
        return self.t


def make_event(method="GET", path="/items", key="test-key", ip="9.9.9.9"):
    return {"headers": {"x-api-key": key}, "rawPath": path,
            "requestContext": {"http": {"method": method, "path": path, "sourceIp": ip}}}


class LambdaHandlerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        model_path = os.path.join(self.tmp.name, "model.json")
        save(train(seed=1, n_sources=60, duration=400, n_hidden=3, epochs=40, lr=0.1, percentile=99), model_path)
        os.environ["IMMUNE_MODEL_PATH"] = model_path
        os.environ["IMMUNE_MODE"] = "enforce"
        os.environ.pop("IMMUNE_TABLE", None)
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda"))
        sys.modules.pop("handler", None)
        self.handler_module = importlib.import_module("handler")
        self.addCleanup(sys.path.remove, sys.path[0])
        self.addCleanup(sys.modules.pop, "handler", None)
        self.addCleanup(os.environ.pop, "IMMUNE_MODEL_PATH", None)
        self.addCleanup(os.environ.pop, "IMMUNE_MODE", None)

    def context(self, mem=256):
        return SimpleNamespace(memory_limit_in_mb=mem)

    def test_known_route_returns_200_with_json(self):
        resp = self.handler_module.handler(make_event(path="/items"), self.context())
        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(json.loads(resp["body"]), {"items": ["widget", "gadget", "gizmo"]})

    def test_unknown_route_returns_404(self):
        resp = self.handler_module.handler(make_event(path="/nope"), self.context())
        self.assertEqual(resp["statusCode"], 404)

    def test_model_is_loaded_once_at_import_time(self):
        self.assertTrue(hasattr(self.handler_module.GUARD, "model"))
        self.assertEqual(self.handler_module.GUARD.model.spec["threshold"], self.handler_module.GUARD.model.threshold)

    def test_a_denial_of_wallet_burst_eventually_gets_a_429(self):
        self.handler_module.GUARD.clock = FakeClock()   # simulate real elapsed time, not the test's own speed
        statuses = []
        for _ in range(400):
            resp = self.handler_module.handler(make_event(path="/search", key="flooder"), self.context())
            statuses.append(resp["statusCode"])
        self.assertIn(429, statuses)
        first_429 = statuses.index(429)
        self.assertTrue(all(s == 429 for s in statuses[first_429:][:5]))   # stays blocked, doesn't flap

    def test_different_keys_are_judged_independently(self):
        self.handler_module.GUARD.clock = FakeClock()
        for _ in range(400):
            self.handler_module.handler(make_event(path="/search", key="flooder-2"), self.context())
        resp = self.handler_module.handler(make_event(path="/items", key="innocent-bystander"), self.context())
        self.assertEqual(resp["statusCode"], 200)


if __name__ == "__main__":
    unittest.main()
