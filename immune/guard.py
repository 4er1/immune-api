"""The middleware: wraps a Lambda handler, watches request METADATA, blocks sources that look foreign.

Per request it costs: a dict lookup, a few float operations, and - at most once per `check_interval`
per source per container - one eventually-consistent DynamoDB read. Writes happen only when a
source is blocked. It never looks at the request body, and any failure of the guard itself is
swallowed (fail-open): the guard must never take the API down.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
import zlib
from collections import OrderedDict
from dataclasses import dataclass
from hashlib import sha256
from typing import Callable, Dict, Optional

from .features import Record, SourceWindow
from .model import Autoencoder
from .store import Block, MemoryBlockStore

_ID_SEGMENT = re.compile(r"^(\d+|[0-9a-f]{8,}|[0-9a-f]{8}-[0-9a-f-]{27})$", re.I)


@dataclass
class GuardConfig:
    mode: str = "enforce"             # "monitor" logs what it WOULD block; "enforce" blocks
    min_events: int = 20              # do not judge a source before it has this many recent requests
    confirm_seconds: float = 5.0      # ...and it must look foreign continuously for this long
    confirm_evals: int = 5            # ...over at least this many consecutive requests
    block_base: float = 60.0          # first block lasts this long; doubles with every strike
    block_max: float = 3600.0
    strike_memory: float = 86400.0    # strikes are forgotten after a day of good behaviour
    check_interval: float = 10.0      # how often a container re-reads a source's block from the store
    max_sources: int = 4096           # per-container memory bound (LRU)
    sns_topic: Optional[str] = None
    namespace: str = "ImmuneGuard"

    @classmethod
    def from_env(cls, env=os.environ) -> "GuardConfig":
        c = cls()
        c.mode = env.get("IMMUNE_MODE", c.mode)
        if c.mode not in ("monitor", "enforce"):
            raise ValueError("IMMUNE_MODE must be 'monitor' or 'enforce'")
        for name, attr, cast in (("IMMUNE_MIN_EVENTS", "min_events", int), ("IMMUNE_CONFIRM_SECONDS", "confirm_seconds", float),
                                 ("IMMUNE_CONFIRM_EVALS", "confirm_evals", int), ("IMMUNE_BLOCK_BASE", "block_base", float),
                                 ("IMMUNE_BLOCK_MAX", "block_max", float), ("IMMUNE_CHECK_INTERVAL", "check_interval", float)):
            if env.get(name):
                setattr(c, attr, cast(env[name]))
        c.sns_topic = env.get("IMMUNE_SNS_TOPIC_ARN") or None
        return c


class _Local:
    __slots__ = ("win", "blocked_until", "strikes", "checked_at", "anom_since", "anom_evals")

    def __init__(self) -> None:
        self.win = SourceWindow()
        self.blocked_until = 0.0
        self.strikes = 0
        self.checked_at = -1e18
        self.anom_since: Optional[float] = None
        self.anom_evals = 0


@dataclass
class Verdict:
    score: Optional[float]      # reconstruction error / threshold (None = not enough evidence yet)
    anomalous: bool
    blocked_now: bool = False


# --- helpers that read only metadata from an API Gateway HTTP API (v2) event -------------------
def source_id(event: dict) -> str:
    key = (event.get("headers") or {}).get("x-api-key")
    if key:
        return "key:" + sha256(key.encode()).hexdigest()[:16]         # never log or store the raw key
    return "ip:" + str(((event.get("requestContext") or {}).get("http") or {}).get("sourceIp", "unknown"))


def route_of(event: dict) -> str:
    http = (event.get("requestContext") or {}).get("http") or {}
    parts = [p for p in str(http.get("path") or event.get("rawPath") or "/").split("/") if p][:2]
    parts = ["{id}" if _ID_SEGMENT.match(p) else p.lower() for p in parts]
    return f"{http.get('method', 'GET').upper()} /" + "/".join(parts)


def route_hash(route: str) -> int:
    return zlib.crc32(route.encode()) & 0xFFFFFFFF                     # stable across processes (unlike hash())


def request_bytes(event: dict) -> int:
    body = event.get("body")
    if body:
        return len(body)
    try:
        return int((event.get("headers") or {}).get("content-length", 0))
    except ValueError:
        return 0


class Guard:
    def __init__(self, model: Autoencoder, store=None, config: Optional[GuardConfig] = None,
                 clock: Callable[[], float] = time.time, emit: Optional[Callable[[dict], None]] = None,
                 notifier: Optional[Callable[[dict], None]] = None) -> None:
        self.model, self.store = model, store
        self.cfg = config or GuardConfig()
        self.clock, self.notifier = clock, notifier
        self.emit = emit or (lambda doc: print(json.dumps(doc, separators=(",", ":"))))
        self._sources: "OrderedDict[str, _Local]" = OrderedDict()
        self.counters = {"anomalies": 0, "blocks": 0, "would_block": 0, "store_errors": 0}

    # -- per-source local state ------------------------------------------------------------
    def _local(self, src: str) -> _Local:
        st = self._sources.get(src)
        if st is None:
            st = self._sources[src] = _Local()
            while len(self._sources) > self.cfg.max_sources:
                self._sources.popitem(last=False)                     # forget the least recently used
        else:
            self._sources.move_to_end(src)
        return st

    # -- before the handler ----------------------------------------------------------------
    def before(self, src: str, now: float) -> Optional[int]:
        """Returns seconds to wait if the request must be rejected, else None."""
        st = self._local(src)
        if self.store is not None and now - st.checked_at >= self.cfg.check_interval:
            st.checked_at = now
            try:
                block = self.store.get_block(src)
            except Exception:
                self.counters["store_errors"] += 1
                block = None
            if block is not None:
                # a store record only exists inside its own TTL window (until + strike_memory), so
                # finding one at all means the strikes are still within memory - keep them.
                st.blocked_until, st.strikes = max(st.blocked_until, block.until), block.strikes
            else:
                # no record on the shared store: either this source was never blocked, or the whole
                # strike-memory window has lapsed. Either way its slate is clean.
                st.strikes = 0
        if st.blocked_until > now and self.cfg.mode == "enforce":
            return max(1, math.ceil(st.blocked_until - now))
        return None

    # -- after the handler -----------------------------------------------------------------
    def after(self, src: str, rec: Record, now: float) -> Verdict:
        st = self._local(src)
        st.win.add(rec)
        feats = st.win.features(now, self.cfg.min_events)
        if feats is None:
            return Verdict(None, False)
        score = self.model.score(feats)
        if score < 1.0:
            st.anom_since, st.anom_evals = None, 0
            return Verdict(score, False)
        self.counters["anomalies"] += 1
        first_seen = st.anom_since is None
        st.anom_since = now if first_seen else st.anom_since
        st.anom_evals += 1
        # confirmation needs >=1 follow-up check after the fault was first seen: a single instant
        # score spike never blocks on its own, however small confirm_seconds/confirm_evals are.
        if not first_seen and now - st.anom_since >= self.cfg.confirm_seconds and st.anom_evals >= self.cfg.confirm_evals:
            self._block(src, st, now, score)
            return Verdict(score, True, blocked_now=True)
        return Verdict(score, True)

    def _block(self, src: str, st: _Local, now: float, score: float) -> None:
        strikes = st.strikes + 1
        duration = min(self.cfg.block_base * 2 ** (strikes - 1), self.cfg.block_max)
        until = now + duration
        doc = {"event": "block" if self.cfg.mode == "enforce" else "would_block", "source": src,
               "score": round(score, 2), "strikes": strikes, "seconds": duration,
               "rate_10s": round(st.win.ema_s, 2), "cost_mgbs_per_s": round(st.win.cost_l, 3)}
        st.anom_since, st.anom_evals = None, 0
        st.win = SourceWindow()                                   # judge the source afresh once the block ends
        if self.cfg.mode != "enforce":
            self.counters["would_block"] += 1
            self._alert(doc, "WouldBlock")
            return
        self.counters["blocks"] += 1
        st.blocked_until, st.strikes = until, strikes
        if self.store is not None:
            try:
                self.store.put_block(src, until, strikes, until + self.cfg.strike_memory)
            except Exception:
                self.counters["store_errors"] += 1
        self._alert(doc, "Blocks")

    def _alert(self, doc: dict, metric: str) -> None:
        """CloudWatch Embedded Metric Format line (becomes a metric AND a searchable log) + optional SNS."""
        self.emit({"_aws": {"Timestamp": int(self.clock() * 1000), "CloudWatchMetrics": [
            {"Namespace": self.cfg.namespace, "Dimensions": [["Mode"]], "Metrics": [{"Name": metric, "Unit": "Count"}]}]},
            "Mode": self.cfg.mode, metric: 1, **doc})
        if self.notifier is not None:
            try:
                self.notifier(doc)
            except Exception:
                self.counters["store_errors"] += 1

    # -- Lambda decorator ------------------------------------------------------------------
    def protect(self, fn: Callable) -> Callable:
        def wrapper(event, context):
            now, src = self.clock(), "unknown"
            try:
                src = source_id(event)
                retry = self.before(src, now)
            except Exception:
                retry = None                                       # fail-open
            if retry is not None:
                return {"statusCode": 429, "headers": {"content-type": "application/json", "retry-after": str(retry)},
                        "body": json.dumps({"message": "temporarily blocked", "retry_after": retry})}
            began, status, out_bytes = time.perf_counter(), 500, 0
            try:
                resp = fn(event, context)
                if isinstance(resp, dict):
                    status = int(resp.get("statusCode", 200))
                    out_bytes = len(resp.get("body") or "")
                else:
                    status = 200
                return resp
            finally:
                try:
                    dur_ms = (time.perf_counter() - began) * 1000
                    mem_mb = float(getattr(context, "memory_limit_in_mb", 128) or 128)
                    cost = mem_mb * dur_ms / 1024.0                # milli GB-seconds billed
                    self.after(src, (now, dur_ms, cost, request_bytes(event) + out_bytes, status,
                                     route_hash(route_of(event))), self.clock())
                except Exception:
                    pass
        wrapper.__wrapped__ = fn
        return wrapper


def build_from_env(model_path: str = "model.json", env=os.environ) -> Guard:
    """Called once per container (module import time)."""
    from .store import DynamoBlockStore
    cfg = GuardConfig.from_env(env)
    table = env.get("IMMUNE_TABLE")
    store = DynamoBlockStore(table) if table else MemoryBlockStore()
    notifier = None
    if cfg.sns_topic:
        import boto3
        sns = boto3.client("sns")
        notifier = lambda doc: sns.publish(TopicArn=cfg.sns_topic, Subject="immune-guard: source blocked",
                                           Message=json.dumps(doc))
    return Guard(Autoencoder.load(model_path), store, cfg, notifier=notifier)
