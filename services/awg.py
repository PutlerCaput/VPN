"""AmneziaWG: a second protocol with one key per device.

AmneziaWG is WireGuard with traffic masking (junk packets, padded handshakes,
custom headers) that DPI systems find harder to recognise. Each subscriber
device gets its own key pair, so a device can be removed on its own, and the
number of keys is limited by the tier's device limit.

The bot is the source of truth. A reconciliation job makes the server agent
match it: peers are enabled only for active subscriptions, deleted devices
disappear, and AmneziaWG traffic counts towards the same 30-day quota as
VLESS. Hiddify Manager stays the meter of record: the bot lowers the user's
panel limit by the AmneziaWG traffic of the current panel window, so whichever
protocol is used, the total stays within the tariff.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import math
import os
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from config import GIB, TIERS, Settings
from db.database import Database
from services.audit import audit
from services.hiddify import HiddifyClient, HiddifyError

logger = logging.getLogger(__name__)

INFO_CACHE_SECONDS = 600.0
# Panel limits are adjusted in steps, so the panel is not edited on every
# poll while AmneziaWG traffic grows.
QUOTA_STEP_BYTES = 100 * 1024**2
MIN_PANEL_LIMIT = 1024**2
CLIENT_ALLOWED_IPS = "0.0.0.0/0, ::/0"  # IPv6 too: nothing leaks around the tunnel
PARAM_KEYS = ("jc", "jmin", "jmax", "s1", "s2", "h1", "h2", "h3", "h4")


class AwgError(Exception):
    pass


def utc_day() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


async def combined_usage(
    db: Database, telegram_id: int, used: int, lifetime: int, limit: int
) -> tuple[int, int]:
    """(used, limit) including AmneziaWG traffic of the same panel window.

    The bot lowers the panel limit by the AmneziaWG traffic (rounded up to
    QUOTA_STEP_BYTES); adding both back shows the tariff's real quota.
    """
    row = await db.get_awg_usage(telegram_id)
    window = max(0, lifetime - used)
    if row is None or int(row["window_key"]) != window or int(row["bytes"]) <= 0:
        return used, limit
    extra = int(row["bytes"])
    return used + extra, (limit + quantize(extra) if limit > 0 else 0)


class AwgLimitError(AwgError):
    pass


class AwgInactiveError(AwgError):
    pass


def generate_keypair() -> tuple[str, str]:
    """(private, public) X25519 keys in WireGuard's base64 form."""
    private = X25519PrivateKey.generate()
    raw_private = private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    raw_public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return (
        base64.b64encode(raw_private).decode(),
        base64.b64encode(raw_public).decode(),
    )


def generate_psk() -> str:
    return base64.b64encode(os.urandom(32)).decode()


def quantize(value: int) -> int:
    return math.ceil(max(0, value) / QUOTA_STEP_BYTES) * QUOTA_STEP_BYTES


@dataclass(frozen=True, slots=True)
class ServerInfo:
    public_key: str
    endpoint_host: str
    port: int
    mtu: int
    dns: str
    params: dict[str, int]

    @classmethod
    def parse(cls, data: dict[str, Any]) -> ServerInfo:
        try:
            params = {key: int(data["params"][key]) for key in PARAM_KEYS}
            return cls(
                public_key=str(data["public_key"]),
                endpoint_host=str(data["endpoint_host"]),
                port=int(data["port"]),
                mtu=int(data["mtu"]),
                dns=str(data["dns"]),
                params=params,
            )
        except (KeyError, TypeError, ValueError):
            raise AwgError("agent info malformed") from None


def render_config(device: dict[str, Any], info: ServerInfo) -> str:
    """Client config understood by AmneziaVPN, AmneziaWG apps and routers."""
    p = info.params
    return (
        "[Interface]\n"
        f"PrivateKey = {device['private_key']}\n"
        f"Address = {device['address']}/32\n"
        f"DNS = {info.dns}\n"
        f"MTU = {info.mtu}\n"
        f"Jc = {p['jc']}\n"
        f"Jmin = {p['jmin']}\n"
        f"Jmax = {p['jmax']}\n"
        f"S1 = {p['s1']}\n"
        f"S2 = {p['s2']}\n"
        f"H1 = {p['h1']}\n"
        f"H2 = {p['h2']}\n"
        f"H3 = {p['h3']}\n"
        f"H4 = {p['h4']}\n"
        "\n"
        "[Peer]\n"
        f"PublicKey = {info.public_key}\n"
        f"PresharedKey = {device['preshared_key']}\n"
        f"Endpoint = {info.endpoint_host}:{info.port}\n"
        f"AllowedIPs = {CLIENT_ALLOWED_IPS}\n"
        "PersistentKeepalive = 25\n"
    )


class AwgAgent:
    """Client of deploy/awg/agent.py over the private Docker network."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={"Authorization": f"Bearer {token}"},
            trust_env=False,  # never through an outbound proxy
        )
        self._info: tuple[float, ServerInfo] | None = None

    async def close(self) -> None:
        await self._client.aclose()

    async def _call(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            response = await self._client.request(method, path, json=body)
        except httpx.HTTPError as exc:
            raise AwgError(f"agent unreachable: {type(exc).__name__}") from None
        if response.status_code != 200:
            raise AwgError(f"agent HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError:
            raise AwgError("agent payload malformed") from None
        if not isinstance(data, dict):
            raise AwgError("agent payload malformed")
        return data

    async def info(self) -> ServerInfo:
        current = time.monotonic()
        if self._info is not None and current - self._info[0] < INFO_CACHE_SECONDS:
            return self._info[1]
        info = ServerInfo.parse(await self._call("GET", "/v1/info"))
        self._info = (current, info)
        return info

    async def peers(self) -> dict[str, dict[str, Any]]:
        data = await self._call("GET", "/v1/peers")
        peers = data.get("peers")
        if not isinstance(peers, list):
            raise AwgError("agent peers malformed")
        return {
            str(peer["public_key"]): peer
            for peer in peers
            if isinstance(peer, dict) and "public_key" in peer
        }

    async def upsert(
        self,
        public_key: str,
        preshared_key: str,
        enabled: bool,
        address: str | None = None,
    ) -> str:
        body: dict[str, Any] = {
            "public_key": public_key,
            "preshared_key": preshared_key,
            "enabled": enabled,
        }
        if address:
            body["address"] = address
        data = await self._call("POST", "/v1/peers/upsert", body)
        address_out = data.get("address")
        if not isinstance(address_out, str):
            raise AwgError("agent upsert malformed")
        return address_out

    async def delete(self, public_key: str) -> None:
        await self._call("POST", "/v1/peers/delete", {"public_key": public_key})


class AwgService:
    def __init__(
        self,
        db: Database,
        agent: AwgAgent,
        hiddify: HiddifyClient,
        settings: Settings,
    ) -> None:
        self.db = db
        self.agent = agent
        self.hiddify = hiddify
        self.settings = settings
        # One lock per owner: the count-then-create in add_device and the
        # delete in remove_device must not interleave, or many parallel taps
        # would each pass the device-limit check and create more peers than the
        # tier allows (a single-process bot, so an asyncio lock is enough).
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    # ---------------------------------------------------------- devices
    async def device_limit(self, user: dict[str, Any]) -> int:
        tier = TIERS.get(str(user.get("tier") or ""))
        return tier.devices if tier else 1

    async def devices(self, telegram_id: int) -> list[dict[str, Any]]:
        return await self.db.awg_devices(telegram_id)

    async def add_device(self, telegram_id: int) -> dict[str, Any]:
        async with self._locks[telegram_id]:
            return await self._add_device(telegram_id)

    async def _add_device(self, telegram_id: int) -> dict[str, Any]:
        user = await self.db.get_user(telegram_id)
        if user is None or user["status"] != "active":
            raise AwgInactiveError("subscription inactive")
        existing = await self.db.awg_devices(telegram_id)
        if len(existing) >= await self.device_limit(user):
            raise AwgLimitError("device limit reached")
        names = {str(device["name"]) for device in existing}
        number = next(
            n for n in range(1, len(existing) + 2) if f"Устройство {n}" not in names
        )
        private_key, public_key = generate_keypair()
        preshared_key = generate_psk()
        address = await self.agent.upsert(public_key, preshared_key, True)
        device = await self.db.add_awg_device(
            telegram_id=telegram_id,
            name=f"Устройство {number}",
            public_key=public_key,
            private_key=private_key,
            preshared_key=preshared_key,
            address=address,
        )
        await audit(self.db, telegram_id, "awg_device_added", str(device["id"]))
        return device

    async def remove_device(self, telegram_id: int, device_id: int) -> bool:
        async with self._locks[telegram_id]:
            device = await self.db.get_awg_device(device_id)
            if device is None or int(device["telegram_id"]) != telegram_id:
                return False
            await self.agent.delete(str(device["public_key"]))
            await self.db.delete_awg_device(device_id)
            await audit(self.db, telegram_id, "awg_device_removed", str(device_id))
            return True

    async def config_for(self, telegram_id: int, device_id: int) -> str | None:
        device = await self.db.get_awg_device(device_id)
        if device is None or int(device["telegram_id"]) != telegram_id:
            return None
        return render_config(device, await self.agent.info())

    # ------------------------------------------------------------ quota
    async def _base_limit(self, user: dict[str, Any]) -> int:
        """The quota the user is entitled to before AmneziaWG traffic."""
        tier = TIERS.get(str(user.get("tier") or ""))
        trial = bool(user.get("trial_used")) and (
            await self.db.count_paid_payments(int(user["telegram_id"])) == 0
        )
        if trial:
            return self.settings.trial_traffic_gb * GIB
        return tier.traffic_bytes if tier else 0

    async def _account(
        self,
        user: dict[str, Any],
        delta: int,
        panel: dict[str, dict[str, Any]] | None,
    ) -> bool:
        """Add AmneziaWG traffic to the window; True if the quota is used up."""
        telegram_id = int(user["telegram_id"])
        base = await self._base_limit(user)
        node = user.get("node_username")
        panel_user = panel.get(str(node)) if panel is not None and node else None
        if base <= 0 or panel_user is None:
            if delta:
                await self.db.add_awg_usage_bytes(telegram_id, delta)
            return False
        used = int(panel_user.get("used_traffic") or 0)
        lifetime = int(panel_user.get("lifetime_used_traffic") or used)
        window = max(0, lifetime - used)
        awg_bytes = await self.db.set_awg_usage(telegram_id, window, delta)
        exhausted = used + awg_bytes >= base
        desired = max(base - quantize(awg_bytes), MIN_PANEL_LIMIT)
        current = int(panel_user.get("data_limit") or 0)
        if user["status"] == "active" and current != desired:
            try:
                await self.hiddify.modify_user(str(node), data_limit=desired)
            except HiddifyError:
                logger.warning("awg_quota_panel_update_failed")
        return exhausted

    # ---------------------------------------------------------- reconcile
    async def reconcile(self) -> None:
        """Make the agent match the bot: enable, disable, restore, delete."""
        devices = await self.db.all_awg_devices()
        actual = await self.agent.peers()
        known = {str(device["public_key"]) for device in devices}
        by_user: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
        for device in devices:
            by_user[int(device["telegram_id"])].append(device)

        panel: dict[str, dict[str, Any]] | None = None
        if by_user:
            try:
                panel = {
                    str(item.get("username")): item
                    for item in await self.hiddify.get_users_usage()
                }
            except HiddifyError:
                logger.warning("awg_reconcile_panel_unavailable")

        server_delta = 0
        for telegram_id, user_devices in by_user.items():
            user = await self.db.get_user(telegram_id)
            delta = 0
            for device in user_devices:
                peer = actual.get(str(device["public_key"]))
                if peer is None:
                    continue
                counter = int(peer.get("rx_bytes") or 0) + int(
                    peer.get("tx_bytes") or 0
                )
                last = int(device["last_counter"])
                delta += counter - last if counter >= last else counter
                if counter != last:
                    await self.db.set_awg_counter(int(device["id"]), counter)
            server_delta += delta
            exhausted = False
            if user is not None:
                exhausted = await self._account(user, delta, panel)
            enabled = user is not None and user["status"] == "active" and not exhausted
            for device in user_devices:
                peer = actual.get(str(device["public_key"]))
                if (
                    peer is not None
                    and peer.get("enabled") is enabled
                    and peer.get("address") == device["address"]
                ):
                    continue
                try:
                    await self.agent.upsert(
                        str(device["public_key"]),
                        str(device["preshared_key"]),
                        enabled,
                        address=str(device["address"]),
                    )
                except AwgError:
                    logger.warning("awg_reconcile_upsert_failed")

        for public_key in set(actual) - known:
            try:
                await self.agent.delete(public_key)
            except AwgError:
                logger.warning("awg_reconcile_delete_failed")

        if server_delta > 0:
            # The server carried this traffic too: it counts against the
            # hosting plan's allowance (services/server_traffic.py).
            try:
                await self.db.add_server_traffic(utc_day(), server_delta)
            except Exception:
                logger.warning("awg_server_traffic_failed")

    async def window_bytes(self, telegram_id: int, window: int) -> int:
        """AmneziaWG traffic of the given panel window (0 if none)."""
        row = await self.db.get_awg_usage(telegram_id)
        if row is None or int(row["window_key"]) != window:
            return 0
        return int(row["bytes"])
