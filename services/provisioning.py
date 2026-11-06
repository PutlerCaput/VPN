"""Business logic: trials, subscriptions, referral bonuses, bans."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from config import DAYS_PER_MONTH, DEFAULT_TIER, TIERS, Settings, device_note
from db.database import Database
from services.audit import audit
from services.marzban import MarzbanClient, MarzbanError, MarzbanNotFound
from services.metrics import (
    AUTOBANS,
    PAYMENTS_PAID,
    SOFT_SUSPENSIONS,
    TRIALS_PROVISIONED,
)
from services.notifier import Notifier
from services.security import node_username_for

logger = logging.getLogger(__name__)

AUTOBAN_TEXT = (
    "🔴 Access denied. Your remote node connection has been permanently blocked "
    "automatically due to severe acceptable use policy violations (cybercrime "
    "indicators detected)."
)


class ProvisioningError(Exception):
    pass


@dataclass(slots=True)
class TrialResult:
    ok: bool
    reason: str
    config_link: str | None = None
    expires_at: int | None = None


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
        marzban: MarzbanClient,
        notifier: Notifier,
        settings: Settings,
    ) -> None:
        self.db = db
        self.marzban = marzban
        self.notifier = notifier
        self.settings = settings
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    def _lock(self, telegram_id: int) -> asyncio.Lock:
        return self._locks[telegram_id]

    async def _disable_and_confirm(self, node_username: str) -> None:
        result = await self.marzban.disable_user(node_username)
        if result.get("status") == "disabled":
            return
        current = await self.marzban.get_user(node_username)
        if current.get("status") != "disabled":
            raise MarzbanError("Marzban did not confirm disabled status")

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
                    user["config_link"],
                    user["expires_at"],
                )
            node = node_username_for(telegram_id)
            expires_at = _ts() + self.settings.trial_seconds
            try:
                created = await self.marzban.create_user(node, expire=expires_at)
                link = MarzbanClient.extract_vless_link(created)
            except MarzbanError:
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
            await self.db.mark_trial_used(telegram_id)
            TRIALS_PROVISIONED.inc()
            await audit(
                self.db, telegram_id, "trial_provisioned", node, expires_at=expires_at
            )
            return TrialResult(True, "provisioned", link, expires_at)

    # ----------------------------------------------------------------- config
    async def fresh_config(self, telegram_id: int) -> str:
        user = await self.db.get_user(telegram_id)
        if user is None or user["status"] != "active" or not user["node_username"]:
            raise ProvisioningError("no_active_node")
        link = await self.marzban.get_vless_link(user["node_username"])
        if link != user["config_link"]:
            await self.db.set_config_link(telegram_id, link)
        return link

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
        node = user["node_username"] or node_username_for(telegram_id)
        base = max(_ts(), int(user["expires_at"] or 0))
        new_expires = base + seconds
        try:
            await self.marzban.modify_user(
                node, expire=new_expires, status="active", note=device_note(tier)
            )
            data = await self.marzban.get_user(node)
        except MarzbanNotFound:
            data = await self.marzban.create_user(node, expire=new_expires, tier=tier)
        link: str | None
        try:
            link = MarzbanClient.extract_vless_link(data)
        except MarzbanError:
            link = None
        status = "suspended" if user["status"] == "suspended" else "active"
        if status == "suspended":
            # Keep a soft-suspended node disabled until an admin lifts it.
            await self.marzban.disable_user(node)
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
            except (ProvisioningError, MarzbanError):
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
        retry jobs persist the target before calling Marzban, then repeat this
        exact target until both the panel and the local DB agree.
        Caller must hold the user's lock.
        """
        user = await self.db.get_user(telegram_id)
        if user is None:
            raise ProvisioningError("no_user")
        if user["status"] == "banned":
            raise ProvisioningError("banned")
        selected_tier = tier if tier in TIERS else str(user.get("tier") or DEFAULT_TIER)
        node = user["node_username"] or node_username_for(telegram_id)
        try:
            await self.marzban.modify_user(
                node,
                expire=target_expires_at,
                status=("disabled" if user["status"] == "suspended" else "active"),
                note=device_note(selected_tier),
            )
            data = await self.marzban.get_user(node)
        except MarzbanNotFound:
            data = await self.marzban.create_user(
                node, expire=target_expires_at, tier=selected_tier
            )
            if user["status"] == "suspended":
                await self._disable_and_confirm(node)
                data = await self.marzban.get_user(node)
        try:
            link = MarzbanClient.extract_vless_link(data)
        except MarzbanError:
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
            except (ProvisioningError, MarzbanError) as exc:
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
            f"✅ Оплата получена. Узел активен до <b>{format_date(expires)}</b>.",
            push_title="Оплата получена",
            push_body=f"Узел активен до {format_date(expires)}",
        )
        await self.apply_referral_bonus(telegram_id)
        return True

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
            except (ProvisioningError, MarzbanError) as exc:
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
                with contextlib.suppress(MarzbanNotFound):
                    await self.marzban.disable_user(fresh["node_username"])
            await self.db.set_user_status(telegram_id, "expired")
        await audit(self.db, telegram_id, "expired", fresh["node_username"])
        await self.notifier.notify(
            telegram_id,
            "⏳ Срок аренды узла истёк, подключение отключено. "
            "Продлить можно в разделе «💳 Оплата».",
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
                except MarzbanNotFound:
                    pass
                except MarzbanError as exc:
                    await self.db.record_pending_ban_error(
                        telegram_id, type(exc).__name__
                    )
                    logger.exception("autoban_disable_failed", extra={"node": node})
                    if is_new_request:
                        await self.notifier.telegram(
                            self.settings.admin_telegram_id,
                            f"⚠️ AUTOBAN <code>{telegram_id}</code> не подтверждён "
                            f"Marzban: узел <code>{node}</code> пока не отключён. "
                            "Задача сохранена, бот будет повторять попытки.",
                        )
                    return False
            async with self.db.transaction() as conn:
                await conn.execute(
                    "UPDATE users SET status = 'banned' WHERE telegram_id = ?",
                    (telegram_id,),
                )
                await conn.execute(
                    "INSERT INTO anomalies (telegram_id, node_username, kind, detail, "
                    "created_at) VALUES (?, ?, 'AUTOBAN', ?, ?)",
                    (
                        telegram_id,
                        node,
                        json.dumps(
                            {"reason": reason, **detail},
                            ensure_ascii=False,
                            default=str,
                        ),
                        _ts(),
                    ),
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
            await self.marzban.disable_user(node_username)
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
            await self.marzban.enable_user(user["node_username"])
            await self.db.set_user_status(telegram_id, "active")
        await audit(self.db, telegram_id, "unsuspend", user["node_username"])
        await self.notifier.telegram(telegram_id, "✅ Подключение восстановлено.")
        return True
