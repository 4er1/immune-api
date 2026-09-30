import random
import unittest

import numpy as np

from immune.features import FEATURES, SourceWindow
from immune.model import Autoencoder
from immune.train import Autoencoder as TrainNet
from immune.train import collect_normal_features, train
from immune.traffic import ATTACKS, materialize, normal_source, split_across_containers


class TrafficGeneratorTests(unittest.TestCase):
    def test_normal_sources_produce_valid_records_and_known_kinds(self):
        rng = random.Random(1)
        kinds = set()
        for _ in range(60):
            kind, records = normal_source(rng, 300)
            kinds.add(kind)
            for t, dur, cost, size, status, route in records:
                self.assertGreaterEqual(t, 0)
                self.assertGreater(dur, 0)
                self.assertGreaterEqual(cost, 0)
                self.assertGreater(size, 0)
                self.assertIn(status, (200, 404))
        self.assertTrue(kinds)
        self.assertTrue(kinds.issubset({"browser_user", "mobile_app", "partner_batch", "internal_service",
                                        "power_user", "admin_user"}))

    def test_records_are_sorted_by_time_within_a_source(self):
        rng = random.Random(2)
        for _ in range(20):
            _, records = normal_source(rng, 300)
            times = [r[0] for r in records]
            self.assertEqual(times, sorted(times))

    def test_every_attack_generator_produces_events_and_features_extract_cleanly(self):
        rng = random.Random(3)
        for name, fn in ATTACKS.items():
            records = fn(rng, 120)
            with self.subTest(attack=name):
                self.assertGreater(len(records), 0)
                win = SourceWindow()
                for r in records:
                    win.add(r)
                feats = win.features(records[-1][0])
                self.assertEqual(len(feats), len(FEATURES))

    def test_dow_flood_rate_is_far_above_any_normal_profile(self):
        rng = random.Random(4)
        flood_rate = len(ATTACKS["dow_flood"](rng, 60)) / 60
        normal_rates = []
        for _ in range(30):
            _, records = normal_source(rng, 300)
            if len(records) > 5:
                normal_rates.append(len(records) / 300)
        self.assertGreater(flood_rate, max(normal_rates) * 3)

    def test_split_across_containers_preserves_every_record_exactly_once(self):
        rng = random.Random(5)
        records = materialize(rng, [(float(i), "GET /items") for i in range(50)])
        parts = split_across_containers(rng, records, k=4)
        self.assertEqual(len(parts), 4)
        self.assertEqual(sorted(r for part in parts for r in part), sorted(records))


class TrainingTests(unittest.TestCase):
    def test_collect_normal_features_shape_and_finiteness(self):
        X, kinds = collect_normal_features(seed=1, n_sources=40, duration=400)
        self.assertEqual(X.shape[1], len(FEATURES))
        self.assertGreater(X.shape[0], 20)
        self.assertTrue(np.all(np.isfinite(X)))
        self.assertEqual(len(kinds), X.shape[0])

    def test_net_forward_shapes_and_fit_reduces_loss(self):
        rng = np.random.default_rng(0)
        Z = rng.normal(size=(200, len(FEATURES)))
        net = TrainNet(len(FEATURES), n_hidden=4, seed=0)
        H, R = net.forward(Z)
        self.assertEqual(H.shape, (200, 4))
        self.assertEqual(R.shape, Z.shape)
        before = float(np.mean((R - Z) ** 2))
        net.fit(Z, epochs=30, lr=0.1, batch_size=32, seed=0)
        _, R2 = net.forward(Z)
        after = float(np.mean((R2 - Z) ** 2))
        self.assertLess(after, before)

    def test_end_to_end_train_produces_a_model_that_immune_model_can_load(self):
        spec = train(seed=1, n_sources=60, duration=400, n_hidden=3, epochs=40, lr=0.1, percentile=95)
        self.assertEqual(spec["features"], FEATURES)
        self.assertIn("meta", spec)
        self.assertGreaterEqual(spec["meta"]["val_false_positive_rate"], 0.0)
        ae = Autoencoder(spec)
        score = ae.score([0.0] * len(FEATURES))
        self.assertGreaterEqual(score, 0.0)

    def test_deterministic_with_the_same_seed(self):
        a = train(seed=7, n_sources=40, duration=300, n_hidden=3, epochs=30, lr=0.1, percentile=95)
        b = train(seed=7, n_sources=40, duration=300, n_hidden=3, epochs=30, lr=0.1, percentile=95)
        self.assertEqual(a["threshold"], b["threshold"])
        self.assertEqual(a["layers"], b["layers"])

    def test_not_enough_traffic_raises_a_clear_error(self):
        with self.assertRaises(RuntimeError):
            collect_normal_features(seed=1, n_sources=1, duration=0.5)


if __name__ == "__main__":
    unittest.main()
