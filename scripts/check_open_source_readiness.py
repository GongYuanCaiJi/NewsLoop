#!/usr/bin/env python3
"""檢查 NewsLoop 可以安全進入公開發布流程的固定條件。"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from check_direct_dependency_licenses import validate as validate_dependency_licenses


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_FILES = {
    "LICENSE",
    "README.md",
    "requirements-dev.txt",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CODE_OF_CONDUCT.md",
    ".env.example",
    ".github/workflows/ci.yml",
    ".github/workflows/secret-scan.yml",
    ".github/pull_request_template.md",
    "docs/open-source-release-checklist.md",
    "docs/research/direct-dependency-licenses.csv",
    "docs/research/open-source-publication-standards.md",
    "scripts/check_direct_dependency_licenses.py",
}
FORBIDDEN_TRACKED_PARTS = {
    ".DS_Store",
    ".env",
    ".pytest_cache",
    ".serena",
    ".streamlit",
    "__pycache__",
    "venv",
    ".venv",
    "versions",
}
TEXT_SUFFIXES = {
    "",
    ".css",
    ".html",
    ".js",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
MACOS_HOME_PREFIX = "/" + "Users/"
FORBIDDEN_PUBLIC_PATTERNS = {
    r"[A-Z0-9._%+-]+@(gmail|outlook|hotmail|yahoo)\.[A-Z]{2,}": "個人信箱",
    r"github\.com/[^/\s]+/workspace-[^/\s)'\"]+": "private workspace repository 名稱",
    r"~[/\\]\.[A-Z0-9_-]+": "維護者 home-level hidden config",
}


def tracked_files() -> list[Path]:
    completed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [Path(item.decode()) for item in completed.stdout.split(b"\0") if item]


def main() -> int:
    failures: list[str] = validate_dependency_licenses()
    tracked = tracked_files()
    tracked_names = {path.as_posix() for path in tracked}

    missing = sorted(REQUIRED_FILES - tracked_names)
    if missing:
        failures.append(f"缺少必要 tracked files: {', '.join(missing)}")

    forbidden = sorted(
        path.as_posix()
        for path in tracked
        if any(part in FORBIDDEN_TRACKED_PARTS for part in path.parts)
    )
    if forbidden:
        failures.append(f"Git 追蹤了本地或敏感狀態: {', '.join(forbidden)}")

    absolute_paths: list[str] = []
    private_fragments: list[str] = []
    for relative in tracked:
        path = ROOT / relative
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if MACOS_HOME_PREFIX in content:
            absolute_paths.append(relative.as_posix())
        for pattern, description in FORBIDDEN_PUBLIC_PATTERNS.items():
            if re.search(pattern, content, flags=re.IGNORECASE):
                private_fragments.append(f"{relative.as_posix()} ({description})")
    if absolute_paths:
        failures.append(f"Tracked text 含 macOS 個人絕對路徑: {', '.join(sorted(absolute_paths))}")
    if private_fragments:
        failures.append(f"Tracked text 含 private workspace 資訊: {', '.join(sorted(private_fragments))}")

    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8") if (ROOT / "LICENSE").exists() else ""
    if "MIT License" not in license_text or "NewsLoop contributors" not in license_text:
        failures.append("LICENSE 不是預期的 NewsLoop MIT 授權文字")

    dev_requirements_path = ROOT / "requirements-dev.txt"
    if dev_requirements_path.exists():
        dev_requirements = {
            line.strip()
            for line in dev_requirements_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        expected_dev_requirements = {
            "-r requirements.txt",
            "pre-commit==4.6.0",
            "pip-audit==2.10.0",
        }
        if dev_requirements != expected_dev_requirements:
            failures.append(
                "requirements-dev.txt 必須引用 runtime requirements 並 exact pin "
                "pre-commit==4.6.0、pip-audit==2.10.0"
            )

    for doc_name in ("README.md", "CONTRIBUTING.md"):
        doc_path = ROOT / doc_name
        if not doc_path.exists():
            continue
        content = doc_path.read_text(encoding="utf-8")
        for fragment in ("requirements-dev.txt", "Node.js 20", "node:test"):
            if fragment not in content:
                failures.append(f"{doc_name} 缺少 contributor prerequisite: {fragment}")

    contributing_path = ROOT / "CONTRIBUTING.md"
    if contributing_path.exists():
        contributing = contributing_path.read_text(encoding="utf-8")
        if "venv/bin/pre-commit run --all-files" not in contributing:
            failures.append("CONTRIBUTING.md 必須從 documented venv 執行 pre-commit")

    ci_path = ROOT / ".github" / "workflows" / "ci.yml"
    if ci_path.exists():
        ci = ci_path.read_text(encoding="utf-8")
        for fragment in (
            "actions/setup-node@820762786026740c76f36085b0efc47a31fe5020",
            'node-version: "20"',
            "--requirement requirements-dev.txt",
        ):
            if fragment not in ci:
                failures.append(f"ci.yml 缺少固定 contributor toolchain: {fragment}")

    claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8").strip()
    if claude != "@AGENTS.md":
        failures.append("CLAUDE.md 必須只以 @AGENTS.md 連接主要 agent 入口")

    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    personal_email_pattern = r"[A-Z0-9._%+-]+@(gmail|outlook|hotmail|yahoo)\.[A-Z]{2,}"
    if "Report a vulnerability" not in security or re.search(
        personal_email_pattern, security, flags=re.IGNORECASE
    ):
        failures.append("SECURITY.md 必須使用 GitHub Private Vulnerability Reporting 且不得公開私人 Gmail")

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    if "https://github.com/GongYuanCaiJi/NewsLoop.git" not in readme:
        failures.append("README.md 必須指向 canonical public repository")

    for workflow in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        content = workflow.read_text(encoding="utf-8")
        for line in content.splitlines():
            if "uses:" in line:
                if not re.search(r"uses:\s+[^\s]+@[0-9a-f]{40}(?:\s+#|\s*$)", line):
                    failures.append(f"GitHub Action 未 pin 完整 commit SHA: {workflow.name}: {line.strip()}")

    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for key in ("NOTION_TOKEN=", "NEWS_ALPHA_DB_ID=", "NEWS_ALPHA_API_KEY="):
        if key not in env_example:
            failures.append(f".env.example 缺少 {key[:-1]}")

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1

    print(f"PASS: {len(tracked)} 個 tracked files 通過 open-source readiness 固定檢查")
    return 0


if __name__ == "__main__":
    sys.exit(main())
