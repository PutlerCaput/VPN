// Which apps to suggest for the visitor's device, where to get them and how to add
// the subscription link in each. The link is copied when an app is chosen and the
// steps stay short on purpose. Only apps that are free, open source, updated
// lately and offered on the developers' own pages are here, and the App Store ones
// were present in the Russian storefront when last checked (docs/CONNECT_APPS.md).
// The install links are pinned by tests/test_connect_screen.py so a typo cannot
// send a buyer of a security product to a look-alike; the same test keeps them
// equal to the bot's (services/connection_guide.py) and to the Mini App's.

export type Platform = "android" | "ios" | "windows" | "macos" | "linux";

export const PLATFORMS: Platform[] = ["android", "ios", "windows", "macos", "linux"];

export const PLATFORM_LABEL: Record<Platform, string> = {
  android: "Android",
  ios: "iPhone или iPad",
  windows: "Windows",
  macos: "macOS",
  linux: "Linux",
};

export interface ClientApp {
  key: string;
  name: string;
  /** The developer's own download page. */
  install: string;
  /** Step 2 is "copy the link": it sits above the steps as a button. */
  steps: string[];
  note?: string;
  /** Russian sites straight through the visitor's provider, in this app's own settings. */
  direct: string[];
}

/** For a link that is one server, not a subscription. */
export const SINGLE_LINK_STEPS =
  "Это ссылка на один сервер, а не подписка. Установите приложение, скопируйте ссылку и добавьте её из буфера обмена: в приложениях для телефона кнопка «+» → импорт из буфера обмена (Import from clipboard), в программах для компьютера — Ctrl+V. Затем выберите сервер и подключитесь.";

const APPLE_STORE_NOTE =
  "ℹ️ Apple время от времени убирает такие приложения из российского App Store. Если приложения нет в вашем магазине, напишите в поддержку бота.";

// v2rayN has the same menus on Windows, macOS and Linux; these are steps 3-5.
const V2RAYN_USE = [
  "В меню «Подписки» (Subscription group) откройте «Настройки подписки» и нажмите «Добавить»: введите любое имя, вставьте ссылку в поле URL и подтвердите.",
  "В том же меню выберите «Обновить подписки» (Update subscriptions): появятся серверы.",
  "Выберите сервер, нажмите Enter и включите в нижней панели системный прокси или режим TUN (для TUN нужны права администратора).",
];

const ONEXRAY_USE = [
  "Откройте «Servers» → «Add servers» → «Add subscription»: введите любое имя, вставьте ссылку в поле URL и подтвердите.",
  "На экране «Connect» выберите источник серверов (для начала подойдёт автоматический выбор) и нажмите «Connect»: дождитесь статуса «Connected».",
];

// Russian sites past the server. Only the app on the device can do this: traffic
// that reached the server leaves it with the server's foreign address. The same
// steps as the bot's (services/connection_guide.py), checked by
// tests/test_connect_screen.py.
export const DIRECT_INTRO =
  "Банки, Госуслуги, маркетплейсы и другие российские сайты будут открываться напрямую, через вашего обычного провайдера, а остальные — через сервер. Так российские сайты работают быстрее, а те, что не пускают зарубежные адреса, открываются как обычно. Шифрование сервиса на прямые соединения не действует.";

export const DIRECT_CHECK =
  "Проверка: на странице yandex.ru/internet должен быть ваш обычный адрес, а на ifconfig.me — адрес сервера.";

// Apps that receive the rule with the subscription once the server sends it
// (ru_direct_auto from /config): Hiddify Manager sends v2rayNG full configs with it.
export const AUTO_DIRECT_APPS = ["v2rayng"];

export const AUTO_DIRECT_NOTE =
  "🇷🇺 Российские сайты v2rayNG открывает напрямую сам: правила приходят вместе с подпиской, настраивать ничего не нужно.";

export const AUTO_DIRECT_STEPS = [
  "Ничего настраивать не нужно: правила приходят вместе с подпиской.",
  "Если подписку добавили раньше, обновите её: на главном экране ⋮ → «Обновить подписку» (Update subscription), затем выберите сервер заново.",
  "Чтобы и российские сайты шли через сервер, используйте NekoBox: в нём по умолчанию весь трафик идёт через сервер.",
];

const V2RAYNG_DIRECT = [
  "В v2rayNG откройте меню ☰ → «Маршрутизация» (Routing Settings).",
  "Нажмите ⋮ вверху справа → «Импорт набора правил» (Import predefined rulesets) → «Белый список России» и подтвердите замену прежних правил.",
  "Откройте ☰ → «Настройки» и в поле «DNS для прямых подключений» впишите 77.88.8.8 (DNS Яндекса): адреса российских сайтов будут определяться в России.",
  "Выключите и снова включите подключение.",
  "Вернуть всё через сервер: там же «Импорт набора правил» → «Общие» (Global).",
];

const NEKOBOX_DIRECT = [
  "В NekoBox откройте меню ☰ → «Маршруты» (Route) → «Создать маршрут» (значок «+»).",
  "В поле «Название маршрута» введите «Россия», в поле domain — geosite:category-ru, в поле dst ip — geoip:ru.",
  "В поле outbound выберите второй пункт списка: прямое соединение, мимо сервера.",
  "Сохраните маршрут (✓ вверху), проверьте, что он включён в списке, и переподключитесь.",
  "Вернуть всё через сервер: выключите этот маршрут в списке «Маршруты».",
];

const KARING_DIRECT = [
  "В Karing откройте «Настройки» → «Разделение» (Diversion) → «Страна и регион» (Country and Region) и выберите Россию.",
  "Вернитесь в «Разделение» → «Правила разделения» (Diversion rules): у правил geosite:ru и geoip:ru выберите действие «Прямое подключение» (Direct).",
  "Отключитесь и подключитесь снова.",
  "Вернуть всё через сервер: у тех же правил выберите действие «Текущий выбор» (Current Selected).",
];

const ONEXRAY_DIRECT = [
  "В OneXray откройте «Routing» → «Smart Routing» → «Edit».",
  "В пункте «Direct region» выберите Russia (по умолчанию там Китай). «Local networks» и «Direct DNS» оставьте включёнными; в «Local DNS address» можно заменить 8.8.8.8 на 77.88.8.8 (DNS Яндекса).",
  "Нажмите «Save» и подтвердите переподключение. Если в «Routing» выбран другой режим, выберите «Smart Routing».",
  "Вернуть всё через сервер: в «Routing» вместо «Smart Routing» выберите режим, в котором весь трафик идёт через сервер.",
];

const V2RAYN_DIRECT = [
  "В v2rayN откройте «Настройки» (Settings) → «Настройка региональных пресетов» (Regional presets setting) → «Россия» (Russia). Программа скачает российские правила через уже работающее подключение и включит DNS Яндекса для российских сайтов.",
  "В нижней панели, в списке маршрутизации (Routing), выберите «Всё, кроме РФ».",
  "Вернуть всё через сервер: в том же списке выберите «Всё».",
];

export const APPS: Record<Platform, ClientApp[]> = {
  android: [
    {
      key: "v2rayng",
      name: "v2rayNG",
      install: "https://github.com/2dust/v2rayNG/releases",
      steps: [
        "Установите v2rayNG кнопкой ниже: это официальная страница на GitHub.",
        "Скопируйте ссылку кнопкой выше.",
        "В v2rayNG откройте меню ☰ → «Группа подписки» (Subscription group setting) → «+». Введите любое имя, вставьте ссылку в поле URL и сохраните.",
        "На главном экране нажмите ⋮ → «Обновить подписку» (Update subscription): появятся серверы.",
        "Выберите сервер, нажмите кнопку подключения и разрешите создание соединения в системном окне Android.",
      ],
      direct: V2RAYNG_DIRECT,
    },
    {
      key: "nekobox",
      name: "NekoBox",
      install: "https://github.com/MatsuriDayo/NekoBoxForAndroid/releases",
      steps: [
        "Установите NekoBox кнопкой ниже, только с GitHub (раздел Releases).",
        "Скопируйте ссылку кнопкой выше.",
        "Откройте меню ☰ → «Группы» (Groups) → «+». Тип группы — «Подписка» (Subscription), в поле ссылки вставьте ссылку, включите автообновление и сохраните.",
        "Дождитесь обновления: в группе появятся серверы. Выберите сервер.",
        "Нажмите кнопку подключения и разрешите создание соединения в системном окне Android.",
      ],
      note: "⚠️ Не ставьте NekoBox из Google Play: по словам авторов, эту версию с мая 2024 года контролирует третья сторона.",
      direct: NEKOBOX_DIRECT,
    },
  ],
  ios: [
    {
      key: "karing",
      name: "Karing",
      install: "https://apps.apple.com/app/id6472431552",
      steps: [
        "Установите Karing из App Store кнопкой ниже.",
        "Скопируйте ссылку кнопкой выше.",
        "В Karing нажмите значок настроек вверху слева → «Добавить профиль» (Add profile) → «Добавить ссылку на профиль» (Add profile link). Вставьте ссылку и нажмите кнопку добавления вверху справа.",
        "На главном экране сервер выбирается сам; другой можно выбрать по названию под переключателем.",
        "Включите переключатель подключения и разрешите добавление конфигурации в системном окне iOS.",
      ],
      note: APPLE_STORE_NOTE,
      direct: KARING_DIRECT,
    },
    {
      key: "onexray",
      name: "OneXray",
      install: "https://apps.apple.com/app/id6745748773",
      steps: [
        "Установите OneXray из App Store кнопкой ниже.",
        "Скопируйте ссылку кнопкой выше.",
        "Откройте OneXray и завершите первоначальную настройку до экрана «Go to Home». Когда iOS спросит, разрешите добавление конфигурации.",
        ...ONEXRAY_USE,
      ],
      note: APPLE_STORE_NOTE,
      direct: ONEXRAY_DIRECT,
    },
  ],
  windows: [
    {
      key: "v2rayn",
      name: "v2rayN",
      install: "https://github.com/2dust/v2rayN/releases",
      steps: [
        "Скачайте v2rayN для Windows кнопкой ниже (раздел Releases на GitHub): для обычного 64-разрядного компьютера — v2rayN-windows-64.zip. Распакуйте архив и запустите программу.",
        "Скопируйте ссылку кнопкой выше.",
        ...V2RAYN_USE,
      ],
      note: "Системный прокси работает для браузеров и программ, которые используют настройки прокси Windows. Для игр и голосовых звонков выберите TUN. Если после сбоя пропал интернет, перезапустите v2rayN или отключите оставленный им прокси в настройках Windows.",
      direct: V2RAYN_DIRECT,
    },
  ],
  macos: [
    {
      key: "onexray",
      name: "OneXray",
      install: "https://apps.apple.com/app/id6745748773",
      steps: [
        "Установите OneXray из Mac App Store кнопкой ниже.",
        "Скопируйте ссылку кнопкой выше.",
        "Откройте OneXray и завершите первоначальную настройку до экрана «Go to Home». Когда macOS спросит, разрешите добавление конфигурации.",
        ...ONEXRAY_USE,
      ],
      note: APPLE_STORE_NOTE,
      direct: ONEXRAY_DIRECT,
    },
    {
      key: "v2rayn",
      name: "v2rayN",
      install: "https://github.com/2dust/v2rayN/releases",
      steps: [
        "Скачайте v2rayN для macOS кнопкой ниже (раздел Releases на GitHub): v2rayN-macos-arm64.dmg для Apple Silicon или v2rayN-macos-64.dmg для Intel. Установите его; если macOS пишет, что приложение повреждено, выполните в Терминале команду из инструкции разработчиков: xattr -cr /Applications/v2rayN.app",
        "Скопируйте ссылку кнопкой выше.",
        ...V2RAYN_USE,
      ],
      note: "ℹ️ Сборка для macOS не подписана, поэтому система предупреждает при запуске: скачивайте её только по кнопке ниже.",
      direct: V2RAYN_DIRECT,
    },
  ],
  linux: [
    {
      key: "v2rayn",
      name: "v2rayN",
      install: "https://github.com/2dust/v2rayN/releases",
      steps: [
        "Скачайте v2rayN для Linux кнопкой ниже (раздел Releases на GitHub) и установите: пакет .deb (Debian 12+, Ubuntu 22.04+) — sudo apt install -y ./v2rayN-linux-64.deb; пакет .rpm (Fedora 36+, RHEL 9+) — sudo dnf install -y ./v2rayN-linux-rhel-x64.rpm; либо распакуйте архив .zip и запустите ./v2rayN. Имена файлов зависят от версии.",
        "Скопируйте ссылку кнопкой выше.",
        ...V2RAYN_USE,
      ],
      direct: V2RAYN_DIRECT,
    },
  ],
};

/** iPadOS reports a desktop Safari user agent, so touch points tell it apart. */
export function detectPlatform(
  ua: string = navigator.userAgent,
  maxTouchPoints: number = navigator.maxTouchPoints,
): Platform {
  if (/iPhone|iPad|iPod/i.test(ua)) return "ios";
  if (/Macintosh/i.test(ua) && maxTouchPoints > 1) return "ios";
  if (/Android/i.test(ua)) return "android"; // its user agent also says Linux
  if (/Macintosh|Mac OS X/i.test(ua)) return "macos";
  if (/Linux|X11/i.test(ua) && !/CrOS/i.test(ua)) return "linux";
  // Windows, and the default for anything unknown: the chips switch it.
  return "windows";
}
