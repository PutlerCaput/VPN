#!/usr/bin/env python3
"""Read-only, redacted VPN readiness check from a Docker inspect snapshot.

On the VPN host:
    docker inspect $(docker ps -aq) | python3 scripts/resilience_preflight.py

Or use saved diagnostics:
    python3 scripts/resilience_preflight.py --inspect inspect.json --ports ports.txt

This cannot prove actual reachability from Russian mobile networks or
independence of exit nodes; those must be measured externally.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import shlex
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Finding:
    level: str
    code: str
    message: str


SENSITIVE = re.compile(
    r"(?:^|_)(?:TOKEN|PASSWORD|PASSWD|SECRET|SECRET_KEY|API_KEY|PRIVATE_KEY|DB_KEY)$"
)


def _validate(containers: Any) -> list[dict[str, Any]]:
    if not isinstance(containers, list):
        raise ValueError("expected array")
    for container in containers:
        if not isinstance(container, dict):
            raise ValueError("expected container")
        for field in ("Config", "State"):
            if not isinstance(container.get(field), dict):
                raise ValueError("expected object")
        if not isinstance(container.get("Name"), str):
            raise ValueError("expected name")
        if not isinstance(container["State"].get("Running"), bool):
            raise ValueError("expected running status")
        config = container["Config"]
        for field in ("Env", "Cmd", "Entrypoint"):
            value = config.get(field)
            if value is not None and (
                not isinstance(value, list)
                or not all(isinstance(item, str) for item in value)
            ):
                raise ValueError("expected string array")
        labels = config.get("Labels")
        if labels is not None and (
            not isinstance(labels, dict)
            or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in labels.items()
            )
        ):
            raise ValueError("expected labels")
    return containers


def _environment(container: dict[str, Any]) -> dict[str, str]:
    return dict(
        item.split("=", 1)
        for item in container["Config"].get("Env") or []
        if "=" in item
    )


def _service(container: dict[str, Any], service: str, legacy_name: str) -> bool:
    labels = container["Config"].get("Labels") or {}
    # Labels take precedence: unrelated projects cannot masquerade by name.
    if "com.docker.compose.service" in labels:
        return bool(labels["com.docker.compose.service"] == service)
    return bool(container["Name"].lstrip("/") == legacy_name)


def _command_credentials(config: dict[str, Any]) -> int:
    count = 0
    # Docker argv may contain a whole `sh -c` command in one element.
    arguments: list[str] = []
    for item in (config.get("Entrypoint") or []) + (config.get("Cmd") or []):
        try:
            arguments.extend(shlex.split(item))
        except ValueError:
            arguments.append(item)
    for index, item in enumerate(arguments):
        name, separator, value = item.lstrip("-").partition("=")
        if not SENSITIVE.search(name.replace("-", "_").upper()):
            continue
        if not separator:
            value = arguments[index + 1] if index + 1 < len(arguments) else ""
        if value and not value.startswith(("--", "$")):
            count += 1
    return count


def _listeners(ports: str) -> list[tuple[str, str, int]]:
    """Read only local endpoints, never the peer address or process command."""
    listeners = []
    for line in ports.splitlines():
        fields = line.split()
        if not fields or fields[0] == "Netid":
            continue
        if fields[0] not in {"tcp", "tcp6", "udp", "udp6"}:
            raise ValueError("unrecognized port snapshot")
        if len(fields) < 6:
            raise ValueError("malformed port snapshot")
        proto = fields[0].rstrip("6")
        if fields[1] != ("LISTEN" if proto == "tcp" else "UNCONN"):
            continue
        host, sep, port = fields[4].rpartition(":")
        if not sep or not port.isdigit() or not 0 < int(port) <= 65535:
            raise ValueError("malformed local endpoint")
        host = host.strip("[]").split("%", 1)[0]
        if host != "*":
            ipaddress.ip_address(host)  # Validate; never echo it in an error.
        listeners.append((proto, host, int(port)))
    return listeners


def _nonloopback(host: str) -> bool:
    if host == "*":
        return True
    address = ipaddress.ip_address(host)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return not address.is_loopback


def audit(
    containers: list[dict[str, Any]],
    ports: str | None = None,
    *,
    bot_name: str | None = None,
    bot_service: str = "bot",
    awg_port: int | None = None,
) -> list[Finding]:
    """Snapshot hints only; no network, writes, credentials or names in output."""
    containers = _validate(containers)
    if awg_port is not None and not 0 < awg_port <= 65535:
        raise ValueError("invalid AWG port")
    findings: list[Finding] = []
    active = [c for c in containers if c["State"]["Running"]]
    bots = [
        c
        for c in active
        if (
            c["Name"].lstrip("/") == bot_name.lstrip("/")
            if bot_name
            else _service(c, bot_service, "node-rental-bot-1")
        )
    ]
    bot = bots[0] if len(bots) == 1 else None
    if not bots:
        findings.append(
            Finding(
                "HIGH",
                "BOT_NOT_RUNNING",
                "No matching running bot in this snapshot; confirm service identity.",
            )
        )
    elif len(bots) > 1:
        findings.append(
            Finding(
                "HIGH",
                "BOT_IDENTITY_AMBIGUOUS",
                "Multiple running bot candidates; select one explicitly and "
                "verify single-writer operation.",
            )
        )

    # Stopped containers retain secrets too. A missing bot must not skip this.
    env_count = sum(
        bool(value) and bool(SENSITIVE.search(key.upper()))
        for container in containers
        for key, value in _environment(container).items()
    )
    cmd_count = sum(_command_credentials(c["Config"]) for c in containers)
    if env_count:
        findings.append(
            Finding(
                "HIGH",
                "CREDENTIALS_IN_DOCKER_ENV",
                f"{env_count} credential variable(s) visible in Docker "
                f"inspect; use mounted secrets. Rotate if exposure occurred.",
            )
        )
    if cmd_count:
        findings.append(
            Finding(
                "HIGH",
                "CREDENTIALS_IN_DOCKER_COMMAND",
                f"{cmd_count} credential argument(s) visible in Docker "
                f"inspect; use secret-file options. Rotate if exposure "
                f"occurred.",
            )
        )

    listeners = _listeners(ports) if ports is not None else []
    if ports is None:
        findings.append(
            Finding(
                "WARN",
                "PORTS_NOT_CHECKED",
                "No port snapshot supplied; listener exposure and UDP "
                "presence were not checked.",
            )
        )
    env = _environment(bot) if bot else {}
    http_port = int(env.get("HTTP_PORT", "8080"))
    if not 0 < http_port <= 65535:
        raise ValueError("invalid HTTP port")
    if any(
        proto == "tcp" and port == http_port and _nonloopback(host)
        for proto, host, port in listeners
    ):
        findings.append(
            Finding(
                "HIGH",
                "PUBLIC_BOT_PORT",
                "Bot HTTP port has a non-loopback bind; verify firewall and "
                "Docker publishing. Internet reachability is unverified.",
            )
        )

    if bot:
        if env.get("ENTRY_SHARDING_ENABLED", "false").lower() != "true":
            findings.append(
                Finding(
                    "WARN",
                    "ENTRY_SHARDING_OFF",
                    "Subscribers may receive all entry hosts. Verify the "
                    "filtered subscription path before release.",
                )
            )
        if env.get("CONFIG_DELIVERY", "link").lower() != "subscription":
            findings.append(
                Finding(
                    "WARN",
                    "SINGLE_LINK_DELIVERY",
                    "A single imported link cannot receive new backup entries.",
                )
            )
        if env.get("AWG_ENABLED", "false").lower() == "true":
            awg = [c for c in active if _service(c, "awg", "cyberdefency-awg")]
            if len(awg) != 1:
                findings.append(
                    Finding(
                        "HIGH",
                        "AWG_AGENT_UNVERIFIED",
                        "No unique local AWG agent; verify any remote agent "
                        "separately.",
                    )
                )
            awg_env = _environment(awg[0]) if len(awg) == 1 else {}
            configured = awg_port or int(
                awg_env.get("AWG_PORT", env.get("AWG_PORT", "39051"))
            )
            if not 0 < configured <= 65535:
                raise ValueError("invalid AWG port")
            if ports is not None and not any(
                proto == "udp" and port == configured for proto, _, port in listeners
            ):
                findings.append(
                    Finding(
                        "WARN",
                        "AWG_UDP_UNVERIFIED",
                        "Configured AWG UDP port is absent from this "
                        "snapshot; check endpoint and connectivity.",
                    )
                )
            findings.append(
                Finding(
                    "INFO",
                    "AWG_TUNNEL_UNVERIFIED",
                    "Container/listener presence does not verify AWG agent "
                    "health or a working client tunnel.",
                )
            )
    findings.extend(
        [
            Finding(
                "INFO",
                "RU_CLIENT_PROBES_REQUIRED",
                "This snapshot is not an ISP/DPI test; test real VPN traffic "
                "over multiple mobile and wired operators.",
            ),
            Finding(
                "INFO",
                "EXIT_REDUNDANCY_UNVERIFIED",
                "Independent exit ASNs, subscription origins, firewall "
                "reachability and backups are not verified by this snapshot.",
            ),
        ]
    )
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--inspect", default="-", help="Docker inspect JSON file or stdin (-)"
    )
    parser.add_argument("--ports", type=Path, help="Optional ss -tulpen output")
    parser.add_argument("--json", action="store_true", help="Redacted JSON output")
    parser.add_argument(
        "--fail-high",
        action="store_true",
        help="Exit 1 on HIGH; ordinary report mode exits 0",
    )
    parser.add_argument(
        "--bot-name", help="Explicit container name (overrides service selection)"
    )
    parser.add_argument("--bot-service", default="bot", help="Compose service label")
    parser.add_argument(
        "--awg-port",
        type=int,
        help="Override AWG port, otherwise use agent Env/default",
    )
    args = parser.parse_args(argv)
    try:
        source = (
            sys.stdin.read() if args.inspect == "-" else Path(args.inspect).read_text()
        )
        parsed = json.loads(source)
        ports = args.ports.read_text() if args.ports else None
        findings = audit(
            parsed,
            ports,
            bot_name=args.bot_name,
            bot_service=args.bot_service,
            awg_port=args.awg_port,
        )
    except (OSError, ValueError, RecursionError) as exc:
        print(f"PREFLIGHT_INPUT_ERROR: {type(exc).__name__}", file=sys.stderr)
        return 2
    if args.json:
        print(
            json.dumps(
                [asdict(item) for item in findings], ensure_ascii=False, indent=2
            )
        )
    else:
        for item in findings:
            print(f"[{item.level}] {item.code}: {item.message}")
    return int(args.fail_high and any(item.level == "HIGH" for item in findings))


if __name__ == "__main__":
    raise SystemExit(main())
