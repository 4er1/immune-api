"""Replay synthetic traffic through a fleet of guarded 'containers' that share one block store.

This is the local, AWS-free way to see the defence work: attackers get blocked, normal clients
(hopefully) do not, and we can count what the attackers cost with and without the guard."""
from __future__ import annotations

import random
import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .features import Record
from .guard import Guard, GuardConfig
from .model import Autoencoder
from .store import MemoryBlockStore
from .traffic import ATTACKS, normal_source

POOL = 16                 # containers available to the function
BLOCKED_COST_MGBS = 0.25  # a rejected request still runs ~1 ms at 256 MB


class SimClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


@dataclass
class SourceResult:
    kind: str
    sent: int = 0
    served: int = 0
    first_block_after: Optional[float] = None
    served_before_block: Optional[int] = None
    cost_total: float = 0.0
    cost_served: float = 0.0


def build_population(seed: int, n_normal: int, per_attack: int, duration: float = 180.0):
    rng = random.Random(seed)
    sources = []                       # (src id, kind, records, containers)
    for i in range(n_normal):
        kind, recs = normal_source(rng, duration)
        if recs:
            sources.append((f"n{i}", "normal:" + kind, recs, rng.choice([1, 1, 1, 2, 3])))
    for kind, fn in ATTACKS.items():
        for i in range(per_attack):
            t0 = rng.uniform(20, 60)
            recs = [(t + t0, *rest) for t, *rest in fn(rng, max(duration - 60, 10))]
            if recs:
                sources.append((f"a-{kind}-{i}", kind, recs, rng.choice([1, 2, 4, 8])))
    return sources


def run(model: Autoencoder, sources, config: Optional[GuardConfig] = None, seed: int = 0) -> Dict[str, SourceResult]:
    rng, clock = random.Random(seed), SimClock()
    store = MemoryBlockStore(clock=clock)
    cfg = config or GuardConfig()
    guards = [Guard(model, store, cfg, clock=clock, emit=lambda d: None) for _ in range(POOL)]
    pools = {sid: rng.sample(range(POOL), k) for sid, _, _, k in sources}
    results = {sid: SourceResult(kind) for sid, kind, _, _ in sources}
    first_t = {sid: recs[0][0] for sid, _, recs, _ in sources}
    events = sorted(((r[0], sid, r) for sid, _, recs, _ in sources for r in recs), key=lambda e: e[0])
    for now, sid, rec in events:
        clock.t = now
        res, g = results[sid], guards[rng.choice(pools[sid])]
        res.sent += 1
        res.cost_total += rec[2]
        if g.before(sid, now) is not None:
            res.cost_served += BLOCKED_COST_MGBS
            continue
        res.served += 1
        res.cost_served += rec[2]
        if g.after(sid, rec, now).blocked_now and res.first_block_after is None:
            res.first_block_after = now - first_t[sid]
            res.served_before_block = res.served
    return results


def summarize(results: Dict[str, SourceResult]) -> dict:
    by_kind: Dict[str, List[SourceResult]] = {}
    for r in results.values():
        by_kind.setdefault(r.kind.split(":")[0], []).append(r)
    out = {}
    for kind, rs in sorted(by_kind.items()):
        blocked = [r for r in rs if r.first_block_after is not None]
        total = sum(r.cost_total for r in rs)
        out[kind] = {
            "sources": len(rs),
            "blocked_share": round(len(blocked) / len(rs), 3),
            "median_seconds_to_block": round(statistics.median(r.first_block_after for r in blocked), 1) if blocked else None,
            "median_requests_before_block": statistics.median(r.served_before_block for r in blocked) if blocked else None,
            "cost_avoided_share": round(1 - sum(r.cost_served for r in rs) / total, 3) if total else 0.0,
            "requests_rejected_share": round(1 - sum(r.served for r in rs) / max(1, sum(r.sent for r in rs)), 4),
        }
    return out


def main() -> None:
    import argparse
    import json
    p = argparse.ArgumentParser(description="replay synthetic traffic through the guard")
    p.add_argument("--model", default="model/model.json")
    p.add_argument("--seed", type=int, default=123)
    p.add_argument("--normal", type=int, default=300)
    p.add_argument("--per-attack", type=int, default=10)
    a = p.parse_args()
    res = run(Autoencoder.load(a.model), build_population(a.seed, a.normal, a.per_attack), seed=a.seed)
    print(json.dumps(summarize(res), indent=2))


if __name__ == "__main__":
    main()
