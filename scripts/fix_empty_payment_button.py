#!/usr/bin/env python3
"""Repair the confirmed empty payment button on the owner's German host.

Docker is invoked only with validated argument arrays and no shell.
"""

from __future__ import annotations

import ast
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess  # nosec B404
import sys
import tempfile
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT = Path("/root/secure-node-bot/node-rental/node-rental")
CONTAINER = "node-rental-bot-1"
IMAGE = "node-rental-bot:latest"
MODULE = Path("keyboards/main_menu.py")
COMPOSE = [
    "docker",
    "compose",
    "--project-directory",
    str(PROJECT),
    "-f",
    str(PROJECT / "docker-compose.yml"),
]


def remove_empty_row(source: str) -> str:
    functions = [
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name == "channel_kb"
    ]
    if len(functions) != 1:
        raise RuntimeError("CHANNEL_FUNCTION_NOT_UNIQUE")
    function = functions[0]
    lines = source.splitlines(keepends=True)
    before = "".join(lines[: function.lineno - 1])
    body = "".join(lines[function.lineno - 1 : function.end_lineno])
    after = "".join(lines[function.end_lineno :])
    pattern = (
        r"(?m)^[ \t]*\[\r?\n[ \t]*InlineKeyboardButton\(\s*\)"
        r"[ \t]*\r?\n[ \t]*\],[ \t]*\r?\n"
    )
    corrected, count = re.subn(pattern, "", body)
    if count != 1:
        raise RuntimeError("EXPECTED_ONE_EMPTY_BUTTON_ROW")
    result = before + corrected + after
    compile(result, str(MODULE), "exec")
    return result


def command(*args: str, timeout: int = 60, operation: str = "docker") -> str:
    result = subprocess.run(  # noqa: S603  # nosec
        list(args),
        cwd=PROJECT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"COMMAND_FAILED:{operation}:{result.returncode}")
    return result.stdout.strip()


def compose_hash() -> str:
    return command(*COMPOSE, "config", "--hash", "bot").split()[-1]


def restart() -> None:
    command(
        *COMPOSE,
        "up",
        "-d",
        "--no-deps",
        "--no-build",
        "--pull",
        "never",
        "--force-recreate",
        "--timeout",
        "60",
        "bot",
        timeout=180,
    )


def verify_running(expected_image: str) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + 90
    last_error = "state"
    while time.monotonic() < deadline:
        try:
            with opener.open("http://127.0.0.1:8080/healthz", timeout=3) as reply:
                health = json.loads(reply.read(4096))
            state = json.loads(
                command("docker", "inspect", "--format", "{{json .State}}", CONTAINER)
            )
            image = command("docker", "inspect", "--format", "{{.Image}}", CONTAINER)
            restarts = command(
                "docker", "inspect", "--format", "{{.RestartCount}}", CONTAINER
            )
            if (
                image == expected_image
                and health.get("db") is True
                and health.get("status") == "ok"
                and state["Running"]
                and not state["OOMKilled"]
                and restarts == "0"
            ):
                return
        except Exception as error:
            last_error = type(error).__name__
        time.sleep(2)
    raise RuntimeError("BOT_HEALTH_CHECK_FAILED:" + last_error)


def replace_source(source: Path, content: bytes) -> None:
    info = source.stat()
    fd, name = tempfile.mkstemp(prefix=".payment-menu-", dir=source.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, info.st_mode & 0o777)
        os.chown(temporary, info.st_uid, info.st_gid)
        os.replace(temporary, source)
    finally:
        temporary.unlink(missing_ok=True)


def rollback(stage: Path, plan: dict[str, Any]) -> None:
    try:
        current = command("docker", "inspect", "--format", "{{.Image}}", CONTAINER)
    except RuntimeError:
        current = None
    latest = command("docker", "image", "inspect", "--format", "{{.Id}}", IMAGE)
    if current not in (None, plan["old_image"], plan["new_image"]) or latest not in (
        plan["old_image"],
        plan["new_image"],
    ):
        raise RuntimeError("ROLLBACK_REFUSED_DIFFERENT_DEPLOYMENT")
    if compose_hash() != plan["compose_hash"]:
        raise RuntimeError("ROLLBACK_REFUSED_COMPOSE_CHANGED")
    source = PROJECT / MODULE
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if digest not in (plan["old_sha256"], plan["new_sha256"]):
        raise RuntimeError("ROLLBACK_REFUSED_SOURCE_CHANGED")
    command("docker", "image", "tag", plan["old_image"], IMAGE)
    restart()
    verify_running(plan["old_image"])
    replace_source(source, (stage / "main_menu.before.py").read_bytes())
    print("PAYMENT_MENU_ROLLBACK_OK")


def run() -> None:
    if os.geteuid() != 0 or not PROJECT.is_dir():
        raise RuntimeError("RUN_AS_ROOT_ON_GERMAN_BOT_SERVER")
    os.umask(0o077)
    with Path("/run/lock/node-payment-menu.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if len(sys.argv) == 3 and sys.argv[1] == "--rollback":
            stage = Path(sys.argv[2]).resolve()
            rollback(stage, json.loads((stage / "plan.json").read_text()))
            return
        if len(sys.argv) != 1:
            raise RuntimeError("UNEXPECTED_ARGUMENTS")
        old_image = command("docker", "inspect", "--format", "{{.Image}}", CONTAINER)
        source = PROJECT / MODULE
        original = source.read_bytes()
        live = command("docker", "exec", CONTAINER, "cat", "/app/" + str(MODULE))
        if original.decode().strip() != live:
            raise RuntimeError("SOURCE_DIFFERS_FROM_RUNNING_IMAGE")
        new = remove_empty_row(original.decode()).encode()
        digest = compose_hash()
        label = command(
            "docker",
            "inspect",
            "--format",
            '{{index .Config.Labels "com.docker.compose.config-hash"}}',
            CONTAINER,
        )
        if digest != label:
            raise RuntimeError("COMPOSE_DIFFERS_FROM_RUNNING_CONTAINER")
        if (
            command("docker", "image", "inspect", "--format", "{{.Id}}", IMAGE)
            != old_image
        ):
            raise RuntimeError("LATEST_TAG_DIFFERS_FROM_RUNNING_IMAGE")
        replicas = command(
            "docker",
            "ps",
            "-aq",
            "--filter",
            "label=com.docker.compose.project=node-rental",
            "--filter",
            "label=com.docker.compose.service=bot",
        ).splitlines()
        if len(replicas) != 1:
            raise RuntimeError("BOT_IS_NOT_A_SINGLETON")
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        stage = Path(tempfile.mkdtemp(prefix=f"vpn-payment-menu-{stamp}-", dir="/root"))
        shutil.copy2(source, stage / "main_menu.before.py")
        print(f"PAYMENT_MENU_WORKDIR={stage}", flush=True)
        shutil.copy2(Path(__file__), stage / "fix.py")
        (stage / "main_menu.py").write_bytes(new)
        (stage / "Dockerfile").write_text(
            "ARG BASE_IMAGE=node-rental-bot:latest\nFROM ${BASE_IMAGE}\n"
            "COPY --chown=1000:1000 main_menu.py /app/keyboards/main_menu.py\n"
        )
        tag = "node-rental-bot:payment-menu-" + stage.name.removeprefix(
            "vpn-payment-menu-"
        )
        rollback_tag = tag + "-rollback"
        command(
            "docker",
            "image",
            "tag",
            old_image,
            rollback_tag,
            operation="rollback_image_tag",
        )
        command(
            "docker",
            "build",
            "--network",
            "none",
            "--pull=false",
            "-t",
            tag,
            "--build-arg",
            "BASE_IMAGE=" + rollback_tag,
            str(stage),
            timeout=180,
            operation="candidate_image_build",
        )
        smoke = """
from keyboards.main_menu import channel_kb
for months, tier in ((None, None), (1, 'basic'), (12, 'prime')):
    keyboard = channel_kb(months, tier)
    assert len(keyboard.inline_keyboard) == 1
    for row in keyboard.inline_keyboard:
        assert len(row) == 1 and row[0].text
        assert row[0].callback_data.startswith('chan:sbp')
print('PAYMENT_MENU_STAGING_OK')
"""
        print(
            command(
                "docker",
                "run",
                "--rm",
                "--pull=never",
                "--network",
                "none",
                "--read-only",
                "--memory",
                "384m",
                "--pids-limit",
                "32",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--entrypoint",
                "python",
                tag,
                "-c",
                smoke,
                operation="offline_keyboard_smoke_test",
            )
        )
        new_image = command("docker", "image", "inspect", "--format", "{{.Id}}", tag)
        plan = {
            "old_image": old_image,
            "new_image": new_image,
            "compose_hash": digest,
            "old_sha256": hashlib.sha256(original).hexdigest(),
            "new_sha256": hashlib.sha256(new).hexdigest(),
        }
        (stage / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
        print(f"ROLLBACK_COMMAND=python3 {stage}/fix.py --rollback {stage}", flush=True)
        if source.read_bytes() != original or compose_hash() != digest:
            raise RuntimeError("SOURCE_OR_COMPOSE_CHANGED_DURING_STAGING")
        if (
            command("docker", "inspect", "--format", "{{.Image}}", CONTAINER)
            != old_image
        ):
            raise RuntimeError("DEPLOYMENT_CHANGED_DURING_STAGING")
        if (
            command("docker", "image", "inspect", "--format", "{{.Id}}", IMAGE)
            != old_image
        ):
            raise RuntimeError("LATEST_TAG_CHANGED_DURING_STAGING")
        try:
            command("docker", "image", "tag", new_image, IMAGE)
            restart()
            verify_running(new_image)
            replace_source(source, new)
        except Exception:
            rollback(stage, plan)
            raise
        print("PAYMENT_MENU_FIX_DEPLOYED")
        print("Payment credentials and other services were not changed.")


if __name__ == "__main__":
    try:
        run()
    except Exception as error:
        print(
            "PAYMENT_MENU_FIX_FAILED: "
            + (str(error) if isinstance(error, RuntimeError) else type(error).__name__)
        )
        sys.exit(1)
