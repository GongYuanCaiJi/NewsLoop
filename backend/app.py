import os
import hmac
import logging
import json
import re
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import Annotated, Callable, Optional

import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

try:
    from .capture_intake import (
        CaptureError,
        CaptureIntake,
        RequestsNotionAdapter,
        TradingThesisInput,
    )
    from .schema_manager import SchemaManager
    from .io_utils import load_json_file, parse_optional_float, request_with_retry
    from .ticker_utils import get_binance_usdt_symbols as shared_get_binance_usdt_symbols
except Exception:
    from capture_intake import (
        CaptureError,
        CaptureIntake,
        RequestsNotionAdapter,
        TradingThesisInput,
    )
    from schema_manager import SchemaManager
    from io_utils import load_json_file, parse_optional_float, request_with_retry
    from ticker_utils import get_binance_usdt_symbols as shared_get_binance_usdt_symbols

load_dotenv()

NOTION_VERSION = os.getenv("NOTION_VERSION", "2022-06-28")


def _build_notion_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }

def _load_env_credentials() -> tuple[str, str]:
    token = (os.getenv("NOTION_TOKEN") or "").strip()
    db_id = (os.getenv("NEWS_ALPHA_DB_ID") or "").strip()
    return token, db_id


_refresh_lock = threading.RLock()
with _refresh_lock:
    NOTION_TOKEN, DB_ID = _load_env_credentials()
    NOTION_HEADERS = _build_notion_headers(NOTION_TOKEN)

LOCAL_CFG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".streamlit", "dashboard_config.json")
SCHEMA_CACHE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".streamlit", "schema_cache.json")
SCHEMA_TTL_SECONDS = int(os.getenv("NOTION_SCHEMA_TTL_SECONDS", "900") or 900)
DB_PROPS_CACHE_TTL_SECONDS = int(os.getenv("NOTION_DB_PROPS_CACHE_TTL_SECONDS", "60") or 60)
API_KEY_HEADER = "x-api-key"
UUID_RE = re.compile(r"^[0-9a-f-]{32,36}$", re.IGNORECASE)
RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("NEWS_ALPHA_RATE_LIMIT_WINDOW", "60") or 60)
RATE_LIMIT_MAX_REQUESTS = int(os.getenv("NEWS_ALPHA_RATE_LIMIT_MAX", "120") or 120)
ALLOWED_ORIGINS_CACHE_TTL_SECONDS = int(os.getenv("NEWS_ALPHA_ALLOWED_ORIGINS_TTL", "30") or 30)
MAX_BODY_BYTES = int(os.getenv("NEWS_ALPHA_MAX_BODY_BYTES", "1048576") or 1048576)
DEFAULT_EXTENSION_ID = "chjpkcalbfppibeffgnjhajnobigbnfb"


def _parse_id_list(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value)
    if not text.strip():
        return []
    parts = [p.strip() for p in text.replace(";", ",").split(",")]
    return [p for p in parts if p]


def _env_true(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _load_extension_ids_from_cfg(cfg: dict) -> list[str]:
    ids = [DEFAULT_EXTENSION_ID]
    if isinstance(cfg, dict):
        ids.extend(_parse_id_list(cfg.get("CHROME_EXTENSION_IDS")))
        ids.extend(_parse_id_list(cfg.get("CHROME_EXTENSION_ID")))
        ids.extend(_parse_id_list(cfg.get("NEWS_ALPHA_EXTENSION_IDS")))
        ids.extend(_parse_id_list(cfg.get("NEWS_ALPHA_EXTENSION_ID")))
    ids.extend(_parse_id_list(os.getenv("NEWS_ALPHA_EXTENSION_IDS")))
    ids.extend(_parse_id_list(os.getenv("NEWS_ALPHA_EXTENSION_ID")))
    # Remove duplicates while preserving order.
    return list(dict.fromkeys([i for i in ids if i]))


def _load_security_config() -> tuple[str, list[str]]:
    cfg = load_json_file(LOCAL_CFG_PATH, {})
    if not isinstance(cfg, dict):
        cfg = {}
    api_key = str(
        cfg.get("API_KEY")
        or cfg.get("NEWS_ALPHA_API_KEY")
        or os.getenv("NEWS_ALPHA_API_KEY")
        or ""
    ).strip()
    extension_ids = _load_extension_ids_from_cfg(cfg)
    return api_key, extension_ids


def _get_security_state(force: bool = False) -> tuple[str, list[str]]:
    global _allowed_origin_cache_ts
    now = time.time()
    with _allowed_origin_cache_lock:
        if (not force) and (now - _allowed_origin_cache_ts) < ALLOWED_ORIGINS_CACHE_TTL_SECONDS:
            return str(_security_cache.get("api_key") or ""), list(_allowed_origin_cache)
    api_key, ids = _load_security_config()
    origins = [f"chrome-extension://{ext_id}" for ext_id in ids]
    with _allowed_origin_cache_lock:
        _allowed_origin_cache.clear()
        _allowed_origin_cache.extend(origins)
        _security_cache["api_key"] = api_key
        _allowed_origin_cache_ts = now
    return api_key, list(origins)


def _get_api_key_dynamic(force: bool = False) -> str:
    api_key, _ = _get_security_state(force=force)
    return api_key


def _get_allowed_extension_origins_dynamic() -> list[str]:
    _, origins = _get_security_state(force=False)
    return origins


def _check_rate_limit(client_key: str) -> bool:
    if RATE_LIMIT_MAX_REQUESTS <= 0 or RATE_LIMIT_WINDOW_SECONDS <= 0:
        return True
    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW_SECONDS
    with _rate_limit_lock:
        global _rate_limit_last_cleanup
        if (now - _rate_limit_last_cleanup) > max(30.0, float(RATE_LIMIT_WINDOW_SECONDS)):
            for key, stamps in list(_rate_limit_hits.items()):
                while stamps and stamps[0] < window_start:
                    stamps.popleft()
                if not stamps:
                    _rate_limit_hits.pop(key, None)
            _rate_limit_last_cleanup = now
        hits = _rate_limit_hits.get(client_key)
        if hits is None:
            hits = deque()
            _rate_limit_hits[client_key] = hits
        while hits and hits[0] < window_start:
            hits.popleft()
        if len(hits) >= RATE_LIMIT_MAX_REQUESTS:
            return False
        hits.append(now)
    return True


class DeleteTagItem(BaseModel):
    name: str


class UpdateExposureItem(BaseModel):
    page_id: str
    exposure: float


class ResyncRequest(BaseModel):
    force: bool = True


class SchemaOverrideItem(BaseModel):
    internal_key: str
    property_id: str = ""


class MaxBodySizeMiddleware:
    def __init__(self, app, max_body_bytes: int):
        self.app = app
        self.max_body_bytes = max(0, int(max_body_bytes or 0))

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or self.max_body_bytes <= 0:
            await self.app(scope, receive, send)
            return
        method = str(scope.get("method") or "").upper()
        if method not in {"POST", "PUT", "PATCH"}:
            await self.app(scope, receive, send)
            return
        headers = {
            str(k, "latin1").lower(): str(v, "latin1")
            for k, v in (scope.get("headers") or [])
        }
        content_len = headers.get("content-length")
        if content_len:
            try:
                if int(content_len) > self.max_body_bytes:
                    response = JSONResponse(status_code=413, content={"detail": "Payload Too Large"})
                    await response(scope, receive, send)
                    return
            except ValueError:
                pass

        consumed = 0
        buffered_messages: list[dict] = []
        while True:
            message = await receive()
            if message.get("type") != "http.request":
                buffered_messages.append(message)
                break
            body = message.get("body", b"")
            consumed += len(body)
            if consumed > self.max_body_bytes:
                response = JSONResponse(status_code=413, content={"detail": "Payload Too Large"})
                await response(scope, receive, send)
                return
            buffered_messages.append(message)
            if not message.get("more_body", False):
                break

        replay_index = 0

        async def replay_receive():
            nonlocal replay_index
            if replay_index < len(buffered_messages):
                message = buffered_messages[replay_index]
                replay_index += 1
                return message
            return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay_receive, send)


logger = logging.getLogger("newsloop.api")
API_KEY, ALLOWED_EXTENSION_IDS = _load_security_config()
ALLOWED_EXTENSION_ORIGINS = [f"chrome-extension://{ext_id}" for ext_id in ALLOWED_EXTENSION_IDS]
if not ALLOWED_EXTENSION_ORIGINS:
    logger.warning("CORS: no Chrome extension ID configured; chrome-extension origins will be blocked.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        cfg = load_local_cfg() or {}
        cfg_token = str(cfg.get("NOTION_TOKEN") or "").strip() if isinstance(cfg, dict) else ""
        cfg_db_id = str(cfg.get("DB_ID") or cfg.get("NEWS_ALPHA_DB_ID") or "").strip() if isinstance(cfg, dict) else ""
        effective_token = cfg_token or (os.getenv("NOTION_TOKEN") or "").strip()
        effective_db_id = cfg_db_id or (os.getenv("NEWS_ALPHA_DB_ID") or "").strip()
        if not effective_token or not effective_db_id:
            raise RuntimeError("NOTION_TOKEN/DB_ID 未設定")
        _, _, mgr = _get_runtime_state()
        mgr.load_cache()
        mgr.sync_with_notion(force=False)
        try:
            t = threading.Thread(target=shared_get_binance_usdt_symbols, kwargs={"force": False}, daemon=True)
            t.start()
        except Exception:
            pass
    except RuntimeError as exc:
        logger.warning("schema warmup skipped: %s", exc)
    except Exception as exc:
        logger.warning("schema warmup failed: %s", exc)
    yield


app = FastAPI(title="NewsLoop API", lifespan=lifespan)
schema_manager: Optional[SchemaManager] = None
_rate_limit_lock = threading.Lock()
_rate_limit_hits: dict[str, deque[float]] = {}
_rate_limit_last_cleanup = 0.0
_allowed_origin_cache: list[str] = []
_allowed_origin_cache_ts = 0.0
_allowed_origin_cache_lock = threading.Lock()
_security_cache = {"api_key": API_KEY}

app.add_middleware(MaxBodySizeMiddleware, max_body_bytes=MAX_BODY_BYTES)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8501",
        "https://localhost:8501",
        "http://127.0.0.1:8501",
        "https://127.0.0.1:8501",
        "http://localhost:8000",
        "https://localhost:8000",
        "http://127.0.0.1:8000",
        "https://127.0.0.1:8000",
        *ALLOWED_EXTENSION_ORIGINS,
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", API_KEY_HEADER],
)


def _is_loopback_host(host: Optional[str]) -> bool:
    return host in {"127.0.0.1", "::1", "localhost"}


@app.middleware("http")
async def enforce_api_security(request: Request, call_next):
    origin = (request.headers.get("origin") or "").strip()
    if origin.startswith("chrome-extension://"):
        allowed_origins = _get_allowed_extension_origins_dynamic()
        if origin not in allowed_origins:
            return JSONResponse(status_code=403, content={"detail": "Forbidden"})
    # Allow CORS preflight without auth checks beyond origin validation.
    if request.method == "OPTIONS":
        return await call_next(request)
    # Health checks should stay simple for local start scripts.
    if request.url.path == "/health":
        return await call_next(request)

    current_api_key = _get_api_key_dynamic()
    if current_api_key:
        provided = (request.headers.get(API_KEY_HEADER) or "").strip()
        if not hmac.compare_digest(provided, current_api_key):
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    else:
        client_host = request.client.host if request.client else ""
        if not _is_loopback_host(client_host):
            return JSONResponse(status_code=403, content={"detail": "Forbidden"})
    client_host = request.client.host if request.client else ""
    if not _check_rate_limit(client_host or "unknown"):
        return JSONResponse(status_code=429, content={"detail": "Too Many Requests"})
    return await call_next(request)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault("Pragma", "no-cache")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(request: Request, exc: RequestValidationError):
    fields: list[str] = []
    for err in exc.errors():
        loc = [str(part) for part in (err.get("loc") or []) if str(part) not in {"body", "query", "path"}]
        if loc:
            fields.append(".".join(loc))
    if fields:
        deduped = list(dict.fromkeys(fields))
        detail = f"資料驗證失敗：{', '.join(deduped)}"
    else:
        detail = "資料驗證失敗"
    return JSONResponse(status_code=422, content={"detail": detail})


def load_local_cfg() -> dict:
    cfg = load_json_file(LOCAL_CFG_PATH, {})
    return cfg if isinstance(cfg, dict) else {}


def _refresh_runtime_clients_if_needed():
    global NOTION_TOKEN, DB_ID, NOTION_HEADERS, schema_manager
    with _refresh_lock:
        cfg = load_local_cfg()
        cfg_token = str(cfg.get("NOTION_TOKEN") or "").strip() if isinstance(cfg, dict) else ""
        cfg_db_id = str(cfg.get("DB_ID") or cfg.get("NEWS_ALPHA_DB_ID") or "").strip() if isinstance(cfg, dict) else ""

        effective_token = cfg_token or (os.getenv("NOTION_TOKEN") or "").strip()
        effective_db_id = cfg_db_id or (os.getenv("NEWS_ALPHA_DB_ID") or "").strip()
        if not effective_token or not effective_db_id:
            raise RuntimeError("Missing NOTION_TOKEN or NEWS_ALPHA_DB_ID (env/config)")

        changed = (effective_token != NOTION_TOKEN) or (effective_db_id != DB_ID) or (schema_manager is None)
        if not changed:
            return

        old_manager = schema_manager
        new_notion_headers = _build_notion_headers(effective_token)
        new_manager = SchemaManager(
            db_id=effective_db_id,
            notion_headers=new_notion_headers,
            cache_path=SCHEMA_CACHE_PATH,
            config_path=LOCAL_CFG_PATH,
            ttl_seconds=SCHEMA_TTL_SECONDS,
            database_ttl_seconds=DB_PROPS_CACHE_TTL_SECONDS,
        )
        try:
            new_manager.load_cache()
        except Exception as exc:
            logger.warning("schema cache reload failed after config change: %s", exc)
        NOTION_TOKEN = effective_token
        DB_ID = effective_db_id
        NOTION_HEADERS = new_notion_headers
        schema_manager = new_manager
        if old_manager is not None and old_manager is not new_manager:
            try:
                old_manager.deactivate()
            except Exception:
                pass

def get_schema_snapshot(force: bool = False) -> dict:
    # SchemaManager already uses SWR and local cache.
    _, _, mgr = _get_runtime_state()
    return mgr.sync_with_notion(force=bool(force))


def _get_runtime_state():
    _refresh_runtime_clients_if_needed()
    with _refresh_lock:
        return DB_ID, dict(NOTION_HEADERS), schema_manager


def _build_capture_intake() -> CaptureIntake:
    db_id, notion_headers, manager = _get_runtime_state()
    return CaptureIntake(
        database_id=db_id,
        schema=manager,
        notion=RequestsNotionAdapter(notion_headers),
    )


def get_capture_intake_factory() -> Callable[[], CaptureIntake]:
    return _build_capture_intake


@app.get("/tags")
def get_tags():
    try:
        _, _, mgr = _get_runtime_state()
        bound_property = mgr.get_bound_property("tags")
        if not bound_property:
            return {"tags": []}
        tags_prop = bound_property["schema"]
        options = tags_prop["multi_select"].get("options", [])
        tags = [opt.get("name") for opt in options if opt.get("name")]
        return {"tags": tags}
    except Exception as exc:
        logger.exception("get_tags failed: %s", exc)
        raise HTTPException(status_code=500, detail="內部錯誤，請稍後再試")


@app.post("/tags/delete")
def delete_tag(item: DeleteTagItem):
    try:
        db_id, notion_headers, mgr = _get_runtime_state()
        target = (item.name or "").strip()
        if not target:
            raise HTTPException(status_code=400, detail="tag name is required")

        db_url = f"https://api.notion.com/v1/databases/{db_id}"
        bound_property = mgr.get_bound_property("tags", force=True)
        if not bound_property:
            raise HTTPException(status_code=400, detail="Notion Tags property is missing or invalid")
        tag_prop_name = bound_property["name"]
        tags_prop = bound_property["schema"]

        options = tags_prop.get("multi_select", {}).get("options", [])
        kept_options = [opt for opt in options if (opt.get("name") or "") != target]
        if len(kept_options) == len(options):
            return {"ok": True, "deleted": False}

        update_options = []
        for opt in kept_options:
            if not opt.get("name"):
                continue
            update_options.append(
                {
                    "name": opt.get("name"),
                    "color": opt.get("color", "default"),
                }
            )

        if not tag_prop_name:
            raise HTTPException(status_code=400, detail="Notion Tags property binding is invalid")
        payload = {"properties": {tag_prop_name: {"multi_select": {"options": update_options}}}}
        patch_resp = request_with_retry("PATCH", db_url, headers=notion_headers, json_payload=payload, timeout=20)
        patch_resp.raise_for_status()
        mgr.invalidate_database_cache()
        return {"ok": True, "deleted": True}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("delete_tag failed: %s", exc)
        raise HTTPException(status_code=500, detail="內部錯誤，請稍後再試")


@app.post("/add_news")
def add_news(
    item: TradingThesisInput,
    intake_factory: Annotated[Callable[[], CaptureIntake], Depends(get_capture_intake_factory)],
):
    try:
        return intake_factory().capture(item)
    except CaptureError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except Exception as exc:
        logger.exception(
            "add_news exception: ticker=%s mindset=%s err=%s",
            str(getattr(item, "ticker", "")),
            getattr(item, "mindset", None),
            exc,
        )
        raise HTTPException(status_code=500, detail="內部錯誤，請稍後再試") from exc


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/schema/snapshot")
def schema_snapshot():
    _, _, mgr = _get_runtime_state()
    status = mgr.get_status()
    snap = status.get("schema") or {}
    display_names = dict(snap.get("display_names") or {})
    return {
        "ok": True,
        "schema": snap,
        "display_names": display_names,
        "meta": status.get("meta") or {},
    }


@app.get("/schema/status")
def schema_status():
    _, _, mgr = _get_runtime_state()
    status = mgr.get_status()
    snap = status.get("schema") or {}
    return {
        "ok": True,
        "status": snap.get("status", "error"),
        "display_names": dict(snap.get("display_names") or {}),
        "bindings": dict(snap.get("bindings") or {}),
        "errors": list(snap.get("errors") or []),
        "warnings": list(snap.get("warnings") or []),
        "meta": status.get("meta") or {},
    }


@app.post("/schema/resync")
def schema_resync(req: Optional[ResyncRequest] = None):
    try:
        _, _, mgr = _get_runtime_state()
        snap = mgr.sync_with_notion(force=True if req is None else bool(req.force))
        return {"ok": True, "schema": snap}
    except Exception as exc:
        logger.exception("schema_resync failed: %s", exc)
        raise HTTPException(status_code=500, detail="內部錯誤，請稍後再試")


@app.post("/schema/override")
def schema_override(item: SchemaOverrideItem):
    try:
        _, _, mgr = _get_runtime_state()
        mgr.set_override(item.internal_key, item.property_id)
        snap = mgr.sync_with_notion(force=True)
        return {"ok": True, "schema": snap}
    except Exception as exc:
        logger.exception("schema_override failed: %s", exc)
        raise HTTPException(status_code=500, detail="內部錯誤，請稍後再試")


@app.post("/update_exposure")
def update_exposure(item: UpdateExposureItem):
    try:
        _, notion_headers, mgr = _get_runtime_state()
        page_id = (item.page_id or "").strip()
        if not page_id:
            raise HTTPException(status_code=400, detail="page_id is required")
        if not UUID_RE.match(page_id):
            raise HTTPException(status_code=400, detail="page_id format invalid")
        exposure = parse_optional_float(item.exposure)
        if exposure is None:
            raise HTTPException(status_code=400, detail="exposure is invalid")

        url = f"https://api.notion.com/v1/pages/{page_id}"
        props = mgr.convert_payload({"exposure": exposure})
        if not props:
            raise HTTPException(
                status_code=400,
                detail="Exposure 欄位未綁定或不存在，請先同步 Notion 欄位對照。",
            )
        payload = {"properties": props}
        resp = request_with_retry("PATCH", url, headers=notion_headers, json_payload=payload, timeout=20)
        resp.raise_for_status()
        return {"ok": True, "page_id": page_id, "exposure": exposure}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("update_exposure failed: %s", exc)
        raise HTTPException(status_code=500, detail="內部錯誤，請稍後再試")


if __name__ == "__main__":
    # Support direct start via: python3 backend/app.py
    host = (os.getenv("NEWS_ALPHA_API_HOST") or "").strip()
    if not host:
        host = "0.0.0.0" if _env_true(os.getenv("NEWS_ALPHA_BIND_ALL")) else "127.0.0.1"
    port = int(os.getenv("NEWS_ALPHA_API_PORT", "8000") or 8000)
    current_api_key = _get_api_key_dynamic(force=True)
    if not _is_loopback_host(host) and not current_api_key:
        raise RuntimeError("Binding to a non-loopback host requires NEWS_ALPHA_API_KEY / API_KEY.")
    uvicorn.run(app, host=host, port=port, reload=False)
