"""scripts/awg_endpoint.sh: the address AmneziaWG configs connect to."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "awg_endpoint.sh"

FAKE_DOCKER = """#!/bin/sh
echo "$*" >> "$FAKE_LOG"
case "$1" in
  inspect) echo "$FAKE_HEALTH" ;;
  exec) echo "running" ;;
esac
exit 0
"""


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    awg_dir = tmp_path / "awg"
    awg_dir.mkdir()
    (awg_dir / ".env").write_text(
        "AWG_PORT=39051\nAWG_SUBNET=10.67.0.0/24\n"
        "AWG_ENDPOINT_HOST=89.125.210.172\nBOT_NETWORK=bot\n",
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER, encoding="utf-8")
    docker.chmod(0o755)
    return {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "AWG_DIR": str(awg_dir),
        "FAKE_LOG": str(tmp_path / "docker.log"),
        "FAKE_HEALTH": "healthy",
        "WAIT_SECONDS": "3",
        "RELEASE_GUARD_BIN": str(tmp_path / "no-guard"),
    }


def run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- fixed test script
        ["/bin/bash", str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def endpoint(env: dict[str, str]) -> str:
    text = (Path(env["AWG_DIR"]) / ".env").read_text(encoding="utf-8")
    return next(
        line.split("=", 1)[1]
        for line in text.splitlines()
        if line.startswith("AWG_ENDPOINT_HOST=")
    )


def test_without_arguments_it_only_shows(env: dict[str, str]) -> None:
    result = run(env)
    assert result.returncode == 0, result.stderr
    assert "89.125.210.172:39051" in result.stdout
    assert endpoint(env) == "89.125.210.172"


def test_set_switches_new_configs_to_the_relay(env: dict[str, str]) -> None:
    result = run(env, "--set", "45.138.157.65")
    assert result.returncode == 0, result.stderr
    assert endpoint(env) == "45.138.157.65"
    assert "AWG_ENDPOINT_OK" in result.stdout
    assert "compose -p cyberdefency-awg up -d" in Path(env["FAKE_LOG"]).read_text(
        encoding="utf-8"
    )


def test_an_unhealthy_container_puts_the_old_address_back(env: dict[str, str]) -> None:
    env["FAKE_HEALTH"] = "unhealthy"
    result = run(env, "--set", "45.138.157.65")
    assert result.returncode == 1
    assert "previous setting" in result.stderr
    assert endpoint(env) == "89.125.210.172"


def test_private_and_malformed_addresses_are_refused(env: dict[str, str]) -> None:
    assert run(env, "--set", "10.0.0.5").returncode == 1
    assert run(env, "--set", "bad host;rm").returncode == 1
    assert endpoint(env) == "89.125.210.172"
