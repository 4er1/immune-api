import time
import unittest

from immune.store import DynamoBlockStore, MemoryBlockStore


class MemoryBlockStoreTests(unittest.TestCase):
    def test_missing_key_returns_none(self):
        self.assertIsNone(MemoryBlockStore().get_block("nope"))

    def test_put_then_get(self):
        s = MemoryBlockStore(clock=lambda: 100.0)
        s.put_block("a", until=150.0, strikes=2, expires_at=200.0)
        b = s.get_block("a")
        self.assertEqual((b.until, b.strikes), (150.0, 2))

    def test_item_disappears_once_expires_at_has_passed(self):
        t = [100.0]
        s = MemoryBlockStore(clock=lambda: t[0])
        s.put_block("a", until=110.0, strikes=1, expires_at=120.0)
        t[0] = 119.9
        self.assertIsNotNone(s.get_block("a"))
        t[0] = 120.1
        self.assertIsNone(s.get_block("a"))

    def test_overwriting_a_key_replaces_it(self):
        s = MemoryBlockStore(clock=lambda: 0.0)
        s.put_block("a", 10.0, 1, 100.0)
        s.put_block("a", 20.0, 2, 200.0)
        self.assertEqual(s.get_block("a").strikes, 2)


class FakeDynamoClient:
    """Minimal stand-in for the boto3 DynamoDB client methods DynamoBlockStore calls."""

    def __init__(self):
        self.table_data = {}
        self.calls = []

    def put_item(self, TableName, Item):
        self.calls.append(("put_item", TableName))
        self.table_data[(TableName, Item["pk"]["S"])] = Item

    def get_item(self, TableName, Key, ConsistentRead=False):
        self.calls.append(("get_item", TableName, ConsistentRead))
        item = self.table_data.get((TableName, Key["pk"]["S"]))
        return {"Item": item} if item else {}


class DynamoBlockStoreTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeDynamoClient()
        self.store = DynamoBlockStore("immune-blocks", client=self.client)

    def test_missing_item_returns_none(self):
        self.assertIsNone(self.store.get_block("src1"))

    def test_put_then_get_roundtrips_types(self):
        now = time.time()
        self.store.put_block("src1", until=now + 60, strikes=3, expires_at=now + 3600)
        b = self.store.get_block("src1")
        self.assertAlmostEqual(b.until, now + 60, places=2)
        self.assertEqual(b.strikes, 3)
        self.assertIsInstance(b.strikes, int)

    def test_uses_the_expected_key_prefix_and_reads_are_eventually_consistent(self):
        self.store.put_block("abc", 1.0, 1, 2.0)
        item = self.client.table_data[("immune-blocks", "block#abc")]
        self.assertEqual(item["pk"]["S"], "block#abc")
        self.store.get_block("abc")
        self.assertIn(("get_item", "immune-blocks", False), self.client.calls)

    def test_negative_or_fractional_until_survives_the_roundtrip(self):
        self.store.put_block("x", until=1234.567, strikes=1, expires_at=9999.0)
        self.assertAlmostEqual(self.store.get_block("x").until, 1234.567, places=2)


if __name__ == "__main__":
    unittest.main()
