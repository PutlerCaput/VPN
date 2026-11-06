"""Web Push (VAPID) delivery via pywebpush."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from py_vapid import Vapid
from pywebpush import WebPushException, webpush

from db.database import Database

logger = logging.getLogger(__name__)


class PushService:
    def __init__(
        self,
        db: Database,
        vapid_public: str | None,
        vapid_private: str | None,
        contact: str,
    ) -> None:
        self._db = db
        self.public_key = vapid_public
        self._contact = contact
        self._vapid: Vapid | None = None
        if vapid_private:
            self._vapid = Vapid.from_string(vapid_private)

    @property
    def enabled(self) -> bool:
        return self._vapid is not None and bool(self.public_key)

    def _send_sync(self, subscription: dict[str, Any], payload: str) -> int | None:
        try:
            webpush(
                subscription_info=subscription,
                data=payload,
                vapid_private_key=self._vapid,
                vapid_claims={"sub": self._contact},
                ttl=24 * 3600,
                timeout=10,
            )
            return None
        except WebPushException as exc:
            status = exc.response.status_code if exc.response is not None else 0
            return int(status)

    async def send(
        self, telegram_id: int, title: str, body: str, url: str = "/"
    ) -> int:
        """Sends to every subscription of the user. Returns deliveries attempted."""
        if not self.enabled:
            return 0
        subs = await self._db.list_push_subscriptions(telegram_id)
        payload = json.dumps(
            {"title": title, "body": body, "url": url}, ensure_ascii=False
        )
        sent = 0
        for sub in subs:
            info = {
                "endpoint": sub["endpoint"],
                "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
            }
            status = await asyncio.to_thread(self._send_sync, info, payload)
            if status in (404, 410):
                await self._db.delete_push_by_endpoint(sub["endpoint"])
            elif status:
                logger.warning(
                    "push_failed", extra={"status": status, "telegram_id": telegram_id}
                )
            else:
                sent += 1
        return sent
