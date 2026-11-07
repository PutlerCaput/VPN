"""Rate limiters: the configured limiter is used, and every one gets pruned."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from aiogram.types import User

import main
from handlers.payments import PAYCHECK_LIMIT
from handlers.profile import REVOKE_LIMITER
from middlewares.throttling import WARNED_PRUNE_SIZE, ThrottlingMiddleware
from services.security import SlidingWindowLimiter

USER = User(id=424242, is_bot=False, first_name="A")


def test_an_empty_custom_limiter_is_not_replaced() -> None:
    limiter = SlidingWindowLimiter(limit=1, window_seconds=60)
    assert len(limiter) == 0  # falsy: this used to swap in the default limiter
    assert ThrottlingMiddleware(limiter).limiter is limiter


async def test_custom_limit_is_enforced() -> None:
    middleware = ThrottlingMiddleware(SlidingWindowLimiter(limit=1, window_seconds=60))
    handler = AsyncMock(return_value="ok")
    data = {"event_from_user": USER}

    assert await middleware(handler, MagicMock(), data) == "ok"
    assert await middleware(handler, MagicMock(), data) is None
    assert handler.await_count == 1


async def test_throttle_warnings_forget_expired_users() -> None:
    middleware = ThrottlingMiddleware(SlidingWindowLimiter(limit=1, window_seconds=60))
    data = {"event_from_user": USER}
    await middleware(AsyncMock(), MagicMock(), data)  # the only allowed request
    middleware._warned = {user_id: -1e9 for user_id in range(WARNED_PRUNE_SIZE + 5)}
    handler = AsyncMock()

    await middleware(handler, MagicMock(), data)

    handler.assert_not_awaited()
    assert set(middleware._warned) == {USER.id}


def test_dispatcher_hands_every_bot_limiter_to_housekeeping(services: Any) -> None:
    dp = main.build_dispatcher(services)
    try:
        assert any(limiter is PAYCHECK_LIMIT for limiter in services.limiters)
        assert any(limiter is REVOKE_LIMITER for limiter in services.limiters)
    finally:
        for root in list(dp.sub_routers):
            for child in list(root.sub_routers):
                root.sub_routers.remove(child)
                child._parent_router = None
