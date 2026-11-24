from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from safesink.mcp_scan import build_manifest
from safesink.mcp_verifier import ManifestError, _build_observers, load_manifest
from safesink.observers import MongoQueryObserver, ObserverError


class ObjectId:
    def __init__(self, value: str) -> None:
        self.value = value

    def __str__(self) -> str:
        return self.value


class _Cursor:
    def __init__(self, documents: list[dict]) -> None:
        self.documents = list(documents)

    def limit(self, value: int) -> "_Cursor":
        self.documents = self.documents[:value]
        return self

    def sort(self, keys: list[tuple[str, int]]) -> "_Cursor":
        return self

    def __iter__(self):
        return iter(self.documents)


class _Collection:
    def __init__(self, documents: list[dict], calls: dict) -> None:
        self._documents = documents
        self._calls = calls

    def find(self, filter, projection, limit: int = 0) -> _Cursor:
        self._calls["find"].append(
            {"filter": filter, "projection": projection, "limit": limit}
        )
        return _Cursor(self._documents)


class _Database:
    def __init__(self, documents: list[dict], calls: dict) -> None:
        self._documents = documents
        self._calls = calls

    def __getitem__(self, name: str) -> _Collection:
        self._calls["collections"].append(name)
        return _Collection(self._documents, self._calls)


def fake_pymongo(documents_by_call: list[list[dict]]):
    module = types.ModuleType("pymongo")
    calls: dict = {
        "uris": [],
        "options": [],
        "databases": [],
        "collections": [],
        "find": [],
        "closed": 0,
        "count": 0,
    }

    class Client:
        def __init__(self, uri: str, **options) -> None:
            calls["uris"].append(uri)
            calls["options"].append(options)

        def __getitem__(self, name: str) -> _Database:
            calls["databases"].append(name)
            index = min(calls["count"], len(documents_by_call) - 1)
            calls["count"] += 1
            return _Database(documents_by_call[index], calls)

        def close(self) -> None:
            calls["closed"] += 1

    module.MongoClient = Client  # type: ignore[attr-defined]
    return module, calls


class MongoObserverTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["TEST_MONGO_URI"] = "mongodb://user:secret@127.0.0.1:27017/shop"
        self.observer = MongoQueryObserver(
            "orders",
            "TEST_MONGO_URI",
            "shop",
            "orders",
            filter={"status": "open"},
            projection={"_id": 1, "status": 1},
            limit=2,
        )

    def tearDown(self) -> None:
        os.environ.pop("TEST_MONGO_URI", None)

    def test_a_snapshot_reads_one_bounded_collection_and_closes(self) -> None:
        module, calls = fake_pymongo([[{"_id": ObjectId("a"), "status": "open"}]])
        with mock.patch.dict(sys.modules, {"pymongo": module}):
            snapshot = self.observer.snapshot()
        self.assertEqual(calls["uris"], ["mongodb://user:secret@127.0.0.1:27017/shop"])
        self.assertEqual(calls["databases"], ["shop"])
        self.assertEqual(calls["collections"], ["orders"])
        self.assertEqual(
            calls["find"],
            [{"filter": {"status": "open"}, "projection": {"_id": 1, "status": 1}, "limit": 2}],
        )
        self.assertEqual(calls["closed"], 1)
        self.assertEqual(snapshot.state["database"], "shop")
        self.assertEqual(snapshot.state["collection"], "orders")
        document = snapshot.state["documents"][0]
        self.assertEqual(document["_id"], {"$type": "ObjectId", "$value": "a"})

    def test_a_removed_document_is_destructive(self) -> None:
        module, _ = fake_pymongo(
            [[{"_id": ObjectId("a")}, {"_id": ObjectId("b")}], [{"_id": ObjectId("a")}]]
        )
        with mock.patch.dict(sys.modules, {"pymongo": module}):
            before = self.observer.snapshot()
            after = self.observer.snapshot()
        delta = self.observer.diff(before, after)
        self.assertTrue(delta.changed)
        self.assertTrue(delta.destructive)

    def test_an_added_document_is_a_change_but_not_destructive(self) -> None:
        module, _ = fake_pymongo([[{"_id": ObjectId("a")}], [{"_id": ObjectId("a")}, {"_id": ObjectId("b")}]])
        with mock.patch.dict(sys.modules, {"pymongo": module}):
            before = self.observer.snapshot()
            after = self.observer.snapshot()
        delta = self.observer.diff(before, after)
        self.assertTrue(delta.changed)
        self.assertFalse(delta.destructive)

    def test_server_order_is_not_part_of_the_state(self) -> None:
        module, _ = fake_pymongo(
            [
                [{"_id": ObjectId("a")}, {"_id": ObjectId("b")}],
                [{"_id": ObjectId("b")}, {"_id": ObjectId("a")}],
            ]
        )
        with mock.patch.dict(sys.modules, {"pymongo": module}):
            before = self.observer.snapshot()
            after = self.observer.snapshot()
        self.assertEqual(before.digest, after.digest)
        self.assertFalse(self.observer.diff(before, after).changed)

    def test_a_missing_environment_variable_fails_closed(self) -> None:
        os.environ.pop("TEST_MONGO_URI")
        module, calls = fake_pymongo([[{"_id": 1}]])
        with mock.patch.dict(sys.modules, {"pymongo": module}):
            with self.assertRaises(ObserverError):
                self.observer.snapshot()
        self.assertEqual(calls["uris"], [])

    def test_a_missing_driver_explains_the_optional_extra(self) -> None:
        with mock.patch.dict(sys.modules, {"pymongo": None}):
            with self.assertRaises(ObserverError) as raised:
                self.observer.snapshot()
        self.assertIn("safesink[mongodb]", str(raised.exception))

    def test_server_side_javascript_is_refused(self) -> None:
        with self.assertRaises(ObserverError) as raised:
            MongoQueryObserver(
                "orders",
                "TEST_MONGO_URI",
                "shop",
                "orders",
                filter={"$where": "this.total > 0"},
            )
        self.assertIn("$where", str(raised.exception))

    def test_a_sort_spec_must_be_one_or_minus_one(self) -> None:
        with self.assertRaises(ObserverError):
            MongoQueryObserver(
                "orders", "TEST_MONGO_URI", "shop", "orders", sort={"created": 2}
            )

    def test_a_limit_must_be_positive(self) -> None:
        with self.assertRaises(ObserverError):
            MongoQueryObserver("orders", "TEST_MONGO_URI", "shop", "orders", limit=0)

    def test_an_in_place_update_is_a_modification_not_a_removal(self) -> None:
        module, _ = fake_pymongo(
            [
                [{"_id": ObjectId("a"), "status": "open", "total": 10}],
                [{"_id": ObjectId("a"), "status": "open", "total": 12}],
            ]
        )
        with mock.patch.dict(sys.modules, {"pymongo": module}):
            before = self.observer.snapshot()
            after = self.observer.snapshot()
        delta = self.observer.diff(before, after)
        self.assertTrue(delta.changed)
        self.assertFalse(delta.destructive)
        self.assertEqual(delta.created, ())
        self.assertEqual(delta.modified, ("ObjectId(a)",))
        self.assertEqual(delta.deleted, ())

    def test_an_update_next_to_a_removal_reports_both(self) -> None:
        module, _ = fake_pymongo(
            [
                [{"_id": ObjectId("a"), "total": 10}, {"_id": ObjectId("b"), "total": 5}],
                [{"_id": ObjectId("a"), "total": 12}],
            ]
        )
        with mock.patch.dict(sys.modules, {"pymongo": module}):
            before = self.observer.snapshot()
            after = self.observer.snapshot()
        delta = self.observer.diff(before, after)
        self.assertTrue(delta.changed)
        self.assertTrue(delta.destructive)
        self.assertEqual(delta.modified, ("ObjectId(a)",))
        self.assertEqual(delta.deleted, ("ObjectId(b)",))

    def test_a_projection_without_id_falls_back_to_whole_documents(self) -> None:
        observer = MongoQueryObserver(
            "orders", "TEST_MONGO_URI", "shop", "orders", projection={"_id": 0, "total": 1}
        )
        module, _ = fake_pymongo([[{"total": 10}], [{"total": 12}]])
        with mock.patch.dict(sys.modules, {"pymongo": module}):
            before = observer.snapshot()
            after = observer.snapshot()
        delta = observer.diff(before, after)
        # Without _id there is no identity to match, so an edit looks exactly like
        # one document removed and another added, and is reported conservatively.
        self.assertTrue(delta.changed)
        self.assertTrue(delta.destructive)


READ_TOOL = {
    "name": "read_orders",
    "annotations": {"readOnlyHint": True},
    "inputSchema": {"type": "object", "properties": {}},
}


class MongoManifestTests(unittest.TestCase):
    def test_build_manifest_references_the_uri_by_environment_variable(self) -> None:
        with tempfile.TemporaryDirectory() as sandbox:
            manifest = build_manifest(
                [READ_TOOL],
                command=["server"],
                observer_root=sandbox,
                mongodb_queries=[("ORDERS_URI", "shop.orders")],
            )
        self.assertEqual(
            [observer["kind"] for observer in manifest["observers"]], ["mongodb-query"]
        )
        observer = manifest["observers"][0]
        self.assertEqual(observer["uriFromEnv"], "ORDERS_URI")
        self.assertEqual(observer["database"], "shop")
        self.assertEqual(observer["collection"], "orders")
        self.assertNotIn("uri", observer)

    def test_build_manifest_rejects_a_target_without_a_collection(self) -> None:
        from safesink.mcp_scan import ScanError

        with tempfile.TemporaryDirectory() as sandbox:
            with self.assertRaises(ScanError):
                build_manifest(
                    [READ_TOOL],
                    command=["server"],
                    observer_root=sandbox,
                    mongodb_queries=[("ORDERS_URI", "orders")],
                )

    def write_manifest(self, root: Path, observer: dict) -> Path:
        path = root / "manifest.json"
        path.write_text(
            json.dumps(
                {
                    "schemaVersion": "effectfence.mcp.v1",
                    "server": {"transport": "stdio", "command": ["server"]},
                    "observers": [observer],
                    "cases": [{"id": "a", "tool": "read_orders"}],
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_a_manifest_round_trip_builds_the_observer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_manifest(
                Path(directory),
                {
                    "id": "orders",
                    "kind": "mongodb-query",
                    "uriFromEnv": "ORDERS_URI",
                    "database": "shop",
                    "collection": "orders",
                    "limit": 50,
                },
            )
            manifest, _ = load_manifest(path)
        observers = _build_observers(manifest["observers"], Path.cwd())
        self.assertIsInstance(observers["orders"], MongoQueryObserver)
        self.assertEqual(observers["orders"].limit, 50)

    def test_a_manifest_requires_the_collection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_manifest(
                Path(directory),
                {
                    "id": "orders",
                    "kind": "mongodb-query",
                    "uriFromEnv": "ORDERS_URI",
                    "database": "shop",
                },
            )
            with self.assertRaises(ManifestError):
                load_manifest(path)

    def test_a_manifest_requires_uri_from_env(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_manifest(
                Path(directory),
                {
                    "id": "orders",
                    "kind": "mongodb-query",
                    "database": "shop",
                    "collection": "orders",
                },
            )
            with self.assertRaises(ManifestError):
                load_manifest(path)


if __name__ == "__main__":
    unittest.main()
