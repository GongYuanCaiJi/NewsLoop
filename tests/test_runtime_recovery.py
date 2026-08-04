from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "runtime_recovery.py"
SPEC = importlib.util.spec_from_file_location("runtime_recovery", SCRIPT_PATH)
assert SPEC and SPEC.loader
runtime_recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime_recovery)


class RuntimeRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.originals = {
            name: getattr(runtime_recovery, name)
            for name in (
                "ROOT",
                "RECOVERY_ROOT",
                "BACKUP_ROOT",
                "RESTORE_REPORT_ROOT",
                "TMP_RESTORE_ROOT",
                "BACKUP_LOCK",
                "BACKUP_STATUS_PATH",
                "RESTORE_STATUS_PATH",
                "SOURCE_FILES",
            )
        }
        recovery_root = self.root / "SharedData" / "recovery"
        runtime_recovery.ROOT = self.root
        runtime_recovery.RECOVERY_ROOT = recovery_root
        runtime_recovery.BACKUP_ROOT = recovery_root / "runtime-state"
        runtime_recovery.RESTORE_REPORT_ROOT = recovery_root / "restore-drills"
        runtime_recovery.TMP_RESTORE_ROOT = self.root / "restore-scratch"
        runtime_recovery.BACKUP_LOCK = recovery_root / ".runtime-backup.lock"
        runtime_recovery.BACKUP_STATUS_PATH = (
            self.root / ".streamlit" / "runtime_backup_status.json"
        )
        runtime_recovery.RESTORE_STATUS_PATH = (
            self.root / ".streamlit" / "restore_drill_status.json"
        )
        runtime_recovery.SOURCE_FILES = (".streamlit/runtime.json",)
        runtime_file = self.root / ".streamlit" / "runtime.json"
        runtime_file.parent.mkdir(parents=True)
        runtime_file.write_text('{"value": 42}\n', encoding="utf-8")

    def tearDown(self) -> None:
        for name, value in self.originals.items():
            setattr(runtime_recovery, name, value)
        self.temp_dir.cleanup()

    def test_backup_then_restore_drill_verifies_manifest(self) -> None:
        backup = runtime_recovery.create_backup()
        drill = runtime_recovery.run_restore_drill()

        self.assertEqual("ok", backup["status"])
        self.assertEqual(1, backup["file_count"])
        self.assertEqual("ok", drill["status"])
        self.assertEqual([".streamlit/runtime.json"], drill["verified_files"])
        self.assertEqual([], drill["errors"])

    def test_fresh_backup_is_skipped(self) -> None:
        runtime_recovery.create_backup()

        result = runtime_recovery.maybe_backup(stale_hours=24)

        self.assertEqual("skipped", result["status"])
        self.assertIn("backup fresh", result["reason"])

    def test_restore_rejects_path_traversal(self) -> None:
        restore_root = self.root / "restore"

        with self.assertRaisesRegex(ValueError, "unsafe artifact path"):
            runtime_recovery.safe_artifact_path(restore_root, "../secret.json")

    def test_invalid_status_timestamp_requests_backup(self) -> None:
        runtime_recovery.BACKUP_STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        runtime_recovery.BACKUP_STATUS_PATH.write_text(
            json.dumps({"last_success_at": "not-a-date"}),
            encoding="utf-8",
        )

        should_run, reason = runtime_recovery.backup_due(stale_hours=24)

        self.assertTrue(should_run)
        self.assertEqual("backup timestamp invalid", reason)


if __name__ == "__main__":
    unittest.main()
