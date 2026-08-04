from __future__ import annotations

import csv
import io
import importlib.util
import json
from contextlib import redirect_stdout
import tempfile
import unittest
from datetime import datetime
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str, relative_path: str):
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LICENSES = load_script(
    "check_direct_dependency_licenses_for_tests",
    "scripts/check_direct_dependency_licenses.py",
)
PUBLIC_RELEASE = load_script(
    "check_public_release_for_tests",
    "scripts/check_public_release.py",
)
RECOVERY = load_script(
    "runtime_recovery_for_tests",
    "scripts/runtime_recovery.py",
)


class DependencyLicenseScriptTests(unittest.TestCase):
    def test_extra_ledger_columns_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            requirements = root / "requirements.txt"
            ledger = root / "direct-dependency-licenses.csv"
            requirements.write_text("example==1.0\n", encoding="utf-8")
            with ledger.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(
                    [
                        "requirement",
                        "installed_version",
                        "license",
                        "metadata_evidence",
                        "upstream_license",
                        "caveat",
                    ]
                )
                writer.writerow(
                    [
                        "example==1.0",
                        "1.0",
                        "MIT",
                        "metadata says MIT",
                        "https://github.com/example/example/blob/" + "a" * 40 + "/LICENSE",
                        "",
                        "unexpected extra field",
                    ]
                )

            metadata = Message()
            metadata["License"] = "MIT"
            distribution = SimpleNamespace(
                metadata=metadata,
                files=[],
                version="1.0",
            )
            original_requirements = LICENSES.REQUIREMENTS_PATH
            original_ledger = LICENSES.LEDGER_PATH
            LICENSES.REQUIREMENTS_PATH = requirements
            LICENSES.LEDGER_PATH = ledger
            try:
                with patch.object(
                    LICENSES.package_metadata,
                    "distribution",
                    return_value=distribution,
                ):
                    failures = LICENSES.validate()
            finally:
                LICENSES.REQUIREMENTS_PATH = original_requirements
                LICENSES.LEDGER_PATH = original_ledger

        self.assertTrue(
            any("多餘欄位" in failure for failure in failures),
            failures,
        )

    def test_pep508_extras_and_markers_resolve_the_distribution_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            requirements = root / "requirements.txt"
            ledger = root / "direct-dependency-licenses.csv"
            requirement = "example[extra]==1.0; python_version >= '3.11' # documented"
            requirements.write_text(requirement + "\n", encoding="utf-8")
            ledger.write_text(
                "requirement,installed_version,license,metadata_evidence,upstream_license,caveat\n"
                "example[extra]==1.0; python_version >= '3.11' # documented,1.0,MIT,metadata says MIT,"
                "https://github.com/example/example/"
                + "blob/"
                + "a" * 40
                + "/LICENSE,marker is not a resolution constraint\n",
                encoding="utf-8",
            )

            metadata = Message()
            metadata["License"] = "MIT"
            distribution = SimpleNamespace(metadata=metadata, files=[], version="1.0")

            def resolve(name: str):
                if name != "example":
                    raise LICENSES.package_metadata.PackageNotFoundError(name)
                return distribution

            original_requirements = LICENSES.REQUIREMENTS_PATH
            original_ledger = LICENSES.LEDGER_PATH
            LICENSES.REQUIREMENTS_PATH = requirements
            LICENSES.LEDGER_PATH = ledger
            try:
                with patch.object(
                    LICENSES.package_metadata,
                    "distribution",
                    side_effect=resolve,
                ):
                    failures = LICENSES.validate()
            finally:
                LICENSES.REQUIREMENTS_PATH = original_requirements
                LICENSES.LEDGER_PATH = original_ledger

        self.assertEqual([], failures)

    def test_license_file_probe_reads_only_a_bounded_prefix(self) -> None:
        class BoundedLicenseFile:
            def read_text(self, **kwargs):
                raise AssertionError("license evidence must not read the whole file")

            def open(self, **kwargs):
                return io.StringIO("MIT License\n" + "x" * 10000)

        metadata = Message()
        distribution = SimpleNamespace(
            metadata=metadata,
            files=["LICENSE"],
            locate_file=lambda _: BoundedLicenseFile(),
        )

        self.assertEqual("MIT", LICENSES.normalized_installed_license(distribution))

    def test_missing_license_inputs_return_a_failure_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "direct-dependency-licenses.csv"
            ledger.write_text(
                "requirement,installed_version,license,metadata_evidence,upstream_license,caveat\n",
                encoding="utf-8",
            )
            original_requirements = LICENSES.REQUIREMENTS_PATH
            original_ledger = LICENSES.LEDGER_PATH
            LICENSES.REQUIREMENTS_PATH = Path(directory) / "missing-requirements.txt"
            LICENSES.LEDGER_PATH = ledger
            output = io.StringIO()
            try:
                with redirect_stdout(output):
                    exit_code = LICENSES.main()
            finally:
                LICENSES.REQUIREMENTS_PATH = original_requirements
                LICENSES.LEDGER_PATH = original_ledger

        self.assertEqual(1, exit_code)
        self.assertIn("FAIL:", output.getvalue())


class PublicReleaseScriptTests(unittest.TestCase):
    def test_binary_tracked_file_is_skipped_instead_of_crashing(self) -> None:
        def run_git(*args, **kwargs):
            if kwargs.get("text"):
                raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid byte")
            return SimpleNamespace(returncode=0, stdout=b"\xff")

        with patch.object(PUBLIC_RELEASE.subprocess, "run", side_effect=run_git):
            self.assertIsNone(PUBLIC_RELEASE.read_at("HEAD", "binary-file"))

    def test_sensitive_config_assignments_reject_real_values_but_allow_examples(self) -> None:
        content = """
NOTION_TOKEN=
NEWS_ALPHA_DB_ID=你的_Notion_database_id
TWELVE_DATA_API_KEY=real-looking-value-123456
"""
        findings = PUBLIC_RELEASE.sensitive_config_assignments(content)
        self.assertEqual(1, len(findings))
        self.assertIn("TWELVE_DATA_API_KEY", findings[0])


class RuntimeRecoveryScriptTests(unittest.TestCase):
    def test_restore_drill_rejects_an_empty_manifest_object(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recovery_root = root / "SharedData" / "recovery"
            backup_root = recovery_root / "runtime-state"
            artifact = backup_root / "newsloop-runtime-backup-20260804-010203"
            artifact.mkdir(parents=True)
            (artifact / "metadata.json").write_text("{}\n", encoding="utf-8")

            original_values = {
                name: getattr(RECOVERY, name)
                for name in (
                    "ROOT",
                    "RECOVERY_ROOT",
                    "BACKUP_ROOT",
                    "RESTORE_REPORT_ROOT",
                    "TMP_RESTORE_ROOT",
                    "BACKUP_LOCK",
                    "BACKUP_STATUS_PATH",
                    "RESTORE_STATUS_PATH",
                )
            }
            RECOVERY.ROOT = root
            RECOVERY.RECOVERY_ROOT = recovery_root
            RECOVERY.BACKUP_ROOT = backup_root
            RECOVERY.RESTORE_REPORT_ROOT = recovery_root / "restore-drills"
            RECOVERY.TMP_RESTORE_ROOT = root / "restore-scratch"
            RECOVERY.BACKUP_LOCK = recovery_root / ".runtime-backup.lock"
            RECOVERY.BACKUP_STATUS_PATH = root / ".streamlit" / "runtime_backup_status.json"
            RECOVERY.RESTORE_STATUS_PATH = root / ".streamlit" / "restore_drill_status.json"
            try:
                drill = RECOVERY.run_restore_drill()
            finally:
                for name, value in original_values.items():
                    setattr(RECOVERY, name, value)

        self.assertEqual("error", drill["status"])

    def test_naive_backup_timestamp_is_interpreted_as_local_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            status_path = Path(directory) / "runtime_backup_status.json"
            status_path.write_text(
                json.dumps(
                    {"last_success_at": datetime.now().replace(microsecond=0).isoformat()}
                ),
                encoding="utf-8",
            )
            original_status_path = RECOVERY.BACKUP_STATUS_PATH
            RECOVERY.BACKUP_STATUS_PATH = status_path
            try:
                should_run, reason = RECOVERY.backup_due(stale_hours=24 * 365)
            finally:
                RECOVERY.BACKUP_STATUS_PATH = original_status_path

        self.assertFalse(should_run)
        self.assertIn("backup fresh", reason)


if __name__ == "__main__":
    unittest.main()
