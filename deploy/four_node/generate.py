"""Render a four-VPS VLESS REALITY topology; never contacts or edits servers.

All inventory and key material must remain outside the public Git repository.
Each NL server has its own REALITY key and VLESS UUID, so RU-1 only forwards
to NL-1 and RU-2 only forwards to NL-2. Failover is client-side, not HAProxy.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import shutil
import subprocess  # nosec B404
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlencode

XRAY_IMAGE = (
    "ghcr.io/xtls/xray-core:26.11.9@"
    "sha256:d42420dca2014cbd06e24daad8a039b455b7d9e905cda1cf871254cfffda2149"
)
FIELDS = ("ru1", "ru2", "nl1", "nl2", "server_name", "target")
REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Credentials:
    private: str
    public: str
    short_id: str
    user_id: str


def load_inventory(path: Path) -> dict[str, str]:
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Inventory must be a JSON object")
    inv: dict[str, str] = {}
    for field in FIELDS:
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Missing or invalid inventory field: {field}")
        inv[field] = value.strip()

    addresses = []
    for field in ("ru1", "ru2", "nl1", "nl2"):
        try:
            address = ipaddress.IPv4Address(inv[field])
        except ipaddress.AddressValueError as exc:
            raise ValueError(f"{field} must be a public IPv4 address") from exc
        if not address.is_global:
            raise ValueError(f"{field} must not be a private/reserved/test IP")
        addresses.append(address)
    if len(set(addresses)) != 4:
        raise ValueError("All four server addresses must be distinct")

    name = inv["server_name"].lower()
    labels = name.split(".")
    if (
        len(name) > 253
        or len(labels) < 2
        or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in labels
        )
    ):
        raise ValueError("server_name must be a valid DNS hostname")
    inv["server_name"] = name
    if inv["target"] != f"{name}:443":
        raise ValueError("target must equal server_name:443 for this first release")
    return inv


def x25519(xray: str) -> tuple[str, str]:
    """Use the official Xray binary; do not expose its private key in logs."""
    completed = subprocess.run(  # noqa: S603  # nosec B603
        [xray, "x25519"],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    result = completed.stdout
    private_match = re.search(r"(?mi)^Private(?:Key| key):\s*(\S+)\s*$", result)
    public_match = re.search(
        r"(?mi)^(?:Password(?: \(PublicKey\))?|Public(?:Key| key)):\s*(\S+)\s*$",
        result,
    )
    if not private_match or not public_match:
        raise ValueError("Unexpected output from xray x25519")
    private, public = private_match.group(1), public_match.group(1)
    for value in (private, public):
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", value):
            raise ValueError("Invalid X25519 key encoding from Xray")
    return private, public


def fresh_credentials(xray: str) -> Credentials:
    import secrets

    private, public = x25519(xray)
    return Credentials(
        private=private,
        public=public,
        short_id=secrets.token_hex(8),
        user_id=str(uuid.uuid4()),
    )


def xray_config(inv: dict[str, str], creds: Credentials) -> dict[str, object]:
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "tag": "vless-reality",
                "listen": "0.0.0.0",  # noqa: S104  # nosec B104
                "port": 443,
                "protocol": "vless",
                "settings": {
                    "clients": [{"id": creds.user_id, "flow": "xtls-rprx-vision"}],
                    "decryption": "none",
                },
                "streamSettings": {
                    "network": "raw",
                    "security": "reality",
                    "realitySettings": {
                        "show": False,
                        "target": inv["target"],
                        "xver": 0,
                        "serverNames": [inv["server_name"]],
                        "privateKey": creds.private,
                        "shortIds": [creds.short_id],
                    },
                },
            }
        ],
        "outbounds": [{"protocol": "freedom", "tag": "direct"}],
    }


def haproxy_config(backend_ip: str) -> str:
    return (
        "global\n"
        "  maxconn 2048\n"
        "  log stdout format raw local0\n"
        "defaults\n"
        "  mode tcp\n"
        "  log global\n"
        "  timeout connect 5s\n"
        "  timeout client 2m\n"
        "  timeout server 2m\n"
        "frontend reality_in\n"
        "  bind :443\n"
        "  default_backend fixed_exit\n"
        "backend fixed_exit\n"
        f"  server exit {backend_ip}:443 check inter 5s fall 3 rise 2\n"
    )


def vless_link(ip: str, title: str, inv: dict[str, str], creds: Credentials) -> str:
    params = {
        "encryption": "none",
        "security": "reality",
        "sni": inv["server_name"],
        "fp": "chrome",
        "pbk": creds.public,
        "sid": creds.short_id,
        "type": "tcp",
        "flow": "xtls-rprx-vision",
    }
    return f"vless://{creds.user_id}@{ip}:443?{urlencode(params)}#{quote(title)}"


def write_private(path: Path, content: str) -> None:
    with path.open("x", encoding="utf-8") as fp:
        fp.write(content)
    path.chmod(0o600)


def render(
    output: Path,
    inv: dict[str, str],
    nl1: Credentials,
    nl2: Credentials,
) -> None:
    destination = output.resolve()
    if destination == REPO_ROOT or REPO_ROOT in destination.parents:
        raise ValueError("Output must be outside the public Git repository")
    if destination.exists():
        raise FileExistsError("Refusing to overwrite existing output directory")

    old_umask = os.umask(0o077)
    try:
        destination.mkdir(parents=True, mode=0o700)
        try:
            for name, creds in (("nl1", nl1), ("nl2", nl2)):
                folder = destination / name
                folder.mkdir(mode=0o700)
                write_private(
                    folder / "config.json",
                    json.dumps(xray_config(inv, creds), indent=2) + "\n",
                )
                write_private(
                    folder / "compose.yaml",
                    "services:\n"
                    "  xray:\n"
                    f"    image: {XRAY_IMAGE}\n"
                    '    network_mode: "host"\n'
                    "    restart: unless-stopped\n"
                    '    user: "0:0"\n'
                    "    read_only: true\n"
                    "    cap_drop: [ALL]\n"
                    "    cap_add: [NET_BIND_SERVICE]\n"
                    "    security_opt: [no-new-privileges:true]\n"
                    "    volumes:\n"
                    "      - ./config.json:/etc/xray/config.json:ro\n"
                    "    command: [run, -c, /etc/xray/config.json]\n",
                )

            for ingress, backend in (("ru1", "nl1"), ("ru2", "nl2")):
                folder = destination / ingress
                folder.mkdir(mode=0o700)
                write_private(folder / "haproxy.cfg", haproxy_config(inv[backend]))

            links = [
                vless_link(inv["ru1"], "RU1 via NL1", inv, nl1),
                vless_link(inv["ru2"], "RU2 via NL2", inv, nl2),
                vless_link(inv["nl1"], "NL1 direct", inv, nl1),
                vless_link(inv["nl2"], "NL2 direct", inv, nl2),
            ]
            write_private(destination / "vless-links.txt", "\n".join(links) + "\n")
            write_private(
                destination / "README.txt",
                "PRIVATE: do not upload this directory to GitHub or a chat.\n"
                "This output has NOT been deployed or validated on real servers.\n"
                "Each RU ingress forwards exclusively to its paired NL node.\n"
                "No server-side cross-NL failover: REALITY keys are independent.\n"
                "Import four links into a compatible VPN client and verify failover.\n"
                "Check TLS target from BOTH NL VPS before deployment.\n",
            )
        except Exception:
            shutil.rmtree(destination)
            raise
    finally:
        os.umask(old_umask)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--xray", default="xray", help="trusted Xray CLI on PATH")
    args = parser.parse_args()
    try:
        inv = load_inventory(args.inventory)
        if args.output.resolve().exists():
            raise FileExistsError("Output directory already exists")
        # Never print private keys or user UUIDs to standard output.
        nl1 = fresh_credentials(args.xray)
        nl2 = fresh_credentials(args.xray)
        render(args.output, inv, nl1, nl2)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"ERROR: {exc}\n")
    print(f"Created private deployment bundle: {args.output}")
    print("No SSH connections made. Nothing installed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
