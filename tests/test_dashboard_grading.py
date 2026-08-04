from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import shutil

import pandas as pd
from streamlit.testing.v1 import AppTest

from backend.grading_cycle import (
    CycleResult,
    DASHBOARD_BACKGROUND_CYCLE_POLICY,
    DASHBOARD_FORCE_TRADE_CYCLE_POLICY,
    DASHBOARD_INTERACTIVE_CYCLE_POLICY,
    InvalidTrack2OutcomeMode,
    MissingTrack1OutcomeMode,
    WriteFailureAction,
)
from backend.grading import GradingOutcome, PriceTimeline
from dashboard_grading import (
    DashboardCycleControl,
    DashboardOutcomeRenderer,
    DashboardTradingThesisRepository,
    InteractiveDashboardMarketData,
    SystemClock,
    run_dashboard_grading_cycle,
)


NOW = datetime(2026, 1, 3, 12, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def _prices() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "date": "2026-01-01T00:00:00Z",
                "open": 100,
                "high": 102,
                "low": 98,
                "close": 100,
            },
            {
                "date": "2026-01-02T00:00:00Z",
                "open": 100,
                "high": 112,
                "low": 99,
                "close": 111,
            },
        ]
    )


def _representative_record() -> dict:
    return {
        "id": "track1",
        "ticker": "BTC-USD",
        "title": "Deterministic Trading Thesis",
        "url": "https://example.com/thesis",
        "origin_url": "https://example.com/thesis",
        "date": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "asset_class": "Crypto",
        "sentiment": "Bullish",
        "confidence": 80,
        "timeframe": "1d",
        "mindset": 80,
        "entry": None,
        "order_type": "Market",
        "tp": 110,
        "sl": 90,
        "tags": [],
        "note": "fixture",
        "system_msg": "",
        "result": "Pending",
        "result_auto": "Pending",
        "result_manual": None,
        "manual_reason": None,
        "reason_code": None,
        "track_mode": "Track1",
        "return": None,
    }


class FakeClock(SystemClock):
    def now(self) -> datetime:
        return NOW


class DashboardGradingOperationTests(unittest.TestCase):
    def test_interactive_operation_uses_shared_cycle_and_preserves_dashboard_contract(
        self,
    ) -> None:
        trading_theses = [
            {
                "id": "track1",
                "ticker": "BTC-USD",
                "track_mode": "Track1",
                "result_auto": "Loss",
                "date": datetime(2026, 1, 1, tzinfo=timezone.utc),
                "sentiment": "Bullish",
                "order_type": "Market",
                "entry": None,
                "tp": 110,
                "sl": 90,
                "tags": [],
                "timeframe": "1h",
                "system_msg": "",
            },
            {
                "id": "track2",
                "ticker": "BTC-USD",
                "track_mode": "Track2",
                "t2_result": "Loss",
                "t2_entry_time": datetime(2026, 1, 1, tzinfo=timezone.utc),
                "t2_entry_price": 100,
                "t2_bars_limit": 2,
                "t2_threshold_pct": 5,
                "sentiment": "Bullish",
                "timeframe": "1h",
            },
        ]
        writes: list[tuple] = []
        output: list[str] = []
        repository = DashboardTradingThesisRepository(
            trading_theses,
            write_track1=lambda page_id, outcome: writes.append(
                ("track1", page_id, outcome.outcome_label, outcome.reason_code)
            ),
            write_track2=lambda page_id, outcome, checked: writes.append(
                (
                    "track2",
                    page_id,
                    outcome.outcome_label,
                    outcome.reason_code,
                    outcome.observed_high,
                    outcome.observed_low,
                    outcome.final_close,
                    checked,
                )
            ),
            write_system_message=lambda page_id, message: writes.append(
                ("message", page_id, message)
            ),
            warn=output.append,
        )

        def unexpected_fetch(*_args):
            raise AssertionError("interactive main prices must stay preloaded")

        market = InteractiveDashboardMarketData(
            ticker="BTC-USD",
            interval="1d",
            prices=_prices(),
            fetch=unexpected_fetch,
        )
        timeline = PriceTimeline(
            to_timestamp=lambda value: pd.to_datetime(value, errors="coerce").tz_localize(None)
            if getattr(pd.to_datetime(value, errors="coerce"), "tzinfo", None)
            else pd.to_datetime(value, errors="coerce"),
            price_dates=lambda frame: pd.to_datetime(
                frame["date"], utc=True, errors="coerce"
            ).dt.tz_localize(None),
            locate=lambda frame, value, _interval: (
                0,
                frame.iloc[0],
            )
            if pd.to_datetime(value, utc=True) <= pd.to_datetime(frame.iloc[0]["date"], utc=True)
            else (1, frame.iloc[1]),
            interval_delta=lambda _interval: timedelta(days=1),
        )
        result = run_dashboard_grading_cycle(
            repository,
            market,
            DashboardCycleControl("1d", 0, len(trading_theses)),
            DASHBOARD_INTERACTIVE_CYCLE_POLICY,
            DashboardOutcomeRenderer(output.append),
            timeline,
            FakeClock(),
        )

        self.assertEqual(CycleResult(updated=2, scanned_groups=2), result)
        self.assertEqual(
            [
                ("track1", "track1", "Win", "tp_hit"),
                (
                    "track2",
                    "track2",
                    "Win",
                    "t2_threshold_hit",
                    112.0,
                    98.0,
                    111.0,
                    NOW.isoformat(),
                ),
            ],
            writes,
        )
        self.assertEqual(
            [
                "🎯 2026-01-01 | Win (第 1 根 K)",
                "🧠 未知日期 | Track2=Win (t2_threshold_hit)",
            ],
            output,
        )
        self.assertEqual("Win", trading_theses[0]["result_auto"])
        self.assertEqual("Win", trading_theses[1]["t2_result"])

    def test_named_policies_preserve_background_and_force_button_semantics(
        self,
    ) -> None:
        background = DASHBOARD_BACKGROUND_CYCLE_POLICY
        force_trade = DASHBOARD_FORCE_TRADE_CYCLE_POLICY

        self.assertEqual("1h", background.interval_for({"timeframe": "1h"}, "1d"))
        self.assertEqual(
            MissingTrack1OutcomeMode.SKIP,
            background.missing_track1_outcome_mode,
        )
        self.assertEqual(
            InvalidTrack2OutcomeMode.TO_PENDING,
            background.invalid_track2_outcome_mode,
        )
        self.assertEqual(WriteFailureAction.ABORT_GROUP, background.write_failure_action)
        self.assertEqual(
            MissingTrack1OutcomeMode.RESET_PENDING,
            force_trade.missing_track1_outcome_mode,
        )
        self.assertEqual(
            InvalidTrack2OutcomeMode.PRESERVE,
            force_trade.invalid_track2_outcome_mode,
        )


class DashboardMarketDataReuseTests(unittest.TestCase):
    def test_interactive_market_reuses_preloaded_prices_for_short_covered_window(self) -> None:
        prices = _prices()
        prices["date"] = pd.to_datetime(prices["date"], utc=True).dt.tz_localize(None)
        fetch_calls: list[tuple] = []

        market = InteractiveDashboardMarketData(
            ticker="BTC-USD",
            interval="1m",
            prices=prices,
            fetch=lambda *args: fetch_calls.append(args) or pd.DataFrame(),
        )

        result = market.fetch(
            "BTC-USD",
            datetime(2026, 1, 1, 0, tzinfo=timezone.utc),
            datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
            "1m",
        )

        self.assertTrue(result.equals(prices))
        self.assertEqual([], fetch_calls)


class DashboardAppCanaryTests(unittest.TestCase):
    @staticmethod
    def _copy_app(isolated_root: Path) -> Path:
        shutil.copytree(ROOT / "backend", isolated_root / "backend")
        shutil.copytree(ROOT / "streamlit", isolated_root / "streamlit")
        shutil.copy2(
            ROOT / "dashboard_grading.py",
            isolated_root / "dashboard_grading.py",
        )
        return isolated_root / "streamlit" / "hybrid_dashboard.py"

    def test_empty_credentials_render_controls_without_runtime_errors(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            isolated_root = Path(temporary_directory)
            app_path = self._copy_app(isolated_root)
            with patch.dict(
                "os.environ",
                {
                    "NOTION_TOKEN": "",
                    "NEWS_ALPHA_DB_ID": "",
                    "TWELVE_DATA_API_KEY": "",
                    "FUGLE_API_KEY": "",
                },
                clear=False,
            ):
                app = AppTest.from_file(str(app_path), default_timeout=30)
                app.session_state["cfg_notion_token"] = ""
                app.session_state["cfg_db_id"] = ""
                app.session_state["cfg_twelve_api_key"] = ""
                app.session_state["cfg_fugle_api_key"] = ""
                app.run()

            self.assertEqual([], list(app.exception))
            labels = [button.label for button in app.button]
            self.assertIn("重算當前交易", labels)
            self.assertIn("重算當前標的", labels)
            self.assertIn("重算所有標的", labels)
            self.assertTrue(
                any("NOTION_TOKEN 與 DB_ID" in info.value for info in app.info)
            )

    def test_dashboard_does_not_fetch_schema_status_again_after_schema_check(self) -> None:
        trading_thesis = _representative_record()
        app_prices = _prices()
        app_prices["date"] = pd.to_datetime(
            app_prices["date"], utc=True
        ).dt.tz_localize(None)
        runtime_adapters = {
            "check_schema": lambda _db_id, _headers: {
                "ok": True,
                "message": "OK",
                "bindings": {
                    "entry_price": {"id": "price-id", "name": "Entry Price"},
                },
            },
            "fetch_records": lambda _db_id, _headers: [trading_thesis],
            "fetch_prices": lambda *_args: (
                app_prices,
                "Fixture",
                "[Fixture] success",
            ),
        }
        status_gets: list[str] = []

        def unexpected_status_get(url, *_args, **_kwargs):
            if url.endswith("/schema/status"):
                status_gets.append(url)
                raise AssertionError(f"unexpected schema status request: {url}")
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"symbols": []},
            )

        with TemporaryDirectory() as temporary_directory:
            app_path = self._copy_app(Path(temporary_directory))
            app = AppTest.from_file(str(app_path), default_timeout=30)
            app.session_state["cfg_notion_token"] = "fixture-token"
            app.session_state["cfg_db_id"] = "fixture-db"
            app.session_state["cfg_twelve_api_key"] = ""
            app.session_state["cfg_fugle_api_key"] = ""
            app.session_state["auto_focus_toggle"] = False
            app.session_state["_dashboard_runtime_adapters"] = runtime_adapters
            app.query_params["ticker"] = "BTC-USD"
            with patch("requests.get", side_effect=unexpected_status_get):
                app.run()

        self.assertEqual([], list(app.exception))
        self.assertEqual([], status_gets)
        property_selectboxes = [
            widget for widget in app.selectbox if widget.label == "Notion Property"
        ]
        self.assertEqual(1, len(property_selectboxes))
        self.assertIn("Entry Price (price-id)", property_selectboxes[0].options)

    def test_representative_read_and_click_grading_action(self) -> None:
        trading_thesis = _representative_record()
        writes: list[tuple[str, str, str]] = []

        def write_track1(page_id, result, _return, _headers, reason_code):
            writes.append((page_id, result, reason_code))
            trading_thesis["result_auto"] = result
            trading_thesis["reason_code"] = reason_code

        app_prices = _prices()
        app_prices["date"] = pd.to_datetime(
            app_prices["date"], utc=True
        ).dt.tz_localize(None)
        runtime_adapters = {
            "check_schema": lambda _db_id, _headers: {
                "ok": True,
                "message": "OK",
            },
            "fetch_records": lambda _db_id, _headers: [trading_thesis],
            "fetch_prices": lambda *_args: (
                app_prices,
                "Fixture",
                "[Fixture] success",
            ),
            "write_track1": write_track1,
            "write_track2": lambda *_args: None,
            "write_system_message": lambda *_args: None,
        }

        with TemporaryDirectory() as temporary_directory:
            app_path = self._copy_app(Path(temporary_directory))
            app = AppTest.from_file(str(app_path), default_timeout=30)
            app.session_state["cfg_notion_token"] = "fixture-token"
            app.session_state["cfg_db_id"] = "fixture-db"
            app.session_state["cfg_twelve_api_key"] = ""
            app.session_state["cfg_fugle_api_key"] = ""
            app.session_state["trading_theses"] = [trading_thesis]
            app.session_state["auto_focus_toggle"] = False
            app.session_state["_dashboard_runtime_adapters"] = runtime_adapters
            app.query_params["ticker"] = "BTC-USD"
            app.run()

            self.assertEqual([], list(app.exception))
            self.assertTrue(list(app.dataframe), "trading_thesis table should render")
            auto_grade = next(
                button for button in app.button if button.label == "重算當前標的"
            )
            auto_grade.click().run()

            self.assertEqual([], list(app.exception))
            self.assertEqual([("track1", "Win", "tp_hit")], writes)
            self.assertEqual("Win", trading_thesis["result_auto"])
            self.assertEqual("tp_hit", trading_thesis["reason_code"])

    def test_force_trade_preserves_original_notion_validation_error(self) -> None:
        trading_thesis = _representative_record()
        trading_thesis["date"] = datetime(2025, 1, 1, tzinfo=timezone.utc)
        app_prices = _prices()
        app_prices["date"] = pd.to_datetime(
            app_prices["date"], utc=True
        ).dt.tz_localize(None)

        def fail_write(*_args):
            raise RuntimeError("validation_error: original notion detail")

        runtime_adapters = {
            "check_schema": lambda _db_id, _headers: {
                "ok": True,
                "message": "OK",
            },
            "fetch_records": lambda _db_id, _headers: [trading_thesis],
            "fetch_prices": lambda *_args: (
                app_prices,
                "Fixture",
                "[Fixture] success",
            ),
            "write_track1": fail_write,
            "write_track2": lambda *_args: None,
            "write_system_message": lambda *_args: None,
        }

        with TemporaryDirectory() as temporary_directory:
            app_path = self._copy_app(Path(temporary_directory))
            app = AppTest.from_file(str(app_path), default_timeout=30)
            app.session_state["cfg_notion_token"] = "fixture-token"
            app.session_state["cfg_db_id"] = "fixture-db"
            app.session_state["cfg_twelve_api_key"] = ""
            app.session_state["cfg_fugle_api_key"] = ""
            app.session_state["trading_theses"] = [trading_thesis]
            app.session_state["auto_focus_toggle"] = False
            app.session_state["_dashboard_runtime_adapters"] = runtime_adapters
            app.query_params["ticker"] = "BTC-USD"
            app.run()

            force_grade = next(
                button for button in app.button if button.label == "重算當前交易"
            )
            force_grade.click().run()

            self.assertEqual([], list(app.exception))
            self.assertTrue(
                any(
                    error.value
                    == "強制重算失敗：validation_error: original notion detail"
                    for error in app.error
                )
            )
            self.assertFalse(
                any("Notion 欄位選項缺少" in warning.value for warning in app.warning)
            )


if __name__ == "__main__":
    unittest.main()
