from __future__ import annotations

import os
import time
import json
import logging
import threading
from typing import Optional, Union
import re
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, parse_qs, urlparse

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st
from dotenv import load_dotenv
from streamlit_lightweight_charts import renderLightweightCharts
from streamlit_searchbox import st_searchbox

load_dotenv()

ENV_NOTION_TOKEN = os.getenv("NOTION_TOKEN", "")
ENV_DB_ID = os.getenv("NEWS_ALPHA_DB_ID", "")
NOTION_VERSION = os.getenv("NOTION_VERSION", "2022-06-28")
ENV_TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY", "")
ENV_FUGLE_API_KEY = os.getenv("FUGLE_API_KEY", "")
LOCAL_CFG_PATH = Path(__file__).resolve().parents[1] / ".streamlit" / "dashboard_config.json"
ROUTING_TABLE_PATH = Path(__file__).resolve().parents[1] / ".streamlit" / "api_routing.json"
ROUTING_STATS_PATH = Path(__file__).resolve().parents[1] / ".streamlit" / "api_routing_stats.json"
OPTIMIZATION_MEMORY_PATH = Path(__file__).resolve().parents[1] / ".streamlit" / "optimization_memory.json"
DAEMON_CONTROL_PATH = Path(__file__).resolve().parents[1] / ".streamlit" / "daemon_control.json"
DAEMON_CONTROL_LOCK_PATH = Path(__file__).resolve().parents[1] / ".streamlit" / "daemon_control.lock"
SCHEMA_CACHE_PATH = Path(__file__).resolve().parents[1] / ".streamlit" / "schema_cache.json"

INTERVAL_OPTIONS = ["1d", "4h", "1h", "15m", "5m", "1m"]
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))
from backend.schema_manager import SchemaManager
from backend.io_utils import FileLock
from backend.grading import (
    PriceTimeline,
    infer_order_type as shared_infer_order_type,
)
from backend.grading_cycle import (
    DASHBOARD_BACKGROUND_CYCLE_POLICY,
    DASHBOARD_FORCE_TRADE_CYCLE_POLICY,
    DASHBOARD_INTERACTIVE_CYCLE_POLICY,
    resolve_force_snap,
)
from dashboard_grading import (
    BackgroundDashboardMarketData,
    DashboardCycleControl,
    DashboardOutcomeCollector,
    DashboardOutcomeRenderer,
    DashboardTradingThesisRepository,
    InteractiveDashboardMarketData,
    SilentDashboardObserver,
    run_dashboard_grading_cycle,
)
from backend.market_data import (
    MarketDataRequest,
    create_dashboard_market_data_resolver,
)
from backend.io_utils import (
    request_with_retry,
    get_plain_text,
    load_json_file,
    save_json_atomic,
)
from backend.ticker_utils import (
    classify_asset as shared_classify_asset,
    normalize_ticker_for_storage as shared_normalize_ticker_for_storage,
    normalize_ticker_key as shared_normalize_ticker_key,
)

ACTIVE_COLUMN_MAPPING = SchemaManager.default_mapping()
logger = logging.getLogger(__name__)


def _configured_market_data_ttl(_interval: str) -> int:
    return int(st.session_state.get("cfg_cache_ttl_seconds", 600) or 600)


_MARKET_DATA_RESOLVER = create_dashboard_market_data_resolver(
    request_with_retry,
    cache_ttl_seconds=_configured_market_data_ttl,
)


def _dashboard_runtime_adapter(name: str):
    adapters = st.session_state.get("_dashboard_runtime_adapters", {})
    if not isinstance(adapters, dict):
        return None
    adapter = adapters.get(name)
    return adapter if callable(adapter) else None


def load_local_cfg():
    data = load_json_file(LOCAL_CFG_PATH, {})
    return data if isinstance(data, dict) else {}


def get_backend_api_headers():
    cfg = load_local_cfg()
    api_key = str(
        cfg.get("API_KEY")
        or cfg.get("NEWS_ALPHA_API_KEY")
        or os.getenv("NEWS_ALPHA_API_KEY")
        or ""
    ).strip() if isinstance(cfg, dict) else str(os.getenv("NEWS_ALPHA_API_KEY") or "").strip()
    headers = {}
    if api_key:
        headers["x-api-key"] = api_key
    return headers


def save_local_cfg(cfg: dict):
    try:
        save_json_atomic(
            LOCAL_CFG_PATH,
            cfg,
            lock_path=Path(str(LOCAL_CFG_PATH) + ".lock"),
            timeout=5,
        )
    except Exception as exc:
        logger.warning("save_local_cfg failed: %s", exc)


def col_name(internal_key: str) -> str:
    return SchemaManager.column_name(ACTIVE_COLUMN_MAPPING, internal_key)


def load_routing_table():
    try:
        if ROUTING_TABLE_PATH.exists():
            data = json.loads(ROUTING_TABLE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        return {}
    return {}


def save_routing_table(ticker, interval, source_name):
    if not ticker or not interval or not source_name:
        return
    try:
        ROUTING_TABLE_PATH.parent.mkdir(parents=True, exist_ok=True)
        table = load_routing_table()
        table[f"{ticker}_{interval}"] = source_name
        save_json_atomic(
            ROUTING_TABLE_PATH,
            table,
            lock_path=Path(str(ROUTING_TABLE_PATH) + ".lock"),
            timeout=5,
        )
    except Exception:
        pass


def _load_routing_stats():
    try:
        if ROUTING_STATS_PATH.exists():
            data = json.loads(ROUTING_STATS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                events = data.get("events") or []
                if isinstance(events, list):
                    return {"events": events}
    except Exception:
        return {"events": []}
    return {"events": []}


def _save_routing_stats(stats: dict):
    try:
        ROUTING_STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
        ROUTING_STATS_PATH.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def record_routing_event(event: str, source: Optional[str] = None):
    if event not in {"hit", "miss", "cold"}:
        return
    stats = _load_routing_stats()
    events = stats.get("events") or []
    events.append(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "source": source or "",
        }
    )
    stats["events"] = events[-100:]
    _save_routing_stats(stats)


def summarize_routing_stats():
    stats = _load_routing_stats()
    events = stats.get("events") or []
    if not events:
        return {"total": 0, "hit": 0, "miss": 0, "cold": 0, "hit_rate": 0.0}
    hit = sum(1 for e in events if e.get("event") == "hit")
    miss = sum(1 for e in events if e.get("event") == "miss")
    cold = sum(1 for e in events if e.get("event") == "cold")
    denom = hit + miss
    hit_rate = (hit / denom) if denom > 0 else 0.0
    return {"total": len(events), "hit": hit, "miss": miss, "cold": cold, "hit_rate": hit_rate}


def load_optimization_memory():
    try:
        if OPTIMIZATION_MEMORY_PATH.exists():
            data = json.loads(OPTIMIZATION_MEMORY_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        return {}
    return {}


def save_optimization_memory(memory: dict):
    if not isinstance(memory, dict):
        return
    try:
        OPTIMIZATION_MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        OPTIMIZATION_MEMORY_PATH.write_text(json.dumps(memory, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def load_daemon_control():
    default = {
        "interval_minutes": 0,
        "run_now_trigger": False,
        "force_all_trigger": False,
        "next_retry_time": "",
        "last_run_time": "",
        "status": "OK",
        "last_error": "",
        "last_error_at": "",
    }
    try:
        if DAEMON_CONTROL_PATH.exists():
            with locked_daemon_control_file():
                data = json.loads(DAEMON_CONTROL_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                out = dict(default)
                out.update(data)
                out["interval_minutes"] = int(out.get("interval_minutes", 0) or 0)
                out["run_now_trigger"] = bool(out.get("run_now_trigger", False))
                out["force_all_trigger"] = bool(out.get("force_all_trigger", False))
                out["next_retry_time"] = str(out.get("next_retry_time") or "")
                out["last_run_time"] = str(out.get("last_run_time") or "")
                out["status"] = str(out.get("status") or "OK")
                out["last_error"] = str(out.get("last_error") or "")
                out["last_error_at"] = str(out.get("last_error_at") or "")
                return out
    except Exception:
        pass
    return dict(default)


def save_daemon_control(control: dict):
    if not isinstance(control, dict):
        return
    try:
        payload = {
            "interval_minutes": int(control.get("interval_minutes", 0) or 0),
            "run_now_trigger": bool(control.get("run_now_trigger", False)),
            "force_all_trigger": bool(control.get("force_all_trigger", False)),
            "next_retry_time": str(control.get("next_retry_time") or ""),
            "last_run_time": str(control.get("last_run_time") or ""),
            "status": str(control.get("status") or "OK"),
            "last_error": str(control.get("last_error") or ""),
            "last_error_at": str(control.get("last_error_at") or ""),
        }
        save_json_atomic(
            DAEMON_CONTROL_PATH,
            payload,
            lock_path=DAEMON_CONTROL_LOCK_PATH,
            timeout=5,
        )
    except Exception as exc:
        logger.warning("save_daemon_control failed: %s", exc)


def locked_daemon_control_file():
    DAEMON_CONTROL_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    return FileLock(str(DAEMON_CONTROL_LOCK_PATH), timeout=5)


def notion_request(method, url, notion_headers, payload=None):
    resp = request_with_retry(method, url, headers=notion_headers, json_payload=payload)
    return resp.json()


def _dashboard_schema_manager(db_id: str, notion_headers: dict) -> SchemaManager:
    manager = SchemaManager(
        db_id=db_id,
        notion_headers=notion_headers,
        cache_path=str(SCHEMA_CACHE_PATH),
        config_path=str(LOCAL_CFG_PATH),
        notion_request=notion_request,
    )
    manager.load_cache()
    return manager


def check_dashboard_schema(db_id: str, notion_headers: dict) -> dict:
    adapter = _dashboard_runtime_adapter("check_schema")
    if adapter is not None:
        return adapter(db_id, notion_headers)
    return _dashboard_schema_manager(db_id, notion_headers).check(
        mapping=ACTIVE_COLUMN_MAPPING,
        force=True,
    )


def format_display_datetime(value):
    if value is None:
        return ""
    ts = pd.to_datetime(value, errors="coerce")
    if pd.isna(ts):
        return ""
    if getattr(ts, "tzinfo", None) is not None:
        ts = ts.tz_convert(None)
    return ts.strftime("%Y/%m/%d %I:%M %p")


def normalize_ticker_key(value: str) -> str:
    return shared_normalize_ticker_key(value)


def parse_page(page):
    props = page.get("properties", {})
    def _select_name(*keys):
        for key in keys:
            name = (props.get(key, {}).get("select") or {}).get("name")
            if name:
                return name
        return None
    def _select_or_multi_select_values(key: str):
        p = props.get(key, {}) or {}
        select_name = (p.get("select") or {}).get("name")
        if select_name:
            return [str(select_name)]
        ms = p.get("multi_select") or []
        values = []
        for item in ms:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if name:
                values.append(name)
        return values
    def _select_or_multi_select_name(key: str):
        values = _select_or_multi_select_values(key)
        return values[0] if values else None
    def _rich_text_or_select(key: str):
        txt = get_plain_text(props.get(key, {}).get("rich_text", []))
        if txt:
            return txt
        return (props.get(key, {}).get("select") or {}).get("name") or ""
    def _formula_text(key: str):
        formula = (props.get(key, {}) or {}).get("formula") or {}
        if not isinstance(formula, dict):
            return None
        formula_type = formula.get("type")
        if formula_type == "string":
            return formula.get("string")
        if formula_type == "number":
            val = formula.get("number")
            return None if val is None else str(val)
        if formula_type == "boolean":
            val = formula.get("boolean")
            return None if val is None else str(val)
        if formula_type == "date":
            date_obj = formula.get("date") or {}
            return date_obj.get("start")
        # Backward/variant-safe fallback.
        return formula.get("string")

    title = get_plain_text(props.get(col_name("title"), {}).get("title", []))
    ticker = get_plain_text(props.get(col_name("ticker"), {}).get("rich_text", []))
    sentiment = (props.get(col_name("sentiment"), {}).get("select") or {}).get("name")
    order_type = (props.get(col_name("order_type"), {}).get("select") or {}).get("name") or "Limit"
    asset_class_values = _select_or_multi_select_values(col_name("asset_class"))
    asset_class = asset_class_values[0] if asset_class_values else None
    sector_values = _select_or_multi_select_values(col_name("sector"))
    sector = sector_values[0] if sector_values else None
    confidence = props.get(col_name("confidence"), {}).get("number")
    mindset = props.get(col_name("mindset"), {}).get("number")
    timeframe = (props.get(col_name("timeframe"), {}).get("select") or {}).get("name")
    entry = props.get(col_name("entry"), {}).get("number")
    tp = props.get(col_name("tp"), {}).get("number")
    sl = props.get(col_name("sl"), {}).get("number")
    tags = [t.get("name") for t in (props.get(col_name("tags"), {}).get("multi_select") or [])]
    note = get_plain_text(props.get(col_name("note"), {}).get("rich_text", []))
    system_msg = get_plain_text(props.get(col_name("system_msg"), {}).get("rich_text", []))
    reason_code = get_plain_text(props.get(col_name("reason_code"), {}).get("rich_text", []))
    if not reason_code:
        reason_code = (props.get(col_name("reason_code"), {}).get("select") or {}).get("name") or ""
    origin_url = props.get(col_name("origin_url"), {}).get("url")
    date_str = (props.get(col_name("date"), {}).get("date") or {}).get("start")
    result_manual = _select_name(col_name("result_manual"))
    result_auto = _select_name(col_name("result_auto"))
    manual_reason = _select_name(col_name("manual_reason"))
    track_mode = _select_name(col_name("track_mode")) or "Track1"
    t2_bars_limit = props.get(col_name("t2_bars_limit"), {}).get("number")
    t2_threshold_pct = props.get(col_name("t2_threshold_pct"), {}).get("number")
    t2_entry_price = props.get(col_name("t2_entry_price"), {}).get("number")
    t2_entry_time_str = (props.get(col_name("t2_entry_time"), {}).get("date") or {}).get("start")
    t2_observed_high = props.get(col_name("t2_observed_high"), {}).get("number")
    t2_observed_low = props.get(col_name("t2_observed_low"), {}).get("number")
    t2_final_close = props.get(col_name("t2_final_close"), {}).get("number")
    t2_last_checked_at_str = (props.get(col_name("t2_last_checked_at"), {}).get("date") or {}).get("start")
    t2_result = _select_name(col_name("t2_result"))
    t2_reason = _rich_text_or_select(col_name("t2_reason"))
    result_formula = _formula_text(col_name("result"))
    result = result_formula or result_manual or result_auto
    ret = props.get(col_name("return"), {}).get("number")

    date = None
    if date_str:
        try:
            date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except ValueError:
            date = None
    t2_entry_time = None
    if t2_entry_time_str:
        try:
            t2_entry_time = datetime.fromisoformat(str(t2_entry_time_str).replace("Z", "+00:00"))
        except ValueError:
            t2_entry_time = None
    t2_last_checked_at = None
    if t2_last_checked_at_str:
        try:
            t2_last_checked_at = datetime.fromisoformat(str(t2_last_checked_at_str).replace("Z", "+00:00"))
        except ValueError:
            t2_last_checked_at = None

    return {
        "id": page.get("id"),
        "url": page.get("url"),
        "title": title,
        "ticker": ticker,
        "sentiment": sentiment,
        "order_type": order_type,
        "asset_class": asset_class,
        "asset_class_values": asset_class_values,
        "sector": sector,
        "sector_values": sector_values,
        "confidence": confidence,
        "mindset": mindset,
        "timeframe": timeframe,
        "entry": entry,
        "tp": tp,
        "sl": sl,
        "tags": tags,
        "note": note,
        "system_msg": system_msg,
        "reason_code": reason_code,
        "origin_url": origin_url,
        "date": date,
        "result": result,
        "result_manual": result_manual,
        "result_auto": result_auto,
        "manual_reason": manual_reason,
        "track_mode": "Track2" if str(track_mode).strip().lower() == "track2" else "Track1",
        "t2_bars_limit": t2_bars_limit,
        "t2_threshold_pct": t2_threshold_pct,
        "t2_entry_price": t2_entry_price,
        "t2_entry_time": t2_entry_time,
        "t2_observed_high": t2_observed_high,
        "t2_observed_low": t2_observed_low,
        "t2_final_close": t2_final_close,
        "t2_last_checked_at": t2_last_checked_at,
        "t2_result": t2_result,
        "t2_reason": t2_reason,
        "return": ret,
    }


def fetch_database_entries(db_id, notion_headers):
    adapter = _dashboard_runtime_adapter("fetch_records")
    if adapter is not None:
        return adapter(db_id, notion_headers)
    url = f"https://api.notion.com/v1/databases/{db_id}/query"
    results = []
    payload = {}
    while True:
        data = notion_request("POST", url, notion_headers, payload)
        results.extend([parse_page(page) for page in data.get("results", [])])
        if not data.get("has_more"):
            break
        next_cursor = data.get("next_cursor")
        if not next_cursor:
            break
        payload = {"start_cursor": next_cursor}
    return results


def _friendly_field_name(raw_name: str) -> str:
    label_map = {
        "Mindset": "心態分數",
        "Return": "報酬率",
        "Ticker": "Ticker",
        "Date": "日期時間",
        "Timeframe": "週期",
        "Result_Auto": "自動結果",
        "Result_Manual": "人工結果",
        "Manual_Reason": "人工原因",
        "System_Msg": "系統訊息",
        "Entry": "進場價",
        "TP": "止盈",
        "SL": "止損",
    }
    key = str(raw_name or "").strip()
    return label_map.get(key, key)


def format_schema_notice(raw_msg: str) -> str:
    msg = str(raw_msg or "").strip()
    if not msg:
        return "欄位檢查發現問題，請檢查 Notion 欄位設定。"
    if msg.startswith("SchemaWarn:"):
        body = msg.replace("SchemaWarn:", "", 1).strip()
        chunks = [c.strip() for c in body.split(";") if c.strip()]
        output = []
        for chunk in chunks:
            if chunk.startswith("missing="):
                fields = [f.strip() for f in chunk.replace("missing=", "", 1).split(",") if f.strip()]
                fields = [_friendly_field_name(f) for f in fields]
                output.append(f"缺少欄位：{'、'.join(fields)}")
            elif chunk.startswith("type_mismatch="):
                output.append("部分欄位型別不一致，請檢查欄位類型")
            else:
                output.append(chunk)
        suffix = "。請到 Notion 補齊欄位，或到「欄位對照表管理」更新對照。"
        return "欄位設定提醒：" + "；".join(output) + suffix
    if msg.startswith("SchemaError:"):
        body = msg.replace("SchemaError:", "", 1).strip()
        return f"欄位設定錯誤：{body}。請先修正 Notion 欄位後再重試。"
    return msg


def update_result(page_id, result, ret_pct, notion_headers, reason_code=None):
    adapter = _dashboard_runtime_adapter("write_track1")
    if adapter is not None:
        return adapter(page_id, result, ret_pct, notion_headers, reason_code)
    url = f"https://api.notion.com/v1/pages/{page_id}"
    properties = {col_name("result_auto"): {"select": {"name": result}}}
    if ret_pct is not None:
        properties[col_name("return")] = {"number": ret_pct}
    if reason_code:
        properties[col_name("reason_code")] = {
            "rich_text": [{"text": {"content": str(reason_code)}}]
        }
    payload = {"properties": properties}
    try:
        notion_request("PATCH", url, notion_headers, payload)
    except Exception as exc:
        err = str(exc)
        if reason_code and "validation_error" in err:
            payload_select_reason = {
                "properties": {
                    col_name("result_auto"): {"select": {"name": result}},
                    col_name("reason_code"): {"select": {"name": str(reason_code)}},
                }
            }
            if ret_pct is not None:
                payload_select_reason["properties"][col_name("return")] = {"number": ret_pct}
            try:
                notion_request("PATCH", url, notion_headers, payload_select_reason)
                return
            except Exception:
                payload = {"properties": {col_name("result_auto"): {"select": {"name": result}}}}
                if ret_pct is not None:
                    payload["properties"][col_name("return")] = {"number": ret_pct}
                notion_request("PATCH", url, notion_headers, payload)
                return
        raise


def update_track2_result(
    page_id,
    result,
    notion_headers,
    reason_code=None,
    observed_high=None,
    observed_low=None,
    final_close=None,
    last_checked_at=None,
):
    adapter = _dashboard_runtime_adapter("write_track2")
    if adapter is not None:
        return adapter(
            page_id,
            result,
            notion_headers,
            reason_code,
            observed_high,
            observed_low,
            final_close,
            last_checked_at,
        )
    url = f"https://api.notion.com/v1/pages/{page_id}"
    properties = {col_name("t2_result"): {"select": {"name": result}}}
    if reason_code:
        properties[col_name("t2_reason")] = {
            "rich_text": [{"text": {"content": str(reason_code)}}]
        }
    if observed_high is not None and pd.notna(observed_high):
        properties[col_name("t2_observed_high")] = {"number": float(observed_high)}
    if observed_low is not None and pd.notna(observed_low):
        properties[col_name("t2_observed_low")] = {"number": float(observed_low)}
    if final_close is not None and pd.notna(final_close):
        properties[col_name("t2_final_close")] = {"number": float(final_close)}
    if last_checked_at is not None:
        try:
            ts = pd.to_datetime(last_checked_at, utc=True, errors="coerce")
            if pd.notna(ts):
                properties[col_name("t2_last_checked_at")] = {"date": {"start": ts.isoformat()}}
        except Exception:
            pass
    payload = {"properties": properties}
    try:
        notion_request("PATCH", url, notion_headers, payload)
    except Exception as exc:
        err = str(exc)
        if reason_code and "validation_error" in err:
            payload_select_reason = {
                "properties": {
                    col_name("t2_result"): {"select": {"name": result}},
                    col_name("t2_reason"): {"select": {"name": str(reason_code)}},
                }
            }
            if observed_high is not None and pd.notna(observed_high):
                payload_select_reason["properties"][col_name("t2_observed_high")] = {
                    "number": float(observed_high)
                }
            if observed_low is not None and pd.notna(observed_low):
                payload_select_reason["properties"][col_name("t2_observed_low")] = {
                    "number": float(observed_low)
                }
            if final_close is not None and pd.notna(final_close):
                payload_select_reason["properties"][col_name("t2_final_close")] = {
                    "number": float(final_close)
                }
            if last_checked_at is not None:
                try:
                    ts = pd.to_datetime(last_checked_at, utc=True, errors="coerce")
                    if pd.notna(ts):
                        payload_select_reason["properties"][col_name("t2_last_checked_at")] = {
                            "date": {"start": ts.isoformat()}
                        }
                except Exception:
                    pass
            try:
                notion_request("PATCH", url, notion_headers, payload_select_reason)
                return
            except Exception:
                payload = {"properties": {col_name("t2_result"): {"select": {"name": result}}}}
                if observed_high is not None and pd.notna(observed_high):
                    payload["properties"][col_name("t2_observed_high")] = {"number": float(observed_high)}
                if observed_low is not None and pd.notna(observed_low):
                    payload["properties"][col_name("t2_observed_low")] = {"number": float(observed_low)}
                if final_close is not None and pd.notna(final_close):
                    payload["properties"][col_name("t2_final_close")] = {"number": float(final_close)}
                if last_checked_at is not None:
                    try:
                        ts = pd.to_datetime(last_checked_at, utc=True, errors="coerce")
                        if pd.notna(ts):
                            payload["properties"][col_name("t2_last_checked_at")] = {
                                "date": {"start": ts.isoformat()}
                            }
                    except Exception:
                        pass
                notion_request("PATCH", url, notion_headers, payload)
                return
        raise


def update_system_msg(page_id, message, notion_headers):
    adapter = _dashboard_runtime_adapter("write_system_message")
    if adapter is not None:
        return adapter(page_id, message, notion_headers)
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
        notion_request("PATCH", url, notion_headers, payload)
    except Exception as exc:
        err = str(exc)
        # Optional column: never block runtime if System_Msg is missing or wrong type.
        if "validation_error" in err or "System_Msg" in err:
            return
        raise


def classify_asset(ticker: str):
    return shared_classify_asset(ticker)


def normalize_symbol(ticker: str) -> str:
    return shared_normalize_ticker_for_storage(ticker)


def to_unix_seconds(ts_value):
    ts = pd.to_datetime(ts_value, errors="coerce", utc=True)
    if pd.isna(ts):
        return None
    return int(ts.timestamp())


def convert_price_df_to_tv(price_df: pd.DataFrame):
    if price_df is None or price_df.empty:
        return []
    # Keep candles strictly ordered by time for stable intraday rendering.
    ordered_df = price_df.copy()
    ordered_df["date"] = pd.to_datetime(ordered_df["date"], errors="coerce", utc=True)
    ordered_df = ordered_df.dropna(subset=["date"]).sort_values("date", ascending=True)

    tv_rows = []
    for row in ordered_df.itertuples(index=False):
        sec = to_unix_seconds(row.date)
        if sec is None:
            continue
        tv_rows.append(
            {
                "time": sec,
                "open": float(row.open),
                "high": float(row.high),
                "low": float(row.low),
                "close": float(row.close),
            }
        )
    # De-duplicate same-second points to avoid broken bars.
    dedup = {}
    for item in tv_rows:
        dedup[item["time"]] = item
    return [dedup[k] for k in sorted(dedup.keys())]


def convert_volume_df_to_tv(price_df: pd.DataFrame):
    if price_df is None or price_df.empty or "volume" not in price_df.columns:
        return []
    ordered_df = price_df.copy()
    ordered_df["date"] = pd.to_datetime(ordered_df["date"], errors="coerce", utc=True)
    ordered_df = ordered_df.dropna(subset=["date"]).sort_values("date", ascending=True)

    vol_rows = []
    for row in ordered_df.itertuples(index=False):
        sec = to_unix_seconds(row.date)
        if sec is None:
            continue
        vol = pd.to_numeric(getattr(row, "volume", None), errors="coerce")
        if pd.isna(vol):
            continue
        up = float(row.close) >= float(row.open)
        vol_rows.append(
            {
                "time": sec,
                "value": float(vol),
                "color": "#26a69a" if up else "#ef5350",
            }
        )

    dedup = {}
    for item in vol_rows:
        dedup[item["time"]] = item
    return [dedup[k] for k in sorted(dedup.keys())]





def _format_delta_human(delta: Optional[Union[pd.Timedelta, timedelta]]) -> str:
    if delta is None or pd.isna(delta):
        return ""
    sec = int(abs(delta.total_seconds()))
    h, rem = divmod(sec, 3600)
    m, _ = divmod(rem, 60)
    sign = "-" if delta.total_seconds() < 0 else "+"
    return f"{sign}{h}h {m}m"


def build_tv_markers(filtered_records, price_df: pd.DataFrame, interval: str, selected_trade=None, show_order_types=False):
    markers = []
    diagnostics = []
    if price_df is None or price_df.empty:
        return markers, diagnostics

    price_dates = pd.to_datetime(price_df.get("date"), errors="coerce").dropna()
    if price_dates.empty:
        return markers, diagnostics
    if getattr(price_dates.dt, "tz", None) is not None:
        price_dates = price_dates.dt.tz_convert(None)
    min_ts = price_dates.min()
    max_ts = price_dates.max()
    align_tolerance = interval_to_timedelta(interval) * 3

    for record in filtered_records:
        sentiment = record.get("sentiment")
        if sentiment not in {"Bullish", "Bearish"}:
            continue
        signal_date = record.get("date")
        if not signal_date:
            continue
        trade_dt = to_naive_timestamp(signal_date)
        if trade_dt is None:
            continue
        if trade_dt < min_ts or trade_dt > max_ts:
            continue

        _, price_row, nearest_delta = align_trade_to_candle(price_df, signal_date, interval)
        if price_row is None or nearest_delta is None:
            continue

        snap_info = None
        use_snapped = False
        if nearest_delta > align_tolerance:
            snap_info = resolve_force_snap(
                record, price_df, interval, GRADING_TIMELINE
            )
            if snap_info:
                use_snapped = True
                price_row = snap_info.snapped_row
            else:
                continue

        marker_time = to_unix_seconds(price_row["date"])
        if marker_time is None:
            continue

        order_type = shared_infer_order_type(record)
        order_type_text = "M" if order_type == "Market" else "L"
        marker_text = ""
        if show_order_types:
            marker_text = f"{order_type_text} ⚠️" if use_snapped else order_type_text
        elif use_snapped:
            marker_text = "⚠️"

        is_selected = bool(selected_trade) and record.get("id") == selected_trade.get("id")
        if sentiment == "Bullish":
            markers.append(
                {
                    "time": marker_time,
                    "position": "belowBar",
                    "color": "#FFA726" if use_snapped else ("#fdd835" if is_selected else "#26a69a"),
                    "shape": "arrowUp",
                    "text": marker_text,
                }
            )
        else:
            markers.append(
                {
                    "time": marker_time,
                    "position": "aboveBar",
                    "color": "#FFA726" if use_snapped else ("#fdd835" if is_selected else "#ef5350"),
                    "shape": "arrowDown",
                    "text": marker_text,
                }
            )

        if use_snapped and snap_info:
            diagnostics.append(
                {
                    "ID": str(record.get("id") or "")[:8],
                    "Ticker": str(record.get("ticker") or ""),
                    "Original Time": snap_info.trading_thesis_time.strftime("%Y/%m/%d %H:%M:%S"),
                    "Snapped Time": snap_info.snapped_at.strftime("%Y/%m/%d %H:%M:%S"),
                    "Delta": _format_delta_human(snap_info.delta),
                    "Order": order_type_text,
                }
            )

    markers.sort(key=lambda x: x["time"])
    return markers, diagnostics


def build_plan_line_series(tv_data: list[dict], selected_trade: dict):
    if not tv_data or not selected_trade:
        return []
    first_t = tv_data[0]["time"]
    end_t = tv_data[-1]["time"]

    trade_t = to_unix_seconds(selected_trade.get("date"))
    if trade_t is None:
        start_t = first_t
    else:
        aligned = None
        for row in tv_data:
            row_t = int(row["time"])
            if row_t >= int(trade_t):
                aligned = row_t
                break
        start_t = aligned if aligned is not None else end_t

    if start_t < first_t:
        start_t = first_t
    if start_t > end_t:
        start_t = end_t

    tp = pd.to_numeric(selected_trade.get("tp"), errors="coerce")
    sl = pd.to_numeric(selected_trade.get("sl"), errors="coerce")
    sentiment = str(selected_trade.get("sentiment") or "").strip()

    # Push TP/SL labels outward from the entry marker by trade direction.
    if sentiment == "Bearish":
        tp_pos = "belowBar"
        sl_pos = "aboveBar"
    else:
        # Default Bullish and unknown sentiment to long-side layout.
        tp_pos = "aboveBar"
        sl_pos = "belowBar"

    plan_series = []
    for val, color, pos in [(tp, "#26a69a", tp_pos), (sl, "#ef5350", sl_pos)]:
        if pd.isna(val):
            continue
        plan_series.append(
            {
                "type": "Line",
                "data": [{"time": start_t, "value": float(val)}, {"time": end_t, "value": float(val)}],
                "markers": [
                    {
                        "time": start_t,
                        "position": pos,
                        "color": color,
                        "shape": "text",
                        "text": f"{float(val):,.2f}",
                    }
                ],
                "options": {
                    "color": color,
                    "lineWidth": 1,
                    "lineStyle": 2,
                    "priceLineVisible": False,
                    "lastValueVisible": False,
                },
            }
        )
    return plan_series


def build_plan_price_lines(selected_trade: dict):
    if not selected_trade:
        return []
    tp = pd.to_numeric(selected_trade.get("tp"), errors="coerce")
    sl = pd.to_numeric(selected_trade.get("sl"), errors="coerce")
    lines = []
    if pd.notna(tp):
        lines.append(
            {
                "price": float(tp),
                "color": "#26a69a",
                "lineWidth": 1,
                "lineStyle": 2,
                "axisLabelVisible": True,
                "title": f"{float(tp):,.2f}",
            }
        )
    if pd.notna(sl):
        lines.append(
            {
                "price": float(sl),
                "color": "#ef5350",
                "lineWidth": 1,
                "lineStyle": 2,
                "axisLabelVisible": True,
                "title": f"{float(sl):,.2f}",
            }
        )
    return lines








def _dashboard_grading_repository(
    records,
    notion_headers,
    *,
    warn_on_unwritable_outcome=True,
):
    return DashboardTradingThesisRepository(
        records,
        write_track1=lambda page_id, outcome: update_result(
            page_id,
            outcome.outcome_label,
            None,
            notion_headers,
            reason_code=outcome.reason_code,
        ),
        write_track2=lambda page_id, outcome, checked: update_track2_result(
            page_id,
            outcome.outcome_label,
            notion_headers,
            reason_code=outcome.reason_code,
            observed_high=outcome.observed_high,
            observed_low=outcome.observed_low,
            final_close=outcome.final_close,
            last_checked_at=checked,
        ),
        write_system_message=lambda page_id, message: update_system_msg(
            page_id, message, notion_headers
        ),
        warn=st.warning,
        warn_on_unwritable_outcome=warn_on_unwritable_outcome,
    )


def _dashboard_grading_fetcher(
    twelve_data_api_key,
    fugle_api_key,
    refresh_nonce,
):
    def _fetch(ticker, start, end, interval):
        return fetch_prices(
            ticker,
            start,
            end,
            interval,
            twelve_data_api_key,
            fugle_api_key,
            refresh_nonce,
        )

    return _fetch


def run_auto_grade(
    records_for_ticker,
    price_df,
    selected_interval,
    notion_headers,
    timeout_bars=None,
    silent=False,
):
    records = list(records_for_ticker or [])
    if not records:
        return 0
    twelve_key = st.session_state.get(
        "cfg_twelve_api_key", ENV_TWELVE_DATA_API_KEY
    )
    fugle_key = st.session_state.get("cfg_fugle_api_key", ENV_FUGLE_API_KEY)
    refresh_nonce = int(st.session_state.get("refresh_nonce", 0) or 0)
    market = InteractiveDashboardMarketData(
        ticker=records[0].get("ticker"),
        interval=selected_interval,
        prices=price_df,
        fetch=_dashboard_grading_fetcher(
            twelve_key,
            fugle_key,
            refresh_nonce,
        ),
    )
    observer = (
        SilentDashboardObserver()
        if silent
        else DashboardOutcomeRenderer(
            st.write,
            timeout_bars=int(timeout_bars or 0),
        )
    )
    result = run_dashboard_grading_cycle(
        _dashboard_grading_repository(records, notion_headers),
        market,
        DashboardCycleControl(
            selected_interval,
            int(timeout_bars or 0),
            len(records),
        ),
        DASHBOARD_INTERACTIVE_CYCLE_POLICY,
        observer,
        GRADING_TIMELINE,
    )
    return result.updated


def run_background_auto_grade_all(
    records_all,
    selected_interval,
    twelve_data_api_key,
    fugle_api_key,
    refresh_nonce,
    notion_headers,
    timeout_bars=None,
):
    records = [record for record in records_all if record.get("ticker")]
    if not records:
        return 0
    result = run_dashboard_grading_cycle(
        _dashboard_grading_repository(records, notion_headers),
        BackgroundDashboardMarketData(
            _dashboard_grading_fetcher(
                twelve_data_api_key,
                fugle_api_key,
                refresh_nonce,
            )
        ),
        DashboardCycleControl(
            selected_interval,
            int(timeout_bars or 0),
            len(records),
        ),
        DASHBOARD_BACKGROUND_CYCLE_POLICY,
        SilentDashboardObserver(),
        GRADING_TIMELINE,
    )
    return result.updated


def run_force_trade_grade(
    record,
    price_df,
    selected_interval,
    notion_headers,
    timeout_bars=None,
):
    collector = DashboardOutcomeCollector()
    twelve_key = st.session_state.get(
        "cfg_twelve_api_key", ENV_TWELVE_DATA_API_KEY
    )
    fugle_key = st.session_state.get("cfg_fugle_api_key", ENV_FUGLE_API_KEY)
    refresh_nonce = int(st.session_state.get("refresh_nonce", 0) or 0)
    run_dashboard_grading_cycle(
        _dashboard_grading_repository(
            [record],
            notion_headers,
            warn_on_unwritable_outcome=False,
        ),
        InteractiveDashboardMarketData(
            ticker=record.get("ticker"),
            interval=selected_interval,
            prices=price_df,
            fetch=_dashboard_grading_fetcher(
                twelve_key,
                fugle_key,
                refresh_nonce,
            ),
        ),
        DashboardCycleControl(
            selected_interval,
            int(timeout_bars or 0),
            1,
        ),
        DASHBOARD_FORCE_TRADE_CYCLE_POLICY,
        collector,
        GRADING_TIMELINE,
    )
    return collector.outcomes[-1] if collector.outcomes else None




def fetch_prices(
    ticker: str,
    start: datetime,
    end: datetime,
    interval: str,
    twelve_api_key: str,
    fugle_api_key: str,
    refresh_nonce: int,
    focus_mode_requested: bool = False,
):
    adapter = _dashboard_runtime_adapter("fetch_prices")
    if adapter is not None:
        return adapter(
            ticker,
            start,
            end,
            interval,
            twelve_api_key,
            fugle_api_key,
            refresh_nonce,
            focus_mode_requested,
        )
    routing_table = load_routing_table()
    optimization_memory = load_optimization_memory()
    resolution = _MARKET_DATA_RESOLVER.resolve(
        MarketDataRequest(
            ticker=ticker,
            start=start,
            end=end,
            interval=interval,
            twelve_api_key=twelve_api_key,
            fugle_api_key=fugle_api_key,
            refresh_intent=refresh_nonce,
            focus_mode_requested=focus_mode_requested,
            routing_table=routing_table,
            optimization_memory=optimization_memory,
        )
    )
    effects = resolution.effects
    if not resolution.from_cache:
        if effects.route_event:
            record_routing_event(effects.route_event, effects.route_source or None)
        memory_key = f"{effects.route_ticker}_{interval}"
        current = optimization_memory.get(memory_key, {})
        optimization_memory[memory_key] = {
            "counter": effects.counter,
            "verified_days": effects.verified_days,
            "preferred_source": (
                effects.learned_source or current.get("preferred_source") or ""
            ),
        }
        save_optimization_memory(optimization_memory)
        if effects.learned_source and not resolution.prices.empty:
            save_routing_table(effects.route_ticker, interval, effects.learned_source)
    return resolution.prices, resolution.source, resolution.debug_log


def interval_to_timedelta(interval: str) -> timedelta:
    mapping = {
        "1m": timedelta(minutes=1),
        "5m": timedelta(minutes=5),
        "15m": timedelta(minutes=15),
        "1h": timedelta(hours=1),
        "4h": timedelta(hours=4),
        "1d": timedelta(days=1),
    }
    return mapping.get(str(interval or "").lower(), timedelta(days=1))


def to_naive_timestamp(value):
    ts = pd.to_datetime(value, errors="coerce")
    if pd.isna(ts):
        return None
    if getattr(ts, "tzinfo", None) is not None:
        ts = ts.tz_convert(None)
    return ts


def align_trade_to_candle(df, signal_date, interval: str):
    signal_ts = to_naive_timestamp(signal_date)
    if signal_ts is None or df.empty:
        return None, None, None
    dates = pd.to_datetime(df["date"], errors="coerce")
    if dates.empty:
        return None, None, None
    diffs = (dates - signal_ts).abs()
    nearest_idx = int(diffs.idxmin())
    nearest_row = df.loc[nearest_idx]
    nearest_delta = diffs.loc[nearest_idx]
    tolerance = interval_to_timedelta(interval) * 3
    # Always return nearest for stability; tolerance only used for debug intent.
    return nearest_idx, nearest_row, nearest_delta if nearest_delta <= tolerance else nearest_delta


def price_at_or_after(df, signal_date, interval: str):
    signal_naive = to_naive_timestamp(signal_date)
    if signal_naive is None:
        return None, None
    dates_naive = pd.to_datetime(df["date"], errors="coerce").apply(
        lambda x: x.tz_convert(None) if getattr(x, "tzinfo", None) is not None else x
    )
    subset = df[dates_naive >= signal_naive]
    if subset.empty:
        idx, row, _ = align_trade_to_candle(df, signal_date, interval)
        return idx, row
    return subset.index[0], subset.iloc[0]

def _dashboard_grading_price_dates(price_df: pd.DataFrame) -> pd.Series:
    return pd.to_datetime(price_df["date"], errors="coerce").apply(
        lambda value: (
            value.tz_convert(None)
            if getattr(value, "tzinfo", None) is not None
            else value
        )
    )


GRADING_TIMELINE = PriceTimeline(
    to_timestamp=to_naive_timestamp,
    price_dates=_dashboard_grading_price_dates,
    locate=price_at_or_after,
    interval_delta=interval_to_timedelta,
)


def choose_selected_trade(filtered_records, query_date, query_interval=None, preferred_record_id=None):
    selected_trade = None
    base_records = list(filtered_records or [])
    if preferred_record_id:
        preferred_norm = _normalize_page_id(preferred_record_id)
        for r in base_records:
            if _normalize_page_id(r.get("id")) == preferred_norm:
                return r
    q_interval = str(query_interval or "").strip().lower()
    if q_interval in INTERVAL_OPTIONS:
        interval_filtered = [r for r in base_records if str(r.get("timeframe") or "").strip().lower() == q_interval]
        if interval_filtered:
            base_records = interval_filtered
    if query_date is not None:
        nearest = None
        for r in base_records:
            r_date = pd.to_datetime(r.get("date"), errors="coerce")
            if pd.isna(r_date):
                continue
            if r_date.tzinfo is not None:
                r_date = r_date.tz_convert(None)
            delta = abs(r_date - query_date)
            if nearest is None or delta < nearest[0]:
                nearest = (delta, r)
        if nearest is not None:
            selected_trade = nearest[1]
    if selected_trade is None:
        sorted_records = sorted(
            [r for r in base_records if r.get("date") is not None],
            key=lambda x: x.get("date"),
            reverse=True,
        )
        if sorted_records:
            selected_trade = sorted_records[0]
    return selected_trade


def _get_query_param(params, candidates):
    if params is None:
        return None
    for key in candidates:
        try:
            if key in params:
                return params.get(key)
        except Exception:
            continue
    try:
        param_keys = list(params.keys())
    except Exception:
        param_keys = []
    lower_map = {str(k).lower(): k for k in param_keys}
    for key in candidates:
        real_key = lower_map.get(str(key).lower())
        if real_key is not None:
            try:
                return params.get(real_key)
            except Exception:
                continue
    return None


def _parse_query_interval(raw_value):
    if raw_value is None:
        return None
    s = str(raw_value).strip().lower()
    mapping = {
        "1d": "1d",
        "d": "1d",
        "day": "1d",
        "daily": "1d",
        "1h": "1h",
        "h1": "1h",
        "4h": "4h",
        "h4": "4h",
        "15m": "15m",
        "m15": "15m",
        "5m": "5m",
        "m5": "5m",
        "1m": "1m",
        "m1": "1m",
    }
    return mapping.get(s)


def _parse_query_datetime(raw_value):
    if raw_value is None:
        return None
    raw_text = str(raw_value).strip()
    if not raw_text:
        return None
    if "%" in raw_text:
        try:
            raw_text = unquote(raw_text).strip()
        except Exception:
            pass
    if raw_text.isdigit():
        try:
            iv = int(raw_text)
            if iv >= 10**12:
                ts = pd.to_datetime(iv, unit="ms", errors="coerce", utc=True)
            else:
                ts = pd.to_datetime(iv, unit="s", errors="coerce", utc=True)
            if pd.notna(ts):
                return ts.tz_convert(None)
        except Exception:
            pass
    ts = pd.to_datetime(raw_text, errors="coerce")
    if pd.isna(ts):
        return None
    if getattr(ts, "tzinfo", None) is not None:
        ts = ts.tz_convert(None)
    return ts


def _extract_page_id_candidates(value):
    if value is None:
        return set()
    raw = str(value).strip()
    if not raw:
        return set()
    if "%" in raw:
        try:
            raw = unquote(raw).strip()
        except Exception:
            pass

    candidates = set()
    pending_texts = [raw]
    try:
        parsed = urlparse(raw)
        if parsed.scheme and parsed.netloc:
            pending_texts.append(parsed.path or "")
            query = parse_qs(parsed.query or "")
            for k in ["id", "page_id", "pageid", "pid", "record_id", "p"]:
                for qv in query.get(k, []):
                    pending_texts.append(qv)
    except Exception:
        pass

    for text in pending_texts:
        t = str(text).replace("-", "").strip().lower()
        if not t:
            continue
        if re.fullmatch(r"[0-9a-f]{32}", t):
            candidates.add(t)
        for m in re.findall(r"[0-9a-f]{32}", t):
            candidates.add(m)
    return candidates


def _normalize_page_id(value):
    candidates = _extract_page_id_candidates(value)
    if candidates:
        return sorted(candidates)[0]
    if value is None:
        return ""
    return str(value).replace("-", "").strip().lower()


st.set_page_config(page_title="NewsLoop", layout="wide")

st.title("NewsLoop 回測儀表板")
st.caption("版本：v1.2")
st.markdown(
    """
    <style>
    section[data-testid="stSidebar"] div[data-baseweb="tab-list"] {
        gap: 0.25rem !important;
    }
    section[data-testid="stSidebar"] div[data-baseweb="tab-list"] button[role="tab"] {
        flex: 1 1 0 !important;
        justify-content: center !important;
        text-align: center !important;
        min-width: 0 !important;
        padding-left: 0.25rem !important;
        padding-right: 0.25rem !important;
    }
    div[data-testid="stDataFrame"] [role="columnheader"] {
        justify-content: center !important;
        text-align: center !important;
    }
    div[data-testid="stDataFrame"] [role="cell"] {
        justify-content: center !important;
        text-align: center !important;
    }
    div[data-testid="stDataFrame"] [role="cell"] a {
        margin: 0 auto !important;
        text-align: center !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

query_params = st.query_params
qp_ticker = _get_query_param(query_params, ["ticker", "symbol", "code", "asset", "t"])
qp_date_raw = _get_query_param(query_params, ["date", "datetime", "dt", "time", "ts", "d"])
qp_tf_raw = _get_query_param(query_params, ["tf", "timeframe", "interval", "k"])
qp_page_id = _get_query_param(query_params, ["id", "page_id", "pageid", "pid", "record_id"])
if isinstance(qp_ticker, (list, tuple)):
    qp_ticker = qp_ticker[0] if qp_ticker else None
if isinstance(qp_date_raw, (list, tuple)):
    qp_date_raw = qp_date_raw[0] if qp_date_raw else None
if isinstance(qp_tf_raw, (list, tuple)):
    qp_tf_raw = qp_tf_raw[0] if qp_tf_raw else None
if isinstance(qp_page_id, (list, tuple)):
    qp_page_id = qp_page_id[0] if qp_page_id else None
deep_link_interval = _parse_query_interval(qp_tf_raw)

if "selected_interval" not in st.session_state:
    st.session_state["selected_interval"] = "1d"
local_cfg = load_local_cfg()
ACTIVE_COLUMN_MAPPING = SchemaManager.normalize_mapping(local_cfg.get("COLUMN_MAPPING"))
if "selected_interval_initialized" not in st.session_state:
    st.session_state["selected_interval"] = local_cfg.get("SELECTED_INTERVAL", st.session_state.get("selected_interval", "1d"))
    st.session_state["selected_interval_initialized"] = True
if "auto_focus_toggle" not in st.session_state:
    st.session_state["auto_focus_toggle"] = bool(local_cfg.get("AUTO_FOCUS_TRADE", True))
if "pending_auto_focus_toggle" in st.session_state:
    try:
        st.session_state["auto_focus_toggle"] = bool(st.session_state.get("pending_auto_focus_toggle"))
    except Exception:
        pass
    st.session_state.pop("pending_auto_focus_toggle", None)

target_interval = st.session_state.get("selected_interval", "1d")
pending_interval = st.session_state.get("selected_interval_pending")
if pending_interval in INTERVAL_OPTIONS:
    target_interval = pending_interval
    st.session_state.pop("selected_interval_pending", None)
if target_interval not in INTERVAL_OPTIONS:
    target_interval = "1d"
if st.session_state.get("selected_interval") != target_interval:
    st.session_state["selected_interval"] = target_interval

schema_check = {}

with st.sidebar:
    st.subheader("設定")
    tab_display, tab_strategy, tab_config = st.tabs(["⚙️ 顯示", "🧠 控制", "🔑 系統"])
    date_range_slot = None
    reset_range_slot = None

    with tab_display:
        selected_interval = st.selectbox(
            "選擇 K 線週期",
            options=INTERVAL_OPTIONS,
            key="selected_interval",
        )
        chart_engine = "TradingView (新)"
        st.divider()
        # Placeholders to keep date controls above later controls in final layout.
        date_range_slot = st.empty()
        reset_range_slot = st.empty()
        st.divider()
        auto_focus_toggle = st.toggle(
            "聚焦交易點位",
            key="auto_focus_toggle",
        )
        show_order_types = st.toggle(
            "顯示訂單類型（市價/限價）",
            value=bool(local_cfg.get("SHOW_ORDER_TYPES", False)),
            key="show_order_types",
        )

    with tab_strategy:
        refresh = st.button("重新載入資料")
        force_regrade = False

        st.divider()
        force_regrade = st.button(
            "重算當前交易",
            help="無視目前狀態，強制重新判定目前選中的交易勝負。",
        )
        auto_grade = st.button("重算當前標的")
        background_auto_grade = st.button(
            "重算所有標的",
            help="會強制重算所有標的，並覆蓋既有 Result_Auto（不只 Pending）。",
        )
        st.caption("注意：`重算所有標的` 會覆蓋既有 `Result_Auto` 結果（包含 Win/Loss/Timed Out）。")
        grade_timeout_bars = st.slider(
            "結算有效期限 (K 棒)",
            min_value=0,
            max_value=300,
            value=int(local_cfg.get("GRADE_TIMEOUT_BARS", 0)),
            help="0 代表不啟用逾時判定；>0 則超過 N 根未觸發 TP/SL 標記為 Timed Out。",
            key="grade_timeout_bars",
        )

        st.divider()
        cache_ttl_seconds = st.slider(
            "價格快取秒數",
            30,
            3600,
            int(local_cfg.get("CACHE_TTL_SECONDS", 600)),
            30,
            key="cfg_cache_ttl_seconds",
        )

    with tab_config:
        notion_token = st.text_input(
            "NOTION_TOKEN",
            value=st.session_state.get("cfg_notion_token", local_cfg.get("NOTION_TOKEN", ENV_NOTION_TOKEN)),
            type="password",
            key="cfg_notion_token",
        ).strip()
        db_id = st.text_input(
            "DB_ID (NEWS_ALPHA_DB_ID)",
            value=st.session_state.get("cfg_db_id", local_cfg.get("DB_ID", ENV_DB_ID)),
            key="cfg_db_id",
        ).strip()
        twelve_data_api_key = st.text_input(
            "TWELVE_DATA_API_KEY",
            value=st.session_state.get(
                "cfg_twelve_api_key",
                local_cfg.get("TWELVE_DATA_API_KEY", ENV_TWELVE_DATA_API_KEY),
            ),
            type="password",
            key="cfg_twelve_api_key",
        ).strip()
        fugle_api_key = st.text_input(
            "FUGLE_API_KEY",
            value=st.session_state.get("cfg_fugle_api_key", local_cfg.get("FUGLE_API_KEY", ENV_FUGLE_API_KEY)),
            type="password",
            key="cfg_fugle_api_key",
        ).strip()
        if notion_token and db_id:
            sidebar_notion_headers = {
                "Authorization": f"Bearer {notion_token}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            }
            try:
                schema_check = check_dashboard_schema(db_id, sidebar_notion_headers)
            except Exception as exc:
                schema_check = {
                    "ok": False,
                    "message": f"SchemaError: {exc}",
                    "bindings": {},
                }
        st.divider()
        st.subheader("AI 設定")
        ai_provider_default = local_cfg.get("AI_PROVIDER", "停用 (預設)")
        ai_provider_options = ["停用 (預設)", "Google Gemini", "OpenAI", "Groq"]
        ai_provider = st.selectbox(
            "選擇 AI 提供商",
            options=ai_provider_options,
            index=ai_provider_options.index(ai_provider_default) if ai_provider_default in ai_provider_options else 0,
        )
        current_api_key = None
        if ai_provider == "Google Gemini":
            env_key = os.getenv("GOOGLE_API_KEY", "")
            current_api_key = st.text_input(
                "Google API Key",
                value=st.session_state.get("google_api_key_input", local_cfg.get("GOOGLE_API_KEY", env_key)),
                type="password",
                key="google_api_key_input",
            )
        elif ai_provider == "OpenAI":
            env_key = os.getenv("OPENAI_API_KEY", "")
            current_api_key = st.text_input(
                "OpenAI API Key",
                value=st.session_state.get("openai_key_input", local_cfg.get("OPENAI_API_KEY", env_key)),
                type="password",
                key="openai_key_input",
            )
        elif ai_provider == "Groq":
            env_key = os.getenv("GROQ_API_KEY", "")
            current_api_key = st.text_input(
                "Groq API Key",
                value=st.session_state.get("groq_api_key_input", local_cfg.get("GROQ_API_KEY", env_key)),
                type="password",
                key="groq_api_key_input",
            )
        if ai_provider != "停用 (預設)" and not current_api_key:
            st.warning("請輸入 Key 以啟用 AI")
        st.divider()
        with st.expander("🤖️ 背景自動結算控制", expanded=False):
            daemon_ctrl = load_daemon_control()
            interval_value = st.number_input(
                "掃描頻率 (分鐘，0=暫停)",
                min_value=0,
                max_value=1440,
                value=int(daemon_ctrl.get("interval_minutes", 0)),
                step=1,
                key="daemon_interval_minutes_input",
            )
            if int(interval_value) != int(daemon_ctrl.get("interval_minutes", 0)):
                daemon_ctrl["interval_minutes"] = int(interval_value)
                save_daemon_control(daemon_ctrl)

            if st.button("⚡ 立即觸發背景掃描", key="btn_trigger_daemon_now"):
                daemon_ctrl = load_daemon_control()
                daemon_ctrl["run_now_trigger"] = True
                save_daemon_control(daemon_ctrl)
                st.session_state["daemon_local_sync_trigger"] = True

            daemon_ctrl = load_daemon_control()
            daemon_status = "運作中" if int(daemon_ctrl.get("interval_minutes", 0)) > 0 else "暫停"
            st.caption(f"機器人狀態：{daemon_status}")
            daemon_runtime_status = str(daemon_ctrl.get("status") or "OK")
            if daemon_runtime_status.startswith("ERROR:"):
                st.error(f"背景結算錯誤：{daemon_runtime_status}")
                last_error_msg = str(daemon_ctrl.get("last_error") or "").strip()
                if last_error_msg:
                    st.caption(f"錯誤訊息：{last_error_msg}")
                last_error_at = str(daemon_ctrl.get("last_error_at") or "").strip()
                if last_error_at:
                    parsed_last_error_at = pd.to_datetime(last_error_at, errors="coerce", utc=True)
                    if pd.notna(parsed_last_error_at):
                        st.caption(f"錯誤時間：{parsed_last_error_at.strftime('%Y/%m/%d %H:%M:%S UTC')}")
                    else:
                        st.caption(f"錯誤時間：{last_error_at}")
            elif daemon_runtime_status.startswith("SchemaWarn:"):
                st.warning(format_schema_notice(daemon_runtime_status))
            last_run = str(daemon_ctrl.get("last_run_time") or "").strip()
            if last_run:
                parsed_last_run = pd.to_datetime(last_run, errors="coerce", utc=True)
                if pd.notna(parsed_last_run):
                    last_run_display = parsed_last_run.strftime("%Y/%m/%d %H:%M:%S UTC")
                else:
                    last_run_display = last_run
            else:
                last_run_display = "尚未執行"
                st.caption(f"上次掃描時間：{last_run_display}")
            if st.button("🔄 同步 Notion 最新欄位", use_container_width=True):
                try:
                    r = requests.post(
                        "http://localhost:8000/schema/resync",
                        json={"force": True},
                        headers=get_backend_api_headers(),
                        timeout=8,
                    )
                    if r.ok:
                        st.success("Schema 已重新同步。")
                    else:
                        st.warning(f"Schema 重同步失敗：HTTP {r.status_code}")
                except Exception as exc:
                    st.warning(f"Schema 重同步失敗：{exc}")
        with st.expander("🧩 欄位對照表管理", expanded=False):
            current_mapping = SchemaManager.normalize_mapping(local_cfg.get("COLUMN_MAPPING"))
            mapping_rows = [{"internal_key": k, "notion_column": current_mapping.get(k, "")} for k in SchemaManager.default_mapping()]
            mapping_df = pd.DataFrame(mapping_rows)
            edited_mapping_df = st.data_editor(
                mapping_df,
                hide_index=True,
                use_container_width=True,
                disabled=["internal_key"],
                key="column_mapping_editor",
                column_config={
                    "internal_key": st.column_config.TextColumn("系統欄位鍵"),
                    "notion_column": st.column_config.TextColumn("Notion 欄位名稱"),
                },
            )
            check_feedback = None
            autofill_feedback = None
            col_map_save, col_map_check, col_map_autofill = st.columns(3)
            with col_map_save:
                if st.button("儲存對照設定", use_container_width=True):
                    new_mapping = {}
                    for _, row in edited_mapping_df.iterrows():
                        k = str(row.get("internal_key") or "").strip()
                        v = str(row.get("notion_column") or "").strip()
                        if k and v:
                            new_mapping[k] = v
                    final_mapping = SchemaManager.normalize_mapping(new_mapping)
                    merged_cfg = dict(local_cfg)
                    merged_cfg["COLUMN_MAPPING"] = final_mapping
                    save_local_cfg(merged_cfg)
                    local_cfg = merged_cfg
                    ACTIVE_COLUMN_MAPPING = final_mapping
                    try:
                        requests.post(
                            "http://localhost:8000/schema/resync",
                            json={"force": True},
                            headers=get_backend_api_headers(),
                            timeout=5,
                        )
                    except Exception as exc:
                        st.warning(f"欄位設定已儲存，但 API 重同步失敗：{exc}")
                    st.success("已儲存欄位對照表。")
                    st.rerun()
            with col_map_check:
                if st.button("檢查對照可用性", use_container_width=True):
                    if not notion_token or not db_id:
                        check_feedback = ("error", "請先填入 NOTION_TOKEN 與 DB_ID。")
                    else:
                        check_mapping_raw = {}
                        for _, row in edited_mapping_df.iterrows():
                            k = str(row.get("internal_key") or "").strip()
                            v = str(row.get("notion_column") or "").strip()
                            if k and v:
                                check_mapping_raw[k] = v
                        check_mapping = SchemaManager.normalize_mapping(check_mapping_raw)
                        temp_headers = {
                            "Authorization": f"Bearer {notion_token}",
                            "Notion-Version": NOTION_VERSION,
                            "Content-Type": "application/json",
                        }
                        schema_check = _dashboard_schema_manager(db_id, temp_headers).check(
                            mapping=check_mapping,
                            force=True,
                        )
                        ok, msg = schema_check["ok"], schema_check["message"]
                        if ok and msg == "OK":
                            check_feedback = ("success", "Schema 檢查通過。")
                        elif ok:
                            check_feedback = ("info", "檢查完成：請以上方欄位提醒為準。")
                        else:
                            check_feedback = ("error", format_schema_notice(msg))
            with col_map_autofill:
                if st.button("自動補齊缺漏欄位", use_container_width=True):
                    if not notion_token or not db_id:
                        autofill_feedback = {
                            "error": ["請先填入 NOTION_TOKEN 與 DB_ID。"],
                            "created": [],
                            "skipped": [],
                        }
                    else:
                        try:
                            fill_mapping_raw = {}
                            for _, row in edited_mapping_df.iterrows():
                                k = str(row.get("internal_key") or "").strip()
                                v = str(row.get("notion_column") or "").strip()
                                if k and v:
                                    fill_mapping_raw[k] = v
                            fill_mapping = SchemaManager.normalize_mapping(fill_mapping_raw)
                            temp_headers = {
                                "Authorization": f"Bearer {notion_token}",
                                "Notion-Version": NOTION_VERSION,
                                "Content-Type": "application/json",
                            }
                            autofill_feedback = _dashboard_schema_manager(db_id, temp_headers).autofill(
                                mapping=fill_mapping
                            )
                            # Refresh API schema snapshot after local auto-fill.
                            try:
                                requests.post(
                                    "http://localhost:8000/schema/resync",
                                    json={"force": True},
                                    headers=get_backend_api_headers(),
                                    timeout=8,
                                )
                            except Exception:
                                pass
                        except Exception as exc:
                            autofill_feedback = {
                                "error": [f"執行失敗：{exc}"],
                                "created": [],
                                "skipped": [],
                            }
            if check_feedback:
                tone, text = check_feedback
                if tone == "success":
                    st.success(text)
                elif tone == "error":
                    st.error(text)
                else:
                    st.info(text)
            created_items = []
            skipped_items = []
            failed_items = []
            if autofill_feedback:
                created_items = list(autofill_feedback.get("created") or [])
                skipped_items = list(autofill_feedback.get("skipped") or [])
                failed_items = list(autofill_feedback.get("failed") or autofill_feedback.get("error") or [])
                if created_items:
                    created_text = "、".join(
                        [
                            f"{_friendly_field_name(item.get('name'))}({item.get('type')})"
                            for item in created_items
                        ]
                    )
                    st.success(f"已建立欄位：{created_text}")
                if skipped_items:
                    skipped_text = "；".join(
                        [
                            f"{_friendly_field_name(item.get('name'))}：{item.get('reason')}"
                            for item in skipped_items
                        ]
                    )
                    st.warning(f"已跳過欄位：{skipped_text}")
                if failed_items:
                    failed_text = "；".join(
                        [
                            (
                                str(item)
                                if not isinstance(item, dict)
                                else f"{_friendly_field_name(item.get('name'))}：{item.get('reason')}"
                            )
                            for item in failed_items
                        ]
                    )
                    st.error(f"建立失敗：{failed_text}")
                if not created_items and not skipped_items and not failed_items:
                    st.info("目前沒有需要補齊的缺漏欄位。")
            st.caption("手動綁定（Internal Key -> Notion Property ID）")
            try:
                bindings = (
                    schema_check.get("bindings") or {}
                    if isinstance(schema_check, dict)
                    else {}
                )
                internal_keys = list(SchemaManager.default_mapping())
                selected_internal_key = st.selectbox("Internal Key", internal_keys, key="schema_override_internal")
                option_pairs = []
                for ik, b in bindings.items():
                    bid = str((b or {}).get("id") or "")
                    bname = str((b or {}).get("name") or "")
                    if bid:
                        option_pairs.append((bid, f"{bname} ({bid})"))
                option_labels = [x[1] for x in option_pairs]
                selected_label = st.selectbox("Notion Property", option_labels, key="schema_override_property")
                if st.button("套用手動綁定", use_container_width=True):
                    selected_id = ""
                    for pid, lbl in option_pairs:
                        if lbl == selected_label:
                            selected_id = pid
                            break
                    if not selected_id:
                        st.warning("請先選擇可用的 Notion Property。")
                    else:
                        rr = requests.post(
                            "http://localhost:8000/schema/override",
                            json={"internal_key": selected_internal_key, "property_id": selected_id},
                            headers=get_backend_api_headers(),
                            timeout=8,
                        )
                        if rr.ok:
                            st.success("手動綁定已更新。")
                        else:
                            st.warning(f"手動綁定失敗：HTTP {rr.status_code}")
            except Exception as exc:
                st.warning(f"無法載入 Schema 狀態（手動綁定暫不可用）：{exc}")

current_cfg = {
    "NOTION_TOKEN": notion_token,
    "DB_ID": db_id,
    "TWELVE_DATA_API_KEY": twelve_data_api_key,
    "FUGLE_API_KEY": fugle_api_key,
    "CHART_ENGINE": chart_engine,
    "SELECTED_INTERVAL": selected_interval,
    "CACHE_TTL_SECONDS": int(cache_ttl_seconds),
    "GRADE_TIMEOUT_BARS": int(grade_timeout_bars),
    "AUTO_FOCUS_TRADE": bool(auto_focus_toggle),
    "SHOW_ORDER_TYPES": bool(show_order_types),
    "COLUMN_MAPPING": SchemaManager.normalize_mapping(local_cfg.get("COLUMN_MAPPING")),
    "AI_PROVIDER": ai_provider,
    "GOOGLE_API_KEY": st.session_state.get("google_api_key_input", local_cfg.get("GOOGLE_API_KEY", "")),
    "OPENAI_API_KEY": st.session_state.get("openai_key_input", local_cfg.get("OPENAI_API_KEY", "")),
    "GROQ_API_KEY": st.session_state.get("groq_api_key_input", local_cfg.get("GROQ_API_KEY", "")),
}
merged_cfg = dict(local_cfg)
merged_cfg.update(current_cfg)
if merged_cfg != local_cfg:
    save_local_cfg(merged_cfg)
    local_cfg = merged_cfg
ACTIVE_COLUMN_MAPPING = SchemaManager.normalize_mapping(local_cfg.get("COLUMN_MAPPING"))

if not notion_token or not db_id:
    st.info("請先在左側「🔑 系統金鑰配置」填入 NOTION_TOKEN 與 DB_ID，填好後會立即生效。")
    st.stop()

if "refresh_nonce" not in st.session_state:
    st.session_state["refresh_nonce"] = 0
if refresh:
    st.session_state["refresh_nonce"] += 1

NOTION_HEADERS = {
    "Authorization": f"Bearer {notion_token}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}

schema_ok = True
schema_msg = "OK"
if schema_check:
    schema_ok, schema_msg = schema_check["ok"], schema_check["message"]
else:
    try:
        schema_check = check_dashboard_schema(db_id, NOTION_HEADERS)
        schema_ok, schema_msg = schema_check["ok"], schema_check["message"]
    except Exception as exc:
        schema_ok, schema_msg = False, f"SchemaError: {exc}"
        schema_check = {"ok": False, "message": schema_msg, "bindings": {}}
if not schema_ok:
    st.error(format_schema_notice(schema_msg))
elif schema_msg.startswith("SchemaWarn:"):
    st.warning(format_schema_notice(schema_msg))

if refresh or "records" not in st.session_state:
    try:
        st.session_state["records"] = fetch_database_entries(db_id, NOTION_HEADERS)
    except Exception as exc:
        st.error(f"Notion 讀取失敗：{exc}")
        st.stop()

records = st.session_state.get("records", [])
records = [r for r in records if r.get("ticker")]
df_notion = pd.DataFrame(records)
if not df_notion.empty and "Ticker" not in df_notion.columns:
    df_notion["Ticker"] = df_notion.get("ticker")
if not df_notion.empty:
    df_notion["DateDisplay"] = pd.to_datetime(df_notion.get("date"), errors="coerce").dt.strftime(
        "%Y/%m/%d %I:%M %p"
    )

if not records:
    st.info("尚無紀錄，請先透過外掛新增一筆新聞。")
    st.stop()

deep_link_record = None
qp_page_id_candidates = _extract_page_id_candidates(qp_page_id)
if qp_page_id_candidates:
    for rec in records:
        rec_candidates = _extract_page_id_candidates(rec.get("id"))
        rec_candidates.update(_extract_page_id_candidates(rec.get("url")))
        if qp_page_id_candidates.intersection(rec_candidates):
            deep_link_record = rec
            break

if not qp_ticker and deep_link_record:
    qp_ticker = deep_link_record.get("ticker")

if deep_link_interval is None and deep_link_record:
    rec_tf = _parse_query_interval(deep_link_record.get("timeframe"))
    if rec_tf:
        deep_link_interval = rec_tf

# Fallback for legacy ticker-only deep links:
# when URL does not include page/date/tf, infer a stable timeframe from ticker records
# instead of drifting to the latest record's intraday timeframe.
deep_link_interval_inferred_from_ticker = False
deep_link_interval_infer_strategy = None
# Disabled by design:
# ticker-only deep links must not auto-guess timeframe, to avoid locking user's manual interval.

# If deep-link provides explicit target (page/date/tf), apply timeframe once per unique link key.
# Do not auto-apply for ticker-only links.
has_explicit_deep_link_target = bool(qp_page_id or qp_date_raw or qp_tf_raw)
if has_explicit_deep_link_target and deep_link_interval in INTERVAL_OPTIONS:
    deep_link_processed_key = (
        f"{normalize_ticker_key(qp_ticker or '')}|"
        f"{_normalize_page_id(qp_page_id or (deep_link_record or {}).get('id') or '')}|"
        f"{str(qp_date_raw or '')}|"
        f"{str(deep_link_interval or '')}"
    )
    if st.session_state.get("deep_link_processed_key") != deep_link_processed_key:
        if st.session_state.get("selected_interval") != deep_link_interval:
            st.session_state["selected_interval_pending"] = deep_link_interval
        st.session_state["deep_link_processed_key"] = deep_link_processed_key
        st.rerun()

all_dates = pd.to_datetime(df_notion.get("date"), errors="coerce") if not df_notion.empty else pd.Series(dtype="datetime64[ns]")
all_dates = all_dates.dropna()
today_date = datetime.now().date()
if not all_dates.empty:
    min_date = all_dates.min().date()
    max_date = all_dates.max().date()
else:
    min_date = today_date - timedelta(days=30)
    max_date = today_date

cfg_start_raw = local_cfg.get("STATS_RANGE_START")
cfg_end_raw = local_cfg.get("STATS_RANGE_END")
cfg_start_date = None
cfg_end_date = None
try:
    if cfg_start_raw:
        cfg_start_date = pd.to_datetime(cfg_start_raw, errors="coerce").date()
    if cfg_end_raw:
        cfg_end_date = pd.to_datetime(cfg_end_raw, errors="coerce").date()
except Exception:
    cfg_start_date = None
    cfg_end_date = None
if cfg_start_date is None or cfg_end_date is None:
    default_range_start, default_range_end = min_date, max_date
else:
    default_range_start = max(min_date, min(max_date, cfg_start_date))
    default_range_end = max(min_date, min(max_date, cfg_end_date))
if default_range_start > default_range_end:
    default_range_start, default_range_end = min_date, max_date

# Deep link date should override stale UI range: auto-expand range to include URL date.
deep_link_target_ts = _parse_query_datetime(qp_date_raw) if qp_date_raw else None
if deep_link_target_ts is None and deep_link_record is not None:
    deep_link_target_ts = to_naive_timestamp(deep_link_record.get("date"))

deep_link_target_start = None
deep_link_target_end = None
if deep_link_target_ts is not None:
    deep_link_target_start = deep_link_target_ts.date()
    deep_link_target_end = deep_link_target_start
elif qp_ticker:
    ticker_norm = normalize_ticker_key(qp_ticker)
    ticker_dates = []
    for rec in records:
        if normalize_ticker_key(rec.get("ticker")) != ticker_norm:
            continue
        d = to_naive_timestamp(rec.get("date"))
        if d is not None:
            ticker_dates.append(d.date())
    if ticker_dates:
        deep_link_target_start = min(ticker_dates)
        deep_link_target_end = max(ticker_dates)

if qp_ticker and deep_link_target_start is not None and deep_link_target_end is not None:

    def _coerce_date(v, fallback):
        try:
            d = pd.to_datetime(v, errors="coerce")
            if pd.isna(d):
                return fallback
            return d.date()
        except Exception:
            return fallback

    current_start = _coerce_date(st.session_state.get("pick_start", default_range_start), default_range_start)
    current_end = _coerce_date(st.session_state.get("pick_end", default_range_end), default_range_end)
    if current_start > current_end:
        current_start, current_end = current_end, current_start

    current_link_signature = (
        f"{normalize_ticker_key(qp_ticker)}|"
        f"{str(qp_page_id or '')}|"
        f"{str(qp_date_raw or '')}|"
        f"{str(deep_link_interval or '')}"
    )
    current_link_id = (
        f"{normalize_ticker_key(qp_ticker)}|"
        f"{str(qp_page_id or '')}|"
        f"{str(qp_date_raw or '')}|"
        f"{str(deep_link_interval or '')}|"
        f"{deep_link_target_start.isoformat()}_{deep_link_target_end.isoformat()}"
    )
    manual_reset_lock_key = st.session_state.get("manual_reset_lock_key")
    if manual_reset_lock_key and manual_reset_lock_key != current_link_signature:
        st.session_state.pop("manual_reset_lock_key", None)
        manual_reset_lock_key = None

    if manual_reset_lock_key == current_link_signature:
        is_new_link = False
        is_range_mismatch = False
    else:
        is_new_link = st.session_state.get("last_processed_deep_link") != current_link_id
        is_range_mismatch = deep_link_target_start < current_start or deep_link_target_end > current_end

    if is_new_link or is_range_mismatch:
        new_start = deep_link_target_start if deep_link_target_start < current_start else current_start
        new_end = deep_link_target_end if deep_link_target_end > current_end else current_end
        st.session_state["pick_start"] = new_start
        st.session_state["pick_end"] = new_end
        st.session_state["pending_auto_focus_toggle"] = True
        st.session_state["last_processed_deep_link"] = current_link_id
        st.rerun()

with date_range_slot.container():
    st.markdown("📅 統計與回測區間")
    if not df_notion.empty and "date" in df_notion.columns:
        data_dates = pd.to_datetime(df_notion["date"], errors="coerce").dropna()
        if not data_dates.empty:
            data_min = data_dates.min().date()
            data_max = data_dates.max().date()
        else:
            data_min, data_max = min_date, max_date
    else:
        data_min, data_max = min_date, max_date

    if "pick_start" not in st.session_state:
        st.session_state["pick_start"] = default_range_start
    if "pick_end" not in st.session_state:
        st.session_state["pick_end"] = default_range_end
    if "pending_range_reset" in st.session_state:
        try:
            _rs, _re = st.session_state.pop("pending_range_reset")
            st.session_state["pick_start"] = _rs
            st.session_state["pick_end"] = _re
        except Exception:
            st.session_state.pop("pending_range_reset", None)

    col_start, col_end = st.columns(2)
    with col_start:
        selected_start_date = st.date_input("開始日期", key="pick_start")
    with col_end:
        selected_end_date = st.date_input("結束日期", key="pick_end")

with reset_range_slot.container():
    if st.button("📅 重置為全歷史範圍 (包含所有交易)", use_container_width=True):
        reset_start = data_min
        reset_end = data_max
        if reset_start > reset_end:
            reset_start, reset_end = reset_end, reset_start
        lock_sig = ""
        if qp_ticker:
            lock_sig = (
                f"{normalize_ticker_key(qp_ticker)}|"
                f"{str(qp_page_id or '')}|"
                f"{str(qp_date_raw or '')}|"
                f"{str(deep_link_interval or '')}"
            )
        if lock_sig:
            st.session_state["manual_reset_lock_key"] = lock_sig
        st.session_state["pending_range_reset"] = (reset_start, reset_end)
        # Reset focus and deep-link state to avoid relock on rerun.
        st.session_state["pending_auto_focus_toggle"] = False
        st.session_state.pop("deep_link_processed_key", None)
        st.session_state.pop("last_processed_deep_link", None)
        st.session_state.pop("auto_tf_applied_key", None)
        st.session_state.pop("last_selected_trade_url", None)
        st.session_state.pop("last_plotly_event", None)
        st.rerun()

if selected_start_date > selected_end_date:
    selected_start_date, selected_end_date = selected_end_date, selected_start_date

range_start_ts = pd.Timestamp(selected_start_date)
range_end_ts = pd.Timestamp(selected_end_date) + timedelta(days=1) - timedelta(microseconds=1)

# If user changes date range, prioritize range control and disable auto-focus for chart rendering.
range_key = f"{selected_start_date.isoformat()}_{selected_end_date.isoformat()}"
prev_range_key = st.session_state.get("last_chart_range_key")
range_changed_this_run = prev_range_key is not None and prev_range_key != range_key
st.session_state["last_chart_range_key"] = range_key

date_cfg_patch = {
    "STATS_RANGE_START": selected_start_date.isoformat(),
    "STATS_RANGE_END": selected_end_date.isoformat(),
}
merged_cfg = dict(local_cfg)
merged_cfg.update(date_cfg_patch)
if merged_cfg != local_cfg:
    save_local_cfg(merged_cfg)
    local_cfg = merged_cfg

qp_date = None
if qp_date_raw:
    qp_date = _parse_query_datetime(qp_date_raw)


def search_tickers(searchterm: str, **kwargs):
    all_tickers = df_notion["Ticker"].unique().tolist()
    if not searchterm:
        return all_tickers
    return [t for t in all_tickers if searchterm.lower() in t.lower()]


all_tickers = df_notion["Ticker"].dropna().astype(str).unique().tolist()
deep_link_ticker = None
if qp_ticker:
    qp_norm = normalize_ticker_key(qp_ticker)
    direct_lookup = {normalize_ticker_key(t): t for t in all_tickers}
    deep_link_ticker = direct_lookup.get(qp_norm)

# Backward compatibility:
# Previous versions may have stored a plain string in this key, but streamlit_searchbox expects a dict.
if "ticker_search" in st.session_state and not isinstance(st.session_state["ticker_search"], dict):
    del st.session_state["ticker_search"]


selected_ticker = st_searchbox(
    search_tickers,
    key="ticker_search",
    label="搜尋 Ticker (支援模糊搜尋)",
    default=deep_link_ticker,
    default_searchterm=deep_link_ticker or "",
    default_options=[deep_link_ticker] if deep_link_ticker else None,
)

# Deep link auto-select:
# If searchbox returns None but URL provides a valid ticker in DB, force-select it and continue.
if not selected_ticker and deep_link_ticker:
    selected_ticker = deep_link_ticker

if not selected_ticker:
    st.stop()

selected_ticker_norm = normalize_ticker_key(selected_ticker)
last_ticker_norm = st.session_state.get("last_ticker_norm")
if last_ticker_norm and last_ticker_norm != selected_ticker_norm:
    st.session_state.pop("last_selected_trade_url", None)
    st.session_state.pop("last_plotly_event", None)
    st.session_state.pop("deep_link_processed_key", None)
st.session_state["last_ticker_norm"] = selected_ticker_norm
ticker_records_all = [r for r in records if normalize_ticker_key(r.get("ticker")) == selected_ticker_norm]

# Manual ticker search takes priority: show all history for this ticker.
filtered = []
for r in ticker_records_all:
    r_date = pd.to_datetime(r.get("date"), errors="coerce")
    if pd.isna(r_date):
        continue
    if getattr(r_date, "tzinfo", None) is not None:
        r_date = r_date.tz_convert(None)
    if range_start_ts <= r_date <= range_end_ts:
        filtered.append(r)

qp_snapshot = {}
try:
    for k in list(query_params.keys()):
        qp_snapshot[str(k)] = query_params.get(k)
except Exception:
    qp_snapshot = {"_error": "query_params unreadable"}

debug_payload = {
    "query_params": qp_snapshot,
    "parsed": {
        "qp_ticker": qp_ticker,
        "qp_date_raw": qp_date_raw,
        "qp_tf_raw": qp_tf_raw,
        "qp_page_id": qp_page_id,
        "qp_page_id_candidates": sorted(list(qp_page_id_candidates)) if qp_page_id_candidates else [],
        "deep_link_interval": deep_link_interval,
        "deep_link_interval_inferred_from_ticker_only": deep_link_interval_inferred_from_ticker,
        "deep_link_interval_infer_strategy": deep_link_interval_infer_strategy,
        "deep_link_record_id": (deep_link_record or {}).get("id"),
        "deep_link_record_date": str((deep_link_record or {}).get("date") or ""),
        "target_start": deep_link_target_start.isoformat() if deep_link_target_start else None,
        "target_end": deep_link_target_end.isoformat() if deep_link_target_end else None,
    },
    "ui_range": {
        "pick_start": str(st.session_state.get("pick_start")),
        "pick_end": str(st.session_state.get("pick_end")),
        "range_start_ts": str(range_start_ts),
        "range_end_ts": str(range_end_ts),
    },
    "selection": {
        "selected_ticker": selected_ticker,
        "selected_interval": selected_interval,
        "ticker_records_all": len(ticker_records_all),
        "filtered_count": len(filtered),
    },
}

has_deep_link_input = bool(qp_ticker or qp_page_id or qp_date_raw or qp_tf_raw)

if not filtered:
    all_codes = sorted({str(r.get("ticker") or "") for r in records if r.get("ticker")})
    st.info("該代碼在所選日期區間尚無資料")
    with st.expander("報錯", expanded=False):
        st.json(debug_payload)
        st.code("\n".join(all_codes[:300]))
    st.stop()

selected_trade = choose_selected_trade(
    filtered,
    qp_date,
    deep_link_interval,
    preferred_record_id=(deep_link_record or {}).get("id"),
)
# Only treat deep-link trade target as explicit when page_id exists
# or query date is parseable to an exact datetime.
selected_trade_explicit = bool(qp_page_id or deep_link_record or (qp_date is not None))
selected_trade_timeframe = str((selected_trade or {}).get("timeframe") or "").strip().lower()
if selected_trade_explicit and selected_trade_timeframe in INTERVAL_OPTIONS:
    selected_trade_key = (
        f"{selected_ticker_norm}|"
        f"{str((selected_trade or {}).get('id') or '')}|"
        f"{str((selected_trade or {}).get('date') or '')}|"
        f"{selected_trade_timeframe}"
    )
    if st.session_state.get("auto_tf_applied_key") != selected_trade_key:
        if st.session_state.get("selected_interval") != selected_trade_timeframe:
            st.session_state["selected_interval_pending"] = selected_trade_timeframe
            st.session_state["auto_tf_applied_key"] = selected_trade_key
            st.rerun()
        else:
            st.session_state["auto_tf_applied_key"] = selected_trade_key

# Deep-link selection diagnostics: show exact matched trade datetime on dashboard.
if has_deep_link_input and selected_trade:
    selected_trade_ts = pd.to_datetime(selected_trade.get("date"), errors="coerce")
    selected_trade_dt_text = "N/A"
    if pd.notna(selected_trade_ts):
        try:
            if getattr(selected_trade_ts, "tzinfo", None) is not None:
                selected_trade_ts = selected_trade_ts.tz_convert(None)
            selected_trade_dt_text = selected_trade_ts.strftime("%Y/%m/%d %H:%M:%S")
        except Exception:
            selected_trade_dt_text = str(selected_trade_ts)
    st.info(
        "目前深連結定位交易："
        f"Ticker={selected_trade.get('ticker') or 'N/A'} | "
        f"DateTime={selected_trade_dt_text} | "
        f"Timeframe={selected_trade.get('timeframe') or 'N/A'} | "
        f"RecordID={(selected_trade.get('id') or '')[:8]}"
    )
    if qp_date_raw and (qp_date is None) and (not qp_page_id):
        st.warning(
            f"深連結 date 參數「{qp_date_raw}」不是精確日期時間，系統可能改用最近/最新交易。"
            "建議 Notion 連結改帶 page_id 或完整 datetime。"
        )
manual_reset_lock_key = str(st.session_state.get("manual_reset_lock_key") or "").strip()
if manual_reset_lock_key:
    st.info("目前以手動重置為準，已暫停同一深連結的自動覆蓋。")

news_df = pd.DataFrame(filtered)
if not news_df.empty:
    news_df["__row_id"] = news_df["id"]
    def _display_date_for_row(row):
        ts = pd.to_datetime(row.get("date"), errors="coerce", utc=True)
        if pd.isna(ts):
            return ""
        is_tw = classify_asset(row.get("ticker")) == "Taiwan Stock"
        if is_tw:
            try:
                return ts.tz_convert("Asia/Taipei").strftime("%Y/%m/%d %I:%M %p (UTC+8)")
            except Exception:
                pass
        return ts.strftime("%Y/%m/%d %I:%M %p (UTC)")

    news_df["Date"] = news_df.apply(_display_date_for_row, axis=1)
    news_df["Title"] = news_df["origin_url"].fillna(news_df["url"])
    news_df["Title_Text"] = news_df["title"]
    news_df["Ticker"] = news_df["ticker"]
    news_df["Asset Class"] = news_df["asset_class"].fillna(news_df["ticker"].apply(classify_asset))
    news_df["Sentiment"] = news_df["sentiment"]
    news_df["Confidence"] = news_df["confidence"]
    news_df["Result"] = news_df["result"]
    news_df["Result_Manual"] = news_df.get("result_manual")
    news_df["Result_Auto"] = news_df.get("result_auto")
    news_df["Reason_Code"] = news_df.get("reason_code")
    news_df["Manual_Reason"] = news_df.get("manual_reason")
    news_df["System_Msg"] = news_df.get("system_msg")
    news_df["Timeframe"] = news_df["timeframe"]
    news_df["Order Type"] = news_df.get("order_type")
    news_df["Entry"] = news_df.get("entry")
    news_df["TP"] = news_df.get("tp")
    news_df["SL"] = news_df.get("sl")
    news_df["Return"] = news_df.get("return")
    news_df["Tags"] = news_df.get("tags").apply(lambda x: ", ".join(x) if isinstance(x, list) else "")
    news_df["Note"] = news_df.get("note")

    _wins = int((news_df["Result"] == "Win").sum())
    _losses = int((news_df["Result"] == "Loss").sum())
    _closed = _wins + _losses
    _win_rate = (_wins / _closed) if _closed > 0 else 0.0
    news_df["Win Rate"] = f"{_win_rate:.2%}"

    selectable_columns = [
        "Date",
        "Title",
        "Title_Text",
        "Ticker",
        "Asset Class",
        "Sentiment",
        "Confidence",
        "Result",
        "Result_Manual",
        "Result_Auto",
        "Reason_Code",
        "Manual_Reason",
        "System_Msg",
        "Timeframe",
        "Order Type",
        "Entry",
        "TP",
        "SL",
        "Return",
        "Tags",
        "Note",
        "Win Rate",
    ]
    default_visible_columns = ["Date", "Title", "Title_Text", "Asset Class", "Sentiment", "Confidence", "Result", "Timeframe"]
    cfg_visible_columns = local_cfg.get("TABLE_VISIBLE_COLUMNS", default_visible_columns)
    if not isinstance(cfg_visible_columns, list):
        cfg_visible_columns = default_visible_columns
    cfg_visible_columns = [c for c in cfg_visible_columns if c in selectable_columns]
    if not cfg_visible_columns:
        cfg_visible_columns = default_visible_columns
    cfg_column_order = local_cfg.get("TABLE_COLUMN_ORDER", selectable_columns)
    if not isinstance(cfg_column_order, list):
        cfg_column_order = selectable_columns
    cfg_column_order = [c for c in cfg_column_order if c in selectable_columns]
    for c in selectable_columns:
        if c not in cfg_column_order:
            cfg_column_order.append(c)
    if "table_column_order_state" not in st.session_state:
        st.session_state["table_column_order_state"] = cfg_column_order
    if "table_visible_columns_state" not in st.session_state:
        st.session_state["table_visible_columns_state"] = cfg_visible_columns
    active_visible_columns = [c for c in st.session_state["table_visible_columns_state"] if c in selectable_columns]
    if not active_visible_columns:
        active_visible_columns = default_visible_columns
    active_order = [c for c in st.session_state["table_column_order_state"] if c in selectable_columns]
    for c in selectable_columns:
        if c not in active_order:
            active_order.append(c)

    with st.expander("顯示欄位設定", expanded=False):
        with st.form("table_layout_form", clear_on_submit=False):
            pending_visible_columns = st.multiselect(
                "顯示欄位",
                options=active_order,
                default=active_visible_columns,
                key="table_visible_columns_form",
            )
            if not pending_visible_columns:
                st.warning("請至少選擇一個欄位。")
                pending_visible_columns = active_visible_columns
            apply_layout = st.form_submit_button("確定套用欄位設定")

        if apply_layout:
            final_ordered_visible = [c for c in pending_visible_columns if c in selectable_columns]
            if not final_ordered_visible:
                final_ordered_visible = [c for c in active_visible_columns if c in selectable_columns]

            unselected_columns = [c for c in active_order if c not in final_ordered_visible]
            new_column_order = final_ordered_visible + unselected_columns
            st.session_state["table_column_order_state"] = new_column_order
            st.session_state["table_visible_columns_state"] = final_ordered_visible

            table_cfg_patch = {
                "TABLE_VISIBLE_COLUMNS": final_ordered_visible,
                "TABLE_COLUMN_ORDER": new_column_order,
            }
            merged_cfg = dict(local_cfg)
            merged_cfg.update(table_cfg_patch)
            if merged_cfg != local_cfg:
                save_local_cfg(merged_cfg)
                local_cfg = merged_cfg
            st.rerun()

    active_visible_columns = [c for c in st.session_state["table_visible_columns_state"] if c in active_order]
    if not active_visible_columns:
        active_visible_columns = default_visible_columns

    with st.expander("交易紀錄列表", expanded=False):
        display_df = news_df[active_visible_columns + ["__row_id"]]
        display_data = display_df.drop(columns=["__row_id"])
        display_data_centered = (
            display_data.style.set_properties(**{"text-align": "center"})
            .set_table_styles([{"selector": "th", "props": [("text-align", "center")]}])
        )

        dynamic_col_cfg = {}
        if "Title" in active_visible_columns:
            dynamic_col_cfg["Title"] = st.column_config.LinkColumn("Title", display_text="開啟原文")
        try:
            table_event = st.dataframe(
                display_data_centered,
                use_container_width=True,
                hide_index=True,
                on_select="rerun",
                selection_mode="single-row",
                key="notion_rows_table",
                column_config=dynamic_col_cfg if dynamic_col_cfg else None,
            )
            selected_rows = []
            if isinstance(table_event, dict):
                selected_rows = (table_event or {}).get("selection", {}).get("rows", [])
            else:
                try:
                    selected_rows = list(getattr(getattr(table_event, "selection", None), "rows", []) or [])
                except Exception:
                    selected_rows = []
            if selected_rows:
                idx = int(selected_rows[0])
                if 0 <= idx < len(display_df):
                    selected_row_id = display_df.iloc[idx]["__row_id"]
                    selected_trade = next((r for r in filtered if r.get("id") == selected_row_id), selected_trade)
        except TypeError:
            st.dataframe(
                display_data_centered,
                use_container_width=True,
                hide_index=True,
                column_config=dynamic_col_cfg if dynamic_col_cfg else None,
            )

end = datetime.now(timezone.utc)
focus_fetch_mode = bool(st.session_state.get("auto_focus_toggle", True)) and (selected_trade is not None)
if focus_fetch_mode:
    anchor_ts = to_naive_timestamp((selected_trade or {}).get("date"))
    if anchor_ts is not None:
        if getattr(anchor_ts, "tzinfo", None) is None:
            anchor_ts = anchor_ts.replace(tzinfo=timezone.utc)
        else:
            anchor_ts = anchor_ts.astimezone(timezone.utc)
        fetch_buffer_by_interval = {
            "1d": 500,
            "4h": 500,
            "1h": 500,
            "15m": 500,
            "5m": 500,
            "1m": 500,
        }
        fetch_buffer_bars = int(fetch_buffer_by_interval.get(selected_interval, 1000))
        fetch_delta = interval_to_timedelta(selected_interval) * fetch_buffer_bars
        start = anchor_ts - fetch_delta
        end = anchor_ts + fetch_delta
    else:
        if selected_interval == "1d":
            start = end - timedelta(days=365 * 5)
        elif selected_interval in {"4h", "1h"}:
            start = end - timedelta(days=730)
        elif selected_interval in {"15m", "5m", "1m"}:
            start = end - timedelta(days=60)
        else:
            start = end - timedelta(days=365)
else:
    # Non-focus mode must respect UI-selected date range.
    start = datetime.combine(selected_start_date, datetime.min.time(), tzinfo=timezone.utc)
    end = datetime.combine(selected_end_date + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)

try:
    price_df, price_source, price_debug_log = fetch_prices(
        selected_ticker,
        start,
        end,
        selected_interval,
        twelve_data_api_key,
        fugle_api_key,
        st.session_state["refresh_nonce"],
        focus_mode_requested=focus_fetch_mode,
    )
except Exception as exc:
    st.error(f"抓價失敗：{exc}")
    st.stop()

if "guardrail_truncated=true" in str(price_debug_log or ""):
    st.warning(
        "效能保護機制已啟動：此週期請求的歷史區間過長，"
        "系統已自動限制實際 K 線載入範圍。"
    )
    suggested_interval_map = {
        "1m": "15m",
        "5m": "1h",
        "15m": "1h",
        "1h": "4h",
    }
    suggested_interval = suggested_interval_map.get(str(selected_interval or "").lower())
    if suggested_interval:
        st.caption(
            f"若需檢視更早的歷史軌跡，建議切換至 {suggested_interval} 週期。"
        )

# Cache latest API-returned visible range for "重置全歷史範圍" button.
if not price_df.empty and "date" in price_df.columns:
    try:
        _api_dates = pd.to_datetime(price_df["date"], errors="coerce").dropna()
        if not _api_dates.empty:
            st.session_state["last_api_range_start"] = _api_dates.min().date().isoformat()
            st.session_state["last_api_range_end"] = _api_dates.max().date().isoformat()
    except Exception:
        pass

selected_asset_type = classify_asset(selected_ticker)
selected_symbol = normalize_symbol(selected_ticker)
selected_symbol_stock = selected_symbol
if selected_symbol.isdigit() and len(selected_symbol) == 4:
    selected_symbol_stock = f"{selected_symbol}.TW"
memory_route_ticker = (
    selected_symbol_stock
    if selected_asset_type in {"Taiwan Stock", "US Stock", "Forex", "Commodity"}
    else selected_symbol
)
memory_key = f"{memory_route_ticker}_{selected_interval}"
memory_entry = load_optimization_memory().get(memory_key, {})
memory_counter = int(memory_entry.get("counter", 0) or 0)
memory_verified_days = int(memory_entry.get("verified_days", 60) or 60)
memory_preferred_source = memory_entry.get("preferred_source") or "(none)"

if price_df.empty:
    if "plan_limited" in (price_debug_log or "") or "pro plan" in (price_debug_log or "").lower():
        st.info("偵測到數據商權限限制，已自動切換本土備援引擎")
    if classify_asset(selected_ticker) == "Taiwan Stock" and selected_interval != "1d":
        st.error("[Error] 供應商暫無此時段之分盤數據，請嘗試切換至日線 (1d)")
    else:
        st.error("抓不到價格資料，請確認代碼格式或 API 金鑰")
    with st.expander("資料來源訊息", expanded=False):
        routing_stats = summarize_routing_stats()
        st.caption(f"資料來源嘗試結果：{price_source}")
        st.caption(
            "SmartRouting(最近100次): "
            f"Hit {routing_stats['hit']} / Miss {routing_stats['miss']} / "
            f"HitRate {routing_stats['hit_rate']:.1%}"
        )
        st.caption(
            "OptimizationMemory: "
            f"counter={memory_counter}, verified_days={memory_verified_days}, "
            f"preferred_source={memory_preferred_source}"
        )
        st.caption("Fugle Endpoint: historical/candles")
        if price_debug_log:
            st.code(price_debug_log)
    st.stop()

if force_regrade:
    if not selected_trade or not selected_trade.get("id"):
        st.warning("請先選擇一筆交易。")
    else:
        try:
            st.toast("正在同步本地數據...", icon="⏳")
            outcome = run_force_trade_grade(
                selected_trade,
                price_df,
                selected_interval,
                NOTION_HEADERS,
                timeout_bars=grade_timeout_bars,
            )
            if outcome is None:
                raise RuntimeError("grading cycle did not write an outcome")
            track_mode = str(
                selected_trade.get("track_mode") or "Track1"
            ).strip()
            if track_mode == "Track2":
                if outcome.outcome_label == "Pending":
                    if outcome.reason_code or outcome.bars_elapsed:
                        st.info(
                            "Track2 尚待觀察"
                            f"（已累積 {outcome.bars_elapsed} 根 K）"
                        )
                    else:
                        st.info("Track2 已重置為 Pending")
                else:
                    st.success(
                        "Track2 已更新為 "
                        f"{outcome.outcome_label}（{outcome.reason_code}）"
                    )
            elif outcome.outcome_label == "Pending":
                selected_trade["result"] = "Pending"
                st.info("已重置為 Pending")
            else:
                selected_trade["result"] = outcome.outcome_label
                st.success(
                    f"已更新為 {outcome.outcome_label} "
                    f"(第 {outcome.bars_elapsed} 根 K)"
                )
            st.session_state["records"] = fetch_database_entries(
                db_id, NOTION_HEADERS
            )
            st.rerun()
        except Exception as exc:
            st.error(f"強制重算失敗：{exc}")



if auto_grade:
    st.toast("正在同步本地數據...", icon="⏳")
    updated = run_auto_grade(
        filtered,
        price_df,
        selected_interval,
        NOTION_HEADERS,
        timeout_bars=grade_timeout_bars,
        silent=False,
    )
    st.session_state["records"] = fetch_database_entries(db_id, NOTION_HEADERS)
    if updated:
        st.success(f"已更新 {updated} 筆結果")
    st.rerun()

if background_auto_grade:
    st.toast("正在同步本地數據...", icon="⏳")
    bg_updated = run_background_auto_grade_all(
        records,
        selected_interval,
        twelve_data_api_key,
        fugle_api_key,
        st.session_state["refresh_nonce"],
        NOTION_HEADERS,
        timeout_bars=grade_timeout_bars,
    )
    time.sleep(1)
    st.session_state["records"] = fetch_database_entries(db_id, NOTION_HEADERS)
    if bg_updated:
        st.success(f"已更新 {bg_updated} 筆結果")
    st.rerun()

if st.session_state.pop("daemon_local_sync_trigger", False):
    with st.spinner("同步中... (正在本地計算並更新 Notion)"):
        run_auto_grade(
            filtered,
            price_df,
            selected_interval,
            NOTION_HEADERS,
            timeout_bars=grade_timeout_bars,
            silent=True,
        )
        time.sleep(2)
        st.session_state["records"] = fetch_database_entries(db_id, NOTION_HEADERS)
    st.rerun()

# Chart range is controlled by sidebar date range first.
focus_mode_requested = bool(st.session_state.get("auto_focus_toggle", True)) and (selected_trade is not None)
chart_base_df = price_df
if not price_df.empty and not focus_mode_requested:
    chart_dates = pd.to_datetime(price_df["date"], errors="coerce")
    if getattr(chart_dates.dt, "tz", None) is not None:
        chart_dates = chart_dates.dt.tz_convert(None)
    chart_mask = (chart_dates >= range_start_ts) & (chart_dates <= range_end_ts)
    chart_base_df = price_df.loc[chart_mask].copy().reset_index(drop=True)

chart_price_df = chart_base_df
auto_focus_enabled = bool(st.session_state.get("auto_focus_toggle", True))
if selected_trade is not None and auto_focus_enabled and not chart_base_df.empty:
    focus_idx, _, _ = align_trade_to_candle(chart_base_df, selected_trade.get("date"), selected_interval)
    if focus_idx is not None:
        pre_bars = 500
        post_bars = 500
        start_i = max(0, int(focus_idx) - pre_bars)
        end_i = min(len(chart_base_df), int(focus_idx) + post_bars + 1)
        chart_price_df = chart_base_df.iloc[start_i:end_i].reset_index(drop=True)

tv_data = convert_price_df_to_tv(chart_price_df)
tv_volume_data = convert_volume_df_to_tv(chart_price_df)
markers_data, snapped_marker_diagnostics = build_tv_markers(
    filtered,
    chart_price_df,
    selected_interval,
    selected_trade=selected_trade,
    show_order_types=bool(st.session_state.get("show_order_types", False)),
)
show_plan_levels = bool(st.session_state.get("auto_focus_toggle", True))
if show_plan_levels:
    plan_line_series = build_plan_line_series(tv_data, selected_trade)
    plan_price_lines = build_plan_price_lines(selected_trade)
else:
    plan_line_series = []
    plan_price_lines = []

if not tv_data:
    st.error("K 線資料轉換失敗，無法渲染 TradingView 圖表。")
    st.stop()

chart_options = {
    "height": 600,
    "layout": {
        "textColor": "#d1d4dc",
        "background": {
            "type": "solid",
            "color": "#131722",
        },
    },
    "localization": {
        "locale": "zh-TW",
        "dateFormat": "yyyy/MM/dd",
    },
    "handleScroll": True,
    "handleScale": True,
    "grid": {
        "vertLines": {
            "color": "#363c4e",
            "style": 1,
        },
        "horzLines": {
            "color": "#363c4e",
            "style": 1,
        },
    },
    "watermark": {
        "visible": True,
        "text": f"{selected_ticker} · {selected_interval}",
        "color": "rgba(171, 190, 209, 0.3)",
        "fontSize": 48,
        "horzAlign": "center",
        "vertAlign": "center",
    },
    "crosshair": {
        "mode": 1,
        "vertLine": {
            "labelVisible": True,
            "labelBackgroundColor": "#131722",
        },
        "horzLine": {
            "labelVisible": True,
            "labelBackgroundColor": "#131722",
        },
    },
    "rightPriceScale": {
        "borderColor": "#485c7b",
        "scaleMargins": {"top": 0.1, "bottom": 0.2},
        "autoScale": True,
        "visible": True,
        "entireTextOnly": False,
    },
    "timeScale": {
        "borderColor": "#485c7b",
        "timeVisible": True,
        "secondsVisible": False,
        "rightBarStaysOnScroll": True,
        "lockVisibleTimeRangeOnResize": False,
        "fixLeftEdge": False,
        "fixRightEdge": False,
        "shiftVisibleRangeOnNewBar": True,
        "rightOffset": 5 if auto_focus_enabled else 8,
        "minBarSpacing": 0.6,
        "visible": True,
        "ticksVisible": True,
    },
}

series_candlestick = [
    {
        "type": "Candlestick",
        "data": tv_data,
        "markers": markers_data,
        "options": {
            "upColor": "#26a69a",
            "downColor": "#ef5350",
            "borderVisible": False,
            "wickUpColor": "#26a69a",
            "wickDownColor": "#ef5350",
            "priceLines": plan_price_lines,
        },
    }
]
if plan_line_series:
    series_candlestick.extend(plan_line_series)
if tv_volume_data:
    series_candlestick.append(
        {
            "type": "Histogram",
            "data": tv_volume_data,
            "options": {
                "priceFormat": {"type": "volume"},
                "priceScaleId": "volume",
                "lastValueVisible": False,
                "priceLineVisible": False,
            },
            "priceScale": {
                "scaleMargins": {
                    "top": 0.8,
                    "bottom": 0,
                }
            },
        }
    )

reset_key = f"tv_chart_reset_nonce_{selected_ticker_norm}_{selected_interval}"
if reset_key not in st.session_state:
    st.session_state[reset_key] = 0
selected_trade_id = (selected_trade or {}).get("id", "none")
plot_key = (
    f"main_chart_tv_{selected_ticker_norm}_{selected_interval}_"
    f"{selected_trade_id}_{int(bool(auto_focus_enabled))}_{st.session_state[reset_key]}"
)
renderLightweightCharts(
    [
        {
            "chart": chart_options,
            "series": series_candlestick,
        }
    ],
    key=plot_key,
)
_, right_col = st.columns([0.94, 0.06])
with right_col:
    if st.button("⟳", key=f"btn_reset_tv_{selected_ticker_norm}_{selected_interval}", help="重置圖表視角"):
        st.session_state[reset_key] += 1
        st.rerun()
if snapped_marker_diagnostics:
    st.warning(f"有 {len(snapped_marker_diagnostics)} 筆交易因時間落在非交易 K 棒而強制吸附。")
    st.dataframe(pd.DataFrame(snapped_marker_diagnostics), use_container_width=True, hide_index=True)
st.caption(f"資料來源標籤：[{price_source}]")

df_stats = pd.DataFrame(filtered)
df_stats_all_time = pd.DataFrame(ticker_records_all)
portfolio_df = df_notion.copy()
if not portfolio_df.empty:
    portfolio_df["__date_filter__"] = pd.to_datetime(portfolio_df.get("date"), errors="coerce")
    portfolio_df = portfolio_df.dropna(subset=["__date_filter__"])
    portfolio_df["__date_filter__"] = portfolio_df["__date_filter__"].apply(
        lambda x: x.tz_convert(None) if getattr(x, "tzinfo", None) is not None else x
    )
    portfolio_df = portfolio_df[
        (portfolio_df["__date_filter__"] >= range_start_ts) & (portfolio_df["__date_filter__"] <= range_end_ts)
    ]


def build_calibration_stats(df: pd.DataFrame, score_col: str) -> pd.DataFrame:
    if df.empty or score_col not in df.columns:
        return pd.DataFrame()
    pnl_series = (
        pd.to_numeric(df.get("return"), errors="coerce")
        if "return" in df.columns
        else pd.Series([float("nan")] * len(df), index=df.index)
    )
    work_df = pd.DataFrame(
        {
            score_col: pd.to_numeric(df.get(score_col), errors="coerce"),
            "result": df.get("result"),
            "pnl": pnl_series,
        }
    ).dropna(subset=[score_col])
    if work_df.empty:
        return pd.DataFrame()
    work_df[score_col] = work_df[score_col].clip(lower=0, upper=5)
    def _agg_one(group: pd.DataFrame) -> pd.Series:
        avg_win = group.loc[(group["result"] == "Win") & group["pnl"].notna(), "pnl"].mean()
        avg_loss = group.loc[(group["result"] == "Loss") & group["pnl"].notna(), "pnl"].mean()
        rr = (avg_win / abs(avg_loss)) if pd.notna(avg_win) and pd.notna(avg_loss) and avg_loss != 0 else float("nan")
        return pd.Series(
            {
                "win_rate": (group["result"] == "Win").mean() * 100.0,
                "volume": len(group),
                "rr": rr,
            }
        )

    stats_df = work_df.groupby(score_col, group_keys=False).apply(_agg_one).reset_index().sort_values(score_col)
    return stats_df


def plot_calibration_bubble(stats_df: pd.DataFrame, score_col: str, title: str) -> None:
    if stats_df.empty:
        return
    plot_df = stats_df.copy()
    has_rr = plot_df["rr"].notna().any()
    color_col = "rr" if has_rr else "win_rate"
    color_scale = "RdBu" if has_rr else "Tealgrn"
    hover_cfg = {
        score_col: True,
        "win_rate": ":.1f",
        "volume": True,
    }
    if has_rr:
        hover_cfg["rr"] = ":.2f"
    else:
        plot_df["rr_display"] = "N/A"
        hover_cfg["rr_display"] = True
    fig = px.scatter(
        plot_df,
        x=score_col,
        y="win_rate",
        size="volume",
        color=color_col,
        size_max=36,
        color_continuous_scale=color_scale,
        color_continuous_midpoint=0 if has_rr else None,
        hover_data=hover_cfg,
        title=title,
        labels={"rr": "RR", "rr_display": "RR", "win_rate": "Win Rate", "volume": "Volume"},
    )
    fig.update_traces(
        marker={"sizemin": 8, "line": {"width": 1, "color": "rgba(255,255,255,0.45)"}},
        cliponaxis=False,
    )
    fig.update_layout(height=360)
    fig.update_xaxes(range=[0, 5], fixedrange=True)
    fig.update_yaxes(range=[0, 100], ticksuffix="%", fixedrange=True)
    fig.add_hline(y=50, line_dash="dash", line_color="#9aa0a6")
    st.plotly_chart(
        fig,
        use_container_width=True,
        config={
            "scrollZoom": False,
            "modeBarButtonsToRemove": [
                "zoom2d",
                "pan2d",
                "select2d",
                "lasso2d",
                "zoomIn2d",
                "zoomOut2d",
                "autoScale2d",
                "resetScale2d",
            ],
        },
    )


with st.expander("區間內的統計數據", expanded=False):
    if not df_stats.empty:
        wins = int((df_stats.get("result") == "Win").sum())
        losses = int((df_stats.get("result") == "Loss").sum())
        total_closed = wins + losses
        win_rate = (wins / total_closed) if total_closed > 0 else 0.0
        pnl_series = pd.to_numeric(df_stats.get("return"), errors="coerce")
        avg_win = pnl_series[(df_stats.get("result") == "Win") & pnl_series.notna()].mean()
        avg_loss = pnl_series[(df_stats.get("result") == "Loss") & pnl_series.notna()].mean()
        rr = (avg_win / abs(avg_loss)) if pd.notna(avg_win) and pd.notna(avg_loss) and avg_loss != 0 else None

        m1, m2, m3 = st.columns(3)
        m1.metric("Closed Trades", total_closed)
        m2.metric("Win Rate", f"{win_rate:.2%}")
        m3.metric("RR (AvgWin/AvgLoss)", f"{rr:.2f}" if rr is not None else "N/A")

        corr_df = pd.DataFrame(
            {
                "Confidence": pd.to_numeric(df_stats.get("confidence"), errors="coerce"),
                "Return": pnl_series,
                "Result": df_stats.get("result"),
            }
        ).dropna(subset=["Confidence", "Return"])
        if not corr_df.empty:
            corr_fig = px.scatter(
                corr_df,
                x="Confidence",
                y="Return",
                color="Result",
                color_discrete_map={"Win": "#26a69a", "Loss": "#ef5350", "Timed Out": "#ffb74d"},
                title="Confidence vs PnL Correlation",
            )
            corr_fig.update_layout(height=320)
            st.plotly_chart(corr_fig, use_container_width=True)

        conf_stats = build_calibration_stats(df_stats, "confidence")
        plot_calibration_bubble(conf_stats, "confidence", "信心校準圖")

        mind_stats = build_calibration_stats(df_stats, "mindset")
        plot_calibration_bubble(mind_stats, "mindset", "心態校準圖")

        tags_expanded = df_stats.explode("tags")
        tags_expanded = tags_expanded[tags_expanded["tags"].notna()]
        if not tags_expanded.empty:
            tag_stats = tags_expanded.groupby("tags")["result"].apply(lambda x: (x == "Win").mean()).reset_index()
            tag_stats = tag_stats.sort_values("result", ascending=False)
            tag_fig = go.Figure(data=[go.Bar(x=tag_stats["tags"], y=tag_stats["result"])])
            tag_fig.update_layout(title="能力圈熱力圖", height=320)
            st.plotly_chart(tag_fig, use_container_width=True)
    else:
        st.info("目前區間內沒有可用的統計資料。")

with st.expander("當前標全時間的統計數據", expanded=False):
    if df_stats_all_time.empty:
        st.info("目前沒有可用的全時間資料。")
    else:
        at_wins = int((df_stats_all_time.get("result") == "Win").sum())
        at_losses = int((df_stats_all_time.get("result") == "Loss").sum())
        at_closed = at_wins + at_losses
        at_win_rate = (at_wins / at_closed) if at_closed > 0 else 0.0
        at_pnl = pd.to_numeric(df_stats_all_time.get("return"), errors="coerce")
        at_avg_win = at_pnl[(df_stats_all_time.get("result") == "Win") & at_pnl.notna()].mean()
        at_avg_loss = at_pnl[(df_stats_all_time.get("result") == "Loss") & at_pnl.notna()].mean()
        at_rr = (at_avg_win / abs(at_avg_loss)) if pd.notna(at_avg_win) and pd.notna(at_avg_loss) and at_avg_loss != 0 else None

        at1, at2, at3 = st.columns(3)
        at1.metric("All-time Closed Trades", at_closed)
        at2.metric("All-time Win Rate", f"{at_win_rate:.2%}")
        at3.metric("All-time RR", f"{at_rr:.2f}" if at_rr is not None else "N/A")

        at_conf = build_calibration_stats(df_stats_all_time, "confidence")
        plot_calibration_bubble(at_conf, "confidence", "全時間信心校準圖")

        at_mind = build_calibration_stats(df_stats_all_time, "mindset")
        plot_calibration_bubble(at_mind, "mindset", "全時間心態校準圖")

        at_tags = df_stats_all_time.explode("tags")
        at_tags = at_tags[at_tags["tags"].notna()]
        if not at_tags.empty:
            at_tag_stats = at_tags.groupby("tags")["result"].apply(lambda x: (x == "Win").mean()).reset_index()
            at_tag_stats = at_tag_stats.sort_values("result", ascending=False)
            at_tag_fig = go.Figure(data=[go.Bar(x=at_tag_stats["tags"], y=at_tag_stats["result"])])
            at_tag_fig.update_layout(title="全時間能力圈熱力圖", height=320)
            st.plotly_chart(at_tag_fig, use_container_width=True)

with st.expander("全域的統計數據", expanded=False):
    if portfolio_df.empty:
        st.info("此日期區間內沒有可用交易資料。")
    else:
        p_wins = int((portfolio_df.get("result") == "Win").sum())
        p_losses = int((portfolio_df.get("result") == "Loss").sum())
        p_total = int(len(portfolio_df))
        p_closed = p_wins + p_losses
        p_win_rate = (p_wins / p_closed) if p_closed > 0 else 0.0
        p_pnl = pd.to_numeric(portfolio_df.get("return"), errors="coerce")
        p_avg_win = p_pnl[(portfolio_df.get("result") == "Win") & p_pnl.notna()].mean()
        p_avg_loss = p_pnl[(portfolio_df.get("result") == "Loss") & p_pnl.notna()].mean()
        p_rr = (p_avg_win / abs(p_avg_loss)) if pd.notna(p_avg_win) and pd.notna(p_avg_loss) and p_avg_loss != 0 else None

        p1, p2, p3 = st.columns(3)
        p1.metric("Total Trades", p_total)
        p2.metric("Portfolio Win Rate", f"{p_win_rate:.2%}")
        p3.metric("Portfolio RR", f"{p_rr:.2f}" if p_rr is not None else "N/A")

        p_conf = build_calibration_stats(portfolio_df, "confidence")
        plot_calibration_bubble(p_conf, "confidence", "全域信心校準圖")

        p_mind = build_calibration_stats(portfolio_df, "mindset")
        plot_calibration_bubble(p_mind, "mindset", "全域心態校準圖")

        p_tags = portfolio_df.explode("tags")
        p_tags = p_tags[p_tags["tags"].notna()]
        if not p_tags.empty:
            p_tag_stats = p_tags.groupby("tags")["result"].apply(lambda x: (x == "Win").mean()).reset_index()
            p_tag_stats = p_tag_stats.sort_values("result", ascending=False)
            p_tag_fig = go.Figure(data=[go.Bar(x=p_tag_stats["tags"], y=p_tag_stats["result"])])
            p_tag_fig.update_layout(title="Portfolio 能力圈熱力圖", height=320)
            st.plotly_chart(p_tag_fig, use_container_width=True)

with st.expander("點擊展開 AI 分析面板", expanded=False):
    st.markdown("AI 交易教練將分析近期交易的信心校準度與情緒模式。")
    if ai_provider == "停用 (預設)" or not current_api_key:
        st.info("目前 AI 功能已停用，如需使用請至側邊欄設定。")
    else:
        recent_n = st.slider("分析最近幾筆交易", 5, 50, 10)
        run_ai = st.button("開始分析")

        if run_ai:
            df_ai = df_notion.copy()
            if "date" in df_ai.columns:
                df_ai = df_ai.sort_values("date", ascending=False)
            df_ai = df_ai.head(recent_n)
            ticker_src_col = "ticker" if "ticker" in df_ai.columns else col_name("ticker")
            if ticker_src_col in df_ai.columns and ticker_src_col != "Ticker":
                df_ai = df_ai.rename(columns={ticker_src_col: "Ticker"})
            if "Ticker" not in df_ai.columns:
                df_ai["Ticker"] = ""
            for required_col, default_value in {
                "date": None,
                "sentiment": "",
                "confidence": None,
                "result": "",
                "tags": [[] for _ in range(len(df_ai))],
                "note": "",
            }.items():
                if required_col not in df_ai.columns:
                    df_ai[required_col] = default_value
            df_ai = df_ai[["Ticker", "date", "sentiment", "confidence", "result", "tags", "note"]]

            if df_ai.empty:
                st.warning("沒有可分析的交易紀錄。")
            else:
                def format_table(df):
                    def _safe_cell_str(val):
                        if val is None:
                            return ""
                        try:
                            if pd.isna(val):
                                return ""
                        except Exception:
                            pass
                        return str(val)

                    df = df.rename(
                        columns={
                            "date": "Date",
                            "sentiment": "Sentiment",
                            "confidence": "Confidence",
                            "result": "Result",
                            "tags": "Tags",
                            "note": "Note",
                        }
                    )
                    df["Date"] = pd.to_datetime(df["Date"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M")
                    df["Tags"] = df["Tags"].apply(lambda x: ", ".join(x) if isinstance(x, list) else "")
                    headers = ["Ticker", "Date", "Sentiment", "Confidence", "Result", "Tags", "Note"]
                    lines = [
                        "| " + " | ".join(headers) + " |",
                        "| " + " | ".join(["---"] * len(headers)) + " |",
                    ]
                    for _, row in df.iterrows():
                        values = [_safe_cell_str(row.get(h, "")) for h in headers]
                        lines.append("| " + " | ".join(values) + " |")
                    return "\n".join(lines)

                table_md = format_table(df_ai)

                system_prompt = (
                    "你是一位嚴格的交易心理教練。請根據使用者的交易紀錄，"
                    "重點分析信心與結果的校準度，以及賠錢交易的情緒模式（Tags/Note）。"
                    "請給出具體、可行的改進建議，避免空泛鼓勵。"
                )
                user_prompt = (
                    "以下是最近的交易紀錄（Markdown 表格）：\n\n"
                    f"{table_md}\n\n"
                    "請輸出：\n"
                    "1. 信心校準度分析\n"
                    "2. 虧損交易的情緒模式\n"
                    "3. 具體改進建議（條列）\n"
                )

                try:
                    missing_dep = False
                    if ai_provider == "Google Gemini":
                        try:
                            import google.generativeai as genai
                        except Exception:
                            st.warning("未安裝 google-generativeai，請先安裝後再使用 Gemini。")
                            missing_dep = True
                        if not missing_dep:
                            genai.configure(api_key=current_api_key)
                            model = genai.GenerativeModel("gemini-1.5-flash")
                            response = model.generate_content(f"{system_prompt}\n\n{user_prompt}")
                            content = getattr(response, "text", None)
                        else:
                            content = None
                    else:
                        try:
                            import openai
                        except Exception:
                            st.warning("未安裝 openai 套件，請先安裝後再使用 AI。")
                            missing_dep = True
                        if not missing_dep:
                            if ai_provider == "Groq":
                                client = openai.OpenAI(
                                    api_key=current_api_key, base_url="https://api.groq.com/openai/v1"
                                )
                                model_name = "llama3-70b-8192"
                            else:
                                client = openai.OpenAI(api_key=current_api_key)
                                model_name = "gpt-4o"
                            resp = client.chat.completions.create(
                                model=model_name,
                                messages=[
                                    {"role": "system", "content": system_prompt},
                                    {"role": "user", "content": user_prompt},
                                ],
                                temperature=0.4,
                            )
                            content = resp.choices[0].message.content if resp.choices else None
                        else:
                            content = None

                    if content:
                        st.markdown(content)
                    else:
                        st.warning("AI 回應為空，請稍後再試。")
                except Exception as exc:
                    st.error(f"AI 分析發生錯誤：{exc}")

if has_deep_link_input and filtered:
    with st.expander("報錯", expanded=False):
        st.json(debug_payload)

with st.expander("資料來源訊息", expanded=False):
    routing_stats = summarize_routing_stats()
    st.caption(f"目前價格來源：{price_source}")
    st.caption(
        "SmartRouting(最近100次): "
        f"Hit {routing_stats['hit']} / Miss {routing_stats['miss']} / "
        f"HitRate {routing_stats['hit_rate']:.1%}"
    )
    st.caption(
        "OptimizationMemory: "
        f"counter={memory_counter}, verified_days={memory_verified_days}, "
        f"preferred_source={memory_preferred_source}"
    )
    st.caption("Fugle Endpoint: historical/candles")
    if selected_interval in {"4h", "1h", "15m", "5m", "1m"} and len(price_df) < 40:
        st.warning(f"目前分時 K 棒數偏少（{len(price_df)} 根），可能是資料源可提供的歷史深度不足。")
    if price_debug_log:
        st.code(price_debug_log)
