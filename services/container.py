"""Dependency container passed to handlers, API views and jobs."""

from __future__ import annotations

from dataclasses import dataclass, field

from aiogram import Bot

from config import Settings
from db.database import Database
from services.billing import BillingService
from services.cryptopay import CryptoPayClient
from services.health import ServiceHealth
from services.marzban import MarzbanClient
from services.notifier import Notifier
from services.provisioning import ProvisioningService
from services.push import PushService
from services.sbp import SbpService
from services.scheduler import AnomalyMonitor
from services.security import SlidingWindowLimiter
from services.stars import StarsPayments
from services.yookassa import YooKassaClient


@dataclass(slots=True)
class Services:
    settings: Settings
    db: Database
    bot: Bot
    marzban: MarzbanClient
    cryptopay: CryptoPayClient | None
    sbp: SbpService
    push: PushService
    notifier: Notifier
    provisioning: ProvisioningService
    billing: BillingService
    monitor: AnomalyMonitor
    yookassa: YooKassaClient | None = None
    stars: StarsPayments | None = None
    health: ServiceHealth | None = None
    limiters: list[SlidingWindowLimiter] = field(default_factory=list)
