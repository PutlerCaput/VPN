// Which apps to suggest for the visitor's device and where to get them. Inside
// Telegram the Mini App knows the platform exactly (WebApp.platform), so it is
// trusted first and the user agent is only a fallback. The steps for each app are
// in i18n/ru.ts. The install links are the developers' own pages, pinned by
// tests/test_connect_screen.py so a typo cannot send a buyer of a security product
// to a look-alike; the same test keeps them equal to the bot's
// (services/connection_guide.py) and to the website's.
import { getWebApp } from "./telegram";

export type Platform = "android" | "ios" | "windows" | "macos";

export const PLATFORMS: Platform[] = ["android", "ios", "windows", "macos"];

export interface ClientApp {
  key: string;
  name: string;
  /** The developer's own download page. */
  install: string;
}

export const APPS: Record<Platform, ClientApp[]> = {
  android: [
    { key: "v2rayng", name: "v2rayNG", install: "https://github.com/2dust/v2rayNG/releases" },
    { key: "nekobox", name: "NekoBox", install: "https://github.com/MatsuriDayo/NekoBoxForAndroid/releases" },
  ],
  ios: [
    { key: "foxray", name: "Foxray", install: "https://apps.apple.com/app/id6448898396" },
    { key: "v2box", name: "V2Box", install: "https://apps.apple.com/app/id6446814690" },
  ],
  windows: [
    { key: "nekoray", name: "Nekoray", install: "https://github.com/MatsuriDayo/nekoray/releases" },
    { key: "v2rayn", name: "v2rayN", install: "https://github.com/2dust/v2rayN/releases" },
  ],
  macos: [{ key: "v2box", name: "V2Box", install: "https://apps.apple.com/app/id6446814690" }],
};

function fromUserAgent(): Platform {
  const ua = navigator.userAgent;
  if (/iPhone|iPad|iPod/i.test(ua)) return "ios";
  if (/Macintosh/i.test(ua) && navigator.maxTouchPoints > 1) return "ios";
  if (/Android/i.test(ua)) return "android";
  if (/Macintosh|Mac OS X/i.test(ua)) return "macos";
  // Windows, and the default for anything unknown: the chips switch it.
  return "windows";
}

export function detectPlatform(): Platform {
  const tg = getWebApp()?.platform;
  if (tg === "ios") return "ios";
  if (tg === "android" || tg === "android_x") return "android";
  if (tg === "macos") return "macos";
  // tdesktop is Windows or Linux, weba and webk are any browser: read the user agent.
  return fromUserAgent();
}
