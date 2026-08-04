from __future__ import annotations

import json
import math
import threading
import time
from urllib.parse import urlparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

try:
    from .io_utils import FileLock, request_with_retry, save_json_atomic
except Exception:
    from io_utils import FileLock, request_with_retry, save_json_atomic

try:
    from .schema_specs import DEFAULT_COLUMN_MAPPING, FIELD_SPECS, normalize_column_mapping
except Exception:
    from schema_specs import DEFAULT_COLUMN_MAPPING, FIELD_SPECS, normalize_column_mapping


def _safe_timestamp(value: Any) -> float:
    try:
        timestamp = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return timestamp if math.isfinite(timestamp) else 0.0


class SchemaManager:
    def __init__(
        self,
        *,
        db_id: str,
        notion_headers: dict,
        cache_path: str,
        config_path: str,
        ttl_seconds: int = 900,
        database_ttl_seconds: int = 60,
        notion_request: Optional[Callable[[str, str, dict, Optional[dict]], dict]] = None,
    ):
        self.db_id = db_id
        self.notion_headers = notion_headers
        self.cache_path = Path(cache_path)
        self.lock_path = self.cache_path.with_suffix(self.cache_path.suffix + ".lock")
        self.config_path = Path(config_path)
        self.ttl_seconds = int(ttl_seconds)
        self.database_ttl_seconds = int(database_ttl_seconds)
        self._notion_request = notion_request or self._default_notion_request
        self._state_lock = threading.RLock()
        self._refresh_sync_lock = threading.Lock()
        self._database_fetch_lock = threading.Lock()
        self._state = {
            "snapshot": None,
            "fetched_at": 0.0,
            "is_refreshing": False,
            "last_error": "",
            "next_refresh_after": 0.0,
            "disabled": False,
            "manual_overrides": {},  # internal_key -> property_id
            "database": None,
            "database_fetched_at": 0.0,
        }

    def _file_lock(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        return FileLock(str(self.lock_path), timeout=5)

    def load_cache(self):
        if not self.cache_path.exists():
            return
        try:
            with self._file_lock():
                data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            snapshot = data.get("snapshot") if isinstance(data.get("snapshot"), dict) else None
            cached_db_id = str((snapshot or {}).get("db_id") or "").strip()
            if cached_db_id and cached_db_id != str(self.db_id).strip():
                snapshot = None
                manual_overrides = {}
                fetched_at = 0.0
                next_refresh_after = 0.0
            else:
                manual_overrides = data.get("manual_overrides") if isinstance(data.get("manual_overrides"), dict) else {}
                fetched_at = _safe_timestamp(data.get("fetched_at"))
                next_refresh_after = _safe_timestamp(data.get("next_refresh_after"))
            with self._state_lock:
                self._state["snapshot"] = snapshot
                self._state["fetched_at"] = fetched_at
                self._state["manual_overrides"] = manual_overrides
                self._state["next_refresh_after"] = next_refresh_after
        except Exception as exc:
            with self._state_lock:
                self._state["last_error"] = str(exc)

    def save_cache(self):
        with self._state_lock:
            payload = {
                "snapshot": self._state.get("snapshot"),
                "fetched_at": _safe_timestamp(self._state.get("fetched_at")),
                "next_refresh_after": _safe_timestamp(self._state.get("next_refresh_after")),
                "manual_overrides": dict(self._state.get("manual_overrides") or {}),
            }
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        save_json_atomic(
            self.cache_path,
            payload,
            lock_path=self.lock_path,
            timeout=5,
        )

    def _load_active_mapping(self) -> dict[str, str]:
        try:
            if self.config_path.exists():
                cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
                if isinstance(cfg, dict):
                    return normalize_column_mapping(cfg.get("COLUMN_MAPPING"))
        except Exception:
            pass
        return dict(DEFAULT_COLUMN_MAPPING)

    @staticmethod
    def default_mapping() -> dict[str, str]:
        return dict(DEFAULT_COLUMN_MAPPING)

    @staticmethod
    def normalize_mapping(raw: Optional[dict]) -> dict[str, str]:
        return normalize_column_mapping(raw)

    @staticmethod
    def column_name(mapping: dict[str, str], internal_key: str) -> str:
        return mapping.get(internal_key, DEFAULT_COLUMN_MAPPING.get(internal_key, internal_key))

    @staticmethod
    def _default_notion_request(method: str, url: str, headers: dict, payload: Optional[dict] = None) -> dict:
        resp = request_with_retry(
            method,
            url,
            headers=headers,
            json_payload=payload,
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()

    def _retrieve_database(self) -> dict:
        url = f"https://api.notion.com/v1/databases/{self.db_id}"
        database = self._notion_request("GET", url, self.notion_headers, None)
        with self._state_lock:
            self._state["database"] = database
            self._state["database_fetched_at"] = time.time()
        return database

    def _get_database(self) -> dict:
        with self._state_lock:
            database = self._state.get("database")
            fetched_at = float(self._state.get("database_fetched_at") or 0.0)
        if isinstance(database, dict) and (time.time() - fetched_at) < self.database_ttl_seconds:
            return database
        with self._database_fetch_lock:
            with self._state_lock:
                database = self._state.get("database")
                fetched_at = float(self._state.get("database_fetched_at") or 0.0)
            if isinstance(database, dict) and (time.time() - fetched_at) < self.database_ttl_seconds:
                return database
            return self._retrieve_database()

    @staticmethod
    def _find_by_id(db_props: dict, prop_id: str) -> Optional[tuple[str, dict]]:
        if not prop_id:
            return None
        for name, obj in db_props.items():
            if str(obj.get("id") or "") == str(prop_id):
                return name, obj
        return None

    def _build_snapshot(
        self,
        db_json: dict,
        previous_snapshot: Optional[dict],
        *,
        mapping: Optional[dict] = None,
    ) -> dict:
        db_props = (db_json or {}).get("properties") or {}
        id_map: dict[str, tuple[str, dict]] = {
            str(obj.get("id") or ""): (name, obj)
            for name, obj in db_props.items()
            if str(obj.get("id") or "")
        }
        prev_bindings = ((previous_snapshot or {}).get("bindings") or {})
        active_mapping = normalize_column_mapping(mapping) if mapping is not None else self._load_active_mapping()
        with self._state_lock:
            manual_overrides = dict(self._state.get("manual_overrides") or {})

        bindings: dict[str, dict] = {}
        errors: list[str] = []
        warnings: list[str] = []
        issues: list[dict] = []

        for ik, configured_name in active_mapping.items():
            spec = FIELD_SPECS.get(ik)
            if not spec:
                continue
            level = str(spec.get("level") or "warn").lower()
            accepted_types = set(spec.get("types") or set())
            prev = prev_bindings.get(ik) or {}
            override_id = str(manual_overrides.get(ik) or "")

            found_name = None
            found_prop = None

            if override_id:
                hit = id_map.get(override_id)
                if hit:
                    found_name, found_prop = hit

            if not found_prop:
                prev_id = str(prev.get("id") or "")
                hit = id_map.get(prev_id) if prev_id else None
                if hit:
                    found_name, found_prop = hit

            if not found_prop:
                # self-heal: same configured name
                if configured_name in db_props:
                    found_name = configured_name
                    found_prop = db_props[configured_name]

            if not found_prop:
                # self-heal: old bound name
                prev_name = str(prev.get("name") or "")
                if prev_name and prev_name in db_props:
                    found_name = prev_name
                    found_prop = db_props[prev_name]

            if not found_prop:
                msg = f"{ik}:{configured_name} missing"
                issues.append(
                    {
                        "key": ik,
                        "name": configured_name,
                        "kind": "missing",
                        "actual": "",
                        "expected": sorted(accepted_types),
                        "level": level,
                    }
                )
                if level == "error":
                    errors.append(msg)
                else:
                    warnings.append(msg)
                continue

            prop_type = str(found_prop.get("type") or "")
            if accepted_types and prop_type not in accepted_types:
                msg = f"{ik}:{found_name}:{prop_type}!={'/'.join(sorted(accepted_types))}"
                issues.append(
                    {
                        "key": ik,
                        "name": str(found_name or configured_name),
                        "kind": "type_mismatch",
                        "actual": prop_type,
                        "expected": sorted(accepted_types),
                        "level": level,
                    }
                )
                if level == "error":
                    errors.append(msg)
                    continue
                else:
                    warnings.append(msg)

            bindings[ik] = {
                "id": str(found_prop.get("id") or ""),
                "name": str(found_name or configured_name),
                "type": prop_type,
                "level": level,
            }

        status = "ok" if not errors and not warnings else ("warn" if not errors else "error")
        display_names = {}
        for ik in active_mapping.keys():
            b = bindings.get(ik) or {}
            display_names[ik] = str(b.get("name") or active_mapping.get(ik) or ik)

        return {
            "status": status,
            "errors": errors,
            "warnings": warnings,
            "issues": issues,
            "bindings": bindings,
            "mapping": active_mapping,
            "display_names": display_names,
            "db_id": self.db_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _compatibility_message(snapshot: dict) -> str:
        issues = list((snapshot or {}).get("issues") or [])
        if not issues:
            for level, source_key in (("error", "errors"), ("warn", "warnings")):
                for raw in (snapshot or {}).get(source_key) or []:
                    text = str(raw)
                    if text.endswith(" missing") and ":" in text:
                        issues.append(
                            {
                                "name": text.removesuffix(" missing").split(":", 1)[1],
                                "kind": "missing",
                                "level": level,
                            }
                        )
                        continue
                    parts = text.split(":", 2)
                    if len(parts) == 3 and "!=" in parts[2]:
                        actual, expected = parts[2].split("!=", 1)
                        issues.append(
                            {
                                "name": parts[1],
                                "kind": "type_mismatch",
                                "actual": actual,
                                "expected": expected.split("/"),
                                "level": level,
                            }
                        )
        for level, prefix in (("error", "SchemaError"), ("warn", "SchemaWarn")):
            missing = [str(item.get("name") or "") for item in issues if item.get("level") == level and item.get("kind") == "missing"]
            mismatches = []
            for item in issues:
                if item.get("level") != level or item.get("kind") != "type_mismatch":
                    continue
                expected = "/".join(str(value) for value in (item.get("expected") or []))
                mismatches.append(f"{item.get('name')}:{item.get('actual')}!={expected}")
            parts = []
            if missing:
                parts.append(f"missing={','.join(missing)}")
            if mismatches:
                parts.append(f"type_mismatch={','.join(mismatches)}")
            if parts:
                return f"{prefix}: {'; '.join(parts)}"
        status = str((snapshot or {}).get("status") or "error")
        if status == "error":
            return "SchemaError: schema unavailable"
        if status == "warn":
            return "SchemaWarn: schema compatibility warning"
        return "OK"

    def check(self, *, mapping: Optional[dict] = None, force: bool = False) -> dict:
        if mapping is None:
            snapshot = self.sync_with_notion(force=force)
        else:
            snapshot = self._build_snapshot(
                self._retrieve_database(),
                self._state.get("snapshot"),
                mapping=mapping,
            )
        status = str((snapshot or {}).get("status") or "error")
        return {
            "ok": status != "error",
            "status": status,
            "message": self._compatibility_message(snapshot),
            "display_names": dict((snapshot or {}).get("display_names") or {}),
            "bindings": dict((snapshot or {}).get("bindings") or {}),
            "errors": list((snapshot or {}).get("errors") or []),
            "warnings": list((snapshot or {}).get("warnings") or []),
            "snapshot": snapshot,
        }

    def get_bound_property(self, internal_key: str, *, force: bool = False) -> Optional[dict]:
        key = str(internal_key or "").strip()
        if not key or key not in FIELD_SPECS:
            return None
        snapshot = self.sync_with_notion(force=force)
        binding = ((snapshot or {}).get("bindings") or {}).get(key) or {}
        if not binding:
            return None
        database = self._get_database()
        properties = (database or {}).get("properties") or {}
        found = None
        name = str(binding.get("name") or "")
        if name in properties:
            found = (name, properties[name])
        elif binding.get("id"):
            found = self._find_by_id(properties, str(binding.get("id")))
        if not found:
            return None
        found_name, schema = found
        accepted_types = set((FIELD_SPECS.get(key) or {}).get("types") or set())
        actual_type = str((schema or {}).get("type") or "")
        if accepted_types and actual_type not in accepted_types:
            return None
        return {
            "name": found_name,
            "id": str((schema or {}).get("id") or binding.get("id") or ""),
            "type": actual_type,
            "schema": schema,
        }

    def invalidate_database_cache(self) -> None:
        with self._state_lock:
            self._state["database"] = None
            self._state["database_fetched_at"] = 0.0

    @staticmethod
    def _select_creatable_type(expected_types: set[str], internal_key: str) -> Optional[str]:
        if internal_key in {"asset_class", "sector"} and "multi_select" in expected_types:
            return "multi_select"
        for notion_type in (
            "title",
            "rich_text",
            "number",
            "select",
            "multi_select",
            "status",
            "date",
            "checkbox",
            "url",
            "email",
            "phone_number",
            "people",
            "files",
        ):
            if notion_type in expected_types:
                return notion_type
        return None

    @staticmethod
    def _property_create_schema(notion_type: str) -> dict:
        if notion_type in {"select", "multi_select"}:
            return {notion_type: {"options": []}}
        return {notion_type: {}}

    def autofill(self, *, mapping: Optional[dict] = None) -> dict:
        active_mapping = normalize_column_mapping(mapping) if mapping is not None else self._load_active_mapping()
        url = f"https://api.notion.com/v1/databases/{self.db_id}"
        database = self._retrieve_database()
        properties = (database or {}).get("properties") or {}
        with self._state_lock:
            previous_snapshot = self._state.get("snapshot")
        resolved_bindings = (
            self._build_snapshot(database, previous_snapshot, mapping=active_mapping).get("bindings") or {}
        )
        title_names = {
            name
            for name, prop in properties.items()
            if str((prop or {}).get("type") or "") == "title"
        }
        created: list[dict] = []
        skipped: list[dict] = []
        failed: list[dict] = []
        attempted_names: list[str] = []

        for internal_key, notion_name in active_mapping.items():
            if notion_name in properties or internal_key in resolved_bindings:
                continue
            spec = FIELD_SPECS.get(internal_key)
            if not spec:
                skipped.append({"key": internal_key, "name": notion_name, "reason": "缺少欄位規格"})
                continue
            expected_types = set(spec.get("types") or set())
            target_type = self._select_creatable_type(expected_types, internal_key)
            if not target_type:
                expected_label = "/".join(sorted(expected_types)) or "unknown"
                skipped.append(
                    {
                        "key": internal_key,
                        "name": notion_name,
                        "reason": f"型別不支援自動建立 ({expected_label})",
                    }
                )
                continue
            if target_type == "title" and title_names:
                skipped.append(
                    {
                        "key": internal_key,
                        "name": notion_name,
                        "reason": f"資料庫已存在 title 欄位 ({', '.join(sorted(title_names))})",
                    }
                )
                continue

            attempted_names.append(notion_name)
            try:
                self._notion_request(
                    "PATCH",
                    url,
                    self.notion_headers,
                    {"properties": {notion_name: self._property_create_schema(target_type)}},
                )
                created.append({"key": internal_key, "name": notion_name, "type": target_type})
                if target_type == "title":
                    title_names.add(notion_name)
                properties[notion_name] = {"type": target_type}
            except Exception as exc:
                failed.append(
                    {
                        "key": internal_key,
                        "name": notion_name,
                        "type": target_type,
                        "reason": str(exc),
                    }
                )

        latest_properties = (self._retrieve_database() or {}).get("properties") or {}
        unresolved = {name for name in attempted_names if name not in latest_properties}
        if unresolved:
            for item in created[:]:
                if item["name"] not in unresolved:
                    continue
                created.remove(item)
                failed.append(
                    {
                        "key": item["key"],
                        "name": item["name"],
                        "type": item.get("type", ""),
                        "reason": "建立後驗證失敗（欄位未出現在最新 Schema）",
                    }
                )

        return {"created": created, "skipped": skipped, "failed": failed}

    def _refresh_sync(self) -> dict:
        with self._refresh_sync_lock:
            with self._state_lock:
                if self._state.get("disabled"):
                    snap = self._state.get("snapshot")
                    return snap if isinstance(snap, dict) else {}
                previous_snapshot = self._state.get("snapshot")
            db_json = self._retrieve_database()
            snap = self._build_snapshot(db_json, previous_snapshot)
            with self._state_lock:
                if self._state.get("disabled"):
                    current = self._state.get("snapshot")
                    return current if isinstance(current, dict) else snap
                self._state["snapshot"] = snap
                self._state["fetched_at"] = time.time()
                self._state["last_error"] = ""
                self._state["next_refresh_after"] = 0.0
            self.save_cache()
            return snap

    def _background_refresh_worker(self):
        try:
            self._refresh_sync()
        except Exception as exc:
            with self._state_lock:
                if self._state.get("disabled"):
                    return
                self._state["last_error"] = str(exc)
                self._state["next_refresh_after"] = time.time() + max(15.0, min(300.0, float(self.ttl_seconds) / 4.0))
            try:
                self.save_cache()
            except Exception:
                pass
        finally:
            with self._state_lock:
                self._state["is_refreshing"] = False

    def sync_with_notion(self, force: bool = False) -> dict:
        now = time.time()
        with self._state_lock:
            if self._state.get("disabled"):
                snap = self._state.get("snapshot")
                return snap if isinstance(snap, dict) else {}
            snap = self._state.get("snapshot")
            fetched_at = _safe_timestamp(self._state.get("fetched_at"))
        stale = (now - fetched_at) > self.ttl_seconds

        if force or snap is None:
            with self._state_lock:
                if self._state.get("is_refreshing"):
                    current = self._state.get("snapshot")
                    return current if isinstance(current, dict) else (snap if isinstance(snap, dict) else {})
                self._state["is_refreshing"] = True
            try:
                return self._refresh_sync()
            finally:
                with self._state_lock:
                    self._state["is_refreshing"] = False

        if stale:
            # SWR: return stale quickly, refresh in background
            with self._state_lock:
                can_refresh_now = now >= _safe_timestamp(self._state.get("next_refresh_after"))
                if can_refresh_now and not self._state.get("is_refreshing"):
                    self._state["is_refreshing"] = True
                    try:
                        t = threading.Thread(target=self._background_refresh_worker, daemon=True)
                        t.start()
                    except Exception as exc:
                        self._state["is_refreshing"] = False
                        self._state["last_error"] = str(exc)
                        self._state["next_refresh_after"] = time.time() + max(
                            15.0, min(300.0, float(self.ttl_seconds) / 4.0)
                        )
            return snap
        return snap

    def deactivate(self):
        with self._state_lock:
            self._state["disabled"] = True
            self._state["is_refreshing"] = False

    def get_active_mapping(self) -> dict[str, str]:
        # Note: this may trigger refresh work via sync_with_notion(force=False).
        # When there is no snapshot yet, a synchronous refresh may occur.
        snap = self.sync_with_notion(force=False)
        return dict((snap or {}).get("display_names") or {})

    def get_status(self) -> dict:
        snap = self.sync_with_notion(force=False)
        with self._state_lock:
            fetched_at = _safe_timestamp(self._state.get("fetched_at"))
            is_refreshing = bool(self._state.get("is_refreshing"))
            last_error = str(self._state.get("last_error") or "")
        return {
            "schema": snap,
            "meta": {
                "fetched_at": datetime.fromtimestamp(fetched_at, tz=timezone.utc).isoformat() if fetched_at else "",
                "ttl_seconds": self.ttl_seconds,
                "is_refreshing": is_refreshing,
                "last_error": last_error,
            },
        }

    def set_override(self, internal_key: str, notion_property_id: str):
        key = str(internal_key or "").strip()
        pid = str(notion_property_id or "").strip()
        if not key:
            return
        with self._state_lock:
            overrides = dict(self._state.get("manual_overrides") or {})
            if pid:
                overrides[key] = pid
            elif key in overrides:
                overrides.pop(key, None)
            self._state["manual_overrides"] = overrides
            # Invalidate snapshot so next read triggers a fresh sync with the new override.
            self._state["snapshot"] = None
            self._state["fetched_at"] = 0.0
        self.save_cache()

    @staticmethod
    def _to_property_payload(notion_type: str, value, *, title_link: Optional[str] = None):
        if value is None:
            return None
        t = str(notion_type or "")
        if t == "title":
            text = {"content": str(value)}
            if title_link:
                parsed = urlparse(str(title_link).strip())
                if parsed.scheme in {"http", "https"}:
                    text["link"] = {"url": str(title_link).strip()}
            return {"title": [{"text": text}]}
        if t == "rich_text":
            return {"rich_text": [{"text": {"content": str(value)}}]}
        if t == "select":
            v = str(value).strip()
            return {"select": {"name": v}} if v else None
        if t == "number":
            try:
                return {"number": float(value)}
            except Exception:
                return None
        if t == "url":
            s = str(value).strip()
            if not s:
                return None
            parsed = urlparse(s)
            if parsed.scheme not in {"http", "https"}:
                return None
            return {"url": s}
        if t == "date":
            return {"date": {"start": str(value)}}
        if t == "multi_select":
            arr = value if isinstance(value, list) else [value]
            cleaned = [{"name": str(v)} for v in arr if str(v).strip()]
            return {"multi_select": cleaned}
        # Formula / rollup and other read-only types are intentionally skipped on write.
        return None

    def convert_payload(self, payload: dict, *, title_link: Optional[str] = None) -> dict:
        snap = self.sync_with_notion(force=False)
        bindings = (snap or {}).get("bindings") or {}
        out = {}
        for ik, value in (payload or {}).items():
            b = bindings.get(ik) or {}
            notion_type = str(b.get("type") or "")
            # Prefer property name for maximum Notion API compatibility, keep ID as fallback.
            prop_key = str(b.get("name") or b.get("id") or "")
            if not prop_key:
                continue
            item = self._to_property_payload(notion_type, value, title_link=title_link if ik == "title" else None)
            if item is not None:
                out[prop_key] = item
        return out
