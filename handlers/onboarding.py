"""User-initiated setup and self-help. No traffic histories or tracking pixels."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from handlers.profile import config_access, deliver_config
from keyboards.main_menu import BTN_SETUP
from keyboards.onboarding import (
    client_apps_kb,
    client_guide_kb,
    direct_kb,
    guide_kb,
    help_kb,
    setup_kb,
)
from services.connection_guide import (
    AMNEZIA_GUIDE,
    AUTO_DIRECT_NOTE,
    AUTO_DIRECT_STEPS,
    DIRECT_CHECK,
    DIRECT_INTRO,
    GUIDES,
    HELP_TEXT,
    KILL_SWITCH_TEXT,
    SINGLE_LINK_STEPS,
    ClientApp,
    DeviceGuide,
    direct_is_automatic,
    find_app,
)
from services.container import Services
from services.security import html_escape

router = Router(name="onboarding")
SETUP_TEXT = (
    "📱 <b>Подключение устройства</b>\n\n"
    "Выберите устройство, затем приложение: бот пришлёт вашу ссылку-подписку "
    "(её можно скопировать одним нажатием) и короткую инструкцию. Для ссылки "
    "нужна активная подписка или пробный доступ после /start.\n\n"
    "Для другой системы подойдёт любое приложение, которое умеет добавлять "
    "подписку по ссылке.\n\n"
    "Российские сайты можно открывать напрямую, без сервера: как это сделать "
    "в выбранном приложении, сказано в его инструкции."
)


def apps_text(device: DeviceGuide) -> str:
    return (
        f"📱 <b>{device.title}</b>\n\n"
        "Выберите приложение: бот пришлёт вашу ссылку-подписку (её можно "
        "скопировать одним нажатием) и короткую инструкцию."
    )


def app_guide_text(
    device: DeviceGuide, app: ClientApp, link: str, auto_direct: bool = False
) -> str:
    """The link and the steps in one message, so the link is at hand for step 2."""
    subscription = link.startswith("https://")
    label = (
        "Ваша ссылка-подписка (нажмите на неё, чтобы скопировать):"
        if subscription
        else "Ваша ссылка для импорта (нажмите на неё, чтобы скопировать):"
    )
    steps = app.steps if subscription else SINGLE_LINK_STEPS
    note = f"\n\n{app.note}" if app.note else ""
    if subscription and direct_is_automatic(app, auto_direct):
        note += f"\n\n{AUTO_DIRECT_NOTE}"
    return (
        f"📱 <b>{device.title} · {app.name}</b>\n\n"
        f"{label}\n<code>{html_escape(link)}</code>\n\n"
        f"{steps}{note}\n\n"
        "Ссылку и QR-код никому не передавайте: они привязаны к вашему аккаунту. "
        "Названия пунктов в приложении могут немного отличаться. Если страница "
        "установки не открывается, нажмите «Не получается» и напишите в поддержку."
    )


def direct_text(device: DeviceGuide, app: ClientApp, auto_direct: bool = False) -> str:
    """How to send Russian sites past the server in this very app."""
    steps = AUTO_DIRECT_STEPS if direct_is_automatic(app, auto_direct) else app.direct
    return (
        f"🇷🇺 <b>Российские сайты напрямую · {app.name}</b>\n\n"
        f"{DIRECT_INTRO}\n\n"
        f"{steps}\n\n"
        f"{DIRECT_CHECK}\n\n"
        f"Это настройка приложения на устройстве ({device.title}), сервис её не "
        "видит и не меняет. Названия пунктов могут немного отличаться между "
        "версиями приложения."
    )


def app_blocked_text(device: DeviceGuide, app: ClientApp, problem: str) -> str:
    return (
        f"📱 <b>{device.title} · {app.name}</b>\n\n{problem}\n\n"
        "Приложение можно установить уже сейчас кнопкой ниже: ссылка и "
        "инструкция придут сюда, как только доступ будет активен."
    )


async def send_app_guide(
    message: Message,
    owner_id: int,
    services: Services,
    device_key: str,
    device: DeviceGuide,
    app: ClientApp,
) -> None:
    access = await config_access(owner_id, services)
    auto = services.settings.ru_direct_subscription
    if access.link is None:
        await message.answer(
            app_blocked_text(device, app, access.problem or ""),
            reply_markup=client_guide_kb(
                device_key, app, None, direct_is_automatic(app, auto)
            ),
        )
        return
    subscription = access.link.startswith("https://")
    await message.answer(
        app_guide_text(device, app, access.link, auto),
        reply_markup=client_guide_kb(
            device_key,
            app,
            access.link,
            subscription and direct_is_automatic(app, auto),
        ),
    )


@router.message(F.text == BTN_SETUP)
@router.message(Command("connect"))
async def start_setup(
    message: Message, state: FSMContext, services: Services | None = None
) -> None:
    await state.clear()
    awg = services is not None and services.awg is not None
    await message.answer(SETUP_TEXT, reply_markup=setup_kb(awg))


@router.message(Command("help"))
async def show_help(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(HELP_TEXT, reply_markup=help_kb())


@router.callback_query(F.data.startswith("setup:"))
async def setup_action(
    callback: CallbackQuery, state: FSMContext, services: Services
) -> None:
    message = callback.message
    if (
        not isinstance(message, Message)
        or message.chat.type != "private"
        or message.chat.id != callback.from_user.id
    ):
        await callback.answer("Откройте личный чат с ботом.")
        return
    action = callback.data or ""
    if action.startswith("setup:guide:"):
        key = action.removeprefix("setup:guide:")
        if key == "amnezia" and services.awg is not None:
            await callback.answer()
            await state.clear()
            await message.answer(
                f"📱 <b>{AMNEZIA_GUIDE.title} · {AMNEZIA_GUIDE.client}</b>\n\n"
                f"{AMNEZIA_GUIDE.steps}\n\n"
                "На одном устройстве удобнее импорт по ссылке. "
                "QR-код можно сканировать с другого экрана. "
                "Ссылку и QR-код никому не передавайте.",
                reply_markup=guide_kb(key),
            )
            return
        device = GUIDES.get(key)
        if device is None:
            await callback.answer("Выберите устройство заново: /connect.")
            return
        await callback.answer()
        await state.clear()
        if len(device.apps) == 1:
            await send_app_guide(
                message, callback.from_user.id, services, key, device, device.apps[0]
            )
        else:
            await message.answer(apps_text(device), reply_markup=client_apps_kb(key))
    elif action.startswith("setup:app:"):
        parts = action.split(":")
        found = find_app(parts[2], parts[3]) if len(parts) == 4 else None
        if found is None:
            await callback.answer("Выберите устройство заново: /connect.")
            return
        await callback.answer()
        await state.clear()
        await send_app_guide(
            message, callback.from_user.id, services, parts[2], found[0], found[1]
        )
    elif action.startswith("setup:direct:"):
        parts = action.split(":")
        found = find_app(parts[2], parts[3]) if len(parts) == 4 else None
        if found is None:
            await callback.answer("Выберите устройство заново: /connect.")
            return
        await callback.answer()
        await state.clear()
        await message.answer(
            direct_text(found[0], found[1], services.settings.ru_direct_subscription),
            reply_markup=direct_kb(parts[2], found[1]),
        )
    elif action == "setup:choose":
        await callback.answer()
        await start_setup(message, state, services)
    elif action == "setup:killswitch":
        await callback.answer()
        await message.answer(KILL_SWITCH_TEXT, reply_markup=help_kb())
    elif action == "setup:help":
        await callback.answer()
        await show_help(message, state)
    elif action == "setup:config":
        await callback.answer()
        await state.clear()
        await deliver_config(message, callback.from_user.id, services)
    else:
        await callback.answer("Инструкция обновлена. Нажмите /connect.")
