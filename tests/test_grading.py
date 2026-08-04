from __future__ import annotations

import unittest
from datetime import timedelta

import pandas as pd

from backend.grading import (
    GradingOutcome,
    PriceTimeline,
    evaluate_track1_outcome,
    evaluate_track2_outcome,
)


def to_timestamp(value):
    if value is None:
        return None
    timestamp = pd.to_datetime(value, errors="coerce")
    if pd.isna(timestamp):
        return None
    if getattr(timestamp, "tzinfo", None) is not None:
        timestamp = timestamp.tz_convert(None)
    return timestamp


def price_dates(frame: pd.DataFrame) -> pd.Series:
    return pd.to_datetime(frame["date"], utc=True, errors="coerce").dt.tz_localize(None)


def locate_price(frame: pd.DataFrame, value, _interval: str):
    target = to_timestamp(value)
    dates = price_dates(frame)
    index = int(dates.searchsorted(target, side="left"))
    if index >= len(frame):
        return None, None
    return index, frame.iloc[index]


def interval_delta(interval: str) -> timedelta:
    return {
        "1m": timedelta(minutes=1),
        "5m": timedelta(minutes=5),
        "15m": timedelta(minutes=15),
        "1h": timedelta(hours=1),
        "4h": timedelta(hours=4),
        "1d": timedelta(days=1),
    }.get(interval, timedelta(days=1))


TIMELINE = PriceTimeline(
    to_timestamp=to_timestamp,
    price_dates=price_dates,
    locate=locate_price,
    interval_delta=interval_delta,
)


def prices(*rows: tuple[str, float, float, float]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"date": date, "high": high, "low": low, "close": close}
            for date, high, low, close in rows
        ]
    )


class Track1GradingTests(unittest.TestCase):
    def test_market_fill_skips_signal_bar_extremes(self) -> None:
        frame = prices(
            ("2026-01-01T00:00:00Z", 120, 80, 100),
            ("2026-01-01T01:00:00Z", 111, 95, 108),
        )
        record = {
            "date": "2026-01-01T00:00:00Z",
            "sentiment": "Bullish",
            "order_type": "Market",
            "tp": 110,
            "sl": 90,
        }

        outcome = evaluate_track1_outcome(record, frame, "1h", 12, TIMELINE)

        self.assertEqual(GradingOutcome("Win", 1, "tp_hit"), outcome)

    def test_limit_order_expires_before_fill(self) -> None:
        frame = prices(
            ("2026-01-01T00:00:00Z", 105, 95, 100),
            ("2026-01-01T01:00:00Z", 106, 96, 101),
            ("2026-01-01T02:00:00Z", 107, 97, 102),
        )
        record = {
            "date": "2026-01-01T00:00:00Z",
            "sentiment": "Bullish",
            "order_type": "Limit",
            "entry": 50,
            "tp": 110,
            "sl": 90,
        }

        outcome = evaluate_track1_outcome(record, frame, "1h", 1, TIMELINE)

        self.assertEqual(
            GradingOutcome("Expired", 2, "unfilled_expired"),
            outcome,
        )

    def test_manual_fill_uses_same_bar_resolver(self) -> None:
        frame = prices(("2026-01-01T00:00:00Z", 112, 88, 100))
        record = {
            "date": "2026-01-01T00:00:00Z",
            "sentiment": "Bullish",
            "tags": ["Filled"],
            "order_type": "Limit",
            "entry": 50,
            "tp": 110,
            "sl": 90,
        }

        outcome = evaluate_track1_outcome(
            record,
            frame,
            "1h",
            12,
            TIMELINE,
            same_bar_resolver=lambda **_kwargs: "Loss",
        )

        self.assertEqual(
            GradingOutcome("Loss", 0, "same_bar_micro_sl_first"),
            outcome,
        )

    def test_missing_prices_report_truncated_history(self) -> None:
        record = {
            "date": "2026-01-01T00:00:00Z",
            "sentiment": "Bearish",
            "tp": 90,
            "sl": 110,
        }

        outcome = evaluate_track1_outcome(
            record,
            pd.DataFrame(),
            "1h",
            12,
            TIMELINE,
        )

        self.assertEqual(
            GradingOutcome("Data_Missing", 0, "history_truncated"),
            outcome,
        )

    def test_filled_trade_times_out_after_limit(self) -> None:
        frame = prices(
            ("2026-01-01T00:00:00Z", 101, 99, 100),
            ("2026-01-01T01:00:00Z", 102, 98, 100),
            ("2026-01-01T02:00:00Z", 103, 97, 100),
        )
        record = {
            "date": "2026-01-01T00:00:00Z",
            "sentiment": "Bullish",
            "order_type": "Limit",
            "entry": 100,
            "tp": 110,
            "sl": 90,
        }

        outcome = evaluate_track1_outcome(record, frame, "1h", 1, TIMELINE)

        self.assertEqual(
            GradingOutcome("Timed Out", 2, "timed_out_after_fill"),
            outcome,
        )

    def test_same_bar_without_micro_resolution_is_unresolved(self) -> None:
        frame = prices(("2026-01-01T00:00:00Z", 112, 88, 100))
        record = {
            "date": "2026-01-01T00:00:00Z",
            "sentiment": "Bearish",
            "tags": ["Manual"],
            "order_type": "Limit",
            "entry": 50,
            "tp": 90,
            "sl": 110,
        }

        outcome = evaluate_track1_outcome(record, frame, "1h", 12, TIMELINE)

        self.assertEqual(
            GradingOutcome("Unresolved", 0, "same_bar_micro_unresolved"),
            outcome,
        )

    def test_numeric_string_trade_levels_are_coerced(self) -> None:
        frame = prices(
            ("2026-01-01T00:00:00Z", 101, 99, 100),
            ("2026-01-01T01:00:00Z", 112, 99, 110),
        )
        record = {
            "date": "2026-01-01T00:00:00Z",
            "sentiment": "Bullish",
            "order_type": "Market",
            "tp": "110",
            "sl": "90",
        }

        outcome = evaluate_track1_outcome(record, frame, "1h", 12, TIMELINE)

        self.assertEqual(GradingOutcome("Win", 1, "tp_hit"), outcome)


class Track2GradingTests(unittest.TestCase):
    def test_partial_window_stays_pending_with_observed_values(self) -> None:
        frame = prices(
            ("2026-01-01T00:00:00Z", 105, 95, 100),
            ("2026-01-01T01:00:00Z", 108, 94, 106),
        )
        record = {
            "sentiment": "Bullish",
            "t2_bars_limit": 3,
            "t2_entry_time": "2026-01-01T00:00:00Z",
            "t2_entry_price": 100,
            "t2_threshold_pct": 5,
        }

        outcome = evaluate_track2_outcome(record, frame, "1h", TIMELINE)

        self.assertEqual(
            GradingOutcome(
                "Pending",
                2,
                "t2_waiting_bars",
                observed_high=108.0,
                observed_low=94.0,
                final_close=106.0,
            ),
            outcome,
        )

    def test_bullish_threshold_hit_is_win(self) -> None:
        frame = prices(
            ("2026-01-01T00:00:00Z", 103, 98, 100),
            ("2026-01-01T01:00:00Z", 108, 99, 106),
        )
        record = {
            "sentiment": "Bullish",
            "t2_bars_limit": 2,
            "t2_entry_time": "2026-01-01T00:00:00Z",
            "t2_entry_price": 100,
            "t2_threshold_pct": 5,
        }

        outcome = evaluate_track2_outcome(record, frame, "1h", TIMELINE)

        self.assertEqual(
            GradingOutcome(
                "Win",
                2,
                "t2_threshold_hit",
                observed_high=108.0,
                observed_low=98.0,
                final_close=106.0,
            ),
            outcome,
        )

    def test_bearish_threshold_not_met_is_loss(self) -> None:
        frame = prices(
            ("2026-01-01T00:00:00Z", 102, 98, 100),
            ("2026-01-01T01:00:00Z", 103, 97, 99),
        )
        record = {
            "sentiment": "Bearish",
            "t2_bars_limit": 2,
            "t2_entry_time": "2026-01-01T00:00:00Z",
            "t2_entry_price": 100,
            "t2_threshold_pct": 5,
        }

        outcome = evaluate_track2_outcome(record, frame, "1h", TIMELINE)

        self.assertEqual(
            GradingOutcome(
                "Loss",
                2,
                "t2_threshold_not_met",
                observed_high=103.0,
                observed_low=97.0,
                final_close=99.0,
            ),
            outcome,
        )

    def test_invalid_bars_limit_is_rejected(self) -> None:
        record = {
            "sentiment": "Bullish",
            "t2_bars_limit": 0,
            "t2_entry_time": "2026-01-01T00:00:00Z",
        }

        outcome = evaluate_track2_outcome(record, pd.DataFrame(), "1h", TIMELINE)

        self.assertEqual(
            GradingOutcome("Invalid_Entry", 0, "t2_invalid_bars_limit"),
            outcome,
        )

    def test_non_integral_bars_limit_is_rejected(self) -> None:
        record = {
            "sentiment": "Bullish",
            "t2_bars_limit": 1.5,
            "t2_entry_time": "2026-01-01T00:00:00Z",
        }

        outcome = evaluate_track2_outcome(record, pd.DataFrame(), "1h", TIMELINE)

        self.assertEqual(
            GradingOutcome("Invalid_Entry", 0, "t2_invalid_bars_limit"),
            outcome,
        )

    def test_non_finite_bars_limit_is_rejected(self) -> None:
        record = {
            "sentiment": "Bullish",
            "t2_bars_limit": float("inf"),
            "t2_entry_time": "2026-01-01T00:00:00Z",
        }

        outcome = evaluate_track2_outcome(record, pd.DataFrame(), "1h", TIMELINE)

        self.assertEqual(
            GradingOutcome("Invalid_Entry", 0, "t2_invalid_bars_limit"),
            outcome,
        )

    def test_non_finite_entry_price_is_rejected(self) -> None:
        frame = prices(
            ("2026-01-01T00:00:00Z", 103, 98, 100),
            ("2026-01-01T01:00:00Z", 108, 99, 106),
        )
        record = {
            "sentiment": "Bullish",
            "t2_bars_limit": 2,
            "t2_entry_time": "2026-01-01T00:00:00Z",
            "t2_entry_price": float("inf"),
            "t2_threshold_pct": 5,
        }

        outcome = evaluate_track2_outcome(record, frame, "1h", TIMELINE)

        self.assertEqual(
            GradingOutcome(
                "Invalid_Entry",
                2,
                "t2_invalid_entry_price",
                observed_high=108.0,
                observed_low=98.0,
                final_close=106.0,
            ),
            outcome,
        )

    def test_missing_entry_time_is_rejected(self) -> None:
        record = {
            "sentiment": "Bullish",
            "t2_bars_limit": 2,
        }

        outcome = evaluate_track2_outcome(record, pd.DataFrame(), "1h", TIMELINE)

        self.assertEqual(
            GradingOutcome("Invalid_Entry", 0, "t2_missing_entry_time"),
            outcome,
        )

    def test_entry_price_falls_back_to_signal_close(self) -> None:
        frame = prices(
            ("2026-01-01T00:00:00Z", 101, 99, 100),
            ("2026-01-01T01:00:00Z", 107, 100, 106),
        )
        record = {
            "sentiment": "Bullish",
            "t2_bars_limit": 2,
            "t2_entry_time": "2026-01-01T00:00:00Z",
            "t2_threshold_pct": 5,
        }

        outcome = evaluate_track2_outcome(record, frame, "1h", TIMELINE)

        self.assertEqual(
            GradingOutcome(
                "Win",
                2,
                "t2_threshold_hit",
                observed_high=107.0,
                observed_low=99.0,
                final_close=106.0,
            ),
            outcome,
        )


if __name__ == "__main__":
    unittest.main()
