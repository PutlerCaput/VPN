"""Background jobs: expiry, reminders, anomaly polling (AUTOBAN), housekeeping."""

from __future__ import annotations

import logging
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config import Settings, format_gb
from db.database import UNPAID_ORDER_TTL_SECONDS, Database
from services.awg import AwgService, combined_usage
from services.health import ServiceHealth
from services.marzban import MarzbanClient, MarzbanError
from services.metrics import ACTIVE_USERS, JOB_FAILURES
from services.notifier import Notifier
from services.payment_reconciliation import PaymentReconciler
from services.provisioning import ProvisioningService, format_date
from services.security import SlidingWindowLimiter, is_valid_node_username
from services.stars import StarsPayments

logger = logging.getLogger(__name__)

# Confidence weights of IDS signals. A ban needs AUTOBAN_CONFIDENCE, so a single
# port scan (60) only alerts the admin, while a scan plus a probe, or a confirmed
# cybercrime report, bans. Signals add up over SIGNAL_WINDOW_SECONDS.
SIGNAL_WEIGHTS: dict[str, int] = {
    "CYBERCRIME": 100,
    "DDOS_ATTACK": 80,
    "NETSCAN_ABUSE": 60,
    "PORT_SCANNING": 60,
    "MALICIOUS_PROBE": 40,
}
SEVERE_KINDS = frozenset(SIGNAL_WEIGHTS)
AUTOBAN_CONFIDENCE = 90
SIGNAL_WINDOW_SECONDS = 24 * 3600
# Hardcoded by policy. Affects only soft suspension; AUTOBAN always triggers.
AUTO_SUSPEND_ON_ANOMALY = True
GB = 1024**3
ONLINE_WINDOW_SECONDS = 10 * 60
SUBSCRIPTION_REMINDER_LEAD_SECONDS = 3 * 24 * 60 * 60
SUBSCRIPTION_REMINDER_INTERVAL_MINUTES = 15
# Marzban already stops an expired node at its expire time; this job only
# brings the bot's own status and the user notice in line with it.
EXPIRY_CHECK_INTERVAL_MINUTES = 15
QUOTA_WARNING_PERCENT = 80
TRAFFIC_NOTICE_RETENTION_SECONDS = 90 * 86400
FLAG_RE = re.compile(r"[A-Z_]{3,}")


def extract_flags(note: str | None) -> set[str]:
    """Abuse flags are written into the Marzban user note by an IDS or an admin."""
    if not note:
        return set()
    return set(FLAG_RE.findall(note)) & SEVERE_KINDS


def confidence(kinds: set[str]) -> int:
    return min(100, sum(SIGNAL_WEIGHTS.get(kind, 0) for kind in kinds))


def _parse_online_at(value: Any) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


class AnomalyMonitor:
    def __init__(
        self,
        db: Database,
        marzban: MarzbanClient,
        provisioning: ProvisioningService,
        settings: Settings,
    ) -> None:
        self.db = db
        self.marzban = marzban
        self.provisioning = provisioning
        self.settings = settings
        self._signals: OrderedDict[int, dict[str, float]] = OrderedDict()
        self._review_alerts: OrderedDict[int, float] = OrderedDict()

    async def _review(self, telegram_id: int) -> None:
        current = time.monotonic()
        if current - self._review_alerts.get(telegram_id, -3600.0) < 3600:
            return
        self._review_alerts[telegram_id] = current
        self._review_alerts.move_to_end(telegram_id)
        while len(self._review_alerts) > 10000:
            self._review_alerts.popitem(last=False)
        await self.provisioning.notifier.telegram(
            self.settings.admin_telegram_id,
            "⚠️ Обнаружен сигнал, требующий проверки с ПК. "
            "Автоматическая блокировка выключена; сигнал не доказывает нарушение.",
        )

    async def handle_report(
        self,
        node_username: str,
        kinds: set[str],
        detail: dict[str, Any],
    ) -> bool:
        """Entry point for external IDS reports. Returns True if a ban was applied."""
        severe = kinds & SEVERE_KINDS
        if not severe or not is_valid_node_username(node_username):
            return False
        user = await self.db.get_user_by_node(node_username)
        if user is None or user["status"] == "banned":
            return False
        telegram_id = int(user["telegram_id"])
        current = time.monotonic()
        for identity in list(self._signals):
            signals = self._signals[identity]
            for kind in list(signals):
                if current - signals[kind] >= SIGNAL_WINDOW_SECONDS:
                    del signals[kind]
            if not signals:
                del self._signals[identity]
        signals = self._signals.setdefault(telegram_id, {})
        for kind in severe:
            signals[kind] = current
        self._signals.move_to_end(telegram_id)
        while len(self._signals) > 10000:
            self._signals.popitem(last=False)
        return await self._evaluate(telegram_id, node_username, severe, detail)

    async def _recent_signals(self, telegram_id: int) -> set[str]:
        current = time.monotonic()
        return {
            kind
            for kind, observed in self._signals.get(telegram_id, {}).items()
            if current - observed < SIGNAL_WINDOW_SECONDS
        }

    async def _evaluate(
        self,
        telegram_id: int,
        node: str,
        new_kinds: set[str],
        detail: dict[str, Any],
        *,
        alert: bool = True,
    ) -> bool:
        kinds = new_kinds | await self._recent_signals(telegram_id)
        score = confidence(kinds)
        if score >= AUTOBAN_CONFIDENCE:
            if self.settings.autoban_mode == "review":
                await self._review(telegram_id)
                return False
            return await self.provisioning.autoban(
                telegram_id,
                node,
                "flags",
                {"flags": sorted(kinds), "confidence": score, **detail},
            )
        if alert:
            await self.provisioning.notifier.telegram(
                self.settings.admin_telegram_id,
                "⚠️ Сигнал IDS: "
                f"{', '.join(sorted(kinds))}. "
                f"Уверенность {score}/{AUTOBAN_CONFIDENCE}, бан не применён. "
                "Сигнал требует проверки с ПК; он не доказывает нарушение.",
            )
        return False

    async def _track_traffic(
        self,
        node: str,
        used: int,
        online_at: str | None,
    ) -> tuple[int, float]:
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        record = await self.db.get_traffic_day(node, day)
        if record is None:
            baseline = used
            previous = used
        else:
            baseline = int(record["baseline_bytes"])
            previous = int(record["last_bytes"])
            if used < baseline:  # counter reset in the panel
                baseline = used
                previous = used
        await self.db.upsert_traffic_day(
            node,
            day,
            baseline_bytes=baseline,
            last_bytes=used,
            last_online_at=online_at,
        )
        return max(0, used - previous), max(0, used - baseline) / GB

    async def _track_connection(
        self,
        telegram_id: int,
        node: str,
        online_ts: int | None,
        delta: int,
    ) -> None:
        current = int(time.time())
        open_log = await self.db.get_open_conn_log(node)
        is_online = (
            online_ts is not None and current - online_ts <= ONLINE_WINDOW_SECONDS
        )
        if is_online and open_log is None:
            await self.db.add_conn_log(
                telegram_id, node, online_ts or current, 0, delta
            )
        elif is_online and open_log is not None:
            if delta:
                await self.db.add_conn_bytes(int(open_log["id"]), delta)
        elif open_log is not None:
            await self.db.close_open_conn_logs(node, online_ts or current)

    async def _quota_notice(
        self, telegram_id: int, node: str, panel_user: dict[str, Any]
    ) -> None:
        """Warn once at 80% of the quota and once when the panel stops the node."""
        try:
            limit = int(panel_user.get("data_limit") or 0)
            used = int(panel_user.get("used_traffic") or 0)
            lifetime = int(panel_user.get("lifetime_used_traffic") or used)
        except (TypeError, ValueError):
            return
        if limit <= 0:
            return
        window = max(0, lifetime - used)
        used, limit = await combined_usage(self.db, telegram_id, used, lifetime, limit)
        exhausted = panel_user.get("status") == "limited" or used >= limit
        if exhausted:
            level = 100
        elif used * 100 >= limit * QUOTA_WARNING_PERCENT:
            level = QUOTA_WARNING_PERCENT
        else:
            return
        if not await self.db.claim_traffic_notice(node, window, level):
            return
        resets = panel_user.get("data_limit_reset_strategy") not in (None, "no_reset")
        amount = f"{format_gb(used)} из {format_gb(limit)} ГБ"
        if level == 100:
            text = (
                f"⛔️ Трафик подписки израсходован ({amount}). Подключение "
                "приостановлено до обнуления счётчика: он обнуляется каждые "
                "30 дней. Если считаете это ошибкой, напишите в «💬 Поддержка»."
                if resets
                else f"⛔️ Трафик пробного доступа израсходован ({amount}). "
                "Оформить подписку можно в разделе «💳 Оплата»."
            )
            title = "Трафик израсходован"
        else:
            text = f"📶 Использовано {QUOTA_WARNING_PERCENT}% трафика: {amount}. " + (
                "Счётчик обнуляется каждые 30 дней. Текущий расход — в «👤 Профиль»."
                if resets
                else "После исчерпания подключение остановится."
            )
            title = "Трафик заканчивается"
        delivered = await self.provisioning.notifier.notify(
            telegram_id, text, push_title=title, push_body=amount
        )
        if not delivered:
            await self.db.release_traffic_notice(node, window, level)

    async def check_user(self, panel_user: dict[str, Any]) -> None:
        node = panel_user.get("username")
        if not isinstance(node, str) or not is_valid_node_username(node):
            return
        user = await self.db.get_user_by_node(node)
        if user is None:
            return
        telegram_id = int(user["telegram_id"])
        used = int(
            panel_user.get("lifetime_used_traffic")
            or panel_user.get("used_traffic")
            or 0
        )
        online_at = panel_user.get("online_at")
        delta, gb_today = await self._track_traffic(node, used, online_at)
        if self.db.activity_logging_enabled:
            await self._track_connection(
                telegram_id, node, _parse_online_at(online_at), delta
            )

        if user["status"] == "banned":
            return
        if user["status"] == "active":
            await self._quota_notice(telegram_id, node, panel_user)
        flags = extract_flags(panel_user.get("note"))
        # Notes persist between polls, so a sub-threshold note does not re-alert.
        if flags and await self._evaluate(
            telegram_id, node, flags, {"gb_today": round(gb_today, 2)}, alert=False
        ):
            return
        if gb_today > self.settings.autoban_gb_per_day:
            if self.settings.autoban_mode == "review":
                await self._review(telegram_id)
                return
            await self.provisioning.autoban(
                telegram_id,
                node,
                "traffic",
                {
                    "gb_today": round(gb_today, 2),
                    "threshold": self.settings.autoban_gb_per_day,
                },
            )
            return
        if (
            AUTO_SUSPEND_ON_ANOMALY
            and self.settings.autoban_mode == "automatic"
            and user["status"] == "active"
            and gb_today > self.settings.traffic_anomaly_gb_per_day
        ):
            day = datetime.now(UTC).strftime("%Y-%m-%d")
            record = await self.db.get_traffic_day(node, day)
            if record is not None and not record["soft_flagged"]:
                await self.db.set_traffic_soft_flag(node, day)
                await self.provisioning.soft_suspend(telegram_id, node, gb_today)

    async def poll(self) -> None:
        users = await self.marzban.get_users_usage()
        active = 0
        for panel_user in users:
            if panel_user.get("status") == "active":
                active += 1
            try:
                await self.check_user(panel_user)
            except Exception:
                logger.exception(
                    "anomaly_check_failed", extra={"node": panel_user.get("username")}
                )
        ACTIVE_USERS.set(active)


class Jobs:
    def __init__(
        self,
        db: Database,
        provisioning: ProvisioningService,
        notifier: Notifier,
        monitor: AnomalyMonitor,
        settings: Settings,
        limiters: list[SlidingWindowLimiter],
        payment_reconciler: PaymentReconciler | None = None,
        *,
        stars: StarsPayments | None = None,
        health: ServiceHealth | None = None,
        awg: AwgService | None = None,
    ) -> None:
        self.db = db
        self.provisioning = provisioning
        self.notifier = notifier
        self.monitor = monitor
        self.settings = settings
        self.limiters = limiters
        self.payment_reconciler = payment_reconciler
        self.stars = stars
        self.health = health
        self.awg = awg

    async def daily_expiry_check(self) -> None:
        for user in await self.db.users_to_expire(int(time.time())):
            try:
                await self.provisioning.expire_user(user)
            except MarzbanError:
                logger.exception(
                    "expire_failed", extra={"telegram_id": user["telegram_id"]}
                )

    async def expiry_reminder(self) -> None:
        current = int(time.time())
        reminder_deadline = current + SUBSCRIPTION_REMINDER_LEAD_SECONDS
        for user in await self.db.users_to_remind(current, reminder_deadline):
            telegram_id = int(user["telegram_id"])
            expires = int(user["expires_at"])
            renewing = (
                self.stars is not None
                and (sub := await self.stars.active_subscription(telegram_id))
                is not None
                and sub["status"] == "active"
            )
            text = (
                f"⏰ Оплаченный срок заканчивается <b>{format_date(expires)}</b>. "
                "Автопродление через Telegram Stars включено: подписка продлится "
                "сама, если на балансе хватит звёзд. Отключить: /autorenew."
                if renewing
                else f"⏰ Подписка заканчивается <b>{format_date(expires)}</b>.\n\n"
                "Продлите заранее в разделе «💳 Оплата»: новый срок добавится к "
                "текущему, ни один оплаченный день не пропадёт, а защита не "
                "прервётся. Оплата через СБП занимает меньше минуты."
            )
            delivered = await self.notifier.notify(
                telegram_id,
                text,
                push_title="Подписка скоро закончится",
                push_body=f"Подписка активна до {format_date(expires)}",
                url="/#/pay",
            )
            if delivered:
                await self.db.mark_reminded(telegram_id, expires)

    async def payment_reconciliation(self) -> None:
        if self.payment_reconciler is not None:
            await self.payment_reconciler.poll()

    async def stars_reconciliation(self) -> None:
        if self.stars is not None:
            await self.stars.reconcile()

    async def awg_reconcile(self) -> None:
        if self.awg is not None:
            await self.awg.reconcile()

    async def health_check(self) -> None:
        if self.health is not None:
            await self.health.poll()

    async def anomaly_poller(self) -> None:
        await self.monitor.poll()

    async def housekeeping(self) -> None:
        current = int(time.time())
        await self.db.purge_refresh_tokens(current)
        retention = self.settings.conn_log_retention_days * 86400
        await self.db.purge_conn_logs(current - retention)
        cutoff = (datetime.now(UTC) - timedelta(days=7)).strftime("%Y-%m-%d")
        await self.db.purge_traffic_days(cutoff)
        await self.db.purge_traffic_notices(current - TRAFFIC_NOTICE_RETENTION_SECONDS)
        if self.stars is not None:
            await self.stars.end_lapsed()
        for limiter in self.limiters:
            limiter.prune()

    async def cancel_unpaid(self) -> None:
        await self.db.cancel_stale_payments(int(time.time()) - UNPAID_ORDER_TTL_SECONDS)

    async def payment_activation_retry(self) -> None:
        await self.provisioning.retry_paid_activations()

    async def referral_bonus_retry(self) -> None:
        await self.provisioning.retry_pending_referral_bonuses()

    async def pending_ban_retry(self) -> None:
        await self.provisioning.retry_pending_bans()


def _guarded(
    name: str, func: Callable[[], Awaitable[None]]
) -> Callable[[], Awaitable[None]]:
    async def runner() -> None:
        try:
            await func()
        except Exception:
            JOB_FAILURES.labels(job=name).inc()
            logger.exception("job_failed", extra={"job": name})

    runner.__name__ = name
    return runner


def build_scheduler(jobs: Jobs) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=UTC)
    defaults = {"coalesce": True, "max_instances": 1, "misfire_grace_time": 600}
    scheduler.add_job(
        _guarded("daily_expiry_check", jobs.daily_expiry_check),
        IntervalTrigger(minutes=EXPIRY_CHECK_INTERVAL_MINUTES, timezone=UTC),
        id="daily_expiry_check",
        **defaults,
    )
    scheduler.add_job(
        _guarded("expiry_reminder", jobs.expiry_reminder),
        IntervalTrigger(minutes=SUBSCRIPTION_REMINDER_INTERVAL_MINUTES, timezone=UTC),
        id="expiry_reminder",
        **defaults,
    )
    scheduler.add_job(
        _guarded("anomaly_poller", jobs.anomaly_poller),
        IntervalTrigger(minutes=5, timezone=UTC),
        id="anomaly_poller",
        **defaults,
    )
    scheduler.add_job(
        _guarded("housekeeping", jobs.housekeeping),
        IntervalTrigger(hours=1, timezone=UTC),
        id="housekeeping",
        **defaults,
    )
    scheduler.add_job(
        _guarded("payment_reconciliation", jobs.payment_reconciliation),
        IntervalTrigger(minutes=1, timezone=UTC),
        id="payment_reconciliation",
        **defaults,
    )
    scheduler.add_job(
        _guarded("awg_reconcile", jobs.awg_reconcile),
        IntervalTrigger(minutes=3, timezone=UTC),
        id="awg_reconcile",
        **defaults,
    )
    scheduler.add_job(
        _guarded("health_check", jobs.health_check),
        IntervalTrigger(minutes=2, timezone=UTC),
        id="health_check",
        **defaults,
    )
    scheduler.add_job(
        _guarded("cancel_unpaid", jobs.cancel_unpaid),
        IntervalTrigger(minutes=5, timezone=UTC),
        id="cancel_unpaid",
        **defaults,
    )
    scheduler.add_job(
        _guarded("stars_reconciliation", jobs.stars_reconciliation),
        IntervalTrigger(minutes=5, timezone=UTC),
        id="stars_reconciliation",
        **defaults,
    )
    scheduler.add_job(
        _guarded("payment_activation_retry", jobs.payment_activation_retry),
        IntervalTrigger(minutes=1, timezone=UTC),
        id="payment_activation_retry",
        **defaults,
    )
    scheduler.add_job(
        _guarded("referral_bonus_retry", jobs.referral_bonus_retry),
        IntervalTrigger(minutes=5, timezone=UTC),
        id="referral_bonus_retry",
        **defaults,
    )
    scheduler.add_job(
        _guarded("pending_ban_retry", jobs.pending_ban_retry),
        IntervalTrigger(minutes=1, timezone=UTC),
        id="pending_ban_retry",
        **defaults,
    )
    return scheduler
