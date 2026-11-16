"""Async Hiddify Manager API v2 client (admin user endpoints).

Endpoints and fields follow the official Hiddify Manager / hiddifypanel
sources and docs, not a third-party panel API:

- Auth: header ``Hiddify-API-Key`` (the admin UUID).
  https://hiddify.com/manager/contribution/How-to-use-API-in-HiddifyManager-project/
- Admin users:
  GET/POST  /{admin_proxy_path}/api/v2/admin/user/
  GET/PATCH/DELETE /{admin_proxy_path}/api/v2/admin/user/{uuid}/
  (hiddifypanel UsersApi / UserApi; schema PostUserSchema / PatchUserSchema)
- Panel health:
  GET /{admin_proxy_path}/api/v2/panel/info/
  GET /{admin_proxy_path}/api/v2/panel/ping/
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import math
import time
import uuid
from typing import Any
from urllib.parse import urlsplit

import httpx

from config import DEFAULT_TIER, GIB, TIERS, TRAFFIC_RESET_STRATEGIES, device_note
from services.metrics import HIDDIFY_ERRORS

logger = logging.getLogger(__name__)

RETRY_ATTEMPTS = 3
BACKOFF_BASE = 0.5
# A subscription is a few kilobytes; anything far bigger is not one.
SUBSCRIPTION_MAX_BYTES = 2 * 1024 * 1024
# Hiddify has no "unlimited" usage_limit_GB; this sentinel is treated as
# unlimited when converting back to the bot's 0-byte convention.
UNLIMITED_USAGE_GB = 10_000.0

# Our reset strategies -> Hiddify UserMode, and back.
# UserMode values: daily, weekly, monthly, no_reset.
_TO_HIDDIFY_MODE = {
    "no_reset": "no_reset",
    "day": "daily",
    "week": "weekly",
    "month": "monthly",
    "year": "monthly",
}
_FROM_HIDDIFY_MODE = {
    "no_reset": "no_reset",
    "daily": "day",
    "weekly": "week",
    "monthly": "month",
}


class HiddifyError(Exception):
    pass


class HiddifyNotFound(HiddifyError):
    pass


class HiddifyForbidden(HiddifyError):
    """The panel answered, but this admin account may not read the resource."""


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


def _json_body(response: httpx.Response, operation: str) -> Any:
    """The decoded body; a page that is not JSON is a panel error, not a crash."""
    try:
        return response.json()
    except ValueError:
        raise HiddifyError(
            f"{operation}: HTTP {response.status_code}, body is not JSON"
        ) from None


def _is_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value.lower()
    except ValueError:
        return False


def _bytes_to_gb(limit_bytes: int) -> float:
    if limit_bytes <= 0:
        return UNLIMITED_USAGE_GB
    return limit_bytes / GIB


def _gb_to_bytes(limit_gb: Any) -> int:
    try:
        value = float(limit_gb)
    except (TypeError, ValueError):
        return 0
    if value <= 0 or value >= UNLIMITED_USAGE_GB:
        return 0
    return int(value * GIB)


def _package_days(expire: int, *, now: int | None = None) -> int:
    remaining = expire - (now if now is not None else int(time.time()))
    return max(1, int(math.ceil(remaining / 86400)))


def _expire_from(start_date: Any, package_days: Any) -> int | None:
    if not start_date or package_days is None:
        return None
    try:
        days = int(package_days)
        if isinstance(start_date, str) and len(start_date) >= 10:
            stamp = time.strptime(start_date[:10], "%Y-%m-%d")
            started = calendar.timegm((*stamp[:3], 0, 0, 0, *stamp[6:]))
            return int(started) + days * 86400
    except (TypeError, ValueError, OverflowError):
        return None
    return None


class HiddifyClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        admin_proxy_path: str = "",
        *,
        user_proxy_path: str = "",
        subscription_base_url: str | None = None,
        verify_tls: bool = True,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("Hiddify API key is empty")
        self._api_key = api_key.strip()
        self._admin_proxy_path = admin_proxy_path.strip().strip("/")
        self._user_proxy_path = user_proxy_path.strip().strip("/")
        self._subscription_base_url = (
            subscription_base_url.rstrip("/") if subscription_base_url else None
        )
        self._origin = base_url.rstrip("/")
        self._timeout = timeout
        self._verify_tls = verify_tls
        self._transport = transport
        self._sub_client: httpx.AsyncClient | None = None
        self._client = httpx.AsyncClient(
            base_url=self._origin,
            timeout=timeout,
            verify=verify_tls,
            transport=transport,
            headers={
                "Accept": "application/json",
                "Hiddify-API-Key": self._api_key,
            },
        )

    async def close(self) -> None:
        await self._client.aclose()
        if self._sub_client is not None:
            await self._sub_client.aclose()

    def subscription_url_for(self, user_uuid: str) -> str | None:
        """Hiddify's own subscription URL of a user (for the sharding proxy)."""
        self._validate(user_uuid)
        return self._subscription_url(user_uuid)

    async def fetch_subscription(
        self, user_uuid: str, user_agent: str
    ) -> tuple[int, str, dict[str, str]]:
        """Downloads the user's Hiddify subscription the way their app would.

        A separate client: the admin API key is never sent to the public
        subscription address. Returns (status, body, headers).
        """
        url = self.subscription_url_for(user_uuid)
        if url is None:
            raise HiddifyError("subscription URL is not HTTPS")
        if self._sub_client is None:
            self._sub_client = httpx.AsyncClient(
                timeout=self._timeout,
                verify=self._verify_tls,
                transport=self._transport,
                follow_redirects=False,
            )
        try:
            response = await self._sub_client.get(
                url, headers={"User-Agent": user_agent[:200] or "v2rayNG/1.9"}
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            HIDDIFY_ERRORS.labels(operation="fetch_subscription").inc()
            raise HiddifyError(f"fetch_subscription failed: {exc}") from None
        if len(response.content) > SUBSCRIPTION_MAX_BYTES:
            raise HiddifyError("fetch_subscription: body too large")
        return response.status_code, response.text, dict(response.headers)

    def _admin(self, *segments: str) -> str:
        parts = [self._admin_proxy_path, "api/v2/admin", *segments]
        return "/" + "/".join(p.strip("/") for p in parts if p and p.strip("/")) + "/"

    def _panel(self, *segments: str) -> str:
        parts = [self._admin_proxy_path, "api/v2/panel", *segments]
        return "/" + "/".join(p.strip("/") for p in parts if p and p.strip("/")) + "/"

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
        for attempt in range(RETRY_ATTEMPTS):
            try:
                response = await self._client.request(
                    method,
                    path,
                    json=json_body,
                    params=params,
                )
                if response.status_code >= 500:
                    last_error = HiddifyError(f"HTTP {response.status_code}")
                else:
                    return response
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
            delay = BACKOFF_BASE * (2**attempt)
            logger.warning(
                "hiddify_retry",
                extra={
                    "operation": operation,
                    "attempt": attempt + 1,
                    "error": str(last_error),
                },
            )
            await asyncio.sleep(delay)
        HIDDIFY_ERRORS.labels(operation=operation).inc()
        raise HiddifyError(f"{operation} failed after retries: {last_error}")

    @staticmethod
    def _check(response: httpx.Response, operation: str) -> dict[str, Any]:
        if response.status_code == 404:
            raise HiddifyNotFound(operation)
        if response.status_code == 403:
            raise HiddifyForbidden(operation)
        if response.status_code >= 400:
            raise HiddifyError(
                f"{operation}: HTTP {response.status_code} {response.text[:200]}"
            )
        data = _json_body(response, operation)
        if not isinstance(data, dict):
            raise HiddifyError(f"{operation}: unexpected payload")
        return data

    @staticmethod
    def _validate(user_uuid: str) -> None:
        if not _is_uuid(user_uuid):
            raise ValueError(f"invalid node uuid: {user_uuid!r}")

    def _subscription_url(self, user_uuid: str) -> str | None:
        """Public user-page URL that Hiddify clients import as a subscription."""
        base = self._subscription_base_url or self._origin
        if not base.startswith("https://"):
            return None
        parts = [base, self._user_proxy_path, user_uuid]
        return "/".join(p.strip("/") for p in parts if p) + "/"

    def normalize(self, user: dict[str, Any]) -> dict[str, Any]:
        """Map a Hiddify user record to the fields the bot already consumes."""
        user_uuid = str(user.get("uuid") or "")
        used_gb = user.get("current_usage_GB") or 0
        try:
            used_bytes = max(0, int(float(used_gb) * GIB))
        except (TypeError, ValueError):
            used_bytes = 0
        limit_bytes = _gb_to_bytes(user.get("usage_limit_GB"))
        mode = str(user.get("mode") or "no_reset")
        enable = user.get("enable")
        is_active = user.get("is_active")
        if enable is False:
            status = "disabled"
        elif is_active is False:
            status = (
                "limited" if limit_bytes and used_bytes >= limit_bytes else "disabled"
            )
        else:
            status = "active"
        expire = _expire_from(user.get("start_date"), user.get("package_days"))
        subscription = (
            self._subscription_url(user_uuid) if _is_uuid(user_uuid) else None
        )
        links: list[str] = []
        if subscription:
            links.append(subscription)
        return {
            **user,
            "uuid": user_uuid,
            "username": user_uuid,
            "status": status,
            "expire": expire,
            "data_limit": limit_bytes,
            "used_traffic": used_bytes,
            "lifetime_used_traffic": used_bytes,
            "data_limit_reset_strategy": _FROM_HIDDIFY_MODE.get(mode, "no_reset"),
            "note": user.get("comment") or user.get("note"),
            "subscription_url": subscription,
            "links": user.get("links") or links,
        }

    def _policy_body(
        self,
        *,
        user_uuid: str,
        expire: int,
        tier: str,
        data_limit: int | None,
        data_limit_reset_strategy: str | None,
        enable: bool = True,
        name: str | None = None,
        comment: str | None = None,
    ) -> dict[str, Any]:
        limit, strategy = traffic_policy(tier, data_limit, data_limit_reset_strategy)
        body: dict[str, Any] = {
            "uuid": user_uuid,
            "name": name or user_uuid,
            "enable": enable,
            "is_active": enable,
            "usage_limit_GB": _bytes_to_gb(limit),
            "mode": _TO_HIDDIFY_MODE.get(strategy, "no_reset"),
            "package_days": _package_days(expire),
            "start_date": time.strftime("%Y-%m-%d", time.gmtime()),
            "comment": comment if comment is not None else device_note(tier),
            "current_usage_GB": 0 if enable else None,
        }
        return {key: value for key, value in body.items() if value is not None}

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
        """Create a Hiddify user; ``username`` is the UUID stored as node_username."""
        self._validate(username)
        if tier not in TIERS:
            raise ValueError(f"unknown tier: {tier!r}")
        if expire is None:
            expire = int(time.time()) + int(days or 1) * 86400
        body = self._policy_body(
            user_uuid=username,
            expire=expire,
            tier=tier,
            data_limit=data_limit,
            data_limit_reset_strategy=data_limit_reset_strategy,
            enable=True,
        )
        response = await self._request(
            "POST", self._admin("user"), operation="create_user", json_body=body
        )
        if response.status_code == 400 and "exist" in response.text.lower():
            return await self.modify_user(
                username,
                expire=expire,
                status="active",
                note=device_note(tier),
                data_limit=data_limit
                if data_limit is not None
                else traffic_policy(tier)[0],
                data_limit_reset_strategy=data_limit_reset_strategy
                or traffic_policy(tier, data_limit, data_limit_reset_strategy)[1],
            )
        return self.normalize(self._check(response, "create_user"))

    async def modify_user(
        self,
        username: str,
        *,
        expire: int | None = None,
        status: str | None = None,
        note: str | None = None,
        data_limit: int | None = None,
        data_limit_reset_strategy: str | None = None,
        new_uuid: str | None = None,
    ) -> dict[str, Any]:
        self._validate(username)
        body: dict[str, Any] = {}
        if new_uuid is not None:
            self._validate(new_uuid)
            body["uuid"] = new_uuid
        if expire is not None:
            body["package_days"] = _package_days(expire)
            body["start_date"] = time.strftime("%Y-%m-%d", time.gmtime())
        if status is not None:
            if status not in {"active", "disabled"}:
                raise ValueError("status must be active or disabled")
            body["enable"] = status == "active"
            if status == "active":
                body["is_active"] = True
        if note is not None:
            body["comment"] = note
        if data_limit is not None:
            if data_limit < 0:
                raise ValueError("data_limit must not be negative")
            body["usage_limit_GB"] = _bytes_to_gb(data_limit)
        if data_limit_reset_strategy is not None:
            if data_limit_reset_strategy not in TRAFFIC_RESET_STRATEGIES:
                raise ValueError("unknown data_limit_reset_strategy")
            body["mode"] = _TO_HIDDIFY_MODE.get(data_limit_reset_strategy, "no_reset")
        response = await self._request(
            "PATCH",
            self._admin("user", username),
            operation="modify_user",
            json_body=body,
        )
        return self.normalize(self._check(response, "modify_user"))

    async def revoke_subscription(self, username: str) -> dict[str, Any]:
        """Rotate the user's UUID. Previous subscription links stop working."""
        self._validate(username)
        new_uuid = str(uuid.uuid4())
        return await self.modify_user(username, new_uuid=new_uuid)

    async def set_user_status(self, username: str, status: str) -> dict[str, Any]:
        return await self.modify_user(username, status=status)

    async def disable_user(self, username: str) -> dict[str, Any]:
        return await self.set_user_status(username, "disabled")

    async def enable_user(self, username: str) -> dict[str, Any]:
        return await self.set_user_status(username, "active")

    async def get_user(self, username: str) -> dict[str, Any]:
        self._validate(username)
        response = await self._request(
            "GET", self._admin("user", username), operation="get_user"
        )
        return self.normalize(self._check(response, "get_user"))

    async def get_vless_link(self, username: str) -> str:
        data = await self.get_user(username)
        return self.extract_vless_link(data)

    @staticmethod
    def extract_vless_link(user: dict[str, Any]) -> str:
        for link in user.get("links") or []:
            if isinstance(link, str) and (
                link.startswith("vless://") or link.startswith("https://")
            ):
                return link
        subscription = user.get("subscription_url")
        if isinstance(subscription, str) and subscription:
            return subscription
        raise HiddifyError("no subscription link in user payload")

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
        """Panel liveness: version from /info/, started from a successful /ping/."""
        ping = await self._request("GET", self._panel("ping"), operation="get_core")
        if ping.status_code == 403:
            raise HiddifyForbidden("get_core")
        if ping.status_code >= 400:
            raise HiddifyError(f"get_core: HTTP {ping.status_code}")
        # A 2xx HTML error page is still a panel failure.
        if ping.content and "json" not in ping.headers.get("content-type", ""):
            try:
                ping.json()
            except ValueError:
                # ping may be an empty or plain "PONG" body; only fail if it looks
                # like a document, not a short text payload.
                text = ping.text.strip()
                if text.startswith("<") or len(text) > 200:
                    raise HiddifyError("get_core: HTTP 200, body is not JSON") from None
        info = await self._request("GET", self._panel("info"), operation="get_core")
        if info.status_code == 403:
            raise HiddifyForbidden("get_core")
        version = "unknown"
        if info.status_code < 400:
            payload = _json_body(info, "get_core")
            if isinstance(payload, dict) and payload.get("version"):
                version = str(payload["version"])
        return {"version": version, "started": True}

    async def get_nodes(self) -> list[dict[str, Any]]:
        """Additional locations are configured in the panel itself.

        The official admin API has no node-inventory list comparable to a
        multi-node panel; an empty list means "only the core".
        """
        return []

    async def get_users_usage(self, page_size: int = 1000) -> list[dict[str, Any]]:
        del page_size  # the official list endpoint is not paginated
        response = await self._request(
            "GET", self._admin("user"), operation="get_users"
        )
        if response.status_code == 404:
            return []
        if response.status_code == 403:
            raise HiddifyForbidden("get_users")
        if response.status_code >= 400:
            raise HiddifyError(f"get_users: HTTP {response.status_code}")
        data = _json_body(response, "get_users")
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            rows = data.get("users") or data.get("data") or []
        else:
            raise HiddifyError("get_users: unexpected payload")
        return [self.normalize(u) for u in rows if isinstance(u, dict)]
