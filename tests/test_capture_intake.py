from __future__ import annotations

import ast
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.app import app, get_capture_intake_factory
from backend.capture_intake import (
    CaptureIntake,
    NotionWriteError,
)


ROOT = Path(__file__).resolve().parents[1]


class FakeSchemaAdapter:
    def __init__(self, *, available: bool = True):
        self.available = available
        self.bindings = {
            key: {"name": name, "id": f"id-{key}", "type": notion_type}
            for key, name, notion_type in (
                ("title", "Title", "title"),
                ("ticker", "Ticker", "rich_text"),
                ("sentiment", "Sentiment", "select"),
                ("order_type", "Order Type", "select"),
                ("asset_class", "Asset Class", "select"),
                ("sector", "Sector", "select"),
                ("confidence", "Confidence", "number"),
                ("mindset", "Mindset", "number"),
                ("timeframe", "Timeframe", "select"),
                ("entry", "Entry", "number"),
                ("tp", "TP", "number"),
                ("sl", "SL", "number"),
                ("rr", "RR", "number"),
                ("instrument", "Instrument", "select"),
                ("exposure", "Exposure", "number"),
                ("tags", "Tags", "multi_select"),
                ("note", "Note", "rich_text"),
                ("origin_url", "Origin_URL", "url"),
                ("date", "Date", "date"),
                ("result_auto", "Result_Auto", "select"),
                ("track_mode", "Track_Mode", "select"),
                ("system_msg", "System_Msg", "rich_text"),
                ("t2_bars_limit", "T2_Bars_Limit", "number"),
                ("t2_threshold_pct", "T2_Threshold_Pct", "number"),
                ("t2_entry_price", "T2_Entry_Price", "number"),
                ("t2_entry_time", "T2_Entry_Time", "date"),
                ("t2_result", "T2_Result", "select"),
            )
        }

    def check(self) -> dict:
        return {
            "ok": self.available,
            "message": "SchemaError: missing=Ticker" if not self.available else "SchemaOK",
            "snapshot": {"bindings": self.bindings},
        }

    def convert_payload(self, payload: dict, *, title_link: str | None = None) -> dict:
        properties = {}
        for logical_name, value in payload.items():
            binding = self.bindings.get(logical_name)
            if binding:
                properties[binding["name"]] = {"value": value}
        if title_link and "Title" in properties:
            properties["Title"]["link"] = title_link
        return properties

    def default_mapping(self) -> dict[str, str]:
        return {key: binding["name"] for key, binding in self.bindings.items()}


class FakeNotionAdapter:
    def __init__(self, failures: list[NotionWriteError] | None = None):
        self.failures = list(failures or [])
        self.payloads: list[dict] = []

    def create_page(self, payload: dict) -> None:
        self.payloads.append(payload)
        if self.failures:
            raise self.failures.pop(0)


class FakeAssetAdapter:
    def normalize_ticker(self, value: str) -> str:
        return str(value).strip().upper()

    def classify(self, ticker: str) -> str:
        return "US Stock"

    def sector(self, ticker: str, asset_class: str) -> tuple[str | None, str | None]:
        return "Technology", None


class CaptureHTTPContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = FakeSchemaAdapter()
        self.notion = FakeNotionAdapter()
        self.intake = CaptureIntake(
            database_id="fixture-db",
            schema=self.schema,
            notion=self.notion,
            assets=FakeAssetAdapter(),
            now=lambda: datetime(2026, 7, 22, 1, 2, 3, tzinfo=timezone.utc),
        )
        app.dependency_overrides[get_capture_intake_factory] = lambda: (lambda: self.intake)
        self.client = TestClient(app, client=("127.0.0.1", 50000))

    def tearDown(self) -> None:
        app.dependency_overrides.clear()

    @staticmethod
    def legacy_track1_payload() -> dict:
        return {
            "title": "  Earnings beat  ",
            "origin_url": "https://example.com/story",
            "ticker": " aapl ",
            "sentiment": "Bullish",
            "confidence": 80,
            "mindset": 70,
            "tags": ["Earnings", "Earnings", "  Macro  "],
            "note": "watch guidance",
            "custom_date": "2026-07-22T09:30:00+08:00",
            "timeframe": "1d",
            "order_type": "Limit",
            "track_mode": "Track1",
            "instrument": "Stock",
            "exposure": 25,
            "entry": 200,
            "tp": 220,
            "sl": 190,
            "rr": 99,
        }

    def test_valid_extension_payload_preserves_success_and_normalized_write(self) -> None:
        response = self.client.post("/add_news", json=self.legacy_track1_payload())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        self.assertEqual(
            self.notion.payloads,
            [
                {
                    "parent": {"database_id": "fixture-db"},
                    "properties": {
                        "Title": {"value": "Earnings beat", "link": "https://example.com/story"},
                        "Ticker": {"value": "AAPL"},
                        "Sentiment": {"value": "Bullish"},
                        "Order Type": {"value": "Limit"},
                        "Asset Class": {"value": "US Stock"},
                        "Sector": {"value": "Technology"},
                        "Confidence": {"value": 80},
                        "Mindset": {"value": 70},
                        "Timeframe": {"value": "1d"},
                        "Entry": {"value": 200.0},
                        "TP": {"value": 220.0},
                        "SL": {"value": 190.0},
                        "RR": {"value": 2.0},
                        "Instrument": {"value": "Stock"},
                        "Exposure": {"value": 25.0},
                        "Tags": {"value": ["Earnings", "Macro"]},
                        "Note": {"value": "watch guidance"},
                        "Origin_URL": {"value": "https://example.com/story"},
                        "Date": {"value": "2026-07-22T01:30:00+00:00"},
                        "Result_Auto": {"value": "Pending"},
                        "Track_Mode": {"value": "Track1"},
                    },
                }
            ],
        )

    def test_invalid_origin_preserves_400_detail_and_does_not_write(self) -> None:
        payload = self.legacy_track1_payload()
        payload["origin_url"] = "javascript:alert(1)"

        response = self.client.post("/add_news", json=payload)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"detail": "origin_url 必須以 http/https 開頭"})
        self.assertEqual(self.notion.payloads, [])

    def test_invalid_explicit_custom_date_is_rejected_without_write(self) -> None:
        payload = self.legacy_track1_payload()
        payload["custom_date"] = "not-a-date"
        payload["date"] = "2026-07-23T00:00:00Z"

        response = self.client.post("/add_news", json=payload)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json(),
            {"detail": "custom_date 格式錯誤，請使用 ISO 8601"},
        )
        self.assertEqual(self.notion.payloads, [])

    def test_missing_required_field_preserves_422_error_contract(self) -> None:
        payload = self.legacy_track1_payload()
        payload.pop("title")

        response = self.client.post("/add_news", json=payload)

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json(), {"detail": "資料驗證失敗：title"})
        self.assertEqual(self.notion.payloads, [])

    def test_schema_property_validation_retries_with_legacy_downgrade(self) -> None:
        self.notion.failures.append(
            NotionWriteError(
                400,
                'body.properties["Asset Class"] should be a multi_select',
            )
        )

        response = self.client.post("/add_news", json=self.legacy_track1_payload())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        self.assertEqual(len(self.notion.payloads), 2)
        retry_properties = self.notion.payloads[1]["properties"]
        self.assertNotIn("Asset Class", retry_properties)
        self.assertEqual(
            retry_properties["System_Msg"],
            {
                "rich_text": [
                    {
                        "text": {
                            "content": "⚠️ 系統降級紀錄：Asset Class -> "
                            'body.properties["Asset Class"] should be a multi_select'
                        }
                    }
                ]
            },
        )

    def test_legacy_track2_alias_and_entry_fallback_remain_compatible(self) -> None:
        payload = {
            "title": "Breakout",
            "ticker": "aapl",
            "sentiment": "Bullish",
            "confidence": 65,
            "date": "2026-07-22T09:30:00+08:00",
            "timeframe": "4h",
            "track_mode": "sense",
            "entry": 201.5,
            "t2_bars_limit": 12,
            "t2_threshold_pct": 3,
            "t2_entry_time": "2026-07-22T09:30:00+08:00",
        }

        response = self.client.post("/add_news", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        properties = self.notion.payloads[0]["properties"]
        self.assertEqual(properties["Track_Mode"], {"value": "Track2"})
        self.assertEqual(properties["T2_Entry_Price"], {"value": 201.5})
        self.assertEqual(properties["T2_Result"], {"value": "Pending"})
        self.assertEqual(properties["Date"], {"value": "2026-07-22T01:30:00+00:00"})


class CaptureRuntimeErrorHTTPContractTests(unittest.TestCase):
    def tearDown(self) -> None:
        app.dependency_overrides.clear()

    def test_missing_credentials_preserve_json_500_detail(self) -> None:
        app.dependency_overrides.clear()
        client = TestClient(
            app,
            raise_server_exceptions=False,
            client=("127.0.0.1", 50000),
        )
        payload = {
            "title": "Capture without credentials",
            "ticker": "AAPL",
            "sentiment": "Neutral",
            "confidence": 50,
        }

        with patch(
            "backend.app._get_runtime_state",
            side_effect=RuntimeError("Missing NOTION_TOKEN or NEWS_ALPHA_DB_ID (env/config)"),
        ):
            response = client.post("/add_news", json=payload)

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"detail": "內部錯誤，請稍後再試"})


class CaptureArchitectureTests(unittest.TestCase):
    def test_capture_input_uses_canonical_trading_thesis_language(self) -> None:
        source = (ROOT / "backend" / "capture_intake.py").read_text(encoding="utf-8")
        class_names = {
            node.name
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.ClassDef)
        }

        self.assertIn("TradingThesisInput", class_names)
        self.assertNotIn("NewsItem", class_names)

    def test_route_cannot_own_capture_workflow(self) -> None:
        source = (ROOT / "backend" / "app.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        route = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "add_news"
        )
        used_names = {node.id for node in ast.walk(route) if isinstance(node, ast.Name)}
        called_attributes = {
            node.func.attr
            for node in ast.walk(route)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }

        self.assertTrue(
            {
                "normalize_ticker",
                "classify_asset",
                "validate_trade_price_logic",
                "compute_rr",
                "request_with_retry",
                "SchemaManager",
            }.isdisjoint(used_names),
            "the HTTP route must delegate the complete workflow to CaptureIntake",
        )
        self.assertTrue(
            {"check", "convert_payload"}.isdisjoint(called_attributes),
            "the HTTP route must not know schema operations",
        )

    def test_deleting_intake_would_remove_capture_write_and_retry_implementation(self) -> None:
        production_sources = {
            path: path.read_text(encoding="utf-8")
            for path in (ROOT / "backend").glob("*.py")
            if path.name != "capture_intake.py"
        }

        for path, source in production_sources.items():
            with self.subTest(path=path.name):
                string_constants = {
                    node.value
                    for node in ast.walk(ast.parse(source, filename=str(path)))
                    if isinstance(node, ast.Constant) and isinstance(node.value, str)
                }
                self.assertNotIn("https://api.notion.com/v1/pages", string_constants)
                self.assertNotIn("系統降級紀錄", source)

    def test_extension_does_not_know_notion_property_implementation(self) -> None:
        source = (ROOT / "extension" / "popup.js").read_text(encoding="utf-8")

        for forbidden in (
            "v1/pages",
            "System_Msg",
            "CORE_FIELDS",
            "SchemaManager",
            '"properties"',
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)
        self.assertIn("`${apiBase}/add_news`", source)


if __name__ == "__main__":
    unittest.main()
