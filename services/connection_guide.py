"""Static client instructions. Access credentials never appear in download URLs."""

from __future__ import annotations

from dataclasses import dataclass

# The developers' own pages. A wrong link in a security product could send a
# buyer to a look-alike, so tests/test_connect_screen.py pins every one of them
# to a short list of official hosts and keeps the site and the Mini App on the
# same pages.
V2RAYNG_URL = "https://github.com/2dust/v2rayNG/releases"
# The project's README says the Google Play build has been controlled by a third
# party since May 2024: GitHub is the only page offered.
NEKOBOX_URL = "https://github.com/MatsuriDayo/NekoBoxForAndroid/releases"
FOXRAY_URL = "https://apps.apple.com/app/id6448898396"
# The free "V2Box - V2ray Client"; the paid "V2Box Pro" is another listing.
V2BOX_URL = "https://apps.apple.com/app/id6446814690"
NEKORAY_URL = "https://github.com/MatsuriDayo/nekoray/releases"
V2RAYN_URL = "https://github.com/2dust/v2rayN/releases"


@dataclass(frozen=True, slots=True)
class ClientGuide:
    title: str
    client: str
    download_url: str
    steps: str


@dataclass(frozen=True, slots=True)
class ClientApp:
    """One client app on one device: where to get it and how to add the link."""

    key: str
    name: str
    download_url: str
    steps: str
    note: str = ""


@dataclass(frozen=True, slots=True)
class DeviceGuide:
    title: str
    apps: tuple[ClientApp, ...]


# Step 2 of every list is the link the bot sends in the same message. Menu names
# follow the apps' own wording; they differ a little between versions.
V2RAYNG = ClientApp(
    "v2rayng",
    "v2rayNG",
    V2RAYNG_URL,
    "1. Установите v2rayNG кнопкой ниже: это официальная страница на GitHub.\n"
    "2. Скопируйте ссылку выше.\n"
    "3. В v2rayNG откройте меню ☰ → «Группа подписки» (Subscription group "
    "setting) → «+». Введите любое имя, вставьте ссылку в поле URL и сохраните.\n"
    "4. На главном экране нажмите ⋮ → «Обновить подписку» (Update subscription): "
    "появятся серверы.\n"
    "5. Выберите сервер, нажмите кнопку подключения и разрешите создание "
    "VPN-соединения в окне Android.",
)
NEKOBOX = ClientApp(
    "nekobox",
    "NekoBox",
    NEKOBOX_URL,
    "1. Установите NekoBox кнопкой ниже, только с GitHub (раздел Releases).\n"
    "2. Скопируйте ссылку выше.\n"
    "3. Откройте меню ☰ → «Группы» (Groups) → «+». Тип группы — «Подписка» "
    "(Subscription), в поле ссылки вставьте ссылку, включите автообновление и "
    "сохраните.\n"
    "4. Дождитесь обновления: в группе появятся серверы. Выберите сервер.\n"
    "5. Нажмите кнопку подключения и разрешите создание VPN-соединения в окне "
    "Android.",
    "⚠️ Не ставьте NekoBox из Google Play: по словам авторов, эту версию с мая "
    "2024 года контролирует третья сторона.",
)
FOXRAY = ClientApp(
    "foxray",
    "Foxray",
    FOXRAY_URL,
    "1. Установите Foxray из App Store кнопкой ниже.\n"
    "2. Скопируйте ссылку выше.\n"
    "3. В Foxray внизу откройте «Subscriptions» → «+». Введите любое имя, "
    "вставьте ссылку в поле URL и нажмите «Subscribe».\n"
    "4. Если серверов нет, обновите подписку значком обновления в списке "
    "подписок.\n"
    "5. Выберите сервер, включите подключение и разрешите добавление "
    "VPN-конфигурации в окне iOS.",
)
V2BOX_IOS = ClientApp(
    "v2box",
    "V2Box",
    V2BOX_URL,
    "1. Установите V2Box из App Store кнопкой ниже: нужна бесплатная «V2Box - "
    "V2ray Client».\n"
    "2. Скопируйте ссылку выше.\n"
    "3. Внизу откройте «Configs» → «+» → «Add Subscription». Введите любое имя, "
    "вставьте ссылку в поле URL и нажмите «Add Subscribe»: серверы проверятся "
    "сами.\n"
    "4. На главном экране («Home») выберите сервер и сдвиньте нижнюю кнопку "
    "вправо, чтобы подключиться. Разрешите добавление VPN-конфигурации в окне "
    "iOS.",
)
V2BOX_MAC = ClientApp(
    "v2box",
    "V2Box",
    V2BOX_URL,
    "1. Установите V2Box из Mac App Store кнопкой ниже: нужна бесплатная "
    "«V2Box - V2ray Client».\n"
    "2. Скопируйте ссылку выше.\n"
    "3. Откройте «Configs» → «+» → «Add Subscription». Введите любое имя, "
    "вставьте ссылку в поле URL и нажмите «Add Subscribe»: серверы проверятся "
    "сами.\n"
    "4. На главном экране («Home») выберите сервер и подключитесь. Разрешите "
    "добавление VPN-конфигурации в системном окне macOS.",
)
NEKORAY = ClientApp(
    "nekoray",
    "Nekoray",
    NEKORAY_URL,
    "1. Скачайте Nekoray для Windows кнопкой ниже (раздел Releases), распакуйте "
    "архив и запустите программу.\n"
    "2. Скопируйте ссылку выше.\n"
    "3. В Nekoray нажмите Ctrl+V: программа предложит добавить ссылку как "
    "подписку (создайте новую группу).\n"
    "4. Чтобы загрузить серверы, выберите в меню «Сервер» пункт «Обновить "
    "подписку» (Ctrl+U). Для обновления VPN должен быть выключен.\n"
    "5. Выберите сервер, нажмите Enter и включите «Системный прокси» или «Режим "
    "TUN» (для TUN нужны права администратора).",
    "ℹ️ Автор остановил разработку Nekoray (репозиторий в архиве с марта 2025): "
    "программа работает, но не обновляется. При проблемах попробуйте v2rayN.",
)
V2RAYN = ClientApp(
    "v2rayn",
    "v2rayN",
    V2RAYN_URL,
    "1. Скачайте v2rayN для Windows кнопкой ниже (раздел Releases на GitHub), "
    "распакуйте архив и запустите программу.\n"
    "2. Скопируйте ссылку выше.\n"
    "3. В меню «Подписки» (Subscription group) откройте «Настройки подписки» и "
    "нажмите «Добавить»: введите любое имя, вставьте ссылку в поле URL и "
    "подтвердите.\n"
    "4. В том же меню выберите «Обновить подписки» (Update subscriptions): "
    "появятся серверы.\n"
    "5. Выберите сервер, нажмите Enter и включите в нижней панели системный "
    "прокси или режим TUN (для TUN нужны права администратора).",
)

GUIDES: dict[str, DeviceGuide] = {
    "android": DeviceGuide("Android", (V2RAYNG, NEKOBOX)),
    "ios": DeviceGuide("iPhone / iPad", (FOXRAY, V2BOX_IOS)),
    "windows": DeviceGuide("Windows", (NEKORAY, V2RAYN)),
    "macos": DeviceGuide("macOS", (V2BOX_MAC,)),
}

# For a link that is one server, not a subscription (CONFIG_DELIVERY=single).
SINGLE_LINK_STEPS = (
    "Это ссылка на один сервер, а не подписка. Установите приложение кнопкой "
    "ниже, скопируйте ссылку и добавьте её из буфера обмена: в приложениях для "
    "телефона кнопка «+» → импорт из буфера обмена (Import from clipboard), в "
    "программах для компьютера — Ctrl+V. Затем выберите сервер и подключитесь."
)


def find_app(device_key: str, app_key: str) -> tuple[DeviceGuide, ClientApp] | None:
    """The device and the app a menu button names, or None for a stale button."""
    device = GUIDES.get(device_key)
    if device is None:
        return None
    for app in device.apps:
        if app.key == app_key:
            return device, app
    return None


AMNEZIA_APP_URL = "https://github.com/amnezia-vpn/amnezia-client/releases"

# Shown only when the AmneziaWG server is connected (AWG_ENABLED).
AMNEZIA_GUIDE = ClientGuide(
    "AmneziaVPN (все системы)",
    "AmneziaVPN",
    AMNEZIA_APP_URL,
    "AmneziaWG — второй протокол с маскировкой трафика; у каждого устройства "
    "свой ключ.\n\n"
    "1. Установите AmneziaVPN: для Windows, macOS, Linux и Android — из раздела "
    "Releases, для iPhone — из App Store. Клиент с открытым кодом (GPL-3.0). "
    "Платную подписку разработчика без необходимости не покупайте.\n"
    "2. Нажмите «Получить мою конфигурацию» ниже → «➕ Добавить устройство». "
    "Бот пришлёт файл .conf и QR-код для этого устройства.\n"
    "3. В AmneziaVPN добавьте подключение из файла или отсканируйте QR-код "
    "с другого экрана.\n"
    "4. Подключитесь и проверьте обычный сайт.\n\n"
    "Если уже пользуетесь AmneziaVPN со своим сервером, новое подключение "
    "добавится рядом, старое останется. Раздельное туннелирование (какие "
    "сайты или приложения идут мимо VPN) настраивается в самом приложении. "
    "Файл подходит и для приложений AmneziaWG и роутеров с поддержкой "
    "AmneziaWG.",
)

HELP_TEXT = (
    "🛠 <b>Помощь с подключением</b>\n\n"
    "1. Откройте /status: срок доступа должен действовать. "
    "Активная подписка сама по себе не подтверждает соединение на устройстве.\n"
    "2. Убедитесь, что интернет работает без VPN, "
    "а дата и время устройства выставлены автоматически.\n"
    "3. Обновите профиль в клиенте и переподключитесь. "
    "Не включайте одновременно несколько VPN-клиентов.\n"
    "4. Сравните работу в Wi-Fi и мобильной сети. Если работает только одна "
    "сеть, сообщите это поддержке. Доступность зависит от сети и устройства.\n"
    "5. Если оплата списана, а срок не обновился, не платите повторно. "
    "Передайте поддержке номер заказа и время оплаты; "
    "скриншот не заменяет проверку платежа.\n\n"
    "6. Список серверов и их состояние: /servers. Защита на случай обрыва "
    "соединения — кнопка «🛡 Защита при обрыве» в /connect.\n\n"
    "Для обращения укажите устройство, версию клиента, сеть и текст ошибки. "
    "Закройте на скриншоте ссылку подключения, QR-код и персональные данные. "
    "Пароли и платёжные ключи не присылайте."
)

# Only steps that exist in the systems/clients themselves; no invented menus.
KILL_SWITCH_TEXT = (
    "🛡 <b>Защита при обрыве соединения</b>\n\n"
    "Если VPN-соединение прервётся, приложения могут продолжить работать "
    "напрямую, без шифрования. Чтобы этого не было:\n\n"
    "<b>Android</b> (встроено в систему): Настройки → Сеть и интернет → VPN → "
    "значок ⚙️ рядом с клиентом → включите «Постоянная VPN» и «Блокировать "
    "подключения без VPN». Названия пунктов могут немного отличаться у "
    "производителей. Пока VPN выключен, интернет на телефоне работать не будет — "
    "это и есть защита.\n\n"
    "<b>iPhone / iPad, Windows, macOS, Linux</b>: отдельной системной "
    "блокировки нет. Используйте режим VPN/TUN в клиенте и, если клиент "
    "предлагает автоподключение или блокировку трафика без VPN, включите их. "
    "Проверить: выключите VPN — обычные сайты должны открываться только "
    "после повторного включения, если блокировка поддерживается.\n\n"
    "Сервис не отключает защиту сам и не управляет вашим устройством удалённо."
)
