"""scripts/net_tuning.sh: BBR, fq and the eight sysctl settings from the audit."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "net_tuning.sh"

TUNING = """\
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
net.ipv4.tcp_rmem = 4096 87380 16777216
net.ipv4.tcp_wmem = 4096 65536 16777216
net.ipv4.tcp_mtu_probing = 1
net.ipv4.tcp_fastopen = 3
"""

FAKE_SYSCTL = r"""#!/usr/bin/env python3
import os, sys
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write("sysctl " + " ".join(sys.argv[1:]) + "\n")
args = sys.argv[1:]
if args == ["--system"]:
    raise SystemExit(0)
if len(args) == 2 and args[0] == "-n":
    print({
        "net.core.default_qdisc": "fq",
        "net.ipv4.tcp_congestion_control": "bbr",
        "net.core.rmem_max": "16777216",
        "net.core.wmem_max": "16777216",
        "net.ipv4.tcp_rmem": "4096 87380 16777216",
        "net.ipv4.tcp_wmem": "4096 65536 16777216",
        "net.ipv4.tcp_mtu_probing": "1",
        "net.ipv4.tcp_fastopen": "3",
    }.get(args[1], "unknown"))
    raise SystemExit(0)
raise SystemExit(1)
"""

FAKE_TC = r"""#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write("tc " + " ".join(args) + "\n")
if args[:3] == ["qdisc", "show", "dev"]:
    print("qdisc fq 8001: root refcnt 2 limit 10000p")
    raise SystemExit(0)
if args[:4] == ["qdisc", "replace", "dev", "eth0"] and args[4:] == ["root", "fq"]:
    raise SystemExit(0)
raise SystemExit(1)
"""

FAKE_IP = """#!/bin/sh
echo 'default via 10.0.0.1 dev eth0 proto dhcp src 10.0.0.8 metric 100'
"""

FAKE_MODPROBE = r"""#!/usr/bin/env python3
import os, sys
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write("modprobe " + " ".join(sys.argv[1:]) + "\n")
raise SystemExit(0 if sys.argv[1:] == ["tcp_bbr"] else 1)
"""


def write_exe(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def env_for(tmp_path: Path) -> dict[str, str]:
    log = tmp_path / "log"
    log.write_text("", encoding="utf-8")
    return {
        **os.environ,
        "FAKE_LOG": str(log),
        "SYSCTL_BIN": str(write_exe(tmp_path / "sysctl", FAKE_SYSCTL)),
        "TC_BIN": str(write_exe(tmp_path / "tc", FAKE_TC)),
        "IP_BIN": str(write_exe(tmp_path / "ip", FAKE_IP)),
        "MODPROBE_BIN": str(write_exe(tmp_path / "modprobe", FAKE_MODPROBE)),
        "SYSCTL_CONF": str(tmp_path / "sysctl.d" / "90-vpn-tuning.conf"),
        "MODULES_CONF": str(tmp_path / "modules-load.d" / "bbr.conf"),
    }


def run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- fixed test script
        ["/bin/bash", str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_check_prints_values_and_changes_nothing(tmp_path: Path) -> None:
    env = env_for(tmp_path)
    result = run(env, "--check")
    assert result.returncode == 0, result.stderr
    assert "NET_TUNING_CHECK_OK: nothing changed" in result.stdout
    assert "net.ipv4.tcp_congestion_control = bbr" in result.stdout
    assert "qdisc(eth0): qdisc fq 8001: root refcnt 2 limit 10000p" in result.stdout
    assert not Path(env["SYSCTL_CONF"]).exists()
    assert not Path(env["MODULES_CONF"]).exists()
    log = Path(env["FAKE_LOG"]).read_text(encoding="utf-8")
    assert "sysctl --system" not in log
    assert "modprobe" not in log
    assert "qdisc replace" not in log


def test_apply_writes_the_eight_settings_and_fq(tmp_path: Path) -> None:
    env = env_for(tmp_path)
    result = run(env, "--apply")
    assert result.returncode == 0, result.stderr
    assert Path(env["SYSCTL_CONF"]).read_text(encoding="utf-8") == TUNING
    assert Path(env["MODULES_CONF"]).read_text(encoding="utf-8") == "tcp_bbr\n"
    log = Path(env["FAKE_LOG"]).read_text(encoding="utf-8")
    assert "modprobe tcp_bbr" in log
    assert "sysctl --system" in log
    assert "tc qdisc replace dev eth0 root fq" in log
    assert "congestion_control: bbr" in result.stdout
    assert "qdisc(eth0): qdisc fq 8001: root refcnt 2 limit 10000p" in result.stdout
    assert "NET_TUNING_OK: bbr on eth0" in result.stdout
    again = run(env)
    assert again.returncode == 0, again.stderr
    assert Path(env["SYSCTL_CONF"]).read_text(encoding="utf-8") == TUNING
    assert Path(env["MODULES_CONF"]).read_text(encoding="utf-8") == "tcp_bbr\n"
