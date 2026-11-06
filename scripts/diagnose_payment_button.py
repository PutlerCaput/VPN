#!/usr/bin/env python3
"""Read deployed payment handlers and a redacted summary of recent errors.

Docker is invoked only with fixed, read-only argument arrays and no shell.
"""

from __future__ import annotations

import json
import re
import subprocess  # nosec B404
import sys
from collections import Counter
from typing import Any

CONTAINER = "node-rental-bot-1"
INSPECT_CODE = """
import ast, hashlib, json
from pathlib import Path
selection = {
    'handlers/payments.py': {'CHANNEL_TEXT', 'show_channels', 'menu_pay'},
    'keyboards/main_menu.py': {'BTN_PAY', 'main_menu_kb', 'channel_kb'},
    'handlers/__init__.py': {'build_root_router'},
}
for name, wanted in selection.items():
    raw = Path('/app', name).read_bytes()
    source = raw.decode('utf-8-sig')
    blocks = []
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name not in wanted:
                continue
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            blocks.append('\\n'.join(source.splitlines()[start-1:node.end_lineno]))
        elif isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id in wanted for t in node.targets):
                blocks.append(ast.get_source_segment(source, node))
    print(json.dumps({'file': name, 'sha256': hashlib.sha256(raw).hexdigest(),
                      'code': '\\n\\n'.join(blocks)}, ensure_ascii=False))
"""


def redact_code(text: str) -> str:
    text = re.sub(r"\b\d{5,15}:[A-Za-z0-9_-]{20,}\b", "[TOKEN]", text)
    text = re.sub(r"\b(?:live|test)_[A-Za-z0-9_-]{10,}\b", "[KEY]", text)
    return re.sub(r"https?://[^\s\"'<>]+", "[URL]", text)


def error_summary(lines: str) -> dict[str, Any]:
    types: Counter[str] = Counter()
    frames: set[str] = set()
    hints: set[str] = set()
    patterns = {
        "telegram_html_error": ("can't parse entities", "cannot parse entities"),
        "telegram_keyboard_error": ("button_data_invalid", "button_url_invalid"),
        "telegram_polling_conflict": ("terminated by other getupdates",),
        "telegram_auth_error": ("telegramunauthorizederror",),
        "telegram_network_error": ("telegramnetworkerror",),
        "database_locked": ("database is locked",),
    }
    for line in lines.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            record = None
        parts = [line]
        if isinstance(record, dict):
            parts.extend(v for v in record.values() if isinstance(v, str))
        text = "\n".join(parts)
        for kind in set(re.findall(r"\b([A-Za-z_]{1,70}(?:Error|Exception))\b", text)):
            types[kind] += 1
        for name, number, function in re.findall(
            r'File "/app/([A-Za-z0-9_/.-]+\.py)", line (\d+), in ([A-Za-z0-9_]+)',
            text,
        ):
            frames.add(f"{name}:{number}:{function}")
        lowered = text.lower()
        for hint, values in patterns.items():
            if any(value in lowered for value in values):
                hints.add(hint)
    return {
        "error_types": dict(types),
        "app_frames": sorted(frames),
        "hints": sorted(hints),
    }


def run() -> None:
    inspected = subprocess.run(  # noqa: S603  # nosec
        ["docker", "exec", "-i", CONTAINER, "python", "-"],  # noqa: S607
        input=INSPECT_CODE,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if inspected.returncode:
        raise RuntimeError("SOURCE_READ_FAILED")
    for line in inspected.stdout.splitlines():
        record = json.loads(line)
        print(f"FILE={record['file']} SHA256={record['sha256']}")
        print(redact_code(record["code"]))
    logs = subprocess.run(  # noqa: S603  # nosec
        ["docker", "logs", "--since", "5m", "--tail", "300", CONTAINER],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if logs.returncode:
        raise RuntimeError("LOG_READ_FAILED")
    print(
        "RECENT_ERRORS=" + json.dumps(error_summary(logs.stdout + "\n" + logs.stderr))
    )
    print("PAYMENT_BUTTON_DIAGNOSTIC_OK")


if __name__ == "__main__":
    try:
        run()
    except Exception as error:
        print("DIAGNOSTIC_FAILED=" + type(error).__name__)
        sys.exit(1)
