"""Async Marzban panel client with token caching and retry/backoff."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from config import DEFAULT_TIER, TIERS, TRAFFIC_RESET_STRATEGIES, device_note
from services.metrics import MARZBAN_ERRORS
from services.security import is_valid_node_username

logger = logging.getLogger(__name__)

RETRY_ATTEMPTS = 3
BACKOFF_BASE = 0.5


class MarzbanError(Exception):
    pass


def traffic_policy(
    tier: str, data_limit: int | None = None, reset_strategy: str | None = None
) -> tuple[int, str]:
    """(data_limit bytes, reset strategy) for a tier; overrides win."""
    spec = TIERS[tier]
    limit = spec.traffic_bytes if data_limit is None else data_limit
    strategy = reset_strategy or (spec.traffic_reset if data_limit is None else None)
    if strategy is None:
        strategy = "no_reset"
    if limit < 0 or strategy not in TRAFFIC_RESET_STRATEGIES:
        raise ValueError("invalid traffic policy")
    return limit, strategy


class MarzbanNotFound(MarzbanError):
    pass


class MarzbanForbidden(MarzbanError):
    """The panel answered, but this admin account may not read the resource."""


class MarzbanClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        inbound_tag: str,
        *,
        verify_tls: bool = True,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._username = username
        self._password = password
        self.inbound_tag = inbound_tag
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            verify=verify_tls,
            transport=transport,
            headers={"Accept": "application/json"},
        )
        self._token: str | None = None
        self._token_obtained = 0.0
        self._token_lock = asyncio.Lock()

    async def close(self) -> None:
        await self._client.aclose()

    # ---------------------------------------------------------------- auth
    async def _authenticate(self, force: bool = False) -> str:
        async with self._token_lock:
            # Marzban tokens live 24h by default; refresh well before that.
            fresh = time.monotonic() - self._token_obtained < 3600
            if self._token and fresh and not force:
                return self._token
            response = await self._client.post(
                "/api/admin/token",
                data={"username": self._username, "password": self._password},
            )
            if response.status_code != 200:
                raise MarzbanError(f"auth failed: HTTP {response.status_code}")
            token = response.json().get("access_token")
            if not token:
                raise MarzbanError("auth failed: no access_token")
            self._token = str(token)
            self._token_obtained = time.monotonic()
            return self._token

    async def _request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        last_error: Exception | None = None
        reauthed = False
        for attempt in range(RETRY_ATTEMPTS):
            try:
                token = await self._authenticate()
                response = await self._client.request(
                    method,
                    path,
                    json=json_body,
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                )
                if response.status_code == 401 and not reauthed:
                    reauthed = True
                    await self._authenticate(force=True)
                    continue
                if response.status_code >= 500:
                    last_error = MarzbanError(f"HTTP {response.status_code}")
                else:
                    return response
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
            delay = BACKOFF_BASE * (2**attempt)
            logger.warning(
                "marzban_retry",
                extra={
                    "operation": operation,
                    "attempt": attempt + 1,
                    "error": str(last_error),
                },
            )
            await asyncio.sleep(delay)
        MARZBAN_ERRORS.labels(operation=operation).inc()
        raise MarzbanError(f"{operation} failed after retries: {last_error}")

    @staticmethod
    def _check(response: httpx.Response, operation: str) -> dict[str, Any]:
        if response.status_code == 404:
            raise MarzbanNotFound(operation)
        if response.status_code == 403:
            raise MarzbanForbidden(operation)
        if response.status_code >= 400:
            raise MarzbanError(
                f"{operation}: HTTP {response.status_code} {response.text[:200]}"
            )
        data = response.json()
        if not isinstance(data, dict):
            raise MarzbanError(f"{operation}: unexpected payload")
        return data

    @staticmethod
    def _validate(username: str) -> None:
        if not is_valid_node_username(username):
            raise ValueError(f"invalid node username: {username!r}")

    # --------------------------------------------------------------- users
    async def create_user(
        self,
        username: str,
        days: int | None = None,
        *,
        expire: int | None = None,
        tier: str = DEFAULT_TIER,
        data_limit: int | None = None,
        data_limit_reset_strategy: str | None = None,
    ) -> dict[str, Any]:
        """Creates a VLESS user; flow xtls-rprx-vision is always set.

        Marzban has no per-user IP/device limit, so the tier's device limit goes into
        the user's note for the admin and is a term of the offer, not a panel setting.
        The traffic quota is the tier's unless ``data_limit`` overrides it
        (the free trial). 0 bytes means unlimited.
        """
        self._validate(username)
        if tier not in TIERS:
            raise ValueError(f"unknown tier: {tier!r}")
        if expire is None:
            expire = int(time.time()) + int(days or 1) * 86400
        limit, strategy = traffic_policy(tier, data_limit, data_limit_reset_strategy)
        body = {
            "username": username,
            "proxies": {"vless": {"flow": "xtls-rprx-vision"}},
            "inbounds": {"vless": [self.inbound_tag]},
            "note": device_note(tier),
            "expire": expire,
            "data_limit": limit,
            "data_limit_reset_strategy": strategy,
            "status": "active",
        }
        response = await self._request(
            "POST", "/api/user", operation="create_user", json_body=body
        )
        if response.status_code == 409:
            return await self.modify_user(
                username,
                expire=expire,
                status="active",
                note=device_note(tier),
                data_limit=limit,
                data_limit_reset_strategy=strategy,
            )
        return self._check(response, "create_user")

    async def modify_user(
        self,
        username: str,
        *,
        expire: int | None = None,
        status: str | None = None,
        note: str | None = None,
        data_limit: int | None = None,
        data_limit_reset_strategy: str | None = None,
    ) -> dict[str, Any]:
        self._validate(username)
        body: dict[str, Any] = {}
        if expire is not None:
            body["expire"] = expire
        if status is not None:
            if status not in {"active", "disabled"}:
                raise ValueError("status must be active or disabled")
            body["status"] = status
        if note is not None:
            body["note"] = note
        if data_limit is not None:
            if data_limit < 0:
                raise ValueError("data_limit must not be negative")
            body["data_limit"] = data_limit
        if data_limit_reset_strategy is not None:
            if data_limit_reset_strategy not in TRAFFIC_RESET_STRATEGIES:
                raise ValueError("unknown data_limit_reset_strategy")
            body["data_limit_reset_strategy"] = data_limit_reset_strategy
        response = await self._request(
            "PUT",
            f"/api/user/{username}",
            operation="modify_user",
            json_body=body,
        )
        return self._check(response, "modify_user")

    async def revoke_subscription(self, username: str) -> dict[str, Any]:
        """New credentials for the user; every previously shared link stops working."""
        self._validate(username)
        response = await self._request(
            "POST", f"/api/user/{username}/revoke_sub", operation="revoke_sub"
        )
        return self._check(response, "revoke_sub")

    async def set_user_status(self, username: str, status: str) -> dict[str, Any]:
        return await self.modify_user(username, status=status)

    async def disable_user(self, username: str) -> dict[str, Any]:
        return await self.set_user_status(username, "disabled")

    async def enable_user(self, username: str) -> dict[str, Any]:
        return await self.set_user_status(username, "active")

    async def get_user(self, username: str) -> dict[str, Any]:
        self._validate(username)
        response = await self._request(
            "GET", f"/api/user/{username}", operation="get_user"
        )
        return self._check(response, "get_user")

    async def get_vless_link(self, username: str) -> str:
        data = await self.get_user(username)
        return self.extract_vless_link(data)

    @staticmethod
    def extract_vless_link(user: dict[str, Any]) -> str:
        for link in user.get("links") or []:
            if isinstance(link, str) and link.startswith("vless://"):
                return link
        subscription = user.get("subscription_url")
        if isinstance(subscription, str) and subscription:
            return subscription
        raise MarzbanError("no vless link in user payload")

    @staticmethod
    def extract_subscription_url(
        user: dict[str, Any], base_url: str | None
    ) -> str | None:
        """Absolute HTTPS subscription URL of a user, or None if unusable."""
        value = user.get("subscription_url")
        if not isinstance(value, str) or not value or len(value) > 2048:
            return None
        if value.startswith("/"):
            if not base_url:
                return None
            value = f"{base_url.rstrip('/')}{value}"
        try:
            url = urlsplit(value)
            valid = (
                url.scheme == "https"
                and bool(url.hostname)
                and url.username is None
                and url.password is None
                and url.path.startswith("/")
            )
        except ValueError:
            valid = False
        return value if valid else None

    async def get_core(self) -> dict[str, Any]:
        """Core (Xray on the panel host) state: {"version", "started", ...}."""
        response = await self._request("GET", "/api/core", operation="get_core")
        return self._check(response, "get_core")

    async def get_nodes(self) -> list[dict[str, Any]]:
        """Additional Marzban nodes (other locations) with their status."""
        response = await self._request("GET", "/api/nodes", operation="get_nodes")
        if response.status_code == 403:
            raise MarzbanForbidden("get_nodes")
        if response.status_code >= 400:
            raise MarzbanError(f"get_nodes: HTTP {response.status_code}")
        data = response.json()
        if not isinstance(data, list):
            raise MarzbanError("get_nodes: unexpected payload")
        return [node for node in data if isinstance(node, dict)]

    async def get_users_usage(self, page_size: int = 1000) -> list[dict[str, Any]]:
        users: list[dict[str, Any]] = []
        offset = 0
        while True:
            response = await self._request(
                "GET",
                "/api/users",
                operation="get_users",
                params={"offset": offset, "limit": page_size},
            )
            data = self._check(response, "get_users")
            batch = data.get("users") or []
            users.extend(u for u in batch if isinstance(u, dict))
            total = int(data.get("total") or 0)
            offset += len(batch)
            if not batch or offset >= total:
                return users
