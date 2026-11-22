"""Check egress through one local Xray SOCKS listener for a dedicated test route."""

from __future__ import annotations

import argparse
import ipaddress
import shutil
import subprocess  # nosec B404
import time
from urllib.parse import urlsplit

ROUTES = ("ru1", "ru2", "nl1", "nl2")


def check_egress(url: str, port: int, expected: str) -> bool:
    endpoint = urlsplit(url)
    if (
        endpoint.scheme != "https"
        or endpoint.hostname is None
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.query
        or endpoint.fragment
    ):
        raise ValueError("The egress endpoint must be an HTTPS URL without credentials")
    if not 1 <= port <= 65535:
        raise ValueError("The local SOCKS port is out of range")
    address = ipaddress.ip_address(expected)
    curl = shutil.which("curl")
    if curl is None:
        return False
    try:
        result = subprocess.run(  # noqa: S603  # nosec B603
            [
                curl,
                "--disable",
                "--silent",
                "--fail",
                "--proto",
                "=https",
                "--noproxy",
                "",
                "--proxy",
                f"socks5h://127.0.0.1:{port}",
                "--max-time",
                "12",
                "--max-filesize",
                "128",
                "--write-out",
                "\\n%{response_code}",
                "--",
                url,
            ],
            capture_output=True,
            check=True,
            timeout=15,
        )
        body, status = result.stdout.rsplit(b"\n", 1)
        return (
            status == b"200"
            and ipaddress.ip_address(body.decode("ascii").strip()) == address
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--route", choices=ROUTES, required=True)
    parser.add_argument("--socks-port", type=int, required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--expected-egress", required=True)
    args = parser.parse_args()
    try:
        ok = check_egress(args.endpoint, args.socks_port, args.expected_egress)
    except ValueError:
        parser.error("Invalid HTTPS endpoint, SOCKS port or expected egress IP")
    labels = f'{{route="{args.route}"}}'
    print(f"vpn_probe_egress_success{labels} {int(ok)}")
    print(f"vpn_probe_egress_checked_timestamp_seconds{labels} {int(time.time())}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
