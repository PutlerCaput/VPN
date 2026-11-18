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
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Finding:
    level: str
    code: str
    message: str


def _environment(container: dict[str, Any]) -> dict[str, str]:
    values = (container.get("Config") or {}).get("Env") or []
    return {
        item.split("=", 1)[0]: item.split("=", 1)[1]
        for item in values
        if isinstance(item, str) and "=" in item
    }


def _running(container: dict[str, Any]) -> bool:
    return (container.get("State") or {}).get("Running") is True


def audit(containers: list[dict[str, Any]], ports: str = "") -> list[Finding]:
    """Return only findings, never runtime credential values or connection URLs."""
    findings: list[Finding] = []
    active = [c for c in containers if isinstance(c, dict) and _running(c)]
    bot = next((c for c in active if c.get("Name") == "/node-rental-bot-1"), None)
    if bot is None:
        findings.append(
            Finding(
                "HIGH",
                "BOT_NOT_RUNNING",
                "Main bot absent from running Docker containers.",
            )
        )
        return findings

    env = _environment(bot)
    sensitive = re.compile(
        r"(?:^|_)(?:TOKEN|PASSWORD|SECRET|SECRET_KEY|API_KEY|PRIVATE_KEY|DB_KEY)$"
    )
    exposed_count = sum(
        bool(value) and bool(sensitive.search(key.upper()))
        for key, value in env.items()
    )
    if exposed_count:
        findings.append(
            Finding(
                "HIGH",
                "CREDENTIALS_IN_DOCKER_ENV",
                f"{exposed_count} credential variable(s) visible in Docker inspect; "
                "move to mounted secrets and rotate exposed credentials.",
            )
        )

    if env.get("ENTRY_SHARDING_ENABLED", "false").lower() != "true":
        findings.append(
            Finding(
                "WARN",
                "ENTRY_SHARDING_OFF",
                "Subscribers may receive all entry hosts. Enable sharding only "
                "after independent entry hosts and client-side tests exist.",
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
        awg_local = any(c.get("Name") == "/cyberdefency-awg" for c in active)
        if not awg_local:
            findings.append(
                Finding(
                    "HIGH",
                    "AWG_AGENT_UNVERIFIED",
                    "AWG is enabled but no local cyberdefency-awg container "
                    "is running; verify the configured agent and UDP endpoint.",
                )
            )
        if ports and not re.search(r"(?m)^udp\s+\S+.*[:*]39051\s", ports):
            findings.append(
                Finding(
                    "WARN",
                    "AWG_UDP_UNVERIFIED",
                    "No UDP :39051 listener in port snapshot; check the "
                    "configured AWG port and external connectivity.",
                )
            )

    if ports and re.search(
        r"(?m)^tcp\s+LISTEN\s+.*(?:0\.0\.0\.0|\*|\[::\]):8080\s",
        ports,
    ):
        findings.append(
            Finding(
                "HIGH",
                "PUBLIC_BOT_PORT",
                "Bot HTTP port 8080 is listening on a public interface.",
            )
        )

    findings.append(
        Finding(
            "INFO",
            "RU_CLIENT_PROBES_REQUIRED",
            "Container health is not an ISP/DPI test; test real VPN tunnels "
            "over multiple Russian mobile and wired operators.",
        )
    )
    findings.append(
        Finding(
            "INFO",
            "EXIT_REDUNDANCY_UNVERIFIED",
            "This snapshot does not prove independent exit ASNs or a separate "
            "subscription origin. Verify both before claiming redundancy.",
        )
    )
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--inspect", default="-", help="Docker inspect JSON file or stdin (-)"
    )
    parser.add_argument("--ports", type=Path, help="Optional ss -tulpen output")
    parser.add_argument("--json", action="store_true", help="Redacted JSON output")
    parser.add_argument("--fail-high", action="store_true")
    args = parser.parse_args(argv)

    try:
        source = (
            sys.stdin.read() if args.inspect == "-" else Path(args.inspect).read_text()
        )
        parsed = json.loads(source)
        if not isinstance(parsed, list):
            raise ValueError("Docker inspect must be a JSON array")
        ports = args.ports.read_text() if args.ports else ""
    except (OSError, ValueError) as exc:
        print(f"PREFLIGHT_INPUT_ERROR: {type(exc).__name__}", file=sys.stderr)
        return 2

    findings = audit(parsed, ports)
    if args.json:
        print(
            json.dumps(
                [asdict(item) for item in findings],
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        for item in findings:
            print(f"[{item.level}] {item.code}: {item.message}")
    return int(args.fail_high and any(item.level == "HIGH" for item in findings))


if __name__ == "__main__":
    raise SystemExit(main())
