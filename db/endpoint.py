"""Address rules shared by the database gateway, its client and the settings.

Standard library only, so ``config`` can import it without pulling in aiohttp.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

PROTOCOL_VERSION = 1
MIN_TOKEN_LENGTH = 32
# Named explicitly: ``ipaddress.is_private`` also covers 0.0.0.0 (every
# interface), link-local and documentation ranges.
PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "127.0.0.0/8",
        "::1/128",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "fc00::/7",
    )
)


def is_private_host(host: str) -> bool:
    """True for a literal loopback, RFC 1918 or IPv6 unique-local address."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(address in network for network in PRIVATE_NETWORKS)


def validate_gateway_url(url: str) -> str:
    """``http://<loopback or private address>:port`` or any ``https://`` URL.

    Plain HTTP carries personal data and the token, so it is accepted only
    for an address inside the private tunnel between the servers.
    """
    try:
        parts = urlsplit(url.strip())
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        raise ValueError("database_url is not a valid URL") from None
    if (
        parts.scheme not in {"http", "https"}
        or not hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or parts.path not in {"", "/"}
    ):
        raise ValueError("database_url must look like http://10.0.0.1:8444")
    if parts.scheme == "http" and (port is None or not is_private_host(hostname)):
        raise ValueError(
            "database_url over plain http must name a loopback or private address "
            "and a port (the tunnel between the servers); use https otherwise"
        )
    return f"{parts.scheme}://{parts.netloc}"
