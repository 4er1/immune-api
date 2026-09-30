import copy
import json
import tempfile
import unittest

from immune.features import FEATURES
from immune.model import Autoencoder, canonical_sha256, save

SPEC = {
    "features": FEATURES,
    "mean": [0.0] * len(FEATURES),
    "std": [1.0] * len(FEATURES),
    "threshold": 0.5,
    "layers": [
        {"W": [[1.0 if i == j else 0.0 for j in range(len(FEATURES))] for i in range(3)], "b": [0.0, 0.0, 0.0], "act": "tanh"},
        {"W": [[1.0 if i == j else 0.0 for j in range(3)] for i in range(len(FEATURES))], "b": [0.0] * len(FEATURES), "act": "tanh"},
    ],
}


class AutoencoderTests(unittest.TestCase):
    def test_rejects_a_mismatched_feature_set(self):
        with self.assertRaises(ValueError):
            Autoencoder({**SPEC, "features": ["only_one"]})

    def test_zero_input_reconstructs_to_zero_error(self):
        ae = Autoencoder(SPEC)
        self.assertAlmostEqual(ae.error([0.0] * len(FEATURES)), 0.0)
        self.assertAlmostEqual(ae.score([0.0] * len(FEATURES)), 0.0)

    def test_score_is_error_divided_by_threshold(self):
        ae = Autoencoder({**SPEC, "threshold": 2.0})
        x = [1.0] + [0.0] * (len(FEATURES) - 1)
        self.assertAlmostEqual(ae.score(x), ae.error(x) / 2.0)

    def test_extreme_values_are_clipped_before_scoring(self):
        ae = Autoencoder(SPEC)
        huge = [1e9] + [0.0] * (len(FEATURES) - 1)
        normal_extreme = [10.0] + [0.0] * (len(FEATURES) - 1)
        self.assertAlmostEqual(ae.error(huge), ae.error(normal_extreme))    # both clipped to the same value

    def test_save_load_roundtrip_and_integrity_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/m.json"
            save(SPEC, path)
            loaded = Autoencoder.load(path)
            self.assertEqual(loaded.threshold, 0.5)
            with open(path) as fh:
                data = json.load(fh)
            data["threshold"] = 999.0                    # tamper with the file directly
            with open(path, "w") as fh:
                json.dump(data, fh)
            with self.assertRaises(ValueError):
                Autoencoder.load(path)

    def test_canonical_hash_ignores_key_order_and_existing_hash_field(self):
        a = dict(SPEC)
        b = {"sha256": "whatever", **copy.deepcopy(SPEC)}
        self.assertEqual(canonical_sha256(a), canonical_sha256(b))


if __name__ == "__main__":
    unittest.main()
