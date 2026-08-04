#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECOVERY_ROOT = ROOT / "SharedData" / "recovery"
BACKUP_ROOT = RECOVERY_ROOT / "runtime-state"
RESTORE_REPORT_ROOT = RECOVERY_ROOT / "restore-drills"
TMP_RESTORE_ROOT = Path("/tmp/newsloop_restore_drill")
BACKUP_LOCK = RECOVERY_ROOT / ".runtime-backup.lock"
BACKUP_STATUS_PATH = ROOT / ".streamlit" / "runtime_backup_status.json"
RESTORE_STATUS_PATH = ROOT / ".streamlit" / "restore_drill_status.json"
DEFAULT_STALE_HOURS = 24

SOURCE_FILES = (
    ".streamlit/daemon_control.json",
    ".streamlit/api_routing.json",
    ".streamlit/api_routing_stats.json",
    ".streamlit/optimization_memory.json",
    ".streamlit/schema_cache.json",
    ".streamlit/sector_cache.json",
)


def now() -> datetime:
    return datetime.now().astimezone()


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def timestamp_slug() -> str:
    return now().strftime("%Y%m%d-%H%M%S")


def ensure_dirs() -> None:
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    RESTORE_REPORT_ROOT.mkdir(parents=True, exist_ok=True)
    TMP_RESTORE_ROOT.mkdir(parents=True, exist_ok=True)
    BACKUP_STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def atomic_write_json(path: Path, payload: dict) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def backup_lock():
    ensure_dirs()
    with BACKUP_LOCK.open("w", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def copy_runtime_state(destination_root: Path) -> list[dict]:
    copied: list[dict] = []
    for relative_name in SOURCE_FILES:
        source = ROOT / relative_name
        if not source.is_file():
            continue
        destination = destination_root / relative_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied.append(
            {
                "path": relative_name,
                "size": destination.stat().st_size,
                "sha256": sha256_file(destination),
            }
        )
    return copied


def latest_backup_artifact() -> Path | None:
    ensure_dirs()
    candidates = sorted(path for path in BACKUP_ROOT.iterdir() if path.is_dir())
    return candidates[-1] if candidates else None


def create_backup() -> dict:
    ensure_dirs()
    artifact_id = f"newsloop-runtime-backup-{timestamp_slug()}"
    final_dir = BACKUP_ROOT / artifact_id
    if final_dir.exists():
        raise RuntimeError(f"backup artifact already exists: {final_dir}")

    with backup_lock():
        temp_dir = Path(tempfile.mkdtemp(prefix=f"{artifact_id}.", dir=RECOVERY_ROOT))
        try:
            payload_dir = temp_dir / artifact_id
            payload_dir.mkdir(parents=True)
            copied_files = copy_runtime_state(payload_dir)
            metadata = {
                "artifact_id": artifact_id,
                "created_at": now_iso(),
                "artifact_type": "newsloop_runtime_state_backup",
                "included_paths": [item["path"] for item in copied_files],
                "file_count": len(copied_files),
                "files": copied_files,
                "exclusions": [
                    ".streamlit/dashboard_config.json",
                    ".streamlit/secrets.toml",
                    ".env",
                    "/tmp/newsloop_*",
                ],
            }
            atomic_write_json(payload_dir / "metadata.json", metadata)
            os.replace(payload_dir, final_dir)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    status = {
        "status": "ok",
        "last_success_at": metadata["created_at"],
        "latest_artifact_path": str(final_dir.relative_to(ROOT)),
        "file_count": metadata["file_count"],
    }
    atomic_write_json(BACKUP_STATUS_PATH, status)
    return status


def read_backup_status() -> dict:
    if not BACKUP_STATUS_PATH.is_file():
        return {}
    try:
        return json.loads(BACKUP_STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def backup_due(stale_hours: int) -> tuple[bool, str]:
    if stale_hours < 0:
        raise ValueError("stale-hours must be zero or greater")
    status = read_backup_status()
    raw_timestamp = status.get("last_success_at")
    if not raw_timestamp:
        return True, "backup status missing"
    try:
        last_success = datetime.fromisoformat(raw_timestamp)
    except ValueError:
        return True, "backup timestamp invalid"
    current = now()
    if last_success.tzinfo is None:
        last_success = last_success.replace(tzinfo=current.tzinfo)
    else:
        last_success = last_success.astimezone(current.tzinfo)
    age = current - last_success
    if age >= timedelta(hours=stale_hours):
        return True, f"backup stale ({age})"
    return False, f"backup fresh ({age})"


def maybe_backup(stale_hours: int, force: bool = False) -> dict:
    should_run, reason = (True, "forced run") if force else backup_due(stale_hours)
    if not should_run:
        return {"status": "skipped", "reason": reason}
    result = create_backup()
    result["reason"] = reason
    return result


def safe_artifact_path(restore_root: Path, relative_name: str) -> Path:
    relative_path = Path(relative_name)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValueError(f"unsafe artifact path: {relative_name}")
    return restore_root / relative_path


def run_restore_drill() -> dict:
    ensure_dirs()
    artifact_dir = latest_backup_artifact()
    if artifact_dir is None:
        raise RuntimeError("no runtime backup artifact found")

    restore_id = f"restore-drill-{timestamp_slug()}-{artifact_dir.name}"
    restore_target = TMP_RESTORE_ROOT / restore_id
    shutil.rmtree(restore_target, ignore_errors=True)
    shutil.copytree(artifact_dir, restore_target)

    errors: list[str] = []
    verified_files: list[str] = []
    metadata_path = restore_target / "metadata.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        metadata = {}
        errors.append(f"invalid metadata.json: {exc}")

    files = []
    if not isinstance(metadata, dict):
        errors.append("invalid metadata.json: expected an object")
    else:
        if metadata.get("artifact_id") != artifact_dir.name:
            errors.append("metadata artifact_id mismatch")
        if metadata.get("artifact_type") != "newsloop_runtime_state_backup":
            errors.append("metadata artifact_type mismatch")
        raw_files = metadata.get("files")
        if not isinstance(raw_files, list):
            errors.append("metadata files must be a list")
        else:
            files = raw_files
        file_count = metadata.get("file_count")
        if not isinstance(file_count, int) or isinstance(file_count, bool):
            errors.append("metadata file_count must be an integer")
        elif file_count != len(files):
            errors.append("metadata file_count mismatch")

    for item in files:
        if not isinstance(item, dict):
            errors.append("metadata file entry must be an object")
            continue
        relative_name = item.get("path", "")
        try:
            restored_file = safe_artifact_path(restore_target, relative_name)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if not restored_file.is_file():
            errors.append(f"missing file: {relative_name}")
            continue
        if restored_file.stat().st_size != item.get("size"):
            errors.append(f"size mismatch: {relative_name}")
            continue
        if sha256_file(restored_file) != item.get("sha256"):
            errors.append(f"checksum mismatch: {relative_name}")
            continue
        if restored_file.suffix == ".json":
            try:
                json.loads(restored_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(f"invalid json: {relative_name}: {exc}")
                continue
        verified_files.append(relative_name)

    report = {
        "generated_at": now_iso(),
        "status": "ok" if not errors else "error",
        "artifact_path": str(artifact_dir.relative_to(ROOT)),
        "restore_target": str(restore_target),
        "verified_files": verified_files,
        "errors": errors,
    }
    report_path = RESTORE_REPORT_ROOT / f"{restore_id}.json"
    atomic_write_json(report_path, report)
    atomic_write_json(
        RESTORE_STATUS_PATH,
        {
            "status": report["status"],
            "last_drill_at": report["generated_at"],
            "last_report_path": str(report_path.relative_to(ROOT)),
            "last_error": errors[0] if errors else None,
        },
    )
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="NewsLoop 本地 runtime backup 與 restore drill")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("backup", help="立即建立 runtime backup")
    maybe_parser = subparsers.add_parser("maybe-backup", help="backup 過舊時才建立")
    maybe_parser.add_argument("--stale-hours", type=int, default=DEFAULT_STALE_HOURS)
    maybe_parser.add_argument("--force", action="store_true")
    subparsers.add_parser("drill", help="驗證最新 backup 可還原且內容完整")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "backup":
        result = create_backup()
    elif args.command == "maybe-backup":
        result = maybe_backup(args.stale_hours, args.force)
    else:
        result = run_restore_drill()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") in {"ok", "skipped"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
