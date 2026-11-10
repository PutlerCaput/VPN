"""Static client instructions. Access credentials never appear in download URLs.

Which apps are offered, and why only these (docs/CONNECT_APPS.md): free, open
source, updated lately, offered on the developers' own pages, and, for the App
Store, present in the Russian storefront when last checked. Apps that fail a
check are removed from here, from the website and from the Mini App together.
"""

from __future__ import annotations

from dataclasses import dataclass

# The developers' own pages. A wrong link in a security product could send a
# buyer to a look-alike (a search for "Foxray" in the Russian App Store finds an
# unrelated app), so App Store links name the app by its id, and
# tests/test_connect_screen.py pins every link to a short list of official hosts
# and keeps the bot, the site and the Mini App on the same pages.
V2RAYNG_URL = "https://github.com/2dust/v2rayNG/releases"
# The project's README says the Google Play build has been controlled by a third
# party since May 2024: GitHub is the only page offered.
NEKOBOX_URL = "https://github.com/MatsuriDayo/NekoBoxForAndroid/releases"
V2RAYN_URL = "https://github.com/2dust/v2rayN/releases"
KARING_URL = "https://apps.apple.com/app/id6472431552"
ONEXRAY_URL = "https://apps.apple.com/app/id6745748773"


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
    # Russian sites past the server, with the app's own routing feature.
    direct: str = ""


@dataclass(frozen=True, slots=True)
class DeviceGuide:
    title: str
    apps: tuple[ClientApp, ...]


# Step 2 of every list is the link the bot sends in the same message. Menu names
# follow the apps' own wording; they differ a little between versions.
APPLE_STORE_NOTE = (
    "ℹ️ Apple время от времени убирает такие приложения из российского App "
    "Store. Если приложения нет в вашем магазине, напишите в поддержку бота."
)
# Russian sites straight through the customer's own provider, the rest through
# the server. Only the app on the device can do this: traffic that has reached
# the server leaves it with the server's foreign address. Each app's own feature
# and wording (docs/CONNECT_APPS.md, "Российские сайты напрямую"); no step needs
# a file from us. The words "обход", "VPN" and "proxy" stay out: the same steps
# are on the website, whose wording is checked in CI.
DIRECT_INTRO = (
    "Банки, Госуслуги, маркетплейсы и другие российские сайты будут открываться "
    "напрямую, через вашего обычного провайдера, а остальные — через сервер. "
    "Так российские сайты работают быстрее, а те, что не пускают зарубежные "
    "адреса, открываются как обычно. Шифрование сервиса на прямые соединения "
    "не действует."
)
DIRECT_CHECK = (
    "Проверка: на странице yandex.ru/internet должен быть ваш обычный адрес, "
    "а на ifconfig.me — адрес сервера."
)
# Apps that receive the rule with the subscription once scripts/ru_direct.sh
# has turned it on (RU_DIRECT_SUBSCRIPTION): Marzban sends v2rayNG full Xray
# configs with it. The others receive server links only and keep their steps.
AUTO_DIRECT_APPS = frozenset({"v2rayng"})
AUTO_DIRECT_NOTE = (
    "🇷🇺 Российские сайты v2rayNG открывает напрямую сам: правила приходят "
    "вместе с подпиской, настраивать ничего не нужно."
)
AUTO_DIRECT_STEPS = (
    "1. Ничего настраивать не нужно: правила приходят вместе с подпиской.\n"
    "2. Если подписку добавили раньше, обновите её: на главном экране ⋮ → "
    "«Обновить подписку» (Update subscription), затем выберите сервер заново.\n"
    "3. Чтобы и российские сайты шли через сервер, используйте NekoBox: в нём "
    "по умолчанию весь трафик идёт через сервер."
)
V2RAYNG_DIRECT = (
    "1. В v2rayNG откройте меню ☰ → «Маршрутизация» (Routing Settings).\n"
    "2. Нажмите ⋮ вверху справа → «Импорт набора правил» (Import predefined "
    "rulesets) → «Белый список России» и подтвердите замену прежних правил.\n"
    "3. Откройте ☰ → «Настройки» и в поле «DNS для прямых подключений» впишите "
    "77.88.8.8 (DNS Яндекса): адреса российских сайтов будут определяться в "
    "России.\n"
    "4. Выключите и снова включите подключение.\n"
    "5. Вернуть всё через сервер: там же «Импорт набора правил» → «Общие» "
    "(Global)."
)
NEKOBOX_DIRECT = (
    "1. В NekoBox откройте меню ☰ → «Маршруты» (Route) → «Создать маршрут» "
    "(значок «+»).\n"
    "2. В поле «Название маршрута» введите «Россия», в поле domain — "
    "geosite:category-ru, в поле dst ip — geoip:ru.\n"
    "3. В поле outbound выберите второй пункт списка: прямое соединение, мимо "
    "сервера.\n"
    "4. Сохраните маршрут (✓ вверху), проверьте, что он включён в списке, и "
    "переподключитесь.\n"
    "5. Вернуть всё через сервер: выключите этот маршрут в списке «Маршруты»."
)
KARING_DIRECT = (
    "1. В Karing откройте «Настройки» → «Разделение» (Diversion) → «Страна и "
    "регион» (Country and Region) и выберите Россию.\n"
    "2. Вернитесь в «Разделение» → «Правила разделения» (Diversion rules): у "
    "правил geosite:ru и geoip:ru выберите действие «Прямое подключение» "
    "(Direct).\n"
    "3. Отключитесь и подключитесь снова.\n"
    "4. Вернуть всё через сервер: у тех же правил выберите действие «Текущий "
    "выбор» (Current Selected)."
)
ONEXRAY_DIRECT = (
    "1. В OneXray откройте «Routing» → «Smart Routing» → «Edit».\n"
    "2. В пункте «Direct region» выберите Russia (по умолчанию там Китай). "
    "«Local networks» и «Direct DNS» оставьте включёнными; в «Local DNS "
    "address» можно заменить 8.8.8.8 на 77.88.8.8 (DNS Яндекса).\n"
    "3. Нажмите «Save» и подтвердите переподключение. Если в «Routing» выбран "
    "другой режим, выберите «Smart Routing».\n"
    "4. Вернуть всё через сервер: в «Routing» вместо «Smart Routing» выберите "
    "режим, в котором весь трафик идёт через сервер."
)
V2RAYN_DIRECT = (
    "1. В v2rayN откройте «Настройки» (Settings) → «Настройка региональных "
    "пресетов» (Regional presets setting) → «Россия» (Russia). Программа "
    "скачает российские правила через уже работающее подключение и включит DNS "
    "Яндекса для российских сайтов.\n"
    "2. В нижней панели, в списке маршрутизации (Routing), выберите «Всё, кроме "
    "РФ».\n"
    "3. Вернуть всё через сервер: в том же списке выберите «Всё»."
)

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
    direct=V2RAYNG_DIRECT,
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
    direct=NEKOBOX_DIRECT,
)
KARING = ClientApp(
    "karing",
    "Karing",
    KARING_URL,
    "1. Установите Karing из App Store кнопкой ниже.\n"
    "2. Скопируйте ссылку выше.\n"
    "3. В Karing нажмите значок настроек вверху слева → «Добавить профиль» "
    "(Add profile) → «Добавить ссылку на профиль» (Add profile link). Вставьте "
    "ссылку и нажмите кнопку добавления вверху справа.\n"
    "4. На главном экране сервер выбирается сам; другой можно выбрать по "
    "названию под переключателем.\n"
    "5. Включите переключатель подключения и разрешите добавление "
    "VPN-конфигурации в окне iOS.",
    APPLE_STORE_NOTE,
    direct=KARING_DIRECT,
)
ONEXRAY_IOS = ClientApp(
    "onexray",
    "OneXray",
    ONEXRAY_URL,
    "1. Установите OneXray из App Store кнопкой ниже.\n"
    "2. Скопируйте ссылку выше.\n"
    "3. Откройте OneXray и завершите первоначальную настройку до экрана «Go to "
    "Home». Когда iOS спросит, разрешите добавление VPN-конфигурации.\n"
    "4. Откройте «Servers» → «Add servers» → «Add subscription»: введите любое "
    "имя, вставьте ссылку в поле URL и подтвердите.\n"
    "5. На экране «Connect» выберите источник серверов (для начала подойдёт "
    "автоматический выбор) и нажмите «Connect»: дождитесь статуса «Connected».",
    APPLE_STORE_NOTE,
    direct=ONEXRAY_DIRECT,
)
ONEXRAY_MAC = ClientApp(
    "onexray",
    "OneXray",
    ONEXRAY_URL,
    "1. Установите OneXray из Mac App Store кнопкой ниже.\n"
    "2. Скопируйте ссылку выше.\n"
    "3. Откройте OneXray и завершите первоначальную настройку до экрана «Go to "
    "Home». Когда macOS спросит, разрешите добавление VPN-конфигурации.\n"
    "4. Откройте «Servers» → «Add servers» → «Add subscription»: введите любое "
    "имя, вставьте ссылку в поле URL и подтвердите.\n"
    "5. На экране «Connect» выберите источник серверов (для начала подойдёт "
    "автоматический выбор) и нажмите «Connect»: дождитесь статуса «Connected».",
    APPLE_STORE_NOTE,
    direct=ONEXRAY_DIRECT,
)
# v2rayN has the same menus on Windows, macOS and Linux; steps 3-5 are shared.
V2RAYN_USE = (
    "3. В меню «Подписки» (Subscription group) откройте «Настройки подписки» и "
    "нажмите «Добавить»: введите любое имя, вставьте ссылку в поле URL и "
    "подтвердите.\n"
    "4. В том же меню выберите «Обновить подписки» (Update subscriptions): "
    "появятся серверы.\n"
    "5. Выберите сервер, нажмите Enter и включите в нижней панели системный "
    "прокси или режим TUN (для TUN нужны права администратора)."
)
V2RAYN_WINDOWS = ClientApp(
    "v2rayn",
    "v2rayN",
    V2RAYN_URL,
    "1. Скачайте v2rayN для Windows кнопкой ниже (раздел Releases на GitHub): "
    "для обычного 64-разрядного компьютера — v2rayN-windows-64.zip. Распакуйте "
    "архив и запустите программу.\n"
    "2. Скопируйте ссылку выше.\n" + V2RAYN_USE,
    direct=V2RAYN_DIRECT,
)
V2RAYN_MAC = ClientApp(
    "v2rayn",
    "v2rayN",
    V2RAYN_URL,
    "1. Скачайте v2rayN для macOS кнопкой ниже (раздел Releases на GitHub): "
    "v2rayN-macos-arm64.dmg для Apple Silicon или v2rayN-macos-64.dmg для Intel. "
    "Установите его; если macOS пишет, что приложение повреждено, выполните в "
    "Терминале команду из инструкции разработчиков: "
    "xattr -cr /Applications/v2rayN.app\n"
    "2. Скопируйте ссылку выше.\n" + V2RAYN_USE,
    "ℹ️ Сборка для macOS не подписана, поэтому система предупреждает при "
    "запуске: скачивайте её только по кнопке ниже.",
    direct=V2RAYN_DIRECT,
)
V2RAYN_LINUX = ClientApp(
    "v2rayn",
    "v2rayN",
    V2RAYN_URL,
    "1. Скачайте v2rayN для Linux кнопкой ниже (раздел Releases на GitHub) и "
    "установите: пакет .deb (Debian 12+, Ubuntu 22.04+) — sudo apt install -y "
    "./v2rayN-linux-64.deb; пакет .rpm (Fedora 36+, RHEL 9+) — sudo dnf install "
    "-y ./v2rayN-linux-rhel-x64.rpm; либо распакуйте архив .zip и запустите "
    "./v2rayN. Имена файлов зависят от версии.\n"
    "2. Скопируйте ссылку выше.\n" + V2RAYN_USE,
    direct=V2RAYN_DIRECT,
)

GUIDES: dict[str, DeviceGuide] = {
    "android": DeviceGuide("Android", (V2RAYNG, NEKOBOX)),
    "ios": DeviceGuide("iPhone / iPad", (KARING, ONEXRAY_IOS)),
    "windows": DeviceGuide("Windows", (V2RAYN_WINDOWS,)),
    "macos": DeviceGuide("macOS", (ONEXRAY_MAC, V2RAYN_MAC)),
    "linux": DeviceGuide("Linux", (V2RAYN_LINUX,)),
}

# For a link that is one server, not a subscription (CONFIG_DELIVERY=single).
SINGLE_LINK_STEPS = (
    "Это ссылка на один сервер, а не подписка. Установите приложение кнопкой "
    "ниже, скопируйте ссылку и добавьте её из буфера обмена: в приложениях для "
    "телефона кнопка «+» → импорт из буфера обмена (Import from clipboard), в "
    "программах для компьютера — Ctrl+V. Затем выберите сервер и подключитесь."
)


def direct_is_automatic(app: ClientApp, enabled: bool) -> bool:
    """True when this app gets the "Russian sites directly" rule by itself."""
    return enabled and app.key in AUTO_DIRECT_APPS


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
