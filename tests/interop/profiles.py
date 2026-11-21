"""Synthetic VLESS REALITY subscriptions for the released-client checks."""

from __future__ import annotations

import base64
from typing import Any

from tests.test_entry_sharding import DE, NODE, RU_A, RU_B, _vless

# Synthetic public key bytes. There is no matching server or private key here.
PUBLIC_KEY = base64.urlsafe_b64encode(bytes(range(1, 33))).decode().rstrip("=")
HOSTS = (RU_A, RU_B, DE)


def singbox() -> dict[str, Any]:
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


def xray() -> list[dict[str, Any]]:
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


def reality_links(hosts: tuple[str, ...] = HOSTS) -> str:
    return "\n".join(
        _vless(
            host,
            encryption="none",
            type="tcp",
            flow="xtls-rprx-vision",
            sni="cover.example.org",
            pbk=PUBLIC_KEY,
            sid="a1b2c3d4",
        )
        for host in hosts
    )
