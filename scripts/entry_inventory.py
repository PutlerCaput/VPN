#!/usr/bin/env python3
"""What the VPN entries are now: read-only, and no secret is printed.

    python3 entry_inventory.py [/var/lib/marzban/xray_config.json]

From Xray's config (the one Marzban runs) it lists each inbound: tag, port,
protocol, transport, security and, for REALITY, the cover names and the target.
Keys, short ids, user ids and passwords are never printed: only whether they
are set. The output is meant to be pasted into the chat.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

DEFAULT = "/var/lib/marzban/xray_config.json"


def _reality(settings: dict[str, Any]) -> list[str]:
    names = settings.get("serverNames") or []
    target = settings.get("dest") or settings.get("target") or "-"
    lines = [
        f"    reality cover names: {', '.join(str(n) for n in names) or '-'}",
        f"    reality target: {target}",
        "    reality private key: "
        + ("set" if settings.get("privateKey") else "MISSING"),
        f"    reality short ids: {len(settings.get('shortIds') or [])}",
    ]
    if settings.get("fingerprint"):
        lines.append(f"    fingerprint: {settings['fingerprint']}")
    return lines


def describe(config: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for inbound in config.get("inbounds") or []:
        stream = inbound.get("streamSettings") or {}
        network = stream.get("network") or "tcp"
        security = stream.get("security") or "none"
        listen = inbound.get("listen") or "all addresses"
        clients = (inbound.get("settings") or {}).get("clients") or []
        flows = sorted({str(c.get("flow")) for c in clients if c.get("flow")})
        out.append(
            f"inbound {inbound.get('tag', '-')}: {inbound.get('protocol', '-')} "
            f"{listen}:{inbound.get('port', '-')} network={network} "
            f"security={security} clients={len(clients)}"
            + (f" flow={','.join(flows)}" if flows else "")
        )
        if security == "reality":
            out.extend(_reality(stream.get("realitySettings") or {}))
        if security == "tls":
            tls = stream.get("tlsSettings") or {}
            out.append(f"    tls server name: {tls.get('serverName') or '-'}")
        for key in ("wsSettings", "grpcSettings", "xhttpSettings", "splithttpSettings"):
            if key in stream:
                part = stream[key] or {}
                path = part.get("path") or part.get("serviceName") or "-"
                out.append(f"    {key[:-8]} path: {'set' if path != '-' else '-'}")
    outbounds = [
        o.get("tag") or o.get("protocol") for o in config.get("outbounds") or []
    ]
    out.append(f"outbounds: {', '.join(str(o) for o in outbounds) or '-'}")
    return out


def main(argv: list[str]) -> int:
    path = Path(argv[1] if len(argv) > 1 else DEFAULT)
    try:
        config = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        print(f"ENTRY_INVENTORY_STOPPED: cannot read {path}: {type(exc).__name__}")
        return 1
    for line in describe(config):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
