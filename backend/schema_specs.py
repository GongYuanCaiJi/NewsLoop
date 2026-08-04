from __future__ import annotations

from typing import Any, Optional

NOTION_VERSION = "2022-06-28"

DEFAULT_COLUMN_MAPPING: dict[str, str] = {
    "title": "Title",
    "ticker": "Ticker",
    "sentiment": "Sentiment",
    "order_type": "Order Type",
    "asset_class": "Asset Class",
    "sector": "Sector",
    "confidence": "Confidence",
    "mindset": "Mindset",
    "timeframe": "Timeframe",
    "entry": "Entry",
    "tp": "TP",
    "sl": "SL",
    "rr": "RR",
    "instrument": "Instrument",
    "exposure": "Exposure",
    "tags": "Tags",
    "note": "Note",
    "origin_url": "Origin_URL",
    "date": "Date",
    "result": "Result",
    "result_manual": "Result_Manual",
    "result_auto": "Result_Auto",
    "manual_reason": "Manual_Reason",
    "return": "Return",
    "system_msg": "System_Msg",
    "reason_code": "Reason_Code",
    "track_mode": "Track_Mode",
    "t2_bars_limit": "T2_Bars_Limit",
    "t2_threshold_pct": "T2_Threshold_Pct",
    "t2_entry_price": "T2_Entry_Price",
    "t2_entry_time": "T2_Entry_Time",
    "t2_observed_high": "T2_Observed_High",
    "t2_observed_low": "T2_Observed_Low",
    "t2_final_close": "T2_Final_Close",
    "t2_last_checked_at": "T2_Last_Checked_At",
    "t2_result": "T2_Result",
    "t2_reason": "T2_Reason",
}

# 這些欄位屬於主流程核心資料，不允許在 fail-open 降級時被移除。
CORE_FIELDS: tuple[str, ...] = (
    "title",
    "ticker",
    "date",
    "sentiment",
    "timeframe",
    "track_mode",
)

FIELD_SPECS: dict[str, dict[str, Any]] = {
    "title": {"types": {"title"}, "level": "warn"},
    "ticker": {"types": {"rich_text"}, "level": "error"},
    "sentiment": {"types": {"select"}, "level": "warn"},
    "order_type": {"types": {"select"}, "level": "warn"},
    "asset_class": {"types": {"multi_select", "select"}, "level": "warn"},
    "sector": {"types": {"multi_select", "select"}, "level": "warn"},
    "confidence": {"types": {"number"}, "level": "warn"},
    "mindset": {"types": {"number"}, "level": "warn"},
    "timeframe": {"types": {"select"}, "level": "error"},
    "entry": {"types": {"number"}, "level": "warn"},
    "tp": {"types": {"number"}, "level": "warn"},
    "sl": {"types": {"number"}, "level": "warn"},
    "rr": {"types": {"number"}, "level": "warn"},
    "instrument": {"types": {"select"}, "level": "warn"},
    "exposure": {"types": {"number"}, "level": "warn"},
    "tags": {"types": {"multi_select"}, "level": "warn"},
    "note": {"types": {"rich_text"}, "level": "warn"},
    "origin_url": {"types": {"url"}, "level": "warn"},
    "date": {"types": {"date"}, "level": "error"},
    # `result` may be a Notion formula (read-only) or legacy select.
    # Formula is intentionally accepted for schema validation, but skipped on write.
    "result": {"types": {"formula", "select"}, "level": "warn"},
    "result_manual": {"types": {"select"}, "level": "warn"},
    "result_auto": {"types": {"select"}, "level": "error"},
    "manual_reason": {"types": {"select"}, "level": "warn"},
    "return": {"types": {"number"}, "level": "warn"},
    # backward compatibility for legacy DBs
    "system_msg": {"types": {"rich_text", "number"}, "level": "warn"},
    "reason_code": {"types": {"rich_text", "select"}, "level": "warn"},
    "track_mode": {"types": {"select"}, "level": "warn"},
    "t2_bars_limit": {"types": {"number"}, "level": "warn"},
    "t2_threshold_pct": {"types": {"number"}, "level": "warn"},
    "t2_entry_price": {"types": {"number"}, "level": "warn"},
    "t2_entry_time": {"types": {"date"}, "level": "warn"},
    "t2_observed_high": {"types": {"number"}, "level": "warn"},
    "t2_observed_low": {"types": {"number"}, "level": "warn"},
    "t2_final_close": {"types": {"number"}, "level": "warn"},
    "t2_last_checked_at": {"types": {"date"}, "level": "warn"},
    "t2_result": {"types": {"select"}, "level": "warn"},
    "t2_reason": {"types": {"rich_text", "select"}, "level": "warn"},
}


def normalize_column_mapping(raw: Optional[dict]) -> dict[str, str]:
    out = dict(DEFAULT_COLUMN_MAPPING)
    if isinstance(raw, dict):
        for k, v in raw.items():
            k_str = str(k).strip()
            v_str = str(v).strip() if v is not None else ""
            if k_str and v_str:
                out[k_str] = v_str
    return out


def col_name(mapping: dict[str, str], internal_key: str) -> str:
    return mapping.get(internal_key, DEFAULT_COLUMN_MAPPING.get(internal_key, internal_key))
