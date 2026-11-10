// Which apps to suggest for the visitor's device and where to get them. Inside
// Telegram the Mini App knows the platform exactly (WebApp.platform), so it is
// trusted first and the user agent is only a fallback. The steps for each app are
// in i18n/ru.ts. Only apps that are free, open source, updated lately and offered
// on the developers' own pages are here, and the App Store ones were present in
// the Russian storefront when last checked (docs/CONNECT_APPS.md). The install
// links are pinned by tests/test_connect_screen.py so a typo cannot send a buyer
// of a security product to a look-alike; the same test keeps them equal to the
// bot's (services/connection_guide.py) and to the website's.
import { getWebApp } from "./telegram";

export type Platform = "android" | "ios" | "windows" | "macos" | "linux";

export const PLATFORMS: Platform[] = ["android", "ios", "windows", "macos", "linux"];

export interface ClientApp {
  key: string;
  name: string;
  /** The developer's own download page. */
  install: string;
}

// Apps that receive "Russian sites directly" with the subscription once the
// server sends it (ru_direct_auto from /config); texts in i18n/ru.ts.
export const AUTO_DIRECT_APPS = ["v2rayng"];

export const APPS: Record<Platform, ClientApp[]> = {
  android: [
    { key: "v2rayng", name: "v2rayNG", install: "https://github.com/2dust/v2rayNG/releases" },
    { key: "nekobox", name: "NekoBox", install: "https://github.com/MatsuriDayo/NekoBoxForAndroid/releases" },
  ],
  ios: [
    { key: "karing", name: "Karing", install: "https://apps.apple.com/app/id6472431552" },
    { key: "onexray", name: "OneXray", install: "https://apps.apple.com/app/id6745748773" },
  ],
  windows: [{ key: "v2rayn", name: "v2rayN", install: "https://github.com/2dust/v2rayN/releases" }],
  macos: [
    { key: "onexray", name: "OneXray", install: "https://apps.apple.com/app/id6745748773" },
    { key: "v2rayn", name: "v2rayN", install: "https://github.com/2dust/v2rayN/releases" },
  ],
  linux: [{ key: "v2rayn", name: "v2rayN", install: "https://github.com/2dust/v2rayN/releases" }],
};

function fromUserAgent(): Platform {
  const ua = navigator.userAgent;
  if (/iPhone|iPad|iPod/i.test(ua)) return "ios";
  if (/Macintosh/i.test(ua) && navigator.maxTouchPoints > 1) return "ios";
  if (/Android/i.test(ua)) return "android"; // its user agent also says Linux
  if (/Macintosh|Mac OS X/i.test(ua)) return "macos";
  if (/Linux|X11/i.test(ua) && !/CrOS/i.test(ua)) return "linux";
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
