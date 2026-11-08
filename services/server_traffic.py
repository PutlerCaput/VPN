"""Server traffic against the hosting plan's monthly allowance.

The anomaly poller adds every subscriber's traffic to a per-day server total,
and AmneziaWG adds its own. From that series this module forecasts the billing
period with damped-trend exponential smoothing (Holt; Gardner and McKenzie):
the smoothing and damping parameters are learned from the series itself by
minimising the one-day-ahead error, and the spread of those errors gives a
90 % upper bound. The owner is warned once per period and level. New trials
can pause when the period's traffic reaches 90 % of the allowance; that is off
by default (SERVER_TRAFFIC_PAUSE_TRIALS) because clause 1.6 of the offer
promises a trial after acceptance and has to allow the pause first.

The panel counts a subscriber's traffic once. The provider counts the server's
network card, which carries it twice (from the internet and to the client)
when both directions are billed: COUNT_FACTORS turns one into the other.
These are estimates; the hosting panel has the provider's own figure.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from config import Settings
from db.database import Database
from services.notifier import Notifier

logger = logging.getLogger(__name__)

# Panel bytes -> bytes on the network card: packet headers and encryption add
# about 5 %, and billing both directions counts the traffic twice.
COUNT_FACTORS: dict[str, float] = {"both": 2.1, "out": 1.05, "in": 1.05}
COUNT_NAMES: dict[str, str] = {
    "both": "входящий и исходящий",
    "out": "только исходящий",
    "in": "только входящий",
}
DECIMAL_GB = 10**9
HISTORY_DAYS = 56  # what the model learns from
RETENTION_DAYS = 400
MIN_HOLT_DAYS = 7
MIN_FORECAST_ALERT_DAYS = 3
Z90 = 1.2816  # one-sided 90 % quantile of the normal distribution
TRIAL_PAUSE_SHARE = 0.9
ALPHAS = (0.1, 0.2, 0.3, 0.5, 0.7, 0.9)
BETAS = (0.0, 0.05, 0.1, 0.2)
PHIS = (0.8, 0.9, 0.98)
MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def billing_period(today: date, reset_day: int) -> tuple[date, date]:
    """[start, end) of the provider's period that contains ``today``."""
    if not 1 <= reset_day <= 28:
        raise ValueError("reset_day must be 1..28")
    if today.day >= reset_day:
        start = today.replace(day=reset_day)
    else:
        start = (today.replace(day=1) - timedelta(days=1)).replace(day=reset_day)
    if start.month == 12:
        end = date(start.year + 1, 1, reset_day)
    else:
        end = date(start.year, start.month + 1, reset_day)
    return start, end


@dataclass(frozen=True)
class Model:
    """Daily traffic model. ``day(h)`` is the h-th day after the last one
    learned from (h = 1 is the next day)."""

    level: float
    trend: float
    phi: float
    sigma: float  # spread of the one-day-ahead errors
    days: int  # days learned from
    method: str  # "holt", "mean" or "today"

    def day(self, h: int) -> float:
        damping = sum(self.phi**j for j in range(1, h + 1))
        return max(0.0, self.level + damping * self.trend)


def _holt(series: Sequence[float], alpha: float, beta: float, phi: float) -> Model:
    level, trend = series[0], 0.0
    errors: list[float] = []
    for value in series[1:]:
        predicted = level + phi * trend
        errors.append(value - predicted)
        new_level = alpha * value + (1 - alpha) * predicted
        trend = beta * (new_level - level) + (1 - beta) * phi * trend
        level = new_level
    # The first errors mostly measure the start values, not the parameters.
    scored = errors[1:] or errors
    sigma = math.sqrt(sum(e * e for e in scored) / max(1, len(scored) - 1))
    return Model(level, trend, phi, sigma, len(series), "holt")


def fit(series: Sequence[float]) -> Model | None:
    """Learn a daily model: the mean for a short series, otherwise damped Holt
    with the parameters that predict the series best one day ahead."""
    n = len(series)
    if n == 0:
        return None
    if n < MIN_HOLT_DAYS:
        mean = sum(series) / n
        if n >= 3:
            sigma = math.sqrt(sum((y - mean) ** 2 for y in series) / (n - 1))
        else:
            sigma = 0.5 * mean
        return Model(mean, 0.0, 0.0, sigma, n, "mean")
    best: Model | None = None
    for alpha in ALPHAS:
        for beta in BETAS:
            for phi in PHIS:
                model = _holt(series, alpha, beta, phi)
                if best is None or model.sigma < best.sigma:
                    best = model
    return best


@dataclass(frozen=True)
class TrafficOutlook:
    today: date  # UTC day the outlook was made
    period_start: date
    period_end: date  # first day of the next period
    limit_bytes: int
    used_bytes: int  # this period so far, as the provider would count it
    expected_bytes: int | None  # the whole period
    high_bytes: int | None  # 90 % upper bound of the whole period
    daily_bytes: int | None  # current pace per day
    exhaust_day: date | None  # when the allowance runs out at this pace
    estimated_days: int  # days of this period without data, filled by the model
    history_days: int
    method: str  # "holt", "mean", "today" or "none"
    clients: int
    client_month_bytes: int | None  # an average client per 30 days
    counts: str
    pause_trials: bool = False  # SERVER_TRAFFIC_PAUSE_TRIALS

    @property
    def period(self) -> str:
        return self.period_start.isoformat()

    def share(self, value: int | None) -> float | None:
        if value is None or self.limit_bytes <= 0:
            return None
        return value / self.limit_bytes

    @property
    def used_share(self) -> float:
        return self.share(self.used_bytes) or 0.0

    @property
    def expected_share(self) -> float | None:
        return self.share(self.expected_bytes)

    @property
    def near_limit(self) -> bool:
        return self.used_share >= TRIAL_PAUSE_SHARE

    @property
    def trials_paused(self) -> bool:
        return self.pause_trials and self.near_limit

    @property
    def capacity_clients(self) -> int | None:
        """Average clients the allowance carries per month at today's usage."""
        if not self.client_month_bytes:
            return None
        return int(self.limit_bytes // self.client_month_bytes)


def build_outlook(
    days: dict[date, int],
    *,
    now: datetime,
    limit_gb: int,
    counts: str,
    reset_day: int,
    clients: int,
    pause_trials: bool = False,
) -> TrafficOutlook:
    """Forecast the billing period from the panel's bytes per UTC day."""
    factor = COUNT_FACTORS[counts]
    today = now.astimezone(UTC).date()
    start, end = billing_period(today, reset_day)
    elapsed = now.astimezone(UTC) - datetime.combine(today, time(0), tzinfo=UTC)
    fraction = min(1.0, max(0.0, elapsed.total_seconds() / 86400))
    billed = {day: value * factor for day, value in days.items()}

    history = [billed[day] for day in sorted(billed) if day < today][-HISTORY_DAYS:]
    model = fit(history)
    if model is None and today in billed and fraction >= 0.1:
        rate = billed[today] / fraction
        model = Model(rate, 0.0, 0.0, rate, 1, "today")

    observed = sum(value for day, value in billed.items() if start <= day <= today)
    missing = sum(
        1
        for offset in range((today - start).days)
        if start + timedelta(days=offset) not in billed
    )
    limit_bytes = limit_gb * DECIMAL_GB
    recent = history[-30:]
    client_month = (
        int(sum(recent) / len(recent) * 30 / clients) if recent and clients else None
    )
    if model is None:
        return TrafficOutlook(
            today=today,
            period_start=start,
            period_end=end,
            limit_bytes=limit_bytes,
            used_bytes=int(observed),
            expected_bytes=None,
            high_bytes=None,
            daily_bytes=None,
            exhaust_day=None,
            estimated_days=0,
            history_days=0,
            method="none",
            clients=clients,
            client_month_bytes=client_month,
            counts=counts,
            pause_trials=pause_trials,
        )

    used = observed + missing * model.level
    full_days = (end - today).days - 1
    rest_of_today = (1 - fraction) * model.day(1)
    remaining = rest_of_today + sum(model.day(h) for h in range(2, full_days + 2))
    # Daily errors add up; the level's own error grows with the horizon.
    weight = (1 - fraction) + full_days + missing
    weight_sq = (1 - fraction) ** 2 + full_days + missing
    spread = model.sigma * math.sqrt(weight_sq + weight**2 / max(1, model.days))

    exhaust: date | None = None
    total = used + rest_of_today
    if used < limit_bytes:
        if total >= limit_bytes:
            exhaust = today
        else:
            for h in range(2, full_days + 2):
                total += model.day(h)
                if total >= limit_bytes:
                    exhaust = today + timedelta(days=h - 1)
                    break
    return TrafficOutlook(
        today=today,
        period_start=start,
        period_end=end,
        limit_bytes=limit_bytes,
        used_bytes=int(used),
        expected_bytes=int(used + remaining),
        high_bytes=int(used + remaining + Z90 * spread),
        daily_bytes=int(model.day(1)),
        exhaust_day=exhaust,
        estimated_days=missing,
        history_days=model.days if model.method != "today" else 0,
        method=model.method,
        clients=clients,
        client_month_bytes=client_month,
        counts=counts,
        pause_trials=pause_trials,
    )


# ---------------------------------------------------------------- texts
def fmt_bytes(value: int) -> str:
    if value >= 10**12:
        return f"{value / 10**12:.2f} ТБ".replace(".", ",")
    if value >= 10 * DECIMAL_GB:
        return f"{value / DECIMAL_GB:.0f} ГБ"
    return f"{value / DECIMAL_GB:.1f} ГБ".replace(".", ",")


def fmt_day(day: date) -> str:
    return f"{day.day} {MONTHS[day.month - 1]}"


def fmt_share(share: float | None) -> str:
    return "—" if share is None else f"{share * 100:.0f}%"


def last_day(outlook: TrafficOutlook) -> date:
    return outlook.period_end - timedelta(days=1)


def alerts(outlook: TrafficOutlook) -> list[tuple[str, str]]:
    """(level, text) of every warning this outlook calls for."""
    found: list[tuple[str, str]] = []
    used = fmt_bytes(outlook.used_bytes)
    limit = fmt_bytes(outlook.limit_bytes)
    share = fmt_share(outlook.used_share)
    if outlook.used_share >= 1:
        found.append(
            (
                "used_100",
                f"🚨 Трафик сервера: по оценке бота месячный лимит исчерпан — "
                f"{used} из {limit}. Проверьте панель хостинга: при превышении "
                "провайдер может снизить скорость или выставить счёт. /traffic",
            )
        )
    elif outlook.near_limit:
        trials = (
            "Новые пробные доступы приостановлены до "
            f"{fmt_day(outlook.period_end)}; платные подписки работают как обычно."
            if outlook.pause_trials
            else "Пробные доступы выдаются как обычно: автопауза выключена "
            "(SERVER_TRAFFIC_PAUSE_TRIALS)."
        )
        found.append(
            (
                "used_90",
                f"⚠️ Трафик сервера: {share} месячного лимита ({used} из {limit}). "
                f"{trials} /traffic",
            )
        )
    elif outlook.used_share >= 0.8:
        found.append(
            (
                "used_80",
                f"📶 Трафик сервера: {share} месячного лимита ({used} из {limit}). "
                "/traffic",
            )
        )
    expected = outlook.expected_share
    if expected is not None and outlook.history_days >= MIN_FORECAST_ALERT_DAYS:
        forecast = (
            f"≈{fmt_bytes(outlook.expected_bytes or 0)} из {limit} "
            f"({fmt_share(expected)}) к {fmt_day(last_day(outlook))}"
        )
        if expected >= 1:
            runs_out = (
                f" Лимит закончится ≈{fmt_day(outlook.exhaust_day)}."
                if outlook.exhaust_day
                else ""
            )
            found.append(
                (
                    "forecast_100",
                    f"📈 Прогноз превышения лимита трафика: {forecast}.{runs_out} "
                    "Варианты: тариф хостинга с бо́льшим трафиком, ещё один сервер "
                    "или пауза в продажах. /traffic",
                )
            )
        elif expected >= 0.9:
            found.append(
                (
                    "forecast_90",
                    f"📈 Прогноз трафика сервера: {forecast}. /traffic",
                )
            )
    return found


METHOD_NAMES: dict[str, str] = {
    "holt": "сглаживание с затухающим трендом, параметры подобраны по истории",
    "mean": "среднее по дням (истории меньше недели)",
    "today": "по сегодняшнему темпу (полных дней ещё нет)",
    "none": "нет данных",
}


def report(outlook: TrafficOutlook) -> str:
    """The owner's /traffic answer."""
    days_left = (outlook.period_end - outlook.today).days
    lines = [
        f"📶 <b>Трафик сервера</b> с {fmt_day(outlook.period_start)} по "
        f"{fmt_day(last_day(outlook))} (осталось дней: {max(0, days_left)})",
        f"Израсходовано: {fmt_bytes(outlook.used_bytes)} из "
        f"{fmt_bytes(outlook.limit_bytes)} ({fmt_share(outlook.used_share)})",
    ]
    if outlook.estimated_days:
        lines.append(
            f"Дней без данных в этом периоде: {outlook.estimated_days}; "
            "их расход оценён моделью."
        )
    if outlook.expected_bytes is None:
        lines.append(
            "Прогноз: пока мало данных, он появится через несколько часов работы бота."
        )
    else:
        lines.append(
            f"Прогноз на период: ≈{fmt_bytes(outlook.expected_bytes)} "
            f"({fmt_share(outlook.expected_share)}); с вероятностью 90% не больше "
            f"{fmt_bytes(outlook.high_bytes or 0)} "
            f"({fmt_share(outlook.share(outlook.high_bytes))})."
        )
        if outlook.daily_bytes is not None:
            lines.append(f"Темп: ≈{fmt_bytes(outlook.daily_bytes)} в сутки.")
        if outlook.exhaust_day is not None:
            lines.append(
                f"При таком темпе лимит закончится ≈{fmt_day(outlook.exhaust_day)}."
            )
    capacity = outlook.capacity_clients
    if outlook.client_month_bytes and capacity is not None:
        lines.append(
            f"Клиент в среднем: {fmt_bytes(outlook.client_month_bytes)} за 30 дней. "
            f"Активных клиентов: {outlook.clients}; лимит сервера рассчитан "
            f"примерно на {capacity}."
        )
    if outlook.trials_paused:
        trials = "приостановлен (90% лимита)."
    elif outlook.pause_trials:
        trials = "открыт; при 90% лимита приостановится."
    else:
        trials = "открыт (автопауза выключена)."
    lines.append(f"Пробный доступ: {trials}")
    lines.append(
        f"Модель: {METHOD_NAMES[outlook.method]}; полных дней в истории: "
        f"{outlook.history_days}."
    )
    lines.append(
        f"Учёт провайдера: {COUNT_NAMES[outlook.counts]} трафик "
        f"(SERVER_TRAFFIC_COUNTS={outlook.counts}). Это оценка по трафику "
        "клиентов: раз в месяц сверяйте её с панелью хостинга."
    )
    return "\n".join(lines)


class ServerTrafficWatch:
    """Keeps the current outlook, warns the owner and gates new trials."""

    def __init__(self, db: Database, notifier: Notifier, settings: Settings) -> None:
        self.db = db
        self.notifier = notifier
        self.settings = settings
        self.outlook: TrafficOutlook | None = None
        self._seeded = False

    def trials_paused(self) -> bool:
        return self.outlook is not None and self.outlook.trials_paused

    async def compute(self, now: datetime | None = None) -> TrafficOutlook:
        now = now or datetime.now(UTC)
        today = now.astimezone(UTC).date()
        if not self._seeded:
            # Days before this feature: the per-user table keeps about a week.
            await self.db.backfill_server_traffic(today.isoformat())
            self._seeded = True
        first = today - timedelta(days=HISTORY_DAYS + 31)
        rows = await self.db.server_traffic_since(first.isoformat())
        self.outlook = build_outlook(
            {date.fromisoformat(day): value for day, value in rows},
            now=now,
            limit_gb=self.settings.server_traffic_limit_gb,
            counts=self.settings.server_traffic_counts,
            reset_day=self.settings.server_traffic_reset_day,
            clients=await self.db.count_active_users(),
            pause_trials=self.settings.server_traffic_pause_trials,
        )
        return self.outlook

    async def refresh(self, now: datetime | None = None) -> TrafficOutlook:
        """Hourly job: a new outlook and the warnings it calls for."""
        outlook = await self.compute(now)
        for level, text in alerts(outlook):
            if not await self.db.claim_server_traffic_alert(outlook.period, level):
                continue
            if not await self.notifier.telegram(self.settings.admin_telegram_id, text):
                await self.db.release_server_traffic_alert(outlook.period, level)
        return outlook

    async def purge(self, now: datetime | None = None) -> None:
        today = (now or datetime.now(UTC)).astimezone(UTC).date()
        cutoff = (today - timedelta(days=RETENTION_DAYS)).isoformat()
        await self.db.purge_server_traffic(cutoff)
        await self.db.purge_server_traffic_alerts(cutoff)
