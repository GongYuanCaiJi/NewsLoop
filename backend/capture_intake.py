from __future__ import annotations

import copy
import logging
import re
from datetime import datetime, timezone
from typing import Annotated, Callable, Optional, Protocol
from urllib.parse import urlparse

import requests
from pydantic import BaseModel, Field, StringConstraints

try:
    from .io_utils import parse_optional_float, request_with_retry
    from .ticker_utils import (
        SectorCacheError,
        classify_asset,
        get_asset_sector_with_reason,
        normalize_ticker_for_storage,
    )
except ImportError:
    from io_utils import parse_optional_float, request_with_retry
    from ticker_utils import (
        SectorCacheError,
        classify_asset,
        get_asset_sector_with_reason,
        normalize_ticker_for_storage,
    )


logger = logging.getLogger("newsloop.capture_intake")
NOTION_PAGES_URL = "https://api.notion.com/v1/pages"


class TradingThesisInput(BaseModel):
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    ticker: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=30)]
    sentiment: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20)]
    confidence: int
    mindset: Optional[int] = None
    tags: Annotated[
        list[Annotated[str, StringConstraints(strip_whitespace=True, max_length=50)]],
        Field(max_length=20),
    ] = Field(default_factory=list)
    note: Annotated[str, StringConstraints(max_length=2000)] = ""
    origin_url: Optional[str] = Field(default=None, max_length=2048)
    date: Optional[str] = Field(default=None, max_length=64)
    custom_date: Optional[str] = Field(default=None, max_length=64)
    timeframe: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=10)] = "1d"
    instrument: Optional[str] = Field(default=None, max_length=32)
    exposure: Optional[float] = None
    entry: Optional[float] = None
    tp: Optional[float] = None
    sl: Optional[float] = None
    rr: Optional[float] = None
    order_type: Optional[str] = Field(default=None, max_length=16)
    track_mode: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=16)] = "Track1"
    t2_bars_limit: Optional[int] = None
    t2_threshold_pct: Optional[float] = None
    t2_entry_price: Optional[float] = None
    t2_entry_time: Optional[str] = Field(default=None, max_length=64)


class CaptureError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = int(status_code)
        self.detail = str(detail)


class NotionWriteError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = int(status_code)
        self.message = str(message)


class SchemaAdapter(Protocol):
    def check(self) -> dict: ...

    def convert_payload(self, payload: dict, *, title_link: Optional[str] = None) -> dict: ...

    def default_mapping(self) -> dict[str, str]: ...


class NotionAdapter(Protocol):
    def create_page(self, payload: dict) -> None: ...


class AssetAdapter(Protocol):
    def normalize_ticker(self, value: str) -> str: ...

    def classify(self, ticker: str) -> str: ...

    def sector(self, ticker: str, asset_class: str) -> tuple[Optional[str], Optional[str]]: ...


class RuntimeAssets:
    def normalize_ticker(self, value: str) -> str:
        return normalize_ticker_for_storage(value)

    def classify(self, ticker: str) -> str:
        return classify_asset(ticker)

    def sector(self, ticker: str, asset_class: str) -> tuple[Optional[str], Optional[str]]:
        return get_asset_sector_with_reason(ticker, asset_class=asset_class, timeout_seconds=3.0)


class RequestsNotionAdapter:
    def __init__(self, notion_headers: dict):
        self._notion_headers = dict(notion_headers)

    @staticmethod
    def _message(response) -> str:
        message = ""
        try:
            data = response.json() or {}
            if isinstance(data, dict):
                message = str(data.get("message") or "").strip()
        except Exception:
            pass
        return message or str(response.text or "").strip() or "Unknown validation error"

    def create_page(self, payload: dict) -> None:
        try:
            request_with_retry(
                "POST",
                NOTION_PAGES_URL,
                headers=self._notion_headers,
                json_payload=payload,
                timeout=20,
            )
        except requests.HTTPError as exc:
            if exc.response is None:
                raise
            raise NotionWriteError(exc.response.status_code, self._message(exc.response)) from exc


class CaptureIntake:
    """Validate, normalize, schema-convert and persist one Trading Thesis."""

    def __init__(
        self,
        *,
        database_id: str,
        schema: SchemaAdapter,
        notion: NotionAdapter,
        assets: Optional[AssetAdapter] = None,
        now: Optional[Callable[[], datetime]] = None,
    ):
        self._database_id = str(database_id)
        self._schema = schema
        self._notion = notion
        self._assets = assets or RuntimeAssets()
        self._now = now or (lambda: datetime.now(timezone.utc))

    def capture(self, item: TradingThesisInput) -> dict:
        schema_check = self._schema.check()
        snapshot = schema_check.get("snapshot") or {}
        if not schema_check.get("ok"):
            raise CaptureError(400, str(schema_check.get("message") or "Schema unavailable"))

        internal_payload, retry_context = self._normalize(item, snapshot)
        properties = self._schema.convert_payload(
            internal_payload,
            title_link=internal_payload.get("origin_url"),
        )
        if not properties:
            raise CaptureError(503, "Schema 尚未載入，請稍後再試")

        notion_payload = {
            "parent": {"database_id": self._database_id},
            "properties": properties,
        }
        try:
            self._notion.create_page(notion_payload)
        except NotionWriteError as exc:
            logger.warning(
                "capture write failed: status=%s ticker=%s mindset=%s body=%s",
                exc.status_code,
                retry_context["ticker"],
                item.mindset,
                exc.message[:500],
            )
            if exc.status_code != 400:
                raise
            retry_payload = self._schema_retry_payload(
                notion_payload,
                error_message=exc.message,
                bindings=retry_context["bindings"],
            )
            self._notion.create_page(retry_payload)
        return {"ok": True}

    def _normalize(self, item: TradingThesisInput, snapshot: dict) -> tuple[dict, dict]:
        ticker = self._assets.normalize_ticker(item.ticker)
        asset_class = self._assets.classify(ticker)
        track_mode = _normalize_track_mode(item.track_mode)
        entry_input = parse_optional_float(item.entry)
        order_type = str(item.order_type or "").strip().title()
        if order_type not in {"Market", "Limit"}:
            order_type = "Market" if entry_input is None else "Limit"

        origin_url = _sanitize_url(item.origin_url)
        if item.origin_url and not origin_url:
            raise CaptureError(400, "origin_url 必須以 http/https 開頭")

        custom_date_value = str(item.custom_date or "").strip()
        parsed_custom = _parse_date(custom_date_value)
        if custom_date_value and not parsed_custom:
            raise CaptureError(400, "custom_date 格式錯誤，請使用 ISO 8601")
        if parsed_custom:
            date_iso = parsed_custom.astimezone(timezone.utc).isoformat()
        elif item.date:
            parsed_date = _parse_date(item.date)
            if not parsed_date:
                raise CaptureError(400, "date 格式錯誤，請使用 ISO 8601")
            date_iso = parsed_date.astimezone(timezone.utc).isoformat()
        else:
            date_iso = self._now().astimezone(timezone.utc).isoformat()

        merged_tags: list[str] = []
        seen_tags: set[str] = set()
        for tag in item.tags or []:
            value = str(tag or "").strip()
            if value and value not in seen_tags:
                seen_tags.add(value)
                merged_tags.append(value)

        bindings = (snapshot.get("bindings") or {}) if isinstance(snapshot, dict) else {}
        asset_binding_type = str((bindings.get("asset_class") or {}).get("type") or "").strip().lower()
        sector_binding_type = str((bindings.get("sector") or {}).get("type") or "").strip().lower()

        internal_payload = {
            "title": item.title,
            "ticker": ticker,
            "sentiment": item.sentiment,
            "order_type": order_type,
            "confidence": int(item.confidence),
            "tags": merged_tags,
            "note": item.note,
            "origin_url": origin_url,
            "date": date_iso,
            "timeframe": item.timeframe,
            "track_mode": track_mode,
        }
        if asset_class:
            internal_payload["asset_class"] = [asset_class] if asset_binding_type == "multi_select" else asset_class
        if item.instrument:
            internal_payload["instrument"] = item.instrument
        if item.mindset is not None:
            internal_payload["mindset"] = int(item.mindset)

        if track_mode == "Track2":
            self._add_track2_fields(internal_payload, item)
        else:
            self._add_track1_fields(internal_payload, item, entry_input)

        sector = None
        sector_reason: Optional[str] = None
        if asset_class in {"Taiwan Stock", "US Stock", "Crypto"}:
            try:
                sector, sector_reason = self._assets.sector(ticker, asset_class)
            except SectorCacheError as exc:
                logger.warning("sector cache conflict ticker=%s err=%s", ticker, exc)
                raise CaptureError(500, "快取寫入衝突，請重新按下儲存") from exc
            except Exception as exc:
                logger.warning("sector lookup failed ticker=%s err=%s", ticker, exc)
                sector_reason = "ApiError"
            if not sector:
                reason_label = {
                    "timeout": "Timeout",
                    "notfound": "NotFound",
                    "apierror": "ApiError",
                }.get(str(sector_reason or "").strip().lower(), "NotFound")
                sector = f"子分類失敗:{reason_label}"
        if sector:
            internal_payload["sector"] = [sector] if sector_binding_type == "multi_select" else sector

        return internal_payload, {"ticker": ticker, "bindings": bindings}

    @staticmethod
    def _add_track2_fields(payload: dict, item: TradingThesisInput) -> None:
        bars_limit = int(item.t2_bars_limit or 0)
        if bars_limit <= 0:
            raise CaptureError(400, "Track2 需要有效的限制K線數量 (t2_bars_limit)")
        threshold = parse_optional_float(item.t2_threshold_pct)
        if threshold is None:
            threshold = 0.0
        if threshold < 0:
            raise CaptureError(400, "t2_threshold_pct 不得為負數")
        entry_price = parse_optional_float(item.t2_entry_price)
        if entry_price is None:
            entry_price = parse_optional_float(item.entry)
        entry_time = _parse_date(item.t2_entry_time)
        if not entry_time:
            raise CaptureError(400, "Track2 需要 t2_entry_time")

        payload["t2_bars_limit"] = bars_limit
        payload["t2_threshold_pct"] = float(threshold)
        if entry_price is not None:
            payload["t2_entry_price"] = float(entry_price)
        payload["t2_entry_time"] = entry_time.astimezone(timezone.utc).isoformat()
        payload["t2_result"] = "Pending"

    @staticmethod
    def _add_track1_fields(payload: dict, item: TradingThesisInput, entry: Optional[float]) -> None:
        payload["result_auto"] = "Pending"
        for logical_name, value in (
            ("exposure", parse_optional_float(item.exposure)),
            ("entry", entry),
            ("tp", parse_optional_float(item.tp)),
            ("sl", parse_optional_float(item.sl)),
        ):
            if value is not None:
                payload[logical_name] = value

        tp = payload.get("tp")
        sl = payload.get("sl")
        price_error = _validate_trade_price_logic(item.sentiment, entry, tp, sl)
        if price_error:
            raise CaptureError(400, price_error)
        computed_rr = _compute_rr(entry, tp, sl)
        if computed_rr is not None:
            payload["rr"] = round(computed_rr, 6)
        else:
            supplied_rr = parse_optional_float(item.rr)
            if supplied_rr is not None:
                payload["rr"] = supplied_rr

    def _schema_retry_payload(self, payload: dict, *, error_message: str, bindings: dict) -> dict:
        error_field = _extract_notion_error_field(error_message)
        default_mapping = self._schema.default_mapping()
        removable_groups = []

        def append_group(logical_name: str, binding: dict, display_fallback: str, extras: list[str]) -> None:
            property_name = str((binding or {}).get("name") or "").strip()
            property_id = str((binding or {}).get("id") or "").strip()
            property_key = str(property_name or property_id or default_mapping.get(logical_name, "")).strip()
            removable_groups.append(
                {
                    "property_key": property_key,
                    "display": property_name or display_fallback or logical_name,
                    "candidates": [
                        property_name,
                        property_id,
                        logical_name,
                        logical_name.replace("_", " "),
                        default_mapping.get(logical_name, ""),
                        *extras,
                    ],
                }
            )

        for logical_name, fallback in (("asset_class", "Asset Class"), ("sector", "Sector")):
            binding = bindings.get(logical_name) or {}
            append_group(logical_name, binding, fallback, [logical_name.replace("_", " ")])
        for logical_name, fallback in default_mapping.items():
            if logical_name.startswith("t2_"):
                append_group(logical_name, bindings.get(logical_name) or {}, fallback, [])

        normalized_error_field = _normalized_field(error_field)
        message_lower = error_message.lower()
        matched_groups = []
        for group in removable_groups:
            normalized_candidates = {
                _normalized_field(candidate)
                for candidate in group["candidates"]
                if str(candidate or "").strip()
            }
            if (
                normalized_error_field
                and normalized_error_field in normalized_candidates
            ) or any(candidate and candidate in message_lower for candidate in normalized_candidates):
                matched_groups.append(group)

        if not matched_groups:
            logger.warning("capture validation error: %s", error_message)
            raise CaptureError(400, "資料驗證失敗，請檢查輸入內容")

        retry_payload = copy.deepcopy(payload)
        properties = retry_payload.get("properties")
        if not isinstance(properties, dict):
            properties = {}
            retry_payload["properties"] = properties

        removed_names = []
        for group in matched_groups:
            if group["display"]:
                removed_names.append(group["display"])
            properties.pop(group["property_key"], None)
            for candidate in group["candidates"]:
                if str(candidate or "").strip():
                    properties.pop(str(candidate).strip(), None)

        system_binding = bindings.get("system_msg") or {}
        system_key = str(system_binding.get("name") or system_binding.get("id") or "System_Msg")
        existing = _extract_rich_text_value(properties.get(system_key) or {})
        readable_field = "、".join(dict.fromkeys(removed_names)) or error_field or "asset_class/sector"
        downgrade_message = f"⚠️ 系統降級紀錄：{readable_field} -> {error_message}"
        final_message = f"{existing}\n{downgrade_message}" if existing else downgrade_message
        properties[system_key] = {"rich_text": [{"text": {"content": final_message}}]}
        return retry_payload


def _normalize_track_mode(value: Optional[str]) -> str:
    normalized = str(value or "").strip().lower()
    return "Track2" if normalized in {"track2", "t2", "sense", "perception"} else "Track1"


def _parse_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _sanitize_url(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    return normalized if urlparse(normalized).scheme in {"http", "https"} else None


def _compute_rr(entry: Optional[float], tp: Optional[float], sl: Optional[float]) -> Optional[float]:
    if entry is None or tp is None or sl is None:
        return None
    risk = abs(entry - sl)
    return None if risk == 0 else abs(tp - entry) / risk


def _validate_trade_price_logic(
    sentiment: str,
    entry: Optional[float],
    tp: Optional[float],
    sl: Optional[float],
) -> Optional[str]:
    if sentiment not in {"Bullish", "Bearish"}:
        return None
    if tp is not None and sl is not None:
        if sentiment == "Bullish" and not tp > sl:
            return "⚠️ [錯誤] 看漲單：止盈必須大於止損"
        if sentiment == "Bearish" and not tp < sl:
            return "⚠️ [錯誤] 看跌單：止盈必須小於止損"
    if entry is not None and tp is not None:
        if sentiment == "Bullish" and not tp > entry:
            return "⚠️ [錯誤] 看漲單：止盈必須大於進場"
        if sentiment == "Bearish" and not tp < entry:
            return "⚠️ [錯誤] 看跌單：止盈必須小於進場"
    if entry is not None and sl is not None:
        if sentiment == "Bullish" and not entry > sl:
            return "⚠️ [錯誤] 看漲單：止損必須小於進場"
        if sentiment == "Bearish" and not entry < sl:
            return "⚠️ [錯誤] 看跌單：止損必須大於進場"
    return None


def _extract_notion_error_field(message: str) -> str:
    for pattern in (r'body\.properties\["([^"]+)"\]', r"body\.properties\.([^\.\[\n]+)"):
        match = re.search(pattern, str(message or ""))
        if match:
            return str(match.group(1) or "").strip().strip("'\"")
    return ""


def _extract_rich_text_value(property_value: dict) -> str:
    rich_text = property_value.get("rich_text") if isinstance(property_value, dict) else None
    if not isinstance(rich_text, list):
        return ""
    chunks = []
    for item in rich_text:
        if not isinstance(item, dict):
            continue
        text = item.get("text") or {}
        if isinstance(text, dict) and text.get("content") is not None:
            chunks.append(str(text["content"]))
        elif item.get("plain_text") is not None:
            chunks.append(str(item["plain_text"]))
    return "".join(chunks).strip()


def _normalized_field(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).lower()
