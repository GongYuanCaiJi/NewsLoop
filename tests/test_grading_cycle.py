from __future__ import annotations

import ast
import unittest
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import auto_grader_daemon as daemon
from backend.grading import GradingOutcome
from backend.grading_cycle import (
    CycleResult,
    CycleSettings,
    DAEMON_CYCLE_POLICY,
    DASHBOARD_BACKGROUND_CYCLE_POLICY,
    DASHBOARD_INTERACTIVE_CYCLE_POLICY,
    GradingCycle,
    WriteFailureAction,
)


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 1, 3, 12, 0, tzinfo=timezone.utc)


def candles(*rows: tuple[str, float, float, float]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"date": date, "open": close, "high": high, "low": low, "close": close}
            for date, high, low, close in rows
        ]
    )


class FakeRepository:
    def __init__(self, trading_theses: list[dict]) -> None:
        self.trading_theses = trading_theses
        self.pending_only_calls: list[bool] = []
        self.track1_writes: list[tuple[str, str, str]] = []
        self.track2_writes: list[dict] = []
        self.message_writes: list[tuple[str, str]] = []
        self.fail_page_ids: set[str] = set()

    def list_candidates(self, *, pending_only: bool) -> list[dict]:
        self.pending_only_calls.append(pending_only)
        return self.trading_theses

    def write_track1(self, trading_thesis: dict, outcome) -> str | None:
        if trading_thesis["id"] in self.fail_page_ids:
            raise RuntimeError("Notion write failed")
        self.track1_writes.append(
            (trading_thesis["id"], outcome.outcome_label, outcome.reason_code)
        )
        return outcome.outcome_label

    def write_track2(self, trading_thesis: dict, outcome, *, last_checked_at: str) -> str | None:
        if trading_thesis["id"] in self.fail_page_ids:
            raise RuntimeError("Notion write failed")
        self.track2_writes.append(
            {
                "page_id": trading_thesis["id"],
                "outcome": outcome.outcome_label,
                "reason": outcome.reason_code,
                "observed_high": outcome.observed_high,
                "observed_low": outcome.observed_low,
                "final_close": outcome.final_close,
                "last_checked_at": last_checked_at,
            }
        )
        return outcome.outcome_label

    def write_system_message(self, trading_thesis: dict, message: str) -> None:
        self.message_writes.append((trading_thesis["id"], message))


class FakeMarketData:
    def __init__(self, prices_by_ticker: dict[str, pd.DataFrame | Exception]) -> None:
        self.prices_by_ticker = prices_by_ticker
        self.calls: list[tuple[str, datetime, datetime, str]] = []

    def fetch(self, ticker: str, start: datetime, end: datetime, interval: str) -> pd.DataFrame:
        self.calls.append((ticker, start, end, interval))
        result = self.prices_by_ticker[ticker]
        if isinstance(result, Exception):
            raise result
        return result


@dataclass
class FakeClock:
    value: datetime = NOW

    def now(self) -> datetime:
        return self.value


class FakeControl:
    def __init__(self, settings: CycleSettings | None = None) -> None:
        self._settings = settings or CycleSettings()

    def settings(self) -> CycleSettings:
        return self._settings


def track1_record(page_id: str = "track1", **overrides) -> dict:
    trading_thesis = {
        "id": page_id,
        "ticker": "AAPL",
        "track_mode": "Track1",
        "result_auto": "Pending",
        "date": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "sentiment": "Bullish",
        "order_type": "Market",
        "entry": None,
        "tp": 110,
        "sl": 90,
        "tags": [],
        "timeframe": "1d",
        "system_msg": "",
    }
    trading_thesis.update(overrides)
    return trading_thesis


def track2_record(page_id: str = "track2", **overrides) -> dict:
    trading_thesis = {
        "id": page_id,
        "ticker": "BTCUSDT",
        "track_mode": "Track2",
        "t2_result": "Pending",
        "t2_entry_time": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "t2_entry_price": 100,
        "t2_bars_limit": 2,
        "t2_threshold_pct": 5,
        "sentiment": "Bullish",
        "timeframe": "1d",
    }
    trading_thesis.update(overrides)
    return trading_thesis


class GradingCycleTests(unittest.TestCase):
    def test_named_snap_policies_preserve_daemon_and_dashboard_alignment_semantics(self) -> None:
        prices = candles(
            ("2026-01-03T00:00:00Z", 102, 98, 100),
            ("2026-01-08T00:00:00Z", 105, 99, 101),
            ("2026-01-09T00:00:00Z", 112, 100, 111),
        )
        daemon_thesis = track1_record(
            entry=100,
            date=datetime(2026, 1, 4, tzinfo=timezone.utc),
        )
        dashboard_thesis = dict(daemon_thesis, id="dashboard")
        daemon_repository = FakeRepository([daemon_thesis])
        dashboard_repository = FakeRepository([dashboard_thesis])

        with patch("backend.grading_cycle.classify_asset", return_value="US Stock"):
            GradingCycle(
                daemon_repository,
                FakeMarketData({"AAPL": prices}),
                FakeClock(),
                FakeControl(),
                policy=DAEMON_CYCLE_POLICY,
            ).run()
            GradingCycle(
                dashboard_repository,
                FakeMarketData({"AAPL": prices}),
                FakeClock(),
                FakeControl(),
                policy=DASHBOARD_INTERACTIVE_CYCLE_POLICY,
            ).run()

        self.assertEqual(
            [
                (
                    "track1",
                    "⚠️ [盤前/休市] 市價單 ➜ 已對齊至 2026-01-08 00:00 開盤",
                )
            ],
            daemon_repository.message_writes,
        )
        self.assertEqual([], dashboard_repository.message_writes)

    def test_dashboard_snap_lifecycle_writes_the_existing_system_message_from_shared_cycle(self) -> None:
        trading_thesis = track1_record(
            entry=100,
            date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        repository = FakeRepository([trading_thesis])
        market = FakeMarketData(
            {
                "AAPL": candles(
                    ("2026-01-05T00:00:00Z", 105, 99, 101),
                    ("2026-01-06T00:00:00Z", 112, 100, 111),
                )
            }
        )

        result = GradingCycle(
            repository,
            market,
            FakeClock(),
            FakeControl(),
            policy=DASHBOARD_INTERACTIVE_CYCLE_POLICY,
        ).run()

        self.assertEqual(CycleResult(updated=1, scanned_groups=1), result)
        self.assertEqual(
            [
                (
                    "track1",
                    "[SYS_WARN] Type: M, Snapped to 2026-01-05 00:00:00 (delta=+96h 0m)",
                )
            ],
            repository.message_writes,
        )

    def test_cycle_takes_candidates_through_market_tracks_and_writes(self) -> None:
        repository = FakeRepository([track1_record(), track2_record()])
        market = FakeMarketData(
            {
                "AAPL": candles(
                    ("2026-01-01T00:00:00Z", 105, 95, 100),
                    ("2026-01-02T00:00:00Z", 112, 100, 111),
                ),
                "BTCUSDT": candles(
                    ("2026-01-01T00:00:00Z", 102, 98, 100),
                    ("2026-01-02T00:00:00Z", 108, 99, 106),
                ),
            }
        )
        control = FakeControl()

        result = GradingCycle(repository, market, FakeClock(), control).run()

        self.assertEqual(CycleResult(updated=2, scanned_groups=2), result)
        self.assertEqual([True], repository.pending_only_calls)
        self.assertEqual([("track1", "Win", "tp_hit")], repository.track1_writes)
        self.assertEqual("Win", repository.track2_writes[0]["outcome"])
        self.assertEqual("t2_threshold_hit", repository.track2_writes[0]["reason"])

    def test_track2_partial_window_is_written_as_pending_with_observability(self) -> None:
        trading_thesis = track2_record()
        repository = FakeRepository([trading_thesis])
        market = FakeMarketData(
            {"BTCUSDT": candles(("2026-01-01T00:00:00Z", 103, 97, 101))}
        )

        result = GradingCycle(
            repository, market, FakeClock(), FakeControl()
        ).run()

        self.assertEqual(CycleResult(updated=1, scanned_groups=1), result)
        self.assertEqual(
            {
                "page_id": "track2",
                "outcome": "Pending",
                "reason": "t2_waiting_bars",
                "observed_high": 103.0,
                "observed_low": 97.0,
                "final_close": 101.0,
                "last_checked_at": NOW.isoformat(),
            },
            repository.track2_writes[0],
        )
        self.assertEqual("Pending", trading_thesis["t2_result"])
        self.assertEqual("t2_waiting_bars", trading_thesis["t2_reason"])

    def test_empty_market_data_touches_track2_once_and_leaves_track1_pending(self) -> None:
        track1 = track1_record()
        track2 = track2_record()
        repository = FakeRepository([track1, track2])
        market = FakeMarketData({"AAPL": pd.DataFrame(), "BTCUSDT": pd.DataFrame()})
        control = FakeControl()
        cycle = GradingCycle(repository, market, FakeClock(), control)

        first = cycle.run()
        second = cycle.run()

        self.assertEqual(CycleResult(updated=1, scanned_groups=1), first)
        self.assertEqual(CycleResult(updated=0, scanned_groups=0), second)
        self.assertEqual([], repository.track1_writes)
        self.assertEqual(1, len(repository.track2_writes))
        self.assertEqual("Pending", track1["result_auto"])

    def test_empty_market_abort_isolated_to_current_track2_group(self) -> None:
        failed = track2_record("failed")
        succeeded = track2_record("succeeded", ticker="ETHUSDT")
        repository = FakeRepository([failed, succeeded])
        repository.fail_page_ids.add("failed")
        policy = replace(
            DASHBOARD_BACKGROUND_CYCLE_POLICY,
            empty_track2_write_failure_action=WriteFailureAction.ABORT_GROUP,
        )
        market = FakeMarketData(
            {"BTCUSDT": pd.DataFrame(), "ETHUSDT": pd.DataFrame()}
        )

        result = GradingCycle(
            repository,
            market,
            FakeClock(),
            FakeControl(),
            policy=policy,
        ).run()

        self.assertEqual(
            CycleResult(updated=1, scanned_groups=1, failed_writes=1), result
        )
        self.assertEqual(["succeeded"], [write["page_id"] for write in repository.track2_writes])
        self.assertEqual("Pending", failed["t2_result"])

    def test_invalid_price_date_with_non_default_index_keeps_position_alignment(self) -> None:
        trading_thesis = track1_record(
            date=datetime(2026, 1, 2, tzinfo=timezone.utc)
        )
        frame = candles(
            ("not-a-date", 100, 100, 100),
            ("2026-01-02T00:00:00Z", 101, 99, 100),
            ("2026-01-03T00:00:00Z", 112, 99, 110),
        )
        frame.index = [10, 20, 30]
        repository = FakeRepository([trading_thesis])

        result = GradingCycle(
            repository,
            FakeMarketData({"AAPL": frame}),
            FakeClock(),
            FakeControl(),
        ).run()

        self.assertEqual(CycleResult(updated=1, scanned_groups=1), result)
        self.assertEqual([("track1", "Win", "tp_hit")], repository.track1_writes)

    def test_provider_timeout_is_isolated_without_overwriting_outcome(self) -> None:
        trading_thesis = track1_record(result_auto="Win")
        repository = FakeRepository([trading_thesis])
        market = FakeMarketData({"AAPL": TimeoutError("provider timeout")})

        result = GradingCycle(
            repository, market, FakeClock(), FakeControl()
        ).run(force_recalc=True)

        self.assertEqual(
            CycleResult(updated=0, scanned_groups=0, failed_groups=1), result
        )
        self.assertEqual([], repository.track1_writes)
        self.assertEqual("Win", trading_thesis["result_auto"])

    def test_rerun_does_not_fetch_or_overwrite_final_grading_outcomes(self) -> None:
        trading_theses = [
            track1_record(result_auto="Loss"),
            track2_record(t2_result="Win"),
        ]
        repository = FakeRepository(trading_theses)
        market = FakeMarketData({})

        result = GradingCycle(
            repository, market, FakeClock(), FakeControl()
        ).run()

        self.assertEqual(CycleResult(), result)
        self.assertEqual([], market.calls)
        self.assertEqual([], repository.track1_writes)
        self.assertEqual([], repository.track2_writes)
        self.assertEqual("Loss", trading_theses[0]["result_auto"])
        self.assertEqual("Win", trading_theses[1]["t2_result"])

    def test_explicit_force_mode_preserves_legacy_final_recalculation(self) -> None:
        track1 = track1_record(result_auto="Loss")
        track2 = track2_record(t2_result="Loss")
        repository = FakeRepository([track1, track2])
        market = FakeMarketData(
            {
                "AAPL": candles(
                    ("2026-01-01T00:00:00Z", 105, 95, 100),
                    ("2026-01-02T00:00:00Z", 112, 100, 111),
                ),
                "BTCUSDT": candles(
                    ("2026-01-01T00:00:00Z", 102, 98, 100),
                    ("2026-01-02T00:00:00Z", 108, 99, 106),
                ),
            }
        )

        result = GradingCycle(
            repository, market, FakeClock(), FakeControl()
        ).run(force_recalc=True)

        self.assertEqual(CycleResult(updated=2, scanned_groups=2), result)
        self.assertEqual([False], repository.pending_only_calls)
        self.assertEqual([("track1", "Win", "tp_hit")], repository.track1_writes)
        self.assertEqual("Win", repository.track2_writes[0]["outcome"])
        self.assertEqual("Win", track1["result_auto"])
        self.assertEqual("Win", track2["t2_result"])

    def test_write_failure_does_not_claim_or_mutate_a_grading_outcome(self) -> None:
        track1 = track1_record()
        track2 = track2_record()
        repository = FakeRepository([track1, track2])
        repository.fail_page_ids.update({track1["id"], track2["id"]})
        market = FakeMarketData(
            {
                "AAPL": candles(
                    ("2026-01-01T00:00:00Z", 105, 95, 100),
                    ("2026-01-02T00:00:00Z", 112, 100, 111),
                ),
                "BTCUSDT": candles(
                    ("2026-01-01T00:00:00Z", 102, 98, 100),
                    ("2026-01-02T00:00:00Z", 108, 99, 106),
                ),
            }
        )

        result = GradingCycle(
            repository, market, FakeClock(), FakeControl()
        ).run()

        self.assertEqual(
            CycleResult(updated=0, scanned_groups=2, failed_writes=2), result
        )
        self.assertEqual("Pending", track1["result_auto"])
        self.assertEqual("Pending", track2["t2_result"])

    def test_background_write_failure_aborts_only_the_current_group(self) -> None:
        first = track1_record("first")
        failing = track1_record("failing")
        skipped = track1_record("skipped")
        repository = FakeRepository([first, failing, skipped])
        repository.fail_page_ids.add("failing")
        market = FakeMarketData(
            {
                "AAPL": candles(
                    ("2026-01-01T00:00:00Z", 105, 95, 100),
                    ("2026-01-02T00:00:00Z", 112, 100, 111),
                )
            }
        )

        result = GradingCycle(
            repository,
            market,
            FakeClock(),
            FakeControl(CycleSettings(max_theses=3)),
            policy=DASHBOARD_BACKGROUND_CYCLE_POLICY,
        ).run(force_recalc=True)

        self.assertEqual(
            CycleResult(updated=0, scanned_groups=1, failed_writes=1), result
        )
        self.assertEqual([("first", "Win", "tp_hit")], repository.track1_writes)
        self.assertEqual("Win", first["result_auto"])
        self.assertEqual("Pending", failing["result_auto"])
        self.assertEqual("Pending", skipped["result_auto"])


class GradingCycleArchitectureTests(unittest.TestCase):
    def test_daemon_only_owns_scheduler_and_control_policy(self) -> None:
        daemon_path = ROOT / "auto_grader_daemon.py"
        daemon_source = daemon_path.read_text(encoding="utf-8")
        daemon_tree = ast.parse(daemon_source, filename=str(daemon_path))
        daemon_functions = {
            node.name for node in ast.walk(daemon_tree) if isinstance(node, ast.FunctionDef)
        }

        self.assertNotIn("run_auto_grade_group", daemon_functions)
        self.assertNotIn("run_background_auto_grade_all", daemon_functions)
        self.assertNotIn("grade_track1", daemon_source)
        self.assertNotIn("grade_track2", daemon_source)
        self.assertIn("GradingCycle", daemon_source)

    def test_dashboard_entrypoints_delegate_the_complete_grading_workflow(self) -> None:
        dashboard_path = ROOT / "streamlit" / "hybrid_dashboard.py"
        dashboard_source = dashboard_path.read_text(encoding="utf-8")
        dashboard_tree = ast.parse(dashboard_source, filename=str(dashboard_path))
        dashboard_functions = {
            node.name: node
            for node in ast.walk(dashboard_tree)
            if isinstance(node, ast.FunctionDef)
        }

        for function_name in (
            "run_auto_grade",
            "run_background_auto_grade_all",
            "run_force_trade_grade",
        ):
            function_source = ast.get_source_segment(
                dashboard_source, dashboard_functions[function_name]
            )
            self.assertIn("run_dashboard_grading_cycle", function_source)
            self.assertNotIn("grade_track1", function_source)
            self.assertNotIn("grade_track2", function_source)

        self.assertNotIn("evaluate_track1_outcome as grade_track1", dashboard_source)
        self.assertNotIn("evaluate_track2_outcome as grade_track2", dashboard_source)
        for lifecycle_marker in (
            "def _resolve_same_bar_by_micro_dashboard",
            "def _touch_track2_pending",
            "groups.setdefault(key, []).append",
            "def detect_force_snap",
            "def maybe_write_system_msg_for_snap",
        ):
            self.assertNotIn(lifecycle_marker, dashboard_source)

    def test_deleting_cycle_module_would_remove_the_complete_grading_workflow(self) -> None:
        cycle_source = (ROOT / "backend" / "grading_cycle.py").read_text(
            encoding="utf-8"
        )
        daemon_source = (ROOT / "auto_grader_daemon.py").read_text(encoding="utf-8")
        dashboard_source = (ROOT / "streamlit" / "hybrid_dashboard.py").read_text(
            encoding="utf-8"
        )
        dashboard_adapter_source = (ROOT / "dashboard_grading.py").read_text(
            encoding="utf-8"
        )

        for marker in (
            "evaluate_track1_outcome",
            "evaluate_track2_outcome",
            "_select_candidates",
            "_grade_group",
            "_touch_empty_track2_group",
        ):
            self.assertIn(marker, cycle_source)
            self.assertNotIn(marker, daemon_source)
        self.assertNotIn("after_write", cycle_source)
        self.assertIn("after_write", daemon_source)
        self.assertIn("backend.grading_cycle", daemon_source)
        self.assertIn("backend.grading_cycle", dashboard_source)
        self.assertFalse((ROOT / "backend" / "daemon_grading_cycle.py").exists())
        for lifecycle_marker in (
            "def track1_outcome",
            "def track2_outcome",
            "def should_write_pending",
            "def raise_write_failure",
        ):
            self.assertNotIn(lifecycle_marker, dashboard_adapter_source)

    def test_production_repository_preserves_notion_property_write_contract(self) -> None:
        repository = daemon.NotionTradingThesisRepository(
            "database-id", {"Authorization": "Bearer fake"}
        )
        track1 = GradingOutcome("Win", 1, "tp_hit")
        track2 = GradingOutcome(
            "Pending",
            1,
            "t2_waiting_bars",
            observed_high=103,
            observed_low=97,
            final_close=101,
        )

        with (
            patch.object(daemon, "update_result_auto") as write_track1,
            patch.object(daemon, "update_track2_result") as write_track2,
            patch.object(daemon, "update_system_msg") as write_message,
        ):
            repository.write_track1({"id": "track1"}, track1)
            repository.write_track2(
                {"id": "track2"},
                track2,
                last_checked_at=NOW.isoformat(),
            )
            repository.write_system_message({"id": "track1"}, "existing message")

        write_track1.assert_called_once_with(
            "track1",
            "Win",
            {"Authorization": "Bearer fake"},
            reason_code="tp_hit",
        )
        write_track2.assert_called_once_with(
            "track2",
            "Pending",
            {"Authorization": "Bearer fake"},
            reason_code="t2_waiting_bars",
            observed_high=103,
            observed_low=97,
            final_close=101,
            last_checked_at=NOW.isoformat(),
        )
        write_message.assert_called_once_with(
            "track1", "existing message", {"Authorization": "Bearer fake"}
        )


if __name__ == "__main__":
    unittest.main()
