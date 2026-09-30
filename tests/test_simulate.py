import unittest

from immune.guard import GuardConfig
from immune.model import Autoencoder
from immune.simulate import build_population, run, summarize
from immune.train import train


class SimulateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = train(seed=1, n_sources=150, duration=900, n_hidden=3, epochs=150, lr=0.05, percentile=98)
        cls.model = Autoencoder(spec)

    def test_build_population_includes_normal_and_every_attack_kind(self):
        pop = build_population(seed=1, n_normal=20, per_attack=2, duration=60)
        kinds = {kind.split(":")[0] for _, kind, _, _ in pop}
        self.assertIn("normal", kinds)
        self.assertTrue({"dow_flood", "scanner", "credential_stuffing", "expensive_lowslow", "event_injection"} <= kinds)

    def test_run_returns_a_result_for_every_source_with_consistent_counts(self):
        pop = build_population(seed=2, n_normal=15, per_attack=2, duration=60)
        res = run(self.model, pop, seed=2)
        self.assertEqual(set(res), {sid for sid, *_ in pop})
        for sid, _, records, _ in pop:
            r = res[sid]
            self.assertEqual(r.sent, len(records))
            self.assertLessEqual(r.served, r.sent)
            if r.first_block_after is not None:
                self.assertLessEqual(r.served_before_block, r.served)

    def test_summarize_groups_by_kind_and_cost_avoided_is_between_0_and_1(self):
        pop = build_population(seed=3, n_normal=40, per_attack=6, duration=120)
        summary = summarize(run(self.model, pop, seed=3))
        self.assertIn("normal", summary)
        self.assertIn("dow_flood", summary)
        for kind, s in summary.items():
            self.assertTrue(0.0 <= s["cost_avoided_share"] <= 1.0 + 1e-9)
            self.assertTrue(0.0 <= s["blocked_share"] <= 1.0)

    def test_the_guard_meaningfully_reduces_attacker_cost_versus_no_guard(self):
        pop = build_population(seed=4, n_normal=30, per_attack=8, duration=150)
        guarded = summarize(run(self.model, pop, seed=4))
        unguarded_cfg = GuardConfig(min_events=10 ** 9)          # never accumulates enough evidence to judge
        unguarded = summarize(run(self.model, pop, config=unguarded_cfg, seed=4))
        for kind in ("dow_flood", "scanner", "credential_stuffing"):
            self.assertGreater(guarded[kind]["cost_avoided_share"], unguarded[kind]["cost_avoided_share"])
        self.assertLess(abs(guarded["normal"]["cost_avoided_share"]), 0.05)   # normal traffic barely touched

    def test_reproducible_given_the_same_seed(self):
        pop = build_population(seed=5, n_normal=20, per_attack=3, duration=90)
        a = summarize(run(self.model, pop, seed=5))
        b = summarize(run(self.model, pop, seed=5))
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
