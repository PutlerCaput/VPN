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
