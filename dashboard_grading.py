from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

import pandas as pd

from backend.grading_cycle import (
    Clock,
    CycleObserver,
    CycleOutcomeEvent,
    CyclePolicy,
    CycleResult,
    CycleSettings,
    CycleSnapEvent,
    GradingCycle,
    MarketData,
    TradingThesisRepository,
)
from backend.grading import GradingOutcome, PriceTimeline
from backend.ticker_utils import normalize_ticker_key


Track1Writer = Callable[[str, GradingOutcome], None]
Track2Writer = Callable[[str, GradingOutcome, str], None]
SystemMessageWriter = Callable[[str, str], None]
PriceFetcher = Callable[[str, datetime, datetime, str], object]


@dataclass(frozen=True)
class DashboardCycleControl:
    selected_interval: str
    timeout_bars: int
    max_theses: int

    def settings(self) -> CycleSettings:
        return CycleSettings(
            selected_interval=self.selected_interval,
            timeout_bars=self.timeout_bars,
            max_theses=self.max_theses,
        )


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


def run_dashboard_grading_cycle(
    repository: TradingThesisRepository,
    market_data: MarketData,
    control: DashboardCycleControl,
    policy: CyclePolicy,
    observer: CycleObserver,
    timeline: PriceTimeline,
    clock: Clock | None = None,
) -> CycleResult:
    return GradingCycle(
        repository,
        market_data,
        clock or SystemClock(),
        control,
        policy=policy,
        observer=observer,
        timeline=timeline,
    ).run(force_recalc=True)




class DashboardTradingThesisRepository:
    def __init__(
        self,
        trading_theses: list[dict],
        *,
        write_track1: Track1Writer,
        write_track2: Track2Writer,
        write_system_message: SystemMessageWriter,
        warn: Callable[[str], None],
        warn_on_unwritable_outcome: bool = True,
    ) -> None:
        self._records = trading_theses
        self._write_track1 = write_track1
        self._write_track2 = write_track2
        self._write_system_message = write_system_message
        self._warn = warn
        self._warn_on_unwritable_outcome = warn_on_unwritable_outcome

    def list_candidates(self, *, pending_only: bool) -> list[dict]:
        del pending_only
        return self._records

    def write_track1(
        self, trading_thesis: dict, outcome: GradingOutcome
    ) -> str | None:
        try:
            self._write_track1(trading_thesis["id"], outcome)
        except Exception as exc:
            if (
                self._warn_on_unwritable_outcome
                and "validation_error" in str(exc)
                and outcome.outcome_label in {"Data_Missing", "Invalid_Entry"}
            ):
                self._warn(
                    "Notion 欄位選項缺少 "
                    f"`{outcome.outcome_label}`，已停止寫入以避免誤寫成 Timed Out。"
                )
                return None
            raise
        return outcome.outcome_label

    def write_track2(
        self,
        trading_thesis: dict,
        outcome: GradingOutcome,
        *,
        last_checked_at: str,
    ) -> str | None:
        try:
            self._write_track2(trading_thesis["id"], outcome, last_checked_at)
        except Exception as exc:
            if (
                self._warn_on_unwritable_outcome
                and "validation_error" in str(exc)
                and outcome.outcome_label in {"Data_Missing", "Invalid_Entry"}
            ):
                self._warn(
                    "Notion 欄位選項缺少 "
                    f"`{outcome.outcome_label}`（Track2），已停止寫入以避免誤降級。"
                )
                return None
            raise
        return outcome.outcome_label

    def write_system_message(self, trading_thesis: dict, message: str) -> None:
        self._write_system_message(trading_thesis.get("id"), message)


class BackgroundDashboardMarketData:
    def __init__(self, fetch: PriceFetcher) -> None:
        self._fetch = fetch

    def fetch(
        self,
        ticker: str,
        start: datetime,
        end: datetime,
        interval: str,
    ) -> pd.DataFrame:
        return _prices_from_fetch_result(self._fetch(ticker, start, end, interval))


def _preloaded_prices_cover_window(
    prices: pd.DataFrame,
    start: datetime,
    end: datetime,
) -> bool:
    if prices.empty or "date" not in prices.columns:
        return False
    dates = pd.to_datetime(prices["date"], errors="coerce", utc=True).dropna()
    if dates.empty:
        return False
    requested = pd.to_datetime([start, end], errors="coerce", utc=True)
    if requested.isna().any():
        return False
    return dates.min() <= requested[0] and dates.max() >= requested[1]


class InteractiveDashboardMarketData(BackgroundDashboardMarketData):
    def __init__(
        self,
        *,
        ticker: str,
        interval: str,
        prices: pd.DataFrame,
        fetch: PriceFetcher,
    ) -> None:
        super().__init__(fetch)
        self._ticker_key = normalize_ticker_key(ticker)
        self._interval = interval
        self._prices = prices

    def fetch(
        self,
        ticker: str,
        start: datetime,
        end: datetime,
        interval: str,
    ) -> pd.DataFrame:
        if (
            normalize_ticker_key(ticker) == self._ticker_key
            and interval == self._interval
            and (
                end - start >= timedelta(days=2)
                or _preloaded_prices_cover_window(self._prices, start, end)
            )
        ):
            return self._prices
        return super().fetch(ticker, start, end, interval)


class DashboardOutcomeRenderer:
    """Render successful domain outcome events through a dashboard callback."""

    def __init__(
        self,
        write: Callable[[str], None],
        *,
        timeout_bars: int = 0,
    ) -> None:
        self._write = write
        self._timeout_bars = timeout_bars

    def outcome_written(self, event: CycleOutcomeEvent) -> None:
        trading_thesis = event.trading_thesis
        outcome = event.outcome
        date_label = _date_label(trading_thesis.get("date"))
        label = outcome.outcome_label
        reason = outcome.reason_code
        bars = outcome.bars_elapsed
        if str(trading_thesis.get("track_mode") or "Track1").strip() == "Track2":
            if label != "Pending":
                self._write(f"🧠 {date_label} | Track2={label} ({reason})")
            return
        if label == "Win":
            self._write(f"🎯 {date_label} | Win (第 {bars} 根 K)")
        elif label == "Loss":
            self._write(f"❌ {date_label} | Loss (第 {bars} 根 K)")
        elif label == "Expired":
            self._write(f"⌛ {date_label} | Expired (逾時未成交)")
        elif label == "Data_Missing":
            self._write(f"📉 {date_label} | Data_Missing (歷史資料不足)")
        elif label == "Unresolved":
            self._write(f"🟠 {date_label} | Unresolved (同根雙觸發，需人工判定)")
        else:
            self._write(
                f"⏱️ {date_label} | Timed Out (超過 {self._timeout_bars} 根 K)"
            )

    def snap_resolved(self, event: CycleSnapEvent) -> None:
        del event


class SilentDashboardObserver:
    def outcome_written(self, event: CycleOutcomeEvent) -> None:
        del event

    def snap_resolved(self, event: CycleSnapEvent) -> None:
        del event


class DashboardOutcomeCollector(SilentDashboardObserver):
    def __init__(self) -> None:
        self.outcomes: list[GradingOutcome] = []

    def outcome_written(self, event: CycleOutcomeEvent) -> None:
        super().outcome_written(event)
        self.outcomes.append(event.outcome)


def _prices_from_fetch_result(result: object) -> pd.DataFrame:
    if isinstance(result, tuple):
        result = result[0] if result else pd.DataFrame()
    return result if isinstance(result, pd.DataFrame) else pd.DataFrame()


def _date_label(value) -> str:
    if value is None:
        return "未知日期"
    timestamp = pd.to_datetime(value, errors="coerce")
    if pd.isna(timestamp):
        return "未知日期"
    if getattr(timestamp, "tzinfo", None) is not None:
        timestamp = timestamp.tz_convert(None)
    return timestamp.strftime("%Y-%m-%d")
