// Which client app to suggest first for the visitor's device, and where to get
// it. Inside Telegram the Mini App knows the platform exactly (WebApp.platform),
// so it is trusted first and the user agent is only a fallback. Every app imports
// a subscription link; the scheme deep links live only in connect.ts and the
// checked bouncer /connect.html. The install links are the developers' own pages,
// pinned by tests/test_connect_screen.py so a typo cannot send a buyer of a
// security product to a look-alike.
import type { ConnectApp } from "./connect";
import { getWebApp } from "./telegram";

export type Platform = "ios" | "android" | "desktop";

interface AppInfo {
  name: string;
  icon: string;
  /** Platforms where this app is a first choice. */
  best: Platform[];
  /** Every platform it runs on. */
  runs: Platform[];
  /** The developer's own download page, per platform. */
  install: Partial<Record<Platform, string>>;
}

const HAPP = "https://happ.su";
const HIDDIFY = "https://github.com/hiddify/hiddify-app/releases";

export const APP_INFO: Record<ConnectApp, AppInfo> = {
  happ: {
    name: "Happ",
    icon: "📲",
    best: ["ios", "android"],
    runs: ["ios", "android", "desktop"],
    install: { ios: HAPP, android: HAPP, desktop: HAPP },
  },
  v2raytun: {
    name: "v2rayTUN",
    icon: "📲",
    best: ["android"],
    runs: ["ios", "android"],
    install: {
      ios: "https://apps.apple.com/app/id6476628951",
      android: "https://play.google.com/store/apps/details?id=com.v2raytun.android",
    },
  },
  hiddify: {
    name: "Hiddify",
    icon: "💻",
    best: ["desktop"],
    runs: ["ios", "android", "desktop"],
    install: { ios: HIDDIFY, android: HIDDIFY, desktop: HIDDIFY },
  },
};

export function detectPlatform(): Platform {
  const tg = getWebApp()?.platform;
  if (tg === "ios") return "ios";
  if (tg === "android" || tg === "android_x") return "android";
  if (tg && tg !== "unknown") return "desktop"; // macos, tdesktop, weba, webk, …
  // Outside Telegram or an unknown client: read the user agent instead.
  const ua = navigator.userAgent;
  if (/iPhone|iPad|iPod/i.test(ua)) return "ios";
  if (/Macintosh/i.test(ua) && navigator.maxTouchPoints > 1) return "ios";
  if (/Android/i.test(ua)) return "android";
  return "desktop";
}

export interface AppChoice {
  app: ConnectApp;
  name: string;
  icon: string;
  recommended: boolean;
  install: string | null;
}

/** The apps that run on this platform, the recommended ones first. */
export function appsForPlatform(platform: Platform): AppChoice[] {
  return (Object.keys(APP_INFO) as ConnectApp[])
    .filter((app) => APP_INFO[app].runs.includes(platform))
    .map((app) => ({
      app,
      name: APP_INFO[app].name,
      icon: APP_INFO[app].icon,
      recommended: APP_INFO[app].best.includes(platform),
      install: APP_INFO[app].install[platform] ?? null,
    }))
    .sort((a, b) => Number(b.recommended) - Number(a.recommended));
}
