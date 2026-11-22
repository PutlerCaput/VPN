"""Exercise generated HAProxy and Xray configs in a private CI Docker network."""

from __future__ import annotations

import json
import subprocess  # nosec B404
import sys
import tempfile
import time
import uuid
from pathlib import Path

from deploy.four_node.generate import (
    HAPROXY_IMAGE,
    XRAY_IMAGE,
    Credentials,
    fresh_credentials,
    haproxy_config,
    xray_config,
)

SUBNET = "172.30.239.0/24"
HOSTS = {
    "ru1": "172.30.239.11",
    "ru2": "172.30.239.12",
    "nl1": "172.30.239.21",
    "nl2": "172.30.239.22",
    "fixture": "172.30.239.31",
    "client": "172.30.239.41",
}
SNI = "cover.example.org"
PYTHON = "python:3.11-bookworm"

FIXTURE = """
import http.server
import ssl
from threading import Thread

class Origin(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        result = self.client_address[0].encode("ascii")
        self.send_response(200)
        self.send_header("Content-Length", str(len(result)))
        self.end_headers()
        self.wfile.write(result)

egress = http.server.ThreadingHTTPServer(("0.0.0.0", 8080), Origin)
Thread(target=egress.serve_forever, daemon=True).start()
cover = http.server.ThreadingHTTPServer(("0.0.0.0", 443), Origin)
tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
tls.minimum_version = ssl.TLSVersion.TLSv1_3
tls.set_alpn_protocols(["h2"])
tls.load_cert_chain("/fixture/cert.pem", "/fixture/key.pem")
cover.socket = tls.wrap_socket(cover.socket, server_side=True)
cover.serve_forever()
"""


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  # nosec B603
        args, check=check, capture_output=True, text=True, timeout=120
    )


def client_config(cases: list[tuple[str, Credentials]]) -> dict[str, object]:
    inbounds: list[dict[str, object]] = []
    outbounds: list[dict[str, object]] = []
    rules: list[dict[str, object]] = []
    for index, (host, creds) in enumerate(cases):
        tag = f"test-{index}"
        inbounds.append(
            {
                "tag": tag,
                "listen": HOSTS["client"],
                "port": 1080 + index,
                "protocol": "socks",
                "settings": {"auth": "noauth"},
            }
        )
        outbounds.append(
            {
                "tag": tag,
                "protocol": "vless",
                "settings": {
                    "vnext": [
                        {
                            "address": HOSTS[host],
                            "port": 443,
                            "users": [
                                {
                                    "id": creds.user_id,
                                    "encryption": "none",
                                    "flow": "xtls-rprx-vision",
                                },
                            ],
                        }
                    ]
                },
                "streamSettings": {
                    "network": "raw",
                    "security": "reality",
                    "realitySettings": {
                        "fingerprint": "chrome",
                        "serverName": SNI,
                        "password": creds.public,
                        "shortId": creds.short_id,
                    },
                },
            }
        )
        rules.append({"type": "field", "inboundTag": [tag], "outboundTag": tag})
    return {
        "log": {"loglevel": "warning"},
        "inbounds": inbounds,
        "outbounds": outbounds,
        "routing": {"rules": rules},
    }


def get_egress(client_host: str, port: int) -> str | None:
    result = run(
        "docker",
        "exec",
        client_host,
        "curl",
        "--disable",
        "--silent",
        "--fail",
        "--max-time",
        "3",
        "--noproxy",
        "",
        "--proxy",
        f"socks5h://{HOSTS['client']}:{port}",
        f"http://{HOSTS['fixture']}:8080/",
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def wait_for_egress(client_host: str, port: int, exit_ip: str) -> None:
    for _ in range(15):
        observed = get_egress(client_host, port)
        if observed == exit_ip:
            return
        if observed is not None:
            raise RuntimeError(f"SOCKS port {port}: traffic exited via the wrong node")
        time.sleep(1)
    raise RuntimeError(f"SOCKS port {port}: no successful tunnel")


def scenario(directory: Path, xray_cli: str) -> None:
    run(
        "openssl",
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-days",
        "1",
        "-subj",
        f"/CN={SNI}",
        "-addext",
        f"subjectAltName=DNS:{SNI}",
        "-keyout",
        str(directory / "key.pem"),
        "-out",
        str(directory / "cert.pem"),
    )
    nl1, nl2 = fresh_credentials(xray_cli), fresh_credentials(xray_cli)
    pairing = [("ru1", nl1), ("ru2", nl2), ("nl1", nl1), ("nl2", nl2)]
    unissued = Credentials(nl1.private, nl1.public, nl1.short_id, str(uuid.uuid4()))
    cases = [*pairing, ("ru1", nl2), ("ru1", unissued)]
    client = directory / "client.json"
    client.write_text(json.dumps(client_config(cases)), encoding="utf-8")
    network = f"four-node-{uuid.uuid4().hex[:10]}"
    containers: dict[str, str] = {}

    def start(role: str, mount: str, image: str, *command: str) -> None:
        name = f"{network}-{role}"
        run(
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--user",
            "0:0",
            "--network",
            network,
            "--ip",
            HOSTS[role],
            "--mount",
            mount,
            image,
            *command,
        )
        containers[role] = name

    run("docker", "network", "create", "--internal", "--subnet", SUBNET, network)
    try:
        start(
            "fixture",
            f"type=bind,src={directory},dst=/fixture,readonly",
            PYTHON,
            "python",
            "-c",
            FIXTURE,
        )
        for role, creds in (("nl1", nl1), ("nl2", nl2)):
            path = directory / f"{role}.json"
            config = xray_config(
                {"server_name": SNI, "target": f"{HOSTS['fixture']}:443"}, creds
            )
            # Xray blocks private egress by default. Permit only the fixture
            # address and port in this internal test network.
            config["outbounds"] = [
                {
                    "protocol": "freedom",
                    "tag": "direct",
                    "settings": {
                        "finalRules": [
                            {
                                "action": "allow",
                                "ip": [f"{HOSTS['fixture']}/32"],
                                "port": "8080",
                                "network": "tcp",
                            }
                        ]
                    },
                }
            ]
            path.write_text(json.dumps(config), encoding="utf-8")
            start(
                role,
                f"type=bind,src={path},dst=/etc/xray/config.json,readonly",
                XRAY_IMAGE,
                "run",
                "-c",
                "/etc/xray/config.json",
            )
        for role, backend in (("ru1", "nl1"), ("ru2", "nl2")):
            path = directory / f"{role}.cfg"
            path.write_text(haproxy_config(HOSTS[backend]), encoding="utf-8")
            start(
                role,
                f"type=bind,src={path},dst=/usr/local/etc/haproxy/haproxy.cfg,readonly",
                HAPROXY_IMAGE,
            )
        start(
            "client",
            f"type=bind,src={client},dst=/etc/xray/config.json,readonly",
            XRAY_IMAGE,
            "run",
            "-c",
            "/etc/xray/config.json",
        )
        fixture = containers["fixture"]
        for index, (role, _) in enumerate(pairing):
            exit_ip = HOSTS["nl1" if role.endswith("1") else "nl2"]
            wait_for_egress(fixture, 1080 + index, exit_ip)
            print(f"{role}: tunnel reaches paired egress", flush=True)
        if (
            get_egress(fixture, 1084) is not None
            or get_egress(fixture, 1085) is not None
        ):
            raise RuntimeError("Unexpected tunnel with the wrong key or unissued UUID")

        run("docker", "stop", "--time", "1", containers["nl1"])
        if get_egress(fixture, 1080) is not None:
            raise RuntimeError("RU-1 still forwarded after its fixed exit stopped")
        wait_for_egress(fixture, 1081, HOSTS["nl2"])
        print("RU-2 remains available with NL-1 stopped", flush=True)
    except (RuntimeError, subprocess.SubprocessError):
        for role, container in containers.items():
            logs = run("docker", "logs", "--tail", "12", container, check=False)
            print(f"{role}: {logs.stdout}{logs.stderr}", file=sys.stderr)
        raise
    finally:
        if containers:
            run("docker", "rm", "-f", *containers.values(), check=False)
        run("docker", "network", "rm", network, check=False)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="four-node-ci-") as temp:
        scenario(Path(temp), sys.argv[1])
