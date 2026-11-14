#!/usr/bin/env python3
"""Writes a candidate Xray config with one more VLESS + REALITY inbound.

    python3 xray_add_inbound.py CONFIG CANDIDATE --cover NAME --port PORT
        [--network grpc|tcp] [--tag VLESS-GRPC-REALITY] [--base "VLESS TCP REALITY"]

The new inbound gets its own cover name, port, short id and, when the
REALITY_PRIVATE_KEY environment variable holds one (vless_transport.sh makes it
with "xray x25519"), its own REALITY key: one entry found out does not lead to
the others. Without it the key of the base inbound is borrowed. gRPC gets a
random service name; tcp is the same kind as the base (clients use Vision).
The live config is never written: the candidate goes to CANDIDATE, and
scripts/vless_transport.sh tests it with Xray before using it.
Nothing secret is printed, and the key never travels on the command line.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import secrets
import sys
from pathlib import Path
from typing import Any

NAME_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9-]{1,63}\.)+[a-z]{2,63}$")
TAG_RE = re.compile(r"^[A-Za-z0-9_-]{3,40}$")


KEY_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


def add_inbound(
    config: dict[str, Any],
    *,
    base: str,
    tag: str,
    cover: str,
    port: int,
    network: str = "grpc",
    private_key: str | None = None,
) -> dict[str, Any]:
    if network not in ("grpc", "tcp"):
        raise ValueError("the network is grpc or tcp")
    if private_key is not None and not KEY_RE.fullmatch(private_key):
        raise ValueError("REALITY_PRIVATE_KEY is not an x25519 key from xray x25519")
    if not NAME_RE.fullmatch(cover):
        raise ValueError(f"not a host name: {cover!r}")
    if not TAG_RE.fullmatch(tag):
        raise ValueError("the tag is 3-40 letters, digits, - or _ (no spaces)")
    if not 1024 <= port <= 65535:
        raise ValueError("the port must be 1024-65535")
    inbounds = config.get("inbounds") or []
    if any(i.get("tag") == tag for i in inbounds):
        raise ValueError(f"an inbound {tag!r} is already there")
    if any(i.get("port") == port for i in inbounds):
        raise ValueError(f"port {port} is already used by another inbound")
    source = next((i for i in inbounds if i.get("tag") == base), None)
    if source is None:
        raise ValueError(f"no inbound {base!r} to borrow the REALITY key from")
    stream = source.get("streamSettings") or {}
    reality = stream.get("realitySettings") or {}
    if stream.get("security") != "reality" or not reality.get("privateKey"):
        raise ValueError(f"{base!r} is not a REALITY inbound with a key")
    new_reality = {
        "show": False,
        "dest": f"{cover}:443",
        "xver": 0,
        "serverNames": [cover],
        "privateKey": private_key or reality["privateKey"],
        # Its own short id either way: clients of one entry cannot try the other.
        "shortIds": [secrets.token_hex(8)],
    }
    stream_settings: dict[str, Any] = {
        "network": network,
        "security": "reality",
        "realitySettings": new_reality,
    }
    if network == "grpc":
        stream_settings["grpcSettings"] = {"serviceName": secrets.token_hex(6)}
    inbound: dict[str, Any] = {
        "tag": tag,
        # Listens where the base inbound listens (it has no default of its own).
        "listen": source.get("listen"),
        "port": port,
        "protocol": "vless",
        "settings": {"clients": [], "decryption": "none"},
        "streamSettings": stream_settings,
    }
    if inbound["listen"] is None:
        del inbound["listen"]
    if "sniffing" in source:
        inbound["sniffing"] = copy.deepcopy(source["sniffing"])
    result = copy.deepcopy(config)
    result["inbounds"] = [*inbounds, inbound]
    return result


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("config")
    parser.add_argument("candidate")
    parser.add_argument("--cover", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--tag", default="VLESS-GRPC-REALITY")
    parser.add_argument("--base", default="VLESS TCP REALITY")
    parser.add_argument("--network", default="grpc", choices=("grpc", "tcp"))
    args = parser.parse_args(argv)
    try:
        config = json.loads(Path(args.config).read_text())
        result = add_inbound(
            config,
            base=args.base,
            tag=args.tag,
            cover=args.cover,
            port=args.port,
            network=args.network,
            private_key=os.environ.get("REALITY_PRIVATE_KEY") or None,
        )
    except (OSError, ValueError) as exc:
        print(f"XRAY_ADD_STOPPED: {exc}")
        return 1
    Path(args.candidate).write_text(json.dumps(result, indent=2, ensure_ascii=False))
    key = "own key" if os.environ.get("REALITY_PRIVATE_KEY") else "borrowed key"
    print(
        f"XRAY_CANDIDATE_OK: {args.tag} vless {args.network} reality on port "
        f"{args.port}, cover {args.cover}, {key}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
