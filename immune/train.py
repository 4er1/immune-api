"""Trains the autoencoder on 'self' (normal) traffic ONLY. Needs numpy - this module never ships
inside the Lambda package (see immune/model.py, which is pure Python)."""
from __future__ import annotations

import argparse
import json
import random
from typing import List, Tuple

import numpy as np

from .features import FEATURES, SourceWindow
from .model import canonical_sha256
from .traffic import normal_source


def collect_normal_features(seed: int, n_sources: int, duration: float, sample_every: float = 5.0
                            ) -> Tuple[np.ndarray, List[str]]:
    """Replays synthetic normal sources and samples their feature vector every `sample_every`
    seconds of activity - the same online, per-source window a container keeps at inference time."""
    rng = random.Random(seed)
    rows, kinds = [], []
    for i in range(n_sources):
        kind, records = normal_source(rng, duration)
        if len(records) < 4:
            continue
        win, next_sample = SourceWindow(), records[0][0] + sample_every
        for rec in records:
            win.add(rec)
            if rec[0] >= next_sample:
                feats = win.features(rec[0])
                if feats is not None:
                    rows.append(feats)
                    kinds.append(kind)
                next_sample = rec[0] + sample_every
    if not rows:
        raise RuntimeError("no training rows were generated - increase --sources or --duration")
    return np.array(rows, dtype=np.float64), kinds


def _init_layer(rng: np.random.Generator, n_in: int, n_out: int) -> Tuple[np.ndarray, np.ndarray]:
    limit = np.sqrt(6.0 / (n_in + n_out))            # Xavier/Glorot uniform init, keeps tanh in its linear range early on
    return rng.uniform(-limit, limit, size=(n_out, n_in)), np.zeros(n_out)


class Autoencoder:
    """d -> h -> d (one hidden layer, tanh), trained by plain-numpy full-batch gradient descent
    with momentum. Small on purpose: a few hundred parameters, so the *inference* side needs
    neither numpy nor a Lambda layer (see model.Autoencoder.error, which mirrors this forward pass)."""

    def __init__(self, n_features: int, n_hidden: int, seed: int = 0) -> None:
        rng = np.random.default_rng(seed)
        self.W1, self.b1 = _init_layer(rng, n_features, n_hidden)
        self.W2, self.b2 = _init_layer(rng, n_hidden, n_features)

    def forward(self, Z: np.ndarray):
        H = np.tanh(Z @ self.W1.T + self.b1)
        R = np.tanh(H @ self.W2.T + self.b2)
        return H, R

    def fit(self, Z: np.ndarray, epochs: int, lr: float, momentum: float = 0.9, batch_size: int = 64,
           weight_decay: float = 1e-4, seed: int = 0, val: "np.ndarray | None" = None,
           patience: int = 30) -> List[float]:
        rng, n = np.random.default_rng(seed), Z.shape[0]
        vW1 = vb1 = vW2 = vb2 = 0.0
        history, best_val, best_state, since_best = [], np.inf, None, 0
        for epoch in range(epochs):
            order = rng.permutation(n)
            for start in range(0, n, batch_size):
                batch = Z[order[start:start + batch_size]]
                m = batch.shape[0]
                H = np.tanh(batch @ self.W1.T + self.b1)
                R = np.tanh(H @ self.W2.T + self.b2)
                dR = (R - batch) * (1 - R ** 2) * (2.0 / m)
                gW2, gb2 = dR.T @ H, dR.sum(0)
                dH = (dR @ self.W2) * (1 - H ** 2)
                gW1, gb1 = dH.T @ batch, dH.sum(0)
                gW1 += weight_decay * self.W1
                gW2 += weight_decay * self.W2
                vW1 = momentum * vW1 - lr * gW1; self.W1 += vW1
                vb1 = momentum * vb1 - lr * gb1; self.b1 += vb1
                vW2 = momentum * vW2 - lr * gW2; self.W2 += vW2
                vb2 = momentum * vb2 - lr * gb2; self.b2 += vb2
            _, R_all = self.forward(Z)
            train_loss = float(np.mean((R_all - Z) ** 2))
            probe = val if val is not None else Z
            _, R_val = self.forward(probe)
            val_loss = float(np.mean((R_val - probe) ** 2))
            history.append(val_loss)
            if val_loss < best_val - 1e-6:
                best_val, best_state, since_best = val_loss, (self.W1.copy(), self.b1.copy(), self.W2.copy(), self.b2.copy()), 0
            else:
                since_best += 1
            if epoch % 20 == 0 or epoch == epochs - 1:
                print(f"  epoch {epoch:4d}  train_mse={train_loss:.5f}  val_mse={val_loss:.5f}")
            if since_best >= patience:
                print(f"  early stop at epoch {epoch} (no improvement for {patience} epochs)")
                break
        if best_state is not None:
            self.W1, self.b1, self.W2, self.b2 = best_state
        return history

    def to_dict(self) -> dict:
        return {"layers": [{"W": self.W1.tolist(), "b": self.b1.tolist(), "act": "tanh"},
                           {"W": self.W2.tolist(), "b": self.b2.tolist(), "act": "tanh"}]}


def train(seed: int, n_sources: int, duration: float, n_hidden: int, epochs: int, lr: float,
         percentile: float, val_split: float = 0.2) -> dict:
    X, kinds = collect_normal_features(seed, n_sources, duration)
    rng = np.random.default_rng(seed)
    order = rng.permutation(len(X))
    n_val = max(1, int(len(X) * val_split))
    val_idx, train_idx = order[:n_val], order[n_val:]

    mean, std = X[train_idx].mean(0), X[train_idx].std(0)
    std[std < 1e-6] = 1e-6
    clip = 10.0
    Z = np.clip((X - mean) / std, -clip, clip)

    ae = Autoencoder(X.shape[1], n_hidden, seed=seed)
    history = ae.fit(Z[train_idx], epochs, lr, val=Z[val_idx], seed=seed)

    _, R = ae.forward(Z)
    errors = ((R - Z) ** 2).mean(1)
    threshold = float(np.percentile(errors[train_idx], percentile))
    val_errors = errors[val_idx]
    false_positive_rate = float(np.mean(val_errors >= threshold))       # normal traffic scored as foreign

    model = {"features": FEATURES, "mean": mean.tolist(), "std": std.tolist(), "threshold": threshold,
             **ae.to_dict(), "meta": {"seed": seed, "n_sources": n_sources, "n_rows": int(len(X)),
                                       "n_hidden": n_hidden, "epochs_run": len(history), "percentile": percentile,
                                       "val_mse": history[-1] if history else None,
                                       "val_false_positive_rate": false_positive_rate,
                                       "kinds": sorted(set(kinds))}}
    model["sha256"] = canonical_sha256(model)
    return model


def main() -> None:
    p = argparse.ArgumentParser(description="train the autoencoder on synthetic 'self' traffic")
    p.add_argument("--out", default="model/model.json")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--sources", type=int, default=400, help="synthetic normal clients to simulate")
    p.add_argument("--duration", type=float, default=1800, help="seconds of traffic per source")
    p.add_argument("--hidden", type=int, default=6)
    p.add_argument("--epochs", type=int, default=300)
    p.add_argument("--lr", type=float, default=0.05)
    p.add_argument("--percentile", type=float, default=99.5, help="threshold = this percentile of normal reconstruction error")
    p.add_argument("--max-fpr", type=float, default=0.03, help="fail the build if held-out normal traffic trips the alarm more than this often")
    a = p.parse_args()

    print(f"simulating {a.sources} normal sources x {a.duration:.0f}s ...")
    model = train(a.seed, a.sources, a.duration, a.hidden, a.epochs, a.lr, a.percentile)
    meta = model["meta"]
    print(f"trained on {meta['n_rows']} feature rows from client kinds: {meta['kinds']}")
    print(f"threshold={model['threshold']:.5f}  held-out false-positive rate={meta['val_false_positive_rate']:.4f}")
    if meta["val_false_positive_rate"] > a.max_fpr:
        raise SystemExit(f"held-out false-positive rate {meta['val_false_positive_rate']:.3f} exceeds --max-fpr {a.max_fpr}; "
                         "retrain with more data, more epochs, or accept a higher threshold percentile")
    import os
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w") as fh:
        json.dump(model, fh, separators=(",", ":"))
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
