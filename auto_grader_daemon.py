from __future__ import annotations

import json
import logging
import os
import random
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Union

import pandas as pd
import requests
from backend.io_utils import FileLock


ROOT = Path(__file__).resolve().parent
CFG_PATH = ROOT / ".streamlit" / "dashboard_config.json"
CONTROL_PATH = ROOT / ".streamlit" / "daemon_control.json"
CONTROL_LOCK_PATH = ROOT / ".streamlit" / "daemon_control.lock"
SCHEMA_CACHE_PATH = ROOT / ".streamlit" / "schema_cache.json"

NOTION_VERSION = "2022-06-28"
MAX_RECORDS_PER_SCAN_DEFAULT = 50
NOTION_WRITE_DELAY_MIN_SECONDS = 0.05
NOTION_WRITE_DELAY_MAX_SECONDS = 0.12
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))
from backend.schema_manager import SchemaManager
from backend.grading_cycle import (
    CycleSettings,
    GradingCycle,
)
from backend.market_data import (
    MarketDataRequest,
    create_daemon_market_data_resolver,
)
from backend.ticker_utils import (
    classify_asset as shared_classify_asset,
    normalize_symbol as shared_normalize_symbol,
    normalize_ticker_key as shared_normalize_ticker_key,
    yahoo_crypto_symbol,
)
from backend.io_utils import request_with_retry, get_plain_text, load_json_file, save_json_atomic

ACTIVE_COLUMN_MAPPING = SchemaManager.default_mapping()
ACTIVE_COLUMN_MAPPING_LOCK = threading.Lock()
_SCHEMA_MANAGER_SINGLETON: Optional[SchemaManager] = None
_SCHEMA_MANAGER_KEY: Optional[tuple[str, str]] = None
_SCHEMA_MANAGER_SINGLETON_LOCK = threading.Lock()
_MARKET_DATA_RESOLVER = create_daemon_market_data_resolver(request_with_retry)
_CFG_CACHE_LOCK = threading.Lock()
_CFG_CACHE = {"ts": 0.0, "mtime": 0.0, "value": None}
_CFG_CACHE_TTL_SECONDS = 30.0


logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def _lock_for_path(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = CONTROL_LOCK_PATH if path.resolve() == CONTROL_PATH.resolve() else path.with_suffix(path.suffix + ".lock")
    return FileLock(str(lock_path), timeout=5)


def read_json(path: Path, default):
    try:
        if not path.exists():
            return default
        with _lock_for_path(path):
            return load_json_file(path, default)
    except Exception as exc:
        logging.warning("read_json failed path=%s err=%s", str(path), exc)
    return default


def write_json(path: Path, payload):
    try:
        lock_path = CONTROL_LOCK_PATH if path.resolve() == CONTROL_PATH.resolve() else path.with_suffix(path.suffix + ".lock")
        save_json_atomic(path, payload, lock_path=lock_path, timeout=5)
    except Exception as exc:
        logging.error("write_json failed path=%s err=%s", str(path), exc)


def col_name(internal_key: str) -> str:
    with ACTIVE_COLUMN_MAPPING_LOCK:
        return SchemaManager.column_name(ACTIVE_COLUMN_MAPPING, internal_key)


def load_dashboard_config():
    now = time.time()
    try:
        mtime = CFG_PATH.stat().st_mtime if CFG_PATH.exists() else 0.0
    except Exception:
        mtime = 0.0
    with _CFG_CACHE_LOCK:
        cached = _CFG_CACHE.get("value")
        cached_ts = float(_CFG_CACHE.get("ts") or 0.0)
        cached_mtime = float(_CFG_CACHE.get("mtime") or 0.0)
        if cached is not None and (now - cached_ts) < _CFG_CACHE_TTL_SECONDS and cached_mtime == mtime:
            return cached
    cfg = read_json(CFG_PATH, {})
    if not isinstance(cfg, dict):
        cfg = {}
    column_mapping = SchemaManager.normalize_mapping(cfg.get("COLUMN_MAPPING"))
    result = {
        "NOTION_TOKEN": cfg.get("NOTION_TOKEN") or os.getenv("NOTION_TOKEN", ""),
        "DB_ID": cfg.get("DB_ID") or cfg.get("NEWS_ALPHA_DB_ID") or os.getenv("NEWS_ALPHA_DB_ID", ""),
        "TWELVE_DATA_API_KEY": cfg.get("TWELVE_DATA_API_KEY") or os.getenv("TWELVE_DATA_API_KEY", ""),
        "FUGLE_API_KEY": cfg.get("FUGLE_API_KEY") or os.getenv("FUGLE_API_KEY", ""),
        "SELECTED_INTERVAL": str(cfg.get("SELECTED_INTERVAL", "1d") or "1d").strip().lower(),
        "GRADE_TIMEOUT_BARS": int(cfg.get("GRADE_TIMEOUT_BARS", 0) or 0),
        "BATCH_SIZE": int(cfg.get("BATCH_SIZE", MAX_RECORDS_PER_SCAN_DEFAULT) or MAX_RECORDS_PER_SCAN_DEFAULT),
        "NOTION_WRITE_DELAY_MIN": float(cfg.get("NOTION_WRITE_DELAY_MIN", NOTION_WRITE_DELAY_MIN_SECONDS) or NOTION_WRITE_DELAY_MIN_SECONDS),
        "NOTION_WRITE_DELAY_MAX": float(cfg.get("NOTION_WRITE_DELAY_MAX", NOTION_WRITE_DELAY_MAX_SECONDS) or NOTION_WRITE_DELAY_MAX_SECONDS),
        "COLUMN_MAPPING": column_mapping,
    }
    with _CFG_CACHE_LOCK:
        _CFG_CACHE["value"] = result
        _CFG_CACHE["mtime"] = mtime
        _CFG_CACHE["ts"] = now
    return result


def build_schema_manager(cfg: dict) -> Optional[SchemaManager]:
    global _SCHEMA_MANAGER_SINGLETON, _SCHEMA_MANAGER_KEY
    token = str(cfg.get("NOTION_TOKEN") or "").strip()
    db_id = str(cfg.get("DB_ID") or "").strip()
    if not token or not db_id:
        return None
    headers = notion_headers(token)
    key = (db_id, token)
    with _SCHEMA_MANAGER_SINGLETON_LOCK:
        if _SCHEMA_MANAGER_SINGLETON is not None and _SCHEMA_MANAGER_KEY == key:
            return _SCHEMA_MANAGER_SINGLETON
        old_mgr = _SCHEMA_MANAGER_SINGLETON
        mgr = SchemaManager(
            db_id=db_id,
            notion_headers=headers,
            cache_path=str(SCHEMA_CACHE_PATH),
            config_path=str(CFG_PATH),
            ttl_seconds=900,
        )
        try:
            mgr.load_cache()
        except Exception as exc:
            logging.warning("schema cache load failed err=%s", exc)
        _SCHEMA_MANAGER_SINGLETON = mgr
        _SCHEMA_MANAGER_KEY = key
        if old_mgr is not None and old_mgr is not mgr:
            try:
                old_mgr.deactivate()
            except Exception:
                pass
        return _SCHEMA_MANAGER_SINGLETON


def load_control():
    data = read_json(CONTROL_PATH, {})
    if not isinstance(data, dict):
        data = {}
    return {
        "interval_minutes": int(data.get("interval_minutes", 0) or 0),
        "run_now_trigger": bool(data.get("run_now_trigger", False)),
        "force_all_trigger": bool(data.get("force_all_trigger", False)),
        "last_run_time": str(data.get("last_run_time") or ""),
        "status": str(data.get("status") or "OK"),
        "last_error": str(data.get("last_error") or ""),
        "last_error_at": str(data.get("last_error_at") or ""),
        "next_retry_time": str(data.get("next_retry_time") or ""),
    }


def save_control(control: dict):
    payload = {
        "interval_minutes": int(control.get("interval_minutes", 0) or 0),
        "run_now_trigger": bool(control.get("run_now_trigger", False)),
        "force_all_trigger": bool(control.get("force_all_trigger", False)),
        "last_run_time": str(control.get("last_run_time") or ""),
        "status": str(control.get("status") or "OK"),
        "last_error": str(control.get("last_error") or ""),
        "last_error_at": str(control.get("last_error_at") or ""),
        "next_retry_time": str(control.get("next_retry_time") or ""),
    }
    write_json(CONTROL_PATH, payload)


def parse_iso_ts(value: str):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def notion_headers(token: str):
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def parse_page(page):
    props = page.get("properties", {})

    def _select_name(*keys):
        for key in keys:
            val = (props.get(key, {}).get("select") or {}).get("name")
            if val:
                return val
        return None

    def _select_or_multi_select_values(key: str):
        p = props.get(key, {})
        select_name = (p.get("select") or {}).get("name")
        if select_name:
            return [select_name]
        ms = p.get("multi_select") or []
        return [m.get("name") for m in ms if m.get("name")]

    def _select_or_multi_select_name(key: str):
        vals = _select_or_multi_select_values(key)
        return vals[0] if vals else None

    def _rich_text_or_select(key: str):
        txt = get_plain_text(props.get(key, {}).get("rich_text", []))
        if txt:
            return txt
        return _select_or_multi_select_name(key) or ""

    ticker = get_plain_text(props.get(col_name("ticker"), {}).get("rich_text", []))
    sentiment = _select_or_multi_select_name(col_name("sentiment"))
    timeframe = _select_or_multi_select_name(col_name("timeframe"))
    order_type = _select_or_multi_select_name(col_name("order_type")) or "Limit"
    asset_class_values = _select_or_multi_select_values(col_name("asset_class"))
    asset_class = asset_class_values[0] if asset_class_values else None
    sector_values = _select_or_multi_select_values(col_name("sector"))
    sector = sector_values[0] if sector_values else None
    date_str = (props.get(col_name("date"), {}).get("date") or {}).get("start")
    tags = [t.get("name") for t in (props.get(col_name("tags"), {}).get("multi_select") or [])]
    entry = props.get(col_name("entry"), {}).get("number")
    tp = props.get(col_name("tp"), {}).get("number")
    sl = props.get(col_name("sl"), {}).get("number")
    result_auto = _select_name(col_name("result_auto"))
    track_mode = _select_name(col_name("track_mode")) or "Track1"
    system_msg = get_plain_text(props.get(col_name("system_msg"), {}).get("rich_text", []))
    reason_code = _rich_text_or_select(col_name("reason_code"))
    t2_bars_limit = props.get(col_name("t2_bars_limit"), {}).get("number")
    t2_threshold_pct = props.get(col_name("t2_threshold_pct"), {}).get("number")
    t2_entry_price = props.get(col_name("t2_entry_price"), {}).get("number")
    t2_entry_time_str = (props.get(col_name("t2_entry_time"), {}).get("date") or {}).get("start")
    t2_last_checked_at_str = (props.get(col_name("t2_last_checked_at"), {}).get("date") or {}).get("start")
    t2_result = _select_name(col_name("t2_result"))
    t2_reason = _rich_text_or_select(col_name("t2_reason"))
    t2_observed_high = props.get(col_name("t2_observed_high"), {}).get("number")
    t2_observed_low = props.get(col_name("t2_observed_low"), {}).get("number")
    t2_final_close = props.get(col_name("t2_final_close"), {}).get("number")

    date = None
    if date_str:
        try:
            date = datetime.fromisoformat(str(date_str).replace("Z", "+00:00"))
        except Exception:
            date = None
    t2_entry_time = None
    if t2_entry_time_str:
        try:
            t2_entry_time = datetime.fromisoformat(str(t2_entry_time_str).replace("Z", "+00:00"))
        except Exception:
            t2_entry_time = None
    t2_last_checked_at = None
    if t2_last_checked_at_str:
        try:
            t2_last_checked_at = datetime.fromisoformat(str(t2_last_checked_at_str).replace("Z", "+00:00"))
        except Exception:
            t2_last_checked_at = None

    return {
        "id": page.get("id"),
        "ticker": ticker,
        "sentiment": sentiment,
        "timeframe": timeframe,
        "order_type": order_type,
        "date": date,
        "tags": tags,
        "asset_class": asset_class,
        "asset_class_values": asset_class_values,
        "sector": sector,
        "sector_values": sector_values,
        "entry": entry,
        "tp": tp,
        "sl": sl,
        "result_auto": result_auto,
        "system_msg": system_msg,
        "reason_code": reason_code,
        "track_mode": "Track2" if str(track_mode).strip().lower() == "track2" else "Track1",
        "t2_bars_limit": t2_bars_limit,
        "t2_threshold_pct": t2_threshold_pct,
        "t2_entry_price": t2_entry_price,
        "t2_entry_time": t2_entry_time,
        "t2_last_checked_at": t2_last_checked_at,
        "t2_result": t2_result,
        "t2_reason": t2_reason,
        "t2_observed_high": t2_observed_high,
        "t2_observed_low": t2_observed_low,
        "t2_final_close": t2_final_close,
    }


def fetch_database_entries(db_id: str, headers: dict, pending_only: bool = False):
    url = f"https://api.notion.com/v1/databases/{db_id}/query"
    pending_filter = {
        "or": [
            {"property": col_name("result_auto"), "select": {"equals": "Pending"}},
            {"property": col_name("result_auto"), "select": {"is_empty": True}},
            {"property": col_name("t2_result"), "select": {"equals": "Pending"}},
            {"property": col_name("t2_result"), "select": {"is_empty": True}},
        ]
    }

    def _is_pending_value(value) -> bool:
        if value is None:
            return True
        if isinstance(value, str):
            text = value.strip()
            return (text == "") or (text.lower() == "pending")
        return False

    for attempt in range(2):
        results = []
        payload = {}
        local_pending_filter_mode = False
        fallback_used = False
        if pending_only:
            payload["filter"] = pending_filter
        try:
            while True:
                try:
                    data = request_with_retry("POST", url, headers=headers, json_payload=payload).json()
                except requests.HTTPError as exc:
                    # Fallback for workspaces that reject select.is_empty filter shape.
                    status_code = exc.response.status_code if exc.response is not None else None
                    if (
                        pending_only
                        and status_code == 400
                        and payload.get("filter") is not None
                        and not fallback_used
                    ):
                        logging.warning(
                            "pending filter rejected by Notion; fallback to full query + local pending filter (preserve empty result_auto)"
                        )
                        # Restart from page 1 because the cursor from a filtered query
                        # may not be valid once we switch to an unfiltered query.
                        results = []
                        payload = {}
                        local_pending_filter_mode = True
                        fallback_used = True
                        continue
                    raise
                batch_rows = [parse_page(page) for page in data.get("results", [])]
                if local_pending_filter_mode and pending_only:
                    batch_rows = [
                        r
                        for r in batch_rows
                        if (
                            (r.get("track_mode") == "Track2" and _is_pending_value(r.get("t2_result")))
                            or (r.get("track_mode") != "Track2" and _is_pending_value(r.get("result_auto")))
                        )
                    ]
                results.extend(batch_rows)
                if not data.get("has_more"):
                    break
                next_cursor = data.get("next_cursor")
                if not next_cursor:
                    break
                next_payload = {"start_cursor": next_cursor}
                if pending_only and not local_pending_filter_mode:
                    next_payload["filter"] = payload.get("filter")
                payload = next_payload
            return results
        except Exception as exc:
            if attempt >= 1:
                raise
            logging.warning("fetch_database_entries failed; retrying err=%s", exc)
            time.sleep(1.0 * (attempt + 1))
    return []


def update_result_auto(page_id: str, result: str, headers: dict, reason_code: Optional[str] = None):
    url = f"https://api.notion.com/v1/pages/{page_id}"
    properties = {col_name("result_auto"): {"select": {"name": result}}}
    if reason_code:
        properties[col_name("reason_code")] = {
            "rich_text": [{"text": {"content": str(reason_code)}}]
        }
    payload = {"properties": properties}
    try:
        request_with_retry("PATCH", url, headers=headers, json_payload=payload)
    except Exception as exc:
        err = str(exc)
        if reason_code and "validation_error" in err:
            # Retry with select type for reason_code if the property is select.
            payload_select_reason = {
                "properties": {
                    col_name("result_auto"): {"select": {"name": result}},
                    col_name("reason_code"): {"select": {"name": str(reason_code)}},
                }
            }
            try:
                request_with_retry("PATCH", url, headers=headers, json_payload=payload_select_reason)
                return
            except Exception:
                payload = {"properties": {col_name("result_auto"): {"select": {"name": result}}}}
                request_with_retry("PATCH", url, headers=headers, json_payload=payload)
                return
        raise


def update_track2_result(
    page_id: str,
    result: str,
    headers: dict,
    reason_code: Optional[str] = None,
    observed_high: Optional[float] = None,
    observed_low: Optional[float] = None,
    final_close: Optional[float] = None,
    last_checked_at: Optional[Union[str, datetime]] = None,
):
    def _to_notion_date_start(value):
        if value is None:
            return None
        ts = pd.to_datetime(value, utc=True, errors="coerce")
        if pd.isna(ts):
            return None
        return ts.isoformat()

    def _build_track2_props(
        *,
        result_value: str,
        reason_value: Optional[str],
        use_select_reason: bool,
        include_reason: bool,
        observed_high_value: Optional[float],
        observed_low_value: Optional[float],
        final_close_value: Optional[float],
        last_checked_start_value: Optional[str],
    ) -> dict:
        props = {col_name("t2_result"): {"select": {"name": result_value}}}
        if include_reason and reason_value:
            if use_select_reason:
                props[col_name("t2_reason")] = {"select": {"name": reason_value}}
            else:
                props[col_name("t2_reason")] = {"rich_text": [{"text": {"content": reason_value}}]}
        if observed_high_value is not None and pd.notna(observed_high_value):
            props[col_name("t2_observed_high")] = {"number": float(observed_high_value)}
        if observed_low_value is not None and pd.notna(observed_low_value):
            props[col_name("t2_observed_low")] = {"number": float(observed_low_value)}
        if final_close_value is not None and pd.notna(final_close_value):
            props[col_name("t2_final_close")] = {"number": float(final_close_value)}
        if last_checked_start_value:
            props[col_name("t2_last_checked_at")] = {"date": {"start": last_checked_start_value}}
        return props

    url = f"https://api.notion.com/v1/pages/{page_id}"
    last_checked_start = _to_notion_date_start(last_checked_at)
    reason_value = str(reason_code) if reason_code else None
    properties = _build_track2_props(
        result_value=result,
        reason_value=reason_value,
        use_select_reason=False,
        include_reason=True,
        observed_high_value=observed_high,
        observed_low_value=observed_low,
        final_close_value=final_close,
        last_checked_start_value=last_checked_start,
    )
    payload = {"properties": properties}
    try:
        request_with_retry("PATCH", url, headers=headers, json_payload=payload)
    except Exception as exc:
        err = str(exc)
        if reason_code and "validation_error" in err:
            payload_select_reason = {
                "properties": _build_track2_props(
                    result_value=result,
                    reason_value=reason_value,
                    use_select_reason=True,
                    include_reason=True,
                    observed_high_value=observed_high,
                    observed_low_value=observed_low,
                    final_close_value=final_close,
                    last_checked_start_value=last_checked_start,
                )
            }
            try:
                request_with_retry("PATCH", url, headers=headers, json_payload=payload_select_reason)
                return
            except Exception:
                payload_minimal = {
                    "properties": _build_track2_props(
                        result_value=result,
                        reason_value=None,
                        use_select_reason=False,
                        include_reason=False,
                        observed_high_value=observed_high,
                        observed_low_value=observed_low,
                        final_close_value=final_close,
                        last_checked_start_value=last_checked_start,
                    )
                }
                request_with_retry("PATCH", url, headers=headers, json_payload=payload_minimal)
                return
        raise


def update_system_msg(page_id: str, message: str, headers: dict):
    if not page_id or not message:
        return
    url = f"https://api.notion.com/v1/pages/{page_id}"
    payload = {
        "properties": {
            col_name("system_msg"): {
                "rich_text": [
                    {"text": {"content": str(message)}},
                ]
            }
        }
    }
    try:
        request_with_retry("PATCH", url, headers=headers, json_payload=payload)
    except Exception as exc:
        err = str(exc)
        # Optional column: do not break daemon if System_Msg type/schema is incompatible.
        system_key = col_name("system_msg")
        err_lower = err.lower()
        if system_key.lower() in err_lower or "system_msg" in err_lower or "system msg" in err_lower:
            logging.warning("skip System_Msg write page=%s err=%s", page_id, err)
            return
        raise


def normalize_ticker_key(value: str) -> str:
    return shared_normalize_ticker_key(value)


def normalize_symbol(ticker: str) -> str:
    return shared_normalize_symbol(ticker)


def classify_asset(ticker: str):
    return shared_classify_asset(ticker)


def fetch_prices(ticker: str, start: datetime, end: datetime, interval: str, twelve_api_key: str, fugle_api_key: str):
    resolution = _MARKET_DATA_RESOLVER.resolve(
        MarketDataRequest(
            ticker=ticker,
            start=start,
            end=end,
            interval=interval,
            twelve_api_key=twelve_api_key,
            fugle_api_key=fugle_api_key,
        )
    )
    for event in resolution.events:
        if event.status in {
            "network_error",
            "http_error",
            "api_error",
            "auth_error",
            "rate_limit",
            "error",
        }:
            logging.warning(
                "%s fetch failed ticker=%s interval=%s err=%s",
                event.source.lower(),
                ticker,
                interval,
                event.detail,
            )
    return resolution.prices


class NotionTradingThesisRepository:
    def __init__(self, db_id: str, headers: dict, *, after_write=None) -> None:
        self._db_id = db_id
        self._headers = headers
        self._after_write = after_write or (lambda: None)

    def list_candidates(self, *, pending_only: bool) -> list[dict]:
        return fetch_database_entries(
            self._db_id,
            self._headers,
            pending_only=pending_only,
        )

    def write_track1(self, trading_thesis: dict, outcome):
        try:
            update_result_auto(
                trading_thesis["id"],
                outcome.outcome_label,
                self._headers,
                reason_code=outcome.reason_code,
            )
            self._after_write()
            return outcome.outcome_label
        except Exception as exc:
            error = str(exc)
            if (
                "validation_error" in error
                and outcome.outcome_label in {"Data_Missing", "Invalid_Entry"}
            ):
                logging.warning(
                    "semantic label unsupported in Notion; skip write page=%s outcome=%s reason=%s err=%s",
                    trading_thesis.get("id"),
                    outcome.outcome_label,
                    outcome.reason_code,
                    error,
                )
                return None
            raise

    def write_track2(
        self,
        trading_thesis: dict,
        outcome,
        *,
        last_checked_at: str,
    ):
        try:
            update_track2_result(
                trading_thesis["id"],
                outcome.outcome_label,
                self._headers,
                reason_code=outcome.reason_code or None,
                observed_high=outcome.observed_high,
                observed_low=outcome.observed_low,
                final_close=outcome.final_close,
                last_checked_at=last_checked_at,
            )
            self._after_write()
            return outcome.outcome_label
        except Exception as exc:
            error = str(exc)
            if (
                "validation_error" in error
                and outcome.outcome_label in {"Data_Missing", "Invalid_Entry"}
            ):
                logging.warning(
                    "semantic T2 label unsupported in Notion; skip write page=%s outcome=%s reason=%s err=%s",
                    trading_thesis.get("id"),
                    outcome.outcome_label,
                    outcome.reason_code,
                    error,
                )
                return None
            raise

    def write_system_message(self, trading_thesis: dict, message: str) -> None:
        update_system_msg(trading_thesis.get("id"), message, self._headers)


class DaemonMarketData:
    def __init__(self, cfg: dict) -> None:
        self._twelve_api_key = str(cfg.get("TWELVE_DATA_API_KEY") or "")
        self._fugle_api_key = str(cfg.get("FUGLE_API_KEY") or "")

    def fetch(
        self,
        ticker: str,
        start: datetime,
        end: datetime,
        interval: str,
    ) -> pd.DataFrame:
        return fetch_prices(
            ticker,
            start,
            end,
            interval,
            self._twelve_api_key,
            self._fugle_api_key,
        )


class UtcClock:
    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)


class DaemonCycleControl:
    def __init__(self, cfg: dict) -> None:
        self._settings = CycleSettings(
            selected_interval=str(cfg.get("SELECTED_INTERVAL") or "1d"),
            timeout_bars=int(cfg.get("GRADE_TIMEOUT_BARS", 0) or 0),
            max_theses=int(
                cfg.get("BATCH_SIZE", MAX_RECORDS_PER_SCAN_DEFAULT)
                or MAX_RECORDS_PER_SCAN_DEFAULT
            ),
        )
        self._write_delay_min = float(
            cfg.get("NOTION_WRITE_DELAY_MIN", NOTION_WRITE_DELAY_MIN_SECONDS)
            or NOTION_WRITE_DELAY_MIN_SECONDS
        )
        self._write_delay_max = float(
            cfg.get("NOTION_WRITE_DELAY_MAX", NOTION_WRITE_DELAY_MAX_SECONDS)
            or NOTION_WRITE_DELAY_MAX_SECONDS
        )

    def settings(self) -> CycleSettings:
        return self._settings

    def after_write(self) -> None:
        lower = max(0.0, self._write_delay_min)
        upper = max(lower, self._write_delay_max)
        time.sleep(random.uniform(lower, upper))


def daemon_loop():
    logging.info("daemon started")
    global ACTIVE_COLUMN_MAPPING
    while True:
        try:
            cfg = load_dashboard_config()
            control = load_control()
            notion_token = str(cfg.get("NOTION_TOKEN") or "").strip()
            db_id = str(cfg.get("DB_ID") or "").strip()
            now = datetime.now(timezone.utc)

            if not notion_token or not db_id:
                control["status"] = "ERROR: Missing NOTION_TOKEN or DB_ID"
                control["last_error"] = "Missing NOTION_TOKEN or DB_ID"
                control["last_error_at"] = datetime.now(timezone.utc).isoformat()
                save_control(control)
                time.sleep(2)
                continue

            schema_mgr = build_schema_manager(cfg)
            with ACTIVE_COLUMN_MAPPING_LOCK:
                ACTIVE_COLUMN_MAPPING = SchemaManager.normalize_mapping(cfg.get("COLUMN_MAPPING"))

            interval_minutes = int(control.get("interval_minutes", 0) or 0)
            run_now_trigger = bool(control.get("run_now_trigger", False))
            force_all_trigger = bool(control.get("force_all_trigger", False))
            last_run_time = parse_iso_ts(str(control.get("last_run_time") or ""))
            next_retry_time = parse_iso_ts(str(control.get("next_retry_time") or ""))

            if (
                next_retry_time is not None
                and now < next_retry_time
                and not run_now_trigger
                and not force_all_trigger
            ):
                time.sleep(2)
                continue

            should_run = False
            force_recalc = False
            run_mode = "scheduled_pending_only"
            if force_all_trigger:
                should_run = True
                force_recalc = True
                run_mode = "manual_force_all_recalc"
                control["force_all_trigger"] = False
                control["run_now_trigger"] = False
                save_control(control)
            elif run_now_trigger:
                should_run = True
                force_recalc = False
                run_mode = "manual_pending_only"
                control["run_now_trigger"] = False
                save_control(control)
            elif interval_minutes > 0:
                if last_run_time is None:
                    should_run = True
                else:
                    elapsed_sec = (now - last_run_time).total_seconds()
                    should_run = elapsed_sec >= interval_minutes * 60

            if should_run:
                try:
                    logging.info("scan start mode=%s", run_mode)
                    headers = notion_headers(notion_token)
                    try:
                        if schema_mgr is None:
                            raise RuntimeError("schema manager unavailable")
                        # Cross-process sync: UI/API may update manual overrides in schema_cache.json.
                        # Reload cache each scan round so daemon picks up the latest bindings.
                        schema_mgr.load_cache()
                        schema_check = schema_mgr.check(force=False)
                        with ACTIVE_COLUMN_MAPPING_LOCK:
                            ACTIVE_COLUMN_MAPPING = SchemaManager.normalize_mapping(
                                schema_check.get("display_names") or cfg.get("COLUMN_MAPPING")
                            )
                        schema_ok = bool(schema_check.get("ok"))
                        schema_msg = str(schema_check.get("message") or "SchemaError: schema unavailable")
                    except Exception as schema_exc:
                        logging.warning("schema manager status failed err=%s", schema_exc)
                        schema_ok, schema_msg = False, f"SchemaError: {schema_exc}"
                    if not schema_ok:
                        logging.error("schema check failed: %s", schema_msg)
                        latest = load_control()
                        latest["status"] = f"ERROR: {schema_msg}"
                        latest["last_error"] = str(schema_msg)
                        latest["last_error_at"] = datetime.now(timezone.utc).isoformat()
                        save_control(latest)
                        time.sleep(2)
                        continue
                    cycle_control = DaemonCycleControl(cfg)
                    cycle = GradingCycle(
                        NotionTradingThesisRepository(
                            db_id,
                            headers,
                            after_write=cycle_control.after_write,
                        ),
                        DaemonMarketData(cfg),
                        UtcClock(),
                        cycle_control,
                    )
                    cycle_result = cycle.run(force_recalc=force_recalc)
                    updated = cycle_result.updated
                    scanned_groups = cycle_result.scanned_groups
                    logging.info(
                        "scan finished mode=%s groups=%s updated=%s",
                        run_mode,
                        scanned_groups,
                        updated,
                    )
                    latest = load_control()
                    latest["status"] = schema_msg if schema_msg.startswith("SchemaWarn") else "OK"
                    latest["last_error"] = ""
                    latest["last_error_at"] = ""
                    latest["next_retry_time"] = ""
                    save_control(latest)
                except Exception as exc:
                    logging.exception("scan failed err=%s", exc)
                    latest = load_control()
                    latest["status"] = f"ERROR: {str(exc)}"
                    latest["last_error"] = str(exc)
                    latest["last_error_at"] = datetime.now(timezone.utc).isoformat()
                    retry_after_seconds = None
                    if isinstance(exc, requests.HTTPError) and exc.response is not None and exc.response.status_code == 429:
                        try:
                            retry_after_seconds = float(exc.response.headers.get("Retry-After") or 0.0)
                        except (TypeError, ValueError):
                            retry_after_seconds = 0.0
                    if retry_after_seconds and retry_after_seconds > 0:
                        latest["next_retry_time"] = (
                            datetime.now(timezone.utc) + timedelta(seconds=retry_after_seconds)
                        ).isoformat()
                    save_control(latest)
                finally:
                    latest = load_control()
                    # Intentional rate-limit behavior: even failed runs advance last_run_time
                    # to avoid tight retry loops against Notion / market data providers.
                    latest["last_run_time"] = datetime.now(timezone.utc).isoformat()
                    save_control(latest)
            time.sleep(2)
        except Exception as exc:
            logging.exception("loop error err=%s", exc)
            time.sleep(2)


if __name__ == "__main__":
    daemon_loop()
