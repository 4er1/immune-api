"""Turn the metadata of a source's recent requests into a fixed-size feature vector.

Only *metadata* is used - timing, duration, billed cost, transferred bytes, status code, route -
never the content of a request. The exact same code runs when training (on synthetic traffic) and
inside the Lambda, so there is no train/serve skew.
"""
from __future__ import annotations

import math
from collections import deque
from typing import Deque, List, Optional, Tuple

RING = 64                 # most recent requests remembered per source
WINDOW_SECONDS = 300.0    # requests older than this are ignored
TAU_SHORT = 10.0          # seconds - "is a burst happening now?"
TAU_LONG = 60.0           # seconds - "is this sustained?" and "how fast is the wallet burning?"

FEATURES = [
    "log_rate_ring",      # requests/s over the ring (burst speed)
    "log_rate_10s",       # exponentially weighted request rate, tau = 10 s
    "log_rate_60s",       # same, tau = 60 s (sustained rate)
    "iat_cv",             # coefficient of variation of inter-arrival times (bots are regular)
    "log_mean_dur_ms",
    "log_p95_dur_ms",
    "log_cost_rate_60s",  # milli GB-seconds billed per second: the wallet burn rate
    "log_mean_bytes",     # bytes transferred per request (request + response)
    "bytes_cv",
    "err_ratio",          # share of 4xx/5xx
    "distinct_ratio",     # distinct routes / requests (scanners)
    "top_route_share",    # share of the most requested route
]

# record = (t_seconds, duration_ms, cost_mgbs, bytes, status, route_hash)
Record = Tuple[float, float, float, int, int, int]


def _cv(values: List[float], cap: float = 5.0) -> float:
    n = len(values)
    mean = sum(values) / n
    if mean <= 0:
        return 0.0
    var = sum((v - mean) ** 2 for v in values) / n
    return min(math.sqrt(var) / mean, cap)


class SourceWindow:
    """Rolling state of ONE source (an API key or IP) as seen by ONE container."""

    __slots__ = ("ring", "ema_s", "ema_l", "cost_l", "last_t")

    def __init__(self) -> None:
        self.ring: Deque[Record] = deque(maxlen=RING)
        self.ema_s = self.ema_l = self.cost_l = 0.0
        self.last_t: Optional[float] = None

    def add(self, rec: Record) -> None:
        t, cost = rec[0], rec[2]
        if self.last_t is not None:
            dt = max(0.0, t - self.last_t)
            ds, dl = math.exp(-dt / TAU_SHORT), math.exp(-dt / TAU_LONG)
            self.ema_s *= ds
            self.ema_l *= dl
            self.cost_l *= dl
        self.ema_s += 1.0 / TAU_SHORT
        self.ema_l += 1.0 / TAU_LONG
        self.cost_l += cost / TAU_LONG
        self.last_t = max(t, self.last_t or t)
        self.ring.append(rec)

    def recent(self, now: float) -> List[Record]:
        return [r for r in self.ring if now - r[0] <= WINDOW_SECONDS]

    def features(self, now: float, min_events: int = 2) -> Optional[List[float]]:
        ev = self.recent(now)
        n = len(ev)
        if n < max(2, min_events):
            return None
        span = ev[-1][0] - ev[0][0]
        rate_ring = (n - 1) / max(span, 0.05)
        iats = [b[0] - a[0] for a, b in zip(ev, ev[1:])]
        durs = sorted(r[1] for r in ev)
        p95 = durs[max(0, math.ceil(0.95 * n) - 1)]
        sizes = [float(r[3]) for r in ev]
        routes = [r[5] for r in ev]
        top = max(routes.count(x) for x in set(routes))
        decay = math.exp(-max(0.0, now - (self.last_t or now)) / TAU_LONG)
        decay_s = math.exp(-max(0.0, now - (self.last_t or now)) / TAU_SHORT)
        return [
            math.log1p(rate_ring),
            math.log1p(self.ema_s * decay_s),
            math.log1p(self.ema_l * decay),
            _cv(iats),
            math.log1p(sum(durs) / n),
            math.log1p(p95),
            math.log1p(self.cost_l * decay),
            math.log1p(sum(sizes) / n),
            _cv(sizes),
            sum(1 for r in ev if r[4] >= 400) / n,
            len(set(routes)) / n,
            top / n,
        ]
