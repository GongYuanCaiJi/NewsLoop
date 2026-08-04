from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Optional, Union

import requests

try:
    from filelock import FileLock as FileLock  # re-export
except ImportError as exc:
    raise RuntimeError("filelock package is required") from exc


def request_with_retry(
    method: str,
    url: str,
    *,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    json_payload: Any = None,
    retries: int = 3,
    timeout: float = 20.0,
    **kwargs,
):
    if "json" in kwargs:
        if json_payload is None:
            json_payload = kwargs.pop("json")
        else:
            kwargs.pop("json")
    if kwargs:
        raise TypeError(f"Unexpected request_with_retry kwargs: {', '.join(kwargs.keys())}")

    last_exc = None
    for attempt in range(max(1, int(retries))):
        try:
            resp = requests.request(
                method,
                url,
                params=params,
                headers=headers,
                json=json_payload,
                timeout=timeout,
            )
            if resp.status_code == 429:
                retry_after_raw = resp.headers.get("Retry-After")
                try:
                    retry_after = float(retry_after_raw) if retry_after_raw is not None else (1.5 * (attempt + 1))
                except (TypeError, ValueError):
                    retry_after = 1.5 * (attempt + 1)
                if attempt < retries - 1:
                    time.sleep(max(0.0, retry_after))
                    continue
                raise requests.HTTPError(
                    f"429 Too Many Requests (Retry-After={retry_after})",
                    response=resp,
                )
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(1.2 * (attempt + 1))
    if last_exc:
        raise last_exc
    raise RuntimeError("request failed")


def get_plain_text(rich_text):
    if not rich_text:
        return ""
    return "".join([chunk.get("plain_text", "") for chunk in rich_text])


def parse_optional_float(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load_json_file(path: Union[str, Path], default: Any = None):
    p = Path(path)
    try:
        if not p.exists():
            return default
        raw = p.read_text(encoding="utf-8").strip()
        if not raw:
            return default
        return json.loads(raw)
    except Exception:
        return default


def save_json_atomic(
    path: Union[str, Path],
    payload: Any,
    *,
    lock_path: Optional[Union[str, Path]] = None,
    timeout: float = 5.0,
) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lp = Path(lock_path) if lock_path else Path(str(p) + ".lock")

    tmp_name = None
    with FileLock(str(lp), timeout=timeout):
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(p.parent),
                prefix=f".{p.name}.",
                suffix=".tmp",
                delete=False,
            ) as tf:
                tf.write(json.dumps(payload, ensure_ascii=False, indent=2))
                tmp_name = tf.name
            os.replace(tmp_name, p)
            tmp_name = None
        finally:
            if tmp_name:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
