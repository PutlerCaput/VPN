"""scripts/db_tunnel.sh with stand-ins for wg, ip, ss, ping and systemctl.

The tunnel carries the bot's database traffic between the two servers. The
script must put the right role on the right server, never show a private key,
and refuse the easy mistakes: the wrong key, a taken port, a taken address.
"""

from __future__ import annotations

import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "db_tunnel.sh"
PRIVATE = "cPrivatePrivatePrivatePrivatePrivatePrivate0="
PUBLIC = "aPublicPublicPublicPublicPublicPublicPublic="
OTHER = "bOtherOtherOtherOtherOtherOtherOtherOtherOt="

pytestmark = pytest.mark.skipif(
    os.geteuid() != 0, reason="the script refuses to run without root"
)

STUBS = {
    "wg": f"""#!/bin/bash
case "$1" in
  genkey) echo "{PRIVATE}" ;;
  pubkey) cat >/dev/null; echo "{PUBLIC}" ;;
  show) [[ -n "$FAKE_HANDSHAKE" ]] && printf 'peer\\t%s\\n' "$FAKE_HANDSHAKE" ;;
esac
""",
    "wg-quick": "#!/bin/bash\nexit 0\n",
    "systemctl": '#!/bin/bash\necho "systemctl $*" >> "$CALLS"\n',
    "ip": """#!/bin/bash
if [[ "$*" == "-4 -o addr show" ]]; then
  n=1
  for pair in $FAKE_ADDRS; do
    echo "$n: ${pair%%=*}    inet ${pair#*=}/24 scope global ${pair%%=*}"
    n=$((n + 1))
  done
elif [[ "$1" == link ]]; then
  [[ "$FAKE_LINK" == up ]]
fi
""",
    "ss": """#!/bin/bash
for p in $FAKE_UDP; do echo "UNCONN 0 0 0.0.0.0:$p 0.0.0.0:*"; done
""",
    "ping": '#!/bin/bash\n[[ "$FAKE_PING" == ok ]]\n',
}


class Box:
    def __init__(self, tmp: Path) -> None:
        self.wg_dir = tmp / "wireguard"
        self.calls = tmp / "calls.log"
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        for name, body in STUBS.items():
            path = bin_dir / name
            path.write_text(body)
            path.chmod(0o755)
        self.env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "WG_DIR": str(self.wg_dir),
            "CALLS": str(self.calls),
            "FAKE_ADDRS": "eth0=45.138.157.65",
            "FAKE_LINK": "down",
            "FAKE_HANDSHAKE": "",
            "FAKE_UDP": "",
            "FAKE_PING": "",
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

    def config(self) -> str:
        return (self.wg_dir / "wg-db.conf").read_text()

    def systemctl(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []


@pytest.fixture
def box(tmp_path: Path) -> Box:
    return Box(tmp_path)


DE = {"FAKE_ADDRS": "eth0=89.125.210.172 docker0=172.17.0.1"}


def test_report_changes_nothing(box: Box) -> None:
    result = box.run()
    assert result.returncode == 0
    assert "this server: ru" in result.stdout
    assert "not created yet" in result.stdout
    assert result.stdout.rstrip().endswith("DB_TUNNEL_REPORT_DONE")
    assert not box.wg_dir.exists()
    assert box.systemctl() == []


def test_it_knows_which_server_it_is(box: Box) -> None:
    assert "this server: de" in box.run(**DE).stdout
    stranger = box.run(FAKE_ADDRS="eth0=203.0.113.9")
    assert stranger.returncode == 1
    assert "DB_TUNNEL_STOPPED" in stranger.stderr
    # A look-alike address is not this server.
    assert box.run(FAKE_ADDRS="eth0=45.138.157.6").returncode == 1


def test_key_is_made_once_and_only_the_public_half_is_shown(box: Box) -> None:
    first = box.run("--key")
    assert first.returncode == 0
    assert f"DB_TUNNEL_PUBLIC_KEY ru {PUBLIC}" in first.stdout
    assert PRIVATE not in first.stdout + first.stderr
    key = box.wg_dir / "wg-db.key"
    assert key.read_text().strip() == PRIVATE
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    key.write_text("kept\n")
    box.run("--key")
    assert key.read_text() == "kept\n"


def test_ru_listens_and_admits_only_the_de_address(box: Box) -> None:
    box.run("--key")
    result = box.run("--up", OTHER)
    assert result.returncode == 0, result.stderr
    assert "DB_TUNNEL_UP" in result.stdout
    assert PRIVATE not in result.stdout + result.stderr
    config = box.config()
    assert "Address = 10.204.0.1/30" in config
    assert "ListenPort = 51820" in config
    assert f"PublicKey = {OTHER}" in config
    assert "AllowedIPs = 10.204.0.2/32" in config
    assert "Endpoint" not in config
    assert stat.S_IMODE((box.wg_dir / "wg-db.conf").stat().st_mode) == 0o600
    assert box.systemctl() == [
        "systemctl enable wg-quick@wg-db",
        "systemctl restart wg-quick@wg-db",
    ]


def test_de_dials_out_and_keeps_the_tunnel_alive(box: Box) -> None:
    box.run("--key", **DE)
    assert box.run("--up", OTHER, **DE).returncode == 0
    config = box.config()
    assert "Address = 10.204.0.2/30" in config
    assert "ListenPort" not in config
    assert "AllowedIPs = 10.204.0.1/32" in config
    assert "Endpoint = 45.138.157.65:51820" in config
    assert "PersistentKeepalive = 25" in config


@pytest.mark.parametrize(
    ("key", "env", "reason"),
    [
        ("not-a-key", {}, "not a WireGuard public key"),
        (OTHER + "x", {}, "not a WireGuard public key"),
        (f"{OTHER}; reboot", {}, "not a WireGuard public key"),
        (PUBLIC, {}, "own key"),
        (OTHER, {"FAKE_ADDRS": "eth0=45.138.157.65 wg-old=10.204.0.9"}, "already used"),
        (OTHER, {"FAKE_UDP": "51820"}, "already taken"),
    ],
)
def test_mistakes_are_refused_before_anything_is_written(
    box: Box, key: str, env: dict[str, str], reason: str
) -> None:
    box.run("--key")
    result = box.run("--up", key, **env)
    assert result.returncode == 1
    assert reason in result.stderr
    assert not (box.wg_dir / "wg-db.conf").exists()
    assert box.systemctl() == []


def test_up_needs_this_sides_key_first(box: Box) -> None:
    result = box.run("--up", OTHER)
    assert result.returncode == 1
    assert "--key first" in result.stderr


def test_its_own_address_and_port_do_not_block_a_restart(box: Box) -> None:
    box.run("--key")
    result = box.run(
        "--up",
        OTHER,
        FAKE_ADDRS="eth0=45.138.157.65 wg-db=10.204.0.1",
        FAKE_UDP="51820",
        FAKE_LINK="up",
    )
    assert result.returncode == 0, result.stderr


def test_status(box: Box) -> None:
    assert box.run("--status").stdout.startswith("WARN tunnel: wg-db is down")
    waiting = box.run("--status", FAKE_LINK="up")
    assert "has not answered yet" in waiting.stdout
    fresh = box.run(
        "--status", FAKE_LINK="up", FAKE_HANDSHAKE=str(int(time.time()) - 20)
    )
    assert fresh.stdout.startswith("OK tunnel")
    stale = box.run(
        "--status", FAKE_LINK="up", FAKE_HANDSHAKE=str(int(time.time()) - 900)
    )
    assert stale.stdout.startswith("WARN tunnel: last handshake")


def test_down_keeps_keys_and_config(box: Box) -> None:
    box.run("--key")
    box.run("--up", OTHER)
    result = box.run("--down")
    assert "DB_TUNNEL_DOWN" in result.stdout
    assert box.systemctl()[-1] == "systemctl disable --now wg-quick@wg-db"
    assert (box.wg_dir / "wg-db.key").exists()
    assert (box.wg_dir / "wg-db.conf").exists()


def test_unknown_arguments_are_refused(box: Box) -> None:
    for args in (["--up"], ["--up", OTHER, "extra"], ["--nope"]):
        result = box.run(*args)
        assert result.returncode == 1
        assert "usage" in result.stderr
