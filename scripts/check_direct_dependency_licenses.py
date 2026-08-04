#!/usr/bin/env python3
"""Validate that every direct requirement has durable license evidence."""

from __future__ import annotations

import csv
import re
import sys
from importlib import metadata as package_metadata
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS_PATH = ROOT / "requirements.txt"
LEDGER_PATH = ROOT / "docs" / "research" / "direct-dependency-licenses.csv"
REQUIRED_COLUMNS = {
    "requirement",
    "installed_version",
    "license",
    "metadata_evidence",
    "upstream_license",
    "caveat",
}
ALLOWED_DIRECT_LICENSES = {"Apache-2.0", "BSD-3-Clause", "MIT"}
IMMUTABLE_UPSTREAM_LICENSE = re.compile(
    r"^https://github\.com/[^/]+/[^/]+/blob/[0-9a-f]{40}/LICENSE(?:\.md|\.txt)?$"
)


def package_name(requirement: str) -> str:
    # Keep this parser dependency-free, but accept the common PEP 508 forms
    # used in requirements files (extras, markers, and inline comments).
    requirement = requirement.split("#", 1)[0].strip()
    match = re.match(r"[A-Za-z0-9][A-Za-z0-9_.-]*", requirement)
    return match.group(0) if match else ""


def normalized_installed_license(distribution: package_metadata.Distribution) -> str:
    metadata = distribution.metadata
    candidates = [
        str(metadata.get("License-Expression") or "").strip(),
        str(metadata.get("License") or "").strip().splitlines()[0]
        if str(metadata.get("License") or "").strip()
        else "",
        *[
            value.removeprefix("License :: OSI Approved :: ").strip()
            for value in (metadata.get_all("Classifier") or [])
            if value.startswith("License :: OSI Approved :: ")
        ],
    ]
    for file in distribution.files or []:
        if Path(file).name.upper().startswith(("LICENSE", "COPYING")):
            try:
                with distribution.locate_file(file).open(encoding="utf-8") as handle:
                    candidates.append(handle.read(500))
            except (OSError, UnicodeDecodeError):
                continue

    for value in candidates:
        stripped_value = value.strip()
        if "Permission is hereby granted, free of charge" in stripped_value:
            return "MIT"
        first_line = stripped_value.splitlines()[0] if stripped_value else ""
        normalized = first_line.lower().replace(" ", "-")
        if normalized in {"mit", "mit-license", "the-mit-license-(mit)"}:
            return "MIT"
        if normalized in {"apache", "apache-2.0", "apache-license-2.0", "apache-software-license"}:
            return "Apache-2.0"
        if normalized in {"bsd-3-clause", "bsd-3-clause-license", "bsd-license"}:
            return "BSD-3-Clause"
    return ""


def declared_requirements() -> list[str]:
    return [
        line.strip()
        for line in REQUIREMENTS_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def validate() -> list[str]:
    failures: list[str] = []
    if not LEDGER_PATH.is_file():
        return [f"缺少 direct dependency license ledger: {LEDGER_PATH.relative_to(ROOT)}"]

    with LEDGER_PATH.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = set(reader.fieldnames or [])
        if columns != REQUIRED_COLUMNS:
            failures.append(
                "License ledger 欄位不符: "
                f"expected={sorted(REQUIRED_COLUMNS)} actual={sorted(columns)}"
            )
        rows = []
        for line_number, row in enumerate(reader, start=2):
            if row.get(None):
                failures.append(f"License ledger 第 {line_number} 行有多餘欄位")
            rows.append(row)

    requirements = declared_requirements()
    ledger_requirements = [str(row.get("requirement") or "").strip() for row in rows]
    if ledger_requirements != requirements:
        failures.append(
            "License ledger 必須逐行對應 requirements.txt: "
            f"requirements={requirements} ledger={ledger_requirements}"
        )

    duplicates = sorted({item for item in ledger_requirements if ledger_requirements.count(item) > 1})
    if duplicates:
        failures.append(f"License ledger 有重複 dependency: {', '.join(duplicates)}")

    for row in rows:
        requirement = str(row.get("requirement") or "").strip() or "<missing>"
        for field in ("installed_version", "license", "metadata_evidence", "upstream_license"):
            if not str(row.get(field) or "").strip():
                failures.append(f"{requirement} 缺少 {field}")
        license_name = str(row.get("license") or "").strip()
        if license_name and license_name not in ALLOWED_DIRECT_LICENSES:
            failures.append(f"{requirement} 使用未審核 license identifier: {license_name}")
        upstream = str(row.get("upstream_license") or "").strip()
        if upstream and not IMMUTABLE_UPSTREAM_LICENSE.fullmatch(upstream):
            failures.append(f"{requirement} upstream license 不是 immutable GitHub evidence: {upstream}")

        if ">=" in requirement and not str(row.get("caveat") or "").strip():
            failures.append(f"{requirement} 是 unbounded requirement，必須記錄 resolution caveat")
        if "metadata license fields are empty" in str(row.get("metadata_evidence") or ""):
            caveat = str(row.get("caveat") or "")
            if "wheel" not in caveat:
                failures.append(f"{requirement} metadata 缺 license，必須在 caveat 記錄 exact wheel evidence")

        name = package_name(requirement)
        if not name:
            failures.append(f"{requirement} 不是可解析的 package requirement")
            continue
        try:
            distribution = package_metadata.distribution(name)
        except (package_metadata.PackageNotFoundError, ValueError):
            failures.append(f"{requirement} 尚未安裝，無法核對 installed metadata")
            continue
        installed_version = str(distribution.version)
        expected_version = str(row.get("installed_version") or "").strip()
        if installed_version != expected_version:
            failures.append(
                f"{requirement} installed resolution 已改變: "
                f"ledger={expected_version} installed={installed_version}；必須重新稽核 license"
            )
        installed_license = normalized_installed_license(distribution)
        if installed_license != license_name:
            failures.append(
                f"{requirement} installed license evidence 不符: "
                f"ledger={license_name or '<missing>'} installed={installed_license or '<unclassified>'}"
            )

    return failures


def main() -> int:
    try:
        failures = validate()
    except (OSError, UnicodeError, csv.Error) as exc:
        print(f"FAIL: 無法讀取 dependency license inputs: {exc}")
        return 1
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print(f"PASS: {len(declared_requirements())} 個 direct dependencies 都有可稽核 license evidence")
    return 0


if __name__ == "__main__":
    sys.exit(main())
