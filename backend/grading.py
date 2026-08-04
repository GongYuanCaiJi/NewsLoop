from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import math
from typing import Any, Callable, Optional

import pandas as pd


@dataclass(frozen=True)
class PriceTimeline:
    """Caller-owned price alignment at the grading seam."""

    to_timestamp: Callable[[Any], Any]
    price_dates: Callable[[pd.DataFrame], pd.Series]
    locate: Callable[[pd.DataFrame, Any, str], tuple[Any, Any]]
    interval_delta: Callable[[str], timedelta]


@dataclass(frozen=True)
class GradingOutcome:
    outcome_label: str
    bars_elapsed: int
    reason_code: str
    observed_high: float | None = None
    observed_low: float | None = None
    final_close: float | None = None


def infer_order_type(record: dict) -> str:
    order_type = str(record.get("order_type") or "").strip().title()
    if order_type in {"Market", "Limit"}:
        return order_type
    entry_value = pd.to_numeric(record.get("entry"), errors="coerce")
    return "Market" if pd.isna(entry_value) else "Limit"


def evaluate_track1_outcome(
    record: dict,
    price_df: pd.DataFrame,
    selected_interval: str,
    timeout_bars: Optional[int],
    timeline: PriceTimeline,
    same_bar_resolver=None,
) -> GradingOutcome | None:
    if not record.get("date"):
        return None
    take_profit = _coerce_float(record.get("tp"))
    stop_loss = _coerce_float(record.get("sl"))
    if take_profit is None or stop_loss is None:
        return None

    sentiment = str(record.get("sentiment") or "").strip()
    if sentiment not in {"Bullish", "Bearish"}:
        return None

    target_timestamp = timeline.to_timestamp(record.get("date"))
    if target_timestamp is None:
        return None
    if price_df is None or price_df.empty:
        return GradingOutcome("Data_Missing", 0, "history_truncated")

    price_dates = timeline.price_dates(price_df)
    first_timestamp = price_dates.min()
    last_timestamp = price_dates.max()
    if pd.isna(first_timestamp) or pd.isna(last_timestamp):
        return GradingOutcome("Data_Missing", 0, "history_truncated")

    interval = timeline.interval_delta(selected_interval)
    forward_window = max(interval * 48, timedelta(days=3))
    if (
        target_timestamp < first_timestamp
        and (first_timestamp - target_timestamp) > forward_window
    ):
        return GradingOutcome("Data_Missing", 0, "history_truncated")

    signal_index, signal_row = timeline.locate(
        price_df,
        record["date"],
        selected_interval,
    )
    if signal_row is None:
        if target_timestamp > last_timestamp:
            return None
        return GradingOutcome("Data_Missing", 0, "history_truncated")

    tags = {
        str(tag).strip()
        for tag in (record.get("tags") or [])
        if tag is not None
    }
    force_fill = "Filled" in tags or "Manual" in tags
    order_type = infer_order_type(record)
    entry = pd.to_numeric(record.get("entry"), errors="coerce")
    entry_value = None if pd.isna(entry) else float(entry)
    market_fill = order_type == "Market"

    is_filled = False
    fill_index = None
    bars_since_signal = 0
    bars_since_fill = 0

    for index in range(int(signal_index), len(price_df)):
        row = price_df.iloc[index]
        bars_since_signal = int(index) - int(signal_index)
        high = row["high"]
        low = row["low"]

        if (
            timeout_bars is not None
            and timeout_bars > 0
            and not is_filled
            and bars_since_signal > timeout_bars
        ):
            return GradingOutcome(
                "Expired",
                bars_since_signal,
                "unfilled_expired",
            )

        if not is_filled:
            fill_by_limit = (
                entry_value is not None and low <= entry_value <= high
            )
            if force_fill or market_fill or fill_by_limit:
                is_filled = True
                fill_index = index
                bars_since_fill = 0
                if market_fill or fill_by_limit:
                    continue
            else:
                continue

        if fill_index is not None:
            bars_since_fill = int(index) - int(fill_index)

        if sentiment == "Bullish":
            same_bar_hit = high >= take_profit and low <= stop_loss
            stop_hit = low <= stop_loss
            target_hit = high >= take_profit
        else:
            same_bar_hit = high >= stop_loss and low <= take_profit
            stop_hit = high >= stop_loss
            target_hit = low <= take_profit

        if same_bar_hit:
            if callable(same_bar_resolver):
                resolved = same_bar_resolver(
                    record=record,
                    parent_row=row,
                    interval=selected_interval,
                    tp=float(take_profit),
                    sl=float(stop_loss),
                    sentiment=sentiment,
                )
                if resolved in {"Win", "Loss"}:
                    reason = (
                        "same_bar_micro_tp_first"
                        if resolved == "Win"
                        else "same_bar_micro_sl_first"
                    )
                    return GradingOutcome(resolved, bars_since_fill, reason)
            return GradingOutcome(
                "Unresolved",
                bars_since_fill,
                "same_bar_micro_unresolved",
            )
        if stop_hit:
            return GradingOutcome("Loss", bars_since_fill, "sl_hit")
        if target_hit:
            return GradingOutcome("Win", bars_since_fill, "tp_hit")
        if (
            timeout_bars is not None
            and timeout_bars > 0
            and bars_since_fill > timeout_bars
        ):
            return GradingOutcome(
                "Timed Out",
                bars_since_fill,
                "timed_out_after_fill",
            )

    if (
        is_filled
        and timeout_bars is not None
        and timeout_bars > 0
        and bars_since_fill > timeout_bars
    ):
        return GradingOutcome(
            "Timed Out",
            bars_since_fill,
            "timed_out_after_fill",
        )
    return None


def _coerce_float(value) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def evaluate_track2_outcome(
    record: dict,
    price_df: pd.DataFrame,
    selected_interval: str,
    timeline: PriceTimeline,
):
    sentiment = str(record.get("sentiment") or "").strip()
    if sentiment not in {"Bullish", "Bearish"}:
        return None

    bars_limit = _coerce_float(
        pd.to_numeric(record.get("t2_bars_limit"), errors="coerce")
    )
    if bars_limit is None or bars_limit <= 0 or not bars_limit.is_integer():
        return GradingOutcome("Invalid_Entry", 0, "t2_invalid_bars_limit")
    bars_limit = int(bars_limit)

    entry_timestamp = timeline.to_timestamp(record.get("t2_entry_time"))
    if entry_timestamp is None:
        return GradingOutcome("Invalid_Entry", 0, "t2_missing_entry_time")
    if price_df is None or price_df.empty:
        return GradingOutcome("Data_Missing", 0, "history_truncated")

    price_dates = timeline.price_dates(price_df)
    first_timestamp = price_dates.min()
    last_timestamp = price_dates.max()
    if pd.isna(first_timestamp) or pd.isna(last_timestamp):
        return GradingOutcome("Data_Missing", 0, "history_truncated")

    interval = timeline.interval_delta(selected_interval)
    forward_window = max(interval * 48, timedelta(days=3))
    if (
        entry_timestamp < first_timestamp
        and (first_timestamp - entry_timestamp) > forward_window
    ):
        return GradingOutcome("Data_Missing", 0, "history_truncated")

    signal_index, signal_row = timeline.locate(
        price_df,
        entry_timestamp,
        selected_interval,
    )
    if signal_row is None:
        if entry_timestamp > last_timestamp:
            return None
        return GradingOutcome("Data_Missing", 0, "history_truncated")

    start_index = int(signal_index)
    available = len(price_df) - start_index
    partial_window = price_df.iloc[start_index:]
    observed_high_partial = (
        _coerce_float(pd.to_numeric(partial_window["high"], errors="coerce").max())
        if not partial_window.empty and "high" in partial_window.columns
        else None
    )
    observed_low_partial = (
        _coerce_float(pd.to_numeric(partial_window["low"], errors="coerce").min())
        if not partial_window.empty and "low" in partial_window.columns
        else None
    )
    final_close_partial = (
        _coerce_float(
            pd.to_numeric(partial_window.iloc[-1].get("close"), errors="coerce")
        )
        if not partial_window.empty
        else None
    )

    if available < bars_limit:
        return GradingOutcome(
            outcome_label="Pending",
            bars_elapsed=int(max(available, 0)),
            reason_code="t2_waiting_bars",
            observed_high=observed_high_partial,
            observed_low=observed_low_partial,
            final_close=final_close_partial,
        )

    end_index = start_index + bars_limit - 1
    window = price_df.iloc[start_index : end_index + 1]
    observed_high = _coerce_float(
        pd.to_numeric(window["high"], errors="coerce").max()
    )
    observed_low = _coerce_float(
        pd.to_numeric(window["low"], errors="coerce").min()
    )
    final_close = _coerce_float(
        pd.to_numeric(window.iloc[-1].get("close"), errors="coerce")
    )

    entry_price_input = record.get("t2_entry_price")
    entry_price_raw = _coerce_float(
        pd.to_numeric(entry_price_input, errors="coerce")
    )
    if entry_price_input is None or (
        isinstance(entry_price_input, str) and not entry_price_input.strip()
    ):
        entry_price_raw = _coerce_float(
            pd.to_numeric(signal_row.get("close"), errors="coerce")
        )
    if entry_price_raw is None or entry_price_raw <= 0:
        return GradingOutcome(
            outcome_label="Invalid_Entry",
            bars_elapsed=bars_limit,
            reason_code="t2_invalid_entry_price",
            observed_high=observed_high,
            observed_low=observed_low,
            final_close=final_close,
        )
    entry_price = entry_price_raw

    threshold_raw = pd.to_numeric(
        record.get("t2_threshold_pct"),
        errors="coerce",
    )
    threshold_pct = float(threshold_raw) if pd.notna(threshold_raw) else 0.0
    if threshold_pct < 0:
        threshold_pct = 0.0

    if sentiment == "Bullish":
        move_pct = ((final_close - entry_price) / entry_price) * 100.0
    else:
        move_pct = ((entry_price - final_close) / entry_price) * 100.0

    if move_pct >= threshold_pct:
        return GradingOutcome(
            outcome_label="Win",
            bars_elapsed=bars_limit,
            reason_code="t2_threshold_hit",
            observed_high=observed_high,
            observed_low=observed_low,
            final_close=final_close,
        )
    return GradingOutcome(
        outcome_label="Loss",
        bars_elapsed=bars_limit,
        reason_code="t2_threshold_not_met",
        observed_high=observed_high,
        observed_low=observed_low,
        final_close=final_close,
    )
