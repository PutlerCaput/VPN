#!/usr/bin/env python3
"""Render payment-only RU/DE gateway configs; never install or reload services."""

from __future__ import annotations

import argparse
import json
import re
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path

PRIVATE_NETWORKS = tuple(
    IPv4Network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
DOMAIN_RE = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
)


def private_ip(value: str) -> str:
    address = IPv4Address(value)
    if not any(address in network for network in PRIVATE_NETWORKS):
        raise ValueError("inter-server addresses must be RFC1918 IPv4")
    return str(address)


def render(domain: str, ru_ip: str, de_ip: str) -> dict[str, str]:
    domain = domain.lower()
    if not DOMAIN_RE.fullmatch(domain):
        raise ValueError("domain must be an ASCII hostname without scheme or path")
    ru_ip, de_ip = private_ip(ru_ip), private_ip(de_ip)
    if ru_ip == de_ip:
        raise ValueError("RU and DE tunnel addresses must differ")
    squid = f"""# RU: bind only to the authenticated WireGuard interface.
http_port {ru_ip}:3128
acl de_bot src {de_ip}/32
acl CONNECT method CONNECT
acl payment_tls port 443
acl payment_host dstdomain -n api.yookassa.ru
http_access allow de_bot CONNECT payment_tls payment_host
http_access deny all
cache deny all
cache_mem 8 MB
access_log none
cache_log /dev/null
cache_store_log none
logfile_rotate 0
forwarded_for delete
via off
connect_timeout 10 seconds
request_timeout 20 seconds
client_lifetime 5 minutes
shutdown_lifetime 5 seconds
forward_max_tries 1
pid_filename /run/node-payment-proxy/squid.pid
coredump_dir /run/node-payment-proxy
"""
    # RU is included in the system nginx. DE uses an isolated nginx process so
    # it never competes with the existing Caddy listener.
    common = """    client_max_body_size 64k;
    client_body_buffer_size 64k;
    client_body_timeout 10s;
    access_log off;
    error_log /dev/null crit;
    server_tokens off;
"""
    forwarded = """        limit_except POST { deny all; }
        proxy_http_version 1.1;
        proxy_pass_request_headers off;
        proxy_set_header Host payment-backend;
        proxy_set_header Content-Type application/json;
        proxy_set_header Connection "";
        proxy_connect_timeout 3s;
        proxy_send_timeout 20s;
        proxy_read_timeout 25s;
        proxy_next_upstream off;
        proxy_buffering off;
        proxy_max_temp_file_size 0;
"""
    de_forwarded = "".join(
        f"    {line}" if line.strip() else line
        for line in forwarded.splitlines(keepends=True)
    )
    ru_nginx = f"""# RU: loopback-only; Cloudflare Tunnel forwards here.
limit_req_zone $server_name zone=node_payment_notifications:1m rate=100r/s;
server {{
    listen 127.0.0.1:18080;
    server_name {domain};
{common}
    location = /yookassa/webhook {{
        limit_req zone=node_payment_notifications burst=200 nodelay;
        limit_req_status 429;
{forwarded}
        proxy_pass http://{de_ip}:18081;
    }}
    location / {{ return 404; }}
}}
"""
    de_nginx = f"""# DE: standalone config for node-payment-backend.service.
worker_processes 1;
pid /run/node-payment-backend/nginx.pid;
error_log /dev/null crit;

events {{
    worker_connections 256;
}}

http {{
    client_max_body_size 64k;
    client_body_buffer_size 64k;
    client_body_timeout 10s;
    client_body_temp_path /run/node-payment-backend/client_temp;
    proxy_temp_path /run/node-payment-backend/proxy_temp;
    fastcgi_temp_path /run/node-payment-backend/fastcgi_temp;
    uwsgi_temp_path /run/node-payment-backend/uwsgi_temp;
    scgi_temp_path /run/node-payment-backend/scgi_temp;
    access_log off;
    server_tokens off;

    server {{
        listen {de_ip}:18081;
        server_name payment-backend;
        allow {ru_ip};
        deny all;

        location = /yookassa/webhook {{
{de_forwarded}
            proxy_pass http://127.0.0.1:8080;
        }}
        location / {{ return 404; }}
    }}
}}
"""
    backend_unit = """[Unit]
Description=Private payment webhook proxy to the bot
Requires=wg-quick@wg-payments.service
After=network-online.target wg-quick@wg-payments.service
BindsTo=wg-quick@wg-payments.service

[Service]
Type=simple
User=www-data
Group=www-data
RuntimeDirectory=node-payment-backend
RuntimeDirectoryMode=0700
ExecStartPre=/usr/bin/mkdir -p /run/node-payment-backend/client_temp
ExecStartPre=/usr/bin/mkdir -p /run/node-payment-backend/proxy_temp
ExecStartPre=/usr/bin/mkdir -p /run/node-payment-backend/fastcgi_temp
ExecStartPre=/usr/bin/mkdir -p /run/node-payment-backend/uwsgi_temp
ExecStartPre=/usr/bin/mkdir -p /run/node-payment-backend/scgi_temp
ExecStartPre=/usr/sbin/nginx -t -c /etc/node-payment-edge/de-nginx.conf
ExecStart=/usr/sbin/nginx -c /etc/node-payment-edge/de-nginx.conf -g "daemon off;"
Restart=on-failure
RestartSec=5
TimeoutStopSec=15
NoNewPrivileges=true
CapabilityBoundingSet=
AmbientCapabilities=
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
RestrictSUIDSGID=true
LockPersonality=true
LimitCORE=0
UMask=0077
MemoryMax=64M
TasksMax=32
ReadWritePaths=/run/node-payment-backend

[Install]
WantedBy=multi-user.target
"""
    unit = """[Unit]
Description=Restricted payment API egress on RU
Requires=wg-quick@wg-payments.service
After=network-online.target wg-quick@wg-payments.service
BindsTo=wg-quick@wg-payments.service

[Service]
Type=simple
User=proxy
Group=proxy
RuntimeDirectory=node-payment-proxy
RuntimeDirectoryMode=0700
ExecStartPre=/usr/sbin/squid -k parse -f /etc/node-payment-edge/squid.conf
ExecStart=/usr/sbin/squid -N -f /etc/node-payment-edge/squid.conf
Restart=on-failure
RestartSec=5
TimeoutStopSec=15
NoNewPrivileges=true
CapabilityBoundingSet=
AmbientCapabilities=
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
RestrictSUIDSGID=true
LockPersonality=true
LimitCORE=0
UMask=0077
MemoryMax=192M
TasksMax=32
ReadWritePaths=/run/node-payment-proxy

[Install]
WantedBy=multi-user.target
"""
    routing = f"""# DE bot only. Merge after the private gateway passes checks.
YOOKASSA_EGRESS=ru_gateway
YOOKASSA_PROXY_URL=http://{ru_ip}:3128
"""
    manifest = {
        "status": "prepared_not_deployed",
        "payment_domain": domain,
        "webhook_url": f"https://{domain}/yookassa/webhook",
        "ru_private_ip": ru_ip,
        "de_private_ip": de_ip,
        "wireguard_interface": "wg-payments",
        "api_allowlist": ["api.yookassa.ru:443"],
        "ru_public_ip_expected": "45.138.157.65",
        "de_public_ip_expected": "89.125.210.172",
        "cloudflare": {
            "hostname": domain,
            "origin_service": "http://127.0.0.1:18080",
            "path": "/yookassa/webhook",
            "mode": "named tunnel; proxied CNAME to <TUNNEL-UUID>.cfargotunnel.com",
            "public_ru_443_listener": False,
        },
        "prerequisites": [
            (
                "create the Cloudflare Tunnel and map only "
                "pay.cyberdefency.com to localhost:18080"
            ),
            "verify pay hostname DNS ownership and existing records",
            "verify tunnel subnet has no overlap on either server or Docker",
            "generate private tunnel keys locally on each server",
            "allow only DE to RU:3128 and RU to DE:18081 inside the tunnel",
            (
                "keep RU's current VPN DNAT on 443 and DE Caddy unchanged; "
                "use a dedicated DE webhook proxy on wg-payments:18081"
            ),
            "test payment mode in an isolated DB with no production provisioning",
        ],
    }
    return {
        "ru-squid.conf": squid,
        "ru-nginx.conf": ru_nginx,
        "de-nginx.conf": de_nginx,
        "node-payment-proxy.service": unit,
        "node-payment-backend.service": backend_unit,
        "de-payment-routing.env": routing,
        "manifest.json": json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    }


def write_bundle(output: Path, files: dict[str, str]) -> None:
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name, content in files.items():
        with (output / name).open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        (output / name).chmod(0o600)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--ru-link-ip", required=True)
    parser.add_argument("--de-link-ip", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    files = render(args.domain, args.ru_link_ip, args.de_link_ip)
    write_bundle(args.output_dir, files)
    print(f"PAYMENT_EDGE_PREPARED={args.output_dir}")
    print("No production changes, DNS updates, keys or certificates were created.")


if __name__ == "__main__":
    main()
