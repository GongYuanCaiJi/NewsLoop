from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import pandas as pd

from backend.market_data import (
    BINANCE_KLINES_URL,
    FINMIND_DATA_URL,
    FUGLE_DASHBOARD_POLICY,
    FUGLE_HISTORICAL_CANDLES_URL,
    GECKO_OHLCV_URL,
    TWELVE_DATA_URL,
    BinanceMarketData,
    FinMindMarketData,
    FugleMarketData,
    GeckoTerminalMarketData,
    MarketDataResolver,
    MarketDataRequest,
    MarketDataResult,
    TwelveDataMarketData,
    TwstockRealtimePatch,
    YahooMarketData,
)
from backend.ticker_utils import get_binance_usdt_symbols, load_binance_usdt_symbols


class FakeResponse:
    def __init__(self, payload) -> None:
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeHttpAdapter:
    def __init__(self, *payloads) -> None:
        self.payloads = list(payloads)
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        return FakeResponse(self.payloads.pop(0))


class FakeTwstockRealtime:
    def __init__(self, payload) -> None:
        self.payload = payload
        self.calls: list[str] = []

    def get(self, symbol: str):
        self.calls.append(symbol)
        return self.payload


class FakeYahooAdapter:
    def __init__(
        self,
        download_frame: pd.DataFrame,
        history_frame: pd.DataFrame | None = None,
        download_error: Exception | None = None,
        history_error: Exception | None = None,
    ) -> None:
        self.download_frame = download_frame
        self.history_frame = history_frame
        self.download_error = download_error
        self.history_error = history_error
        self.download_calls: list[tuple[str, dict]] = []
        self.history_calls: list[tuple[str, dict]] = []

    def download(self, symbol: str, **kwargs) -> pd.DataFrame:
        self.download_calls.append((symbol, kwargs))
        if self.download_error is not None:
            raise self.download_error
        return self.download_frame

    def history(self, symbol: str, **kwargs) -> pd.DataFrame:
        self.history_calls.append((symbol, kwargs))
        if self.history_error is not None:
            raise self.history_error
        if self.history_frame is None:
            raise AssertionError("successful downloads should not require a history probe")
        return self.history_frame


class StubResolver:
    def __init__(self, result: MarketDataResult, calls: list[str], name: str) -> None:
        self.result = result
        self.calls = calls
        self.name = name

    def resolve(self, *_args, **_kwargs) -> MarketDataResult:
        self.calls.append(self.name)
        return self.result


class YahooMarketDataTests(unittest.TestCase):
    def test_daily_resolution_normalizes_yahoo_range_and_candles(self) -> None:
        raw = pd.DataFrame(
            {
                "Open": [99, 100, 101],
                "High": [101, 102, 103],
                "Low": [98, 99, 100],
                "Close": [100, 101, 102],
                "Volume": [10, 20, 30],
            },
            index=pd.DatetimeIndex(
                ["2026-01-01", "2026-01-02", "2026-01-03"],
                name="Date",
            ),
        )
        adapter = FakeYahooAdapter(raw)

        result = YahooMarketData(adapter).resolve(
            "AAPL",
            datetime(2026, 1, 1),
            datetime(2026, 1, 2),
            "1d",
        )

        self.assertEqual("success", result.status)
        self.assertEqual(["date", "open", "high", "low", "close", "volume"], list(result.prices.columns))
        self.assertEqual([100, 101], result.prices["close"].tolist())
        self.assertEqual(
            (
                "AAPL",
                {
                    "interval": "1d",
                    "progress": False,
                    "auto_adjust": False,
                    "group_by": "column",
                    "multi_level_index": False,
                    "start": "2026-01-01",
                    "end": "2026-01-03",
                },
            ),
            adapter.download_calls[0],
        )

    def test_four_hour_resolution_requests_hourly_data_and_aggregates_bars(self) -> None:
        raw = pd.DataFrame(
            {
                "Open": [10, 11, 12, 13, 14],
                "High": [12, 13, 14, 15, 16],
                "Low": [9, 10, 11, 12, 13],
                "Close": [11, 12, 13, 14, 15],
                "Volume": [1, 2, 3, 4, 5],
            },
            index=pd.DatetimeIndex(
                [
                    "2026-01-01 00:00:00",
                    "2026-01-01 01:00:00",
                    "2026-01-01 02:00:00",
                    "2026-01-01 03:00:00",
                    "2026-01-01 04:00:00",
                ],
                name="Datetime",
            ),
        )
        adapter = FakeYahooAdapter(raw)

        result = YahooMarketData(adapter).resolve(
            "AAPL",
            datetime(2026, 1, 1, 0, 0),
            datetime(2026, 1, 1, 4, 0),
            "4h",
        )

        self.assertEqual("success", result.status)
        self.assertEqual([10, 14], result.prices["open"].tolist())
        self.assertEqual([15, 16], result.prices["high"].tolist())
        self.assertEqual([9, 13], result.prices["low"].tolist())
        self.assertEqual([14, 15], result.prices["close"].tolist())
        self.assertEqual([10, 5], result.prices["volume"].tolist())
        self.assertEqual("60m", adapter.download_calls[0][1]["interval"])
        self.assertEqual("2026-01-01 00:00:00", adapter.download_calls[0][1]["start"])
        self.assertEqual("2026-01-01 04:00:00", adapter.download_calls[0][1]["end"])

    def test_four_hour_alignment_warning_is_emitted_once_per_resolver(self) -> None:
        resolver = YahooMarketData(FakeYahooAdapter(pd.DataFrame()))

        with self.assertLogs("backend.market_data", level="INFO") as captured:
            resolver.resolve(
                "AAPL",
                datetime(2026, 1, 1),
                datetime(2026, 1, 2),
                "4h",
                probe_missing=False,
            )
            resolver.resolve(
                "AAPL",
                datetime(2026, 1, 1),
                datetime(2026, 1, 2),
                "4h",
                probe_missing=False,
            )

        warnings = [message for message in captured.output if "UTC boundaries" in message]
        self.assertEqual(1, len(warnings))

    def test_empty_download_distinguishes_missing_range_from_invalid_symbol(self) -> None:
        start = datetime(2026, 1, 1)
        end = datetime(2026, 1, 2)
        existing_symbol = FakeYahooAdapter(pd.DataFrame(), pd.DataFrame({"Close": [100]}))
        invalid_symbol = FakeYahooAdapter(pd.DataFrame(), pd.DataFrame())

        missing_range = YahooMarketData(existing_symbol).resolve("AAPL", start, end, "1d")
        invalid = YahooMarketData(invalid_symbol).resolve("NOT-A-SYMBOL", start, end, "1d")

        self.assertTrue(missing_range.prices.empty)
        self.assertEqual(("no_data_in_range", "history exists but requested range empty"), (missing_range.status, missing_range.detail))
        self.assertTrue(invalid.prices.empty)
        self.assertEqual(("invalid_symbol", "history probe empty"), (invalid.status, invalid.detail))
        self.assertEqual(
            ("AAPL", {"period": "1mo", "interval": "1d", "auto_adjust": False}),
            existing_symbol.history_calls[0],
        )

    def test_download_failure_is_observable_without_losing_original_error(self) -> None:
        failure = TimeoutError("Yahoo timed out")
        adapter = FakeYahooAdapter(pd.DataFrame(), download_error=failure)

        result = YahooMarketData(adapter).resolve(
            "AAPL",
            datetime(2026, 1, 1),
            datetime(2026, 1, 2),
            "1d",
        )

        self.assertTrue(result.prices.empty)
        self.assertEqual(("network_error", "Yahoo timed out"), (result.status, result.detail))
        self.assertIs(failure, result.error)

    def test_history_probe_timeout_is_provider_error_not_invalid_symbol(self) -> None:
        failure = TimeoutError("Yahoo history probe timed out")
        adapter = FakeYahooAdapter(pd.DataFrame(), history_error=failure)

        result = YahooMarketData(adapter).resolve(
            "AAPL",
            datetime(2026, 1, 1),
            datetime(2026, 1, 2),
            "1d",
        )

        self.assertTrue(result.prices.empty)
        self.assertEqual(
            ("network_error", "Yahoo history probe timed out"),
            (result.status, result.detail),
        )
        self.assertIs(failure, result.error)

    def test_single_symbol_multiindex_is_normalized_to_candles(self) -> None:
        columns = pd.MultiIndex.from_product(
            [["Open", "High", "Low", "Close", "Volume"], ["AAPL"]]
        )
        raw = pd.DataFrame(
            [[100, 103, 99, 102, 50]],
            index=pd.DatetimeIndex(["2026-01-01"], name="Date"),
            columns=columns,
        )

        result = YahooMarketData(FakeYahooAdapter(raw)).resolve(
            "AAPL",
            datetime(2026, 1, 1),
            datetime(2026, 1, 1),
            "1d",
        )

        self.assertEqual("success", result.status)
        self.assertEqual(
            {"open": 100, "high": 103, "low": 99, "close": 102, "volume": 50},
            result.prices.iloc[0][["open", "high", "low", "close", "volume"]].to_dict(),
        )

    def test_downloaded_candles_outside_requested_range_report_missing_data(self) -> None:
        raw = pd.DataFrame(
            {
                "Open": [99],
                "High": [101],
                "Low": [98],
                "Close": [100],
                "Volume": [10],
            },
            index=pd.DatetimeIndex(["2025-12-01"], name="Date"),
        )

        result = YahooMarketData(FakeYahooAdapter(raw)).resolve(
            "AAPL",
            datetime(2026, 1, 1),
            datetime(2026, 1, 2),
            "1d",
        )

        self.assertTrue(result.prices.empty)
        self.assertEqual(
            ("no_data_in_range", "downloaded data exists but filtered range empty"),
            (result.status, result.detail),
        )

    def test_empty_download_can_preserve_daemon_no_probe_behavior(self) -> None:
        adapter = FakeYahooAdapter(pd.DataFrame())

        result = YahooMarketData(adapter).resolve(
            "AAPL",
            datetime(2026, 1, 1),
            datetime(2026, 1, 2),
            "1d",
            probe_missing=False,
        )

        self.assertTrue(result.prices.empty)
        self.assertEqual(("no_data_in_range", "download empty"), (result.status, result.detail))
        self.assertEqual([], adapter.history_calls)

    def test_incomplete_yahoo_rows_report_missing_data(self) -> None:
        raw = pd.DataFrame(
            {"Close": [100]},
            index=pd.DatetimeIndex(["2026-01-01"], name="Date"),
        )

        result = YahooMarketData(FakeYahooAdapter(raw)).resolve(
            "AAPL",
            datetime(2026, 1, 1),
            datetime(2026, 1, 2),
            "1d",
        )

        self.assertTrue(result.prices.empty)
        self.assertEqual(
            ("no_data_in_range", "download missing OHLCV columns"),
            (result.status, result.detail),
        )


class ProviderMarketDataTests(unittest.TestCase):
    def test_binance_symbol_loader_ignores_malformed_entries(self) -> None:
        response = FakeResponse(
            {
                "symbols": [
                    {"symbol": "BTCUSDT"},
                    None,
                    "malformed",
                    {"symbol": "ETHUSDC"},
                ]
            }
        )

        with patch("requests.get", return_value=response):
            symbols = load_binance_usdt_symbols()

        self.assertEqual({"BTCUSDT"}, symbols)

    def test_daemon_profile_preserves_us_and_taiwan_provider_orders(self) -> None:
        us_calls: list[str] = []
        yahoo_prices = pd.DataFrame([{"close": 101}])
        us_providers = type(
            "Providers",
            (),
            {
                "twelve": StubResolver(
                    MarketDataResult(pd.DataFrame(), "network_error", "down"),
                    us_calls,
                    "TwelveData",
                ),
                "yahoo": StubResolver(
                    MarketDataResult(yahoo_prices, "success", "ok"),
                    us_calls,
                    "Yahoo",
                ),
            },
        )()
        us_result = MarketDataResolver.for_daemon(
            us_providers, asset_classifier=lambda _ticker: "US Stock"
        ).resolve(
            MarketDataRequest(
                "AAPL",
                datetime(2026, 1, 1),
                datetime(2026, 1, 2),
                "1d",
                twelve_api_key="key",
            )
        )

        tw_calls: list[str] = []
        patched_prices = pd.DataFrame([{"close": 102}])
        tw_providers = type(
            "Providers",
            (),
            {
                "fugle": StubResolver(
                    MarketDataResult(pd.DataFrame(), "auth_missing", "missing"),
                    tw_calls,
                    "Fugle",
                ),
                "yahoo": StubResolver(
                    MarketDataResult(yahoo_prices, "success", "ok"),
                    tw_calls,
                    "Yahoo",
                ),
                "twstock": StubResolver(
                    MarketDataResult(patched_prices, "patched", "ok"),
                    tw_calls,
                    "twstock",
                ),
            },
        )()
        tw_result = MarketDataResolver.for_daemon(
            tw_providers, asset_classifier=lambda _ticker: "Taiwan Stock"
        ).resolve(
            MarketDataRequest(
                "2330",
                datetime(2026, 1, 1),
                datetime(2026, 1, 2),
                "1d",
            )
        )

        self.assertEqual(["TwelveData", "Yahoo"], us_calls)
        self.assertIs(yahoo_prices, us_result.prices)
        self.assertEqual(["Fugle", "Yahoo", "twstock"], tw_calls)
        self.assertIs(patched_prices, tw_result.prices)

    def test_daemon_taiwan_patch_failure_preserves_yahoo_prices(self) -> None:
        yahoo_prices = pd.DataFrame([{"date": "2026-01-01", "close": 101}])
        calls: list[str] = []
        providers = type(
            "Providers",
            (),
            {
                "fugle": StubResolver(
                    MarketDataResult(pd.DataFrame(), "auth_missing", "missing"),
                    calls,
                    "Fugle",
                ),
                "yahoo": StubResolver(
                    MarketDataResult(yahoo_prices, "success", "ok"),
                    calls,
                    "Yahoo",
                ),
                "twstock": StubResolver(
                    MarketDataResult(pd.DataFrame(), "error", "patch unavailable"),
                    calls,
                    "twstock",
                ),
            },
        )()

        result = MarketDataResolver.for_daemon(
            providers, asset_classifier=lambda _ticker: "Taiwan Stock"
        ).resolve(
            MarketDataRequest(
                "2330",
                datetime(2026, 1, 1),
                datetime(2026, 1, 2),
                "1d",
            )
        )

        self.assertEqual(["Fugle", "Yahoo", "twstock"], calls)
        self.assertEqual("success", result.status)
        self.assertIs(yahoo_prices, result.prices)

    def test_dashboard_crypto_waterfall_keeps_twelve_yahoo_binance_order(self) -> None:
        calls: list[str] = []
        empty = MarketDataResult(pd.DataFrame(), "no_data_in_range", "empty")
        binance_prices = pd.DataFrame([{"date": "2026-01-01", "close": 101}])
        providers = type(
            "Providers",
            (),
            {
                "twelve": StubResolver(empty, calls, "TwelveData"),
                "yahoo": StubResolver(empty, calls, "Yahoo"),
                "binance": StubResolver(
                    MarketDataResult(binance_prices, "success", "ok"),
                    calls,
                    "Binance",
                ),
            },
        )()

        result = MarketDataResolver.for_dashboard(
            providers,
            asset_classifier=lambda _ticker: "Crypto",
            dashboard_symbol_normalizer=lambda ticker: ticker,
        ).resolve(
            MarketDataRequest(
                "BTCUSDT",
                datetime(2026, 1, 1),
                datetime(2026, 1, 2),
                "1h",
            )
        )

        self.assertEqual(["TwelveData", "Yahoo", "Binance"], calls)
        self.assertEqual("Binance", result.source)

    def test_dashboard_profile_preserves_smart_route_fallback_and_learning_effects(self) -> None:
        calls: list[str] = []
        empty = MarketDataResult(pd.DataFrame(), "network_error", "unavailable")
        prices = pd.DataFrame([{"date": "2026-01-01", "close": 101}])
        yahoo = StubResolver(empty, calls, "Yahoo")
        twelve = StubResolver(MarketDataResult(prices, "success", "ok"), calls, "TwelveData")
        providers = type(
            "Providers",
            (),
            {
                "yahoo": yahoo,
                "twelve": twelve,
            },
        )()
        resolver = MarketDataResolver.for_dashboard(
            providers,
            asset_classifier=lambda _ticker: "US Stock",
            dashboard_symbol_normalizer=lambda ticker: ticker,
        )

        result = resolver.resolve(
            MarketDataRequest(
                ticker="AAPL",
                start=datetime(2026, 1, 1),
                end=datetime(2026, 1, 2),
                interval="1d",
                twelve_api_key="key",
                optimization_memory={
                    "AAPL_1d": {
                        "counter": 1,
                        "verified_days": 60,
                        "preferred_source": "YahooFinance",
                    }
                },
            )
        )

        self.assertEqual(["Yahoo", "TwelveData"], calls)
        self.assertEqual("TwelveData", result.source)
        self.assertEqual(("miss", "YahooFinance"), (result.effects.route_event, result.effects.route_source))
        self.assertEqual("TwelveData", result.effects.learned_source)
        self.assertIn("[SmartRouting] fallback: waterfall", result.debug_log)

    def test_dashboard_does_not_retry_network_errors_for_large_probe_windows(self) -> None:
        twelve_calls: list[str] = []
        empty = pd.DataFrame()
        failure = TimeoutError("Twelve Data timed out")
        providers = type(
            "Providers",
            (),
            {
                "twelve": StubResolver(
                    MarketDataResult(empty, "network_error", str(failure), failure),
                    twelve_calls,
                    "TwelveData",
                ),
                "yahoo": StubResolver(
                    MarketDataResult(empty, "no_data_in_range", "empty"),
                    [],
                    "Yahoo",
                ),
                "gecko": StubResolver(
                    MarketDataResult(empty, "no_data_in_range", "empty"),
                    [],
                    "Gecko",
                ),
            },
        )()

        result = MarketDataResolver.for_dashboard(
            providers,
            asset_classifier=lambda _ticker: "US Stock",
            dashboard_symbol_normalizer=lambda ticker: ticker,
        ).resolve(
            MarketDataRequest(
                ticker="AAPL",
                start=datetime(2026, 1, 1),
                end=datetime(2026, 3, 3),
                interval="1d",
                twelve_api_key="key",
            )
        )

        self.assertEqual(["TwelveData"], twelve_calls)
        twelve_events = [event for event in result.events if event.source == "TwelveData"]
        self.assertEqual(["network_error"], [event.status for event in twelve_events])

    def test_dashboard_keeps_prices_when_realtime_patch_fails(self) -> None:
        prices = pd.DataFrame(
            [
                {
                    "date": datetime(2026, 1, 1),
                    "open": 100,
                    "high": 101,
                    "low": 99,
                    "close": 100,
                    "volume": 10,
                }
            ]
        )
        failure = RuntimeError("realtime patch unavailable")

        def fail(*_args, **_kwargs):
            raise failure

        providers = type(
            "Providers",
            (),
            {
                "finmind": StubResolver(
                    MarketDataResult(prices, "success", "ok"),
                    [],
                    "FinMind",
                ),
                "twstock": type("Twstock", (), {"resolve": fail})(),
            },
        )()

        result = MarketDataResolver.for_dashboard(
            providers,
            asset_classifier=lambda _ticker: "Taiwan Stock",
            dashboard_symbol_normalizer=lambda ticker: ticker,
        ).resolve(
            MarketDataRequest(
                ticker="2330",
                start=datetime(2026, 1, 1),
                end=datetime(2026, 1, 2),
                interval="1d",
            )
        )

        self.assertEqual("success", result.status)
        pd.testing.assert_frame_equal(prices, result.prices)

    def test_dashboard_profile_caches_same_request_until_ttl_expires(self) -> None:
        calls: list[str] = []
        now = [1000.0]
        prices = pd.DataFrame([{"date": "2026-01-01", "close": 101}])
        twelve = StubResolver(MarketDataResult(prices, "success", "ok"), calls, "TwelveData")
        providers = type("Providers", (), {"twelve": twelve})()
        resolver = MarketDataResolver.for_dashboard(
            providers,
            clock=lambda: now[0],
            cache_ttl_seconds=60,
            asset_classifier=lambda _ticker: "US Stock",
            dashboard_symbol_normalizer=lambda ticker: ticker,
        )
        request = MarketDataRequest(
            ticker="AAPL",
            start=datetime(2026, 1, 1),
            end=datetime(2026, 1, 2),
            interval="1d",
            twelve_api_key="key",
        )

        first = resolver.resolve(request)
        now[0] = 1059.0
        second = resolver.resolve(request)
        now[0] = 1060.0
        expired = resolver.resolve(request)

        self.assertFalse(first.from_cache)
        self.assertTrue(second.from_cache)
        self.assertFalse(expired.from_cache)
        self.assertEqual(["TwelveData", "TwelveData"], calls)

    def test_dashboard_profile_honors_explicit_refresh_intent(self) -> None:
        calls: list[str] = []
        prices = pd.DataFrame([{"date": "2026-01-01", "close": 101}])
        providers = type(
            "Providers",
            (),
            {"twelve": StubResolver(MarketDataResult(prices, "success", "ok"), calls, "TwelveData")},
        )()
        resolver = MarketDataResolver.for_dashboard(
            providers,
            clock=lambda: 1000.0,
            cache_ttl_seconds=60,
            asset_classifier=lambda _ticker: "US Stock",
            dashboard_symbol_normalizer=lambda ticker: ticker,
        )
        request = MarketDataRequest(
            "AAPL",
            datetime(2026, 1, 1),
            datetime(2026, 1, 2),
            "1d",
        )

        first = resolver.resolve(request)
        refreshed = resolver.resolve(
            MarketDataRequest(**{**request.__dict__, "refresh_intent": 1})
        )
        repeated = resolver.resolve(
            MarketDataRequest(**{**request.__dict__, "refresh_intent": 1})
        )

        self.assertFalse(first.from_cache)
        self.assertFalse(refreshed.from_cache)
        self.assertTrue(repeated.from_cache)
        self.assertEqual(["TwelveData", "TwelveData"], calls)

    def test_daemon_profile_never_caches(self) -> None:
        calls: list[str] = []
        prices = pd.DataFrame([{"close": 101}])
        providers = type(
            "Providers",
            (),
            {"twelve": StubResolver(MarketDataResult(prices, "success", "ok"), calls, "TwelveData")},
        )()
        resolver = MarketDataResolver.for_daemon(
            providers,
            clock=lambda: 1000.0,
            asset_classifier=lambda _ticker: "US Stock",
        )
        request = MarketDataRequest(
            "AAPL",
            datetime(2026, 1, 1),
            datetime(2026, 1, 2),
            "1d",
        )

        first = resolver.resolve(request)
        second = resolver.resolve(request)

        self.assertFalse(first.from_cache)
        self.assertFalse(second.from_cache)
        self.assertEqual(["TwelveData", "TwelveData"], calls)

    def test_daemon_profile_owns_fallback_and_returns_failure_taxonomy(self) -> None:
        calls = []
        failure = TimeoutError("binance unavailable")
        binance = StubResolver(MarketDataResult(pd.DataFrame(), "network_error", str(failure), failure), calls, "Binance")
        yahoo_prices = pd.DataFrame([{"close": 101}])
        yahoo = StubResolver(MarketDataResult(yahoo_prices, "success", "ok"), calls, "Yahoo")
        providers = type("Providers", (), {"binance": binance, "yahoo": yahoo})()

        result = MarketDataResolver.for_daemon(
            providers, asset_classifier=lambda _ticker: "Crypto"
        ).resolve(
            MarketDataRequest(
                ticker="BTCUSDT",
                start=datetime(2026, 1, 1),
                end=datetime(2026, 1, 2),
                interval="1h",
            )
        )

        self.assertEqual(["Binance", "Yahoo"], calls)
        self.assertIs(yahoo_prices, result.prices)
        self.assertEqual("YahooFinance", result.source)

    def test_daemon_binance_fallback_canonicalizes_usd_bridge_symbols(self) -> None:
        prices = pd.DataFrame([{"close": 101}])

        class RecordingResolver:
            def __init__(self) -> None:
                self.calls = []

            def resolve(self, *args, **kwargs) -> MarketDataResult:
                self.calls.append((args, kwargs))
                return MarketDataResult(prices, "success", "ok")

        binance = RecordingResolver()
        providers = type("Providers", (), {"binance": binance})()
        response = FakeResponse({"symbols": [{"symbol": "BTCUSDT"}]})

        with patch("requests.get", return_value=response):
            get_binance_usdt_symbols(force=True)
            result = MarketDataResolver.for_daemon(
                providers,
                asset_classifier=lambda _ticker: "Crypto",
            ).resolve(
                MarketDataRequest(
                    ticker="BTC-USD",
                    start=datetime(2026, 1, 1),
                    end=datetime(2026, 1, 2),
                    interval="1h",
                )
            )

        self.assertEqual("Binance", result.source)
        self.assertEqual("BTCUSDT", binance.calls[0][0][0])

    def test_binance_exception_is_classified_instead_of_escaping(self) -> None:
        failure = TimeoutError("Binance timed out")

        def fail(*_args, **_kwargs):
            raise failure

        result = BinanceMarketData(fail).resolve(
            "BTCUSDT",
            datetime(2026, 1, 1),
            datetime(2026, 1, 2),
            "1h",
        )

        self.assertEqual(("network_error", "Binance timed out"), (result.status, result.detail))
        self.assertIs(failure, result.error)

    def test_gecko_provider_errors_are_not_reported_as_missing_data(self) -> None:
        failure = TimeoutError("Gecko timed out")

        def fail(*_args, **_kwargs):
            raise failure

        result = GeckoTerminalMarketData(fail).resolve(
            "pool-address",
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 2, tzinfo=timezone.utc),
        )

        self.assertEqual("network_error", result.status)
        self.assertIs(failure, result.error)

    def test_provider_exception_contract_can_preserve_the_original_error(self) -> None:
        failure = TimeoutError("provider unavailable")
        result = MarketDataResult(
            pd.DataFrame(),
            "network_error",
            str(failure),
            failure,
        )

        with self.assertRaises(TimeoutError) as raised:
            result.prices_or_raise()

        self.assertIs(failure, raised.exception)

    def test_binance_contract_paginates_and_normalizes_candles(self) -> None:
        first_page = [
            [1767225600000, "10", "12", "9", "11", "2", 1767229199999],
        ] * 1000
        second_page = [
            [1767229200000, "11", "13", "10", "12", "3", 1767232799999],
        ]
        request = FakeHttpAdapter(first_page, second_page)

        result = BinanceMarketData(request).resolve(
            "BTCUSDT",
            datetime(2026, 1, 1, 0, 0),
            datetime(2026, 1, 1, 2, 0),
            "1h",
        )

        self.assertEqual(("success", "ok"), (result.status, result.detail))
        self.assertEqual([11.0, 12.0], result.prices["close"].drop_duplicates().tolist())
        self.assertEqual(BINANCE_KLINES_URL, request.calls[0][1])
        self.assertEqual(1767229200000, request.calls[1][2]["params"]["startTime"])

    def test_twelve_data_contract_preserves_status_and_aggregates_four_hours(self) -> None:
        request = FakeHttpAdapter(
            {
                "values": [
                    {
                        "datetime": f"2026-01-01 0{hour}:00:00",
                        "open": str(10 + hour),
                        "high": str(12 + hour),
                        "low": str(9 + hour),
                        "close": str(11 + hour),
                        "volume": str(hour + 1),
                    }
                    for hour in range(4)
                ]
            }
        )

        result = TwelveDataMarketData(request).resolve(
            "AAPL",
            datetime(2026, 1, 1),
            datetime(2026, 1, 2),
            "4h",
            "twelve-key",
        )

        self.assertEqual(("success", "ok"), (result.status, result.detail))
        self.assertEqual(
            {"open": 10.0, "high": 15.0, "low": 9.0, "close": 14.0, "volume": 10.0},
            result.prices.iloc[0][["open", "high", "low", "close", "volume"]].to_dict(),
        )
        self.assertEqual(TWELVE_DATA_URL, request.calls[0][1])
        self.assertEqual("4h", request.calls[0][2]["params"]["interval"])

    def test_twelve_data_contract_classifies_api_errors_without_network(self) -> None:
        request = FakeHttpAdapter(
            {"status": "error", "code": 401, "message": "API key is invalid"}
        )

        result = TwelveDataMarketData(request).resolve(
            "AAPL",
            datetime(2026, 1, 1),
            datetime(2026, 1, 2),
            "1d",
            "bad-key",
        )

        self.assertTrue(result.prices.empty)
        self.assertEqual(("auth_error", "API key is invalid"), (result.status, result.detail))

    def test_fugle_contract_uses_named_dashboard_policy_and_parses_dict_candles(self) -> None:
        request = FakeHttpAdapter(
            {
                "data": {
                    "candles": [
                        {
                            "date": "2026-01-01",
                            "open": "100",
                            "high": "103",
                            "low": "99",
                            "close": "102",
                            "vol": "50",
                        }
                    ]
                }
            }
        )

        result = FugleMarketData(request, policy=FUGLE_DASHBOARD_POLICY).resolve(
            "2330.TW",
            datetime(2026, 1, 1),
            datetime(2026, 1, 2),
            "1d",
            "fugle-key",
        )

        self.assertEqual(("success", "endpoint=historical/candles"), (result.status, result.detail))
        self.assertEqual(102.0, result.prices.iloc[0]["close"])
        self.assertEqual(FUGLE_HISTORICAL_CANDLES_URL.format(symbol="2330"), request.calls[0][1])
        self.assertEqual(1000, request.calls[0][2]["params"]["limit"])

    def test_twstock_realtime_contract_reports_patch_provenance(self) -> None:
        realtime = FakeTwstockRealtime(
            {"success": True, "realtime": {"latest_trade_price": "105.5"}}
        )
        prices = pd.DataFrame(
            [
                {
                    "date": datetime(2026, 1, 1),
                    "open": 100,
                    "high": 101,
                    "low": 99,
                    "close": 100,
                    "volume": 10,
                }
            ]
        )

        result = TwstockRealtimePatch(realtime).resolve(
            prices,
            "2330.TW",
            "1d",
            now=pd.Timestamp("2026-01-03 12:00:00"),
        )

        self.assertEqual("patched", result.status)
        self.assertEqual("symbol=2330; date=2026-01-03 00:00:00", result.detail)
        self.assertEqual([100.0, 105.5], result.prices["close"].tolist())
        self.assertEqual(["2330"], realtime.calls)

    def test_finmind_contract_preserves_free_daily_status_and_candles(self) -> None:
        request = FakeHttpAdapter(
            {
                "data": [
                    {
                        "date": "2026-01-01",
                        "open": 100,
                        "max": 103,
                        "min": 99,
                        "close": 102,
                        "Trading_Volume": 50,
                    }
                ]
            }
        )

        result = FinMindMarketData(request).resolve(
            "2330.TW",
            datetime(2026, 1, 1),
            datetime(2026, 1, 2),
            "1d",
        )

        self.assertEqual(("success", "ok"), (result.status, result.detail))
        self.assertEqual(102.0, result.prices.iloc[0]["close"])
        self.assertEqual(FINMIND_DATA_URL, request.calls[0][1])
        self.assertEqual("2330", request.calls[0][2]["params"]["data_id"])

        unsupported = FinMindMarketData(request).resolve(
            "2330.TW",
            datetime(2026, 1, 1),
            datetime(2026, 1, 2),
            "1h",
        )
        self.assertEqual(
            "interval_not_supported",
            unsupported.status,
        )

    def test_gecko_contract_preserves_network_order_and_provenance(self) -> None:
        timestamp = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
        request = FakeHttpAdapter(
            {"data": {"attributes": {"ohlcv_list": []}}},
            {
                "data": {
                    "attributes": {
                        "ohlcv_list": [[timestamp, 10, 12, 9, 11, 2]]
                    }
                }
            },
        )

        result = GeckoTerminalMarketData(request).resolve(
            "pool-address",
            datetime(2025, 12, 31, tzinfo=timezone.utc),
            datetime(2026, 1, 2, tzinfo=timezone.utc),
        )

        self.assertEqual(
            ("success", "network=eth"),
            (result.status, result.detail),
        )
        self.assertEqual(11.0, result.prices.iloc[0]["close"])
        self.assertEqual(
            GECKO_OHLCV_URL.format(network="solana", pool="pool-address"),
            request.calls[0][1],
        )
        self.assertEqual(
            GECKO_OHLCV_URL.format(network="eth", pool="pool-address"),
            request.calls[1][1],
        )

    def test_gecko_malformed_candle_falls_through_to_next_network(self) -> None:
        timestamp = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
        request = FakeHttpAdapter(
            {"data": {"attributes": {"ohlcv_list": [[timestamp, 10]]}}},
            {
                "data": {
                    "attributes": {
                        "ohlcv_list": [[timestamp, 10, 12, 9, 11, 2]]
                    }
                }
            },
        )

        result = GeckoTerminalMarketData(request).resolve(
            "pool-address",
            datetime(2025, 12, 31, tzinfo=timezone.utc),
            datetime(2026, 1, 2, tzinfo=timezone.utc),
        )

        self.assertEqual(("success", "network=eth"), (result.status, result.detail))
        self.assertEqual(11.0, result.prices.iloc[0]["close"])
        self.assertEqual(2, len(request.calls))


if __name__ == "__main__":
    unittest.main()
