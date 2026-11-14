"""AmneziaWG server agent: brings up the interface and manages peers.

Runs as PID 1 of the cyberdefency-awg container (stdlib only). It is a
separate AmneziaWG instance for subscribers; the owner's own Amnezia
container is never touched.

HTTP API (bearer token, only on the Docker network shared with the bot):

  GET  /healthz            liveness, no data
  GET  /v1/info            server public key, endpoint, port, MTU, DNS and
                           obfuscation parameters clients need
  GET  /v1/peers           peers with address, enabled flag and counters
  POST /v1/peers/upsert    {public_key, preshared_key, enabled, address?}
  POST /v1/peers/delete    {public_key}

Security choices:
* every command is an argument list, never a shell string;
* VPN clients cannot reach the agent, the host's private networks or each
  other: INPUT from the tunnel is dropped and forwarding to RFC1918, CGNAT
  and link-local ranges is refused before NAT to the internet;
* keys and addresses are validated before they reach ``awg``.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import signal
import subprocess  # nosec B404 -- fixed argument lists only
import sys
import tempfile
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

logger = logging.getLogger("awg-agent")

AWG = os.environ.get("AWG_BIN", "/usr/bin/awg")
AWG_GO = os.environ.get("AWG_GO_BIN", "/usr/bin/amneziawg-go")
IP = os.environ.get("IP_BIN", "/sbin/ip")
IPTABLES = os.environ.get("IPTABLES_BIN", "/sbin/iptables")
UNBOUND = os.environ.get("UNBOUND_BIN", "/usr/sbin/unbound")
# Upstreams of the filtering resolver: DNS-over-TLS, so the provider cannot read
# the names clients look up on their way out.
UPSTREAMS = (
    "1.1.1.1@853#cloudflare-dns.com",
    "1.0.0.1@853#cloudflare-dns.com",
)
BLOCKED_DESTINATIONS = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "100.64.0.0/10",
    "169.254.0.0/16",
)
MAX_BODY = 4096
PARAM_KEYS = ("jc", "jmin", "jmax", "s1", "s2", "h1", "h2", "h3", "h4")

Runner = Callable[[Sequence[str], str | None], str]


def run_command(args: Sequence[str], stdin: str | None = None) -> str:
    """Run a fixed binary with arguments; raise with stderr on failure."""
    result = subprocess.run(  # noqa: S603  # nosec B603 -- argument list, no shell
        list(args),
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{Path(args[0]).name} failed: {result.stderr.strip()[:300]}"
        )
    return result.stdout


def valid_key(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 44:
        return False
    try:
        return len(base64.b64decode(value, validate=True)) == 32
    except (binascii.Error, ValueError):
        return False


def generate_params(rng: secrets.SystemRandom | None = None) -> dict[str, int]:
    """AmneziaWG 1.0 obfuscation parameters (understood by old and new clients).

    S1/S2 pad the handshake messages and H1-H4 replace the message type
    headers; they must be identical on the server and every client. Jc/Jmin/
    Jmax control junk packets sent before a handshake.
    """
    rng = rng or secrets.SystemRandom()
    jmin = rng.randint(10, 40)
    s1 = rng.randint(15, 150)
    s2 = rng.randint(15, 150)
    while s1 + 56 == s2:  # equal sizes would make the two messages alike
        s2 = rng.randint(15, 150)
    headers: set[int] = set()
    while len(headers) < 4:
        headers.add(rng.randint(5, 2**31 - 1))
    h1, h2, h3, h4 = sorted(headers)
    return {
        "jc": rng.randint(4, 8),
        "jmin": jmin,
        "jmax": jmin + rng.randint(20, 60),
        "s1": s1,
        "s2": s2,
        "h1": h1,
        "h2": h2,
        "h3": h3,
        "h4": h4,
    }


@dataclass
class Config:
    interface: str = "awg0"
    port: int = 39051
    subnet: str = "10.67.0.0/24"
    endpoint_host: str = ""
    dns: str = "1.1.1.1, 1.0.0.1"
    # Clients use the server's own resolver, which blocks malware, phishing, adult
    # and gambling domains. Off only for debugging (AWG_DNS_FILTER=0).
    dns_filter: bool = True
    mtu: int = 1280
    state_dir: Path = Path("/var/lib/awg")
    # tmpfs for short-lived files with key material (configs, preshared keys).
    run_dir: Path = Path("/run")
    listen: str = "0.0.0.0"  # noqa: S104  # nosec B104 -- tunnel input is dropped
    api_port: int = 8090
    token: str = ""

    @classmethod
    def from_env(cls) -> Config:
        token_file = Path(
            os.environ.get("AWG_AGENT_TOKEN_FILE", "/run/secrets/awg_agent_token")
        )
        token = token_file.read_text().strip() if token_file.is_file() else ""
        token = token or os.environ.get("AWG_AGENT_TOKEN", "").strip()
        return cls(
            interface=os.environ.get("AWG_INTERFACE", "awg0"),
            port=int(os.environ.get("AWG_PORT", "39051")),
            subnet=os.environ.get("AWG_SUBNET", "10.67.0.0/24"),
            endpoint_host=os.environ.get("AWG_ENDPOINT_HOST", ""),
            dns=os.environ.get("AWG_DNS", "1.1.1.1, 1.0.0.1"),
            dns_filter=os.environ.get("AWG_DNS_FILTER", "1") != "0",
            mtu=int(os.environ.get("AWG_CLIENT_MTU", "1280")),
            state_dir=Path(os.environ.get("AWG_STATE_DIR", "/var/lib/awg")),
            api_port=int(os.environ.get("AWG_AGENT_PORT", "8090")),
            token=token,
        )

    def validate(self) -> None:
        network = ipaddress.ip_network(self.subnet, strict=True)
        if not isinstance(network, ipaddress.IPv4Network) or network.prefixlen > 28:
            raise ValueError("AWG_SUBNET must be an IPv4 network of /28 or larger")
        if not network.is_private:
            raise ValueError("AWG_SUBNET must be a private range")
        if not 1024 <= self.port <= 65535:
            raise ValueError("AWG_PORT must be 1024-65535")
        if not self.endpoint_host or any(c in self.endpoint_host for c in " /@:"):
            raise ValueError("AWG_ENDPOINT_HOST must be a host name or IPv4")
        if len(self.token) < 32:
            raise ValueError("agent token must be at least 32 characters")
        if not 576 <= self.mtu <= 1500:
            raise ValueError("AWG_CLIENT_MTU must be 576-1500")


@dataclass
class Peer:
    address: str
    preshared_key: str
    enabled: bool


@dataclass
class State:
    private_key: str
    public_key: str
    params: dict[str, int]
    peers: dict[str, Peer] = field(default_factory=dict)


BLOCK_ZONE_HEADER = (
    "# Generated by deploy/awg/dns/blocklist.py. Do not edit by hand.\n"
    "# Domains answer NXDOMAIN together with their subdomains.\n"
)


def render_resolver_conf(server_ip: str, subnet: str, blocklist: Path) -> str:
    """unbound configuration: answers the tunnel clients, blocks, forwards over TLS.

    Only the tunnel subnet and localhost may ask; everything else is refused.
    The block zone is an include file, so a rebuilt list needs only a reload.
    """
    upstreams = "".join(f"    forward-addr: {addr}\n" for addr in UPSTREAMS)
    return (
        "server:\n"
        f"    interface: {server_ip}\n"
        "    port: 53\n"
        f"    access-control: {subnet} allow\n"
        "    access-control: 127.0.0.0/8 allow\n"
        "    do-daemonize: no\n"
        "    hide-identity: yes\n"
        "    hide-version: yes\n"
        "    qname-minimisation: yes\n"
        "    harden-glue: yes\n"
        "    harden-dnssec-stripped: yes\n"
        "    tls-cert-bundle: /etc/ssl/certs/ca-certificates.crt\n"
        f'    include: "{blocklist}"\n'
        "\n"
        "forward-zone:\n"
        '    name: "."\n'
        "    forward-tls-upstream: yes\n"
        f"{upstreams}"
    )


class Agent:
    def __init__(self, config: Config, runner: Runner = run_command) -> None:
        self.config = config
        self.run = runner
        self.lock = threading.Lock()
        self.network = ipaddress.IPv4Network(config.subnet)
        self.server_ip = next(self.network.hosts())
        # With the filter, clients ask the resolver on the tunnel address only.
        self.client_dns = str(self.server_ip) if config.dns_filter else config.dns
        self._resolver: subprocess.Popen[bytes] | None = None
        self.state = self._load_or_create()

    # ----------------------------------------------------------- state
    @property
    def _server_file(self) -> Path:
        return self.config.state_dir / "server.json"

    @property
    def _peers_file(self) -> Path:
        return self.config.state_dir / "peers.json"

    def _load_or_create(self) -> State:
        self.config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self._server_file.is_file():
            server = json.loads(self._server_file.read_text())
        else:
            private_key = self.run([AWG, "genkey"], None).strip()
            server = {
                "private_key": private_key,
                "public_key": self.run([AWG, "pubkey"], private_key + "\n").strip(),
                "params": generate_params(),
            }
            self._write_json(self._server_file, server)
        peers: dict[str, Peer] = {}
        if self._peers_file.is_file():
            for key, value in json.loads(self._peers_file.read_text()).items():
                peers[key] = Peer(
                    str(value["address"]),
                    str(value["preshared_key"]),
                    bool(value["enabled"]),
                )
        params = {key: int(server["params"][key]) for key in PARAM_KEYS}
        return State(server["private_key"], server["public_key"], params, peers)

    @staticmethod
    def _write_json(path: Path, data: Any) -> None:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=1, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    def _save_peers(self) -> None:
        self._write_json(
            self._peers_file,
            {
                key: {
                    "address": peer.address,
                    "preshared_key": peer.preshared_key,
                    "enabled": peer.enabled,
                }
                for key, peer in self.state.peers.items()
            },
        )

    # --------------------------------------------------------- interface
    def server_conf(self) -> str:
        p = self.state.params
        lines = [
            "[Interface]",
            f"PrivateKey = {self.state.private_key}",
            f"ListenPort = {self.config.port}",
            f"Jc = {p['jc']}",
            f"Jmin = {p['jmin']}",
            f"Jmax = {p['jmax']}",
            f"S1 = {p['s1']}",
            f"S2 = {p['s2']}",
            f"H1 = {p['h1']}",
            f"H2 = {p['h2']}",
            f"H3 = {p['h3']}",
            f"H4 = {p['h4']}",
        ]
        for key, peer in self.state.peers.items():
            if peer.enabled:
                lines += [
                    "",
                    "[Peer]",
                    f"PublicKey = {key}",
                    f"PresharedKey = {peer.preshared_key}",
                    f"AllowedIPs = {peer.address}/32",
                ]
        return "\n".join(lines) + "\n"

    def up(self) -> None:
        iface = self.config.interface
        self.run([AWG_GO, iface], None)
        conf = self.config.run_dir / f"{iface}.conf"
        conf.write_text(self.server_conf())
        os.chmod(conf, 0o600)
        try:
            self.run([AWG, "setconf", iface, str(conf)], None)
        finally:
            conf.unlink(missing_ok=True)
        prefix = self.network.prefixlen
        self.run(
            [IP, "address", "add", f"{self.server_ip}/{prefix}", "dev", iface], None
        )
        self.run([IP, "link", "set", "dev", iface, "mtu", "1420", "up"], None)
        self._firewall(iface)
        if self.config.dns_filter:
            self.start_resolver()

    def _firewall(self, iface: str) -> None:
        """Clients reach the internet only: not this host, private nets or peers.

        NAT applies to whichever interface routes the packet (never the tunnel),
        so it does not depend on the order in which Docker attaches networks.
        """
        rules: list[list[str]] = []
        if self.config.dns_filter:
            # The resolver on the tunnel address is the only DNS clients may reach.
            for proto in ("udp", "tcp"):
                rules.append(
                    ["-A", "INPUT", "-i", iface, "-p", proto, "--dport", "53",
                     "-j", "ACCEPT"]
                )  # fmt: skip
        rules += [
            ["-A", "INPUT", "-i", iface, "-j", "DROP"],
            ["-A", "FORWARD", "-i", iface, "-o", iface, "-j", "DROP"],
        ]
        rules += [
            ["-A", "FORWARD", "-i", iface, "-d", target, "-j", "DROP"]
            for target in BLOCKED_DESTINATIONS
        ]
        if self.config.dns_filter:
            # Public resolvers, plain and over TLS, are refused: asking one of them
            # directly would skip the filter.
            for proto, port in (
                ("udp", "53"),
                ("tcp", "53"),
                ("tcp", "853"),
                ("udp", "853"),
            ):
                rules.append(
                    ["-A", "FORWARD", "-i", iface, "-p", proto, "--dport", port,
                     "-j", "DROP"]
                )  # fmt: skip
        rules += [
            ["-A", "FORWARD", "-i", iface, "!", "-o", iface, "-j", "ACCEPT"],
            [
                "-A", "FORWARD", "-o", iface, "-m", "conntrack",
                "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT",
            ],
            ["-A", "FORWARD", "-o", iface, "-j", "DROP"],
            [
                "-t", "nat", "-A", "POSTROUTING", "-s", str(self.network),
                "!", "-o", iface, "-j", "MASQUERADE",
            ],
        ]  # fmt: skip
        for rule in rules:
            self.run([IPTABLES, *rule], None)

    def down(self) -> None:
        self.stop_resolver()
        with contextlib.suppress(RuntimeError):
            self.run([IP, "link", "del", "dev", self.config.interface], None)

    # ------------------------------------------------------------ resolver
    @property
    def _dns_dir(self) -> Path:
        return self.config.state_dir / "dns"

    def start_resolver(self) -> None:
        """Start unbound on the tunnel address with the current block zone."""
        self._dns_dir.mkdir(parents=True, exist_ok=True)
        blocklist = self._dns_dir / "blocklist.conf"
        if not blocklist.is_file():
            # Nothing blocked until the first list is built; the file is still valid.
            blocklist.write_text(BLOCK_ZONE_HEADER, encoding="utf-8")
        conf = self._dns_dir / "unbound.conf"
        conf.write_text(
            render_resolver_conf(str(self.server_ip), self.config.subnet, blocklist),
            encoding="utf-8",
        )
        if self._resolver is not None and self._resolver.poll() is None:
            return
        self._resolver = subprocess.Popen(  # noqa: S603  # nosec B603 -- fixed argv
            [UNBOUND, "-d", "-c", str(conf)]
        )
        logger.info("dns resolver started on %s", self.server_ip)

    def reload_resolver(self) -> None:
        """Pick up a rebuilt block zone; unbound re-reads its files on SIGHUP."""
        if self._resolver is not None and self._resolver.poll() is None:
            self._resolver.send_signal(signal.SIGHUP)
            logger.info("dns resolver reloaded")

    def stop_resolver(self) -> None:
        if self._resolver is not None and self._resolver.poll() is None:
            self._resolver.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self._resolver.wait(timeout=5)

    # ------------------------------------------------------------- peers
    def _allocate(self, wanted: str | None, owner: str | None = None) -> str:
        used = {peer.address for key, peer in self.state.peers.items() if key != owner}
        if wanted:
            try:
                address = ipaddress.IPv4Address(wanted)
            except ValueError:
                address = None
            if (
                address is not None
                and address in self.network
                and address not in (self.server_ip, self.network.broadcast_address)
                and str(address) not in used
            ):
                return str(address)
        for host in self.network.hosts():
            if host != self.server_ip and str(host) not in used:
                return str(host)
        raise RuntimeError("address pool exhausted")

    def _apply(self, key: str, peer: Peer) -> None:
        iface = self.config.interface
        if not peer.enabled:
            self.run([AWG, "set", iface, "peer", key, "remove"], None)
            return
        fd, psk_file = tempfile.mkstemp(dir=self.config.run_dir, prefix=".psk-")
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(peer.preshared_key + "\n")
            self.run(
                [
                    AWG, "set", iface, "peer", key, "preshared-key", psk_file,
                    "allowed-ips", f"{peer.address}/32",
                ],
                None,
            )  # fmt: skip
        finally:
            Path(psk_file).unlink(missing_ok=True)

    def upsert(self, body: dict[str, Any]) -> dict[str, Any]:
        key, psk = body.get("public_key"), body.get("preshared_key")
        enabled = body.get("enabled")
        if not valid_key(key) or not valid_key(psk) or not isinstance(enabled, bool):
            raise ValueError("invalid peer")
        assert isinstance(key, str)  # nosec B101
        assert isinstance(psk, str)  # nosec B101
        wanted = body.get("address")
        with self.lock:
            current = self.state.peers.get(key)
            wanted_address = wanted if isinstance(wanted, str) else None
            if current is not None and wanted_address in (None, current.address):
                address = current.address
            else:
                # New peer, or the bot asks to restore the address it stored.
                address = self._allocate(wanted_address, owner=key)
            peer = Peer(address, psk, enabled)
            self._apply(key, peer)
            self.state.peers[key] = peer
            self._save_peers()
        return {"address": address, "enabled": enabled}

    def delete(self, body: dict[str, Any]) -> dict[str, Any]:
        key = body.get("public_key")
        if not valid_key(key):
            raise ValueError("invalid peer")
        assert isinstance(key, str)  # nosec B101
        with self.lock:
            peer = self.state.peers.pop(key, None)
            if peer is not None and peer.enabled:
                self.run(
                    [AWG, "set", self.config.interface, "peer", key, "remove"], None
                )
            self._save_peers()
        return {"deleted": peer is not None}

    def peers(self) -> dict[str, Any]:
        counters: dict[str, tuple[int, int, int]] = {}
        dump = self.run([AWG, "show", self.config.interface, "dump"], None)
        for line in dump.splitlines()[1:]:
            fields = line.split("\t")
            if len(fields) >= 7:
                counters[fields[0]] = (int(fields[4]), int(fields[5]), int(fields[6]))
        with self.lock:
            items = list(self.state.peers.items())
        return {
            "peers": [
                {
                    "public_key": key,
                    "address": peer.address,
                    "enabled": peer.enabled,
                    "latest_handshake": counters.get(key, (0, 0, 0))[0],
                    "rx_bytes": counters.get(key, (0, 0, 0))[1],
                    "tx_bytes": counters.get(key, (0, 0, 0))[2],
                }
                for key, peer in items
            ]
        }

    def info(self) -> dict[str, Any]:
        return {
            "public_key": self.state.public_key,
            "endpoint_host": self.config.endpoint_host,
            "port": self.config.port,
            "mtu": self.config.mtu,
            "dns": self.client_dns,
            "params": dict(self.state.params),
        }


def make_handler(agent: Agent) -> type[BaseHTTPRequestHandler]:
    expected = f"Bearer {agent.config.token}"

    class Handler(BaseHTTPRequestHandler):
        server_version = "awg-agent"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:
            return  # no access log: requests would reveal subscriber keys

        def _send(self, status: HTTPStatus, data: dict[str, Any]) -> None:
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            return hmac.compare_digest(self.headers.get("Authorization", ""), expected)

        def do_GET(self) -> None:
            if self.path == "/healthz":
                self._send(HTTPStatus.OK, {"ok": True})
            elif not self._authorized():
                self._send(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            elif self.path == "/v1/info":
                self._send(HTTPStatus.OK, agent.info())
            elif self.path == "/v1/peers":
                self._handle(agent.peers)
            else:
                self._send(HTTPStatus.NOT_FOUND, {"error": "not_found"})

        def do_POST(self) -> None:
            if not self._authorized():
                self._send(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            actions = {
                "/v1/peers/upsert": agent.upsert,
                "/v1/peers/delete": agent.delete,
            }
            action = actions.get(self.path)
            if action is None:
                self._send(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY:
                self._send(HTTPStatus.BAD_REQUEST, {"error": "invalid_body"})
                return
            try:
                body = json.loads(self.rfile.read(length))
            except ValueError:
                self._send(HTTPStatus.BAD_REQUEST, {"error": "invalid_body"})
                return
            if not isinstance(body, dict):
                self._send(HTTPStatus.BAD_REQUEST, {"error": "invalid_body"})
                return
            self._handle(lambda: action(body))

        def _handle(self, call: Callable[[], dict[str, Any]]) -> None:
            try:
                self._send(HTTPStatus.OK, call())
            except ValueError as exc:
                self._send(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except RuntimeError as exc:
                logger.error("agent_command_failed: %s", exc)
                self._send(
                    HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "command_failed"}
                )

    return Handler


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    config = Config.from_env()
    config.validate()
    agent = Agent(config)
    agent.up()
    server = ThreadingHTTPServer((config.listen, config.api_port), make_handler(agent))

    def stop(*_: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    # `docker kill -s HUP` after a rebuilt block list: the resolver re-reads it.
    signal.signal(signal.SIGHUP, lambda *_: agent.reload_resolver())
    logger.info(
        "awg agent ready: port=%s peers=%d", config.port, len(agent.state.peers)
    )
    try:
        server.serve_forever()
    finally:
        agent.down()


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError) as exc:
        sys.stderr.write(f"awg agent cannot start: {exc}\n")
        raise SystemExit(2) from None
