"""Synthetic API traffic: who counts as 'self' (normal client profiles) and who does not (attacks).

EVERYTHING here is an assumption written by the author - real traffic will differ. It exists to
train and to test the pipeline end to end when you have no production logs; with real logs you would
train on those instead (see README, "From synthetic to real").
"""
from __future__ import annotations

import math
import random
import zlib
from typing import Dict, List, Sequence, Tuple

from .features import Record

# route -> (mean duration ms, lognormal sigma, min bytes transferred, max bytes transferred)
CATALOG: Dict[str, Tuple[float, float, int, int]] = {
    "GET /items": (45, 0.35, 3000, 15000),
    "GET /items/{id}": (35, 0.30, 500, 4000),
    "GET /search": (350, 0.50, 2000, 20000),          # expensive
    "POST /orders": (120, 0.40, 800, 3000),
    "GET /me": (25, 0.30, 300, 800),
    "POST /events": (60, 0.35, 300, 1500),
    "GET /sync": (30, 0.30, 200, 2000),
    "POST /auth/login": (90, 0.30, 400, 700),
    "GET /reports": (1200, 0.40, 50000, 500000),      # very expensive
}
DEFAULT_ROUTE = (20.0, 0.3, 100, 400)
Event = Tuple[float, str]


def route_hash(route: str) -> int:
    return zlib.crc32(route.encode()) & 0xFFFFFFFF


def materialize(rng: random.Random, events: Sequence[Event], mem_mb: float = 256.0, err_rate: float = 0.01,
                err_status: int = 404, size_scale: float = 1.0) -> List[Record]:
    out: List[Record] = []
    for t, route in events:
        mean, sigma, lo, hi = CATALOG.get(route, DEFAULT_ROUTE)
        dur = rng.lognormvariate(math.log(mean) - sigma * sigma / 2, sigma)
        size = int(rng.randint(lo, hi) * size_scale)
        status = err_status if rng.random() < err_rate else 200
        out.append((t, dur, mem_mb * dur / 1024.0, size, status, route_hash(route)))
    return out


# --- normal ('self') profiles ---------------------------------------------------------------
def _pick(rng, routes_weights):
    routes, weights = zip(*routes_weights)
    return rng.choices(routes, weights)[0]


def browser_user(rng, D) -> List[Event]:
    mix = [("GET /items", 4), ("GET /items/{id}", 4), ("GET /me", 2), ("GET /search", 1), ("POST /orders", 0.4)]
    t, out = rng.uniform(0, 120), []
    while t < D:
        for _ in range(rng.randint(3, 12)):              # a page load fires several calls at once
            t += rng.expovariate(1 / 0.15)
            out.append((t, _pick(rng, mix)))
        t += rng.lognormvariate(math.log(25), 0.8)       # think time
    return [e for e in out if e[0] < D]


def mobile_app(rng, D) -> List[Event]:
    period, t, out = rng.uniform(15, 60), rng.uniform(0, 60), []
    while t < D:
        out.append((t, "GET /sync" if rng.random() < 0.9 else "POST /events"))
        t += period * rng.uniform(0.8, 1.2)
    return out


def partner_batch(rng, D) -> List[Event]:
    t, out = rng.uniform(0, 120), []
    while t < D:
        rate = rng.uniform(10, 40)                        # legitimate short bursts of 10-40 req/s
        for _ in range(rng.randint(20, 80)):
            t += rng.expovariate(rate)
            out.append((t, "POST /orders"))
        t += rng.uniform(60, 300)
    return [e for e in out if e[0] < D]


def internal_service(rng, D) -> List[Event]:
    rate, t, out = rng.uniform(1, 6), 0.0, []
    mix = [("GET /items", 5), ("GET /items/{id}", 4), ("POST /events", 2)]
    while True:
        t += rng.expovariate(rate)
        if t >= D:
            return out
        out.append((t, _pick(rng, mix)))


def power_user(rng, D) -> List[Event]:
    rate, t, out = rng.uniform(0.3, 1.5), 0.0, []
    while True:
        t += rng.expovariate(rate)
        if t >= D:
            return out
        out.append((t, "GET /search" if rng.random() < 0.7 else "GET /items"))


def admin_user(rng, D) -> List[Event]:
    t, out = rng.uniform(0, 300), []
    while t < D:
        out.append((t, "GET /reports" if rng.random() < 0.3 else "GET /me"))
        t += rng.uniform(60, 600)
    return out


NORMAL = [(browser_user, 45, dict(size_scale=1.0)), (mobile_app, 25, {}), (internal_service, 10, {}),
          (partner_batch, 8, dict(size_scale=8.0)), (power_user, 8, {}), (admin_user, 4, {})]


def normal_source(rng: random.Random, D: float) -> Tuple[str, List[Record]]:
    fn, _, kw = rng.choices(NORMAL, [w for _, w, _ in NORMAL])[0]
    return fn.__name__, materialize(rng, fn(rng, D), err_rate=rng.uniform(0.002, 0.03), **kw)


# --- attacks ('non-self') ------------------------------------------------------------------
def _poisson(rng, D, rate, route_fn) -> List[Event]:
    t, out = 0.0, []
    while True:
        t += rng.expovariate(rate)
        if t >= D:
            return out
        out.append((t, route_fn()))


def _regular(rng, D, rate, jitter, route_fn) -> List[Event]:
    t, out = rng.uniform(0, 1 / rate), []
    while t < D:
        out.append((t, route_fn()))
        t += (1 / rate) * rng.uniform(1 - jitter, 1 + jitter)
    return out


def dow_flood(rng, D) -> List[Record]:
    """Denial-of-Wallet: a torrent of cheap calls (30-500 req/s) that adds up on the bill."""
    route = rng.choice(["GET /items", "GET /search"])
    return materialize(rng, _poisson(rng, D, math.exp(rng.uniform(math.log(30), math.log(500))), lambda: route), err_rate=0.02)


def expensive_lowslow(rng, D) -> List[Record]:
    """Few, regular calls to the most expensive routes: stays under any request-rate limit."""
    route = rng.choice(["GET /reports", "GET /search"])
    return materialize(rng, _regular(rng, D, rng.uniform(1, 3), 0.05, lambda: route), err_rate=0.01)


def scanner(rng, D) -> List[Record]:
    """Forced browsing: many different routes, almost all 404."""
    words = ["admin", ".env", "wp-login.php", "backup", "config", "api", "v1", "debug", "console", "graphql", "old", "test"]
    return materialize(rng, _poisson(rng, D, rng.uniform(3, 15), lambda: "GET /" + rng.choice(words) + str(rng.randint(0, 40))),
                       err_rate=0.92)


def credential_stuffing(rng, D) -> List[Record]:
    return materialize(rng, _poisson(rng, D, rng.uniform(2, 10), lambda: "POST /auth/login"), err_rate=0.96, err_status=401)


def event_injection(rng, D) -> List[Record]:
    """Machine-regular POSTs of oversized event payloads."""
    return materialize(rng, _regular(rng, D, rng.uniform(1, 5), 0.10, lambda: "POST /events"), err_rate=0.02,
                       size_scale=rng.uniform(40, 150))


ATTACKS = {"dow_flood": dow_flood, "expensive_lowslow": expensive_lowslow, "scanner": scanner,
           "credential_stuffing": credential_stuffing, "event_injection": event_injection}


def split_across_containers(rng: random.Random, records: Sequence[Record], k: int) -> List[List[Record]]:
    """Lambda spreads a source's requests over several warm containers; each sees only its share."""
    parts: List[List[Record]] = [[] for _ in range(k)]
    for r in records:
        parts[rng.randrange(k)].append(r)
    return parts
