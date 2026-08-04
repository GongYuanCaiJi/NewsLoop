from __future__ import annotations

import json
import threading
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.schema_manager import SchemaManager


FIXTURES = Path(__file__).with_name("fixtures")


class FakeNotionClient:
    def __init__(self, database: dict):
        self.database = database
        self.requests: list[tuple[str, dict | None]] = []

    def request(self, method: str, _url: str, _headers: dict, payload: dict | None = None) -> dict:
        self.requests.append((method, payload))
        if method == "PATCH":
            for name, schema in ((payload or {}).get("properties") or {}).items():
                notion_type = next(iter(schema))
                self.database["properties"][name] = {
                    "id": f"created-{name}",
                    "type": notion_type,
                }
        return self.database


class SchemaManagerContractTests(unittest.TestCase):
    def make_manager(self, client: FakeNotionClient, root: Path) -> SchemaManager:
        return SchemaManager(
            db_id="fixture-db",
            notion_headers={"Authorization": "Bearer fake"},
            cache_path=str(root / "schema_cache.json"),
            config_path=str(root / "dashboard_config.json"),
            notion_request=client.request,
        )

    def test_check_owns_missing_and_incompatible_error_semantics(self):
        database = json.loads((FIXTURES / "notion_schema.json").read_text(encoding="utf-8"))
        del database["properties"]["Ticker"]
        database["properties"]["Timeframe"]["type"] = "number"
        client = FakeNotionClient(database)

        with tempfile.TemporaryDirectory() as tmp:
            result = self.make_manager(client, Path(tmp)).check(force=True)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "error")
        self.assertEqual(
            result["message"],
            "SchemaError: missing=Ticker; type_mismatch=Timeframe:number!=select",
        )
        self.assertEqual(result["display_names"]["ticker"], "Ticker")
        self.assertEqual([method for method, _ in client.requests], ["GET"])

    def test_autofill_creates_missing_columns_and_verifies_latest_schema(self):
        database = json.loads((FIXTURES / "notion_schema.json").read_text(encoding="utf-8"))
        del database["properties"]["Ticker"]
        del database["properties"]["Asset Class"]
        client = FakeNotionClient(database)

        with tempfile.TemporaryDirectory() as tmp:
            result = self.make_manager(client, Path(tmp)).autofill()

        self.assertEqual(
            result,
            {
                "created": [
                    {"key": "ticker", "name": "Ticker", "type": "rich_text"},
                    {"key": "asset_class", "name": "Asset Class", "type": "multi_select"},
                ],
                "skipped": [],
                "failed": [],
            },
        )
        self.assertEqual([method for method, _ in client.requests], ["GET", "PATCH", "PATCH", "GET"])

    def test_override_and_snapshot_cache_survive_a_new_manager(self):
        database = json.loads((FIXTURES / "notion_schema.json").read_text(encoding="utf-8"))
        database["properties"]["Alternative Ticker"] = {"id": "alt-ticker", "type": "rich_text"}
        client = FakeNotionClient(database)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = self.make_manager(client, root)
            first.set_override("ticker", "alt-ticker")
            first_result = first.check(force=True)

            second = self.make_manager(client, root)
            second.load_cache()
            client.requests.clear()
            cached_result = second.check()

        self.assertEqual(first_result["display_names"]["ticker"], "Alternative Ticker")
        self.assertEqual(cached_result["display_names"]["ticker"], "Alternative Ticker")
        self.assertEqual(client.requests, [])

    def test_cache_from_another_database_is_not_reused(self):
        database = json.loads((FIXTURES / "notion_schema.json").read_text(encoding="utf-8"))
        client = FakeNotionClient(database)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_manager = SchemaManager(
                db_id="old-db",
                notion_headers={"Authorization": "Bearer fake"},
                cache_path=str(root / "schema_cache.json"),
                config_path=str(root / "dashboard_config.json"),
                notion_request=client.request,
            )
            old_manager.check(force=True)

            new_manager = self.make_manager(client, root)
            new_manager.load_cache()
            client.requests.clear()
            result = new_manager.check()

        self.assertEqual([method for method, _ in client.requests], ["GET"])
        self.assertEqual(result["snapshot"]["db_id"], "fixture-db")

    def test_non_finite_cache_timestamp_does_not_break_status(self):
        database = json.loads((FIXTURES / "notion_schema.json").read_text(encoding="utf-8"))
        client = FakeNotionClient(database)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = self.make_manager(client, root)
            manager.check(force=True)
            cache_path = root / "schema_cache.json"
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            cache["fetched_at"] = float("nan")
            cache["next_refresh_after"] = time.time() + 3600
            cache_path.write_text(json.dumps(cache), encoding="utf-8")

            reloaded = self.make_manager(client, root)
            reloaded.load_cache()
            status = reloaded.get_status()

        self.assertEqual(status["meta"]["fetched_at"], "")
        self.assertFalse(status["meta"]["is_refreshing"])

    def test_background_refresh_start_failure_releases_refresh_state(self):
        database = json.loads((FIXTURES / "notion_schema.json").read_text(encoding="utf-8"))
        client = FakeNotionClient(database)

        with tempfile.TemporaryDirectory() as tmp:
            manager = self.make_manager(client, Path(tmp))
            manager.check(force=True)
            manager.ttl_seconds = -1

            with patch("backend.schema_manager.threading.Thread", side_effect=RuntimeError("thread unavailable")):
                stale = manager.sync_with_notion()
                status = manager.get_status()

        self.assertEqual(stale["db_id"], "fixture-db")
        self.assertFalse(status["meta"]["is_refreshing"])
        self.assertIn("thread unavailable", status["meta"]["last_error"])

    def test_concurrent_bound_property_reads_share_database_fetch(self):
        database = json.loads((FIXTURES / "notion_schema.json").read_text(encoding="utf-8"))

        class BlockingNotionClient(FakeNotionClient):
            def __init__(self, value):
                super().__init__(value)
                self.call_count = 0
                self.first_fetch_started = threading.Event()
                self.second_fetch_started = threading.Event()
                self.release_first = threading.Event()

            def request(self, method, url, headers, payload=None):
                self.call_count += 1
                if self.call_count == 2:
                    self.first_fetch_started.set()
                    self.release_first.wait(timeout=2)
                elif self.call_count == 3:
                    self.second_fetch_started.set()
                return super().request(method, url, headers, payload)

        client = BlockingNotionClient(database)

        with tempfile.TemporaryDirectory() as tmp:
            manager = self.make_manager(client, Path(tmp))
            manager.check(force=True)
            manager.invalidate_database_cache()
            results = []
            errors = []

            def read_bound_property():
                try:
                    results.append(manager.get_bound_property("tags"))
                except Exception as exc:
                    errors.append(exc)

            first = threading.Thread(target=read_bound_property)
            second = threading.Thread(target=read_bound_property)
            first.start()
            self.assertTrue(client.first_fetch_started.wait(timeout=2))
            second.start()
            duplicate_fetch_observed = client.second_fetch_started.wait(timeout=1)
            client.release_first.set()
            first.join(timeout=2)
            second.join(timeout=2)

        self.assertFalse(duplicate_fetch_observed)
        self.assertEqual(errors, [])
        self.assertEqual([item["name"] for item in results], ["Tags", "Tags"])

    def test_legacy_cached_snapshot_keeps_actionable_error_message(self):
        database = json.loads((FIXTURES / "notion_schema.json").read_text(encoding="utf-8"))
        client = FakeNotionClient(database)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "schema_cache.json").write_text(
                json.dumps(
                    {
                        "snapshot": {
                            "status": "error",
                            "errors": ["ticker:Ticker missing"],
                            "warnings": [],
                            "bindings": {},
                            "display_names": SchemaManager.default_mapping(),
                        },
                        "fetched_at": time.time(),
                        "manual_overrides": {},
                    }
                ),
                encoding="utf-8",
            )
            manager = self.make_manager(client, root)
            manager.load_cache()
            result = manager.check()

        self.assertFalse(result["ok"])
        self.assertEqual(result["message"], "SchemaError: missing=Ticker")
        self.assertEqual(client.requests, [])

    def test_bound_property_hides_mapping_id_cache_and_type_validation(self):
        database = json.loads((FIXTURES / "notion_schema.json").read_text(encoding="utf-8"))
        database["properties"]["Tags"]["multi_select"] = {
            "options": [{"name": "Macro", "color": "blue"}]
        }
        client = FakeNotionClient(database)

        with tempfile.TemporaryDirectory() as tmp:
            manager = self.make_manager(client, Path(tmp))
            first = manager.get_bound_property("tags")
            second = manager.get_bound_property("tags")

            database["properties"]["Tags"]["type"] = "rich_text"
            incompatible = manager.get_bound_property("tags", force=True)

        self.assertEqual(first["name"], "Tags")
        self.assertEqual(first["schema"]["multi_select"]["options"][0]["name"], "Macro")
        self.assertEqual(second, first)
        self.assertIsNone(incompatible)
        self.assertEqual([method for method, _ in client.requests], ["GET", "GET"])

    def test_autofill_respects_override_instead_of_creating_duplicate_column(self):
        database = json.loads((FIXTURES / "notion_schema.json").read_text(encoding="utf-8"))
        del database["properties"]["Ticker"]
        database["properties"]["Alternative Ticker"] = {"id": "alt-ticker", "type": "rich_text"}
        client = FakeNotionClient(database)

        with tempfile.TemporaryDirectory() as tmp:
            manager = self.make_manager(client, Path(tmp))
            manager.set_override("ticker", "alt-ticker")
            result = manager.autofill()

        self.assertNotIn("ticker", {item["key"] for item in result["created"]})
        self.assertNotIn("Ticker", database["properties"])
        self.assertEqual([method for method, _ in client.requests], ["GET", "GET"])


if __name__ == "__main__":
    unittest.main()
