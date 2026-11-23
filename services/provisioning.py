"""Business logic: trials, subscriptions, referral bonuses, bans."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
import weakref
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from config import DAYS_PER_MONTH, DEFAULT_TIER, GIB, TIERS, Settings, device_note
from db.database import Database
from services.audit import audit
from services.hiddify import (
    HiddifyClient,
    HiddifyError,
    HiddifyNotFound,
    traffic_policy,
)
from services.metrics import (
    AUTOBANS,
    PAYMENTS_PAID,
    SOFT_SUSPENSIONS,
    TRIALS_PROVISIONED,
)
from services.notifier import Notifier
from services.sharding import sharded_subscription_url

logger = logging.getLogger(__name__)

# Profile and dashboard reads share one panel request per user per minute,
# and never wait long for a slow panel.
TRAFFIC_CACHE_SECONDS = 60.0
TRAFFIC_TIMEOUT_SECONDS = 4.0
TRAFFIC_CACHE_SIZE = 10000

AUTOBAN_TEXT = (
    "🔴 Access denied. Your remote node connection has been permanently blocked "
    "automatically due to severe acceptable use policy violations (cybercrime "
    "indicators detected)."
)


class ProvisioningError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class TrafficStatus:
    used_bytes: int
    limit_bytes: int  # 0 = unlimited
    reset_strategy: str
    limited: bool  # the panel stopped the node because the quota is used up


@dataclass(slots=True)
class TrialResult:
    ok: bool
    reason: str
    config_link: str | None = None
    expires_at: int | None = None


def _is_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value.lower()
    except ValueError:
        return False


def safe_cached_link(user: dict[str, Any], settings: Settings) -> str | None:
    """Never return a pre-sharding unfiltered link in segmented mode."""
    if settings.entry_sharding_enabled:
        node = str(user.get("node_username") or "")
        if not _is_uuid(node):
            return None
        return sharded_subscription_url(
            settings.entry_sharding_base_url or settings.public_base_url,
            node.lower(),
        )
    stored = user.get("config_link")
    return stored if isinstance(stored, str) and stored else None


def _ts() -> int:
    return int(time.time())


def format_date(ts: int | None) -> str:
    if not ts:
        return "—"
    return time.strftime("%d.%m.%Y %H:%M UTC", time.gmtime(ts))


class ProvisioningService:
    def __init__(
        self,
        db: Database,
        hiddify: HiddifyClient,
        notifier: Notifier,
        settings: Settings,
    ) -> None:
        self.db = db
        self.hiddify = hiddify
        self.notifier = notifier
        self.settings = settings
        # One lock per user while someone holds or waits for it; a lock nobody
        # references any more drops out, so the map does not grow with every
        # user the bot has ever served.
        self._locks: weakref.WeakValueDictionary[int, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        self._traffic_cache: OrderedDict[str, tuple[float, TrafficStatus]] = (
            OrderedDict()
        )
        # True while new trials are paused (the server is near its monthly
        # traffic allowance: services/server_traffic.py).
        self.trial_gate: Callable[[], bool] | None = None

    def _lock(self, telegram_id: int) -> asyncio.Lock:
        lock = self._locks.get(telegram_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[telegram_id] = lock
        return lock

    def _config_from(self, data: dict[str, Any]) -> str:
        """What the user imports: all-locations subscription or a single link."""
        if self.settings.config_delivery == "subscription":
            url = HiddifyClient.extract_subscription_url(
                data, self.settings.subscription_base_url
            )
            if self.settings.entry_sharding_enabled:
                node = str(data.get("uuid") or "")
                if url is None or not _is_uuid(node):
                    # Never hand out an unfiltered panel link or the single
                    # default VLESS link when entry isolation is enabled.
                    # Both would bypass the subscription segmentation policy.
                    logger.warning("sharded_subscription_unavailable")
                    raise HiddifyError("sharded subscription unavailable")
                return sharded_subscription_url(
                    self.settings.entry_sharding_base_url
                    or self.settings.public_base_url,
                    node,
                )
            if url is not None:
                return url
            logger.warning("subscription_url_unavailable")
        return HiddifyClient.extract_vless_link(data)

    def _new_node(self, telegram_id: int) -> str:
        """Fresh panel UUID for an account that has none yet."""
        del telegram_id
        return str(uuid.uuid4())

    async def _ensure_node(self, user: dict[str, Any]) -> str:
        """Reuse a stored UUID, or generate one and persist it before the API call."""
        node = user.get("node_username")
        if node and _is_uuid(str(node)):
            return str(node)
        if node:
            # A Marzban-era username (not a UUID) has no Hiddify account: give the
            # user a fresh UUID so a payment still provisions instead of failing.
            logger.warning("legacy_node_username_replaced")
        fresh: str = self._new_node(int(user["telegram_id"]))
        await self.db.set_node_username(int(user["telegram_id"]), fresh)
        return fresh

    async def _disable_and_confirm(self, node_username: str) -> None:
        result = await self.hiddify.disable_user(node_username)
        if result.get("status") == "disabled":
            return
        current = await self.hiddify.get_user(node_username)
        if current.get("status") != "disabled":
            raise HiddifyError("Hiddify Manager did not confirm disabled status")

    # ------------------------------------------------------------------ trial
    async def accept_offer_and_start_trial(self, telegram_id: int) -> TrialResult:
        async with self._lock(telegram_id):
            user = await self.db.get_user(telegram_id)
            if user is None:
                return TrialResult(False, "no_user")
            if user["status"] == "banned":
                return TrialResult(False, "banned")
            await self.db.accept_offer(telegram_id)
            await audit(self.db, telegram_id, "offer_accepted")
            if user["trial_used"]:
                return TrialResult(
                    True,
                    "already_used",
                    safe_cached_link(user, self.settings),
                    user["expires_at"],
                )
            if self.trial_gate is not None and self.trial_gate():
                return TrialResult(False, "trials_paused")
            node = await self._ensure_node(user)
            expires_at = _ts() + self.settings.trial_seconds
            tier = self.settings.default_tier
            try:
                created = await self.hiddify.create_user(
                    node,
                    expire=expires_at,
                    tier=tier,
                    data_limit=self.settings.trial_traffic_gb * GIB,
                    data_limit_reset_strategy="no_reset",
                )
                link = self._config_from(created)
            except HiddifyError:
                logger.exception(
                    "trial_provision_failed", extra={"telegram_id": telegram_id}
                )
                return TrialResult(False, "provision_failed")
            await self.db.set_subscription(
                telegram_id,
                status="active",
                expires_at=expires_at,
                node_username=node,
                config_link=link,
            )
            await self.db.set_user_tier(telegram_id, tier)
            await self.db.mark_trial_used(telegram_id)
            TRIALS_PROVISIONED.inc()
            await audit(
                self.db, telegram_id, "trial_provisioned", node, expires_at=expires_at
            )
            return TrialResult(True, "provisioned", link, expires_at)

    # ----------------------------------------------------------------- config
    async def fresh_config(self, telegram_id: int) -> str:
        async with self._lock(telegram_id):
            user = await self.db.get_user(telegram_id)
            if user is None or user["status"] != "active" or not user["node_username"]:
                raise ProvisioningError("no_active_node")
            link = self._config_from(await self.hiddify.get_user(user["node_username"]))
            if link != user["config_link"]:
                await self.db.set_config_link(telegram_id, link)
            return link

    async def revoke_link(self, telegram_id: int) -> str:
        """Replace credentials on the panel and update the bot subscription."""
        async with self._lock(telegram_id):
            user = await self.db.get_user(telegram_id)
            if user is None or user["status"] != "active" or not user["node_username"]:
                raise ProvisioningError("no_active_node")
            old_uuid = str(user["node_username"])
            data = await self.hiddify.revoke_subscription(old_uuid)
            new_uuid = str(data.get("uuid") or "")
            if not _is_uuid(new_uuid) or new_uuid == old_uuid:
                raise HiddifyError("Hiddify Manager did not rotate the user UUID")
            # Commit the confirmed UUID and clear the cached old credential
            # before generating delivery links. A later refresh can recover.
            await self.db.set_subscription(
                telegram_id,
                status=str(user["status"]),
                expires_at=int(user["expires_at"] or 0),
                node_username=new_uuid,
                config_link="",
            )
            self._traffic_cache.pop(old_uuid, None)
            self._traffic_cache.pop(new_uuid, None)
            try:
                link = self._config_from(data)
            except HiddifyError:
                link = self._config_from(await self.hiddify.get_user(new_uuid))
            await self.db.set_config_link(telegram_id, link)
        await audit(self.db, telegram_id, "link_revoked", new_uuid)
        return link

    async def traffic_status(self, user: dict[str, Any]) -> TrafficStatus | None:
        """Current quota usage from the panel; None when unknown or no node."""
        node = user.get("node_username")
        if not node:
            return None
        node = str(node)
        cached = self._traffic_cache.get(node)
        current = time.monotonic()
        if cached is not None and current - cached[0] < TRAFFIC_CACHE_SECONDS:
            return cached[1]
        try:
            data = await asyncio.wait_for(
                self.hiddify.get_user(node), timeout=TRAFFIC_TIMEOUT_SECONDS
            )
        except (HiddifyError, TimeoutError):
            return None
        try:
            used = max(0, int(data.get("used_traffic") or 0))
            limit = max(0, int(data.get("data_limit") or 0))
            lifetime = max(used, int(data.get("lifetime_used_traffic") or used))
        except (TypeError, ValueError):
            return None
        from services.awg import combined_usage

        used, limit = await combined_usage(
            self.db, int(user["telegram_id"]), used, lifetime, limit
        )
        status = TrafficStatus(
            used_bytes=used,
            limit_bytes=limit,
            reset_strategy=str(data.get("data_limit_reset_strategy") or "no_reset"),
            limited=data.get("status") == "limited",
        )
        self._traffic_cache[node] = (current, status)
        self._traffic_cache.move_to_end(node)
        while len(self._traffic_cache) > TRAFFIC_CACHE_SIZE:
            self._traffic_cache.popitem(last=False)
        return status

    # ----------------------------------------------------------- subscription
    async def _extend(
        self, telegram_id: int, seconds: int, tier: str | None = None
    ) -> int:
        """Extends a node from max(now, expires_at). Caller holds the lock.

        A paid period sets the tier for the whole remaining time; bonus days pass
        ``tier=None`` and keep the user's current tier.
        """
        user = await self.db.get_user(telegram_id)
        if user is None:
            raise ProvisioningError("no_user")
        if user["status"] == "banned":
            raise ProvisioningError("banned")
        tier = tier if tier in TIERS else str(user.get("tier") or DEFAULT_TIER)
        node = await self._ensure_node(user)
        base = max(_ts(), int(user["expires_at"] or 0))
        new_expires = base + seconds
        try:
            await self.hiddify.modify_user(
                node, expire=new_expires, status="active", note=device_note(tier)
            )
            data = await self.hiddify.get_user(node)
        except HiddifyNotFound:
            data = await self.hiddify.create_user(node, expire=new_expires, tier=tier)
        link: str | None
        try:
            link = self._config_from(data)
        except HiddifyError:
            link = None
        status = "suspended" if user["status"] == "suspended" else "active"
        if status == "suspended":
            # Keep a soft-suspended node disabled until an admin lifts it.
            await self.hiddify.disable_user(node)
        await self.db.set_subscription(
            telegram_id,
            status=status,
            expires_at=new_expires,
            node_username=node,
            config_link=link,
        )
        await self.db.set_user_tier(telegram_id, tier)
        return new_expires

    async def add_bonus_days(
        self, telegram_id: int, days: int, reason: str
    ) -> int | None:
        async with self._lock(telegram_id):
            try:
                expires = await self._extend(telegram_id, days * 86400)
            except (ProvisioningError, HiddifyError):
                logger.warning(
                    "bonus_skipped",
                    extra={"telegram_id": telegram_id, "reason": reason},
                )
                return None
        await audit(self.db, telegram_id, "bonus_days", reason=reason, days=days)
        return expires

    async def _apply_expiration_target(
        self, telegram_id: int, target_expires_at: int, tier: str | None = None
    ) -> int:
        """Apply an absolute expiry target; safe to repeat after a crash.

        This method never adds time. Payment and referral
        retry jobs persist the target before calling the panel, then repeat this
        exact target until both the panel and the local DB agree.
        Caller must hold the user's lock.
        """
        user = await self.db.get_user(telegram_id)
        if user is None:
            raise ProvisioningError("no_user")
        if user["status"] == "banned":
            raise ProvisioningError("banned")
        selected_tier = tier if tier in TIERS else str(user.get("tier") or DEFAULT_TIER)
        if selected_tier not in TIERS:
            selected_tier = DEFAULT_TIER
        node = await self._ensure_node(user)
        # A paid period sets the tier's quota; bonus days keep the current one.
        limit: int | None = None
        strategy: str | None = None
        if tier in TIERS:
            limit, strategy = traffic_policy(selected_tier)
        try:
            await self.hiddify.modify_user(
                node,
                expire=target_expires_at,
                status=("disabled" if user["status"] == "suspended" else "active"),
                note=device_note(selected_tier),
                data_limit=limit,
                data_limit_reset_strategy=strategy,
            )
            data = await self.hiddify.get_user(node)
        except HiddifyNotFound:
            data = await self.hiddify.create_user(
                node, expire=target_expires_at, tier=selected_tier
            )
            if user["status"] == "suspended":
                await self._disable_and_confirm(node)
                data = await self.hiddify.get_user(node)
        try:
            link = self._config_from(data)
        except HiddifyError:
            link = None
        status = "suspended" if user["status"] == "suspended" else "active"
        await self.db.set_subscription(
            telegram_id,
            status=status,
            expires_at=target_expires_at,
            node_username=node,
            config_link=link,
        )
        await self.db.set_user_tier(telegram_id, selected_tier)
        self._traffic_cache.pop(node, None)
        return target_expires_at

    async def process_paid_payment(self, payment_id: str) -> bool:
        """Provision a paid order with retry-safe, absolute expiry targets."""
        payment = await self.db.get_payment(payment_id)
        if payment is None:
            return False
        newly_paid = await self.db.mark_payment_paid(payment_id)
        payment = await self.db.get_payment(payment_id)
        if payment is None or payment["status"] != "paid":
            return False
        telegram_id = int(payment["telegram_id"])
        if newly_paid:
            PAYMENTS_PAID.labels(method=payment["method"]).inc()
            await audit(
                self.db,
                telegram_id,
                "payment_paid",
                payment_id,
                amount=payment["amount"],
                currency=payment["currency"],
            )

        async with self._lock(telegram_id):
            # A refund is recorded under this same lock. One that came in while
            # this call waited for the lock must win: the period is not given.
            current = await self.db.get_payment(payment_id)
            if current is None or current["status"] != "paid":
                return False
            activation = await self.db.get_payment_activation(payment_id)
            if activation is not None and activation["status"] == "completed":
                return False
            try:
                activation = await self.db.prepare_payment_activation(
                    payment_id,
                    int(activation["target_expires_at"])
                    if activation is not None and activation["target_expires_at"]
                    else None,
                )
                user = await self.db.get_user(telegram_id)
                if user is None:
                    raise ProvisioningError("no_user")
                if user["status"] == "banned":
                    raise ProvisioningError("banned")
                target = (
                    int(activation["target_expires_at"])
                    if activation is not None and activation["target_expires_at"]
                    else max(_ts(), int(user["expires_at"] or 0))
                    + int(payment["months"]) * DAYS_PER_MONTH * 86400
                )
                activation = await self.db.prepare_payment_activation(
                    payment_id, target
                )
                if activation["status"] == "completed":
                    return False
                expires = await self._apply_expiration_target(
                    telegram_id,
                    int(activation["target_expires_at"]),
                    str(payment.get("tier") or ""),
                )
                await self.db.complete_payment_activation(payment_id)
            except (ProvisioningError, HiddifyError) as exc:
                await self.db.record_payment_activation_error(
                    payment_id, type(exc).__name__
                )
                logger.exception("activation_failed", extra={"payment_id": payment_id})
                attempts = int((activation or {}).get("attempts") or 0)
                if attempts == 0:
                    await self.notifier.telegram(
                        self.settings.admin_telegram_id,
                        f"⚠️ Оплата <code>{payment_id}</code> получена, но узел не "
                        f"продлён: {type(exc).__name__}. Бот будет повторять попытки.",
                    )
                return True

        await audit(
            self.db,
            telegram_id,
            "subscription_activated",
            payment_id,
            months=int(payment["months"]),
            tier=payment.get("tier"),
            expires_at=expires,
        )
        await self.notifier.notify(
            telegram_id,
            f"✅ <b>Оплата получена, спасибо!</b> Защита активна до "
            f"<b>{format_date(expires)}</b>. Если устройство ещё не подключено, "
            "нажмите «📱 Подключить устройство».",
            push_title="Оплата получена",
            push_body=f"Узел активен до {format_date(expires)}",
            animation=True,
        )
        await self.apply_referral_bonus(telegram_id)
        return True

    async def revoke_refunded_payment(self, payment_id: str) -> int | None:
        """Take back the period of a refunded payment, exactly once.

        Returns the new expiry, or None if the payment was not paid or was
        already handled. Other periods (later payments, bonuses) stay intact.
        """
        payment = await self.db.get_payment(payment_id)
        if payment is None:
            return None
        telegram_id = int(payment["telegram_id"])
        async with self._lock(telegram_id):
            activation = await self.db.get_payment_activation(payment_id)
            if not await self.db.mark_payment_refunded(payment_id):
                return None
            user = await self.db.get_user(telegram_id)
            applied = activation is not None and activation["status"] == "completed"
            if user is None or not applied or user["status"] == "banned":
                await audit(self.db, telegram_id, "payment_refunded", payment_id)
                return None
            period = int(payment["months"]) * DAYS_PER_MONTH * 86400
            current = int(user["expires_at"] or 0)
            target = max(_ts(), current - period)
            try:
                await self._apply_expiration_target(telegram_id, target)
            except (ProvisioningError, HiddifyError):
                logger.exception(
                    "refund_revoke_failed", extra={"payment_id": payment_id}
                )
                await self.notifier.telegram(
                    self.settings.admin_telegram_id,
                    f"⚠️ Возврат <code>{payment_id}</code> записан, но срок в панели "
                    "не уменьшен. Проверьте пользователя вручную.",
                )
                return None
        await audit(
            self.db, telegram_id, "payment_refunded", payment_id, expires_at=target
        )
        if target <= _ts():
            fresh = await self.db.get_user(telegram_id)
            if fresh is not None:
                await self.expire_user(fresh)
        else:
            await self.notifier.telegram(
                telegram_id,
                "↩️ Платёж возвращён. Срок подписки уменьшен на оплаченный период: "
                f"теперь до <b>{format_date(target)}</b>.",
            )
        return target

    async def apply_referral_bonus(self, telegram_id: int) -> None:
        user = await self.db.get_user(telegram_id)
        if user is None or not user["referred_by"]:
            return
        job = await self.db.get_referral_bonus_job(telegram_id)
        if job is None:
            if await self.db.count_paid_payments(telegram_id) != 1:
                return
            await self.db.record_referral_bonus(
                int(user["referred_by"]),
                telegram_id,
                self.settings.referral_inviter_bonus_days,
                self.settings.referral_invitee_bonus_days,
            )
            job = await self.db.get_referral_bonus_job(telegram_id)
        if job is None or job["status"] == "completed":
            return

        inviter_id = int(job["inviter_id"])
        inviter_days = int(job["inviter_days"])
        invitee_days = int(job["invitee_days"])
        inviter_expires = await self._apply_referral_reward(
            telegram_id, inviter_id, inviter_days, "inviter"
        )
        if inviter_expires:
            await self.notifier.notify(
                inviter_id,
                f"🎁 Приглашённый вами пользователь оплатил аренду узла. "
                f"Вам начислено +{inviter_days} дней, узел активен до "
                f"<b>{format_date(inviter_expires)}</b>.",
                push_title="Реферальный бонус",
                push_body=f"+{inviter_days} дней к аренде узла",
            )
        invitee_expires = await self._apply_referral_reward(
            telegram_id, telegram_id, invitee_days, "invitee"
        )
        if invitee_expires:
            await self.notifier.telegram(
                telegram_id,
                f"🎁 Бонус за регистрацию по приглашению: +{invitee_days} дня.",
            )

    async def _apply_referral_reward(
        self,
        invitee_id: int,
        recipient_id: int,
        days: int,
        role: str,
    ) -> int | None:
        if role not in {"inviter", "invitee"}:
            raise ValueError("invalid referral reward role")
        job = await self.db.get_referral_bonus_job(invitee_id)
        if job is None or job[f"{role}_applied_at"] is not None:
            return None
        async with self._lock(recipient_id):
            job = await self.db.get_referral_bonus_job(invitee_id)
            if job is None or job[f"{role}_applied_at"] is not None:
                return None
            user = await self.db.get_user(recipient_id)
            if user is None or user["status"] == "banned":
                await self.db.record_referral_bonus_error(
                    invitee_id, role, "recipient_unavailable"
                )
                return None
            if days <= 0:
                await self.db.mark_referral_bonus_applied(invitee_id, role)
                return None
            target = job.get(f"{role}_target_expires_at")
            if target is None:
                target = max(_ts(), int(user["expires_at"] or 0)) + days * 86400
                target = await self.db.set_referral_bonus_target(
                    invitee_id, role, int(target)
                )
            try:
                expires = await self._apply_expiration_target(recipient_id, int(target))
                await self.db.mark_referral_bonus_applied(invitee_id, role)
                await audit(
                    self.db,
                    recipient_id,
                    "bonus_days",
                    reason=f"referral_{role}",
                    days=days,
                )
                return expires
            except (ProvisioningError, HiddifyError) as exc:
                await self.db.record_referral_bonus_error(
                    invitee_id, role, type(exc).__name__
                )
                logger.exception(
                    "referral_bonus_failed",
                    extra={"invitee_id": invitee_id, "recipient_id": recipient_id},
                )
                return None

    async def retry_pending_referral_bonuses(self) -> None:
        # Also find the narrow crash window after payment activation completed
        # but before the referral job row was created.
        candidates = await self.db.pending_referral_invitees()
        for row in candidates:
            try:
                await self.apply_referral_bonus(int(row["telegram_id"]))
            except Exception:
                logger.exception(
                    "referral_bonus_retry_failed",
                    extra={"telegram_id": row["telegram_id"]},
                )

    async def retry_paid_activations(self) -> None:
        for row in await self.db.payments_pending_activation():
            try:
                await self.process_paid_payment(str(row["payment_id"]))
            except Exception:
                logger.exception(
                    "payment_activation_retry_failed",
                    extra={"payment_id": row["payment_id"]},
                )

    # ----------------------------------------------------------------- expiry
    async def expire_user(self, user: dict[str, Any]) -> None:
        telegram_id = int(user["telegram_id"])
        async with self._lock(telegram_id):
            fresh = await self.db.get_user(telegram_id)
            if fresh is None or fresh["status"] not in ("active", "suspended"):
                return
            if fresh["expires_at"] and int(fresh["expires_at"]) > _ts():
                return
            if fresh["node_username"]:
                with contextlib.suppress(HiddifyNotFound):
                    await self.hiddify.disable_user(fresh["node_username"])
            await self.db.set_user_status(telegram_id, "expired")
        await audit(self.db, telegram_id, "expired", fresh["node_username"])
        await self.notifier.notify(
            telegram_id,
            "⏳ <b>Срок подписки закончился</b>, защита подключения выключена.\n\n"
            "Продлите в разделе «💳 Оплата» — подключение заработает сразу после "
            "оплаты, настраивать устройства заново не нужно. Выгоднее всего — "
            "сразу на 6 месяцев.",
            push_title="Срок аренды истёк",
            push_body="Продлите аренду узла, чтобы восстановить подключение",
            url="/#/pay",
        )

    # ------------------------------------------------------------------- bans
    async def autoban(
        self,
        telegram_id: int,
        node_username: str | None,
        reason: str,
        detail: dict[str, Any],
    ) -> bool:
        async with self._lock(telegram_id):
            user = await self.db.get_user(telegram_id)
            if user is None or user["status"] == "banned":
                if user is not None and user["status"] == "banned":
                    await self.db.delete_pending_ban(telegram_id)
                return False

            pending = await self.db.get_pending_ban(telegram_id)
            is_new_request = pending is None
            if pending is not None:
                node = (
                    pending["node_username"] or node_username or user["node_username"]
                )
                reason = str(pending["reason"])
                try:
                    stored_detail = json.loads(pending["detail"])
                    detail = (
                        stored_detail if isinstance(stored_detail, dict) else detail
                    )
                except (TypeError, ValueError):
                    pass
            else:
                node = node_username or user["node_username"]
                await self.db.queue_pending_ban(
                    telegram_id,
                    node,
                    reason,
                    json.dumps(detail, ensure_ascii=False, default=str),
                )

            if node:
                try:
                    await self._disable_and_confirm(node)
                except HiddifyNotFound:
                    pass
                except HiddifyError as exc:
                    await self.db.record_pending_ban_error(
                        telegram_id, type(exc).__name__
                    )
                    logger.exception("autoban_disable_failed", extra={"node": node})
                    if is_new_request:
                        await self.notifier.telegram(
                            self.settings.admin_telegram_id,
                            f"⚠️ AUTOBAN <code>{telegram_id}</code> не подтверждён "
                            f"Hiddify: узел <code>{node}</code> пока не отключён. "
                            "Задача сохранена, бот будет повторять попытки.",
                        )
                    return False
            await self.db.confirm_autoban(
                telegram_id,
                node,
                json.dumps(
                    {"reason": reason, **detail}, ensure_ascii=False, default=str
                ),
                _ts(),
            )
            await self.db.revoke_all_refresh_tokens(telegram_id)
            await self.db.delete_pending_ban(telegram_id)
        AUTOBANS.labels(reason=reason).inc()
        await audit(self.db, telegram_id, "autoban", node, reason=reason, **detail)
        await self.notifier.notify(
            telegram_id,
            AUTOBAN_TEXT,
            push_title="Access denied",
            push_body=AUTOBAN_TEXT,
        )
        await self.notifier.telegram(
            self.settings.admin_telegram_id,
            f"🚫 AUTOBAN <code>{telegram_id}</code> ({reason}): "
            f"<code>{json.dumps(detail, default=str)[:500]}</code>",
        )
        return True

    async def retry_pending_bans(self) -> None:
        for pending in await self.db.pending_bans():
            try:
                detail = json.loads(pending["detail"])
                if not isinstance(detail, dict):
                    detail = {}
                await self.autoban(
                    int(pending["telegram_id"]),
                    pending["node_username"],
                    str(pending["reason"]),
                    detail,
                )
            except Exception:
                logger.exception(
                    "pending_ban_retry_failed",
                    extra={"telegram_id": pending["telegram_id"]},
                )

    async def soft_suspend(
        self, telegram_id: int, node_username: str, gb: float
    ) -> bool:
        async with self._lock(telegram_id):
            user = await self.db.get_user(telegram_id)
            if user is None or user["status"] != "active":
                return False
            await self.db.add_anomaly(
                telegram_id,
                node_username,
                "TRAFFIC_ANOMALY",
                json.dumps({"gb_per_day": round(gb, 2)}),
            )
            await self.hiddify.disable_user(node_username)
            await self.db.set_user_status(telegram_id, "suspended")
        SOFT_SUSPENSIONS.inc()
        await audit(
            self.db, telegram_id, "soft_suspend", node_username, gb=round(gb, 2)
        )
        await self.notifier.notify(
            telegram_id,
            "⚠️ Подключение временно приостановлено: зафиксирован аномальный объём "
            f"трафика ({gb:.0f} ГБ за сутки). Напишите в «💬 Поддержка» для проверки.",
            push_title="Подключение приостановлено",
            push_body="Аномальный объём трафика, свяжитесь с поддержкой",
        )
        await self.notifier.telegram(
            self.settings.admin_telegram_id,
            f"⚠️ Soft suspend <code>{telegram_id}</code>: {gb:.0f} GB/day. "
            f"Снять: <code>/unsuspend {telegram_id}</code>",
        )
        return True

    async def unsuspend(self, telegram_id: int) -> bool:
        async with self._lock(telegram_id):
            user = await self.db.get_user(telegram_id)
            if (
                user is None
                or user["status"] != "suspended"
                or not user["node_username"]
            ):
                return False
            if int(user["expires_at"] or 0) <= _ts():
                await self.db.set_user_status(telegram_id, "expired")
                return True
            await self.hiddify.enable_user(user["node_username"])
            await self.db.set_user_status(telegram_id, "active")
        await audit(self.db, telegram_id, "unsuspend", user["node_username"])
        await self.notifier.telegram(telegram_id, "✅ Подключение восстановлено.")
        return True
