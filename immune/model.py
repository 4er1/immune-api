"""The 'self' model: a small autoencoder trained ONLY on normal traffic.

A request pattern the network cannot reconstruct well is 'non-self'. Inference is plain Python
(a few hundred multiplications), so it runs inside a Lambda without numpy or a layer.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Dict, List

from .features import FEATURES


CLIP = 10.0   # standardised features are clipped so one wild value cannot dominate; training does the same


def canonical_sha256(model: Dict) -> str:
    body = {k: v for k, v in model.items() if k != "sha256"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def save(model: Dict, path: str) -> None:
    model = {**model, "sha256": canonical_sha256(model)}
    with open(path, "w") as fh:
        json.dump(model, fh, separators=(",", ":"))


class Autoencoder:
    def __init__(self, spec: Dict) -> None:
        if spec.get("features") != FEATURES:
            raise ValueError("model was trained with a different feature set")
        self.spec = spec
        self.mean, self.std = spec["mean"], spec["std"]
        self.layers = spec["layers"]                  # [{"W": [[...]], "b": [...], "act": "tanh"|"linear"}]
        self.threshold = float(spec["threshold"])

    @classmethod
    def load(cls, path: str) -> "Autoencoder":
        with open(path) as fh:
            spec = json.load(fh)
        if spec.get("sha256") != canonical_sha256(spec):
            raise ValueError("model file failed its integrity check")
        return cls(spec)

    def error(self, x: List[float]) -> float:
        z = [max(-CLIP, min(CLIP, (v - m) / s)) for v, m, s in zip(x, self.mean, self.std)]
        h = z
        for layer in self.layers:
            W, b = layer["W"], layer["b"]
            h = [sum(w * v for w, v in zip(col, h)) + bias for col, bias in zip(W, b)]
            if layer["act"] == "tanh":
                h = [math.tanh(v) for v in h]
        return sum((a - c) ** 2 for a, c in zip(z, h)) / len(z)

    def score(self, x: List[float]) -> float:
        """reconstruction error / threshold: >= 1.0 means 'foreign'."""
        return self.error(x) / self.threshold
