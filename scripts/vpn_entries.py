#!/usr/bin/env python3
"""Extra entry addresses in the subscription: the same VPN reached another way.

Runs inside the bot container (it has the panel's address and password):

    docker exec -i node-rental-bot-1 python3 - [args] < scripts/vpn_entries.py

    (no args)              what is in the subscription now, and what --add would do
    --add ADDRESS:PORT     add this address for the inbound, copying the cover name
                           and the other settings of the inbound's first address
    --remove ADDRESS:PORT  take it out again
    --attach TAG           give every user the VLESS inbound TAG as well (after a
                           new inbound was added to Xray: their links follow)
    --fingerprint NAME     the TLS fingerprint clients imitate, on every address
                           (chrome, firefox, safari, ios, edge, random, randomized)
    --tag TAG              (first) work on that inbound instead of the main one
    --fragment-copy ADDRESS:PORT [SETTING]
                           next to that address, the same one with the TLS
                           ClientHello fragmented (default 100-200,10-20,tlshello)
    --fragment-remove ADDRESS:PORT
                           take the fragmented copy away again
    --remove {SERVER_IP}   drop the panel's default address of the inbound (a new
                           inbound gets one; an entry only the relay may reach
                           must not be offered with this server's own address)

Only the panel host list changes: no key, user or Xray setting, no restart of the
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


FINGERPRINTS = ("chrome", "firefox", "safari", "ios", "edge", "random", "randomized")


def with_fingerprint(hosts: dict[str, Any], name: str) -> dict[str, Any]:
    if name not in FINGERPRINTS:
        raise ValueError(f"fingerprint is one of {', '.join(FINGERPRINTS)}")
    result = copy.deepcopy(hosts)
    for entries in result.values():
        for entry in entries or []:
            entry["fingerprint"] = name
    return result


def describe(hosts: dict[str, Any]) -> list[str]:
    lines = []
    for tag, entries in hosts.items():
        for entry in entries or []:
            state = "off" if entry.get("is_disabled") else "on"
            spec = entry.get("fragment_setting")
            fragment = f" fragment={spec}" if spec else ""
            lines.append(
                f"{tag}: {entry.get('address')}:{entry.get('port') or 'inbound port'}"
                f" cover={entry.get('sni') or '-'}"
                f" fingerprint={entry.get('fingerprint') or '-'}"
                + fragment
                + f" remark={entry.get('remark')!r} {state}"
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


SERVER_IP = "{SERVER_IP}"
# The fragment setting: packet lengths, pause in ms, which packets
# ("tlshello": the TLS ClientHello, which carries the cover name).
FRAGMENT_RE = re.compile(
    r"^\d{1,4}(-\d{1,4})?,\d{1,4}(-\d{1,4})?,(tlshello|\d{1,3}-\d{1,3})$"
)
DEFAULT_FRAGMENT = "100-200,10-20,tlshello"


def with_fragment_copy(
    hosts: dict[str, Any], address: str, port: int, spec: str
) -> dict[str, Any]:
    """Next to an address, the same address with the ClientHello fragmented.

    The plain one stays: on some networks fragmenting helps, on others it is
    itself a sign or breaks the handshake, so clients and the probe compare both.
    """
    if not FRAGMENT_RE.fullmatch(spec):
        raise ValueError(f"not a fragment setting like {DEFAULT_FRAGMENT}: {spec!r}")
    for tag, entries in hosts.items():
        plain = [
            e
            for e in entries or []
            if e.get("address") == address
            and e.get("port") == port
            and not e.get("fragment_setting")
        ]
        if not plain:
            continue
        if any(
            e.get("address") == address
            and e.get("port") == port
            and e.get("fragment_setting") == spec
            for e in entries
        ):
            return hosts
        result = copy.deepcopy(hosts)
        new = copy.deepcopy(plain[0])
        new["fragment_setting"] = spec
        new["remark"] = f"{plain[0].get('remark') or 'VPN'} (fragment)"
        result[tag].append(new)
        return result
    raise ValueError(f"no plain address {address}:{port} to copy")


def without_fragment_copy(
    hosts: dict[str, Any], address: str, port: int
) -> dict[str, Any]:
    result = copy.deepcopy(hosts)
    for tag, entries in result.items():
        result[tag] = [
            e
            for e in entries or []
            if not (
                e.get("address") == address
                and e.get("port") == port
                and e.get("fragment_setting")
            )
        ]
    return result


def matches(entry: dict[str, Any], address: str, port: int) -> bool:
    """SERVER_IP matches the panel's default address whatever its port."""
    if entry.get("address") != address:
        return False
    return address == SERVER_IP or entry.get("port") == port


def without(hosts: dict[str, Any], tag: str, address: str, port: int) -> dict[str, Any]:
    entries = hosts.get(tag) or []
    kept = [e for e in entries if not matches(e, address, port)]
    if not kept:
        raise ValueError("refusing to remove the last address of the inbound")
    result = copy.deepcopy(hosts)
    result[tag] = kept
    return result


def tags_with(current: list[str], base: str, extra: str) -> list[str] | None:
    """The user's VLESS tags with ``extra``; None if nothing changes."""
    tags = list(current) or [base]
    if extra in tags:
        return None
    return [*tags, extra]


async def attach(client: Any, base: str, extra: str, hosts: dict[str, Any]) -> int:
    if extra not in hosts:
        print(
            f"VPN_ENTRIES_STOPPED: the panel has no inbound {extra!r}; "
            "add it to Xray first (vless_transport.sh)"
        )
        return 1
    changed = skipped = 0
    for user in await client.get_users_usage():
        username = str(user.get("username") or "")
        current = list((user.get("inbounds") or {}).get("vless") or [])
        wanted = tags_with(current, base, extra)
        if wanted is None:
            skipped += 1
            continue
        await client.set_vless_inbounds(username, wanted)
        changed += 1
    print(f"VPN_ENTRIES_ATTACHED: {extra} for {changed} users ({skipped} had it)")
    return 0


async def main(argv: list[str]) -> int:
    """Host-table edits are not exposed by Hiddify Manager API v2.

    Addresses and fragments are configured in the panel UI (proxies / domains).
    The helpers in this file still describe a host table and are used in tests.
    """
    del argv
    print(
        "VPN_ENTRIES_STOPPED: Hiddify Manager API v2 has no host-table "
        "endpoints. Add or remove entry addresses in the panel UI. "
        "See docs/HIDDIFY.md and docs/VPN_ENTRIES.md."
    )
    return 2



if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
