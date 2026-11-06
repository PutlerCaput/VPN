"""Bot routers. Order matters: admin first, catch-all last."""

from __future__ import annotations

from aiogram import F, Router

from handlers import (
    admin,
    amnezia,
    legal,
    onboarding,
    payments,
    profile,
    referral,
    start,
    support,
)


def build_root_router() -> Router:
    root = Router(name="root")
    # Configuration links, billing and support are personal conversations only.
    root.message.filter(F.chat.type == "private")
    root.callback_query.filter(F.message.chat.type == "private")
    root.include_router(admin.router)
    root.include_router(start.router)
    root.include_router(legal.router)
    root.include_router(payments.router)
    root.include_router(profile.router)
    root.include_router(onboarding.router)
    root.include_router(amnezia.router)
    root.include_router(referral.router)
    root.include_router(support.router)
    return root
