#!/usr/bin/env python3
"""Serve the real capture route with deterministic external adapters for tests."""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ["PYTHON_DOTENV_DISABLED"] = "1"

import uvicorn

import backend.app as backend_app
from backend.capture_intake import CaptureIntake

app = backend_app.app
get_capture_intake_factory = backend_app.get_capture_intake_factory


class FixedSchema:
    def check(self) -> dict:
        return {"ok": True, "snapshot": {"bindings": {}}}

    def convert_payload(self, payload: dict, *, title_link: str | None = None) -> dict:
        del title_link
        return dict(payload)

    def default_mapping(self) -> dict[str, str]:
        return {}


class FixedAssets:
    def normalize_ticker(self, value: str) -> str:
        return value.strip().upper()

    def classify(self, ticker: str) -> str:
        del ticker
        return ""

    def sector(self, ticker: str, asset_class: str) -> tuple[None, None]:
        del ticker, asset_class
        return None, None


class RecordingNotion:
    def __init__(self, output_path: Path):
        self.output_path = output_path
        self.writes: list[dict] = []

    def create_page(self, payload: dict) -> None:
        self.writes.append(payload)
        temporary = self.output_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"write_count": len(self.writes), "writes": self.writes}),
            encoding="utf-8",
        )
        temporary.replace(self.output_path)


def main() -> None:
    output_path = Path(os.environ["NEWSLOOP_CAPTURE_E2E_OUTPUT"])
    ready_path = Path(os.environ["NEWSLOOP_CAPTURE_E2E_READY"])
    backend_app.LOCAL_CFG_PATH = os.environ["NEWSLOOP_CAPTURE_E2E_CONFIG"]
    backend_app._security_cache = {"api_key": ""}
    notion = RecordingNotion(output_path)
    intake = CaptureIntake(
        database_id="test-database",
        schema=FixedSchema(),
        notion=notion,
        assets=FixedAssets(),
    )
    app.dependency_overrides[get_capture_intake_factory] = lambda: (lambda: intake)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    ready_path.write_text(json.dumps({"port": port}), encoding="utf-8")

    config = uvicorn.Config(app, log_level="warning", lifespan="off")
    uvicorn.Server(config).run(sockets=[listener])


if __name__ == "__main__":
    main()
