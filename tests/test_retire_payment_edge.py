"""scripts/retire_payment_edge.sh with stand-ins for ufw, apt, dpkg, docker and systemd.

Stage 2 removes what is left of the old payment relay. It must leave alone the
database tunnel, which uses the same udp port, and refuse while anything of
the old chain is still running.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "retire_payment_edge.sh"

pytestmark = pytest.mark.skipif(
    os.geteuid() != 0, reason="the script refuses to run without root"
)

UFW = r"""#!/bin/bash
RULES="$T/ufw.rules"
if [[ "$1" == status ]]; then
  echo "Status: active"
  n=1
  while IFS= read -r rule; do
    printf '[%2d] %s\n' "$n" "$rule"
    n=$((n + 1))
  done < "$RULES"
elif [[ "$1" == "--force" && "$2" == delete ]]; then
  sed -i "${3}d" "$RULES"
fi
"""
STUBS = {
    "ufw": UFW,
    "systemctl": """#!/bin/bash
echo "systemctl $*" >> "$T/calls.log"
if [[ "$1" == is-active ]]; then
  if grep -qxF "$2" "$T/active" 2>/dev/null; then
    echo active
  else
    echo inactive
    exit 3
  fi
fi
exit 0
""",
    "dpkg": '#!/bin/bash\n[[ "$1" == "-s" ]] && [[ -f "$T/pkg-$2" ]]\n',
    "apt-get": """#!/bin/bash
echo "apt-get $*" >> "$T/calls.log"
[[ "$1" == purge ]] && rm -f "$T/pkg-squid"
exit 0
""",
    "docker": '#!/bin/bash\necho "$T/bot"\n',
    "ip": '#!/bin/bash\necho "2: eth0    inet ${FAKE_ADDR}/24 scope global eth0"\n',
}
UFW_RULES = """22/tcp                     ALLOW IN    Anywhere
443/tcp                    ALLOW IN    Anywhere
45.138.157.65 51820/udp    ALLOW OUT   Anywhere
18081/tcp on wg-payments   ALLOW IN    10.203.0.1
3128/tcp                   ALLOW IN    45.138.157.65
2096/tcp                   ALLOW IN    45.138.157.65
"""


class Box:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.etc = tmp / "etc"
        self.home = tmp / "home"
        for folder in ("node-payment-edge", "wireguard", "systemd/system", "nginx"):
            (self.etc / folder).mkdir(parents=True)
        self.home.mkdir()
        (tmp / "bot").mkdir()
        (tmp / "bot" / ".env").write_text("BOT_USERNAME=test_bot\n")
        (self.etc / "node-payment-edge" / "squid.conf").write_text("old\n")
        self.old_tunnel = self.etc / "wireguard" / "wg-payments.conf"
        self.old_tunnel.write_text("[Interface]\nPrivateKey = old\n")
        # The database tunnel: same folder, must survive.
        self.db_tunnel = self.etc / "wireguard" / "wg-db.conf"
        self.db_tunnel.write_text("[Interface]\nPrivateKey = current\n")
        for unit in ("node-payment-backend.service", "node-payment-proxy.service"):
            (self.etc / "systemd" / "system" / unit).write_text("[Unit]\n")
        (self.etc / "nginx" / "pay-callback.conf.retired").write_text("old\n")
        (self.home / "ru3128.txt").write_text("capture\n")
        (tmp / "pkg-squid").write_text("")
        (tmp / "ufw.rules").write_text(UFW_RULES)
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        for name, body in STUBS.items():
            path = bin_dir / name
            path.write_text(body)
            path.chmod(0o755)
        self.env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "T": str(tmp),
            "ETC_DIR": str(self.etc),
            "ROOT_HOME": str(self.home),
            "FAKE_ADDR": "89.125.210.172",
        }

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            ["/bin/bash", str(SCRIPT), *args],
            env={**self.env, **env},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def rules(self) -> str:
        return (self.tmp / "ufw.rules").read_text()


@pytest.fixture
def box(tmp_path: Path) -> Box:
    return Box(tmp_path)


RU = {"FAKE_ADDR": "45.138.157.65"}


def test_report_lists_what_is_left_and_removes_nothing(box: Box) -> None:
    result = box.run()
    assert result.returncode == 0
    assert "(de)" in result.stdout
    assert "the squid package" in result.stdout
    assert "18081/tcp on wg-payments" in result.stdout
    assert "wg-payments.conf" in result.stdout
    assert "51820" not in result.stdout
    assert result.stdout.rstrip().endswith("RETIRE_REPORT_DONE")
    assert box.old_tunnel.exists()
    assert box.rules() == UFW_RULES


def test_de_loses_the_relay_and_keeps_the_database_tunnel(box: Box) -> None:
    result = box.run("--apply")
    assert result.returncode == 0, result.stderr
    assert "RETIRE_DONE (de)" in result.stdout
    rules = box.rules()
    assert "18081" not in rules
    assert "3128" not in rules
    # The database tunnel's rule and everything else stay.
    assert "45.138.157.65 51820/udp    ALLOW OUT" in rules
    assert rules.count("\n") == 4
    assert not box.old_tunnel.exists()
    assert box.db_tunnel.read_text().endswith("PrivateKey = current\n")
    assert not (box.etc / "node-payment-edge").exists()
    assert not (box.etc / "systemd/system/node-payment-backend.service").exists()
    assert not (box.tmp / "pkg-squid").exists()
    # RU's own leftovers are not this server's business.
    assert (box.etc / "systemd/system/node-payment-proxy.service").exists()
    assert (box.home / "ru3128.txt").exists()
    again = box.run()
    assert "nothing is left" in again.stdout
    assert box.run("--apply").returncode == 0


def test_ru_loses_its_side(box: Box) -> None:
    result = box.run("--apply", **RU)
    assert result.returncode == 0, result.stderr
    assert "RETIRE_DONE (ru)" in result.stdout
    assert not (box.etc / "systemd/system/node-payment-proxy.service").exists()
    assert not (box.etc / "nginx/pay-callback.conf.retired").exists()
    assert not (box.home / "ru3128.txt").exists()
    assert not box.old_tunnel.exists()
    assert box.db_tunnel.exists()
    # ufw belongs to DE: RU's firewall is not touched here.
    assert box.rules() == UFW_RULES


@pytest.mark.parametrize(
    "running", ["node-payment-backend.service", "wg-quick@wg-payments", "squid"]
)
def test_nothing_is_removed_while_the_old_chain_runs(box: Box, running: str) -> None:
    (box.tmp / "active").write_text(running + "\n")
    result = box.run("--apply")
    assert result.returncode == 1
    assert f"{running} is still running" in result.stderr
    assert "Nothing was removed" in result.stderr
    assert box.old_tunnel.exists()
    assert box.rules() == UFW_RULES
    assert "IN USE" in box.run().stdout


@pytest.mark.parametrize(
    "setting",
    [
        "YOOKASSA_PROXY_URL=http://10.203.0.1:18080",
        "YOOKASSA_EGRESS=ru_gateway",
        "TELEGRAM_PROXY_URL=http://10.203.0.2:3128",
    ],
)
def test_nothing_is_removed_while_the_bot_still_uses_it(box: Box, setting: str) -> None:
    (box.tmp / "bot" / ".env").write_text(f"BOT_USERNAME=test_bot\n{setting}\n")
    result = box.run("--apply")
    assert result.returncode == 1
    assert "the bot is still set to use the relay or the proxy" in result.stderr
    assert box.rules() == UFW_RULES
    # An empty setting is not a use.
    (box.tmp / "bot" / ".env").write_text("YOOKASSA_PROXY_URL=\nTELEGRAM_PROXY_URL=\n")
    assert box.run("--apply").returncode == 0


def test_a_server_that_is_neither_is_refused(box: Box) -> None:
    result = box.run("--apply", FAKE_ADDR="203.0.113.9")
    assert result.returncode == 1
    assert "run it on RU or DE" in result.stderr
    assert box.run("--nope").returncode == 1
