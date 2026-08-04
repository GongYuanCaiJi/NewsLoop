from __future__ import annotations

import atexit
import json
import logging
import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from pathlib import Path
from typing import Optional

import requests

try:
    from .io_utils import save_json_atomic
except Exception:
    from io_utils import save_json_atomic

# Force yfinance to use requests backend instead of curl_cffi to avoid
# macOS dynamic-library policy issues when loading curl_cffi dylibs.
os.environ.setdefault("YF_USE_CURL", "0")

BINANCE_EXCHANGE_INFO_URL = "https://api.binance.com/api/v3/exchangeInfo"
BINANCE_SYMBOLS_REFRESH_SECONDS = 300
BINANCE_SYMBOLS_FAILURE_COOLDOWN_SECONDS = 15

_BINANCE_USDT_SYMBOLS: Optional[set[str]] = None
_BINANCE_LAST_ATTEMPT = 0.0
_BINANCE_LAST_SUCCESS = 0.0
_BINANCE_LOADING = False
_BINANCE_CACHE_LOCK = threading.Lock()

SECTOR_CACHE_PATH = Path(__file__).resolve().parents[1] / ".streamlit" / "sector_cache.json"
SECTOR_CACHE_LOCK_PATH = SECTOR_CACHE_PATH.with_suffix(SECTOR_CACHE_PATH.suffix + ".lock")
SECTOR_CACHE_TTL_SECONDS = int(os.getenv("SECTOR_CACHE_TTL_SECONDS", "86400") or 86400)
SECTOR_ERROR_TTL_SECONDS = int(os.getenv("SECTOR_ERROR_TTL_SECONDS", "60") or 60)
SECTOR_RATE_LIMIT_TTL_SECONDS = int(os.getenv("SECTOR_RATE_LIMIT_TTL_SECONDS", "300") or 300)
SECTOR_FETCH_TIMEOUT_SECONDS = float(os.getenv("SECTOR_FETCH_TIMEOUT_SECONDS", "3") or 3)

_SECTOR_CACHE_LOCK = threading.RLock()
_SECTOR_CACHE_LOADED = False
_SECTOR_CACHE: dict[str, dict] = {}
_SECTOR_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="sector-fetch")
_SECTOR_INFLIGHT: dict[str, Future] = {}
_SECTOR_RATE_LIMIT_UNTIL = 0.0

def _shutdown_sector_executor() -> None:
    try:
        _SECTOR_EXECUTOR.shutdown(wait=False, cancel_futures=True)
    except TypeError:
        _SECTOR_EXECUTOR.shutdown(wait=False)
    except Exception:
        pass

atexit.register(_shutdown_sector_executor)

COMMON_CRYPTO_BASES = {
    "BTC",
    "ETH",
    "BNB",
    "SOL",
    "XRP",
    "ADA",
    "DOGE",
    "LTC",
    "TRX",
    "DOT",
    "AVAX",
    "LINK",
    "MATIC",
    "NEAR",
    "BCH",
    "ETC",
    "ATOM",
    "FIL",
    "SUI",
    "TON",
    "SEI",
    "INJ",
    "ARB",
    "OP",
    "APT",
    "XLM",
    "XMR",
    "XTZ",
    "UNI",
}


class SectorCacheError(RuntimeError):
    pass


def load_binance_usdt_symbols() -> Optional[set[str]]:
    try:
        resp = requests.get(BINANCE_EXCHANGE_INFO_URL, timeout=(1.5, 2.5))
        resp.raise_for_status()
        data = resp.json() or {}
        symbols = data.get("symbols") or []
        return {
            symbol
            for item in symbols
            if isinstance(item, dict)
            for symbol in [str(item.get("symbol") or "").upper()]
            if symbol.endswith("USDT")
        }
    except Exception:
        return None


def get_binance_usdt_symbols(force: bool = False) -> set[str]:
    global _BINANCE_USDT_SYMBOLS, _BINANCE_LAST_ATTEMPT, _BINANCE_LAST_SUCCESS, _BINANCE_LOADING
    now = time.time()
    with _BINANCE_CACHE_LOCK:
        if force:
            _BINANCE_USDT_SYMBOLS = None
            _BINANCE_LAST_SUCCESS = 0.0
            _BINANCE_LAST_ATTEMPT = 0.0
            _BINANCE_LOADING = False
        if _BINANCE_USDT_SYMBOLS is not None and (now - _BINANCE_LAST_SUCCESS) < BINANCE_SYMBOLS_REFRESH_SECONDS:
            return set(_BINANCE_USDT_SYMBOLS)
        # Retry failed loads quickly, but do not hammer Binance.
        if (now - _BINANCE_LAST_ATTEMPT) < BINANCE_SYMBOLS_FAILURE_COOLDOWN_SECONDS:
            return set(_BINANCE_USDT_SYMBOLS or set())
        if _BINANCE_LOADING:
            return set(_BINANCE_USDT_SYMBOLS or set())
        _BINANCE_LOADING = True
        _BINANCE_LAST_ATTEMPT = now
    loaded = load_binance_usdt_symbols()
    with _BINANCE_CACHE_LOCK:
        _BINANCE_LOADING = False
        if loaded is not None:
            _BINANCE_USDT_SYMBOLS = loaded
            _BINANCE_LAST_SUCCESS = time.time()
            return set(_BINANCE_USDT_SYMBOLS)
        # Keep symbol cache unset on API failure so the next retry can recover quickly.
        return set(_BINANCE_USDT_SYMBOLS or set())


def normalize_ticker_key(value: str) -> str:
    if value is None:
        return ""
    s = str(value).strip().upper().replace(" ", "")
    if s.endswith(".TWO"):
        base = s[:-4]
        if base.isdigit() and len(base) >= 4:
            s = base
    elif s.endswith(".TW"):
        base = s[:-3]
        if base.isdigit() and len(base) >= 4:
            s = base
    if s.endswith("-USDT") or s.endswith("_USDT") or s.endswith("/USDT"):
        s = s[:-5] + "USDT"
    return s


def _read_sector_cache_from_disk_unlocked() -> dict[str, dict]:
    if not SECTOR_CACHE_PATH.exists():
        return {}
    try:
        raw = SECTOR_CACHE_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        logging.warning("sector cache read failed path=%s err=%s", SECTOR_CACHE_PATH, exc)
        return {}
    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        backup_path = SECTOR_CACHE_PATH.with_name(
            f"{SECTOR_CACHE_PATH.stem}.corrupt.{int(time.time())}{SECTOR_CACHE_PATH.suffix}"
        )
        try:
            SECTOR_CACHE_PATH.replace(backup_path)
        except OSError:
            backup_path = None
        logging.warning(
            "sector cache decode failed path=%s backup=%s err=%s",
            SECTOR_CACHE_PATH,
            backup_path,
            exc,
        )
        return {}
    if not isinstance(data, dict):
        logging.warning("sector cache format invalid path=%s", SECTOR_CACHE_PATH)
        return {}
    return data


def _write_sector_cache_to_disk_unlocked(cache_data: dict[str, dict]) -> None:
    try:
        save_json_atomic(
            SECTOR_CACHE_PATH,
            cache_data,
            lock_path=SECTOR_CACHE_LOCK_PATH,
            timeout=5.0,
        )
    except Exception as exc:
        raise SectorCacheError(f"sector cache write failed: {exc}") from exc


def _replace_memory_sector_cache(cache_data: dict[str, dict]) -> None:
    global _SECTOR_CACHE_LOADED, _SECTOR_CACHE
    with _SECTOR_CACHE_LOCK:
        _SECTOR_CACHE = dict(cache_data)
        _SECTOR_CACHE_LOADED = True


def _load_sector_cache_once_locked() -> None:
    global _SECTOR_CACHE_LOADED, _SECTOR_CACHE
    if _SECTOR_CACHE_LOADED:
        return
    disk_cache = _read_sector_cache_from_disk_unlocked()
    _SECTOR_CACHE = disk_cache
    _SECTOR_CACHE_LOADED = True


def _build_sector_candidates(ticker: str, asset_class: str) -> list[str]:
    raw = str(ticker or "").strip().upper()
    norm = normalize_ticker_key(raw)
    candidates: list[str] = []
    if asset_class == "Taiwan Stock":
        if raw.endswith(".TW") or raw.endswith(".TWO"):
            candidates.extend([raw])
            if raw.endswith(".TW"):
                candidates.append(raw[:-3] + ".TWO")
            elif raw.endswith(".TWO"):
                candidates.append(raw[:-4] + ".TW")
        elif norm.isdigit() and len(norm) == 4:
            candidates.extend([f"{norm}.TW", f"{norm}.TWO"])
        else:
            candidates.append(raw or norm)
    else:
        candidates.append(raw or norm)

    seen: set[str] = set()
    out: list[str] = []
    for sym in candidates:
        s = str(sym or "").strip().upper()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _fetch_sector_from_yfinance(symbol: str) -> Optional[str]:
    try:
        import yfinance as yf  # lazy import
    except Exception as exc:
        raise RuntimeError(f"yfinance unavailable: {exc}") from exc

    ticker_obj = yf.Ticker(symbol)
    info = {}
    if hasattr(ticker_obj, "get_info"):
        info = ticker_obj.get_info() or {}
    else:
        info = ticker_obj.info or {}
    if not isinstance(info, dict):
        return None

    sector = str(info.get("sector") or "").strip()
    if sector:
        return sector
    industry = str(info.get("industry") or "").strip()
    return industry or None


def _get_cached_sector(symbol: str) -> tuple[bool, Optional[str], str]:
    now = time.time()
    cache_key = symbol.upper()

    with _SECTOR_CACHE_LOCK:
        _load_sector_cache_once_locked()
        entry = _SECTOR_CACHE.get(cache_key)
    if isinstance(entry, dict):
        status = str(entry.get("status") or "").strip().lower()
        sector = entry.get("sector")
        fetched_at = float(entry.get("fetched_at") or 0.0)
        if status == "rate_limited":
            ttl = SECTOR_RATE_LIMIT_TTL_SECONDS
        elif status in {"error", "timeout"}:
            ttl = SECTOR_ERROR_TTL_SECONDS
        else:
            ttl = SECTOR_CACHE_TTL_SECONDS
        if fetched_at > 0 and (now - fetched_at) < ttl:
            if status == "ok":
                return True, str(sector or ""), "ok"
            if status in {"not_found", "error", "timeout", "rate_limited"}:
                return True, None, status

    return False, None, ""


def _set_cached_sector(symbol: str, status: str, sector: Optional[str]) -> None:
    cache_key = symbol.upper()
    payload = {
        "status": status,
        "sector": str(sector).strip() if sector else None,
        "fetched_at": time.time(),
    }
    had_prev = False
    prev_payload: Optional[dict] = None
    with _SECTOR_CACHE_LOCK:
        _load_sector_cache_once_locked()
        existing = _SECTOR_CACHE.get(cache_key)
        if isinstance(existing, dict):
            existing_status = str(existing.get("status") or "").strip().lower()
            # Never downgrade a known-good value due to transient fetch errors.
            if existing_status == "ok" and payload["status"] != "ok":
                return
            had_prev = True
            prev_payload = dict(existing)
        _SECTOR_CACHE[cache_key] = payload
        snapshot = dict(_SECTOR_CACHE)
    try:
        _write_sector_cache_to_disk_unlocked(snapshot)
    except SectorCacheError:
        with _SECTOR_CACHE_LOCK:
            if had_prev and prev_payload is not None:
                _SECTOR_CACHE[cache_key] = prev_payload
            else:
                _SECTOR_CACHE.pop(cache_key, None)
        raise


def _extract_retry_after_seconds(exc: Exception) -> float:
    response = getattr(exc, "response", None)
    if response is not None:
        retry_after = (response.headers or {}).get("Retry-After")
        try:
            return max(0.0, float(retry_after))
        except (TypeError, ValueError):
            pass
    return float(SECTOR_RATE_LIMIT_TTL_SECONDS)


def _exception_is_rate_limited(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "status_code", None) == 429:
        return True
    text = str(exc or "")
    return ("429" in text) or ("Too Many Requests" in text)


def _enter_sector_rate_limit_cooldown(seconds: float, *, symbol: str) -> None:
    global _SECTOR_RATE_LIMIT_UNTIL
    cooldown = max(1.0, float(seconds or SECTOR_RATE_LIMIT_TTL_SECONDS))
    now = time.time()
    until = now + cooldown
    should_log = False
    with _SECTOR_CACHE_LOCK:
        if until > _SECTOR_RATE_LIMIT_UNTIL:
            _SECTOR_RATE_LIMIT_UNTIL = until
            should_log = True
    if should_log:
        logging.warning("sector lookup rate-limited symbol=%s cooldown=%.1fs", symbol, cooldown)


def _sector_rate_limit_remaining_seconds() -> float:
    with _SECTOR_CACHE_LOCK:
        remaining = _SECTOR_RATE_LIMIT_UNTIL - time.time()
    return max(0.0, remaining)


def get_asset_sector_with_reason(
    ticker: str,
    asset_class: Optional[str] = None,
    timeout_seconds: Optional[float] = None,
) -> tuple[Optional[str], Optional[str]]:
    resolved_asset = str(asset_class or classify_asset(ticker))
    if resolved_asset not in {"Taiwan Stock", "US Stock", "Crypto"}:
        return None, "NotFound"

    timeout = SECTOR_FETCH_TIMEOUT_SECONDS if timeout_seconds is None else max(0.5, float(timeout_seconds))
    candidates = _build_sector_candidates(ticker, resolved_asset)
    if not candidates:
        return None, "NotFound"

    last_reason: Optional[str] = None

    for symbol in candidates:
        hit, value, status = _get_cached_sector(symbol)
        if hit:
            if value:
                return value, None
            if status == "timeout":
                last_reason = "Timeout"
            elif status == "rate_limited":
                last_reason = "ApiError"
            elif status == "error":
                last_reason = "ApiError"
            elif status == "not_found":
                last_reason = "NotFound"
            continue

        if _sector_rate_limit_remaining_seconds() > 0:
            last_reason = "ApiError"
            continue

        future = None
        with _SECTOR_CACHE_LOCK:
            future = _SECTOR_INFLIGHT.get(symbol)
            if future is None:
                future = _SECTOR_EXECUTOR.submit(_fetch_sector_from_yfinance, symbol)
                _SECTOR_INFLIGHT[symbol] = future
        try:
            sector = future.result(timeout=timeout)
        except FuturesTimeoutError:
            _set_cached_sector(symbol, "timeout", None)
            last_reason = "Timeout"
            continue
        except Exception as exc:
            if _exception_is_rate_limited(exc):
                _enter_sector_rate_limit_cooldown(_extract_retry_after_seconds(exc), symbol=symbol)
                _set_cached_sector(symbol, "rate_limited", None)
                last_reason = "ApiError"
                continue
            _set_cached_sector(symbol, "error", None)
            last_reason = "ApiError"
            continue
        finally:
            if future is not None:
                with _SECTOR_CACHE_LOCK:
                    if _SECTOR_INFLIGHT.get(symbol) is future:
                        _SECTOR_INFLIGHT.pop(symbol, None)

        if sector:
            _set_cached_sector(symbol, "ok", sector)
            return sector, None
        _set_cached_sector(symbol, "not_found", None)
        last_reason = "NotFound"

    return None, (last_reason or "NotFound")


def get_asset_sector(ticker: str, asset_class: Optional[str] = None, timeout_seconds: Optional[float] = None) -> Optional[str]:
    sector, _ = get_asset_sector_with_reason(ticker, asset_class=asset_class, timeout_seconds=timeout_seconds)
    return sector


def normalize_symbol(ticker: str) -> str:
    return str(ticker or "").strip().upper()


def normalize_ticker_for_storage(raw_ticker: str) -> str:
    if not raw_ticker:
        return raw_ticker
    raw_upper = str(raw_ticker).strip().upper()
    symbols = get_binance_usdt_symbols()

    # Keep common crypto bridge inputs stable:
    # BTC-USD / BTC_USD / BTC/USD -> BTCUSDT
    if raw_upper.endswith(("-USD", "_USD", "/USD")):
        base = raw_upper[:-4].replace("-", "").replace("_", "").replace("/", "")
        if f"{base}USDT" in symbols:
            return f"{base}USDT"
        if base in COMMON_CRYPTO_BASES and not symbols:
            return f"{base}USDT"

    trimmed = (
        str(raw_ticker)
        .strip()
        .replace(" ", "")
        .replace("-", "")
        .replace("_", "")
        .replace("/", "")
    )
    if len(trimmed) > 30:
        return str(raw_ticker).strip()
    upper = trimmed.upper()
    if upper in symbols:
        return upper
    if f"{upper}USDT" in symbols:
        return f"{upper}USDT"
    if upper.endswith("USD") and f"{upper}T" in symbols:
        return f"{upper}T"
    # Degrade gracefully when Binance symbol list is temporarily unavailable.
    if not symbols and upper in COMMON_CRYPTO_BASES:
        return f"{upper}USDT"
    if not symbols and upper.endswith("USD"):
        base = upper[:-3]
        if base in COMMON_CRYPTO_BASES:
            return f"{base}USDT"
    return upper


def classify_asset(ticker: str) -> str:
    if not ticker:
        return "Other"
    raw = str(ticker).strip()
    # Treat obviously non-standard long/symbol-heavy tickers as Meme, but avoid
    # classifying long plain alphanumeric symbols purely by length.
    if len(raw) > 30 and (not raw.isalnum()):
        return "Meme"
    upper = raw.upper()
    norm = normalize_ticker_key(raw)
    fiat_codes = {
        "USD", "EUR", "JPY", "GBP", "AUD", "CAD", "CHF", "NZD", "CNY", "HKD", "SGD",
        "SEK", "NOK", "DKK", "ZAR", "MXN", "TRY", "PLN", "HUF", "CZK", "THB",
    }
    crypto_codes = set(COMMON_CRYPTO_BASES)

    if ".TW" in upper or ".TWO" in upper or (upper.isdigit() and len(upper) == 4):
        return "Taiwan Stock"

    symbols = get_binance_usdt_symbols()
    usd_bridge_is_crypto = False
    if norm.endswith("USD") and len(norm) >= 6:
        base = norm[:-3]
        if base and base not in fiat_codes and f"{norm}T" in symbols:
            usd_bridge_is_crypto = True
    if (
        norm in symbols
        or norm.endswith("USDT")
        or upper.endswith("-USD")
        or upper.endswith("_USD")
        or upper.endswith("/USD")
        or usd_bridge_is_crypto
    ):
        return "Crypto"

    # Binance symbols may be temporarily unavailable; treat common bare crypto symbols
    # (e.g. BTC / ETH / XRP) as crypto instead of falling back to US Stock.
    if norm.isalpha() and 2 <= len(norm) <= 10 and norm in crypto_codes:
        return "Crypto"
    if not symbols and norm.endswith("USDT"):
        base = norm[:-4]
        if (base in crypto_codes) or (base.isalpha() and 2 <= len(base) <= 10):
            return "Crypto"

    commodity_spot_pairs = {"XAUUSD", "XAGUSD", "XPTUSD", "XPDUSD"}
    if upper.endswith("=F") or norm in commodity_spot_pairs:
        return "Commodity"

    if norm.isalpha() and len(norm) == 6:
        base, quote = norm[:3], norm[3:]
        if base in crypto_codes or quote in crypto_codes:
            return "Crypto"
        if base in fiat_codes and quote in fiat_codes:
            return "Forex"
        return "Other"

    if norm.isalpha() and 1 <= len(norm) <= 5:
        return "US Stock"

    return "Other"


def yahoo_crypto_symbol(ticker: str) -> str:
    raw = str(ticker or "").strip().upper()
    if raw.endswith("-USD"):
        return raw
    s = normalize_ticker_key(ticker)
    if s.endswith("USDT"):
        return f"{s[:-4]}-USD"
    if s.endswith("USD"):
        return f"{s[:-3]}-USD"
    return f"{s}-USD" if s else str(ticker or "")
