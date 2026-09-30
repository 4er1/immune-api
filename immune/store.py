"""Where blocks live, so that every container of the function learns about them.

Detection state is deliberately per-container (free, fast). Only *blocks* are shared: one small
item per blocked source in DynamoDB with a TTL, plus a strike counter (the 'immune memory') so a
repeat offender is blocked for longer each time."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class Block:
    until: float
    strikes: int


class MemoryBlockStore:
    """In-process stand-in for DynamoDB (tests, local simulation)."""

    def __init__(self, clock=time.time) -> None:
        self._items: Dict[str, Dict[str, float]] = {}
        self._clock = clock
        self.reads = self.writes = 0

    def get_block(self, src: str) -> Optional[Block]:
        self.reads += 1
        item = self._items.get(src)
        if item is None or item["expires_at"] < self._clock():
            return None
        return Block(item["until"], int(item["strikes"]))

    def put_block(self, src: str, until: float, strikes: int, expires_at: float) -> None:
        self.writes += 1
        self._items[src] = {"until": until, "strikes": strikes, "expires_at": expires_at}


class DynamoBlockStore:
    """Table with partition key `pk` (string) and TTL attribute `expires_at`. Uses the low-level
    client, which the Lambda Python runtime already ships (boto3)."""

    def __init__(self, table: str, client=None) -> None:
        if client is None:
            import boto3
            client = boto3.client("dynamodb")
        self.table, self.client = table, client

    def get_block(self, src: str) -> Optional[Block]:
        resp = self.client.get_item(TableName=self.table, Key={"pk": {"S": "block#" + src}}, ConsistentRead=False)
        item = resp.get("Item")
        if not item:
            return None
        return Block(float(item["until"]["N"]), int(item["strikes"]["N"]))

    def put_block(self, src: str, until: float, strikes: int, expires_at: float) -> None:
        self.client.put_item(TableName=self.table, Item={
            "pk": {"S": "block#" + src}, "until": {"N": repr(round(until, 3))},
            "strikes": {"N": str(int(strikes))}, "expires_at": {"N": str(int(expires_at))}})
