"""Run explicitly with the Hiddify Core version pinned in CI."""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.test_entry_sharding import (
    DE,
    NODE,
    RU_A,
    RU_B,
    Upstream,
    _client,
    _sharded_settings,
    _user,
    _vless,
)

# Synthetic public key bytes. There is no matching server or private key here.
PUBLIC_KEY = base64.urlsafe_b64encode(bytes(range(1, 33))).decode().rstrip("=")
HOSTS = (RU_A, RU_B, DE)
APP_AGENT = "HiddifyNext/5.5.5 (android) like ClashMeta v2ray sing-box"


def _singbox() -> dict[str, Any]:
    return {
        "outbounds": [
            {
                "type": "vless",
                "tag": f"route-{index}",
                "server": host,
                "server_port": 443,
                "uuid": NODE,
                "flow": "xtls-rprx-vision",
                "tls": {
                    "enabled": True,
                    "server_name": "cover.example.org",
                    "reality": {"enabled": True, "public_key": PUBLIC_KEY},
                },
            }
            for index, host in enumerate(HOSTS)
        ]
    }


def _xray() -> list[dict[str, Any]]:
    return [
        {
            "outbounds": [
                {
                    "protocol": "vless",
                    "tag": f"route-{index}",
                    "settings": {
                        "vnext": [
                            {
                                "address": host,
                                "port": 443,
                                "users": [
                                    {
                                        "id": NODE,
                                        "flow": "xtls-rprx-vision",
                                        "encryption": "none",
                                    }
                                ],
                            }
                        ]
                    },
                    "streamSettings": {
                        "network": "tcp",
                        "security": "reality",
                        "realitySettings": {
                            "serverName": "cover.example.org",
                            "publicKey": PUBLIC_KEY,
                            "shortId": "a1b2c3d4",
                        },
                    },
                }
            ]
        }
        for index, host in enumerate(HOSTS)
    ]


@pytest.fixture(params=["plain", "base64", "singbox", "xray"])
async def subscription_body(services: Any, request: pytest.FixtureRequest) -> bytes:
    services.settings = _sharded_settings()
    await _user(services.db, 4212)
    await services.db.set_entry_segment(4212, "ru-a", "admin")
    if request.param in ("plain", "base64"):
        links = "\n".join(
            _vless(
                host,
                encryption="none",
                type="tcp",
                flow="xtls-rprx-vision",
                sni="cover.example.org",
                pbk=PUBLIC_KEY,
                sid="a1b2c3d4",
            )
            for host in HOSTS
        )
        if request.param == "base64":
            links = base64.b64encode(links.encode()).decode()
        # The panel offers an array of whole sing-box configs first.
        upstream = Upstream(
            {APP_AGENT: json.dumps([_singbox()]), "v2rayNG/1.9.0": links}
        )
        expected_agents = [APP_AGENT, "v2rayNG/1.9.0"]
    else:
        config = _singbox() if request.param == "singbox" else _xray()
        upstream = Upstream({APP_AGENT: json.dumps(config)})
        expected_agents = [APP_AGENT]
    services.hiddify.fetch_subscription = upstream
    async with await _client(services) as client:
        response = await client.get(f"/sub/{NODE}/", headers={"User-Agent": APP_AGENT})
        assert response.status == 200
        assert upstream.agents == expected_agents
        return await response.read()


def test_hiddify_imports_only_the_assigned_and_shared_routes(
    subscription_body: bytes, tmp_path: Path
) -> None:
    core = shutil.which("hiddify-core")
    assert core, "Hiddify Core 5.5.5 is required for this integration test"
    source = tmp_path / "subscription.txt"
    result = tmp_path / "parsed.json"
    source.write_bytes(subscription_body)
    subprocess.run(  # noqa: S603 - fixed parse command on a synthetic profile
        [core, "parse", source.name, "-o", result.name],
        cwd=tmp_path,
        check=True,
        timeout=30,
    )
    parsed = json.loads(result.read_text())
    proxies = [out for out in parsed["outbounds"] if out["type"] == "vless"]
    assert len(proxies) == 2
    assert {out["server"] for out in proxies} == {RU_A, DE}
    assert RU_B not in json.dumps(parsed)
    for out in proxies:
        assert (out["server_port"], out["uuid"], out["flow"]) == (
            443,
            NODE,
            "xtls-rprx-vision",
        )
        assert out["tls"]["reality"]["public_key"] == PUBLIC_KEY
