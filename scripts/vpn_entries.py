#!/usr/bin/env python3
"""Extra entry addresses in the subscription: the same VPN reached another way.

Runs inside the bot container (it has the panel's address and password):

    docker exec -i node-rental-bot-1 python3 - [args] < scripts/vpn_entries.py

    (no args)              what is in the subscription now, and what --add would do
    --add ADDRESS:PORT     add this address for the inbound, copying the cover name
                           and the other settings of the inbound's first address
    --remove ADDRESS:PORT  take it out again

Only Marzban's host table changes: no key, user or Xray setting, no restart of the
VPN. A client gets the new address with its next subscription update and can
switch to it when the first one does not answer. The firewall on the server must
let the port in; this script does not touch it (docs/VPN_ENTRIES.md).
Nothing secret is printed: addresses, ports and cover names only.
"""

from __future__ import annotations

import asyncio
import copy
import ipaddress
import re
import sys
from typing import Any

sys.path.insert(0, "/app")

HOST_RE = re.compile(r"^(?P<address>[A-Za-z0-9.-]{1,253}):(?P<port>[0-9]{1,5})$")


def parse_target(value: str) -> tuple[str, int]:
    found = HOST_RE.fullmatch(value)
    if not found or not 1 <= int(found["port"]) <= 65535:
        raise ValueError(f"expected ADDRESS:PORT, got {value!r}")
    address = found["address"]
    try:
        ipaddress.ip_address(address)
    except ValueError:
        if "." not in address:
            raise ValueError(f"not an IP address or a domain: {address!r}") from None
    return address, int(found["port"])


def describe(hosts: dict[str, Any]) -> list[str]:
    lines = []
    for tag, entries in hosts.items():
        for entry in entries or []:
            state = "off" if entry.get("is_disabled") else "on"
            lines.append(
                f"{tag}: {entry.get('address')}:{entry.get('port') or 'inbound port'}"
                f" cover={entry.get('sni') or '-'} remark={entry.get('remark')!r}"
                f" {state}"
            )
    return lines


def with_added(
    hosts: dict[str, Any], tag: str, address: str, port: int
) -> dict[str, Any]:
    """The table with the new address; unchanged if it is already there."""
    entries = hosts.get(tag)
    if not entries:
        raise ValueError(f"the inbound {tag!r} has no address to copy settings from")
    if any(e.get("address") == address and e.get("port") == port for e in entries):
        return hosts
    result = copy.deepcopy(hosts)
    new = copy.deepcopy(entries[0])
    new["address"] = address
    new["port"] = port
    new["is_disabled"] = False
    base = str(entries[0].get("remark") or "VPN")
    new["remark"] = f"{base} ({address})"
    result[tag].append(new)
    return result


def without(hosts: dict[str, Any], tag: str, address: str, port: int) -> dict[str, Any]:
    entries = hosts.get(tag) or []
    kept = [
        e
        for e in entries
        if not (e.get("address") == address and e.get("port") == port)
    ]
    if not kept:
        raise ValueError("refusing to remove the last address of the inbound")
    result = copy.deepcopy(hosts)
    result[tag] = kept
    return result


async def main(argv: list[str]) -> int:
    from config import get_settings
    from services.marzban import MarzbanClient, MarzbanError

    settings = get_settings()
    tag = settings.marzban_inbound_tag
    client = MarzbanClient(
        settings.marzban_url,
        settings.marzban_username,
        settings.marzban_password.get_secret_value(),
        tag,
        verify_tls=settings.marzban_verify_tls,
        timeout=15,
    )
    try:
        hosts = await client.get_hosts()
        if len(argv) == 0:
            print("\n".join(describe(hosts)) or "no addresses")
            print(f"VPN_ENTRIES_PLAN: --add ADDRESS:PORT adds an address for {tag!r}")
            return 0
        if len(argv) != 2 or argv[0] not in ("--add", "--remove"):
            print("usage: [--add|--remove ADDRESS:PORT]")
            return 2
        address, port = parse_target(argv[1])
        if argv[0] == "--add":
            wanted = with_added(hosts, tag, address, port)
        else:
            wanted = without(hosts, tag, address, port)
        if wanted == hosts:
            print(f"VPN_ENTRIES_UNCHANGED: {address}:{port} is already there")
            return 0
        await client.put_hosts(wanted)
        after = await client.get_hosts()
        present = any(
            e.get("address") == address and e.get("port") == port
            for e in after.get(tag) or []
        )
        print("\n".join(describe(after)))
        if present == (argv[0] == "--add"):
            print(f"VPN_ENTRIES_OK: {argv[0][2:]} {address}:{port}")
            return 0
        print("VPN_ENTRIES_NOT_APPLIED: the panel did not keep the change")
        return 1
    except (MarzbanError, ValueError) as exc:
        print(f"VPN_ENTRIES_STOPPED: {exc}")
        return 1
    finally:
        await client.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
