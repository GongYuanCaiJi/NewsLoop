from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable, Protocol

import pandas as pd

from backend.grading import (
    GradingOutcome,
    PriceTimeline,
    evaluate_track1_outcome,
    evaluate_track2_outcome,
    infer_order_type,
)
from backend.ticker_utils import (
    classify_asset,
    normalize_ticker_for_storage,
    normalize_ticker_key,
)


LOGGER = logging.getLogger(__name__)
INTERVAL_OPTIONS = ("1d", "4h", "1h", "15m", "5m", "1m")


@dataclass(frozen=True)
class CycleSettings:
    selected_interval: str = "1d"
    timeout_bars: int = 0
    max_theses: int = 50


@dataclass(frozen=True)
class CycleResult:
    updated: int = 0
    scanned_groups: int = 0
    failed_groups: int = 0
    failed_writes: int = 0


@dataclass(frozen=True)
class CycleOutcomeEvent:
    trading_thesis: dict
    outcome: GradingOutcome
    prices: pd.DataFrame
    interval: str


@dataclass(frozen=True)
class ForceSnapDecision:
    trading_thesis_time: datetime
    snapped_at: datetime
    snapped_row: object
    delta: timedelta
    order_type: str


@dataclass(frozen=True)
class CycleSnapEvent:
    trading_thesis: dict
    decision: ForceSnapDecision
    interval: str


class TradingThesisRepository(Protocol):
    def list_candidates(self, *, pending_only: bool) -> list[dict]: ...

    def write_track1(
        self, trading_thesis: dict, outcome: GradingOutcome
    ) -> str | None: ...

    def write_track2(
        self,
        trading_thesis: dict,
        outcome: GradingOutcome,
        *,
        last_checked_at: str,
    ) -> str | None: ...

    def write_system_message(self, trading_thesis: dict, message: str) -> None: ...


class MarketData(Protocol):
    def fetch(
        self,
        ticker: str,
        start: datetime,
        end: datetime,
        interval: str,
    ) -> pd.DataFrame: ...


class Clock(Protocol):
    def now(self) -> datetime: ...


class CycleControl(Protocol):
    def settings(self) -> CycleSettings: ...


class PendingWriteMode(str, Enum):
    DEDUPLICATE = "deduplicate"
    ALWAYS = "always"


class MissingTrack1OutcomeMode(str, Enum):
    SKIP = "skip"
    RESET_PENDING = "reset_pending"


class InvalidTrack2OutcomeMode(str, Enum):
    TO_PENDING = "to_pending"
    PRESERVE = "preserve"


class WriteFailureAction(str, Enum):
    CONTINUE = "continue"
    ABORT_GROUP = "abort_group"
    RAISE = "raise"


class SnapTiming(str, Enum):
    BEFORE_EVALUATION = "before_evaluation"
    AFTER_OUTCOME = "after_outcome"


@dataclass(frozen=True)
class CyclePolicy:
    """Named caller policy built from lifecycle primitives owned here."""

    interval_for: Callable[[dict, str], str]
    market_ticker: Callable[[dict, str], str]
    pending_write_mode: PendingWriteMode
    missing_track1_outcome_mode: MissingTrack1OutcomeMode
    invalid_track2_outcome_mode: InvalidTrack2OutcomeMode
    write_failure_action: WriteFailureAction
    pending_track2_write_failure_action: WriteFailureAction
    empty_track2_write_failure_action: WriteFailureAction
    log_failures: bool
    snap_message: Callable[[dict, str, datetime, timedelta], str]
    snap_timing: SnapTiming
    snap_requires_nearest_gap: bool


class CycleObserver(Protocol):
    def outcome_written(self, event: CycleOutcomeEvent) -> None: ...

    def snap_resolved(self, event: CycleSnapEvent) -> None: ...


def _thesis_interval_policy(trading_thesis: dict, selected_interval: str) -> str:
    return _thesis_interval(trading_thesis, selected_interval)


def _selected_interval_policy(trading_thesis: dict, selected_interval: str) -> str:
    del trading_thesis
    return selected_interval


def _normalized_market_ticker(trading_thesis: dict, normalized_key: str) -> str:
    raw_ticker = str(trading_thesis.get("ticker") or "").strip()
    return normalize_ticker_for_storage(raw_ticker or normalized_key)


def _raw_market_ticker(trading_thesis: dict, normalized_key: str) -> str:
    return str(trading_thesis.get("ticker") or normalized_key).strip()


def _daemon_snap_message(
    trading_thesis: dict,
    order_type: str,
    snapped_at: datetime,
    delta: timedelta,
) -> str:
    del delta
    current_message = str(trading_thesis.get("system_msg") or "")
    order_type_zh = "限價單" if order_type == "Limit" else "市價單"
    warning = (
        f"⚠️ [盤前/休市] {order_type_zh} ➜ "
        f"已對齊至 {snapped_at.strftime('%Y-%m-%d %H:%M')} 開盤"
    )
    return f"{current_message}\n{warning}".strip() if current_message else warning


def _dashboard_snap_message(
    trading_thesis: dict,
    order_type: str,
    snapped_at: datetime,
    delta: timedelta,
) -> str:
    current_message = str(trading_thesis.get("system_msg") or "")
    seconds = int(abs(delta.total_seconds()))
    hours, remainder = divmod(seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    sign = "-" if delta.total_seconds() < 0 else "+"
    warning = (
        f"[SYS_WARN] Type: {'M' if order_type == 'Market' else 'L'}, "
        f"Snapped to {snapped_at.strftime('%Y-%m-%d %H:%M:%S')} "
        f"(delta={sign}{hours}h {minutes}m)"
    )
    return f"{current_message}\n{warning}".strip() if current_message else warning


DAEMON_CYCLE_POLICY = CyclePolicy(
    interval_for=_thesis_interval_policy,
    market_ticker=_normalized_market_ticker,
    pending_write_mode=PendingWriteMode.DEDUPLICATE,
    missing_track1_outcome_mode=MissingTrack1OutcomeMode.SKIP,
    invalid_track2_outcome_mode=InvalidTrack2OutcomeMode.TO_PENDING,
    write_failure_action=WriteFailureAction.CONTINUE,
    pending_track2_write_failure_action=WriteFailureAction.CONTINUE,
    empty_track2_write_failure_action=WriteFailureAction.CONTINUE,
    log_failures=True,
    snap_message=_daemon_snap_message,
    snap_timing=SnapTiming.BEFORE_EVALUATION,
    snap_requires_nearest_gap=False,
)

DASHBOARD_INTERACTIVE_CYCLE_POLICY = CyclePolicy(
    interval_for=_selected_interval_policy,
    market_ticker=_raw_market_ticker,
    pending_write_mode=PendingWriteMode.ALWAYS,
    missing_track1_outcome_mode=MissingTrack1OutcomeMode.SKIP,
    invalid_track2_outcome_mode=InvalidTrack2OutcomeMode.TO_PENDING,
    write_failure_action=WriteFailureAction.RAISE,
    pending_track2_write_failure_action=WriteFailureAction.CONTINUE,
    empty_track2_write_failure_action=WriteFailureAction.CONTINUE,
    log_failures=False,
    snap_message=_dashboard_snap_message,
    snap_timing=SnapTiming.AFTER_OUTCOME,
    snap_requires_nearest_gap=True,
)

DASHBOARD_BACKGROUND_CYCLE_POLICY = CyclePolicy(
    interval_for=_thesis_interval_policy,
    market_ticker=_raw_market_ticker,
    pending_write_mode=PendingWriteMode.ALWAYS,
    missing_track1_outcome_mode=MissingTrack1OutcomeMode.SKIP,
    invalid_track2_outcome_mode=InvalidTrack2OutcomeMode.TO_PENDING,
    write_failure_action=WriteFailureAction.ABORT_GROUP,
    pending_track2_write_failure_action=WriteFailureAction.CONTINUE,
    empty_track2_write_failure_action=WriteFailureAction.CONTINUE,
    log_failures=False,
    snap_message=_dashboard_snap_message,
    snap_timing=SnapTiming.AFTER_OUTCOME,
    snap_requires_nearest_gap=True,
)

DASHBOARD_FORCE_TRADE_CYCLE_POLICY = CyclePolicy(
    interval_for=_selected_interval_policy,
    market_ticker=_raw_market_ticker,
    pending_write_mode=PendingWriteMode.ALWAYS,
    missing_track1_outcome_mode=MissingTrack1OutcomeMode.RESET_PENDING,
    invalid_track2_outcome_mode=InvalidTrack2OutcomeMode.PRESERVE,
    write_failure_action=WriteFailureAction.RAISE,
    pending_track2_write_failure_action=WriteFailureAction.RAISE,
    empty_track2_write_failure_action=WriteFailureAction.RAISE,
    log_failures=False,
    snap_message=_dashboard_snap_message,
    snap_timing=SnapTiming.AFTER_OUTCOME,
    snap_requires_nearest_gap=True,
)




class _NullCycleObserver:
    def outcome_written(self, event: CycleOutcomeEvent) -> None:
        del event

    def snap_resolved(self, event: CycleSnapEvent) -> None:
        del event


class _AbortGroup(Exception):
    pass


class GradingCycle:
    """Run one complete Trading Thesis grading cycle across external seams."""

    def __init__(
        self,
        repository: TradingThesisRepository,
        market_data: MarketData,
        clock: Clock,
        control: CycleControl,
        *,
        policy: CyclePolicy | None = None,
        observer: CycleObserver | None = None,
        timeline: PriceTimeline | None = None,
    ) -> None:
        self._repository = repository
        self._market_data = market_data
        self._clock = clock
        self._control = control
        self._policy = policy or DAEMON_CYCLE_POLICY
        self._observer = observer or _NullCycleObserver()
        self._timeline = timeline or PriceTimeline(
            to_timestamp=_to_naive_timestamp,
            price_dates=_price_dates_utc_naive,
            locate=_locate_price,
            interval_delta=_interval_delta,
        )

    def run(self, *, force_recalc: bool = False) -> CycleResult:
        settings = self._control.settings()
        selected_interval = _selected_interval(settings.selected_interval)
        trading_theses = self._repository.list_candidates(pending_only=not force_recalc)
        candidates = _select_candidates(trading_theses, force_recalc=force_recalc)
        if settings.max_theses > 0:
            candidates = candidates[: settings.max_theses]
        else:
            candidates = candidates[:50]

        groups: dict[tuple[str, str, str], list[dict]] = {}
        for trading_thesis in candidates:
            interval = self._policy.interval_for(trading_thesis, selected_interval)
            interval = _selected_interval(interval)
            track_mode = _track_mode(trading_thesis)
            key = (
                track_mode,
                normalize_ticker_key(trading_thesis.get("ticker")),
                interval,
            )
            groups.setdefault(key, []).append(trading_thesis)

        updated = 0
        scanned_groups = 0
        failed_groups = 0
        failed_writes = 0
        now = _as_utc(self._clock.now())

        for (track_mode, normalized_key, interval), group in groups.items():
            ticker = self._policy.market_ticker(group[0], normalized_key)
            if not ticker:
                continue
            start = _market_start(group, track_mode, interval, now)
            try:
                prices = self._market_data.fetch(ticker, start, now, interval)
            except Exception as exc:
                failed_groups += 1
                if self._policy.log_failures:
                    LOGGER.warning(
                        "group scan failed track=%s ticker=%s interval=%s err=%s",
                        track_mode,
                        ticker,
                        interval,
                        exc,
                    )
                continue

            if prices is None or prices.empty:
                if track_mode == "Track2":
                    try:
                        touched, write_failures = self._touch_empty_track2_group(
                            group, interval
                        )
                    except _AbortGroup:
                        failed_writes += 1
                        continue
                    updated += touched
                    failed_writes += write_failures
                    if touched > 0:
                        scanned_groups += 1
                continue

            scanned_groups += 1
            try:
                group_updated, group_write_failures = self._grade_group(
                    group,
                    prices,
                    interval,
                    timeout_bars=settings.timeout_bars,
                    force_recalc=force_recalc,
                )
            except _AbortGroup:
                failed_writes += 1
                continue
            updated += group_updated
            failed_writes += group_write_failures

        return CycleResult(
            updated=updated,
            scanned_groups=scanned_groups,
            failed_groups=failed_groups,
            failed_writes=failed_writes,
        )

    def _grade_group(
        self,
        trading_theses: list[dict],
        prices: pd.DataFrame,
        interval: str,
        *,
        timeout_bars: int,
        force_recalc: bool,
    ) -> tuple[int, int]:
        updated = 0
        failed_writes = 0
        for trading_thesis in trading_theses:
            if _track_mode(trading_thesis) == "Track2":
                if not force_recalc and _is_final(trading_thesis.get("t2_result")):
                    continue
                outcome = evaluate_track2_outcome(
                    trading_thesis,
                    prices,
                    interval,
                    self._timeline,
                )
                outcome = _track2_outcome(
                    outcome, self._policy.invalid_track2_outcome_mode
                )
                if outcome.outcome_label == "Pending" and not self._should_write_pending(
                    trading_thesis,
                    interval=interval,
                    last_checked_at=self._clock.now(),
                    outcome=outcome,
                ):
                    continue
                if self._write_track2(
                    trading_thesis,
                    outcome,
                    pending_write=outcome.outcome_label == "Pending",
                ):
                    updated += 1
                    self._observer.outcome_written(
                        CycleOutcomeEvent(trading_thesis, outcome, prices, interval)
                    )
                else:
                    failed_writes += 1
                continue

            if not force_recalc and _is_final(trading_thesis.get("result_auto")):
                continue
            if self._policy.snap_timing == SnapTiming.BEFORE_EVALUATION:
                self._write_snap_message(trading_thesis, prices, interval)
            outcome = evaluate_track1_outcome(
                trading_thesis,
                prices,
                interval,
                timeout_bars,
                self._timeline,
                same_bar_resolver=self._resolve_same_bar,
            )
            outcome = _track1_outcome(
                outcome, self._policy.missing_track1_outcome_mode
            )
            if outcome is None:
                continue
            try:
                written_label = self._repository.write_track1(trading_thesis, outcome)
            except Exception as exc:
                action = self._write_failure_action(trading_thesis, outcome)
                if action == WriteFailureAction.RAISE:
                    raise
                if action == WriteFailureAction.ABORT_GROUP:
                    raise _AbortGroup from exc
                failed_writes += 1
                if self._policy.log_failures:
                    LOGGER.error(
                        "update_result_auto failed page=%s err=%s",
                        trading_thesis.get("id"),
                        exc,
                    )
                continue
            if written_label is None:
                continue
            trading_thesis["result_auto"] = written_label
            trading_thesis["reason_code"] = outcome.reason_code
            updated += 1
            self._observer.outcome_written(
                CycleOutcomeEvent(trading_thesis, outcome, prices, interval)
            )
            if self._policy.snap_timing == SnapTiming.AFTER_OUTCOME:
                self._write_snap_message(trading_thesis, prices, interval)
        return updated, failed_writes

    def _touch_empty_track2_group(
        self, trading_theses: list[dict], interval: str
    ) -> tuple[int, int]:
        touched = 0
        failed_writes = 0
        for trading_thesis in trading_theses:
            if _is_final(trading_thesis.get("t2_result")):
                continue
            outcome = GradingOutcome("Pending", 0, "")
            if not self._should_write_pending(
                trading_thesis,
                interval=interval,
                last_checked_at=self._clock.now(),
                outcome=outcome,
            ):
                continue
            if self._write_track2(trading_thesis, outcome, empty_market=True):
                touched += 1
                self._observer.outcome_written(
                    CycleOutcomeEvent(trading_thesis, outcome, pd.DataFrame(), interval)
                )
            else:
                failed_writes += 1
        return touched, failed_writes

    def _write_track2(
        self,
        trading_thesis: dict,
        outcome: GradingOutcome,
        *,
        pending_write: bool = False,
        empty_market: bool = False,
    ) -> bool:
        checked_at = _as_utc(self._clock.now()).isoformat()
        try:
            written_label = self._repository.write_track2(
                trading_thesis,
                outcome,
                last_checked_at=checked_at,
            )
        except Exception as exc:
            action = self._write_failure_action(
                trading_thesis, outcome, empty_market=empty_market
            )
            if action == WriteFailureAction.RAISE:
                raise
            if action == WriteFailureAction.ABORT_GROUP:
                raise _AbortGroup from exc
            if not self._policy.log_failures:
                return False
            if empty_market:
                LOGGER.warning(
                    "track2 touch on empty prices failed page=%s err=%s",
                    trading_thesis.get("id"),
                    exc,
                )
            elif pending_write:
                LOGGER.warning(
                    "touch_track2_pending failed page=%s err=%s",
                    trading_thesis.get("id"),
                    exc,
                )
            else:
                LOGGER.error(
                    "update_track2_result failed page=%s err=%s",
                    trading_thesis.get("id"),
                    exc,
                )
            return False
        if written_label is None:
            return False
        trading_thesis["t2_result"] = written_label
        if outcome.reason_code:
            trading_thesis["t2_reason"] = outcome.reason_code
        if outcome.observed_high is not None and pd.notna(outcome.observed_high):
            trading_thesis["t2_observed_high"] = outcome.observed_high
        if outcome.observed_low is not None and pd.notna(outcome.observed_low):
            trading_thesis["t2_observed_low"] = outcome.observed_low
        if outcome.final_close is not None and pd.notna(outcome.final_close):
            trading_thesis["t2_final_close"] = outcome.final_close
        trading_thesis["t2_last_checked_at"] = pd.to_datetime(checked_at, errors="coerce")
        return True

    def _should_write_pending(
        self,
        trading_thesis: dict,
        *,
        interval: str,
        last_checked_at: datetime,
        outcome: GradingOutcome,
    ) -> bool:
        if self._policy.pending_write_mode == PendingWriteMode.ALWAYS:
            return True
        return _should_write_pending(
            trading_thesis,
            interval=interval,
            last_checked_at=last_checked_at,
            outcome=outcome,
        )

    def _write_failure_action(
        self,
        trading_thesis: dict,
        outcome: GradingOutcome,
        *,
        empty_market: bool = False,
    ) -> WriteFailureAction:
        if empty_market:
            return self._policy.empty_track2_write_failure_action
        if (
            _track_mode(trading_thesis) == "Track2"
            and outcome.outcome_label == "Pending"
        ):
            return self._policy.pending_track2_write_failure_action
        return self._policy.write_failure_action

    def _resolve_same_bar(
        self,
        *,
        trading_thesis: dict,
        parent_row,
        interval: str,
        tp: float,
        sl: float,
        sentiment: str,
    ) -> str | None:
        try:
            ticker = str(trading_thesis.get("ticker") or "").strip()
            parent_timestamp = _to_naive_timestamp(parent_row.get("date"))
            if not ticker or parent_timestamp is None:
                return None
            micro_interval = "1m" if interval in {"15m", "5m", "1m"} else "5m"
            micro_delta = _interval_delta(micro_interval)
            micro = self._market_data.fetch(
                ticker,
                parent_timestamp - micro_delta,
                parent_timestamp + _interval_delta(interval) + micro_delta,
                micro_interval,
            )
            if micro is None or micro.empty:
                return None
            frame = micro.copy()
            frame["date"] = _price_dates_utc_naive(micro)
            frame = frame.dropna(subset=["date"]).sort_values("date")
            frame = frame[
                (frame["date"] >= parent_timestamp)
                & (frame["date"] < parent_timestamp + _interval_delta(interval))
            ]
            for _, row in frame.iterrows():
                high = pd.to_numeric(row.get("high"), errors="coerce")
                low = pd.to_numeric(row.get("low"), errors="coerce")
                if pd.isna(high) or pd.isna(low):
                    continue
                tp_hit = high >= tp if sentiment == "Bullish" else low <= tp
                sl_hit = low <= sl if sentiment == "Bullish" else high >= sl
                if tp_hit and sl_hit:
                    return "Unresolved"
                if tp_hit:
                    return "Win"
                if sl_hit:
                    return "Loss"
        except Exception as exc:
            if self._policy.log_failures:
                LOGGER.warning(
                    "same-bar micro resolve failed page=%s err=%s",
                    trading_thesis.get("id"),
                    exc,
                )
        return None

    def _write_snap_message(
        self, trading_thesis: dict, prices: pd.DataFrame, interval: str
    ) -> None:
        snap = resolve_force_snap(
            trading_thesis,
            prices,
            interval,
            self._timeline,
            require_nearest_gap=self._policy.snap_requires_nearest_gap,
        )
        if snap is None:
            return
        current_message = str(trading_thesis.get("system_msg") or "")
        if "[SYS_WARN]" in current_message or "⚠️ [盤前/休市]" in current_message:
            return
        self._observer.snap_resolved(
            CycleSnapEvent(trading_thesis, snap, interval)
        )
        order_type = snap.order_type or infer_order_type(trading_thesis)
        message = self._policy.snap_message(
            trading_thesis,
            order_type=order_type,
            snapped_at=snap.snapped_at,
            delta=snap.delta,
        )
        if not message:
            return
        try:
            self._repository.write_system_message(trading_thesis, message)
            trading_thesis["system_msg"] = message
        except Exception as exc:
            LOGGER.warning(
                "System_Msg write skipped page=%s err=%s",
                trading_thesis.get("id"),
                exc,
            )


def _track1_outcome(
    outcome: GradingOutcome | None,
    mode: MissingTrack1OutcomeMode,
) -> GradingOutcome | None:
    if outcome is not None or mode == MissingTrack1OutcomeMode.SKIP:
        return outcome
    return GradingOutcome("Pending", 0, "")


def _track2_outcome(
    outcome: GradingOutcome | None,
    mode: InvalidTrack2OutcomeMode,
) -> GradingOutcome:
    if outcome is None:
        return GradingOutcome("Pending", 0, "")
    if (
        mode == InvalidTrack2OutcomeMode.TO_PENDING
        and outcome.outcome_label in {"Data_Missing", "Invalid_Entry"}
    ):
        return GradingOutcome(
            "Pending",
            outcome.bars_elapsed,
            outcome.reason_code or outcome.outcome_label,
            outcome.observed_high,
            outcome.observed_low,
            outcome.final_close,
        )
    return outcome


def _track_mode(trading_thesis: dict) -> str:
    return "Track2" if str(trading_thesis.get("track_mode") or "Track1").strip() == "Track2" else "Track1"


def _is_final(value) -> bool:
    text = str(value or "").strip()
    return bool(text and text != "Pending")


def _select_candidates(trading_theses: list[dict], *, force_recalc: bool) -> list[dict]:
    candidates = []
    for trading_thesis in trading_theses:
        if not trading_thesis.get("ticker"):
            continue
        if force_recalc:
            candidates.append(trading_thesis)
        elif _track_mode(trading_thesis) == "Track2":
            if not _is_final(trading_thesis.get("t2_result")):
                candidates.append(trading_thesis)
        elif not _is_final(trading_thesis.get("result_auto")):
            candidates.append(trading_thesis)
    return candidates


def _selected_interval(value: str) -> str:
    interval = str(value or "1d").strip().lower()
    return interval if interval in INTERVAL_OPTIONS else "1d"


def _thesis_interval(trading_thesis: dict, default: str) -> str:
    interval = str(trading_thesis.get("timeframe") or default or "1d").strip().lower()
    return interval if interval in INTERVAL_OPTIONS else default


def _market_start(
    trading_theses: list[dict], track_mode: str, interval: str, now: datetime
) -> datetime:
    anchors = []
    for trading_thesis in trading_theses:
        anchor = trading_thesis.get("t2_entry_time") if track_mode == "Track2" else trading_thesis.get("date")
        timestamp = pd.to_datetime(anchor, utc=True, errors="coerce")
        if pd.notna(timestamp):
            anchors.append(timestamp.to_pydatetime())
    if anchors:
        return min(anchors) - timedelta(days=3)
    if interval == "1d":
        return now - timedelta(days=365 * 5)
    if interval in {"4h", "1h"}:
        return now - timedelta(days=730)
    return now - timedelta(days=60)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _interval_delta(interval: str) -> timedelta:
    return {
        "1m": timedelta(minutes=1),
        "5m": timedelta(minutes=5),
        "15m": timedelta(minutes=15),
        "1h": timedelta(hours=1),
        "4h": timedelta(hours=4),
        "1d": timedelta(days=1),
    }.get(interval, timedelta(days=1))


def _to_naive_timestamp(value):
    if value is None:
        return None
    timestamp = pd.to_datetime(value, errors="coerce")
    if pd.isna(timestamp):
        return None
    if getattr(timestamp, "tzinfo", None) is not None:
        timestamp = timestamp.tz_convert(None)
    return timestamp


def _price_dates_utc_naive(prices: pd.DataFrame) -> pd.Series:
    if prices is None or prices.empty:
        return pd.Series(dtype="datetime64[ns]")
    return pd.to_datetime(prices["date"], utc=True, errors="coerce").dt.tz_localize(None)


def _locate_price(prices: pd.DataFrame, value, _interval: str):
    if prices is None or prices.empty:
        return None, None
    target = _to_naive_timestamp(value)
    if target is None:
        return None, None
    dates = _price_dates_utc_naive(prices)
    if dates.isna().any():
        mask = (dates >= target).to_numpy()
        if not mask.any():
            return None, None
        index = int(mask.argmax())
        return index, prices.iloc[index]
    index = int(dates.searchsorted(target, side="left"))
    if index >= len(prices):
        return None, None
    return index, prices.iloc[index]


def _numeric_changed(current, upcoming, tolerance: float = 1e-9) -> bool:
    current_number = pd.to_numeric(current, errors="coerce")
    upcoming_number = pd.to_numeric(upcoming, errors="coerce")
    if pd.isna(current_number) and pd.isna(upcoming_number):
        return False
    if pd.isna(current_number) != pd.isna(upcoming_number):
        return True
    return abs(float(current_number) - float(upcoming_number)) > tolerance


def _should_write_pending(
    trading_thesis: dict,
    *,
    interval: str,
    last_checked_at,
    outcome: GradingOutcome,
) -> bool:
    current_checked = _to_naive_timestamp(trading_thesis.get("t2_last_checked_at"))
    next_checked = _to_naive_timestamp(last_checked_at)
    if current_checked is None or next_checked is None:
        return True
    if str(trading_thesis.get("t2_reason") or "").strip() != str(outcome.reason_code or "").strip():
        return True
    if _numeric_changed(trading_thesis.get("t2_observed_high"), outcome.observed_high):
        return True
    if _numeric_changed(trading_thesis.get("t2_observed_low"), outcome.observed_low):
        return True
    if _numeric_changed(trading_thesis.get("t2_final_close"), outcome.final_close):
        return True
    cooldown = max(
        timedelta(seconds=30),
        min(_interval_delta(interval), timedelta(minutes=15)),
    )
    return (next_checked - current_checked) >= cooldown


def resolve_force_snap(
    trading_thesis: dict,
    prices: pd.DataFrame,
    interval: str,
    timeline: PriceTimeline,
    *,
    require_nearest_gap: bool = True,
) -> ForceSnapDecision | None:
    """Resolve the shared force-snap lifecycle through an injected timeline."""

    trade_at = timeline.to_timestamp(trading_thesis.get("date"))
    entry = pd.to_numeric(trading_thesis.get("entry"), errors="coerce")
    if prices is None or prices.empty or trade_at is None or pd.isna(entry):
        return None
    tolerance = timeline.interval_delta(interval) * 3
    if require_nearest_gap:
        dates = timeline.price_dates(prices)
        valid_dates = dates.dropna()
        if valid_dates.empty:
            return None
        nearest_delta = (valid_dates - trade_at).abs().min()
        if nearest_delta <= tolerance:
            return None
    _, next_row = timeline.locate(prices, trading_thesis.get("date"), interval)
    if next_row is None:
        return None
    snapped_at = timeline.to_timestamp(next_row.get("date"))
    if snapped_at is None:
        return None
    delta = snapped_at - trade_at
    snap_max = (
        timedelta(days=1)
        if classify_asset(trading_thesis.get("ticker")) == "Taiwan Stock"
        else timeline.interval_delta(interval) * 6
    )
    if (
        delta < timedelta(0)
        or delta <= tolerance
        or delta > snap_max
    ):
        return None
    return ForceSnapDecision(
        trading_thesis_time=trade_at,
        snapped_at=snapped_at,
        snapped_row=next_row,
        delta=delta,
        order_type=infer_order_type(trading_thesis),
    )
