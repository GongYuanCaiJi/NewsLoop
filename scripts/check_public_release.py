#!/usr/bin/env python3
"""檢查指定 Git ref 是否可作為公開發布的乾淨來源。

這個檢查刻意不把 CI 綠燈當成公開發布證明；它會另外檢查 Git history、
commit metadata、所有 reachable objects，以及目前 tree 的私人資料痕跡。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path


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

FORBIDDEN_PATH_PARTS = {
    ".DS_Store",
    ".env",
    ".pytest_cache",
    ".serena",
    ".streamlit",
    "__pycache__",
    "state",
    "venv",
    ".venv",
    "versions",
}

PRIVATE_TEXT_PATTERNS = {
    re.compile(r"/(?:Users|home)/[^\s/]+", re.IGNORECASE): "個人絕對路徑",
    re.compile(r"[A-Z0-9._%+-]+@(gmail|outlook|hotmail|yahoo|icloud)\.[A-Z]{2,}", re.IGNORECASE): "私人信箱",
    re.compile(r"github\.com/[^/\s]+/workspace-[^/\s)'\"]+", re.IGNORECASE): "private workspace repository",
    re.compile(r"~[/\\]\.[A-Z0-9_-]+", re.IGNORECASE): "維護者 home-level 設定",
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16})\b"): "疑似 credential",
    re.compile(r"BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY"): "private key",
    re.compile(r"\b(?:secret|ntn)_[A-Za-z0-9_-]{16,}\b"): "疑似 Notion credential",
}

SENSITIVE_CONFIG_KEYS = re.compile(
    r"^(?:NOTION_TOKEN|NEWS_ALPHA_DB_ID|TWELVE_DATA_API_KEY|FUGLE_API_KEY|"
    r"OPENAI_API_KEY|GOOGLE_API_KEY|GROQ_API_KEY|NEWS_ALPHA_API_KEY)$",
    re.IGNORECASE,
)
SENSITIVE_JSON_KEYS = re.compile(
    r"^[\"'](?:NOTION_TOKEN|NEWS_ALPHA_DB_ID|TWELVE_DATA_API_KEY|FUGLE_API_KEY|"
    r"OPENAI_API_KEY|GOOGLE_API_KEY|GROQ_API_KEY|NEWS_ALPHA_API_KEY)[\"']$",
    re.IGNORECASE,
)
WORKING_TREE_CONFIG_SUFFIXES = {".env", ".ini", ".json", ".toml", ".yaml", ".yml"}
WORKING_TREE_SKIP_DIRS = {".git", ".pytest_cache", "__pycache__", "node_modules", "venv", ".venv"}

PRIVATE_EMAIL = re.compile(
    r"^[^@\s]+@(gmail|outlook|hotmail|yahoo|icloud)\.[^@\s]+$",
    re.IGNORECASE,
)


def run(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.stdout


def tracked_files(ref: str) -> list[str]:
    return [line for line in run("ls-tree", "-r", "--name-only", ref).splitlines() if line]


def read_at(ref: str, path: str) -> str | None:
    completed = subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        text=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if completed.returncode != 0:
        return None
    if isinstance(completed.stdout, str):
        return completed.stdout
    try:
        return completed.stdout.decode("utf-8")
    except (AttributeError, UnicodeDecodeError):
        return None


def history_commits(ref: str) -> list[tuple[str, str, str, str, str]]:
    output = run("log", "--format=%H%x09%an%x09%ae%x09%cn%x09%ce%x09%s", ref)
    commits: list[tuple[str, str, str, str, str]] = []
    for line in output.splitlines():
        parts = line.split("\t", 5)
        if len(parts) == 6:
            commits.append((parts[0], parts[1], parts[2], parts[3], parts[4]))
    return commits


def working_tree_files() -> list[Path]:
    """列出 working tree 的 project-owned files，包含被 Git ignore 的檔案。"""

    paths: list[Path] = []
    for root, directories, filenames in os.walk(Path.cwd()):
        directories[:] = [name for name in directories if name not in WORKING_TREE_SKIP_DIRS]
        root_path = Path(root)
        paths.extend(root_path / filename for filename in filenames if filename != ".git")
    return sorted(paths)


def _is_placeholder(value: str) -> bool:
    normalized = value.strip().strip("\"'").strip()
    if not normalized:
        return True
    return bool(
        re.match(
            r"(?i)^(?:your|replace|example|sample|dummy|changeme|test|"
            r"your[_ -].*|你的|請填|填入|此處).*",
            normalized,
        )
    )


def sensitive_config_assignments(content: str) -> list[str]:
    """找出設定檔裡非空、非範例的 credential assignment，不回傳值本身。"""

    findings: list[str] = []
    for line_number, line in enumerate(content.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" in stripped and stripped.lstrip().startswith(("\"", "'")):
            key, value = stripped.split(":", 1)
            key = key.strip().rstrip(",").strip()
            value = value.strip().rstrip(",")
            key_match = SENSITIVE_JSON_KEYS.fullmatch(key)
        else:
            if "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            key_match = SENSITIVE_CONFIG_KEYS.fullmatch(key.strip())
        if key_match and not _is_placeholder(value):
            clean_key = key_match.group(0).strip("'\"")
            findings.append(f"敏感設定 {clean_key} at line {line_number}")
    return findings


def audit_working_tree() -> int:
    failures: list[str] = []
    files = working_tree_files()
    for path in files:
        relative_path = path.relative_to(Path.cwd()).as_posix()
        parts = set(Path(relative_path).parts)
        forbidden = sorted(parts & FORBIDDEN_PATH_PARTS)
        if forbidden:
            failures.append(
                f"working tree private/runtime path: {relative_path} ({', '.join(forbidden)})"
            )

        if Path(relative_path).suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for pattern, description in PRIVATE_TEXT_PATTERNS.items():
            if pattern.search(content):
                failures.append(f"working tree text {description}: {relative_path}")
        if Path(relative_path).suffix.lower() in WORKING_TREE_CONFIG_SUFFIXES:
            failures.extend(
                f"working tree {relative_path}: {finding}"
                for finding in sensitive_config_assignments(content)
            )

    if failures:
        print("PUBLIC_RELEASE_AUDIT=FAIL scope=working-tree")
        for failure in sorted(set(failures)):
            print(f"- {failure}")
        return 1

    print(f"PUBLIC_RELEASE_AUDIT=PASS scope=working-tree files={len(files)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ref", nargs="?", default="HEAD", help="要檢查的 Git ref（預設：HEAD）")
    parser.add_argument(
        "--working-tree",
        action="store_true",
        help="檢查目前 project-owned working tree，包含 Git ignore 的本機檔案",
    )
    args = parser.parse_args()

    if args.working_tree:
        return audit_working_tree()

    failures: list[str] = []
    files = tracked_files(args.ref)

    for path in files:
        parts = set(Path(path).parts)
        forbidden = sorted(parts & FORBIDDEN_PATH_PARTS)
        if forbidden:
            failures.append(f"tracked private/runtime path: {path} ({', '.join(forbidden)})")

        suffix = Path(path).suffix.lower()
        if suffix not in TEXT_SUFFIXES:
            continue
        content = read_at(args.ref, path)
        if content is None:
            continue
        for pattern, description in PRIVATE_TEXT_PATTERNS.items():
            if pattern.search(content):
                failures.append(f"tracked text {description}: {path}")

    for commit, author, author_email, committer, committer_email in history_commits(args.ref):
        for role, name, email in (
            ("author", author, author_email),
            ("committer", committer, committer_email),
        ):
            if PRIVATE_EMAIL.fullmatch(email):
                failures.append(f"{role} private email in {commit[:12]} ({name})")

    object_paths = run("rev-list", "--objects", args.ref).splitlines()
    for entry in object_paths:
        path = entry.split(" ", 1)[1] if " " in entry else ""
        if set(Path(path).parts) & FORBIDDEN_PATH_PARTS:
            failures.append(f"reachable history object contains private/runtime path: {path}")

    if failures:
        print("PUBLIC_RELEASE_AUDIT=FAIL")
        for failure in sorted(set(failures)):
            print(f"- {failure}")
        return 1

    print(f"PUBLIC_RELEASE_AUDIT=PASS ref={args.ref} files={len(files)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
