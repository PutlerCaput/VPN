"""scripts/server_cleanup.sh with a stand-in docker: what is removed by itself,
what is only listed, and what is never touched."""

from __future__ import annotations

import json
import os
import platform
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "server_cleanup.sh"
OLD_KERNEL = "linux-image-6.8.0-40-generic"

FAKE_DOCKER = r"""#!/usr/bin/env python3
import json, os, sys
state = json.load(open(os.environ["FAKE_DOCKER_STATE"]))
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write("docker " + " ".join(args) + "\n")
fmt = args[args.index("--format") + 1] if "--format" in args else ""
fmt = args[args.index("-f") + 1] if "-f" in args else fmt
containers = state["containers"]
if args[:1] == ["ps"]:
    if "status=exited" in args:
        for c in containers:
            if not c["running"]:
                print(f"{c['name']} ({c['image']}, Exited (0) 3 weeks ago)")
    elif "{{.Image}}" in fmt:
        print("\n".join(c["image"] for c in containers))
    else:
        print("\n".join(c["name"] for c in containers))
elif args[:1] == ["inspect"]:
    if "working_dir" in fmt:
        print(state["workdir"])
    elif "LogPath" in fmt:
        print(state["logs"].get(args[-1], ""))
elif args[:1] == ["images"]:
    repo = args[-1] if not args[-1].startswith("{{") else None
    for image in state["images"]:
        if repo is None or image.split(":")[0] == repo:
            print(image)
elif args[:2] == ["image", "inspect"]:
    print(123 * 1048576)
elif args[:2] == ["volume", "ls"]:
    print("\n".join(state["volumes"]))
elif args[:1] == ["exec"]:
    print("rows ok")
"""

FAKE_APT_GET = f"""#!/usr/bin/env bash
echo "apt-get $*" >> "$FAKE_LOG"
if [[ "$*" == "-s autoremove" ]]; then
  echo "Remv {OLD_KERNEL} [6.8.0-40.40]"
  echo "Remv linux-image-$(uname -r) [current]"
  echo "Remv libfoo1 [1.0]"
fi
"""


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in {
        "docker": FAKE_DOCKER,
        "apt-get": FAKE_APT_GET,
        "journalctl": '#!/usr/bin/env bash\necho "journalctl $*" >> "$FAKE_LOG"\n',
    }.items():
        (bin_dir / name).write_text(body, encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    workdir = tmp_path / "project"
    (workdir / "data").mkdir(parents=True)
    big_log = tmp_path / "marzban-json.log"
    with big_log.open("wb") as handle:  # sparse: 250 MB on paper, nothing on disk
        handle.truncate(250 * 1024 * 1024)
    small_log = tmp_path / "bot-json.log"
    small_log.write_text("ok\n", encoding="utf-8")
    state = {
        "workdir": str(workdir),
        "containers": [
            {
                "name": "node-rental-bot-1",
                "image": "node-rental-bot:latest",
                "running": True,
            },
            {
                "name": "marzban-marzban-1",
                "image": "gozargah/marzban:v0.8.4",
                "running": True,
            },
            {
                "name": "old-tunnel",
                "image": "cloudflare/cloudflared:2024.1.0",
                "running": False,
            },
        ],
        "images": [
            "node-rental-bot:latest",
            "gozargah/marzban:v0.8.4",
            "gozargah/marzban:v0.8.3",
            "cloudflare/cloudflared:2024.1.0",
            "hello-world:latest",
            "python:3.11-slim-bookworm",
            "caddy:2.11.7-builder",
        ],
        "volumes": ["3f9c0ffee"],
        "logs": {
            "marzban-marzban-1": str(big_log),
            "node-rental-bot-1": str(small_log),
        },
    }
    (tmp_path / "docker.json").write_text(json.dumps(state), encoding="utf-8")
    return {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_DOCKER_STATE": str(tmp_path / "docker.json"),
        "FAKE_LOG": str(tmp_path / "calls.log"),
        "BIG_LOG": str(big_log),
    }


def _run(env: dict[str, str], *args: str) -> tuple[str, list[str]]:
    result = subprocess.run(  # noqa: S603
        ["bash", str(SCRIPT), *args],  # noqa: S607
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    log = Path(env["FAKE_LOG"])
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return result.stdout + result.stderr, calls


def test_report_lists_and_removes_nothing(env: dict[str, str]) -> None:
    out, calls = _run(env)
    assert "REPORT ONLY" in out
    assert "would remove: gozargah/marzban:v0.8.3 (no container uses it" in out
    assert "hello-world:latest (123 MB)" in out  # listed, never removed
    assert "old-tunnel (cloudflare/cloudflared:2024.1.0" in out
    assert "3f9c0ffee" in out
    assert "would empty: marzban-marzban-1 (250 MB)" in out
    assert f"would remove old kernels: {OLD_KERNEL}" in out
    assert platform.release() not in out.split("would remove old kernels:")[1]
    assert "libfoo1" not in out
    assert not [c for c in calls if c.startswith(("docker rmi", "docker rm "))]
    assert not [c for c in calls if "purge" in c or "prune" in c]
    assert Path(env["BIG_LOG"]).stat().st_size == 250 * 1024 * 1024


def test_apply_removes_only_the_safe_part(env: dict[str, str]) -> None:
    out, calls = _run(env, "--apply")
    removed = [c.split()[-1] for c in calls if c.startswith("docker rmi")]
    assert removed == ["gozargah/marzban:v0.8.3"]
    for kept in (
        "gozargah/marzban:v0.8.4",
        "hello-world:latest",
        "python:3.11-slim-bookworm",
        "caddy:2.11.7-builder",
        "cloudflare/cloudflared:2024.1.0",
        "node-rental-bot:latest",
    ):
        assert kept not in removed
    assert not [c for c in calls if c.startswith(("docker rm ", "docker volume rm"))]
    assert "emptied: marzban-marzban-1 (250 MB)" in out
    assert Path(env["BIG_LOG"]).stat().st_size == 0
    purges = [c for c in calls if "purge" in c]
    assert purges == [f"apt-get -y -q purge {OLD_KERNEL}"]
    assert "journalctl --vacuum-time=14d" in calls
