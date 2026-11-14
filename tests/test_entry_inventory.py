"""The entry inventory tells the shape of the inbounds and never a secret."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "entry_inventory.py"

SECRETS = ("PRIVATE-KEY-VALUE", "abcd1234", "11111111-2222-3333-4444-555555555555")

CONFIG = {
    "inbounds": [
        {
            "tag": "VLESS TCP REALITY",
            "listen": "0.0.0.0",  # noqa: S104
            "port": 2096,
            "protocol": "vless",
            "settings": {
                "clients": [{"id": SECRETS[2], "flow": "xtls-rprx-vision"}],
                "decryption": "none",
            },
            "streamSettings": {
                "network": "tcp",
                "security": "reality",
                "realitySettings": {
                    "dest": "www.example.com:443",
                    "serverNames": ["www.example.com"],
                    "privateKey": SECRETS[0],
                    "shortIds": [SECRETS[1]],
                },
            },
        },
        {
            "tag": "api",
            "listen": "127.0.0.1",
            "port": 62789,
            "protocol": "dokodemo-door",
        },
    ],
    "outbounds": [{"protocol": "freedom", "tag": "DIRECT"}, {"tag": "BLOCK"}],
}


def run(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPT), str(path)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_it_lists_the_inbounds_and_prints_no_secret(tmp_path: Path) -> None:
    path = tmp_path / "xray_config.json"
    path.write_text(json.dumps(CONFIG))
    result = run(path)
    assert result.returncode == 0, result.stdout
    out = result.stdout
    assert "inbound VLESS TCP REALITY: vless 0.0.0.0:2096 network=tcp" in out
    assert "security=reality clients=1 flow=xtls-rprx-vision" in out
    assert "reality cover names: www.example.com" in out
    assert "reality target: www.example.com:443" in out
    assert "reality private key: set" in out
    assert "reality short ids: 1" in out
    assert "inbound api: dokodemo-door 127.0.0.1:62789" in out
    assert "outbounds: DIRECT, BLOCK" in out
    for secret in SECRETS:
        assert secret not in out


def test_an_unreadable_config_is_said_plainly(tmp_path: Path) -> None:
    result = run(tmp_path / "missing.json")
    assert result.returncode == 1
    assert "ENTRY_INVENTORY_STOPPED" in result.stdout
