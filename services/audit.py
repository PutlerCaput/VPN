"""Minimal database audit: regular user activity is omitted by default."""

from __future__ import annotations

import json
import logging
from typing import Any

from db.database import Database

logger = logging.getLogger("audit")


async def audit(
    db: Database,
    telegram_id: int | None,
    action: str,
    target: str | None = None,
    **detail: Any,
) -> None:
    payload = json.dumps(detail, ensure_ascii=False, default=str) if detail else None
    try:
        await db.add_audit(telegram_id, action, target, payload)
    except Exception:
        logger.exception("audit_write_failed", extra={"action": action})
