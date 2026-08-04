from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable, Protocol

import pandas as pd
import requests

from backend.ticker_utils import (
    classify_asset,
    normalize_symbol,
    normalize_ticker_for_storage,
    normalize_ticker_key,
    yahoo_crypto_symbol,
)


LOGGER = logging.getLogger(__name__)

BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
TWELVE_DATA_URL = "https://api.twelvedata.com/time_series"
FUGLE_HISTORICAL_CANDLES_URL = (
    "https://api.fugle.tw/marketdata/v1.0/stock/historical/candles/{symbol}"
)
FINMIND_DATA_URL = "https://api.finmindtrade.com/api/v4/data"
GECKO_OHLCV_URL = (
    "https://api.geckoterminal.com/api/v2/networks/{network}/pools/{pool}/ohlcv/day"
)
GECKO_NETWORK_CANDIDATES = (
    "solana",
    "eth",
    "base",
    "arbitrum",
    "polygon",
    "bsc",
)


class YahooAdapter(Protocol):
    def download(self, symbol: str, **kwargs) -> pd.DataFrame: ...

    def history(self, symbol: str, **kwargs) -> pd.DataFrame: ...


class HttpRequest(Protocol):
    def __call__(self, method: str, url: str, **kwargs): ...


class RealtimeAdapter(Protocol):
    def get(self, symbol: str) -> dict: ...


class YFinanceAdapter:
    """Adapt the yfinance module to the Yahoo market-data seam."""

    def __init__(self, client) -> None:
        self._client = client

    def download(self, symbol: str, **kwargs) -> pd.DataFrame:
        return self._client.download(symbol, **kwargs)

    def history(self, symbol: str, **kwargs) -> pd.DataFrame:
        return self._client.Ticker(symbol).history(**kwargs)


@dataclass(frozen=True)
class MarketDataResult:
    prices: pd.DataFrame
    status: str
    detail: str
    error: Exception | None = None

    def prices_or_raise(self) -> pd.DataFrame:
        """Return caller-compatible prices while preserving provider exceptions."""

        if self.error is not None:
            raise self.error
        return self.prices


@dataclass(frozen=True)
class FourHourPolicy:
    """Name an existing caller's missing-value behavior during 4h aggregation."""

    require_complete_row: bool = False


FOUR_HOUR_OHLC_POLICY = FourHourPolicy()
FOUR_HOUR_COMPLETE_ROW_POLICY = FourHourPolicy(require_complete_row=True)


@dataclass(frozen=True)
class FugleRequestPolicy:
    """Preserve caller-specific Fugle request and warning behavior."""

    request_limit: int | None
    warn_on_four_hour_resample: bool


FUGLE_DAEMON_POLICY = FugleRequestPolicy(
    request_limit=None,
    warn_on_four_hour_resample=True,
)
FUGLE_DASHBOARD_POLICY = FugleRequestPolicy(
    request_limit=1000,
    warn_on_four_hour_resample=False,
)


class BinanceMarketData:
    """Resolve paginated Binance klines into canonical candles."""

    def __init__(self, request: HttpRequest) -> None:
        self._request = request

    def resolve(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        interval: str,
    ) -> MarketDataResult:
        start_utc = pd.to_datetime(start, utc=True, errors="coerce")
        end_utc = pd.to_datetime(end, utc=True, errors="coerce")
        if pd.isna(start_utc) or pd.isna(end_utc):
            raise ValueError(
                f"Invalid datetime range for Binance: start={start!r}, end={end!r}"
            )

        params = {"symbol": symbol, "interval": interval, "limit": 1000}
        start_ms = int(start_utc.timestamp() * 1000)
        end_ms = int(end_utc.timestamp() * 1000)
        rows = []
        try:
            while True:
                params["startTime"] = start_ms
                params["endTime"] = end_ms
                klines = self._request(
                    "GET", BINANCE_KLINES_URL, params=params
                ).json()
                if not isinstance(klines, list) or not klines:
                    break
                for kline in klines:
                    rows.append(
                        {
                            "date": datetime.fromtimestamp(
                                kline[0] / 1000, tz=timezone.utc
                            ),
                            "open": kline[1],
                            "high": kline[2],
                            "low": kline[3],
                            "close": kline[4],
                            "volume": kline[5],
                        }
                    )
                last_close = int(klines[-1][6])
                if last_close >= end_ms or len(klines) < params["limit"]:
                    break
                start_ms = last_close + 1
        except Exception as exc:
            return MarketDataResult(pd.DataFrame(), "network_error", str(exc), exc)

        prices = build_price_frame(rows)
        if prices.empty:
            return MarketDataResult(prices, "no_data_in_range", "klines is empty")
        return MarketDataResult(prices, "success", "ok")


class TwelveDataMarketData:
    """Resolve Twelve Data responses while retaining provider status provenance."""

    def __init__(
        self,
        request: HttpRequest,
        *,
        four_hour_policy: FourHourPolicy = FOUR_HOUR_OHLC_POLICY,
    ) -> None:
        self._request = request
        self._four_hour_policy = four_hour_policy

    def resolve(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        interval: str,
        api_key: str,
    ) -> MarketDataResult:
        if not api_key:
            return MarketDataResult(
                pd.DataFrame(), "auth_missing", "TWELVE_DATA_API_KEY missing"
            )
        provider_interval = {
            "1d": "1day",
            "4h": "4h",
            "1h": "1h",
            "15m": "15min",
            "5m": "5min",
            "1m": "1min",
        }.get(interval, "1day")
        date_format = "%Y-%m-%d" if interval == "1d" else "%Y-%m-%d %H:%M:%S"
        params = {
            "symbol": symbol,
            "interval": provider_interval,
            "start_date": start.strftime(date_format),
            "end_date": end.strftime(date_format),
            "apikey": api_key,
            "format": "JSON",
            "outputsize": 5000,
        }
        try:
            response = self._request("GET", TWELVE_DATA_URL, params=params)
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            if status_code == 429:
                return MarketDataResult(pd.DataFrame(), "rate_limit", "HTTP 429", exc)
            if status_code in {400, 404}:
                return MarketDataResult(
                    pd.DataFrame(), "invalid_symbol", f"HTTP {status_code}", exc
                )
            return MarketDataResult(
                pd.DataFrame(), "http_error", f"HTTP {status_code or 'unknown'}", exc
            )
        except Exception as exc:
            return MarketDataResult(pd.DataFrame(), "network_error", str(exc), exc)

        data = response.json()
        if isinstance(data, dict) and data.get("status") == "error":
            message = str(data.get("message") or "")
            code = str(data.get("code") or "")
            raw = f"{code} {message}".strip().lower()
            if "pro plan" in raw or "available starting with pro" in raw:
                status = "plan_limited"
            elif "api key" in raw or "unauthorized" in raw:
                status = "auth_error"
            elif "symbol" in raw or "invalid" in raw or "not found" in raw:
                status = "invalid_symbol"
            elif "limit" in raw or "quota" in raw or "too many" in raw:
                status = "rate_limit"
            else:
                status = "api_error"
            return MarketDataResult(pd.DataFrame(), status, message or code)

        values = data.get("values") or []
        if not values:
            return MarketDataResult(
                pd.DataFrame(), "no_data_in_range", "values is empty"
            )
        rows = [
            {
                "date": item.get("datetime"),
                "open": item.get("open"),
                "high": item.get("high"),
                "low": item.get("low"),
                "close": item.get("close"),
                "volume": item.get("volume"),
            }
            for item in values
        ]
        prices = normalize_interval_frame(
            build_price_frame(rows),
            interval,
            policy=self._four_hour_policy,
        )
        if prices.empty:
            return MarketDataResult(
                pd.DataFrame(), "no_data_in_range", "rows cleaned to empty"
            )
        return MarketDataResult(prices, "success", "ok")


class FugleMarketData:
    """Resolve Fugle candles under an explicit caller request policy."""

    def __init__(
        self,
        request: HttpRequest,
        *,
        policy: FugleRequestPolicy,
    ) -> None:
        self._request = request
        self._policy = policy

    def resolve(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        interval: str,
        api_key: str,
    ) -> MarketDataResult:
        if not api_key:
            return MarketDataResult(
                pd.DataFrame(), "auth_missing", "FUGLE_API_KEY missing"
            )
        timeframe = {
            "1d": "D",
            "1m": "1",
            "5m": "5",
            "15m": "15",
            "1h": "60",
            "4h": "60",
        }.get(interval)
        if timeframe is None:
            return MarketDataResult(
                pd.DataFrame(), "interval_not_supported", f"interval={interval}"
            )
        if interval == "4h" and self._policy.warn_on_four_hour_resample:
            LOGGER.warning(
                "Fugle 4h requested: using 1h candles and resampling to 4h "
                "(alignment may differ)"
            )

        raw_symbol = str(symbol).replace(".TWO", "").replace(".TW", "").strip()
        params = {
            "timeframe": timeframe,
            "from": start.strftime("%Y-%m-%d"),
            "to": end.strftime("%Y-%m-%d"),
        }
        if self._policy.request_limit is not None:
            params["limit"] = self._policy.request_limit
        headers = {"X-API-KEY": api_key}
        try:
            response = self._request(
                "GET",
                FUGLE_HISTORICAL_CANDLES_URL.format(symbol=raw_symbol),
                params=params,
                headers=headers,
            )
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            if status_code in {401, 403}:
                status, detail = "auth_error", f"HTTP {status_code}"
            elif status_code == 404:
                status, detail = "invalid_symbol", "HTTP 404"
            elif status_code == 429:
                status, detail = "rate_limit", "HTTP 429"
            else:
                status, detail = "http_error", f"HTTP {status_code or 'unknown'}"
            return MarketDataResult(pd.DataFrame(), status, detail, exc)
        except Exception as exc:
            return MarketDataResult(pd.DataFrame(), "network_error", str(exc), exc)

        candles = _extract_fugle_candles(response.json())
        if not candles:
            return MarketDataResult(
                pd.DataFrame(), "no_data_in_range", "candles is empty"
            )
        rows = []
        for item in candles:
            if isinstance(item, (list, tuple)) and len(item) >= 5:
                row = {
                    "date": item[0],
                    "open": item[1],
                    "high": item[2],
                    "low": item[3],
                    "close": item[4],
                    "volume": item[5] if len(item) > 5 else None,
                }
            elif isinstance(item, dict):
                volume = item.get("volume")
                row = {
                    "date": item.get("date") or item.get("datetime") or item.get("time"),
                    "open": item.get("open"),
                    "high": item.get("high"),
                    "low": item.get("low"),
                    "close": item.get("close"),
                    "volume": volume if volume is not None else item.get("vol"),
                }
            else:
                continue
            rows.append(row)

        prices = build_price_frame(rows)
        if prices.empty:
            return MarketDataResult(
                pd.DataFrame(), "no_data_in_range", "rows cleaned to empty"
            )
        if interval == "4h":
            prices = aggregate_four_hour(
                prices, policy=FOUR_HOUR_COMPLETE_ROW_POLICY
            )
            if prices.empty:
                return MarketDataResult(
                    pd.DataFrame(), "no_data_in_range", "4h resample empty"
                )
        return MarketDataResult(prices, "success", "endpoint=historical/candles")


class FinMindMarketData:
    """Resolve FinMind's free daily Taiwan-stock candles."""

    def __init__(self, request: HttpRequest) -> None:
        self._request = request

    def resolve(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        interval: str,
    ) -> MarketDataResult:
        if interval != "1d":
            return MarketDataResult(
                pd.DataFrame(),
                "interval_not_supported",
                f"FinMind free tier only returns daily for symbol={symbol}",
            )
        data_id = (
            str(symbol)
            .replace(".TWO", "")
            .replace(".TW", "")
            .replace(":TPE", "")
            .strip()
        )
        if not data_id.isdigit():
            return MarketDataResult(
                pd.DataFrame(), "invalid_symbol", f"symbol={symbol}"
            )
        params = {
            "dataset": "TaiwanStockPrice",
            "data_id": data_id,
            "start_date": start.strftime("%Y-%m-%d"),
            "end_date": end.strftime("%Y-%m-%d"),
        }
        try:
            response = self._request("GET", FINMIND_DATA_URL, params=params)
        except Exception as exc:
            return MarketDataResult(
                pd.DataFrame(), "network_error", str(exc), exc
            )
        data_rows = response.json().get("data") or []
        if not data_rows:
            return MarketDataResult(
                pd.DataFrame(), "no_data_in_range", "data is empty"
            )
        rows = [
            {
                "date": item.get("date"),
                "open": item.get("open"),
                "high": item.get("max"),
                "low": item.get("min"),
                "close": item.get("close"),
                "volume": item.get("Trading_Volume") or item.get("volume"),
            }
            for item in data_rows
        ]
        prices = build_price_frame(rows)
        if prices.empty:
            return MarketDataResult(
                pd.DataFrame(), "no_data_in_range", "rows cleaned to empty"
            )
        return MarketDataResult(prices, "success", "ok")


class GeckoTerminalMarketData:
    """Resolve pool candles across the dashboard's existing network order."""

    def __init__(self, request: HttpRequest) -> None:
        self._request = request

    def resolve(
        self,
        pool_address: str,
        start: datetime,
        end: datetime,
    ) -> MarketDataResult:
        last_error: Exception | None = None
        for network in GECKO_NETWORK_CANDIDATES:
            url = GECKO_OHLCV_URL.format(
                network=network,
                pool=pool_address,
            )
            try:
                response = self._request("GET", url, params={"aggregate": 1})
            except Exception as exc:
                last_error = exc
                continue
            ohlcv = (
                (response.json().get("data") or {})
                .get("attributes", {})
                .get("ohlcv_list")
                or []
            )
            rows = []
            for entry in ohlcv:
                if not isinstance(entry, (list, tuple)) or len(entry) < 6:
                    continue
                timestamp, open_price, high, low, close, volume = entry[:6]
                try:
                    candle_time = datetime.fromtimestamp(
                        timestamp, tz=timezone.utc
                    )
                except (TypeError, ValueError, OverflowError):
                    continue
                if start <= candle_time <= end:
                    rows.append(
                        {
                            "date": candle_time,
                            "open": open_price,
                            "high": high,
                            "low": low,
                            "close": close,
                            "volume": volume,
                        }
                    )
            prices = build_price_frame(rows)
            if not prices.empty:
                return MarketDataResult(
                    prices,
                    "success",
                    f"network={network}",
                )
        if last_error is not None:
            return MarketDataResult(
                pd.DataFrame(),
                "network_error",
                str(last_error),
                last_error,
            )
        return MarketDataResult(
            pd.DataFrame(),
            "no_data_in_range",
            "all network candidates empty",
        )


class TwstockRealtimePatch:
    """Patch a stale Taiwan-stock frame and report caller-visible provenance."""

    def __init__(self, adapter: RealtimeAdapter | None) -> None:
        self._adapter = adapter

    def resolve(
        self,
        prices: pd.DataFrame,
        ticker: str,
        interval: str,
        *,
        now: pd.Timestamp | None = None,
    ) -> MarketDataResult:
        if self._adapter is None or prices.empty:
            return MarketDataResult(prices, "unchanged", "realtime unavailable or frame empty")
        last_date = pd.to_datetime(prices["date"].iloc[-1], errors="coerce")
        if pd.isna(last_date):
            return MarketDataResult(prices, "unchanged", "last candle date invalid")
        now_naive = now
        if now_naive is None:
            now_naive = pd.Timestamp.now(tz="UTC").tz_convert(None)
        elif now_naive.tzinfo is not None:
            now_naive = now_naive.tz_convert(None)
        if now_naive - last_date < (_interval_delta(interval) * 1.5):
            return MarketDataResult(prices, "unchanged", "last candle is recent")

        symbol = str(ticker).replace(".TWO", "").replace(".TW", "").strip()
        if not symbol.isdigit():
            return MarketDataResult(prices, "unchanged", "symbol is not numeric")
        try:
            payload = self._adapter.get(symbol)
        except Exception as exc:
            return MarketDataResult(prices, "error", str(exc), exc)
        if not payload or not payload.get("success"):
            return MarketDataResult(prices, "no_data", f"symbol={symbol}")

        realtime = payload.get("realtime") or {}
        price = (
            realtime.get("latest_trade_price")
            or realtime.get("best_bid_price")
            or realtime.get("best_ask_price")
        )
        try:
            price = float(price)
        except Exception:
            return MarketDataResult(prices, "invalid_price", f"symbol={symbol}")

        floor = {
            "1d": "D",
            "4h": "4h",
            "1h": "1h",
            "15m": "15min",
            "5m": "5min",
            "1m": "1min",
        }.get(interval, "1min")
        patch_date = now_naive.floor(floor)
        if (pd.to_datetime(prices["date"], errors="coerce") == patch_date).any():
            return MarketDataResult(prices, "unchanged", "patch candle already exists")
        patch = pd.DataFrame(
            [
                {
                    "date": patch_date,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": 0,
                }
            ]
        )
        merged = pd.concat([prices, patch], ignore_index=True)
        merged = (
            merged.drop_duplicates(subset=["date"], keep="first")
            .sort_values("date")
            .reset_index(drop=True)
        )
        return MarketDataResult(
            merged,
            "patched",
            f"symbol={symbol}; date={patch_date}",
        )


class YahooMarketData:
    """Resolve Yahoo candles behind one caller-facing interface."""

    def __init__(
        self,
        adapter: YahooAdapter,
        *,
        symbol_normalizer: Callable[[str], str] | None = None,
    ) -> None:
        self._adapter = adapter
        self._symbol_normalizer = symbol_normalizer or (lambda symbol: symbol)
        self._four_hour_warning_emitted = False
        self._warning_lock = threading.Lock()

    def resolve(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        interval: str,
        *,
        probe_missing: bool = True,
    ) -> MarketDataResult:
        symbol = self._symbol_normalizer(symbol)
        if interval == "4h":
            with self._warning_lock:
                if not self._four_hour_warning_emitted:
                    LOGGER.info(
                        "Yahoo 4h uses 1h resample aligned to UTC boundaries; "
                        "may differ from exchange-native anchors."
                    )
                    self._four_hour_warning_emitted = True
        yahoo_interval = {
            "1d": "1d",
            "4h": "60m",
            "1h": "60m",
            "15m": "15m",
            "5m": "5m",
            "1m": "1m",
        }.get(interval, "1d")
        download_kwargs = {
            "interval": yahoo_interval,
            "progress": False,
            "auto_adjust": False,
            "group_by": "column",
            "multi_level_index": False,
        }
        if interval == "1d":
            download_kwargs["start"] = start.strftime("%Y-%m-%d")
            download_kwargs["end"] = (end + timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            download_kwargs["start"] = start.strftime("%Y-%m-%d %H:%M:%S")
            download_kwargs["end"] = end.strftime("%Y-%m-%d %H:%M:%S")
        try:
            raw = self._adapter.download(symbol, **download_kwargs)
        except Exception as exc:
            return MarketDataResult(pd.DataFrame(), "network_error", str(exc), exc)
        if raw.empty:
            if not probe_missing:
                return MarketDataResult(pd.DataFrame(), "no_data_in_range", "download empty")
            return self._resolve_empty_download(symbol)
        if isinstance(raw.columns, pd.MultiIndex):
            first_level = list(dict.fromkeys(raw.columns.get_level_values(0)))
            second_level = list(dict.fromkeys(raw.columns.get_level_values(1)))
            raw = raw.copy()
            if len(first_level) == 1:
                raw.columns = raw.columns.get_level_values(1)
            elif len(second_level) == 1:
                raw.columns = raw.columns.get_level_values(0)
        frame = raw.reset_index()
        date_column = "Datetime" if "Datetime" in frame.columns else "Date"
        required_columns = [date_column, "Open", "High", "Low", "Close", "Volume"]
        if any(column not in frame.columns for column in required_columns):
            return MarketDataResult(
                pd.DataFrame(),
                "no_data_in_range",
                "download missing OHLCV columns",
            )
        rows = (
            frame[required_columns]
            .rename(
                columns={
                    date_column: "date",
                    "Open": "open",
                    "High": "high",
                    "Low": "low",
                    "Close": "close",
                    "Volume": "volume",
                }
            )
            .to_dict("records")
        )
        prices = build_price_frame(rows)
        if prices.empty:
            return MarketDataResult(pd.DataFrame(), "no_data_in_range", "rows cleaned to empty")
        start_naive = pd.to_datetime(start, utc=True, errors="coerce").tz_convert(None)
        end_naive = pd.to_datetime(end, utc=True, errors="coerce").tz_convert(None)
        prices = prices[(prices["date"] >= start_naive) & (prices["date"] <= end_naive)]
        if interval == "4h":
            prices = aggregate_four_hour(prices)
        if prices.empty:
            return MarketDataResult(
                pd.DataFrame(),
                "no_data_in_range",
                "downloaded data exists but filtered range empty",
            )
        return MarketDataResult(prices.reset_index(drop=True), "success", "ok")

    def _resolve_empty_download(self, symbol: str) -> MarketDataResult:
        try:
            history = self._adapter.history(
                symbol,
                period="1mo",
                interval="1d",
                auto_adjust=False,
            )
        except Exception as exc:
            return MarketDataResult(pd.DataFrame(), "network_error", str(exc), exc)
        if history.empty:
            return MarketDataResult(pd.DataFrame(), "invalid_symbol", "history probe empty")
        return MarketDataResult(
            pd.DataFrame(),
            "no_data_in_range",
            "history exists but requested range empty",
        )


@dataclass(frozen=True)
class MarketDataProviders:
    """Configured provider adapters available at the shared market-data seam."""

    yahoo: YahooMarketData
    binance: BinanceMarketData
    twelve: TwelveDataMarketData
    fugle: FugleMarketData
    finmind: FinMindMarketData
    gecko: GeckoTerminalMarketData
    twstock: TwstockRealtimePatch


def _create_market_data_providers(
    request: HttpRequest,
    *,
    profile: MarketDataPolicyProfile,
) -> MarketDataProviders:
    """Construct every live provider from one named resolution profile."""

    if profile.name == "daemon":
        fugle_policy = FUGLE_DAEMON_POLICY
        four_hour_policy = FOUR_HOUR_COMPLETE_ROW_POLICY
        normalize_yahoo_crypto = False
    elif profile.name == "dashboard":
        fugle_policy = FUGLE_DASHBOARD_POLICY
        four_hour_policy = FOUR_HOUR_OHLC_POLICY
        normalize_yahoo_crypto = True
    else:
        raise ValueError(f"unknown market-data profile: {profile.name}")

    os.environ.setdefault("YF_USE_CURL", "0")
    import yfinance as yf

    try:
        import twstock
    except Exception:
        twstock = None

    yahoo_symbol_normalizer = None
    if normalize_yahoo_crypto:
        from backend.ticker_utils import classify_asset, yahoo_crypto_symbol

        def normalize_yahoo_symbol(symbol: str) -> str:
            if classify_asset(str(symbol or "")) == "Crypto":
                return yahoo_crypto_symbol(symbol)
            return symbol

        yahoo_symbol_normalizer = normalize_yahoo_symbol

    return MarketDataProviders(
        yahoo=YahooMarketData(
            YFinanceAdapter(yf),
            symbol_normalizer=yahoo_symbol_normalizer,
        ),
        binance=BinanceMarketData(request),
        twelve=TwelveDataMarketData(
            request,
            four_hour_policy=four_hour_policy,
        ),
        fugle=FugleMarketData(request, policy=fugle_policy),
        finmind=FinMindMarketData(request),
        gecko=GeckoTerminalMarketData(request),
        twstock=TwstockRealtimePatch(
            twstock.realtime if twstock is not None else None
        ),
    )


@dataclass(frozen=True)
class MarketDataPolicyProfile:
    """Name a caller's complete resolution lifecycle without exposing its waterfall."""

    name: str
    cache_enabled: bool


_DAEMON_MARKET_DATA_PROFILE = MarketDataPolicyProfile("daemon", cache_enabled=False)
_DASHBOARD_MARKET_DATA_PROFILE = MarketDataPolicyProfile("dashboard", cache_enabled=True)


@dataclass(frozen=True)
class MarketDataRequest:
    ticker: str
    start: datetime
    end: datetime
    interval: str
    twelve_api_key: str = ""
    fugle_api_key: str = ""
    refresh_intent: int = 0
    focus_mode_requested: bool = False
    routing_table: dict = field(default_factory=dict, compare=False, repr=False)
    optimization_memory: dict = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class MarketDataEvent:
    source: str
    status: str
    detail: str = ""

    def render(self) -> str:
        suffix = f": {self.detail}" if self.detail else ""
        return f"[{self.source}] {self.status}{suffix}"


@dataclass(frozen=True)
class MarketDataEffects:
    route_ticker: str = ""
    route_event: str = ""
    route_source: str = ""
    learned_source: str = ""
    counter: int = 0
    verified_days: int = 60


@dataclass(frozen=True)
class MarketDataResolution:
    prices: pd.DataFrame
    source: str
    status: str
    detail: str = ""
    events: tuple[MarketDataEvent, ...] = ()
    effects: MarketDataEffects = field(default_factory=MarketDataEffects)
    from_cache: bool = False

    @property
    def debug_log(self) -> str:
        return "\n".join(event.render() for event in self.events)


@dataclass(frozen=True)
class _CachedResolution:
    expires_at: float
    refresh_intent: int
    resolution: MarketDataResolution


class MarketDataResolver:
    """Own provider selection, fallback, probing, cache and missing-data semantics."""

    def __init__(
        self,
        providers: MarketDataProviders,
        *,
        profile: MarketDataPolicyProfile,
        cache_ttl_seconds: int | Callable[[str], int] = 600,
        clock: Callable[[], float] = time.monotonic,
        asset_classifier: Callable[[str], str] = classify_asset,
        dashboard_symbol_normalizer: Callable[[str], str] = normalize_ticker_for_storage,
    ) -> None:
        self._providers = providers
        self._profile = profile
        self._cache_ttl_seconds = (
            cache_ttl_seconds
            if callable(cache_ttl_seconds)
            else lambda _interval: int(cache_ttl_seconds)
        )
        self._clock = clock
        self._asset_classifier = asset_classifier
        self._dashboard_symbol_normalizer = dashboard_symbol_normalizer
        self._cache: dict[tuple, _CachedResolution] = {}
        self._cache_lock = threading.Lock()

    @classmethod
    def for_daemon(cls, providers: MarketDataProviders, **kwargs) -> MarketDataResolver:
        return cls(providers, profile=_DAEMON_MARKET_DATA_PROFILE, **kwargs)

    @classmethod
    def for_dashboard(cls, providers: MarketDataProviders, **kwargs) -> MarketDataResolver:
        return cls(providers, profile=_DASHBOARD_MARKET_DATA_PROFILE, **kwargs)

    def resolve(self, request: MarketDataRequest) -> MarketDataResolution:
        selected_profile = self._profile
        ttl_seconds = max(30, int(self._cache_ttl_seconds(request.interval)))
        cache_key = self._cache_key(request, selected_profile, ttl_seconds)
        now = self._clock()
        if cache_key is not None:
            with self._cache_lock:
                cached = self._cache.get(cache_key)
            if (
                cached is not None
                and cached.refresh_intent == request.refresh_intent
                and now < cached.expires_at
            ):
                return MarketDataResolution(
                    cached.resolution.prices.copy(deep=True),
                    cached.resolution.source,
                    cached.resolution.status,
                    cached.resolution.detail,
                    cached.resolution.events,
                    cached.resolution.effects,
                    from_cache=True,
                )

        if selected_profile == _DAEMON_MARKET_DATA_PROFILE:
            resolved = self._resolve_daemon(request)
        elif selected_profile == _DASHBOARD_MARKET_DATA_PROFILE:
            resolved = self._resolve_dashboard(request)
        else:
            raise ValueError(f"unknown market-data policy: {selected_profile.name}")

        if cache_key is not None:
            with self._cache_lock:
                self._cache[cache_key] = _CachedResolution(
                    expires_at=now + ttl_seconds,
                    refresh_intent=request.refresh_intent,
                    resolution=MarketDataResolution(
                        resolved.prices.copy(deep=True),
                        resolved.source,
                        resolved.status,
                        resolved.detail,
                        resolved.events,
                        resolved.effects,
                    ),
                )
        return resolved

    @staticmethod
    def _cache_key(
        request: MarketDataRequest,
        policy: MarketDataPolicyProfile,
        ttl_seconds: int,
    ) -> tuple | None:
        if not policy.cache_enabled:
            return None
        return (
            policy.name,
            request.ticker,
            request.start,
            request.end,
            request.interval,
            request.twelve_api_key,
            request.fugle_api_key,
            request.focus_mode_requested,
            ttl_seconds,
        )


    def _resolve_daemon(self, request: MarketDataRequest) -> MarketDataResolution:
        asset_type = self._asset_classifier(request.ticker)
        symbol = normalize_symbol(request.ticker)
        if symbol.isdigit() and len(symbol) == 4:
            symbol = f"{symbol}.TW"
        events: list[MarketDataEvent] = []

        def attempt(source: str, resolver: Callable[[], MarketDataResult]) -> MarketDataResult:
            try:
                result = resolver()
            except Exception as exc:
                result = MarketDataResult(pd.DataFrame(), "network_error", str(exc), exc)
            events.append(MarketDataEvent(source, result.status, result.detail))
            return result

        if asset_type == "Crypto":
            binance_symbol = normalize_ticker_for_storage(request.ticker)
            if not binance_symbol.endswith("USDT"):
                binance_symbol = binance_symbol.replace("-", "").replace("_", "").replace("/", "")
            first = attempt(
                "Binance",
                lambda: self._providers.binance.resolve(
                    binance_symbol, request.start, request.end, request.interval
                ),
            )
            if not first.prices.empty:
                return self._success(first, "Binance", events)
            second = attempt(
                "YahooFinance",
                lambda: self._providers.yahoo.resolve(
                    yahoo_crypto_symbol(request.ticker or binance_symbol),
                    request.start,
                    request.end,
                    request.interval,
                    probe_missing=False,
                ),
            )
            return self._result(second, "YahooFinance", events)

        if asset_type == "Taiwan Stock":
            resolved = attempt(
                "Fugle",
                lambda: self._providers.fugle.resolve(
                    symbol,
                    request.start,
                    request.end,
                    request.interval,
                    request.fugle_api_key,
                ),
            )
            if resolved.prices.empty:
                resolved = attempt(
                    "YahooFinance",
                    lambda: self._providers.yahoo.resolve(
                        symbol,
                        request.start,
                        request.end,
                        request.interval,
                        probe_missing=False,
                    ),
                )
            if not resolved.prices.empty:
                patch = attempt(
                    "twstock",
                    lambda: self._providers.twstock.resolve(
                        resolved.prices, symbol, request.interval
                    ),
                )
                if not patch.prices.empty:
                    resolved = MarketDataResult(
                        patch.prices,
                        patch.status,
                        patch.detail,
                        patch.error,
                    )
            source = "Fugle" if len(events) > 0 and events[0].status == "success" else "YahooFinance"
            return self._result(resolved, source, events)

        if asset_type in {"US Stock", "Forex", "Commodity"}:
            first = attempt(
                "TwelveData",
                lambda: self._providers.twelve.resolve(
                    symbol,
                    request.start,
                    request.end,
                    request.interval,
                    request.twelve_api_key,
                ),
            )
            if not first.prices.empty:
                return self._success(first, "TwelveData", events)
            second = attempt(
                "YahooFinance",
                lambda: self._providers.yahoo.resolve(
                    symbol,
                    request.start,
                    request.end,
                    request.interval,
                    probe_missing=False,
                ),
            )
            return self._result(second, "YahooFinance", events)

        resolved = attempt(
            "YahooFinance",
            lambda: self._providers.yahoo.resolve(
                symbol,
                request.start,
                request.end,
                request.interval,
                probe_missing=False,
            ),
        )
        return self._result(resolved, "YahooFinance", events)

    def _resolve_dashboard(self, request: MarketDataRequest) -> MarketDataResolution:
        now = datetime.now(timezone.utc)
        now_ts = pd.Timestamp(now)
        req_start = pd.to_datetime(request.start, errors="coerce", utc=True)
        req_end = pd.to_datetime(request.end, errors="coerce", utc=True)
        if pd.isna(req_start):
            req_start = now_ts - pd.Timedelta(days=60)
        if pd.isna(req_end):
            req_end = now_ts
        if req_end > now_ts:
            req_end = now_ts
        if req_start > req_end:
            req_start, req_end = req_end, req_start
        end = req_end.to_pydatetime()

        asset_type = self._asset_classifier(request.ticker)
        symbol = self._dashboard_symbol_normalizer(request.ticker)
        symbol_stock = f"{symbol}.TW" if symbol.isdigit() and len(symbol) == 4 else symbol
        crypto_canonical = normalize_ticker_key(request.ticker) if asset_type == "Crypto" else symbol
        route_ticker = (
            symbol_stock
            if asset_type in {"Taiwan Stock", "US Stock", "Forex", "Commodity"}
            else crypto_canonical
        )

        memory_key = f"{route_ticker}_{request.interval}"
        persisted_learning = (
            request.optimization_memory.get(memory_key, {})
            if isinstance(request.optimization_memory, dict)
            else {}
        )
        counter = int(persisted_learning.get("counter", 0) or 0) + 1
        verified_days = max(
            1, int(persisted_learning.get("verified_days", 60) or 60)
        )
        requested_days_raw = max(
            1, int(((req_end - req_start).total_seconds() / 86400)) + 1
        )
        max_days_by_interval = {
            "1m": 10,
            "5m": 60,
            "15m": 180,
            "1h": 365,
            "4h": 730,
            "1d": 5000,
        }
        interval_max_days = int(max_days_by_interval.get(request.interval, 365))
        requested_days = min(requested_days_raw, interval_max_days)
        verified_days = min(verified_days, interval_max_days)
        is_probe_round = (counter == 1) or (counter % 30 == 0)
        if request.focus_mode_requested:
            request_windows = [requested_days]
            is_probe_round = False
        elif is_probe_round:
            primary_probe_days = max(requested_days, min(interval_max_days, 60))
            request_windows = [primary_probe_days]
            if primary_probe_days > 60:
                request_windows.append(60)
        else:
            request_windows = [max(verified_days, requested_days)]
        request_windows = sorted({max(1, int(days)) for days in request_windows}, reverse=True)
        start = end - timedelta(days=max(request_windows))
        events = [
            MarketDataEvent(
                "OptimizationMemory",
                "probe_round" if is_probe_round else "normal_round",
                f"counter={counter}; request_days={request_windows}; verified_days={verified_days}",
            )
        ]
        if requested_days_raw > interval_max_days:
            events.append(
                MarketDataEvent(
                    "Guardrail",
                    "truncated",
                    (
                        f"requested_days={requested_days_raw} > max_days={interval_max_days}; "
                        f"effective_days={requested_days}; interval={request.interval}; "
                        "guardrail_truncated=true"
                    ),
                )
            )

        def effects(
            *,
            learned_source: str = "",
            days_used: int | None = None,
            route_event: str = "",
            route_source: str = "",
        ) -> MarketDataEffects:
            return MarketDataEffects(
                route_ticker=route_ticker,
                route_event=route_event,
                route_source=route_source,
                learned_source=learned_source,
                counter=counter,
                verified_days=max(1, int(days_used or verified_days)),
            )

        def safe(source_name: str, fetch: Callable[[], MarketDataResult]) -> MarketDataResult:
            try:
                return fetch()
            except Exception as exc:
                return MarketDataResult(pd.DataFrame(), "network_error", str(exc), exc)

        def probe(
            source_name: str,
            fetch: Callable[[datetime, datetime], MarketDataResult],
        ) -> tuple[MarketDataResult, int]:
            last = MarketDataResult(pd.DataFrame(), "no_data_in_range", "empty")
            last_days = request_windows[-1]
            for index, days in enumerate(request_windows):
                probe_start = end - timedelta(days=days)
                last = safe(source_name, lambda: fetch(probe_start, end))
                last_days = days
                if not last.prices.empty:
                    return last, days
                error_response = getattr(last.error, "response", None)
                range_limit_error = getattr(error_response, "status_code", None) == 400
                if index == 0 and len(request_windows) > 1:
                    if last.status != "no_data_in_range" and not range_limit_error:
                        break
                    events.append(
                        MarketDataEvent(
                            source_name,
                            "degrade",
                            (
                                "Requested range too large (HTTP 400/Empty), "
                                f"retrying with safe limit ({request_windows[1]}d)..."
                            ),
                        )
                    )
            return last, last_days

        def log_result(source_name: str, result: MarketDataResult, symbol_used: str) -> None:
            detail = f"symbol={symbol_used}"
            if result.detail:
                detail += f"; {result.detail}"
            events.append(MarketDataEvent(source_name, result.status, detail))

        def patch_tw(prices: pd.DataFrame, raw_digits: str) -> pd.DataFrame:
            patch = safe(
                "twstock",
                lambda: self._providers.twstock.resolve(
                    prices, raw_digits, request.interval
                ),
            )
            if patch.status in {"error", "no_data", "invalid_price", "patched"}:
                events.append(MarketDataEvent("twstock", patch.status, patch.detail))
            if not patch.prices.empty or prices.empty:
                return patch.prices
            return prices

        def success(
            result: MarketDataResult,
            source_name: str,
            days_used: int,
            *,
            route_event: str = "",
            route_source: str = "",
        ) -> MarketDataResolution:
            return self._success(
                result,
                source_name,
                events,
                effects(
                    learned_source=source_name,
                    days_used=days_used,
                    route_event=route_event,
                    route_source=route_source,
                ),
            )

        preferred_source = (
            str(persisted_learning.get("preferred_source") or "")
            or (
                request.routing_table.get(memory_key, "")
                if isinstance(request.routing_table, dict)
                else ""
            )
        )
        if preferred_source:
            events.append(MarketDataEvent("SmartRouting", "prefer", preferred_source))
            try:
                fast = self._dashboard_fast_track(
                    preferred_source,
                    request,
                    asset_type,
                    symbol,
                    symbol_stock,
                    start,
                    end,
                    probe,
                    log_result,
                    patch_tw,
                )
            except Exception as exc:
                events.append(MarketDataEvent("SmartRouting", "error", str(exc)))
                fast = None
            if fast is not None:
                fast_result, fast_source, fast_days = fast
                events.append(MarketDataEvent("SmartRouting", "Hit", fast_source))
                return success(
                    fast_result,
                    fast_source,
                    fast_days,
                    route_event="hit",
                    route_source=fast_source,
                )
            events.append(MarketDataEvent("SmartRouting", "fallback", "waterfall"))
            route_event = "miss"
            route_source = preferred_source
        else:
            route_event = "cold"
            route_source = ""

        def complete(result: MarketDataResult, source_name: str, days_used: int):
            return success(
                result,
                source_name,
                days_used,
                route_event=route_event,
                route_source=route_source,
            )

        source_steps: list[str] = []
        if asset_type in {"Taiwan Stock", "US Stock", "Forex", "Commodity"}:
            if asset_type == "Taiwan Stock":
                raw_digits = symbol_stock.replace(".TWO", "").replace(".TW", "")
                if request.interval == "1d":
                    source_steps.append("FinMind")
                    result, days = probe(
                        "FinMind",
                        lambda scan_start, scan_end: self._providers.finmind.resolve(
                            raw_digits, scan_start, scan_end, request.interval
                        ),
                    )
                    log_result("FinMind", result, raw_digits)
                    if not result.prices.empty:
                        patched = MarketDataResult(
                            patch_tw(result.prices, raw_digits),
                            result.status,
                            result.detail,
                            result.error,
                        )
                        return complete(patched, "FinMind", days)
                else:
                    source_steps.append("Fugle")
                    fugle, fugle_days = probe(
                        "Fugle",
                        lambda scan_start, scan_end: self._providers.fugle.resolve(
                            raw_digits,
                            scan_start,
                            scan_end,
                            request.interval,
                            request.fugle_api_key,
                        ),
                    )
                    fugle_prices = pd.DataFrame()
                    if not fugle.prices.empty:
                        fugle_prices = patch_tw(
                            normalize_interval_frame(fugle.prices, request.interval),
                            raw_digits,
                        )
                        if _intraday_coverage_ok(fugle_prices, request.interval):
                            log_result("Fugle", fugle, raw_digits)
                            return complete(
                                MarketDataResult(fugle_prices, fugle.status, fugle.detail, fugle.error),
                                "Fugle",
                                fugle_days,
                            )
                        events.append(
                            MarketDataEvent(
                                "Fugle",
                                "insufficient_history",
                                f"symbol={raw_digits}; rows={len(fugle_prices)}",
                            )
                        )
                    else:
                        log_result("Fugle", fugle, raw_digits)

                    source_steps.append("YahooFinance")
                    yahoo, yahoo_days = probe(
                        "YahooFinance",
                        lambda scan_start, scan_end: self._providers.yahoo.resolve(
                            f"{raw_digits}.TW",
                            scan_start,
                            scan_end,
                            request.interval,
                        ),
                    )
                    log_result("YahooFinance", yahoo, f"{raw_digits}.TW")
                    if not yahoo.prices.empty:
                        prices = normalize_interval_frame(yahoo.prices, request.interval)
                        prices = _merge_price_data(prices, fugle_prices)
                        prices = patch_tw(prices, raw_digits)
                        return complete(
                            MarketDataResult(prices, yahoo.status, yahoo.detail, yahoo.error),
                            "YahooFinance",
                            yahoo_days,
                        )

                    source_steps.append("TwelveData")
                    for candidate in (f"{raw_digits}.TW", raw_digits):
                        twelve, twelve_days = probe(
                            "TwelveData",
                            lambda scan_start, scan_end, candidate=candidate: self._providers.twelve.resolve(
                                candidate,
                                scan_start,
                                scan_end,
                                request.interval,
                                request.twelve_api_key,
                            ),
                        )
                        log_result("TwelveData", twelve, candidate)
                        if not twelve.prices.empty:
                            prices = normalize_interval_frame(twelve.prices, request.interval)
                            prices = _merge_price_data(prices, fugle_prices)
                            prices = patch_tw(prices, raw_digits)
                            return complete(
                                MarketDataResult(prices, twelve.status, twelve.detail, twelve.error),
                                "TwelveData",
                                twelve_days,
                            )
                    if not fugle_prices.empty:
                        events.append(
                            MarketDataEvent(
                                "Fugle", "fallback_used", f"symbol={raw_digits}"
                            )
                        )
                        return complete(
                            MarketDataResult(fugle_prices, "success", fugle.detail),
                            "Fugle",
                            fugle_days,
                        )
                return MarketDataResolution(
                    pd.DataFrame(),
                    " -> ".join(source_steps),
                    "no_data_in_range",
                    events=tuple(events),
                    effects=effects(route_event=route_event, route_source=route_source),
                )

            for source_name, fetch in (
                (
                    "TwelveData",
                    lambda scan_start, scan_end: self._providers.twelve.resolve(
                        symbol_stock,
                        scan_start,
                        scan_end,
                        request.interval,
                        request.twelve_api_key,
                    ),
                ),
                (
                    "YahooFinance",
                    lambda scan_start, scan_end: self._providers.yahoo.resolve(
                        symbol_stock, scan_start, scan_end, request.interval
                    ),
                ),
            ):
                source_steps.append(source_name)
                result, days = probe(source_name, fetch)
                log_result(source_name, result, symbol_stock)
                if not result.prices.empty:
                    return complete(result, source_name, days)
            return MarketDataResolution(
                pd.DataFrame(),
                " -> ".join(source_steps),
                "no_data_in_range",
                events=tuple(events),
                effects=effects(route_event=route_event, route_source=route_source),
            )

        if asset_type == "Meme":
            source_steps.append("GeckoTerminal")
            result = safe(
                "GeckoTerminal",
                lambda: self._providers.gecko.resolve(symbol, start, end),
            )
            log_result("GeckoTerminal", result, symbol)
            if not result.prices.empty:
                return complete(result, "GeckoTerminal", max(request_windows))
            return MarketDataResolution(
                pd.DataFrame(),
                " -> ".join(source_steps),
                result.status,
                result.detail,
                tuple(events),
                effects(route_event=route_event, route_source=route_source),
            )

        for source_name, fetch in (
            (
                "TwelveData",
                lambda scan_start, scan_end: self._providers.twelve.resolve(
                    symbol_stock,
                    scan_start,
                    scan_end,
                    request.interval,
                    request.twelve_api_key,
                ),
            ),
            (
                "YahooFinance",
                lambda scan_start, scan_end: self._providers.yahoo.resolve(
                    symbol_stock, scan_start, scan_end, request.interval
                ),
            ),
        ):
            source_steps.append(source_name)
            result, days = probe(source_name, fetch)
            log_result(source_name, result, symbol_stock)
            if not result.prices.empty:
                return complete(result, source_name, days)

        if asset_type == "Crypto":
            source_steps.append("Binance")
            binance_symbol = normalize_ticker_for_storage(request.ticker)
            result = safe(
                "Binance",
                lambda: self._providers.binance.resolve(
                    binance_symbol, start, end, request.interval
                ),
            )
            log_result("Binance", result, binance_symbol)
            if not result.prices.empty:
                return complete(result, "Binance", max(request_windows))

        source_steps.append("GeckoTerminal")
        result = safe(
            "GeckoTerminal",
            lambda: self._providers.gecko.resolve(symbol, start, end),
        )
        log_result("GeckoTerminal", result, symbol)
        if not result.prices.empty:
            return complete(result, "GeckoTerminal", max(request_windows))
        return MarketDataResolution(
            pd.DataFrame(),
            " -> ".join(source_steps),
            result.status,
            result.detail,
            tuple(events),
            effects(route_event=route_event, route_source=route_source),
        )

    def _dashboard_fast_track(
        self,
        preferred_source: str,
        request: MarketDataRequest,
        asset_type: str,
        symbol: str,
        symbol_stock: str,
        start: datetime,
        end: datetime,
        probe,
        log_result,
        patch_tw,
    ) -> tuple[MarketDataResult, str, int] | None:
        if preferred_source == "YahooFinance":
            candidates = (
                [f"{symbol_stock.replace('.TWO', '').replace('.TW', '')}.TW"]
                if asset_type == "Taiwan Stock"
                else [symbol_stock]
            )
            for candidate in candidates:
                result, days = probe(
                    "YahooFinance",
                    lambda scan_start, scan_end, candidate=candidate: self._providers.yahoo.resolve(
                        candidate, scan_start, scan_end, request.interval
                    ),
                )
                log_result("YahooFinance", result, candidate)
                if not result.prices.empty:
                    prices = result.prices
                    if asset_type == "Taiwan Stock":
                        raw = symbol_stock.replace(".TWO", "").replace(".TW", "")
                        prices = patch_tw(prices, raw)
                    return MarketDataResult(prices, result.status, result.detail, result.error), "YahooFinance", days
        elif preferred_source == "TwelveData":
            raw = symbol_stock.replace(".TWO", "").replace(".TW", "")
            candidates = [f"{raw}.TW", raw] if asset_type == "Taiwan Stock" else [symbol_stock]
            for candidate in candidates:
                result, days = probe(
                    "TwelveData",
                    lambda scan_start, scan_end, candidate=candidate: self._providers.twelve.resolve(
                        candidate,
                        scan_start,
                        scan_end,
                        request.interval,
                        request.twelve_api_key,
                    ),
                )
                log_result("TwelveData", result, candidate)
                if not result.prices.empty:
                    prices = patch_tw(result.prices, raw) if asset_type == "Taiwan Stock" else result.prices
                    return MarketDataResult(prices, result.status, result.detail, result.error), "TwelveData", days
        elif preferred_source == "Binance" and asset_type == "Crypto":
            symbol_used = normalize_ticker_for_storage(request.ticker)
            result = self._providers.binance.resolve(symbol_used, start, end, request.interval)
            log_result("Binance", result, symbol_used)
            if not result.prices.empty:
                return result, "Binance", max(1, (end - start).days)
        elif preferred_source == "Fugle" and asset_type == "Taiwan Stock" and request.interval != "1d":
            raw = symbol_stock.replace(".TWO", "").replace(".TW", "")
            result, days = probe(
                "Fugle",
                lambda scan_start, scan_end: self._providers.fugle.resolve(
                    raw,
                    scan_start,
                    scan_end,
                    request.interval,
                    request.fugle_api_key,
                ),
            )
            log_result("Fugle", result, raw)
            if not result.prices.empty:
                prices = patch_tw(
                    normalize_interval_frame(result.prices, request.interval), raw
                )
                return MarketDataResult(prices, result.status, result.detail, result.error), "Fugle", days
        return None

    @staticmethod
    def _success(
        result: MarketDataResult,
        source: str,
        events: list[MarketDataEvent],
        effects: MarketDataEffects | None = None,
    ) -> MarketDataResolution:
        return MarketDataResolution(
            result.prices,
            source,
            "success",
            result.detail,
            tuple(events),
            effects or MarketDataEffects(),
        )

    @staticmethod
    def _result(
        result: MarketDataResult,
        source: str,
        events: list[MarketDataEvent],
        effects: MarketDataEffects | None = None,
    ) -> MarketDataResolution:
        return MarketDataResolution(
            result.prices,
            source,
            result.status,
            result.detail,
            tuple(events),
            effects or MarketDataEffects(),
        )


def create_daemon_market_data_resolver(request: HttpRequest) -> MarketDataResolver:
    """Build the daemon's no-cache market-data profile and live adapters."""

    return MarketDataResolver.for_daemon(
        _create_market_data_providers(request, profile=_DAEMON_MARKET_DATA_PROFILE)
    )


def create_dashboard_market_data_resolver(
    request: HttpRequest,
    *,
    cache_ttl_seconds: int | Callable[[str], int] = 600,
    clock: Callable[[], float] = time.monotonic,
) -> MarketDataResolver:
    """Build the dashboard profile with module-owned cache lifecycle."""

    return MarketDataResolver.for_dashboard(
        _create_market_data_providers(request, profile=_DASHBOARD_MARKET_DATA_PROFILE),
        cache_ttl_seconds=cache_ttl_seconds,
        clock=clock,
    )


def build_price_frame(rows: list[dict]) -> pd.DataFrame:
    """Build the canonical naive-UTC OHLC(V) candle frame."""

    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    frame["date"] = pd.to_datetime(frame["date"], utc=True, errors="coerce").dt.tz_localize(None)
    for column in ["open", "high", "low", "close", "volume"]:
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["date", "open", "high", "low", "close"])
    return frame.sort_values("date").reset_index(drop=True)


def aggregate_four_hour(
    prices: pd.DataFrame,
    *,
    policy: FourHourPolicy = FOUR_HOUR_OHLC_POLICY,
) -> pd.DataFrame:
    """Aggregate canonical candles on the callers' existing UTC 4h anchor."""

    if prices is None or prices.empty:
        return pd.DataFrame()
    aggregations = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        **({"volume": "sum"} if "volume" in prices.columns else {}),
    }
    aggregated = prices.set_index("date").resample("4h").agg(aggregations)
    if policy.require_complete_row:
        aggregated = aggregated.dropna()
    else:
        aggregated = aggregated.dropna(subset=["open", "high", "low", "close"])
    return build_price_frame(aggregated.reset_index().to_dict("records"))


def normalize_interval_frame(
    prices: pd.DataFrame,
    interval: str,
    *,
    policy: FourHourPolicy = FOUR_HOUR_OHLC_POLICY,
) -> pd.DataFrame:
    """Normalize finer upstream candles to the requested canonical interval."""

    if prices is None or prices.empty:
        return pd.DataFrame()
    if interval == "4h":
        return aggregate_four_hour(prices, policy=policy)
    return prices


def _intraday_coverage_ok(prices: pd.DataFrame, interval: str) -> bool:
    if prices is None or prices.empty or "date" not in prices.columns or len(prices) < 20:
        return False
    dates = pd.to_datetime(prices["date"], errors="coerce").dropna()
    if dates.empty:
        return False
    span_days = (dates.max() - dates.min()).total_seconds() / 86400
    minimum = {"4h": 20, "1h": 20, "15m": 20, "5m": 5, "1m": 2}.get(
        interval, 5
    )
    return span_days >= minimum


def _merge_price_data(base: pd.DataFrame, patch: pd.DataFrame) -> pd.DataFrame:
    if base is None or base.empty:
        return patch if patch is not None else pd.DataFrame()
    if patch is None or patch.empty:
        return base
    return (
        pd.concat([base, patch], ignore_index=True)
        .sort_values("date")
        .drop_duplicates(subset=["date"], keep="last")
        .reset_index(drop=True)
    )


def _extract_fugle_candles(payload) -> list:
    candles = None
    if isinstance(payload, dict):
        data_block = payload.get("data")
        if isinstance(data_block, dict):
            candles = data_block.get("candles")
        elif isinstance(data_block, list):
            candles = data_block
        if candles is None:
            candles = payload.get("candles")
    elif isinstance(payload, list):
        candles = payload
    return candles if isinstance(candles, list) else []


def _interval_delta(interval: str) -> timedelta:
    return {
        "1m": timedelta(minutes=1),
        "5m": timedelta(minutes=5),
        "15m": timedelta(minutes=15),
        "1h": timedelta(hours=1),
        "4h": timedelta(hours=4),
        "1d": timedelta(days=1),
    }.get(interval, timedelta(days=1))
