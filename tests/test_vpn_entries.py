"""Extra entry addresses: the host table logic and the panel calls."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import httpx
import pytest

from services.marzban import MarzbanClient

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "vpn_entries", ROOT / "scripts" / "vpn_entries.py"
)
assert spec is not None
assert spec.loader is not None
entries = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entries)

TAG = "VLESS TCP REALITY"
HOSTS: dict[str, Any] = {
    TAG: [
        {
            "remark": "CHEBURNET",
            "address": "45.138.157.65",
            "port": 443,
            "sni": "www.apple.com",
            "host": "",
            "security": "inbound_default",
            "fingerprint": "chrome",
            "is_disabled": False,
        }
    ],
    "Shadowsocks TCP": [{"remark": "x", "address": "{SERVER_IP}", "port": None}],
}


def test_adding_copies_the_settings_and_changes_only_the_address() -> None:
    result = entries.with_added(HOSTS, TAG, "89.125.210.172", 2096)
    assert len(result[TAG]) == 2
    first, new = result[TAG]
    assert first == HOSTS[TAG][0]
    assert (new["address"], new["port"]) == ("89.125.210.172", 2096)
    assert new["sni"] == "www.apple.com"
    assert new["fingerprint"] == "chrome"
    assert new["remark"] == "CHEBURNET (89.125.210.172)"
    assert result["Shadowsocks TCP"] == HOSTS["Shadowsocks TCP"]
    # The input is not touched, and adding twice changes nothing.
    assert len(HOSTS[TAG]) == 1
    assert entries.with_added(result, TAG, "89.125.210.172", 2096) == result


def test_removing_keeps_at_least_one_address() -> None:
    added = entries.with_added(HOSTS, TAG, "89.125.210.172", 2096)
    back = entries.without(added, TAG, "89.125.210.172", 2096)
    assert back == HOSTS
    with pytest.raises(ValueError, match="last address"):
        entries.without(HOSTS, TAG, "45.138.157.65", 443)


@pytest.mark.parametrize(
    "value", ["89.125.210.172", "89.125.210.172:0", "x:443", "a b:1", "1.2.3.4:70000"]
)
def test_bad_targets_are_refused(value: str) -> None:
    with pytest.raises(ValueError, match="expected ADDRESS:PORT|not an IP"):
        entries.parse_target(value)


def test_targets() -> None:
    assert entries.parse_target("89.125.210.172:2096") == ("89.125.210.172", 2096)
    assert entries.parse_target("de.example.com:443") == ("de.example.com", 443)


def test_the_description_prints_addresses_and_covers_only() -> None:
    text = "\n".join(entries.describe(HOSTS))
    assert "VLESS TCP REALITY: 45.138.157.65:443 cover=www.apple.com" in text
    assert "Shadowsocks TCP: {SERVER_IP}:inbound port" in text


async def test_the_client_reads_and_writes_the_host_table() -> None:
    seen: list[tuple[str, str, Any]] = []
    table = {TAG: list(HOSTS[TAG])}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/admin/token":
            return httpx.Response(200, json={"access_token": "t"})
        seen.append((request.method, request.url.path, request.content))
        if request.method == "PUT":
            import json

            table.clear()
            table.update(json.loads(request.content))
        return httpx.Response(200, json=table)

    client = MarzbanClient(
        "https://panel.test",
        "admin",
        "pw",
        TAG,
        transport=httpx.MockTransport(handler),
    )
    try:
        assert await client.get_hosts() == {TAG: HOSTS[TAG]}
        wanted = entries.with_added(await client.get_hosts(), TAG, "89.1.1.1", 2096)
        assert await client.put_hosts(wanted) == wanted
    finally:
        await client.close()
    assert [m for m, _, _ in seen] == ["GET", "GET", "PUT"]


def test_the_panels_default_address_can_be_dropped() -> None:
    hosts = {
        "VLESS-TCP-REALITY-RU": [
            {"address": "{SERVER_IP}", "port": None, "sni": ""},
            {"address": "198.51.100.7", "port": 2083, "sni": ""},
        ]
    }
    result = entries.without(hosts, "VLESS-TCP-REALITY-RU", "{SERVER_IP}", 0)
    assert result["VLESS-TCP-REALITY-RU"] == [
        {"address": "198.51.100.7", "port": 2083, "sni": ""}
    ]
    only_default = {"X": [{"address": "{SERVER_IP}", "port": None}]}
    with pytest.raises(ValueError, match="last address"):
        entries.without(only_default, "X", "{SERVER_IP}", 0)


def test_the_fingerprint_goes_on_every_address() -> None:
    result = entries.with_fingerprint(HOSTS, "chrome")
    assert {e["fingerprint"] for tag in result.values() for e in tag} == {"chrome"}
    assert "fingerprint" not in HOSTS["Shadowsocks TCP"][0]  # input untouched
    with pytest.raises(ValueError, match="fingerprint is one of"):
        entries.with_fingerprint(HOSTS, "netscape")
    assert "fingerprint=chrome" in "\n".join(entries.describe(HOSTS))
