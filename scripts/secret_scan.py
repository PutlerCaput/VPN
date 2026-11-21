#!/usr/bin/env python3
"""Scan Git files or explicit paths; report locations/codes, never values.

Exit 0: no findings; 1: forbidden artifact/credential; 2: incomplete scan.
This is a guard for source changes, not proof that Git history has no secrets.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess  # nosec B404 - only fixed git argv, no shell
import sys
from pathlib import Path
from typing import Any

PRIVATE_DIRS = {"secrets", "diagnostics", "server-backups", "backups", "de-rescue"}
PRIVATE_SUFFIXES = (
    ".db",
    ".db-wal",
    ".db-shm",
    ".sqlite",
    ".sqlite3",
    ".sqlite3-wal",
    ".sqlite3-shm",
    ".sql",
    ".sql.gz",
    ".dump",
    ".backup",
    ".age",
    ".gpg",
    ".tar",
    ".tar.gz",
    ".tgz",
    ".zip",
    ".7z",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
)
KEY_NAME = (
    r"[\w.-]*(?:password|passwd|token|secret|api[_-]?key|private[_-]?key|db[_-]?key)"
)
ASSIGNMENT = re.compile(
    rf"""(?ix)(?<![\w^])['"]?({KEY_NAME})['"]?\s*[:=]\s*
        (?:"([^"\n]*)"|'([^'\n]*)')"""
)
ENV_ASSIGNMENT = re.compile(
    rf"(?ix)^\s*(?:export\s+)?({KEY_NAME})\s*[:=]\s*([A-Za-z0-9_+/:.=@-]+)\s*(?:\#.*)?$"
)
CLI_SECRET = re.compile(
    r"""(?ix)--((?:auth-|access-)?token|password|passwd|secret|api-key|private-key)
        (?:=|\s+)(?:"([^"\n]*)"|'([^'\n]*)'|([^\s"']+))"""
)
PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----|\bAGE-SECRET-KEY-1[A-Z0-9]{20,}"
)
KNOWN_TOKEN = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b"
)
PLACEHOLDER = re.compile(
    r"(?ix)(?:\$\{[^}]+\}|\$[A-Z_][A-Z0-9_]*|<[^>]+>|\{\{.*\}\}|"
    r"(?:test|example|synthetic|placeholder)(?:[-_].*)?|"
    r"(?:change[-_]?me|replace[-_]?me|your[-_].*)|none|null|true|false)"
)


def forbidden_path(path: Path) -> bool:
    parts = [part.lower() for part in path.parts]
    name = path.name.lower()
    if name == ".gitkeep":
        return False
    if any(
        part in PRIVATE_DIRS or part.startswith(("de-rescue-", "restore-"))
        for part in parts
    ):
        return True
    if name.startswith(".env") and name not in {
        ".env.example",
        ".env.sample",
        ".env.template",
    }:
        return True
    return (
        name.endswith(PRIVATE_SUFFIXES)
        or name in {"age-key.txt", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa"}
        or name.startswith("age-secret-key-")
        or (name.startswith("docker-inspect") and name.endswith((".json", ".txt")))
    )


def _literal(value: str) -> bool:
    value = value.strip()
    return (
        len(value) >= 6
        and not PLACEHOLDER.fullmatch(value)
        and not value.startswith(("/", "./", "../", "${", "$", "<"))
    )


def _docker_inspect(value: Any) -> bool:
    if isinstance(value, list):
        return any(_docker_inspect(item) for item in value)
    if not isinstance(value, dict):
        return False
    config = value.get("Config")
    if isinstance(config, dict) and any(
        k in config for k in ("Env", "Cmd", "Entrypoint")
    ):
        return True
    return any(_docker_inspect(item) for item in value.values())


def _reviewed_fixture(path: Path, line: str) -> bool:
    """Only exact reviewed test lines; changing one byte invalidates the waiver."""
    root = Path(__file__).resolve().parents[1]
    try:
        relative = path.resolve().relative_to(root).as_posix()
    except ValueError:
        return False
    if not relative.startswith("tests/"):
        return False
    allowed = json.loads((root / "scripts/secret_scan_allowlist.json").read_text())
    digest = hashlib.sha256(line.encode()).hexdigest()
    return digest in allowed.get(relative, [])


def scan_file(path: Path) -> list[tuple[int, str]]:
    if forbidden_path(path):
        return [(0, "PRIVATE_RUNTIME_ARTIFACT")]
    if path.is_symlink():
        raise ValueError("symlink")
    data = path.read_bytes()
    if data.startswith(b"SQLite format 3\0"):
        return [(0, "DATABASE_CONTENT")]
    if data.startswith((b"PK\x03\x04", b"\x1f\x8b", b"7z\xbc\xaf\x27\x1c")):
        return [(0, "ARCHIVE_CONTENT")]
    binary_asset = path.suffix.lower() in {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".ico",
        ".webp",
        ".woff",
        ".woff2",
        ".ttf",
        ".mp4",
    }
    if b"\0" in data:
        if binary_asset:
            return []
        raise ValueError("unscannable binary")
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        if binary_asset:
            return []
        raise
    try:
        parsed = json.loads(content)
    except (ValueError, RecursionError):
        parsed = None
    if _docker_inspect(parsed):
        return [(0, "DOCKER_INSPECT_CONTENT")]
    findings = []
    for number, line in enumerate(content.splitlines(), 1):
        if PRIVATE_KEY.search(line) or KNOWN_TOKEN.search(line):
            findings.append((number, "PRIVATE_KEY_OR_TOKEN"))
            continue
        patterns = [ASSIGNMENT, CLI_SECRET]
        if path.suffix.lower() not in {".py", ".js", ".ts", ".tsx", ".jsx"}:
            patterns.append(ENV_ASSIGNMENT)
        for pattern in patterns:
            if any(
                _literal(next((v for v in match.groups()[1:] if v is not None), ""))
                for match in pattern.finditer(line)
            ):
                findings.append((number, "CREDENTIAL_LITERAL"))
                break
    lines = content.splitlines()
    return [
        item for item in findings if not _reviewed_fixture(path, lines[item[0] - 1])
    ]


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        if args:
            paths = [Path(arg) for arg in args]
        else:
            root = Path(__file__).resolve().parents[1]
            git = shutil.which("git")
            if git is None:
                raise OSError("git unavailable")
            # Fixed arguments, no shell; the executable is resolved from PATH.
            result = subprocess.run(  # noqa: S603  # nosec B603
                [git, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            paths = [
                root / name.decode("utf-8")
                for name in result.stdout.split(b"\0")
                if name
            ]
        count = 0
        for path in dict.fromkeys(paths):
            findings = scan_file(path)
            for line, code in findings:
                print(
                    f"{json.dumps(str(path), ensure_ascii=True)}:{line}: {code}",
                    file=sys.stderr,
                )
            count += len(findings)
    except (OSError, ValueError, RecursionError, subprocess.CalledProcessError) as exc:
        print(f"SECRET_SCAN_INPUT_ERROR: {type(exc).__name__}", file=sys.stderr)
        return 2
    if count:
        print("SECRET LEAK OR PRIVATE ARTIFACT DETECTED", file=sys.stderr)
        return 1
    print("secret scan: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
