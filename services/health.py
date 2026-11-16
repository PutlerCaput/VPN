"""Service health: which locations serve traffic, with outage alerts.

Large VPN services publish a server list with live status and react to an
outage before users write in. Here the source is the Hiddify Manager panel:

* ``/api/v2/panel/info`` and ``/api/v2/panel/ping`` — the panel itself
  (the main location). Additional locations are configured in the panel.

A location must fail ``failures_to_alert`` consecutive checks before the owner
is alerted, so a single slow answer does not page anyone; recovery is reported
once. Nothing about users or their traffic is read or stored.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol

from services.hiddify import HiddifyError, HiddifyForbidden
from services.metrics import LOCATION_UP
from services.security import html_escape

logger = logging.getLogger(__name__)

PANEL_KEY = "__panel__"
CHECK_TIMEOUT_SECONDS = 20.0
FRESH_SECONDS = 5 * 60


class HealthSource(Protocol):
    async def get_core(self) -> dict[str, Any]: ...

    async def get_nodes(self) -> list[dict[str, Any]]: ...


class AdminNotifier(Protocol):
    async def telegram(self, telegram_id: int, text: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class Location:
    name: str
    online: bool


@dataclass(frozen=True, slots=True)
class HealthReport:
    checked_at: int
    panel_ok: bool
    locations: tuple[Location, ...]

    @property
    def all_online(self) -> bool:
        return self.panel_ok and all(item.online for item in self.locations)


class ServiceHealth:
    def __init__(
        self,
        source: HealthSource,
        notifier: AdminNotifier,
        admin_telegram_id: int,
        main_location_name: str,
        *,
        failures_to_alert: int = 2,
    ) -> None:
        self.source = source
        self.notifier = notifier
        self.admin_telegram_id = admin_telegram_id
        self.main_location_name = main_location_name
        self.failures_to_alert = max(1, failures_to_alert)
        self.last: HealthReport | None = None
        self._streaks: dict[str, int] = {}
        self._alerted: set[str] = set()
        self._forbidden_logged: set[str] = set()

    async def _read(self) -> HealthReport:
        locations: list[Location] = []
        try:
            core = await asyncio.wait_for(
                self.source.get_core(), timeout=CHECK_TIMEOUT_SECONDS
            )
            locations.append(
                Location(self.main_location_name, core.get("started") is True)
            )
        except HiddifyForbidden:
            # The panel answered: it is up, but this account cannot see the core.
            self._warn_forbidden("core")
        except (HiddifyError, TimeoutError):
            return HealthReport(int(time.time()), False, ())
        try:
            nodes = await asyncio.wait_for(
                self.source.get_nodes(), timeout=CHECK_TIMEOUT_SECONDS
            )
        except HiddifyForbidden:
            self._warn_forbidden("nodes")
            nodes = []
        except (HiddifyError, TimeoutError):
            return HealthReport(int(time.time()), False, tuple(locations))
        for node in nodes:
            status = node.get("status")
            name = node.get("name")
            if status == "disabled" or not isinstance(name, str) or not name:
                continue  # switched off on purpose by the owner
            locations.append(Location(name[:64], status == "connected"))
        return HealthReport(int(time.time()), True, tuple(locations))

    def _warn_forbidden(self, what: str) -> None:
        if what not in self._forbidden_logged:
            self._forbidden_logged.add(what)
            logger.warning("health_forbidden", extra={"resource": what})

    async def check(self) -> HealthReport:
        report = await self._read()
        self.last = report
        return report

    async def current(self) -> HealthReport:
        """A report no older than a few minutes, for user-facing screens."""
        if self.last is not None and time.time() - self.last.checked_at < FRESH_SECONDS:
            return self.last
        return await self.check()

    async def poll(self) -> HealthReport:
        """Scheduled check: update metrics and alert the owner on changes."""
        report = await self.check()
        states: dict[str, bool] = {PANEL_KEY: report.panel_ok}
        if report.panel_ok:
            for location in report.locations:
                states[location.name] = location.online
                LOCATION_UP.labels(location=location.name).set(int(location.online))
        for key, ok in states.items():
            await self._track(key, ok)
        return report

    async def _track(self, key: str, ok: bool) -> None:
        label = "Панель Hiddify" if key == PANEL_KEY else f"Сервер {html_escape(key)}"
        if ok:
            self._streaks[key] = 0
            if key in self._alerted:
                self._alerted.discard(key)
                await self.notifier.telegram(
                    self.admin_telegram_id, f"🟢 {label} снова работает."
                )
            return
        self._streaks[key] = self._streaks.get(key, 0) + 1
        if self._streaks[key] >= self.failures_to_alert and key not in self._alerted:
            self._alerted.add(key)
            logger.warning("location_down", extra={"location": key})
            await self.notifier.telegram(
                self.admin_telegram_id,
                f"🔴 {label} не отвечает уже {self._streaks[key]} проверки подряд. "
                "Пользователи видят предупреждение в /servers и /status.",
            )


def servers_text(report: HealthReport, *, subscription: bool) -> str:
    if not report.panel_ok:
        return (
            "📡 <b>Серверы</b>\n\nСейчас не удалось проверить состояние серверов. "
            "Если подключение не работает, напишите в «💬 Поддержка»."
        )
    if not report.locations:
        return (
            "📡 <b>Серверы</b>\n\nСервис работает, но список серверов сейчас "
            "недоступен. Если подключение не работает, напишите в «💬 Поддержка»."
        )
    lines = ["📡 <b>Серверы</b>", ""]
    for location in report.locations:
        mark = "🟢" if location.online else "🔴"
        state = "работает" if location.online else "недоступен"
        lines.append(f"{mark} {html_escape(location.name)} — {state}")
    checked = time.strftime("%H:%M UTC", time.gmtime(report.checked_at))
    lines.append("")
    lines.append(f"Проверено: {checked}.")
    if subscription and len(report.locations) > 1:
        lines.append(
            "Все серверы уже есть в вашей ссылке-подписке: выберите сервер "
            "в клиенте или включите автовыбор, если клиент его поддерживает."
        )
    elif not subscription:
        lines.append("Ваша ссылка подключения ведёт на основной сервер.")
    return "\n".join(lines)


OUTAGE_NOTICE = (
    "⚠️ Сейчас есть неполадки на сервере. Мы уже знаем о них и восстанавливаем "
    "работу; подробнее — /servers."
)
