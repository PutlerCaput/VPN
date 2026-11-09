// Which apps to suggest for the visitor's device, where to get them and how to add
// the subscription link in each. The link is copied when an app is chosen and the
// steps stay short on purpose. The install links are the developers' own pages,
// pinned by tests/test_connect_screen.py so a typo cannot send a buyer of a
// security product to a look-alike; the same test keeps them equal to the bot's
// (services/connection_guide.py) and to the Mini App's.

export type Platform = "android" | "ios" | "windows" | "macos";

export const PLATFORMS: Platform[] = ["android", "ios", "windows", "macos"];

export const PLATFORM_LABEL: Record<Platform, string> = {
  android: "Android",
  ios: "iPhone или iPad",
  windows: "Windows",
  macos: "macOS",
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

const V2BOX_STEPS = [
  "Установите V2Box из App Store кнопкой ниже: нужна бесплатная «V2Box - V2ray Client».",
  "Скопируйте ссылку кнопкой выше.",
  "Внизу откройте «Configs» → «+» → «Add Subscription». Введите любое имя, вставьте ссылку в поле URL и нажмите «Add Subscribe»: серверы проверятся сами.",
  "На главном экране («Home») выберите сервер и сдвиньте нижнюю кнопку вправо, чтобы подключиться. Разрешите добавление конфигурации в системном окне iOS.",
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
      key: "foxray",
      name: "Foxray",
      install: "https://apps.apple.com/app/id6448898396",
      steps: [
        "Установите Foxray из App Store кнопкой ниже.",
        "Скопируйте ссылку кнопкой выше.",
        "В Foxray внизу откройте «Subscriptions» → «+». Введите любое имя, вставьте ссылку в поле URL и нажмите «Subscribe».",
        "Если серверов нет, обновите подписку значком обновления в списке подписок.",
        "Выберите сервер, включите подключение и разрешите добавление конфигурации в системном окне iOS.",
      ],
    },
    {
      key: "v2box",
      name: "V2Box",
      install: "https://apps.apple.com/app/id6446814690",
      steps: V2BOX_STEPS,
    },
  ],
  windows: [
    {
      key: "nekoray",
      name: "Nekoray",
      install: "https://github.com/MatsuriDayo/nekoray/releases",
      steps: [
        "Скачайте Nekoray для Windows кнопкой ниже (раздел Releases), распакуйте архив и запустите программу.",
        "Скопируйте ссылку кнопкой выше.",
        "В Nekoray нажмите Ctrl+V: программа предложит добавить ссылку как подписку (создайте новую группу).",
        "Чтобы загрузить серверы, выберите в меню «Сервер» пункт «Обновить подписку» (Ctrl+U). Для обновления подключение должно быть выключено.",
        "Выберите сервер, нажмите Enter и включите «Системный прокси» или «Режим TUN» (для TUN нужны права администратора).",
      ],
      note: "ℹ️ Автор остановил разработку Nekoray (репозиторий в архиве с марта 2025): программа работает, но не обновляется. При проблемах попробуйте v2rayN.",
    },
    {
      key: "v2rayn",
      name: "v2rayN",
      install: "https://github.com/2dust/v2rayN/releases",
      steps: [
        "Скачайте v2rayN для Windows кнопкой ниже (раздел Releases на GitHub), распакуйте архив и запустите программу.",
        "Скопируйте ссылку кнопкой выше.",
        "В меню «Подписки» (Subscription group) откройте «Настройки подписки» и нажмите «Добавить»: введите любое имя, вставьте ссылку в поле URL и подтвердите.",
        "В том же меню выберите «Обновить подписки» (Update subscriptions): появятся серверы.",
        "Выберите сервер, нажмите Enter и включите в нижней панели системный прокси или режим TUN (для TUN нужны права администратора).",
      ],
    },
  ],
  macos: [
    {
      key: "v2box",
      name: "V2Box",
      install: "https://apps.apple.com/app/id6446814690",
      steps: [
        "Установите V2Box из Mac App Store кнопкой ниже: нужна бесплатная «V2Box - V2ray Client».",
        "Скопируйте ссылку кнопкой выше.",
        "Откройте «Configs» → «+» → «Add Subscription». Введите любое имя, вставьте ссылку в поле URL и нажмите «Add Subscribe»: серверы проверятся сами.",
        "На главном экране («Home») выберите сервер и подключитесь. Разрешите добавление конфигурации в системном окне macOS.",
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
  if (/Android/i.test(ua)) return "android";
  if (/Macintosh|Mac OS X/i.test(ua)) return "macos";
  // Windows, and the default for anything unknown: the chips switch it.
  return "windows";
}
