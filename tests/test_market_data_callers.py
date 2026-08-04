from __future__ import annotations

import ast
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import auto_grader_daemon as daemon
from backend.market_data import MarketDataResolution, MarketDataResult


ROOT = Path(__file__).resolve().parents[1]


class StubMarketData:
    def __init__(self, result: MarketDataResult) -> None:
        self.result = result
        self.calls: list[tuple[tuple, dict]] = []

    def resolve(self, *args, **kwargs) -> MarketDataResult:
        self.calls.append((args, kwargs))
        return self.result


class StubResolver:
    def __init__(self, resolution: MarketDataResolution) -> None:
        self.resolution = resolution
        self.calls = []

    def resolve(self, request):
        self.calls.append(request)
        return self.resolution


class MarketDataCallerContractTests(unittest.TestCase):
    def test_callers_do_not_own_provider_implementations_or_thin_delegates(self) -> None:
        forbidden_functions = {
            "fetch_binance_prices",
            "fetch_twelve_prices",
            "fetch_finmind_prices",
            "fetch_fugle_prices",
            "fetch_yahoo_prices",
            "fetch_yahoo_recent_daily",
            "fetch_gecko_prices",
            "maybe_patch_twstock_realtime",
            "fetch_prices_cached",
            "fetch_with_dynamic_probing",
            "patch_taiwan_stock_prices_for_dashboard",
            "intraday_coverage_ok",
            "merge_price_data",
        }
        forbidden_imports = {"twstock", "yfinance"}
        forbidden_adapter_names = {
            "BinanceMarketData",
            "FugleMarketData",
            "TwelveDataMarketData",
            "TwstockRealtimePatch",
            "YahooMarketData",
            "YFinanceAdapter",
            "MarketDataResolver",
            "MarketDataProviders",
            "MarketDataPolicyProfile",
            "create_market_data_providers",
            "_create_market_data_providers",
            "DAEMON_MARKET_DATA_POLICY",
            "DASHBOARD_MARKET_DATA_POLICY",
            "FUGLE_DAEMON_POLICY",
            "FUGLE_DASHBOARD_POLICY",
            "FOUR_HOUR_COMPLETE_ROW_POLICY",
        }
        forbidden_endpoints = {
            "BINANCE_EXCHANGE_INFO_URL",
            "FINMIND_DATA_URL",
            "GECKO_OHLCV_URL",
        }

        for relative_path in ["auto_grader_daemon.py", "streamlit/hybrid_dashboard.py"]:
            with self.subTest(caller=relative_path):
                tree = ast.parse(
                    (ROOT / relative_path).read_text(encoding="utf-8"),
                    filename=relative_path,
                )
                function_names = {
                    node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                }
                imported_modules = {
                    alias.name.split(".")[0]
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Import)
                    for alias in node.names
                }
                imported_names = {
                    alias.name
                    for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom)
                    and node.module == "backend.market_data"
                    for alias in node.names
                }
                assigned_names = {
                    target.id
                    for node in ast.walk(tree)
                    if isinstance(node, (ast.Assign, ast.AnnAssign))
                    for target in (
                        node.targets if isinstance(node, ast.Assign) else [node.target]
                    )
                    if isinstance(target, ast.Name)
                }

                self.assertEqual(set(), function_names & forbidden_functions)
                self.assertEqual(set(), imported_modules & forbidden_imports)
                self.assertEqual(set(), imported_names & forbidden_adapter_names)
                self.assertEqual(set(), assigned_names & forbidden_endpoints)
                source = (ROOT / relative_path).read_text(encoding="utf-8")
                self.assertNotIn("cache_slot", source)
                self.assertNotIn("time.time() / ttl", source)
                for provider_attribute in (
                    "_MARKET_DATA.binance",
                    "_MARKET_DATA.yahoo",
                    "_MARKET_DATA.twelve",
                    "_MARKET_DATA.fugle",
                    "_MARKET_DATA.finmind",
                    "_MARKET_DATA.gecko",
                    "_MARKET_DATA.twstock",
                ):
                    self.assertNotIn(provider_attribute, source)

    def test_deleting_shared_module_would_leave_no_provider_implementation_in_production(self) -> None:
        provider_markers = {
            "api.binance.com/api/v3/klines",
            "api.twelvedata.com/time_series",
            "api.fugle.tw/marketdata",
            "api.finmindtrade.com/api/v4/data",
            "api.geckoterminal.com/api/v2/networks",
            "latest_trade_price",
            "multi_level_index",
        }
        shared_source = (ROOT / "backend" / "market_data.py").read_text(encoding="utf-8")
        production_paths = [
            *ROOT.glob("*.py"),
            *(ROOT / "backend").glob("*.py"),
            *(ROOT / "scripts").glob("*.py"),
            *(ROOT / "streamlit").glob("*.py"),
        ]
        production_paths.remove(ROOT / "backend" / "market_data.py")

        missing_from_shared = {
            marker for marker in provider_markers if marker not in shared_source
        }
        leaked_implementations = {
            (str(path.relative_to(ROOT)), marker)
            for path in production_paths
            for marker in provider_markers
            if marker in path.read_text(encoding="utf-8")
        }
        self.assertEqual(set(), missing_from_shared)
        self.assertEqual(set(), leaked_implementations)

    def test_daemon_caller_delegates_request_without_owning_fallback(self) -> None:
        prices = pd.DataFrame([{"close": 101}])
        resolver = StubResolver(
            MarketDataResolution(prices, "YahooFinance", "success")
        )

        with patch.object(daemon, "_MARKET_DATA_RESOLVER", resolver):
            actual = daemon.fetch_prices(
                "AAPL",
                datetime(2026, 1, 1),
                datetime(2026, 1, 2),
                "1d",
                "twelve-key",
                "fugle-key",
            )

        self.assertIs(prices, actual)
        request = resolver.calls[0]
        self.assertEqual(
            ("AAPL", "1d", "twelve-key", "fugle-key"),
            (
                request.ticker,
                request.interval,
                request.twelve_api_key,
                request.fugle_api_key,
            ),
        )
        self.assertEqual(0, request.refresh_intent)



if __name__ == "__main__":
    unittest.main()
